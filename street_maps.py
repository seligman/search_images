#!/usr/bin/env python3
"""Street-level maps offline: OpenStreetMap map tiles, downloaded once, served locally.

The map page, the trip pages' maps and the viewer's map of a drive draw
the NASA world image (make_tiles.py) as far as it has detail; past that,
where street maps were downloaded, they draw the streets (static/streets.js
draws them in the browser, from zoom DRAWN_FROM on). The street maps are
vector tiles of the Protomaps basemap (an OpenStreetMap-based map, one
PMTiles file for the whole world, rebuilt daily and listed in BUILDS_URL);
only the tiles around the places of the pictures (20 km around each, by
default; hidden pictures left out) and along the drive routes (5 km either
side) are downloaded, down to street level (zoom 15). Downloading is the
only time anything goes over the network: neither the server nor an
exported site asks another computer for anything while it runs.

    data/maps/streets.pmtiles   the tiles (a PMTiles v3 archive)
    data/maps/streets.json      what was downloaded: build, source, when,
                                the settings, sizes (no places)
    data/maps/plan.json         the last size check (numbers only)
    data/maps/cache/            the archive's directories while a download
                                is being planned (removed afterwards)
    data/maps/download.*        a download in progress (it continues where
                                it stopped when started again, from the
                                build it started with while that is there)

The download is planned first: the archive's directories (its table of
contents, some MB) are read to know where the wanted tiles are and how
large they are, and the size is shown before anything else comes. With
"whole_degrees" (the default) the directories of every tile of the
whole-degree squares (about 111 km tall) around the places are read, square
by square, then every tile of those squares is asked for and only the
wanted ones are kept, so the map's host learns no more than whole degrees
of latitude and longitude; without it only the wanted tiles are asked for
(a smaller download, but the host can then see which areas they are).

A download of LARGE_BYTES (5 GB) or more, to download or to keep, is
refused unless it is allowed explicitly (--allow-large; the Manage page
asks a second time), and one with too little free disk space for it is
refused before it starts.

The map data is OpenStreetMap's: "(c) OpenStreetMap contributors", under
the Open Database License (ODbL); the maps say so wherever they show
street tiles, with a link to the licence (LICENSE_TEXT: /maps/LICENSE.txt
on the server, maps/LICENSE.txt in an export that includes them).

    python street_maps.py [--status]          what is there (and how many
                                              located pictures it leaves out)
    python street_maps.py --plan              check the size of a download
    python street_maps.py --download [--yes] [--expect-bytes N] [--allow-large]
                                              download (shows the size first
                                              and asks, unless --yes; refused
                                              when it is well over N bytes)
    python street_maps.py --remove            delete the street maps

A download takes the job lock, like scan.py (and so does --remove); stop it
with the abort file (data/abort) and start it again to continue.
"""

import argparse
import base64
import bisect
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

import common

ATTRIBUTION = "\u00a9 OpenStreetMap contributors"
ATTRIBUTION_ASCII = "(c) OpenStreetMap contributors"
LICENSE_NAME = "Open Database License (ODbL) 1.0"
# The list of the Protomaps basemap's daily builds, and where they are.
BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BASE = "https://build.protomaps.com/"
# The renderer (static/streets.js) draws the layers of this major version.
SCHEMA_MAJOR = 4
USER_AGENT = "Search-Images (street maps)"
ARCHIVE_NAME = "streets.pmtiles"
MANIFEST_NAME = "streets.json"
PLAN_NAME = "plan.json"
DEFAULTS = {"max_zoom": 15, "radius_km": 20, "route_km": 5, "whole_degrees": True,
            "source": ""}
# (low, high) of the numbers of config.json "street_maps".
RANGES = {"max_zoom": (8, 15), "radius_km": (1, 200), "route_km": (0, 50)}
# The owner's rule: street maps of about this size or more (downloaded or kept) are
# not worth it; such a download is refused unless allowed explicitly.
LARGE_BYTES = 5 * 10 ** 9
# Free disk space a download leaves, at least (besides what it writes).
SPACE_MARGIN = 100 * 10 ** 6
# The pages draw street tiles from this zoom on (static/streets.js FROM_ZOOM; the
# world image below it); an export carries none of the zooms below it.
DRAWN_FROM = 9
KM_PER_DEGREE = 111.32
MAX_LAT = 85.0511287798
# Places closer than this (degrees) count as one spot when planning.
SPOT_DEGREES = 0.005
# Ask for byte ranges whose gaps are at most this long in one request, and
# no request longer than MAX_RANGE.
MERGE_GAP = 128 * 1024
MAX_RANGE = 16 * 1024 * 1024
# A download may come out this much larger than the size agreed to (and 10%).
GROWTH_SLACK = 50 * 10 ** 6
READ_CHUNK = 1024 * 1024
TIMEOUT = 60
TRIES = 3
# PMTiles v3 (https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)
HEADER_SIZE = 127
ROOT_LIMIT = 16384 - HEADER_SIZE
# Tile entries a leaf directory holds at least (when the root alone is too large).
LEAF_ENTRIES = 4096
COMPRESSION_NONE = 1
COMPRESSION_GZIP = 2
TILE_TYPE_MVT = 1
# The tile scripts (served and exported) call this function of streets.js.
SCRIPT_GLOBAL = "STREETS_DATA"
INDEX_FORMAT = 1


class MapsError(Exception):
    """The street maps cannot be planned, downloaded or read; the message says why."""


# ---------------------------------------------------------------------------
# Settings and files

def settings(config):
    """config.json "street_maps" checked, with defaults: {"max_zoom" (8-15), "radius_km"
    (around each place), "route_km" (either side of drive routes), "whole_degrees" (ask
    for whole-degree squares), "source" (a PMTiles address; "" = the newest Protomaps
    build)}."""
    stored = config.get("street_maps") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    result = dict(DEFAULTS)
    for key, (low, high) in RANGES.items():
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and \
                math.isfinite(value) and low <= value <= high:
            result[key] = int(value) if key == "max_zoom" else float(value)
    if isinstance(stored.get("whole_degrees"), bool):
        result["whole_degrees"] = stored["whole_degrees"]
    source = stored.get("source")
    if isinstance(source, str) and re.match(r"^https?://\S+$", source.strip()):
        result["source"] = source.strip()
    return result


def maps_dir():
    return os.path.join(common.data_dir(), "maps")


def archive_path():
    return os.path.join(maps_dir(), ARCHIVE_NAME)


def manifest_path():
    return os.path.join(maps_dir(), MANIFEST_NAME)


def plan_path():
    return os.path.join(maps_dir(), PLAN_NAME)


def cache_dir():
    return os.path.join(maps_dir(), "cache")


# Files a download or a size check that was cut short (the process ended) may leave in
# data/maps: removed with the street maps, and at the start of every download.
LEFTOVERS = ("write.data", ARCHIVE_NAME + ".tmp", ARCHIVE_NAME + ".data", PLAN_NAME + ".tmp",
             MANIFEST_NAME + ".tmp", "download.json.tmp")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(temp, path)


def human_bytes(value):
    """"1.4 GB", "551 MB", "12 kB" (decimal units, as download sizes are given)."""
    value = float(value or 0)
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if value >= size:
            shown = value / size
            return ("%.1f %s" if shown < 10 else "%.0f %s") % (shown, unit)
    return "%d bytes" % value


# ---------------------------------------------------------------------------
# Tiles: zoom, column, row (Web Mercator, row 0 at the top) and PMTiles ids

