#!/usr/bin/env python3
"""Drives: video clips a camera recorded one after the other, shown and searched as one.

A dashcam (and many action cameras) cuts what it records into short files,
a minute or a few each. Such a run of clips is a "drive": the search page
shows it as one tile with its whole length, the viewer plays it clip after
clip with a strip across the whole drive, and the describe step describes
it as a whole (from frames spread over all of it) as well as clip by clip.

Which clips make a drive (detect) is worked out from the index whenever it
is wanted (it takes a moment, and only the videos' rows): videos in the same
folder whose names follow the same pattern (the name with every run of
digits taken as one), of the same size and codec (never 360 videos, nor
any other .insv file of an Insta360 camera), where
each one starts at most "slack" seconds (default 20) after the one before
it ended. Cameras date a clip by its start or by its end; either way two
clips of one recording start one clip length apart, so a clip follows the
one before it when it starts no later than the longer of the two lengths
plus the slack after it, and not before the shorter one has passed (give
or take CUT_TOLERANCE: two cameras recording at the same time make two
drives, and so do videos that someone recorded over each other). And the
clip before must be one the camera cut, not one that was stopped: at
least CUT_SECONDS (30 s) long and as long as the other clips the camera
cut in that recording (the median of them, or the next clip while there
is none), give or take CUT_TOLERANCE (5 %, at least 2 s). So a shorter
clip ends the drive, as the engine stopping does; each recording is
judged by its own clips (a camera whose clip length was changed, or an
action camera that splits long recordings into chapters among clips
stopped by hand); and videos someone started and stopped by hand, one
after another (a phone's clips of one evening), vary in length and make
no drives. When the names hold a counter that goes up by one from a clip
to one that may follow it (most dashcams number their files), it must go
on: by one (then a last clip of a few seconds that a camera dates by its
end follows too), or by skipping up to MAX_SKIPPED numbers when the time
between them fits the missing clips: their files were deleted or kept
elsewhere (a "locked" event clip), and the drive goes on, with a hole
(record key dh). Any other jump starts another drive (another camera of
the same kind in the folder). A run of at least "min_clips" clips
(default 3) is a drive. Its id is its first clip's content hash, so it
keeps it while clips are added at its end, and so does its description.

Search records (search_index.build_records) get:

    dv   the drive: the id of its first clip in the records (on every clip
         of a drive; the search page folds the clips into one tile)
    dx   the clip's place in the drive (0, the first, is left out)
    dh   seconds missing before the clip (a hole, see above; and the
         clips of the drive left out of the records before it)
    dvt  dvd  dvg  the drive's own title, description and tags, and dvp
         (1: private information) and dvs (its sensitive level) its flags,
         on one clip of it: its first that is not hidden (search-core
         searches them as the drive's words, not that clip's)

Clips that are left out of the records (hidden ones for visitors, pictures
an export leaves out) are left out of their drive, which has a hole (dh)
where they were; a drive with fewer than min_clips clips left is no drive
(regroup). A drive's description is made
from frames of all of its clips: when one of them is hidden, only the
admin's records hold it (visitors, exports and the meaning vectors never
do, as for the drive's sped-up preview), and an export leaves out one
whose flags its options leave out (export_texts).

Descriptions of drives live in data/drives.json (beside index.db: they
cost a model's time, so they are not a cache), by library and drive id:

    {"format": 1, "libraries": {"<library id>": {"<drive id>": {
        "clips": "<clips key>", "count": clips, "length": seconds,
        "info": {the description, as for a picture}, "provenance": {...},
        "failures": runs that failed, "error": the last failure}}}}

The clips key changes when the drive does (a clip added or changed): the
drive is described again by the next describe run, and keeps its old
description until then. A new description that talks about its frames
instead of the drive ("a sequence of dashcam footage ...", see
sheet_talk) is asked for again once (describe.describe_checked); one that
does and was not asked again (its provenance has no "reasked") is out of
date, as a video's is: described again by a run that re-describes out of
date descriptions (redescribe_stale), unless config.json's
"video_sheet_check" is false. The search text
for meaning of the clip that carries the drive's words
(embeddings.library_texts, cover_texts) holds its title, description and
tags too, so drives are found by meaning as well (not those with a hidden
clip, or flagged private or sensitive).

Sped-up previews (hyperlapses, "drive_previews" in config.json, off by
default): make_previews (the "drive_previews" job) writes an H.264 copy of
each drive, speed times faster (60) and at most height pixels high (720),
without sound, into data/drive-previews/<library id>/. ffmpeg reads the
clips one after the other (its concat reader) and, when the clips have
key frames often enough, decodes only those (-skip_frame nokey), which
makes a sped-up copy many times quicker to make on the CPU.

The routes of drives (the GPS tracks of their clips, one after the other)
are tracks.py's (tracks.routes).

Run "python drives.py" to list the drives of the libraries; --sheet FOLDER
also writes their contact sheets (the 3 x 3 frames the model sees).

This module uses only the standard library (frames through videos.py).
"""

import argparse
import bisect
import collections
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time

import common
import imageinfo
import sheet_talk
import videos

# Seconds a clip may start after the one before it ended ("drives": "slack").
DEFAULT_SLACK = 20
SLACK_RANGE = (0, 600)
# Clips a drive has at least ("drives": "min_clips").
MIN_CLIPS = 3
MIN_CLIPS_RANGE = (2, 1000)
# A counter in the names may skip this many clips within a drive.
MAX_SKIPPED = 5
# Clips a camera cut at its usual length are at least this long (seconds), and
# as long as its usual clip give or take CUT_TOLERANCE (a part of it, at least
# CUT_SECONDS_OFF seconds).
CUT_SECONDS = 30
CUT_TOLERANCE = 0.05
CUT_SECONDS_OFF = 2.0
# Frames on a drive's contact sheet for describing (3 x 3).
SHEET_FRAMES = 9
FILE_NAME = "drives.json"
FORMAT = 1
# Sped-up previews ("drive_previews" in config.json).
PREVIEW_DEFAULTS = {"enabled": False, "speed": 60, "height": 720}
PREVIEW_SPEEDS = (2, 600)
PREVIEW_HEIGHTS = (144, 1080)
PREVIEW_FPS = 30
PREVIEW_CRF = 28
PREVIEW_FOLDER = "drive-previews"
# How much of a drive's first clip is read to see how often it has key frames.
KEYFRAME_SECONDS = 6
# Record keys of a drive's own text, and the description fields they hold.
TEXT_KEYS = (("dvt", "title"), ("dvd", "description"), ("dvg", "tags"))
# Record keys of its flags: private information (1) and its sensitive level.
FLAG_KEYS = ("dvp", "dvs")
SENSITIVE_LEVELS = ("suggestive", "explicit", "graphic")
# Longest drive text kept in a record (characters).
TEXT_LENGTH = 4000

_DIGITS_RE = re.compile(r"\d+")
_MOMENT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")
_SAFE_NAME_RE = re.compile(r"^[0-9a-f#]{1,64}$")


# ---------------------------------------------------------------------------
# Settings

