#!/usr/bin/env python3
"""Estimated places: where a picture without a usable GPS position was most likely taken.

Many pictures have no position of their own: cameras without a GPS
receiver, phones that saved a picture without its location, positions the
GPS check (gps_check.py) set aside. run() estimates a place for each of
them from what the index already knows, by the first of these rules that
gives one:

  copy   a copy of a located picture: the same file (content hash), a
         picture of the same camera model taken in the same second, one
         with the same dHash taken within gps_check.COPY_SECONDS (2
         minutes), or one that looks the same (look vectors, looks.py: at
         least search_index.SAME_PICTURE_COSINE alike) taken within
         COPY_LOOK_SECONDS (10 minutes). Its position; the radius is the
         accuracy the copy states (at least RADIUS_MIN_KM), and for a
         dHash or look copy (another shot) also how far the camera may
         have gone in the time between them: the speed its GPS states, or
         that of the same camera's shots just before and after it, at
         least WALK_KMH. Such a copy whose radius is more than
         max_spread_km (a coarse fix; shots of the clouds from an
         aeroplane) is none.
  time   the pictures taken around the same time: positions the GPS check
         keeps and counts on (gps_check.Check.references: not set aside,
         not weak; other libraries' too unless gps_check's
         across_libraries is off), whose stated accuracy is at most
         max_spread_km, taken within window_minutes (default 30) of the
         picture by the check's clocks (gps_check.Check.timing: UTC
         times, with every time zone the picture may have had; the window
         must hold its furthest time). The nearest picture before it and
         the nearest after it, when they are at most max_spread_km
         (default 2) apart: the position between them, in proportion to
         the times; the radius is how far apart they are (or their stated
         accuracy, if more), plus WALK_KMH for the time to the nearer.
         Two sides further apart (a drive, a flight) give no estimate.
         With pictures on one side only: the nearest, when it and the next
         shots on that side (up to MAX_SIDE_SHOTS; at least MIN_SIDE_SHOTS
         shots, copies of one shot counting once) are at most
         max_spread_km apart -- the camera stood still, or walked -- and
         it was taken within half the window. The radius is how far apart
         those pictures are (or their accuracy), plus how far the camera
         may have gone since: at the speed of those shots (or their GPS's,
         at least WALK_KMH), and after VEHICLE_SECONDS at least
         VEHICLE_KMH (it may have boarded a car, a boat, a helicopter). A
         radius of more than max_spread_km gives no position ("coarse" in
         the detail), only the town of the nearest picture -- the region
         alone above TOWN_KM, and nothing above COARSE_KM.
         A video's GPS track (tracks.py) counts as pictures taken every
         gps_check.TRACK_STEP seconds along it: a picture taken on the way
         is placed by the track (the detail's "track": "all", or "some"
         with other pictures; an export treats those as the track's).
  area   the library's area (library_area, default on): when at least
         AREA_SHARE (95%) of a library's located pictures (at least
         AREA_MIN of them) are within AREA_KM (100 km) of their middle,
         and that share of those names one region (else one country), a
         picture of the library that no rule above placed gets the region
         ("Tuscany, Italy") and no position: when it was taken within
         AREA_HOURS (3 hours) of the located pictures' times (by its
         camera's clock in no known time zone: with any time zone it may
         have had; a program's export, dated when it was saved: then or
         before) and, by UTC, no located picture taken nearer in time is
         outside the area -- a picture of another trip that ended up in
         the library, or one taken on the way there (at the airport, on
         the plane), gets nothing; or when it has no date to go by. A
         picture without a date in UTC (undated, a clock's, an export's)
         gets it only when the located pictures are at least
         AREA_LIBRARY_SHARE (10%) of the library's pictures: a few located
         ones in a large library say little about the rest.

Dates are those the GPS check can time (gps_check.timed_row: the camera's,
a video's own, a date in a file name; one from a program's export -- a
file name an app gave it, or EXIF DateTime alone, with a Software tag --
reaches 2 hours back, so it never fits the window). The camera's clock
is taken to be right: one that is an hour off (a camera that ignores
daylight saving) places its pictures where the others were an hour
earlier or later. A date from the file's
time (taken_source "mtime") counts only when the file times around it in
its folder are capture times: the pictures of the folder with a capture
date whose files were written within FILE_TIME_REACH (an hour) of its own
-- at least FILE_TIME_PEERS of them -- all have file times within
FILE_TIME_SLACK (2 minutes) of their capture dates (a video's: of its
end). A camera's files copied from its card often keep their times; files
downloaded, exported or copied by an app get the time of the copy, and so
do the pictures around them.

The rules and their defaults were chosen by hiding the positions of about
1,800 located pictures (a tenth at a time, three times over) and
estimating them: 61% got an estimate (two in five of those from a copy,
such as a photo's JPEG beside its HEIC); of those with a position 99.6% in
the right town, none more than 1.5 km off (the median 1 m) and 99.5%
within their radius, and those too rough for one (about 2%) all named the
right town. One side without a second shot, without the speed of its
shots, or a longer window put pictures taken on a flight or a drive tens
of kilometres off; without the accuracy of the positions used, the time
to a look-alike copy and a vehicle's pace after a couple of minutes,
pictures taken from an aeroplane, or just after a helicopter took off,
were placed kilometres (or hundreds of kilometres) off with a radius of a
few hundred metres.

The owner decides last (place_choices, kept by content like gps_choices,
so a choice follows the picture when it moves): "set" gives the picture a
place of the owner's -- an estimate accepted, a town or position chosen
(set_choice) -- and "reject" gives it none. The owner's place counts as
the picture's own (not estimated); it never makes the picture a GPS
picture (has:gps stays the file's GPS only), and it is no reference for
other estimates.

Estimates are worked out again after every GPS check (gps_check.run calls
update: after each scan, previews and places job, before an export with
locations and when the owner decides), so a new located picture can
improve them. They are kept in index.db (ensure_tables; tables made on
first use, not a schema version: they hold nothing that cannot be worked
out again, except the owner's choices):

    place_estimates  image_id, source ("copy", "time", "area" or "owner"),
                     lat, lon (NULL for an area, a time estimate too rough
                     for a position, or the owner's town or region),
                     radius_km (how far off it may be; NULL: not known),
                     detail (JSON: "ids" the pictures it came from,
                     "seconds" their times less the picture's, "km" how
                     far apart they are, "kind" what made a copy one,
                     "track" when they include a video's GPS track,
                     "coarse" for a time estimate without a position),
                     place_city, place_region, place_country, place_cc,
                     place_km (its place names: places.place_columns of
                     the position, an area's region and country),
                     updated
    place_choices    content_key (gps_check.content_key), choice ("set" or
                     "reject"), lat, lon (the owner's position; NULL with
                     a town or region only), place_city, place_region,
                     place_country, place_cc (their names), detail (JSON:
                     "track" when the estimate accepted came from a video's
                     GPS track: an export holds it as it holds the track's),
                     updated

A change touches the picture's images.updated, so the search index
notices. Search records (search_index) carry an estimate's names (ci, rg,
co, cc), pe 1 and ps (its source: "c", "t" or "a"); the owner's place
only ps "o". The map shows positioned estimates as hollow pins
(located()). An export holds estimates only with its location and
estimated_places options, the owner's places with location -- those from
a video's GPS track (their detail's "track", an accepted one's too) only
as it holds the track's own positions (export._Routes: with the routes
option, and not near where a drive starts or ends).

Settings: config.json "place_estimates" (see settings() and SETTINGS.md).

This module uses only the standard library.
"""

