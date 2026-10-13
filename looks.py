#!/usr/bin/env python3
"""Looks: one image vector per picture (data/image_vectors.db), for "Looks like this"
and search by an example picture.

An image embedding model (config.json "image_embedding", see SETTINGS.md:
for instance EmbeddingGemma 2 with its image projector, served by
llama.cpp's llama-server) turns what a picture looks like into a vector.
"Looks like this" (look:<id>) ranks the other pictures by how close their
vectors are (cosine), the way "More like this" (similar:<id>, by meaning,
embeddings.py) does with the vectors of the descriptions; it needs no model
at all, so it also works in a static export (the export's "looks" option).
On the server, the owner can drop a picture on the search page: it is
turned into a vector the same way (ExampleEmbedder, never kept) and the
pictures that look like it are found.

What the model sees of a picture (picture_jpeg): its stored preview (or,
without one, its original) made upright and at most image_embedding.max_edge
pixels on its long edge (images.prepare_for_looks); for a 360-degree photo
the front view its thumbnail shows, not the whole panorama; for a video its
poster frame (a 360 video's front view, as its thumbnail shows it). Hidden
pictures are embedded too: an image endpoint must be on this computer or
the local network (there is no allow_cloud for pictures), and their vectors
only go to the admin's pages.

data/image_vectors.db is a database of its own. Deleting it is harmless but
costly: making the vectors again takes hours on a CPU (it is a GPU job). So
only a run that writes it (connect(create=True)) ever empties a file of an
older layout (LOOKS_VERSION); one of a newer version is left alone.

    meta           key, value: version, epoch (random, new for every file),
                   generation (raised by every change, for ETags), recipe
                   (see recipe()), dims, native_dims (the model's own vector
                   length), served (the model's name as its server reports
                   it), probe (the vectors of the probe pictures at the
                   model's own length, JSON: the reference later runs are
                   checked against), probe_key (the model files it was made
                   with, see probe_key()), probe_note (when and where it was
                   made), mismatch (why the last check failed, '' if it did
                   not)
    image_vectors  content_hash, kind, vec, updated

A vector belongs to a picture's content (its content hash) and the kind of
picture the model saw (KIND_PHOTO: an ordinary picture, KIND_PANO: the front
view of a 360 photo, KIND_POSTER: the poster of a video), so copies share
one vector and a moved or renamed picture keeps its own. vec holds signed
bytes, as embeddings.py's: the model's vector cut to its first dims numbers
(Matryoshka models keep their meaning when cut), L2-normalised, times 127
and rounded.

The recipe is the SHA-1 of the model file's and the image projector's
SHA-256, dims and the way pictures are prepared (max_edge, PREPARE_VERSION).
Another recipe makes the stored vectors out of date for search by an
example picture (they no longer match what the model makes now), but they
still match each other, so "Looks like this" keeps working with them; since
making them again costs a long GPU run, they are only thrown away when the
owner asks for it (Rebuild looks on the Manage page, scan.py --embed-images
--rebuild-looks: update(rebuild=True)). A rebuild only throws away vectors
of other settings (or, after a failed probe check, see below, the vectors
and the reference): a rebuild run that was stopped and goes on, or a second
one for another library, keeps what the first made. A change of the text
model (search by meaning) never touches them.

Before the first batch of every run, three probe pictures drawn in code
(PROBE_NAMES: shapes and gradients, no photos) are embedded and compared with
the reference vectors kept in meta, made by the first run with these model
files (probe_key: a rebuild for other dims or picture sizes keeps it, as the
probe pictures do not change with them): a GPU server with flash attention
on, or running in float16, makes image vectors that differ (llama.cpp issues
29629, 29251), and the run stops with a message that says so instead of
filling the store with them (meta's mismatch says why, and the Manage page
offers Rebuild looks: for when the reference itself was made by such a
server). Make the first run on a CPU server (or with flash attention off) to
have a trusted reference; image_embedding.probe_check false turns the check
off.

update() (the "image_embed" job, scan.py --embed-images) embeds the
pictures without a vector, in batches, each batch committed (a stopped run
keeps what it did); it is not run after scans by itself, as it needs the
image model (a GPU job): the Manage page shows how many pictures wait.
Vectors whose content no index row has any more are dropped (purge, also
right after rows are deleted: forget_unreferenced): rows of a library that
is offline stay in the index, so theirs stay too.

This module uses only the standard library; images.py (Pillow) is
imported when pictures are prepared.
"""

import base64
import collections
import concurrent.futures
import gzip
import hashlib
import json
import math
import os
import secrets
import sqlite3
import struct
import threading
import time

import browser_model
import common
import db
import embeddings
import imageinfo
import llm
import videos

# Raise when the layout of image_vectors.db changes: an older file is then
# emptied by the next run that writes it (its vectors must be made again).
LOOKS_VERSION = 1
# Raise when the pictures are prepared differently for the model
# (images.prepare_for_looks, picture_jpeg): the recipe changes with it.
PREPARE_VERSION = 1
# The kinds of picture a vector is made of.
KIND_PHOTO = "photo"
KIND_PANO = "360-front"
KIND_POSTER = "video-poster"
DEFAULTS = {
    "endpoint": "",
    "dims": 256,
    "max_edge": 1024,
    "batch": 8,
    # The model file and image projector the endpoint runs, for the recipe
    # and the served-name check ('' = those of the model server config.json's
    # local_server runs for the endpoint).
    "model_file": "",
    "mmproj_file": "",
    "probe_check": True,
}
MAX_DIMS = 8192
MIN_DIMS = 16
MAX_BATCH = 64
MIN_EDGE = 320
MAX_EDGE = 2048
# A probe picture this close (cosine) to its reference is made the same way.
PROBE_SAME = 0.99
# The probe pictures (drawn by _probe_bitmap): their names, in order.
PROBE_NAMES = ("gradient", "disc", "stripes")
PROBE_SIZE = (320, 240)
# Pictures prepared at the same time while the model works on a batch.
PREPARE_THREADS = 4
# Failed pictures named in the log of one run at most.
FAILURE_LOG_LIMIT = 5
ID_CHUNK = 500
# Search by an example picture: the largest picture taken (bytes), how long
# the model may take for it, and per client QUERY_BURST at once, refilled
# at QUERY_RATE a second.
QUERY_MAX_BYTES = 20 * 1024 * 1024
QUERY_TIMEOUT = 120
QUERY_BURST = 6
QUERY_RATE = 0.5
# How long a run waits for a folder to answer before leaving its pictures
# without a preview for later.
FOLDER_CHECK_TIMEOUT = 5.0

NOT_CONFIGURED = ("Looks are off: set \"image_embedding\": {\"endpoint\": \"NAME\"} in "
                  "config.json to an endpoint on this computer that serves an image "
                  "embedding model (see SETTINGS.md).")