def _int_setting(value, default, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return int(max(low, min(high, value)))


def settings(config):
    """config "drives" checked: {"slack" (seconds), "min_clips"}."""
    stored = config.get("drives") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    return {"slack": _int_setting(stored.get("slack"), DEFAULT_SLACK, *SLACK_RANGE),
            "min_clips": _int_setting(stored.get("min_clips"), MIN_CLIPS, *MIN_CLIPS_RANGE)}


def preview_settings(config):
    """config "drive_previews" checked: {"enabled", "speed", "height"} (see PREVIEW_DEFAULTS)."""
    stored = config.get("drive_previews") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    result = dict(PREVIEW_DEFAULTS)
    result["enabled"] = stored.get("enabled") is True
    result["speed"] = _int_setting(stored.get("speed"), PREVIEW_DEFAULTS["speed"],
                                   *PREVIEW_SPEEDS)
    result["height"] = _int_setting(stored.get("height"), PREVIEW_DEFAULTS["height"],
                                    *PREVIEW_HEIGHTS)
    return result


# ---------------------------------------------------------------------------
# Finding drives

Clip = collections.namedtuple(
    "Clip", "id path name folder pattern numbers start duration width height codec "
            "content_hash hidden taken_at")


class Drive:
    """A drive: its clips (Clip, in order) and the seconds missing before each one (holes)."""

    def __init__(self, clips, holes):
        self.clips = list(clips)
        self.holes = list(holes)
        first = self.clips[0]
        self.id = first.content_hash or "#%d" % first.id

    @property
    def key(self):
        """The image id of its first clip."""
        return self.clips[0].id

    @property
    def ids(self):
        return [clip.id for clip in self.clips]

    @property
    def length(self):
        """Seconds of video: the clips' lengths added up."""
        return sum(clip.duration for clip in self.clips)

    def offsets(self):
        """Seconds from the drive's start to each clip's start: the clips before it and
        the holes up to it."""
        found = []
        at = 0.0
        for index, clip in enumerate(self.clips):
            if index:
                at += self.clips[index - 1].duration + self.holes[index]
            found.append(at)
        return found

    @property
    def folder(self):
        return self.clips[0].folder

    @property
    def hidden(self):
        """True when any of its clips is hidden."""
        return any(clip.hidden for clip in self.clips)

    @property
    def clips_key(self):
        """A digest of its clips' contents: it changes when the drive does."""
        digest = hashlib.sha1()
        for clip in self.clips:
            digest.update(("%s\n" % (clip.content_hash or "#%d" % clip.id)).encode("ascii",
                                                                                "replace"))
        return digest.hexdigest()

    def __repr__(self):
        return "Drive(%s, %d clips, %.0f s)" % (self.id[:12], len(self.clips), self.length)


def name_pattern(name):
    """(pattern, numbers) of a file name: lower case with every run of digits before the
    extension as one "#", and those runs as numbers. "20230817_142005_0412_N_A.MP4" ->
    ("#_#_#_n_a.mp4", [20230817, 142005, 412])."""
    stem, extension = os.path.splitext(name or "")
    return (_DIGITS_RE.sub("#", stem) + extension).lower(), [
        int(digits) for digits in _DIGITS_RE.findall(stem)]


def moment(taken_at):
    """A taken_at text ('YYYY-MM-DDTHH:MM:SS') as seconds (from year 1), or None."""
    match = _MOMENT_RE.match(taken_at or "") if isinstance(taken_at, str) else None
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups()[:5])
    second = int(match.group(6) or 0)
    try:
        import datetime
        days = datetime.date(year, month, day).toordinal()
    except ValueError:
        return None
    return days * 86400.0 + hour * 3600 + minute * 60 + second


def _codec(exif_text):
    return str(videos.video_facts(exif_text).get("codec") or "")


def clip_from_row(row):
    """The Clip of a video row (id, path, name, taken_at, duration, width, height,
    content_hash, hidden and exif or codec; projection), or None when it cannot be in
    a drive (no time or no length, or a 360 video: the viewer plays those on their
    own; so is every .insv file, also of a camera videos.py reads as one lens)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "projection" in keys and row["projection"] in imageinfo.PROJECTIONS_360:
        return None
    path = row["path"] or ""
    name = row["name"] or path.rpartition("/")[2]
    if videos.is_360_video(name):
        return None
    start = moment(row["taken_at"])
    duration = row["duration"]
    if start is None or not isinstance(duration, (int, float)) or not duration > 0:
        return None
    pattern, numbers = name_pattern(name)
    codec = row["codec"] if "codec" in keys else _codec(row["exif"] if "exif" in keys else "")
    return Clip(int(row["id"]), path, name, path.rpartition("/")[0], pattern, numbers, start,
                float(duration), row["width"] or 0, row["height"] or 0, codec or "",
                row["content_hash"] or "", bool(row["hidden"]) if "hidden" in keys else False,
                row["taken_at"])


def _close(first, second):
    """True when two clip lengths are the same give or take CUT_TOLERANCE (at least
    CUT_SECONDS_OFF seconds)."""
    return abs(first - second) <= max(CUT_SECONDS_OFF, CUT_TOLERANCE * max(first, second))


def _earliest(prev, clip):
    """The fewest seconds after prev's start that clip may start as the next clip of
    the same recording: the shorter of the two lengths (a camera dates a clip by its
    start, so the next one starts as this one ends, or by its end), give or take
    CUT_TOLERANCE (at least CUT_SECONDS_OFF, for times in whole seconds)."""
    shorter = min(prev.duration, clip.duration)
    return shorter - max(CUT_SECONDS_OFF, CUT_TOLERANCE * shorter)


def _joined(prev, clip, slack):
    """True when clip starts as the next clip of one recording after prev would."""
    step = clip.start - prev.start
    return _earliest(prev, clip) <= step <= max(prev.duration, clip.duration) + slack


def cut_length(members):
    """The usual length of the clips a camera cut (the median of those of at least
    CUT_SECONDS), or None when there are none."""
    lengths = sorted(clip.duration for clip in members if clip.duration >= CUT_SECONDS)
    return lengths[len(lengths) // 2] if lengths else None


def _counter_step(before, after):
    """How far a counter went from before to after; a counter that wrapped (9999 ->
    0000) went on by one."""
    if after >= before:
        return after - before
    modulus = 10 ** len(str(before))
    return (after - before) % modulus if after < modulus else after - before


def _counter(members, slack):
    """Which run of digits in the names counts the clips, or None: the one that goes
    up by one from a clip to a clip that may follow it in time (see _joined; another
    camera's clips may come between them), for at least two clips and at least half
    of those that some clip may follow."""
    counts = collections.Counter()
    followed = 0
    longest = max((clip.duration for clip in members), default=0)
    for at, prev in enumerate(members):
        found = set()
        any_next = False
        for later in range(at + 1, len(members)):
            clip = members[later]
            if clip.start - prev.start > longest + slack:
                break               # (members are in time order)
            if len(prev.numbers) != len(clip.numbers) or not _joined(prev, clip, slack):
                continue
            any_next = True
            for index, (before, after) in enumerate(zip(prev.numbers, clip.numbers)):
                if _counter_step(before, after) == 1:
                    found.add(index)
        if any_next:
            followed += 1
            counts.update(found)
    if not counts:
        return None
    index, count = counts.most_common(1)[0]
    return index if count >= 2 and count * 2 >= followed else None


class _Run:
    """A drive being put together: its clips, the seconds missing before each one,
    and the lengths of the clips the camera cut (all of them but the last)."""

    def __init__(self, clip):
        self.clips = [clip]
        self.holes = [0.0]
        self.cut = []

    def cut_length(self):
        """The length the camera cuts this recording's clips at (the median of the
        clips cut so far), or None while it has one clip."""
        return self.cut[len(self.cut) // 2] if self.cut else None

    def add(self, clip, hole):
        bisect.insort(self.cut, self.clips[-1].duration)
        self.clips.append(clip)
        self.holes.append(hole)


def _follows(run, clip, slack, counter):
    """Seconds missing between a run's last clip and clip when clip goes on that drive
    (0.0 for none), or None when it does not.

    The last clip must be one the camera cut (_was_cut); the counter in the names,
    when there is one, must go on by one (or skip up to MAX_SKIPPED clips whose
    time fits between them)."""
    prev = run.clips[-1]
    step = clip.start - prev.start
    longer = max(prev.duration, clip.duration)
    reference = run.cut_length()
    if not _was_cut(prev, reference if reference is not None else clip.duration):
        return None             # the recording stopped there
    jump = None
    if counter is not None and len(prev.numbers) > counter and len(clip.numbers) > counter:
        jump = _counter_step(prev.numbers[counter], clip.numbers[counter])
    if jump == 1:
        # The next file: dated by its start or (some cameras) its end, which
        # for a last clip of a few seconds is hardly after the one before.
        return 0.0 if -CUT_SECONDS_OFF <= step <= longer + slack else None
    if jump is not None and not 2 <= jump <= MAX_SKIPPED + 1:
        return None             # another camera's clips, or another recording
    if step < _earliest(prev, clip):
        return None             # they overlap: not one camera's clips one after another
    if jump is None:
        return 0.0 if step <= longer + slack else None
    cut = reference if reference is not None else prev.duration
    if step <= longer + (jump - 1) * cut + slack:
        return max(0.0, step - longer)
    return None


def _was_cut(clip, reference):
    """True when the camera cut clip (so the recording went on): it is at least
    CUT_SECONDS long and as long as the recording's other clips (reference: the
    length the camera cuts at, or the next clip's while there is no other)."""
    return clip.duration >= CUT_SECONDS and _close(clip.duration, reference)