def tile_id(z, x, y):
    """The PMTiles tile id of a tile (its place on the Hilbert curve of its zoom, after
    every tile of the zooms above)."""
    acc = ((1 << (2 * z)) - 1) // 3
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def tile_zxy(tid):
    """(z, x, y) of a PMTiles tile id (tile_id's inverse)."""
    z = 0
    acc = 0
    while True:
        count = 1 << (2 * z)
        if tid < acc + count:
            break
        acc += count
        z += 1
    d = tid - acc
    n = 1 << z
    x = y = 0
    s = 1
    while s < n:
        rx = 1 & (d // 2)
        ry = 1 & (d ^ rx)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        d //= 4
        s *= 2
    return z, x, y


def lat_row(lat, z):
    """The row of a latitude at zoom z (the edge rows beyond about 85 degrees)."""
    n = 1 << z
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return min(n - 1, max(0, int(math.floor(y))))


def lon_column(lon, z):
    n = 1 << z
    return int(math.floor((lon + 180.0) / 360.0 * n)) % n


def tile_bounds(z, x, y):
    """(south, west, north, east) of a tile, in degrees."""
    n = float(1 << z)

    def lat(row):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def box_around(lat, lon, km):
    """(south, west, north, east) of the square km around a place (west may be below
    -180 and east above 180 near the date line)."""
    dlat = km / KM_PER_DEGREE
    dlon = km / (KM_PER_DEGREE * max(0.05, math.cos(math.radians(lat))))
    return (max(-MAX_LAT, lat - dlat), lon - dlon, min(MAX_LAT, lat + dlat), lon + dlon)


def box_tiles(box, z, out):
    """Add the (x, y) of the tiles at zoom z a box covers to the set out."""
    south, west, north, east = box
    n = 1 << z
    y0, y1 = lat_row(north, z), lat_row(south, z)
    if east - west >= 360.0:
        columns = range(n)
    else:
        first = int(math.floor((west + 180.0) / 360.0 * n))
        last = int(math.floor((east + 180.0) / 360.0 * n))
        columns = [column % n for column in range(first, last + 1)]
    for x in columns:
        for y in range(y0, y1 + 1):
            out.add((x, y))


def with_ancestors(tiles, max_zoom):
    """{zoom: set of (x, y)} of tiles at max_zoom and every tile above them."""
    levels = {max_zoom: set(tiles)}
    for z in range(max_zoom - 1, -1, -1):
        levels[z] = {(x >> 1, y >> 1) for x, y in levels[z + 1]}
    return levels


def whole_degree_boxes(boxes):
    """The whole-degree squares (south, west, north, east) any of the boxes touches."""
    cells = set()
    for south, west, north, east in boxes:
        for lat in range(int(math.floor(south)), int(math.floor(north)) + 1):
            for lon in range(int(math.floor(west)), int(math.floor(east)) + 1):
                cells.add((lat, ((lon + 180) % 360) - 180))
    return [(max(-MAX_LAT, lat), lon, min(MAX_LAT, lat + 1), lon + 1)
            for lat, lon in sorted(cells) if lat + 1 > -MAX_LAT and lat < MAX_LAT]


def tile_ids(levels):
    """Sorted tile ids of {zoom: set of (x, y)}."""
    return sorted(tile_id(z, x, y) for z, tiles in levels.items() for x, y in tiles)


def index_rows(levels):
    """The page's index of the tiles there are: {"z": [[y, x0, x1, x0, x1, ...]]} (runs
    of columns x0..x1 of each row y, in order)."""
    rows = {}
    for z, tiles in sorted(levels.items()):
        by_row = {}
        for x, y in tiles:
            by_row.setdefault(y, []).append(x)
        listed = []
        for y in sorted(by_row):
            columns = sorted(by_row[y])
            runs = [y]
            start = prev = columns[0]
            for x in columns[1:]:
                if x != prev + 1:
                    runs.extend((start, prev))
                    start = x
                prev = x
            runs.extend((start, prev))
            listed.append(runs)
        if listed:
            rows[str(z)] = listed
    return rows


# ---------------------------------------------------------------------------
# PMTiles v3: reading and writing

def _varint(data, pos):
    shift = 0
    value = 0
    while True:
        if pos >= len(data):
            raise MapsError("The map's directory is cut short.")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _put_varint(out, value):
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _decompress(raw, compression):
    if compression == COMPRESSION_GZIP:
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError, ValueError) as error:
            raise MapsError("A part of the map file is damaged (%s)." % error)
    if compression in (0, COMPRESSION_NONE):
        return raw
    raise MapsError("The map file uses a compression this program cannot read (%d)."
                    % compression)


def parse_header(data):
    """The PMTiles v3 header (the first 127 bytes) as a dict."""
    if len(data) < HEADER_SIZE or data[:7] != b"PMTiles":
        raise MapsError("This is not a PMTiles map file.")
    if data[7] != 3:
        raise MapsError("This PMTiles map file is of version %d; version 3 is needed."
                        % data[7])
    keys = ("root_offset", "root_length", "metadata_offset", "metadata_length",
            "leaf_offset", "leaf_length", "data_offset", "data_length", "addressed",
            "entries", "contents")
    header = dict(zip(keys, struct.unpack_from("<11Q", data, 8)))
    (header["clustered"], header["internal_compression"], header["tile_compression"],
     header["tile_type"], header["min_zoom"], header["max_zoom"]) = \
        struct.unpack_from("<6B", data, 96)
    bounds = struct.unpack_from("<4i", data, 102)
    header["bounds"] = [value / 1e7 for value in bounds]
    header["center_zoom"] = data[118]
    header["center"] = [value / 1e7 for value in struct.unpack_from("<2i", data, 119)]
    return header


def parse_directory(raw, compression):
    """[(tile_id, run_length, offset, length)] of a directory (run_length 0: a leaf
    directory at offset in the leaf section)."""
    data = _decompress(raw, compression)
    count, pos = _varint(data, 0)
    ids, runs, lengths, offsets = [], [], [], []
    last = 0
    for _ in range(count):
        delta, pos = _varint(data, pos)
        last += delta
        ids.append(last)
    for _ in range(count):
        value, pos = _varint(data, pos)
        runs.append(value)
    for _ in range(count):
        value, pos = _varint(data, pos)
        lengths.append(value)
    for index in range(count):
        value, pos = _varint(data, pos)
        if value == 0 and index > 0:
            offsets.append(offsets[index - 1] + lengths[index - 1])
        else:
            offsets.append(value - 1)
    return list(zip(ids, runs, offsets, lengths))


def serialize_directory(entries):
    """A directory's bytes (gzip) from [(tile_id, run_length, offset, length)]."""
    out = bytearray()
    _put_varint(out, len(entries))
    last = 0
    for entry in entries:
        _put_varint(out, entry[0] - last)
        last = entry[0]
    for entry in entries:
        _put_varint(out, entry[1])
    for entry in entries:
        _put_varint(out, entry[3])
    for index, entry in enumerate(entries):
        if index > 0 and entry[2] == entries[index - 1][2] + entries[index - 1][3]:
            _put_varint(out, 0)
        else:
            _put_varint(out, entry[2] + 1)
    return gzip.compress(bytes(out), compresslevel=9, mtime=0)