import bisect
import collections
import json
import math
import re
import sqlite3
import time

import gps_check
import places
import world_map

DEFAULTS = {
    "enabled": True,
    "window_minutes": 30,       # pictures this near in time are asked
    "max_spread_km": 2.0,       # ... and must be this near each other
    "library_area": True,       # a library's region for pictures left without one
}
RANGES = {
    "window_minutes": (1, 240),
    "max_spread_km": (0.1, 50.0),
}

# The search records' ps values (search_index) of the sources.
RECORD_SOURCES = {"copy": "c", "time": "t", "area": "a", "owner": "o"}

COPY_LOOK_SECONDS = 600     # a picture that looks the same, taken this near: a copy
RADIUS_MIN_KM = 0.1
WALK_KMH = 5.0              # the radius grows this much per hour to the nearest picture ...
VEHICLE_SECONDS = 120       # ... and on one side, after this long, at least ...
VEHICLE_KMH = 50.0          # ... this much (the camera may have boarded a car)
COARSE_KM = 25.0            # a radius above max_spread_km, up to this: no position, ...
TOWN_KM = 10.0              # ... and up to this the town, beyond it the region only
MIN_SIDE_SHOTS = 2          # shots on one side, at least ...
MAX_SIDE_SHOTS = 3          # ... and the nearest this many must agree ...
ONE_SIDE_SHARE = 0.5        # ... the nearest within this share of the window
NOISE_KM = 0.1              # two positions this near may be one place
AREA_KM = 100.0
AREA_SHARE = 0.95
AREA_MIN = 20
AREA_HOURS = 3              # a dated picture: this near the located pictures' times
AREA_LIBRARY_SHARE = 0.1    # an undated one: located pictures this share of the library
FILE_TIME_REACH = 3600      # s: the folder's file times this near ...
FILE_TIME_PEERS = 2         # ... at least this many ...
FILE_TIME_SLACK = 120       # ... within this of their capture dates

Estimate = collections.namedtuple("Estimate", "source lat lon radius_km detail area")
Estimate.__doc__ = """A place estimate (see the module notes).

source     "copy", "time", "area" or "owner"
lat, lon   the position (None for an area, or the owner's region)
radius_km  how far off the position may be (None: not known)
detail     {"ids", "seconds", "km", "kind"} as the detail column keeps it
area       (region, country, cc) of an area (the library's, or the owner's region), or
           (region, country, cc, town) of a time estimate too rough for a position
           (town "": the region only) or of the owner's town; else None
"""

_TABLES = (
    """CREATE TABLE IF NOT EXISTS place_estimates (
        image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        lat REAL,
        lon REAL,
        radius_km REAL,
        detail TEXT NOT NULL DEFAULT '{}',
        place_city TEXT NOT NULL DEFAULT '',
        place_region TEXT NOT NULL DEFAULT '',
        place_country TEXT NOT NULL DEFAULT '',
        place_cc TEXT NOT NULL DEFAULT '',
        place_km REAL,
        updated REAL
    )""",
    """CREATE TABLE IF NOT EXISTS place_choices (
        content_key TEXT PRIMARY KEY,
        choice TEXT NOT NULL CHECK (choice IN ('set', 'reject')),
        lat REAL,
        lon REAL,
        place_city TEXT NOT NULL DEFAULT '',
        place_region TEXT NOT NULL DEFAULT '',
        place_country TEXT NOT NULL DEFAULT '',
        place_cc TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}',
        updated REAL
    )""",
)
TABLES = ("place_estimates", "place_choices")
# Columns added to a table made by an earlier version: (table, column, definition).
_ADDED_COLUMNS = (("place_choices", "place_city", "TEXT NOT NULL DEFAULT ''"),
                  ("place_choices", "detail", "TEXT NOT NULL DEFAULT '{}'"))


# ---------------------------------------------------------------------------
# Settings

