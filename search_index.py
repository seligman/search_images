#!/usr/bin/env python3
"""Compact search records for the search page, and near-duplicate groups.

The search page searches in the browser (static/search-core.js), in the
server and in a static export alike: it downloads one small record per
picture. build_records() makes those records. The server sends them from
/api/index through IndexService, with a strong ETag, gzip and 304 answers,
so reloading the page costs one tiny request while nothing has changed.

Record keys are short to keep the download small. A key is left out when
its value is empty, zero, or the "nothing" value noted below:

    i    image id                         l    library id
    n    file name (server; an export only when file names are allowed)
    f    folder: the folder part of the relative path, "/" separators
         (server; an export only when allowed); left out at the top level
    t    title                            d    description
    s    subjects []                      g    tags [] (lower case, unique)
    o    text in the picture (OCR)        a    animals []
    fo   food []                          v    vehicles []
    pd   people descriptors []            sc   scene
    st   setting (not "unknown")          td   time of day (not "unknown")
    ev   event (not "none")               ty   photo type
    dt   document type (not "none")       c    colors [] (lower case)
    pp   people: "none", "1", "2", "3-5", "6-10" or "crowd" -- "none" is
         kept, because "no people" is a real answer; missing when the
         picture is not described, or only by the older (v4) prompt
    ex   extended-profile words: object names, landmarks, activities, mood
         (not "neutral") and the location guess, joined with ", "
         (landmarks and location guess in an export only with location)
    q    aesthetic score 1-5              sh   sharpness score 1-5
    qf   quality flags []                 se   sensitive (not "none")
    pr   1 when the picture shows private information
    dk   date taken "YYYY-MM-DD" (an export: only from the camera or the
         file name, never from the file's time)
    ts   date and time taken (server)
    cm   camera                           gp   1 when the picture has a GPS
                                               position (never the position
                                               itself; an export only with
                                               location)
    w    displayed width                  hgt  displayed height
    p    "360" for a 360-degree photo
    dg   duplicate group: the id of the group's cover picture, on every
         member of a group of two or more (see duplicate_groups; the cover
         is chosen by cover_key)
    h    1 for a hidden picture (server, for the admin only; never exported)
    ds   1 when described                 v4   1 when the description comes
                                               from the older version 4
                                               prompt (only d, s, o, g and c)

Values are what the describe step stored (enums such as "sunrise_sunset"
or "card_or_id" as they are); tags and colors are lower-cased here because
older descriptions were not.

This module uses only the standard library.
"""

import collections
import datetime
import gzip
import hashlib
import heapq
import itertools
import json
import math
import os
import re
import sys
import threading
import time

import common
import db
import imageinfo
import prompts

# The shape of the records and of the /api/index answer. Raise it when either
# changes, so browsers do not keep using an older index.
INDEX_VERSION = 1
# Raise when duplicate_groups() or the groups' inputs (see build_records)
# decide differently, so cached groups are worked out again.
GROUPS_VERSION = 4
# dHashes (64 bits) at most this many bits apart are near-duplicates, when
# both pictures have a capture date to trust and were taken at about the
# same time (see SAME_TIME_SECONDS) ...
DUPLICATE_THRESHOLD = 8
# ... and at most this many when either picture's date cannot be trusted
# (it comes from the file's modification time, or there is none). Old
# camera photos of quite different things (a guitar, a sunset, a furnace)
# can be 7 or 8 bits apart, while bursts and re-saved or edited copies are
# nearly always much closer, and taken at the same time.
UNDATED_THRESHOLD = 6
# ... and at most this many when both pictures have a trusted capture time
# and were taken at most BURST_SECONDS apart: the shots of a burst or an
# exposure bracket differ more than copies do (the subject moved, the
# light changed), but pictures taken within the same two seconds that
# look this much alike are the same shot.
BURST_THRESHOLD = 12
BURST_SECONDS = 2
# A second in which this many groups have started already is no burst: a
# camera whose clock was never set gives every picture the same time.
BURST_GROUPS = 8
# Where a trusted capture date comes from (the taken_source column): the
# camera's EXIF data or a date in the file name. A file's modification time
# changes when it is copied or edited, so it says little.
RELIABLE_DATE_SOURCES = frozenset(("exif", "filename"))
# Two pictures with trusted dates are near-duplicates only when they were
# taken on the same calendar day, or at most this many seconds apart (a
# burst across midnight).
SAME_TIME_SECONDS = 12 * 3600
# A dHash with fewer set bits than this (or more than 64 minus this) comes
# from a picture with little detail: black, white, a plain sky or wall, or
# a page of text on white (9 to 20 bits; photos have about 20 to 45).
# Unrelated pictures like that have nearly the same dHash, so they are only
# grouped when they are exact copies.
LOW_DETAIL_BITS = 12
# Pages of text, screenshots and diagrams look alike to a dHash even when
# they say different things, so described pictures of these kinds (or with
# a document type) are only grouped as exact copies.
EXACT_ONLY_TYPES = frozenset(("document", "scan", "screenshot", "diagram"))
# How many group representatives one picture is compared with at most in
# one banded index (see _BandIndex): this many, or twice as many as evenly
# spread hashes bring up (about 210 in an index of 100,000 groups),
# whichever is more. So normal photos never reach it, while a library of
# very many alike, low-detail hashes does not take quadratic time. The
# nearest candidates are looked at first (see _probes).
MAX_CANDIDATES = 256
# The groups of one capture day are compared one by one with a new picture
# of about that day, until the day has this many; then the day gets a
# banded index of its own.
DAY_INDEX_SIZE = 64
# How many built /api/index answers IndexService keeps (one per ETag).
MEMO_SIZE = 4
# A fast setting: the answer is built again after every change, and its
# size matters little on a local network.
GZIP_LEVEL = 3
# How many per-library record lists IndexService keeps.
LIBRARY_MEMO_SIZE = 64

