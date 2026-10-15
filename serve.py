#!/usr/bin/env python3
"""Search the image database, and export a library as a static website.

Run with no arguments (or "serve") to start the interactive Flask server;
its options (--host, --port) work with or without "serve".
Run with "package" to export one library as a static website that searches
in the browser with no server (see export.py, which does the work).
"""

import argparse
import collections
import contextlib
import errno
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import socket
import sqlite3
import sys
import threading
import urllib.parse

import admin
import albums
import cache
import common
import db
import drives
import embeddings
import gps_check
import image_edits
import imageinfo
import jobs
import local_times
import looks
import opening_views
import people
import people_groups
import place_estimates
import raw_photos
import search_index
import street_maps
import tracks
import trip_stories
import trips
import videos
import world_map

TEMPLATE_DIR = os.path.join(common.BASE_DIR, "templates")
STATIC_DIR = os.path.join(common.BASE_DIR, "static")
FALLBACK_BASEMAP = os.path.join(STATIC_DIR, "worldmap.jpg")
TILE_SIZE = 256
# Without a tile pyramid the bundled basemap is sliced into zoom 0..3.
FALLBACK_MAX_ZOOM = 3
# Raise this when _fallback_tile_bytes cuts or encodes the tiles
# differently: an export then cuts its copies of them again.
FALLBACK_TILE_VERSION = 1
# No tile request above this zoom is ever answered, whatever the manifest says.
TILE_ZOOM_LIMIT = 20
# How long browsers may reuse thumbnails, previews and originals, and tiles.
CACHE_SECONDS = 3600
TILE_CACHE_SECONDS = 86400
# Originals with these extensions are converted to JPEG for browsers.
CONVERTED_EXTENSIONS = common.HEIC_EXTENSIONS + (".tif", ".tiff") + common.RAW_EXTENSIONS
# Provenance fields the detail page may show (who described an image, when).
PROVENANCE_FIELDS = ("model", "endpoint", "prompt_ver", "profile", "at")
# SQLite integers are 64-bit; larger ids in a URL simply do not exist.
MAX_IMAGE_ID = 2 ** 63 - 1
# Host names the server always answers to. Requests naming any other host are
# refused, so a web page cannot reach the server through DNS rebinding.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# Sent with every page: scripts and styles only from this server (the page
# settings are a JSON block, not a script), requests only to it (no other
# host is ever asked for anything: the street maps are its own files too),
# never shown inside a frame, no <base> that could move the scripts'
# relative addresses, forms only sent here, and no plugins.
CONTENT_SECURITY_POLICY = ("default-src 'self'; connect-src 'self'; "
                           "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
                           "object-src 'none'")
FACE_CROPS = 512            # face pictures (/face/<id>) kept in memory
# The name each page template goes by in the page settings ("page").
PAGE_NAMES = {"page.html": "search", "detail.html": "detail", "map.html": "map",
              "manage.html": "manage", "settings.html": "settings",
              "duplicates.html": "duplicates", "albums.html": "albums",
              "trips.html": "trips"}


def _images():
    """The images module, or None when Pillow is not installed.

    It is imported on first use so the server starts without Pillow; only
    previews and format conversion need it.
    """
    try:
        import images
    except (ImportError, SystemExit):
        return None
    return images


# --- Small shared helpers ----------------------------------------------------

def load_json(text, default=None):
    """Parse JSON stored in the database; NaN and Infinity become None.

    Empty or unreadable text gives default. The result can always be sent
    to a browser, whose JSON parser rejects NaN.
    """
    if not text:
        return default
    try:
        return json.loads(text, parse_constant=lambda _name: None)
    except (TypeError, ValueError):
        return default


