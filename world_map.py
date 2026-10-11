#!/usr/bin/env python3
"""Generate a world map PNG with one pin per area where images were taken.

GPS positions come from the lat/lon columns of the database, which the
scanner fills from each image's EXIF data. Images whose pins would land near
each other on the output map are merged into a single pin labelled with the
number of photos in that cluster.

Run with no arguments to write `world_map.png` in the data folder, or pass a
path. The `generate_map_bytes` and `render_map` functions are intended for
the server, which can call them to build the image in memory.
"""

import argparse
import io
import math
import os

import common
import db


def _pillow():
    """Lazy Pillow import: this module's data helpers do not need it."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise SystemExit(
            "This tool needs Pillow. Install it with:\n"
            "    python -m pip install Pillow")
    return Image, ImageDraw, ImageFont


DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 800
DEFAULT_CLUSTER_RADIUS = 18

OCEAN_COLOR = (180, 210, 235)
GRID_COLOR = (155, 185, 210)
PIN_COLOR = (220, 40, 40)
PIN_OUTLINE = (255, 255, 255)
PIN_TEXT = (255, 255, 255)

# Default equirectangular base map (NASA Blue Marble, public domain). If the
# file is missing, a plain ocean fill with a lat/lon graticule is drawn so
# pins still land at the right spot.
DEFAULT_BASEMAP = os.path.join(common.BASE_DIR, "static", "worldmap.jpg")


def valid_position(lat, lon):
    """True for a usable GPS position (finite, in range, not exactly 0,0)."""
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lon)):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    # Cameras often write 0,0 when they have no GPS fix.
    return not (lat == 0.0 and lon == 0.0)


def latlon_to_pixel(lat, lon, width, height):
    """Equirectangular projection from (lat, lon) to image pixel."""
    x = (lon + 180.0) * (width / 360.0)
    y = (90.0 - lat) * (height / 180.0)
    if x < 0:
        x = 0
    if x >= width:
        x = width - 1
    if y < 0:
        y = 0
    if y >= height:
        y = height - 1
    return int(round(x)), int(round(y))


def collect_locations(conn=None, library_ids=None):
    """Return a list of (lat, lon) for every shown image with a GPS position."""
    return [(lat, lon) for _id, lat, lon in collect_located_images(conn, library_ids)]


def collect_located_images(conn=None, library_ids=None):
    """Return a list of (id, lat, lon) for every shown image with a GPS position.

    Only present (not missing), non-hidden images count. library_ids limits
    the result to those libraries; None means the enabled libraries in
    config.json. conn is a db.connect() connection (one is opened if None).
    """
    if library_ids is None:
        library_ids = [library["id"] for library
                       in common.enabled_libraries(common.load_config())
                       if library.get("id")]
    library_ids = list(library_ids)
    if not library_ids:
        return []
    close_after = False
    if conn is None:
        conn = db.connect()
        close_after = True
    try:
        rows = conn.execute(
            "SELECT id, lat, lon FROM images WHERE lat IS NOT NULL "
            "AND lon IS NOT NULL AND missing_since IS NULL AND hidden = 0 "
            "AND library_id IN (%s) ORDER BY id" % ",".join("?" * len(library_ids)),
            library_ids).fetchall()
    finally:
        if close_after:
            conn.close()
    return [(row[0], float(row[1]), float(row[2])) for row in rows
            if valid_position(row[1], row[2])]


def cluster_points(points, width, height, radius):
    """Greedy agglomerative clustering in output-pixel space.

    Returns a list of (x, y, count) tuples; nearby photos collapse to one pin.
    """
    clusters = []
    radius_sq = radius * radius
    for lat, lon in points:
        px, py = latlon_to_pixel(lat, lon, width, height)
        joined = False
        for cluster in clusters:
            cx = cluster[0] / cluster[2]
            cy = cluster[1] / cluster[2]
            if (cx - px) ** 2 + (cy - py) ** 2 <= radius_sq:
                cluster[0] += px
                cluster[1] += py
                cluster[2] += 1
                joined = True
                break
        if not joined:
            clusters.append([px, py, 1])
    return [(int(c[0] // c[2]), int(c[1] // c[2]), c[2]) for c in clusters]


def load_basemap(width, height, basemap_path):
    """Load a base map and resize to (width, height), or build a fallback."""
    Image, ImageDraw, _ = _pillow()
    if basemap_path and os.path.isfile(basemap_path):
        with Image.open(basemap_path) as src:
            img = src.convert("RGB")
            if img.size != (width, height):
                img = img.resize((width, height), Image.LANCZOS)
            return img
    img = Image.new("RGB", (width, height), OCEAN_COLOR)
    draw = ImageDraw.Draw(img)
    for lon in range(-180, 181, 30):
        x, _ = latlon_to_pixel(0, lon, width, height)
        draw.line([(x, 0), (x, height - 1)], fill=GRID_COLOR)
    for lat in range(-90, 91, 30):
        _, y = latlon_to_pixel(lat, 0, width, height)
        draw.line([(0, y), (width - 1, y)], fill=GRID_COLOR)
    return img


def _text_size(draw, text, font):
    """Return (width, height) of `text` using whichever API this Pillow has."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        return draw.textsize(text, font=font)