def detect(clips, slack=DEFAULT_SLACK, min_clips=MIN_CLIPS):
    """The drives among clips (Clip): [Drive], in the order of their first clips' times.

    See the module notes for the rules.
    """
    groups = collections.defaultdict(list)
    for clip in clips:
        if clip is not None:
            groups[(clip.folder, clip.pattern, clip.width, clip.height, clip.codec)].append(clip)
    found = []
    for members in groups.values():
        if len(members) < min_clips or cut_length(members) is None:
            continue
        members.sort(key=lambda clip: (clip.start, clip.numbers, clip.name, clip.id))
        counter = _counter(members, slack)
        # How long after a run's last clip started another one may still go on it.
        reach = (MAX_SKIPPED + 2) * max(clip.duration for clip in members) + slack
        # Each clip goes on the open run it follows best (two cameras of one kind
        # recording at once make two runs): the one whose counter it continues,
        # else the one with the shortest hole, else the closest in time.
        runs = []
        open_runs = []          # (those a later clip may still go on: in reach)
        for clip in members:
            open_runs = [run for run in open_runs
                         if clip.start - run.clips[-1].start <= reach]
            best, best_key = None, None
            for run in open_runs:
                last = run.clips[-1]
                hole = _follows(run, clip, slack, counter)
                if hole is None:
                    continue
                step = abs(clip.start - last.start - max(last.duration, clip.duration))
                continues = (counter is not None and len(last.numbers) > counter
                             and len(clip.numbers) > counter
                             and _counter_step(last.numbers[counter],
                                               clip.numbers[counter]) == 1)
                key = (0 if continues else 1, hole, step)
                if best_key is None or key < best_key:
                    best, best_key = run, key
            if best is None:
                best = _Run(clip)
                runs.append(best)
                open_runs.append(best)
            else:
                best.add(clip, best_key[1])
        found.extend(Drive(run.clips, run.holes) for run in runs if len(run.clips) >= min_clips)
    found.sort(key=lambda drive: (drive.clips[0].start, drive.key))
    return found


_ROWS_SQL = ("SELECT id, path, name, taken_at, duration, width, height, content_hash, hidden, "
             "exif, projection FROM images WHERE library_id = ? AND media = 'video' "
             "AND missing_since IS NULL ORDER BY id")

_memo_lock = threading.Lock()
_memo = collections.OrderedDict()      # library id -> (fingerprint, [Drive])
MEMO_SIZE = 32


def library_drives(conn, library_id, config=None):
    """The drives of a library's present videos (hidden ones included): [Drive].

    Worked out again only when its videos' rows changed (remembered per
    library); config's "drives" settings apply (default: config.json's).
    """
    if config is None:
        try:
            config = common.load_config()
        except common.ConfigError:
            config = {}
    chosen = settings(config)
    rows = conn.execute(_ROWS_SQL, (library_id,)).fetchall()
    # Clips follow each other by their camera's clock: a clip given the local
    # time of its place (local_times.py) goes by the clock it was recorded
    # with, so a drive across a time zone border stays one drive.
    import local_times
    clocks = local_times.camera_times(conn, [row["id"] for row in rows])
    if clocks:
        rows = [dict(row, taken_at=clocks[row["id"]][0]) if row["id"] in clocks else row
                for row in rows]
    digest = hashlib.sha1(("%d %d\n" % (chosen["slack"], chosen["min_clips"])).encode("ascii"))
    for row in rows:
        digest.update(("%d|%s|%s|%r|%s|%s|%s|%d|%s\n" % (
            row["id"], row["path"], row["taken_at"], row["duration"], row["width"],
            row["height"], row["content_hash"], 1 if row["hidden"] else 0,
            row["projection"])).encode("utf-8", "replace"))
    fingerprint = digest.hexdigest()
    with _memo_lock:
        found = _memo.get(library_id)
        if found is not None and found[0] == fingerprint:
            _memo.move_to_end(library_id)
            return found[1]
    drives = detect([clip_from_row(row) for row in rows], chosen["slack"], chosen["min_clips"])
    with _memo_lock:
        _memo[library_id] = (fingerprint, drives)
        while len(_memo) > MEMO_SIZE:
            _memo.popitem(last=False)
    return drives


def drive_of(conn, library_id, image_id, config=None):
    """The Drive that holds a picture of a library, or None."""
    for drive in library_drives(conn, library_id, config):
        if image_id in drive.ids:
            return drive
    return None


# ---------------------------------------------------------------------------
# Descriptions (data/drives.json)

_store_lock = threading.Lock()
_store_memo = {"stamp": None, "data": {}}


def store_path():
    """The file that keeps the drives' descriptions: data/drives.json."""
    return os.path.join(common.data_dir(), FILE_NAME)


def _stamp(path):
    try:
        stat = os.stat(path)
    except OSError:
        return (path, None, None)
    return (path, stat.st_mtime_ns, stat.st_size)


