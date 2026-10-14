#!/usr/bin/env python3
"""Search by meaning: one embedding vector per described picture (data/vectors.db).

An embedding model (config.json "embedding", see SETTINGS.md) turns the
search text of each described picture into a vector; the search page
turns what is typed into one too (/api/embed) and ranks pictures by how
close their vectors are (cosine), next to the word matches. "More like
this" (similar:<id>) ranks by the closeness of one picture's vector, and
needs no model at all, so it also works in a static export.

The search text (search_text) is made from what the description already
shows: the effective title, description and tags (the owner's edits laid
over the model's), subjects, animals, food, vehicles, people, objects,
activities, scene, setting, time of day, event, colors, the kind of
picture and the start of the text in it (and for the clip that carries
a drive's own words, the drive's title, description and tags: see
drives.cover_texts). Never file or folder names, library names, dates,
places, GPS positions (not even those written in its text, such as a
dashcam's overlay: imageinfo.scrub_positions), cameras or who described
the picture: a vector
reveals nothing that the description does not show, so an export can
ship vectors whatever its privacy options are (an export checks a
clip's vector against the drive words it publishes, see fresh_vectors).

data/vectors.db is a database of its own (deleting it is harmless: it is
built again):

    meta     key, value: version, model, dims (as set: 0 is all),
             native_dims (the length the model gives before cutting),
             served (the model's name as its server reports it), changed
             (why the vectors are out of date: another model answers
             queries now; '' when they are not), recipe (see recipe()),
             probe (the vectors of PROBE_TEXTS, JSON), epoch (random, new
             for every file), generation (raised by every change, for ETags)
    vectors  image_id, library_id, text_hash, vec, updated

vec holds signed bytes: the model's vector cut to its first dims numbers
(Matryoshka models such as Qwen3-Embedding keep their meaning when cut;
dims 0 keeps all of them, see vector_size), L2-normalised, then times 127
and rounded. The cosine of two pictures is then the dot product of their
bytes / 127^2. text_hash is the SHA-1 of the search text, the model and
dims: a picture is embedded again only when it changes (an edit, a new
description, the refine pass, another model). Changing the model or dims
empties the table, and so does another model behind the same endpoint
(its name or its vectors' length changed: noticed by the next embed run
that has something to embed, or by a query, which marks every vector
out of date for the Manage page and the next run).
Only vectors made from a picture's current search text are ever served
or exported (an edit makes the old one stale at once).

update() (the "embed" job, and the end of scan, describe and refine runs)
embeds the pictures whose vector is missing or stale, in batches, and
drops the vectors of pictures that are gone. Hidden pictures are only
sent to an endpoint on this computer or the local network. A text that
is too long for the model is cut (the text read in the picture goes
first) and tried again.

Some small models (mdbr-leaf-ir) end in a sentence-transformers Dense
layer that llama.cpp leaves out ("dense_file", see SETTINGS.md). With
one, the endpoint is asked for its raw pooled vectors (the request field
"embd_normalize": -1, which llama.cpp's server honours) and the layer is
applied here (apply_dense), for pictures and queries alike; an endpoint
that answers with normalised vectors anyway is refused. The vectors are
then tied to the model file and the Dense head ("recipe"), so another file
empties the store like another model does, and every run that embeds
anything also embeds PROBE_TEXTS (stored in meta "probe"): a probe that
comes out differently means another model answers the endpoint now. An
export can then let visitors' browsers embed their queries with the same
model (browser_model.py, the export's "meaning" option): the probe lets
the page check that it makes the same vectors.

This module uses only the standard library.
"""

import base64
import collections
import gzip
import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import time

import browser_model
import common
import db
import drives
import image_edits
import imageinfo
import llm
import videos

# Raise when the layout of vectors.db changes: an older file is then
# emptied and built again.
VECTORS_VERSION = 1
# What queries are prefixed with: retrieval models (mdbr-leaf-ir,
# snowflake-arctic-embed, bge) match a query to documents better when told
# what the query is for; documents get no prefix. Qwen3-Embedding wants an
# instruction instead (embedding.query_prefix, see SETTINGS.md).
DEFAULT_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
DEFAULTS = {
    "endpoint": "",
    "model": "",
    "dims": 256,
    "query_prefix": DEFAULT_QUERY_PREFIX,
    # (A batch of a small model takes a fraction of a second on a CPU.)
    "batch": 32,
    "allow_cloud": False,
    # The GGUF file the endpoint runs ('' = the model of the model server
    # config.json's local_server runs for it), its Dense head, the model's
    # license and a credit line (for exports that ship the model).
    "model_file": "",
    "dense_file": "",
    "license_file": "",
    "credit": "",
}
MAX_CREDIT = 200
# Two made-up sentences embedded (as documents) by every run that embeds
# anything: when they come out differently, another model answers the
# endpoint; an export ships them so the visitor's browser can check itself.
PROBE_TEXTS = ("A brown dog runs along a sandy beach at sunset.",
               "A plate of pasta with tomato sauce on a wooden kitchen table.")
# A probe this close (cosine) to the stored one is the same model.
PROBE_SAME = 0.99
# An endpoint whose vectors all have a length this close to 1 normalised them.
NORMALISED = 1e-3
# What llama.cpp says when a text is too long for the model's context.
TOO_LONG_MARKERS = ("is too large to process", "exceeds the available context size",
                    "larger than the max context size")
# A text that is too long is cut this many times at most.
CUT_TRIES = 3
OCR_LABEL = "Text in the picture: "
MAX_DIMS = 8192
MAX_BATCH = 256
# How much of the text in a picture (OCR) goes into its search text.
OCR_CHARS = 500
# Vectors are stored as signed bytes: unit length times this.
SCALE = 127
# A query longer than this (characters) is cut.
QUERY_CHARS = 1000
# Texts one /api/embed request may ask for at once (a query and its words:
# search-core.js meaningTexts, MEANING_TEXTS); more is a 400. A request counts
# against QUERY_BURST once per QUERY_TEXTS_PER_TOKEN texts the model must embed.
QUERY_TEXTS = 9
QUERY_TEXTS_PER_TOKEN = 3
# Seconds a query embedding may take.
QUERY_TIMEOUT = 20
# Query vectors remembered by the web server.
QUERY_CACHE_SIZE = 256
# Per client: this many query embeddings at once, refilled at RATE a second.
QUERY_BURST = 20
QUERY_RATE = 4.0
# Failed pictures named in the log of one run at most.
FAILURE_LOG_LIMIT = 5
ID_CHUNK = 500

NOT_CONFIGURED = ("Search by meaning is off: set \"embedding\": {\"endpoint\": \"NAME\"} in "
                  "config.json to an endpoint that serves an embedding model (see SETTINGS.md).")
# What /api/embed tells other computers (the details are the admin's).
PUBLIC_MESSAGES = {
    "off": "Search by meaning is not set up on this server.",
    "starting": "Search by meaning is starting.",
    "unavailable": "Search by meaning is unavailable right now.",
}
# How long the automatic embed step (after scans, describe and refine runs)
# waits for an embedding endpoint on this computer that does not answer.
UNREACHABLE_WAIT = 3.0

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS vectors (image_id INTEGER PRIMARY KEY, library_id TEXT NOT NULL, "
    "text_hash TEXT NOT NULL, vec BLOB NOT NULL, updated REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS vectors_library ON vectors(library_id)",
)


# ---------------------------------------------------------------------------
# Settings