PROBE_MISMATCH = ("The image model makes other vectors of the probe pictures than the "
                  "reference ones (cosine %.3f, at least %.2f is needed), so nothing was "
                  "embedded. A GPU server with flash attention on, or one that runs the model "
                  "in float16, does this: start llama-server with -fa off (and without "
                  "float16), then update the looks again. If the server is right and the "
                  "reference was not (it was %s), or another model answers the endpoint now "
                  "on purpose, Rebuild looks on the Manage page (scan.py --embed-images "
                  "--rebuild-looks) throws away the reference and the kept looks and makes "
                  "them again.")
# Rebuild looks after a failed probe check: what goes.
REBUILD_MISMATCH = ("Rebuilding looks after a failed probe check: the reference and %d kept "
                    "vector(s) were thrown away.")


class StoreError(sqlite3.DatabaseError):
    """data/image_vectors.db cannot be written (made by a newer version of this program)."""

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS image_vectors (content_hash TEXT NOT NULL, kind TEXT NOT NULL, "
    "vec BLOB NOT NULL, updated REAL NOT NULL, PRIMARY KEY (content_hash, kind))",
)


# ---------------------------------------------------------------------------
# Settings

def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def settings(config):
    """config.json's "image_embedding" settings, every one filled in and checked (DEFAULTS)."""
    raw = config.get("image_embedding") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULTS)
    result["endpoint"] = _text(raw.get("endpoint"))
    dims = raw.get("dims")
    if _whole(dims) and (dims == 0 or MIN_DIMS <= dims <= MAX_DIMS):
        result["dims"] = dims
    edge = raw.get("max_edge")
    if _whole(edge) and MIN_EDGE <= edge <= MAX_EDGE:
        result["max_edge"] = edge
    batch = raw.get("batch")
    if _whole(batch) and 1 <= batch <= MAX_BATCH:
        result["batch"] = batch
    for key in ("model_file", "mmproj_file"):
        result[key] = common.unquote_path(raw.get(key))
    result["probe_check"] = raw.get("probe_check") is not False
    return result


def configured(config):
    """True when config.json names an image embedding endpoint (usable or not)."""
    return bool(settings(config)["endpoint"])


def endpoint_for(config, options=None):
    """(the image embedding endpoint, '') or (None, why it cannot be used)."""
    options = options or settings(config)
    name = options["endpoint"]
    if not name:
        return None, NOT_CONFIGURED
    endpoint = common.get_endpoint(config, name)
    if endpoint is None:
        return None, ("There is no endpoint called %s (image_embedding.endpoint in "
                      "config.json): add it on the Settings page." % name)
    if not endpoint["enabled"]:
        return None, "The image embedding endpoint %s is turned off." % endpoint["name"]
    if endpoint["kind"] != "openai":
        return None, ("The endpoint %s cannot embed pictures: looks need an OpenAI-compatible "
                      "endpoint of a llama.cpp server (llama-server --embeddings --mmproj)."
                      % endpoint["name"])
    if not common.is_local_endpoint(endpoint):
        return None, ("The image embedding endpoint %s is not on this computer or the local "
                      "network: pictures are only sent to an image model here."
                      % endpoint["name"])
    return endpoint, ""


def _file(path):
    return common.resolve_config_relative(path) if path else ""


def model_files(config, options=None, endpoint_name=None):
    """(model file, image projector file, why they cannot be told now or ''): the
    settings' model_file and mmproj_file, else those of the embeddings model server
    config.json's local_server runs for the endpoint (its "mmproj"), else ''."""
    options = options or settings(config)
    model, mmproj = _file(options["model_file"]), _file(options["mmproj_file"])
    if model and mmproj:
        return model, mmproj, ""
    import local_server
    server = local_server.find_server(config, endpoint_name or options["endpoint"])
    if server is None or not local_server.embeddings_server(server):
        return model, mmproj, ""
    try:
        return (model or local_server.model_path(server),
                mmproj or local_server.mmproj_path(server), "")
    except local_server.ServerError as error:
        return model, mmproj, str(error)


def prepare_recipe(options):
    """How pictures are prepared for the model, as text (part of the recipe)."""
    return "prepare-%d edge-%d" % (PREPARE_VERSION, options["max_edge"])


def _digests(config, options):
    """The SHA-256 of the model file and of the image projector ('' for one that is not
    known). Raises browser_model.ModelError (see recipe)."""
    model, mmproj, problem = model_files(config, options)
    if problem:
        raise browser_model.ModelError(problem)
    digests = []
    for path, setting in ((model, "image_embedding.model_file, or the model of its model "
                                  "server"),
                          (mmproj, "image_embedding.mmproj_file, or the mmproj of its model "
                                   "server")):
        digest = browser_model.file_sha256(path) if path else ""
        if path and not digest:
            raise browser_model.ModelError("The file %s (%s) cannot be read." % (path, setting))
        digests.append(digest)
    return digests


def recipe(config, options=None):
    """What the vectors are made with: the SHA-1 of the model file's and the image
    projector's SHA-256 ('' for one that is not known), dims and prepare_recipe().

    Raises browser_model.ModelError when a file the vectors are made with
    is known but cannot be read now (a drive that is not there): the stored
    vectors are then neither known to match nor known not to.
    """
    options = options or settings(config)
    digests = _digests(config, options)
    text = "%s\n%s\n%d\n%s" % (digests[0], digests[1], options["dims"], prepare_recipe(options))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def probe_key(config, options=None):
    """What the probe pictures' vectors depend on: the SHA-1 of the model file's and the
    image projector's SHA-256 and PREPARE_VERSION (not dims or max_edge: the probe
    pictures are kept at the model's own length, and are smaller than any max_edge).
    Raises browser_model.ModelError (see recipe)."""
    options = options or settings(config)
    digests = _digests(config, options)
    text = "probe\n%s\n%s\n%d" % (digests[0], digests[1], PREPARE_VERSION)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def recipe_problem(config, options=None):
    """Why the recipe cannot be told now (see recipe), or ''."""
    try:
        recipe(config, options)
    except browser_model.ModelError as error:
        return str(error)
    return ""


def look_id(recipe_value):
    """The opaque id pages know a recipe's vectors by ("l-" and 12 hex digits)."""
    return "l-" + (recipe_value or "0" * 40)[:12]


# ---------------------------------------------------------------------------
# Pictures

def kind_of(row):
    """The kind of picture the model sees of an index row (KIND_PHOTO, KIND_PANO or
    KIND_POSTER)."""
    keys = row.keys() if hasattr(row, "keys") else row
    if "media" in keys and row["media"] == videos.MEDIA_VIDEO:
        return KIND_POSTER
    if "projection" in keys and row["projection"] in imageinfo.PROJECTIONS_360:
        return KIND_PANO
    return KIND_PHOTO


_ROW_COLUMNS = "id, library_id, path, content_hash, media, projection, has_preview, hidden"


