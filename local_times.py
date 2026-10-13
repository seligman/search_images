#!/usr/bin/env python3
"""Times in the local time of the place: when a picture was taken, by the clocks there.

A camera dates a picture by its own clock, and that clock is not always
the local time of where the picture was taken: many dashcams ignore
daylight saving (their videos are an hour behind all summer), a phone in
an aeroplane keeps the time of the airport it left until it lands, and a
camera taken abroad keeps the time of home. When the UTC moment of a
picture is known for sure and the time zone of its place says the clocks
there showed another time, the picture gets that time: images.taken_at
becomes the local time of its place and images.tz_offset that place's
offset at that moment (daylight saving included), as a phone writes them.
The timeline, "on this day", date searches and everything else that reads
taken_at then use it. The camera's own clock is kept beside it (the
local_times table) and shown in the picture's details.

How the UTC moment is known (decide), from the picture's own facts:

  track     a video with a GPS track (tracks.py: a dashcam's): its GPS
            time at the start (UTC) against its clock (tracks.clock_offset:
            a whole quarter hour apart, give or take a minute; a clock set
            to UTC too).
  gps       a picture whose GPS time (EXIF GPSTimeStamp and GPSDateStamp)
            is a whole number of quarter hours from its clock, give or take
            CLOCK_SLACK: the fix is from the moment it was taken, and the
            clock that far from UTC.
  gps_date  a picture with a UTC offset whose GPS date (no GPS time) is
            not the UTC day its clock and offset give, while its clock read
            as the local time of its place gives that day: a phone that
            showed the local time but wrote the offset of the time zone it
            came from (only the offset is wrong; the time stays).

A GPS time that is the clock's own time (offset 0) is no evidence: some
cameras write their local time as the GPS time. A picture without such
facts (no position, a GPS fix that is not from the moment it was taken,
a video without a track) goes by its camera's clock when a picture of the
same camera (gps_check.camera_key; a video without a camera name: its
folder) with such facts was taken within NEIGHBOUR_SECONDS of it by that
clock and gave the same UTC offset in its file (or is a copy: taken in
the same second): "like" -- the nearest such picture, unless its clock's
offset cannot be this picture's: it would put the picture before its own
GPS fix (a fix may be old, never from later; FIX_AHEAD_SECONDS of slack),
or the picture has a position whose time zone has another offset at that
moment than the time zone of that picture's place (the camera went from
one time zone to another, and its clock may have been set in between).
A picture without a position also takes that picture's time zone when
they were taken at most place_estimates' window_minutes apart (default
30: the window of an estimated place), and so does a video whose track
says when it was recorded but whose position the GPS check set aside.

The place's time zone is the one of the nearest town (places.PlaceIndex,
GeoNames' time zone ids; zoneinfo gives the offset at the UTC moment).
Nothing is changed:

  - for a picture whose UTC offset in its file fits the time zone of its
    place (a phone's, mostly): it is right;
  - when the clock's offset is that of a town at most ZONE_BORDER_KM
    farther than the nearest one (the place is near the border of a time
    zone, and the camera may well be right), or when towns that near are
    in time zones with other offsets at that moment and none is the
    clock's (which side of the border it was taken on is not known);
  - without a time zone (no town within places.MAX_KM, or a place index
    built before time zones were kept: it is built again from the
    downloaded files), or without a time zone database (Python's zoneinfo
    needs the tzdata package where the system has none): the times stay
    the camera's, and the log says why;
  - for pictures whose dates come from the file's time (taken_source
    "mtime").

A picture whose UTC offset in its file does not fit its place, without
any of the facts above, is left as it is ("unsure": the clock may be
right and the offset wrong, or the other way round).

The local_times table (made on first use, not a schema version: it can
be worked out again) keeps, per picture changed:

    image_id      the picture
    clock         its camera's clock ('YYYY-MM-DDTHH:MM:SS', as its file says)
    clock_offset  the UTC offset its file gives ('' when none)
    taken_at, tz_offset   the local time and offset it was given
    zone          the time zone of its place ("Europe/Paris")
    kind          why: "track", "gps", "gps_date" or "like"
    source        for "like": the picture whose clock it goes by
    seconds       for "like": how far apart (by the clock)

run() works it out again after every scan, previews and places job, and
before an export that publishes positions (after the GPS check, which
sets aside positions that cannot be right; when times change, the GPS
check runs again: it times pictures by them), and at once when the owner
uses or ignores a picture's position or sets its place (after_check).
A picture whose file is read again (a scan writes its camera's time
back) is decided again. Everything is reversible: with config.json
"local_times": {"enabled": false} the next run gives every picture its
camera's time back and empties the table.

Run "python local_times.py" to see what the next run gives each library
(nothing is changed; --list names every picture).

This module uses only the standard library (zoneinfo).
"""

import argparse
import bisect
import collections
import datetime
import json
import re
import sqlite3
import sys
import time

import gps_check
import imageinfo
import place_estimates
import places
import tracks
import world_map

