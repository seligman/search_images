#!/usr/bin/env python3
"""Trips: journeys found in the index, each with a page of its own (/trips, /trip/<id>).

detect() works the trips out from what the index already knows (dates,
GPS positions, estimated places), every time they are wanted -- it reads
only the database, takes a moment, and needs no job:

  library trips  a library whose folder is named the way trips often are,
                 a date first ("2024-11-09 - Name", "2024-11 Name",
                 "20241109_Name"; the library's name leaves the date out,
                 see common.default_library_name), while "library_folders"
                 is on. Every picture of the library belongs to it. The
                 trip's days are the days its pictures were taken (a date
                 to trust: the camera's, the file name's or the video's,
                 search_index.RELIABLE_DATE_SOURCES -- not an EXIF date that
                 is only when an editor saved the file, its DateTime tag
                 without DateTimeOriginal or DateTimeDigitized), their main
                 run: days at most LIBRARY_GAP_DAYS apart join, and the run
                 with the most pictures is the trip's. Its other pictures
                 (no date to trust, or a camera whose clock was set wrong)
                 are the trip's "other" pictures.
  away trips     a run of days away from home: days with a picture whose own
                 GPS position (any library's) is more than away_km from
                 home. Two such days with at most max_gap_days between them
                 join, unless a picture of a day between was taken at home;
                 so do two at most STAY_GAP_DAYS apart (days without
                 pictures in the middle of a stay) when the last position of
                 the first and the first of the second are at most STAY_KM
                 apart. A run that has days near a library trip's pictures
                 (within JOIN_FACTOR times away_km) and shares a day with its
                 days becomes part of that trip (its dates grow to hold those
                 days); any other run is a trip of its own when it is big
                 enough (big_enough: min_pictures pictures, or half as many
                 on two days or more).

The pictures of the libraries that are not trip libraries belong to the
trip whose dates hold their own (a date to trust) when they were taken away
from home: a position (their own, or else an estimated one, see
place_estimates.py) more than away_km from home and within JOIN_FACTOR
times away_km of the trip's own positions (a picture dated wrongly far away
does not join), or no position on a day with a picture away from home or a
day between two of them -- unless its place names a town of home (one with
located pictures within away_km of home). A copy of one of a library trip's
pictures in another library (the same file, or the same camera and second)
is left out: the trip shows the trip library's.

A picture taken from a plane (its GPS altitude, see flying()) is no place
the trip went to: it is left out of the trip's places and its title.

Home is where pictures were taken on the most days: of the GPS positions of
every library's pictures, the grid cell (HOME_CELL_DEGREES) that, with its
eight neighbours, holds the most distinct days, and the median position of
the pictures in those cells -- or the "home" setting. It decides which days
are away, and is never sent to a page nor written anywhere.

The owner's changes (rename, hide, merge, split) are kept in data/trips.json
(beside index.db: they are decisions, not something worked out again), with
the options of the trip's last export (trip_export.py; the options that
differ from the defaults):

    {"format": 1,
     "trips": {"<trip id>": {"title": "...", "hidden": true,
                             "span": ["YYYY-MM-DD", "YYYY-MM-DD"]}},
     "merges": [{"ids": ["<trip id>", ...], "spans": [[start, end], ...]}],
     "splits": {"<trip id>": {"dates": ["YYYY-MM-DD", ...],
                              "span": [start, end]}},
     "exports": {"<trip id>": {"options": {...}, "span": [start, end]}}}

A trip's id stays while it is found again: a library trip's is its
library's id, an away trip's "away-YYYYMMDD" (its first day). Merged trips
take the id of their earliest one; the parts of a split trip are
"<id>.YYYYMMDD" (the day the part starts; the first part keeps the id).
Each change records the dates the trip had (span): when an away trip's
first day moves (pictures added or removed), a change whose id is no longer
found applies to the away trip whose dates overlap its span.

view() is what /api/trips sends: the trips (newest first) with their days
and the ids of their pictures by day; visitors get no hidden trip and no
hidden picture, and what they get of a trip comes from the pictures they
may see: its dates, its title (unless the owner gave it one) and whether an
away trip is big enough to be one. Hidden pictures still count when the
trips are found, so a trip's id is the same for everyone. Each day also
lists its look units (look_units: pictures of the day that look alike,
which its strip shows once). The page (static/trips.js) chooses the best
pictures of each day from the search records.

The command line, python trips.py, lists the trips (--list) and writes their
stories (--stories, see trip_stories.py).

This module uses only the standard library.
"""

import collections
import datetime
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time

import common
import imageinfo
import looks
import place_estimates
import places
import search_index
import world_map

FILE_NAME = "trips.json"
FORMAT = 1
DEFAULTS = {"enabled": True, "library_folders": True, "away_km": 50.0, "max_gap_days": 1,
            "min_pictures": 10, "best_per_day": 6, "home": None}
# (low, high, whole number) of the number settings.
RANGES = {"away_km": (5.0, 20000.0, False), "max_gap_days": (0, 7, True),
          "min_pictures": (1, 10000, True), "best_per_day": (1, 24, True)}
# Days of a trip library's pictures at most this many days apart are one run:
# a library is one trip, but a camera whose clock was wrong (or a picture of
# months before) must not stretch it over half a year.
LIBRARY_GAP_DAYS = 7
# Days away at most this many days apart (none at home between) join when the last
# position of the one and the first of the other are at most STAY_KM apart: days
# without pictures in the middle of a stay in one area, or of a road trip. (Two
# trips a few days apart a continent apart stay two.)
STAY_GAP_DAYS = 3
STAY_KM = 500.0
# A picture of another library (or a run of days away) joins a trip only within
# this many times away_km of the trip's own positions: a picture whose date is
# wrong, taken far away, is not part of it.
JOIN_FACTOR = 20
# Taken from a plane (see flying()): a GPS altitude of FLYING_METRES or more, unless
# its GPS speed says it stood still (less than FLYING_SPEED, in the camera's unit:
# km/h, mph or knots -- a mountain top); FLYING_ALWAYS_METRES or more whatever it says.
FLYING_METRES = 2500.0
FLYING_ALWAYS_METRES = 5000.0
FLYING_SPEED = 50.0
# The EXIF tags of when a camera took a picture (DateTime alone is when the file
# was last saved: an editor's).
CAMERA_DATE_TAGS = ("DateTimeOriginal", "DateTimeDigitized")
# Pictures of one day whose looks (looks.py) are at least this close are one look
# unit: the strip shows one of them (single link: a picture joins a unit when it
# is this close to any of its pictures).
LOOK_UNIT_COSINE = 0.93
# ... or at least LOOK_UNIT_NEAR_COSINE when taken at most SPREAD_SECONDS apart (a
# burst of a dark or moving scene: the moon twice, half a minute apart).
LOOK_UNIT_NEAR_COSINE = 0.90
# ... compared with the next this many of the day (in the order taken): a view taken
# again is taken soon after (and a day of hundreds of pictures takes no time).
LOOK_UNIT_WINDOW = 40
# Home: the grid of positions (degrees, about 11 km of latitude), and how many
# distinct days the cell with the most needs at least.
HOME_CELL_DEGREES = 0.1
HOME_MIN_DAYS = 3
TITLE_LENGTH = 100
# A trip id: a library id or "away-YYYYMMDD", and ".YYYYMMDD" for a part of a split trip.
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}(?:\.\d{8})?$")
_AWAY_ID_RE = re.compile(r"^away-(\d{4})(\d{2})(\d{2})$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")
# What _exif_facts looks for in the text of the exif column.
_CAMERA_DATE_RE = re.compile(r'"(?:DateTimeOriginal|DateTimeDigitized)"\s*:\s*"([^"\\]*)"')
_ALTITUDE_RE = re.compile(r'"GPSAltitude"\s*:\s*\[?\s*(-?[0-9][0-9.eE+-]*)')
# Shares of a trip's located pictures that make a place the trip's title (see
# auto_title).
TITLE_MAIN = 0.8
TITLE_TOWN = 0.6
TITLE_SECOND = 0.2
# Most places listed for a trip (the most pictured, kept in the order of the
# first visit).
MAX_PLACES = 12
# Pictures taken this close together (seconds) are spread apart among a day's
# best, and this close are the same moment (static/trips.js has the same
# numbers): an export, which publishes no times, says which of a day's
# pictures were that close (gap_classes).
SPREAD_SECONDS = 120
SAME_MOMENT_SECONDS = 20

