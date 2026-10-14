#!/usr/bin/env python3
"""Areas of the map: the search by drawing on the map, read on the server's side.

On the map page an area is drawn (a rectangle, a free shape, the map's
view) or picked from the street maps (a street with some metres either
side, a named park, a point of interest with a circle around it); the
search then finds only the pictures taken in it. The search runs in the
browser (static/search-core.js, "Areas on the map"); the area travels as
short text in the address (area=, with its name in aname=) and in a smart
album's options (albums.py). This module reads that text the same way, so
the server can check what it stores and an export can tell where an area
is:

    kind [metres] "." part ["." part ...]

kind "b" is a rectangle (one part: its south-west and north-east corners),
"p" polygons (each part a ring of three points or more; inside any of them
counts), "l" lines with metres (1 to MAX_METRES) either side (each part a
line of one point or more: a street, or a point of interest when it is one
point). A part is the latitude and longitude of its points in 1/100000
degree, the first as it is and each next one as the change from the one
before (a longitude may so run past 180: a shape across the date line),
each number zig-zag coded (0, -1, 1, -2, ... are 0, 1, 2, 3, ...) and
written five bits a character, lowest first, in DIGITS, the character's 32
bit saying that more of the number follows. Only letters, digits, "-", "_"
and ".": nothing of it is escaped in an address. At most MAX_TEXT
characters, MAX_PARTS parts and MAX_POINTS points.

The edges of a shape are straight on the map (Web Mercator, as drawn), and
distances are measured on the map at the place's latitude. A place counts
when it is in the area at least depth metres deep (the pages ask half an
estimated place's radius, nothing of a picture's own position).

An area is location data: the request log never shows it (serve.py), an
export publishes a smart album's area only with its location option, and a
trip's export never one that comes near home (trip_export.py).

This module uses only the standard library.
"""

import math
import re

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
SCALE = 100000
MAX_TEXT = 6000
MAX_PARTS = 200
MAX_POINTS = 2000
MAX_METRES = 5000
NAME_LENGTH = 100
MAX_LAT = 85.0511287798
EARTH_METRES = 40075016.686
_AREA_RE = re.compile(r"^([bpl])([1-9]\d{0,3})?((?:\.[0-9A-Za-z_-]+)+)\Z")
_VALUES = {char: index for index, char in enumerate(DIGITS)}
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]+")


def world_y(lat):
    """0 at the map's north edge to 1 at its south edge (Web Mercator)."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    r = math.radians(lat)
    return (1 - math.log(math.tan(math.pi / 4 + r / 2)) / math.pi) / 2


def latitude_of(y):
    """The latitude of world_y's y."""
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))


def world_scale(lat):
    """Metres per world unit (the world's width on the map) at a latitude."""
    return EARTH_METRES * math.cos(math.radians(max(-MAX_LAT, min(MAX_LAT, lat))))


def _number(value):
    left = -2 * value - 1 if value < 0 else 2 * value
    out = []
    while left >= 32:
        out.append(DIGITS[32 + left % 32])
        left //= 32
    out.append(DIGITS[left])
    return "".join(out)