DEFAULTS = {"enabled": True}
TABLE = "local_times"
KINDS = ("track", "gps", "gps_date", "like")
# The keys of a picture's search record (search_index.py) that its time changes.
RECORD_KEYS = ("dk", "ts")
# A GPS time this near a whole quarter hour from the clock (seconds) is from the moment
# the picture was taken.
CLOCK_SLACK = 60
QUARTER = 900
OFFSET_RANGE = (-12 * 60, 14 * 60)      # minutes east of UTC
# A picture without facts of its own goes by the clock of the same camera's picture
# with facts taken at most this long before or after it (seconds, by that clock).
NEIGHBOUR_SECONDS = 3 * 3600
# A picture without a position takes the time zone of the same camera's picture
# only when they were taken at most this long apart (seconds, by the clock; run()
# uses place_estimates' window_minutes, see zone_seconds).
ZONE_SECONDS = 30 * 60
# A picture's own GPS fix may be this long after the moment a borrowed clock
# offset gives it (seconds: clocks and fixes are a little off), not more: a fix
# may be old, but it is never from after the picture was taken.
FIX_AHEAD_SECONDS = 300
# Pictures of one camera this near (seconds) are copies of one shot: the clock's
# offset is the same whatever offset their files give.
SAME_SHOT_SECONDS = 1
# A clock whose offset is that of a town at most this much farther (km) than the
# nearest one is taken to be right (the place is near a time zone border).
ZONE_BORDER_KM = 50.0
# Time zones looked up to tell whether a time zone database is there.
PROBE_ZONES = ("Europe/London", "America/New_York")
TRUSTED_SOURCES = gps_check.TRUSTED_SOURCES

_EPOCH = datetime.datetime(1970, 1, 1)
_UTC_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_DATE_RE = re.compile(r"^(\d{4})[:\-](\d\d)[:\-](\d\d)")

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS local_times (
    image_id INTEGER PRIMARY KEY,
    clock TEXT NOT NULL,
    clock_offset TEXT NOT NULL DEFAULT '',
    taken_at TEXT NOT NULL,
    tz_offset TEXT NOT NULL DEFAULT '',
    zone TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    source INTEGER,
    seconds REAL,
    updated REAL
)"""


# ---------------------------------------------------------------------------
# Settings and time zones

def settings(config):
    """config.json "local_times" checked: {"enabled": bool} (default on; a bare true or
    false is taken as "enabled")."""
    stored = config.get("local_times") if isinstance(config, dict) else None
    result = dict(DEFAULTS)
    if isinstance(stored, dict) and isinstance(stored.get("enabled"), bool):
        result["enabled"] = stored["enabled"]
    elif isinstance(stored, bool):
        result["enabled"] = stored
    return result


class Zones:
    """UTC offsets of time zones by IANA id (zoneinfo), each zone loaded once.

    Without a time zone database (Windows without the tzdata package) no
    zone loads: available() is False and offset() None.
    """

    def __init__(self):
        self._zones = {}
        self._available = None

    def get(self, name):
        """The zoneinfo.ZoneInfo of a time zone id, or None."""
        if not isinstance(name, str) or not name:
            return None
        if name not in self._zones:
            try:
                import zoneinfo
                self._zones[name] = zoneinfo.ZoneInfo(name)
            except Exception:       # (no zoneinfo, no such zone, no database, a bad id)
                self._zones[name] = None
        return self._zones[name]

    def available(self):
        """True when time zones can be looked up (a time zone database is there)."""
        if self._available is None:
            self._available = all(self.get(name) is not None for name in PROBE_ZONES)
        return self._available

    def offset(self, name, utc):
        """Minutes east of UTC of a time zone at a UTC moment (seconds since 1970), or
        None (an unknown zone, a moment out of range)."""
        zone = self.get(name)
        if zone is None or utc is None:
            return None
        try:
            delta = (_UTC_EPOCH + datetime.timedelta(seconds=utc)).astimezone(zone).utcoffset()
        except (OverflowError, ValueError, OSError):
            return None
        if delta is None:
            return None
        return int(round(delta.total_seconds() / 60.0))


# ---------------------------------------------------------------------------
# Facts of one picture (pure)

def offset_text(minutes):
    """'-07:00' of minutes east of UTC (images.tz_offset)."""
    return tracks.offset_text(minutes)


def local_text(seconds):
    """'YYYY-MM-DDTHH:MM:SS' of gps_check.local_seconds' seconds."""
    return imageinfo.format_datetime(_EPOCH + datetime.timedelta(seconds=seconds))


def clock_key(row):
    """What pictures share a clock under: the camera's name (gps_check.camera_key), a
    video without one its folder's videos, else None (no neighbours)."""
    camera = gps_check.camera_key(row.get("camera"))
    if camera:
        return camera
    if row.get("media") == "video":
        return ("folder", row.get("library_id"), (row.get("path") or "").rpartition("/")[0])
    return None