def _read(path):
    """{library id: {drive id: entry}} from the file ({} when there is none or it is broken)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        print("Warning: could not read %s: %s" % (path, error), file=sys.stderr)
        return {}
    libraries = data.get("libraries") if isinstance(data, dict) else None
    if not isinstance(libraries, dict):
        return {}
    result = {}
    for library_id, entries in libraries.items():
        if isinstance(library_id, str) and isinstance(entries, dict):
            result[library_id] = {drive_id: entry for drive_id, entry in entries.items()
                                  if isinstance(drive_id, str) and isinstance(entry, dict)}
    return result


def descriptions():
    """{library id: {drive id: entry}} of data/drives.json (do not change it)."""
    path = store_path()
    stamp = _stamp(path)
    with _store_lock:
        if _store_memo["stamp"] == stamp:
            return _store_memo["data"]
    data = _read(path)
    with _store_lock:
        _store_memo["stamp"] = stamp
        _store_memo["data"] = data
    return data


def version():
    """Text that changes whenever data/drives.json does (for the search index's ETag)."""
    _path, mtime, size = _stamp(store_path())
    return "%s-%s" % (mtime, size) if mtime is not None else ""


def _write(data):
    path = store_path()
    common.ensure_dir(os.path.dirname(path))
    common.write_file_atomic(path, json.dumps(
        {"format": FORMAT, "libraries": data}, ensure_ascii=True, sort_keys=True,
        separators=(",", ":")) + "\n")
    with _store_lock:
        _store_memo["stamp"] = None


_change_lock = threading.Lock()


def _change(change):
    """Read data/drives.json, change(data) it in place, and write it back (unless it
    returns False)."""
    with _change_lock:
        data = _read(store_path())
        if change(data) is not False:
            _write(data)


def entry_for(library_id, drive):
    """The stored entry of a drive (its description and failures), or None."""
    return descriptions().get(library_id, {}).get(drive.id)


def description(library_id, drive):
    """The stored description (dict) of a drive, or None (also while it is being made again)."""
    entry = entry_for(library_id, drive)
    info = entry.get("info") if entry else None
    return info if isinstance(info, dict) else None


def save_description(library_id, drive, info, provenance):
    """Keep a drive's description (for its current clips)."""
    def change(data):
        data.setdefault(library_id, {})[drive.id] = {
            "clips": drive.clips_key, "count": len(drive.clips),
            "length": round(drive.length, 3), "info": info, "provenance": provenance}
    _change(change)


def record_failure(library_id, drive, error):
    """Count a failed attempt at describing a drive (its description, if any, stays)."""
    def change(data):
        entry = dict(data.setdefault(library_id, {}).get(drive.id) or {})
        if entry.get("failed_clips") != drive.clips_key:
            entry["failures"] = 0
        entry["failed_clips"] = drive.clips_key
        entry["failures"] = int(entry.get("failures") or 0) + 1
        entry["error"] = common.redact(str(error))[:500]
        data[library_id][drive.id] = entry
    _change(change)


def prune(library_id, drives):
    """Forget the descriptions of a library's drives that are gone. Returns how many."""
    wanted = {drive.id for drive in drives}
    removed = []

    def change(data):
        entries = data.get(library_id) or {}
        for drive_id in list(entries):
            if drive_id not in wanted:
                del entries[drive_id]
                removed.append(drive_id)
        if not entries:
            data.pop(library_id, None)
        return bool(removed)
    if (descriptions().get(library_id) or {}).keys() - wanted:
        _change(change)
    return len(removed)


def forget_library(library_id):
    """Forget a library's drive descriptions and remove its sped-up previews (its data
    was deleted)."""
    def change(data):
        return data.pop(library_id, None) is not None
    if library_id in descriptions():
        _change(change)
    try:
        folder = _previews_folder(library_id)
    except ValueError:
        return
    shutil.rmtree(folder, ignore_errors=True)


def move_library(source_id, target_id):
    """Give a library id's drive descriptions and previews to another id (rows keep their ids)."""
    def change(data):
        entries = data.pop(source_id, None)
        if entries is None:
            return False
        data.setdefault(target_id, {}).update(entries)
    if source_id in descriptions():
        _change(change)
    try:
        source, target = _previews_folder(source_id), _previews_folder(target_id)
    except ValueError:
        return
    if os.path.isdir(source):
        common.ensure_dir(target)
        for name in os.listdir(source):
            try:
                os.replace(os.path.join(source, name), os.path.join(target, name))
            except OSError:
                pass
        shutil.rmtree(source, ignore_errors=True)


# ---------------------------------------------------------------------------
# Search records

def _clean_text(value):
    return value.strip()[:TEXT_LENGTH] if isinstance(value, str) else ""


def _tags(value):
    result = []
    for item in value if isinstance(value, list) else ():
        text = _clean_text(item).lower()
        if text and text not in result:
            result.append(text)
    return result


def is_private(info):
    """True when a drive's description is flagged as showing private information."""
    return isinstance(info, dict) and info.get("private_info") is True


def sensitive_level(info):
    """A drive's description's sensitive level ("suggestive", "explicit", "graphic"), or ''."""
    level = info.get("sensitive") if isinstance(info, dict) else None
    return level if level in SENSITIVE_LEVELS else ""


def text_fields(info):
    """The record keys of a drive's description: {dvt, dvd, dvg (only those it has),
    dvp, dvs (only when flagged)}."""
    fields = {}
    if not isinstance(info, dict):
        return fields
    for key, field in TEXT_KEYS:
        value = _tags(info.get(field)) if field == "tags" else _clean_text(info.get(field))
        if value:
            fields[key] = value
    if fields and is_private(info):
        fields["dvp"] = 1
    if fields and sensitive_level(info):
        fields["dvs"] = sensitive_level(info)
    return fields


def add_text(record, info):
    """Put a drive's description (title, description and tags, and its flags) on the
    record of the clip that carries it."""
    record.update(text_fields(info))


def _text_keys():
    return tuple(key for key, _field in TEXT_KEYS) + FLAG_KEYS


def _drop(record):
    for key in ("dv", "dx", "dh") + _text_keys():
        record.pop(key, None)


def annotate(records, drives, library_id, min_clips=MIN_CLIPS, owner=True):
    """Mark the clips of a library's drives in its records (dv, dx, dh, and the drive's
    own words and flags on its first clip there that is not hidden: the clip whose
    meaning vector holds them too, see cover_texts). Clips not in records are left
    out of their drive. owner: the records are the admin's; others (visitors, an
    export) get no words of a drive with a hidden clip. Returns the number of
    drives marked."""
    by_id = {record["i"]: record for record in records if record.get("l") == library_id}
    entries = descriptions().get(library_id, {})
    count = 0
    for drive in drives:
        present = [(index, clip) for index, clip in enumerate(drive.clips) if clip.id in by_id]
        if len(present) < min_clips:
            continue
        key = present[0][1].id
        offsets = drive.offsets()
        before = None
        for index, clip in present:
            record = by_id[clip.id]
            record["dv"] = key
            if index:
                record["dx"] = index
            if before is not None:
                # (The hole, and any clips left out of the records since.)
                _set_hole(record, offsets[index] - offsets[before] -
                          drive.clips[before].duration)
            before = index
        entry = entries.get(drive.id)
        carrier = next((clip for _index, clip in present if not clip.hidden), None)
        if entry and carrier is not None and (owner or not drive.hidden):
            add_text(by_id[carrier.id], entry.get("info"))
        count += 1
    return count


def _set_hole(record, seconds):
    """A record's dh: the seconds missing before it (none under a second)."""
    if seconds >= 1:
        record["dh"] = int(round(seconds))
    else:
        record.pop("dh", None)


def regroup(records, min_clips=MIN_CLIPS, texts=None, places=None):
    """Drives over the records at hand (an export leaves some pictures out): each one's
    dv becomes the id of its first clip there and its words and flags move to that
    clip (or, when the clip that had them is not there, come from texts: {image id:
    the description of its drive}, see clip_texts); one with fewer than min_clips
    clips there is no drive. places ({image id: (seconds from its drive's start,
    length)}, see clip_places) gives each clip after one left out a hole (dh) as long
    as what is missing there. In place."""
    groups = collections.defaultdict(list)
    for record in records:
        if record.get("dv") is not None:
            groups[record["dv"]].append(record)
    for members in groups.values():
        if len(members) < min_clips:
            for record in members:
                _drop(record)
            continue
        members.sort(key=lambda record: (record.get("dx") or 0, record["i"]))
        if places:
            members[0].pop("dh", None)
            for before, record in zip(members, members[1:]):
                start, length = places.get(before["i"], (None, None))
                then = places.get(record["i"], (None, None))[0]
                if start is not None and then is not None:
                    _set_hole(record, then - start - length)
        cover = members[0]
        text = {}
        for record in members:
            for key in _text_keys():
                if key in record:
                    text.setdefault(key, record.pop(key))
        if not text and texts:
            info = next((texts[record["i"]] for record in members if record["i"] in texts),
                        None)
            add_text(text, info)
        cover.update(text)
        for record in members:
            record["dv"] = cover["i"]


def clip_texts(conn, library_id, config=None):
    """{image id: its drive's description} of every clip of a library's described drives
    that an export may publish (none of their clips is hidden)."""
    entries = descriptions().get(library_id)
    result = {}
    if not entries:
        return result
    for drive in library_drives(conn, library_id, config):
        info = (entries.get(drive.id) or {}).get("info")
        if isinstance(info, dict) and not drive.hidden:
            for clip in drive.clips:
                result[clip.id] = info
    return result


def clip_places(conn, library_id, config=None):
    """{image id: (seconds from its drive's start to its start, its length)} of every
    clip of a library's drives (regroup's places)."""
    result = {}
    for drive in library_drives(conn, library_id, config):
        for clip, offset in zip(drive.clips, drive.offsets()):
            result[clip.id] = (offset, clip.duration)
    return result


def export_texts(records, options, min_clips=MIN_CLIPS, texts=None, places=None):
    """Drives over an export's records (regroup, with places: holes where clips are
    left out), keeping only the drives' words its options allow: none of a drive
    flagged private (exclude_private) or sensitive (exclude_sensitive), and without
    the location no GPS position written out in them. In place;
    returns {"drives": how many, "described": how many keep their own words,
    "private", "sensitive": how many lose them for that}."""
    regroup(records, min_clips, texts, places)
    summary = {"drives": 0, "described": 0, "private": 0, "sensitive": 0}
    for record in records:
        if record.get("dv") is None:
            continue
        if record["dv"] == record["i"]:
            summary["drives"] += 1
        if not any(key in record for key, _field in TEXT_KEYS):
            continue
        if not options.get("location"):
            # (No GPS position written out in its words without the location.)
            for key in ("dvt", "dvd"):
                if key in record:
                    record[key] = imageinfo.scrub_positions(record[key])
        reason = ("private" if record.get("dvp") and options.get("exclude_private")
                  else "sensitive" if record.get("dvs") and options.get("exclude_sensitive")
                  else None)
        if reason is None:
            summary["described"] += 1
            continue
        summary[reason] += 1
        for key in _text_keys():
            record.pop(key, None)
    return summary


def cover_texts(conn, library_ids, config=None):
    """{image id: the drive's description} of the first visible clip (the one annotate
    puts its words on) of every described drive of some libraries: the search by
    meaning adds its title, description and tags to that clip's text. Not for a drive
    with a hidden clip, nor one whose description is flagged private or sensitive
    (vectors are served to visitors and exported, and a vector would find them)."""
    result = {}
    stored = descriptions()
    for library_id in library_ids:
        entries = stored.get(library_id)
        if not entries:
            continue
        for drive in library_drives(conn, library_id, config):
            entry = entries.get(drive.id)
            info = entry.get("info") if entry else None
            if (not isinstance(info, dict) or drive.hidden or is_private(info)
                    or sensitive_level(info)):
                continue
            result[drive.clips[0].id] = info
    return result


# ---------------------------------------------------------------------------
# The contact sheet a drive is described from

def sheet_places(drive, count=SHEET_FRAMES):
    """[(clip index, seconds into it)] of count moments spread evenly over a drive's video."""
    places = []
    for moment_at in videos.sheet_times(drive.length, count):
        start = 0.0
        for index, clip in enumerate(drive.clips):
            if moment_at < start + clip.duration or index == len(drive.clips) - 1:
                places.append((index, max(0.0, min(clip.duration, moment_at - start))))
                break
            start += clip.duration
    return places


def drive_note(frames, drive):
    """The sentence the model is told about a drive's contact sheet."""
    minutes = max(1, int(round(drive.length / 60.0)))
    return ("This picture holds %d frames, in order (left to right, top to bottom), from a "
            "drive of %d minute%s made of %d clips. Describe the drive itself, as one trip: "
            "where it goes, the roads, places and scenery it passes through and what "
            "happens on the way. The title and the description are about the drive; they "
            "never mention frames, panels, a grid or a collage." % (
                frames, minutes, "" if minutes == 1 else "s", len(drive.clips)))


def drive_sheet(tools, root, drive, edge, count=SHEET_FRAMES):
    """(JPEG bytes, frames on it): count frames spread over a whole drive, in a grid.

    Raises videos.ToolsMissing without ffmpeg, images.SourceReadError when a
    clip cannot be read and videos.VideoReadError when no frame could be.
    """
    if tools is None:
        raise videos.ToolsMissing()
    infos = {}
    frames = []
    size = None
    for index, offset in sheet_places(drive, count):
        clip = drive.clips[index]
        path = os.path.join(root, *clip.path.split("/"))
        try:
            info = infos.get(index)
            if info is None:
                info = infos[index] = videos.parse_probe(videos.probe(tools, path), clip.name)
            if size is None:
                size = (info["width"], info["height"])
            layout = videos.sheet_layout(size[0], size[1], count, edge)
            cell = max(layout[2], layout[3])
            frame = videos.grab_frame(tools, path, offset, info, cell)
            if frame is None and offset > 0:
                frame = videos.grab_frame(tools, path, offset / 2.0, info, cell)
        except videos.ToolsMissing:
            raise
        except videos.VideoReadError as error:
            failure = videos._read_failure(error, path)
            if failure is not error:
                raise failure
            continue
        if frame is not None:
            frames.append(frame)
    if not frames:
        raise videos.VideoReadError("no frame of the drive could be read")
    return videos.contact_sheet(frames, size[0], size[1], edge, len(frames)), len(frames)


# ---------------------------------------------------------------------------
# Describing drives (the end of every describe run)

def _new_summary():
    return {"described": 0, "failed": 0, "pending": 0, "skipped": 0, "stopped": False,
            "error": "", "requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": None,
            "reasked": 0, "reask_fixed": 0, "reask_failed": 0}


def _count_usage(summary, result):
    """Add a describe_jpeg result's requests, tokens and cost to a summary."""
    summary["requests"] += result.get("requests") or 0
    for key in ("tokens_in", "tokens_out"):
        if result.get(key) is not None:
            summary[key] += result[key]
    if result.get("cost") is not None:
        summary["cost"] = (summary["cost"] or 0.0) + result["cost"]


def usage_text(summary):
    """What a summary used, to end a log line (" Tokens: N in, M out (about $X)."), or
    '' when it used no tokens."""
    if not summary.get("tokens_in") and not summary.get("tokens_out"):
        return ""
    text = " Tokens: %d in, %d out" % (summary["tokens_in"], summary["tokens_out"])
    if summary.get("cost") is not None:
        text += " (about $%.4f)" % summary["cost"]
    return text + "."


def pending(conn, config, libraries, redescribe_stale=False, retry_failed=False):
    """[(library, Drive)] of the drives that need a description: none yet, or one of
    other clips (the drive grew), or (redescribe_stale) one that is out of date: of an
    older prompt, or one that talks about its frames and was not asked again
    (sheet_talk.unsettled, unless config turns the check off). Drives that failed
    MAX_DESCRIBE_FAILURES runs wait for retry_failed."""
    import prompts
    check = sheet_talk.enabled(config)
    work = []
    for library in libraries:
        found = library_drives(conn, library["id"], config)
        entries = descriptions().get(library["id"], {})
        for drive in found:
            entry = entries.get(drive.id) or {}
            info = entry.get("info")
            provenance = entry.get("provenance") if isinstance(entry.get("provenance"),
                                                               dict) else {}
            current = isinstance(info, dict) and entry.get("clips") == drive.clips_key
            if current and not (redescribe_stale and (
                    prompts.is_stale(provenance.get("prompt_ver", 0), "drive")
                    or (check and sheet_talk.unsettled(info, provenance)))):
                continue
            if (not retry_failed and entry.get("failed_clips") == drive.clips_key
                    and int(entry.get("failures") or 0) >= common.MAX_DESCRIBE_FAILURES):
                continue
            work.append((library, drive))
    return work


def _routes(config, libraries, endpoint_name=None):
    """{library id: [endpoints with a model]} that describe each library's drives."""
    chosen = common.resolve_endpoints(config, endpoint_name) if endpoint_name else None
    routes = {}
    for library in libraries:
        found = chosen if chosen is not None else common.endpoints_for_library(config, library)
        routes[library["id"]] = [endpoint for endpoint in found if endpoint.get("model")]
    return routes


def endpoint_names(config, work, endpoint_name=None, routes=None):
    """The names of the endpoints the drives of work ([(library, Drive)]) go to."""
    if routes is None:
        routes = _routes(config, [library for library, _drive in work], endpoint_name)
    names = []
    for library, _drive in work:
        for endpoint in routes.get(library["id"]) or ():
            if endpoint["name"] not in names:
                names.append(endpoint["name"])
    return names


def reachable(work, ctx, timeout=10.0):
    """(work, how many were left out): work ([(library, Drive)]) without the drives of
    libraries whose folder cannot be reached now (an offline share); they wait for
    another run (one line per library says so)."""
    import library_ops      # (imports nothing that needs Pillow)
    offline = {}
    for library_id, library in {library["id"]: library for library, _drive in work}.items():
        root = common.library_root(library)
        if not library_ops.folder_status(root, timeout=timeout)["reachable"]:
            offline[library_id] = (common.library_label(library), root)
    if not offline:
        return work, 0
    kept = [(library, drive) for library, drive in work if library["id"] not in offline]
    for library_id, (label, root) in offline.items():
        count = sum(1 for library, _drive in work if library["id"] == library_id)
        ctx.log("Library %s: %d drive(s) wait for a description: the folder %s cannot be "
                "reached (offline?)." % (label, count, root or "(none)"), "warn")
    return kept, len(work) - len(kept)


def describe_drives(conn, config, ctx, libraries, endpoint_name=None, redescribe_stale=False,
                    retry_failed=False, servers=None, sleep=time.sleep, work=None):
    """Describe the drives of some libraries (dicts) that need it (see pending).

    Each drive is sent once, as a 3 x 3 contact sheet of frames spread over
    the whole drive (drive_sheet), to its library's endpoint (or the one
    endpoint_name names), with drive_note in the prompt. A drive with a
    hidden clip only goes to endpoints on this computer or the local
    network; the drives of a library whose folder cannot be reached wait
    (reachable). Model servers config.json's local_server runs are started
    when needed (as for pictures). work: the drives to describe when the
    caller found them already (pending, then reachable). Returns
    {"described", "failed", "pending", "skipped", "stopped", "error", "requests",
    "tokens_in", "tokens_out", "cost" (None when unknown), "reasked", "reask_fixed",
    "reask_failed" (drives asked again because their description talked about the
    frames, how many the second answer fixed, how many could not be asked again:
    see describe.describe_checked)}.
    """
    import describe
    import images
    import local_server
    import prompts
    summary = _new_summary()
    for library in libraries:
        found = library_drives(conn, library["id"], config)
        prune(library["id"], found)
    if work is None:
        work, summary["skipped"] = reachable(
            pending(conn, config, libraries, redescribe_stale, retry_failed), ctx,
            describe.FOLDER_CHECK_TIMEOUT)
    summary["pending"] = len(work)
    if not work:
        return summary
    tools = videos.find_tools(config)
    if tools is None:
        ctx.log("%d drive(s) wait for a description: %s" % (len(work), videos.TOOLS_MISSING),
                "warn")
        summary["skipped"] += len(work)
        return summary
    routes = _routes(config, libraries, endpoint_name)
    names = endpoint_names(config, work, endpoint_name, routes)
    if not names:
        ctx.log("%d drive(s) wait for a description: their libraries have no endpoint with a "
                "model." % len(work), "warn")
        summary["skipped"] += len(work)
        return summary
    retired = {}
    lease = local_server.lease_for(ctx, config, names, manager=servers)
    ctx.stage("drives", total=len(work))
    ctx.log("Describing %d drive(s) from %d frames each..." % (len(work), SHEET_FRAMES))
    try:
        for name, problem in lease.acquire().items():
            retired[name] = problem
        for library, drive in work:
            ctx.wait_if_paused()
            if ctx.should_stop():
                summary["stopped"] = True
                break
            route = [endpoint for endpoint in routes.get(library["id"]) or ()
                     if endpoint["name"] not in retired]
            if drive.hidden:
                route = [endpoint for endpoint in route if common.is_local_endpoint(endpoint)]
            if not route:
                summary["skipped"] += 1
                ctx.step(item=drive.clips[0].path)
                continue
            outcome = _describe_one(conn, config, ctx, library, drive, route[0], tools,
                                    describe, images, prompts, sleep, summary)
            if outcome == "ok":
                summary["described"] += 1
            elif outcome in ("fatal", "endpoint"):
                retired[route[0]["name"]] = outcome
                summary["failed"] += 1
                if outcome == "fatal":
                    summary["error"] = "The endpoint %s cannot be used." % route[0]["name"]
                    break
            elif outcome == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
            ctx.step(item=drive.clips[0].path)
    finally:
        lease.release()
    parts = ["Described %d drive(s)" % summary["described"]]
    if summary["failed"]:
        parts.append("%d failed" % summary["failed"])
    if summary["skipped"]:
        parts.append("%d skipped" % summary["skipped"])
    ctx.log(", ".join(parts) + "." + usage_text(summary))
    line = describe.reask_summary("drive", summary)
    if line:
        ctx.log(line)
    return summary


def _describe_one(conn, config, ctx, library, drive, endpoint, tools, describe, images,
                  prompts, sleep, summary):
    """Describe one drive with one endpoint: "ok", "skipped" (its clips cannot be read
    now), "failed", or the ProviderError kind "fatal" / "endpoint". Its requests,
    tokens and cost are added to summary."""
    settings_ = common.endpoint_settings(endpoint)
    edge = settings_.get("max_image_edge") or common.DEFAULT_MAX_IMAGE_EDGE
    root = common.library_root(library)
    label = "of %d clips from %s" % (len(drive.clips), drive.clips[0].path)
    try:
        sheet, frames = drive_sheet(tools, root, drive, edge)
        jpeg = images.prepare_for_llm(sheet, max_edge=edge)
    except images.SourceReadError as error:
        ctx.log("The drive %s: its clips cannot be read now (%s); it waits for another run."
                % (label, error), "warn")
        return "skipped"
    except (videos.VideoReadError, images.ImageReadError) as error:
        record_failure(library["id"], drive, error)
        ctx.log("Could not make the frames of the drive %s: %s" % (label, error), "warn")
        return "failed"
    profile = describe.library_profile(library)
    base = prompts.user_text(library)
    note = drive_note(frames, drive)
    head = prompts.USER_TEXT
    text = (head + "\n\n" + note + base[len(head):]) if base.startswith(head) \
        else base + "\n\n" + note
    if sheet_talk.enabled(config):
        result = describe.describe_checked(endpoint, jpeg, "drive", profile=profile,
                                           user_text=text, should_stop=ctx.should_stop,
                                           sleep=sleep)
    else:
        result = describe.describe_jpeg(endpoint, jpeg, profile=profile, user_text=text,
                                        should_stop=ctx.should_stop, sleep=sleep)
    _count_usage(summary, result)
    describe.reask_count(summary, result.get("reasked"))
    if result["status"] != "ok":
        kind = result.get("kind")
        record_failure(library["id"], drive, result.get("error") or kind)
        ctx.log("Could not describe the drive %s: %s" % (label, result.get("error") or kind),
                "warn")
        return kind if kind in ("fatal", "endpoint") else "failed"
    info = dict(result["info"])
    model = result.get("model") or settings_.get("model") or ""
    info["_model_name"] = model
    provenance = {"model": model, "endpoint": endpoint["name"],
                  "seconds": round(result["seconds"], 2),
                  "tokens_in": result.get("tokens_in"), "tokens_out": result.get("tokens_out"),
                  "prompt_ver": prompts.PROMPT_VER, "prompt_hash": prompts.prompt_hash(profile),
                  "profile": profile, "requests": result["requests"], "frames": frames,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if result.get("truncated") or result.get("repaired"):
        info = describe.fill_missing(info, result.get("missing"))
        provenance["truncated" if result.get("truncated") else "repaired"] = True
    describe.reask_provenance(provenance, result.get("reasked"))
    save_description(library["id"], drive, info, provenance)
    note = describe.reask_note(result.get("reasked"))
    ctx.log("Described the drive %s (%.1f s)%s" % (label, result["seconds"],
                                                    "; " + note if note else ""))
    return "ok"


# ---------------------------------------------------------------------------
# Sped-up previews (hyperlapses)

def previews_dir():
    """data/drive-previews."""
    return os.path.join(common.data_dir(), PREVIEW_FOLDER)


def _previews_folder(library_id):
    if not isinstance(library_id, str) or not common.LIBRARY_ID_RE.match(library_id):
        raise ValueError("invalid library id: %r" % (library_id,))
    return os.path.join(previews_dir(), library_id)


def preview_name(drive, speed, height):
    """The file name of a drive's sped-up preview: it changes with the drive and the settings."""
    drive_id = re.sub(r"[^0-9a-f]", "", drive.id.lower())[:16] or "x%d" % drive.key
    return "%s-%s-%dx-%d.mp4" % (drive_id, drive.clips_key[:12], int(speed), int(height))


def preview_path(library_id, drive, speed, height):
    """data/drive-previews/<library id>/<preview_name>."""
    return os.path.join(_previews_folder(library_id), preview_name(drive, speed, height))


def existing_preview(library_id, drive, config=None):
    """(path, speed) of a drive's sped-up preview for its current clips, or (None, None)."""
    chosen = preview_settings(config if config is not None else _config())
    try:
        path = preview_path(library_id, drive, chosen["speed"], chosen["height"])
    except ValueError:
        return None, None
    return (path, chosen["speed"]) if os.path.isfile(path) else (None, None)


def _config():
    try:
        return common.load_config()
    except common.ConfigError:
        return {}


def preview_usage():
    """{library id: {"count", "bytes"}} of the sped-up previews."""
    usage = {}
    try:
        folders = [entry for entry in os.scandir(previews_dir()) if entry.is_dir()]
    except OSError:
        return usage
    for folder in folders:
        count = total = 0
        try:
            for entry in os.scandir(folder.path):
                if entry.name.endswith(".mp4") and entry.is_file():
                    count += 1
                    total += entry.stat().st_size
        except OSError:
            continue
        usage[folder.name] = {"count": count, "bytes": total}
    return usage


def concat_list(paths):
    """The text of an ffmpeg concat list of files (absolute paths, quoted)."""
    lines = ["ffconcat version 1.0"]
    for path in paths:
        lines.append("file '%s'" % ("file:" + path).replace("'", "'\\''"))
    return "\n".join(lines) + "\n"


def keyframe_gap(tools, path):
    """The longest time (seconds) between key frames at the start of a video, or None
    when it cannot be told."""
    argv = tools.ffprobe + ["-v", "error", "-hide_banner", "-select_streams", "v:0",
                            "-show_entries", "packet=pts_time,flags", "-read_intervals",
                            "%%+%d" % KEYFRAME_SECONDS, "-of", "json", videos._input(path)]
    try:
        code, out, _err = videos._run(argv, videos.PROBE_TIMEOUT)
        data = json.loads(out.decode("utf-8", "replace")) if code == 0 else None
    except (videos.VideoReadError, ValueError):
        return None
    packets = data.get("packets") if isinstance(data, dict) else None
    if not isinstance(packets, list):
        return None
    times = []
    for packet in packets:
        if isinstance(packet, dict) and "K" in str(packet.get("flags") or ""):
            try:
                times.append(float(packet.get("pts_time")))
            except (TypeError, ValueError):
                continue
    times.sort()
    if len(times) < 2:
        return None
    return max(after - before for before, after in zip(times, times[1:]))


def preview_command(tools, list_file, target, speed=60, height=720, keyframes_only=False):
    """The ffmpeg command that makes a sped-up preview of the clips in list_file."""
    argv = tools.ffmpeg + ["-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if keyframes_only:
        argv += ["-skip_frame", "nokey"]
    chain = ["setpts=PTS/%d" % int(speed), "fps=%d" % PREVIEW_FPS,
             "scale=w='if(gt(iw,ih),-2,min(%d,trunc(iw/2)*2))':"
             "h='if(gt(iw,ih),min(%d,trunc(ih/2)*2),-2)'" % (height, height),
             "format=yuv420p"]
    return argv + [
        "-f", "concat", "-safe", "0", "-i", videos._input(list_file),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", ",".join(chain), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", str(PREVIEW_CRF), "-movflags", "+faststart", "-f", "mp4", videos._input(target)]


def make_preview(tools, paths, target, speed=60, height=720, length=None, timeout=None):
    """Write a sped-up preview of the clips (paths, in order) to target. Returns target.

    Only key frames are decoded when the first clip has one at least every
    speed / PREVIEW_FPS seconds (each frame of the preview stands for that
    much of the drive). Raises videos.VideoReadError.
    """
    if tools is None:
        raise videos.ToolsMissing()
    gap = keyframe_gap(tools, paths[0]) if paths else None
    keyframes_only = gap is not None and gap <= float(speed) / PREVIEW_FPS + 1e-6
    if timeout is None:
        timeout = max(videos.REMUX_TIMEOUT, int(2 * (length or 0)) + 120)
    common.ensure_dir(os.path.dirname(os.path.abspath(target)))
    list_file = "%s.list-%d.txt" % (target, os.getpid())
    try:
        with open(list_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(concat_list(paths))
        return videos._write_with(
            lambda temp: preview_command(tools, list_file, temp, speed, height, keyframes_only),
            target, timeout, "make a sped-up preview")
    finally:
        try:
            os.remove(list_file)
        except OSError:
            pass


def _remove_stale_previews(library_id, wanted):
    try:
        folder = _previews_folder(library_id)
        names = os.listdir(folder)
    except (OSError, ValueError):
        return 0
    removed = 0
    for name in names:
        if name not in wanted and re.match(r"^[0-9a-fx]+-[0-9a-f]+-\d+x-\d+\.mp4", name):
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    return removed


def make_previews(conn, config, ctx, library_ids):
    """The drive_previews job: a sped-up preview of every drive that has none.

    Previews of drives that are gone or changed are removed. Returns
    {"made", "kept", "failed", "removed", "stopped"}. Raises
    videos.ToolsMissing without ffmpeg.
    """
    chosen = preview_settings(config)
    tools = videos.find_tools(config)
    if tools is None:
        raise videos.ToolsMissing()
    summary = {"made": 0, "kept": 0, "failed": 0, "removed": 0, "stopped": False}
    work = []
    for library_id in library_ids:
        library = common.get_library(config, library_id) or {}
        found = library_drives(conn, library_id, config)
        wanted = {preview_name(drive, chosen["speed"], chosen["height"]) for drive in found}
        summary["removed"] += _remove_stale_previews(library_id, wanted)
        root = common.library_root(library)
        for drive in found:
            path = preview_path(library_id, drive, chosen["speed"], chosen["height"])
            if os.path.isfile(path):
                summary["kept"] += 1
            elif root:
                work.append((root, library_id, drive, path))
    ctx.stage("drive_previews", total=len(work))
    if work:
        ctx.log("Making sped-up previews of %d drive(s) (%dx, %dp)" % (
            len(work), chosen["speed"], chosen["height"]))
    for root, library_id, drive, path in work:
        ctx.wait_if_paused()
        if ctx.should_stop():
            summary["stopped"] = True
            break
        sources = [os.path.join(root, *clip.path.split("/")) for clip in drive.clips]
        started = time.monotonic()
        try:
            make_preview(tools, sources, path, chosen["speed"], chosen["height"], drive.length)
            summary["made"] += 1
            ctx.log("Made the sped-up preview of %d clips from %s (%.0f s, %.1f MB)" % (
                len(drive.clips), drive.clips[0].path, time.monotonic() - started,
                os.path.getsize(path) / 1048576.0))
        except (videos.VideoReadError, OSError) as error:
            summary["failed"] += 1
            ctx.log("Could not make a sped-up preview of the drive from %s: %s" % (
                drive.clips[0].path, error), "warn")
        ctx.step(item=drive.clips[0].path)
    return summary


def library_view(conn, config, library_id, usage=None):
    """A library's drives for the Manage page: {"count", "described", "seconds",
    "previews": {"count", "bytes", "pending" (None unless previews are on)}}."""
    found = library_drives(conn, library_id, config)
    entries = descriptions().get(library_id, {})
    usage = preview_usage() if usage is None else usage
    view = {"count": len(found),
            "described": sum(1 for drive in found if isinstance(
                (entries.get(drive.id) or {}).get("info"), dict)),
            "seconds": round(sum(drive.length for drive in found), 1),
            "previews": dict(usage.get(library_id) or {"count": 0, "bytes": 0}, pending=None)}
    chosen = preview_settings(config)
    if found and chosen["enabled"]:
        view["previews"]["pending"] = sum(
            1 for drive in found if not os.path.isfile(
                preview_path(library_id, drive, chosen["speed"], chosen["height"])))
    return view


# ---------------------------------------------------------------------------
# Command line

def _length(seconds):
    return videos.duration_text(seconds) or "0:00"


def main(argv=None):
    """python drives.py [--library NAME] [--sheet FOLDER]: list the drives (no data changes)."""
    import db
    parser = argparse.ArgumentParser(description="List the drives found among the videos.")
    parser.add_argument("--library", help="only this library (name or id)")
    parser.add_argument("--sheet", metavar="FOLDER",
                        help="also write each drive's contact sheet (as the model sees it) here")
    args = parser.parse_args(argv)
    try:
        config = common.load_config()
    except common.ConfigError as error:
        print(error)
        return 1
    libraries = [library for library in config.get("libraries", [])
                 if isinstance(library, dict) and library.get("id")]
    if args.library:
        wanted = common.get_library(config, args.library)
        if wanted is None:
            print("No library is named %r." % args.library)
            return 1
        libraries = [wanted]
    path = common.db_path()
    if not os.path.isfile(path):
        print("There is no index yet (%s)." % path)
        return 1
    conn = db.connect_readonly(path)
    tools = videos.find_tools(config) if args.sheet else None
    try:
        for library in libraries:
            found = library_drives(conn, library["id"], config)
            if not found:
                continue
            print("%s: %d drive(s), %s" % (common.library_label(library), len(found),
                                           _length(sum(drive.length for drive in found))))
            for number, drive in enumerate(found, 1):
                holes = sum(1 for hole in drive.holes if hole > 0)
                described_ = description(library["id"], drive)
                print("  %2d. %s  %3d clips  %s .. %s%s%s" % (
                    number, _length(drive.length).rjust(8), len(drive.clips),
                    drive.clips[0].path, drive.clips[-1].name,
                    "  (%d hole%s)" % (holes, "" if holes == 1 else "s") if holes else "",
                    "  - %s" % described_.get("title") if described_ else ""))
                if args.sheet and tools is not None:
                    edge = videos.sheet_edge(config, library)
                    sheet, _frames = drive_sheet(tools, common.library_root(library), drive,
                                                 edge)
                    common.ensure_dir(args.sheet)
                    name = "%s-%02d.jpg" % (library["id"], number)
                    with open(os.path.join(args.sheet, name), "wb") as handle:
                        handle.write(sheet)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