def _numbers(text):
    """The numbers of a part's text, or None."""
    out = []
    value = 0
    scale = 1
    for char in text:
        digit = _VALUES.get(char)
        if digit is None or scale > 10 ** 12:
            return None
        value += (digit % 32) * scale
        if digit < 32:
            out.append(-(value + 1) // 2 if value % 2 else value // 2)
            value = 0
            scale = 1
        else:
            scale *= 32
    return out if scale == 1 else None


class Area:
    """An area of the map (parse): kind ("b", "p" or "l"), metres (of "l"), parts (lists of
    (lat, lon); a rectangle as its four corners), text."""

    def __init__(self, text, kind, metres, parts):
        self.text = text
        self.kind = kind
        self.metres = metres
        self.parts = parts
        self.shapes = [[((lon + 180.0) / 360.0, world_y(lat)) for lat, lon in part]
                       for part in parts]
        xs = [x for shape in self.shapes for x, _y in shape]
        ys = [y for shape in self.shapes for _x, y in shape]
        self.box = (min(xs), min(ys), max(xs), max(ys))
        self.cx = (self.box[0] + self.box[2]) / 2

    def bounds(self):
        """(south, west, north, east) in degrees, with a line's metres around it (west may be
        before -180, or east past 180, for an area across the date line)."""
        x0, y0, x1, y1 = self.box
        north, south = latitude_of(y0), latitude_of(y1)
        west, east = x0 * 360 - 180, x1 * 360 - 180
        if self.kind == "l":
            lat_step = self.metres / (EARTH_METRES / 360.0)
            lon_step = lat_step / max(0.01, math.cos(math.radians(
                min(MAX_LAT, max(abs(north), abs(south))))))
            north, south = min(90.0, north + lat_step), max(-90.0, south - lat_step)
            west, east = west - lon_step, east + lon_step
        return south, west, north, east

    def contains(self, lat, lon, depth=0.0):
        """True when the place (degrees) is in the area at least depth metres deep."""
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or \
                not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90:
            return False
        scale = world_scale(lat)
        if scale <= 0:
            return False
        x = (lon + 180.0) / 360.0
        x += round(self.cx - x)
        y = world_y(lat)
        depth = max(0.0, depth or 0.0)
        reach = (self.metres - depth) / scale if self.kind == "l" else 0.0
        x0, y0, x1, y1 = self.box
        if reach < 0 or x < x0 - reach or x > x1 + reach or y < y0 - reach or y > y1 + reach:
            return False
        for shape in self.shapes:
            if self.kind == "l":
                if _distance(shape, x, y, False) <= reach:
                    return True
            elif _in_ring(shape, x, y) and (not depth or
                                            _distance(shape, x, y, True) * scale >= depth):
                return True
        return False


def _in_ring(ring, x, y):
    inside = False
    j = len(ring) - 1
    for k in range(len(ring)):
        xk, yk = ring[k]
        xj, yj = ring[j]
        if (yk > y) != (yj > y) and x < (xj - xk) * (y - yk) / (yj - yk) + xk:
            inside = not inside
        j = k
    return inside


def _distance(shape, x, y, closed):
    if len(shape) == 1:
        return math.hypot(shape[0][0] - x, shape[0][1] - y)
    best = float("inf")
    for k in range(0 if closed else 1, len(shape)):
        ax, ay = shape[k - 1]
        bx, by = shape[k]
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = ((x - ax) * dx + (y - ay) * dy) / length if length > 0 else 0.0
        t = min(1.0, max(0.0, t))
        best = min(best, math.hypot(ax + t * dx - x, ay + t * dy - y))
    return best


def parse(text):
    """The Area of a text, or None when it is not one (see the module notes)."""
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
        return None
    match = _AREA_RE.match(text)
    if not match:
        return None
    kind = match.group(1)
    metres = int(match.group(2)) if match.group(2) else 0
    if (kind == "l") != bool(match.group(2)) or metres > MAX_METRES:
        return None
    texts = match.group(3)[1:].split(".")
    if len(texts) > MAX_PARTS or (kind == "b" and len(texts) != 1):
        return None
    half = 180 * SCALE
    parts = []
    count = 0
    for part_text in texts:
        numbers = _numbers(part_text)
        if not numbers or len(numbers) % 2:
            return None
        count += len(numbers) // 2
        if count > MAX_POINTS:
            return None
        lat = lon = 0
        points = []
        for k in range(0, len(numbers), 2):
            lat += numbers[k]
            lon += numbers[k + 1]
            if not -half // 2 <= lat <= half // 2 or not -3 * half <= lon <= 3 * half or \
                    (k == 0 and not -half <= lon <= half):
                return None
            points.append((lat / SCALE, lon / SCALE))
        if kind == "p" and len(points) < 3:
            return None
        if kind == "b":
            if len(points) != 2:
                return None
            (south, west), (north, east) = points
            if not (north > south and east > west and east - west < 360):
                return None
            points = [(south, west), (north, west), (north, east), (south, east)]
        parts.append(points)
    area = Area(text, kind, metres, parts)
    if area.box[2] - area.box[0] >= 1:
        return None
    return area


def encode(kind, parts, metres=0):
    """The text of an area: kind "b", "p" or "l"; parts a list of [(lat, lon), ...] (a
    rectangle's one part [(south, west), (north, east)]); metres for "l". Raises ValueError
    when that makes no area."""
    if kind not in ("b", "p", "l") or not parts:
        raise ValueError("not an area")
    pieces = [kind + ("%d" % math.floor(metres + 0.5) if kind == "l" else "")]
    for part in parts:
        shift = 0
        last_lat = last_lon = 0
        out = []
        for index, (lat, lon) in enumerate(part):
            # (Rounded as the pages round: halves up.)
            lat, lon = int(math.floor(lat * SCALE + 0.5)), int(math.floor(lon * SCALE + 0.5))
            if index == 0:
                shift = int(math.floor(lon / (360.0 * SCALE) + 0.5)) * 360 * SCALE
            lon -= shift
            out.append(_number(lat - last_lat) + _number(lon - last_lon))
            last_lat, last_lon = lat, lon
        pieces.append("".join(out))
    text = ".".join(pieces)
    if parse(text) is None:
        raise ValueError("not an area")
    return text


def clean_name(value):
    """An area's name as an address or a smart album gives it: one line, at most
    NAME_LENGTH characters ("" for none)."""
    if not isinstance(value, str):
        return ""
    return _CONTROL_RE.sub(" ", value).strip()[:NAME_LENGTH]
