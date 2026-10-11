#!/usr/bin/env python3
"""Download the NASA Blue Marble image and slice it into map tiles.

The world-map page falls back to the small `static/worldmap.jpg` if no tiles
are present, so this script is optional. Run it once to get high-resolution
tiles for the live server and the static-site export.

Output layout (equirectangular projection, world is 2:1 at zoom 0), in the
data folder (SETTINGS_BASE/data):
    data/tiles/<z>/<x>/<y>.jpg     256x256 JPEG tiles
    data/tiles/manifest.json       max zoom level

Existing tiles are kept, so reruns only fill in missing pieces. Each tile
is written under a temporary name (<y>.jpg.part) and then renamed, so an
interrupted run never leaves a half-written tile under a tile's name; a
rerun removes the temporary files it left. A rerun also checks the tiles
that are there (cheaply: that each is a whole JPEG file, see
imageinfo.jpeg_complete) and writes again any that were cut short -- by an
older version that wrote tiles in place, or by a disk that lost the end of
a file.
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.request

import common
import imageinfo

try:
    from PIL import Image
except ImportError:
    raise SystemExit(
        "This tool needs Pillow. Install it with:\n"
        "    python -m pip install Pillow")

# The NASA Blue Marble is 21600 x 10800 = 233 megapixels, above Pillow's
# default 179 MP "decompression bomb" cap. Lifting it is safe here: the
# source file is fetched from a known URL or pointed at by the user.
Image.MAX_IMAGE_PIXELS = None


# July 2004 Blue Marble Next Generation w/ Topography and Bathymetry,
# 21600 x 10800, about 27 MB. NASA's static asset URLs do shift around over
# time; if this 404s, the spec for a working replacement is in the docstring
# of `download` below, and any source file can be supplied with --source.
DEFAULT_URL = (
    "https://assets.science.nasa.gov/content/dam/science/esd/eo/"
    "images/bmng/bmng-topography-bathymetry/july/"
    "world.topo.bathy.200407.3x21600x10800.jpg")
TILE_SIZE = 256
DEFAULT_MAX_ZOOM = 6


def default_source():
    """Where the downloaded source image is kept: data/blue_marble.jpg."""
    return os.path.join(common.data_dir(), "blue_marble.jpg")


def download(url, dest):
    """Fetch a large image, showing rough progress so the user knows it works.

    Any equirectangular-projection world image works as a source. The
    requirements are:
      - 2:1 aspect ratio (longitude across, latitude down).
      - Edges at lon=+/-180 (date line) and lat=+/-90 (poles).
      - JPEG or PNG; Pillow opens both.
      - Resolution sets the highest useful zoom level: max_zoom is roughly
        log2(width / 256). For zoom 6 (US-state level) use 21600 x 10800 or
        larger. Smaller sources still work, just blurrier at high zoom.
    Drop the file at data/blue_marble.jpg (or pass --source PATH) to skip
    this download entirely.
    """
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print("Source already present: " + dest)
        return
    common.ensure_dir(os.path.dirname(os.path.abspath(dest)))
    temp = dest + ".part"
    print("Downloading " + url)
    with urllib.request.urlopen(url, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        last_report = 0
        with open(temp, "wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if done - last_report >= 4 * 1024 * 1024:
                    if total:
                        sys.stdout.write("  %d / %d MB (%.0f%%)\r"
                                         % (done // (1024 * 1024),
                                            total // (1024 * 1024),
                                            100.0 * done / total))
                    else:
                        sys.stdout.write("  %d MB\r" % (done // (1024 * 1024),))
                    sys.stdout.flush()
                    last_report = done
    sys.stdout.write("\n")
    os.replace(temp, dest)
    print("Saved " + dest)


def tile_path(zoom, x, y):
    return os.path.join(common.tiles_dir(), str(zoom), str(x), "%d.jpg" % y)


_PART_RE = re.compile(r"^\d+\.jpg\.part$")


def remove_partial_files():
    """Remove the temporary files an interrupted run left in the tiles folder.

    Returns how many were removed.
    """
    removed = 0
    top = common.tiles_dir()
    if not os.path.isdir(top):
        return 0
    for folder, _dirs, names in os.walk(top):
        for name in names:
            if _PART_RE.match(name):
                try:
                    os.remove(os.path.join(folder, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def write_tile(path, data):
    """Write one tile's bytes: to <path>.part, then renamed to path.

    An interrupted run leaves at most the .part file (removed by the next
    run), never a half-written tile under its real name. The rename is
    retried for a moment while the server is sending the old tile (Windows
    refuses to replace an open file); the next run repairs a tile that a
    crash cut short, so it is not flushed to the disk first.
    """
    common.write_file_atomic(path, data, tmp_path=path + ".part", fsync=False)


def slice_zoom(source, zoom, quality, force):
    """Write all tiles for one zoom level.

    Tiles that are there and whole are kept (unless force); missing ones are
    written and damaged ones (cut short) written again. Returns {"written",
    "repaired", "kept"}.
    """
    cols = 2 ** (zoom + 1)
    rows = 2 ** zoom
    src_w, src_h = source.size
    total = cols * rows
    counts = {"written": 0, "repaired": 0, "kept": 0}
    for x in range(cols):
        common.ensure_dir(os.path.join(common.tiles_dir(), str(zoom), str(x)))
        # Map the tile's column to a source x-range. Use float math so the
        # rounding does not accumulate; the last tile in a row still reaches
        # the source's right edge.
        sx0 = int(round(x * src_w / cols))
        sx1 = int(round((x + 1) * src_w / cols))
        for y in range(rows):
            out = tile_path(zoom, x, y)
            damaged = False
            if not force and os.path.exists(out):
                if imageinfo.jpeg_file_complete(out):
                    counts["kept"] += 1
                    continue
                damaged = True
            sy0 = int(round(y * src_h / rows))
            sy1 = int(round((y + 1) * src_h / rows))
            crop = source.crop((sx0, sy0, sx1, sy1))
            if crop.size != (TILE_SIZE, TILE_SIZE):
                crop = crop.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
            buffer = io.BytesIO()
            crop.save(buffer, format="JPEG", quality=quality, optimize=True)
            write_tile(out, buffer.getvalue())
            counts["repaired" if damaged else "written"] += 1
    print("  zoom %d: %d tiles (%d written, %d repaired, %d already there)"
          % (zoom, total, counts["written"], counts["repaired"], counts["kept"]))
    return counts


def write_manifest(max_zoom):
    path = os.path.join(common.ensure_dir(common.tiles_dir()), "manifest.json")
    common.write_file_atomic(
        path, json.dumps({"max_zoom": max_zoom, "tile_size": TILE_SIZE}) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Build a world-map tile pyramid from a NASA Blue Marble "
                    "JPEG. Existing tiles are kept, so reruns only fill gaps "
                    "(and repair tiles an interrupted run cut short).")
    parser.add_argument("--source", default=None,
                        help="Path to the source equirectangular JPEG "
                             "(default: data/blue_marble.jpg). Downloaded "
                             "from NASA if missing.")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="Where to download the source from when --source "
                             "is missing.")
    parser.add_argument("--max-zoom", type=int, default=DEFAULT_MAX_ZOOM,
                        help="Highest zoom level to generate (default: %d). "
                             "Each step quadruples the tile count."
                             % DEFAULT_MAX_ZOOM)
    parser.add_argument("--quality", type=int, default=82,
                        help="JPEG quality for output tiles (default: %(default)s).")
    parser.add_argument("--force", action="store_true",
                        help="Rewrite tiles even if they already exist.")
    args = parser.parse_args()
    source_path = args.source or default_source()

    if not os.path.exists(source_path):
        download(args.url, source_path)

    removed = remove_partial_files()
    if removed:
        print("Removed %d half-written tile(s) of an interrupted run." % removed)
    print("Slicing " + source_path)
    try:
        with Image.open(source_path) as source:
            source = source.convert("RGB")
            for zoom in range(args.max_zoom + 1):
                slice_zoom(source, zoom, args.quality, args.force)
    except KeyboardInterrupt:
        raise SystemExit("Stopped (Ctrl+C). The tiles written so far are kept; run "
                         "make_tiles.py again to fill in the rest.")
    write_manifest(args.max_zoom)
    print("Done. Wrote tiles to " + common.tiles_dir())


if __name__ == "__main__":
    main()