_DHASH_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_MOMENT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NOT_DIGITS_RE = re.compile(r"\D")
# A UTF-16 surrogate on its own (older releases could store one, e.g. half
# of an emoji cut off); it cannot be written as UTF-8.
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")

# (record key, description field) for lists of short phrases.
_LIST_FIELDS = (("s", "subjects"), ("a", "animals"), ("fo", "food"), ("v", "vehicles"),
                ("pd", "people_descriptors"), ("qf", "quality_flags"))
# Lists that are lower-cased and made unique (older descriptions were not).
_LOWER_LIST_FIELDS = (("g", "tags"), ("c", "colors"))
# (record key, description field, the value that means "nothing").
_ENUM_FIELDS = (("st", "setting", "unknown"), ("td", "time_of_day", "unknown"),
                ("ev", "event", "none"), ("ty", "photo_type", ""),
                ("dt", "document_type", "none"), ("se", "sensitive", "none"),
                ("pp", "people", ""))
_TEXT_FIELDS = (("t", "title"), ("d", "description"), ("o", "text"), ("sc", "scene"))
_SCORE_FIELDS = (("q", "aesthetic"), ("sh", "sharpness"))

_INDEX_COLUMNS = ("id, library_id, path, name, width, height, content_hash, dhash, taken_at, "
                  "taken_source, camera, lat, lon, info, described, prompt_ver, hidden, "
                  "projection")


# ---------------------------------------------------------------------------
# Records

def _text(value):
    """value stripped if it is text, else ""."""
    return value.strip() if isinstance(value, str) else ""