def _quarter_minutes(seconds):
    """Minutes east of UTC of a clock seconds ahead of UTC when that is a whole quarter
    hour within CLOCK_SLACK and OFFSET_RANGE, else None."""
    quarter = round(seconds / QUARTER) * QUARTER
    if abs(seconds - quarter) > CLOCK_SLACK:
        return None
    minutes = int(quarter // 60)
    return minutes if OFFSET_RANGE[0] <= minutes <= OFFSET_RANGE[1] else None


def gps_minutes(gps, local):
    """The UTC offset (minutes) of a clock by the GPS time saved with its picture (see
    the module notes, "gps"), or None. local: gps_check.local_seconds of the clock."""
    if not isinstance(gps, dict) or local is None:
        return None
    fix = gps_check.fix_time(gps, local)
    return None if fix is None else _quarter_minutes(local - fix)


def gps_day(gps):
    """The UTC day (days since 1970) of a GPS dict's GPSDateStamp, or None."""
    date = gps.get("GPSDateStamp") if isinstance(gps, dict) else None
    match = _DATE_RE.match(date) if isinstance(date, str) else None
    if not match:
        return None
    try:
        day = datetime.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return (day - _EPOCH).days


def local_offset(zones, zone, local):
    """The offset (minutes) of a time zone at the moment its clocks showed local
    (seconds), or None."""
    first = zones.offset(zone, local)
    if first is None:
        return None
    return zones.offset(zone, local - first * 60)


def date_minutes(gps, local, recorded, zone, zones):
    """The UTC offset (minutes) of a clock that shows the local time of its place while
    its file gives another offset that its GPS date contradicts (see the module notes,
    "gps_date"), or None. recorded: the file's offset (minutes); zone: the place's."""
    if recorded is None or local is None or not zone or not isinstance(gps, dict):
        return None
    if gps.get("GPSTimeStamp") not in (None, "", []):
        return None             # (a GPS time says more: gps_minutes)
    day = gps_day(gps)
    if day is None or (local - recorded * 60) // 86400 == day:
        return None
    minutes = local_offset(zones, zone, local)
    if minutes is None or minutes == recorded or (local - minutes * 60) // 86400 != day:
        return None
    return minutes


Decision = collections.namedtuple(
    "Decision", "kind minutes zone source seconds taken_at tz_offset")
Decision.__doc__ = """A picture's local time (decide): kind (KINDS), minutes (its clock's
UTC offset), zone (its place's time zone), source and seconds (for "like": the picture
whose clock it goes by, and how far apart they are by that clock, else None), and the
values for images.taken_at and images.tz_offset."""


class Item:
    """One picture as decide() takes it.

    clock and recorded are its camera's time and the UTC offset its file
    gives ("" when none); lat, lon its position (None without one); gps its
    EXIF GPS dict (or None); track its kept GPS track (tracks.Facts, or
    None); key its clock_key.
    """

    __slots__ = ("id", "library_id", "key", "clock", "recorded", "lat", "lon", "gps",
                 "track", "local", "minutes_recorded", "zone", "zone_km", "file_offset")

    def __init__(self, image_id, library_id, key, clock, recorded, lat=None, lon=None,
                 gps=None, track=None):
        self.id = image_id
        self.library_id = library_id
        self.key = key
        self.clock = clock
        self.recorded = recorded or ""
        positioned = world_map.valid_position(lat, lon)
        self.lat = float(lat) if positioned else None
        self.lon = float(lon) if positioned else None
        self.gps = gps if isinstance(gps, dict) else None
        self.track = track
        self.local = gps_check.local_seconds(clock)
        self.minutes_recorded = gps_check.offset_minutes(self.recorded)
        self.zone = None
        self.zone_km = None
        # The offset as its file gave it, for neighbours to compare (decide: not the
        # one a GPS track gave a video, tracks.apply_to_result).
        self.file_offset = self.recorded


def own_evidence(item, zones):
    """(kind, minutes) of the facts a picture has of its own (see the module notes), or
    None. item.zone must be set (gps_date needs it)."""
    if item.local is None:
        return None
    if item.track is not None and item.track.fixes:
        # (A track's times are UTC, tracks.py: a clock set to UTC, 0, is known too.)
        minutes = tracks.clock_offset(item.track.start, item.clock, gps_check.local_seconds)[0]
        if minutes is not None:
            return "track", minutes
        return None
    if item.lat is None or item.gps is None:
        return None
    minutes = gps_minutes(item.gps, item.local)
    if minutes is not None:
        return ("gps", minutes) if minutes else None
    minutes = date_minutes(item.gps, item.local, item.minutes_recorded, item.zone, zones)
    if minutes is not None:
        return "gps_date", minutes
    return None


def _fits(item, zone, zones):
    """True when the offset a picture's file gives is its place's at that moment."""
    if item.minutes_recorded is None or not zone:
        return False
    return zones.offset(zone, item.local - item.minutes_recorded * 60) == item.minutes_recorded


def _neighbours(item, by_key, items_by_id, window):
    """The pictures with facts of their own whose clock a picture may go by (see "like"),
    nearest first: the same clock key, at most window seconds apart by the clock, with
    the same offset in their file (or a copy, SAME_SHOT_SECONDS apart)."""
    entries = by_key.get(item.key)
    if not entries:
        return []
    index = bisect.bisect_left(entries, (item.local - window, -1))
    found = []
    while index < len(entries) and entries[index][0] <= item.local + window:
        other = items_by_id[entries[index][1]]
        index += 1
        if other.id == item.id:
            continue
        apart = abs(other.local - item.local)
        if other.file_offset != item.file_offset and apart > SAME_SHOT_SECONDS:
            continue
        found.append(((apart, other.id), other))
    found.sort(key=lambda pair: pair[0])
    return [other for _key, other in found]


def _neighbour(item, by_key, items_by_id, window):
    """The nearest of _neighbours, or None."""
    found = _neighbours(item, by_key, items_by_id, window)
    return found[0] if found else None


def contradicts_fix(item, minutes):
    """True when a clock offset (minutes) would put a picture before its own GPS fix (its
    GPSDateStamp, with the GPSTimeStamp when there is one): a fix may be old, but it is
    never from after the picture was taken (FIX_AHEAD_SECONDS of slack)."""
    if item.gps is None or item.local is None:
        return False
    utc = item.local - minutes * 60
    fix = gps_check.fix_time(item.gps)          # (None without a GPS date)
    if fix is not None:
        return fix - utc > FIX_AHEAD_SECONDS
    day = gps_day(item.gps)
    return day is not None and day > utc // 86400


def moved_zones(item, other, minutes, zones):
    """True when a picture with a position of its own was taken in a time zone whose
    offset at that moment (by the clock offset minutes) is not that of the time zone of
    the other picture's place: the camera went from one to the other, and its clock may
    have been set in between."""
    if item.lat is None or not item.zone or not other.zone or item.zone == other.zone:
        return False
    utc = item.local - minutes * 60
    return zones.offset(item.zone, utc) != zones.offset(other.zone, utc)


def zone_seconds(config):
    """How far apart (seconds, by the clock) a picture without a position and the
    picture whose time zone it takes may be: place_estimates' window_minutes (the
    window of an estimated place)."""
    return place_estimates.settings(config)["window_minutes"] * 60.0


def decide(items, index, zones, window=NEIGHBOUR_SECONDS, zone_window=ZONE_SECONDS):
    """The local times of pictures (Items): ({image id: Decision} of those whose time or
    offset changes, {image id: why} of the others, which keep their camera's time:
    "right" (it is the local time), "border" (it may be, near a time zone border; or
    which side of it is not known), "unsure" (their file's offset does not fit their
    place, or the clock of the same camera's pictures cannot be theirs, and nothing
    says which is wrong), "no_zone" (no time zone for their place) or "no_facts").

    window: how far apart by the clock (seconds) a picture without facts and
    the one whose clock it goes by may be; zone_window: a picture without a
    position and the one whose time zone it takes. index finds the time zone
    of a position (places.PlaceIndex: zone_of, zones_near), zones its offset
    at a moment (Zones). Pure: no database, no clock.
    """
    by_id = {item.id: item for item in items}
    evidence = {}
    for item in items:
        if item.lat is not None and item.local is not None:
            found = index.zone_of(item.lat, item.lon)
            if found is not None:
                item.zone, item.zone_km = found
        own = own_evidence(item, zones)
        if own is not None:
            evidence[item.id] = own
            if own[0] == "track" and item.recorded == offset_text(own[1]):
                item.file_offset = ""       # (the track's, tracks.apply_to_result)
    by_key = collections.defaultdict(list)
    for item_id, _own in evidence.items():
        item = by_id[item_id]
        if item.key is not None and item.zone:
            by_key[item.key].append((item.local, item.id))
    for entries in by_key.values():
        entries.sort()
    decisions = {}
    kept = {}
    for item in items:
        if item.local is None:
            continue
        if item.zone and _fits(item, item.zone, zones):
            # (The offset its file gives is the place's: never changed.)
            kept[item.id] = "right"
            continue
        source = seconds = None
        if item.id in evidence:
            kind, minutes = evidence[item.id]
            zone = item.zone
            if not zone and item.lat is None and item.key is not None:
                # (A clip whose position was set aside: the time zone of the
                # same camera's nearest picture, taken near enough in time.)
                other = _neighbour(item, by_key, by_id, zone_window)
                zone = other.zone if other is not None else None
        else:
            found = _neighbours(item, by_key, by_id, window) if item.key is not None else []
            # (Not one whose clock's offset cannot be this picture's.)
            other = next((other for other in found
                          if not contradicts_fix(item, evidence[other.id][1])
                          and not moved_zones(item, other, evidence[other.id][1], zones)),
                         None)
            if other is None:
                kept[item.id] = ("unsure" if found or (item.zone and
                                                       item.minutes_recorded is not None)
                                 else "no_facts")
                continue
            kind, minutes = "like", evidence[other.id][1]
            source, seconds = other.id, abs(other.local - item.local)
            if item.lat is not None:
                zone = item.zone
            else:
                # (Where that picture was, when taken near enough in time.)
                zone = other.zone if seconds <= zone_window else None
        if not zone:
            kept[item.id] = "no_zone"
            continue
        if _fits(item, zone, zones):
            kept[item.id] = "right"
            continue
        utc = item.local - minutes * 60
        offset = zones.offset(zone, utc)
        if offset is None:
            kept[item.id] = "no_zone"
            continue
        border = False
        if offset != minutes and item.lat is not None and item.zone_km is not None:
            # (Near the border of a time zone the nearest town may be across it:
            # the clock may be right; or which side it was taken on is not known.)
            near = sorted(index.zones_near(item.lat, item.lon, item.zone_km + ZONE_BORDER_KM))
            offsets = {other_zone: zones.offset(other_zone, utc) for other_zone in near}
            fitting = [other_zone for other_zone in near if offsets[other_zone] == minutes]
            if fitting:
                zone, offset, border = fitting[0], minutes, True
            elif len(set(offsets.values()) - {None}) > 1:
                kept[item.id] = "border"
                continue
        taken_at = local_text(item.local + (offset - minutes) * 60)
        tz_offset = "" if offset == minutes and not item.recorded else offset_text(offset)
        if (taken_at, tz_offset) == (item.clock, item.recorded):
            kept[item.id] = "border" if border else "right"
            continue
        decisions[item.id] = Decision(kind, minutes, zone, source, seconds, taken_at, tz_offset)
    return decisions, kept


# ---------------------------------------------------------------------------
# The table

def have_table(conn):
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                        "AND name = ?", (TABLE,)).fetchone()[0] == 1