def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def settings(config):
    """config.json's "embedding" settings, every one filled in and checked (DEFAULTS)."""
    raw = config.get("embedding") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULTS)
    result["endpoint"] = _text(raw.get("endpoint"))
    result["model"] = _text(raw.get("model"))
    dims = raw.get("dims")
    if _whole(dims) and 0 <= dims <= MAX_DIMS:
        result["dims"] = dims
    batch = raw.get("batch")
    if _whole(batch) and 1 <= batch <= MAX_BATCH:
        result["batch"] = batch
    if isinstance(raw.get("query_prefix"), str):
        result["query_prefix"] = raw["query_prefix"]
    result["allow_cloud"] = raw.get("allow_cloud") is True
    for key in ("model_file", "dense_file", "license_file"):
        result[key] = common.unquote_path(raw.get(key))
    result["credit"] = " ".join(_text(raw.get("credit")).split())[:MAX_CREDIT]
    return result


def file_setting(path):
    """A file named in the "embedding" settings as a full path ('' for none): relative
    paths are relative to the folder of config.json."""
    return common.resolve_config_relative(path) if path else ""


def model_source(config, options=None, endpoint_name=None):
    """(the GGUF file the embedding endpoint runs, why it cannot be told now or ''): the
    file is embedding.model_file, else the model of the embeddings server config.json's
    local_server runs for the endpoint (when that cannot be found: '' and why), else ''."""
    options = options or settings(config)
    if options["model_file"]:
        return file_setting(options["model_file"]), ""
    import local_server
    server = local_server.find_server(config, endpoint_name or options["endpoint"])
    if server is None or not local_server.embeddings_server(server):
        return "", ""
    try:
        return local_server.model_path(server), ""
    except local_server.ServerError as error:
        return "", str(error)


def model_file(config, options=None, endpoint_name=None):
    """The GGUF file the embedding endpoint runs (see model_source), or ''."""
    return model_source(config, options, endpoint_name)[0]


_dense_cache = {}
_dense_lock = threading.Lock()


def dense_head(options):
    """The Dense head of embedding.dense_file cut to dims rows ({"out", "cols", "rows",
    "weight", "bias"}, see browser_model.read_dense), or None when there is none.
    Remembered by the file's path, size and time. Raises browser_model.ModelError."""
    path = file_setting(options["dense_file"])
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError as error:
        raise browser_model.ModelError("The Dense head %s (embedding.dense_file) cannot be read "
                                       "(%s)." % (path, error.strerror or error))
    key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns,
           options["dims"])
    with _dense_lock:
        found = _dense_cache.get(key)
    if found is None:
        try:
            found = browser_model.read_dense(path, options["dims"])
        except browser_model.ModelError as error:
            raise browser_model.ModelError("The Dense head %s (embedding.dense_file) %s."
                                           % (path, error))
        if options["dims"] and found["out"] < options["dims"]:
            raise browser_model.ModelError(
                "The Dense head %s (embedding.dense_file) gives %d numbers, fewer than "
                "embedding.dims (%d)." % (path, found["out"], options["dims"]))
        with _dense_lock:
            _dense_cache.clear()
            _dense_cache[key] = found
    return found


def apply_dense(dense, vector):
    """The Dense head applied to a raw pooled vector: its rows times the vector, plus its
    bias, in double precision (as the browser engine does). Raises ValueError when the
    vector is not as long as the head expects."""
    cols = dense["cols"]
    if len(vector) != cols:
        raise ValueError("the model gives %d numbers per text, but the Dense head "
                         "(embedding.dense_file) takes %d: it belongs to another model"
                         % (len(vector), cols))
    weights, biases = dense["weight"], dense["bias"]
    values = [float(value) for value in vector]
    return [sum(map(float.__mul__, weights[row * cols:(row + 1) * cols].tolist(), values))
            + biases[row] for row in range(dense["rows"])]


def recipe(config, options=None, endpoint_name=None):
    """What the vectors are made with beyond the model's name and dims: the SHA-1 of the
    model file's and the Dense head's SHA-256 (and dims); '' when neither file is known.
    Another recipe empties the store (prepare_model).

    Raises browser_model.ModelError when a file the vectors are made with is
    known but cannot be read now (a renamed folder, a drive that is not
    there): the stored vectors are then neither known to match nor known not
    to, and must not be thrown away for it.
    """
    options = options or settings(config)
    model_path, problem = model_source(config, options, endpoint_name)
    if problem:
        raise browser_model.ModelError(problem)
    dense_path = file_setting(options["dense_file"])
    if not model_path and not dense_path:
        return ""
    digests = []
    for path, setting in ((model_path, "embedding.model_file, or the model of its model "
                                       "server"),
                          (dense_path, "embedding.dense_file")):
        digest = browser_model.file_sha256(path) if path else ""
        if path and not digest:
            raise browser_model.ModelError("The file %s (%s) cannot be read." % (path, setting))
        digests.append(digest)
    text = "%s\n%s\n%d" % (digests[0], digests[1], options["dims"])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def recipe_problem(config, options=None):
    """Why the recipe of the vectors cannot be told now (see recipe), or ''."""
    try:
        recipe(config, options)
    except browser_model.ModelError as error:
        return str(error)
    return ""


def endpoint_for(config, options=None):
    """(the embedding endpoint, '') or (None, why it cannot be used)."""
    options = options or settings(config)
    name = options["endpoint"]
    if not name:
        return None, NOT_CONFIGURED
    endpoint = common.get_endpoint(config, name)
    if endpoint is None:
        return None, ("There is no endpoint called %s (embedding.endpoint in config.json): add "
                      "it on the Settings page." % name)
    if not endpoint["enabled"]:
        return None, "The embedding endpoint %s is turned off." % endpoint["name"]
    if endpoint["kind"] == "claude":
        return None, ("The endpoint %s cannot make embeddings (Anthropic's API has none): choose "
                      "an OpenAI-compatible or Ollama endpoint." % endpoint["name"])
    if not common.is_local_endpoint(endpoint) and not options["allow_cloud"]:
        return None, ("The embedding endpoint %s is not on this computer or the local network; "
                      "set embedding.allow_cloud to true in config.json to use it anyway."
                      % endpoint["name"])
    if options["dense_file"] and endpoint["kind"] == "ollama":
        return None, ("The embedding endpoint %s is an Ollama one, which cannot give the raw "
                      "vectors a Dense head needs (embedding.dense_file): use a llama.cpp "
                      "server (its OpenAI-compatible API)." % endpoint["name"])
    return endpoint, ""


def model_key(endpoint, options):
    """The name vectors are made with: the model asked for, else the endpoint's model or name."""
    return options["model"] or endpoint["model"] or endpoint["name"]


def configured(config):
    """True when config.json names an embedding endpoint (usable or not)."""
    return bool(settings(config)["endpoint"])


# ---------------------------------------------------------------------------
# Search text

