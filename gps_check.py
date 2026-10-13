#!/usr/bin/env python3
"""The GPS check: positions that cannot be where a picture was taken are set aside.

A camera writes the GPS position it has, which is not always where the
picture was taken: a phone in an aeroplane keeps the last fix it had on
the ground, and a camera without a GPS receiver of its own (many 360
cameras, and other cameras paired with a phone app) writes the last
position the app gave it -- hours or days ago, at home. Such a
position gives the picture the wrong place, puts it on the map in the
wrong spot and makes searches for that place find it.

run() looks at the positions of every present picture of the configured
libraries (all of them together: a phone in one library vouches for, or
contradicts, a camera in another) after each scan, previews and places
job (also one that was stopped), before an export with locations, and
when the admin decides about a picture. It reads only the database
(never an original), and decides again every time, so a decision can
change when pictures come or go. The rules (see evaluate):

  void      the camera marked its fix as not valid (EXIF GPSStatus "V").
  stale     the GPS time saved with the picture shows that the fix is
            older than the picture by more than stale_minutes (default
            5), and either by more than 15 minutes or by long enough for
            the speed the fix states to have taken the camera more than
            2 km away (a phone standing still keeps a fix of a few
            minutes that is still right). With the time zone of the
            picture known that is plain subtraction; without it (or with
            one in doubt, see below) every offset the picture may have
            had must make the fix stale: the same camera's within the
            hour and the library's within 36 hours, or else any offset
            at all -- and a GPS time a whole quarter hour (give or take
            a minute) from the camera's clock is fresh. Only for a camera
            whose other pictures mostly have fresh fixes: a camera whose
            clock is a few minutes off would otherwise lose every
            position.
  frozen    one camera gave the same position and the same speed (above
            5 km/h) to pictures taken minutes apart: a fix that says it
            was moving cannot stay put. Only the pictures taken long
            enough after the first of them for that speed to have taken
            the camera further than the position's tolerance.
  travel    the picture's position does not fit the pictures taken nearest
            in time: of its `neighbours` (default 4) nearest independent
            pictures with a position, within window_hours (12), a strict
            majority -- and at least 2 -- are too far away to be reached
            at max_speed (1,300 km/h: an airliner with a jet stream
            behind it) in the time between them.

"Independent" pictures: copies of the same shot never count (the same
content hash; the same camera and second; the same dHash within 2
minutes), nor pictures at exactly the picture's own position (the same
cached fix), and pictures of one camera at one position count once. The
distance between two pictures is taken less 1 km and each position's
stated accuracy (GPSHPositioningError, at most 200 km); the time between
them is the longest the clocks allow, plus 2 minutes. Times are UTC
when the time zone is known (the picture's own offset, a fresh GPS time,
the same camera's offset within the hour); otherwise the interval spans
every offset recorded in the library within 36 hours (or, failing that,
in any library; with no such offset at all, the camera's clock is
compared as it is, and only with other pictures timed that way). A
picture's own offset is in doubt when it is more than 4 hours from the
one its longitude suggests (lon / 15), or when its GPS time or date
contradicts it (a phone that kept the time zone of the day before after
a flight): its interval then also spans 2 hours either side of the
longitude's. A date taken from the name of a file a program wrote (no
capture date of its own, but a Software tag: an app's export, named when
it was exported) is the latest the picture may have been taken; its
interval reaches 2 hours earlier.

Some positions are weak: never a reference for others, and set aside
when more than half of the pictures compared contradict them, even if
that is only one. A position is weak when its source saves no GPS time
and no accuracy (it cannot be checked), its camera has no GPS receiver
of its own (no two of its pictures have such a fix), and the camera gave
exactly the same position to pictures taken more than repeat_hours
(default 1) apart, with no other position of that camera in between nor
more than a week without a picture: the "last known position" of a
camera that has no fix of its own. Positions set aside by a rule above
never count as references either.

A position that is set aside ("ignored") is kept in the gps_checks table
(db.SCHEMA_V6) with the reason, and the picture's lat, lon and place_*
columns are cleared: it has no place, is not on the map, is not found by
place or has:gps searches, and an export never holds it (the EXIF GPS
data stays in the exif column, as the file has it, and is only shown to
the admin). When the scanner reads the file again (scan.py), a position
that is still the one set aside stays set aside; any other (or none)
drops the row, and the next check decides about it (file_read_again).
The admin's choice -- "Use this location anyway" or "Ignore location" in
the viewer -- is kept by content (gps_choices, keyed by the content hash:
it follows the picture when it moves), applies to every copy of the shot
(see "independent" above: set_choice stores it for each, and a copy
without a choice of its own follows the others) and survives every
scan; choosing what the check decides anyway removes it.

Optionally (estimate_places, off by default) an ignored picture gets the
place of the pictures taken within estimate_minutes of it when they all
agree on one: its place_* columns are filled, without a position, and it
is labelled as estimated wherever it is shown (search records: pe);
an export holds it only with its Location option, like any place name.

Settings: config.json "gps_check" (see settings() and SETTINGS.md).

This module uses only the standard library.
"""

import bisect
import collections
import datetime
import json
import math
import re
import sqlite3
import time

import places
import world_map

# config.json "gps_check": the defaults and the ranges a value is held to.
DEFAULTS = {
    "enabled": True,
    "max_speed": 1300,          # km/h: faster than this is impossible travel
    "window_hours": 12,         # how far in time neighbours are looked for
    "neighbours": 4,            # how many of the nearest neighbours vote
    "stale_minutes": 5,         # a GPS fix older than this is stale
    "repeat_hours": 1,          # the same position this long: a camera's last known one
    "across_libraries": True,   # pictures of other libraries count as neighbours
    "estimate_places": False,   # give ignored pictures the place their neighbours agree on
    "estimate_minutes": 30,     # ... taken within this many minutes
}
RANGES = {
    "max_speed": (300, 5000),
    "window_hours": (1, 72),
    "neighbours": (2, 9),
    "stale_minutes": (1, 240),
    "repeat_hours": (0.25, 72),
    "estimate_minutes": (1, 240),
}
_BOOLEANS = ("enabled", "across_libraries", "estimate_places")