def ensure_table(conn):
    """Create the local_times table when it is not there yet."""
    if not have_table(conn):
        conn.execute(_TABLE_SQL)


def stored(conn):
    """{image id: local_times row as a dict} ({} before the table is made)."""
    if not have_table(conn):
        return {}
    return {row["image_id"]: dict(row) for row in conn.execute("SELECT * FROM local_times")}


def applies(side, row):
    """True when a picture's images row still has the local time a local_times row
    gave it (a scan that read the file again writes the camera's time back)."""
    return (side is not None and row["taken_at"] == side["taken_at"]
            and (row["tz_offset"] or "") == (side["tz_offset"] or ""))


def file_read_again(conn, image_id):
    """Forget the local time of a picture whose file was read again (the scanner wrote its
    camera's time back): the next run decides about it again."""
    if have_table(conn):
        conn.execute("DELETE FROM local_times WHERE image_id = ?", (image_id,))


def for_picture(conn, row):
    """The local_times row (a dict) of an images row whose local time it gave, or None."""
    if not have_table(conn):
        return None
    found = conn.execute("SELECT * FROM local_times WHERE image_id = ?",
                         (row["id"],)).fetchone()
    side = dict(found) if found is not None else None
    return side if applies(side, row) else None


def view(conn, row, admin=False):
    """What the viewer shows of a picture's camera clock (/api/image "clock"), or None
    when its time is its camera's: {"taken_at", "tz_offset" (the clock and the offset
    its file gives), "kind", "zone"} -- the zone only for the admin or with a position
    of its own (a zone says roughly where it was) -- and for the admin "source" and
    "seconds" of a "like"."""
    side = for_picture(conn, row)
    if side is None:
        return None
    result = {"taken_at": side["clock"], "tz_offset": side["clock_offset"] or "",
              "kind": side["kind"]}
    if admin or row["lat"] is not None:
        result["zone"] = side["zone"]
    if admin and side["kind"] == "like":
        result["source"] = side["source"]
        result["seconds"] = side["seconds"]
    return result