def _phrases(value, lower=False):
    """The non-empty strings of a list (lower-cased and unique when lower), in order."""
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    seen = set()
    for item in value:
        text = _text(item)
        if lower:
            text = text.lower()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _score(value):
    """A 1-5 score, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    value = int(round(value))
    low, high = prompts.SCORE_RANGE
    return value if low <= value <= high else None


def _load_info(text):
    if not text:
        return None
    try:
        info = json.loads(text, parse_constant=lambda _name: None)
    except (TypeError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def export_settings(export_options=None):
    """The privacy switches of an export: {"file_names", "folders", "location"} (all off by default).

    file_names: records keep the file name (n); folders: the folder (f);
    location: the "has GPS" flag (gp), landmarks and the location guess.
    """
    options = export_options if isinstance(export_options, dict) else {}
    return {key: options.get(key) is True for key in ("file_names", "folders", "location")}


def _extended_words(info, location):
    words = []
    objects = info.get("objects")
    if isinstance(objects, list):
        words.extend(_text(item.get("name")) for item in objects if isinstance(item, dict))
    if location:
        words.extend(_phrases(info.get("landmarks")))
    words.extend(_phrases(info.get("activities")))
    mood = _text(info.get("mood"))
    if mood and mood != "neutral":
        words.append(mood)
    if location:
        words.append(_text(info.get("location_guess")))
    return ", ".join(word for word in words if word)


def record_for_row(row, for_export=False, export_options=None, info=None):
    """The compact search record of one images row (see the module notes for the keys).

    row needs the columns of _INDEX_COLUMNS; info is its parsed description
    when the caller already has it.
    """
    options = export_settings(export_options) if for_export else None
    record = {"i": row["id"], "l": row["library_id"]}
    path = row["path"] or ""
    if not for_export or options["file_names"]:
        if row["name"]:
            record["n"] = row["name"]
    if not for_export or options["folders"]:
        folder = path.rpartition("/")[0]
        if folder:
            record["f"] = folder
    described = bool(row["described"])
    if info is None and described:
        info = _load_info(row["info"])
    if described and isinstance(info, dict):
        for key, field in _TEXT_FIELDS:
            value = _text(info.get(field))
            if value:
                record[key] = value
        for key, field in _LIST_FIELDS:
            items = _phrases(info.get(field))
            if items:
                record[key] = items
        for key, field in _LOWER_LIST_FIELDS:
            items = _phrases(info.get(field), lower=True)
            if items:
                record[key] = items
        for key, field, nothing in _ENUM_FIELDS:
            value = _text(info.get(field))
            if value and value != nothing:
                record[key] = value
        words = _extended_words(info, not for_export or options["location"])
        if words:
            record["ex"] = words
        for key, field in _SCORE_FIELDS:
            value = _score(info.get(field))
            if value is not None:
                record[key] = value
        if info.get("private_info") is True:
            record["pr"] = 1
    taken = row["taken_at"] or ""
    # An export only has dates from the camera or the file name: a date
    # from the file's modification time is a file time, and says little.
    if _DATE_RE.match(taken) and (not for_export
                                  or row["taken_source"] in RELIABLE_DATE_SOURCES):
        record["dk"] = taken[:10]
        if not for_export:
            record["ts"] = taken
    if row["camera"]:
        record["cm"] = row["camera"]
    if not for_export or options["location"]:
        lat, lon = row["lat"], row["lon"]
        if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and math.isfinite(lat) and math.isfinite(lon)):
            record["gp"] = 1
    if row["width"]:
        record["w"] = row["width"]
    if row["height"]:
        record["hgt"] = row["height"]
    if row["projection"] in imageinfo.PROJECTIONS_360:
        record["p"] = "360"
    if row["hidden"] and not for_export:
        record["h"] = 1
    if described:
        record["ds"] = 1
        if row["prompt_ver"] <= 4:
            record["v4"] = 1
    return record


def _configured_ids(config, library_ids):
    """library_ids without those that config does not list (config None: all of them)."""
    if config is None:
        return list(library_ids)
    known = {library.get("id") for library in config.get("libraries", [])
             if isinstance(library, dict)}
    return [library_id for library_id in library_ids if library_id in known]


def build_records(conn, config, library_ids, for_export=False, export_options=None,
                  include_hidden=True, use_cache=True):
    """The search records of the present pictures of some libraries, in id order.

    conn is an index.db connection; library_ids the libraries to include
    (ids that config does not list are skipped, so unattached data never
    shows up). Missing pictures are never included. Hidden pictures are
    included (with h: 1) unless include_hidden is False or for_export is
    set; an export never contains them. for_export also leaves out what
    an export must not hold unless export_options allow it (see
    export_settings and the module notes). Duplicate groups (dg) are added
    per library; use_cache=False works them out again instead of using the
    cached groups.
    """
    library_ids = _configured_ids(config, library_ids)
    if not library_ids:
        return []
    with_hidden = include_hidden and not for_export
    sql = ("SELECT %s FROM images WHERE missing_since IS NULL AND library_id IN (%s)%s "
           "ORDER BY id" % (_INDEX_COLUMNS, ",".join("?" * len(library_ids)),
                            "" if with_hidden else " AND hidden = 0"))
    records = []
    # library id -> [(id, dhash, content hash, trusted date taken)]
    items = collections.defaultdict(list)
    for row in conn.execute(sql, library_ids):
        record = record_for_row(row, for_export, export_options)
        records.append(record)
        if not row["hidden"]:
            # (No dHash: only exact copies of it are grouped.)
            dhash = "" if exact_copies_only(record) else row["dhash"]
            items[row["library_id"]].append(
                (row["id"], dhash, row["content_hash"],
                 trusted_date(row["taken_at"], row["taken_source"])))
    groups = []
    for library_id in library_ids:
        if items.get(library_id):
            groups.extend(library_groups(library_id, items[library_id], use_cache=use_cache))
    apply_groups(records, groups)
    return records


# ---------------------------------------------------------------------------
# Duplicate groups

def parse_dhash(text):
    """A 16-hex-digit dHash as an int, or None when it is missing or malformed."""
    if isinstance(text, str) and _DHASH_RE.match(text):
        return int(text, 16)
    return None


if hasattr(int, "bit_count"):
    def _bits(value):
        return value.bit_count()
else:                                       # Python 3.9
    def _bits(value):
        return bin(value).count("1")


def _band_masks(distance):
    """Every 16-bit mask with exactly distance bits set."""
    masks = []
    for combo in itertools.combinations(range(16), distance):
        mask = 0
        for bit in combo:
            mask |= 1 << bit
        masks.append(mask)
    return masks


def _spread_tables():
    """Per byte of a hash: [byte value] -> its bits at their dealt positions (see _deal_bands)."""
    tables = []
    for byte_index in range(8):
        table = []
        for value in range(256):
            spread = 0
            for bit in range(8):
                if value >> bit & 1:
                    position = 8 * byte_index + bit
                    spread |= 1 << (16 * (position % 4) + position // 4)
            table.append(spread)
        tables.append(table)
    return tables


# The 64 hash bits are dealt into four 16-bit bands, every fourth bit to the
# same band (bit p goes to band p % 4). A dHash is 8 rows of 8 bits, so each
# band takes two bits from every row: a plain sky in the top rows does not
# leave one band nearly always zero, which would pile unrelated pictures
# into the same index bucket. The Hamming distance is the same either way.
_SPREAD = _spread_tables()


def _deal_bands(value):
    """value (64 bits) with its bits dealt into four contiguous 16-bit bands."""
    result = 0
    for index in range(8):
        result |= _SPREAD[index][(value >> (8 * index)) & 0xFF]
    return result


def _probes(distance):
    """The (band, mask) lookups that find every hash at most distance bits away, nearest first.

    A hash's own value in each band, then every value one bit away in each
    band: up to 7 bits that finds everything, as some band then differs in
    at most one bit. For 8 bits also the values exactly two bits away in
    band 0 (see duplicate_groups).
    """
    probes = [(band, 0) for band in range(4)]
    probes += [(band, mask) for mask in _band_masks(1) for band in range(4)]
    if distance >= 8:
        probes += [(0, mask) for mask in _band_masks(2)]
    return tuple(probes)


def _low_detail(value):
    bits = _bits(value)
    return bits < LOW_DETAIL_BITS or bits > 64 - LOW_DETAIL_BITS


def exact_copies_only(record):
    """True for a record whose picture is grouped only with exact copies of itself.

    Described documents, scans, screenshots and diagrams (and anything
    with a document type): pages of text look alike to a dHash.
    """
    return record.get("ty") in EXACT_ONLY_TYPES or bool(record.get("dt"))


def trusted_date(taken_at, taken_source):
    """The capture date to trust for grouping: taken_at, the day alone, or "".

    taken_at when its source is trusted (RELIABLE_DATE_SOURCES), else "".
    A date from a file name without a time (IMG-20210625-WA0001 is stored
    as midnight) comes back as the day alone, "YYYY-MM-DD", so that
    midnight is not taken for a capture time.
    """
    if (taken_source not in RELIABLE_DATE_SOURCES or not isinstance(taken_at, str)
            or not _DATE_RE.match(taken_at)):
        return ""
    if taken_source == "filename" and taken_at[10:] in ("T00:00:00", "T00:00"):
        return taken_at[:10]
    return taken_at


def _moment(text):
    """(day number, seconds into the day or None) of a date taken "YYYY-MM-DD[THH:MM[:SS]]".

    None when text is empty or not a real date.
    """
    match = _MOMENT_RE.match(text or "")
    if not match:
        return None
    try:
        day = datetime.date(int(match.group(1)), int(match.group(2)),
                            int(match.group(3))).toordinal()
    except ValueError:
        return None
    if match.group(4) is None:
        return (day, None)
    hours, minutes = int(match.group(4)), int(match.group(5))
    seconds = min(59, int(match.group(6) or 0))
    if hours > 23 or minutes > 59:
        return (day, None)
    return (day, hours * 3600 + minutes * 60 + seconds)


def _same_time(first, second):
    """True if two moments (see _moment) are on the same day, or SAME_TIME_SECONDS apart."""
    if first[0] == second[0]:
        return True
    if first[1] is None or second[1] is None:
        return False
    return abs((first[0] - second[0]) * 86400 + first[1] - second[1]) <= SAME_TIME_SECONDS


def _second(moment):
    """The capture second of a moment with a time (seconds since day 1), else None."""
    if moment is None or moment[1] is None:
        return None
    return moment[0] * 86400 + moment[1]


def _item(item):
    """(image id, dhash, content hash, trusted date) of an item with or without the date."""
    taken = item[3] if len(item) > 3 else ""
    return (int(item[0]), item[1] or "", item[2] or "", taken or "")


class _BandIndex:
    """Group representatives by the values of their four 16-bit bands (see _deal_bands)."""

    def __init__(self):
        self.tables = ({}, {}, {}, {})          # band value -> [representative index]
        self.count = 0

    def add(self, dealt, index):
        for band, table in enumerate(self.tables):
            table.setdefault((dealt >> (16 * band)) & 0xFFFF, []).append(index)
        self.count += 1

    def remove(self, dealt, index):
        for band, table in enumerate(self.tables):
            value = (dealt >> (16 * band)) & 0xFFFF
            bucket = table[value]
            bucket.remove(index)
            if not bucket:
                del table[value]
        self.count -= 1


class _Grouper:
    """The state of duplicate_groups(): pictures are added oldest first.

    add() takes more pictures, all newer (higher ids) than those it has;
    groups() gives the groups so far. Adding pictures to a grouper gives
    the same groups as working them out from scratch with all pictures,
    except in one case add() reports by returning False (and then must
    not be used further): a new exact copy brings the first usable dHash,
    or the first trusted date, to an older picture that had none.

    The groups' representatives (first pictures) are found through these
    indexes:
    - everyone and exact: all of them, for a picture without a trusted
      date (it may join any group, by its hash alone);
    - days: those of the groups that have a trusted date (their anchor),
      by capture day. A picture with a trusted date may only join one of
      these from about its own day: they are compared with it one by one,
      or through a banded index of the day (day_bands) once the day has
      DAY_INDEX_SIZE groups;
    - seconds: those with a trusted capture time, by the second, for
      bursts (BURST_SECONDS; a second with BURST_GROUPS of them is no
      burst);
    - loose: those of the groups without any trusted date yet, which a
      picture of any day may join.
    """

    def __init__(self, threshold=DUPLICATE_THRESHOLD, undated_threshold=UNDATED_THRESHOLD,
                 burst_threshold=BURST_THRESHOLD):
        threshold = int(threshold)
        if threshold > 8:
            raise ValueError("the banded index finds distances up to 8 bits only")
        self.threshold = threshold
        self.undated_threshold = min(int(undated_threshold), threshold)
        self.burst_threshold = max(int(burst_threshold), threshold)
        self.probes = _probes(threshold)
        self.undated_probes = _probes(self.undated_threshold)
        self.representatives = []               # dealt hashes
        self.when = []                          # per representative: its moment or None
        self.anchors = []                       # per group: its first trusted moment or None
        self.members = []                       # [ids] per representative
        self.everyone = _BandIndex()
        self.exact = {}                         # dealt hash -> [representative index]
        self.loose = _BandIndex()
        self.days = {}                          # anchor day -> [representative index]
        self.day_bands = {}                     # anchor day -> _BandIndex
        self.seconds = {}                       # capture second -> [representative index]
        self.alone = []                         # [ids] per unit that groups only as copies
        # unit key -> ("g" | "a", index, dhash or None, moment or None)
        self.units = {}
        self.max_id = None
        self.limited = 0                        # lookups cut short by MAX_CANDIDATES

    def _buckets(self, bands, dealt, probes):
        """The lists of representatives that probes bring up in bands (a _BandIndex).

        Nearest first; stops after about MAX_CANDIDATES representatives
        (counted in limited).
        """
        # (A representative may come up in more than one band.)
        left = max(MAX_CANDIDATES, (bands.count * 2 * len(probes)) >> 16)
        lookups = [table.get for table in bands.tables]
        parts = [(dealt >> shift) & 0xFFFF for shift in (0, 16, 32, 48)]
        for band, mask in probes:
            bucket = lookups[band](parts[band] ^ mask)
            if not bucket:
                continue
            if len(bucket) >= left:
                self.limited += 1
                yield bucket[:left]
                return
            left -= len(bucket)
            yield bucket

    def _rule(self, index, moment):
        """(limit, rank) for a picture with a trusted date (moment) and representative index.

        limit is how many bits apart their hashes may be for the picture to
        join that group (-1: it may not, being of another day); rank is 1
        when the representative has no trusted date of its own, else 0.
        """
        anchor = self.anchors[index]
        if anchor is not None and not _same_time(moment, anchor):
            return -1, 0
        when = self.when[index]
        if when is None:
            return self.undated_threshold, 1
        if moment[1] is not None and when[1] is not None:
            second = when[0] * 86400 + when[1]
            if (abs(moment[0] * 86400 + moment[1] - second) <= BURST_SECONDS
                    and len(self.seconds[second]) < BURST_GROUPS):
                return self.burst_threshold, 0
        return self.threshold, 0

    def _nearest(self, dealt, moment):
        """The representative a picture joins (see duplicate_groups), or None."""
        representatives = self.representatives
        if moment is None:
            same = self.exact.get(dealt)
            if same:
                return same[0]
            best, best_distance = None, self.undated_threshold + 1
            for bucket in self._buckets(self.everyone, dealt, self.undated_probes):
                for index in bucket:
                    distance = _bits(dealt ^ representatives[index])
                    # The nearest one; of equally near ones, the oldest.
                    if distance < best_distance or (distance == best_distance
                                                    and best is not None and index < best):
                        best, best_distance = index, distance
            return best
        # The groups of about the same day (the day before, the day itself,
        # the day after), and those taken within a burst.
        lists = []
        for day in (moment[0] - 1, moment[0], moment[0] + 1):
            group_ids = self.days.get(day)
            if group_ids:
                bands = self.day_bands.get(day)
                if bands is None:
                    lists.append(group_ids)
                else:
                    lists.extend(self._buckets(bands, dealt, self.probes))
        second = _second(moment)
        if second is not None and self.burst_threshold > self.threshold:
            for near in range(second - BURST_SECONDS, second + BURST_SECONDS + 1):
                group_ids = self.seconds.get(near)
                if group_ids and len(group_ids) < BURST_GROUPS:
                    lists.append(group_ids)
        widest = self.burst_threshold
        best, best_key = None, (2, 0, 0)
        for group_ids in lists:
            for index in group_ids:
                distance = _bits(dealt ^ representatives[index])
                if distance > widest:
                    continue
                limit, rank = self._rule(index, moment)
                # Dated representatives first; then the nearest; then the oldest.
                if distance <= limit and (rank, distance, index) < best_key:
                    best, best_key = index, (rank, distance, index)
        if best_key[0] == 0:
            return best
        # Groups without any trusted date take it by its hash alone.
        for bucket in self._buckets(self.loose, dealt, self.undated_probes):
            for index in bucket:
                distance = _bits(dealt ^ representatives[index])
                if distance <= self.undated_threshold and (1, distance, index) < best_key:
                    best, best_key = index, (1, distance, index)
        return best

    def _anchor(self, index, moment):
        """Give group index its first trusted date (moment): file it under that day."""
        self.anchors[index] = moment
        group_ids = self.days.setdefault(moment[0], [])
        group_ids.append(index)
        bands = self.day_bands.get(moment[0])
        if bands is not None:
            bands.add(self.representatives[index], index)
        elif len(group_ids) >= DAY_INDEX_SIZE:
            bands = self.day_bands[moment[0]] = _BandIndex()
            for other in group_ids:
                bands.add(self.representatives[other], other)

    def _start_group(self, dealt, moment, ids):
        """Start a group of ids whose first picture has the dealt hash and moment; its index."""
        index = len(self.representatives)
        self.representatives.append(dealt)
        self.when.append(moment)
        self.anchors.append(None)
        self.members.append(list(ids))
        self.everyone.add(dealt, index)
        self.exact.setdefault(dealt, []).append(index)
        second = _second(moment)
        if second is not None:
            self.seconds.setdefault(second, []).append(index)
        if moment is None:
            self.loose.add(dealt, index)
        else:
            self._anchor(index, moment)
        return index

    def add(self, items):
        """Add items; returns False when they cannot be added.

        Items are (image id, dhash, content hash[, trusted date]); the
        trusted date is "" or a capture date to trust, "YYYY-MM-DDTHH:MM:SS"
        or "YYYY-MM-DD" (see trusted_date()).
        """
        items = sorted(_item(item) for item in items)
        if not items:
            return True
        if self.max_id is not None and items[0][0] <= self.max_id:
            return False
        fresh = collections.OrderedDict()       # unit key -> [ids], oldest first
        first_hash = {}
        first_moment = {}
        for image_id, dhash, content_hash, taken in items:
            key = ("c", content_hash) if content_hash else ("i", image_id)
            value = parse_dhash(dhash)
            moment = _moment(taken)
            known = self.units.get(key)
            if known is not None:
                if known[2] is None and value is not None:
                    return False
                if known[0] == "g" and known[3] is None and moment is not None:
                    return False
                continue
            fresh.setdefault(key, [])
            if first_hash.get(key) is None:
                first_hash[key] = value
            if first_moment.get(key) is None:
                first_moment[key] = moment
        # Nothing has been changed so far; from here on add() cannot fail.
        for image_id, _dhash, content_hash, _taken in items:
            key = ("c", content_hash) if content_hash else ("i", image_id)
            known = self.units.get(key)
            if known is not None:
                (self.members if known[0] == "g" else self.alone)[known[1]].append(image_id)
            else:
                fresh[key].append(image_id)
        for key, ids in fresh.items():          # oldest first: ids are increasing
            value = first_hash.get(key)
            moment = first_moment.get(key)
            if value is None or _low_detail(value):
                self.units[key] = ("a", len(self.alone), value, moment)
                self.alone.append(ids)
                continue
            dealt = _deal_bands(value)
            best = self._nearest(dealt, moment)
            if best is None:
                best = self._start_group(dealt, moment, ids)
            else:
                self.members[best].extend(ids)
                if moment is not None and self.anchors[best] is None:
                    self.loose.remove(self.representatives[best], best)
                    self._anchor(best, moment)
            self.units[key] = ("g", best, value, moment)
        self.max_id = items[-1][0]
        return True

    def groups(self):
        """The groups of two or more pictures: sorted id lists, sorted by first id."""
        result = [sorted(ids) for ids in self.members + self.alone if len(ids) > 1]
        result.sort(key=lambda ids: ids[0])
        return result


def duplicate_groups(items, threshold=DUPLICATE_THRESHOLD, undated_threshold=UNDATED_THRESHOLD,
                     burst_threshold=BURST_THRESHOLD):
    """Near-duplicate groups: lists of image ids (sorted, two or more each), sorted by first id.

    items are (image id, dhash text, content hash, trusted date) tuples of
    one library. The trusted date is the capture date "YYYY-MM-DDTHH:MM:SS"
    (or the day alone, "YYYY-MM-DD") when it comes from EXIF data or the
    file name, else "" (see trusted_date(); an item of three counts as one
    without). Exact copies (the same non-empty content hash) are always one
    group. Otherwise two pictures go together when their dHashes are close
    enough, by these rules:

    - Dates: when both pictures have a trusted date, they must have been
      taken on the same calendar day, or at most SAME_TIME_SECONDS apart
      (across midnight); then their dHashes may be up to threshold bits
      apart, or up to burst_threshold when both have a capture time and
      those are at most BURST_SECONDS apart (a burst or a bracket; but not
      once BURST_GROUPS groups have started in the earlier one's second,
      as a camera whose clock was never set stamps every picture with the
      same time). When
      either has no trusted date, the dHashes must be at most
      undated_threshold bits apart: going by the picture alone needs a
      closer match.
    - Leader clustering, not single-link: pictures are taken oldest first
      (by id), and each joins the group of the nearest representative (a
      group's first picture) it may go with by the rules above; otherwise
      it starts a group of its own. A picture with a trusted date prefers
      the representatives with a trusted date of their own, however much
      nearer one without is: its own burst beats a look-alike that has
      only a file time. Every member is therefore close to its
      representative, and a group cannot drift. Single-link would let a
      chain of small steps join pictures that look nothing alike (A~B,
      B~C, C~D, but A and D far apart). Dates do not chain either: a
      group keeps the first trusted date it gets (its representative's,
      when it has one), and a picture with a trusted date only joins a
      group whose date is about the same, so its dated pictures cannot
      creep from day to day. Taking the oldest first keeps groups stable
      as new pictures arrive, and lets library_groups() add new pictures
      to the groups it has instead of starting over.
    - Pictures with little detail (see LOW_DETAIL_BITS) and pictures
      without a valid dHash (build_records leaves it out for documents
      and screenshots) only group as exact copies.

    Representatives are never found by comparing every pair, nor by
    sorting the hashes (sorting misses pairs that differ in a high bit).
    A picture with a trusted date is compared with the groups dated the
    day before, the same day and the day after (and those of its burst),
    the others only through banded indexes (see _Grouper). The bits are
    dealt into four 16-bit bands (_deal_bands). If two hashes are at most
    8 bits apart, either some band differs in at most one bit, or all four
    bands differ in exactly two. So looking up every band value within one
    bit in all four bands, plus the values exactly two bits away in band
    0, finds every representative within 8 bits. The search is exact
    unless more than MAX_CANDIDATES representatives come up in one index
    for one picture (only with very many alike hashes); then the
    nearest-looking ones are compared and the rest are not, which keeps
    the time linear.
    """
    grouper = _Grouper(threshold, undated_threshold, burst_threshold)
    grouper.add(items)
    return grouper.groups()


def _date_number(record):
    """The date taken as a number (YYYYMMDDhhmmss), or None."""
    digits = _NOT_DIGITS_RE.sub("", record.get("ts") or record.get("dk") or "")[:14]
    return int(digits.ljust(14, "0")) if len(digits) >= 8 else None


def cover_key(record):
    """Sort key of a group member as its cover (the largest key wins).

    Best aesthetic, then sharpness, then the most pixels, then a picture
    with a camera name (an original rather than a copy that lost its
    EXIF data), then the oldest date (a re-saved or forwarded copy is
    dated by its file), then the lowest id (found first).
    """
    taken = _date_number(record)
    return (record.get("q") or 0, record.get("sh") or 0,
            (record.get("w") or 0) * (record.get("hgt") or 0),
            1 if record.get("cm") else 0,
            -taken if taken is not None else -10 ** 15,
            -record["i"])


def apply_groups(records, groups):
    """Set dg on the records that belong to a group (in place); returns the number of groups.

    groups are lists of image ids (duplicate_groups). Only records in the
    list count: a group with fewer than two of them (hidden ones are not
    counted) gets no dg, so this can be run again after records were left
    out (an export). dg is the id of the group's cover (cover_key).
    """
    by_id = {}
    for record in records:
        record.pop("dg", None)
        by_id[record["i"]] = record
    count = 0
    for ids in groups:
        members = [by_id[image_id] for image_id in ids
                   if image_id in by_id and not by_id[image_id].get("h")]
        if len(members) < 2:
            continue
        cover = max(members, key=cover_key)["i"]
        for record in members:
            record["dg"] = cover
        count += 1
    return count


def _fingerprint_start():
    return hashlib.sha1(("groups %d %d %d %d %d %d %d %d %d %d\n" % (
        GROUPS_VERSION, DUPLICATE_THRESHOLD, UNDATED_THRESHOLD, BURST_THRESHOLD, BURST_SECONDS,
        BURST_GROUPS, SAME_TIME_SECONDS, LOW_DETAIL_BITS, MAX_CANDIDATES,
        DAY_INDEX_SIZE)).encode("ascii"))


def _fingerprint_line(item):
    image_id, dhash, content_hash, taken = item
    return ("%d %s %s %s\n" % (image_id, dhash, content_hash, taken)).encode("utf-8", "replace")


def groups_fingerprint(items):
    """A digest of what duplicate_groups() depends on (items sorted by id)."""
    digest = _fingerprint_start()
    for item in items:
        digest.update(_fingerprint_line(_item(item)))
    return digest.hexdigest()


def _fingerprints(items, upto):
    """(groups_fingerprint(items), the same of the items with ids up to upto)."""
    digest = _fingerprint_start()
    prefix = digest.hexdigest() if upto is None else None
    for item in items:
        if prefix is None and item[0] > upto:
            prefix = digest.hexdigest()
        digest.update(_fingerprint_line(item))
    full = digest.hexdigest()
    return full, prefix if prefix is not None else full


def groups_cache_path(library_id):
    """The file that keeps a library's duplicate groups: data/cache/groups-<id>.json."""
    name = library_id if _SAFE_NAME_RE.match(library_id or "") else (
        "x" + hashlib.sha1((library_id or "").encode("utf-8")).hexdigest()[:16])
    return os.path.join(common.data_dir(), "cache", "groups-%s.json" % name)


_groups_lock = threading.Lock()
_groups_memo = {}           # library id -> (fingerprint, groups, grouper or None)


def _read_groups_file(path, fingerprint):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    groups = data.get("groups")
    if not isinstance(groups, list):
        return None
    for ids in groups:
        if not isinstance(ids, list) or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in ids):
            return None
    return groups