def _phrases(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [text for text in (_text(item) for item in value) if text]


def _unique(items):
    seen, result = set(), []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# Enum values that read better spelled out.
_ENUM_WORDS = {"sunrise_sunset": "sunrise or sunset"}


def _enum(info, field, nothing=("", "unknown", "none")):
    value = _text(info.get(field))
    if value.lower() in nothing:
        return ""
    return _ENUM_WORDS.get(value.lower(), value.replace("_", " "))


def _cut(text, limit):
    """text cut to at most limit characters, at a word where there is one."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return cut[:space] if space > limit // 2 else cut


def search_text(info, edit=None, media=None):
    """The text a picture is embedded by: what its (effective) description shows.

    info is the stored description (a dict, or None when the picture is
    not described); edit the owner's edit (image_edits) laid over it;
    media the row's media column ('video': its kind is "video", as the
    search records and the info pane say, see videos.effective_info; the
    model's own kind follows unless it is photo or other, so a screen
    recording still reads as a screenshot). Returns '' when there is
    nothing to embed. Never holds names, dates, places, positions,
    cameras or provenance (see the module notes).
    """
    if not image_edits.is_empty(edit):
        info = image_edits.apply(info if isinstance(info, dict) else None, edit)
    if not isinstance(info, dict):
        return ""
    model_kind = _enum(info, "photo_type")
    info = videos.effective_info(info, media)
    lines = []
    title = _text(info.get("title"))
    if title:
        lines.append(title if title[-1:] in ".!?" else title + ".")
    description = _text(info.get("description"))
    if description:
        lines.append(" ".join(description.split()))

    def add(label, items):
        items = _unique(items)
        if items:
            lines.append("%s: %s." % (label, ", ".join(items)))

    add("Tags", _phrases(info.get("tags")))
    add("Shows", _phrases(info.get("subjects")) + _phrases(info.get("animals"))
        + _phrases(info.get("food")) + _phrases(info.get("vehicles"))
        + _phrases(info.get("people_descriptors")))
    objects = info.get("objects")
    if isinstance(objects, list):
        add("Objects", [_text(item.get("name")) for item in objects
                        if isinstance(item, dict) and _text(item.get("name"))])
    add("Activities", _phrases(info.get("activities")))
    scene = _text(info.get("scene"))
    add("Scene", ([scene] if scene else []) + [word for word in (
        _enum(info, "setting"), _enum(info, "time_of_day")) if word])
    add("Event", [word for word in (_enum(info, "event", ("", "unknown", "none", "other")),)
                  if word])
    kinds = [_enum(info, "photo_type")]
    if media == videos.MEDIA_VIDEO and model_kind.lower() not in ("photo", "other",
                                                                  videos.MEDIA_VIDEO):
        kinds.append(model_kind)
    kinds.append(_enum(info, "document_type", ("", "none", "other")))
    add("Kind", [word for word in kinds if word])
    add("Colors", _phrases(info.get("colors")))
    text = _text(info.get("text"))
    if text:
        lines.append("Text in the picture: %s" % _cut(imageinfo.scrub_positions(text),
                                                      OCR_CHARS))
    # (No GPS position written out either -- as a dashcam prints its own over
    # its video, which the model read -- in any line: an export ships the
    # vectors whatever its location options are.)
    return imageinfo.scrub_positions("\n".join(lines))


def text_hash(text, model, dims):
    """The SHA-1 (hex) of a search text with the model and dims it is embedded with."""
    return hashlib.sha1(("%s\n%d\n%s" % (model, dims, text)).encode("utf-8")).hexdigest()


def _load_info(text):
    if not text:
        return None
    try:
        info = json.loads(text, parse_constant=lambda _name: None)
    except (TypeError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def drive_text(fields):
    """The search text of a drive's own words as its records publish them (fields: dvt,
    dvd and dvg, see drives.text_fields): its title, description and tags only."""
    fields = fields or {}
    return search_text({"title": fields.get("dvt"), "description": fields.get("dvd"),
                        "tags": fields.get("dvg")})


def drive_texts(conn, library_ids):
    """{image id: search text of its drive} for the clip that carries the words of every
    described drive of some libraries (drives.cover_texts): search by meaning finds
    the drive by that clip."""
    return {image_id: drive_text(drives.text_fields(info))
            for image_id, info in drives.cover_texts(conn, library_ids).items()}


def _with_drive(text, words):
    """A clip's search text with its drive's (words) added, when it carries them."""
    if not words:
        return text
    return (text + "\n" if text else "") + "Drive: " + words


def library_texts(conn, library_ids, driven=None):
    """[(image id, library id, hidden, search text)] of the present pictures of some
    libraries that have one (described, or edited by the owner), in id order.
    driven: {image id: the search text of the drive words it carries} (default:
    drive_texts')."""
    library_ids = list(library_ids)
    if not library_ids:
        return []
    edits = image_edits.for_libraries(conn, library_ids)
    if driven is None:
        driven = drive_texts(conn, library_ids)
    result = []
    for row in conn.execute("SELECT id, library_id, info, described, hidden, media FROM images "
                            "WHERE missing_since IS NULL AND library_id IN (%s) ORDER BY id"
                            % ",".join("?" * len(library_ids)), library_ids):
        info = _load_info(row["info"]) if row["described"] else None
        text = _with_drive(search_text(info, edits.get(row["id"]), row["media"]),
                           driven.get(row["id"]))
        if text:
            result.append((row["id"], row["library_id"], bool(row["hidden"]), text))
    return result


_ROWS_SQL = ("SELECT i.id, i.library_id, i.hidden, i.described, i.updated, e.updated "
             "FROM images AS i LEFT JOIN image_edits AS e ON e.image_id = i.id "
             "WHERE i.missing_since IS NULL AND i.library_id IN (%s)")
# TextHashes forgets everything when it knows more pictures than this.
TEXT_HASHES_LIMIT = 2000000


class TextHashes:
    """The text_hash of every present picture's search text, worked out again only for
    the rows that changed since (their updated time, described flag or edit time:
    describing, refining and edits all change one of them).

    The web server keeps one (VectorService.hashes), so the Manage page's
    counts and /api/vectors' check that a vector is up to date cost one
    query instead of reading every description again. Thread safe.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._where = None          # (data folder, model, dims) the hashes are for
        self._rows = {}             # image id -> (row key, hash or '' without a text)

    def current(self, conn, library_ids, model, dims):
        """{image id: (library id, hidden, text hash)} of the present pictures of some
        libraries that have a search text (see library_texts)."""
        library_ids = list(library_ids)
        if not library_ids:
            return {}
        rows = conn.execute(_ROWS_SQL % ",".join("?" * len(library_ids)),
                            library_ids).fetchall()
        where = (os.path.normcase(os.path.abspath(common.data_dir())), model, dims)
        with self._lock:
            if self._where != where or len(self._rows) > TEXT_HASHES_LIMIT:
                self._where = where
                self._rows = {}
            known = {row[0]: self._rows.get(row[0]) for row in rows}
        driven = drive_texts(conn, library_ids)
        keys = {row[0]: (row[3], row[4], row[5], driven.get(row[0], "")) for row in rows}
        changed = [image_id for image_id, entry in known.items()
                   if entry is None or entry[0] != keys[image_id]]
        fresh = {}
        for start in range(0, len(changed), ID_CHUNK):
            part = changed[start:start + ID_CHUNK]
            edits = image_edits.load_many(conn, part)
            for row in conn.execute("SELECT id, info, described, media FROM images "
                                    "WHERE id IN (%s)" % ",".join("?" * len(part)), part):
                info = _load_info(row[1]) if row[2] else None
                text = _with_drive(search_text(info, edits.get(row[0]), row[3]),
                                   driven.get(row[0]))
                fresh[row[0]] = text_hash(text, model, dims) if text else ""
        with self._lock:
            if self._where == where:
                for image_id, digest in fresh.items():
                    self._rows[image_id] = (keys[image_id], digest)
        result = {}
        for row in rows:
            image_id = row[0]
            digest = fresh.get(image_id) if image_id in fresh else (known[image_id] or ("", ""))[1]
            if digest:
                result[image_id] = (row[1], bool(row[2]), digest)
        return result


# ---------------------------------------------------------------------------
# Vectors

class SizeError(ValueError):
    """The model's vectors are shorter than embedding.dims asks for."""

    def __init__(self, native, dims):
        ValueError.__init__(self, "The embedding model gives %d numbers per text, fewer than "
                            "embedding.dims (%d): set \"dims\" in config.json's \"embedding\" "
                            "to %d or less (0 keeps them all)." % (native, dims, native))
        self.native = native


def quantize(vector, dims=0):
    """A model's vector as stored: its first dims numbers (0: all), unit length, times
    SCALE, rounded, as signed bytes. Raises SizeError when the vector is shorter than
    dims, ValueError for an empty or zero vector."""
    if dims and len(vector) < dims:
        raise SizeError(len(vector), dims)
    values = vector[:dims] if dims else vector
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not values or not norm or not math.isfinite(norm):
        raise ValueError("the model gave an empty vector")
    out = bytearray(len(values))
    for position, value in enumerate(values):
        number = int(round(value / norm * SCALE))
        out[position] = max(-SCALE, min(SCALE, number)) & 0xFF
    return bytes(out)


def vectors_path():
    """data/vectors.db."""
    return os.path.join(common.data_dir(), "vectors.db")


def _meta_rows(conn):
    return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}