def library_rows(conn, library_ids):
    """The present pictures of some libraries whose content hash is known, in id order."""
    library_ids = list(library_ids)
    if not library_ids:
        return []
    return conn.execute("SELECT %s FROM images WHERE missing_since IS NULL AND "
                        "content_hash != '' AND library_id IN (%s) ORDER BY id"
                        % (_ROW_COLUMNS, ",".join("?" * len(library_ids))),
                        library_ids).fetchall()


def picture_jpeg(row, options, previews, config=None, helper=None, root=None):
    """The JPEG bytes the model is sent for an index row (see the module notes).

    previews is a cache.PreviewStore; root the library's folder (for a
    picture without a stored preview). Raises images.ImageReadError (or
    videos.VideoReadError, OSError) when the picture cannot be read.
    """
    import images
    kind = kind_of(row)
    data = None
    if row["has_preview"] == 1:
        data = previews.read(row["library_id"], row["id"])
    if data is None:
        if not root:
            raise images.ImageReadError("there is no preview and the library's folder is "
                                        "not known")
        full = os.path.join(root, *row["path"].split("/"))
        if kind == KIND_POSTER:
            data = videos.poster_jpeg(videos.find_tools(config), full)
        elif kind == KIND_PANO:
            # An unstitched original is stitched first, as its preview is.
            data = images.prepare_for_llm(images.read_source_bytes(full, helper),
                                          max_edge=images.PREVIEW_EDGE,
                                          projection=row["projection"])
        else:
            data = images.read_source_bytes(full, helper)
    # (A 360 video's poster is a stitched panorama: its front view is seen,
    # as its thumbnail shows it.)
    panorama = kind == KIND_PANO or (kind == KIND_POSTER and "projection" in row.keys()
                                     and row["projection"] in imageinfo.PROJECTIONS_360)
    return images.prepare_for_looks(data, options["max_edge"], panorama=panorama)


def _bmp(width, height, pixel):
    """A 24-bit BMP of width x height whose colour at (x, y) is pixel(x, y)."""
    row_bytes = (width * 3 + 3) & ~3
    body = bytearray()
    for y in range(height - 1, -1, -1):
        line = bytearray()
        for x in range(width):
            red, green, blue = pixel(x, y)
            line += bytes((blue, green, red))
        line += b"\0" * (row_bytes - len(line))
        body += line
    header = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return header + info + bytes(body)


