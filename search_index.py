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
    du   1 when dk is only the file's modification time (server): sorting
         by date uses it, but the timeline, on: and the dates chosen on
         the timeline do not (an export has no such dates at all)
    cm   camera                           gp   1 when the picture has a GPS
                                               position (never the position
                                               itself; an export only with
                                               location)
    ci   place: the town nearest to the GPS position (see places.py), when
         it is at most places.CITY_KM away   km   how far (whole km)
    rg   the town's region                co   its country
    cc   the country's ISO code ("US")    (place names: an export only
                                           with location)
    pe   1 when the place is only estimated (the picture has no usable
         position of its own, see place_estimates.py): ci, rg, co and cc
         are the estimate's (an export: only with location and
         estimated_places)
    ps   where a place without the picture's own position comes from:
         "c" a copy of a located picture, "t" the pictures taken around
         the same time, "a" the library's area (region and country only)
         -- these with pe -- or "o" the owner (a place set or an estimate
         accepted; an export: with location)
    gx   1 when the GPS check set the picture's position aside (server,
         for the admin only; never exported): it has no gp and no place
    w    displayed width                  hgt  displayed height
    p    "360" for a 360-degree photo (or video)
    mv   1 for a video (see videos.py); its ty is always "video"
    dr   a video's length in seconds (one decimal, rounded down, at least
         0.1: players show whole seconds rounded down)
    vc   a video's codec ("h264", "hevc", ...: the pages ask the browser
         whether it can play it; a 360 video's is its LRV's, else its
         original's only when that holds both lenses in one stream; an
         export's is what it published)
    fx   a 360 video's lens geometry, for the player that stitches its
         frames (videos.Video360.fisheye: lens circles, fov, turn, front,
         mirror, blend, gain, fill)
    lv   1 when the player turns a 360 video's frames level: its full
         record holds the level track ("level", see insta360.level_track;
         an export's only with the video)
    ov   where a 360 picture opens when not at the front: [yaw, pitch] or
         [yaw, pitch, fov] in degrees (see opening_views.py)
    vt   a video's kind according to the AI model when it is not a photo
         ("screenshot" for a screen recording, ...): searched with the
         kind words, and an export's exclude_types look at it
    dg   duplicate group: the id of the group's cover picture, on every
         member of a group of two or more (see duplicate_groups; the cover
         is chosen by cover_key)
    dv   drive: the id of the drive's first clip, on every clip of a drive
         (a run of video clips recorded one after the other, see drives.py)
    dx   the clip's place in its drive (the first, 0, is left out)
    dh   seconds missing before the clip (clips the camera made that are
         not in the library)
    dvt  dvd  dvg   the drive's own title, description and tags, dvp and
         dvs its flags (private, sensitive level), on one of its clips
         (see drives.annotate: only the admin's records hold those of a
         drive with a hidden clip)
    h    1 for a hidden picture (server, for the admin only; never exported)
    ed   1 when the owner edited the title, description or tags (server,
         for the admin only; see image_edits.py)
    fav  1 for one of the owner's favorites   rt   its rating, 1-5 stars
    al   the ids of the albums it is in [] (manual albums; see albums.py)
         (the server; an export only with its "marks" option, which also
         renumbers the albums)
    ds   1 when described                 v4   1 when the description comes
                                               from the older version 4
                                               prompt (only d, s, o, g and c)

Values are what the describe step stored (enums such as "sunrise_sunset"
or "card_or_id" as they are), with the owner's edits laid over them (a
title, a description and tags of their own: the server and an export
alike show and search those); tags and colors are lower-cased here
because older descriptions were not.

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
import operator
import os
import re
import sqlite3
import struct
import sys
import threading
import time

import albums
import common
import db
import drives
import gps_check
import image_edits
import imageinfo
import insta360
import looks
import not_duplicates
import opening_views
import place_estimates
import prompts
import videos

# The shape of the records and of the /api/index answer. Raise it when either
# changes, so browsers do not keep using an older index.
INDEX_VERSION = 1
# Raise when duplicate_groups() or the groups' inputs (see build_records)
# decide differently, so cached groups are worked out again.
GROUPS_VERSION = 6
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
# camera's EXIF data, a date in the file name, or a video's own metadata
# (videos.creation_date). A file's modification time changes when it is
# copied or edited, so it says little.
RELIABLE_DATE_SOURCES = frozenset(("exif", "filename", "video"))
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
# Two pictures that are not identical files are only grouped when their
# shapes are about the same: their aspect ratios (width / height, as
# displayed) at most this much apart, the wider at most 15% wider than the
# other (see same_shape). A 4:3 photo and a 3:2 crop of it (12.5%) may
# group; a 4:3 photo and a 16:9 one, a panorama, or an upright picture may
# not, however alike their dHashes (made from the picture squeezed into a
# square) are: a night shot and a wide panorama of the same evening can be
# close. Every two pictures of a group keep to it, not just each picture
# and the group's first. Identical files always group.
SHAPE_TOLERANCE = 0.15
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
# Looks (looks.py: what a picture looks like to an image model, one vector
# per picture) stack pictures too (see duplicate_groups). The cut-offs are
# cosines of two look vectors (each divided by its own length, see
# _look_cosine), fixed rather than worked out per library: near duplicates
# came out at about the same cosine in every library, while what two
# pictures taken at random come to depends on what else a library holds,
# and a cut-off taken from that changed when a picture was added or hidden.
# They were set on about 12,000 photos with EmbeddingGemma 2 at 256
# dimensions (two pictures taken at random: 0.65 to 0.82 by library), by
# looking at the pairs:
# - Two pictures taken at most LOOK_STACK_SECONDS apart stack when their
#   looks are at least LOOK_STACK_COSINE close, however far apart their
#   dHashes are (a crop, a filter, a re-saved copy, the next shot of a
#   burst where the subject moved). Pairs that close and that near in time
#   were nearly always one shot or the same view taken again; a few
#   minutes apart, half of those from 0.985 to 0.99 were other shots of the
#   subject (the frog from the side, the next patch of leaves), so ...
LOOK_STACK_COSINE = 0.985
LOOK_STACK_SECONDS = 60
# ... taken up to LOOK_SAME_SECONDS apart they must look the same
# (SAME_PICTURE_COSINE); taken further apart, only their dHashes can stack
# them (the same guitar 80 minutes later, re-framed, came out at 0.994).
LOOK_SAME_SECONDS = 600
# Two pictures whose looks are at most LOOK_APART close are not duplicates,
# however close their dHashes are (pressed coins, casino chips or the sides
# of a mug photographed one by one on the same cloth come out at 0.75 to
# 0.85), nor are two taken more than LOOK_STACK_SECONDS apart whose looks
# are at most LOOK_APART_LATER close (other coins and the other side of the
# mug a few minutes later, up to 0.93). Taken within a minute, true bursts
# of a dark or moving scene came out at 0.86 to 0.93, and a black-and-white
# copy (without a date) at 0.91: so alike things photographed within one
# minute (or without times to compare) may still stack.
LOOK_APART = 0.85
LOOK_APART_LATER = 0.93
# "Same picture, any date" (the Duplicates page, look_copies): pictures whose
# looks are at least this close, whatever their dates: a copy saved again,
# edited a little or sent and saved back (an edited copy of a painting came
# out at 0.990; at 0.988 there were other poses of one photo session).
SAME_PICTURE_COSINE = 0.990
# look_copies compares a picture only with the leaders that share one band
# of this many signs of its look with it ...
LOOK_BAND_BITS = 14
# ... when there are more than this many pictures (fewer are compared one by
# one: the signs need many pictures to say something).
LOOK_BAND_MIN = 200
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
                  "projection, place_city, place_region, place_country, place_cc, place_km, "
                  "media, duration, CASE WHEN media = 'video' THEN exif ELSE '' END AS vexif")