def _write_groups_file(path, fingerprint, groups):
    try:
        common.ensure_dir(os.path.dirname(path))
        common.write_file_atomic(path, json.dumps(
            {"format": 1, "fingerprint": fingerprint, "groups": groups},
            separators=(",", ":")) + "\n")
    except OSError as error:
        print("Warning: could not save the duplicate groups (%s): %s" % (path, error),
              file=sys.stderr)


def library_groups(library_id, items, use_cache=True):
    """The duplicate groups of one library's pictures, worked out only when they changed.

    items are (image id, dhash, content hash, trusted date) of the
    library's present, visible pictures (see duplicate_groups). The groups
    are remembered in memory and in data/cache/groups-<library id>.json,
    keyed by a digest of exactly those inputs (groups_fingerprint): a new
    description, a rename or a new quality score never makes them be
    worked out again; a new, changed, hidden or removed picture does.
    When the only change is new pictures (a scan found more), only those
    are added to the groups in memory (see _Grouper); anything else works
    them out from scratch. The result must not be changed.
    """
    items = sorted(_item(item) for item in items)
    found = None
    if use_cache:
        with _groups_lock:
            found = _groups_memo.get(library_id)
    grouper = found[2] if found is not None else None
    fingerprint, before = _fingerprints(items, grouper.max_id if grouper is not None else None)
    if found is not None and found[0] == fingerprint:
        return found[1]
    path = groups_cache_path(library_id)
    groups = None
    if grouper is not None and before == found[0]:
        # Only newer pictures than the grouper has: add them to it.
        added = [item for item in items if item[0] > grouper.max_id]
        if grouper.add(added):
            groups = grouper.groups()
        else:
            grouper = None
    else:
        grouper = None
        if use_cache:
            groups = _read_groups_file(path, fingerprint)
    if groups is None:
        grouper = _full_grouper(items)
        groups = grouper.groups()
    if use_cache:
        _write_groups_file(path, fingerprint, groups)
    with _groups_lock:
        _groups_memo[library_id] = (fingerprint, groups, grouper)
    return groups


