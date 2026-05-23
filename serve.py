#!/usr/bin/env python3
"""Search the image database and package it as a static website.

Run with no arguments (or "serve") to start the interactive Flask server.
Run with "package" to build a backend-free copy of the site that searches the
data entirely in the browser.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import time

import common
# world_map is imported lazily inside the functions that need it: it pulls in
# Pillow, and the original serve.py avoided that at startup so users running
# the search server on a stdlib-only setup still worked.

EXPORT_STATE_PATH = os.path.join(common.DATA_DIR, "export-state.json")
TEMPLATE_DIR = os.path.join(common.BASE_DIR, "templates")
STATIC_DIR = os.path.join(common.BASE_DIR, "static")
TILES_DIR = os.path.join(common.DATA_DIR, "tiles")
FALLBACK_BASEMAP = os.path.join(STATIC_DIR, "worldmap.jpg")
ASSET_FILES = ("style.css", "site.js", "app.js", "detail.js", "map.js")
TILE_SIZE = 256


def tiles_max_zoom():
    """Return the highest pre-generated zoom level, or None if no tiles."""
    manifest = os.path.join(TILES_DIR, "manifest.json")
    if not os.path.isfile(manifest):
        return None
    try:
        with open(manifest, "r") as handle:
            return int(json.load(handle).get("max_zoom"))
    except (OSError, ValueError, TypeError):
        return None


def _fallback_tile_bytes(zoom, x, y):
    """Slice a tile out of the small bundled basemap.

    Used when the pre-generated tile pyramid is absent. Always returns
    something usable so the map page still works without running make_tiles.py.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    if not os.path.isfile(FALLBACK_BASEMAP):
        return None
    cols = 2 ** (zoom + 1)
    rows = 2 ** zoom
    if x < 0 or x >= cols or y < 0 or y >= rows:
        return None
    with Image.open(FALLBACK_BASEMAP) as source:
        source = source.convert("RGB")
        src_w, src_h = source.size
        sx0 = int(round(x * src_w / cols))
        sx1 = int(round((x + 1) * src_w / cols))
        sy0 = int(round(y * src_h / rows))
        sy1 = int(round((y + 1) * src_h / rows))
        crop = source.crop((sx0, sy0, sx1, sy1))
        if crop.size != (TILE_SIZE, TILE_SIZE):
            crop = crop.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=82)
        return buffer.getvalue()


def render_template_file(name, mode, asset_prefix, data_prefix,
                         tiles_max=None, tile_prefix=None):
    """Build a page from a template file, shared by server and static modes."""
    with open(os.path.join(TEMPLATE_DIR, name), "r") as handle:
        html = handle.read()
    config = common.load_config()
    app_config = {
        "mode": mode,
        "libraries": [lib.get("name", "") for lib in config.get("libraries", [])],
    }
    if mode == "server":
        app_config["api"] = "/api"
        app_config["tiles"] = "/tile"
    else:
        app_config["data"] = data_prefix
        app_config["tiles"] = tile_prefix
    app_config["tilesMax"] = tiles_max
    app_config["tileSize"] = TILE_SIZE
    html = html.replace("__ASSET_PREFIX__", asset_prefix)
    html = html.replace("__APP_CONFIG__", json.dumps(app_config))
    return html


def library_roots():
    config = common.load_config()
    roots = {}
    for library in config.get("libraries", []):
        roots[library.get("name", "")] = library.get("path", "")
    return roots


def disk_path(row, roots):
    root = roots.get(row["library"])
    if not root:
        return None
    return os.path.join(root, *row["path"].split("/"))


def image_extension(name):
    if common.is_heic_file(name):
        return "jpg"
    return os.path.splitext(name)[1].lower().lstrip(".") or "jpg"


def detail_record(row):
    """Build the full metadata record used by the detail page."""
    return {
        "id": row["id"],
        "library": row["library"],
        "path": row["path"],
        "name": row["name"],
        "size": row["size"],
        "mtime": row["mtime"],
        "ctime": row["ctime"],
        "width": row["width"],
        "height": row["height"],
        "info": json.loads(row["info"]) if row["info"] else None,
        "exif": json.loads(row["exif"]) if row["exif"] else {},
    }


# --- Interactive Flask server ------------------------------------------------