def _number(value, low, high, whole):
    """value held to [low, high] (whole: rounded), or None when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = max(low, min(high, value))
    return int(round(value)) if whole else float(value)


def settings(config):
    """config.json "place_estimates" checked, with defaults: {"enabled", "window_minutes",
    "max_spread_km", "library_area"} (see DEFAULTS; numbers held to RANGES).

    The GPS check's older "estimate_places" and "estimate_minutes" stand in
    for "enabled" and "window_minutes" when those are not given.
    """
    config = config if isinstance(config, dict) else {}
    stored = config.get("place_estimates")
    stored = stored if isinstance(stored, dict) else {}
    older = config.get("gps_check")
    older = older if isinstance(older, dict) else {}
    result = dict(DEFAULTS)
    if isinstance(stored.get("enabled"), bool):
        result["enabled"] = stored["enabled"]
    elif isinstance(older.get("estimate_places"), bool):
        result["enabled"] = older["estimate_places"]
    if isinstance(stored.get("library_area"), bool):
        result["library_area"] = stored["library_area"]
    low, high = RANGES["window_minutes"]
    for value in (stored.get("window_minutes"), older.get("estimate_minutes")):
        number = _number(value, low, high, True)
        if number is not None:
            result["window_minutes"] = number
            break
    low, high = RANGES["max_spread_km"]
    number = _number(stored.get("max_spread_km"), low, high, False)
    if number is not None:
        result["max_spread_km"] = number
    return result


# ---------------------------------------------------------------------------
# Words

def _distance_words(km):
    if km < 1:
        return "%d m" % max(10, int(round(km * 1000, -1)))
    return "{:,} km".format(int(round(km))) if km >= 10 else "%.1f km" % km


def _time_words(seconds):
    when = "later" if seconds > 0 else "earlier"
    if abs(seconds) < 1:
        return "at the same time"
    return "%s %s" % (gps_check.duration_words(seconds), when)


def reason_text(source, detail=None, area_name=""):
    """Where an estimate comes from, in words (no full stop): "estimated from ..." or
    "set by you"."""
    detail = detail if isinstance(detail, dict) else {}
    seconds = [value for value in detail.get("seconds") or []
               if isinstance(value, (int, float)) and math.isfinite(value)]
    km = detail.get("km")
    km = km if isinstance(km, (int, float)) and math.isfinite(km) else None
    if source == "owner":
        return "set by you"
    if source == "copy":
        kind = detail.get("kind")
        if kind == "second":
            # (The same camera model in the same second: often the same
            # camera, but it may be another of that model there then.)
            return ("estimated from a picture with a position taken in the same second by a "
                    "camera of the same model")
        what = {"file": "the same file", "dhash": "a copy",
                "look": "a picture that looks the same"}.get(kind, "a copy")
        if kind in ("dhash", "look") and seconds:
            what += " taken %s" % _time_words(seconds[0])
        return "estimated from a copy of this picture with a position (%s)" % what
    if source == "time":
        text = "estimated from pictures taken around the same time"
        # (A track's points are no pictures: "points of the track" then.)
        track = detail.get("track") == "all"
        if track:
            text = "estimated from the GPS track of a video recorded around the same time"
        elif detail.get("track"):
            text = ("estimated from pictures taken around the same time (and a video's GPS "
                    "track)")
        if len(seconds) >= 2 and seconds[0] < 0 < seconds[-1]:
            text += ": one %s %s, one %s" % ("point of the track" if track else "taken",
                                            _time_words(seconds[0]), _time_words(seconds[-1]))
        elif seconds:
            nearest = min(seconds, key=abs)
            text += ": %d %s %s, the nearest %s" % (
                len(seconds), ("point of the track" if len(seconds) == 1 else
                               "points of the track") if track else "taken",
                "before it" if nearest <= 0 else "after it", _time_words(nearest))
        if km is not None and len(seconds) >= 2:
            text += ", %s apart" % _distance_words(km)
        if detail.get("coarse"):
            text += " (it may have been taken some way off, so there is no position)"
        return text
    if source == "area":
        return ("estimated from the library: nearly all of its pictures with a position "
                "were taken in %s" % (area_name or "one region"))
    return ""


# ---------------------------------------------------------------------------
# The estimates (pure)

def _file_times(rows):
    """The ids of the pictures dated by their file's time whose folder vouches for it
    (see the module notes)."""
    folders = collections.defaultdict(list)
    for row in rows:
        written = _written(row)
        if written is not None and gps_check.timed_row(row):
            length = row.get("duration")
            length = (float(length) if isinstance(length, (int, float))
                      and not isinstance(length, bool) and math.isfinite(length)
                      and length > 0 else 0.0)
            # (How much later the file was written than the picture was taken.)
            late = written - gps_check.local_seconds(row.get("taken_at"))
            folders[(row.get("library_id"), _folder(row))].append(
                (written, -FILE_TIME_SLACK <= late <= length + FILE_TIME_SLACK))
    for entries in folders.values():
        entries.sort()
    trusted = set()
    for row in rows:
        if row.get("taken_source") != "mtime":
            continue
        written = _written(row)
        entries = folders.get((row.get("library_id"), _folder(row)))
        if written is None or not entries:
            continue
        start = bisect.bisect_left(entries, (written - FILE_TIME_REACH,))
        near = []
        for index in range(start, len(entries)):
            if entries[index][0] > written + FILE_TIME_REACH:
                break
            near.append(entries[index][1])
        if len(near) >= FILE_TIME_PEERS and all(near):
            trusted.add(row["id"])
    return trusted


def _folder(row):
    return (row.get("path") or "").rpartition("/")[0]


def _written(row):
    """The file's time as a local clock reads it (seconds, as gps_check.local_seconds), or
    None. For a picture dated by its file that is its taken_at."""
    mtime = row.get("mtime")
    if isinstance(mtime, bool) or not isinstance(mtime, (int, float)) or not math.isfinite(mtime):
        return None
    try:
        local = time.localtime(mtime)
    except (OverflowError, OSError, ValueError):
        return None
    return gps_check.local_seconds("%04d-%02d-%02dT%02d:%02d:%02d" % local[:6])


def _median(values):
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0


def _spread(points):
    """The largest distance (km) between two of points [(lat, lon)]."""
    best = 0.0
    for first in range(len(points)):
        for second in range(first + 1, len(points)):
            best = max(best, places.distance_km(points[first][0], points[first][1],
                                                points[second][0], points[second][1]))
    return best


def _speed(references):
    """The fastest the camera went between two of references (km/h), a GPS position's
    NOISE_KM aside."""
    fastest = 0.0
    for first in range(len(references)):
        for second in range(first + 1, len(references)):
            one, other = references[first], references[second]
            km = places.distance_km(one.lat, one.lon, other.lat, other.lon) - NOISE_KM
            if km > 0:
                hours = max(1.0, abs(one.mid - other.mid)) / 3600.0
                fastest = max(fastest, km / hours)
    return fastest


def _accuracy(reference):
    """How far off a reference's position may be by its own account (km, at least
    RADIUS_MIN_KM)."""
    return max(RADIUS_MIN_KM, reference.tolerance - gps_check.BASE_TOLERANCE_KM)


def _paces(references):
    """{(id, group): km/h} of references: how fast each one's camera went then -- the
    speed its GPS states, or between it and the same camera's nearest shots before and
    after it (within COPY_LOOK_SECONDS, other shots than its copies), a GPS position's
    NOISE_KM aside; 0 when nothing says. A camera without a name has no shots of its own
    to go by."""
    by_camera = collections.defaultdict(list)
    for reference in references:
        by_camera[(reference.camera, reference.clock_only)].append(reference)
    paces = {}
    for (camera, _clock_only), members in by_camera.items():
        members.sort(key=lambda reference: (reference.mid, str(reference.group)))
        for index, reference in enumerate(members):
            pace = reference.speed if isinstance(reference.speed, (int, float)) else 0.0
            for step in ((-1, 1) if camera else ()):
                other = index + step
                while 0 <= other < len(members) and members[other].group == reference.group:
                    other += step
                if not 0 <= other < len(members):
                    continue
                neighbour = members[other]
                seconds = abs(neighbour.mid - reference.mid)
                if seconds > COPY_LOOK_SECONDS:
                    continue
                km = places.distance_km(reference.lat, reference.lon, neighbour.lat,
                                        neighbour.lon) - NOISE_KM
                if km > 0:
                    pace = max(pace, km / (max(1.0, seconds) / 3600.0))
            paces[(reference.id, reference.group)] = pace
    return paces


class _Times:
    """The references in time order, for one clock kind (UTC, or clocks as they are)."""

    def __init__(self, references):
        self.references = sorted(references, key=lambda reference: reference.mid)
        self.mids = [reference.mid for reference in self.references]

    def near(self, mid, reach):
        start = bisect.bisect_left(self.mids, mid - reach)
        for index in range(start, len(self.mids)):
            if self.mids[index] > mid + reach:
                break
            yield self.references[index]

    def nearest(self, mid, wanted):
        """The reference nearest mid in time for which wanted(reference) is true, or None."""
        after = bisect.bisect_left(self.mids, mid)
        before = after - 1
        while before >= 0 or after < len(self.mids):
            if after >= len(self.mids) or (
                    before >= 0 and mid - self.mids[before] <= self.mids[after] - mid):
                index, before = before, before - 1
            else:
                index, after = after, after + 1
            if wanted(self.references[index]):
                return self.references[index]
        return None


def _copy_estimate(row, timing, group, facts, looks, options):
    """The copy rule (see the module notes): an Estimate, or None."""
    by_hash, by_second, by_dhash, times, paces = facts
    limit = options["max_spread_km"]
    found = []

    def moved(gap, kind, reference):
        # (Another shot: where the camera went in the time between them.)
        if _accuracy(reference) > limit:
            return
        pace = max(WALK_KMH, paces.get((reference.id, reference.group), 0.0))
        radius = _accuracy(reference) + pace * abs(gap) / 3600.0
        if radius <= limit:
            found.append((gap, kind, reference, radius))

    digest = row.get("content_hash")
    for reference in by_hash.get(digest, ()) if digest else ():
        found.append((0.0, "file", reference, _accuracy(reference)))
    camera = gps_check.camera_key(row.get("camera"))
    if not found and timing is not None and camera and gps_check.timed_row(row):
        for reference in by_second.get((camera, (row.get("taken_at") or "")[:19]), ()):
            found.append((0.0, "second", reference, _accuracy(reference)))
    if not found and timing is not None:
        dhash = row.get("dhash") or ""
        for reference in by_dhash.get(dhash, ()) if dhash.strip("0") else ():
            gap = reference.mid - timing[2]
            if reference.clock_only == timing[3] and abs(gap) <= gps_check.COPY_SECONDS:
                moved(gap, "dhash", reference)
    if not found and timing is not None and looks:
        own = looks.get(row["id"])
        if own is not None:
            import search_index
            for reference in times[timing[3]].near(timing[2], COPY_LOOK_SECONDS):
                other = looks.get(reference.id)
                if (other is not None and reference.group != group
                        and search_index._look_cosine(own, other)
                        >= search_index.SAME_PICTURE_COSINE):
                    moved(reference.mid - timing[2], "look", reference)
    found = [entry for entry in found if entry[2].group != group or group is None]
    if not found:
        return None
    gap, kind, reference, radius = min(found, key=lambda entry: (abs(entry[0]), entry[2].id))
    return Estimate("copy", reference.lat, reference.lon,
                    round(max(RADIUS_MIN_KM, radius), 3),
                    _with_track({"ids": [reference.id], "seconds": [round(gap)], "kind": kind},
                                [reference]), None)


def _with_track(detail, used):
    """detail with "track": "all" (or "some") when the references used (all or some of
    them) are videos' GPS tracks (gps_check.Reference.track), and each id once."""
    detail["ids"] = list(dict.fromkeys(detail["ids"]))
    from_tracks = sum(1 for reference in used if reference.track)
    if from_tracks:
        detail["track"] = "all" if from_tracks == len(used) else "some"
    return detail


