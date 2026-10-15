"""Made-up street maps with named places, for the tests of named_places.py.

Vector tiles (MVT, gzip) of a made-up area, as the Protomaps basemap has
them: an "earth" layer (land everywhere), "pois" (named points of
interest: kind, name, min_zoom), "landuse" (outlined areas: kind; their
names are on the point of interest with the same id) and "buildings"
(building outlines, at the most detailed zoom only: a building's id is
that of the point of interest it is, if any; and address points),
written into a PMTiles archive (street_maps.ArchiveWriter) at
data/maps/streets.pmtiles. Every place and name here is made up.

    poi(fid, name, kind, lat, lon, min_zoom=15)
    area(fid, kind, ring)                      ring: [(lat, lon), ...]
    building(fid, ring, holes=(), kind="building")
    address(fid, lat, lon)
    write_streets(features, zooms=range(10, 16), buffer=None)

With buffer (tile units), a polygon reaching further past a tile's border
is cut there, as the real tiles are (buffer=64); without, each tile holds
the whole of every polygon that reaches into it.
"""

import gzip
import math
import os

import street_maps

EXTENT = 4096


def _varint(out, value):
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _key(out, number, wire):
    _varint(out, (number << 3) | wire)


def _bytes(out, number, data):
    _key(out, number, 2)
    _varint(out, len(data))
    out.extend(data)


def _zigzag(value):
    return (value << 1) ^ (value >> 63)


def _commands(parts, kind):
    """The geometry commands of parts ([[(x, y), ...]]): points (1), a line (2) or rings (3)."""
    out = []
    x = y = 0
    for part in parts:
        for index, (px, py) in enumerate(part):
            if index == 0:
                out.append(1 | (1 << 3))
            elif index == 1:
                out.append(2 | ((len(part) - 1) << 3))
            out.extend((_zigzag(px - x), _zigzag(py - y)))
            x, y = px, py
        if kind == 3:
            out.append(7 | (1 << 3))
    return out


def _value(value):
    out = bytearray()
    if isinstance(value, bool):
        _key(out, 7, 0)
        _varint(out, 1 if value else 0)
    elif isinstance(value, int):
        _key(out, 6, 0)
        _varint(out, _zigzag(value))
    else:
        _bytes(out, 1, str(value).encode("utf-8"))
    return bytes(out)


def layer(name, features):
    """A layer's bytes: features [(id or None, type, props, parts in tile units)]."""
    keys, values = [], []
    body = bytearray()
    _key(body, 15, 0)
    _varint(body, 2)
    _bytes(body, 1, name.encode("utf-8"))
    for fid, kind, props, parts in features:
        feature = bytearray()
        if fid is not None:
            _key(feature, 1, 0)
            _varint(feature, fid)
        tags = []
        for prop, value in props.items():
            if prop not in keys:
                keys.append(prop)
            encoded = _value(value)
            if encoded not in values:
                values.append(encoded)
            tags.extend((keys.index(prop), values.index(encoded)))
        packed = bytearray()
        for tag in tags:
            _varint(packed, tag)
        _bytes(feature, 2, packed)
        _key(feature, 3, 0)
        _varint(feature, kind)
        geometry = bytearray()
        for command in _commands(parts, kind):
            _varint(geometry, command)
        _bytes(feature, 4, geometry)
        _bytes(body, 2, feature)
    for prop in keys:
        _bytes(body, 3, prop.encode("utf-8"))
    for encoded in values:
        _bytes(body, 4, encoded)
    _key(body, 5, 0)
    _varint(body, EXTENT)
    return bytes(body)


def tile(layers):
    """A vector tile (gzip) of [(layer name, features)]."""
    out = bytearray()
    for name, features in layers:
        _bytes(out, 3, layer(name, features))
    return gzip.compress(bytes(out), mtime=0)


def poi(fid, name, kind, lat, lon, min_zoom=15, **props):
    """A named point of interest."""
    found = {"name": name, "kind": kind, "min_zoom": min_zoom}
    found.update(props)
    return {"layer": "pois", "id": fid, "props": found, "points": [(lat, lon)], "type": 1}


def area(fid, kind, ring):
    """An outlined area (its name: a poi with the same id)."""
    return {"layer": "landuse", "id": fid, "props": {"kind": kind}, "points": list(ring),
            "type": 3}


def building(fid, ring, holes=(), kind="building"):
    """A building's outline (kind "building" or "building_part"), with holes (rings inside
    it: a stadium's field), at the most detailed zoom."""
    return {"layer": "buildings", "id": fid, "props": {"kind": kind, "sort_rank": 400},
            "points": list(ring), "holes": [list(hole) for hole in holes], "type": 3}


def address(fid, lat, lon, number="1"):
    """An address point of the buildings layer (named_places reads no address)."""
    return {"layer": "buildings", "id": fid, "props": {"kind": "address",
                                                       "addr_housenumber": number},
            "points": [(lat, lon)], "type": 1}


def _clockwise(units, outer):
    """A ring in the winding MVT wants (outer rings one way, holes the other)."""
    area = sum(units[k][0] * units[(k + 1) % len(units)][1]
               - units[(k + 1) % len(units)][0] * units[k][1] for k in range(len(units)))
    return units if (area > 0) == outer else list(reversed(units))


def road(name, kind, line):
    """A road (a line), drawn by the pages' street maps."""
    return {"layer": "roads", "id": None, "props": {"kind": kind, "name": name},
            "points": list(line), "type": 2}