def detail(conn, row):
    """The time of a picture as the admin's /api/image gives it: {"taken_at",
    "tz_offset", "clock" (view, or None)}, for a route that may have changed it."""
    return {"taken_at": row["taken_at"], "tz_offset": row["tz_offset"],
            "clock": view(conn, row, admin=True)}


def camera_times(conn, ids=None):
    """{image id: (camera's clock, offset its file gives)} of the pictures whose time is
    a local one (ids: of these only). For what goes by one camera's clock, such as the
    clips of a drive (drives.py)."""
    if not have_table(conn):
        return {}
    sql = ("SELECT l.image_id, l.clock, l.clock_offset FROM local_times AS l "
           "JOIN images AS i ON i.id = l.image_id "
           "WHERE i.taken_at = l.taken_at AND i.tz_offset = l.tz_offset")
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        found = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            for row in conn.execute(sql + " AND l.image_id IN (%s)" % ",".join("?" * len(part)),
                                    part):
                found[row[0]] = (row[1], row[2] or "")
        return found
    return {row[0]: (row[1], row[2] or "") for row in conn.execute(sql)}


# ---------------------------------------------------------------------------
# Running

_ROWS_SQL = """
    SELECT id, library_id, path, media, camera, taken_at, taken_source, tz_offset, lat, lon,
        content_hash, CASE WHEN exif LIKE '%%"GPS"%%' THEN exif END AS exif
    FROM images WHERE missing_since IS NULL AND library_id IN (%s)"""


def _library_ids(config):
    return [library.get("id") for library in (config or {}).get("libraries", [])
            if isinstance(library, dict) and library.get("id")]


def _gps_of(exif_text):
    if not exif_text:
        return None
    try:
        exif = json.loads(exif_text, parse_constant=lambda _name: None)
    except ValueError:
        return None
    gps = exif.get("GPS") if isinstance(exif, dict) else None
    return gps if isinstance(gps, dict) else None


def _empty_summary(state):
    return {"state": state, "checked": 0, "local": 0, "shifted": 0, "offset_only": 0,
            "kinds": {}, "changed": 0, "restored": 0, "kept": {}, "libraries": {},
            "seconds": 0.0}