def _names_of(columns, town):
    """(region, country, cc, town) of a picture's place columns (region "" when it has
    none; town "" unless town is true), or None without a country."""
    columns = columns or {}
    if not columns.get("place_country"):
        return None
    return (columns.get("place_region") or "", columns["place_country"],
            columns.get("place_cc") or "", (columns.get("place_city") or "") if town else "")


def _time_estimate(row, timing, group, times, options, across, paces, names):
    """The time rule (see the module notes): an Estimate, or None. names(reference):
    the place columns of a reference's picture (for an estimate without a position)."""
    lo, hi, mid, clock_only = timing
    reach = options["window_minutes"] * 60.0
    spread_limit = options["max_spread_km"]
    before, after = [], []
    seen = set()
    for reference in times[clock_only].near(mid, reach):
        if (reference.id == row["id"] or reference.group == group
                or (not across and reference.library != row.get("library_id"))):
            continue
        # (The longest the time between them may be, with every offset.)
        if max(reference.hi - lo, hi - reference.lo) > reach:
            continue
        # (A position that may be further off than the pictures may be apart
        # -- a phone's coarse fix on an aeroplane -- places nothing.)
        if _accuracy(reference) > spread_limit:
            continue
        (before if reference.mid <= mid else after).append(reference)
    before.sort(key=lambda reference: (-reference.mid, reference.id))
    after.sort(key=lambda reference: (reference.mid, reference.id))

    def shots(side):
        # (Copies of one shot count once: the nearest of them.)
        result = []
        for reference in side:
            if reference.group not in seen:
                seen.add(reference.group)
                result.append(reference)
        return result

    before, after = shots(before), shots(after)

    def gap_of(reference):
        return max(reference.hi - lo, hi - reference.lo)

    if before and after:
        first, last = before[0], after[0]
        apart = places.distance_km(first.lat, first.lon, last.lat, last.lon)
        if apart > spread_limit:
            return None
        span = last.mid - first.mid
        share = (mid - first.mid) / span if span > 0 else 0.5
        lat = first.lat + (last.lat - first.lat) * share
        lon = first.lon + (last.lon - first.lon) * share
        nearest = min(gap_of(first), gap_of(last))
        radius = (max(RADIUS_MIN_KM, apart, _accuracy(first), _accuracy(last))
                  + WALK_KMH * nearest / 3600.0)
        return Estimate("time", lat, lon, round(radius, 3),
                        _with_track({"ids": [first.id, last.id],
                                     "seconds": [round(first.mid - mid), round(last.mid - mid)],
                                     "km": round(apart, 3)}, [first, last]), None)
    side = before or after
    if len(side) < MIN_SIDE_SHOTS or gap_of(side[0]) > reach * ONE_SIDE_SHARE:
        return None
    used = side[:MAX_SIDE_SHOTS]
    apart = _spread([(reference.lat, reference.lon) for reference in used])
    if apart > spread_limit:
        return None
    nearest = used[0]
    # (Shots taken seconds apart on a flight are near each other too: how fast
    # they went -- or their GPS says they went -- tells how far the camera may
    # have gone since; after a couple of minutes it may have boarded a car.)
    pace = max([WALK_KMH, _speed(used)]
               + [paces.get((reference.id, reference.group), 0.0) for reference in used])
    gap = gap_of(nearest)
    moved = (pace * min(gap, VEHICLE_SECONDS)
             + max(pace, VEHICLE_KMH) * max(0.0, gap - VEHICLE_SECONDS)) / 3600.0
    radius = max([RADIUS_MIN_KM, apart] + [_accuracy(reference) for reference in used]) + moved
    detail = _with_track({"ids": [reference.id for reference in used],
                          "seconds": sorted(round(reference.mid - mid) for reference in used),
                          "km": round(apart, 3)}, used)
    if radius <= spread_limit:
        return Estimate("time", nearest.lat, nearest.lon, round(radius, 3), detail, None)
    found = _names_of(names(nearest), radius <= TOWN_KM)
    if radius > COARSE_KM or found is None:
        return None
    # (Too rough for a position: the nearest picture's town or region.)
    detail["coarse"] = 1
    return Estimate("time", None, None, round(radius, 3), detail, found)