# Capture dates good enough to time a picture (images.taken_source): from
# the camera, the file name or the video's own date; never the file's time.
TRUSTED_SOURCES = frozenset(("exif", "filename", "video"))
QUARTER = 900               # s: every time zone is a whole quarter hour from UTC
CLOCK_SLACK = 60            # s: a GPS time this near a whole quarter hour is fresh
OFFSET_RANGE = (-12 * 3600, 14 * 3600)
CAMERA_OFFSET_SECONDS = 3600         # the same camera's offset is borrowed this near
LIBRARY_OFFSET_SECONDS = 36 * 3600   # the library's offsets are tried this near
FRESH_SHARE = 0.5           # a camera's stale fixes count when this share is fresh ...
FRESH_MIN = 2               # ... and at least this many
STALE_ANYWAY_SECONDS = 15 * 60       # a stale fix this old is set aside whatever its speed
STALE_MOVED_KM = 2.0        # a younger one only when its speed took it this far since
DOUBTFUL_HOURS = 4          # an offset this far from the longitude's (lon / 15) is in doubt
DOUBTFUL_MINUTES = 120      # ... and then the longitude's, give or take this, may be right
EXPORT_SECONDS = 2 * 3600   # a date from an exported file's name: taken up to this earlier
GAP_SLACK = 120             # s added to every time between two pictures
BASE_TOLERANCE_KM = 1.0     # how far off any position may be
ACCURACY_CAP_KM = 200.0     # stated inaccuracy counted at most
COPY_SECONDS = 120          # the same dHash this near in time: the same shot
FROZEN_SPEED = 5.0          # km/h: slower is standing still
SCAN_LIMIT = 300            # neighbours looked at per picture at most (alike ones are skipped)
REPEAT_GAP_SECONDS = 7 * 86400       # a camera's repeated position: no longer gap than this
# Two positions this near (degrees, about a metre) are the same fix: a fix
# written in another form (degrees, minutes and seconds to 1/100 s, or
# decimal degrees) is up to 0.000003 degrees off.
SAME_POSITION_DEGREES = 0.00001
SPEED_FACTORS = {"K": 1.0, "M": 1.609344, "N": 1.852}
# iPhones write GPSSpeed in metres per second, though GPSSpeedRef says "K":
# their stated speeds are 3.6 times below the distance over the time between
# their fixes. The makes (camera_key's first word) that do.
METRES_PER_SECOND_MAKES = frozenset(("apple",))

CHOICES = ("use", "ignore")
# The keys of a search record (search_index) a position changes.
RECORD_KEYS = ("gp", "ci", "rg", "co", "cc", "km", "pe", "gx")
REASON_KINDS = ("void", "stale", "frozen", "travel")
OTHERS_MAX = 500            # the choice route sends at most this many changed records

_EPOCH = datetime.datetime(1970, 1, 1)
_OFFSET_RE = re.compile(r"^([+-])(\d\d):?(\d\d)$")
_DATE_RE = re.compile(r"^(\d{4})[:\-](\d\d)[:\-](\d\d)")


# ---------------------------------------------------------------------------
# Settings

def _number(value, default, low, high, whole):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    value = max(low, min(high, value))
    return int(round(value)) if whole else float(value)


def settings(config):
    """config.json "gps_check" checked, with defaults: {"enabled", "max_speed",
    "window_hours", "neighbours", "stale_minutes", "repeat_hours", "across_libraries",
    "estimate_places", "estimate_minutes"} (see DEFAULTS; numbers held to RANGES)."""
    stored = config.get("gps_check") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    result = dict(DEFAULTS)
    for key in _BOOLEANS:
        if isinstance(stored.get(key), bool):
            result[key] = stored[key]
    for key, (low, high) in RANGES.items():
        whole = isinstance(DEFAULTS[key], int) and key != "repeat_hours"
        result[key] = _number(stored.get(key), DEFAULTS[key], low, high, whole)
    return result


# ---------------------------------------------------------------------------
# Facts of one picture (pure)

def local_seconds(taken_at):
    """Seconds since 1970 of a naive 'YYYY-MM-DDTHH:MM:SS' (the camera's clock), or None."""
    if not isinstance(taken_at, str) or len(taken_at) < 19:
        return None
    try:
        moment = datetime.datetime.fromisoformat(taken_at[:19])
    except ValueError:
        return None
    return (moment - _EPOCH).total_seconds()


def offset_minutes(text):
    """'+02:00' / '-10:00' (images.tz_offset) as minutes east of UTC, or None."""
    match = _OFFSET_RE.match(text or "") if isinstance(text, str) else None
    if not match:
        return None
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    return -minutes if match.group(1) == "-" else minutes


def _first_number(value):
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def fix_time(gps, local=None):
    """UTC seconds (since 1970) of the GPS fix an EXIF GPS dict holds, or None.

    GPSTimeStamp is the time of the fix (UTC) and GPSDateStamp its day.
    Without a day, local (local_seconds of the picture) picks the day that
    puts the fix nearest the picture (UTC is 14 hours ahead to 12 behind).
    Some older phones write hundredths of a second as whole seconds.
    """
    if not isinstance(gps, dict):
        return None
    stamp = gps.get("GPSTimeStamp")
    if not isinstance(stamp, (list, tuple)) or len(stamp) != 3:
        return None
    parts = [_first_number(value) for value in stamp]
    if None in parts:
        return None
    hours, minutes, seconds = parts
    if seconds >= 60:
        seconds /= 100.0
    if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 61):
        return None
    of_day = hours * 3600 + minutes * 60 + seconds
    date = gps.get("GPSDateStamp")
    match = _DATE_RE.match(date) if isinstance(date, str) else None
    if match:
        try:
            day = datetime.datetime(int(match.group(1)), int(match.group(2)),
                                    int(match.group(3)))
        except ValueError:
            day = None
        if day is not None:
            return (day - _EPOCH).total_seconds() + of_day
    if local is None:
        return None
    best = None
    start = local - local % 86400
    for days in (-1, 0, 1):
        candidate = start + days * 86400 + of_day
        ahead = candidate - local
        if -14.1 * 3600 <= ahead <= 12.1 * 3600 and (best is None
                                                     or abs(ahead) < abs(best - local)):
            best = candidate
    return best


def speed_kmh(gps, camera=""):
    """The speed an EXIF GPS dict states, in km/h, or None.

    camera (images.camera): the makes of METRES_PER_SECOND_MAKES write
    metres per second where GPSSpeedRef says km/h.
    """
    if not isinstance(gps, dict):
        return None
    value = _first_number(gps.get("GPSSpeed"))
    if value is None or value < 0:
        return None
    ref = gps.get("GPSSpeedRef")
    ref = ref.strip().upper()[:1] if isinstance(ref, str) and ref.strip() else "K"
    if ref == "K" and camera_key(camera).split(" ", 1)[0] in METRES_PER_SECOND_MAKES:
        return value * 3.6
    return value * SPEED_FACTORS.get(ref, 1.0)


def accuracy_km(gps):
    """How far off the position may be by its own account (GPSHPositioningError), in km."""
    value = _first_number(gps.get("GPSHPositioningError")) if isinstance(gps, dict) else None
    return value / 1000.0 if value is not None and value > 0 else 0.0


def checkable(gps):
    """True when the GPS data says when or how well the position was measured.

    A source that saves neither a GPS time nor an accuracy (many cameras
    paired with a phone app, drones, videos) cannot be checked by itself.
    """
    if not isinstance(gps, dict):
        return False
    return any(gps.get(key) not in (None, "", [])
               for key in ("GPSTimeStamp", "GPSDateStamp", "GPSHPositioningError", "GPSDOP"))


