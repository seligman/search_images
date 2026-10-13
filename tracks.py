#!/usr/bin/env python3
"""GPS tracks of videos: where a dashcam was, second by second, and drives' routes.

Many dashcams record the fixes of their GPS receiver into their videos,
once a second, as their overlay prints them over the picture. read_track()
reads them, streaming through the file (it never holds more than a few
megabytes of it):

  - Novatek-style "freeGPS" blocks (many dashcams with a Novatek chip): a
    block inside the media data each second, which holds the fix as an
    NMEA RMC sentence ("$GPRMC,...", "$GNRMC,...") or as binary fields
    (hour, minute, second, status, latitude and longitude as NMEA
    DDMM.MMMM numbers with their hemispheres, speed in knots, course,
    year, month and day; the numbers as 8-byte doubles or as 4-byte
    floats) in one of the layouts exiftool documents for them
    (BINARY_LAYOUTS; other cameras' blocks -- encrypted ones, other
    layouts -- are not read). A "gps " box in the movie header lists
    where the blocks are, and only those are read (the n-th entry is the
    n-th second, also when one is empty). Without it the first
    PROBE_BYTES of the file are searched for a block, and only when there
    is one is the rest of the file read (CHUNK_BYTES at a time; the n-th
    block found is the n-th second).
  - NMEA RMC sentences in the samples of a text (subtitle) track (one whose
    first TEXT_PROBE samples hold none is not read on).

A fix counts when its status is "A" (valid), its checksum (when it has
one) is right, its position is a real one (not 0, 0) and its date a real
one from YEARS. Of a video's fixes, one whose GPS time is more than
TIME_SLACK from where the others put it (its place in the video and
theirs) is left out, and so is one that jumps away and back (faster than
MAX_KMH both ways; the first and last against the two next to them) and a
second fix with the time of the one before it.
A fix is Fix(at, utc, lat, lon, speed, course): at the seconds into the
video (the block's place, one a second, or the text sample's time), utc
the GPS time (seconds since 1970), speed in km/h and course in degrees
(None when not given). A video with no such fix has no track.

The scanner (scan.py) reads the track of every video it reads, and
scan.read_tracks those of videos indexed before (a few small reads per
video). A video with a track and no position of its own gets the
position of its middle fix (Track.middle) -- lat, lon and place columns
like any picture with GPS -- and, when it has no UTC offset, the one its
clock has by the GPS time (clock_offset: a whole quarter hour from it,
give or take CLOCK_SLACK; a dashcam's clock knows no time zone). The GPS check
(gps_check.py) then times the video by its GPS time and counts its
position as a strong one, and the place estimates (place_estimates.py)
use points of its track as pictures taken at those times.

Tracks are kept in index.db (ensure_table; a table made on first use,
not a schema version: everything in it can be read again), by content
(gps_check.content_key), so a moved or copied video keeps its track:

    video_tracks  content_key, format (FORMAT: a track read by an older
                  version is read again), fixes (how many; 0: the video
                  has no track), kind ("freegps" or "text"), start (UTC
                  at the video's start: the fixes' times less their
                  place in the video), end (UTC of the last fix), at,
                  lat, lon, speed (the middle fix: seconds into the
                  video, position, km/h), extent_km (the furthest fix
                  from it), points (the track simplified to within
                  SIMPLIFY_METRES, see encode), updated

The points are the track simplified (simplify: a point is left out when
the line between the points kept, at its time, passes within
SIMPLIFY_METRES of it), so the position at any moment of the video is
known to that much from the points kept: a minute of driving takes about
a hundred bytes.

A drive (drives.py) has a route: the tracks of its clips one after the
other (routes(); a clip without a track, or one left out, is a gap).
The map draws the routes, and the viewer shows a drive's route with the
place it has played to. Routes and the positions of clips are location
data: visitors see them as they see positions (never of hidden clips);
an export publishes them only with its location and routes options, and
leaves out every point within its routes_trim metres (default 500) of
where a drive of any library starts or ends (privacy_zones), so that a
home is not shown. It never publishes the positions written in the texts
of a dashcam's clips (related_ids) -- but a clip's picture shows what its
overlay prints, which the export review warns about.

Settings: config.json "gps_tracks": {"enabled": true} (false: no tracks
are read, and those read are not used: the next scan takes the positions
they gave away).

Run "python tracks.py FILE ..." to see the track read from videos.

This module uses only the standard library.
"""

import argparse
import calendar
import collections
import json
import math
import os
import re
import struct
import sys
import time

import places
import world_map

# The reader's version: tracks read by an older one are read again.
FORMAT = 1
# The start of a file searched for a freeGPS block when it has no index.
PROBE_BYTES = 4 * 1024 * 1024
# How much of a file is read at a time when it is searched.
CHUNK_BYTES = 1024 * 1024
# The most of a block (or a text sample) read: the fix is at its start.
BLOCK_BYTES = 4096
# Blocks (or text samples) read from one video at most (a day at one a second).
MAX_BLOCKS = 86400
# A text track whose first this many samples hold no RMC sentence is not read on.
TEXT_PROBE = 10
# Fixes from these years count.
YEARS = (2000, 2100)
# A fix this fast away from both its neighbours is a glitch (km/h).
MAX_KMH = 400.0
# A fix whose GPS time is this far (s) from the others' less their places in the video
# is a glitch.
TIME_SLACK = 10.0
# The simplified track is this near the full one (metres).
SIMPLIFY_METRES = 10.0
# A clock this near a whole quarter hour from the GPS time (seconds) has that offset.
CLOCK_SLACK = 60
QUARTER = 900
OFFSET_RANGE = (-12 * 60, 14 * 60)      # minutes
KNOTS = 1.852
EARTH_M = 6371008.8
DEGREE_METRES = EARTH_M * math.pi / 180.0
# Export: points this near where a drive starts or ends are left out (metres).
DEFAULT_TRIM_METRES = 500
TRIM_RANGE = (0, 5000)
DEFAULTS = {"enabled": True}

Fix = collections.namedtuple("Fix", "at utc lat lon speed course")