Area = collections.namedtuple("Area", "region country cc lat lon share earliest latest "
                                      "clock_earliest clock_latest")
Area.__doc__ = """A library's area (library_areas).

region, country, cc          its names (region "" when only the country is shared)
lat, lon                     the middle of its located pictures
share                        its located pictures' share of the library's pictures
earliest, latest             the UTC times (gps_check) of its located pictures, or None
clock_earliest, clock_latest those of its located pictures timed by their clocks as they
                             are (no time zone known), or None
"""


def library_areas(rows, references):
    """{library id: Area} of the libraries whose located pictures are in one area (see the
    module notes). Needs the pictures' place names (place_country of rows)."""
    by_id = {row["id"]: row for row in rows}
    pictures = collections.Counter(row.get("library_id") for row in rows)
    located = collections.defaultdict(dict)
    for reference in references:
        # (A picture once: a video's track points are not pictures of their own.)
        located[reference.library].setdefault(reference.id, reference)
    areas = {}
    for library_id, members in located.items():
        members = list(members.values())
        if len(members) < AREA_MIN:
            continue
        middle = (_median([member.lat for member in members]),
                  _median([member.lon for member in members]))
        inside = [member for member in members
                  if places.distance_km(middle[0], middle[1], member.lat, member.lon) <= AREA_KM]
        if len(inside) < AREA_SHARE * len(members):
            continue
        names = [by_id.get(member.id) or {} for member in inside]
        if not all(name.get("place_country") for name in names):
            continue            # (place names missing: no area until they are there)
        regions = collections.Counter((name.get("place_region") or "", name["place_country"],
                                       name.get("place_cc") or "") for name in names)
        countries = collections.Counter((name["place_country"], name.get("place_cc") or "")
                                        for name in names)
        (region, country, cc), count = regions.most_common(1)[0]
        if not (region and count >= AREA_SHARE * len(inside)):
            (country, cc), count = countries.most_common(1)[0]
            if count < AREA_SHARE * len(inside):
                continue
            region = ""
        utc = [member.mid for member in members if not member.clock_only]
        clocks = [member.mid for member in members if member.clock_only]
        areas[library_id] = Area(region, country, cc, middle[0], middle[1],
                                 len(members) / float(max(1, pictures[library_id])),
                                 min(utc) if utc else None, max(utc) if utc else None,
                                 min(clocks) if clocks else None,
                                 max(clocks) if clocks else None)
    return areas


def area_name(area):
    """"Tuscany, Italy" of an area (region, country, ...)."""
    return ", ".join(part for part in area[:2] if part)


def _area_fits(row, timing, area, times, across):
    """True when a picture that no other rule placed gets its library's area (see the
    module notes): undated, or taken near the times of the located pictures."""
    reach = AREA_HOURS * 3600.0
    saved = timing is not None and gps_check.export_date(row)
    if timing is None or saved or timing[3]:
        # (Few located pictures in a large library say little about the rest.)
        if area.share < AREA_LIBRARY_SHARE:
            return False
    if timing is None:
        return True
    lo, hi, mid, clock_only = timing
    if saved:
        lo = -math.inf          # (saved then: taken then, or at any time before)
    east, west = gps_check.OFFSET_RANGE[1], -gps_check.OFFSET_RANGE[0]
    spans = []
    # (A clock without a time zone may be up to 14 hours ahead of UTC, or 12
    # hours behind it.)
    if area.earliest is not None:
        spans.append((area.earliest, area.latest, east if clock_only else 0,
                      west if clock_only else 0))
    if area.clock_earliest is not None:
        spans.append((area.clock_earliest, area.clock_latest, 0 if clock_only else west,
                      0 if clock_only else east))
    if not any(hi + ahead >= first - reach and lo - behind <= last + reach
               for first, last, behind, ahead in spans):
        return False            # (taken on another day: another trip, or the way there)
    if clock_only or saved:
        return True
    # A located picture taken nearer in time somewhere else: the camera was
    # not in the area yet (or any more).
    nearest = times[False].nearest(mid, lambda reference: (
        across or reference.library == row.get("library_id")))
    return nearest is None or places.distance_km(
        area.lat, area.lon, nearest.lat, nearest.lon) <= AREA_KM