def _probe_bitmap(name):
    """One of the probe pictures (PROBE_NAMES): simple shapes and gradients, drawn here."""
    width, height = PROBE_SIZE

    def clamp(value):
        return max(0, min(255, int(value)))
    if name == "gradient":
        def pixel(x, y):
            return (clamp(40 + 200 * x / width), clamp(90 + 120 * y / height),
                    clamp(230 - 180 * x / width))
    elif name == "disc":
        def pixel(x, y):
            if (x - 160) ** 2 + (y - 110) ** 2 < 70 ** 2:
                return (210, 40, 30)
            if y > 190:
                return (40, 110, 50)
            return (250, 235, 180)
    else:
        def pixel(x, y):
            if 200 <= x < 280 and 40 <= y < 120:
                return (30, 70, 200)
            return (20, 20, 20) if ((x + y) // 20) % 2 else (240, 240, 240)
    return _bmp(width, height, pixel)


def probe_pictures(options):
    """[(name, JPEG bytes)] of the probe pictures, prepared as pictures are."""
    import images
    return [(name, images.prepare_for_looks(_probe_bitmap(name), options["max_edge"]))
            for name in PROBE_NAMES]


def image_input(jpeg):
    """A picture as an item of an embeddings request (llama.cpp's content parts)."""
    return {"content": [{"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}}]}


# ---------------------------------------------------------------------------
# The store

def store_path():
    """data/image_vectors.db."""
    return os.path.join(common.data_dir(), "image_vectors.db")


def _meta_rows(conn):
    return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}


def _stored_version(conn):
    """The LOOKS_VERSION an open store was written with: 0 for a new file (or one
    without it), None when it cannot be told (not a number), else the number."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
                    ).fetchone() is None:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def connect(path=None, create=True):
    """Open data/image_vectors.db (made when missing, unless create is False: then None).

    Only with create (the runs that write the store) is a new file set up,
    or one of an older layout (LOOKS_VERSION) emptied first; StoreError for
    one of a newer version (or one whose version cannot be told), which is
    left as it is. Without create, such a file gives None (no looks), and
    nothing is written.
    """
    path = path or store_path()
    if not create and not os.path.isfile(path):
        return None
    conn = db.connect(path, check_same_thread=False)
    try:
        version = _stored_version(conn)
        if version == LOOKS_VERSION:
            return conn
        if not create:
            conn.close()
            return None
        if version is None or version > LOOKS_VERSION:
            raise StoreError("%s was written by a newer version of Search Images (or is "
                             "damaged): it is left as it is. Use that version, or delete the "
                             "file to make the looks again." % path)
        for statement in _SCHEMA:
            conn.execute(statement)
        with conn:
            conn.execute("DELETE FROM image_vectors")
            conn.execute("DELETE FROM meta")
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", (
                ("version", str(LOOKS_VERSION)), ("epoch", secrets.token_hex(8)),
                ("generation", "0"), ("recipe", ""), ("dims", "0"), ("native_dims", "0"),
                ("served", ""), ("probe", ""), ("probe_key", ""), ("probe_note", ""),
                ("mismatch", "")))
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def meta(conn):
    """{version, epoch, generation, recipe, dims, native_dims, served, probe: [{"name",
    "vec" (base64)}], probe_key, probe_note, mismatch, count (vectors stored)} of an
    open store."""
    rows = _meta_rows(conn)

    def number(key):
        try:
            return int(rows.get(key) or 0)
        except ValueError:
            return 0
    try:
        probe = json.loads(rows.get("probe") or "[]")
    except ValueError:
        probe = []
    if not isinstance(probe, list) or not all(isinstance(item, dict) and isinstance(
            item.get("name"), str) and isinstance(item.get("vec"), str) for item in probe):
        probe = []
    count = conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]
    return {"version": number("version"), "epoch": rows.get("epoch") or "",
            "generation": number("generation"), "recipe": rows.get("recipe") or "",
            "dims": number("dims"), "native_dims": number("native_dims"),
            "served": rows.get("served") or "", "probe": probe,
            "probe_key": rows.get("probe_key") or "", "probe_note": rows.get("probe_note") or "",
            "mismatch": rows.get("mismatch") or "", "count": count}


def read_meta():
    """meta() of data/image_vectors.db, or None when there is none (or it cannot be read)."""
    try:
        store = connect(create=False)
    except sqlite3.Error:
        return None
    if store is None:
        return None
    try:
        return meta(store)
    except sqlite3.Error:
        return None
    finally:
        store.close()


_key_lock = threading.Lock()
_key_memo = {"stamp": None, "key": None, "read": 0.0}
# store_key() reads the store again after this many seconds even when its
# files look unchanged (a write-ahead log that was reset and written again
# can keep its size, and a file's time may lag on some systems).
KEY_RECHECK = 2.0


def _file_stamp(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def store_key():
    """A short value that changes whenever the kept vectors do: [epoch, generation,
    dims, native dims] of data/image_vectors.db, or None without one (or one that
    cannot be read).

    For the caches of what is worked out from the looks (the duplicate stacks
    of search_index, the Duplicates page). Cheap: the store is only read
    again when its file or its write-ahead log changed (every write bumps
    the generation, in the same commit), or KEY_RECHECK seconds passed.
    """
    path = store_path()
    stamp = (path, _file_stamp(path), _file_stamp(path + "-wal"))
    now = time.monotonic()
    with _key_lock:
        if _key_memo["stamp"] == stamp and now - _key_memo["read"] < KEY_RECHECK:
            return _key_memo["key"]
    key = None
    if stamp[1] is not None:
        try:
            store = connect(path, create=False)
        except sqlite3.Error:
            store = None
        if store is not None:
            try:
                rows = _meta_rows(store)
                key = [rows.get("epoch") or "", rows.get("generation") or "0",
                       rows.get("dims") or "0", rows.get("native_dims") or "0"]
            except sqlite3.Error:
                key = None
            finally:
                store.close()
    with _key_lock:
        _key_memo.update(stamp=stamp, key=key, read=now)
    return key


def _set_meta(conn, values):
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     [(key, str(value)) for key, value in values.items()])


def _bump(conn):
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")


def reset(conn, recipe_value, dims, keep_probe=False):
    """Empty the store for vectors of this recipe. The probe reference (with what is
    known of the model) goes too, unless keep_probe. Returns how many vectors were
    dropped."""
    values = {"recipe": recipe_value, "dims": dims, "mismatch": ""}
    if not keep_probe:
        values.update(native_dims=0, served="", probe="", probe_key="", probe_note="")
    with conn:
        dropped = conn.execute("DELETE FROM image_vectors").rowcount
        _set_meta(conn, values)
        _bump(conn)
    return max(0, dropped)


def stored_keys(conn):
    """The (content hash, kind) pairs that have a vector."""
    return {(row[0], row[1]) for row in conn.execute(
        "SELECT content_hash, kind FROM image_vectors")}


def write(conn, rows, native_dims=None, served=None):
    """Store [(content hash, kind, vector bytes)] in one transaction."""
    now = time.time()
    with conn:
        conn.executemany("INSERT OR REPLACE INTO image_vectors (content_hash, kind, vec, updated) "
                         "VALUES (?, ?, ?, ?)",
                         [(digest, kind, sqlite3.Binary(vec), now) for digest, kind, vec in rows])
        values = {}
        if native_dims:
            values["native_dims"] = native_dims
        if served:
            values["served"] = served
        if values:
            _set_meta(conn, values)
        _bump(conn)


def purge(store, conn):
    """Drop the vectors whose content no row of index.db has any more (as that kind of
    picture). Rows that are only missing for now, or of a library that is offline,
    stay in the index, so their vectors stay. Returns how many were dropped."""
    known = set()
    for row in conn.execute("SELECT DISTINCT content_hash, media, projection FROM images "
                            "WHERE content_hash != ''"):
        known.add((row[0], kind_of(row)))
    gone = [key for key in stored_keys(store) if key not in known]
    if not gone:
        return 0
    with store:
        for start in range(0, len(gone), ID_CHUNK):
            part = gone[start:start + ID_CHUNK]
            store.executemany("DELETE FROM image_vectors WHERE content_hash = ? AND kind = ?",
                              part)
        _bump(store)
    return len(gone)


def forget_unreferenced(conn):
    """purge() right after rows were deleted from index.db (Delete indexed data, the
    scanner's purge of rows missing too long), when data/image_vectors.db exists, so
    no look outlives its picture. conn is the index.db connection that deleted them.
    Best effort; returns how many were dropped."""
    try:
        store = connect(create=False)
    except sqlite3.Error:
        return 0
    if store is None:
        return 0
    try:
        return purge(store, conn)
    except sqlite3.Error:
        return 0
    finally:
        store.close()


def vectors_for(store, keys):
    """{(content hash, kind): vector bytes} of the stored vectors among keys."""
    wanted = list(set(keys))
    found = {}
    by_hash = collections.defaultdict(set)
    for digest, kind in wanted:
        by_hash[digest].add(kind)
    hashes = sorted(by_hash)
    for start in range(0, len(hashes), ID_CHUNK):
        part = hashes[start:start + ID_CHUNK]
        for digest, kind, vec in store.execute(
                "SELECT content_hash, kind, vec FROM image_vectors WHERE content_hash IN (%s)"
                % ",".join("?" * len(part)), part):
            if kind in by_hash[digest]:
                found[(digest, kind)] = bytes(vec)
    return found


# ---------------------------------------------------------------------------
# Status (the Manage page)

def status(config):
    """Looks for the Manage page: {"configured", "ready" (pictures can be embedded now),
    "problem" (why not, or how to turn it on), "endpoint", "recipe" (the current
    one, '' when not known), "stored" (vectors kept), "changed" (True when the kept
    vectors were made with other settings: they need Rebuild looks), "mismatch" (why
    the last probe check failed, '' if it did not: Rebuild looks is offered too),
    "reference" (a probe reference for these model files is kept, and a rebuild keeps
    it: else the next run makes one), "dims"}."""
    options = settings(config)
    endpoint, problem = endpoint_for(config, options)
    made_with = probe = ""
    if endpoint is not None:
        try:
            made_with = recipe(config, options)
            probe = probe_key(config, options)
        except browser_model.ModelError as error:
            problem, endpoint = str(error), None
    current = read_meta()
    stored = current["count"] if current else 0
    changed = bool(made_with and current and stored and current["recipe"] != made_with)
    mismatch = current["mismatch"] if current else ""
    reference = bool(probe and current and current["probe"] and not mismatch
                     and current["probe_key"] == probe)
    return {"configured": bool(options["endpoint"]), "ready": endpoint is not None,
            "problem": problem, "endpoint": options["endpoint"], "recipe": made_with,
            "stored": stored, "changed": changed, "mismatch": mismatch,
            "reference": reference, "dims": options["dims"]}


def counts(conn, config, library_ids, store=None):
    """{library id: {"pictures": present pictures, "embedded": with a vector, "pending":
    without one}} for some libraries (pictures whose content hash is not known yet
    are not counted: the next scan works it out). When the kept vectors were made
    with other settings (status()["changed"]) every picture counts as pending."""
    library_ids = list(library_ids)
    result = {library_id: {"pictures": 0, "embedded": 0, "pending": 0}
              for library_id in library_ids}
    own = store is None
    if own:
        store = connect(create=False)
    try:
        have = stored_keys(store) if store is not None else set()
        current = meta(store) if store is not None else None
    finally:
        if own and store is not None:
            store.close()
    if have and current is not None:
        options = settings(config)
        try:
            made_with = recipe(config, options) if options["endpoint"] else current["recipe"]
        except browser_model.ModelError:
            made_with = current["recipe"]       # (cannot be told now: they count as they are)
        if made_with != current["recipe"]:
            have = set()
    for row in library_rows(conn, library_ids):
        entry = result[row["library_id"]]
        entry["pictures"] += 1
        if (row["content_hash"], kind_of(row)) in have:
            entry["embedded"] += 1
        else:
            entry["pending"] += 1
    return result


# ---------------------------------------------------------------------------
# The embed run

class SizeError(ValueError):
    """The model's vectors are shorter than image_embedding.dims asks for."""

    def __init__(self, native, dims):
        ValueError.__init__(self, "The image embedding model gives %d numbers per picture, "
                            "fewer than image_embedding.dims (%d): set \"dims\" in config.json's "
                            "\"image_embedding\" to %d or less." % (native, dims, native))


def _new_summary():
    return {"configured": False, "embedded": 0, "failed": 0, "up_to_date": 0, "pending": 0,
            "removed": 0, "offline": 0, "tokens": 0, "seconds": 0.0, "stopped": False,
            "error": "", "changed": False, "rebuilt": 0, "probe": "", "dims": 0}


def _served_check(reply, model_path):
    """Raise ProviderError when a llama.cpp server names another GGUF than model_path."""
    served = reply.get("model") or ""
    name = os.path.basename(model_path or "")
    if name and served.lower().endswith(".gguf") and served.lower() != name.lower():
        raise llm.ProviderError("endpoint", "it serves the model %s, but looks are set up for "
                                "%s (image_embedding.model_file, or the model of its model "
                                "server): load that model, or change the setting"
                                % (served, name))
    return served


def _vectors(reply, options, model_path):
    """(the reply's vectors as stored bytes, the model's own length, the served name).
    Raises SizeError, ProviderError."""
    served = _served_check(reply, model_path)
    out = []
    for vector in reply["vectors"]:
        if options["dims"] and len(vector) < options["dims"]:
            raise SizeError(len(vector), options["dims"])
        try:
            out.append(embeddings.quantize(vector, options["dims"]))
        except ValueError as error:
            raise llm.ProviderError("content", str(error))
    native = len(reply["vectors"][0]) if reply["vectors"] else 0
    return out, native, served


def _per_item(error):
    """True when a failed request may be the fault of one picture in it (a picture
    the server cannot decode), not of the endpoint."""
    return error.kind in ("content", "image") or (error.kind == "endpoint"
                                                  and error.status in (400, 413, 422))


def _embed(endpoint, jpegs, ctx, sleep):
    def on_retry(error, delay):
        ctx.log("Looks: %s; trying again in %.0f s." % (error, delay), "warn")
    return llm.embed_with_retries(endpoint, [image_input(jpeg) for jpeg in jpegs], model="",
                                  should_stop=ctx.should_stop, sleep=sleep, on_retry=on_retry)


def check_probe(reference, probe):
    """The lowest cosine of the probe vectors ([{"name", "vec"}]) to their reference
    (of one length or another: both are cut to the shorter one), or None when there
    is nothing to compare."""
    old = {item["name"]: item["vec"] for item in reference or []}
    lowest = None
    for item in probe:
        before = old.get(item["name"])
        if before is None:
            continue
        first, second = base64.b64decode(before), base64.b64decode(item["vec"])
        # (Both cut to the shorter length, as Matryoshka vectors may be.)
        size = min(len(first), len(second))
        value = embeddings.signed_cosine(first[:size], second[:size]) if size else 0.0
        lowest = value if lowest is None else min(lowest, value)
    return lowest


def _offline_libraries(rows, libraries, ctx, helper):
    """{library id} of the libraries whose folder cannot be reached now, among those
    with rows that have no stored preview (their pictures wait for a later run)."""
    if helper is not None and getattr(helper, "load_image", None) is not None:
        return set()
    needed = {row["library_id"] for row in rows if row["has_preview"] != 1}
    if not needed:
        return set()
    import library_ops
    offline = set()
    for library in libraries:
        if library["id"] in needed:
            root = common.library_root(library)
            found = library_ops.folder_status(root, timeout=FOLDER_CHECK_TIMEOUT)
            if not found["reachable"]:
                offline.add(library["id"])
                ctx.log("Library %s: its pictures without a preview wait for a later run: the "
                        "folder %s cannot be reached (offline?)."
                        % (common.library_label(library), root or "(none)"), "warn")
    return offline


def _newest_first(todo, limit):
    todo = sorted(todo, key=lambda row: -row["id"])
    if limit is not None and len(todo) > max(0, limit):
        todo = todo[:max(0, limit)]
    return todo


def update(conn, config, ctx, library_ids=None, limit=None, servers=None, sleep=time.sleep,
           rebuild=False, helper=None):
    """Embed the pictures that have no vector yet. Returns a summary dict.

    conn is an index.db connection; ctx a jobs.JobContext. Only enabled
    libraries (limited to library_ids) are looked at; at most limit
    pictures are embedded (newest first), one per content (copies share
    it). Nothing is asked of the endpoint (and no model server is started)
    when every picture has its vector. A model server that config.json's
    local_server runs for the endpoint is started when needed and stopped
    afterwards (auto_start, auto_stop, stop_at: see local_server.Lease;
    servers is its ServerManager). When the kept vectors were made with
    other settings (see recipe()) nothing is done unless rebuild is True:
    then they are all thrown away first (summary rebuilt; the probe
    reference stays when the model files are the same). The probe pictures
    go first (see the module notes): a mismatch stops the run, and is kept
    in meta; then rebuild throws away the reference and every kept vector.
    Otherwise rebuild changes nothing, so a rebuild that is run again (a
    stopped job resumed, one per library) goes on where it was. Each batch
    is committed. A picture without a stored preview whose library folder
    cannot be reached waits for a later run (offline).

    Summary keys: configured, embedded, failed, up_to_date, pending,
    removed, offline, tokens, seconds, stopped, error, changed (the kept
    vectors need a rebuild), rebuilt (vectors thrown away for it), probe
    ("reference": stored as the reference now; "checked"; "off"; "mismatch":
    the run stopped there; "" when nothing was embedded), dims.
    """
    summary = _new_summary()
    options = settings(config)
    if not options["endpoint"]:
        return summary
    summary["configured"] = True
    summary["dims"] = options["dims"]
    endpoint, problem = endpoint_for(config, options)
    if endpoint is None:
        summary["error"] = problem
        ctx.log(problem, "error")
        return summary
    wanted = None if library_ids is None else set(library_ids)
    libraries = [library for library in common.enabled_libraries(config)
                 if library.get("id") and (wanted is None or library["id"] in wanted)]
    try:
        store = connect()
    except sqlite3.Error as error:
        summary["error"] = "The looks cannot be stored: %s" % error
        ctx.log(summary["error"], "error")
        return summary
    try:
        try:
            recipe_value = recipe(config, options)
            probe_value = probe_key(config, options)
            model_path = model_files(config, options)[0]
        except browser_model.ModelError as error:
            summary["error"] = str(error)
            ctx.log(summary["error"], "error")
            return summary
        current = meta(store)
        if current["recipe"] != recipe_value:
            if current["count"] and not rebuild:
                summary["changed"] = True
                summary["error"] = (
                    "The %d kept looks were made with other settings (another model or image "
                    "projector, dims or max_edge), so new ones would not match them. Rebuild "
                    "looks on the Manage page (scan.py --embed-images --rebuild-looks) throws "
                    "them away and makes them all again (a long GPU run)." % current["count"])
                ctx.log(summary["error"], "error")
                return summary
            keep = bool(current["probe"]) and not current["mismatch"] and (
                current["probe_key"] == probe_value)
            summary["rebuilt"] = reset(store, recipe_value, options["dims"], keep_probe=keep)
            if summary["rebuilt"]:
                ctx.log("Rebuilding looks: %d kept vector(s) were thrown away%s."
                        % (summary["rebuilt"], " (the probe reference is kept: the model "
                           "files are the same)" if keep else ""), "warn")
        elif rebuild and current["mismatch"]:
            summary["rebuilt"] = reset(store, recipe_value, options["dims"])
            ctx.log(REBUILD_MISMATCH % summary["rebuilt"], "warn")
        summary["removed"] = purge(store, conn)
        have = stored_keys(store)
        todo, seen = [], set()
        for row in library_rows(conn, [library["id"] for library in libraries]):
            key = (row["content_hash"], kind_of(row))
            if key in have:
                summary["up_to_date"] += 1
            elif key not in seen:
                seen.add(key)
                todo.append(row)
        offline = _offline_libraries(todo, libraries, ctx, helper) if todo else set()
        if offline:
            waiting = [row for row in todo if row["library_id"] in offline
                       and row["has_preview"] != 1]
            summary["offline"] = len(waiting)
            todo = [row for row in todo if not (row["library_id"] in offline
                                                 and row["has_preview"] != 1)]
        todo = _newest_first(todo, limit)
        summary["pending"] = len(todo)
        if not todo:
            return summary
        roots = {library["id"]: common.library_root(library) for library in libraries}
        _run(store, endpoint, options, todo, ctx, summary, config, servers, sleep, helper,
             roots, model_path, probe_value)
    finally:
        store.close()
    return summary


def _run(store, endpoint, options, todo, ctx, summary, config, servers, sleep, helper, roots,
         model_path, probe_value):
    """Embed todo (index rows) in batches (see update)."""
    import cache
    import images
    import local_server
    started = time.monotonic()
    ctx.stage("looks", total=len(todo))
    ctx.log("Making the looks of %d picture(s) (%s)..." % (len(todo), endpoint["name"]))
    lease = local_server.lease_for(ctx, config, [endpoint["name"]], manager=servers)
    previews = cache.PreviewStore()
    failures = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=PREPARE_THREADS)
    try:
        problems = lease.acquire()
        if problems:
            summary["error"] = "; ".join(problems.values())
            ctx.log("Cannot make looks: %s" % summary["error"], "error")
            return
        try:
            if not _probe(store, endpoint, options, ctx, summary, sleep, model_path, probe_value):
                return
        except llm.ProviderError as error:
            summary["stopped"] = ctx.should_stop()
            if not summary["stopped"]:
                summary["error"] = llm.explain_error(error, endpoint)
                ctx.log(summary["error"], "error")
            return
        except SizeError as error:
            summary["error"] = str(error)
            ctx.log(summary["error"], "error")
            return
        except images.ImageReadError as error:
            summary["error"] = "The probe pictures cannot be made: %s" % error
            ctx.log(summary["error"], "error")
            return

        def prepare(row):
            try:
                return picture_jpeg(row, options, previews, config, helper,
                                    roots.get(row["library_id"]))
            except (images.ImageReadError, videos.VideoReadError, OSError) as error:
                return error

        position = 0
        upcoming = None
        while position < len(todo):
            batch = todo[position:position + options["batch"]]
            ctx.wait_if_paused()
            if ctx.should_stop():
                summary["stopped"] = True
                break
            prepared = upcoming.result() if upcoming is not None else list(pool.map(prepare,
                                                                                     batch))
            following = todo[position + len(batch):position + 2 * len(batch)]
            # (The next batch is prepared while the model works on this one.)
            upcoming = pool.submit(lambda rows: list(map(prepare, rows)), following) \
                if following else None
            ready = []
            for row, made in zip(batch, prepared):
                if isinstance(made, Exception):
                    summary["failed"] += 1
                    failures.append((row["id"], common.redact(str(made))))
                else:
                    ready.append((row, made))
            try:
                rows, native, served = _embed_batch(endpoint, options, ready, ctx, summary,
                                                    failures, sleep, model_path)
            except llm.ProviderError as error:
                summary["stopped"] = ctx.should_stop()
                if not summary["stopped"]:
                    summary["error"] = llm.explain_error(error, endpoint)
                    ctx.log(summary["error"], "error")
                break
            except SizeError as error:
                summary["error"] = str(error)
                ctx.log(summary["error"], "error")
                break
            position += len(batch)
            if rows:
                write(store, rows, native, served)
                summary["embedded"] += len(rows)
            ctx.step(len(batch))
    finally:
        pool.shutdown(wait=True)
        lease.release()
        summary["seconds"] = round(time.monotonic() - started, 3)
    for image_id, error in failures[:FAILURE_LOG_LIMIT]:
        ctx.log("Picture %d could not be embedded: %s" % (image_id, error), "warn")
    if len(failures) > FAILURE_LOG_LIMIT:
        ctx.log("... and %d more." % (len(failures) - FAILURE_LOG_LIMIT), "warn")
    if summary["embedded"] or summary["failed"]:
        each = summary["seconds"] / summary["embedded"] if summary["embedded"] else 0.0
        tokens = (", about %d image tokens a picture" % round(summary["tokens"] /
                                                               summary["embedded"])
                  if summary["tokens"] and summary["embedded"] else "")
        ctx.log("Made the looks of %d picture(s) in %.1f s (%.2f s a picture%s)%s." % (
            summary["embedded"], summary["seconds"], each, tokens,
            "; %d failed" % summary["failed"] if summary["failed"] else ""))


def _mismatch(store, summary, ctx, message):
    """Stop the run for a failed probe check, and keep why in meta (status(): the Manage
    page then offers Rebuild looks)."""
    summary["probe"] = "mismatch"
    summary["error"] = message
    with store:
        _set_meta(store, {"mismatch": message})
        _bump(store)
    ctx.log(message, "error")
    return False


def _probe(store, endpoint, options, ctx, summary, sleep, model_path, probe_value):
    """Embed the probe pictures and check them against the reference (see the module
    notes); the first run with these model files (probe_value: probe_key()) stores them
    as the reference, at the model's own length. False when the run must stop (the
    summary says why). Raises ProviderError, SizeError, images.ImageReadError."""
    pictures = probe_pictures(options)
    reply = _embed(endpoint, [jpeg for _name, jpeg in pictures], ctx, sleep)
    _vectors(reply, options, model_path)        # (the checks of the served name and size)
    native = len(reply["vectors"][0]) if reply["vectors"] else 0
    served = reply.get("model") or ""
    try:
        probe = [{"name": name, "vec": base64.b64encode(embeddings.quantize(vector)).decode(
            "ascii")} for (name, _jpeg), vector in zip(pictures, reply["vectors"])]
    except ValueError as error:
        raise llm.ProviderError("content", str(error))
    current = meta(store)
    if current["native_dims"] and native != current["native_dims"]:
        return _mismatch(store, summary, ctx, (
            "The image model now gives %d numbers per picture, not %d: another model answers "
            "%s. If that is on purpose, Rebuild looks on the Manage page (scan.py "
            "--embed-images --rebuild-looks)." % (native, current["native_dims"],
                                                  endpoint["name"])))
    if not current["probe"]:
        with store:
            _set_meta(store, {"probe": json.dumps(probe), "probe_key": probe_value,
                              "native_dims": native, "served": served, "mismatch": "",
                              "probe_note": "made by the first run with these model files, on "
                              "%s at %s" % (endpoint["name"],
                                            time.strftime("%Y-%m-%d %H:%M"))})
            _bump(store)
        summary["probe"] = "reference"
        ctx.log("Looks: the probe pictures could not be checked (this is the first run with "
                "these model files): their vectors are the reference later runs are checked "
                "against. (Make the first run on a CPU server, or with flash attention off, to "
                "have a trusted reference; see SETTINGS.md.)", "warn")
        return True
    lowest = check_probe(current["probe"], probe) if options["probe_check"] else None
    if lowest is not None and lowest < PROBE_SAME:
        return _mismatch(store, summary, ctx, PROBE_MISMATCH % (
            lowest, PROBE_SAME, current["probe_note"] or "made by an earlier run"))
    if current["mismatch"]:
        with store:
            _set_meta(store, {"mismatch": ""})
            _bump(store)
    if not options["probe_check"]:
        summary["probe"] = "off"
        return True
    summary["probe"] = "checked"
    ctx.log("Looks: the probe pictures match the reference (cosine %.4f)." % (
        lowest if lowest is not None else 1.0))
    return True


def _embed_batch(endpoint, options, ready, ctx, summary, failures, sleep, model_path):
    """([(content hash, kind, vector bytes)], native dims, served name) for one batch
    of [(row, JPEG bytes)]. A batch the server refuses as a whole is tried again
    picture by picture; pictures that still fail are counted and left for a later
    run. Raises ProviderError when the endpoint cannot be used at all."""
    if not ready:
        return [], None, ""
    try:
        replies = [(ready, _embed(endpoint, [jpeg for _row, jpeg in ready], ctx, sleep))]
    except llm.ProviderError as error:
        if not _per_item(error) or len(ready) == 1:
            if len(ready) == 1 and _per_item(error):
                summary["failed"] += 1
                failures.append((ready[0][0]["id"], str(error)))
                return [], None, ""
            raise
        replies = []
        for item in ready:
            if ctx.should_stop():
                break
            try:
                replies.append(([item], _embed(endpoint, [item[1]], ctx, sleep)))
            except llm.ProviderError as single:
                if not _per_item(single):
                    raise
                summary["failed"] += 1
                failures.append((item[0]["id"], str(single)))
    rows, native, served = [], None, ""
    for items, answer in replies:
        summary["tokens"] += answer.get("tokens") or 0
        vectors, size, name = _vectors(answer, options, model_path)
        native, served = size or native, name or served
        for (row, _jpeg), vec in zip(items, vectors):
            rows.append((row["content_hash"], kind_of(row), vec))
    return rows, native, served


# ---------------------------------------------------------------------------
# Exports

def fresh_looks(conn, library_id, image_ids, store=None):
    """The vectors of some pictures of a library, for an export: {"model" (look_id),
    "dims", "vectors": {image id: bytes}, "missing": how many have none}."""
    wanted = set(image_ids)
    result = {"model": "", "dims": 0, "vectors": {}, "missing": 0}
    own = store is None
    if own:
        store = connect(create=False)
    if store is None:
        result["missing"] = len(wanted)
        return result
    try:
        current = meta(store)
        rows = [row for row in library_rows(conn, [library_id]) if row["id"] in wanted]
        found = vectors_for(store, [(row["content_hash"], kind_of(row)) for row in rows])
    finally:
        if own:
            store.close()
    size = current["dims"] or current["native_dims"]
    result.update(model=look_id(current["recipe"]), dims=size)
    for row in rows:
        vec = found.get((row["content_hash"], kind_of(row)))
        if vec is not None and size and len(vec) == size:
            result["vectors"][row["id"]] = vec
    result["missing"] = len(wanted) - len(result["vectors"])
    return result


# ---------------------------------------------------------------------------
# The web server: the vectors of the pictures a caller sees, and example pictures

class LooksService:
    """Answers /api/looks: the image vectors of the pictures a caller may see, as compact
    JSON {"version", "model" (look_id), "dims", "query" (whether this caller can
    search by an example picture with them), "count", "ids": [...], "data": base64
    of the vectors one after the other}, with a strong ETag, gzip and 304 (as
    embeddings.VectorService). Hidden pictures' vectors are only sent with
    include_hidden (the admin), missing pictures' never, exactly like /api/index.
    Thread safe."""

    def __init__(self, memo_size=4):
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._memo = collections.OrderedDict()
        self._memo_size = memo_size
        self.builds = 0

    def respond(self, conn, config, libraries, include_hidden=False, accept_gzip=False,
                etag_matches=None, index_state=None):
        """(status, body bytes, headers) for the looks of libraries ({id: name}).

        index_state is search_index.data_state() of the libraries.
        """
        store = connect(create=False)
        try:
            current = meta(store) if store is not None else None
            query = False
            if include_hidden and current is not None and current["count"]:
                try:
                    query = (endpoint_for(config)[0] is not None
                             and recipe(config) == current["recipe"])
                except browser_model.ModelError:
                    query = False
            key = [LOOKS_VERSION, current, query, bool(include_hidden),
                   [[library_id, name] for library_id, name in libraries.items()], index_state]
            etag = hashlib.sha1(json.dumps(key, separators=(",", ":"), sort_keys=True)
                                .encode("utf-8")).hexdigest()[:32]
            headers = {"ETag": '"%s"' % (etag + "-gzip" if accept_gzip else etag),
                       "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding",
                       "X-Content-Type-Options": "nosniff"}
            if etag_matches is not None and (etag_matches(etag) or etag_matches(etag + "-gzip")):
                return 304, b"", headers
            with self._lock:
                data = self._memo.get(etag)
                if data is not None:
                    self._memo.move_to_end(etag)
            if data is None:
                with self._build_lock:
                    data = self._build(conn, store, current, query, libraries, include_hidden)
                    self.builds += 1
                with self._lock:
                    self._memo[etag] = data
                    while len(self._memo) > self._memo_size:
                        self._memo.popitem(last=False)
        finally:
            if store is not None:
                store.close()
        if accept_gzip:
            headers["Content-Encoding"] = "gzip"
            return 200, data, headers
        return 200, gzip.decompress(data), headers

    def _build(self, conn, store, current, query, libraries, include_hidden):
        ids, chunks = [], []
        size = (current["dims"] or current["native_dims"]) if current else 0
        if store is not None and libraries and size:
            rows = [row for row in library_rows(conn, list(libraries))
                    if include_hidden or not row["hidden"]]
            found = vectors_for(store, [(row["content_hash"], kind_of(row)) for row in rows])
            for row in rows:
                vec = found.get((row["content_hash"], kind_of(row)))
                if vec is not None and len(vec) == size:
                    ids.append(row["id"])
                    chunks.append(vec)
        payload = {"version": LOOKS_VERSION,
                   "model": look_id(current["recipe"]) if current else "",
                   "dims": size, "query": bool(query), "count": len(ids), "ids": ids,
                   "data": base64.b64encode(b"".join(chunks)).decode("ascii")}
        text = json.dumps(payload, separators=(",", ":"))
        return gzip.compress(text.encode("utf-8"), 6, mtime=0)


class ExampleEmbedder:
    """Answers /api/looks/query (the admin's search by an example picture): the vector of
    a picture sent by the page, made the way the pictures' are (prepared in memory;
    never written to disk, never logged). An embeddings server that config.json's
    local_server manages is started when needed (its auto_start), answering
    "starting" until it has loaded its model, and stopped again after its
    idle_stop_minutes. Per client QUERY_BURST pictures at once, refilled at
    QUERY_RATE a second. Thread safe."""

    def __init__(self, servers=None, config_loader=None, burst=QUERY_BURST, rate=QUERY_RATE,
                 clock=time.monotonic):
        self._servers = servers
        self._config_loader = config_loader
        self._lock = threading.Lock()
        self._buckets = collections.OrderedDict()
        self._burst = float(burst)
        self._rate = float(rate)
        self._clock = clock

    def _manager(self):
        if self._servers is None:
            import local_server
            self._servers = local_server.default_manager()
        return self._servers

    def allow(self, client):
        """True when the client may have one more picture embedded now (token bucket)."""
        now = self._clock()
        with self._lock:
            tokens, when = self._buckets.pop(client, (self._burst, now))
            tokens = min(self._burst, tokens + max(0.0, now - when) * self._rate)
            allowed = tokens >= 1.0
            self._buckets[client] = (tokens - 1.0 if allowed else tokens, now)
            while len(self._buckets) > 1024:
                self._buckets.popitem(last=False)
        return allowed

    def answer(self, config, data, client=""):
        """(HTTP status, JSON-friendly dict) for a picture (its bytes) from client.

        200 {"model" (look_id of the current recipe), "dims", "vec": base64,
        "matches" (the kept vectors are of the same recipe), "thumb" (a data:
        URL of a small JPEG of the picture as the model saw it, for the page
        to show: a browser cannot show every kind it may send, such as
        HEIC)}; 400 for a picture that cannot be read; 404 {"state": "off"}
        when looks are not set up; 413 for a picture over QUERY_MAX_BYTES (or
        with more pixels than images.EXAMPLE_MAX_PIXELS); 415 for a kind of
        file that is not read (images.prepare_example_for_looks: only common
        picture formats, told by their first bytes, ever reach Pillow); 429
        when asked too often; 503 {"state": "starting" | "off" |
        "unavailable", "error"}.
        """
        options = settings(config)
        endpoint, problem = endpoint_for(config, options)
        if endpoint is None:
            return 404, {"state": "off", "error": problem}
        if not data:
            return 400, {"error": "No picture was sent."}
        if len(data) > QUERY_MAX_BYTES:
            return 413, {"error": "The picture is larger than %d MB." % (
                QUERY_MAX_BYTES // (1024 * 1024))}
        try:
            made_with = recipe(config, options)
            model_path = model_files(config, options)[0]
        except browser_model.ModelError as error:
            return 503, {"state": "unavailable", "error": str(error)}
        if not self.allow(client):
            return 429, {"error": "Too many pictures at once; try again in a moment."}
        try:
            import images
        except (ImportError, SystemExit):
            return 503, {"state": "unavailable", "error": "Reading pictures needs Pillow."}
        try:
            jpeg = images.prepare_example_for_looks(bytes(data), options["max_edge"])
            thumb = images.example_chip_jpeg(jpeg)
        except images.PictureNotAllowed as error:
            return 415, {"error": "This kind of file cannot be compared: %s." % error}
        except images.PictureTooLarge as error:
            return 413, {"error": "This picture is too large: %s." % error}
        except images.ImageReadError as error:
            return 400, {"error": "This picture cannot be read: %s." % error}
        servers = self._manager()
        state, message = servers.start_for_queries(config, endpoint["name"])
        if state == "starting" and self._config_loader is not None:
            servers.start_scheduler(self._config_loader)
        if state != "ready":
            return 503, {"state": state, "error": message}
        try:
            reply = llm.embed(endpoint, [image_input(jpeg)], model="", timeout=QUERY_TIMEOUT)
            vectors, _native, _served = _vectors(reply, options, model_path)
        except llm.ProviderError as error:
            if error.status == 503:
                return 503, {"state": "starting", "error": "The image model is loading."}
            return 503, {"state": "unavailable", "error": llm.explain_error(error, endpoint)}
        except ValueError as error:
            return 503, {"state": "unavailable", "error": str(error)}
        servers.touch(endpoint["name"])
        current = read_meta()
        return 200, {"model": look_id(made_with), "dims": len(vectors[0]),
                     "vec": base64.b64encode(vectors[0]).decode("ascii"),
                     "matches": bool(current and current["recipe"] == made_with),
                     "thumb": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(
                         "ascii")}