def finite(value):
    """value if it is a finite number, else None."""
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def script_json(value):
    """JSON text that is safe to place inside an HTML <script> element.

    '<', '>' and '&' are written as \\u003c, \\u003e and \\u0026, so a value
    such as "</script><script>..." cannot end the element early. json.dumps
    already escapes every non-ASCII character (U+2028 and U+2029 included).
    """
    text = json.dumps(value, allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def provenance_record(text):
    """The public part of a row's provenance JSON: model, endpoint, prompt version, time."""
    data = load_json(text, None)
    if not isinstance(data, dict):
        return None
    record = {key: data[key] for key in PROVENANCE_FIELDS
              if isinstance(data.get(key), (str, int)) and not isinstance(data.get(key), bool)}
    return record or None


def visible_libraries(config):
    """{library id: display name} for the enabled libraries, in config order."""
    result = {}
    for library in common.enabled_libraries(config):
        library_id = library.get("id")
        if library_id:
            result[library_id] = common.library_label(library)
    return result


def library_roots(config):
    """{library id: folder} for the enabled libraries (see common.library_root)."""
    return {library["id"]: common.library_root(library)
            for library in common.enabled_libraries(config) if library.get("id")}


def trusted_hosts(bind_host="127.0.0.1"):
    """The host names (lower case) that requests may name in their Host header.

    Always the loopback names; also the address the server is bound to, and
    when bound to every interface (0.0.0.0 or ::), this machine's names and
    addresses.
    """
    hosts = set(LOCAL_HOSTS)
    host = (bind_host or "").strip().strip("[]").lower()
    if host in ("", "0.0.0.0", "::"):
        name = socket.gethostname()
        hosts.update({name.lower(), socket.getfqdn().lower()})
        try:
            hosts.update(str(info[4][0]).lower() for info in socket.getaddrinfo(name, None))
        except OSError:
            pass
    else:
        hosts.add(host)
    return hosts


def host_name(header):
    """The host part of a Host header: 'example.com:5000' -> 'example.com', '[::1]:80' -> '::1'."""
    header = (header or "").strip().lower()
    if header.startswith("["):
        end = header.find("]")
        return header[1:end] if end > 0 else ""
    if header.count(":") == 1:
        return header.split(":", 1)[0]
    return header


def sniff_file_mime(path, default="image/jpeg"):
    """The image type of a file judged from its first bytes (default if unknown)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError:
        return default
    return imageinfo.sniff_mime(head) or default


def load_helper_for_config(config=None):
    """Load the helper module configured in config.json, or None."""
    if config is None:
        config = common.load_config()
    return common.load_helper(config.get("helper", ""))


def disk_path(row, roots):
    """Full path of a row's original, or None if its library has no folder."""
    root = roots.get(row["library_id"])
    if not root:
        return None
    return os.path.join(root, *row["path"].split("/"))


def projection_of(row):
    """The row's 360-degree projection, or "" (also while it is unchecked)."""
    projection = row["projection"]
    return projection if projection in imageinfo.PROJECTIONS_360 else ""


def place_of(row, estimate=None):
    """The place names of a row (places.py) as /api/image sends them, or None.

    {"city" ("" when no town is near enough), "region", "country", "cc",
    "km" (how far the town is, when there is one)}. A picture without a
    place of its own has its estimate's (estimate: its place_estimates
    row, see place_estimates.py), with "source" ("copy", "time", "area",
    "owner" or "file") and "estimated": true (not for the owner's own place
    nor the place its file names). A picture with a position whose file
    names its place has those names, with "source" "file" and no "km" but
    for a town filled in from its position (place_estimates.gps_file_columns).
    """
    named = place_estimates.gps_file_columns(row)
    if named is not None:
        place = {"city": named["place_city"], "region": named["place_region"],
                 "country": named["place_country"], "cc": named["place_cc"],
                 "source": "file"}
        km = finite(named["place_km"])
        if place["city"] and km is not None and km >= 0:
            place["km"] = km
        return place
    if not row["place_country"]:
        if not place_estimates.usable(row, estimate) or not estimate["place_country"]:
            return None
        place = {"city": estimate["place_city"], "region": estimate["place_region"],
                 "country": estimate["place_country"], "cc": estimate["place_cc"],
                 "source": estimate["source"]}
        if estimate["source"] not in place_estimates.OWN_SOURCES:
            place["estimated"] = True
        return place
    place = {"city": row["place_city"], "region": row["place_region"],
             "country": row["place_country"], "cc": row["place_cc"]}
    km = finite(row["place_km"])
    if place["city"] and km is not None and km >= 0:
        place["km"] = km
    return place


def detail_record(row, library_name, edit=None, for_admin=False, marks=None, estimate=None):
    """Build the full metadata record used by the detail page.

    info is the description as shown and searched: the model's with the
    owner's edit (image_edits) laid over it. For the admin (for_admin)
    also edited, whether there is one, model_info, the model's own
    description, and edits, the edit itself (image_edits.view). marks
    are the owner's {"favorite", "rating", "albums": [{id, name}]}
    (albums.py), sent as they are. A 360 video's level track (which way
    was up in its panorama over time, see insta360.level_track) is sent
    once, as "level", while the "level_horizon" setting is on.
    estimate is the picture's place
    estimate (a place_estimates row, or None): its place when the picture
    has none of its own (place_of).
    """
    exif = load_json(row["exif"], {})
    model_info = load_json(row["info"], None)
    record = {
        "id": row["id"],
        "library": library_name,
        "library_id": row["library_id"],
        "path": row["path"],
        "name": row["name"],
        "size": row["size"],
        "mtime": finite(row["mtime"]),
        "ctime": finite(row["ctime"]),
        "width": row["width"],
        "height": row["height"],
        "projection": projection_of(row),
        "taken_at": row["taken_at"],
        "taken_source": row["taken_source"],
        "tz_offset": row["tz_offset"],
        "camera": row["camera"],
        "lat": finite(row["lat"]),
        "lon": finite(row["lon"]),
        "place": place_of(row, estimate),
        "hidden": bool(row["hidden"]),
        "info": image_edits.apply(model_info, edit),
        "described": bool(row["described"]),
        "prompt_ver": row["prompt_ver"],
        "exif": exif if isinstance(exif, dict) else {},
        "provenance": provenance_record(row["provenance"]),
    }
    if "media" in row.keys() and row["media"] == videos.MEDIA_VIDEO:
        # A video: its length and what ffprobe said (codec, frame rate, ...);
        # its kind is "video" whatever the description says.
        record["media"] = videos.MEDIA_VIDEO
        record["duration"] = finite(row["duration"])
        facts = record["exif"].get("video")
        facts = dict(facts) if isinstance(facts, dict) else {}
        # A 360 video's level track goes out once, as "level", and only
        # while levelling is on (the viewer turns each frame with it).
        level = videos.level_of(facts) if projection_of(row) else None
        facts.pop("level", None)
        if isinstance(record["exif"].get("video"), dict):
            record["exif"]["video"] = facts
        record["video"] = facts
        if level:
            record["level"] = level
        record["info"] = videos.effective_info(record["info"], row["media"])
    else:
        record["media"] = "image"
    if for_admin:
        record["edited"] = not image_edits.is_empty(edit)
        record["model_info"] = model_info
        record["edits"] = image_edits.view(edit)
    if marks is not None:
        record["marks"] = marks
    return record


def picture_marks_view(conn, image_id):
    """{"favorite", "rating", "albums": [{id, name}, by name]} of a picture (albums.py)."""
    favorite, rating, album_ids = albums.picture_marks(conn, image_id)
    names = []
    if album_ids:
        names = [{"id": row[0], "name": row[1]} for row in conn.execute(
            "SELECT id, name FROM albums WHERE id IN (%s)" % ",".join("?" * len(album_ids)),
            album_ids)]
        names.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return {"favorite": bool(favorite), "rating": rating, "albums": names}


# --- Map tiles ---------------------------------------------------------------

def tiles_max_zoom():
    """Return the highest pre-generated zoom level, or None if no tiles."""
    manifest = os.path.join(common.tiles_dir(), "manifest.json")
    if not os.path.isfile(manifest):
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("max_zoom"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def tiles_version():
    """A short text that changes whenever the tiles the tile route answers with may.

    The pages add it to the tiles' addresses (SITE.tileUrl: "?v=..."). Browsers
    keep a tile for a day (TILE_CACHE_SECONDS) under its address, and the same
    address answers with the bundled map's cut (darker, coarser, with a dotted
    15-degree grid) until make_tiles.py has made the tile pyramid and with the
    pyramid's tile after: without the version, a tile looked at before would
    stay the old one, a darker block among the new. Every make_tiles.py run
    writes the manifest again (a new modification time), and
    FALLBACK_TILE_VERSION covers a new cut of the bundled map.
    """
    try:
        stat = os.stat(os.path.join(common.tiles_dir(), "manifest.json"))
    except OSError:
        return "%d" % FALLBACK_TILE_VERSION
    return "%d-%x" % (FALLBACK_TILE_VERSION, stat.st_mtime_ns)


def tile_zoom_limit(max_zoom):
    """Highest zoom the tile route answers: the pyramid's, or the fallback's."""
    if max_zoom is None:
        return FALLBACK_MAX_ZOOM
    return min(max(max_zoom, FALLBACK_MAX_ZOOM), TILE_ZOOM_LIMIT)


_fallback_lock = threading.Lock()
_fallback_source = []       # [decoded basemap] once loaded
_fallback_tiles = {}        # (zoom, x, y) -> JPEG bytes, for zoom <= FALLBACK_MAX_ZOOM


def _fallback_basemap():
    """The bundled basemap, decoded once per process (call with the lock held)."""
    if not _fallback_source:
        try:
            from PIL import Image
        except ImportError:
            return None
        if not os.path.isfile(FALLBACK_BASEMAP):
            return None
        with Image.open(FALLBACK_BASEMAP) as source:
            _fallback_source.append(source.convert("RGB"))
    return _fallback_source[0]


def _fallback_tile_bytes(zoom, x, y):
    """Slice a tile out of the small bundled basemap.

    Used when the pre-generated tile pyramid is absent (or a tile is missing
    from it), so the map page still works without running make_tiles.py.
    Returns None when the tile is out of range or no basemap is available.
    """
    cols = 2 ** (zoom + 1)
    rows = 2 ** zoom
    if x < 0 or x >= cols or y < 0 or y >= rows:
        return None
    key = (zoom, x, y)
    with _fallback_lock:
        if key in _fallback_tiles:
            return _fallback_tiles[key]
        source = _fallback_basemap()
        if source is None:
            return None
        from PIL import Image
        src_w, src_h = source.size
        sx0 = int(round(x * src_w / cols))
        sx1 = int(round((x + 1) * src_w / cols))
        sy0 = int(round(y * src_h / rows))
        sy1 = int(round((y + 1) * src_h / rows))
        crop = source.crop((sx0, sy0, sx1, sy1))
        if crop.size != (TILE_SIZE, TILE_SIZE):
            crop = crop.resize((TILE_SIZE, TILE_SIZE), getattr(Image, "Resampling", Image).LANCZOS)
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=82)
        data = buffer.getvalue()
        # Only the small zoom 0..3 set is kept; deeper tiles are rarely needed.
        if zoom <= FALLBACK_MAX_ZOOM:
            _fallback_tiles[key] = data
        return data


# --- Pages -------------------------------------------------------------------

def render_template_file(name, mode, asset_prefix, data_prefix,
                         tiles_max=None, tile_prefix=None, libraries=None,
                         capabilities=None, extra=None):
    """Build a page from a template file, shared by server and static modes.

    The page settings go into the template's
    <script type="application/json" id="app-config"> block, which site.js
    and shell.js read (a JSON block, unlike an inline script, is allowed by
    the pages' Content-Security-Policy and also works from file://).

    libraries are the libraries for the page's library picker: {id: name}
    (or a list of names); by default, the enabled libraries in config.json.
    capabilities are {"admin": the Manage and Settings pages may be used,
    "static": a static export, ...}; admin is off unless given (and always
    off in static mode). extra holds more page settings (an export's
    "title"); it cannot replace the ones made here.
    """
    with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as handle:
        html = handle.read()
    if libraries is None:
        libraries = visible_libraries(common.load_config())
    if isinstance(libraries, dict):
        library_list = [{"id": library_id, "name": label}
                        for library_id, label in libraries.items()]
    else:
        library_list = [{"id": "", "name": label} for label in libraries]
    capabilities = dict(capabilities or {})
    capabilities["admin"] = bool(capabilities.get("admin")) and mode == "server"
    capabilities["static"] = mode != "server"
    app_config = {
        "mode": mode,
        "page": PAGE_NAMES.get(name, os.path.splitext(name)[0]),
        "libraries": [item["name"] for item in library_list],
        "libraryList": library_list,
        "capabilities": capabilities,
    }
    if mode == "server":
        app_config["api"] = "/api"
        app_config["tiles"] = "/tile"
        if capabilities["admin"]:
            app_config["adminApi"] = admin.URL_PREFIX
    else:
        app_config["data"] = data_prefix
        app_config["tiles"] = tile_prefix
    app_config["tilesMax"] = tiles_max
    app_config["tileSize"] = TILE_SIZE
    for key, value in (extra or {}).items():
        app_config.setdefault(key, value)
    html = html.replace("__ASSET_PREFIX__", asset_prefix)
    html = html.replace("__APP_CONFIG__", script_json(app_config))
    return html


# --- Interactive Flask server ------------------------------------------------

class ServerState:
    """What every request shares: the config, the helper and the stores.

    The config is re-read when config.json changes, so library renames and
    enable/disable show up without a restart. The thumbnail store keeps one
    connection that all request threads share. index answers /api/index
    (search_index.IndexService), vectors /api/vectors and embedder /api/embed
    (embeddings.VectorService, embeddings.QueryEmbedder), looks /api/looks and
    examples /api/looks/query (looks.LooksService, looks.ExampleEmbedder).
    helper is what requests read originals
    through: its load_image and stat hooks take turns with each other and
    with the jobs' (common.thread_safe_helper), as the helper module was
    promised; module is the helper module itself.
    """

    def __init__(self, helper=None):
        self.module = helper
        self.helper = common.thread_safe_helper(helper)
        self.previews = cache.PreviewStore()
        self.index = search_index.IndexService()
        self.vectors = embeddings.VectorService()
        self.embedder = embeddings.QueryEmbedder(config_loader=self.config, log=print)
        self.looks = looks.LooksService()
        self.examples = looks.ExampleEmbedder(config_loader=self.config)
        self.trips = trips.TripsService()
        self._lock = threading.Lock()
        self._config = None
        self._config_key = None
        self._thumbs = None
        self._pano_locks = {}       # image id -> [lock, users]

    @contextlib.contextmanager
    def pano_lock(self, image_id):
        """Held while making the pano of one image, so two requests do not both make it."""
        with self._lock:
            entry = self._pano_locks.setdefault(image_id, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if not entry[1]:
                    self._pano_locks.pop(image_id, None)

    def config(self):
        try:
            stat = os.stat(common.config_path())
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        with self._lock:
            if self._config is None or key != self._config_key:
                try:
                    self._config = common.load_config()
                except common.ConfigError as error:
                    if self._config is None:
                        raise
                    # Keep serving with the last good settings while the
                    # file is being edited.
                    print("Warning: %s; still using the previous settings." % error)
                self._config_key = key
            return self._config

    def thumbs(self):
        with self._lock:
            if self._thumbs is None:
                self._thumbs = cache.ThumbStore()
            return self._thumbs

    def close(self):
        self.index.close()
        with self._lock:
            if self._thumbs is not None:
                self._thumbs.close()
                self._thumbs = None


def _read_head(helper, path):
    """(first bytes, all bytes or None) of an original.

    When the helper supplies the bytes they are returned whole; otherwise
    only the start of the file is read, to recognise its format.
    """
    loader = getattr(helper, "load_image", None) if helper is not None else None
    if loader is not None:
        data = loader(path)
        if data is not None:
            data = bytes(data)
            return data[:64], data
    with open(path, "rb") as handle:
        return handle.read(64), None


def _source_etag(kind, image_id, stat, made_by=""):
    """An ETag for something derived from an original: changes with the file,
    and with made_by (what else the picture depends on, such as the way
    360-degree photos are stitched, images.stitch_key())."""
    text = "%s:%d:%d:%r:%s" % (kind, image_id, stat.st_size, stat.st_mtime, made_by)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:24]


def _pano_kind(row):
    """What a row's pano is called in messages."""
    return ("360-degree panorama" if row["projection"] in imageinfo.PROJECTIONS_360
            else "larger picture")


def _made_by(images, row):
    """What a preview or pano made from row's original depends on besides the
    file: the stitching, for an unstitched 360 photo ("" otherwise)."""
    if images is not None and row["projection"] == imageinfo.PROJECTION_DUAL_FISHEYE:
        return images.stitch_key()
    return ""


def build_app(init_db=True, bind_host="127.0.0.1", job_manager=None):
    """Create the Flask app.

    The database is prepared here, once (db.init), unless init_db is False
    because the caller already did it. Requests only open connections.
    bind_host is the address the server listens on; requests must name it,
    or a loopback name, as their host (see trusted_hosts). The admin pages
    and routes (admin.py) only work when bind_host is a loopback address.
    job_manager is the jobs.JobManager for the admin routes (default: a new
    one, which main() starts; until then jobs only queue up).
    """
    try:
        from flask import Flask, Response, g, jsonify, redirect, request, send_file
    except ImportError:
        raise SystemExit(
            "This tool needs Flask. Install it with:\n"
            "    python -m pip install Flask")

    if init_db:
        db.init()
    state = ServerState(load_helper_for_config())
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    app.extensions["search_images"] = state
    allowed_hosts = trusted_hosts(bind_host)
    admin_enabled = admin.is_loopback_host(bind_host)
    admin.init_app(app, state, job_manager if job_manager is not None else jobs.JobManager(),
                   allowed_hosts, enabled=admin_enabled)

    @app.before_request
    def check_host():
        # A page on another site that rebinds its DNS name to this machine
        # would send its own name here; such requests are refused.
        if host_name(request.host) not in allowed_hosts:
            return Response("Unknown host name.", status=400, mimetype="text/plain")
        return None

    def get_conn():
        conn = g.get("db_conn")
        if conn is None:
            conn = g.db_conn = db.connect()
        return conn

    @app.after_request
    def page_headers(response):
        if response.mimetype == "text/html":
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.teardown_appcontext
    def close_conn(_error):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    def private_cache(response, seconds=CACHE_SECONDS):
        response.cache_control.public = False
        response.cache_control.private = True
        response.cache_control.max_age = seconds
        return response

    def not_found():
        return Response(status=404)

    def not_modified(etag):
        response = Response(status=304)
        response.set_etag(etag)
        return private_cache(response)

    def send_bytes(data, mimetype, etag):
        response = Response(data, mimetype=mimetype)
        response.set_etag(etag)
        private_cache(response)
        return response.make_conditional(request)

    def is_admin():
        return admin.admin_allowed(request, admin_enabled)

    def visible_row(image_id, columns):
        """The row (columns plus library_id) if the image may be shown, else None.

        Missing images, and those of disabled or unknown libraries, are
        treated as if they did not exist; so are hidden images, except for
        the admin (who must see them to unhide them).
        """
        if image_id > MAX_IMAGE_ID:
            return None
        names = visible_libraries(state.config())
        if columns != "*":
            columns = "library_id, " + columns
        row = get_conn().execute(
            "SELECT %s FROM images WHERE id = ? AND missing_since IS NULL%s"
            % (columns, "" if is_admin() else " AND hidden = 0"),
            (image_id,)).fetchone()
        if row is None or row["library_id"] not in names:
            return None
        return row

    def original_path(row):
        """(full path, stat) of a row's original, or (None, None) if unreachable."""
        full = disk_path(row, library_roots(state.config()))
        stat = common.stat_path(state.helper, full) if full else None
        if stat is None:
            return None, None
        return full, stat

    def people_shown():
        """True when the pages show people (names, the People page) to this requester:
        People is on or there is face data, and the requester may see it (the admin;
        others only with "visitors": true)."""
        config = state.config()
        return people_groups.may_see(config, is_admin()) and (
            people.enabled(config) or os.path.isfile(people.store_path()))

    def page(name, extra=None):
        capabilities = {"admin": is_admin()}
        if not trips.settings(state.config())["enabled"]:
            capabilities["trips"] = False   # (no Trips link: see SITE.tripsUrl)
        if not search_index.plain_language(state.config()):
            capabilities["plain"] = False   # (no plain-language search: app.js plainOn)
        if people_shown():
            capabilities["people"] = True
        streets = street_maps.page_info()
        if streets is not None:
            # (The street maps, street_maps.py: /maps/index.js and the tiles.)
            extra = dict(extra or {}, streets=streets)
        # (The world tiles' version, in their addresses: tiles_version.)
        extra = dict(extra or {}, tilesVersion=tiles_version())
        return render_template_file(name, "server", "/static/", None,
                                    tiles_max=tiles_max_zoom(),
                                    libraries=visible_libraries(state.config()),
                                    capabilities=capabilities, extra=extra)

    def admin_page(name):
        if not is_admin():
            return Response("Manage, Settings and Duplicates only work on this computer, when "
                            "the server listens on localhost.", status=403,
                            mimetype="text/plain")
        # The query string opens dialogs (?dialog=...). A link on another
        # site gets the bare page, so it can never start anything there.
        if request.query_string and request.headers.get("Sec-Fetch-Site") in ("cross-site",
                                                                              "same-site"):
            return redirect(request.path, code=303)
        return page(name)

    @app.route("/")
    def index():
        return page("page.html")

    @app.route("/detail")
    def detail():
        return page("detail.html")

    @app.route("/map")
    def map_page():
        return page("map.html")

    @app.route("/manage")
    def manage_page():
        return admin_page("manage.html")

    @app.route("/settings")
    def settings_page():
        return admin_page("settings.html")

    @app.route("/duplicates")
    def duplicates_page():
        return admin_page("duplicates.html")

    @app.route("/albums")
    def albums_page():
        # Everyone may look at the albums; only the admin changes them.
        return page("albums.html")

    @app.route("/trips")
    def trips_page():
        # The trips (trips.py); everyone may look, only the admin changes them.
        return page("trips.html")

    @app.route("/trip/<trip_id>")
    def trip_page(trip_id):
        # One trip's page; the page asks /api/trips and says when there is no such trip.
        if not trips.ID_RE.match(trip_id):
            return not_found()
        return page("trips.html", extra={"trip": trip_id})

    @app.route("/api/trips")
    def api_trips():
        """{"enabled", "best_per_day", "trips": [...]} of the enabled libraries (see
        trips.view): visitors get no hidden trip and no hidden picture."""
        status, body, headers = state.trips.respond(
            get_conn(), state.config(), visible_libraries(state.config()), admin=is_admin(),
            etag_matches=request.if_none_match.contains)
        return Response(body, status=status, headers=headers, mimetype="application/json")

    @app.route("/api/trips/<trip_id>/story")
    def api_trip_story(trip_id):
        """A trip's story (trip_stories.story_view): visitors get none of a hidden trip or
        a hidden story, and no sentence that names people unless People is shown to
        them, nor one linking a hidden picture; the admin also its state."""
        if not trips.ID_RE.match(trip_id):
            return not_found()
        try:
            story = trip_stories.story_view(get_conn(), state.config(),
                                            list(visible_libraries(state.config())), trip_id,
                                            admin=is_admin())
        except trips.TripError:
            return not_found()
        response = jsonify(story)
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    @app.route("/people")
    def people_page():
        # The people the faces show (people_groups.py): the admin's, and with "visitors":
        # true in config.json's "people", others may look at the named ones.
        if not people_shown():
            return Response("There is no People page here.", status=404, mimetype="text/plain")
        return page("people.html")

    def open_faces():
        """data/faces.db for reading, or None (none, or it cannot be read)."""
        try:
            return people.connect(create=False)
        except Exception:
            return None

    @app.route("/api/people")
    def api_people():
        """The people of the enabled libraries' pictures (people_groups.search_data), for
        person: and the People page; ?hidden=1 (the admin's) lists hidden people too.
        Nobody else gets anything unless config.json's "people" says "visitors": true."""
        config = state.config()
        admin = is_admin()
        if not people_groups.may_see(config, admin):
            response = jsonify({"error": "People is not shown here."})
            response.status_code = 404
            return response
        if not same_origin():
            # (Names are for this server's own pages only.)
            response = jsonify({"error": "Requests from other sites are not allowed."})
            response.status_code = 403
            return response
        names = visible_libraries(config)
        store = open_faces()
        try:
            hidden = admin and request.args.get("hidden") == "1"
            key = json.dumps([people_groups.state_key(store),
                              search_index.data_state(get_conn(), list(names)),
                              list(names), admin, hidden, people.settings(config)["visitors"]])
            etag = hashlib.sha1(key.encode("utf-8")).hexdigest()[:32]
            if request.if_none_match.contains(etag):
                response = Response(status=304)
            else:
                response = jsonify(people_groups.search_data(
                    store, get_conn(), config, list(names), admin, include_hidden_people=hidden))
            response.set_etag(etag)
        finally:
            if store is not None:
                store.close()
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    face_crops = collections.OrderedDict()      # ETag -> JPEG bytes (the last FACE_CROPS)
    face_lock = threading.Lock()

    def face_response(response):
        # (Asked again every time, cheaply, by its ETag: a face id names another face
        # after Delete face data, and a person may be hidden or unnamed since.)
        response.cache_control.public = False
        response.cache_control.private = True
        response.cache_control.no_cache = True
        response.cache_control.max_age = None
        return response

    @app.route("/face/<int:face_id>")
    def face_picture(face_id):
        """A square picture of one face (people_groups.crop), cut from its picture's
        preview (or the one kept of a face found in full size, people.stored_crop):
        for the admin, or (with "visitors": true) a named person's face in a picture
        the requester may see. Never kept by the browser without asking."""
        config = state.config()
        admin = is_admin()
        # (Faces only show on this server's own pages, never in another site's.)
        if not people_groups.may_see(config, admin) or face_id > MAX_IMAGE_ID or \
                not same_origin():
            return not_found()
        store = open_faces()
        if store is None:
            return not_found()
        try:
            face, row = people_groups.face_source(store, get_conn(), face_id,
                                                  list(visible_libraries(config)), admin)
            epoch = people_groups.state_key(store)[:1]
            # (A face found in full size has a picture of its own: the preview is
            # too small for it.)
            kept = people.stored_crop(store, face_id) if face is not None else None
        finally:
            store.close()
        if face is None:
            return not_found()
        if kept is not None:
            etag = hashlib.sha1(json.dumps([epoch, face_id, "kept", kept[1], len(kept[0])])
                                .encode("utf-8")).hexdigest()[:24]
            response = Response(kept[0], mimetype="image/jpeg")
            response.set_etag(etag)
            return face_response(response).make_conditional(request)
        path = None
        if row["has_preview"] == 1:
            try:
                path = state.previews.path_for(row["library_id"], row["id"])
            except ValueError:
                path = None
        try:
            stat = os.stat(path) if path else None
        except OSError:
            stat = None
        box = (face["x"], face["y"], face["w"], face["h"])
        etag = hashlib.sha1(json.dumps([epoch, face_id, box, row["id"],
                                        stat.st_size if stat else 0,
                                        stat.st_mtime_ns if stat else 0]).encode(
                                            "utf-8")).hexdigest()[:24]
        if request.if_none_match.contains(etag):
            return face_response(not_modified(etag))
        with face_lock:
            data = face_crops.get(etag)
        if data is None:
            try:
                source = None
                if stat is not None:
                    with open(path, "rb") as handle:
                        source = handle.read()
                else:
                    source = state.thumbs().get(row["id"])
                if source is None:
                    return not_found()
                data = people_groups.crop(source, box)
            except Exception:
                return not_found()
            with face_lock:
                face_crops[etag] = data
                while len(face_crops) > FACE_CROPS:
                    face_crops.popitem(last=False)
        response = Response(data, mimetype="image/jpeg")
        response.set_etag(etag)
        return face_response(response).make_conditional(request)

    @app.route("/api/locations")
    def api_locations():
        """[{id, lat, lon}] of the located pictures, then the estimated positions
        (place_estimates.located) with "e": 1 and "r": their radius in km (the map's
        hollow pins), and the owner's own places with "o": 1 (and "r" too when known: an
        estimate accepted, a town chosen)."""
        library_ids = list(visible_libraries(state.config()))
        rows = world_map.collect_located_images(get_conn(), library_ids)
        points = [{"id": i, "lat": lat, "lon": lon} for i, lat, lon in rows]
        for image_id, lat, lon, radius, owner in place_estimates.located(get_conn(),
                                                                        library_ids):
            point = {"id": image_id, "lat": lat, "lon": lon}
            point["o" if owner else "e"] = 1
            if isinstance(radius, (int, float)) and math.isfinite(radius) and radius > 0:
                point["r"] = round(radius, 3)
            points.append(point)
        return jsonify(points)

    @app.route("/api/routes")
    def api_routes():
        """{"routes": the routes of the drives (and other videos with a GPS track) of the
        enabled libraries, their hidden clips left out (tracks.routes)}."""
        config = state.config()
        return jsonify({"routes": tracks.routes(get_conn(), config,
                                                list(visible_libraries(config)))})

    @app.route("/tile/<int:zoom>/<int:x>/<int:y>.jpg")
    def tile(zoom, x, y):
        max_zoom = tiles_max_zoom()
        # Checked before any work: 2 ** zoom of a huge zoom costs memory.
        if zoom > tile_zoom_limit(max_zoom):
            return not_found()
        if x >= 2 ** (zoom + 1) or y >= 2 ** zoom:
            return not_found()
        local = os.path.join(common.tiles_dir(), str(zoom), str(x), "%d.jpg" % y)
        if os.path.isfile(local):
            return send_file(local, mimetype="image/jpeg",
                             max_age=TILE_CACHE_SECONDS)
        data = _fallback_tile_bytes(zoom, x, y)
        if data is None:
            return not_found()
        response = Response(data, mimetype="image/jpeg")
        response.cache_control.max_age = TILE_CACHE_SECONDS
        return response

    @app.route("/maps/index.js")
    def street_maps_index():
        # The street map tiles there are (street_maps.py), as the script an export
        # writes too; 404 without street maps.
        text, version = street_maps.shared.index_script()
        if text is None:
            return not_found()
        return send_bytes(text, "text/javascript", "streets-" + version)

    @app.route("/maps/LICENSE.txt")
    def street_maps_licence():
        # The street maps' licence (OpenStreetMap, ODbL), which the maps' credit links
        # to (an export carries the same file).
        return send_bytes(street_maps.LICENSE_TEXT.encode("ascii"), "text/plain",
                          "streets-licence-" + hashlib.sha1(
                              street_maps.LICENSE_TEXT.encode("ascii")).hexdigest()[:10])

    @app.route("/maps/<int:zoom>/<int:x>/<int:y>.js")
    def street_maps_tile(zoom, x, y):
        # One street map tile, as a script (street_maps.tile_script); 404 when
        # there is none.
        if zoom > street_maps.RANGES["max_zoom"][1] or x >= 2 ** zoom or y >= 2 ** zoom:
            return not_found()
        data, version = street_maps.shared.tile(zoom, x, y)
        if data is None:
            return not_found()
        return send_bytes(street_maps.tile_script(zoom, x, y, data), "text/javascript",
                          "streets-%s-%d-%d-%d" % (version, zoom, x, y))

    @app.route("/api/index")
    def api_index():
        # ?lib=<library id> or all (the default): see search_index for the records.
        config = state.config()
        names = visible_libraries(config)
        wanted = request.args.get("lib", "all")
        if wanted not in ("", "all"):
            if wanted not in names:
                return jsonify({"error": "There is no enabled library with that id."}), 404
            names = {wanted: names[wanted]}
        status, body, headers = state.index.respond(
            get_conn(), config, names, include_hidden=is_admin(),
            accept_gzip=request.accept_encodings.quality("gzip") > 0,
            etag_matches=request.if_none_match.contains_weak)
        return Response(body, status=status, headers=headers, mimetype="application/json")

    @app.route("/api/vectors")
    def api_vectors():
        # ?lib=<library id> or all: the meaning vectors of the pictures /api/index lists.
        config = state.config()
        names = visible_libraries(config)
        wanted = request.args.get("lib", "all")
        if wanted not in ("", "all"):
            if wanted not in names:
                return jsonify({"error": "There is no enabled library with that id."}), 404
            names = {wanted: names[wanted]}
        conn = get_conn()
        status, body, headers = state.vectors.respond(
            conn, config, names, include_hidden=is_admin(),
            accept_gzip=request.accept_encodings.quality("gzip") > 0,
            etag_matches=request.if_none_match.contains_weak,
            index_state=state.index.state(conn, list(names)))
        return Response(body, status=status, headers=headers, mimetype="application/json")

    @app.route("/api/looks")
    def api_looks():
        # ?lib=<library id> or all: the image vectors ("Looks like this") of the
        # pictures /api/index lists.
        config = state.config()
        names = visible_libraries(config)
        wanted = request.args.get("lib", "all")
        if wanted not in ("", "all"):
            if wanted not in names:
                return jsonify({"error": "There is no enabled library with that id."}), 404
            names = {wanted: names[wanted]}
        conn = get_conn()
        status, body, headers = state.looks.respond(
            conn, config, names, include_hidden=is_admin(),
            accept_gzip=request.accept_encodings.quality("gzip") > 0,
            etag_matches=request.if_none_match.contains_weak,
            index_state=state.index.state(conn, list(names)))
        return Response(body, status=status, headers=headers, mimetype="application/json")

    @app.route("/api/looks/query", methods=["POST"])
    def api_looks_query():
        # The body is a picture (JPEG, PNG, WebP, HEIC...): its look vector, so
        # the page can find the pictures that look like it (looks.ExampleEmbedder).
        # The admin's only, with the admin routes' checks: it may wake a model.
        problem = ""
        if not is_admin():
            problem = "Search by an example picture only works on this computer."
        elif request.headers.get(admin.HEADER) != "1":
            problem = "This request needs the %s header." % admin.HEADER
        elif not same_origin():
            problem = "Requests from other sites are not allowed."
        if problem:
            response = jsonify({"error": problem})
            response.status_code = 403
        elif (request.content_length or 0) > looks.QUERY_MAX_BYTES:
            response = jsonify({"error": "The picture is larger than %d MB."
                                % (looks.QUERY_MAX_BYTES // (1024 * 1024))})
            response.status_code = 413
        else:
            data = request.stream.read(looks.QUERY_MAX_BYTES + 1)
            status, payload = state.examples.answer(state.config(), data,
                                                    client=request.remote_addr or "")
            response = jsonify(payload)
            response.status_code = status
            if status == 429:
                response.headers["Retry-After"] = "2"
        response.headers["Cache-Control"] = "no-store"
        return response

    def same_origin():
        """False for a request another site's page made (it may start a model server)."""
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return False
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        try:
            parts = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return (parts.scheme in ("http", "https")
                and parts.netloc.lower() == request.host.lower())

    @app.route("/api/embed")
    def api_embed():
        # ?q=<what was typed>: its meaning vector (see embeddings.QueryEmbedder);
        # several q= (a query and its words): their vectors in one answer.
        # Only for this server's own pages: it may wake a model.
        if not same_origin():
            response = jsonify({"error": "Requests from other sites are not allowed."})
            response.status_code = 403
        else:
            texts = request.args.getlist("q")
            status, payload = state.embedder.answer(state.config(),
                                                    texts if len(texts) > 1
                                                    else request.args.get("q", ""),
                                                    client=request.remote_addr or "",
                                                    admin=is_admin())
            response = jsonify(payload)
            response.status_code = status
            if status == 429:
                response.headers["Retry-After"] = "2"
        # (What was typed is kept by the page itself, never by the browser's cache.)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/image/<int:image_id>")
    def api_image(image_id):
        row = visible_row(image_id, "*")
        if row is None:
            return jsonify({"error": "not found"}), 404
        names = visible_libraries(state.config())
        edit = image_edits.load(get_conn(), image_id)
        estimate = place_estimates.for_picture(get_conn(), image_id)
        record = detail_record(row, names[row["library_id"]], edit, for_admin=is_admin(),
                               marks=picture_marks_view(get_conn(), image_id),
                               estimate=estimate)
        # Where an estimated place comes from (place_estimates.view: the admin
        # also gets its position, the pictures it came from and the choice).
        record["place_estimate"] = place_estimates.view(get_conn(), row, estimate,
                                                        admin=is_admin())
        # The camera's own clock, when the time is the local time of the place
        # (local_times.py).
        clock = local_times.view(get_conn(), row, admin=is_admin())
        if clock is not None:
            record["clock"] = clock
        # A camera RAW file (raw_photos.view): the picture's RAW original, or the
        # RAW file the picture is (shown by its preview). Only the owner may
        # download it (/raw/<id>): it holds every fact the camera wrote.
        raw = raw_photos.view(record["exif"], row["name"], row["size"])
        record["exif"].pop("raw", None)
        if raw is not None:
            if is_admin():
                raw["url"] = "/raw/%d" % row["id"]
            record["raw"] = raw
        if is_admin():
            # The GPS check of the picture: why its position was set aside.
            record["location_check"] = gps_check.picture_view(get_conn(), row)
        elif row["lat"] is None and gps_check.is_ignored(get_conn(), image_id):
            # A position set aside is nobody else's business, not even raw
            # (nor the place names its XMP gives: often made from it).
            record["exif"].pop("GPS", None)
            record["exif"].pop("XMPPlace", None)
        # Who is in it (people_groups.picture_view): the names, and the admin also the
        # boxes of its faces. Nobody else gets them unless People's "visitors" says so.
        found = None
        if people_groups.may_see(state.config(), is_admin()):
            store = open_faces()
            if store is not None:
                try:
                    found = people_groups.picture_view(store, get_conn(), row, state.config(),
                                                       is_admin())
                except sqlite3.Error:
                    found = None
                finally:
                    store.close()
        if found is not None:
            record["people"] = found
        if record["projection"]:
            # Where its 360 view opens (opening_views.py; the admin is also
            # told whether it is the one set with "Open here").
            record["opening_view"] = opening_views.detail(get_conn(), image_id,
                                                          row["content_hash"], owner=is_admin())
        if record["media"] == videos.MEDIA_VIDEO:
            # /video/<id> sends a playable (H.264) copy when there is one
            # (for a 360 video also its LRV, or an original browsers play).
            record["playable_copy"] = bool(videos.existing_proxy(
                row["library_id"], row["id"], row["content_hash"]))
            if not record["playable_copy"] and row["projection"] == videos.PROJECTION_360:
                record["playable_copy"] = videos.playable_360(
                    row, original_path(row)[0])[0] is not None
            # Its GPS track (tracks.py): whether its position is the track's.
            record["track"] = track_view(row)
            # (A drive never holds 360 videos: drives.clip_from_row leaves them out.)
            preview = drive_preview(row)
            if preview is not None:
                # A clip of a drive: its sped-up preview (/drive-preview/<id>),
                # and which of its clips the caller sees have a playable copy.
                clips = [clip for clip in preview[0].clips if is_admin() or not clip.hidden]
                record["drive"] = {
                    "clips": len(clips), "preview": bool(preview[1]), "speed": preview[2],
                    "copies": [clip.id for clip in clips if videos.existing_proxy(
                        row["library_id"], clip.id, clip.content_hash)]}
        return jsonify(record)

    def track_view(row):
        """{"fixes", "start", "end" (UTC, ISO), "position" (its position is the track's)} of
        a video's kept GPS track, or None."""
        found = tracks.for_rows(get_conn(), [row], state.config()).get(row["id"])
        if found is None:
            return None
        return {"fixes": found.fixes, "start": tracks.iso_utc(found.start),
                "end": tracks.iso_utc(found.end),
                "position": tracks.is_position(found, row["lat"], row["lon"])}

    def drive_preview(row):
        """(Drive, path of its sped-up preview or None, speed) of a video row in a drive,
        or None. Visitors get no preview of a drive with a hidden clip."""
        config = state.config()
        drive = drives.drive_of(get_conn(), row["library_id"], row["id"], config)
        if drive is None:
            return None
        path, speed = drives.existing_preview(row["library_id"], drive, config)
        if path is None or (drive.hidden and not is_admin()):
            return drive, None, None
        return drive, path, speed

    @app.route("/drive-preview/<int:image_id>")
    def drive_preview_file(image_id):
        # The sped-up preview of the drive a clip belongs to (drives.make_previews).
        row = visible_row(image_id, "id, media")
        if row is None or row["media"] != videos.MEDIA_VIDEO:
            return not_found()
        found = drive_preview(row)
        if found is None or found[1] is None:
            return not_found()
        drive, path = found[0], found[1]
        try:
            response = send_file(path, mimetype="video/mp4", conditional=True,
                                 etag=_source_etag("drive-preview", drive.key, os.stat(path)),
                                 max_age=CACHE_SECONDS)
        except OSError:
            return not_found()
        response.headers["Accept-Ranges"] = "bytes"
        return private_cache(response)

    @app.route("/thumb/<int:image_id>")
    def thumb(image_id):
        row = visible_row(image_id, "id")
        data = state.thumbs().get(image_id) if row is not None else None
        if data is None:
            return not_found()
        return send_bytes(data, "image/jpeg", hashlib.sha1(data).hexdigest()[:24])

    @app.route("/preview/<int:image_id>")
    def preview(image_id):
        row = visible_row(image_id, "id, path, name, has_preview, projection, media")
        if row is None:
            return not_found()
        path = None
        if row["has_preview"] == 1:
            # Only a preview the scanner vouched for: a leftover file may
            # still show an older picture.
            try:
                path = state.previews.path_for(row["library_id"], row["id"])
            except ValueError:
                path = None
        if path and os.path.isfile(path):
            return private_cache(send_file(path, mimetype=sniff_file_mime(path),
                                           max_age=CACHE_SECONDS))
        # No stored preview (not built yet, or turned off for the library):
        # make one from the original.
        full, stat = original_path(row)
        if full is None:
            return not_found()
        images = _images()
        etag = _source_etag("preview", row["id"], stat, _made_by(images, row))
        if request.if_none_match.contains(etag):
            return not_modified(etag)
        if images is None:
            return not_found()
        if row["media"] == videos.MEDIA_VIDEO:
            # A video's preview is its poster frame (ffmpeg); a 360 video's
            # is turned level with the track the scan kept (reading it from
            # the file again takes a while).
            level = None
            if row["projection"] == imageinfo.PROJECTION_DUAL_FISHEYE:
                kept = get_conn().execute("SELECT exif FROM images WHERE id = ?",
                                          (row["id"],)).fetchone()
                level = videos.stored_level(kept[0]) if kept is not None else None
            try:
                data = videos.poster_jpeg(videos.find_tools(state.config()), full, level=level)
            except (videos.VideoReadError, images.ImageReadError) as error:
                print("Could not make a poster of video %d: %s" % (row["id"], error))
                return not_found()
            return send_bytes(data, "image/jpeg", etag)
        try:
            data = images.read_image(full, state.helper, mtime=stat.st_mtime)["preview"]
        except images.ImageReadError as error:
            print("Could not make a preview of image %d: %s" % (row["id"], error))
            return not_found()
        if not data:
            return not_found()
        return send_bytes(data, imageinfo.sniff_mime(data) or "image/jpeg", etag)

    @app.route("/pano/<int:image_id>")
    def pano(image_id):
        # The large panorama of a 360 photo (cache.PreviewStore), for the
        # 360 view, or the larger picture of a wide photo, for the viewer's
        # pan view (images.gets_pano); 404 for any other picture (a video
        # too: it plays), and for one not larger than its preview (which
        # is then as good).
        row = visible_row(image_id, "id, path, name, width, height, has_preview, projection, "
                                    "media")
        if row is None or row["media"] == videos.MEDIA_VIDEO:
            return not_found()
        images = _images()
        if images is not None and not images.gets_pano(row["width"], row["height"],
                                                       row["projection"], row["media"]):
            return not_found()
        # A pano on disk is trusted like the preview it belongs to: the
        # scanner writes (or removes) it whenever it writes the preview.
        path = None
        if row["has_preview"] == 1:
            try:
                path = state.previews.pano_path_for(row["library_id"], row["id"])
            except ValueError:
                path = None
        if path and os.path.isfile(path):
            return private_cache(send_file(path, mimetype="image/jpeg", max_age=CACHE_SECONDS))
        # Not made yet (a row scanned before panos existed): make it from
        # the original now, and keep it when previews are kept.
        full, stat = original_path(row)
        if full is None:
            return not_found()
        etag = _source_etag("pano", row["id"], stat, _made_by(images, row))
        if request.if_none_match.contains(etag):
            return not_modified(etag)
        if images is None:
            return not_found()
        with state.pano_lock(row["id"]):
            if path and os.path.isfile(path):
                # Another request made it meanwhile.
                return private_cache(send_file(path, mimetype="image/jpeg",
                                               max_age=CACHE_SECONDS))
            try:
                result = images.read_image(full, state.helper, want_preview=False,
                                           want_pano=True, mtime=stat.st_mtime)
            except images.ImageReadError as error:
                print("Could not make the %s of image %d: %s"
                      % (_pano_kind(row), row["id"], error))
                return not_found()
            data = result.get("pano")
            if not data:
                return not_found()
            library = common.get_library(state.config(), row["library_id"]) or {}
            if path and library.get("previews", True):
                # Kept only while the library keeps previews (a preview made
                # before they were turned off stays, but nothing new is
                # stored), and while the index still describes this content,
                # so an original changed since the last scan leaves nothing
                # behind.
                current = get_conn().execute(
                    "SELECT content_hash, has_preview FROM images WHERE id = ?",
                    (row["id"],)).fetchone()
                if (current is not None and current["has_preview"] == 1
                        and current["content_hash"] == result["content_hash"]):
                    try:
                        state.previews.write_pano(row["library_id"], row["id"], data)
                    except OSError as error:
                        print("Could not save the %s of image %d: %s"
                              % (_pano_kind(row), row["id"], error))
        return send_bytes(data, "image/jpeg", etag)

    @app.route("/video/<int:image_id>")
    def video(image_id):
        # A video, for the viewer's player: its playable copy (see
        # videos.make_proxies) when there is one, else the original -- for
        # a 360 video its LRV (both lenses side by side; the player
        # stitches them), else its original only when that holds both
        # lenses in one stream browsers play (videos.playable_360): no
        # browser plays lens streams. Byte ranges (206, 416), ETags and 304
        # come from send_file, which streams the file from disk.
        row = visible_row(image_id, "id, path, name, media, content_hash, projection, exif")
        if row is None or row["media"] != videos.MEDIA_VIDEO:
            return not_found()
        proxy = videos.existing_proxy(row["library_id"], row["id"], row["content_hash"])
        if proxy is not None:
            try:
                path, stat, kind, mime = proxy, os.stat(proxy), "video-proxy", "video/mp4"
            except OSError:
                proxy = None
        if proxy is None and row["projection"] == videos.PROJECTION_360:
            path, kind = videos.playable_360(row, original_path(row)[0])
            if path is None:
                return not_found()
            try:
                stat, mime = os.stat(path), "video/mp4"
            except OSError:
                return not_found()
        elif proxy is None:
            path, stat = original_path(row)
            if path is None:
                return not_found()
            kind, mime = "video", videos.mime_type(row["name"])
        try:
            response = send_file(path, mimetype=mime, conditional=True,
                                 etag=_source_etag(kind, row["id"], stat),
                                 max_age=CACHE_SECONDS)
        except OSError:
            return not_found()
        response.headers["Accept-Ranges"] = "bytes"
        return private_cache(response)

    @app.route("/raw/<int:image_id>")
    def raw_file(image_id):
        # The RAW file of a picture (its RAW original, or the RAW file it is), as
        # it is: a download for the owner only (raw_photos.py). Not for a page
        # of another site, nor for anyone else.
        site = request.headers.get("Sec-Fetch-Site")
        if not is_admin() or (site is not None and site not in ("same-origin", "none")):
            return not_found()
        row = visible_row(image_id, "id, path, name, exif")
        if row is None:
            return not_found()
        exif = load_json(row["exif"], {})
        entry = raw_photos.paired_record(exif)
        if entry is not None and raw_photos.safe_path(entry["path"]):
            source = {"library_id": row["library_id"], "path": entry["path"]}
        elif raw_photos.view(exif, row["name"]) is not None and common.is_raw_file(row["name"]):
            source = row
        else:
            return not_found()
        full = disk_path(source, library_roots(state.config()))
        stat = common.stat_path(state.helper, full) if full else None
        if stat is None:
            return not_found()
        name = source["path"].rsplit("/", 1)[-1]
        etag = _source_etag("raw", row["id"], stat)
        try:
            _head, data = _read_head(state.helper, full)
            if data is not None:
                response = Response(data, mimetype="application/octet-stream")
                response.set_etag(etag)
                response.headers["Content-Disposition"] = (
                    "attachment; filename*=UTF-8''%s" % urllib.parse.quote(name))
                return private_cache(response.make_conditional(request))
            return private_cache(send_file(
                full, mimetype="application/octet-stream", conditional=True, etag=etag,
                max_age=CACHE_SECONDS, as_attachment=True, download_name=name))
        except OSError:
            return not_found()

    @app.route("/image/<int:image_id>")
    def image(image_id):
        row = visible_row(image_id, "id, path, name")
        if row is None:
            return not_found()
        full, stat = original_path(row)
        if full is None:
            return not_found()
        if common.is_video_file(row["name"]):
            # A video's original, as it is: a download (the viewer's link),
            # since a browser tab that cannot decode it would play only its
            # sound over a black page.
            try:
                return private_cache(send_file(
                    full, mimetype=videos.mime_type(row["name"]), conditional=True,
                    etag=_source_etag("image", row["id"], stat), max_age=CACHE_SECONDS,
                    as_attachment=True, download_name=row["name"]))
            except OSError:
                return not_found()
        try:
            head, data = _read_head(state.helper, full)
        except OSError:
            return not_found()
        images = _images()
        if images is not None:
            mime = imageinfo.sniff_mime(head)
            displayable = mime is not None and imageinfo.browser_can_display(mime)
            if mime is None and not row["name"].lower().endswith(CONVERTED_EXTENSIONS):
                # Unknown to the sniffer: let the browser try it as named.
                mime = mimetypes.guess_type(row["name"])[0]
                displayable = True
        else:
            # Without Pillow nothing can be converted; go by the extension.
            mime = mimetypes.guess_type(row["name"])[0]
            displayable = not row["name"].lower().endswith(CONVERTED_EXTENSIONS)
        mime = mime or "application/octet-stream"
        if displayable:
            if data is None:
                return private_cache(send_file(full, mimetype=mime,
                                               max_age=CACHE_SECONDS))
            return send_bytes(data, mime, _source_etag("image", row["id"], stat))
        # HEIC, HEIF, AVIF, TIFF, ...: browsers cannot show these, so send a
        # JPEG. The ETag lets the browser skip the slow conversion next time.
        etag = _source_etag("image-jpeg", row["id"], stat)
        if request.if_none_match.contains(etag):
            return not_modified(etag)
        if images is None:
            return Response("Showing this image needs Pillow.", status=415,
                            mimetype="text/plain")
        try:
            converted = images.to_browser_jpeg(full, state.helper)
        except images.ImageReadError as error:
            print("Could not convert image %d: %s" % (row["id"], error))
            return Response("This image could not be converted for the browser.",
                            status=415, mimetype="text/plain")
        return send_bytes(converted, "image/jpeg", etag)

    return app


# --- Static export ("package") ------------------------------------------------

def package_library(config, wanted=None):
    """The library "package" exports: wanted (a name or id), or the one enabled library.

    Raises export.ExportError when wanted is unknown, or when it is not
    given and there is not exactly one enabled library.
    """
    import export
    if wanted:
        library = common.get_library(config, wanted)
        if library is None:
            raise export.ExportError("There is no library %r in the settings." % (wanted,))
        return library
    enabled = [library for library in common.enabled_libraries(config) if library.get("id")]
    if len(enabled) == 1:
        return enabled[0]
    if not enabled:
        raise export.ExportError("There is no enabled library to export.")
    raise export.ExportError(
        "An export holds one library; choose one with --library (%s)."
        % ", ".join(common.library_label(library) for library in enabled))


def package(output_dir=None, library=None, log=print, options=None, ctx=None):
    """Export one library as a static website (export.export_library); returns its summary.

    library is a name or id (needed when more than one library is
    enabled); output_dir defaults to data/exports/<library name>/ in the
    data root; options override the library's saved export options. The
    log lines go to log, or to ctx (a jobs.JobContext) when given. The
    database must already be prepared (db.init). Raises export.ExportError
    when the export cannot be made.
    """
    import export
    config = common.load_config()
    chosen = package_library(config, library)
    conn = db.connect()
    try:
        return export.export_library(ctx or export.LogContext(log), conn, config, chosen["id"],
                                     output_dir, options,
                                     helper=load_helper_for_config(config))
    finally:
        conn.close()


def port_problem(host, port):
    """Why the server cannot listen on host:port (a sentence), or None.

    On Windows a second server may share a port another one already listens
    on (Werkzeug lets sockets reuse addresses): it says it is running while
    every request still goes to the first one. So the port is first taken
    on its own (SO_EXCLUSIVEADDRUSE) for a moment. Elsewhere such a second
    server fails to start with a clear message by itself, so nothing is
    checked.
    """
    if os.name != "nt" or not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        return None
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        address = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM,
                                     socket.IPPROTO_TCP)[0][4]
    except (OSError, IndexError, UnicodeError):
        return None         # the server's own start reports a bad host
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(address)
    except OSError as error:
        code = getattr(error, "winerror", None)
        if error.errno == errno.EADDRINUSE or code == 10048:
            return ("Port %d is already in use (is another serve.py still running?). Stop "
                    "it, or start this one with --port and another number." % port)
        if code == 10013:
            return ("Port %d cannot be used: another program holds it, or Windows keeps it "
                    "for itself. Start the server with --port and another number." % port)
        return None
    finally:
        probe.close()
    return None


def _command_line(argv):
    """argv with "serve" put first when it starts with an option ("--port 8080")."""
    argv = list(argv)
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv.insert(0, "serve")
    return argv


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Search and package the image database. With no command (or with "
                    "options such as --host and --port only) the search server starts.",
        epilog="Run '%(prog)s <command> --help' for options on a specific command.")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="Run the interactive search server.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Set the IP to bind to (default: '127.0.0.1')")
    serve_parser.add_argument("--port", type=int, default=5000, help="Set the port to run on (default: 5000)")
    jobs.add_settings_base_argument(serve_parser)

    package_parser = sub.add_parser(
        "package", help="Export one library as a static website (see export.py).")
    package_parser.add_argument(
        "output", nargs="?", default=None,
        help="Output folder (default: data/exports/<library name> in the data "
             "folder). It must be empty or an earlier export.")
    package_parser.add_argument(
        "--library", default=None,
        help="The library to export (name or id); needed when more than one "
             "library is enabled.")
    jobs.add_settings_base_argument(package_parser)

    args = parser.parse_args(_command_line(sys.argv[1:] if argv is None else argv))
    jobs.use_settings_base(parser, args)

    try:
        db.init()
    except (db.DatabaseError, common.ConfigError) as error:
        raise SystemExit("Error: %s" % error)

    if args.command == "package":
        import export
        ctx = jobs.ConsoleContext("export")
        jobs.log_tool_command(ctx, "serve.py", sys.argv[1:] if argv is None else argv)
        try:
            summary = package(args.output, library=args.library, ctx=ctx)
        except KeyboardInterrupt:
            raise SystemExit("\nStopped (Ctrl+C). Run the same command again to finish the "
                             "export; what is done is kept.")
        except (export.ExportError, common.ConfigError, db.DatabaseError) as error:
            raise SystemExit("Error: %s" % error)
        except OSError as error:
            raise SystemExit("Error: %s" % export.os_error_text(error))
        status = export.report(summary)
        if status:
            raise SystemExit(status)
        return

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 5000)
    quiet_query_log()
    problem = port_problem(host, port)
    if problem:
        raise SystemExit("Error: " + problem)
    try:
        app = build_app(init_db=False, bind_host=host)
    except common.ConfigError as error:
        raise SystemExit("Error: %s" % error)
    manager = app.extensions["search_images_jobs"]
    model_servers = app.extensions["search_images_model_servers"]
    base_url = "http://%s:%d" % ("[%s]" % host if ":" in host else host, port)
    print("Serving on %s (press Ctrl+C to stop)" % base_url)
    if admin.is_loopback_host(host):
        # Jobs run in this process only, never in a reloader's second one.
        if not manager.start():
            print("Warning: " + manager.message)
        # The daily stop time of a model server started from the Settings page.
        model_servers.start_scheduler(app.extensions["search_images"].config)
        print("Manage libraries: %s/manage" % base_url)
        print("Settings:         %s/settings" % base_url)
    else:
        print("Warning: the server listens on %s, so other computers can search the "
              "pictures. Manage and Settings are turned off; start the server without "
              "--host to use them." % host)
    # The reloader would start a second process that runs db.init again.
    try:
        app.run(host=host, port=port, use_reloader=False)
    finally:
        # A running job stops at its next safe point and can be resumed.
        manager.stop(timeout=5)
        # A model server a job (or a search) started (with auto_stop) is stopped too.
        try:
            server_config = app.extensions["search_images"].config()
        except common.ConfigError:
            server_config = None
        model_servers.shutdown(config=server_config)
        # Closing the shared thumbnail connection lets SQLite tidy up its
        # -wal/-shm files when the server stops (Ctrl+C).
        app.extensions["search_images"].close()


# The query of a requested address in a request log line: Werkzeug logs the
# address decoded (uri_to_iri), so a typed quote or accented letter shows as
# itself, but a space stays %20 and "&", "=" and "+" stay as they are.
_LOGGED_QUERY_RE = re.compile(r"(\s/[^\s?]*)\?(\S*)")
# The query parameters whose values are only ids, numbers or switches: they are
# logged as they are. Any other value is logged as "...": words searched for
# (q=, on the pages and in /api/embed, or a person's name in person:"Name"), a
# place (place=), tags and the other facets, a trip's title (pick=), the map's
# centre (view=), dates (from=, to=), an area drawn on the map and its name
# (area=, aname=: see map_areas.py).
_LOGGED_PARAMETERS = frozenset((
    "album", "by", "dialog", "drives", "hidden", "id", "kind", "lib", "limit", "log", "min",
    "offset", "page", "person", "since", "size", "sort", "stack", "step", "v", "which"))
_PARAMETER_NAME_RE = re.compile(r"[A-Za-z_]{1,20}\Z")


def _quiet_query(match):
    """A _LOGGED_QUERY_RE match as quiet_request_line logs it (a part without a name
    of the right shape, or without "=", is "..." too)."""
    parts = []
    for part in match.group(2).split("&"):
        name, equals, _value = part.partition("=")
        if not part:
            parts.append(part)
        elif equals and name in _LOGGED_PARAMETERS:
            parts.append(part)
        elif equals and _PARAMETER_NAME_RE.match(name):
            parts.append(name + "=...")
        else:
            parts.append("...")
    return "%s?%s" % (match.group(1), "&".join(parts))


def quiet_request_line(message):
    """A request log line without what people typed or chose: the value of every query
    parameter but those of _LOGGED_PARAMETERS is "..." (GET /?q=... HTTP/1.1,
    GET /api/embed?q=..., GET /detail?id=39&place=...)."""
    return _LOGGED_QUERY_RE.sub(_quiet_query, message)


class QueryLogFilter(logging.Filter):
    """Keeps what people type or choose out of the request log (quiet_request_line):
    the words of a search (/?q=..., /api/embed?q=... when searching by meaning, a
    person:"Name" from the People page), a place chosen (/?place=...), a trip's title
    (/?pick=..., a trip page's Open in search), an area of the map (/map?area=...&aname=...),
    a town looked for in the viewer's Place dialog (its q=)."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:       # (a record that cannot be formatted is left alone)
            return True
        if "?" in message:
            quiet = quiet_request_line(message)
            if quiet != message:
                record.msg = quiet
                record.args = ()
        return True


def quiet_query_log():
    """Put QueryLogFilter on the web server's request log (once)."""
    logger = logging.getLogger("werkzeug")
    if not any(isinstance(item, QueryLogFilter) for item in logger.filters):
        logger.addFilter(QueryLogFilter())


if __name__ == "__main__":
    main()