def estimate(rows, check, options, across=True, looks=None, choices=None, owner_place=None):
    """The place estimates of the pictures without a usable position: {image id: Estimate}.

    rows: the GPS check's rows (gps_check.evaluate takes them; also path,
    mtime and duration for the file-time rule, and the place columns for
    the areas), check its Check; options settings(); across: other
    libraries' pictures count (gps_check's across_libraries); looks:
    {image id: look (search_index._look)} for the copy rule's looks
    (optional); choices: {content key: place_choices row as a dict}
    (optional); owner_place(choice) -> Estimate or None turns a "set" choice
    into the owner's estimate (default: its position, or its region). Pure:
    no database, no clock.
    """
    choices = choices or {}
    references = check.references()
    by_clock = collections.defaultdict(list)
    for reference in references:
        by_clock[reference.clock_only].append(reference)
    times = {False: _Times(by_clock[False]), True: _Times(by_clock[True])}
    by_id = {row["id"]: row for row in rows}
    by_hash = collections.defaultdict(list)
    by_second = collections.defaultdict(list)
    by_dhash = collections.defaultdict(list)
    for reference in references:
        row = by_id.get(reference.id) or {}
        if row.get("content_hash"):
            by_hash[row["content_hash"]].append(reference)
        if reference.camera and gps_check.timed_row(row):
            by_second[(reference.camera, (row.get("taken_at") or "")[:19])].append(reference)
        if (row.get("dhash") or "").strip("0"):
            by_dhash[row["dhash"]].append(reference)
    paces = _paces(references)
    facts = (by_hash, by_second, by_dhash, times, paces)

    def names(reference):
        return by_id.get(reference.id)

    trusted_files = _file_times(rows)
    result = {}
    left = []
    for row in targets(rows, check):
        choice = choices.get(gps_check.content_key(row.get("content_hash"), row["id"]))
        if choice is not None:
            if choice.get("choice") == "set":
                found = (owner_place or _owner_estimate)(choice)
                if found is not None:
                    result[row["id"]] = found
            continue            # (rejected: no estimate)
        if not options["enabled"]:
            continue
        timing = check.timing(row, trusted=row["id"] in trusted_files)
        group = check.group_of(row["id"])
        found = _copy_estimate(row, timing, group, facts, looks, options)
        if found is None and timing is not None:
            found = _time_estimate(row, timing, group, times, options, across, paces, names)
        if found is not None:
            result[row["id"]] = found
        else:
            left.append((row, timing))
    if options["enabled"] and options["library_area"] and left:
        areas = library_areas(rows, references)
        for row, timing in left:
            area = areas.get(row.get("library_id"))
            if area is not None and _area_fits(row, timing, area, times, across):
                result[row["id"]] = Estimate("area", None, None, None, {}, area[:3])
    return result


def targets(rows, check):
    """The rows of the pictures without a usable position: none, or one set aside."""
    found = []
    for row in rows:
        verdict = check.verdicts.get(row["id"])
        if verdict is not None and not verdict.ignored:
            continue            # (a position of its own, kept)
        if verdict is None and world_map.valid_position(row.get("lat"), row.get("lon")):
            continue
        found.append(row)
    return found


def _choice_detail(choice):
    """The detail an owner's place keeps of the estimate it was accepted from: {"track"}
    when that came from a video's GPS track (an export treats it as the track's), else
    {}."""
    detail = choice.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail or "{}")
        except ValueError:
            detail = {}
    detail = detail if isinstance(detail, dict) else {}
    return {"track": detail["track"]} if detail.get("track") in ("all", "some") else {}


def _owner_estimate(choice):
    """The owner's place of a "set" choice (a place_choices row as a dict), or None."""
    lat, lon = choice.get("lat"), choice.get("lon")
    if world_map.valid_position(lat, lon):
        return Estimate("owner", float(lat), float(lon), None, _choice_detail(choice), None)
    if choice.get("place_country"):
        names = (choice.get("place_region") or "", choice["place_country"],
                 choice.get("place_cc") or "")
        if choice.get("place_city"):
            names += (choice["place_city"],)
        return Estimate("owner", None, None, None, _choice_detail(choice), names)
    return None


# ---------------------------------------------------------------------------
# The database

def ensure_tables(conn):
    """Create the tables of the estimates (TABLES) when they are not there yet (and add
    the columns an earlier version's lack, _ADDED_COLUMNS).

    Writes nothing when they are; commits what conn had pending first
    otherwise.
    """
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = []
    for table, column, definition in _ADDED_COLUMNS:
        if table in present and column not in {
                row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}:
            missing.append("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))
    if all(name in present for name in TABLES) and not missing:
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _TABLES:
            conn.execute(statement)
        for statement in missing:
            conn.execute(statement)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _have_tables(conn):
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN "
                        "('place_estimates', 'place_choices')").fetchone()[0] == len(TABLES)


def choices(conn):
    """{content key: the place_choices row as a dict} ({} before there are any)."""
    if not _have_tables(conn):
        return {}
    return {row["content_key"]: dict(row) for row in conn.execute(
        "SELECT content_key, choice, lat, lon, place_city, place_region, place_country, "
        "place_cc, detail FROM place_choices")}


def _looks_for(conn, rows, check, options):
    """{image id: look} of the pictures the copy rule may compare (see estimate), or None.

    Only the pictures without a position that a reference was taken near
    (COPY_LOOK_SECONDS), and those references: a few look vectors, not all.
    """
    if not options["enabled"]:
        return None
    import looks as looks_module
    import search_index
    references = sorted(check.references(), key=lambda reference: reference.mid)
    mids = [reference.mid for reference in references]
    wanted = set()
    for row in targets(rows, check):
        if not row.get("content_hash"):
            continue
        timing = check.timing(row)
        if timing is None:
            continue
        start = bisect.bisect_left(mids, timing[2] - COPY_LOOK_SECONDS)
        stop = bisect.bisect_right(mids, timing[2] + COPY_LOOK_SECONDS, start)
        if stop > start:
            wanted.add(row["id"])
            wanted.update(reference.id for reference in references[start:stop])
    if not wanted:
        return None
    keys = {row["id"]: (row["content_hash"], looks_module.kind_of(row)) for row in rows
            if row["id"] in wanted and row.get("content_hash")}
    try:
        store = looks_module.connect(create=False)
    except sqlite3.Error:
        return None
    if store is None:
        return None
    try:
        found = looks_module.vectors_for(store, keys.values())
    except sqlite3.Error:
        return None
    finally:
        store.close()
    result = {}
    for image_id, key in keys.items():
        vector = search_index._look(found.get(key))
        if vector is not None:
            result[image_id] = vector
    return result