def _full_grouper(items):
    """A _Grouper holding all of items: the groups worked out from scratch."""
    grouper = _Grouper(DUPLICATE_THRESHOLD, UNDATED_THRESHOLD, BURST_THRESHOLD)
    grouper.add(items)
    return grouper


# ---------------------------------------------------------------------------
# /api/index

# Everything about the present pictures of a library that the records
# depend on, as sums: a row that is added, removed, hidden, described,
# rescanned or marked as a 360 photo changes at least one of them, also when
# the change did not touch the "updated" column.
_STATE_SQL = """
    SELECT library_id, COUNT(*), TOTAL(id), MAX(id), MAX(updated), TOTAL(updated),
        TOTAL(hidden * id), TOTAL(described * id), TOTAL(prompt_ver * id),
        TOTAL(length(projection) * id), TOTAL(length(dhash) * id),
        TOTAL(length(content_hash) * id)
    FROM images
    WHERE missing_since IS NULL AND library_id IN (%s)
    GROUP BY library_id ORDER BY library_id"""


def data_state(conn, library_ids):
    """A JSON-friendly summary of the libraries' rows that changes whenever their records do."""
    library_ids = list(library_ids)
    if not library_ids:
        return []
    rows = conn.execute(_STATE_SQL % ",".join("?" * len(library_ids)), library_ids)
    return [[value if value is not None else 0 for value in row] for row in rows]