def connect(path=None, create=True):
    """Open data/vectors.db (made when missing, unless create is False: then None).

    A file of another layout (VECTORS_VERSION) is emptied first.
    """
    path = path or vectors_path()
    if not create and not os.path.isfile(path):
        return None
    conn = db.connect(path, check_same_thread=False)
    try:
        for statement in _SCHEMA:
            conn.execute(statement)
        meta = _meta_rows(conn)
        if meta.get("version") != str(VECTORS_VERSION):
            with conn:
                conn.execute("DELETE FROM vectors")
                conn.execute("DELETE FROM meta")
                conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", (
                    ("version", str(VECTORS_VERSION)), ("epoch", secrets.token_hex(8)),
                    ("generation", "0"), ("model", ""), ("dims", "0"), ("native_dims", "0"),
                    ("served", ""), ("changed", ""), ("recipe", ""), ("probe", "")))
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def meta(conn):
    """{version, epoch, generation, model, dims, native_dims, served, changed, recipe,
    probe} of an open vectors.db (probe: [{"text", "vec" (base64)}], see PROBE_TEXTS)."""
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
            item.get("text"), str) and isinstance(item.get("vec"), str) for item in probe):
        probe = []
    return {"version": number("version"), "epoch": rows.get("epoch") or "",
            "generation": number("generation"), "model": rows.get("model") or "",
            "dims": number("dims"), "native_dims": number("native_dims"),
            "served": rows.get("served") or "", "changed": rows.get("changed") or "",
            "recipe": rows.get("recipe") or "", "probe": probe}


def read_meta():
    """meta() of data/vectors.db, or None when there is none (or it cannot be read)."""
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


def vector_size(current):
    """How many bytes the stored vectors hold: the dims they were cut to, or (dims 0:
    all of them) the model's own length; 0 while unknown. current is meta()."""
    if not current:
        return 0
    return current["dims"] or current["native_dims"]


def model_changed(current, native, served):
    """Why the stored vectors were made by another model than the one that answered
    now (another length, or another name from its server), or ''."""
    if not current:
        return ""
    if current["native_dims"] and native and native != current["native_dims"]:
        return "the model now gives %d numbers per text, not %d" % (native,
                                                                    current["native_dims"])
    if current["served"] and served and served != current["served"]:
        return "the endpoint now serves %s, not %s" % (served, current["served"])
    return ""


def _set_meta(conn, values):
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     [(key, str(value)) for key, value in values.items()])


def _bump(conn):
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")


def prepare_model(conn, model, dims, recipe_value=""):
    """Make the store hold vectors of this model, size and recipe (see recipe()): empties
    it when they differ.

    Returns True when vectors were thrown away.
    """
    current = meta(conn)
    if current["model"] == model and current["dims"] == dims and \
            current["recipe"] == recipe_value:
        return False
    return reset(conn, {"model": model, "dims": dims, "native_dims": 0, "served": "",
                        "changed": "", "recipe": recipe_value})


def note_model_changed(reason):
    """Mark every stored vector out of date: another model answers the endpoint now
    (a query noticed, see QueryEmbedder). The Manage page then offers to update
    them, and the next embed run makes them all again."""
    try:
        store = connect(create=False)
    except sqlite3.Error:
        return
    if store is None:
        return
    try:
        if meta(store)["changed"] != reason:
            with store:
                _set_meta(store, {"changed": reason})
                _bump(store)
    except sqlite3.Error:
        pass
    finally:
        store.close()


def reset(conn, values):
    """Empty the store and set meta values ({key: value}; the probe goes too); True when
    vectors were dropped."""
    with conn:
        dropped = conn.execute("DELETE FROM vectors").rowcount
        _set_meta(conn, dict({"probe": ""}, **values))
        _bump(conn)
    return dropped > 0


def stored(conn, library_ids=None):
    """{image id: (library id, text hash)} of the stored vectors (of some libraries)."""
    if library_ids is None:
        rows = conn.execute("SELECT image_id, library_id, text_hash FROM vectors")
    else:
        library_ids = list(library_ids)
        if not library_ids:
            return {}
        rows = conn.execute("SELECT image_id, library_id, text_hash FROM vectors "
                            "WHERE library_id IN (%s)" % ",".join("?" * len(library_ids)),
                            library_ids)
    return {row[0]: (row[1], row[2]) for row in rows}


def remove(conn, ids):
    """Delete the vectors of these image ids; returns how many there were."""
    ids = list(ids)
    count = 0
    with conn:
        for start in range(0, len(ids), ID_CHUNK):
            part = ids[start:start + ID_CHUNK]
            count += conn.execute("DELETE FROM vectors WHERE image_id IN (%s)"
                                  % ",".join("?" * len(part)), part).rowcount
        if count:
            _bump(conn)
    return count


def purge(store, conn):
    """Drop the vectors of pictures index.db no longer has (purged or deleted). Returns how many.

    Pictures that are only missing for now keep theirs (they may come back).
    """
    known = {row[0] for row in conn.execute("SELECT id FROM images")}
    gone = [image_id for image_id in stored(store) if image_id not in known]
    return remove(store, gone) if gone else 0


def forget(ids):
    """Drop the vectors of pictures whose rows were deleted (when data/vectors.db exists).

    For the code that deletes rows (a library's data, files missing too
    long), so no vector outlives its picture. Best effort; returns how
    many were dropped.
    """
    ids = list(ids)
    if not ids:
        return 0
    try:
        store = connect(create=False)
    except sqlite3.Error:
        return 0
    if store is None:
        return 0
    try:
        return remove(store, ids)
    except sqlite3.Error:
        return 0
    finally:
        store.close()


def write(store, rows, native_dims=None, served=None):
    """Store [(image id, library id, text hash, vector bytes)] in one transaction
    (with the model's own vector length and name, when given)."""
    now = time.time()
    with store:
        store.executemany("INSERT OR REPLACE INTO vectors (image_id, library_id, text_hash, vec, "
                          "updated) VALUES (?, ?, ?, ?, ?)",
                          [(image_id, library_id, digest, sqlite3.Binary(vec), now)
                           for image_id, library_id, digest, vec in rows])
        values = {}
        if native_dims:
            values["native_dims"] = native_dims
        if served:
            values["served"] = served
        if values:
            _set_meta(store, values)
        _bump(store)


# ---------------------------------------------------------------------------
# Status (the Manage page)