def _place_of(found, index, memo):
    """The place columns of an Estimate (index: a PlaceIndex, or None)."""
    if found.area is not None:
        region, country, cc = found.area[:3]
        return {"place_city": found.area[3] if len(found.area) > 3 else "",
                "place_region": region, "place_country": country, "place_cc": cc,
                "place_km": None}
    key = (found.lat, found.lon)
    if key not in memo:
        memo[key] = (places.place_columns(found.lat, found.lon, index) if index
                     else places.empty_columns())
    return memo[key]


def _detail_text(found):
    return json.dumps(found.detail or {}, sort_keys=True, separators=(",", ":"))


def update(conn, config, rows, check, ctx=None, now=None, changed=None):
    """Work out the estimates again from a GPS check's rows and Check, and keep them.

    Called by gps_check.run after it committed. Writes the place_estimates
    rows that changed (and touches those pictures' images.updated) in one
    transaction; changed (a set, optional) gets their ids. Returns
    {"estimated", "by_source": {source: count}, "changed", "seconds"}.
    """
    started = time.perf_counter()
    options = settings(config)
    ensure_tables(conn)
    now = time.time() if now is None else now
    looks = None
    try:
        looks = _looks_for(conn, rows, check, options)
    except Exception as error:      # (looks are a help, never a reason to fail)
        if ctx is not None:
            ctx.log("Place estimates go without looks (%s)." % error, "warn")
    across = gps_check.settings(config)["across_libraries"]
    found = estimate(rows, check, options, across=across, looks=looks,
                     choices=choices(conn))
    index = None
    if any(item.lat is not None for item in found.values()):
        index = places.shared()
    memo = {}
    ids = {row["id"] for row in rows}
    summary = {"estimated": 0, "by_source": {}, "changed": 0, "seconds": 0.0}
    counts = collections.Counter()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        stored = {row["image_id"]: dict(row) for row in conn.execute(
            "SELECT * FROM place_estimates")}
        touched = []
        for image_id, item in found.items():
            counts[item.source] += 1
            values = {"source": item.source, "lat": item.lat, "lon": item.lon,
                      "radius_km": item.radius_km, "detail": _detail_text(item)}
            values.update(_place_of(item, index, memo))
            old = stored.get(image_id)
            if old is not None and all(old.get(key) == value for key, value in values.items()):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO place_estimates (image_id, source, lat, lon, radius_km, "
                "detail, place_city, place_region, place_country, place_cc, place_km, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (image_id, values["source"], values["lat"], values["lon"], values["radius_km"],
                 values["detail"], values["place_city"], values["place_region"],
                 values["place_country"], values["place_cc"], values["place_km"], now))
            touched.append(image_id)
        for image_id in stored:
            # (Only the pictures looked at: other libraries' rows stay.)
            if image_id in ids and image_id not in found:
                conn.execute("DELETE FROM place_estimates WHERE image_id = ?", (image_id,))
                touched.append(image_id)
        for start in range(0, len(touched), 500):
            part = touched[start:start + 500]
            conn.execute("UPDATE images SET updated = ? WHERE id IN (%s)"
                         % ",".join("?" * len(part)), [now] + part)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if changed is not None:
        changed.update(touched)
    summary["estimated"] = sum(count for source, count in counts.items() if source != "owner")
    summary["by_source"] = dict(counts)
    summary["changed"] = len(touched)
    summary["seconds"] = round(time.perf_counter() - started, 3)
    if ctx is not None and (summary["estimated"] or touched):
        ctx.log(summary_text(summary))
    return summary


def summary_text(summary):
    """The log line of an update (see update)."""
    by_source = summary["by_source"]
    parts = ["{:,} {}".format(by_source[source], words) for source, words in (
        ("copy", "from a copy"), ("time", "from pictures taken around the same time"),
        ("area", "from the library's area")) if by_source.get(source)]
    text = "Estimated places: {:,} picture{} without a position".format(
        summary["estimated"], "" if summary["estimated"] == 1 else "s")
    if parts:
        text += " (" + ", ".join(parts) + ")"
    if by_source.get("owner"):
        text += "; {:,} with a place you set".format(by_source["owner"])
    if summary["changed"]:
        text += "; {:,} changed".format(summary["changed"])
    return text + ". The admin finds them with is:place-estimated."


def for_libraries(conn, library_ids):
    """{image id: the place_estimates row (a dict)} of the present pictures of some libraries."""
    library_ids = list(library_ids)
    if not library_ids or not _have_tables(conn):
        return {}
    return {row["image_id"]: dict(row) for row in conn.execute(
        "SELECT e.* FROM place_estimates AS e JOIN images AS i ON i.id = e.image_id "
        "WHERE i.missing_since IS NULL AND i.library_id IN (%s)"
        % ",".join("?" * len(library_ids)), library_ids)}


def for_picture(conn, image_id):
    """The place_estimates row (a dict) of a picture, or None."""
    if not _have_tables(conn):
        return None
    row = conn.execute("SELECT * FROM place_estimates WHERE image_id = ?",
                       (image_id,)).fetchone()
    return dict(row) if row is not None else None


def usable(row, estimate_row):
    """True when an estimate (a place_estimates row) applies to an images row: the
    picture has no place of its own (no position, no place names)."""
    return (estimate_row is not None and not row["place_country"]
            and not world_map.valid_position(row["lat"], row["lon"])
            and bool(estimate_row.get("place_country") or estimate_row.get("lat") is not None))


def located(conn, library_ids):
    """[(id, lat, lon, radius_km, owner)] of the estimates with a position of the present,
    shown pictures of some libraries (the map's hollow pins; owner: the owner's place)."""
    library_ids = list(library_ids)
    if not library_ids or not _have_tables(conn):
        return []
    found = []
    for row in conn.execute(
            "SELECT e.image_id, e.lat, e.lon, e.radius_km, e.source FROM place_estimates AS e "
            "JOIN images AS i ON i.id = e.image_id WHERE i.missing_since IS NULL "
            "AND i.hidden = 0 AND i.lat IS NULL AND e.lat IS NOT NULL AND i.library_id IN (%s) "
            "ORDER BY e.image_id" % ",".join("?" * len(library_ids)), library_ids):
        if world_map.valid_position(row[1], row[2]):
            found.append((row[0], float(row[1]), float(row[2]), row[3], row[4] == "owner"))
    return found