def build_directories(entries):
    """(root bytes, leaf section bytes) for tile entries [(tile_id, run_length, offset,
    length)] in tile id order: one root directory when it fits in the first 16 kB, else
    leaf directories of a size that lets the root fit (as go-pmtiles does)."""
    root = serialize_directory(entries)
    if len(root) <= ROOT_LIMIT:
        return root, b""
    leaf_size = max(LEAF_ENTRIES, len(entries) // 3500)
    while True:
        leaves = bytearray()
        root_entries = []
        for start in range(0, len(entries), leaf_size):
            chunk = entries[start:start + leaf_size]
            data = serialize_directory(chunk)
            root_entries.append((chunk[0][0], 0, len(leaves), len(data)))
            leaves.extend(data)
        root = serialize_directory(root_entries)
        if len(root) <= ROOT_LIMIT:
            return root, bytes(leaves)
        leaf_size = int(leaf_size * 1.2) + 1


def write_pmtiles(path, entries, data_path, data_size, addressed, contents, zooms, metadata,
                  bounds, center=None, clustered=True):
    """Write a PMTiles v3 file at path (as <path>.tmp first, then put in place): its
    header, directories and metadata, then the tile data, copied from the file
    data_path (data_size bytes). entries: [(tile_id, run_length, offset in the data,
    length)] in tile id order; addressed: the tiles they name; contents: the
    different tiles; zooms: (min, max); bounds: (west, south, east, north); clustered:
    the data is in tile id order. Returns the file's size."""
    root, leaves = build_directories(entries)
    meta = gzip.compress(json.dumps(metadata, sort_keys=True).encode("utf-8"),
                         compresslevel=9, mtime=0)
    root_offset = HEADER_SIZE
    meta_offset = root_offset + len(root)
    leaf_offset = meta_offset + len(meta)
    data_offset = leaf_offset + len(leaves)
    west, south, east, north = bounds
    if center is None:
        center = ((west + east) / 2.0, (south + north) / 2.0)
    header = bytearray(b"PMTiles\x03")
    header += struct.pack("<11Q", root_offset, len(root), meta_offset, len(meta),
                          leaf_offset, len(leaves), data_offset, data_size,
                          addressed, len(entries), contents)
    header += struct.pack("<6B", 1 if clustered else 0, COMPRESSION_GZIP, COMPRESSION_GZIP,
                          TILE_TYPE_MVT, zooms[0], zooms[1])
    header += struct.pack("<4i", *[int(round(value * 1e7))
                                   for value in (west, south, east, north)])
    header += struct.pack("<B2i", zooms[1], int(round(center[0] * 1e7)),
                          int(round(center[1] * 1e7)))
    assert len(header) == HEADER_SIZE
    temp = path + ".tmp"
    try:
        with open(temp, "wb") as out:
            out.write(bytes(header))
            out.write(root)
            out.write(meta)
            out.write(leaves)
            with open(data_path, "rb") as source:
                left = data_size
                while left > 0:
                    piece = source.read(min(READ_CHUNK, left))
                    if not piece:
                        raise MapsError("The downloaded map data is cut short; download "
                                        "again.")
                    out.write(piece)
                    left -= len(piece)
        replace_file(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return data_offset + data_size


class ArchiveWriter:
    """Write a PMTiles v3 file: add(tile_id, data) in tile id order (the same data
    twice is kept once), then finish(metadata, bounds). The file appears under its
    name only when finished (written as <path>.tmp first)."""

    def __init__(self, path, spool=None):
        self.path = path
        self.spool_path = spool or path + ".data"
        self.spool = open(self.spool_path, "wb")
        self.size = 0
        self.by_hash = {}
        self.entries = []           # [tile_id, run_length, offset, length]
        self.addressed = 0
        self.min_zoom = None
        self.max_zoom = None

    def add(self, tid, data, key=None):
        """Add a tile (ids must grow). key: what makes two tiles the same (default:
        a hash of data)."""
        if self.entries and tid <= self.entries[-1][0] + self.entries[-1][1] - 1:
            raise ValueError("tile ids must grow")
        key = key if key is not None else hashlib.sha1(data).digest()
        at = self.by_hash.get(key)
        if at is None:
            at = (self.size, len(data))
            self.spool.write(data)
            self.size += len(data)
            self.by_hash[key] = at
        last = self.entries[-1] if self.entries else None
        if last is not None and last[0] + last[1] == tid and last[2] == at[0] and \
                last[3] == at[1]:
            last[1] += 1
        else:
            self.entries.append([tid, 1, at[0], at[1]])
        self.addressed += 1
        z = tile_zxy(tid)[0]
        self.min_zoom = z if self.min_zoom is None else min(self.min_zoom, z)
        self.max_zoom = z if self.max_zoom is None else max(self.max_zoom, z)

    def finish(self, metadata, bounds, center=None):
        """Write the file; returns its size. bounds: (west, south, east, north)."""
        self.spool.close()
        size = write_pmtiles(self.path, [tuple(entry) for entry in self.entries],
                             self.spool_path, self.size, self.addressed, len(self.by_hash),
                             (self.min_zoom or 0, self.max_zoom or 0), metadata, bounds,
                             center)
        try:
            os.remove(self.spool_path)
        except OSError:
            pass
        return size

    def discard(self):
        try:
            self.spool.close()
        except OSError:
            pass
        for path in (self.spool_path, self.path + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass


class Archive:
    """A PMTiles v3 archive read through read(offset, length) -> bytes: its header,
    metadata, and the place of each tile. Leaf directories are kept once read (up to
    LEAF_CACHE of them; leaf_store, when given, is asked first and told what was
    read: an on-disk cache of a remote archive's directories)."""

    LEAF_CACHE = 256

    def __init__(self, read, leaf_store=None):
        self.read = read
        self.leaf_store = leaf_store
        first = read(0, 16384)
        self.header = parse_header(first)
        h = self.header
        if h["root_offset"] + h["root_length"] <= len(first):
            raw = first[h["root_offset"]:h["root_offset"] + h["root_length"]]
        else:
            raw = read(h["root_offset"], h["root_length"])
        self.root = parse_directory(raw, h["internal_compression"])
        self.root_ids = [entry[0] for entry in self.root]
        self.leaves = {}
        self.lock = threading.Lock()
        self.leaf_bytes = 0

    def metadata(self):
        h = self.header
        if not h["metadata_length"]:
            return {}
        raw = self.read(h["metadata_offset"], h["metadata_length"])
        try:
            value = json.loads(_decompress(raw, h["internal_compression"]).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _leaf(self, offset, length):
        with self.lock:
            found = self.leaves.get(offset)
        if found is not None:
            return found
        raw = self.leaf_store.get(offset, length) if self.leaf_store else None
        if raw is None:
            raw = self.read(self.header["leaf_offset"] + offset, length)
            self.leaf_bytes += len(raw)
            if self.leaf_store:
                self.leaf_store.put(offset, length, raw)
        entries = parse_directory(raw, self.header["internal_compression"])
        found = ([entry[0] for entry in entries], entries)
        with self.lock:
            if len(self.leaves) >= self.LEAF_CACHE:
                self.leaves.pop(next(iter(self.leaves)))
            self.leaves[offset] = found
        return found

    def find(self, tid):
        """(offset in the tile data section, length) of a tile id, or None."""
        ids, entries = self.root_ids, self.root
        for _depth in range(4):
            index = bisect.bisect_right(ids, tid) - 1
            if index < 0:
                return None
            start, run, offset, length = entries[index]
            if run == 0:
                ids, entries = self._leaf(offset, length)
                continue
            if tid < start + run:
                return offset, length
            return None
        return None

    def get(self, z, x, y):
        """The tile's bytes as stored (gzip MVT), or None when the archive has none."""
        found = self.find(tile_id(z, x, y))
        if found is None or not found[1]:
            return None
        return self.read(self.header["data_offset"] + found[0], found[1])

    def entries(self):
        """Every tile entry [(tile_id, run_length, offset, length)] in tile id order."""
        result = []
        for entry in self.root:
            if entry[1] == 0:
                result.extend(self._leaf(entry[2], entry[3])[1])
            else:
                result.append(entry)
        return [entry for entry in result if entry[1] > 0]


def _file_reader(path):
    """read(offset, length) of a file, opened for each read: a file kept open could
    not be replaced (a new download) or deleted on Windows while the server runs."""
    def read(offset, length):
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(length)
    read.close = lambda: None
    return read


def replace_file(source, target, tries=40):
    """os.replace(source, target), tried again for a few seconds while another program
    (a virus checker, a server reading a tile) has target open."""
    for attempt in range(tries):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.1)


def remove_file(path, tries=40):
    """os.remove(path), tried again for a few seconds while it is open; True if
    removed."""
    for attempt in range(tries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.1)
    return False


def open_local(path):
    """An Archive of a PMTiles file on this computer (close with archive.read.close())."""
    read = _file_reader(path)
    try:
        return Archive(read)
    except Exception:
        read.close()
        raise


# ---------------------------------------------------------------------------
# The remote archive (only while planning and downloading)

def _http_get(url, first=None, last=None, timeout=TIMEOUT):
    """(response, headers) of a GET (a range request when first is given). The caller
    reads and closes the response."""
    headers = {"User-Agent": USER_AGENT}
    if first is not None:
        headers["Range"] = "bytes=%d-%d" % (first, last)
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


class RemoteFile:
    """Range reads of a file on a web server that answers range requests. It must stay
    the same file throughout (its size and ETag are checked on every read)."""

    def __init__(self, url, should_stop=None):
        self.url = url
        self.size = None
        self.etag = None
        self.requests = 0
        self.received = 0
        self.should_stop = should_stop

    def read(self, offset, length, sink=None):
        """length bytes from offset (fewer at the end of the file); with sink, each
        piece is passed to sink(bytes) instead and None is returned."""
        if length <= 0:
            return b""
        error = None
        for attempt in range(TRIES):
            if attempt:
                time.sleep(2 * attempt)
            try:
                return self._read(offset, length, sink)
            except MapsError:
                raise
            except (urllib.error.URLError, OSError, ValueError) as caught:
                error = caught
                if sink is not None:
                    break       # (pieces were passed on already)
        raise MapsError("Could not read the map file %s: %s" % (self.url, _reason(error)))

    def _read(self, offset, length, sink):
        with _http_get(self.url, offset, offset + length - 1) as response:
            self.requests += 1
            status = getattr(response, "status", 200)
            ranges = response.headers.get("Content-Range", "")
            match = re.match(r"^bytes (\d+)-(\d+)/(\d+|\*)$", ranges.strip())
            if status != 206 or not match or int(match.group(1)) != offset:
                raise MapsError("The map's server does not answer range requests (%s); it "
                                "cannot be used." % self.url)
            total = int(match.group(3)) if match.group(3) != "*" else None
            etag = response.headers.get("ETag")
            if self.size is None:
                self.size, self.etag = total, etag
            elif (total is not None and total != self.size) or \
                    (etag and self.etag and etag != self.etag):
                raise MapsError("The map file changed on its server while it was being "
                                "read; check the size again.")
            parts = []
            got = 0
            while got < length:
                if self.should_stop is not None and self.should_stop():
                    raise Stopped()
                piece = response.read(min(READ_CHUNK, length - got))
                if not piece:
                    break
                got += len(piece)
                self.received += len(piece)
                if sink is not None:
                    sink(piece)
                else:
                    parts.append(piece)
            expected = min(length, (self.size - offset) if self.size else length)
            if got < expected:
                raise MapsError("The map's server sent %d of %d bytes; try again." % (
                    got, expected))
            return None if sink is not None else b"".join(parts)


class Stopped(Exception):
    """A download was asked to stop."""


def _reason(error):
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP %d" % error.code
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error)


class LeafCache:
    """The leaf directories of a remote archive, kept on disk (data/maps/cache/<key>/)
    between the size check and the download."""

    def __init__(self, url):
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        self.folder = os.path.join(cache_dir(), key)

    def _path(self, offset, length):
        return os.path.join(self.folder, "%d-%d.bin" % (offset, length))

    def get(self, offset, length):
        try:
            with open(self._path(offset, length), "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        return data if len(data) == length else None

    def put(self, offset, length, data):
        try:
            os.makedirs(self.folder, exist_ok=True)
            path = self._path(offset, length)
            with open(path + ".tmp", "wb") as handle:
                handle.write(data)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


def newest_build(builds_url=BUILDS_URL):
    """{"url", "key", "size", "version"} of the newest build of the basemap whose
    layers static/streets.js draws. Raises MapsError."""
    try:
        with _http_get(builds_url) as response:
            builds = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise MapsError("Could not read the list of map builds (%s): %s" % (
            builds_url, _reason(error)))
    usable = []
    for build in builds if isinstance(builds, list) else []:
        if not isinstance(build, dict) or not isinstance(build.get("key"), str):
            continue
        version = str(build.get("version") or "")
        major = version.split(".")[0]
        if major.isdigit() and int(major) == SCHEMA_MAJOR and \
                re.match(r"^\d{8}\.pmtiles$", build["key"]):
            usable.append(build)
    if not usable:
        raise MapsError("The list of map builds names no build this program can draw "
                        "(version %d)." % SCHEMA_MAJOR)
    build = max(usable, key=lambda item: item["key"])
    base = builds_url.rsplit("/", 1)[0] + "/" if builds_url != BUILDS_URL else BUILD_BASE
    return {"url": base + build["key"], "key": build["key"],
            "size": build.get("size") if isinstance(build.get("size"), int) else None,
            "version": str(build.get("version"))}


# ---------------------------------------------------------------------------
# What to download: the places of the pictures and the drive routes

def coverage_points(conn, config):
    """(spots, route points): [(lat, lon)] of the located pictures and estimated places
    of the enabled libraries, merged to about SPOT_DEGREES, and of the drive routes, a
    point at least every kilometre: the places the map shows (as /api/locations and
    /api/routes do; never those of hidden pictures, as the street maps' coverage is
    seen by other computers too)."""
    import place_estimates
    import tracks
    import world_map
    library_ids = [library["id"] for library in common.enabled_libraries(config)
                   if library.get("id")]
    spots = {}

    def add(lat, lon):
        if world_map.valid_position(lat, lon):
            key = (round(lat / SPOT_DEGREES), round(lon / SPOT_DEGREES))
            spots.setdefault(key, (float(lat), float(lon)))
    if library_ids:
        for _id, lat, lon in world_map.collect_located_images(conn, library_ids):
            add(lat, lon)
        for _id, lat, lon, _radius, _owner in place_estimates.located(conn, library_ids):
            add(lat, lon)
    route_points = []
    for route in tracks.routes(conn, config, library_ids) if library_ids else []:
        for clip in route.get("c", []):
            points = [(lat, lon) for _at, lat, lon, _speed in tracks.decode(clip.get("p"))]
            route_points.extend(densify(points, 1.0))
    return sorted(spots.values()), route_points


def _km(first, second):
    lat1, lon1, lat2, lon2 = map(math.radians, (first[0], first[1], second[0], second[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def densify(points, step_km):
    """The points of a line with more in between, so that none are more than step_km
    apart."""
    result = []
    for index, point in enumerate(points):
        if index:
            before = points[index - 1]
            km = _km(before, point)
            parts = int(km // step_km)
            for part in range(1, parts + 1):
                share = part / float(parts + 1)
                result.append((before[0] + (point[0] - before[0]) * share,
                               before[1] + (point[1] - before[1]) * share))
        result.append(point)
    return result


def coverage_boxes(spots, route_points, radius_km, route_km):
    """The boxes to cover: radius_km around each spot, route_km around each route
    point (none with route_km 0)."""
    boxes = [box_around(lat, lon, radius_km) for lat, lon in spots]
    if route_km > 0:
        boxes.extend(box_around(lat, lon, route_km) for lat, lon in route_points)
    return boxes


def coverage_levels(boxes, max_zoom):
    """{zoom: set of (x, y)}: the tiles at max_zoom the boxes touch, and their ancestors."""
    tiles = set()
    for box in boxes:
        box_tiles(box, max_zoom, tiles)
    return with_ancestors(tiles, max_zoom)


def _merge_ranges(spans, gap=None, limit=None):
    """[(offset, length)] of byte ranges covering every (offset, length) of spans,
    joining those at most gap (MERGE_GAP) apart, up to limit (MAX_RANGE) bytes a range;
    spans that overlap are always joined (no byte is asked for twice), whatever the
    limit (the download reads a longer range in pieces)."""
    gap = MERGE_GAP if gap is None else gap
    limit = MAX_RANGE if limit is None else limit
    ranges = []
    for offset, length in sorted(set(spans)):
        if not length:
            continue
        if ranges:
            start, size = ranges[-1]
            end = start + size
            if offset < end or (offset <= end + gap and
                                max(end, offset + length) - start <= limit):
                ranges[-1] = (start, max(end, offset + length) - start)
                continue
        ranges.append((offset, length))
    return ranges


def stopped_download():
    """The download that stopped half way (data/maps/download.json, with its spool
    file): {"key", "ranges_done", "ranges" (of how many), "source", "build",
    "version"}, or None."""
    progress = _read_json(_progress_path())
    if not progress or not os.path.isfile(_spool_path()):
        return None
    return progress


def _open_remote(url, ctx):
    """(RemoteFile, Archive) of the remote archive at url (its header and root read)."""
    remote = RemoteFile(url, should_stop=ctx.should_stop if ctx is not None else None)
    return remote, Archive(remote.read, leaf_store=LeafCache(url))


def plan(conn, config, ctx=None, source=None, archive=None):
    """Work out a download (reads the remote archive's directories, not its tiles):
    {"source", "build", "version", "mode" ("whole_degrees" or "exact"), "max_zoom",
    "radius_km", "route_km", "spots", "route_points", "tiles" (to keep), "keep_bytes",
    "download_bytes" (tiles asked for), "requests", "index_bytes" (directories read),
    "squares" (whole-degree squares asked for), "large" (keep_bytes or download_bytes
    at least LARGE_BYTES), "at"} with "_keep" ({tile id: (offset, length)}), "_ranges"
    and "_archive" for download().

    The archive: source, else config.json street_maps.source, else the build of a
    download that stopped half way (while it can still be read: so that it goes on
    where it stopped), else the newest build. With whole_degrees the directories are
    read square by square (in the same order for any places in the same squares), and
    only then those of the tiles to keep are looked up (already read). Raises
    MapsError."""
    options = settings(config)
    log = ctx.log if ctx is not None else (lambda *_args, **_kw: None)
    spots, route_points = coverage_points(conn, config)
    if not spots and not route_points:
        raise MapsError("No picture has a place (a GPS position or an estimated place) and "
                        "there are no drive routes: there is nothing to download maps for.")
    build = None
    remote = None
    if archive is None:
        url = source or options["source"]
        if not url:
            stopped = stopped_download()
            if stopped and isinstance(stopped.get("source"), str) and stopped["source"]:
                try:
                    remote, archive = _open_remote(stopped["source"], ctx)
                    url = stopped["source"]
                    build = {"key": str(stopped.get("build") or os.path.basename(url)),
                             "version": str(stopped.get("version") or "")}
                    log("A download stopped half way, of the build %s: that build is "
                        "checked again, so that the download goes on where it stopped."
                        % build["key"])
                except MapsError as error:
                    log("The build of the download that stopped half way (%s) cannot be "
                        "read any more (%s): the newest build is checked, and a download "
                        "starts over." % (stopped.get("build") or "?", error), "warn")
                    remote = archive = None
            if archive is None:
                build = newest_build()
                url = build["url"]
        if archive is None:
            remote, archive = _open_remote(url, ctx)
    else:
        url = source or "local"
    h = archive.header
    if h["tile_type"] != TILE_TYPE_MVT:
        raise MapsError("The map file does not hold vector tiles.")
    max_zoom = min(options["max_zoom"], h["max_zoom"])
    boxes = coverage_boxes(spots, route_points, options["radius_km"], options["route_km"])
    levels = coverage_levels(boxes, max_zoom)
    if ctx is not None:
        ctx.stage("street maps size", total=0)
    log("Street maps: %d place(s) of pictures and %d point(s) of drive routes, %s km "
        "around the places and %s km along the routes, zoom 0-%d: %d tiles."
        % (len(spots), len(route_points), _num(options["radius_km"]),
           _num(options["route_km"]), max_zoom, sum(len(tiles) for tiles in levels.values())))
    spans = []
    squares = []
    if options["whole_degrees"]:
        # Every tile of each square, square by square, first: the directories asked
        # for are those of the squares, in the squares' order, whatever the places in
        # them (the tiles to keep are looked up after, in directories already read).
        squares = whole_degree_boxes(boxes)
        for square in squares:
            if ctx is not None and ctx.should_stop():
                raise Stopped()
            # (Joined square by square: a square at zoom 15 has some 20,000 tiles.)
            found_here = []
            for tid in tile_ids(coverage_levels([square], max_zoom)):
                found = archive.find(tid)
                if found is not None and found[1]:
                    found_here.append(found)
            spans.extend(_merge_ranges(found_here))
    keep = {}
    for tid in tile_ids(levels):
        found = archive.find(tid)
        if found is not None and found[1]:
            keep[tid] = found
    spans.extend(keep.values())
    ranges = _merge_ranges(spans)
    unique = {}
    for offset, length in keep.values():
        unique[offset] = length
    result = {
        "source": url, "build": build["key"] if build else os.path.basename(url),
        "version": (build or {}).get("version") or str(archive.metadata().get("version", "")),
        "mode": "whole_degrees" if options["whole_degrees"] else "exact",
        "max_zoom": max_zoom, "radius_km": options["radius_km"],
        "route_km": options["route_km"], "spots": len(spots),
        "route_points": len(route_points), "tiles": len(keep),
        "keep_bytes": sum(unique.values()), "download_bytes": sum(size for _o, size in ranges),
        "requests": len(ranges), "index_bytes": archive.leaf_bytes + 16384,
        "squares": len(squares), "at": time.time(),
    }
    result["large"] = max(result["keep_bytes"], result["download_bytes"]) >= LARGE_BYTES
    result["_keep"] = keep
    result["_ranges"] = ranges
    result["_archive"] = archive
    result["_remote"] = remote
    return result


def _num(value):
    return ("%d" % value) if float(value) == int(value) else ("%.1f" % value)


def public_plan(found):
    """A plan without its internal parts (as kept in plan.json and shown)."""
    return {key: value for key, value in found.items() if not key.startswith("_")}


def large_words(found):
    """Why a plan of LARGE_BYTES or more is not downloaded unless allowed."""
    return ("The street maps would be %s to download and %s to keep: %s or more. They are "
            "not downloaded unless that is allowed (--allow-large on the command line); a "
            "smaller radius_km, or max_zoom 14, in config.json \"street_maps\" makes them "
            "much smaller." % (human_bytes(found["download_bytes"]),
                               human_bytes(found["keep_bytes"]), human_bytes(LARGE_BYTES)))


def plan_words(found):
    """One paragraph about a plan, for the log and the command line."""
    text = ("Download %s (%d request(s)) of the street maps of %s, to keep %s: %d tiles, "
            "zoom 0-%d." % (human_bytes(found["download_bytes"]), found["requests"],
                            found["build"], human_bytes(found["keep_bytes"]), found["tiles"],
                            found["max_zoom"]))
    if found["mode"] == "whole_degrees":
        text += (" Every tile of %d whole-degree square(s) is asked for, so the map's host "
                 "learns no more than whole degrees of where your pictures are; only the "
                 "tiles around the places are kept." % found["squares"])
    else:
        text += (" Only the tiles kept are asked for (whole_degrees is off): the map's host "
                 "can see which areas are downloaded.")
    if found["large"]:
        text += " " + large_words(found)
    return text


def save_plan(found):
    _write_json(plan_path(), public_plan(found))


# ---------------------------------------------------------------------------
# Downloading

def _progress_path():
    return os.path.join(maps_dir(), "download.json")


def _spool_path():
    return os.path.join(maps_dir(), "download.data")


def free_bytes(path):
    """The free disk space where path is (a folder that exists)."""
    return shutil.disk_usage(path).free


def _download_key(found, ranges, pieces):
    """What makes a download the same as one that stopped: its source, ranges and
    where each tile goes."""
    digest = hashlib.sha1(found["source"].encode("utf-8"))
    for item in ranges:
        digest.update(b"%d:%d;" % item)
    for offset, size in sorted(pieces.items()):
        digest.update(b"%d:%d:%d;" % (offset, size[0], size[1]))
    return digest.hexdigest()


def resume_state(found):
    """What a download of the plan found does with a download that stopped half way:
    ("none", None) when there is none, ("continues", progress) when it goes on where
    that one stopped, ("discarded", progress) when that one is of another plan (another
    build, other places or settings) and starts over."""
    progress = stopped_download()
    if not progress:
        return "none", None
    _layout_found, pieces, _ends = _layout(found["_ranges"], found["_keep"])
    if progress.get("key") == _download_key(found, found["_ranges"], pieces):
        return "continues", progress
    return "discarded", progress


def stopped_words(progress):
    """"the build B, 12 of 80 requests done" of a download that stopped."""
    return "the build %s, %s of %s requests done" % (
        progress.get("build") or "?", progress.get("ranges_done", "?"),
        progress.get("ranges", "?"))


def _remove_leftovers():
    for name in LEFTOVERS:
        try:
            remove_file(os.path.join(maps_dir(), name))
        except OSError:
            pass


def download(conn, config, ctx, expect_bytes=None, source=None, found=None,
             allow_large=False):
    """Plan (unless found is a plan), then download the street maps into
    data/maps/streets.pmtiles (replacing the ones there only when complete). Refuses
    a plan of LARGE_BYTES or more unless allow_large, one more than 10% (and 50 MB)
    larger than expect_bytes (the size the owner agreed to), and one there is not
    enough free disk space for. Continues an interrupted download of the same plan
    (another plan starts over, and says so). Returns {"tiles", "bytes", "downloaded",
    "build", "stopped"}. Raises MapsError (OSError when a file cannot be written)."""
    if found is None:
        found = plan(conn, config, ctx, source=source)
    ctx.log(plan_words(found))
    if found["large"] and not allow_large:
        raise MapsError(large_words(found))
    if expect_bytes is not None and found["download_bytes"] > \
            max(expect_bytes * 1.1, expect_bytes + GROWTH_SLACK):
        raise MapsError("The download is now %s, more than the %s agreed to; check the "
                        "size again." % (human_bytes(found["download_bytes"]),
                                         human_bytes(expect_bytes)))
    archive = found["_archive"]
    remote = found["_remote"]
    keep = found["_keep"]
    ranges = found["_ranges"]
    os.makedirs(maps_dir(), exist_ok=True)
    _remove_leftovers()
    layout, pieces, ends = _layout(ranges, keep)
    key = _download_key(found, ranges, pieces)
    progress = stopped_download() or {}
    spool_path = _spool_path()
    done = 0
    if progress.get("key") == key and \
            isinstance(progress.get("ranges_done"), int) and \
            0 < progress["ranges_done"] <= len(ranges) and \
            os.path.getsize(spool_path) >= ends[progress["ranges_done"] - 1]:
        done = progress["ranges_done"]
    elif progress:
        ctx.log("The download that stopped half way (%s) is not of this size check: it "
                "starts over." % stopped_words(progress), "warn")
    # Free space: the tiles still to come, then the map file (as large as all of them,
    # and its directories) while they are still there.
    total = ends[-1] if ends else 0
    need = (total - (ends[done - 1] if done else 0)) + total + 16 * len(keep) + 65536
    free = free_bytes(maps_dir())
    if free < need + SPACE_MARGIN:
        raise MapsError("There is not enough free disk space for the street maps: %s free "
                        "where the data folder is, %s needed (the tiles, then the map file "
                        "made of them, about %s each)." % (
                            human_bytes(free), human_bytes(need + SPACE_MARGIN),
                            human_bytes(total)))
    if done:
        with open(spool_path, "r+b") as handle:
            handle.truncate(ends[done - 1])
        ctx.log("Continuing the download where it stopped (%d of %d requests done)."
                % (done, len(ranges)))
    else:
        with open(spool_path, "wb"):
            pass
    ctx.stage("street maps download", total=len(ranges))
    if done:
        ctx.step(done)
    data_offset = archive.header["data_offset"]
    read = remote.read if remote is not None else archive.read
    received = 0
    record = {"key": key, "ranges": len(ranges), "source": found["source"],
              "build": found["build"], "version": found["version"]}
    try:
        with open(spool_path, "ab") as spool:
            for number in range(done, len(ranges)):
                if ctx.should_stop():
                    raise Stopped()
                start, length = ranges[number]
                received += _fetch_range(read, data_offset, start, length, layout[number],
                                         spool)
                spool.flush()
                _write_json(_progress_path(), dict(record, ranges_done=number + 1))
                ctx.step()
    except Stopped:
        ctx.log("The download stopped; start it again to continue.", "warn")
        return {"stopped": True, "tiles": 0, "bytes": 0, "downloaded": received,
                "build": found["build"]}
    ctx.stage("street maps file", total=0)
    shared.forget()
    size = _write_archive(found, keep, pieces, spool_path, total)
    manifest = {"build": found["build"], "version": found["version"],
                "source": found["source"], "downloaded": time.time(),
                "max_zoom": found["max_zoom"], "radius_km": found["radius_km"],
                "route_km": found["route_km"], "mode": found["mode"],
                "tiles": found["tiles"], "bytes": size,
                "download_bytes": found["download_bytes"]}
    _write_json(manifest_path(), manifest)
    for path in (spool_path, _progress_path()):
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(cache_dir(), ignore_errors=True)
    shared.forget()
    ctx.log("Street maps downloaded: %d tiles, %s (data/maps/%s)." % (
        found["tiles"], human_bytes(size), ARCHIVE_NAME))
    return {"stopped": False, "tiles": found["tiles"], "bytes": size,
            "downloaded": received, "build": found["build"]}


def _fetch_range(read, base, start, length, wanted, out):
    """Ask for the bytes start to start + length of the tile data (base: where it
    begins), in reads of at most MAX_RANGE, and write the tiles wanted ([(offset,
    size)] in order, each wholly in the range) to out as they come. The whole range is
    asked for (what the host sees is what was planned). Returns the bytes read."""
    end = start + length
    position = start
    buffer = b""
    buffer_start = start            # (the offset of buffer[0])
    received = 0

    def more(position):
        size = min(MAX_RANGE, end - position)
        data = read(base + position, size) if size > 0 else b""
        if not data:
            raise MapsError("A part of the map file was cut short; try again.")
        return data
    for offset, size in wanted:
        if offset < buffer_start or offset + size > end:
            raise MapsError("The download plan does not fit the map file; check the size "
                            "again.")
        while buffer_start + len(buffer) < offset + size:
            data = more(position)
            received += len(data)
            position += len(data)
            cut = min(len(buffer), offset - buffer_start)
            buffer = buffer[cut:] + data
            buffer_start += cut
        at = offset - buffer_start
        out.write(memoryview(buffer)[at:at + size])
    while position < end:
        data = more(position)
        received += len(data)
        position += len(data)
    return received


def _layout(ranges, keep):
    """Where each kept tile's bytes go in the download's spool file, worked out before
    anything is downloaded (so a download that stopped can continue): ([for each range,
    the (source offset, length) of the tiles it brings first, in the order written],
    {source offset: (spool offset, length)}, [the spool's size after each range])."""
    wanted = sorted({offset: length for offset, length in keep.values()}.items())
    layout, pieces, ends = [], {}, []
    position = 0
    for start, length in ranges:
        new = []
        first = bisect.bisect_left(wanted, (start, -1))
        for offset, size in wanted[first:]:
            if offset >= start + length:
                break
            if offset in pieces or offset + size > start + length:
                continue
            pieces[offset] = (position, size)
            position += size
            new.append((offset, size))
        layout.append(new)
        ends.append(position)
    missing = [offset for offset, _size in wanted if offset not in pieces]
    if missing:
        raise MapsError("The download plan misses %d tile(s); check the size again."
                        % len(missing))
    return layout, pieces, ends


def _write_archive(found, keep, pieces, spool_path, data_size):
    """Write streets.pmtiles with the downloaded tiles (the spool file is its data as it
    is: no copy of it is made first); returns its size."""
    entries = []
    seen = set()
    last_new = -1
    clustered = True
    south = west = 180.0
    north = east = -180.0
    min_zoom = max_zoom = None
    for tid in sorted(keep):
        spool_offset, size = pieces[keep[tid][0]]
        if spool_offset not in seen:
            seen.add(spool_offset)
            clustered = clustered and spool_offset > last_new
            last_new = spool_offset
        last = entries[-1] if entries else None
        if last is not None and last[0] + last[1] == tid and last[2] == spool_offset and \
                last[3] == size:
            last[1] += 1
        else:
            entries.append([tid, 1, spool_offset, size])
        z, x, y = tile_zxy(tid)
        min_zoom = z if min_zoom is None else min(min_zoom, z)
        max_zoom = z if max_zoom is None else max(max_zoom, z)
        if z == found["max_zoom"]:
            s, w, n, e = tile_bounds(z, x, y)
            south, west, north, east = min(south, s), min(west, w), max(north, n), \
                max(east, e)
    metadata = found["_archive"].metadata()
    metadata = {key: metadata[key] for key in ("vector_layers", "version", "type",
                                               "attribution", "name", "description")
                if key in metadata}
    metadata["attribution"] = ATTRIBUTION
    metadata["street_maps"] = {"build": found["build"], "max_zoom": found["max_zoom"]}
    if west > east:
        west, south, east, north = -180.0, -MAX_LAT, 180.0, MAX_LAT
    return write_pmtiles(archive_path(), [tuple(entry) for entry in entries], spool_path,
                         data_size, len(keep), len(seen), (min_zoom or 0, max_zoom or 0),
                         metadata, (west, south, east, north), clustered=clustered)


def remove():
    """Delete the street maps (and any download in progress, and what one cut short
    left); returns how many files. Raises OSError when a file stays open elsewhere.
    (Callers hold the job lock: see main and admin.py.)"""
    shared.forget()
    count = 0
    for path in (archive_path(), manifest_path(), plan_path(), _spool_path(),
                 _progress_path()):
        if remove_file(path):
            count += 1
    for name in LEFTOVERS:
        remove_file(os.path.join(maps_dir(), name))
    shutil.rmtree(cache_dir(), ignore_errors=True)
    shared.forget()
    return count


# ---------------------------------------------------------------------------
# Serving: the tiles as small scripts (the same files an export writes)

def tile_script(z, x, y, data):
    """The script of one tile: STREETS_DATA.tile(z, x, y, "<base64 of the gzip MVT>")."""
    return ("%s.tile(%d,%d,%d,\"%s\");\n" % (SCRIPT_GLOBAL, z, x, y,
                                             base64.b64encode(data).decode("ascii"))
            ).encode("ascii")


def index_script(levels, min_zoom, max_zoom):
    """The script of the index of the tiles there are (index_rows)."""
    value = {"format": INDEX_FORMAT, "minzoom": min_zoom, "maxzoom": max_zoom,
             "attribution": ATTRIBUTION, "rows": index_rows(levels)}
    text = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    return ("%s.index(%s);\n" % (SCRIPT_GLOBAL, text)).encode("ascii")


class SharedMaps:
    """data/maps/streets.pmtiles for the server's requests: opened on first use and
    again when the file changes; the index script made once per file."""

    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.archive = None
        self.index = None
        self.levels = None
        self.version = None

    def forget(self):
        with self.lock:
            self._close()

    def _close(self):
        if self.archive is not None:
            try:
                self.archive.read.close()
            except OSError:
                pass
        self.key = self.archive = self.index = self.levels = self.version = None

    def _current(self):
        """The open archive (None: no street maps); call with the lock held."""
        path = archive_path()
        try:
            stat = os.stat(path)
        except OSError:
            self._close()
            return None
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key != self.key:
            self._close()
            try:
                self.archive = open_local(path)
            except (OSError, MapsError):
                return None
            self.key = key
            self.version = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:10]
        return self.archive

    def info(self):
        """{"url", "v"} for the pages (app config "streets"), or None."""
        with self.lock:
            if self._current() is None:
                return None
            return {"url": "/maps/", "v": self.version}

    def levels_now(self):
        """{zoom: set of (x, y)} of the tiles there are (None without street maps)."""
        with self.lock:
            archive = self._current()
            if archive is None:
                return None
            if self.levels is None:
                levels = {}
                for tid, run, _offset, _length in archive.entries():
                    for value in range(tid, tid + run):
                        z, x, y = tile_zxy(value)
                        levels.setdefault(z, set()).add((x, y))
                self.levels = levels
            return self.levels

    def index_script(self):
        levels = self.levels_now()
        with self.lock:
            archive = self._current()
            if archive is None or levels is None:
                return None, None
            if self.index is None:
                self.index = index_script(levels, archive.header["min_zoom"],
                                          archive.header["max_zoom"])
            return self.index, self.version

    def tile(self, z, x, y):
        """(the tile's bytes or None, version)."""
        with self.lock:
            archive = self._current()
            version = self.version
        if archive is None:
            return None, None
        try:
            return archive.get(z, x, y), version
        except (OSError, ValueError, MapsError):
            return None, version


shared = SharedMaps()


def page_info():
    """The street maps for the server's pages ({"url", "v"}), or None."""
    return shared.info()


# ---------------------------------------------------------------------------
# Exports: only the tiles around what the export shows

def export_plan(points, route_points, km, max_zoom, home=None, levels=None):
    """{zoom: set of (x, y)} of the street map tiles an export carries: those km around
    the published positions and routes, of zoom DRAWN_FROM (the first the pages draw)
    to max_zoom, that the downloaded maps have, without any that touches home (an area
    with south, north, west and width, see trip_export.HomeArea; None: none): the
    pages never fill a gap from a tile coarser than DRAWN_FROM, so nothing of home is
    drawn but the world image. levels: the tiles there are (default: the downloaded
    ones'). {} without street maps."""
    have = shared.levels_now() if levels is None else levels
    if not have or (not points and not route_points):
        return {}
    have_max = max(have)
    max_zoom = min(max_zoom, have_max)
    if max_zoom < DRAWN_FROM:
        return {}
    boxes = coverage_boxes(points, densify_lines(route_points), km, min(km, 2.0))
    wanted = coverage_levels(boxes, max_zoom)
    result = {}
    for z, tiles in wanted.items():
        if z < DRAWN_FROM:
            continue
        kept = tiles & have.get(z, set())
        if home is not None:
            kept = {(x, y) for x, y in kept if not _touches_home(home, z, x, y)}
        if kept:
            result[z] = kept
    return result


def densify_lines(lines):
    """The points of lines ([[(lat, lon)]]) with a point at least every kilometre."""
    result = []
    for line in lines or []:
        result.extend(densify(list(line), 1.0))
    return result


def _touches_home(home, z, x, y):
    south, west, north, east = tile_bounds(z, x, y)
    if north <= home.south or south >= home.north:
        return False
    span = east - west
    start = (west - home.west + 180.0) % 360.0 - 180.0
    return start < home.width and start + span > 0


def export_size(levels):
    """(tiles, bytes of their scripts) of export_plan's tiles."""
    archive_tiles = 0
    total = 0
    with shared.lock:
        archive = shared._current()
    if archive is None:
        return 0, 0
    seen = {}
    for z, tiles in levels.items():
        for x, y in tiles:
            found = archive.find(tile_id(z, x, y))
            if found is None:
                continue
            archive_tiles += 1
            # (base64 is 4 bytes for 3, and the call around it about 40.)
            size = seen.setdefault(found[0], 4 * ((found[1] + 2) // 3) + 40)
            total += size
    return archive_tiles, total


def export_files(levels):
    """Yield (relative path, script bytes, signature) of an export's street map files:
    maps/index.js and maps/z/x/y.js of each tile export_plan chose."""
    with shared.lock:
        archive = shared._current()
        version = shared.version
    if archive is None or not levels:
        return
    present = {}
    for z, tiles in sorted(levels.items()):
        for x, y in sorted(tiles):
            found = archive.find(tile_id(z, x, y))
            if found is None:
                continue
            present.setdefault(z, set()).add((x, y))
            sig = "street:%s:%d:%d" % (version, found[0], found[1])
            yield ("maps/%d/%d/%d.js" % (z, x, y), lambda z=z, x=x, y=y: tile_script(
                z, x, y, archive.get(z, x, y) or b""), sig)
    if present:
        text = index_script(present, min(present), max(present))
        yield "maps/index.js", lambda: text, None
        yield "maps/LICENSE.txt", lambda: LICENSE_TEXT.encode("ascii"), None


LICENSE_TEXT = """Street maps

The street map tiles are made from OpenStreetMap data:
(c) OpenStreetMap contributors.

OpenStreetMap data is available under the Open Database License (ODbL)
1.0, https://opendatacommons.org/licenses/odbl/1-0/ ; see
https://www.openstreetmap.org/copyright . The tiles are of the Protomaps
basemap (https://protomaps.com).

The land cover of its least detailed tiles (zoom 0 to 7) is derived from
ESA WorldCover: (c) ESA WorldCover project 2020 / Contains modified
Copernicus Sentinel data (2020) processed by ESA WorldCover consortium,
under the Creative Commons Attribution 4.0 International license,
https://creativecommons.org/licenses/by/4.0/ .
"""


# ---------------------------------------------------------------------------
# What is there (the Manage page, the command line)

def status(conn=None, config=None):
    """{"available", "manifest" (what was downloaded, or None), "bytes", "tiles",
    "min_zoom", "max_zoom", "plan" (the last size check, or None), "settings",
    "attribution", "license", "command" (the command line that downloads them:
    download_command), "outside" (located pictures whose place has no street map at
    max zoom, when conn and config are given and there are maps), "downloading" (a
    download stopped half way, or running: {"build", "done", "total"} (requests);
    None)}."""
    stopped = stopped_download()
    result = {"available": False, "manifest": _read_json(manifest_path()),
              "plan": _read_json(plan_path()), "attribution": ATTRIBUTION,
              "license": LICENSE_NAME, "command": download_command(),
              "downloading": {"build": str(stopped.get("build") or ""),
                              "done": stopped.get("ranges_done"),
                              "total": stopped.get("ranges")} if stopped else None}
    if config is not None:
        result["settings"] = settings(config)
    try:
        result["bytes"] = os.path.getsize(archive_path())
    except OSError:
        return result
    levels = shared.levels_now()
    if levels is None:
        result["error"] = "The street map file data/maps/%s cannot be read." % ARCHIVE_NAME
        return result
    result["available"] = True
    result["tiles"] = sum(len(tiles) for tiles in levels.values())
    result["min_zoom"] = min(levels) if levels else 0
    result["max_zoom"] = max(levels) if levels else 0
    if conn is not None and config is not None and levels:
        spots, _routes = coverage_points(conn, config)
        top = max(levels)
        have = levels[top]
        result["outside"] = sum(1 for lat, lon in spots
                                if (lon_column(lon, top), lat_row(lat, top)) not in have)
    return result


# ---------------------------------------------------------------------------
# Command line

def _source_argument(text):
    text = text.strip()
    if not re.match(r"^https?://\S+$", text):
        raise argparse.ArgumentTypeError("give the http:// or https:// address of a PMTiles "
                                          "file")
    return text


def _bytes_argument(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError("give a number of bytes (a whole number above 0)")
    return value


def command_args(params):
    """The arguments of street_maps.py that do what a "maps" job with params does (its
    command line is python street_maps.py and these): a size check ({}), or a download
    ({"download": True, "expect_bytes", "source", "allow_large"}). main reads them back
    the same (job_params)."""
    if params.get("remove"):
        return ["--remove"]
    args = ["--download", "--yes"] if params.get("download") else ["--plan"]
    expect = params.get("expect_bytes")
    if params.get("download") and isinstance(expect, int) and not isinstance(expect, bool) \
            and expect > 0:
        args += ["--expect-bytes", "%d" % expect]
    if params.get("download") and params.get("allow_large"):
        args.append("--allow-large")
    if params.get("source"):
        args += ["--source", params["source"]]
    return args


def command_line(params):
    """The command line that does what a "maps" job with params does (command_args)."""
    return " ".join(["python", "street_maps.py"] + command_args(params))


def download_command():
    """The command line that downloads the street maps (it shows the size first and
    asks), as the pages and the status show it: python street_maps.py --download, with
    --settings-base DIR when the settings are not in the program folder (as a job's
    command line has it: jobs.settings_base_tokens)."""
    import jobs
    args = ["--download"] + jobs.settings_base_tokens()
    try:
        return jobs.command_text("street_maps.py", args)
    except jobs.NoCommand:
        return "python street_maps.py --download"


def job_params(args):
    """The params of the "maps" job that does what the command line args (parsed) do
    (command_args' inverse; None for one that is no job: the status, --remove)."""
    if args.remove or args.status or not (args.plan or args.download):
        return None
    params = {}
    if args.download:
        params["download"] = True
        if args.expect_bytes is not None:
            params["expect_bytes"] = args.expect_bytes
        if args.allow_large:
            params["allow_large"] = True
    if args.source:
        params["source"] = args.source
    return params


def parser():
    """The command line's argparse parser."""
    import jobs
    found = argparse.ArgumentParser(
        prog="street_maps.py",
        description="Street-level maps offline: download OpenStreetMap map tiles around "
                    "the places of the pictures and the drive routes, once.")
    found.add_argument("--status", action="store_true",
                       help="Show what is there (the default without --plan, --download or "
                            "--remove).")
    found.add_argument("--plan", action="store_true",
                       help="Check the size of a download (reads the map's directories, "
                            "a few MB, and no tiles).")
    found.add_argument("--download", action="store_true",
                       help="Download the street maps (shows the size first and asks).")
    found.add_argument("--yes", action="store_true",
                       help="With --download: do not ask.")
    found.add_argument("--expect-bytes", type=_bytes_argument, metavar="N",
                       help="With --download: the size agreed to (bytes to download, as a "
                            "size check said): a download that has grown well past it "
                            "(10%% and 50 MB) is refused.")
    found.add_argument("--allow-large", action="store_true",
                       help="With --download: download even %s or more (refused "
                            "otherwise)." % human_bytes(LARGE_BYTES))
    found.add_argument("--remove", action="store_true", help="Delete the street maps.")
    found.add_argument("--source", metavar="URL", type=_source_argument,
                       help="A PMTiles file to download from (default: the newest build "
                            "of the Protomaps basemap, or config.json street_maps.source).")
    jobs.add_settings_base_argument(found)
    return found


def main(argv=None):
    import db
    import jobs
    command = parser()
    args = command.parse_args(argv)
    if args.status and (args.plan or args.download or args.remove):
        command.error("--status shows what is there: give it alone")
    jobs.use_settings_base(command, args)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    ctx = jobs.ConsoleContext("street maps")
    jobs.log_tool_command(ctx, "street_maps.py", argv)
    missing = jobs.missing_settings()
    if missing:
        print(missing)
        return 2
    try:
        config = common.load_config()
    except common.ConfigError as error:
        print("Error: %s" % error)
        return 2
    if args.remove:
        lock = jobs.WorkerLock()
        if not lock.acquire("street_maps.py --remove"):
            print("Another job is running: %s. Try again when it has finished."
                  % (lock.holder() or "another process"))
            return 1
        try:
            removed = remove()
        except OSError as error:
            print("Error: the street maps could not be deleted: %s" % common.redact(error))
            return 1
        finally:
            lock.release()
        ctx.log("Removed the street maps (%d file(s))." % removed if removed
                else "There were no street maps.")
        return 0
    try:
        db.init(config, log=ctx.log)
        conn = db.connect()
    except (db.DatabaseError, common.ConfigError) as error:
        print("Error: %s" % error)
        return 2
    try:
        if not (args.plan or args.download):
            found = status(conn, config)
            _print_status(found)
            return 0
        lock = jobs.WorkerLock()
        if args.download and not lock.acquire("street_maps.py"):
            print("Another job is running: %s. Try again when it has finished."
                  % (lock.holder() or "another process"))
            return 1
        try:
            with ctx.stop_on_ctrl_c():
                found = plan(conn, config, ctx, source=args.source)
                save_plan(found)
                print(plan_words(found))
                if not args.download:
                    return 0
                if found["large"] and not args.allow_large:
                    print("Nothing was downloaded: it is %s or more (--allow-large downloads "
                          "it all the same)." % human_bytes(LARGE_BYTES))
                    return 1
                state, progress = resume_state(found)
                if state == "continues":
                    print("It goes on where the download that stopped half way stopped (%s)."
                          % stopped_words(progress))
                elif state == "discarded":
                    print("The download that stopped half way (%s) is not of this size "
                          "check: downloading starts over." % stopped_words(progress))
                if not args.yes:
                    try:
                        answer = input("Download %s now? [y/N] "
                                       % human_bytes(found["download_bytes"]))
                    except EOFError:
                        answer = ""
                    if answer.strip().lower() not in ("y", "yes"):
                        print("Nothing was downloaded.")
                        return 0
                result = download(conn, config, ctx, found=found,
                                  expect_bytes=args.expect_bytes,
                                  allow_large=args.allow_large)
        finally:
            if args.download:
                lock.release()
        return 1 if result["stopped"] else 0
    except Stopped:
        print("Stopped.")
        return 1
    except MapsError as error:
        print("Error: %s" % error)
        return 1
    except OSError as error:
        print("Error: a street maps file could not be written or read: %s"
              % common.redact(error))
        return 1
    finally:
        conn.close()


def _status_words(found):
    """The first line of the status: what is there (without the build and the date when
    data/maps/streets.json does not say them: a map file put there by hand)."""
    manifest = found.get("manifest") or {}
    if not found["available"]:
        return ("No street maps yet. %s downloads them (it shows the size first)."
                % found.get("command", "python street_maps.py --download"))
    words = "Street maps: %s, %d tiles, zoom %d-%d" % (
        human_bytes(found.get("bytes")), found.get("tiles", 0), found.get("min_zoom", 0),
        found.get("max_zoom", 0))
    downloaded = manifest.get("downloaded")
    when = (time.strftime("%Y-%m-%d", time.localtime(downloaded))
            if isinstance(downloaded, (int, float)) and not isinstance(downloaded, bool)
            and downloaded > 0 else "")
    build = manifest.get("build") or ""
    if build and when:
        words += ", of the build %s (downloaded %s)" % (build, when)
    elif build:
        words += ", of the build %s" % build
    elif when:
        words += " (downloaded %s)" % when
    return words + "."


def _print_status(found):
    print(_status_words(found))
    if found["available"] and found.get("outside"):
        print("%d place(s) of pictures have no street map: download again to add them."
              % found["outside"])
    stopped = found.get("downloading")
    if stopped:
        print("A download stopped half way (the build %s, %s of %s requests done); %s "
              "continues it while that build is there."
              % (stopped.get("build") or "?", stopped.get("done", "?"),
                 stopped.get("total", "?"), found.get("command",
                                                      "python street_maps.py --download")))
    if found.get("plan"):
        print("Last size check: " + plan_words(found["plan"]))


if __name__ == "__main__":
    sys.exit(main())