def _restore_all(conn, summary, now, changed=None):
    """Give every picture whose time is a local one its camera's time back, and empty
    the table (local times turned off)."""
    if not have_table(conn):
        return
    for image_id, side in stored(conn).items():
        row = conn.execute("SELECT id, taken_at, tz_offset FROM images WHERE id = ?",
                           (image_id,)).fetchone()
        if row is not None and applies(side, row):
            conn.execute("UPDATE images SET taken_at = ?, tz_offset = ?, updated = ? "
                         "WHERE id = ?", (side["clock"], side["clock_offset"] or "", now,
                                          image_id))
            summary["restored"] += 1
            if changed is not None:
                changed.add(image_id)
    conn.execute("DELETE FROM local_times")


def state(config, index=None, zones=None):
    """Whether local times can be worked out now: "done" (yes), "off" (turned off in
    config.json), "no_places" (no place names: download them), "no_zones" (a place index
    without time zones: built again from the downloaded files by the next run) or
    "no_tzdata" (no time zone database: install the tzdata package). index: the place
    index to use (without one, only the facts of places.db are read)."""
    if not settings(config)["enabled"]:
        return "off"
    if index is not None:
        zoned = bool(getattr(index, "has_zones", False))
    else:
        try:
            zoned = places.has_zones(places.read_meta())
        except places.PlacesError:
            return "no_places"
    if not zoned:
        return "no_zones"
    if not (zones or Zones()).available():
        return "no_tzdata"
    return "done"


def plan(conn, config, index, zones, library_ids):
    """What run() works out, reading only: (rows, items, decisions, kept, stored rows).

    rows are the present pictures of the libraries (dicts with "clock" and
    "recorded": their camera's time and the offset their file gives),
    items the Items of those decide() looks at (trusted dates), decisions
    and kept decide()'s, stored rows the local_times rows (stored()).
    """
    rows = [dict(row) for row in conn.execute(
        _ROWS_SQL % ",".join("?" * len(library_ids)), library_ids)]
    sides = stored(conn)
    found = tracks.for_rows(conn, rows, config)
    items = []
    for row in rows:
        side = sides.get(row["id"])
        if applies(side, row):
            row["clock"], row["recorded"] = side["clock"], side["clock_offset"] or ""
        else:
            row["clock"], row["recorded"] = row["taken_at"], row["tz_offset"] or ""
        if row["taken_source"] not in TRUSTED_SOURCES:
            continue
        item = Item(row["id"], row["library_id"], clock_key(row), row["clock"],
                    row["recorded"], row["lat"], row["lon"], _gps_of(row["exif"]),
                    found.get(row["id"]))
        if item.local is not None:
            items.append(item)
    decisions, kept = decide(items, index, zones, zone_window=zone_seconds(config))
    return rows, items, decisions, kept, sides