def _units(lat, lon, z, x, y):
    n = float(1 << z)
    lat = max(-street_maps.MAX_LAT, min(street_maps.MAX_LAT, lat))
    r = math.radians(lat)
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return int(round((fx - x) * EXTENT)), int(round((fy - y) * EXTENT))


def _clip(ring, low, high):
    """A ring cut to the square low..high (tile units; Sutherland and Hodgman), or [] when
    none of it is inside."""
    def across(x):
        return lambda a, b: (x, int(round(a[1] + (b[1] - a[1]) * (x - a[0])
                                          / float(b[0] - a[0]))))

    def along(y):
        return lambda a, b: (int(round(a[0] + (b[0] - a[0]) * (y - a[1])
                                       / float(b[1] - a[1]))), y)

    points = list(ring)
    for keep, cross in ((lambda p: p[0] >= low, across(low)),
                        (lambda p: p[0] <= high, across(high)),
                        (lambda p: p[1] >= low, along(low)),
                        (lambda p: p[1] <= high, along(high))):
        out = []
        for k, point in enumerate(points):
            last = points[k - 1]
            if keep(point):
                if not keep(last):
                    out.append(cross(last, point))
                out.append(point)
            elif keep(last):
                out.append(cross(last, point))
        points = out
    return points


def tile_of(features, z, x, y, top, buffer=None):
    """The tile (z, x, y) of features: each point of interest in the tile (from its
    min_zoom on; every one at the top zoom), each area and road that reaches into it,
    and land under all of it (buffer: polygons cut that far past its border)."""
    pois, landuse, roads, buildings = [], [], [], []
    margin = buffer or 0

    def cut(rings):
        if buffer is None:
            return rings
        return [ring for ring in (_clip(ring, -buffer, EXTENT + buffer) for ring in rings)
                if len(ring) >= 3]

    def away(units):
        xs = [px for px, _py in units]
        ys = [py for _px, py in units]
        return max(xs) < -margin or min(xs) >= EXTENT + margin or max(ys) < -margin or \
            min(ys) >= EXTENT + margin

    for feature in features:
        units = [_units(lat, lon, z, x, y) for lat, lon in feature["points"]]
        if feature["layer"] == "buildings":
            if z < top or away(units):
                continue
            if feature["type"] == 1:
                buildings.append((feature["id"], 1, feature["props"], [units]))
                continue
            rings = cut([_clockwise(units, True)] + [
                _clockwise([_units(lat, lon, z, x, y) for lat, lon in hole], False)
                for hole in feature.get("holes", ())])
            if rings:
                buildings.append((feature["id"], 3, feature["props"], rings))
            continue
        if feature["type"] == 1:
            px, py = units[0]
            if not (0 <= px < EXTENT and 0 <= py < EXTENT):
                continue
            if z < top and feature["props"].get("min_zoom", 0) > z:
                continue
            pois.append((feature["id"], 1, feature["props"], [units]))
            continue
        if away(units):
            continue
        if feature["layer"] == "landuse":
            rings = cut([units])
            if rings:
                landuse.append((feature["id"], feature["type"], feature["props"], rings))
            continue
        roads.append((feature["id"], feature["type"], feature["props"], [units]))
    earth = [(None, 3, {"kind": "earth"}, [[(0, 0), (EXTENT, 0), (EXTENT, EXTENT),
                                             (0, EXTENT)]])]
    layers = [("earth", earth), ("landuse", landuse), ("roads", roads)]
    if buildings:
        layers.append(("buildings", buildings))
    return tile(layers + [("pois", pois)])


def write_streets(features, zooms=range(10, 16), margin_km=1.0, path=None, buffer=None):
    """Write the street maps of features (made-up tiles of every zoom of zooms around
    them) to data/maps/streets.pmtiles (or path; buffer: polygons cut that far past each
    tile's border, see tile_of); returns {zoom: set of (x, y)}."""
    lats = [lat for feature in features for lat, _lon in feature["points"]]
    lons = [lon for feature in features for _lat, lon in feature["points"]]
    south, west = min(lats), min(lons)
    north, east = max(lats), max(lons)
    pad_lat = margin_km / street_maps.KM_PER_DEGREE
    pad_lon = pad_lat / max(0.05, math.cos(math.radians((south + north) / 2)))
    box = (south - pad_lat, west - pad_lon, north + pad_lat, east + pad_lon)
    zooms = sorted(zooms)
    levels = {}
    for z in zooms:
        found = set()
        street_maps.box_tiles(box, z, found)
        levels[z] = found
    path = path or street_maps.archive_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = street_maps.ArchiveWriter(path)
    top = zooms[-1]
    for tid in street_maps.tile_ids(levels):
        z, x, y = street_maps.tile_zxy(tid)
        writer.add(tid, tile_of(features, z, x, y, top, buffer))
    writer.finish({"vector_layers": [], "version": "4.9.0"},
                  (box[1], box[0], box[3], box[2]))
    street_maps.shared.forget()
    return levels


def offset(lat, lon, north_m=0.0, east_m=0.0):
    """The position north_m metres north and east_m metres east of lat, lon."""
    return (lat + north_m / 111320.0,
            lon + east_m / (111320.0 * math.cos(math.radians(lat))))


def square(lat, lon, half_m):
    """A square ring of half_m metres either side of lat, lon."""
    return [offset(lat, lon, -half_m, -half_m), offset(lat, lon, -half_m, half_m),
            offset(lat, lon, half_m, half_m), offset(lat, lon, half_m, -half_m)]