def view(conn, row, estimate_row=None, admin=False):
    """What /api/image says about a picture's estimated place (an images row), or None.

    {"source", "reason" (words), "radius_km" (or None), "place" ("Town,
    Region, Country"), "track" for an estimate from pictures taken around the
    same time that are videos' GPS tracks ("all") or include one ("some"),
    and for the admin "lat", "lon", "ids" (the pictures it came from),
    "choice" (the owner's: "set", "reject" or "")}. None for a picture with
    a place of its own and no choice.
    """
    if estimate_row is None:
        estimate_row = for_picture(conn, row["id"])
    choice = ""
    if admin and _have_tables(conn):
        found = conn.execute("SELECT choice FROM place_choices WHERE content_key = ?",
                             (gps_check.content_key(row["content_hash"], row["id"]),)).fetchone()
        choice = found[0] if found is not None else ""
    if not usable(row, estimate_row):
        if admin and choice == "reject" and not row["place_country"]:
            return {"source": "", "reason": "you rejected the estimate", "radius_km": None,
                    "place": "", "choice": choice, "lat": None, "lon": None, "ids": []}
        return None
    try:
        detail = json.loads(estimate_row.get("detail") or "{}")
    except ValueError:
        detail = {}
    detail = detail if isinstance(detail, dict) else {}
    area = (area_name((estimate_row["place_region"], estimate_row["place_country"]))
            if estimate_row["source"] == "area" else "")
    radius = estimate_row.get("radius_km")
    result = {"source": estimate_row["source"],
              "reason": reason_text(estimate_row["source"], detail, area),
              "radius_km": radius if isinstance(radius, (int, float)) else None,
              "place": gps_check.place_text(estimate_row)}
    if estimate_row["source"] == "time" and detail.get("track") in ("all", "some"):
        result["track"] = detail["track"]
    if admin:
        ids = detail.get("ids") if isinstance(detail.get("ids"), list) else []
        result.update(lat=estimate_row.get("lat"), lon=estimate_row.get("lon"),
                      ids=[value for value in ids if isinstance(value, int)], choice=choice)
    return result


def place_record(estimate_row):
    """The search record keys (ci, rg, co, cc, pe, ps) of an estimate (a place_estimates row)."""
    record = {}
    if not estimate_row or not estimate_row.get("place_country"):
        return record
    for key, column in (("ci", "place_city"), ("rg", "place_region"), ("co", "place_country"),
                        ("cc", "place_cc")):
        value = estimate_row.get(column)
        if isinstance(value, str) and value.strip():
            record[key] = value.strip()
    source = estimate_row.get("source")
    if source != "owner":
        record["pe"] = 1
    if source in RECORD_SOURCES:
        record["ps"] = RECORD_SOURCES[source]
    return record


def status(conn):
    """{"estimated", "copy", "time", "area", "owner", "rejected"}: how many present pictures
    have an estimated place (by source) or the owner's, for the Manage page."""
    result = {"estimated": 0, "copy": 0, "time": 0, "area": 0, "owner": 0, "rejected": 0}
    if not _have_tables(conn):
        return result
    for source, count in conn.execute(
            "SELECT e.source, COUNT(*) FROM place_estimates AS e JOIN images AS i "
            "ON i.id = e.image_id WHERE i.missing_since IS NULL AND i.lat IS NULL "
            "GROUP BY e.source"):
        if source in result:
            result[source] = count
    result["estimated"] = result["copy"] + result["time"] + result["area"]
    result["rejected"] = conn.execute("SELECT COUNT(*) FROM place_choices "
                                      "WHERE choice = 'reject'").fetchone()[0]
    return result


_POSITION_RE = re.compile(r"^\s*([-+]?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")


def parse_position(text):
    """(lat, lon) of a position typed as "43.84, 10.50" (decimal degrees), or None."""
    match = _POSITION_RE.match(str(text or ""))
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if world_map.valid_position(lat, lon) else None


class ChoiceError(ValueError):
    """A choice that cannot be made: an HTTP status and a message."""

    def __init__(self, status_code, message):
        ValueError.__init__(self, message)
        self.status = status_code


def set_choice(conn, image_id, choice, lat=None, lon=None, now=None):
    """The owner's choice about a picture's place (kept by content, see the module notes).

    choice: "accept" (the picture's estimate becomes the owner's place: its
    position, or an area's region), "set" (the owner's position lat, lon),
    "reject" (no estimate) or "clear" (the estimates decide again). Does
    not work out the estimates again: gps_check.run does. Raises
    ChoiceError when the picture is not there, has a position of its own,
    or there is no estimate to accept.
    """
    ensure_tables(conn)
    row = conn.execute("SELECT id, content_hash, lat, lon FROM images WHERE id = ? AND "
                       "missing_since IS NULL", (image_id,)).fetchone()
    if row is None:
        raise ChoiceError(404, "There is no picture %d." % image_id)
    if choice != "clear" and world_map.valid_position(row["lat"], row["lon"]):
        raise ChoiceError(409, "This picture has a GPS position of its own; ignore it first "
                               "(Ignore location) to give it another place.")
    key = gps_check.content_key(row["content_hash"], image_id)
    stamp = time.time() if now is None else now
    if choice == "clear":
        conn.execute("DELETE FROM place_choices WHERE content_key = ?", (key,))
        return
    if choice == "reject":
        conn.execute("INSERT OR REPLACE INTO place_choices (content_key, choice, updated) "
                     "VALUES (?, 'reject', ?)", (key, stamp))
        return
    names = ("", "", "", "")
    detail = {}
    if choice == "accept":
        found = for_picture(conn, image_id)
        if found is None or found["source"] == "owner":
            raise ChoiceError(409, "This picture has no estimated place to accept.")
        lat, lon = found["lat"], found["lon"]
        if lat is None:
            # (An area, or a time estimate too rough for a position: its names.)
            names = (found["place_region"], found["place_country"], found["place_cc"],
                     found["place_city"])
        # (A place from a video's GPS track stays one: an export holds it as
        # it holds the track's, see export._Routes.)
        detail = _choice_detail({"detail": found.get("detail")})
    elif choice == "set":
        if not world_map.valid_position(lat, lon):
            raise ChoiceError(400, "Send a position: lat from -90 to 90 and lon from -180 "
                                   "to 180 (not 0, 0).")
    else:
        raise ChoiceError(400, "Unknown choice: %s." % choice)
    conn.execute("INSERT OR REPLACE INTO place_choices (content_key, choice, lat, lon, "
                 "place_city, place_region, place_country, place_cc, detail, updated) "
                 "VALUES (?, 'set', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key, lat, lon, names[3], names[0], names[1], names[2],
                  json.dumps(detail, sort_keys=True, separators=(",", ":")), stamp))