# (record key, images column) of the place names.
_PLACE_FIELDS = (("ci", "place_city"), ("rg", "place_region"), ("co", "place_country"),
                 ("cc", "place_cc"))


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
    """The privacy switches of an export: {"file_names", "folders", "location", "marks",
    "estimated_places"} (all off by default).

    file_names: records keep the file name (n); folders: the folder (f);
    location: the "has GPS" flag (gp), landmarks and the location guess,
    place names (and the owner's own places, ps "o"); estimated_places
    (with location only): estimated places (pe); marks: favorites,
    ratings and album places (fav, rt, al).
    """
    options = export_options if isinstance(export_options, dict) else {}
    result = {key: options.get(key) is True
              for key in ("file_names", "folders", "location", "marks", "estimated_places")}
    result["estimated_places"] = result["estimated_places"] and result["location"]
    return result


def add_marks(record, marks):
    """Add a picture's marks ((favorite, rating, [album ids]), see albums.for_libraries)
    to its record: fav, rt and al, each only when set."""
    if not marks:
        return
    favorite, rating, album_ids = marks
    if favorite:
        record["fav"] = 1
    if rating:
        record["rt"] = rating
    if album_ids:
        record["al"] = list(album_ids)


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


def record_for_row(row, for_export=False, export_options=None, info=None, edit=None,
                   owner=False, marks=None, gps_ignored=False, estimate=None):
    """The compact search record of one images row (see the module notes for the keys).

    row needs the columns of _INDEX_COLUMNS; info is its parsed description
    when the caller already has it; edit the owner's edit of the picture
    (image_edits), laid over the description. owner: the record is for the
    admin, who is told which pictures are edited (ed) and whose position
    the GPS check set aside (gps_ignored: gx). marks: the picture's
    (favorite, rating, album ids) (albums.for_libraries); an export keeps
    them only with its "marks" option. estimate: its place_estimates row
    (place_estimates.for_libraries), used when it has no place of its own
    (pe, ps; an export: see export_settings).
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
    edited = not image_edits.is_empty(edit)
    if edited:
        info = image_edits.apply(info if described else None, edit)
    if (described or edited) and isinstance(info, dict):
        # (An export without the location: no GPS position written out in
        # the text either, as a dashcam prints it over its video.)
        scrub = for_export and not options["location"]
        for key, field in _TEXT_FIELDS:
            value = _text(info.get(field))
            if value and scrub:
                value = imageinfo.scrub_positions(value)
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
        if row["prompt_ver"] > 4:
            # Version 4 rated sharpness from 0 to 1 (and had no aesthetic
            # score): read as a 1-5 score it would be the worst there is.
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
            if row["taken_source"] == "mtime":
                record["du"] = 1
    if row["camera"]:
        record["cm"] = row["camera"]
    if not for_export or options["location"]:
        lat, lon = row["lat"], row["lon"]
        if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and math.isfinite(lat) and math.isfinite(lon)):
            record["gp"] = 1
        add_place(record, row)
        if (not record.get("co") and estimate is not None
                and place_estimates.usable(row, estimate)):
            keys = place_estimates.place_record(estimate)
            if not for_export or keys.get("ps") == "o" or options["estimated_places"]:
                record.update(keys)
    if row["width"]:
        record["w"] = row["width"]
    if row["height"]:
        record["hgt"] = row["height"]
    if row["projection"] in imageinfo.PROJECTIONS_360:
        record["p"] = "360"
    if row["media"] == videos.MEDIA_VIDEO:
        add_video(record, row)
    if row["hidden"] and not for_export:
        record["h"] = 1
    if edited and owner and not for_export:
        record["ed"] = 1
    if gps_ignored and owner and not for_export:
        record["gx"] = 1
    if marks and (not for_export or options["marks"]):
        add_marks(record, marks)
    if described:
        record["ds"] = 1
        if row["prompt_ver"] <= 4:
            record["v4"] = 1
    return record


def add_video(record, row):
    """Add what a record says about a video (mv, dr, vc, vt; ty "video") from its row."""
    record["mv"] = 1
    # The kind of picture is "video" whatever the description says (the
    # model only saw a few of its frames; see videos.effective_info). The
    # model's own kind stays in vt when it is not a photo: a screen
    # recording is still a screenshot to an export's privacy options.
    kind = record.get("ty")
    if kind and kind not in ("photo", videos.MEDIA_VIDEO):
        record["vt"] = kind
    record["ty"] = videos.MEDIA_VIDEO
    duration = row["duration"]
    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
        # (Rounded down, as players show a length; site.js does the same.)
        record["dr"] = max(0.1, math.floor(duration * 10) / 10)
    facts = _load_info(row["vexif"]) if "vexif" in row.keys() else None
    facts = facts.get("video") if isinstance(facts, dict) else None
    facts = facts if isinstance(facts, dict) else {}
    is_360 = "projection" in row.keys() and row["projection"] == imageinfo.PROJECTION_DUAL_FISHEYE
    codec = _text(facts.get("codec"))
    if is_360:
        # The codec the player gets: a 360 video plays its LRV, or its
        # original when that holds both lenses in one stream; lens streams
        # play only as a playable copy (none: the page asks the server).
        lrv = facts.get("lrv") if isinstance(facts.get("lrv"), dict) else {}
        codec = _text(lrv.get("codec")) or (codec if facts.get("lenses") == "frame" else "")
    if codec:
        record["vc"] = codec[:20]
    if is_360:
        geometry = fisheye_record(facts.get("fisheye"))
        if geometry:
            record["fx"] = geometry
            if videos.level_of(facts):
                record["lv"] = 1


def fisheye_record(geometry):
    """A 360 video's lens geometry (videos.Video360.fisheye) checked for a record's fx, or None.

    Only finite numbers in the expected shapes pass (the player stitches
    the frames with them).
    """
    if not isinstance(geometry, dict):
        return None

    def numbers(value, count):
        if (not isinstance(value, list) or len(value) != count
                or not all(isinstance(item, (int, float)) and not isinstance(item, bool)
                           and math.isfinite(item) for item in value)):
            return None
        return [float(item) for item in value]

    lens = geometry.get("lens")
    lenses = [numbers(item, 3) for item in lens] if isinstance(lens, list) and len(lens) == 2 \
        else [None]
    gain = geometry.get("gain")
    gains = [numbers(item, 3) for item in gain] if isinstance(gain, list) and len(gain) == 2 \
        else [None]
    turn, fill = numbers(geometry.get("turn"), 2), numbers(geometry.get("fill"), 2)
    fov, blend = numbers([geometry.get("fov")], 1), numbers([geometry.get("blend")], 1)
    if None in lenses or None in gains or None in (turn, fill, fov, blend):
        return None
    if geometry.get("front") not in (0, 1) or not 90 < fov[0] < 360:
        return None
    return {"lens": lenses, "fov": fov[0], "turn": turn, "front": geometry["front"],
            "mirror": geometry.get("mirror") is True, "blend": blend[0], "gain": gains,
            "fill": fill}


def add_place(record, row):
    """Add the place names of a row (place_* columns, see places.py) to a record."""
    if not row["place_country"]:
        return
    for key, column in _PLACE_FIELDS:
        value = _text(row[column])
        if value:
            record[key] = value
    km = row["place_km"]
    if (record.get("ci") and isinstance(km, (int, float)) and math.isfinite(km)
            and km >= 0):
        record["km"] = int(round(km))


def picture_record(conn, image_id, edit=None):
    """The admin's search record (server variant, without dg) of one present picture, or None.

    edit is the picture's edit (image_edits) to lay over its description.
    """
    row = conn.execute("SELECT %s FROM images WHERE id = ? AND missing_since IS NULL"
                       % _INDEX_COLUMNS, (image_id,)).fetchone()
    if row is None:
        return None
    record = record_for_row(row, edit=edit, owner=True,
                            marks=albums.picture_marks(conn, image_id),
                            gps_ignored=gps_check.is_ignored(conn, image_id),
                            estimate=place_estimates.for_picture(conn, image_id))
    opening_views.annotate(conn, [record])
    return record


def record_tags(tags):
    """Tags as a search record keeps them (g): stripped, lower case, unique, in order."""
    return _phrases(tags, lower=True)


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
    shows up). The owner's edits are laid over the descriptions (an
    export too). Missing pictures are never included. Hidden pictures are
    included (with h: 1) unless include_hidden is False or for_export is
    set; an export never contains them. Records with hidden pictures are
    the admin's, and they also mark the edited pictures (ed) and those
    whose position the GPS check set aside (gx). for_export
    also leaves out what an export must not hold unless export_options
    allow it (see export_settings and the module notes): its records carry
    the owner's marks (fav, rt, al with the database's album ids) only
    with the "marks" option. Duplicate groups
    (dg) are added per library, split where the owner said pictures are
    not duplicates (see not_duplicates.py); use_cache=False works them out
    again instead of using the cached groups. The groups use the pictures'
    look vectors when data/image_vectors.db has them (group_looks). The
    clips of drives get dv, dx and dh (see drives.annotate).
    """
    library_ids = _configured_ids(config, library_ids)
    if not library_ids:
        return []
    with_hidden = include_hidden and not for_export
    sql = ("SELECT %s FROM images WHERE missing_since IS NULL AND library_id IN (%s)%s "
           "ORDER BY id" % (_INDEX_COLUMNS, ",".join("?" * len(library_ids)),
                            "" if with_hidden else " AND hidden = 0"))
    records = []
    edits = image_edits.for_libraries(conn, library_ids)
    marks = (albums.for_libraries(conn, library_ids)
             if not for_export or export_settings(export_options)["marks"] else {})
    # (Only the admin is told whose positions the GPS check set aside.)
    gps_ignored = gps_check.ignored_ids(conn, library_ids) if with_hidden else set()
    estimates = place_estimates.for_libraries(conn, library_ids)
    kept_apart = not_duplicates.pairs()
    keys = {}                   # image id -> not_duplicates.unit_key (when needed)
    # library id -> [(id, dhash, content hash, trusted date taken, aspect ratio)]
    items = collections.defaultdict(list)
    wanted = {}                 # image id -> its look key (looks.kind_of), for group_looks
    for row in conn.execute(sql, library_ids):
        record = record_for_row(row, for_export, export_options, edit=edits.get(row["id"]),
                                owner=with_hidden, marks=marks.get(row["id"]),
                                gps_ignored=row["id"] in gps_ignored,
                                estimate=estimates.get(row["id"]))
        records.append(record)
        if not row["hidden"]:
            # (No dHash: only exact copies of it are grouped.)
            dhash = "" if exact_copies_only(record) else row["dhash"]
            items[row["library_id"]].append(
                (row["id"], dhash, row["content_hash"],
                 trusted_date(row["taken_at"], row["taken_source"]),
                 aspect_ratio(row["width"], row["height"])))
            key = look_key(row, record)
            if key is not None:
                wanted[row["id"]] = key
            if kept_apart:
                keys[row["id"]] = not_duplicates.unit_key(row["id"], row["content_hash"])
    vectors = group_looks(wanted)
    groups = []
    for library_id in library_ids:
        if items.get(library_id):
            if vectors:
                items[library_id] = [item + (vectors.get(item[0]),)
                                     for item in items[library_id]]
            groups.extend(library_groups(library_id, items[library_id], use_cache=use_cache))
    apply_groups(records, not_duplicates.split_groups(groups, keys, kept_apart))
    # Runs of video clips recorded one after the other (drives.py).
    min_clips = drives.settings(config)["min_clips"]
    for library_id in library_ids:
        drives.annotate(records, drives.library_drives(conn, library_id, config or {}),
                        library_id, min_clips, owner=with_hidden)
    # Where 360 pictures open (opening_views.py).
    opening_views.annotate(conn, records)
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
    with a document type): pages of text look alike to a dHash. Videos too:
    a video never stacks with a photo, nor with another video unless it is
    the same file.
    """
    return (record.get("ty") in EXACT_ONLY_TYPES or bool(record.get("dt"))
            or bool(record.get("mv")))


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


def aspect_ratio(width, height):
    """width / height (the displayed size) as a float, or None when either is unknown."""
    try:
        width, height = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        return None
    if not (width > 0 and height > 0 and math.isfinite(width) and math.isfinite(height)):
        return None
    return width / height


def same_shape(first, second, tolerance=SHAPE_TOLERANCE):
    """True if two aspect ratios are at most tolerance apart (the larger at
    most 1 + tolerance times the smaller); an unknown one (None) fits any."""
    if first is None or second is None:
        return True
    return max(first, second) <= min(first, second) * (1.0 + tolerance) + 1e-9


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
    """(image id, dhash, content hash, trusted date, aspect ratio or None, look vector
    bytes or None) of an item.

    The trusted date, the aspect ratio and the look vector may be left out
    (no date; any shape; no look).
    """
    taken = item[3] if len(item) > 3 else ""
    aspect = item[4] if len(item) > 4 else None
    if (isinstance(aspect, bool) or not isinstance(aspect, (int, float))
            or not math.isfinite(aspect) or aspect <= 0):
        aspect = None
    look = item[5] if len(item) > 5 else None
    look = bytes(look) if isinstance(look, (bytes, bytearray, memoryview)) and look else None
    return (int(item[0]), item[1] or "", item[2] or "", taken or "",
            float(aspect) if aspect is not None else None, look)


def look_key(row, record):
    """The look a picture is grouped by, (content hash, looks.KIND_PHOTO), or None.

    row is its images row (content_hash, media, projection), record its
    search record. Only ordinary pictures have one: not documents,
    screenshots or videos (exact_copies_only), whose looks are alike
    whatever they say or show, nor 360-degree photos, whose look is that of
    their front view only.
    """
    if exact_copies_only(record) or not row["content_hash"]:
        return None
    kind = looks.kind_of(row)
    return (row["content_hash"], kind) if kind == looks.KIND_PHOTO else None


def group_looks(wanted):
    """{image id: look vector bytes} of the pictures wanted ({image id: (content hash,
    kind)}, see looks.kind_of) that have one in data/image_vectors.db.

    Only vectors of the store's length count. {} when there is no store (or it
    cannot be read): the groups then go by the dHashes alone.
    """
    if not wanted:
        return {}
    try:
        store = looks.connect(create=False)
    except sqlite3.Error:
        return {}
    if store is None:
        return {}
    try:
        current = looks.meta(store)
        found = looks.vectors_for(store, wanted.values())
    except sqlite3.Error:
        return {}
    finally:
        store.close()
    size = current["dims"] or current["native_dims"]
    result = {}
    for image_id, key in wanted.items():
        vec = found.get(key)
        if vec is not None and size and len(vec) == size:
            result[image_id] = vec
    return result


_ZEROS = {}                 # vector length -> a tuple of that many zeros (for math.dist)


def _look(vec):
    """(the signed numbers of a look vector, its length) of vector bytes, or None
    (no vector, or one of length 0)."""
    if not vec:
        return None
    numbers = struct.unpack("%db" % len(vec), vec)
    zeros = _ZEROS.get(len(numbers))
    if zeros is None:
        zeros = _ZEROS.setdefault(len(numbers), (0,) * len(numbers))
    length = math.dist(numbers, zeros)
    return (numbers, length) if length > 0 else None


def _look_cosine(first, second):
    """The cosine of two look vectors (_look's pairs; -2.0 when their lengths differ).

    The vectors are unit length times 127 rounded to whole numbers, so their
    lengths are 127 give or take about 1.5%: the dot product is divided by
    their actual lengths, not by 127 squared, which would add noise as large
    as the steps between the cut-offs above. math.dist works in C on every
    Python this runs on: the dot product is half of |a|^2 + |b|^2 - |a - b|^2.
    """
    if len(first[0]) != len(second[0]):
        return -2.0
    distance = math.dist(first[0], second[0])
    return ((first[1] * first[1] + second[1] * second[1] - distance * distance)
            / (2.0 * first[1] * second[1]))


def _shapes_fit(first, second, tolerance):
    """True if two groups' shapes ([narrowest, widest] aspect ratios, or None) fit:
    every two of their aspect ratios at most tolerance apart (see same_shape)."""
    if first is None or second is None:
        return True
    return same_shape(min(first[0], second[0]), max(first[1], second[1]), tolerance)


def _look_second(moment, crowd):
    """The capture second of a moment (see _second) for the look rules, or None: also
    when it is a day alone, or a second that crowd (a Counter of seconds) holds
    BURST_GROUPS or more of."""
    second = _second(moment)
    if second is None or crowd.get(second, 0) >= BURST_GROUPS:
        return None
    return second


def _any_apart(first, second, apart):
    """True if a picture of first and one of second ([(moment, _look())]) look too
    different to be duplicates (apart None: never): their looks at most apart
    close, or at most LOOK_APART_LATER close when both have a capture time
    and those are more than LOOK_STACK_SECONDS apart."""
    if apart is None:
        return False
    later = max(apart, LOOK_APART_LATER)
    for moment, one in first:
        second_one = _second(moment)
        for when, other in second:
            value = _look_cosine(one, other)
            if value <= -2.0:
                continue
            if value <= apart:
                return True
            if value <= later and second_one is not None:
                second_other = _second(when)
                if (second_other is not None
                        and abs(second_one - second_other) > LOOK_STACK_SECONDS):
                    return True
    return False


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

    Each group also keeps the narrowest and widest shape (aspect ratio) of
    its pictures: a picture only joins a group it fits with all of them
    (_fits; see SHAPE_TOLERANCE).

    With look vectors, no picture joins a group by its dHash when it and
    any picture of the group look too different (_apart); and groups()
    puts together the groups of about one moment whose representatives'
    looks are close enough (_look_stacks).
    """

    def __init__(self, threshold=DUPLICATE_THRESHOLD, undated_threshold=UNDATED_THRESHOLD,
                 burst_threshold=BURST_THRESHOLD, shape_tolerance=SHAPE_TOLERANCE,
                 look_threshold=None, look_apart=None):
        threshold = int(threshold)
        if threshold > 8:
            raise ValueError("the banded index finds distances up to 8 bits only")
        self.threshold = threshold
        self.undated_threshold = min(int(undated_threshold), threshold)
        self.burst_threshold = max(int(burst_threshold), threshold)
        self.shape_tolerance = float(shape_tolerance)
        self.look_threshold = None if look_threshold is None else float(look_threshold)
        self.look_apart = None if look_apart is None else float(look_apart)
        self.probes = _probes(threshold)
        self.undated_probes = _probes(self.undated_threshold)
        self.representatives = []               # dealt hashes
        self.when = []                          # per representative: its moment or None
        self.anchors = []                       # per group: its first trusted moment or None
        self.members = []                       # [ids] per representative
        self.shapes = []                        # per group: [narrowest, widest] or None
        self.looks = []                         # per representative: _look() or None
        # per group: (moment, _look()) of each unit (identical files) with a look
        self.unit_looks = []
        self.unit_counts = []                   # per group: how many units it holds
        self.everyone = _BandIndex()
        self.exact = {}                         # dealt hash -> [representative index]
        self.loose = _BandIndex()
        self.days = {}                          # anchor day -> [representative index]
        self.day_bands = {}                     # anchor day -> _BandIndex
        self.seconds = {}                       # capture second -> [representative index]
        self.alone = []                         # [ids] per unit that groups only as copies
        self.alone_info = []                    # per such unit: (moment, aspect, _look())
        # unit key -> ("g" | "a", index, dhash or None, moment or None, aspect or None,
        #              look vector bytes or None)
        self.units = {}
        self.max_id = None
        self.limited = 0                        # lookups cut short by MAX_CANDIDATES
        self.look_matches = 0                   # groups put into another by their looks
        self.look_singles = 0                   # ... of them a single picture of a burst
        self.look_vetoes = 0                    # dHash matches turned down by the looks

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

    def _fits(self, index, aspect):
        """True if a picture of this aspect ratio (or None) may join group index."""
        shape = self.shapes[index]
        if aspect is None or shape is None:
            return True
        return (same_shape(shape[0], aspect, self.shape_tolerance)
                and same_shape(shape[1], aspect, self.shape_tolerance))

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

    def _apart(self, index, moment, look):
        """True if a picture taken at moment (or None) whose look is look (or None) and
        any picture of group index look too different to be duplicates, however
        close their dHashes (_any_apart: every picture of the group counts, not
        just its first, so a picture turned down by one group does not then
        bring two different things together in another)."""
        if look is None or self.look_apart is None:
            return False
        if _any_apart([(moment, look)], self.unit_looks[index], self.look_apart):
            self.look_vetoes += 1
            return True
        return False

    def _nearest(self, dealt, moment, aspect, look=None):
        """The representative a picture joins (see duplicate_groups), or None."""
        representatives = self.representatives
        if moment is None:
            for index in self.exact.get(dealt, ()):
                if self._fits(index, aspect) and not self._apart(index, moment, look):
                    return index
            best, best_distance = None, self.undated_threshold + 1
            for bucket in self._buckets(self.everyone, dealt, self.undated_probes):
                for index in bucket:
                    distance = _bits(dealt ^ representatives[index])
                    # The nearest one; of equally near ones, the oldest.
                    if ((distance < best_distance or (distance == best_distance
                                                      and best is not None and index < best))
                            and self._fits(index, aspect) and not self._apart(index, moment, look)):
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
                if (distance <= limit and (rank, distance, index) < best_key
                        and self._fits(index, aspect) and not self._apart(index, moment, look)):
                    best, best_key = index, (rank, distance, index)
        if best_key[0] == 0:
            return best
        # Groups without any trusted date take it by its hash alone.
        for bucket in self._buckets(self.loose, dealt, self.undated_probes):
            for index in bucket:
                distance = _bits(dealt ^ representatives[index])
                if (distance <= self.undated_threshold and (1, distance, index) < best_key
                        and self._fits(index, aspect) and not self._apart(index, moment, look)):
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

    def _start_group(self, dealt, moment, aspect, ids, look=None):
        """Start a group of ids whose first picture has the dealt hash, moment,
        aspect ratio and look (_look() or None); its index."""
        index = len(self.representatives)
        self.representatives.append(dealt)
        self.when.append(moment)
        self.anchors.append(None)
        self.members.append(list(ids))
        self.shapes.append([aspect, aspect] if aspect is not None else None)
        self.looks.append(look)
        self.unit_looks.append([(moment, look)] if look is not None else [])
        self.unit_counts.append(1)
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

        Items are (image id, dhash, content hash[, trusted date[, aspect
        ratio[, look vector]]]); the trusted date is "" or a capture date to
        trust, "YYYY-MM-DDTHH:MM:SS" or "YYYY-MM-DD" (see trusted_date()); the
        aspect ratio is width / height as displayed, or None when unknown;
        the look vector its bytes (looks.py), or None.
        """
        items = sorted(_item(item) for item in items)
        if not items:
            return True
        if self.max_id is not None and items[0][0] <= self.max_id:
            return False
        fresh = collections.OrderedDict()       # unit key -> [ids], oldest first
        first_hash = {}
        first_moment = {}
        first_aspect = {}
        first_look = {}
        for image_id, dhash, content_hash, taken, aspect, look in items:
            key = ("c", content_hash) if content_hash else ("i", image_id)
            value = parse_dhash(dhash)
            moment = _moment(taken)
            known = self.units.get(key)
            if known is not None:
                if known[2] is None and value is not None:
                    return False
                if known[5] is None and look is not None:
                    return False
                # (Dates and shapes count for groups, and for the looks of
                # pictures that only group as copies by their dHashes.)
                if known[0] == "g" or known[5] is not None:
                    if known[3] is None and moment is not None:
                        return False
                    if known[4] is None and aspect is not None:
                        return False
                continue
            fresh.setdefault(key, [])
            if first_hash.get(key) is None:
                first_hash[key] = value
            if first_moment.get(key) is None:
                first_moment[key] = moment
            if first_aspect.get(key) is None:
                first_aspect[key] = aspect
            if first_look.get(key) is None:
                first_look[key] = look
        # Nothing has been changed so far; from here on add() cannot fail.
        for image_id, _dhash, content_hash, _taken, _aspect, _vec in items:
            key = ("c", content_hash) if content_hash else ("i", image_id)
            known = self.units.get(key)
            if known is not None:
                (self.members if known[0] == "g" else self.alone)[known[1]].append(image_id)
            else:
                fresh[key].append(image_id)
        for key, ids in fresh.items():          # oldest first: ids are increasing
            value = first_hash.get(key)
            moment = first_moment.get(key)
            aspect = first_aspect.get(key)
            vec = first_look.get(key)
            look = _look(vec) if (vec and (self.look_threshold is not None
                                           or self.look_apart is not None)) else None
            if value is None or _low_detail(value):
                self.units[key] = ("a", len(self.alone), value, moment, aspect, vec)
                self.alone.append(ids)
                self.alone_info.append((moment, aspect, look))
                continue
            dealt = _deal_bands(value)
            best = self._nearest(dealt, moment, aspect, look)
            if best is None:
                best = self._start_group(dealt, moment, aspect, ids, look)
            else:
                self.members[best].extend(ids)
                self.unit_counts[best] += 1
                if look is not None:
                    self.unit_looks[best].append((moment, look))
                if moment is not None and self.anchors[best] is None:
                    self.loose.remove(self.representatives[best], best)
                    self._anchor(best, moment)
                if aspect is not None:
                    shape = self.shapes[best]
                    if shape is None:
                        self.shapes[best] = [aspect, aspect]
                    else:
                        shape[0], shape[1] = min(shape[0], aspect), max(shape[1], aspect)
            self.units[key] = ("g", best, value, moment, aspect, vec)
        self.max_id = items[-1][0]
        return True

    def _look_stacks(self):
        """The members ([ids]) of every group, then of every unit that groups only
        as copies (alone), with those whose looks are alike put together (see
        duplicate_groups, "Looks").

        Two passes. First leader clustering again, one level up: the groups
        and the units alone (pictures with little detail, whose dHash says
        little, are among them) are taken by their first picture, and each
        whose representative has a look and a capture time (_look_second)
        joins the leader whose look is closest to its own among those whose
        representative was taken at most LOOK_STACK_SECONDS from it with a
        cosine of at least look_threshold, or at most LOOK_SAME_SECONDS from
        it with one of at least SAME_PICTURE_COSINE, if every shape of the
        two fits and no picture of the one looks too different from one of
        the other (_any_apart); else it leads. A group that joins comes
        whole, and leaders do not change, so the dHash groups stay as they
        are and nothing chains. Then a single picture (one unit: a picture
        and its identical files) still on its own joins the stack holding a
        picture taken at most BURST_SECONDS from it whose look is at least
        SAME_PICTURE_COSINE close (the next frame of a burst whose first
        frame was too far off), by the same shape and look rules: it brings
        nothing else along and is not compared with in turn, so this does
        not chain either.
        """
        members = [list(ids) for ids in self.members] + [list(ids) for ids in self.alone]
        if self.look_threshold is None:
            return members
        reps = self.looks + [info[2] for info in self.alone_info]
        moments = self.when + [info[0] for info in self.alone_info]
        shapes = ([list(shape) if shape is not None else None for shape in self.shapes]
                  + [[info[1], info[1]] if info[1] is not None else None
                     for info in self.alone_info])
        unit_looks = ([list(found) for found in self.unit_looks]
                      + [[(info[0], info[2])] if info[2] is not None else []
                         for info in self.alone_info])
        counts = self.unit_counts + [1] * len(self.alone)
        # (A second that this many groups and units started in is no moment:
        # a camera whose clock was never set; see BURST_GROUPS.)
        crowd = collections.Counter(_second(moment) for moment in moments)
        seconds = [_look_second(moment, crowd) for moment in moments]
        order = sorted(range(len(members)), key=lambda unit: members[unit][0])
        rank = {unit: place for place, unit in enumerate(order)}
        near_by = max(1, int(LOOK_STACK_SECONDS))
        span = max(near_by, int(LOOK_SAME_SECONDS))
        wide = max(self.look_threshold, SAME_PICTURE_COSINE)
        leaders = {}                            # second // span -> [(second, leading unit)]
        self.look_matches = self.look_singles = 0

        def join(best, unit):
            members[best].extend(members[unit])
            members[unit] = []
            if shapes[unit] is not None:
                shapes[best] = (list(shapes[unit]) if shapes[best] is None
                                else [min(shapes[best][0], shapes[unit][0]),
                                      max(shapes[best][1], shapes[unit][1])])
            unit_looks[best].extend(unit_looks[unit])
            unit_looks[unit] = []
            counts[best] += counts[unit]
            counts[unit] = 0

        def better(unit, other, value, best, best_value):
            # The closest; of equally close ones, the oldest; if they fit.
            return ((best is None or value > best_value
                     or (value == best_value and rank[other] < rank[best]))
                and _shapes_fit(shapes[other], shapes[unit], self.shape_tolerance)
                and not _any_apart(unit_looks[unit], unit_looks[other], self.look_apart))

        for unit in order:
            look, second = reps[unit], seconds[unit]
            if look is None or second is None:
                continue
            slot = second // span
            near = []
            for key in (slot - 1, slot, slot + 1):
                for other_second, other in leaders.get(key, ()):
                    if abs(other_second - second) <= span:
                        near.append((abs(other_second - second), rank[other], other))
            if len(near) > MAX_CANDIDATES:
                # (Only with very many pictures of about one moment.)
                near.sort()
                del near[MAX_CANDIDATES:]
                self.limited += 1
            best, best_value = None, None
            for gap, _rank, other in near:
                value = _look_cosine(look, reps[other])
                # (Taken further apart, they must look the same.)
                if (value >= (self.look_threshold if gap <= near_by else wide)
                        and better(unit, other, value, best, best_value)):
                    best, best_value = other, value
            if best is None:
                leaders.setdefault(slot, []).append((second, unit))
            else:
                self.look_matches += 1
                join(best, unit)
        # Single pictures: through any picture of a stack taken in the same burst.
        burst = {}                              # second -> [(look, stack)] of the stacks
        for stack in order:
            if counts[stack] > 1:
                for moment, look in unit_looks[stack]:
                    second = _look_second(moment, crowd)
                    if second is not None:
                        burst.setdefault(second, []).append((look, stack))
        for unit in order:
            look, second = reps[unit], seconds[unit]
            if counts[unit] != 1 or look is None or second is None:
                continue
            best, best_value = None, None
            for near in range(second - BURST_SECONDS, second + BURST_SECONDS + 1):
                for other_look, stack in burst.get(near, ()):
                    value = _look_cosine(look, other_look)
                    if value >= wide and better(unit, stack, value, best, best_value):
                        best, best_value = stack, value
            if best is not None:
                self.look_matches += 1
                self.look_singles += 1
                join(best, unit)
        return members

    def groups(self):
        """The groups of two or more pictures: sorted id lists, sorted by first id."""
        result = [sorted(ids) for ids in self._look_stacks() if len(ids) > 1]
        result.sort(key=lambda ids: ids[0])
        return result


def duplicate_groups(items, threshold=DUPLICATE_THRESHOLD, undated_threshold=UNDATED_THRESHOLD,
                     burst_threshold=BURST_THRESHOLD, shape_tolerance=SHAPE_TOLERANCE,
                     look_threshold=LOOK_STACK_COSINE, look_apart=LOOK_APART):
    """Near-duplicate groups: lists of image ids (sorted, two or more each), sorted by first id.

    items are (image id, dhash text, content hash, trusted date, aspect
    ratio, look vector) tuples of one library. The trusted date is the
    capture date "YYYY-MM-DDTHH:MM:SS" (or the day alone, "YYYY-MM-DD")
    when it comes from EXIF data or the file name, else "" (see
    trusted_date()); the aspect ratio is width / height as displayed
    (aspect_ratio()), or None when unknown; the look vector the picture's
    signed-byte vector from data/image_vectors.db (bytes, see looks.py), or
    None. An item of three counts as one without a date, one of four as
    one of unknown shape, one of five as one without a look. Exact copies
    (the same non-empty content hash) are always one group. Otherwise two
    pictures go together when their dHashes are close enough, or their
    looks, by these rules:

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
    - Shapes: a picture only joins a group when its aspect ratio is at
      most shape_tolerance (SHAPE_TOLERANCE) apart from that of every
      picture in it (see same_shape; an unknown one fits any), so a
      photo and a panorama, or a landscape and an upright picture, are
      never grouped unless they are identical files. It joins the nearest
      group it fits.
    - Pictures with little detail (see LOW_DETAIL_BITS) and pictures
      without a valid dHash (build_records leaves it out for documents,
      screenshots and videos, and gives those no look) only group as
      exact copies by their dHashes (a picture with little detail may
      still stack by its look).
    - Looks: a picture never joins a group by its dHash when its look and
      the look of any picture in the group are too different (_any_apart:
      a cosine of at most look_apart, LOOK_APART, or of at most
      LOOK_APART_LATER for two taken more than LOOK_STACK_SECONDS apart):
      alike things photographed one by one on the same plain background
      have close dHashes. Once the pictures are grouped by their dHashes,
      groups (a picture alone is a group of one) whose representatives
      have looks and capture times become one when those were taken at
      most LOOK_STACK_SECONDS apart and their looks have a cosine of at
      least look_threshold (LOOK_STACK_COSINE), or at most
      LOOK_SAME_SECONDS apart with looks at least SAME_PICTURE_COSINE
      close, if all their shapes fit and no two of their pictures look too
      different: leader clustering again, one level up, so a dHash group
      always stays whole and nothing chains; and a single picture left on
      its own joins a stack holding a picture of the same burst
      (BURST_SECONDS) whose look is at least SAME_PICTURE_COSINE close (see
      _Grouper._look_stacks). Pictures without a capture time (no trusted
      date, a day alone, or a second that BURST_GROUPS groups share) never
      group by their look: nothing says they are of one moment. None turns
      look_threshold or look_apart off.

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
    grouper = _Grouper(threshold, undated_threshold, burst_threshold, shape_tolerance,
                       look_threshold, look_apart)
    grouper.add(items)
    return grouper.groups()


def _sign_order(dims):
    """A fixed shuffled order of dims vector positions (Park-Miller, seed 4242)."""
    order = list(range(dims))
    seed = 4242
    for index in range(dims - 1, 0, -1):
        seed = seed * 48271 % 2147483647
        other = seed % (index + 1)
        order[index], order[other] = order[other], order[index]
    return order


def look_copies(items, threshold=None, banded=True):
    """Groups of pictures that look the same, whenever they were taken (the
    Duplicates page's "Same picture, any date").

    items are (image id, content hash, aspect ratio or None, look vector
    bytes) of the pictures to compare (of one library or of several).
    Identical files (one content hash) are one unit. Leader clustering, as duplicate_groups: units are taken
    by their first id, and each joins the leader whose look is closest to
    its own, if their cosine is at least threshold (SAME_PICTURE_COSINE)
    and every shape of the two fits (SHAPE_TOLERANCE); else it leads a
    group of its own. Returns sorted id lists of two or more, sorted by
    first id.

    Leaders are not compared one by one (that takes minutes on ten
    thousand pictures in Python): each look is turned into signs, one per
    number of the vector (above the items' mean in that place, or not),
    dealt in a fixed shuffled order into bands of LOOK_BAND_BITS signs,
    and a unit is only compared with the leaders that share all the signs
    of at least one band with it. Two looks at 0.99 differ in about one
    sign in thirteen, so they share a band of 14 nearly always (about 99.9
    times in 100 with 18 bands, as looks of 256 numbers have), while two
    unrelated pictures differ in about half their signs and share one of
    the bands about once in a thousand. The signs are taken against the
    mean, so they need a varied set: with LOOK_BAND_MIN pictures or fewer
    every leader is compared, and a library made mostly of one look (a
    webcam's thousand frames of one view) may miss a few of its copies.
    banded=False compares every leader (the tests check that both agree).
    """
    threshold = SAME_PICTURE_COSINE if threshold is None else float(threshold)
    units = collections.OrderedDict()           # content key -> [ids, aspect, look bytes]
    for image_id, content_hash, aspect, vec in sorted(items, key=lambda item: item[0]):
        if not vec:
            continue
        key = ("c", content_hash) if content_hash else ("i", image_id)
        unit = units.get(key)
        if unit is None:
            if (isinstance(aspect, bool) or not isinstance(aspect, (int, float))
                    or not math.isfinite(aspect) or aspect <= 0):
                aspect = None
            units[key] = [[image_id], aspect, bytes(vec)]
        else:
            unit[0].append(image_id)
    dims = max((len(unit[2]) for unit in units.values()), default=0)
    entries = []
    looks = []
    for unit in units.values():
        look = _look(unit[2]) if len(unit[2]) == dims else None
        if look is not None:
            entries.append(unit)
            looks.append(look)
    if len(entries) < 2:
        return [sorted(unit[0]) for unit in entries if len(unit[0]) > 1]
    bands = []
    banded = banded and dims >= LOOK_BAND_BITS and len(entries) > LOOK_BAND_MIN
    if banded:
        # (The mean of each place, and per place which byte values are above it.)
        means = [sum(column) / len(looks) for column in zip(*(look[0] for look in looks))]
        order = _sign_order(dims)
        tables = []
        for place in order:
            tables.append(bytes(1 if (value - 256 if value > 127 else value) > means[place] else 0
                                for value in range(256)))
        pick = operator.itemgetter(*order)
        count = max(1, dims // LOOK_BAND_BITS)
        for unit in entries:
            signs = bytes(map(operator.getitem, tables, pick(unit[2])))
            bands.append([signs[band * LOOK_BAND_BITS:(band + 1) * LOOK_BAND_BITS]
                          for band in range(count)])
    leaders = []                                # unit index of every leader
    indexes = collections.defaultdict(list)     # (band, signs) -> [leader number]
    members = []                                # per leader: [ids]
    shapes = []                                 # per leader: [narrowest, widest] or None
    for index, unit in enumerate(entries):
        if banded:
            seen = set()
            candidates = []
            for band, signs in enumerate(bands[index]):
                for number in indexes.get((band, signs), ()):
                    if number not in seen:
                        seen.add(number)
                        candidates.append(number)
        else:
            candidates = range(len(leaders))
        shape = [unit[1], unit[1]] if unit[1] is not None else None
        best, best_value = None, threshold
        for number in candidates:
            value = _look_cosine(looks[index], looks[leaders[number]])
            # The closest one; of equally close ones, the oldest.
            if ((value > best_value or (value == best_value and (best is None or number < best)))
                    and _shapes_fit(shapes[number], shape, SHAPE_TOLERANCE)):
                best, best_value = number, value
        if best is None:
            number = len(leaders)
            leaders.append(index)
            members.append(list(unit[0]))
            shapes.append(shape)
            if banded:
                for band, signs in enumerate(bands[index]):
                    indexes[(band, signs)].append(number)
            continue
        members[best].extend(unit[0])
        if shape is not None:
            if shapes[best] is None:
                shapes[best] = shape
            else:
                shapes[best] = [min(shapes[best][0], shape[0]), max(shapes[best][1], shape[1])]
    result = [sorted(ids) for ids in members if len(ids) > 1]
    result.sort(key=lambda ids: ids[0])
    return result


def _date_number(record):
    """The date taken as a number (YYYYMMDDhhmmss), or None."""
    digits = _NOT_DIGITS_RE.sub("", record.get("ts") or record.get("dk") or "")[:14]
    return int(digits.ljust(14, "0")) if len(digits) >= 8 else None


def cover_key(record):
    """Sort key of a group member as its cover (the largest key wins).

    The one the owner marked (albums.py: a favorite, then the higher
    rating, then one in an album; as the Duplicates page's best, see
    duplicates.rank_members; records carry the marks on the server, and in
    an export only with its "marks" option), then the best aesthetic, then
    sharpness, then the most pixels, then a picture with a camera name (an
    original rather than a copy that lost its EXIF data), then the oldest
    date (a re-saved or forwarded copy is dated by its file), then the
    lowest id (found first).
    """
    taken = _date_number(record)
    return (1 if record.get("fav") else 0, record.get("rt") or 0, 1 if record.get("al") else 0,
            record.get("q") or 0, record.get("sh") or 0,
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
    return hashlib.sha1(("groups %d %d %d %d %d %d %d %d %d %d %r %r %r %r %r %r %r\n" % (
        GROUPS_VERSION, DUPLICATE_THRESHOLD, UNDATED_THRESHOLD, BURST_THRESHOLD, BURST_SECONDS,
        BURST_GROUPS, SAME_TIME_SECONDS, LOW_DETAIL_BITS, MAX_CANDIDATES,
        DAY_INDEX_SIZE, SHAPE_TOLERANCE, LOOK_STACK_COSINE, LOOK_STACK_SECONDS, LOOK_SAME_SECONDS,
        LOOK_APART, LOOK_APART_LATER, SAME_PICTURE_COSINE)).encode("ascii"))


def _fingerprint_line(item):
    image_id, dhash, content_hash, taken, aspect, look = item
    line = ("%d %s %s %s %s %d\n" % (image_id, dhash, content_hash, taken,
                                     "-" if aspect is None else repr(aspect),
                                     len(look) if look else 0)).encode("utf-8", "replace")
    # (The look's bytes themselves: a rebuilt look changes the groups too.)
    return line + look if look else line


def _fingerprints(items, upto):
    """(a digest of what duplicate_groups() depends on, the same of the items with ids up to upto).

    items are sorted by id, each as _item() gives it.
    """
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

    items are (image id, dhash, content hash, trusted date, aspect ratio,
    look vector) of the library's present, visible pictures (see
    duplicate_groups). The groups are remembered in memory and in
    data/cache/groups-<library id>.json, keyed by a digest of exactly those
    inputs (_fingerprints): a new description, a rename or a new quality
    score never makes them be worked out again; a new, changed, hidden or
    removed picture does, and so does a new look. When the only change is
    new pictures (a scan found more), only those are added to the groups
    in memory (see _Grouper); anything else works them out from scratch.
    The result must not be changed.
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
    grouper = _Grouper(DUPLICATE_THRESHOLD, UNDATED_THRESHOLD, BURST_THRESHOLD,
                       SHAPE_TOLERANCE, LOOK_STACK_COSINE, LOOK_APART)
    grouper.add(items)
    return grouper


# ---------------------------------------------------------------------------
# /api/index

# Everything about the present pictures of a library that the records
# depend on, as sums: a row that is added, removed, hidden, described,
# rescanned, marked as a 360 photo or given a place changes at least one of
# them, also when the change did not touch the "updated" column.
_STATE_SQL = """
    SELECT library_id, COUNT(*), TOTAL(id), MAX(id), MAX(updated), TOTAL(updated),
        TOTAL(hidden * id), TOTAL(described * id), TOTAL(prompt_ver * id),
        TOTAL(length(projection) * id), TOTAL(length(dhash) * id),
        TOTAL(length(content_hash) * id), TOTAL(IFNULL(place_km, -2) * id)
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


def drives_key(config=None):
    """What the drives in the records depend on beyond the images rows: their settings
    (config's) and their descriptions (data/drives.json)."""
    return [drives.version(), sorted(drives.settings(config).items())]


def index_etag(conn, libraries, include_hidden, state=None, *, marks=None,
               config=None, looks_state=False):
    """The ETag (hex digits) of the /api/index answer for libraries ({id: name}).

    state is data_state() of the libraries when the caller already has it;
    marks (keyword only) albums.data_state() of them (worked out when
    missing: the owner's marks and albums, in a fixed order); config (keyword
    only) the configuration the records are built with (its drive settings);
    looks_state (keyword only) looks.store_key(), which the groups depend on
    (worked out when not given). Whether 360 videos are levelled (lv) counts
    too.
    """
    if state is None:
        state = data_state(conn, libraries)
    if marks is None:
        marks = albums.data_state(conn, libraries)
    if looks_state is False:
        looks_state = looks.store_key()
    key = [INDEX_VERSION, GROUPS_VERSION, DUPLICATE_THRESHOLD, UNDATED_THRESHOLD,
           BURST_THRESHOLD, BURST_SECONDS, BURST_GROUPS, SAME_TIME_SECONDS, LOW_DETAIL_BITS,
           SHAPE_TOLERANCE, prompts.PROMPT_VER, bool(include_hidden), not_duplicates.version(),
           [[library_id, name] for library_id, name in libraries.items()], state]
    key.append(marks)
    key.append(drives_key(config))
    key.append(looks_state)
    key.append(insta360.level_enabled())
    key.append(albums.people_searches_shown(config, include_hidden))
    return hashlib.sha1(json.dumps(key, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


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

    def _cached(self, key, compute):
        """compute(), remembered under key until the database changes."""
        # Read the counter first: a commit made while the state is worked
        # out then shows up as a new counter next time.
        version = self._data_version()
        with self._lock:
            if version is not None and version == self._version and key in self._states:
                return self._states[key]
        state = compute()
        with self._lock:
            if version != self._version:
                self._version = version
                self._states = {}
            if version is not None:
                self._states[key] = state
        return state

    def state(self, conn, library_ids):
        """data_state() of the libraries, computed again only after the database changed."""
        library_ids = list(library_ids)
        return self._cached(tuple(library_ids), lambda: data_state(conn, library_ids))

    def marks_state(self, conn, library_ids):
        """albums.data_state() of the libraries (the owner's marks and albums), likewise."""
        library_ids = list(library_ids)
        return self._cached(("marks",) + tuple(library_ids),
                            lambda: albums.data_state(conn, library_ids))

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
        # (Drive descriptions and settings change the drives' records too,
        # and so does the level_horizon setting a 360 video's lv.)
        row = (row, json.dumps(drives_key(config)), insta360.level_enabled())
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

    def compressed_body(self, conn, config, libraries, include_hidden, etag, state=None, *,
                        marks=None, looks_state=False):
        """The gzip-compressed JSON answer for etag, built if it is not remembered.

        state is the data_state() the ETag was made from, marks (keyword
        only) the albums.data_state() and looks_state (keyword only) the
        looks.store_key() (all worked out when missing); the records of a
        library are reused while its rows of the first two, and the third,
        are the same. The records are in id order, as build_records() gives
        them; the answer also lists the albums (albums.index_albums).
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
            if marks is None:
                marks = albums.data_state(conn, list(libraries))
            if looks_state is False:
                looks_state = looks.store_key()
            # (Pictures kept apart as not duplicates, and new looks, change the
            # groups too.)
            apart = not_duplicates.version()
            marked = marks.get("libraries", {})
            seen = json.dumps(looks_state)
            rows = {row[0]: tuple(row) + (apart, tuple(marked.get(row[0]) or ()), seen)
                    for row in state}
            lists = [self._library_records(conn, config, library_id, include_hidden,
                                           rows.get(library_id))
                     for library_id in libraries]
            lists = [pieces for pieces in lists if pieces]
            merged = lists[0] if len(lists) == 1 else heapq.merge(*lists)
            text = '{"version":%d,"generated":%d,"libraries":%s,"albums":%s,"records":[%s]}' % (
                INDEX_VERSION, int(time.time()),
                _json([{"id": library_id, "name": name}
                       for library_id, name in libraries.items()]),
                _json(albums.index_albums(conn, list(libraries), include_hidden,
                                          albums.people_searches_shown(config, include_hidden))),
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
        marks = self.marks_state(conn, list(libraries))
        looks_state = looks.store_key()
        etag = index_etag(conn, libraries, include_hidden, state, marks=marks,
                          config=config, looks_state=looks_state)
        sent = etag + "-gzip" if accept_gzip else etag
        headers = {"ETag": '"%s"' % sent, "Cache-Control": "private, no-cache",
                   "Vary": "Accept-Encoding", "X-Content-Type-Options": "nosniff"}
        if etag_matches is not None and (etag_matches(etag) or etag_matches(etag + "-gzip")):
            return 304, b"", headers
        data = self.compressed_body(conn, config, libraries, include_hidden, etag, state,
                                    marks=marks, looks_state=looks_state)
        if accept_gzip:
            headers["Content-Encoding"] = "gzip"
            return 200, data, headers
        return 200, gzip.decompress(data), headers