def run(conn, config, ctx=None, now=None, index=None, zones=None, library_ids=None,
        changed=None):
    """Work out the local times of the present pictures of config's libraries (enabled
    or not; library_ids limits it, for tests) and write them (see the module notes).
    changed (a set) gets the ids of the pictures whose time or offset it changed.

    Without what it needs (state() is not "done") nothing changes, unless local
    times are turned off: then every picture gets its camera's time back.
    Everything is written in one transaction. Returns {"state", "checked"
    (pictures looked at), "local" (pictures that have a local time now),
    "shifted" (of those, whose time is not their camera's), "offset_only",
    "kinds": {kind: pictures}, "changed" (rows this run changed), "restored"
    (rows given their camera's time back), "kept": decide's Counter as a
    dict, "libraries": {library id: {"shifted", "offset_only", "unsure"}},
    "seconds"}.
    """
    started = time.perf_counter()
    now = time.time() if now is None else now
    zones = zones or Zones()
    if index is None and settings(config)["enabled"]:
        index = places.shared()
        if index is None:
            return _empty_summary("no_places")
    summary = _empty_summary(state(config, index, zones))
    if summary["state"] == "off":
        if have_table(conn):
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                _restore_all(conn, summary, now, changed)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if summary["restored"] and ctx is not None:
            ctx.log("Local times are off: %d picture(s) have their camera's time again."
                    % summary["restored"])
        return summary
    if summary["state"] != "done":
        return summary
    if library_ids is None:
        library_ids = _library_ids(config)
    library_ids = list(library_ids)
    if not library_ids:
        return summary
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows, items, decisions, kept, sides = plan(conn, config, index, zones, library_ids)
        summary["checked"] = len(items)
        summary["kept"] = dict(collections.Counter(kept.values()))
        kinds = collections.Counter()
        libraries = collections.defaultdict(lambda: {"shifted": 0, "offset_only": 0,
                                                     "unsure": 0})
        for item in items:
            if kept.get(item.id) == "unsure":
                libraries[item.library_id]["unsure"] += 1
        if decisions:
            ensure_table(conn)
        for row in rows:
            image_id = row["id"]
            decision = decisions.get(image_id)
            side = sides.get(image_id)
            if decision is not None:
                wanted = (decision.taken_at, decision.tz_offset)
                kinds[decision.kind] += 1
                summary["local"] += 1
                bucket = "shifted" if decision.taken_at != row["clock"] else "offset_only"
                summary[bucket] += 1
                libraries[row["library_id"]][bucket] += 1
            else:
                wanted = (row["clock"], row["recorded"])
            if wanted != (row["taken_at"], row["tz_offset"] or ""):
                conn.execute("UPDATE images SET taken_at = ?, tz_offset = ?, updated = ? "
                             "WHERE id = ?", wanted + (now, image_id))
                if changed is not None:
                    changed.add(image_id)
                if decision is not None:
                    summary["changed"] += 1
                else:
                    summary["restored"] += 1
            if decision is not None:
                values = (row["clock"], row["recorded"], decision.taken_at, decision.tz_offset,
                          decision.zone, decision.kind, decision.source, decision.seconds)
                if side is None or tuple(side[key] for key in (
                        "clock", "clock_offset", "taken_at", "tz_offset", "zone", "kind",
                        "source", "seconds")) != values:
                    conn.execute("INSERT OR REPLACE INTO local_times (image_id, clock, "
                                 "clock_offset, taken_at, tz_offset, zone, kind, source, "
                                 "seconds, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (image_id,) + values + (now,))
            elif side is not None:
                conn.execute("DELETE FROM local_times WHERE image_id = ?", (image_id,))
        if have_table(conn):
            # (Rows of pictures that are gone; a missing one keeps its own.)
            conn.execute("DELETE FROM local_times WHERE image_id NOT IN "
                         "(SELECT id FROM images)")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    summary["kinds"] = dict(kinds)
    summary["libraries"] = {key: dict(value) for key, value in libraries.items()}
    summary["seconds"] = round(time.perf_counter() - started, 3)
    if ctx is not None and (summary["changed"] or summary["restored"]):
        ctx.log(summary_text(summary))
    return summary


_KIND_WORDS = (("track", "by their GPS track"), ("gps", "by their GPS time"),
               ("gps_date", "by their GPS date"),
               ("like", "by another picture of the same camera"))


def summary_text(summary):
    """The log line of a run that changed something."""
    parts = ["{:,} {}".format(summary["kinds"][kind], words) for kind, words in _KIND_WORDS
             if summary["kinds"].get(kind)]
    text = "Local times: {:,} picture{} show the local time of their place".format(
        summary["local"], "" if summary["local"] == 1 else "s")
    if parts:
        text += " (" + ", ".join(parts) + ")"
    details = []
    if summary["shifted"]:
        details.append("{:,} at another time than their camera's clock".format(
            summary["shifted"]))
    if summary["offset_only"]:
        details.append("{:,} with another UTC offset only".format(summary["offset_only"]))
    if details:
        text += ": " + ", ".join(details)
    changes = []
    if summary["changed"]:
        changes.append("{:,} changed now".format(summary["changed"]))
    if summary["restored"]:
        changes.append("{:,} back to their camera's time".format(summary["restored"]))
    if changes:
        text += "; " + ", ".join(changes)
    return text + "."


_STATE_WORDS = {
    "no_places": "there are no place names (download them on the Manage page)",
    "no_zones": "the place index has no time zones yet (a scan builds it again from the "
                "downloaded files; without them, download the place names again)",
    "no_tzdata": "Python has no time zone database here (install it: pip install tzdata)",
}


def upgrade_places(conn, ctx=None):
    """Build the place index again from the downloaded files when it has no time zones
    (or cannot be used): places.rebuild_if_needed. The pictures with a position that
    have no place yet are then looked up. Returns True when it was built."""
    try:
        rebuilt = places.rebuild_if_needed(ctx)
    except places.PlacesError as error:
        if ctx is not None:
            ctx.log("The place index could not be built again: %s" % error, "warn")
        return False
    if rebuilt is None:
        return False
    if ctx is not None:
        places.label_pictures(conn, ctx)
    return True


def run_safely(conn, config, ctx=None):
    """run() for a job, after the GPS check: the place index is built again first when it
    has no time zones (upgrade_places), a busy database is only a warning in the log
    (and the answer None), and when any picture's time changed the GPS check runs
    again (it times pictures by them)."""
    if settings(config)["enabled"]:
        upgrade_places(conn, ctx)
    try:
        summary = run(conn, config, ctx)
    except sqlite3.OperationalError as error:
        if ctx is not None:
            ctx.log("Local times could not be worked out (%s); they are after the next "
                    "scan." % error, "warn")
        return None
    if summary["state"] in _STATE_WORDS and ctx is not None:
        ctx.log("Times stay as the cameras' clocks have them: %s."
                % _STATE_WORDS[summary["state"]], "warn" if summary["state"] == "no_tzdata"
                else "info")
    if summary["changed"] or summary["restored"]:
        gps_check.run_safely(conn, config, ctx)
    return summary


def after_check(conn, config, changed=None, ctx=None):
    """run() right after a GPS check that may have set positions aside or given them back:
    the owner's choice about a position or a place (admin.py), an export's check
    (export.py). The local times are decided again now, not at the next scan. The
    place index is not built again here, and nothing is said of what is missing;
    when times changed the GPS check runs again (it times pictures by them). changed
    (a set) gets the ids of the pictures whose time (or then position, or place)
    changed. A busy database raises sqlite3.OperationalError. Returns the set of the
    ids of the pictures whose time changed."""
    times = set()
    run(conn, config, ctx, changed=times)
    if times:
        gps_check.run(conn, config, ctx, changed=changed)
        if changed is not None:
            changed.update(times)
    return times


def after_check_safely(conn, config, ctx=None):
    """after_check() for a job: a busy database is only a warning in the log (and the
    answer None)."""
    try:
        return after_check(conn, config, ctx=ctx)
    except sqlite3.OperationalError as error:
        if ctx is not None:
            ctx.log("Local times could not be worked out (%s); they are after the next "
                    "scan." % error, "warn")
        return None


def status(conn, config=None):
    """What the Manage page shows: {"enabled", "state" (see state()), "local" (present
    pictures with a local time), "shifted", "offset_only", "kinds": {kind: pictures}}."""
    if config is None:
        import common
        try:
            config = common.load_config()
        except common.ConfigError:
            config = {}
    result = {"enabled": settings(config)["enabled"], "state": state(config), "local": 0,
              "shifted": 0, "offset_only": 0, "kinds": {}}
    if conn is None or not have_table(conn):
        return result
    kinds = collections.Counter()
    for row in conn.execute(
            "SELECT l.kind, l.clock <> l.taken_at FROM local_times AS l "
            "JOIN images AS i ON i.id = l.image_id WHERE i.missing_since IS NULL "
            "AND i.taken_at = l.taken_at AND i.tz_offset = l.tz_offset"):
        kinds[row[0]] += 1
        result["local"] += 1
        result["shifted" if row[1] else "offset_only"] += 1
    result["kinds"] = dict(kinds)
    return result


# ---------------------------------------------------------------------------
# Command line

def _library_names(conn, config):
    names = {row[0]: row[1] for row in conn.execute("SELECT id, name FROM libraries")}
    for library in (config or {}).get("libraries", []):
        if isinstance(library, dict) and library.get("id"):
            names[library["id"]] = library.get("name") or library["id"]
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Show which pictures get the local time of their place (nothing is "
                    "changed: scans do that).")
    parser.add_argument("--list", action="store_true",
                        help="list every picture whose time or UTC offset is not its "
                             "camera's, and those left as they are for want of facts")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    import common
    import db
    try:
        config = common.load_config()
        conn = db.connect_readonly()
    except (common.ConfigError, db.DatabaseError) as error:
        print("Error: %s" % error)
        return 2
    try:
        zones = Zones()
        index = places.shared()
        found = state(config, index, zones) if index is not None else "no_places"
        if found == "no_places" and not settings(config)["enabled"]:
            found = "off"
        if found != "done":
            print("Times stay as the cameras' clocks have them: %s." % (
                "local times are off in config.json" if found == "off"
                else _STATE_WORDS[found]))
            return 1 if found == "no_tzdata" else 0
        names = _library_names(conn, config)
        rows, items, decisions, kept, _sides = plan(conn, config, index, zones,
                                                    _library_ids(config))
    finally:
        conn.close()
    by_id = {row["id"]: row for row in rows}
    counts = collections.defaultdict(collections.Counter)
    for image_id, decision in decisions.items():
        row = by_id[image_id]
        counts[row["library_id"]]["shifted" if decision.taken_at != row["clock"]
                                  else "offset only"] += 1
        counts[row["library_id"]][decision.kind] += 1
    for image_id, why in kept.items():
        if why == "unsure":
            counts[by_id[image_id]["library_id"]]["unsure"] += 1
    print("%d pictures looked at; %d get the local time of their place (or its UTC offset)."
          % (len(items), len(decisions)))
    for library_id in sorted(counts, key=lambda key: names.get(key, key).casefold()):
        found = counts[library_id]
        print("  %s: %s" % (names.get(library_id, library_id), ", ".join(
            "%d %s" % (found[key], key) for key in ("shifted", "offset only") + KINDS
            + ("unsure",) if found.get(key))))
    if args.list:
        for image_id in sorted(decisions, key=lambda key: (by_id[key]["library_id"],
                                                           by_id[key]["clock"], key)):
            row, decision = by_id[image_id], decisions[image_id]
            print("%s  %s  %s %s -> %s %s  %s%s  %s" % (
                names.get(row["library_id"], row["library_id"]), row["path"], row["clock"],
                row["recorded"] or "-", decision.taken_at, decision.tz_offset or "-",
                decision.kind, "" if decision.source is None else
                " picture %d, %d s apart" % (decision.source, decision.seconds),
                decision.zone))
        for image_id in sorted((key for key, why in kept.items() if why == "unsure"),
                               key=lambda key: (by_id[key]["library_id"], by_id[key]["clock"])):
            row = by_id[image_id]
            print("%s  %s  %s %s  left as it is: its UTC offset does not fit its place, and "
                  "nothing says which is right" % (names.get(row["library_id"],
                                                             row["library_id"]),
                                                   row["path"], row["clock"], row["recorded"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