_lock = threading.Lock()
_memo = {"stamp": None, "edits": None}


class TripError(ValueError):
    """A change the owner asked for that cannot be made: status 400, 404 or 409."""

    def __init__(self, message, status=400):
        ValueError.__init__(self, message)
        self.status = status


# ---------------------------------------------------------------------------
# Settings

def _number(value, low, high, whole):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = max(low, min(high, value))
    return int(round(value)) if whole else float(value)


def parse_home(value):
    """(lat, lon) of a "home" setting ("47.1, 8.2" or [47.1, 8.2]), or None."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        try:
            value = [float(part) for part in parts] if len(parts) == 2 else None
        except ValueError:
            value = None
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                    for item in value)
            and world_map.valid_position(float(value[0]), float(value[1]))):
        return float(value[0]), float(value[1])
    return None


def settings(config):
    """config.json "trips" checked, with defaults (see DEFAULTS; numbers held to RANGES):
    {"enabled", "library_folders", "away_km", "max_gap_days", "min_pictures",
    "best_per_day", "home" ((lat, lon) or None)}."""
    stored = config.get("trips") if isinstance(config, dict) else None
    if isinstance(stored, bool):
        stored = {"enabled": stored}
    stored = stored if isinstance(stored, dict) else {}
    result = dict(DEFAULTS)
    for key in ("enabled", "library_folders"):
        if isinstance(stored.get(key), bool):
            result[key] = stored[key]
    for key, (low, high, whole) in RANGES.items():
        number = _number(stored.get(key), low, high, whole)
        if number is not None:
            result[key] = number
    result["home"] = parse_home(stored.get("home"))
    return result


# ---------------------------------------------------------------------------
# Dates

def _date(text):
    try:
        return datetime.date(int(text[:4]), int(text[5:7]), int(text[8:10]))
    except (TypeError, ValueError):
        return None


def _day_text(date):
    return date.isoformat()


def _days_between(first, second):
    """Whole days from first to second ("YYYY-MM-DD" texts)."""
    return (_date(second) - _date(first)).days


def _add_days(text, count):
    return _day_text(_date(text) + datetime.timedelta(days=count))


def trusted_day(row, saved_only=False):
    """The day a row was taken ("YYYY-MM-DD") when its date can be trusted, else None.
    saved_only: its EXIF date is only when the file was saved (see camera_dated)."""
    taken = row["taken_at"] or ""
    if (row["taken_source"] in search_index.RELIABLE_DATE_SOURCES and _DAY_RE.match(taken[:10])
            and _date(taken) is not None and not (saved_only and row["taken_source"] == "exif")):
        return taken[:10]
    return None


def camera_dated(exif):
    """True when an EXIF dict (the exif column's) says when a camera took the picture
    (CAMERA_DATE_TAGS, read as the scan reads them); False when only DateTime -- when an
    editor last saved the file -- can date it."""
    if not isinstance(exif, dict):
        return False
    wanted = {tag: exif.get(tag) for tag in CAMERA_DATE_TAGS}
    return imageinfo.parse_capture_date(wanted, "", None)[1] == "exif"


def _exif_number(value):
    """A finite number of an EXIF value (a number, or a list holding one), or None."""
    if isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def flying(gps):
    """True when an EXIF GPS dict (the exif column's "GPS") says the picture was taken from
    a plane: see FLYING_METRES."""
    if not isinstance(gps, dict):
        return False
    altitude = _exif_number(gps.get("GPSAltitude"))
    if altitude is None or _exif_number(gps.get("GPSAltitudeRef")) == 1.0:
        return False            # (1: below sea level)
    if altitude >= FLYING_ALWAYS_METRES:
        return True
    if altitude < FLYING_METRES:
        return False
    speed = _exif_number(gps.get("GPSSpeed"))
    return speed is None or speed >= FLYING_SPEED


def _exif_facts(row, located):
    """(its EXIF date is only when the file was saved -- DateTime, and no camera date --,
    taken from a plane) of a row. (The
    exif column of every picture is read on every look at the trips: it is parsed only
    when a quick look at its text says it may matter.)"""
    text = row["exif"] or ""
    saved_only = False
    if row["taken_source"] == "exif" and '"DateTime"' in text:
        saved_only = not any(imageinfo.parse_capture_date({"DateTimeOriginal": value}, "",
                                                          None)[1] == "exif"
                             for value in _CAMERA_DATE_RE.findall(text))
        if saved_only:
            # (Not only the plain text the quick look reads: the whole of it.)
            try:
                exif = json.loads(text)
                saved_only = (isinstance(exif, dict) and not camera_dated(exif) and
                              imageinfo.parse_capture_date({"DateTime": exif.get("DateTime")},
                                                           "", None)[1] == "exif")
            except ValueError:
                saved_only = False
    high = _ALTITUDE_RE.search(text) if located else None
    altitude = _float(high.group(1)) if high else None
    if altitude is None or altitude < FLYING_METRES:
        return saved_only, False
    try:
        exif = json.loads(text)
    except ValueError:
        return saved_only, False
    return saved_only, isinstance(exif, dict) and flying(exif.get("GPS"))


def _float(text):
    try:
        return float(text)
    except ValueError:
        return None


def is_trip_folder(path):
    """True when a library folder's name starts with a date ("2024-11-09 - Name")."""
    parts = [part for part in re.split(r"[\\/]+", (path or "").strip()) if part]
    if not parts:
        return False
    base = parts[-1].strip()
    name = common.default_library_name(base)
    return bool(name) and name != base and len(name) < len(base)


def trip_libraries(config, library_ids=None):
    """The ids of the libraries that are trips (folder named with a date first), in config
    order; library_ids limits them (None: every library of config)."""
    if not settings(config)["library_folders"]:
        return []
    result = []
    for library in (config or {}).get("libraries", []):
        if not isinstance(library, dict) or not library.get("id"):
            continue
        if library_ids is not None and library["id"] not in library_ids:
            continue
        if is_trip_folder(library.get("path") or ""):
            result.append(library["id"])
    return result


# ---------------------------------------------------------------------------
# Home

def find_home(points):
    """(lat, lon) of home from [(lat, lon, day)] (see the module notes), or None when no
    place has pictures of HOME_MIN_DAYS days."""
    cells = collections.defaultdict(list)
    for lat, lon, day in points:
        cells[(int(math.floor(lat / HOME_CELL_DEGREES)),
               int(math.floor(lon / HOME_CELL_DEGREES)))].append((lat, lon, day))
    best = None
    for key in cells:
        days = set()
        count = 0
        for row in range(key[0] - 1, key[0] + 2):
            for column in range(key[1] - 1, key[1] + 2):
                for _lat, _lon, day in cells.get((row, column), ()):
                    if day:
                        days.add(day)
                    count += 1
        score = (len(days), count, key)
        if best is None or score > best[0]:
            best = (score, key)
    if best is None or best[0][0] < HOME_MIN_DAYS:
        return None
    key = best[1]
    near = [point for row in range(key[0] - 1, key[0] + 2)
            for column in range(key[1] - 1, key[1] + 2)
            for point in cells.get((row, column), ())]
    lats = sorted(point[0] for point in near)
    lons = sorted(point[1] for point in near)
    return lats[len(lats) // 2], lons[len(lons) // 2]


# ---------------------------------------------------------------------------
# Finding the trips

_COLUMNS = ("id, library_id, taken_at, taken_source, lat, lon, place_city, place_region, "
            "place_country, place_cc, media, hidden, content_hash, camera, projection, exif")


class _Picture:
    """What detect() needs of a picture."""

    __slots__ = ("id", "library", "taken", "day", "position", "estimated", "place", "hidden",
                 "video", "content", "camera", "exif_time", "flying", "look_kind")

    def __init__(self, row, estimate):
        self.id = row["id"]
        self.library = row["library_id"]
        self.taken = row["taken_at"] or ""
        lat, lon = row["lat"], row["lon"]
        self.position = (float(lat), float(lon)) if world_map.valid_position(lat, lon) else None
        saved_only, self.flying = _exif_facts(row, self.position is not None)
        self.day = trusted_day(row, saved_only)
        self.look_kind = looks.kind_of(row)
        self.estimated = None
        place = None
        # (The names its file gives, when it has a position: see place_estimates.)
        named = place_estimates.gps_file_columns(row) if self.position is not None else None
        if named is not None:
            place = (named["place_city"], named["place_region"], named["place_country"],
                     named["place_cc"])
        elif row["place_country"]:
            place = (row["place_city"] or "", row["place_region"] or "",
                     row["place_country"] or "", row["place_cc"] or "")
        elif estimate is not None and place_estimates.usable(row, estimate):
            if estimate.get("place_country"):
                place = (estimate.get("place_city") or "", estimate.get("place_region") or "",
                         estimate.get("place_country") or "", estimate.get("place_cc") or "")
            if (self.position is None
                    and world_map.valid_position(estimate.get("lat"), estimate.get("lon"))):
                self.estimated = (float(estimate["lat"]), float(estimate["lon"]))
        self.place = place
        self.hidden = bool(row["hidden"])
        self.video = row["media"] == "video"
        self.content = row["content_hash"] or ""
        self.camera = row["camera"] or ""
        self.exif_time = (self.camera, self.taken) if (
            self.camera and row["taken_source"] == "exif" and self.taken
            and not saved_only) else None


def _load(conn, library_ids):
    """[_Picture] of the present pictures of some libraries, in the order taken."""
    if not library_ids:
        return []
    estimates = place_estimates.for_libraries(conn, library_ids)
    rows = conn.execute("SELECT %s FROM images WHERE missing_since IS NULL AND library_id IN (%s)"
                        % (_COLUMNS, ",".join("?" * len(library_ids))), list(library_ids))
    pictures = [_Picture(row, estimates.get(row["id"])) for row in rows]
    pictures.sort(key=lambda picture: (picture.taken, picture.id))
    return pictures


def _runs(days, gap, allowed=None):
    """Runs of sorted day texts: a day joins the run before it when at most gap days lie
    between them and allowed(first day between, last day between) says yes."""
    runs = []
    for day in days:
        if runs:
            between = _days_between(runs[-1][-1], day) - 1
            if between <= gap and (between <= 0 or allowed is None
                                   or allowed(_add_days(runs[-1][-1], 1),
                                              _add_days(day, -1))):
                runs[-1].append(day)
                continue
        runs.append([day])
    return runs


class _Trip:
    """A trip while detect() puts it together."""

    def __init__(self, trip_id, kind, start, end, libraries=()):
        self.id = trip_id
        self.kind = kind
        self.start = start
        self.end = end
        self.libraries = list(libraries)
        self.pictures = []          # its pictures dated within start..end
        self.other = []             # its pictures with no date in it (library trips)
        self.title = ""

    def holds(self, day):
        return day is not None and self.start <= day <= self.end


def _main_run(pictures):
    """(start, end) of the run of days (LIBRARY_GAP_DAYS) with the most pictures, or None."""
    counts = collections.Counter(picture.day for picture in pictures if picture.day)
    if not counts:
        return None
    best = None
    for run in _runs(sorted(counts), LIBRARY_GAP_DAYS):
        total = sum(counts[day] for day in run)
        if best is None or total > best[0]:
            best = (total, run[0], run[-1])
    return best[1], best[2]


def detect(conn, config, library_ids, options=None):
    """The trips of some libraries (ids), found as the module notes say: [_Trip] in the
    order of their first days, with the owner's changes not yet applied. options:
    settings() of config (worked out when not given)."""
    return _detect(conn, config, library_ids, options)[0]


def big_enough(pictures, min_pictures):
    """True when an away run's pictures make a trip: at least min_pictures of them, or at
    least half as many (rounded up) taken on two days or more."""
    count = len(pictures)
    if count >= min_pictures:
        return True
    return (count >= (min_pictures + 1) // 2
            and len({picture.day for picture in pictures if picture.day}) >= 2)


def _spots(positions):
    """The distinct places (to about a kilometre) of some positions: a short list to measure
    distances to."""
    return sorted({(round(lat, 2), round(lon, 2)) for lat, lon in positions})


def _near(position, spots, km):
    """True when a position is at most km from one of spots (none: no way to tell, True)."""
    if not spots:
        return True
    return any(places.distance_km(position[0], position[1], lat, lon) <= km
               for lat, lon in spots)


def _detect(conn, config, library_ids, options=None):
    """(detect()'s trips, every picture of the libraries, home or None)."""
    options = options or settings(config)
    if not options["enabled"]:
        return [], [], None
    library_ids = [library_id for library_id in library_ids if library_id]
    pictures = _load(conn, library_ids)
    trip_ids = set(trip_libraries(config, set(library_ids)))
    home = options["home"]
    if home is None:
        home = find_home([picture.position + (picture.day,) for picture in pictures
                          if picture.position is not None])
    away_km = options["away_km"]
    join_km = JOIN_FACTOR * away_km

    def away(position):
        return places.distance_km(position[0], position[1], home[0], home[1]) > away_km

    # Library trips, and where their own pictures were taken.
    trips = []
    spots = {}                      # trip id -> _spots of its own pictures
    for library_id in library_ids:
        if library_id not in trip_ids:
            continue
        own = [picture for picture in pictures if picture.library == library_id]
        if not own:
            continue
        span = _main_run(own) or (None, None)
        trip = _Trip(library_id, "library", span[0], span[1], [library_id])
        for picture in own:
            (trip.pictures if trip.holds(picture.day) else trip.other).append(picture)
        trips.append(trip)
        spots[trip.id] = _spots([picture.position for picture in trip.pictures
                                 if picture.position is not None]
                                or [picture.estimated for picture in trip.pictures
                                    if picture.estimated is not None])

    # Days at home and away (every library's own positions), the positions away of
    # each day in the order taken, and the towns of home.
    status = {}
    away_at = collections.defaultdict(list)
    home_towns = set()
    if home is not None:
        for picture in pictures:
            if picture.position is None:
                continue
            if not away(picture.position):
                if picture.place is not None and picture.place[0]:
                    home_towns.add(picture.place[:3])
                if picture.day:
                    status.setdefault(picture.day, "home")
            elif picture.day:
                status[picture.day] = "away"
                away_at[picture.day].append(picture.position)

        def joins(first, last):
            # (The days between two days away: first..last.)
            day = first
            while day <= last:
                if status.get(day) == "home":
                    return False
                day = _add_days(day, 1)
            if _days_between(first, last) + 1 <= options["max_gap_days"]:
                return True
            before = away_at[_add_days(first, -1)][-1]
            after = away_at[_add_days(last, 1)][0]
            return places.distance_km(before[0], before[1], after[0], after[1]) <= STAY_KM

        runs = _runs(sorted(day for day, value in status.items() if value == "away"),
                     max(options["max_gap_days"], STAY_GAP_DAYS), joins)
    else:
        runs = []

    # A run joins the library trip whose dates it shares and near whose pictures it has
    # the most days, else it makes a trip of its own.
    inside = set()                  # days between the away days of a run
    for run in runs:
        start, end = run[0], run[-1]
        day = start
        while day <= end:
            if status.get(day) != "away":
                inside.add(day)
            day = _add_days(day, 1)
        best = None
        for trip in trips:
            if trip.kind != "library" or trip.start is None:
                continue
            if min(end, trip.end) < max(start, trip.start):
                continue
            near = [day for day in run if any(_near(position, spots[trip.id], join_km)
                                               for position in away_at[day])]
            if near and (best is None or len(near) > len(best[1])):
                best = (trip, near)
        if best is not None:
            trip, near = best
            trip.start, trip.end = min(trip.start, near[0]), max(trip.end, near[-1])
        else:
            trip = _Trip("away-" + start.replace("-", ""), "away", start, end)
            trips.append(trip)
            spots[trip.id] = _spots(position for day in run for position in away_at[day])

    # The other libraries' pictures join the trip that holds their day (and, with a
    # position, is near it).
    trips.sort(key=lambda trip: (trip.start or "9999", trip.kind != "library", trip.id))
    own_copies = {}
    for trip in trips:
        if trip.kind == "library":
            members = trip.pictures + trip.other
            own_copies[trip.id] = ({picture.content for picture in members if picture.content},
                                   {picture.exif_time for picture in members if picture.exif_time})
    for picture in pictures:
        if picture.library in trip_ids or picture.day is None or home is None:
            continue
        position = picture.position or picture.estimated
        if position is not None:
            if not away(position):
                continue
        elif status.get(picture.day) != "away" and picture.day not in inside:
            continue
        elif picture.place is not None and picture.place[:3] in home_towns:
            continue                # (a place of home, and no position)
        for trip in trips:
            if not trip.holds(picture.day):
                continue
            if position is not None and not _near(position, spots.get(trip.id), join_km):
                continue
            copies = own_copies.get(trip.id)
            if copies and (picture.content in copies[0] or picture.exif_time in copies[1]):
                break
            trip.pictures.append(picture)
            break
    found = []
    for trip in trips:
        if trip.kind == "away" and not big_enough(trip.pictures, options["min_pictures"]):
            continue
        trip.pictures.sort(key=lambda picture: (picture.taken, picture.id))
        found.append(trip)
    return found, pictures, home


# ---------------------------------------------------------------------------
# Titles and places

def _place_name(place):
    """The words a place goes by in a trip: its town, else its region, else its country."""
    return place[0] or place[1] or place[2]


def trip_places(pictures, limit=MAX_PLACES):
    """[{"name", "region", "country", "cc", "count"}] of the places of some pictures (in
    the order taken): each town once, in the order of the first picture there; at most
    limit, the most pictured. A region named alone (a picture placed by its library's
    area only) is left out when a town of that region is there, and so is a picture
    taken from a plane (its flying)."""
    found = collections.OrderedDict()
    towns = set()                   # (region, country) of the towns found
    for picture in pictures:
        if picture.place is None or getattr(picture, "flying", False):
            continue
        name = _place_name(picture.place)
        if not name:
            continue
        key = (name, picture.place[1], picture.place[2], bool(picture.place[0]))
        entry = found.get(key)
        if entry is None:
            found[key] = entry = {"name": name, "region": picture.place[1],
                                  "country": picture.place[2], "cc": picture.place[3],
                                  "count": 0}
        entry["count"] += 1
        if picture.place[0]:
            towns.add((picture.place[1], picture.place[2]))
    entries = [entry for key, entry in found.items()
               if key[3] or (key[1], key[2]) not in towns]
    if len(entries) > limit:
        keep = sorted(range(len(entries)), key=lambda index: (-entries[index]["count"], index))
        keep = set(keep[:limit])
        entries = [entry for index, entry in enumerate(entries) if index in keep]
    return entries


def _join(names):
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def auto_title(pictures, home_country=""):
    """A title for a trip from the places of its pictures: the town most of them were taken
    in ("Town, Region"), the region ("Region", with its country when it is not
    home_country's), two regions, the country, or up to three countries; "" when no
    picture has a place (one taken from a plane does not count)."""
    placed = [picture.place for picture in pictures
              if picture.place is not None and not getattr(picture, "flying", False)]
    if not placed:
        return ""
    total = float(len(placed))
    countries = collections.Counter(place[2] for place in placed if place[2])
    if not countries:
        return ""
    country, count = countries.most_common(1)[0]
    if count / total < TITLE_MAIN:
        names = [name for name, value in countries.most_common(3) if value / total >= TITLE_SECOND]
        return _join(names or [country])
    in_country = [place for place in placed if place[2] == country]
    regions = collections.Counter(place[1] for place in in_country if place[1])
    suffix = "" if country == home_country else ", " + country
    if not regions:
        return country
    region, count = regions.most_common(1)[0]
    if count / float(len(in_country)) < TITLE_MAIN:
        names = [name for name, value in regions.most_common(2)
                 if value / float(len(in_country)) >= TITLE_SECOND]
        if len(names) == 2:
            return _join(names) + suffix
        return country
    towns = collections.Counter(place[0] for place in in_country
                                if place[1] == region and place[0])
    if towns:
        town, count = towns.most_common(1)[0]
        if count / float(len(in_country)) >= TITLE_TOWN:
            return "%s, %s" % (town, region)
    return region + suffix


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def span_words(start, end):
    """"9-15 November 2024", "30 October - 2 November 2024", "30 December 2023 - 2 January
    2024" or "9 November 2024" (one day)."""
    first, last = _date(start), _date(end)
    if first is None or last is None:
        return ""
    if first == last:
        return "%d %s %d" % (first.day, MONTHS[first.month - 1], first.year)
    if first.year != last.year:
        return "%d %s %d - %d %s %d" % (first.day, MONTHS[first.month - 1], first.year,
                                         last.day, MONTHS[last.month - 1], last.year)
    if first.month != last.month:
        return "%d %s - %d %s %d" % (first.day, MONTHS[first.month - 1], last.day,
                                      MONTHS[last.month - 1], last.year)
    return "%d-%d %s %d" % (first.day, last.day, MONTHS[first.month - 1], first.year)


def _home_country(pictures, home, away_km):
    if home is None:
        return ""
    countries = collections.Counter(
        picture.place[2] for picture in pictures
        if picture.position is not None and picture.place is not None and picture.place[2]
        and places.distance_km(picture.position[0], picture.position[1],
                               home[0], home[1]) <= away_km)
    return countries.most_common(1)[0][0] if countries else ""


# ---------------------------------------------------------------------------
# The owner's changes (data/trips.json)

def path():
    """The file of the owner's changes: data/trips.json."""
    return os.path.join(common.data_dir(), FILE_NAME)


def _empty():
    return {"format": FORMAT, "trips": {}, "merges": [], "splits": {}, "exports": {}}


def _span(value):
    if (isinstance(value, list) and len(value) == 2 and all(
            isinstance(item, str) and _DAY_RE.match(item) and _date(item) for item in value)
            and value[0] <= value[1]):
        return [value[0], value[1]]
    return None


def clean_edits(data):
    """The well-formed part of what data/trips.json holds (a new dict)."""
    result = _empty()
    if not isinstance(data, dict):
        return result
    trips = data.get("trips")
    for trip_id, entry in (trips.items() if isinstance(trips, dict) else ()):
        if not isinstance(trip_id, str) or not ID_RE.match(trip_id) or not isinstance(entry, dict):
            continue
        clean = {}
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            clean["title"] = clean_title(title)
        if entry.get("hidden") is True:
            clean["hidden"] = True
        span = _span(entry.get("span"))
        if span:
            clean["span"] = span
        if clean.get("title") or clean.get("hidden"):
            result["trips"][trip_id] = clean
    merges = data.get("merges")
    for entry in (merges if isinstance(merges, list) else ()):
        if not isinstance(entry, dict) or not isinstance(entry.get("ids"), list):
            continue
        ids = [item for item in entry["ids"] if isinstance(item, str) and ID_RE.match(item)]
        spans = entry.get("spans") if isinstance(entry.get("spans"), list) else []
        spans = [_span(item) for item in spans]
        if len(ids) >= 2 and len(set(ids)) == len(ids):
            if len(spans) != len(ids):
                spans = [None] * len(ids)
            result["merges"].append({"ids": ids, "spans": spans})
    splits = data.get("splits")
    for trip_id, entry in (splits.items() if isinstance(splits, dict) else ()):
        if not isinstance(trip_id, str) or not ID_RE.match(trip_id) or not isinstance(entry, dict):
            continue
        dates = sorted({item for item in entry.get("dates") or []
                        if isinstance(item, str) and _DAY_RE.match(item) and _date(item)})
        if dates:
            result["splits"][trip_id] = {"dates": dates, "span": _span(entry.get("span"))}
    exports = data.get("exports")
    for trip_id, entry in (exports.items() if isinstance(exports, dict) else ()):
        if not isinstance(trip_id, str) or not ID_RE.match(trip_id) or not isinstance(entry, dict):
            continue
        options = entry.get("options")
        # (The options are checked where they are used: trip_export.trip_options.)
        result["exports"][trip_id] = {"options": options if isinstance(options, dict) else {},
                                      "span": _span(entry.get("span"))}
    return result


def _stamp(file_path):
    try:
        stat = os.stat(file_path)
    except OSError:
        return (file_path, None, None)
    return (file_path, stat.st_mtime_ns, stat.st_size)


def _read(file_path):
    """The owner's changes in the file (empty when there is none); ValueError when the
    file is there but cannot be read as such."""
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return _empty()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(str(error))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("not a trips file")
    return clean_edits(data)


def load_edits():
    """The owner's changes (clean_edits), read again only when the file changed. A file
    that cannot be read counts as no changes (with a warning)."""
    file_path = path()
    stamp = _stamp(file_path)
    with _lock:
        if _memo["stamp"] == stamp and _memo["edits"] is not None:
            return json.loads(json.dumps(_memo["edits"]))
        try:
            edits = _read(file_path)
        except ValueError as error:
            print("Warning: %s cannot be read (%s); the trips are shown as found."
                  % (file_path, error))
            edits = _empty()
        _memo["stamp"] = stamp
        _memo["edits"] = edits
        return json.loads(json.dumps(edits))


def edits_version():
    """A short text that changes whenever data/trips.json does."""
    stamp = _stamp(path())
    return "%s:%s" % (stamp[1], stamp[2])


def _rewrite(change):
    """Re-read the file, let change(edits) change them in place, and write them back in
    one go (under a lock). A file that cannot be read is kept as trips.json.broken-<time>.
    Returns what change returned. Raises OSError when the file cannot be written."""
    file_path = path()
    with _lock:
        try:
            edits = _read(file_path)
        except ValueError:
            edits = _empty()
            if os.path.exists(file_path):
                os.replace(file_path, "%s.broken-%d" % (file_path, int(time.time())))
        result = change(edits)
        edits = clean_edits(edits)
        common.ensure_dir(os.path.dirname(file_path))
        common.write_file_atomic(file_path, json.dumps(edits, indent=1, sort_keys=True,
                                                       ensure_ascii=True) + "\n")
        _memo["stamp"] = None
    return result


def clean_title(value):
    """A trip title as kept: one line, spaces collapsed, at most TITLE_LENGTH characters."""
    text = _CONTROL_RE.sub(" ", value if isinstance(value, str) else "")
    return re.sub(r"\s+", " ", text).strip()[:TITLE_LENGTH]


# ---------------------------------------------------------------------------
# The owner's changes applied

def _overlaps(span, start, end):
    return bool(span) and start is not None and span[0] <= end and start <= span[1]


def _base_of(key):
    """The trip a part id ("<id>.YYYYMMDD") is a part of (the id itself when it is none)."""
    return key.partition(".")[0]


def _claims(edits, trips):
    """{edit id: trip id} for the trip ids the owner's changes name (parts by their
    trip's): an id that names a detected trip is that trip's; one that names none is
    the away trip (not named by another change) whose dates overlap the span it was
    made with -- the trip's first day moved since."""
    spans = {}
    for entry_id, entry in edits["trips"].items():
        if "." not in entry_id:
            spans.setdefault(entry_id, entry.get("span"))
    for entry_id, entry in edits["splits"].items():
        spans.setdefault(entry_id, entry.get("span"))
    for entry_id, entry in edits["exports"].items():
        if "." not in entry_id:
            spans.setdefault(entry_id, entry.get("span"))
    for merge in edits["merges"]:
        for entry_id, span in zip(merge["ids"], merge["spans"]):
            spans.setdefault(entry_id, span)
    for entry_id in list(edits["trips"]) + list(edits["exports"]):
        spans.setdefault(_base_of(entry_id), None)
    present = {trip.id for trip in trips}
    taken = {entry_id for entry_id in spans if entry_id in present}
    result = {entry_id: entry_id for entry_id in taken}
    for entry_id in sorted(spans):
        if entry_id in present:
            continue
        for trip in trips:
            if (trip.kind == "away" and trip.id not in taken
                    and _overlaps(spans[entry_id], trip.start, trip.end)):
                result[entry_id] = trip.id
                taken.add(trip.id)
                break
    return result


def _current_id(claims, key):
    """The id of the trip (or part) found now that an id of the changes names."""
    base, dot, part = key.partition(".")
    return claims.get(base, base) + dot + part


def _edit_key(claims, trip_id):
    """The id the changes know the trip (or part) trip_id found now by."""
    base, dot, part = trip_id.partition(".")
    for key, target in sorted(claims.items()):
        if target == base:
            return key + dot + part
    return trip_id


def _merged(group):
    """One trip made of a group of trips (in date order: the first gives the id)."""
    first = group[0]
    starts = [item.start for item in group if item.start]
    ends = [item.end for item in group if item.end]
    trip = _Trip(first.id, "library" if any(item.kind == "library" for item in group) else "away",
                 min(starts) if starts else None, max(ends) if ends else None)
    trip.merged = [item.id for item in group]
    for item in group:
        for library_id in item.libraries:
            if library_id not in trip.libraries:
                trip.libraries.append(library_id)
        trip.pictures.extend(item.pictures)
        trip.other.extend(item.other)
        if not trip.title and item.title:
            trip.title = item.title
    trip.pictures.sort(key=lambda picture: (picture.taken, picture.id))
    trip.other.sort(key=lambda picture: (picture.taken, picture.id))
    return trip


def _split(trip, dates):
    """The parts of a trip split before each of dates that lies within it (after its
    first day); the first part keeps the trip's id and its other pictures."""
    cuts = sorted(date for date in set(dates) if trip.start and trip.start < date <= trip.end)
    if not cuts:
        return [trip]
    parts = []
    bounds = [trip.start] + cuts
    for index, start in enumerate(bounds):
        end = _add_days(bounds[index + 1], -1) if index + 1 < len(bounds) else trip.end
        part = _Trip(trip.id if not index else "%s.%s" % (trip.id, start.replace("-", "")),
                     trip.kind, start, end, trip.libraries)
        part.base = trip.id
        part.title = trip.title
        part.merged = trip.merged
        part.pictures = [picture for picture in trip.pictures if part.holds(picture.day)]
        if not index:
            part.other = list(trip.other)
        parts.append(part)
    return parts


def apply_edits(trips, edits, claims=None):
    """The trips (detect's) with the owner's changes: merges, then splits, then titles and
    hidden. A new list of _Trip with .hidden, .edited (the title is the owner's), .base
    (the trip a part was split from: its own id when it is no part) and .merged (the
    ids of the trips it was made of, or None)."""
    claims = _claims(edits, trips) if claims is None else claims
    result = list(trips)
    for trip in result:
        trip.merged = None
    for merge in edits["merges"]:
        wanted = {_current_id(claims, key) for key in merge["ids"]}
        members = [trip for trip in result if trip.id in wanted]
        if len(members) < 2:
            continue
        members.sort(key=lambda trip: (trip.start or "9999", trip.id))
        trip = _merged(members)
        position = min(result.index(member) for member in members)
        result = [item for item in result if item not in members]
        result.insert(position, trip)
    splits = {}
    for key, entry in edits["splits"].items():
        splits.setdefault(_current_id(claims, key), set()).update(entry["dates"])
    parts = []
    for trip in result:
        trip.base = trip.id
        parts.extend(_split(trip, splits.get(trip.id, ())))
    by_id = {trip.id: trip for trip in parts}
    for trip in parts:
        trip.hidden = False
        trip.edited = False
    for key, entry in sorted(edits["trips"].items()):
        trip = by_id.get(_current_id(claims, key))
        if trip is None:
            continue
        if entry.get("title"):
            trip.title = entry["title"]
            trip.edited = True
        trip.hidden = bool(entry.get("hidden"))
    return parts


# ---------------------------------------------------------------------------
# Everything together

class Trips:
    """The trips of some libraries with the owner's changes (see build()): .trips (in date
    order), .found (detect's), .claims (see _claims), .edits and .home ((lat, lon) or
    None: for trip_export.py only, never sent to a page)."""

    def __init__(self, trips, found, claims, edits, home=None):
        self.trips = trips
        self.found = found
        self.claims = claims
        self.edits = edits
        self.home = home

    def get(self, trip_id):
        """The trip trip_id; TripError (404) when there is none. An id the trip had before
        its first day moved (as a command line logged earlier names it) still finds it:
        by the key the owner's changes know it by (key), else -- "away-YYYYMMDD" -- the
        away trip (not a part of one) that holds that day."""
        for trip in self.trips:
            if trip.id == trip_id:
                return trip
        for trip in self.trips:
            if self.key(trip) == trip_id:
                return trip
        match = _AWAY_ID_RE.match(trip_id) if isinstance(trip_id, str) else None
        if match:
            day = "%s-%s-%s" % match.groups()
            for trip in self.trips:
                if trip.kind == "away" and "." not in trip.id and trip.holds(day):
                    return trip
        raise TripError("There is no trip %s." % trip_id, 404)

    def key(self, trip):
        """The id the owner's changes know a trip by (it stays when an away trip's first
        day moves; see _claims)."""
        return _edit_key(self.claims, trip.id)

    def export_options(self, trip):
        """The options a trip was last exported with (as kept: unchecked), or {}."""
        entry = self.edits["exports"].get(self.key(trip))
        return dict(entry["options"]) if entry else {}


def dated_title(start, end):
    """The title of a trip with no place to name it by: "Trip, 9-15 November 2024"."""
    return "Trip, " + span_words(start, end) if start else "Trip"


def build(conn, config, library_ids, edits=None):
    """The trips of some libraries (ids), with the owner's changes (edits: default
    data/trips.json's) applied, and titled: a library trip has its library's name, an
    away trip one made from its places (auto_title), unless the owner gave it one.
    Each trip also has .auto (auto_title of its pictures), .auto_titled (its title is
    that, not the owner's or its library's) and .home_country (see told_by)."""
    options = settings(config)
    found, pictures, home = _detect(conn, config, library_ids, options)
    edits = load_edits() if edits is None else clean_edits(edits)
    names = {library.get("id"): common.library_label(library)
             for library in (config or {}).get("libraries", []) if isinstance(library, dict)}
    for trip in found:
        if trip.kind == "library":
            trip.title = names.get(trip.id) or trip.id
    home_country = _home_country(pictures, home, options["away_km"]) if found else ""
    claims = _claims(edits, found)
    trips = apply_edits(found, edits, claims)
    for trip in trips:
        trip.auto = auto_title(trip.pictures, home_country)
        trip.auto_titled = not trip.title
        trip.home_country = home_country
        if not trip.title:
            trip.title = trip.auto or dated_title(trip.start, trip.end)
    return Trips(trips, found, claims, edits, home)


def told_by(trip, pictures):
    """(start, end, title) of a trip as some of its pictures tell it -- those a visitor
    may see, or those an export publishes: the first and last of their days ("", "" when
    none has one), and the owner's or its library's title, else one made from their
    places (auto_title; never from pictures the others do not see)."""
    days = sorted(picture.day for picture in pictures if picture.day)
    start, end = (days[0], days[-1]) if days else ("", "")
    if not getattr(trip, "auto_titled", False):
        return start, end, trip.title
    return start, end, (auto_title(pictures, getattr(trip, "home_country", ""))
                        or dated_title(start, end))


def _day_view(day, pictures, units=None):
    entry = {"date": day, "ids": [picture.id for picture in pictures],
             "places": [entry["name"] for entry in trip_places(pictures, limit=6)]}
    if units:
        entry["looks"] = units
    return entry


def trip_view(trip, admin=False, units=None):
    """What a page gets of a trip (a dict): id, title, kind ("library" or "away"), start,
    end, libraries (ids: the trip's own first), count (pictures), videos, places
    (trip_places), days [{date, ids (in the order taken), places (names), looks (its
    look units of two pictures or more: [[ids]], see look_units; only when it has
    some)}], other (the ids of its pictures with no date in it) and other_looks; for
    the admin also hidden, edited (the title is the owner's), auto_title, base (the
    trip a part was split from) and merged (the trips it was made of). Visitors get
    none of its hidden pictures, and its dates and title as the others tell them
    (told_by). units: {day (or "other"): look units}."""
    shown = [picture for picture in trip.pictures if admin or not picture.hidden]
    other = [picture for picture in trip.other if admin or not picture.hidden]
    units = units or {}
    days = collections.OrderedDict()
    for picture in shown:
        days.setdefault(picture.day, []).append(picture)
    libraries = list(trip.libraries)
    libraries.extend(sorted({picture.library for picture in shown + other} - set(libraries)))
    if admin:
        start, end, title = trip.start or "", trip.end or "", trip.title
    else:
        start, end, title = told_by(trip, shown)
    result = {"id": trip.id, "title": title, "kind": trip.kind,
              "start": start, "end": end, "libraries": libraries,
              "count": len(shown) + len(other),
              "videos": sum(1 for picture in shown + other if picture.video),
              "places": trip_places(shown),
              "days": [_day_view(day, items, units.get(day)) for day, items in days.items()],
              "other": [picture.id for picture in other]}
    if units.get("other"):
        result["other_looks"] = units["other"]
    if admin:
        result["hidden"] = bool(trip.hidden)
        result["edited"] = bool(trip.edited)
        result["auto_title"] = trip.auto
        result["base"] = trip.base
        if trip.merged:
            result["merged"] = list(trip.merged)
    return result


def look_units(conn, config, groups):
    """The look units of each list of pictures in groups (a day's): [[ids]] of the units of
    two pictures or more, pictures whose looks (looks.py: the vectors of an image model)
    are at least LOOK_UNIT_COSINE close (LOOK_UNIT_NEAR_COSINE when taken within
    SPREAD_SECONDS of each other), each linked to the next (single link; each
    picture is compared with the next LOOK_UNIT_WINDOW of the list, in the order taken). Videos
    count (by their poster), the clips of drives do not (a drive is one already, and
    road views look alike), nor do pictures without a look vector. No store: none."""
    import drives
    library_ids = sorted({picture.library for group in groups for picture in group})
    clips = set()
    for library_id in library_ids:
        for drive in drives.library_drives(conn, library_id, config):
            clips.update(clip.id for clip in drive.clips)
    wanted = {picture.id: (picture.content, picture.look_kind)
              for group in groups for picture in group
              if picture.content and picture.id not in clips}
    found = {}
    for image_id, vec in search_index.group_looks(wanted).items():
        look = search_index._look(vec)
        if look is not None:
            found[image_id] = look
    moments = {}
    for group in groups:
        for picture in group:
            try:
                moments[picture.id] = datetime.datetime.strptime(picture.taken[:19],
                                                                 "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                moments[picture.id] = None

    def alike(first, second):
        cosine = search_index._look_cosine(found[first], found[second])
        if cosine >= LOOK_UNIT_COSINE:
            return True
        if cosine < LOOK_UNIT_NEAR_COSINE or moments[first] is None or moments[second] is None:
            return False
        return abs((moments[first] - moments[second]).total_seconds()) <= SPREAD_SECONDS
    result = []
    for group in groups:
        members = [picture.id for picture in group if picture.id in found]
        parent = {image_id: image_id for image_id in members}

        def root(image_id):
            while parent[image_id] != image_id:
                parent[image_id] = parent[parent[image_id]]
                image_id = parent[image_id]
            return image_id
        for index, first in enumerate(members):
            for second in members[index + 1:index + 1 + LOOK_UNIT_WINDOW]:
                if root(first) != root(second) and alike(first, second):
                    parent[root(second)] = root(first)
        units = collections.OrderedDict()
        for image_id in members:
            units.setdefault(root(image_id), []).append(image_id)
        result.append([ids for ids in units.values() if len(ids) > 1])
    return result


def gap_classes(times):
    """How far apart pictures taken one after the other were, as one digit each after the
    first (times: their "YYYY-MM-DDTHH:MM:SS" texts, in the order taken): "0" within
    SAME_MOMENT_SECONDS, "1" within SPREAD_SECONDS, "2" further (or unknown). An export
    publishes this instead of the times (static/trips.js spreads a day's best by it)."""
    digits = []
    previous = None
    for index, text in enumerate(times):
        try:
            moment = datetime.datetime.strptime((text or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            moment = None
        if index:
            if moment is None or previous is None:
                digits.append("2")
            else:
                seconds = abs((moment - previous).total_seconds())
                digits.append("0" if seconds < SAME_MOMENT_SECONDS
                              else "1" if seconds < SPREAD_SECONDS else "2")
        previous = moment
    return "".join(digits)


def visible_by_base(built):
    """{base trip id: its pictures that are not hidden} of built (a Trips): the pictures of
    a trip's parts count together."""
    visible = collections.defaultdict(list)
    for trip in built.trips:
        visible[trip.base].extend(picture for picture in trip.pictures if not picture.hidden)
    return visible


def visitors_see(trip, options, visible):
    """True when visitors are shown a trip (view): Trips is on, the trip is not hidden, it
    holds a picture they may see, and an away trip (not merged by the owner) is one by
    the pictures they may see (big_enough: not by the owner's hidden pictures). options:
    settings(); visible: visible_by_base."""
    if not options["enabled"] or trip.hidden:
        return False
    if trip.kind == "away" and not trip.merged and not big_enough(visible[trip.base],
                                                                  options["min_pictures"]):
        return False
    return any(not picture.hidden for picture in list(trip.pictures) + list(trip.other))


def view(conn, config, library_ids, admin=False, edits=None):
    """What /api/trips sends: {"enabled", "best_per_day", "trips": [trip_view], newest
    first}. Visitors get no hidden trip, no trip with nothing they may see, and no away
    trip found by the owner's hidden pictures: one whose pictures they may see are not
    big_enough (unless the owner merged it)."""
    options = settings(config)
    result = {"enabled": options["enabled"], "best_per_day": options["best_per_day"],
              "trips": []}
    if not options["enabled"]:
        return result
    built = build(conn, config, library_ids, edits)
    visible = visible_by_base(built)
    listed = []
    for trip in built.trips:
        if not admin and not visitors_see(trip, options, visible):
            continue
        listed.append(trip)
    # The look units of every day of the trips, worked out together.
    keys, groups = [], []
    for trip in listed:
        days = collections.OrderedDict()
        for picture in trip.pictures:
            if admin or not picture.hidden:
                days.setdefault(picture.day, []).append(picture)
        days["other"] = [picture for picture in trip.other if admin or not picture.hidden]
        for day, pictures in days.items():
            if len(pictures) > 1:
                keys.append((trip.id, day))
                groups.append(pictures)
    units = collections.defaultdict(dict)
    for (trip_id, day), found in zip(keys, look_units(conn, config, groups)):
        if found:
            units[trip_id][day] = found
    for trip in listed:
        shown = trip_view(trip, admin, units.get(trip.id))
        if not admin and not shown["count"]:
            continue
        result["trips"].append(shown)
    result["trips"].sort(key=lambda item: (item["start"], item["id"]), reverse=True)
    return result


def state_key(conn, library_ids):
    """What the trips depend on beyond config.json, as JSON-friendly values: the
    libraries' rows (search_index.data_state), the estimated places and the owner's
    changes. It changes whenever the trips may."""
    try:
        estimates = list(conn.execute("SELECT COUNT(*), TOTAL(updated), TOTAL(image_id) FROM "
                                      "place_estimates").fetchone())
    except sqlite3.Error:      # (no estimates yet)
        estimates = None
    return [search_index.data_state(conn, library_ids), estimates, edits_version()]


class TripsService:
    """Answers /api/trips: view() as JSON with an ETag (304 when it matches), worked out
    again only when what the trips depend on changed (state_key, the settings, the
    libraries). Thread safe."""

    MEMO_SIZE = 4

    def __init__(self):
        self._lock = threading.Lock()
        self._memo = collections.OrderedDict()      # etag -> body bytes

    def respond(self, conn, config, libraries, admin=False, etag_matches=None):
        """(status, body bytes, headers) for the trips of libraries ({id: name})."""
        key = [FORMAT, sorted(settings(config).items()), sorted(libraries.items()),
               trip_libraries(config, set(libraries)), bool(admin),
               state_key(conn, list(libraries)), place_estimates.FILE_PLACE_RULES]
        etag = hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode("utf-8")
                            ).hexdigest()[:32]
        headers = {"ETag": '"%s"' % etag, "Cache-Control": "private, no-cache",
                   "X-Content-Type-Options": "nosniff"}
        if etag_matches is not None and etag_matches(etag):
            return 304, b"", headers
        with self._lock:
            body = self._memo.get(etag)
        if body is None:
            body = json.dumps(view(conn, config, list(libraries), admin=admin),
                              ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8")
            with self._lock:
                self._memo[etag] = body
                while len(self._memo) > self.MEMO_SIZE:
                    self._memo.popitem(last=False)
        return 200, body, headers


# ---------------------------------------------------------------------------
# Changes (the admin's)

def set_trip(conn, config, library_ids, trip_id, title=None, hidden=None):
    """Rename a trip (title; "" gives it back the title it had before) or hide it (hidden
    True) or show it again; returns its trip_view (the admin's). Raises TripError (404:
    there is no such trip)."""
    if title is not None and not isinstance(title, str):
        raise TripError("A title is text.")
    if hidden is not None and not isinstance(hidden, bool):
        raise TripError("hidden is true or false.")
    current = build(conn, config, library_ids)
    trip = current.get(trip_id)

    def change(edits):
        key = _edit_key(_claims(edits, current.found), trip.id)
        entry = edits["trips"].setdefault(key, {})
        if title is not None:
            entry["title"] = clean_title(title)
        if hidden is not None:
            entry["hidden"] = hidden
        if trip.start:
            entry["span"] = [trip.start, trip.end]
    _rewrite(change)
    return trip_view(build(conn, config, library_ids).get(trip_id), admin=True)


def split_trip(conn, config, library_ids, trip_id, date):
    """Split a trip before one of its days (not its first): the days from date on are a
    trip of their own. Returns the ids of the two parts. Raises TripError."""
    if not isinstance(date, str) or not _DAY_RE.match(date) or _date(date) is None:
        raise TripError("Name the day the second part starts as YYYY-MM-DD.")
    current = build(conn, config, library_ids)
    trip = current.get(trip_id)
    if not trip.start or not trip.start < date <= trip.end:
        raise TripError("A trip can only be split at one of its days after the first.")
    whole = [item for item in current.trips if item.base == trip.base]

    def change(edits):
        key = _edit_key(_claims(edits, current.found), trip.base)
        entry = edits["splits"].setdefault(key, {"dates": [], "span": None})
        if date not in entry["dates"]:
            entry["dates"].append(date)
        entry["span"] = [min(item.start for item in whole), max(item.end for item in whole)]
    _rewrite(change)
    return [trip.id, "%s.%s" % (trip.base, date.replace("-", ""))]


def merge_trips(conn, config, library_ids, ids):
    """Make one trip of two or more: parts of one split trip are joined again (the splits
    between them go); whole trips are merged (with the trips they were merged with
    before). Returns the id of the trip they make. Raises TripError (409: a part of a
    split trip and another trip)."""
    if (not isinstance(ids, list) or len(ids) < 2 or len(set(ids)) != len(ids)
            or not all(isinstance(item, str) and ID_RE.match(item) for item in ids)):
        raise TripError("Name two or more trips to merge.")
    current = build(conn, config, library_ids)
    chosen = sorted((current.get(trip_id) for trip_id in ids),
                    key=lambda trip: (trip.start or "9999", trip.id))
    if len({trip.base for trip in chosen}) == 1:
        base, drop = chosen[0].base, {trip.start for trip in chosen[1:]}

        def unsplit(edits):
            key = _edit_key(_claims(edits, current.found), base)
            entry = edits["splits"].get(key)
            if entry is not None:
                entry["dates"] = [item for item in entry["dates"] if item not in drop]
                if not entry["dates"]:
                    del edits["splits"][key]
        _rewrite(unsplit)
        return chosen[0].id
    split = {item.base for item in current.trips if item.id != item.base}
    if any(trip.base in split for trip in chosen):
        raise TripError("A split trip can only be merged with another trip once its parts "
                        "are joined again.", 409)

    def change(edits):
        claims = _claims(edits, current.found)
        members, spans = [], []

        def add(key, span):
            if key not in members:
                members.append(key)
                spans.append(span)
        for trip in chosen:
            for member in (trip.merged or [trip.id]):
                found = next((item for item in current.found if item.id == member), None)
                add(_edit_key(claims, member),
                    [found.start, found.end] if found is not None and found.start else None)
        for merge in list(edits["merges"]):
            if set(merge["ids"]) & set(members):
                for key, span in zip(merge["ids"], merge["spans"]):
                    add(key, span)
                edits["merges"].remove(merge)
        edits["merges"].append({"ids": members, "spans": spans})
    _rewrite(change)
    return chosen[0].id


def record_export(conn, config, library_ids, trip_id, options=None):
    """Note in data/trips.json that a trip was exported (its dates, so the export's
    folder and ids keep following the trip when its first day moves), and with options
    (a dict, those that differ from the defaults) the options to use next time; None
    keeps the ones saved. Returns the trip's key (Trips.key). Raises TripError, OSError."""
    current = build(conn, config, library_ids)
    trip = current.get(trip_id)

    def change(edits):
        key = _edit_key(_claims(edits, current.found), trip.id)
        entry = edits["exports"].setdefault(key, {"options": {}, "span": None})
        if options is not None:
            entry["options"] = dict(options)
        if trip.start:
            entry["span"] = [trip.start, trip.end]
        return key
    return _rewrite(change)


def reset_trip(conn, config, library_ids, trip_id):
    """Forget the owner's changes of a trip -- its title and hidden, those of its parts,
    its splits and the merge it was made by -- so it is shown as found again. Returns
    the ids of the trips it is made of."""
    current = build(conn, config, library_ids)
    trip = current.get(trip_id)
    members = list(trip.merged or [trip.base])

    def change(edits):
        claims = _claims(edits, current.found)
        keys = {_edit_key(claims, member) for member in members} | set(members)
        keys.add(_edit_key(claims, trip.base))
        for key in list(edits["trips"]):
            if _base_of(key) in keys:
                del edits["trips"][key]
        for key in list(edits["splits"]):
            if key in keys:
                del edits["splits"][key]
        edits["merges"] = [merge for merge in edits["merges"] if not set(merge["ids"]) & keys]
    _rewrite(change)
    return sorted(members)


# ---------------------------------------------------------------------------
# Command line

def main(argv=None):
    """python trips.py --list | --stories [--trip ID] [--redo] (trip_stories.main_cli).
    Returns the exit code."""
    import trip_stories
    return trip_stories.main_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