def counts(conn, config, library_ids, store=None, hashes=None):
    """{library id: {"pictures": with a search text, "embedded": with an up-to-date vector,
    "stale": with an older vector, "missing": without one, "held_back": hidden ones
    without an up-to-date vector that a cloud endpoint is never sent}} for some
    libraries.

    store is an open vectors.db (default: opened here, if it exists);
    hashes a TextHashes to use (it remembers what it worked out).
    """
    library_ids = list(library_ids)
    result = {library_id: {"pictures": 0, "embedded": 0, "stale": 0, "missing": 0,
                           "held_back": 0}
              for library_id in library_ids}
    options = settings(config)
    endpoint, _problem = endpoint_for(config, options)
    own = store is None
    if own:
        store = connect(create=False)
    try:
        have = stored(store) if store is not None else {}
        current = meta(store) if store is not None else None
    finally:
        if own and store is not None:
            store.close()
    model = model_key(endpoint, options) if endpoint is not None else (
        current["model"] if current else "")
    dims = options["dims"] if endpoint is not None else (current["dims"] if current else 0)
    try:
        made_with = recipe(config, options) if endpoint is not None else None
    except browser_model.ModelError:
        made_with = None        # (cannot be told now: the vectors count as they are)
    if made_with is None:
        made_with = current["recipe"] if current else ""
    usable = (current is not None and current["model"] == model and current["dims"] == dims
              and current["recipe"] == made_with and not current["changed"])
    cloud = endpoint is not None and not common.is_local_endpoint(endpoint)
    texts = (hashes or TextHashes()).current(conn, library_ids, model, dims)
    for image_id, (library_id, hidden, digest) in texts.items():
        entry = result[library_id]
        entry["pictures"] += 1
        found = have.get(image_id)
        if usable and found is not None and found[1] == digest:
            entry["embedded"] += 1
        elif hidden and cloud:
            entry["held_back"] += 1
        elif found is None:
            entry["missing"] += 1
        else:
            entry["stale"] += 1
    return result


# ---------------------------------------------------------------------------
# The embed step

def _new_summary():
    return {"configured": False, "embedded": 0, "failed": 0, "up_to_date": 0, "pending": 0,
            "skipped_hidden": 0, "removed": 0, "cut": 0, "tokens": 0, "seconds": 0.0,
            "stopped": False, "error": "", "model": "", "dims": 0}


class Recipe:
    """How the vectors of an endpoint are made: the settings, the Dense head (or None)
    and the model file ('' when not known), for vectors_of()."""

    def __init__(self, config, options, endpoint):
        self.options = options
        self.endpoint = endpoint
        self.dense = dense_head(options)        # (browser_model.ModelError)
        self.model_path = model_file(config, options, endpoint["name"])

    def extra(self):
        """What the request needs on top (the raw vectors, for a Dense head)."""
        return {"embd_normalize": -1} if self.dense is not None else None


def vectors_of(reply, made):
    """(the vectors of an llm.embed reply, ready to quantize, the model's own vector
    length, the served model's name). made is a Recipe.

    With a Dense head the replies must be raw (a llama.cpp server honours the
    request's embd_normalize): when every vector of the reply is of unit length
    the endpoint normalised them, and ProviderError says so. A llama.cpp server
    that names a GGUF file other than the model file the vectors are made
    with is refused too.
    """
    served = reply.get("model") or ""
    name = os.path.basename(made.model_path)
    if (name and served.lower().endswith(".gguf") and served.lower() != name.lower()):
        raise llm.ProviderError("endpoint", "it serves the model %s, but search by meaning is "
                                "set up for %s (embedding.model_file, or the model of its "
                                "model server): load that model, or change the setting"
                                % (served, name))
    vectors = reply["vectors"]
    if made.dense is None:
        return vectors, len(vectors[0]) if vectors else None, served
    if vectors and all(abs(math.sqrt(math.fsum(value * value for value in vector)) - 1.0)
                       <= NORMALISED for vector in vectors):
        raise llm.ProviderError("endpoint", "it answered with normalised vectors, but a Dense "
                                "head (embedding.dense_file) needs the raw ones: the endpoint "
                                "must be a llama.cpp server recent enough to honour the "
                                "request's embd_normalize")
    try:
        return [apply_dense(made.dense, vector) for vector in vectors], made.dense["out"], served
    except ValueError as error:
        raise llm.ProviderError("endpoint", str(error))


def _embed_texts(endpoint, options, texts, ctx, sleep, made=None):
    """llm.embed_with_retries for one batch, logging retries."""
    def on_retry(error, delay):
        ctx.log("Embedding: %s; trying again in %.0f s." % (error, delay), "warn")
    return llm.embed_with_retries(endpoint, texts, model=options["model"] or None,
                                  should_stop=ctx.should_stop, sleep=sleep, on_retry=on_retry,
                                  extra=made.extra() if made is not None else None)


def too_long(error):
    """True when an endpoint refused a text for being too long for the model."""
    text = str(error).lower()
    return any(marker in text for marker in TOO_LONG_MARKERS)


