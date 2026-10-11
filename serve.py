#!/usr/bin/env python3
"""Search the image database and package it as a static website.

Run with no arguments (or "serve") to start the interactive Flask server.
Run with "package" to build a backend-free copy of the site that searches the
data entirely in the browser.
"""

import argparse
import base64
import contextlib
import fnmatch
import hashlib
import io
import json
import math
import mimetypes
import os
import shutil
import socket
import sys
import threading
import time

import admin
import cache
import common
import db
import imageinfo
import jobs
import prompts
import search_index
import world_map

TEMPLATE_DIR = os.path.join(common.BASE_DIR, "templates")
STATIC_DIR = os.path.join(common.BASE_DIR, "static")
FALLBACK_BASEMAP = os.path.join(STATIC_DIR, "worldmap.jpg")
ASSET_FILES = ("style.css", "site.js", "shell.js", "search-core.js", "viewer.js", "app.js",
               "detail.js", "map.js")
TILE_SIZE = 256
# Without a tile pyramid the bundled basemap is sliced into zoom 0..3.
FALLBACK_MAX_ZOOM = 3
# No tile request above this zoom is ever answered, whatever the manifest says.
TILE_ZOOM_LIMIT = 20
# The static site's data files are kept at or below this size.
SHARD_LIMIT = 10 * 1024 * 1024
# Marks a folder as built by "package". Only such a folder (or an empty one)
# is written to, because packaging deletes stale files inside it.
EXPORT_MARKER = ".search-images-export"
# How long browsers may reuse thumbnails, previews and originals, and tiles.
CACHE_SECONDS = 3600
TILE_CACHE_SECONDS = 86400
# JPEG data under another extension (Insta360 .insp): copied as .jpg files.
JPEG_EXTENSIONS = (".insp",)
# Originals with these extensions are converted to JPEG for browsers.
CONVERTED_EXTENSIONS = common.HEIC_EXTENSIONS + (".tif", ".tiff")
# Provenance fields the detail page may show (who described an image, when).
PROVENANCE_FIELDS = ("model", "endpoint", "prompt_ver", "profile", "at")
# SQLite integers are 64-bit; larger ids in a URL simply do not exist.
MAX_IMAGE_ID = 2 ** 63 - 1
# Host names the server always answers to. Requests naming any other host are
# refused, so a web page cannot reach the server through DNS rebinding.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# Folders directly inside the program folder that git ignores, and that
# "package" may therefore write to.
IGNORED_SITE_FOLDERS = ("site", "site_*", "exports")
# Sent with every page: scripts and styles only from this server (the page
# settings are a JSON block, not a script), never shown inside a frame, no
# <base> that could move the scripts' relative addresses, forms only sent
# here, and no plugins.
CONTENT_SECURITY_POLICY = ("default-src 'self'; img-src 'self' data: blob:; "
                           "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                           "base-uri 'none'; form-action 'self'; object-src 'none'")
# The name each page template goes by in the page settings ("page").
PAGE_NAMES = {"page.html": "search", "detail.html": "detail", "map.html": "map",
              "manage.html": "manage", "settings.html": "settings"}


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


def search_text(name, path, info, taken_at="", projection=""):
    """The lower-cased text the search page matches a query against.

    See prompts.search_terms: the words of the description (info, a dict
    or None; any prompt version), the file name and path, and the capture
    year and month, and "360 panorama" for a 360-degree photo.
    """
    return prompts.search_terms(info, name, path, taken_at, projection)


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


def query_visible(conn, library_ids, columns, order_by="id"):
    """Rows of present, non-hidden images in the given libraries."""
    library_ids = list(library_ids)
    if not library_ids:
        return []
    sql = ("SELECT %s FROM images WHERE missing_since IS NULL AND hidden = 0 "
           "AND library_id IN (%s) ORDER BY %s"
           % (columns, ",".join("?" * len(library_ids)), order_by))
    return conn.execute(sql, library_ids).fetchall()


def image_extension(name):
    """Extension used for an original in the static site ("jpg" if converted)."""
    lower = name.lower()
    if lower.endswith(CONVERTED_EXTENSIONS + JPEG_EXTENSIONS):
        return "jpg"
    return os.path.splitext(lower)[1].lstrip(".") or "jpg"


def projection_of(row):
    """The row's 360-degree projection, or "" (also while it is unchecked)."""
    projection = row["projection"]
    return projection if projection in imageinfo.PROJECTIONS_360 else ""


def detail_record(row, library_name):
    """Build the full metadata record used by the detail page."""
    exif = load_json(row["exif"], {})
    return {
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
        "hidden": bool(row["hidden"]),
        "info": load_json(row["info"], None),
        "exif": exif if isinstance(exif, dict) else {},
        "provenance": provenance_record(row["provenance"]),
    }


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
                         capabilities=None):
    """Build a page from a template file, shared by server and static modes.

    The page settings go into the template's
    <script type="application/json" id="app-config"> block, which site.js
    and shell.js read (a JSON block, unlike an inline script, is allowed by
    the pages' Content-Security-Policy and also works from file://).

    libraries are the libraries for the page's library picker: {id: name}
    (or a list of names); by default, the enabled libraries in config.json.
    capabilities are {"admin": the Manage and Settings pages may be used,
    "static": a packaged site}; admin is off unless given.
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
    html = html.replace("__ASSET_PREFIX__", asset_prefix)
    html = html.replace("__APP_CONFIG__", script_json(app_config))
    return html


# --- Interactive Flask server ------------------------------------------------

class ServerState:
    """What every request shares: the config, the helper and the stores.

    The config is re-read when config.json changes, so library renames and
    enable/disable show up without a restart. The thumbnail store keeps one
    connection that all request threads share. index answers /api/index
    (search_index.IndexService).
    """

    def __init__(self, helper=None):
        self.helper = helper
        self.previews = cache.PreviewStore()
        self.index = search_index.IndexService()
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


def _source_etag(kind, image_id, stat):
    """An ETag for something derived from an original: changes with the file."""
    text = "%s:%d:%d:%r" % (kind, image_id, stat.st_size, stat.st_mtime)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:24]


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

    def page(name):
        capabilities = {"admin": is_admin()}
        return render_template_file(name, "server", "/static/", None,
                                    tiles_max=tiles_max_zoom(),
                                    libraries=visible_libraries(state.config()),
                                    capabilities=capabilities)

    def admin_page(name):
        if not is_admin():
            return Response("Manage and Settings only work on this computer, when the "
                            "server listens on localhost.", status=403, mimetype="text/plain")
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

    @app.route("/api/locations")
    def api_locations():
        library_ids = list(visible_libraries(state.config()))
        rows = world_map.collect_located_images(get_conn(), library_ids)
        return jsonify([{"id": i, "lat": lat, "lon": lon}
                        for i, lat, lon in rows])

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

    @app.route("/api/image/<int:image_id>")
    def api_image(image_id):
        row = visible_row(image_id, "*")
        if row is None:
            return jsonify({"error": "not found"}), 404
        names = visible_libraries(state.config())
        return jsonify(detail_record(row, names[row["library_id"]]))

    @app.route("/thumb/<int:image_id>")
    def thumb(image_id):
        row = visible_row(image_id, "id")
        data = state.thumbs().get(image_id) if row is not None else None
        if data is None:
            return not_found()
        return send_bytes(data, "image/jpeg", hashlib.sha1(data).hexdigest()[:24])

    @app.route("/preview/<int:image_id>")
    def preview(image_id):
        row = visible_row(image_id, "id, path, name, has_preview")
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
        etag = _source_etag("preview", row["id"], stat)
        if request.if_none_match.contains(etag):
            return not_modified(etag)
        images = _images()
        if images is None:
            return not_found()
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
        # 360 view; 404 for any other picture, and for a 360 photo not
        # larger than its preview (which is then the whole picture).
        row = visible_row(image_id, "id, path, name, width, height, has_preview, projection")
        if row is None or row["projection"] not in imageinfo.PROJECTIONS_360:
            return not_found()
        images = _images()
        if images is not None and not images.has_pano(row["width"], row["height"]):
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
        etag = _source_etag("pano", row["id"], stat)
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
                print("Could not make the 360-degree panorama of image %d: %s"
                      % (row["id"], error))
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
                        print("Could not save the 360-degree panorama of image %d: %s"
                              % (row["id"], error))
        return send_bytes(data, "image/jpeg", etag)

    @app.route("/image/<int:image_id>")
    def image(image_id):
        row = visible_row(image_id, "id, path, name")
        if row is None:
            return not_found()
        full, stat = original_path(row)
        if full is None:
            return not_found()
        try:
            head, data = _read_head(state.helper, full)
        except OSError:
            return not_found()
        images = _images()
        if images is not None:
            mime = images.sniff_mime(head)
            displayable = mime is not None and images.browser_can_display(mime)
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


# --- Static site packaging ---------------------------------------------------

class ExportError(Exception):
    """The static site cannot be written to the chosen folder."""


def default_export_dir():
    """Where "package" writes when no folder is given: data/exports/site."""
    return os.path.join(common.exports_dir(), "site")


def _normalized(path):
    return os.path.normcase(os.path.abspath(path))


def _is_within(path, folder):
    """True if path is folder or somewhere inside it (no file system access)."""
    path, folder = _normalized(path), _normalized(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _git_ignored_site_folder(output_dir):
    """True if output_dir is in a folder directly inside the program folder that git ignores."""
    rel = os.path.relpath(_normalized(output_dir), _normalized(common.BASE_DIR))
    first = rel.split(os.sep)[0]
    return any(fnmatch.fnmatchcase(first, os.path.normcase(pattern))
               for pattern in IGNORED_SITE_FOLDERS)


def check_export_dir(output_dir, config, force=False):
    """Raise ExportError if package() must not write to output_dir.

    Packaging deletes stale files inside the folder, so it refuses a folder
    that overlaps a library, holds the data folder or the program, sits in
    the data folder outside data/exports, or is not empty and lacks the
    marker an earlier export left (force accepts such a folder, for example
    a site built by an older version). Inside the program folder (a git
    checkout) only folders git ignores are allowed: site/, site_*/,
    exports/ and data/exports/, so private photos never end up in a commit.
    """
    for library in config.get("libraries", []):
        root = common.library_root(library)
        if root and (_is_within(output_dir, root) or _is_within(root, output_dir)):
            raise ExportError(
                "The output folder %s overlaps the folder of library %s. "
                "Choose a folder outside your libraries."
                % (output_dir, common.library_label(library)))
    for folder, what in ((common.data_dir(), "the data folder"),
                         (common.BASE_DIR, "the program folder")):
        if _is_within(folder, output_dir):
            raise ExportError("The output folder %s contains %s. Choose a "
                              "different folder." % (output_dir, what))
    if (_is_within(output_dir, common.data_dir())
            and not _is_within(output_dir, common.exports_dir())):
        raise ExportError("Inside the data folder, exports can only go to %s."
                          % common.exports_dir())
    if (_is_within(output_dir, common.BASE_DIR)
            and not _is_within(output_dir, common.exports_dir())
            and not _git_ignored_site_folder(output_dir)):
        raise ExportError(
            "The output folder %s is inside the program folder %s, where its private "
            "photos could end up in a commit. Choose a folder outside it, or one named "
            "site or site_<something> directly inside it." % (output_dir, common.BASE_DIR))
    if os.path.exists(output_dir) and not os.path.isdir(output_dir):
        raise ExportError("%s is a file, not a folder." % output_dir)
    if (not force and os.path.isdir(output_dir) and os.listdir(output_dir)
            and not os.path.isfile(os.path.join(output_dir, EXPORT_MARKER))):
        raise ExportError(
            "%s is not empty and was not created by 'serve.py package'. Choose "
            "an empty or new folder. To update a site built by an older version, "
            "add --force; files in its images, data and tiles folders may then "
            "be replaced or deleted." % output_dir)


def load_export_state(output_dir):
    """The shard layout of the previous export into output_dir ([] if none)."""
    path = os.path.join(output_dir, EXPORT_MARKER)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            shards = json.load(handle).get("shards", [])
    except (OSError, ValueError, AttributeError):
        return []
    return shards if isinstance(shards, list) else []


def save_export_state(output_dir, shards):
    """Write the marker file, which also records the shard layout."""
    write_json(os.path.join(output_dir, EXPORT_MARKER),
               {"format": 1, "generated": int(time.time()), "shards": shards})


def plan_shards(records, previous):
    """Assign images to shard files, reusing previous shards where possible.

    Returns (shards, dirty, by_id). Each shard is a list of [id, digest] pairs.
    dirty is the set of shard indexes whose contents changed.
    """
    by_id = {}
    order = []
    for image_id, record, digest, size in records:
        by_id[image_id] = (record, digest, size)
        order.append(image_id)
    shards = []
    dirty = set()
    placed = set()
    for prev in previous:
        rebuilt = []
        is_dirty = False
        for entry in prev:
            image_id, old_digest = entry[0], entry[1]
            if image_id not in by_id or image_id in placed:
                is_dirty = True
                continue
            digest = by_id[image_id][1]
            if digest != old_digest:
                is_dirty = True
            rebuilt.append([image_id, digest])
            placed.add(image_id)
        shards.append(rebuilt)
        if is_dirty:
            dirty.add(len(shards) - 1)
    leftovers = [i for i in order if i not in placed]
    if leftovers and not shards:
        shards.append([])
    for image_id in leftovers:
        size = by_id[image_id][2]
        index = len(shards) - 1
        used = sum(by_id[e[0]][2] for e in shards[index])
        if shards[index] and used + size > SHARD_LIMIT:
            shards.append([])
            index += 1
        shards[index].append([image_id, by_id[image_id][1]])
        dirty.add(index)
    return shards, dirty, by_id


def write_json(path, value):
    """Write JSON atomically. NaN/Infinity are refused: browsers cannot parse them."""
    common.write_file_atomic(path, json.dumps(value, allow_nan=False) + "\n")


def copy_original(row, roots, dest, helper=None, log=print):
    """Copy (or convert to JPEG) one original into the static site folder.

    Returns "copied", "current" (dest is already up to date), "missing" (the
    original cannot be reached) or "failed". The file is written under a
    temporary name first, so a failure never leaves a partial image behind,
    and one bad file does not stop the export.
    """
    source = disk_path(row, roots)
    stat = common.stat_path(helper, source) if source else None
    if stat is None:
        return "missing"
    try:
        if os.path.isfile(dest) and os.path.getmtime(dest) >= stat.st_mtime:
            return "current"
        images = _images()
        convert = row["name"].lower().endswith(CONVERTED_EXTENSIONS)
        data = None
        if not convert:
            data = common.load_image_bytes(helper, source)
            if images is not None:
                mime = images.sniff_mime(data)
                convert = mime is not None and not images.browser_can_display(mime)
        if convert:
            if images is None:
                log("Warning: skipped %s: converting it needs Pillow." % row["path"])
                return "failed"
            data = images.to_browser_jpeg(source, helper)
        common.write_file_atomic(dest, data)
        return "copied"
    except Exception as error:
        log("Warning: could not copy %s: %s" % (row["path"], error))
        return "failed"


def package(output_dir=None, force=False, log=print):
    """Build or update the static site in output_dir; return a summary dict.

    Only present, non-hidden images of enabled libraries are included. The
    database must already be prepared (db.init). Raises ExportError when the
    folder is not safe to write to (see check_export_dir).
    """
    config = common.load_config()
    output_dir = os.path.abspath(output_dir or default_export_dir())
    check_export_dir(output_dir, config, force)
    common.ensure_dir(output_dir)
    previous = load_export_state(output_dir)
    if not os.path.isfile(os.path.join(output_dir, EXPORT_MARKER)):
        # Mark the folder before writing anything else, so an interrupted
        # first export can simply be run again.
        save_export_state(output_dir, [])

    names = visible_libraries(config)
    roots = library_roots(config)
    helper = load_helper_for_config(config)
    images_dir = common.ensure_dir(os.path.join(output_dir, "images"))
    data_dir = common.ensure_dir(os.path.join(output_dir, "data"))

    conn = db.connect()
    thumbs = cache.ThumbStore()
    try:
        rows = query_visible(conn, names, "*")
        records = []
        image_names = set()
        copies = {"copied": 0, "current": 0, "missing": 0, "failed": 0}
        for row in rows:
            image_name = "%d.%s" % (row["id"], image_extension(row["name"]))
            image_names.add(image_name)
            status = copy_original(row, roots, os.path.join(images_dir, image_name),
                                   helper, log)
            copies[status] += 1
            data = thumbs.get(row["id"])
            thumb = ""
            if data:
                thumb = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
            info = load_json(row["info"], None)
            exif = load_json(row["exif"], {})
            record = {
                "id": row["id"], "library": names[row["library_id"]],
                "path": row["path"], "name": row["name"], "size": row["size"],
                "mtime": finite(row["mtime"]), "ctime": finite(row["ctime"]),
                "width": row["width"], "height": row["height"],
                "projection": projection_of(row),
                "taken_at": row["taken_at"], "taken_source": row["taken_source"],
                "tz_offset": row["tz_offset"], "camera": row["camera"],
                "info": info, "exif": exif if isinstance(exif, dict) else {},
                "text": search_text(row["name"], row["path"], info, row["taken_at"],
                                    projection_of(row)),
                "dhash": row["dhash"], "thumb": thumb,
                "image": "images/" + image_name,
            }
            text = json.dumps(record, sort_keys=True, allow_nan=False)
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            records.append((row["id"], record, digest, len(text) + 2))

        # Remove originals left over from images that are no longer exported.
        for existing in os.listdir(images_dir):
            target = os.path.join(images_dir, existing)
            if existing not in image_names and os.path.isfile(target):
                os.remove(target)

        shards, dirty, by_id = plan_shards(records, previous)
        shard_names = []
        written = 0
        for index, shard in enumerate(shards):
            name = "data-%04d.json" % index
            shard_names.append(name)
            target = os.path.join(data_dir, name)
            if index in dirty or not os.path.exists(target):
                write_json(target, [by_id[entry[0]][0] for entry in shard])
                written += 1
        for existing in os.listdir(data_dir):
            if (existing.startswith("data-") and existing.endswith(".json")
                    and existing not in shard_names):
                os.remove(os.path.join(data_dir, existing))

        libraries = list(names.values())
        write_json(os.path.join(data_dir, "manifest.json"), {
            "libraries": libraries, "shards": shard_names,
            "count": len(records), "generated": int(time.time()),
        })

        for asset in ASSET_FILES:
            shutil.copyfile(os.path.join(STATIC_DIR, asset),
                            os.path.join(output_dir, asset))

        locations = world_map.collect_located_images(conn, list(names))
        write_json(os.path.join(data_dir, "locations.json"),
                   [{"id": i, "lat": lat, "lon": lon}
                    for i, lat, lon in locations])
    finally:
        thumbs.close()
        conn.close()

    tiles_max = export_tiles(output_dir)
    tile_prefix = "tiles/" if tiles_max is not None else None
    for page in ("index.html", "detail.html", "map.html"):
        template = "page.html" if page == "index.html" else page
        common.write_file_atomic(os.path.join(output_dir, page), render_template_file(
            template, "static", "", "data/", tiles_max=tiles_max,
            tile_prefix=tile_prefix, libraries=names))

    save_export_state(output_dir, shards)
    log("Packaged %d image(s) into %s (%d data shard(s) written)."
        % (len(records), output_dir, written))
    if copies["missing"] or copies["failed"]:
        log("Warning: %d original(s) could not be reached and %d could not be "
            "copied; their full-size links will not work."
            % (copies["missing"], copies["failed"]))
    return {"count": len(records), "written": written, "output": output_dir,
            "copies": copies}


def export_tiles(output_dir):
    """Copy data/tiles into the static site. Returns the max zoom, or None.

    When no tile pyramid is present the fallback basemap is sliced into a
    small zoom-0..3 pyramid so the static map page still works offline.
    """
    max_zoom = tiles_max_zoom()
    dest_root = os.path.join(output_dir, "tiles")
    if max_zoom is not None:
        if os.path.isdir(dest_root):
            shutil.rmtree(dest_root)
        shutil.copytree(common.tiles_dir(), dest_root)
        return max_zoom
    if not os.path.isfile(FALLBACK_BASEMAP):
        return None
    for zoom in range(FALLBACK_MAX_ZOOM + 1):
        cols = 2 ** (zoom + 1)
        rows = 2 ** zoom
        for x in range(cols):
            for y in range(rows):
                data = _fallback_tile_bytes(zoom, x, y)
                if data is None:
                    return None
                tile_dir = common.ensure_dir(os.path.join(dest_root, str(zoom), str(x)))
                with open(os.path.join(tile_dir, "%d.jpg" % y), "wb") as handle:
                    handle.write(data)
    return FALLBACK_MAX_ZOOM


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Search and package the image database.",
        epilog="Run '%(prog)s <command> --help' for options on a specific command.")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="Run the interactive search server.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Set the IP to bind to (default: '127.0.0.1')")
    serve_parser.add_argument("--port", type=int, default=5000, help="Set the port to run on (default: 5000)")

    package_parser = sub.add_parser("package", help="Build a backend-free static site.")
    package_parser.add_argument(
        "output", nargs="?", default=None,
        help="Output folder for the static site (default: data/exports/site "
             "in the data folder). It must be empty or an earlier export.")
    package_parser.add_argument(
        "--force", action="store_true",
        help="Also write to a non-empty folder that is not marked as an "
             "export, such as a site built by an older version.")

    args = parser.parse_args()

    try:
        db.init()
    except (db.DatabaseError, common.ConfigError) as error:
        raise SystemExit("Error: %s" % error)

    if args.command == "package":
        try:
            package(args.output, force=args.force)
        except (ExportError, common.ConfigError) as error:
            raise SystemExit("Error: %s" % error)
        return

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 5000)
    try:
        app = build_app(init_db=False, bind_host=host)
    except common.ConfigError as error:
        raise SystemExit("Error: %s" % error)
    manager = app.extensions["search_images_jobs"]
    base_url = "http://%s:%d" % ("[%s]" % host if ":" in host else host, port)
    print("Serving on %s (press Ctrl+C to stop)" % base_url)
    if admin.is_loopback_host(host):
        # Jobs run in this process only, never in a reloader's second one.
        if not manager.start():
            print("Warning: " + manager.message)
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
        # Closing the shared thumbnail connection lets SQLite tidy up its
        # -wal/-shm files when the server stops (Ctrl+C).
        app.extensions["search_images"].close()


if __name__ == "__main__":
    main()