def _default_font():
    _, _, ImageFont = _pillow()
    try:
        return ImageFont.load_default(size=14)
    except TypeError:
        return ImageFont.load_default()


def draw_pin(draw, x, y, count, font):
    """Draw a single map pin, with a count label when more than one photo."""
    radius = 7
    if count > 1:
        radius = min(20, 7 + len(str(count)) * 3)
    draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                 fill=PIN_COLOR, outline=PIN_OUTLINE)
    if count > 1:
        label = str(count)
        tw, th = _text_size(draw, label, font)
        draw.text((x - tw / 2, y - th / 2), label, fill=PIN_TEXT, font=font)


def render_map(width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
               basemap_path=DEFAULT_BASEMAP,
               cluster_radius=DEFAULT_CLUSTER_RADIUS, points=None):
    """Render the world map and return a PIL.Image.

    `points` may be supplied to skip the database query (useful for tests or
    when the caller already has the coordinates in hand).
    """
    _, ImageDraw, _ = _pillow()
    if points is None:
        points = collect_locations()
    img = load_basemap(width, height, basemap_path)
    clusters = cluster_points(points, width, height, cluster_radius)
    draw = ImageDraw.Draw(img)
    font = _default_font()
    for x, y, count in clusters:
        draw_pin(draw, x, y, count, font)
    return img


def generate_map_bytes(width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                       basemap_path=DEFAULT_BASEMAP,
                       cluster_radius=DEFAULT_CLUSTER_RADIUS, points=None):
    """Return PNG bytes for the world map. Convenient for HTTP responses."""
    img = render_map(width, height, basemap_path, cluster_radius, points)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(
        description="Generate a world map PNG with one pin per area where "
                    "images were taken.")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output PNG path (default: world_map.png in the "
                             "data folder).")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="Output width in pixels.")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                        help="Output height in pixels.")
    parser.add_argument("--basemap", default=DEFAULT_BASEMAP,
                        help="Equirectangular base map PNG; a plain "
                             "graticule is used if missing.")
    parser.add_argument("--cluster-radius", type=int,
                        default=DEFAULT_CLUSTER_RADIUS,
                        help="Pixels within which pins are merged.")
    args = parser.parse_args()
    output = args.output or os.path.join(common.ensure_dir(common.data_dir()),
                                         "world_map.png")
    try:
        db.init()
        img = render_map(args.width, args.height, args.basemap,
                         args.cluster_radius)
    except (db.DatabaseError, common.ConfigError) as error:
        raise SystemExit("Error: %s" % error)
    img.save(output, format="PNG")
    print("Wrote " + output)


if __name__ == "__main__":
    main()