def build_app():
    try:
        from flask import Flask, Response, jsonify, send_file
    except ImportError:
        raise SystemExit(
            "This tool needs Flask. Install it with:\n"
            "    python -m pip install Flask")

    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

    @app.route("/")
    def index():
        return render_template_file("page.html", "server", "/static/", None,
                                    tiles_max=tiles_max_zoom())

    @app.route("/detail")
    def detail():
        return render_template_file("detail.html", "server", "/static/", None,
                                    tiles_max=tiles_max_zoom())

    @app.route("/map")
    def map_page():
        return render_template_file("map.html", "server", "/static/", None,
                                    tiles_max=tiles_max_zoom())

    @app.route("/api/locations")
    def api_locations():
        import world_map
        rows = world_map.collect_located_images()
        return jsonify([{"id": i, "lat": lat, "lon": lon}
                        for i, lat, lon in rows])

    @app.route("/tile/<int:zoom>/<int:x>/<int:y>.jpg")
    def tile(zoom, x, y):
        local = os.path.join(TILES_DIR, str(zoom), str(x), "%d.jpg" % y)
        if os.path.isfile(local):
            return send_file(local, mimetype="image/jpeg")
        data = _fallback_tile_bytes(zoom, x, y)
        if data is None:
            return Response(status=404)
        return Response(data, mimetype="image/jpeg")

    @app.route("/api/index")
    def api_index():
        conn = common.open_db()
        rows = conn.execute("SELECT id, name, library, search_text, dhash "
                            "FROM images ORDER BY name").fetchall()
        conn.close()
        return jsonify([{"id": r["id"], "name": r["name"],
                         "library": r["library"], "text": r["search_text"],
                         "dhash": r["dhash"]}
                        for r in rows])

    @app.route("/api/image/<int:image_id>")
    def api_image(image_id):
        conn = common.open_db()
        row = conn.execute("SELECT * FROM images WHERE id = ?",
                            (image_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail_record(row))

    @app.route("/thumb/<int:image_id>")
    def thumb(image_id):
        conn = common.open_db()
        row = conn.execute("SELECT thumb_shard, thumb_offset, thumb_length "
                            "FROM images WHERE id = ?", (image_id,)).fetchone()
        conn.close()
        if not row or row["thumb_shard"] is None:
            return Response(status=404)
        store = common.ThumbStore(common.THUMB_DIR)
        data = store.read(row["thumb_shard"], row["thumb_offset"],
                          row["thumb_length"])
        return Response(data, mimetype="image/jpeg")

    @app.route("/image/<int:image_id>")
    def image(image_id):
        conn = common.open_db()
        row = conn.execute("SELECT library, path, name FROM images WHERE id = ?",
                            (image_id,)).fetchone()
        conn.close()
        if not row:
            return Response(status=404)
        full = disk_path(row, library_roots())
        if not full or not os.path.isfile(full):
            return Response(status=404)
        if common.is_heic_file(full):
            import images
            if images.ensure_heif():
                return Response(images.heic_to_jpeg_bytes(full),
                                mimetype="image/jpeg")
        return send_file(full)

    return app


# --- Static site packaging ---------------------------------------------------

def load_export_state():
    if os.path.exists(EXPORT_STATE_PATH):
        with open(EXPORT_STATE_PATH, "r") as handle:
            return json.load(handle).get("shards", [])
    return []


def save_export_state(shards):
    common.ensure_data_dirs()
    with open(EXPORT_STATE_PATH, "w") as handle:
        json.dump({"shards": shards}, handle)
        handle.write("\n")


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
            if image_id not in by_id:
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
        if shards[index] and used + size > common.SHARD_LIMIT:
            shards.append([])
            index += 1
        shards[index].append([image_id, by_id[image_id][1]])
        dirty.add(index)
    return shards, dirty, by_id


def write_json(path, value):
    with open(path, "w") as handle:
        json.dump(value, handle)
        handle.write("\n")


def copy_original(row, roots, dest):
    """Copy (or convert) one original image into the static site folder."""
    source = disk_path(row, roots)
    if not source or not os.path.isfile(source):
        return
    if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(source):
        return
    if common.is_heic_file(source):
        import images
        if not images.ensure_heif():
            return
        with open(dest, "wb") as handle:
            handle.write(images.heic_to_jpeg_bytes(source))
    else:
        shutil.copyfile(source, dest)


def package(output_dir):
    conn = common.open_db()
    store = common.ThumbStore(common.THUMB_DIR)
    roots = library_roots()
    rows = conn.execute("SELECT * FROM images ORDER BY id").fetchall()

    images_dir = os.path.join(output_dir, "images")
    data_dir = os.path.join(output_dir, "data")
    for path in (output_dir, images_dir, data_dir):
        if not os.path.isdir(path):
            os.makedirs(path)

    records = []
    image_names = set()
    for row in rows:
        image_name = "%d.%s" % (row["id"], image_extension(row["name"]))
        image_names.add(image_name)
        copy_original(row, roots, os.path.join(images_dir, image_name))
        thumb = ""
        if row["thumb_shard"] is not None:
            data = store.read(row["thumb_shard"], row["thumb_offset"],
                              row["thumb_length"])
            thumb = "data:image/jpeg;base64," + \
                base64.b64encode(data).decode("ascii")
        record = {
            "id": row["id"], "library": row["library"], "path": row["path"],
            "name": row["name"], "size": row["size"], "mtime": row["mtime"],
            "ctime": row["ctime"], "width": row["width"],
            "height": row["height"],
            "info": json.loads(row["info"]) if row["info"] else None,
            "exif": json.loads(row["exif"]) if row["exif"] else {},
            "text": row["search_text"], "dhash": row["dhash"],
            "thumb": thumb, "image": "images/" + image_name,
        }
        text = json.dumps(record, sort_keys=True)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        records.append((row["id"], record, digest, len(text) + 2))

    # Remove originals left over from images that no longer exist.
    for existing in os.listdir(images_dir):
        if existing not in image_names:
            os.remove(os.path.join(images_dir, existing))

    shards, dirty, by_id = plan_shards(records, load_export_state())
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

    config = common.load_config()
    libraries = [lib.get("name", "") for lib in config.get("libraries", [])]
    write_json(os.path.join(data_dir, "manifest.json"), {
        "libraries": libraries, "shards": shard_names,
        "count": len(records), "generated": int(time.time()),
    })

    for asset in ASSET_FILES:
        shutil.copyfile(os.path.join(STATIC_DIR, asset),
                        os.path.join(output_dir, asset))

    import world_map
    locations = world_map.collect_located_images(conn)
    write_json(os.path.join(data_dir, "locations.json"),
               [{"id": i, "lat": lat, "lon": lon}
                for i, lat, lon in locations])

    tiles_max = export_tiles(output_dir)
    tile_prefix = "tiles/" if tiles_max is not None else None

    with open(os.path.join(output_dir, "index.html"), "w") as handle:
        handle.write(render_template_file(
            "page.html", "static", "", "data/",
            tiles_max=tiles_max, tile_prefix=tile_prefix))
    with open(os.path.join(output_dir, "detail.html"), "w") as handle:
        handle.write(render_template_file(
            "detail.html", "static", "", "data/",
            tiles_max=tiles_max, tile_prefix=tile_prefix))
    with open(os.path.join(output_dir, "map.html"), "w") as handle:
        handle.write(render_template_file(
            "map.html", "static", "", "data/",
            tiles_max=tiles_max, tile_prefix=tile_prefix))

    save_export_state(shards)
    conn.close()
    print("Packaged %d image(s) into %s (%d data shard(s) written)."
          % (len(records), output_dir, written))


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
        shutil.copytree(TILES_DIR, dest_root)
        return max_zoom
    if not os.path.isfile(FALLBACK_BASEMAP):
        return None
    fallback_max = 3
    for zoom in range(fallback_max + 1):
        cols = 2 ** (zoom + 1)
        rows = 2 ** zoom
        for x in range(cols):
            for y in range(rows):
                data = _fallback_tile_bytes(zoom, x, y)
                if data is None:
                    return None
                tile_dir = os.path.join(dest_root, str(zoom), str(x))
                if not os.path.isdir(tile_dir):
                    os.makedirs(tile_dir)
                with open(os.path.join(tile_dir, "%d.jpg" % y), "wb") as handle:
                    handle.write(data)
    return fallback_max


def main():
    parser = argparse.ArgumentParser(
        description="Search and package the image database.")
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve", help="Run the interactive search server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5000)
    package_parser = sub.add_parser("package",
                                    help="Build a backend-free static site.")
    package_parser.add_argument(
        "output", nargs="?", default=os.path.join(common.BASE_DIR, "site"),
        help="Output folder for the static site (default: site).")
    args = parser.parse_args()

    if args.command == "package":
        package(args.output)
        return
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 5000)
    app = build_app()
    print("Serving on http://%s:%d (press Ctrl+C to stop)" % (host, port))
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