def void_fix(gps):
    """True when the camera marked the fix as not valid (GPSStatus "V")."""
    status = gps.get("GPSStatus") if isinstance(gps, dict) else None
    return isinstance(status, str) and status.strip().upper() == "V"


def camera_key(camera):
    """A camera's name as compared: single spaces, any case."""
    return " ".join(str(camera or "").split()).casefold()


def _quarter(seconds):
    """(the whole quarter hour nearest seconds, how far it is)."""
    quarter = round(seconds / QUARTER) * QUARTER
    return quarter, seconds - quarter


def longitude_offsets(lon):
    """The offsets (minutes) a clock at this longitude may well have: the
    whole hours of lon / 15, give or take DOUBTFUL_MINUTES, in quarter hours."""
    middle = int(round(lon / 15.0)) * 60
    low = max(OFFSET_RANGE[0] // 60, middle - DOUBTFUL_MINUTES)
    high = min(OFFSET_RANGE[1] // 60, middle + DOUBTFUL_MINUTES)
    return set(range(low, high + 1, QUARTER // 60))


def doubtful_offset(offset, lon, gps, local, fix, stale_limit):
    """True when a picture's own offset (minutes) cannot be trusted as it is.

    A phone keeps the time zone it had until it learns the new one: after a
    flight its pictures may carry the offset of the day before. The tells:
    the offset is more than DOUBTFUL_HOURS from the one the longitude
    suggests; the GPS time (fix, UTC seconds) would be after the picture;
    or, with a GPS date but no time, that date is not the picture's UTC day.
    """
    if abs(offset / 60.0 - lon / 15.0) > DOUBTFUL_HOURS:
        return True
    utc = local - offset * 60
    if fix is not None:
        return utc - fix < -stale_limit
    date = gps.get("GPSDateStamp") if isinstance(gps, dict) else None
    match = _DATE_RE.match(date) if isinstance(date, str) else None
    if not match:
        return False
    try:
        day = (datetime.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
               - _EPOCH).total_seconds()
    except ValueError:
        return False
    return not day <= utc < day + 86400


# ---------------------------------------------------------------------------
# Words

def _round_km(km):
    if km >= 1000:
        return int(round(km, -2))
    if km >= 100:
        return int(round(km, -1))
    return int(round(km))


def duration_words(seconds):
    """"11 seconds", "3 minutes", "5 hours", "2 days"."""
    seconds = abs(seconds)
    if seconds < 90:
        count, unit = int(round(seconds)), "second"
    elif seconds < 120 * 60:
        count, unit = int(round(seconds / 60.0)), "minute"
    elif seconds < 48 * 3600:
        count, unit = int(round(seconds / 3600.0)), "hour"
    else:
        count, unit = int(round(seconds / 86400.0)), "day"
    return "%d %s%s" % (count, unit, "" if count == 1 else "s")


def reason_text(reason):
    """Why a position was set aside, in words (no full stop), from its reason dict."""
    if not isinstance(reason, dict):
        return ""
    kind = reason.get("kind")
    if kind == "owner":
        return "you chose to ignore %s" % ("a copy of it" if reason.get("copy") else "it")
    if kind == "void":
        return "the camera marked its GPS position as not valid"
    if kind == "stale":
        minutes = reason.get("minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            return ("its GPS position was measured %s%s before the picture was taken"
                    % ("at least " if reason.get("least") else "", duration_words(minutes * 60)))
        return "its GPS position was measured earlier than the picture was taken"
    if kind == "frozen":
        return ("the camera gave %d pictures taken over %s the same position while it said "
                "it was moving at %d km/h" % (reason.get("count") or 2,
                                              duration_words((reason.get("minutes") or 0) * 60),
                                              int(round(reason.get("speed") or 0))))
    if kind == "travel":
        when = "later" if reason.get("later") else "earlier"
        nearest = "taken %s %s" % (duration_words(reason.get("seconds") or 0), when)
        km = "{:,} km".format(_round_km(reason.get("km") or 0))
        count = reason.get("count") or 1
        if count <= 1:
            return "a picture %s is %s away" % (nearest, km)
        return ("%d pictures taken around the same time are far away: one %s is %s away"
                % (count, nearest, km))
    return ""


def place_text(columns):
    """"Town, Region, Country" of place columns (places.place_columns), or ""."""
    if not columns or not columns.get("place_country"):
        return ""
    return ", ".join(part for part in (columns.get("place_city"), columns.get("place_region"),
                                       columns.get("place_country")) if part)


# ---------------------------------------------------------------------------
# The check (pure)

Verdict = collections.namedtuple("Verdict", "ignored finding weak choice copy")
Verdict.__doc__ = """What the check says about a picture with a position.

ignored  True when the position is set aside (the owner's choice applied)
finding  the check's own reason (a dict, see reason_text) or None: what it
         decides without the owner's choice
weak     True for a weak position (see the module notes)
choice   the owner's choice that applies: "use", "ignore" or None
copy     True when that choice is not the picture's own but its copies'
"""


class _Item:
    __slots__ = ("id", "library", "camera", "local", "offset", "doubtful", "lat", "lon", "gps",
                 "speed", "hash", "dhash", "choice", "copy", "timed", "fix", "gps_offset",
                 "early", "finding", "weak", "lo", "hi", "mid", "clock_only", "tolerance",
                 "group", "spot", "key", "voter", "row")


class _Groups:
    """Union-find over item numbers."""

    def __init__(self, count):
        self.parent = list(range(count))

    def find(self, index):
        parent = self.parent
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(self, first, second):
        first, second = self.find(first), self.find(second)
        if first != second:
            self.parent[second] = first


def _nearest_offset(entries, local, reach):
    """The offset (minutes) of entries [(local, offset)] nearest local within reach, or None."""
    index = bisect.bisect_left(entries, (local - reach, -10 ** 9))
    best = None
    while index < len(entries) and entries[index][0] <= local + reach:
        if best is None or abs(entries[index][0] - local) < abs(best[0] - local):
            best = entries[index]
        index += 1
    return best[1] if best is not None else None


def _offsets_near(entries, local, reach):
    index = bisect.bisect_left(entries, (local - reach, -10 ** 9))
    found = set()
    while index < len(entries) and entries[index][0] <= local + reach:
        found.add(entries[index][1])
        index += 1
    return found


class Check:
    """The verdicts of evaluate(): verdicts {image id: Verdict} (pictures with a position)."""

    def __init__(self, verdicts, items, options):
        self.verdicts = verdicts
        self._items = items
        self._options = options

    def copies(self, image_id):
        """The ids of the pictures with a position that are copies of one shot with
        image_id (see the module notes; image_id first), or [] when it has none."""
        found = [item for item in self._items if item.id == image_id]
        if not found:
            return []
        group = found[0].group
        return [image_id] + sorted(item.id for item in self._items
                                   if item.group == group and item.id != image_id)

    def estimates(self, place_of):
        """{image id: place columns} for ignored pictures whose neighbours agree on a place.

        The pictures taken within estimate_minutes of an ignored one whose
        positions are kept and count as references (not weak, not copies of
        it; other libraries only with across_libraries) are asked
        place_of(row) -> place columns (or None when unknown); when there is
        at least one and all name the same town, region and country, that
        is the estimate (place_km None: no distance is claimed).
        """
        reach = self._options["estimate_minutes"] * 60.0
        across = self._options["across_libraries"]
        kept = sorted((item.mid, n) for n, item in enumerate(self._items)
                      if item.voter and not self.verdicts[item.id].ignored)
        mids = [mid for mid, _n in kept]
        labels = {}
        result = {}
        for item in self._items:
            if not self.verdicts[item.id].ignored or item.mid is None:
                continue
            start = bisect.bisect_left(mids, item.mid - reach)
            names = set()
            unknown = False
            for index in range(start, len(kept)):
                if mids[index] > item.mid + reach:
                    break
                other = self._items[kept[index][1]]
                if (other.group == item.group or other.clock_only != item.clock_only
                        or (not across and other.library != item.library)):
                    continue
                if other.id not in labels:
                    columns = place_of(other.row)
                    labels[other.id] = (None if not columns or not columns.get("place_country")
                                        else tuple(columns.get(key) or "" for key in (
                                            "place_city", "place_region", "place_country",
                                            "place_cc")))
                if labels[other.id] is None:
                    unknown = True
                    break
                names.add(labels[other.id])
            if len(names) == 1 and not unknown:
                city, region, country, cc = names.pop()
                result[item.id] = {"place_city": city, "place_region": region,
                                   "place_country": country, "place_cc": cc, "place_km": None}
        return result


def evaluate(rows, options):
    """Check the positions of pictures. Returns a Check (its verdicts: {id: Verdict}).

    rows: one dict per present picture of the libraries looked at (also
    those without a position: their time zones help to time the others)
    with id, library_id, taken_at, taken_source, tz_offset, camera, media,
    content_hash, dhash, lat, lon (None: no position), gps (the EXIF GPS
    dict, or None), software (the EXIF Software tag, or "") and choice
    (the owner's: "use", "ignore" or None).
    options: settings(). Pure: no database, no clock.
    """
    stale_limit = options["stale_minutes"] * 60.0
    items = []
    library_offsets = collections.defaultdict(list)
    camera_offsets = collections.defaultdict(list)
    for row in rows:
        local = local_seconds(row.get("taken_at"))
        timed = (local is not None and row.get("taken_source") in TRUSTED_SOURCES
                 # A video without a camera name is mostly a copy made by a
                 # program, dated when it was made.
                 and not (row.get("media") == "video" and not row.get("camera")))
        offset = offset_minutes(row.get("tz_offset")) if timed else None
        library = row.get("library_id")
        camera = camera_key(row.get("camera"))
        lat, lon = row.get("lat"), row.get("lon")
        positioned = world_map.valid_position(lat, lon)
        gps = row.get("gps") if positioned and isinstance(row.get("gps"), dict) else {}
        fix = fix_time(gps, local) if timed and positioned else None
        doubtful = (offset is not None and positioned
                    and doubtful_offset(offset, float(lon), gps, local, fix, stale_limit))
        if offset is not None and not doubtful:
            library_offsets[library].append((local, offset))
            camera_offsets[(library, camera)].append((local, offset))
        if not positioned:
            continue
        item = _Item()
        item.row = row
        item.id = row["id"]
        item.library = library
        item.camera = camera
        item.local = local if timed else None
        item.offset = offset
        item.doubtful = doubtful
        item.lat, item.lon = float(lat), float(lon)
        item.gps = gps
        item.speed = speed_kmh(gps, row.get("camera"))
        item.hash = row.get("content_hash") or ""
        item.dhash = row.get("dhash") or ""
        item.choice = row.get("choice") if row.get("choice") in CHOICES else None
        item.copy = False
        item.timed = timed
        item.fix = fix
        item.gps_offset = None
        if fix is not None and (offset is None or doubtful):
            quarter, off_by = _quarter(local - fix)
            if abs(off_by) <= CLOCK_SLACK and OFFSET_RANGE[0] <= quarter <= OFFSET_RANGE[1]:
                item.gps_offset = int(quarter // 60)
                camera_offsets[(library, camera)].append((local, item.gps_offset))
                library_offsets[library].append((local, item.gps_offset))
        # A date from the name of a file a program wrote is when it was
        # written: the picture may have been taken before.
        item.early = (EXPORT_SECONDS if timed and row.get("taken_source") == "filename"
                      and row.get("software") else 0)
        item.finding = None
        item.weak = False
        item.mid = item.lo = item.hi = None
        item.clock_only = False
        item.tolerance = BASE_TOLERANCE_KM + min(accuracy_km(gps), ACCURACY_CAP_KM)
        item.spot = item.key = None
        item.voter = False
        item.group = ("alone", len(items))
        items.append(item)
    for entries in list(library_offsets.values()) + list(camera_offsets.values()):
        entries.sort()
    every_offset = sorted(entry for entries in library_offsets.values() for entry in entries)
    zones = (library_offsets, every_offset, camera_offsets)

    _same_positions(items)
    if options["enabled"]:
        _intrinsic(items, zones, stale_limit, options)
    timed = [item for item in items if item.timed]
    for item in timed:
        _time_interval(item, zones, options)
    _copy_groups(timed)
    _shared_choices(timed)
    if options["enabled"]:
        _repeats(timed, options)
    for item in timed:
        item.voter = (item.choice == "use" or (item.choice != "ignore" and item.finding is None
                                               and not item.weak))
    if options["enabled"]:
        _travel(timed, options)
    verdicts = {}
    for item in items:
        ignored = item.choice == "ignore" or (item.choice != "use" and item.finding is not None)
        verdicts[item.id] = Verdict(ignored, item.finding, item.weak, item.choice,
                                    item.copy and item.choice is not None)
    return Check(verdicts, items, options)


def _candidate_offsets(item, zones, options):
    """The offsets (minutes) a picture's clock may have had when its own is unknown or
    in doubt: the same camera's within the hour and the library's within 36 hours
    (or any library's), and with an offset in doubt that one and the longitude's."""
    library_offsets, every_offset, camera_offsets = zones
    offsets = set()
    borrowed = _nearest_offset(camera_offsets.get((item.library, item.camera), ()),
                               item.local, CAMERA_OFFSET_SECONDS)
    if borrowed is not None:
        offsets.add(borrowed)
    near = _offsets_near(library_offsets.get(item.library, ()), item.local,
                         LIBRARY_OFFSET_SECONDS)
    if not near and options["across_libraries"]:
        near = _offsets_near(every_offset, item.local, LIBRARY_OFFSET_SECONDS)
    offsets |= near
    if item.doubtful:
        offsets.add(item.offset)
        offsets |= longitude_offsets(item.lon)
    return offsets


def _intrinsic(items, zones, stale_limit, options):
    """The void, stale and frozen rules (findings on the items)."""
    # How often each camera's fixes are fresh (see FRESH_SHARE).
    timed_fixes = collections.Counter()
    fresh_fixes = collections.Counter()
    for item in items:
        if item.fix is None:
            continue
        timed_fixes[item.camera] += 1
        if item.offset is not None and not item.doubtful:
            age = item.local - item.offset * 60 - item.fix
            fresh = -stale_limit <= age <= stale_limit
        else:
            fresh = item.gps_offset is not None
        if fresh:
            fresh_fixes[item.camera] += 1
    for item in items:
        if void_fix(item.gps):
            item.finding = {"kind": "void"}
            continue
        if item.fix is None:
            continue
        fresh = fresh_fixes[item.camera]
        if fresh < FRESH_MIN or fresh < FRESH_SHARE * timed_fixes[item.camera]:
            continue            # this camera's GPS times do not follow its clock
        if item.offset is not None and not item.doubtful:
            offsets = {item.offset}
        elif item.gps_offset is not None:
            continue            # (a whole quarter hour from the clock: fresh)
        else:
            offsets = _candidate_offsets(item, zones, options)
        if offsets:
            age = min(item.local - offset * 60 - item.fix for offset in offsets)
        else:
            # No offset known at all: the youngest the fix can be.
            age = item.local - OFFSET_RANGE[1] - item.fix
        if age <= stale_limit:
            continue            # (fresh with some offset the picture may have had)
        moved = item.speed * age / 3600.0 if item.speed is not None else None
        if age <= STALE_ANYWAY_SECONDS and (moved is None or moved <= STALE_MOVED_KM):
            continue            # (a few minutes old, and it says it stood still)
        item.finding = {"kind": "stale", "minutes": int(round(age / 60.0))}
        if len(offsets) != 1:
            item.finding["least"] = True
    # Frozen: one moving fix given to pictures minutes apart.
    moving = collections.defaultdict(list)
    for item in items:
        if item.local is not None and item.speed is not None and item.speed >= FROZEN_SPEED:
            moving[(item.spot, round(item.speed, 3))].append(item)
    for (_spot, speed), group in moving.items():
        if len(group) < 2:
            continue
        first = min(item.local for item in group)
        span = max(item.local for item in group) - first
        for item in group:
            # (The first shots may still have been near the fix.)
            if item.finding is None and (item.local - first) / 3600.0 * speed > item.tolerance:
                item.finding = {"kind": "frozen", "minutes": int(round(span / 60.0)),
                                "speed": int(round(speed)), "count": len(group)}


def _time_interval(item, zones, options):
    """The earliest and latest UTC the picture may have been taken at (lo, hi, mid)."""
    library_offsets, every_offset, camera_offsets = zones
    offsets = set()
    if item.offset is not None and not item.doubtful:
        offsets.add(item.offset)
    elif item.gps_offset is not None and item.finding is None:
        offsets.add(item.gps_offset)
    elif not item.doubtful:
        borrowed = _nearest_offset(camera_offsets.get((item.library, item.camera), ()),
                                   item.local, CAMERA_OFFSET_SECONDS)
        if borrowed is not None:
            offsets.add(borrowed)
    if item.doubtful:
        offsets.add(item.offset)
        offsets |= longitude_offsets(item.lon)
    if not offsets:
        offsets = _offsets_near(library_offsets.get(item.library, ()), item.local,
                                LIBRARY_OFFSET_SECONDS)
    if not offsets and options["across_libraries"]:
        offsets = _offsets_near(every_offset, item.local, LIBRARY_OFFSET_SECONDS)
    item.clock_only = not offsets
    if not offsets:
        # No time zone anywhere near: such pictures only meet each other,
        # and are compared by their clocks as they are.
        offsets = {0}
    item.lo = item.local - max(offsets) * 60
    item.hi = item.local - min(offsets) * 60
    # (mid: when it was most likely taken; an exported file's name reaches
    # back only for the interval.)
    item.mid = (item.lo + item.hi) / 2.0
    item.lo -= item.early


def _same_positions(items):
    """item.spot (and item.key): one value for the pictures of one camera at one position.

    Positions at most SAME_POSITION_DEGREES apart are the same (and so are
    their chains); exact repeats are joined first, so a camera that gave
    one position to a thousand pictures costs no more than one picture.
    """
    groups = _Groups(len(items))
    exact = {}
    # (camera, row, column) -> {a group's number when the first of its
    # positions in the cell came: those positions}. A group already joined
    # is not looked through again, so a camera whose fix wanders within a
    # metre over thousands of pictures costs no more than a few.
    cells = collections.defaultdict(dict)
    step = SAME_POSITION_DEGREES
    for index, item in enumerate(items):
        key = (item.camera, item.lat, item.lon)
        if key in exact:
            groups.union(exact[key], index)
            continue
        exact[key] = index
        row, column = int(math.floor(item.lat / step)), int(math.floor(item.lon / step))
        for near_row in (row - 1, row, row + 1):
            for near_column in (column - 1, column, column + 1):
                cell = cells.get((item.camera, near_row, near_column))
                for root, members in (cell or {}).items():
                    if groups.find(root) == groups.find(index):
                        continue
                    for other in members:
                        if (abs(items[other].lat - item.lat) <= step
                                and abs(items[other].lon - item.lon) <= step):
                            groups.union(other, index)
                            break
        cells[(item.camera, row, column)].setdefault(groups.find(index), []).append(index)
    for index, item in enumerate(items):
        item.spot = (item.camera, groups.find(index))
        item.key = item.spot


def _copy_groups(items):
    """item.group: the same value for copies of one shot (see the module notes)."""
    groups = _Groups(len(items))
    first = {}
    for index, item in enumerate(items):
        keys = [("t", item.camera, item.local)]
        if item.hash:
            keys.append(("h", item.hash))
        for key in keys:
            if key in first:
                groups.union(first[key], index)
            else:
                first[key] = index
    by_dhash = collections.defaultdict(list)
    for index, item in enumerate(items):
        if item.dhash and item.dhash.strip("0"):
            by_dhash[item.dhash].append(index)
    for members in by_dhash.values():
        members.sort(key=lambda index: items[index].local)
        for position in range(1, len(members)):
            if (items[members[position]].local - items[members[position - 1]].local
                    <= COPY_SECONDS):
                groups.union(members[position - 1], members[position])
    for index, item in enumerate(items):
        item.group = ("copies", groups.find(index))


def _shared_choices(items):
    """A copy of a shot without a choice of its own follows its copies' (when they agree)."""
    chosen = collections.defaultdict(set)
    for item in items:
        if item.choice is not None:
            chosen[item.group].add(item.choice)
    for item in items:
        if item.choice is None and len(chosen.get(item.group, ())) == 1:
            item.choice = next(iter(chosen[item.group]))
            item.copy = True


def _repeats(items, options):
    """item.weak: an uncheckable source gave this exact position to shots hours apart.

    Only a camera without a GPS receiver of its own (fewer than FRESH_MIN of
    its pictures can be checked), and only a run of its pictures at one
    position: no picture of the camera more than BASE_TOLERANCE_KM away
    in between, and no gap longer than REPEAT_GAP_SECONDS (a phone's
    videos at home, years apart, are not a last known position).
    """
    reach = options["repeat_hours"] * 3600.0
    receivers = collections.Counter(item.camera for item in items if checkable(item.gps))
    by_camera = collections.defaultdict(list)
    for item in items:
        if receivers[item.camera] < FRESH_MIN:
            by_camera[item.camera].append(item)

    def close(run):
        unchecked = [member for member in run if not checkable(member.gps)]
        if (len({member.group for member in unchecked}) >= 2
                and unchecked[-1].local - unchecked[0].local > reach):
            for member in unchecked:
                member.weak = True

    for members in by_camera.values():
        members.sort(key=lambda item: item.local)
        runs = {}               # spot -> its open run (in time order)
        for item in members:
            for spot, run in list(runs.items()):
                last = run[-1]
                if (item.local - last.local > REPEAT_GAP_SECONDS
                        or (spot != item.spot and places.distance_km(
                            last.lat, last.lon, item.lat, item.lon) > BASE_TOLERANCE_KM)):
                    close(run)
                    del runs[spot]
            runs.setdefault(item.spot, []).append(item)
        for run in runs.values():
            close(run)


def _contradicts(item, other, max_speed):
    """(contradicts, km, seconds between their middle times) of two pictures."""
    gap = max(other.hi - item.lo, item.hi - other.lo) + GAP_SLACK
    km = places.distance_km(item.lat, item.lon, other.lat, other.lon)
    needed = max(0.0, km - item.tolerance - other.tolerance) / (gap / 3600.0)
    return needed > max_speed, km, other.mid - item.mid


def _runs(voters):
    """(start, end, box, groups) per voter (in mid order): its run of neighbours of one
    camera at one position in one library, the box its positions are in
    (lowest lat, highest lat, lowest lon, highest lon) and the copy groups
    in it (one set per run)."""
    result = [None] * len(voters)
    start = 0
    for index in range(1, len(voters) + 1):
        if index < len(voters) and (voters[index].key, voters[index].library) == (
                voters[start].key, voters[start].library):
            continue
        run = voters[start:index]
        box = (min(member.lat for member in run), max(member.lat for member in run),
               min(member.lon for member in run), max(member.lon for member in run))
        entry = (start, index - 1, box, frozenset(member.group for member in run))
        for position in range(start, index):
            result[position] = entry
        start = index
    return result


def _travel(items, options):
    """The travel rule: findings for pictures the nearest references contradict."""
    voters = sorted((item for item in items if item.voter), key=lambda item: item.mid)
    mids = [item.mid for item in voters]
    runs = _runs(voters)
    window = options["window_hours"] * 3600.0
    wanted = options["neighbours"]
    max_speed = options["max_speed"]
    across = options["across_libraries"]
    for item in items:
        if item.finding is not None:
            continue
        right = bisect.bisect_left(mids, item.mid)
        left = right - 1
        seen_groups = set()
        seen_keys = set()
        votes = []
        examined = 0
        while len(votes) < wanted:
            # The next picture outwards in time, whichever side is nearer.
            left_gap = item.mid - mids[left] if left >= 0 else None
            right_gap = mids[right] - item.mid if right < len(mids) else None
            if left_gap is None and right_gap is None:
                break
            if right_gap is None or (left_gap is not None and left_gap <= right_gap):
                index, gap, step = left, left_gap, -1
                left -= 1
            else:
                index, gap, step = right, right_gap, 1
                right += 1
            if gap > window:
                break           # (the nearer side: everything further is too)
            other = voters[index]
            examined += 1
            if examined > SCAN_LIMIT:
                votes = None    # (too many alike to look through: no judgement)
                break
            start, end, box, groups = runs[index]
            skip_run = False    # (the rest of its run would be skipped too: jump over it)
            if other.clock_only != item.clock_only:
                continue
            if other.group == item.group or other.group in seen_groups:
                continue
            if not across and other.library != item.library:
                skip_run = True
            elif (abs(other.lat - item.lat) <= SAME_POSITION_DEGREES
                    and abs(other.lon - item.lon) <= SAME_POSITION_DEGREES):
                # (The same fix: no second opinion; nor the rest of the run
                # when all of it is.)
                skip_run = (box[1] - item.lat <= SAME_POSITION_DEGREES
                            and item.lat - box[0] <= SAME_POSITION_DEGREES
                            and box[3] - item.lon <= SAME_POSITION_DEGREES
                            and item.lon - box[2] <= SAME_POSITION_DEGREES)
            else:
                seen_groups.add(other.group)
                if other.key not in seen_keys:
                    seen_keys.add(other.key)
                    votes.append(_contradicts(item, other, max_speed))
                    continue
                seen_groups.update(groups)
                skip_run = True
            if skip_run:
                if step < 0:
                    left = min(left, start - 1)
                else:
                    right = max(right, end + 1)
        if votes is None:
            continue
        against = [vote for vote in votes if vote[0]]
        needed = 1 if item.weak else 2
        if len(against) * 2 > len(votes) and len(against) >= needed:
            _contradicted, km, seconds = min(against, key=lambda vote: abs(vote[2]))
            item.finding = {"kind": "travel", "seconds": int(round(abs(seconds))),
                            "later": seconds > 0, "km": round(km, 1), "count": len(against),
                            "of": len(votes)}


# ---------------------------------------------------------------------------
# The database

def content_key(content_hash, image_id):
    """What the owner's choice about a picture is kept under: its content hash (or its id)."""
    return content_hash or "#%d" % image_id


def choices(conn):
    """{content key: "use" or "ignore"} of the gps_choices table."""
    return {row[0]: row[1] for row in conn.execute(
        "SELECT content_key, choice FROM gps_choices")}


# A position set aside: its gps_checks row says so and the picture has no
# position of its own (a row left from before the file was read again
# never counts).
_IGNORED_SQL = ("SELECT g.image_id FROM gps_checks AS g JOIN images AS i ON i.id = g.image_id "
                "WHERE g.ignored = 1 AND i.lat IS NULL AND i.missing_since IS NULL")


def ignored_ids(conn, library_ids=None):
    """The ids of the present pictures whose position is set aside (of some libraries)."""
    sql = _IGNORED_SQL
    params = []
    if library_ids is not None:
        library_ids = list(library_ids)
        if not library_ids:
            return set()
        sql += " AND i.library_id IN (%s)" % ",".join("?" * len(library_ids))
        params = library_ids
    return {row[0] for row in conn.execute(sql, params)}


def is_ignored(conn, image_id):
    """True when the position of a picture is set aside."""
    return conn.execute(_IGNORED_SQL + " AND i.id = ?", (image_id,)).fetchone() is not None


def _reason_dict(text):
    """A gps_checks reason: the check's finding (kind, ...; {} when none) and "copy": the
    choice of its copies that applies to it."""
    try:
        reason = json.loads(text or "{}")
    except ValueError:
        return {}
    return reason if isinstance(reason, dict) else {}


def picture_view(conn, row, index=None):
    """What the admin's viewer shows about the GPS check of a picture (an images row), or None.

    None for a picture without a position. Else {"ignored": the position
    is set aside, "reason": why, in words ("" when the check finds nothing),
    "choice": the owner's that applies ("use", "ignore" or "": its own, or
    its copies'), "place": the place of the position set aside ("" when it
    is not, or there are no place names), "estimated": its place is
    estimated}.
    """
    check = conn.execute("SELECT lat, lon, ignored, reason FROM gps_checks WHERE image_id = ?",
                         (row["id"],)).fetchone()
    positioned = world_map.valid_position(row["lat"], row["lon"])
    if check is None and not positioned:
        return None
    if check is not None and positioned and (check["ignored"] or (
            check["lat"], check["lon"]) != (row["lat"], row["lon"])):
        check = None            # (read again since: the next check decides)
    own = conn.execute("SELECT choice FROM gps_choices WHERE content_key = ?",
                       (content_key(row["content_hash"], row["id"]),)).fetchone()
    own = own[0] if own is not None else ""
    stored = _reason_dict(check["reason"]) if check is not None else {}
    finding = stored if stored.get("kind") in REASON_KINDS else None
    copied = stored.get("copy") if stored.get("copy") in CHOICES else ""
    choice = own or copied
    ignored = bool(check is not None and check["ignored"])
    if ignored and (own == "ignore" or (not own and copied == "ignore" and finding is None)):
        reason = reason_text({"kind": "owner", "copy": own != "ignore"})
    else:
        reason = reason_text(finding) if finding else ""
    place = ""
    if ignored:
        index = index if index is not None else places.shared()
        if index is not None:
            place = place_text(places.place_columns(check["lat"], check["lon"], index))
    return {"ignored": ignored, "reason": reason, "choice": choice, "place": place,
            "estimated": bool(ignored and row["place_country"])}


def file_read_again(conn, image_id, values):
    """The scanner read a picture's file again and is about to write values (images
    columns, with lat, lon and the place_* columns).

    A position the check set aside that the file still gives stays set aside
    (values change: no position, no place) until the next check decides
    again; any other position, or none, drops the picture's gps_checks row,
    so that the next check decides about what the file gives now.
    """
    try:
        row = conn.execute("SELECT lat, lon, ignored FROM gps_checks WHERE image_id = ?",
                           (image_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return              # (a database from before the GPS check)
        raise
    if row is None:
        return
    lat, lon = values.get("lat"), values.get("lon")
    if (row[2] and world_map.valid_position(lat, lon)
            and abs(lat - row[0]) <= 1e-9 and abs(lon - row[1]) <= 1e-9):
        values.update(lat=None, lon=None)
        values.update(places.empty_columns())
    else:
        conn.execute("DELETE FROM gps_checks WHERE image_id = ?", (image_id,))


_ROWS_SQL = """
    SELECT i.id, i.library_id, i.taken_at, i.taken_source, i.tz_offset, i.camera, i.media,
        i.content_hash, i.dhash, i.lat, i.lon, i.place_city, i.place_region, i.place_country,
        i.place_cc, i.place_km,
        CASE WHEN i.lat IS NOT NULL OR g.image_id IS NOT NULL THEN i.exif END AS exif,
        g.image_id AS checked, g.lat AS raw_lat, g.lon AS raw_lon, g.ignored AS was_ignored,
        g.reason AS old_reason
    FROM images AS i LEFT JOIN gps_checks AS g ON g.image_id = i.id
    WHERE i.missing_since IS NULL AND i.library_id IN (%s)"""

_PLACE_COLUMNS = ("place_city", "place_region", "place_country", "place_cc", "place_km")


def _facts_of(exif_text):
    """{"gps": the GPS dict of an images.exif text (or None), "software": its Software
    tag (or "")}."""
    facts = {"gps": None, "software": ""}
    if not exif_text:
        return facts
    try:
        exif = json.loads(exif_text, parse_constant=lambda _name: None)
    except ValueError:
        return facts
    if isinstance(exif, dict):
        if isinstance(exif.get("GPS"), dict):
            facts["gps"] = exif["GPS"]
        if isinstance(exif.get("Software"), str):
            facts["software"] = exif["Software"].strip()
    return facts


def _library_ids(config):
    return [library.get("id") for library in (config or {}).get("libraries", [])
            if isinstance(library, dict) and library.get("id")]


def _load(conn, library_ids):
    """The rows evaluate() wants (with the database facts run() needs beside them)."""
    owner = choices(conn)
    rows = []
    for row in conn.execute(_ROWS_SQL % ",".join("?" * len(library_ids)), library_ids):
        item = dict(row)
        item["shown_lat"], item["shown_lon"] = row["lat"], row["lon"]
        if row["lat"] is None and row["checked"] is not None and row["was_ignored"]:
            # Set aside: the position the file gives is kept in gps_checks.
            item["lat"], item["lon"] = row["raw_lat"], row["raw_lon"]
        item.update(_facts_of(row["exif"] if item["lat"] is not None else None))
        item["choice"] = owner.get(content_key(row["content_hash"], row["id"]))
        rows.append(item)
    return rows


def _log(ctx, message, level="info"):
    if ctx is not None:
        ctx.log(message, level)


_KIND_WORDS = (
    ("travel", "that do not match pictures taken around the same time",
     "that does not match pictures taken around the same time"),
    ("stale", "with a GPS fix older than the picture", None),
    ("frozen", "with a GPS fix that stood still while moving", None),
    ("void", "the camera marked as not valid", None),
    ("owner", "you chose to ignore", None),
)


def summary_text(summary):
    """The log line of a run (see run)."""
    kinds = summary["ignored_by"]
    parts = ["{:,} {}".format(kinds[kind], one if kinds[kind] == 1 and one else words)
             for kind, words, one in _KIND_WORDS if kinds.get(kind)]
    checked = summary["checked"]
    text = "GPS check of {:,} picture{} with a position: ".format(
        checked, "" if checked == 1 else "s")
    if summary["ignored"]:
        text += "ignored {:,}".format(summary["ignored"])
        if parts:
            text += " (" + ", ".join(parts) + ")"
    else:
        text += "none ignored"
    changes = []
    if summary["newly_ignored"]:
        changes.append("{:,} newly ignored".format(summary["newly_ignored"]))
    if summary["restored"]:
        changes.append("{:,} used again".format(summary["restored"]))
    if summary["estimated"]:
        changes.append("{:,} with an estimated place".format(summary["estimated"]))
    if changes:
        text += "; " + ", ".join(changes)
    text += "."
    if summary["ignored"]:
        text += " The admin finds them with is:gps-ignored."
    return text


def run(conn, config, ctx=None, now=None, library_ids=None, changed=None):
    """Check the GPS positions of the present pictures and set aside those that cannot be right.

    Every library of config is looked at (enabled or not; library_ids
    limits it, for tests) -- the whole check takes well under a second
    for ten thousand pictures. Positions set aside get their gps_checks
    row and lose their lat, lon and place columns; positions the check
    (or the owner) no longer sets aside get them back (the place again
    from the place names, or left to be looked up when there are none).
    Everything is written in one transaction. ctx (optional) is told the
    summary; changed (a set, optional) gets the ids of the pictures whose
    position or place changed. Returns {"checked", "ignored", "ignored_by":
    {kind: count}, "newly_ignored", "restored", "estimated", "seconds"}.
    """
    started = time.perf_counter()
    options = settings(config)
    if library_ids is None:
        library_ids = _library_ids(config)
    summary = {"checked": 0, "ignored": 0, "ignored_by": {}, "newly_ignored": 0,
               "restored": 0, "estimated": 0, "seconds": 0.0}
    if not library_ids:
        return summary
    now = time.time() if now is None else now
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = _load(conn, list(library_ids))
        check = evaluate(rows, options)
        estimates = {}
        if options["estimate_places"]:
            estimates = check.estimates(
                lambda row: {key: row[key] for key in _PLACE_COLUMNS}
                if row["shown_lat"] is not None and row["place_km"] is not None else None)
        index = None
        counts = collections.Counter()
        for row in rows:
            verdict = check.verdicts.get(row["id"])
            if verdict is None:
                if row["checked"] is not None:
                    # (No position to keep aside: a row left from before.)
                    conn.execute("DELETE FROM gps_checks WHERE image_id = ?", (row["id"],))
                continue
            summary["checked"] += 1
            if verdict.ignored:
                summary["ignored"] += 1
                counts["owner" if verdict.choice == "ignore" else verdict.finding["kind"]] += 1
            keep_row = verdict.finding is not None or verdict.choice == "ignore"
            stored = dict(verdict.finding or {})
            if verdict.copy:
                stored["copy"] = verdict.choice
            reason = json.dumps(stored, sort_keys=True)
            raw = (row["lat"], row["lon"])
            if keep_row and (row["checked"] is None or row["raw_lat"] != raw[0]
                             or row["raw_lon"] != raw[1] or bool(row["was_ignored"])
                             != verdict.ignored or row["old_reason"] != reason):
                conn.execute("INSERT OR REPLACE INTO gps_checks (image_id, lat, lon, ignored, "
                             "reason, updated) VALUES (?, ?, ?, ?, ?, ?)",
                             (row["id"], raw[0], raw[1], int(verdict.ignored), reason, now))
            elif not keep_row and row["checked"] is not None:
                conn.execute("DELETE FROM gps_checks WHERE image_id = ?", (row["id"],))
            shown = row["shown_lat"] is not None
            if verdict.ignored:
                place = estimates.get(row["id"]) or places.empty_columns()
                if place.get("place_country"):
                    summary["estimated"] += 1
                if shown:
                    summary["newly_ignored"] += 1
                if shown or any(row[key] != place[key] for key in _PLACE_COLUMNS):
                    conn.execute(
                        "UPDATE images SET lat = NULL, lon = NULL, place_city = ?, "
                        "place_region = ?, place_country = ?, place_cc = ?, place_km = ?, "
                        "updated = ? WHERE id = ?",
                        [place[key] for key in _PLACE_COLUMNS] + [now, row["id"]])
                    if changed is not None:
                        changed.add(row["id"])
            elif not shown:
                if index is None:
                    index = places.shared() or False
                place = (places.place_columns(raw[0], raw[1], index) if index
                         else places.empty_columns())
                conn.execute(
                    "UPDATE images SET lat = ?, lon = ?, place_city = ?, place_region = ?, "
                    "place_country = ?, place_cc = ?, place_km = ?, updated = ? WHERE id = ?",
                    [raw[0], raw[1]] + [place[key] for key in _PLACE_COLUMNS]
                    + [now, row["id"]])
                summary["restored"] += 1
                if changed is not None:
                    changed.add(row["id"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    summary["ignored_by"] = dict(counts)
    summary["seconds"] = round(time.perf_counter() - started, 3)
    if summary["checked"]:
        _log(ctx, summary_text(summary))
    return summary


def run_safely(conn, config, ctx=None):
    """run() for a job that may have been stopped: a busy database is only a warning
    in the log (and the answer None)."""
    try:
        return run(conn, config, ctx)
    except sqlite3.OperationalError as error:
        _log(ctx, "The GPS check could not run (%s); it runs again after the next scan."
             % error, "warn")
        return None


def set_choice(conn, config, image_id, use, now=None):
    """The owner's choice about a picture's position: use it (True) or not (False).

    The choice is made for every copy of the shot with a position (see
    Check.copies). For each it is kept (by content, see content_key) only
    while it differs from what the check finds about that picture;
    choosing what the check decides anyway removes it. Does not apply it:
    run() does. Returns the ids it was made for (image_id first), or []
    when image_id is no present picture of config's libraries with a
    position (or one set aside).
    """
    library_ids = _library_ids(config)
    if not library_ids:
        return []
    rows = _load(conn, library_ids)
    check = evaluate(rows, settings(config))
    ids = check.copies(image_id)
    by_id = {row["id"]: row for row in rows}
    stamp = time.time() if now is None else now
    for member in ids:
        key = content_key(by_id[member]["content_hash"], member)
        checked = check.verdicts[member].finding is not None
        wanted = ("use" if checked else None) if use else (None if checked else "ignore")
        if wanted is None:
            conn.execute("DELETE FROM gps_choices WHERE content_key = ?", (key,))
        else:
            conn.execute("INSERT OR REPLACE INTO gps_choices (content_key, choice, updated) "
                         "VALUES (?, ?, ?)", (key, wanted, stamp))
    return ids