def shorter(text):
    """A text cut for a model whose context it does not fit: without the text read in
    the picture when it has some, else three quarters of it (at a word)."""
    lines = text.split("\n")
    if lines and lines[-1].startswith(OCR_LABEL) and len(lines) > 1:
        return "\n".join(lines[:-1])
    return _cut(text, max(1, len(text) * 3 // 4))


def signed_cosine(first, second):
    """The cosine of two stored vectors (signed bytes)."""
    a = [value - 256 if value > 127 else value for value in bytearray(first)]
    b = [value - 256 if value > 127 else value for value in bytearray(second)]
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def probe_changed(stored, probe):
    """Why the probe (PROBE_TEXTS embedded now: [{"text", "vec"}]) shows another model
    than the stored one, or ''."""
    old = {item["text"]: item["vec"] for item in stored or []}
    for item in probe:
        before = old.get(item["text"])
        if before is None:
            continue
        first, second = base64.b64decode(before), base64.b64decode(item["vec"])
        if len(first) != len(second):
            return "its vectors have another length"
        if signed_cosine(first, second) < PROBE_SAME:
            return "it embeds the same text differently now"
    return ""


def _newest_first(todo, limit):
    """todo sorted newest picture first, cut to limit (None: all of it)."""
    todo = sorted(todo, key=lambda item: -item[0])
    if limit is not None and len(todo) > max(0, limit):
        todo = todo[:max(0, limit)]
    return todo


def update(conn, config, ctx, library_ids=None, limit=None, servers=None, sleep=time.sleep,
           quick=False):
    """Embed the pictures whose vector is missing or stale. Returns a summary dict.

    conn is an index.db connection; ctx a jobs.JobContext. Only enabled
    libraries (limited to library_ids) are looked at; at most limit
    pictures are embedded (newest ids first). Nothing is asked of the
    endpoint (and no model server is started) when every vector is up to
    date. A model server that config.json's local_server runs for the
    endpoint is started when needed and stopped afterwards (auto_start,
    auto_stop; servers is the local_server.ServerManager). Vectors of
    pictures that are gone, or no longer have a description, are dropped.
    Hidden pictures are only sent to an endpoint on this computer or the
    local network (skipped_hidden). Each batch is committed, so a stopped
    run keeps what it did. Every run that embeds anything first embeds
    PROBE_TEXTS: when they (or the model's vector length or name, see
    model_changed) show that another model now serves the endpoint, every
    vector is dropped and every picture embedded again. A store without a
    probe yet gets one even when nothing else is to do (not in a quick
    step). When a file the vectors are made with (the model file, the Dense
    head) cannot be read, nothing is done (summary error): the vectors stay.

    quick is for the step at the end of other runs: an endpoint on this
    computer that no model server here manages, and that does not answer,
    is given UNREACHABLE_WAIT seconds and then left for a later run with
    one warning (summary error), instead of being asked again for minutes.

    Summary keys: configured, embedded, failed, up_to_date, pending,
    skipped_hidden, removed, cut (texts cut to fit the model), tokens,
    seconds, stopped, error, model, dims.
    """
    summary = _new_summary()
    options = settings(config)
    if not options["endpoint"]:
        return summary
    summary["configured"] = True
    endpoint, problem = endpoint_for(config, options)
    if endpoint is None:
        summary["error"] = problem
        ctx.log(problem, "warn")
        return summary
    model, dims = model_key(endpoint, options), options["dims"]
    summary.update(model=model, dims=dims)
    wanted = None if library_ids is None else set(library_ids)
    libraries = [library["id"] for library in common.enabled_libraries(config)
                 if library.get("id") and (wanted is None or library["id"] in wanted)]
    local = common.is_local_endpoint(endpoint)
    store = connect()
    try:
        try:
            made = Recipe(config, options, endpoint)
            recipe_value = recipe(config, options)
        except browser_model.ModelError as error:
            summary["error"] = str(error)
            ctx.log(summary["error"], "warn" if quick else "error")
            return summary
        if prepare_model(store, model, dims, recipe_value):
            ctx.log("The embedding model or size changed: every picture is embedded again.")
        changed = meta(store)["changed"]
        if changed:
            ctx.log("The model of the embedding endpoint %s changed (%s): every picture is "
                    "embedded again." % (endpoint["name"], changed), "warn")
            reset(store, {"native_dims": 0, "served": "", "changed": ""})
        summary["removed"] = purge(store, conn)
        have = stored(store)
        todo = []
        texts = library_texts(conn, libraries)
        with_text = {item[0] for item in texts}
        present = set()
        if libraries:
            present = {row[0] for row in conn.execute(
                "SELECT id FROM images WHERE missing_since IS NULL AND library_id IN (%s)"
                % ",".join("?" * len(libraries)), libraries)}
        # Pictures that lost their description lose their vector too.
        textless = [image_id for image_id in have if image_id in present
                    and image_id not in with_text]
        if textless:
            summary["removed"] += remove(store, textless)
        moved = []
        for image_id, library_id, hidden, text in texts:
            digest = text_hash(text, model, dims)
            found = have.get(image_id)
            if found is not None and found[1] == digest:
                summary["up_to_date"] += 1
                if found[0] != library_id:
                    moved.append((library_id, image_id))
                continue
            if hidden and not local:
                summary["skipped_hidden"] += 1
                continue
            todo.append((image_id, library_id, text, digest))
        if moved:
            with store:
                store.executemany("UPDATE vectors SET library_id = ? WHERE image_id = ?", moved)
        if summary["skipped_hidden"]:
            ctx.log("%d hidden picture(s) were not sent to %s: hidden pictures are only embedded "
                    "by endpoints on this computer or the local network."
                    % (summary["skipped_hidden"], common.url_host(endpoint["base_url"])), "warn")
        todo = _newest_first(todo, limit)
        summary["pending"] = len(todo)
        # (A store without a probe gets one, but not from the quick step: it
        # would wake the endpoint after every scan for two sentences.)
        if not todo and (quick or meta(store)["probe"] or not have):
            return summary

        def everything():
            # Every picture again (another model now serves the endpoint).
            return _newest_first([(image_id, library_id, text, text_hash(text, model, dims))
                                  for image_id, library_id, hidden, text in texts
                                  if local or not hidden], limit)
        _run(store, endpoint, options, todo, ctx, summary, config, servers, sleep, everything,
             quick, made)
    finally:
        store.close()
    return summary


def _unreachable(endpoint, config, ctx, sleep):
    """For the quick step: why an endpoint on this computer (that no model server here
    manages) cannot be used now, or '' (it answers, is loading, or is not checked)."""
    import local_server
    if (not common.is_local_endpoint(endpoint)
            or local_server.managed(config, endpoint["name"]) is not None):
        return ""
    info = llm.probe(endpoint)
    if info["reachable"] is not False or info["loading"]:
        return ""
    if UNREACHABLE_WAIT > 0:
        waited = llm.wait_until_ready(endpoint, UNREACHABLE_WAIT, should_stop=ctx.should_stop,
                                      sleep=sleep)
        if waited["ready"] or waited["loading"]:
            return ""
    return ("search by meaning was not updated: the embedding endpoint %s does not answer at "
            "%s. Is its server running? (Update meaning search on the Manage page, or the next "
            "scan, does it then.)" % (endpoint["name"], common.redact(endpoint["base_url"])))


def _run(store, endpoint, options, todo, ctx, summary, config, servers, sleep, everything,
         quick=False, made=None):
    """Embed todo [(image id, library id, text, hash)] in batches (see update).

    PROBE_TEXTS go first: when they, the model's vector length or its name
    show that another model serves the endpoint now, every picture is
    embedded again (everything() gives the todo of every picture).
    """
    import local_server     # stdlib only; imported here as describe.py does
    started = time.monotonic()
    ctx.stage("embed", total=len(todo))
    if todo:
        ctx.log("Embedding %d picture(s) for search by meaning (%s)..."
                % (len(todo), endpoint["name"]))
    lease = local_server.lease_for(ctx, config, [endpoint["name"]], manager=servers)
    failures = []
    try:
        problems = lease.acquire()
        if problems:
            summary["error"] = "; ".join(problems.values())
            ctx.log("Cannot embed: %s" % summary["error"], "warn" if quick else "error")
            return
        problem = _unreachable(endpoint, config, ctx, sleep) if quick else ""
        if problem:
            summary["error"] = problem[:1].upper() + problem[1:]
            ctx.log(summary["error"], "warn")
            return
        try:
            probe, native, served = _probe(endpoint, options, ctx, sleep, made)
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
        current = meta(store)
        changed = model_changed(current, native, served) or probe_changed(current["probe"],
                                                                          probe)
        if changed:
            ctx.log("The model of the embedding endpoint %s changed (%s): every picture "
                    "is embedded again." % (endpoint["name"], changed), "warn")
            reset(store, {"native_dims": native, "served": served})
            todo = everything()
            summary["pending"] = len(todo)
            ctx.set_total(summary["pending"])
        with store:
            _set_meta(store, {"probe": json.dumps(probe), "native_dims": native,
                              "served": served})
            _bump(store)
        position = 0
        while position < len(todo):
            batch = todo[position:position + options["batch"]]
            ctx.wait_if_paused()
            if ctx.should_stop():
                summary["stopped"] = True
                break
            try:
                rows, native, served = _embed_batch(endpoint, options, batch, ctx, summary,
                                                    failures, sleep, made)
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
        lease.release()
        summary["seconds"] = round(time.monotonic() - started, 3)
    for image_id, error in failures[:FAILURE_LOG_LIMIT]:
        ctx.log("Picture %d could not be embedded: %s" % (image_id, error), "warn")
    if len(failures) > FAILURE_LOG_LIMIT:
        ctx.log("... and %d more." % (len(failures) - FAILURE_LOG_LIMIT), "warn")
    if summary["embedded"] or summary["failed"]:
        rate = summary["embedded"] / summary["seconds"] if summary["seconds"] else 0.0
        ctx.log("Embedded %d picture(s) in %.1f s (%.1f a second)%s%s." % (
            summary["embedded"], summary["seconds"], rate,
            "; %d failed" % summary["failed"] if summary["failed"] else "",
            "; %d cut to fit the model" % summary["cut"] if summary["cut"] else ""))


def _probe(endpoint, options, ctx, sleep, made):
    """PROBE_TEXTS embedded as pictures are: ([{"text", "vec" (base64)}], the model's own
    vector length, its served name). Raises ProviderError, SizeError."""
    reply = _embed_texts(endpoint, options, list(PROBE_TEXTS), ctx, sleep, made)
    vectors, native, served = vectors_of(reply, made) if made is not None else (
        reply["vectors"], len(reply["vectors"][0]), reply.get("model") or "")
    probe = []
    for text, vector in zip(PROBE_TEXTS, vectors):
        try:
            data = quantize(vector, options["dims"])
        except SizeError:
            raise
        except ValueError as error:
            raise llm.ProviderError("content", str(error))
        probe.append({"text": text, "vec": base64.b64encode(data).decode("ascii")})
    return probe, native, served


def _per_text(error):
    """True when a failed request may be the fault of a text in it (too long, say), not of
    the endpoint: a 400, 413 or 422 reply, or a reply that holds no usable vectors."""
    return error.kind in ("content", "image") or (error.kind == "endpoint"
                                                  and error.status in (400, 413, 422))


def _embed_one(endpoint, options, item, ctx, summary, sleep, made):
    """The reply for one picture's text; a text too long for the model is cut and tried
    again (CUT_TRIES times at most, counted in the summary). Raises ProviderError."""
    text = item[2]
    for tries in range(CUT_TRIES + 1):
        try:
            reply = _embed_texts(endpoint, options, [text], ctx, sleep, made)
        except llm.ProviderError as error:
            if not (tries < CUT_TRIES and _per_text(error) and too_long(error)):
                raise
            text = shorter(text)
            continue
        if tries:
            summary["cut"] += 1
        return reply
    raise AssertionError("not reached")


def _embed_batch(endpoint, options, batch, ctx, summary, failures, sleep, made=None):
    """([(id, library id, hash, vector bytes)], native dims, served model name) for one
    batch.

    A batch the server refuses as a whole (one text too long for it, say)
    is tried again picture by picture (a text too long for the model is cut,
    see _embed_one); pictures that still fail are counted and left for a later
    run. Raises ProviderError when the endpoint cannot be used at all (or does
    not answer, or its replies do not fit the settings: see vectors_of),
    SizeError when its vectors are shorter than dims.
    """
    try:
        reply = _embed_texts(endpoint, options, [item[2] for item in batch], ctx, sleep, made)
        replies = [(batch, reply)]
    except llm.ProviderError as error:
        if not _per_text(error):
            raise
        replies = []
        for item in batch:
            if ctx.should_stop():
                break
            try:
                replies.append(([item], _embed_one(endpoint, options, item, ctx, summary, sleep,
                                                   made)))
            except llm.ProviderError as single:
                if not _per_text(single):
                    raise
                summary["failed"] += 1
                failures.append((item[0], str(single)))
    rows, native, served = [], None, ""
    for items, answer in replies:
        summary["tokens"] += answer.get("tokens") or 0
        vectors, size, name = vectors_of(answer, made) if made is not None else (
            answer["vectors"], None, answer.get("model") or "")
        served = name or served
        for item, vector in zip(items, vectors):
            native = size or len(vector)
            try:
                rows.append((item[0], item[1], item[3], quantize(vector, options["dims"])))
            except SizeError:
                raise
            except ValueError as error:
                summary["failed"] += 1
                failures.append((item[0], str(error)))
    return rows, native, served


def after_run(conn, config, ctx, library_ids=None, servers=None):
    """The embed step at the end of a scan, describe or refine run (when embedding is
    configured and the run was not stopped; update() with quick). Returns update()'s
    summary, or None."""
    if not configured(config) or ctx.should_stop():
        return None
    try:
        return update(conn, config, ctx, library_ids=library_ids, servers=servers, quick=True)
    except (sqlite3.Error, OSError) as error:
        ctx.log("Search by meaning could not be updated: %s" % common.redact(str(error)), "warn")
        return None


# ---------------------------------------------------------------------------
# Exports

def fresh_vectors(conn, library_id, image_ids, store=None, carried=None):
    """The up-to-date vectors of some pictures of a library, for an export.

    Only vectors made from the picture's current search text count (an
    edit or a new description since makes a vector stale: it is left out,
    so a vector never holds words the exported description does not), with
    the model and dims vectors.db holds. carried: {image id: the drive words
    (dvt, dvd, dvg) its exported record carries}: a clip's text holds those
    and no others (a vector made with a drive's words that the export does
    not publish, or on another clip, is stale). Returns {"model", "dims",
    "vectors": {image id: bytes}, "stale": how many had an older vector,
    "missing": how many had none}; vectors is empty when there is no
    vectors.db.
    """
    wanted = set(image_ids)
    result = {"model": "", "dims": 0, "vectors": {}, "stale": 0, "missing": 0}
    own = store is None
    if own:
        store = connect(create=False)
    if store is None:
        result["missing"] = len(wanted)
        return result
    try:
        current = meta(store)
        size = vector_size(current)
        result.update(model=current["model"], dims=size)
        have = {}
        ids = sorted(wanted)
        for start in range(0, len(ids), ID_CHUNK):
            part = ids[start:start + ID_CHUNK]
            for row in store.execute("SELECT image_id, text_hash, vec FROM vectors WHERE "
                                     "image_id IN (%s)" % ",".join("?" * len(part)), part):
                have[row[0]] = (row[1], bytes(row[2]))
    finally:
        if own:
            store.close()
    driven = None if carried is None else {
        image_id: drive_text(fields) for image_id, fields in carried.items() if fields}
    for image_id, _library, _hidden, text in library_texts(conn, [library_id], driven):
        if image_id not in wanted:
            continue
        found = have.get(image_id)
        if found is None:
            result["missing"] += 1
        elif (size and found[0] == text_hash(text, current["model"], current["dims"])
              and len(found[1]) == size):
            result["vectors"][image_id] = found[1]
        else:
            result["stale"] += 1
    return result


# ---------------------------------------------------------------------------
# The web server: query vectors, and the vectors of the pictures a caller sees

class QueryEmbedder:
    """Answers /api/embed: the vector of a query, the way pictures' vectors are made.

    The query prefix is put in front, the model's vector cut to dims and
    stored as for pictures. Answers are remembered per client
    (QUERY_CACHE_SIZE in all, most recently used: one client never learns
    what another searched for); the others are limited per client to
    QUERY_BURST at once, refilled at QUERY_RATE a second, as each may wake
    a model. An embeddings server that config.json's local_server manages
    is started when a query needs it (its auto_start), answering "starting"
    until it has loaded its model (see
    local_server.ServerManager.start_for_queries). Only the admin hears
    why search by meaning cannot be used; other computers get
    PUBLIC_MESSAGES (log, when given, gets the details). Thread safe.
    """

    def __init__(self, servers=None, config_loader=None, cache_size=QUERY_CACHE_SIZE,
                 burst=QUERY_BURST, rate=QUERY_RATE, clock=time.monotonic, log=None):
        self._servers = servers
        self._config_loader = config_loader
        self._lock = threading.Lock()
        self._cache = collections.OrderedDict()
        self._cache_size = cache_size
        self._buckets = collections.OrderedDict()   # client -> (tokens, when)
        self._burst = float(burst)
        self._rate = float(rate)
        self._clock = clock
        self._log = log
        self._logged = ""
        self.requests = 0       # how many queries were sent to the endpoint (tests)

    def _manager(self):
        if self._servers is None:
            import local_server
            self._servers = local_server.default_manager()
        return self._servers

    def allow(self, client, cost=1.0):
        """True when the client may have one more query (costing cost tokens) embedded
        now (token bucket)."""
        now = self._clock()
        with self._lock:
            tokens, when = self._buckets.pop(client, (self._burst, now))
            tokens = min(self._burst, tokens + max(0.0, now - when) * self._rate)
            allowed = tokens >= cost
            self._buckets[client] = (tokens - cost if allowed else tokens, now)
            while len(self._buckets) > 4096:
                self._buckets.popitem(last=False)
        return allowed

    def answer(self, config, text, client="", admin=False):
        """(HTTP status, JSON-friendly dict) for a query from client (its address).

        200 {"model", "dims", "vec": base64 of dims signed bytes}; 400 for
        an empty query; 404 {"state": "off", "error"} when search by
        meaning is not set up; 429 when the client asks too often; 503
        {"state": "starting" (ask again soon) | "off" | "unavailable",
        "error"} when there is no vector now. The error says why only to
        the admin. text may also be a list of texts (a query and its words,
        search-core.js meaningTexts), embedded in one call: then 200 also has
        "vecs", the vector of each in order ("vec" is the first's); 400 when
        one of them is empty or there are more than QUERY_TEXTS. Every
        QUERY_TEXTS_PER_TOKEN texts the model must embed count as one query
        against the client's QUERY_BURST.
        """
        status, payload = self._answer(config, text, client)
        state = payload.get("state")
        if status != 200 and state in PUBLIC_MESSAGES:
            detail = common.redact(payload.get("error") or "")
            if admin:
                payload["error"] = detail
            else:
                payload = {"state": state, "error": PUBLIC_MESSAGES[state]}
                if detail and state != "starting" and self._log is not None:
                    with self._lock:
                        fresh, self._logged = detail != self._logged, detail
                    if fresh:
                        self._log("Search by meaning: %s" % detail)
        return status, payload

    def _answer(self, config, text, client):
        options = settings(config)
        endpoint, problem = endpoint_for(config, options)
        if endpoint is None:
            return 404, {"state": "off", "error": problem}
        several = isinstance(text, (list, tuple))
        if several and len(text) > QUERY_TEXTS:
            return 400, {"error": "At most %d texts at once." % QUERY_TEXTS}
        texts = [" ".join((item or "").split())[:QUERY_CHARS]
                 for item in (text if several else [text])]
        if not texts or not all(texts):
            return 400, {"error": "There is nothing to look for."}
        model, dims = model_key(endpoint, options), options["dims"]
        try:
            how = Recipe(config, options, endpoint)
        except browser_model.ModelError as error:
            return 503, {"state": "unavailable", "error": str(error)}
        current = read_meta()
        made = (current["model"], current["dims"], current["native_dims"],
                current["served"], current["recipe"]) if current else None

        def key_of(item):
            return (client, model, dims, options["query_prefix"], made, item)

        known = {}
        with self._lock:
            for item in texts:
                found = self._cache.get(key_of(item))
                if found is not None:
                    self._cache.move_to_end(key_of(item))
                    known[item] = found

        def answer():
            vecs = [known[item][0] for item in texts]
            payload = {"model": model, "dims": known[texts[0]][1], "vec": vecs[0]}
            if several:
                payload["vecs"] = vecs
            return 200, payload

        missing = []
        for item in texts:
            if item not in known and item not in missing:
                missing.append(item)
        if not missing:
            return answer()
        if not self.allow(client, float(-(-len(missing) // QUERY_TEXTS_PER_TOKEN))):
            return 429, {"error": "Too many searches at once; try again in a moment."}
        servers = self._manager()
        state, message = servers.start_for_queries(config, endpoint["name"])
        if state == "starting" and self._config_loader is not None:
            # (Its daily stop time and idle_stop_minutes are looked after there.)
            servers.start_scheduler(self._config_loader)
        if state != "ready":
            return 503, {"state": state, "error": message}
        try:
            reply = llm.embed(endpoint, [options["query_prefix"] + item for item in missing],
                              model=options["model"] or None, timeout=QUERY_TIMEOUT,
                              extra=how.extra())
            vectors, native, served = vectors_of(reply, how)
            if len(vectors) != len(missing):
                raise ValueError("the endpoint answered with %d vectors for %d texts"
                                 % (len(vectors), len(missing)))
            quantized = [quantize(vector, dims) for vector in vectors]
        except llm.ProviderError as error:
            if error.status == 503:
                return 503, {"state": "starting", "error": "The embedding model is loading."}
            return 503, {"state": "unavailable", "error": llm.explain_error(error, endpoint)}
        except ValueError as error:
            return 503, {"state": "unavailable", "error": str(error)}
        servers.touch(endpoint["name"])
        if current is not None and current["model"] == model and current["dims"] == dims:
            changed = model_changed(current, native, served)
            if changed:
                note_model_changed(changed)
                return 503, {"state": "unavailable", "error": (
                    "The model of the embedding endpoint %s changed since the pictures were "
                    "embedded (%s): update search by meaning on the Manage page."
                    % (endpoint["name"], changed))}
        with self._lock:
            self.requests += 1
            for item, vector in zip(missing, quantized):
                known[item] = (base64.b64encode(vector).decode("ascii"), len(vector))
                self._cache[key_of(item)] = known[item]
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return answer()


def query_ready(config, store_meta):
    """True when queries can be embedded to match the stored vectors (same model, dims and
    recipe, and no other model behind the endpoint since: see note_model_changed)."""
    options = settings(config)
    endpoint, _problem = endpoint_for(config, options)
    if not (endpoint is not None and store_meta is not None
            and store_meta["model"] == model_key(endpoint, options)
            and store_meta["dims"] == options["dims"] and not store_meta["changed"]):
        return False
    try:
        return store_meta["recipe"] == recipe(config, options)
    except browser_model.ModelError:
        return False        # (the model file cannot be read now: see recipe)


class VectorService:
    """Answers /api/vectors: the vectors of the pictures a caller may see, as compact JSON
    {"version", "model", "dims", "query", "count", "ids": [...], "data": base64 of
    the vectors one after the other}, with a strong ETag, gzip and 304 (as
    search_index.IndexService).

    Hidden pictures' vectors are only sent with include_hidden (the admin),
    missing pictures' never, exactly like /api/index; and only vectors made
    from a picture's current search text (hashes, a TextHashes: after an
    edit, the old words are not searched by meaning any more, until the
    picture is embedded again). "query" says whether /api/embed can make
    query vectors that match these. The last few answers are kept
    compressed. Thread safe.
    """

    def __init__(self, memo_size=4):
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._memo = collections.OrderedDict()
        self._memo_size = memo_size
        self.hashes = TextHashes()
        self.builds = 0

    def respond(self, conn, config, libraries, include_hidden=False, accept_gzip=False,
                etag_matches=None, index_state=None):
        """(status, body bytes, headers) for the vectors of libraries ({id: name}).

        index_state is search_index.data_state() of the libraries (it
        changes when a picture is hidden, added, removed, described again
        or edited).
        """
        store = connect(create=False)
        try:
            current = meta(store) if store is not None else None
            ready = query_ready(config, current)
            key = [VECTORS_VERSION, current, ready, bool(include_hidden),
                   [[library_id, name] for library_id, name in libraries.items()], index_state]
            # (A drive's description changes the text its vector must match.)
            key.append(drives.version())
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
                    data = self._build(conn, store, current, ready, libraries, include_hidden)
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

    def _build(self, conn, store, current, ready, libraries, include_hidden):
        ids, chunks = [], []
        size = vector_size(current)
        library_ids = list(libraries)
        if store is not None and library_ids and size:
            texts = self.hashes.current(conn, library_ids, current["model"], current["dims"])
            for image_id, digest, vec in store.execute("SELECT image_id, text_hash, vec FROM "
                                                       "vectors ORDER BY image_id"):
                entry = texts.get(image_id)
                if (entry is not None and (include_hidden or not entry[1])
                        and entry[2] == digest and len(vec) == size):
                    ids.append(image_id)
                    chunks.append(bytes(vec))
        payload = {"version": VECTORS_VERSION, "model": current["model"] if current else "",
                   "dims": size, "query": bool(ready), "count": len(ids), "ids": ids,
                   "data": base64.b64encode(b"".join(chunks)).decode("ascii")}
        text = json.dumps(payload, separators=(",", ":"))
        return gzip.compress(text.encode("utf-8"), 6, mtime=0)