_SENTENCE_RE = re.compile(rb"\$(?:G[A-Z])RMC,[\x20-\x7e]*?(?=[\x00\r\n$]|$)")
_CHECKSUM_RE = re.compile(r"\*([0-9A-Fa-f]{2})\s*$")
_GPS_MAGIC = b"freeGPS "
_TEXT_HANDLERS = (b"text", b"sbtl", b"subt")


# ---------------------------------------------------------------------------
# Settings

def settings(config):
    """config.json "gps_tracks" checked: {"enabled": bool} (default on)."""
    stored = config.get("gps_tracks") if isinstance(config, dict) else None
    result = dict(DEFAULTS)
    if isinstance(stored, dict) and isinstance(stored.get("enabled"), bool):
        result["enabled"] = stored["enabled"]
    elif isinstance(stored, bool):
        result["enabled"] = stored
    return result


# ---------------------------------------------------------------------------
# Fixes (pure)

def _utc(year, month, day, seconds):
    if not YEARS[0] <= year <= YEARS[1] or not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    try:
        return calendar.timegm((year, month, day, 0, 0, 0)) + seconds
    except (ValueError, OverflowError):
        return None


def _nmea_degrees(value, hemisphere, limit):
    """Degrees of an NMEA DDMM.MMMM number and its hemisphere ("N", "S", "E", "W"), or None.
    (The number's sign is the hemisphere's: some cameras write it negative too.)"""
    if value is None or not math.isfinite(value):
        return None
    value = abs(value)
    whole = int(value // 100)
    minutes = value - whole * 100
    if minutes >= 60:
        return None
    degrees = whole + minutes / 60.0
    if degrees > limit:
        return None
    return -degrees if hemisphere in ("S", "W") else degrees


def _float(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fix(utc, lat, lon, speed_knots, course):
    """(utc, lat, lon, km/h, course) of a fix's values, or None when it is not a real one."""
    if utc is None or lat is None or lon is None or not world_map.valid_position(lat, lon):
        return None
    if lat == 0 and lon == 0:
        return None
    speed = speed_knots * KNOTS if speed_knots is not None and 0 <= speed_knots < 10000 else None
    course = course if course is not None and 0 <= course <= 360 else None
    return utc, lat, lon, speed, course


def parse_rmc(sentence):
    """(utc, lat, lon, speed km/h or None, course or None) of an NMEA RMC sentence
    (text or bytes) with status A and a right checksum (when it has one), or None."""
    if isinstance(sentence, bytes):
        sentence = sentence.decode("ascii", "replace")
    if not isinstance(sentence, str):
        return None
    sentence = sentence.strip()
    if not sentence.startswith("$") or sentence[3:6] != "RMC":
        return None
    checked = _CHECKSUM_RE.search(sentence)
    if checked:
        body = sentence[1:checked.start()]
        total = 0
        for character in body:
            total ^= ord(character)
        if total != int(checked.group(1), 16):
            return None
        sentence = sentence[:checked.start()]
    fields = sentence.split(",")
    if len(fields) < 10 or fields[2] != "A":
        return None
    clock, date = fields[1], fields[9]
    if len(clock) < 6 or len(date) != 6 or not date.isdigit() or not clock[:6].isdigit():
        return None
    seconds = _float(clock[4:])
    hours, minutes = int(clock[:2]), int(clock[2:4])
    if seconds is None or hours > 23 or minutes > 59 or not 0 <= seconds < 61:
        return None
    day, month, year = int(date[:2]), int(date[2:4]), 2000 + int(date[4:6])
    utc = _utc(year, month, day, hours * 3600 + minutes * 60 + seconds)
    lat = _nmea_degrees(_float(fields[3]), fields[4], 90)
    lon = _nmea_degrees(_float(fields[5]), fields[6], 180)
    return _fix(utc, lat, lon, _float(fields[7]), _float(fields[8]))


# Binary freeGPS layouts, as exiftool's QuickTimeStream.pl (ProcessFreeGPS)
# documents them -- offsets from the block's start, its 4-byte size: (name,
# number format, offsets of the hour/minute/second (int32 each), the status
# ("A"), the latitude (an NMEA DDMM.MMMM number), its hemisphere ("N"/"S"), the
# longitude, its hemisphere ("E"/"W"), the speed (knots), the course, the
# year/month/day (int32 each), and True when the year must count from 2000: a
# camera that writes a full year there writes its local time, not UTC).
BINARY_LAYOUTS = (
    # Doubles after the clock, the date after them (exiftool's Vantrue N4 type;
    # such blocks hold an RMC sentence too).
    ("doubles", "<d", 0x10, 0x1c, 0x20, 0x28, 0x30, 0x38, 0x40, 0x48, 0x50, False),
    # The same from 0x30 (exiftool's "Type 2").
    ("doubles-30", "<d", 0x30, 0x3c, 0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70, False),
    # Floats after the clock and the date (the Novatek "Type 3": Viofo A119S and
    # others).
    ("floats", "<f", 0x30, 0x48, 0x4c, 0x49, 0x50, 0x4a, 0x54, 0x58, 0x3c, False),
    # The same from 0x10 (Viofo A119 V3).
    ("floats-10", "<f", 0x10, 0x28, 0x2c, 0x29, 0x30, 0x2a, 0x34, 0x38, 0x1c, True),
)

# Text where floats-10 has its latitude and longitude: another camera's (base64,
# encrypted, or decimal digits), not that layout.
_TEXT_FIELD_RES = (re.compile(rb"^[A-Za-z0-9+/]{8,20}={0,2}\x00*$"),
                   re.compile(rb"^\d{1,5}\.\d+\x00*$"))


def _binary(data, layout):
    name, number, clock, status, lat_at, lat_side, lon_at, lon_side, speed_at, course_at, \
        date_at, short_year = layout
    size = struct.calcsize(number)
    if (len(data) < max(clock + 12, date_at + 12, lat_at + size, lon_at + size,
                        speed_at + size, course_at + size)
            or data[status:status + 1] != b"A"):
        return None
    north, east = data[lat_side:lat_side + 1], data[lon_side:lon_side + 1]
    if north not in (b"N", b"S") or east not in (b"E", b"W"):
        return None
    if name == "floats" and data[16:19] == b"IQS":
        return None             # (integers there: exiftool's IQS variant)
    if name == "floats-10" and len(data) >= 0x78 and any(
            all(pattern.match(field) for field in (data[0x2c:0x40], data[0x40:0x54]))
            for pattern in _TEXT_FIELD_RES):
        return None
    hours, minutes, seconds = struct.unpack_from("<3i", data, clock)
    year, month, day = struct.unpack_from("<3i", data, date_at)
    if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 61):
        return None
    if 0 <= year < 100:
        year += 2000
    elif short_year:
        return None
    utc = _utc(year, month, day, hours * 3600 + minutes * 60 + seconds)
    lat = _nmea_degrees(struct.unpack_from(number, data, lat_at)[0], north.decode(), 90)
    lon = _nmea_degrees(struct.unpack_from(number, data, lon_at)[0], east.decode(), 180)
    speed = struct.unpack_from(number, data, speed_at)[0]
    course = struct.unpack_from(number, data, course_at)[0]
    return _fix(utc, lat, lon, speed if math.isfinite(speed) else None,
                course if math.isfinite(course) else None)


def parse_block(data):
    """The fix of a freeGPS block (bytes from its 4-byte size on), as parse_rmc gives it,
    or None: its RMC sentence when it has one, else its binary fields."""
    if len(data) < 12 or data[4:12] != _GPS_MAGIC:
        return None
    found = _SENTENCE_RE.search(data, 12)
    if found:
        return parse_rmc(found.group(0))
    for layout in BINARY_LAYOUTS:
        fix = _binary(data, layout)
        if fix is not None:
            return fix
    return None


def _metres(first, second):
    return places.distance_km(first.lat, first.lon, second.lat, second.lon) * 1000.0


def clean(fixes):
    """Fixes in time order without glitches -- a GPS time more than TIME_SLACK from the
    others' (by the median of the fixes' times less their places in the video), a
    fix more than MAX_KMH away from both its neighbours while they are near each
    other (the first and last fix: from the next two, while those are near each
    other) -- and without repeats (the same time, or place in the video, as the one
    before)."""
    ordered = sorted(fixes, key=lambda fix: (fix.at, fix.utc))
    if ordered:
        offsets = sorted(fix.utc - fix.at for fix in ordered)
        middle = offsets[len(offsets) // 2]
        ordered = [fix for fix in ordered if abs(fix.utc - fix.at - middle) <= TIME_SLACK]
    kept = []
    for fix in ordered:
        if kept and (fix.utc <= kept[-1].utc or fix.at <= kept[-1].at):
            continue
        kept.append(fix)

    def kmh(first, second):
        hours = max(1.0, abs(second.utc - first.utc)) / 3600.0
        return _metres(first, second) / 1000.0 / hours

    def glitch(fix, one, other):
        # (Far from both, while they are near each other.)
        return kmh(fix, one) > MAX_KMH and kmh(fix, other) > MAX_KMH and kmh(
            one, other) <= MAX_KMH

    result = []
    last = len(kept) - 1
    for index, fix in enumerate(kept):
        if 0 < index < last:
            neighbours = (kept[index - 1], kept[index + 1])
        elif index == 0 and last >= 2:
            neighbours = (kept[1], kept[2])
        elif index == last and last >= 2:
            neighbours = (kept[index - 1], kept[index - 2])
        else:
            neighbours = None
        if neighbours is not None and glitch(fix, *neighbours):
            continue
        result.append(fix)
    return result


class Track:
    """A video's GPS track: fixes (Fix, cleaned) and kind ("freegps" or "text")."""

    def __init__(self, fixes, kind):
        self.fixes = list(fixes)
        self.kind = kind

    @property
    def start(self):
        """UTC at the video's start: the median of the fixes' times less their places."""
        offsets = sorted(fix.utc - fix.at for fix in self.fixes)
        middle = len(offsets) // 2
        return (offsets[middle] if len(offsets) % 2
                else (offsets[middle - 1] + offsets[middle]) / 2.0)

    @property
    def end(self):
        return self.fixes[-1].utc

    def middle(self, duration=None):
        """The fix nearest the middle of the video (duration seconds long; without it, of
        the fixes' span): the video's position."""
        if isinstance(duration, (int, float)) and duration > 0:
            target = duration / 2.0
        else:
            target = (self.fixes[0].at + self.fixes[-1].at) / 2.0
        return min(self.fixes, key=lambda fix: (abs(fix.at - target), fix.at))

    def extent_km(self, fix):
        """How far (km) the furthest fix is from fix."""
        return max(places.distance_km(fix.lat, fix.lon, other.lat, other.lon)
                   for other in self.fixes)

    def __repr__(self):
        return "Track(%s, %d fixes)" % (self.kind, len(self.fixes))


def clock_offset(start_utc, taken_at, local_seconds):
    """(minutes, seconds off) of a video's clock: taken_at (its local start time) less its
    GPS start (UTC), as a whole quarter hour within OFFSET_RANGE, and how far the clock is
    from it -- or (None, seconds off from the nearest quarter) when that is more than
    CLOCK_SLACK, or (None, None) without a time. local_seconds: gps_check.local_seconds."""
    local = local_seconds(taken_at)
    if local is None or start_utc is None:
        return None, None
    difference = local - start_utc
    quarter = round(difference / QUARTER) * QUARTER
    off = difference - quarter
    minutes = int(quarter // 60)
    if abs(off) > CLOCK_SLACK or not OFFSET_RANGE[0] <= minutes <= OFFSET_RANGE[1]:
        return None, off
    return minutes, off


def iso_utc(seconds):
    """'2023-08-17T12:00:00Z' of UTC seconds since 1970 (None for None)."""
    if seconds is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(seconds)))


def offset_text(minutes):
    """'-08:00' of minutes east of UTC (images.tz_offset)."""
    sign = "-" if minutes < 0 else "+"
    minutes = abs(int(minutes))
    return "%s%02d:%02d" % (sign, minutes // 60, minutes % 60)


# ---------------------------------------------------------------------------
# Simplifying and encoding (pure)

def _xy(lat0, fix):
    """Metres east and north of (0, 0) near latitude lat0 (fine for a few km)."""
    scale = math.cos(math.radians(lat0))
    return (math.radians(fix.lon) * EARTH_M * scale, math.radians(fix.lat) * EARTH_M)


def simplify(fixes, tolerance=SIMPLIFY_METRES):
    """The fixes to keep so that the line between them, at each fix's time (its place in
    the video), passes within tolerance metres of it (Douglas-Peucker with the
    distance at the same time); the first and last are always kept."""
    if len(fixes) <= 2:
        return list(fixes)
    lat0 = fixes[0].lat
    points = [_xy(lat0, fix) for fix in fixes]
    keep = [False] * len(fixes)
    keep[0] = keep[-1] = True
    stack = [(0, len(fixes) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        span = fixes[last].at - fixes[first].at
        worst, worst_at = -1.0, None
        for index in range(first + 1, last):
            share = (fixes[index].at - fixes[first].at) / span if span > 0 else 0.0
            x = points[first][0] + (points[last][0] - points[first][0]) * share
            y = points[first][1] + (points[last][1] - points[first][1]) * share
            off = math.hypot(points[index][0] - x, points[index][1] - y)
            if off > worst:
                worst, worst_at = off, index
        if worst > tolerance:
            keep[worst_at] = True
            stack.append((first, worst_at))
            stack.append((worst_at, last))
    return [fix for fix, kept in zip(fixes, keep) if kept]


def encode(fixes):
    """A compact text of fixes (simplified): JSON [[at in tenths of a second, lat and lon
    in 1e-5 degrees, km/h or -1], ...], each number after the first point less the one
    before it (speed as it is)."""
    rows = []
    last = None
    for fix in fixes:
        values = [int(round(fix.at * 10)), int(round(fix.lat * 1e5)), int(round(fix.lon * 1e5))]
        speed = int(round(fix.speed)) if fix.speed is not None else -1
        if last is None:
            rows.append(values + [speed])
        else:
            rows.append([values[0] - last[0], values[1] - last[1], values[2] - last[2], speed])
        last = values
    return json.dumps(rows, separators=(",", ":"))


def decode(text):
    """[(at, lat, lon, speed or None)] of encode()'s text ([] when it is not one)."""
    try:
        rows = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    result = []
    total = None
    for row in rows if isinstance(rows, list) else ():
        if (not isinstance(row, list) or len(row) != 4
                or not all(isinstance(value, int) and not isinstance(value, bool)
                           for value in row)):
            return []
        values = row[:3] if total is None else [total[0] + row[0], total[1] + row[1],
                                                total[2] + row[2]]
        total = values
        result.append((values[0] / 10.0, values[1] / 1e5, values[2] / 1e5,
                       row[3] if row[3] >= 0 else None))
    return result


# ---------------------------------------------------------------------------
# Reading a video

def _boxes(handle, start, end):
    """(type, start, payload start, end) of the MP4 boxes between start and end."""
    at = start
    while at + 8 <= end:
        handle.seek(at)
        head = handle.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head[:8])
        payload = at + 8
        if size == 1:
            if len(head) < 16:
                return
            size = struct.unpack(">Q", head[8:16])[0]
            payload = at + 16
        elif size == 0:
            size = end - at
        if size < payload - at:
            return
        yield kind, at, payload, min(at + size, end)
        at += size


def _child(handle, start, end, kind):
    for found, _at, payload, stop in _boxes(handle, start, end):
        if found == kind:
            return payload, stop
    return None


def _read(handle, at, size):
    handle.seek(at)
    return handle.read(size)


def _index_blocks(handle, payload, stop, file_size):
    """[(place, offset, size)] of a "gps " box: version, count and (offset, size) pairs;
    place is the entry's (the second of the video), empty entries left out."""
    data = _read(handle, payload, min(stop - payload, 8 + 8 * MAX_BLOCKS))
    if len(data) < 8:
        return []
    count = struct.unpack(">I", data[4:8])[0]
    entries = []
    for index in range(min(count, (len(data) - 8) // 8, MAX_BLOCKS)):
        offset, size = struct.unpack_from(">II", data, 8 + 8 * index)
        if 0 < offset < file_size and 12 <= size:
            entries.append((index, offset, size))
    return entries


def _fixes_of_blocks(handle, entries):
    fixes = []
    for place, offset, size in entries:
        values = parse_block(_read(handle, offset, min(size, BLOCK_BYTES)))
        if values is not None:
            fixes.append(Fix(float(place), *values))
    return fixes


def _scan_blocks(handle, start, end):
    """The fixes of the freeGPS blocks between start and end, found by reading it all
    (CHUNK_BYTES at a time). The n-th block is at n seconds."""
    fixes = []
    place = 0
    at = start
    carry, carry_at = b"", start
    last = -1               # (where the last block found starts)
    while at < end:
        chunk = _read(handle, at, min(CHUNK_BYTES, end - at))
        if not chunk:
            break
        data = carry + chunk
        search = 0
        while True:
            found = data.find(_GPS_MAGIC, search)
            if found < 0:
                break
            search = found + 1
            block_at = carry_at + found - 4
            if block_at <= last or block_at < start:
                continue        # (seen in the chunk before)
            last = block_at
            block = data[found - 4:found - 4 + BLOCK_BYTES] if found >= 4 else b""
            if len(block) < min(BLOCK_BYTES, end - block_at):
                # (It goes on into the next chunk: read it whole.)
                block = _read(handle, block_at, min(BLOCK_BYTES, end - block_at))
            values = parse_block(block)
            if values is not None:
                fixes.append(Fix(float(place), *values))
            place += 1
            if place >= MAX_BLOCKS:
                return fixes
        at += len(chunk)
        # (A block whose magic is split between two chunks is found with this.)
        keep = len(_GPS_MAGIC) + 4
        carry = data[-keep:]
        carry_at = carry_at + len(data) - len(carry)
    return fixes


def _table(handle, stbl, kind):
    found = _child(handle, stbl[0], stbl[1], kind)
    return _read(handle, found[0], found[1] - found[0]) if found else None


def _text_fixes(handle, trak, file_size):
    """The fixes of the NMEA RMC sentences in a text track's samples (at: the sample's
    time), or None when trak is not a text track."""
    mdia = _child(handle, trak[0], trak[1], b"mdia")
    if mdia is None:
        return None
    hdlr = _child(handle, mdia[0], mdia[1], b"hdlr")
    mdhd = _child(handle, mdia[0], mdia[1], b"mdhd")
    if hdlr is None or mdhd is None:
        return None
    if _read(handle, hdlr[0] + 8, 4) not in _TEXT_HANDLERS:
        return None
    head = _read(handle, mdhd[0], 32)
    if len(head) < 24:
        return None
    timescale = struct.unpack_from(">I", head, 20 if head[0] == 1 else 12)[0]
    minf = _child(handle, mdia[0], mdia[1], b"minf")
    stbl = _child(handle, minf[0], minf[1], b"stbl") if minf else None
    if stbl is None or not timescale:
        return None
    stsz, stsc = _table(handle, stbl, b"stsz"), _table(handle, stbl, b"stsc")
    stts = _table(handle, stbl, b"stts")
    stco = _table(handle, stbl, b"stco")
    co64 = None if stco is not None else _table(handle, stbl, b"co64")
    if not stsz or not stsc or not stts or (stco is None and co64 is None):
        return None
    # Sample sizes.
    fixed, count = struct.unpack_from(">II", stsz, 4)
    count = min(count, MAX_BLOCKS)
    sizes = ([fixed] * count if fixed else
             [struct.unpack_from(">I", stsz, 12 + 4 * index)[0]
              for index in range(min(count, (len(stsz) - 12) // 4))])
    # Chunk offsets.
    table, width = (stco, 4) if stco is not None else (co64, 8)
    chunks = struct.unpack_from(">I", table, 4)[0]
    chunks = min(chunks, (len(table) - 8) // width)
    offsets = [struct.unpack_from(">I" if width == 4 else ">Q", table, 8 + width * index)[0]
               for index in range(chunks)]
    # Samples per chunk.
    runs = [struct.unpack_from(">III", stsc, 8 + 12 * index)
            for index in range(min(struct.unpack_from(">I", stsc, 4)[0], (len(stsc) - 8) // 12))]
    # Sample times.
    times = []
    clock = 0
    for index in range(min(struct.unpack_from(">I", stts, 4)[0], (len(stts) - 8) // 8)):
        repeat, delta = struct.unpack_from(">II", stts, 8 + 8 * index)
        for _ in range(min(repeat, count - len(times))):
            times.append(clock / float(timescale))
            clock += delta
    fixes = []
    sample = 0
    for chunk_index, offset in enumerate(offsets):
        per_chunk = 0
        for first, samples, _description in runs:
            if first - 1 <= chunk_index:
                per_chunk = samples
        at = offset
        for _ in range(per_chunk):
            if sample >= len(sizes):
                break
            size = sizes[sample]
            if 0 < at < file_size and size:
                data = _read(handle, at, min(size, BLOCK_BYTES))
                found = _SENTENCE_RE.search(data)
                values = parse_rmc(found.group(0)) if found else None
                if values is not None and sample < len(times):
                    fixes.append(Fix(times[sample], *values))
            at += size
            sample += 1
            if sample == TEXT_PROBE and not fixes:
                return fixes    # (subtitles of another kind: not read through)
    return fixes


def read_track(path):
    """The GPS track (Track) of a video file, or None when it has none (see the module
    notes). Raises OSError when the file cannot be read."""
    with open(path, "rb") as handle:
        try:
            return _read_track(handle)
        except (struct.error, ValueError, OverflowError, IndexError):
            return None         # (laid out in no way read here)


def _read_track(handle):
    file_size = os.fstat(handle.fileno()).st_size
    top = list(_boxes(handle, 0, file_size))
    if not top or top[0][0] not in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide"):
        return None
    moov = next(((payload, stop) for kind, _at, payload, stop in top if kind == b"moov"),
                None)
    index = None
    texts = []
    if moov is not None:
        for kind, _at, payload, stop in _boxes(handle, moov[0], moov[1]):
            if kind == b"gps ":
                index = _index_blocks(handle, payload, stop, file_size)
            elif kind == b"udta" and index is None:
                inner = _child(handle, payload, stop, b"gps ")
                if inner is not None:
                    index = _index_blocks(handle, inner[0], inner[1], file_size)
            elif kind == b"trak":
                texts.append((payload, stop))
    if index:
        fixes = clean(_fixes_of_blocks(handle, index))
        return Track(fixes, "freegps") if fixes else None
    for trak in texts:
        fixes = _text_fixes(handle, trak, file_size)
        if fixes:
            fixes = clean(fixes)
            if fixes:
                return Track(fixes, "text")
    # No index: a block near the start, then all of them.
    mdat = next(((at, stop) for kind, at, _payload, stop in top if kind == b"mdat"),
                (0, file_size))
    probe = _read(handle, mdat[0], min(PROBE_BYTES, mdat[1] - mdat[0]))
    if _GPS_MAGIC not in probe:
        return None
    fixes = clean(_scan_blocks(handle, mdat[0], mdat[1]))
    return Track(fixes, "freegps") if fixes else None


# ---------------------------------------------------------------------------
# Kept tracks (index.db)

_TABLE = """CREATE TABLE IF NOT EXISTS video_tracks (
    content_key TEXT PRIMARY KEY,
    format INTEGER NOT NULL,
    fixes INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '',
    start REAL,
    end REAL,
    at REAL,
    lat REAL,
    lon REAL,
    speed REAL,
    extent_km REAL,
    points TEXT NOT NULL DEFAULT '',
    updated REAL
)"""
TABLE = "video_tracks"

Facts = collections.namedtuple(
    "Facts", "key fixes kind start end at lat lon speed extent_km points")
Facts.__doc__ = """A kept track (a video_tracks row, see the module notes): points is
decode()'s list [(at, lat, lon, speed or None)]."""


def have_table(conn):
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                        "AND name = ?", (TABLE,)).fetchone()[0] == 1


def ensure_table(conn):
    """Create video_tracks when it is not there yet (commits what conn had pending first
    then; writes nothing when it is)."""
    if have_table(conn):
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute(_TABLE)
    conn.commit()


def content_key(row):
    """What a video's track is kept under (gps_check.content_key of its row)."""
    import gps_check
    return gps_check.content_key(row["content_hash"], row["id"])


def facts_of(track, duration=None):
    """The columns of a video's track (Track, or None: no track) as a dict: fixes, kind,
    start, end, at, lat, lon, speed, extent_km, points."""
    if track is None or not track.fixes:
        return {"fixes": 0, "kind": "", "start": None, "end": None, "at": None, "lat": None,
                "lon": None, "speed": None, "extent_km": None, "points": ""}
    middle = track.middle(duration)
    return {"fixes": len(track.fixes), "kind": track.kind, "start": round(track.start, 3),
            "end": round(track.end, 3), "at": middle.at, "lat": middle.lat, "lon": middle.lon,
            "speed": None if middle.speed is None else round(middle.speed, 1),
            "extent_km": round(track.extent_km(middle), 3),
            "points": encode(simplify(track.fixes))}


def store(conn, key, track, duration=None, now=None):
    """Keep what was read of a video (its Track, or None for a video without one) under
    its content key. Returns its Facts."""
    ensure_table(conn)
    values = facts_of(track, duration)
    conn.execute(
        "INSERT OR REPLACE INTO video_tracks (content_key, format, fixes, kind, start, end, at, "
        "lat, lon, speed, extent_km, points, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, FORMAT, values["fixes"], values["kind"], values["start"], values["end"],
         values["at"], values["lat"], values["lon"], values["speed"], values["extent_km"],
         values["points"], time.time() if now is None else now))
    return _facts(dict(values, content_key=key))


def _facts(row):
    points = decode(row["points"]) if row["fixes"] else []
    return Facts(row["content_key"], row["fixes"], row["kind"], row["start"], row["end"],
                 row["at"], row["lat"], row["lon"], row["speed"], row["extent_km"], points)


def stored(conn, keys=None):
    """{content key: Facts} of the kept tracks with fixes (of keys; all of them by default;
    {} before there are any). Tracks of an older FORMAT are left out."""
    if not have_table(conn):
        return {}
    sql = "SELECT * FROM video_tracks WHERE fixes > 0 AND format = ?"
    found = {}
    if keys is None:
        for row in conn.execute(sql, (FORMAT,)):
            found[row["content_key"]] = _facts(row)
        return found
    keys = list(dict.fromkeys(keys))
    for start in range(0, len(keys), 500):
        part = keys[start:start + 500]
        for row in conn.execute(sql + " AND content_key IN (%s)" % ",".join("?" * len(part)),
                                [FORMAT] + part):
            found[row["content_key"]] = _facts(row)
    return found


def read_keys(conn):
    """The content keys of the videos whose track was read by this FORMAT (with or without
    one)."""
    if not have_table(conn):
        return set()
    return {row[0] for row in conn.execute(
        "SELECT content_key FROM video_tracks WHERE format = ?", (FORMAT,))}


def for_rows(conn, rows, config=None):
    """{image id: Facts} of the rows (dicts or sqlite rows with id, content_hash, media)
    whose video has a kept track ({} when gps_tracks is off in config)."""
    if config is not None and not settings(config)["enabled"]:
        return {}
    keys = {}
    for row in rows:
        if row["media"] == "video":
            keys[row["id"]] = content_key(row)
    if not keys:
        return {}
    found = stored(conn, keys.values())
    return {image_id: found[key] for image_id, key in keys.items() if key in found}


def is_position(facts, lat, lon):
    """True when lat, lon is the position a kept track gave its video (its middle fix)."""
    return bool(facts is not None and facts.fixes and lat is not None and lon is not None
                and abs(lat - facts.lat) <= 1e-9 and abs(lon - facts.lon) <= 1e-9)


def gps_facts(facts):
    """EXIF-style GPS facts of a kept track, as gps_check reads a picture's (a fix at the
    video's start, in UTC, its middle fix's speed, and how far its furthest fix is from
    the middle one as the accuracy): {GPSStatus, GPSDateStamp, GPSTimeStamp, GPSSpeed,
    GPSSpeedRef, GPSHPositioningError}."""
    moment = time.gmtime(int(facts.start // 1))
    result = {"GPSStatus": "A", "GPSDateStamp": "%04d:%02d:%02d" % moment[:3],
              "GPSTimeStamp": [moment.tm_hour, moment.tm_min,
                               round(moment.tm_sec + facts.start % 1, 3)],
              "GPSHPositioningError": round((facts.extent_km or 0.0) * 1000.0, 1)}
    if facts.speed is not None:
        result.update(GPSSpeed=facts.speed, GPSSpeedRef="K")
    return result


def position_at(points, at):
    """(lat, lon, speed or None) at a moment of a video (seconds in) by its kept points
    (decode()'s): the line between the points around it, the first or last point
    outside them; None without points."""
    if not points:
        return None
    if at <= points[0][0]:
        return points[0][1], points[0][2], points[0][3]
    for before, after in zip(points, points[1:]):
        if before[0] <= at <= after[0]:
            span = after[0] - before[0]
            share = (at - before[0]) / span if span > 0 else 0.0
            speed = (before[3] if after[3] is None else after[3] if before[3] is None
                     else before[3] + (after[3] - before[3]) * share)
            return (before[1] + (after[1] - before[1]) * share,
                    before[2] + (after[2] - before[2]) * share, speed)
    return points[-1][1], points[-1][2], points[-1][3]


def samples(facts, step):
    """[(utc, lat, lon)] along a kept track every step seconds from its first point, and
    its last point."""
    points = facts.points
    if not points:
        return []
    found = []
    at = points[0][0]
    while at < points[-1][0]:
        where = position_at(points, at)
        found.append((facts.start + at, where[0], where[1]))
        at += step
    found.append((facts.start + points[-1][0], points[-1][1], points[-1][2]))
    return found


def prune(conn):
    """Forget the tracks of contents that no video (present or missing) has any more.
    Returns how many."""
    if not have_table(conn):
        return 0
    cursor = conn.execute(
        "DELETE FROM video_tracks WHERE content_key NOT IN (SELECT content_hash FROM images "
        "WHERE media = 'video' AND content_hash <> '') AND content_key NOT IN "
        "(SELECT '#' || id FROM images WHERE media = 'video')")
    return cursor.rowcount


def apply_to_result(result, track):
    """A video just read (videos.read_video's dict) and its track (or None): result gets
    "track", and from the track the position of its middle fix when it has none of its
    own and the UTC offset of its clock when it has none (clock_offset). In place."""
    import gps_check
    result["track"] = track
    if track is None or not track.fixes:
        return
    if result.get("lat") is None or result.get("lon") is None:
        middle = track.middle(result.get("duration"))
        result["lat"], result["lon"] = middle.lat, middle.lon
    if not result.get("tz_offset"):
        minutes, _off = clock_offset(track.start, result.get("taken_at"),
                                     gps_check.local_seconds)
        if minutes is not None:
            result["tz_offset"] = offset_text(minutes)


def row_values(row, facts, index=None):
    """The images columns to change for a video row whose track (Facts) was just read: its
    position (lat, lon and place columns) when it has none, and tz_offset when it has
    none; {} when nothing changes."""
    import gps_check
    values = {}
    if facts is None or not facts.fixes:
        return values
    if row["lat"] is None or row["lon"] is None:
        values["lat"], values["lon"] = facts.lat, facts.lon
        values.update(places.place_columns(facts.lat, facts.lon, index))
    if not row["tz_offset"]:
        minutes, _off = clock_offset(facts.start, row["taken_at"], gps_check.local_seconds)
        if minutes is not None:
            values["tz_offset"] = offset_text(minutes)
    return values


_CANDIDATES_SQL = """
    SELECT id, path, name, content_hash, media, lat, lon, tz_offset, taken_at, duration,
        projection
    FROM images WHERE library_id = ? AND media = 'video' AND missing_since IS NULL
    ORDER BY path"""


def candidates(conn, library_id):
    """The rows of a library's present videos whose track was not read (by this FORMAT):
    not 360 videos, nor any .insv file."""
    import imageinfo
    import videos
    read = read_keys(conn)
    return [row for row in conn.execute(_CANDIDATES_SQL, (library_id,)).fetchall()
            if content_key(row) not in read
            and row["projection"] not in imageinfo.PROJECTIONS_360
            and not videos.is_360_video(row["name"])]


def unplaced_rows(conn, library_id):
    """The rows (id, content_hash, media, lat, lon, tz_offset, taken_at) of a library's
    present videos with a kept track that have no position, and none the GPS check set
    aside: their track's position is theirs again (row_values)."""
    rows = conn.execute(
        "SELECT i.id, i.content_hash, i.media, i.lat, i.lon, i.tz_offset, i.taken_at "
        "FROM images AS i LEFT JOIN gps_checks AS g ON g.image_id = i.id "
        "WHERE i.library_id = ? AND i.media = 'video' AND i.missing_since IS NULL "
        "AND i.lat IS NULL AND g.image_id IS NULL", (library_id,)).fetchall()
    found = for_rows(conn, rows)
    return [(row, found[row["id"]]) for row in rows if row["id"] in found]


def position_rows(conn, library_id):
    """The rows (id, content_hash, media, lat, lon) of a library's videos whose position is
    the one their kept track gave them (its middle fix)."""
    rows = conn.execute(
        "SELECT id, content_hash, media, lat, lon FROM images WHERE library_id = ? "
        "AND media = 'video' AND lat IS NOT NULL", (library_id,)).fetchall()
    found = for_rows(conn, rows)
    return [row for row in rows if is_position(found.get(row["id"]), row["lat"], row["lon"])]


def clock_words(offsets):
    """"UTC+02:00 (12 videos)" of a Counter {minutes or None: videos} (None: a clock that
    is not a whole quarter hour from the GPS time)."""
    parts = []
    for minutes, count in sorted(offsets.items(), key=lambda item: (-item[1], str(item[0]))):
        name = ("UTC" + offset_text(minutes)) if minutes is not None else "off the GPS time"
        parts.append("{} ({:,} video{})".format(name, count, "" if count == 1 else "s"))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Routes

def encode_points(points):
    """encode() of decode()'s points."""
    return encode([Fix(at, 0.0, lat, lon, speed, None) for at, lat, lon, speed in points])


def routes(conn, config, library_ids, ids=None):
    """The routes of the drives (drives.py) of some libraries, and of the videos with a
    track that are in no drive, as the map and the viewer take them:

        [{"d": the id of its first clip listed, "l": library id,
          "c": [{"i": clip id, "o": seconds from the first clip's start to its start
                 (by their GPS times), "p": encode() text of its track}, ...]}]

    (trim_route may cut a clip in pieces: entries with the same "i" and "t": 1.)

    in the order of their first clips' times. Only present clips that are not hidden
    are listed (ids, a set: only those); a clip without a track is left out (the
    route has a gap there). [] when gps_tracks is off."""
    import drives
    if not settings(config)["enabled"] or not have_table(conn):
        return []
    result = []
    for library_id in library_ids:
        rows = conn.execute(
            "SELECT id, content_hash, media, hidden, taken_at FROM images WHERE library_id = ? "
            "AND media = 'video' AND missing_since IS NULL", (library_id,)).fetchall()
        found = for_rows(conn, rows)
        if not found:
            continue
        shown = {row["id"] for row in rows if not row["hidden"]
                 and (ids is None or row["id"] in ids)}
        taken = {row["id"]: row["taken_at"] or "" for row in rows}
        in_drives = set()
        for drive in drives.library_drives(conn, library_id, config):
            in_drives.update(drive.ids)
            listed = [clip.id for clip in drive.clips if clip.id in shown and clip.id in found]
            if listed:
                base = found[listed[0]].start
                result.append((taken.get(listed[0], ""), {
                    "d": listed[0], "l": library_id,
                    "c": [{"i": image_id, "o": round(found[image_id].start - base, 1),
                           "p": encode_points(found[image_id].points)}
                          for image_id in listed]}))
        for image_id, facts in found.items():
            if image_id not in in_drives and image_id in shown:
                result.append((taken.get(image_id, ""), {
                    "d": image_id, "l": library_id,
                    "c": [{"i": image_id, "o": 0.0, "p": encode_points(facts.points)}]}))
    result.sort(key=lambda item: (item[0], item[1]["d"]))
    return [route for _taken, route in result]


def privacy_zones(conn, config, library_ids=None):
    """[(lat, lon)] where the drives (and the videos with a track in no drive) of some
    libraries (default: every library of config) start and end: the first and last
    points of their tracks, every clip counted (hidden ones too)."""
    import drives
    zones = []
    if not have_table(conn):
        return zones
    if library_ids is None:
        library_ids = [library.get("id") for library in (config or {}).get("libraries", [])
                       if isinstance(library, dict) and library.get("id")]
    for library_id in library_ids:
        rows = conn.execute(
            "SELECT id, content_hash, media FROM images WHERE library_id = ? "
            "AND media = 'video' AND missing_since IS NULL", (library_id,)).fetchall()
        found = for_rows(conn, rows)
        if not found:
            continue
        in_drives = set()
        runs = []
        for drive in drives.library_drives(conn, library_id, config):
            in_drives.update(drive.ids)
            runs.append([found[clip.id] for clip in drive.clips if clip.id in found])
        runs.extend([facts] for image_id, facts in sorted(found.items())
                    if image_id not in in_drives)
        for run in runs:
            points = [point for facts in run for point in facts.points]
            if points:
                zones.append((points[0][1], points[0][2]))
                zones.append((points[-1][1], points[-1][2]))
    return zones


def related_ids(conn, config, library_id):
    """The ids of a library's present videos that belong with GPS tracks: those with a
    kept track, every clip of a drive one of whose clips has one, and the videos of a
    folder with one (of its camera, or without a camera name) -- whether gps_tracks is
    on or not. A dashcam prints its position
    over its video -- a clip without a fix (a cold start, a garage) the last one it
    had, often where the drive started -- and the model reads it into the clip's
    text: an export never publishes the positions written in their texts (the route
    shows where they were, with the routes option)."""
    import drives
    if not have_table(conn):
        return set()
    rows = conn.execute("SELECT id, path, content_hash, media, camera FROM images "
                        "WHERE library_id = ? AND media = 'video' AND missing_since IS NULL",
                        (library_id,)).fetchall()
    found = for_rows(conn, rows)
    if not found:
        return set()
    related = set(found)

    def folder(row):
        return (row["path"] or "").rpartition("/")[0]

    # (A folder's videos of the camera of one with a track, or without a camera
    # name: not a phone's video that ended up beside them.)
    cameras = {(folder(row), row["camera"] or "") for row in rows if row["id"] in found}
    related.update(row["id"] for row in rows if (folder(row), row["camera"] or "") in cameras
                   or (not row["camera"] and any(place == folder(row)
                                                 for place, _camera in cameras)))
    for drive in drives.library_drives(conn, library_id, config):
        if any(clip.id in found for clip in drive.clips):
            related.update(drive.ids)
    return related


def outside(lat, lon, zones, metres):
    """True when a position is further than metres from every zone (privacy_zones)."""
    if not metres:
        return True
    km = metres / 1000.0
    return all(places.distance_km(lat, lon, zone[0], zone[1]) > km for zone in zones)


def _passes_near(first, second, zones, metres):
    """True when the line between two points (decode()'s) passes within metres of a
    zone (a flat earth: fine for the few km between two points of a track)."""
    for lat, lon in zones:
        scale = math.cos(math.radians(lat))
        ax, ay = (first[2] - lon) * scale, first[1] - lat
        dx, dy = (second[2] - lon) * scale - ax, second[1] - lat - ay
        length = dx * dx + dy * dy
        share = 0.0 if length <= 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
        if math.hypot(ax + dx * share, ay + dy * share) * DEGREE_METRES <= metres:
            return True
    return False


def trim_route(route, zones, metres):
    """A route (as routes() gives it) without its points within metres of a zone, or None
    when nothing is left of it. A new dict. A clip that loses points, or whose line
    between two points passes that near a zone, is cut there (one entry, with the same
    "i", for each run of points left, and "t": 1), so that no line is drawn across what
    was left out, nor a place shown there."""
    clips = []
    for clip in route["c"]:
        points = decode(clip["p"])
        runs, run = [], []
        for point in points:
            if not outside(point[1], point[2], zones, metres):
                if run:
                    runs.append(run)
                    run = []
                continue
            if run and metres and _passes_near(run[-1], point, zones, metres):
                runs.append(run)
                run = []
            run.append(point)
        if run:
            runs.append(run)
        cut = len(runs) != 1 or len(runs[0]) != len(points)
        for run in runs:
            entry = dict(clip, p=encode_points(run))
            if cut:
                entry["t"] = 1
            clips.append(entry)
    if not clips:
        return None
    return dict(route, d=clips[0]["i"], c=clips)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show the GPS tracks read from videos "
                                                 "(nothing is changed).")
    parser.add_argument("files", nargs="+", help="video files")
    parser.add_argument("--fixes", action="store_true", help="list every fix")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    for path in args.files:
        started = time.perf_counter()
        try:
            track = read_track(path)
        except OSError as error:
            print("%s: cannot be read (%s)" % (path, error))
            continue
        seconds = time.perf_counter() - started
        if track is None:
            print("%s: no GPS track (%.2f s)" % (path, seconds))
            continue
        middle = track.middle()
        kept = simplify(track.fixes)
        print("%s: %d fixes (%s), %s to %s UTC, middle %.5f, %.5f (%.1f s in), "
              "%d points kept (%d bytes), %.2f s" % (
                  path, len(track.fixes), track.kind,
                  time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(track.start)),
                  time.strftime("%H:%M:%S", time.gmtime(track.end)), middle.lat, middle.lon,
                  middle.at, len(kept), len(encode(kept)), seconds))
        if args.fixes:
            for fix in track.fixes:
                print("  %6.1f s  %s  %.6f, %.6f  %s km/h  %s" % (
                    fix.at, time.strftime("%H:%M:%S", time.gmtime(fix.utc)), fix.lat, fix.lon,
                    "-" if fix.speed is None else "%.1f" % fix.speed,
                    "-" if fix.course is None else "%.0f" % fix.course))
    return 0


if __name__ == "__main__":
    sys.exit(main())