def index_etag(conn, libraries, include_hidden, state=None):
    """The ETag (hex digits) of the /api/index answer for libraries ({id: name}).

    state is data_state() of the libraries when the caller already has it.
    """
    if state is None:
        state = data_state(conn, libraries)
    key = [INDEX_VERSION, GROUPS_VERSION, DUPLICATE_THRESHOLD, UNDATED_THRESHOLD,
           BURST_THRESHOLD, BURST_SECONDS, BURST_GROUPS, SAME_TIME_SECONDS, LOW_DETAIL_BITS,
           prompts.PROMPT_VER, bool(include_hidden),
           [[library_id, name] for library_id, name in libraries.items()], state]
    return hashlib.sha1(json.dumps(key, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


def index_payload(conn, config, libraries, include_hidden=False):
    """The /api/index answer: {"version", "generated", "libraries": [{id, name}], "records"}."""
    records = build_records(conn, config, list(libraries), include_hidden=include_hidden)
    return {"version": INDEX_VERSION, "generated": int(time.time()),
            "libraries": [{"id": library_id, "name": name}
                          for library_id, name in libraries.items()],
            "records": records}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def utf8(text):
    """text as UTF-8 bytes; a lone surrogate (which UTF-8 cannot hold) becomes U+FFFD."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return _LONE_SURROGATE_RE.sub("\ufffd", text).encode("utf-8")


class IndexService:
    """Answers /api/index requests: compact records with a strong ETag, gzip and 304.

    The ETag comes from one summary query (data_state). That query is only
    run again when the database has changed since: a connection kept open
    for the purpose asks SQLite (PRAGMA data_version), which notices every
    commit by any other connection or process, at no cost. Built answers
    are kept gzip-compressed for the last few ETags (MEMO_SIZE), so several
    tabs or a reload do not build them again. The records are also kept
    per library (as JSON), keyed by that library's row of data_state: a
    change in one library (a description saved, a picture hidden) builds
    only that library's records again. Thread safe; builds run one at a
    time. close() closes the watching connection.
    """

    def __init__(self, memo_size=MEMO_SIZE, watch=True):
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._memo = collections.OrderedDict()      # etag -> gzip-compressed JSON
        self._memo_size = memo_size
        # (library id, include_hidden) -> (state row, [(image id, record JSON)])
        self._libraries = collections.OrderedDict()
        self._watch_lock = threading.Lock()
        self._watching = watch
        self._watch = None                          # (database path, connection)
        self._version = None
        self._states = {}                           # library ids -> data_state()
        self.builds = 0                             # how many answers were built (tests)
        self.library_builds = 0                     # how many library record lists (tests)

    def _data_version(self):
        """The database's change counter as the watching connection sees it (None: unknown)."""
        if not self._watching:
            return None
        path = common.db_path()
        with self._watch_lock:
            try:
                if self._watch is not None and self._watch[0] != path:
                    self._watch[1].close()
                    self._watch = None
                if self._watch is None:
                    if not os.path.isfile(path):
                        return None
                    self._watch = (path, db.connect(path, check_same_thread=False))
                return (path, self._watch[1].execute("PRAGMA data_version").fetchone()[0])
            except Exception:
                return None

    def state(self, conn, library_ids):
        """data_state() of the libraries, computed again only after the database changed."""
        key = tuple(library_ids)
        # Read the counter first: a commit made while the state is worked
        # out then shows up as a new counter next time.
        version = self._data_version()
        with self._lock:
            if version is not None and version == self._version and key in self._states:
                return self._states[key]
        state = data_state(conn, library_ids)
        with self._lock:
            if version != self._version:
                self._version = version
                self._states = {}
            if version is not None:
                self._states[key] = state
        return state

    def close(self):
        """Close the watching connection (it is opened again when needed)."""
        with self._watch_lock:
            if self._watch is not None:
                self._watch[1].close()
                self._watch = None
        with self._lock:
            self._version = None
            self._states = {}

    def _remembered(self, etag):
        with self._lock:
            data = self._memo.get(etag)
            if data is not None:
                self._memo.move_to_end(etag)
            return data

    def _library_records(self, conn, config, library_id, include_hidden, row):
        """[(image id, record JSON)] of one library, built again only when its state row changed."""
        key = (library_id, bool(include_hidden))
        with self._lock:
            found = self._libraries.get(key)
            if found is not None and found[0] == row:
                self._libraries.move_to_end(key)
                return found[1]
        records = build_records(conn, config, [library_id], include_hidden=include_hidden)
        pieces = [(record["i"], _json(record)) for record in records]
        self.library_builds += 1
        with self._lock:
            self._libraries[key] = (row, pieces)
            while len(self._libraries) > LIBRARY_MEMO_SIZE:
                self._libraries.popitem(last=False)
        return pieces

    def compressed_body(self, conn, config, libraries, include_hidden, etag, state=None):
        """The gzip-compressed JSON answer for etag, built if it is not remembered.

        state is the data_state() the ETag was made from (worked out when
        missing); the records of a library are reused while its row is
        the same. The records are in id order, as build_records() gives
        them.
        """
        data = self._remembered(etag)
        if data is not None:
            return data
        with self._build_lock:
            data = self._remembered(etag)
            if data is not None:
                return data
            if state is None:
                state = data_state(conn, list(libraries))
            rows = {row[0]: tuple(row) for row in state}
            lists = [self._library_records(conn, config, library_id, include_hidden,
                                           rows.get(library_id))
                     for library_id in libraries]
            lists = [pieces for pieces in lists if pieces]
            merged = lists[0] if len(lists) == 1 else heapq.merge(*lists)
            text = '{"version":%d,"generated":%d,"libraries":%s,"records":[%s]}' % (
                INDEX_VERSION, int(time.time()),
                _json([{"id": library_id, "name": name}
                       for library_id, name in libraries.items()]),
                ",".join(piece for _image_id, piece in merged))
            data = gzip.compress(utf8(text), GZIP_LEVEL, mtime=0)
            self.builds += 1
            with self._lock:
                self._memo[etag] = data
                while len(self._memo) > self._memo_size:
                    self._memo.popitem(last=False)
        return data

    def respond(self, conn, config, libraries, include_hidden=False, accept_gzip=False,
                etag_matches=None):
        """(status, body bytes, headers) for a request for the records of libraries ({id: name}).

        accept_gzip: the client takes Content-Encoding gzip. etag_matches(tag)
        tells whether the request's If-None-Match names an ETag; a match
        gets 304 with no body. The two encodings have their own strong
        ETags ("<tag>" and "<tag>-gzip"); either one in If-None-Match counts,
        as both hold the same records.
        """
        state = self.state(conn, list(libraries))
        etag = index_etag(conn, libraries, include_hidden, state)
        sent = etag + "-gzip" if accept_gzip else etag
        headers = {"ETag": '"%s"' % sent, "Cache-Control": "private, no-cache",
                   "Vary": "Accept-Encoding", "X-Content-Type-Options": "nosniff"}
        if etag_matches is not None and (etag_matches(etag) or etag_matches(etag + "-gzip")):
            return 304, b"", headers
        data = self.compressed_body(conn, config, libraries, include_hidden, etag, state)
        if accept_gzip:
            headers["Content-Encoding"] = "gzip"
            return 200, data, headers
        return 200, gzip.decompress(data), headers
