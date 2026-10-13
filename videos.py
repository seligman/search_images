#!/usr/bin/env python3
"""Videos: what ffprobe says about them, posters, contact sheets and playable copies.

Videos are indexed like pictures when ffmpeg and ffprobe can be found (the
"ffmpeg" setting in config.json, else the PATH) and the library's "videos"
setting is on. read_video() reads one video and returns what
images.read_image() returns for a picture, plus media "video", the
duration and a contact sheet:

- the facts ffprobe gives (parse_probe): the duration, the displayed size
  (rotation and pixel shape applied), codecs, frame rate and bit rate,
  when it was taken (see creation_date: the QuickTime creation date with
  its UTC offset, else the container's creation time, which is UTC, in
  this computer's time zone -- as a photo's file time is -- unless it is
  when the file was converted, or a camera's own clock that agrees with
  the file name, else a date in the file name, else the file's time),
  the GPS position (ISO 6709) and the camera;
- a poster: the frame at min(1 s, 10 % of the duration), or a later one
  when that frame is black. It becomes the thumbnail, the preview and the
  dHash, as a photo's own picture does;
- a contact sheet for describing: frames (default 4) frames spread evenly
  over the video (at 12.5 %, 37.5 %, 62.5 % and 87.5 % for four), in a
  grid in one JPEG whose long edge is the library's describe size;
- a quick content hash (quick_hash): sha1 over the size and the first and
  last 4 MiB. Reading every byte of many large videos on a network share
  would take far too long; this still finds moved, renamed and copied
  videos, as the full hash does for pictures.

Programs run without a shell and with time limits, and frames are decoded
on the CPU (never with hardware decoding). HDR (HLG or PQ) frames are tone
mapped to ordinary colours when ffmpeg has the zscale filter. When a video
cannot be read because its file cannot (a share that went away, a program
that took too long), read_video raises images.SourceReadError, which a
scan does not record as a bad file: the next scan tries again.

Browsers play H.264 videos (and VP8, VP9 and AV1 in WebM); many cannot
play HEVC. "video_proxies" in config.json (off by default) lets a proxies
job write H.264 copies of the others into data/proxies/<library id>/,
which /video/<id> then serves instead (see make_proxies).

Insta360 360-degree videos (.insv) of the cameras whose lenses are known
here (CAMERAS_360) are read from the small copy the camera writes next to
them (the LRV), or from their two lenses, and their frames are stitched
like unstitched 360 photos (see Video360): the poster and the contact
sheet are equirectangular panoramas, and the viewer plays the LRV (or a
proxy with both lenses side by side) and stitches each frame as it is
shown. The LRV and the second lens's file of a video recorded as two files
are never indexed on their own (is_companion).

Run "python videos.py FILE" to see what is read from one video and where
ffmpeg was found.
"""

import collections
import datetime
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

import common
import imageinfo
import insta360

# Frames on a contact sheet (the "video_frames" setting, 1 to MAX_FRAMES).
DEFAULT_FRAMES = 4
MAX_FRAMES = 9
# The quick hash reads this much from the start and from the end of a video.
QUICK_HASH_BYTES = 4 * 1024 * 1024
# Seconds a program may take before it is stopped.
PROBE_TIMEOUT = 60
FRAME_TIMEOUT = 120
REMUX_TIMEOUT = 1800
# The poster is the frame at min(POSTER_SECONDS, POSTER_FRACTION x duration).
POSTER_SECONDS = 1.0
POSTER_FRACTION = 0.1
# A frame whose mean brightness (0-255) is below this is black; then the
# poster is taken later in the video (at these fractions of its length).
BLACK_LEVEL = 12
LATER_POSTERS = (0.25, 0.5)
SHEET_QUALITY = 88
SHEET_BACKGROUND = (24, 24, 24)
# How long a lookup of ffmpeg and ffprobe is trusted.
TOOLS_SECONDS = 60.0
# Proxies ("video_proxies" in config.json).
PROXY_DEFAULTS = {"enabled": False, "height": 720, "crf": 26}
PROXY_HEIGHTS = (240, 2160)
PROXY_CRF = (14, 40)

MEDIA_VIDEO = "video"
TAKEN_SOURCE = "video"          # the taken_source of a date found in the video's metadata
# A creation time later than the file's own time by more than this (seconds)
# cannot be when the video was recorded (the file is written as it is
# recorded): it is when an app converted or exported it. (A camera east of
# Greenwich that writes its local time as if it were UTC is caught too; its
# videos then keep their file's time, as photos without a date do, unless
# their names hold the time: see local_clock_time.)
CREATION_SLACK = 15 * 60

TOOLS_MISSING = ("Videos need ffmpeg and ffprobe: install ffmpeg, or set \"ffmpeg\" in "
                 "config.json to the ffmpeg program (ffprobe is looked for next to it).")

# Tone mapping from HDR (HLG, PQ) to ordinary (BT.709) colours; it needs
# ffmpeg's zscale filter (libzimg), which not every build has.
HDR_FILTER = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
              "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")

# Pixel formats every browser decodes for H.264 (8 bits, 4:2:0).
PLAYABLE_PIXEL_FORMATS = ("yuv420p", "yuvj420p")
PLAYABLE_CONTAINERS = ("mp4", "mov", "m4v")
WEBM_CODECS = ("vp8", "vp9", "av1")

# MIME types /video/<id> sends, by file extension: browsers play H.264 in
# QuickTime files when they are called MP4.
MIME_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".wmv": "video/x-ms-wmv", ".mts": "video/mp2t",
    ".m2ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".insv": "video/mp4",
}


class VideoReadError(Exception):
    """A video could not be read (see read_video); images.ImageReadError's role for videos."""


class ToolsMissing(VideoReadError):
    """ffmpeg or ffprobe is not there."""

    def __init__(self, message=TOOLS_MISSING):
        VideoReadError.__init__(self, message)


class VideoTimeout(VideoReadError):
    """A program took longer than its time limit (the file may be on a slow or lost share)."""


def _images():
    """The images module (Pillow), imported when first needed."""
    import images
    return images


def _source_error(message):
    """images.SourceReadError: the file could not be read at all (maybe for now)."""
    return _images().SourceReadError(message)


# ---------------------------------------------------------------------------
# Finding ffmpeg and ffprobe

class Tools:
    """The ffmpeg and ffprobe commands (lists: a program and any arguments before the rest).

    hdr_filter is False once ffmpeg turned out not to have the zscale
    filter, so HDR frames are converted without tone mapping from then on.
    (Another failure of the tone mapping, such as a video whose colours
    zscale cannot convert, only leaves it out for that frame or copy.)
    """

    def __init__(self, ffmpeg, ffprobe):
        self.ffmpeg = list(ffmpeg)
        self.ffprobe = list(ffprobe)
        self.hdr_filter = True

    def __repr__(self):
        return "Tools(%r, %r)" % (self.ffmpeg, self.ffprobe)


_tools_lock = threading.Lock()
_tools_cache = {}


def _sibling(program, name):
    """The program called name (ffprobe) in the folder of program (ffmpeg), or None."""
    folder, base = os.path.split(program)
    candidates = []
    lowered = base.lower()
    if "ffmpeg" in lowered:
        start = lowered.index("ffmpeg")
        # ffmpeg-7.exe -> ffprobe-7.exe, FFmpeg.EXE -> ffprobe.EXE
        candidates.append(base[:start] + name + base[start + len("ffmpeg"):])
    candidates += [name, name + ".exe"]
    for candidate in candidates:
        path = os.path.join(folder, candidate)
        if os.path.isfile(path):
            return path
    return None


def locate(setting=""):
    """(ffmpeg path, ffprobe path, problem) for an "ffmpeg" setting.

    The setting is the ffmpeg program, or the folder holding it (relative
    paths are relative to config.json's folder); "" looks on the PATH.
    ffprobe is looked for next to ffmpeg, then on the PATH. A path is None
    when the program was not found; problem then says what is wrong ("" when
    both were found).
    """
    setting = setting.strip() if isinstance(setting, str) else ""
    if not setting:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if ffmpeg and ffprobe:
            return ffmpeg, ffprobe, ""
        return ffmpeg, ffprobe, TOOLS_MISSING
    path = common.resolve_config_relative(setting)
    ffmpeg = None
    if os.path.isdir(path):
        for name in ("ffmpeg", "ffmpeg.exe"):
            if os.path.isfile(os.path.join(path, name)):
                ffmpeg = os.path.join(path, name)
                break
    elif os.path.isfile(path):
        ffmpeg = path
    if ffmpeg is None:
        return None, None, ("The ffmpeg program set in config.json (\"ffmpeg\": %s) was not "
                            "found." % path)
    ffprobe = _sibling(ffmpeg, "ffprobe") or shutil.which("ffprobe")
    if ffprobe is None:
        return ffmpeg, None, ("ffprobe was not found next to %s or on the PATH; it comes "
                              "with ffmpeg." % ffmpeg)
    return ffmpeg, ffprobe, ""


def find_tools(config=None):
    """Tools for the "ffmpeg" setting of config (default: config.json), or None when missing.

    Lookups are remembered for TOOLS_SECONDS, so a scan or a server asks
    the file system once in a while, not for every video.
    """
    if config is None:
        try:
            config = common.load_config()
        except common.ConfigError:
            config = {}
    setting = config.get("ffmpeg") if isinstance(config, dict) else ""
    key = (setting if isinstance(setting, str) else "", os.environ.get("PATH", ""))
    now = time.monotonic()
    with _tools_lock:
        found = _tools_cache.get(key)
        if found is not None and now - found[0] < TOOLS_SECONDS:
            return found[1]
    ffmpeg, ffprobe, _problem = locate(key[0])
    tools = Tools([ffmpeg], [ffprobe]) if ffmpeg and ffprobe else None
    with _tools_lock:
        _tools_cache[key] = (now, tools)
    return tools


def tools_status(config=None):
    """{"available", "ffmpeg", "ffprobe", "problem"} for the Manage page.

    The paths are the programs found ("" when not found); problem says what
    to do when they are not both there.
    """
    if config is None:
        try:
            config = common.load_config()
        except common.ConfigError:
            config = {}
    ffmpeg, ffprobe, problem = locate(config.get("ffmpeg", "") if isinstance(config, dict)
                                      else "")
    return {"available": bool(ffmpeg and ffprobe), "ffmpeg": ffmpeg or "",
            "ffprobe": ffprobe or "", "problem": problem}


# ---------------------------------------------------------------------------
# Settings

def frame_count(config):
    """The frames on a contact sheet: config "video_frames" (1..MAX_FRAMES, default 4)."""
    value = config.get("video_frames", DEFAULT_FRAMES) if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_FRAMES
    return max(1, min(MAX_FRAMES, value))


def sheet_edge(config, library):
    """The long edge of a library's contact sheets: its describe endpoint's max_image_edge."""
    for endpoint in common.endpoints_for_library(config, library):
        edge = common.endpoint_settings(endpoint).get("max_image_edge")
        if isinstance(edge, int) and edge > 0:
            return edge
    return common.DEFAULT_MAX_IMAGE_EDGE


def proxy_settings(config):
    """config "video_proxies" checked: {"enabled", "height", "crf"} (see PROXY_DEFAULTS)."""
    stored = config.get("video_proxies") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    result = dict(PROXY_DEFAULTS)
    result["enabled"] = stored.get("enabled") is True
    for key, (low, high) in (("height", PROXY_HEIGHTS), ("crf", PROXY_CRF)):
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(low, min(high, value))
    return result


class Setup:
    """What a scan needs to read videos: the tools, the frame count and the config.

    tools is None when ffmpeg or ffprobe is missing; then videos are
    listed (so indexed ones are never marked missing) but not read.
    """

    def __init__(self, config, tools=None, look=True):
        self.config = config if isinstance(config, dict) else {}
        self.tools = tools if tools is not None or not look else find_tools(self.config)
        self.frames = frame_count(self.config)

    def sheet_edge(self, library):
        return sheet_edge(self.config, library)

    def problem(self):
        """What to do to read videos (tools_status's problem), "" when they can be read."""
        if self.tools is not None:
            return ""
        return tools_status(self.config)["problem"] or TOOLS_MISSING


def setup(config=None):
    """A Setup for config (default: config.json)."""
    if config is None:
        try:
            config = common.load_config()
        except common.ConfigError:
            config = {}
    return Setup(config)


# ---------------------------------------------------------------------------
# Running the programs

def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def _run(argv, timeout):
    """Run a program (never through a shell). Returns (exit code, stdout bytes, stderr text).

    A program that runs longer than timeout seconds is stopped
    (VideoTimeout); one that cannot be started raises ToolsMissing.
    """
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=timeout,
                                   creationflags=_no_window())
    except subprocess.TimeoutExpired:
        raise VideoTimeout("%s took longer than %d seconds" % (
            os.path.basename(str(argv[0])), timeout))
    except OSError as error:
        raise ToolsMissing("Could not run %s: %s" % (argv[0], error))
    stderr = completed.stderr.decode("utf-8", "replace") if completed.stderr else ""
    return completed.returncode, completed.stdout or b"", stderr


def _input(path):
    # "file:" keeps ffmpeg from reading anything else into a name (a
    # protocol such as "concat:", or an option for a name starting with -).
    return "file:" + path


def _error_line(stderr, path=None):
    """The last meaningful line ffmpeg wrote, shortened.

    The input's name, which ffmpeg puts in front of its reason ("file:<the
    whole path>: Invalid data found when processing input"), is left out
    (the caller names the file), so a long path never hides the reason.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    line = lines[-1] if lines else "no output"
    if path:
        for name in (_input(path), path):
            line = line.replace(name + ": ", "").replace(name, os.path.basename(path))
    return line[:300]


# What ffmpeg and ffprobe say when a file could not be read (rather than
# understood): the share went away, the file was moved or locked.
_IO_WORDS = ("no such file or directory", "permission denied", "i/o error",
             "input/output error", "resource temporarily unavailable")


def _io_problem(error):
    """True when a failure says the file could not be read (or a program took too long)."""
    if isinstance(error, VideoTimeout):
        return True
    text = str(error).lower()
    return any(words in text for words in _IO_WORDS)


def _readable(path, size=None):
    """True when the file is still there (with this size) and its first and last byte read."""
    try:
        with open(path, "rb") as handle:
            found = os.fstat(handle.fileno()).st_size
            if size is not None and found != size:
                return False
            handle.read(1)
            if found > 1:
                handle.seek(found - 1)
                handle.read(1)
    except OSError:
        return False
    return True


def _read_failure(error, path, size=None):
    """The error to raise for a failure while reading path: images.SourceReadError when
    the file could not be read (so a scan does not record it as a bad file), else error."""
    if isinstance(error, ToolsMissing):
        return error
    if _io_problem(error) or not _readable(path, size):
        failure = _source_error("cannot read file: %s" % error)
        failure.__cause__ = error
        return failure
    return error


# ---------------------------------------------------------------------------
# What ffprobe says

def probe(tools, path):
    """ffprobe's JSON for a video (format and streams) as a dict. Raises VideoReadError."""
    argv = tools.ffprobe + ["-v", "error", "-hide_banner", "-show_format", "-show_streams",
                            "-of", "json", _input(path)]
    code, out, err = _run(argv, PROBE_TIMEOUT)
    if code != 0:
        raise VideoReadError("not a video ffprobe can read (%s)" % _error_line(err, path))
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        raise VideoReadError("ffprobe's answer could not be read")
    if not isinstance(data, dict):
        raise VideoReadError("ffprobe's answer could not be read")
    return data


def _number(value):
    """A finite float from a number or text, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(value):
    """'30000/1001' or '16:9' -> a float (None when not a positive ratio)."""
    if not isinstance(value, str):
        return _number(value)
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[/:]\s*(\d+(?:\.\d+)?)\s*$", value)
    if match:
        top, bottom = float(match.group(1)), float(match.group(2))
        return top / bottom if top > 0 and bottom > 0 else None
    number = _number(value)
    return number if number is not None and number > 0 else None


def _tags(item):
    """An item's tags with lower-case keys (text values only)."""
    tags = item.get("tags") if isinstance(item, dict) else None
    if not isinstance(tags, dict):
        return {}
    return {str(key).lower(): value.strip() for key, value in tags.items()
            if isinstance(value, str) and value.strip()}


def rotation_of(stream):
    """The clockwise turn (0, 90, 180 or 270) that shows a video stream upright."""
    degrees = None
    for item in stream.get("side_data_list") or ():
        if isinstance(item, dict) and _number(item.get("rotation")) is not None:
            # ffprobe gives the display matrix's angle counter-clockwise.
            degrees = -_number(item["rotation"])
            break
    if degrees is None:
        degrees = _number(_tags(stream).get("rotate"))     # older ffmpeg versions
    if degrees is None:
        return 0
    return int(round(degrees / 90.0)) * 90 % 360


_TAGGED_DATE_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?\s*"
    r"(Z|[+-]\d{2}:?\d{2})?\s*$", re.IGNORECASE)

# Tags that hold the local date and time of the recording (with the UTC
# offset, on Apple devices), most trusted first.
_LOCAL_DATE_KEYS = ("com.apple.quicktime.creationdate", "date", "date-eng",
                    "date_recorded")
_MAKE_KEYS = ("com.apple.quicktime.make", "make", "make-eng", "com.android.manufacturer",
              "manufacturer")
_MODEL_KEYS = ("com.apple.quicktime.model", "model", "model-eng", "com.android.model")
_LOCATION_KEYS = ("com.apple.quicktime.location.iso6709", "location", "location-eng",
                  "com.android.location")


def _datetime(year, month, day, hour, minute, second):
    try:
        when = datetime.datetime(int(year), int(month), int(day), int(hour), int(minute),
                                 int(second or 0))
    except (TypeError, ValueError, OverflowError):
        return None
    return when if imageinfo.YEAR_MIN <= when.year <= imageinfo.YEAR_MAX else None


def _utc_to_local(when):
    """A UTC datetime as this computer's local time (None for an unset 1904 / 1970 date)."""
    if when.year <= 1970:
        return None             # a zero creation time, not a recording date
    seconds = (when - datetime.datetime(1970, 1, 1)).total_seconds()
    return imageinfo.local_datetime(seconds)


def parse_tagged_date(text):
    """(local datetime, UTC offset) of a metadata date, or (None, "").

    '2014-03-22T10:15:37-0800' -> (10:15:37 on that day, '-08:00'); a UTC
    time ('...Z') is turned into this computer's local time, offset ''. A
    time without a zone is taken as it is.
    """
    match = _TAGGED_DATE_RE.match(text) if isinstance(text, str) else None
    if not match:
        return None, ""
    when = _datetime(*match.groups()[:6])
    if when is None:
        return None, ""
    zone = match.group(7)
    if zone and zone.upper() == "Z":
        return _utc_to_local(when), ""
    return when, imageinfo.normalize_offset(zone) if zone else ""


def _too_late(when, mtime):
    """True when a creation time is so much later than the file's own time that it
    cannot be when the video was recorded (it is when a copy was made)."""
    written = imageinfo.local_datetime(mtime) if mtime is not None else None
    return written is not None and (when - written).total_seconds() > CREATION_SLACK


# An Insta360 camera's file name: VID_<date>_<time>_<nn>_<number>.insv (or
# .mp4, and LRV_... .lrv or .insv for its low-resolution copy). The date
# and time are when the recording started, on the camera's own clock.
_INSTA360_NAME_RE = re.compile(
    r"^(VID|LRV)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_(\d{2})_(\d+)\.(insv|lrv|mp4)$",
    re.IGNORECASE)
# How far (minutes) a camera's local name time may be from its UTC creation
# time plus a whole number of quarter hours, and the largest UTC offset.
NAME_OFFSET_SLACK = 2
NAME_OFFSET_MAX = 14 * 60


def named_local_time(filename, format_tags, stream_tags):
    """(taken_at, tz_offset) of an Insta360 camera's video from its name, or None.

    The camera names a video by when it started on its own clock (as its
    photos' EXIF dates are) and writes the creation time in UTC, so their
    difference is the UTC offset where it was filmed: taken when it is a
    whole number of quarter hours (within NAME_OFFSET_SLACK minutes, at most
    NAME_OFFSET_MAX). VID_20190514_142301_00_007.insv created at
    2019-05-14T12:23:01Z -> ('2019-05-14T14:23:01', '+02:00'). A difference
    of zero says nothing (offset ""): older cameras (the ONE R) write their
    own clock's time as the creation time, marked as UTC.
    """
    match = _INSTA360_NAME_RE.match(os.path.basename(filename or ""))
    if not match:
        return None
    named = _datetime(*match.groups()[1:7])
    if named is None:
        return None
    for tags in (format_tags, stream_tags):
        found = _TAGGED_DATE_RE.match(tags.get("creation_time") or "")
        if not found or (found.group(7) or "").upper() != "Z":
            continue
        utc = _datetime(*found.groups()[:6])
        if utc is None or utc.year <= 1970:
            continue
        minutes = (named - utc).total_seconds() / 60.0
        quarters = int(round(minutes / 15.0)) * 15
        if abs(minutes - quarters) > NAME_OFFSET_SLACK or abs(quarters) > NAME_OFFSET_MAX:
            return None
        if quarters == 0:
            return imageinfo.format_datetime(named), ""
        return imageinfo.format_datetime(named), "%s%02d:%02d" % (
            "-" if quarters < 0 else "+", abs(quarters) // 60, abs(quarters) % 60)
    return None


# A camera that writes its own clock's time as the creation time, marked as
# UTC, and names its files by the same clock (dashcams): read as local time
# the creation time is at most this many seconds before the name's time, or
# after it by the video's length and this (some date a clip by its end).
LOCAL_CLOCK_SLACK = 2 * 60
# File names in UTC (Pixel phones' PXL_...): never a camera's own clock.
_UTC_NAME_RE = re.compile(r"^PXL_", re.IGNORECASE)
# Tags only phones write: their creation time is UTC for real.
_PHONE_TAG_PREFIXES = ("com.android.", "com.apple.")


def _clock_name(filename):
    """The date and time in a file name (a datetime) that may be a camera's own clock,
    or None: none in it, a date without a time, or a name in UTC."""
    if _UTC_NAME_RE.match(os.path.basename(filename or "")):
        return None
    named = imageinfo.parse_capture_date({}, filename, None)
    if named[1] != "filename" or named[0].endswith("T00:00:00"):
        return None
    return datetime.datetime.strptime(named[0][:19], "%Y-%m-%dT%H:%M:%S")


def _clock_matches(clock, start, duration):
    """True when clock (a creation time read as local time) is the name's start time,
    give or take LOCAL_CLOCK_SLACK, or its end (duration seconds later)."""
    length = duration if isinstance(duration, (int, float)) and duration > 0 else 0.0
    seconds = (clock - start).total_seconds()
    return -LOCAL_CLOCK_SLACK <= seconds <= length + LOCAL_CLOCK_SLACK


def local_clock_time(filename, format_tags, stream_tags, duration=None):
    """The time in a video's name (taken_at) when its creation time is the camera's own
    clock marked as UTC, or None.

    Dashcams (and other cameras that know no time zone) write the time of
    their own clock as the creation time, marked as UTC, and name the file
    by the same clock: 20230817_142005_0412_N_A.MP4 created at
    2023-08-17T14:21:05Z (the end of a one-minute clip). Read as local time,
    with no conversion, the creation time then agrees with the name (see
    _clock_matches), and the name's time (when it started) is taken, with
    no UTC offset. Phones write a real UTC time: never for a name in UTC
    (Pixel's PXL_...) or a video with a phone's own tags (com.android...,
    com.apple...), nor when the creation time converted to this computer's
    time agrees with the name (creation_date's usual rules take the name
    then: a phone, or a camera in this computer's time zone).
    """
    start = _clock_name(filename)
    if start is None or any(str(key).lower().startswith(_PHONE_TAG_PREFIXES)
                            for key in format_tags):
        return None
    for tags in (format_tags, stream_tags):
        found = _TAGGED_DATE_RE.match(tags.get("creation_time") or "")
        if not found or (found.group(7) or "").upper() != "Z":
            continue
        clock = _datetime(*found.groups()[:6])
        if clock is None or clock.year <= 1970:
            continue
        if not _clock_matches(clock, start, duration):
            return None
        converted = _utc_to_local(clock)
        if converted is not None and abs((converted - start).total_seconds()) <= CREATION_SLACK:
            return None
        return imageinfo.format_datetime(start)
    return None


def clock_candidate(filename, taken_at, duration=None):
    """True when a stored date may be a camera's own clock read as UTC: one from the
    creation time (source "video", no UTC offset) that, moved back by this computer's
    UTC offset at that time, agrees with the time in the name (as local_clock_time
    reads it). Such videos are read again (scan.redate_videos)."""
    start = _clock_name(filename)
    if start is None or not isinstance(taken_at, str):
        return False
    try:
        stored = datetime.datetime.strptime(taken_at[:19], "%Y-%m-%dT%H:%M:%S")
        clock = datetime.datetime(1970, 1, 1) + datetime.timedelta(
            seconds=time.mktime(stored.timetuple()))
    except (ValueError, OverflowError, OSError):
        return False
    return clock != stored and _clock_matches(clock, start, duration)


def creation_date(format_tags, stream_tags, filename="", mtime=None, duration=None):
    """(taken_at, taken_source, tz_offset) of a video.

    In order: the local date tags (the QuickTime creation date with its UTC
    offset first: phones write it when they record), source "video"; an
    Insta360 camera's name with its UTC creation time (named_local_time:
    the camera's local time, and the offset), source "video"; the name of a
    camera that writes its own clock's time as the creation time, marked as
    UTC (local_clock_time: a dashcam's; duration is the video's length),
    source "video", no offset; the container's (else the video stream's)
    creation time, which is UTC, in this computer's time zone, source
    "video" -- unless it is more than
    CREATION_SLACK later than the file's time, as in videos converted or
    exported by an app, whose creation time is when that was done; a date
    in the file name (as for photos), source "filename"; else the file's
    time (imageinfo.parse_capture_date). Some cameras write a creation time
    when the recording ended: a date and time in the file name at most
    CREATION_SLACK from it (when the recording started) is taken instead.
    A name in UTC (as Pixel phones write it) is hours off, so the creation
    time stays.
    """
    for key in _LOCAL_DATE_KEYS:
        when, offset = parse_tagged_date(format_tags.get(key))
        if when is not None:
            return imageinfo.format_datetime(when), TAKEN_SOURCE, offset
    named_local = named_local_time(filename, format_tags, stream_tags)
    if named_local is not None:
        return named_local[0], TAKEN_SOURCE, named_local[1]
    clock = local_clock_time(filename, format_tags, stream_tags, duration)
    if clock is not None:
        return clock, TAKEN_SOURCE, ""
    recorded = None
    for tags in (format_tags, stream_tags):
        when, offset = parse_tagged_date(tags.get("creation_time"))
        if when is not None:
            recorded = (when, offset)
            break
    if recorded is not None and _too_late(recorded[0], mtime):
        recorded = None
    named = imageinfo.parse_capture_date({}, filename, None)
    from_name = named[1] == "filename"
    if recorded is None:
        return named if from_name else imageinfo.parse_capture_date({}, filename, mtime)
    if from_name and not named[0].endswith("T00:00:00"):
        start = datetime.datetime.strptime(named[0][:19], "%Y-%m-%dT%H:%M:%S")
        if abs((recorded[0] - start).total_seconds()) <= CREATION_SLACK:
            return named
    return imageinfo.format_datetime(recorded[0]), TAKEN_SOURCE, recorded[1]


# Model names that are Apple's, for videos whose files name no maker.
_APPLE_MODELS = ("iphone", "ipad", "ipod")


def _camera(format_tags):
    """The camera of a video from its QuickTime (or other) maker and model tags."""
    make, model = _first(format_tags, _MAKE_KEYS), _first(format_tags, _MODEL_KEYS)
    if not make and model.lower().startswith(_APPLE_MODELS):
        # (Photos of the same phone name Apple as their maker.)
        make = "Apple"
    return imageinfo.camera_name({"Make": make, "Model": model})


# Colour facts zscale cannot do without, and what HDR video almost always
# uses (BT.2020) when its file leaves them out.
_UNKNOWN_COLOURS = ("", "unknown", "unspecified", "reserved")


def _hdr_input(stream):
    """zscale input options for colour facts an HDR stream leaves out ("" when it has them)."""
    options = []
    if str(stream.get("color_primaries") or "") in _UNKNOWN_COLOURS:
        options.append("pin=bt2020:p=bt2020")
    if str(stream.get("color_space") or "") in _UNKNOWN_COLOURS:
        options.append("min=bt2020nc")
    return ":".join(options)


_ISO6709_RE = re.compile(r"^\s*([+-]\d+(?:\.\d*)?)([+-]\d+(?:\.\d*)?)([+-]\d+(?:\.\d*)?)?"
                         r"(?:CRS[^/]*)?/?\s*$")


def _iso_part(text, degree_digits):
    """One ISO 6709 coordinate (+DD.D, +DDMM.M or +DDMMSS.S, signed; DDD for longitudes) in degrees."""
    sign = -1.0 if text.startswith("-") else 1.0
    body = text[1:]
    whole = body.split(".", 1)[0]
    value = float(body)
    if len(whole) <= degree_digits:
        degrees = value
    elif len(whole) == degree_digits + 2:
        degrees = int(whole[:degree_digits]) + (value - int(whole[:degree_digits]) * 100) / 60.0
    elif len(whole) == degree_digits + 4:
        head = int(whole[:degree_digits])
        minutes = int(whole[degree_digits:degree_digits + 2])
        seconds = value - head * 10000 - minutes * 100
        degrees = head + minutes / 60.0 + seconds / 3600.0
    else:
        return None
    return sign * degrees


def parse_iso6709(text):
    """(lat, lon) of an ISO 6709 position ('+38.1234-097.5678+150.250/'), or (None, None)."""
    match = _ISO6709_RE.match(text) if isinstance(text, str) else None
    if not match:
        return None, None
    try:
        lat = _iso_part(match.group(1), 2)
        lon = _iso_part(match.group(2), 3)
    except ValueError:
        return None, None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    if lat == 0 and lon == 0:
        return None, None       # no fix, written as zeros
    return lat, lon


def _first(tags, keys):
    for key in keys:
        if tags.get(key):
            return tags[key]
    return ""


def _container(format_info, tags, filename):
    """A short name of the container: mov, mp4, m4v, 3gp, mkv, webm, avi, ..."""
    names = str(format_info.get("format_name") or "").lower().split(",")
    first = names[0].strip() if names else ""
    extension = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if first == "mov":
        brand = str(tags.get("major_brand") or "").strip().lower()
        if brand == "qt":
            return "mov"
        if brand.startswith("3g2"):
            return "3g2"
        if brand.startswith("3gp"):
            return "3gp"
        if brand.startswith("m4v"):
            return "m4v"
        return "mp4"
    if first == "matroska":
        return "webm" if extension == "webm" else "mkv"
    return {"asf": "wmv", "mpegts": "mts", "mpeg": "mpeg", "mpegvideo": "mpeg"}.get(
        first, first or extension)


def _video_stream(streams):
    """The first video stream that is not a cover picture, or None."""
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        disposition = stream.get("disposition")
        if isinstance(disposition, dict) and disposition.get("attached_pic") == 1:
            continue
        return stream
    return None


def parse_probe(data, filename="", mtime=None):
    """The facts of a video from ffprobe's JSON (probe()). Raises VideoReadError.

    Returns {width, height (as shown: rotation and pixel shape applied),
    duration (seconds or None), taken_at, taken_source, tz_offset (see
    creation_date), camera, lat, lon, video: {codec, profile, fps, bitrate,
    audio_codec, rotation, container, pix_fmt, hdr ("hlg" / "pq", only for
    HDR)}, sar (pixel shape when not square, else None), span (the seconds
    that have pictures: the video stream's length when it is shorter than
    the file's, as when the sound runs on; else the duration) and
    hdr_input (zscale options for colour facts an HDR file leaves out)}.
    """
    if not isinstance(data, dict):
        raise VideoReadError("ffprobe gave no information")
    streams = [stream for stream in data.get("streams") or () if isinstance(stream, dict)]
    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    video = _video_stream(streams)
    if video is None:
        raise VideoReadError("the file has no video in it")
    width, height = _number(video.get("width")), _number(video.get("height"))
    if not width or not height or width <= 0 or height <= 0:
        raise VideoReadError("the video's picture size is unknown")
    width, height = int(width), int(height)
    sar = _ratio(video.get("sample_aspect_ratio"))
    sar = sar if sar is not None and abs(sar - 1.0) > 0.01 and 0.1 < sar < 10 else None
    if sar is not None:
        width = max(1, int(round(width * sar)))
    rotation = rotation_of(video)
    if rotation in (90, 270):
        width, height = height, width
    duration = _number(format_info.get("duration"))
    if duration is None or duration <= 0:
        duration = _number(video.get("duration"))
    duration = duration if duration is not None and duration > 0 else None
    own = _number(video.get("duration"))
    span = own if duration and own and 0 < own < duration else duration
    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    bitrate = _number(format_info.get("bit_rate")) or _number(video.get("bit_rate"))
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    format_tags, stream_tags = _tags(format_info), _tags(video)
    taken_at, source, offset = creation_date(format_tags, stream_tags, filename, mtime,
                                             duration)
    lat, lon = parse_iso6709(_first(format_tags, _LOCATION_KEYS))
    camera = _camera(format_tags)
    facts = {
        "codec": str(video.get("codec_name") or ""),
        "profile": str(video.get("profile") or ""),
        "fps": round(fps, 3) if fps and fps < 10000 else None,
        "bitrate": int(bitrate) if bitrate and bitrate > 0 else None,
        "audio_codec": str(audio.get("codec_name") or "") if audio else "",
        "rotation": rotation,
        "container": _container(format_info, format_tags, filename),
        "pix_fmt": str(video.get("pix_fmt") or ""),
    }
    transfer = video.get("color_transfer")
    hdr_input = ""
    if transfer in HDR_TRANSFERS:
        facts["hdr"] = "hlg" if transfer == "arib-std-b67" else "pq"
        hdr_input = _hdr_input(video)
    return {"width": width, "height": height, "duration": duration, "taken_at": taken_at,
            "taken_source": source, "tz_offset": offset, "camera": camera, "lat": lat,
            "lon": lon, "video": facts, "sar": sar, "span": span, "hdr_input": hdr_input}


def playable(facts):
    """Can a browser play a video as it is? "yes", "maybe" (HEVC: some can) or "no".

    facts is the video facts dict (parse_probe's "video", the exif column's
    "video"). H.264 in 8-bit 4:2:0 in an MP4 or QuickTime file, and VP8,
    VP9 or AV1 in WebM, play everywhere that matters.
    """
    facts = facts if isinstance(facts, dict) else {}
    codec, container = facts.get("codec"), facts.get("container")
    if codec == "h264" and container in PLAYABLE_CONTAINERS and (
            facts.get("pix_fmt") or "yuv420p") in PLAYABLE_PIXEL_FORMATS:
        return "yes"
    if codec in WEBM_CODECS and container == "webm":
        return "yes"
    if codec == "hevc" and container in PLAYABLE_CONTAINERS:
        return "maybe"
    return "no"


def mime_type(name):
    """The MIME type /video/<id> sends for a video file (by its extension)."""
    return MIME_TYPES.get(os.path.splitext(name or "")[1].lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# The quick hash

def quick_hash(path):
    """(sha1 hex, size) of a video: over its size and its first and last QUICK_HASH_BYTES.

    A file up to twice that size is hashed whole. Raises
    images.SourceReadError when the file cannot be read.
    """
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            digest.update(b"video:%d\n" % size)
            if size <= 2 * QUICK_HASH_BYTES:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            else:
                digest.update(handle.read(QUICK_HASH_BYTES))
                handle.seek(size - QUICK_HASH_BYTES)
                digest.update(handle.read(QUICK_HASH_BYTES))
    except OSError as error:
        raise _source_error("cannot read file: %s" % (error.strerror or error))
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# Frames, posters and contact sheets

def _scale_filter(edge):
    # Never larger than the video; quoted, as the expressions hold commas.
    return ("scale=w='min(iw,%d)':h='min(ih,%d)':force_original_aspect_ratio=decrease"
            % (edge, edge))


def hdr_filter(info):
    """HDR_FILTER for a video (info: parse_probe's result), BT.2020 assumed where its file
    leaves the colours out (zscale cannot convert them otherwise)."""
    extra = info.get("hdr_input") or ""
    if not extra:
        return HDR_FILTER
    return HDR_FILTER.replace("zscale=t=linear:npl=100", "zscale=t=linear:npl=100:" + extra, 1)


def _filter_missing(text):
    """True when ffmpeg said a filter (zscale) does not exist in this build."""
    text = str(text).lower()
    return "no such filter" in text or "filter not found" in text


def frame_filters(info, edge, tone_map=True):
    """The -vf chain for a frame of a video (info: parse_probe's result) at most edge pixels."""
    chain = []
    if info.get("sar"):
        chain.append("scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1")
    if tone_map and info.get("video", {}).get("hdr"):
        chain.append(hdr_filter(info))
    chain.append(_scale_filter(int(edge)))
    return ",".join(chain)


def grab_frame(tools, path, seconds, info, edge):
    """The frame at seconds as an RGB Pillow image at most edge pixels, or None when there is none.

    ffmpeg turns it upright (rotation) and, for HDR video, tone maps it
    when it can: when the tone mapping fails the frame is taken without it
    (and when ffmpeg has no zscale filter, every frame from then on: see
    Tools.hdr_filter). Raises VideoReadError when ffmpeg fails.
    """
    images = _images()

    def run(tone_map):
        argv = tools.ffmpeg + ["-nostdin", "-hide_banner", "-loglevel", "error"]
        if seconds and seconds > 0:
            argv += ["-ss", "%.3f" % seconds]
        argv += ["-i", _input(path), "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                 "-vf", frame_filters(info, edge, tone_map), "-f", "image2pipe", "-c:v", "bmp",
                 "pipe:1"]
        return _run(argv, FRAME_TIMEOUT)

    tone_map = tools.hdr_filter and bool(info.get("video", {}).get("hdr"))
    code, out, err = run(tone_map)
    if code != 0 and tone_map and not _io_problem(err):
        if _filter_missing(err):
            tools.hdr_filter = False    # this ffmpeg has no zscale
        # (Else this video's colours cannot be converted: this frame
        # without tone mapping, the next ones try again.)
        code, out, err = run(False)
    if code != 0:
        raise VideoReadError("ffmpeg could not read a frame (%s)" % _error_line(err, path))
    return _decode_frame(out, images)


def _decode_frame(out, images):
    """The RGB Pillow image of the BMP ffmpeg wrote (None when it wrote nothing)."""
    if not out:
        return None
    try:
        with images.Image.open(io.BytesIO(out)) as frame:
            frame.load()
            return images.to_rgb(frame)
    except Exception as error:
        raise VideoReadError("ffmpeg's frame could not be read (%s)" % error)


def _brightness(image):
    from PIL import ImageStat
    return ImageStat.Stat(image.convert("L")).mean[0]


def poster_times(duration):
    """Where to look for the poster: min(1 s, 10 %), then later when that frame is black."""
    if not duration:
        return [0.0]
    first = min(POSTER_SECONDS, POSTER_FRACTION * duration)
    return [first] + [fraction * duration for fraction in LATER_POSTERS]


def poster(tools, path, info, edge, grab=None):
    """The poster frame (an RGB image at most edge pixels). Raises VideoReadError when there is none.

    The frame at min(1 s, 10 % of the duration); when it is black (a fade
    in, a hand over the lens), the first later one of LATER_POSTERS that is
    not, else the brightest. grab(seconds, edge) takes the frames when
    given (a 360 video's are stitched, see Video360); else grab_frame.
    """
    if grab is None:
        def grab(seconds, size):
            return grab_frame(tools, path, seconds, info, size)
    best, best_level = None, -1.0
    for seconds in poster_times(info.get("duration")):
        frame = grab(seconds, edge)
        if frame is None and seconds > 0:
            continue
        if frame is None:
            break
        level = _brightness(frame)
        if level >= BLACK_LEVEL:
            return frame
        if level > best_level:
            best, best_level = frame, level
    if best is None and info.get("duration"):
        best = grab(0.0, edge)
    if best is None:
        raise VideoReadError("no frame could be read from the video")
    return best


def sheet_times(duration, count):
    """count moments spread evenly over a video: (k + 0.5) / count of its duration (or span)."""
    if not duration:
        return []
    return [(index + 0.5) / count * duration for index in range(count)]


def sheet_layout(width, height, count, edge):
    """(columns, rows, cell width, cell height, gap, sheet width, sheet height) of a contact sheet.

    count frames of a width x height video in a grid as near square as can
    be (2 x 2 for four), the sheet's long edge edge pixels.
    """
    count = max(1, int(count))
    columns = int(math.ceil(math.sqrt(count)))
    rows = int(math.ceil(count / float(columns)))
    gap = max(2, int(edge) // 128) if count > 1 else 0
    aspect = float(width) / float(height) if width and height else 16.0 / 9.0
    if columns * aspect >= rows:
        cell_w = max(1, (int(edge) - (columns - 1) * gap) // columns)
        cell_h = max(1, int(round(cell_w / aspect)))
    else:
        cell_h = max(1, (int(edge) - (rows - 1) * gap) // rows)
        cell_w = max(1, int(round(cell_h * aspect)))
    return (columns, rows, cell_w, cell_h, gap, columns * cell_w + (columns - 1) * gap,
            rows * cell_h + (rows - 1) * gap)


def contact_sheet(frames, width, height, edge, count=None):
    """JPEG bytes of frames (RGB images, in order) in a grid (see sheet_layout)."""
    images = _images()
    count = count or len(frames)
    columns, _rows, cell_w, cell_h, gap, sheet_w, sheet_h = sheet_layout(
        width, height, count, edge)
    sheet = images.Image.new("RGB", (sheet_w, sheet_h), SHEET_BACKGROUND)
    resample = getattr(images.Image, "Resampling", images.Image).LANCZOS
    for index, frame in enumerate(frames):
        cell = frame if frame.size == (cell_w, cell_h) else frame.resize((cell_w, cell_h),
                                                                          resample)
        column, row = index % columns, index // columns
        sheet.paste(cell, (column * (cell_w + gap), row * (cell_h + gap)))
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=SHEET_QUALITY)
    return buffer.getvalue()


def make_sheet(tools, path, info, edge, count=DEFAULT_FRAMES, poster_frame=None, grab=None):
    """(JPEG bytes, frames on it): the contact sheet of a video, count frames, long edge edge.

    The frames are spread over the part of the video that has pictures
    (info's span). A frame that cannot be read (past the last one) is
    looked for once more half way back to the moment before it, else left
    out; with none at all (or no known duration) the sheet is the poster
    alone. The describe step tells the model how many frames the sheet
    really holds. grab: as for poster().
    """
    if grab is None:
        def grab(seconds, size):
            return grab_frame(tools, path, seconds, info, size)
    _c, _r, cell_w, cell_h, _g, _w, _h = sheet_layout(info["width"], info["height"], count,
                                                    edge)
    frames = []
    before = 0.0
    for seconds in sheet_times(info.get("span") or info.get("duration"), count):
        frame = grab(seconds, max(cell_w, cell_h))
        if frame is None and seconds > before:
            frame = grab((before + seconds) / 2.0, max(cell_w, cell_h))
        if frame is not None:
            frames.append(frame)
        before = seconds
    if not frames:
        frame = poster_frame if poster_frame is not None else poster(
            tools, path, info, max(cell_w, cell_h), grab)
        return contact_sheet([frame], info["width"], info["height"], edge, 1), 1
    return (contact_sheet(frames, info["width"], info["height"], edge, len(frames)),
            len(frames))


# ---------------------------------------------------------------------------
# 360-degree videos (Insta360 .insv)
#
# An Insta360 camera saves a 360-degree video as VID_<date>_<time>_00_<n>.insv,
# an MP4 holding the picture of each lens in a stream of its own (the X4) --
# or, on cameras that write each lens to a file of its own (the ONE R), one
# lens, the other being in VID_<date>_<time>_10_<n>.insv. Next to it the
# camera writes a small H.264 copy with both lenses side by side,
# LRV_<date>_<time>_<nn>_<n>.lrv (or .insv), which browsers play. The poster
# and the contact sheet are made from that copy's frames -- or, without one,
# from the two lenses put side by side (ffmpeg's hstack filter, on the
# processor) -- stitched into equirectangular panoramas as unstitched 360
# photos are (images.dual_fisheye_to_equirect with the camera's lens model,
# see CAMERAS_360, and the pole fill settings). The viewer plays the copy and
# stitches each frame the same way (the lens geometry is kept in the facts:
# "fisheye"). The videos of a camera whose lenses are not known here are read
# as ordinary videos (the picture of one lens). An LRV and a second lens's
# file are never indexed on their own (is_companion). The camera's motion
# sensor says which way was up as it recorded (insta360.level_track, kept
# in the facts as "level"): the poster and the sheet's frames are turned
# level with it, and so is each frame the viewer shows (unless the
# "level_horizon" setting is off).

PROJECTION_360 = imageinfo.PROJECTION_DUAL_FISHEYE
INSV_EXTENSION = ".insv"
# The cameras whose 360 videos are stitched, by the model the trailer of
# their files names (insta360_model, compared without spaces or case): the
# name of their lens model in images.py, and which lens is on the left and
# which on the right of a frame holding both, as the camera's LRV has them --
# the video streams of one file (layout "streams": their indexes), or the
# two files (layout "files": 0 the _00_ file, 1 the _10_ one).
CAMERAS_360 = {
    "insta360x4": {"params": "INSTA360_VIDEO_PARAMS", "order": (1, 0)},
    "insta360oner": {"params": "INSTA360_ONE_R_VIDEO_PARAMS", "order": (0, 1)},
}
# The middle field of the names of a video recorded as two files.
FIRST_LENS, SECOND_LENS = "00", "10"
# The end of an Insta360 file (its "trailer") names the camera.
TRAILER_BYTES = 64 * 1024
_TRAILER_MAGIC = b"8db42d694ccc418790edff439fe026bf"
_TRAILER_MODEL_RE = re.compile(rb"\x12([\x01-\x3f])(Insta360[\x20-\x7e]*)")
# The maker Insta360 photos name in their EXIF, so a camera's videos and
# photos have the same camera name.
INSTA360_MAKER = "Arashi Vision"
# The frames of a 2:1 dual-fisheye picture may be this far from 2:1.
LENS_ASPECT_SLACK = 0.02
# A folder's names (to find an LRV or a second lens's file), as the server
# looks for them, are listed again after this many seconds (or when the
# folder's time changes, where the file system says so at once); this many
# folders are remembered. Reading a video always lists its folder now.
LISTING_SECONDS = 10.0
LISTINGS_KEPT = 64

_listing_lock = threading.Lock()
_listings = collections.OrderedDict()      # folder -> (its mtime_ns, when listed, names)


def is_360_video(name):
    """True for the name of a file read as a 360-degree video when it is one (an .insv)."""
    return isinstance(name, str) and name.lower().endswith(INSV_EXTENSION)


def _insta360_parts(name):
    """(kind "VID" or "LRV", date, time, middle field, number, extension) of an
    Insta360 camera's file name (the extension in lower case), or None."""
    match = _INSTA360_NAME_RE.match(os.path.basename(name or ""))
    if not match:
        return None
    groups = match.groups()
    return (groups[0].upper(), "".join(groups[1:4]), "".join(groups[4:7]), groups[7],
            groups[8], groups[9].lower())


def _folder_names(folder, cached=False):
    """The names in a folder, or () when it cannot be listed.

    cached: a listing made less than LISTING_SECONDS ago will do, unless
    the folder's time changed since (LISTINGS_KEPT folders are kept): the
    server looks for a video's LRV each time it is opened or played, and a
    listing of a folder on a share is slow. Else it is listed now (and
    kept).
    """
    folder = folder or "."
    try:
        stamp = os.stat(folder).st_mtime_ns
    except OSError:
        return ()
    now = time.monotonic()
    with _listing_lock:
        found = _listings.get(folder)
        if (cached and found is not None and found[0] == stamp
                and now - found[1] < LISTING_SECONDS):
            _listings.move_to_end(folder)
            return found[2]
    try:
        names = tuple(sorted(os.listdir(folder)))
    except OSError:
        return ()
    with _listing_lock:
        _listings[folder] = (stamp, now, names)
        _listings.move_to_end(folder)
        while len(_listings) > LISTINGS_KEPT:
            _listings.popitem(last=False)
    return names


def _same_recording(parts, other):
    """True when two name parts (_insta360_parts) have the same date, time and number."""
    return other[1:3] == parts[1:3] and other[4] == parts[4]


def _lens_file_name(parts, middle, names):
    """The name among names of the VID_ .insv of the recording parts names, with the
    middle field middle (FIRST_LENS, SECOND_LENS), whatever the case; or None."""
    for other in names:
        found = _insta360_parts(other)
        if (found and found[0] == "VID" and found[5] == "insv" and found[3] == middle
                and _same_recording(parts, found)):
            return other
    return None


def is_companion(name, names):
    """True for a file an Insta360 camera writes beside a 360 video, not indexed on its own.

    Its low-resolution copy: any LRV_<date>_<time>_<nn>_<number> name (.lrv
    is no video extension, but some cameras write .insv); and the second
    lens's file of a video recorded as two files, VID_..._10_<number>.insv
    when its VID_..._00_<number>.insv is among names (the other names in
    its folder). A second lens's file alone is indexed (as one lens).
    """
    parts = _insta360_parts(name)
    if parts is None:
        return False
    if parts[0] == "LRV":
        return True
    return (parts[3] == SECOND_LENS and parts[5] == "insv"
            and _lens_file_name(parts, FIRST_LENS, names or ()) is not None)


def lrv_companion(path, hint=None, cached=False):
    """The path of an Insta360 video's low-resolution copy (its LRV), or None.

    For VID_<date>_<time>_<nn>_<number>.insv that is the file in the same
    folder named LRV_<date>_<time>_<any nn>_<number>.lrv (or .insv, as
    older cameras name it): the same date, time and number, whatever the
    case of the letters. hint is the name a scan found (looked at first);
    cached: a recent listing of the folder will do (_folder_names). The
    second lens's file of a video recorded as two files has none: the LRV
    is its first file's.
    """
    folder, name = os.path.split(path or "")
    parts = _insta360_parts(name)
    if parts is None or parts[0] != "VID" or parts[5] != "insv":
        return None
    names = None
    if parts[3] == SECOND_LENS:
        names = _folder_names(folder, cached)
        if _lens_file_name(parts, FIRST_LENS, names) is not None:
            return None
    hinted = _insta360_parts(hint) if isinstance(hint, str) else None
    if (hinted and hinted[0] == "LRV" and hinted[5] in ("lrv", "insv")
            and _same_recording(parts, hinted)):
        candidate = os.path.join(folder, os.path.basename(hint))
        if os.path.isfile(candidate):
            return candidate
    for other in _folder_names(folder, cached) if names is None else names:
        found = _insta360_parts(other)
        if (found and found[0] == "LRV" and found[5] in ("lrv", "insv")
                and _same_recording(parts, found)):
            candidate = os.path.join(folder, other)
            if os.path.isfile(candidate):
                return candidate
    return None


def second_lens_file(path, cached=False):
    """The file with the other lens of an Insta360 video recorded as two files, or None.

    For VID_<date>_<time>_00_<number>.insv: VID_<date>_<time>_10_<number>.insv
    in the same folder (whatever the case of the letters). cached: see
    lrv_companion.
    """
    folder, name = os.path.split(path or "")
    parts = _insta360_parts(name)
    if parts is None or parts[0] != "VID" or parts[3] != FIRST_LENS or parts[5] != "insv":
        return None
    other = _lens_file_name(parts, SECOND_LENS, _folder_names(folder, cached))
    return os.path.join(folder, other) if other else None


def companions(path):
    """An .insv's companions as text, for a scan to see when they change ("" for none):
    its LRV (name and size) and its second lens's file (name)."""
    found = []
    _folder_names(os.path.dirname(path))        # (listed now, once, for both lookups)
    lrv = lrv_companion(path, cached=True)
    if lrv is not None:
        try:
            size = os.path.getsize(lrv)
        except OSError:
            size = None
        found.append("lrv:%s:%s" % (os.path.basename(lrv), size))
    second = second_lens_file(path, cached=True)
    if second is not None:
        found.append("lens:%s" % os.path.basename(second))
    return " ".join(found)


def companions_changed(path, exif_text):
    """True when an indexed .insv's LRV or second lens's file is not what it was read with
    (exif_text: its exif column; read before this was kept: True)."""
    return video_facts(exif_text).get("companions") != companions(path)


def camera_360(model):
    """CAMERAS_360's entry for a camera model (with "model"), or None for one not known."""
    model = model.strip() if isinstance(model, str) else ""
    found = CAMERAS_360.get(re.sub(r"\s+", "", model).lower())
    return dict(found, model=model) if found else None


def _video_streams(data):
    return [stream for stream in data.get("streams") or ()
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
            and not (isinstance(stream.get("disposition"), dict)
                     and stream["disposition"].get("attached_pic") == 1)]


def _stream_sizes(data):
    return [(_number(stream.get("width")), _number(stream.get("height")))
            for stream in _video_streams(data if isinstance(data, dict) else {})]


def _is_lens_pair(width, height):
    """True for a 2:1 frame (two lens circles side by side)."""
    return bool(width and height) and abs(float(width) / height - 2.0) < LENS_ASPECT_SLACK * 2


def lens_layout(data, second=None):
    """How a probed .insv (ffprobe's JSON) holds its two lenses.

    "streams": two video streams of the same size (one per lens); "frame":
    one 2:1 stream holding both; "files": one square stream, and second
    (ffprobe's JSON of its second lens's file, see second_lens_file) one of
    the same size; "" for anything else.
    """
    sizes = _stream_sizes(data)
    if len(sizes) >= 2 and sizes[0] == sizes[1] and sizes[0][0] and sizes[0][1]:
        return "streams"
    if len(sizes) == 1 and _is_lens_pair(*sizes[0]):
        return "frame"
    if (len(sizes) == 1 and second is not None and sizes[0][0]
            and sizes[0][0] == sizes[0][1] and _stream_sizes(second) == sizes):
        return "files"
    return ""


def insta360_model(path):
    """The camera an Insta360 file's trailer names ("Insta360 X4"), or "".

    The trailer at the end of the file (it ends with a fixed 32-byte mark)
    holds the camera's details as protobuf: its model is the string field
    starting with "Insta360". Only the last TRAILER_BYTES are read.
    """
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - TRAILER_BYTES))
            tail = handle.read(TRAILER_BYTES)
    except (OSError, TypeError, ValueError):
        return ""
    if not tail.endswith(_TRAILER_MAGIC):
        return ""
    for match in _TRAILER_MODEL_RE.finditer(tail):
        length = match.group(1)[0]
        text = match.group(2)[:length]
        if len(text) == length:
            return text.decode("ascii").strip()
    return ""


def media_data_size(path):
    """The bytes of an MP4's media data (its mdat boxes), or None when it is not read as one.

    About what a copy without metadata keeps (strip_copy): not the other
    boxes, nor what a camera writes after them (an Insta360 trailer).
    """
    total = 0
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            position = 0
            while position + 8 <= size:
                handle.seek(position)
                header = handle.read(16)
                length, kind = struct.unpack(">I4s", header[:8])
                if length == 1 and len(header) == 16:
                    length = struct.unpack(">Q", header[8:16])[0]
                elif length == 0:
                    length = size - position
                if length < 8 or position + length > size or not kind.isalnum():
                    break               # (no box: a trailer)
                if kind == b"mdat":
                    total += length
                position += length
    except (OSError, struct.error, TypeError, ValueError):
        return None
    return total or None


def _lens_inputs(path, seconds, layout, order, second=None):
    """(ffmpeg's input arguments, the two lenses' inputs to the filter graph, left to
    right) of an .insv: its streams (layout "streams"; order: their indexes), or its
    file and second, its second lens's ("files": order 0 is path, 1 second)."""
    def source(name):
        return (["-ss", "%.3f" % seconds] if seconds and seconds > 0 else []) + [
            "-i", _input(name)]
    if layout == "files":
        return source(path) + source(second), tuple("[%d:v:0]" % index for index in order)
    return source(path), tuple("[0:v:%d]" % index for index in order)


def lens_filter(width, inputs=("[0:v:1]", "[0:v:0]")):
    """The -filter_complex that puts an .insv's two lenses (inputs, see _lens_inputs)
    side by side, width wide: [frame], width x width / 2 (even numbers)."""
    width = max(4, int(width) // 4 * 4)
    return "%s%shstack=inputs=2,scale=w=%d:h=%d[frame]" % (inputs[0], inputs[1], width,
                                                         width // 2)


def grab_lenses(tools, path, seconds, edge, pair_width, layout="streams", order=(1, 0),
                second=None):
    """The frame at seconds of an .insv's two lenses side by side (RGB, 2:1), or None.

    layout, order and second: see _lens_inputs. At most edge pixels wide
    (and never wider than pair_width, the two lenses' width together).
    Raises VideoReadError when ffmpeg fails.
    """
    width = max(4, min(int(edge), int(pair_width or edge)))
    inputs, labels = _lens_inputs(path, seconds, layout, order, second)
    argv = tools.ffmpeg + ["-nostdin", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", lens_filter(width, labels), "-map", "[frame]", "-frames:v", "1",
        "-an", "-sn", "-dn", "-f", "image2pipe", "-c:v", "bmp", "pipe:1"]
    code, out, err = _run(argv, FRAME_TIMEOUT)
    if code != 0:
        raise VideoReadError("ffmpeg could not read a frame (%s)" % _error_line(err, path))
    return _decode_frame(out, _images())


class Video360:
    """A 360-degree video's frames, stitched (see video_360()).

    grab(seconds, edge) is the equirectangular panorama at seconds, at most
    edge pixels wide (None past the end): a frame of the LRV (lrv, read with
    lrv_info), else of the lenses (layout "streams": its two streams,
    "files": its file and second, see _lens_inputs) or the video's own 2:1
    stream (layout "frame"), stitched with the lens model of camera (an
    entry of CAMERAS_360). info is the video's parse_probe() result with
    width x height the panorama's full size: twice a lens's width by its
    width (7680 x 3840 for two 3840 px lenses), as the original's stitched
    panorama would be. The lens circles and gains of the stitches are kept
    for the player (fisheye()). level is the video's level track
    (insta360.level_track), or None: when levelling is on, each panorama
    grab() makes is turned level for its time.
    """

    def __init__(self, tools, path, info, layout, camera, lrv=None, lrv_info=None,
                 stream_width=0, second=None, level=None):
        self.tools = tools
        self.path = path
        self.layout = layout
        self.camera = camera
        self.params = getattr(_images(), camera["params"])
        self.lrv = lrv
        self.lrv_info = lrv_info
        self.second = second if layout == "files" else None
        self.stream_width = int(stream_width or 0)
        if layout == "frame":
            size = (int(info["width"]), max(1, int(info["width"]) // 2))
        else:
            size = (2 * self.stream_width, self.stream_width)
        if not size[0] and lrv_info:
            size = (int(lrv_info["width"]), max(1, int(lrv_info["width"]) // 2))
        self.info = dict(info, width=size[0], height=size[1])
        self.frame_size = None
        self.circles = None
        self.gains = []
        self.level = level

    def frame(self, seconds, edge):
        """The unstitched frame (both lenses side by side) at seconds, at most edge wide."""
        if self.lrv:
            return grab_frame(self.tools, self.lrv, seconds, self.lrv_info, edge)
        if self.layout in ("streams", "files"):
            return grab_lenses(self.tools, self.path, seconds, edge, 2 * self.stream_width,
                               self.layout, self.camera["order"], self.second)
        return grab_frame(self.tools, self.path, seconds, self.info, edge)

    def grab(self, seconds, edge):
        images = _images()
        # (The circles are stretched over the whole panorama: a frame a
        # little wider than it holds its detail; see images.py.)
        frame = self.frame(seconds, images._fisheye_source_edge(edge, self.params))
        if frame is None:
            return None
        details = {}
        up = inverted = None
        if self.level and images.stitch_settings()["level_horizon"]:
            up = insta360.up_at(self.level, seconds)
            inverted = insta360.inverted_at(self.level, seconds)
        panorama = images.dual_fisheye_to_equirect(frame, min(int(edge), frame.size[0]),
                                                   self.params, details=details, level=up,
                                                   level_inverted=inverted)
        self.frame_size = frame.size
        self.circles = details.get("circles")
        if details.get("gains"):
            self.gains.append(details["gains"])
        return panorama

    def fisheye(self):
        """The lens geometry the player stitches the frames with, or None before a stitch.

        {"lens": [[x, y, r] of the left and the right lens circle: centre
        and radius as fractions of the frame's width (x) and height (y, r)],
        "fov", "turn" (each lens's rotation, degrees), "front" (0: the left
        lens is the middle of the panorama, 1: the right one), "mirror",
        "blend" (degrees either side of the seams, as used), "gain" (the
        front and the back lens's red, green and blue gains, the mean of
        those of the frames stitched), "fill" (the nadir and zenith fill
        degrees)}.
        """
        if not self.circles or not self.frame_size:
            return None
        images = _images()
        params = self.params
        width, height = self.frame_size
        fov = float(params["fov"])
        gains = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
        if self.gains:
            gains = [[sum(found[lens][channel] for found in self.gains) / len(self.gains)
                      for channel in range(3)] for lens in (0, 1)]
        fill = images.stitch_settings()
        return {
            "lens": [[round(cx / float(width), 5), round(cy / float(height), 5),
                      round(radius / float(height), 5)] for cx, cy, radius in self.circles],
            "fov": fov,
            "turn": [float(turn) for turn in (params.get("rotation") or (0, 0))],
            "front": 0 if params.get("front", "left") == "left" else 1,
            "mirror": bool(params.get("mirror")),
            "blend": round(min(max(0.0, float(params.get("blend", 0.0))),
                               max(0.0, fov / 2.0 - 90.0)), 3),
            "gain": [[round(value, 4) for value in lens] for lens in gains],
            "fill": [fill["nadir_fill_deg"], fill["zenith_fill_deg"]],
        }


def video_360(tools, path, data, info, model=None, level=None):
    """A Video360 for an .insv at path (data: ffprobe's JSON, info: its parse_probe()), or None.

    The camera is the one the trailer of the file (or of its LRV) names
    (model: already read); None for one whose lenses are not known here
    (CAMERAS_360: the video is then read as an ordinary one). Its LRV
    (lrv_companion) is used when ffprobe reads it as a 2:1 video; else its
    own lenses (lens_layout: its streams, or it and its second lens's
    file). None when neither will do. Its level track comes from the
    motion samples at the end of the file (or of its LRV) -- which takes
    a few seconds for a long video -- unless level gives it: a track kept
    before (stored_level), or False for none (as when only its frames are
    copied, not stitched).
    """
    model = insta360_model(path) if model is None else model
    _folder_names(os.path.dirname(path))        # (listed now, once, for both lookups)
    lrv, lrv_info = lrv_companion(path, cached=True), None
    if not model and lrv is not None:
        model = insta360_model(lrv)
    camera = camera_360(model)
    if camera is None:
        return None
    second, second_data = second_lens_file(path, cached=True), None
    if second is not None:
        try:
            second_data = probe(tools, second)
        except VideoReadError:
            second = None           # (an unreadable second lens: as if it were not there)
    layout = lens_layout(data, second_data)
    if lrv is not None:
        try:
            lrv_info = parse_probe(probe(tools, lrv), os.path.basename(lrv))
        except VideoReadError:
            lrv = None              # (an unreadable copy: the lenses instead)
        else:
            if not _is_lens_pair(lrv_info["width"], lrv_info["height"]):
                lrv, lrv_info = None, None
    if lrv is None and not layout:
        return None
    sizes = _stream_sizes(data)
    stream_width = int(sizes[0][0] or 0) if sizes else 0
    if level is False:
        level = None
    elif level is None:
        fps = (info.get("video") or {}).get("fps")
        for source in (path, lrv):
            if source is not None and level is None:
                level = insta360.level_track(source, camera["model"], info.get("duration"),
                                             fps)
    else:
        level = insta360.checked_track(level)
    return Video360(tools, path, info, layout, camera, lrv, lrv_info, stream_width, second,
                    level)


def _facts_360(v360, facts):
    """A 360 video's facts (exif "video"): the original's; how its lenses are stored
    ("lenses": its layout, "lrv" when only its LRV holds both); "lens_file", its
    second lens's file; its LRV ({name, codec, pix_fmt, container, width, height,
    size, media_size: see media_data_size}); the lens geometry; and its level
    track ("level", see insta360.level_track; kept whatever the setting)."""
    facts = dict(facts)
    facts["lenses"] = v360.layout or "lrv"
    if v360.second:
        facts["lens_file"] = os.path.basename(v360.second)
    if v360.lrv and v360.lrv_info:
        try:
            size = os.path.getsize(v360.lrv)
        except OSError:
            size = None
        lrv = v360.lrv_info["video"]
        facts["lrv"] = {"name": os.path.basename(v360.lrv), "codec": lrv.get("codec", ""),
                        "pix_fmt": lrv.get("pix_fmt", ""),
                        "container": lrv.get("container", ""),
                        "width": v360.lrv_info["width"], "height": v360.lrv_info["height"],
                        "size": size, "media_size": media_data_size(v360.lrv)}
    geometry = v360.fisheye()
    if geometry is not None:
        facts["fisheye"] = geometry
    if v360.level:
        facts["level"] = v360.level
    return facts


def lrv_facts(exif_text):
    """The "lrv" facts of a 360 video's exif column ({} when it has no LRV)."""
    lrv = video_facts(exif_text).get("lrv")
    return lrv if isinstance(lrv, dict) else {}


def stored_level(exif_text):
    """The level track kept in a 360 video's exif column (its "video" facts), or
    None (none kept: video_360 then reads the file's)."""
    return insta360.checked_track(video_facts(exif_text).get("level"))


def level_of(facts):
    """The level track a viewer turns a 360 video's frames with (facts: its exif
    "video" facts), or None: none kept, or the "level_horizon" setting is off."""
    if not isinstance(facts, dict) or not insta360.level_enabled():
        return None
    return insta360.checked_track(facts.get("level"))


def _read_360(v360, want_preview, preview_edge, sheet_edge, frames, want_sheet):
    """_read_video for a 360 video: its stitched poster and sheet (see Video360)."""
    images = _images()
    info = v360.info
    working_edge = max(preview_edge, images.THUMB_SIZE)
    panorama = poster(v360.tools, v360.path, info, working_edge, v360.grab)
    # As for a 360 photo: the thumbnail (and dHash) show the front view,
    # the preview the whole panorama.
    view = images.front_view(panorama)
    preview = None
    if want_preview:
        wanted = _fit(panorama.size, preview_edge)
        resample = getattr(images.Image, "Resampling", images.Image).LANCZOS
        preview = _jpeg(panorama if panorama.size == wanted
                        else panorama.resize(wanted, resample), images.PREVIEW_QUALITY)
    if want_sheet is None:
        want_sheet = want_preview
    sheet, on_sheet = None, None
    if want_sheet:
        sheet, on_sheet = make_sheet(v360.tools, v360.path, info, sheet_edge, frames, panorama,
                                     v360.grab)
    facts = _facts_360(v360, info["video"])
    if want_sheet:
        facts.update(sheet_frames=on_sheet, sheet_asked=frames)
    return {
        "width": info["width"], "height": info["height"],
        "thumb": _jpeg(view, images.THUMB_QUALITY), "preview": preview,
        "exif": {"video": facts}, "dhash": images.compute_dhash(view),
        "mime": mime_type(v360.path),
        "taken_at": info["taken_at"], "taken_source": info["taken_source"],
        "tz_offset": info["tz_offset"],
        "camera": info["camera"] or _insta360_camera(v360.camera["model"]), "lat": info["lat"],
        "lon": info["lon"], "projection": PROJECTION_360, "pano": None, "media": MEDIA_VIDEO,
        "duration": round(info["duration"], 3) if info["duration"] else None,
        "sheet": sheet,
    }


def _insta360_camera(model):
    """The camera name of an Insta360 model as its photos give it ("" for none)."""
    return imageinfo.camera_name({"Make": INSTA360_MAKER, "Model": model}) if model else ""


def _fit(size, edge):
    width, height = size
    scale = min(1.0, float(edge) / max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _jpeg(image, quality):
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def read_video(path, tools, want_preview=True, mtime=None, preview_edge=None,
               sheet_edge=common.DEFAULT_MAX_IMAGE_EDGE, frames=DEFAULT_FRAMES,
               want_sheet=None):
    """Read a video once (ffprobe, then ffmpeg for the frames); images.read_image's dict for it.

    Returns width, height (as shown), thumb (JPEG of the poster), preview
    (the poster, long edge <= preview_edge, or None without want_preview),
    exif ({"video": the video facts of parse_probe, with sheet_frames
    (the frames on the sheet) and sheet_asked (frames) when there is a
    sheet}), dhash (of the poster), content_hash (quick_hash), mime,
    taken_at, taken_source, tz_offset, camera, lat, lon, projection "",
    pano None -- and media "video", duration (seconds or None) and sheet
    (the contact sheet's JPEG bytes, up to frames frames with a long edge
    of sheet_edge; None unless want_sheet, which defaults to want_preview).
    A 360-degree video (is_360_video, see Video360) has projection
    "dual_fisheye": its poster and sheet frames are stitched panoramas, the
    preview is the poster's panorama, the thumbnail and dHash its front
    view, width x height the full panorama's size, and its facts say how
    its lenses are stored, which LRV it has and the lens geometry.
    Raises ToolsMissing without tools, images.SourceReadError when the file
    cannot be read (also when it could not be while ffprobe or ffmpeg read
    it, or a program took too long: see _read_failure) and VideoReadError
    when it is not a video that can be read. A failure of a file that can
    still be read is tried once more (a share that was gone for a moment).
    """
    if tools is None:
        raise ToolsMissing()
    content_hash, size = quick_hash(path)
    if mtime is None:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = None
    for attempt in (1, 2):
        try:
            result = _read_video(path, tools, want_preview, mtime, preview_edge, sheet_edge,
                                 frames, want_sheet)
            break
        except ToolsMissing:
            raise
        except VideoReadError as error:
            failure = _read_failure(error, path, size)
            if failure is not error or attempt == 2:
                raise failure
    result.update(content_hash=content_hash, size=size)
    return result


def _read_video(path, tools, want_preview, mtime, preview_edge, sheet_edge, frames,
                want_sheet):
    """read_video without the content hash (and without its second try)."""
    images = _images()
    preview_edge = int(preview_edge or images.PREVIEW_EDGE)
    name = os.path.basename(path)
    data = probe(tools, path)
    info = parse_probe(data, name, mtime)
    if is_360_video(name):
        # (Also an .insv read as an ordinary video, a camera not known
        # here: a scan reads it again when its companions change.)
        model = insta360_model(path)
        v360 = video_360(tools, path, data, info, model)
        if v360 is not None:
            result = _read_360(v360, want_preview, preview_edge, sheet_edge, frames, want_sheet)
        else:
            result = _read_flat(tools, path, info, want_preview, preview_edge, sheet_edge,
                                frames, want_sheet)
            result["camera"] = result["camera"] or _insta360_camera(model)
        result["exif"]["video"]["companions"] = companions(path)
        return result
    return _read_flat(tools, path, info, want_preview, preview_edge, sheet_edge, frames,
                      want_sheet)


def _read_flat(tools, path, info, want_preview, preview_edge, sheet_edge, frames, want_sheet):
    """_read_video for an ordinary video (info: its parse_probe())."""
    images = _images()
    name = os.path.basename(path)
    working_edge = max(preview_edge, images.THUMB_SIZE)
    frame = poster(tools, path, info, working_edge)
    resample = getattr(images.Image, "Resampling", images.Image).LANCZOS
    thumb_image = frame.resize(_fit(frame.size, images.THUMB_SIZE), resample) \
        if max(frame.size) > images.THUMB_SIZE else frame
    preview = None
    if want_preview:
        preview_image = frame
        wanted = _fit(frame.size, preview_edge)
        if frame.size != wanted:
            preview_image = frame.resize(wanted, resample)
        preview = _jpeg(preview_image, images.PREVIEW_QUALITY)
    if want_sheet is None:
        want_sheet = want_preview
    facts = dict(info["video"])
    sheet = None
    if want_sheet:
        sheet, on_sheet = make_sheet(tools, path, info, sheet_edge, frames, frame)
        facts.update(sheet_frames=on_sheet, sheet_asked=frames)
    return {
        "width": info["width"], "height": info["height"],
        "thumb": _jpeg(thumb_image, images.THUMB_QUALITY), "preview": preview,
        "exif": {"video": facts}, "dhash": images.compute_dhash(frame),
        "mime": mime_type(name),
        "taken_at": info["taken_at"], "taken_source": info["taken_source"],
        "tz_offset": info["tz_offset"], "camera": info["camera"], "lat": info["lat"],
        "lon": info["lon"], "projection": "", "pano": None, "media": MEDIA_VIDEO,
        "duration": round(info["duration"], 3) if info["duration"] else None,
        "sheet": sheet,
    }


def poster_jpeg(tools, path, edge=None, level=None):
    """JPEG bytes of a video's poster at most edge pixels (default: the preview size).

    A 360 video's is its stitched panorama (as its preview; level: its
    level track when it is known, see video_360). Raises as read_video
    does (images.SourceReadError when the file cannot be read).
    """
    images = _images()
    if tools is None:
        raise ToolsMissing()
    try:
        info, grab = _frames_of(tools, path, level)
        frame = poster(tools, path, info, int(edge or images.PREVIEW_EDGE), grab)
    except VideoReadError as error:
        raise _read_failure(error, path)
    return _jpeg(frame, images.PREVIEW_QUALITY)


def _frames_of(tools, path, level=None):
    """(info, grab) for poster() and make_sheet(): a 360 video's stitched frames
    (level: see video_360), or (parse_probe's result, None) for any other video."""
    data = probe(tools, path)
    info = parse_probe(data, os.path.basename(path))
    v360 = video_360(tools, path, data, info, level=level) if is_360_video(path) else None
    if v360 is not None:
        return v360.info, v360.grab
    return info, None


def sheet_jpeg(tools, path, edge=common.DEFAULT_MAX_IMAGE_EDGE, frames=DEFAULT_FRAMES,
               level=None):
    """(JPEG bytes, frames on it) of a video's contact sheet, made now (make_sheet).

    For when none was kept with the previews (a 360 video's frames are
    stitched; level: see video_360). Raises as read_video does.
    """
    if tools is None:
        raise ToolsMissing()
    try:
        info, grab = _frames_of(tools, path, level)
        return make_sheet(tools, path, info, edge, frames, grab=grab)
    except VideoReadError as error:
        raise _read_failure(error, path)


def sheet_frames(exif_text):
    """(frames on a video's stored contact sheet, frames asked for) from its exif column.

    Either is None when not known (no sheet, or one made by an older version).
    """
    facts = video_facts(exif_text)
    counts = []
    for key in ("sheet_frames", "sheet_asked"):
        value = facts.get(key)
        counts.append(value if isinstance(value, int) and not isinstance(value, bool)
                      and value > 0 else None)
    return tuple(counts)


def duration_text(seconds):
    """'0:24', '1:02:03' (the length of a video, as players show it: whole seconds,
    rounded down, but at least 0:01), or '' when unknown."""
    seconds = _number(seconds)
    if seconds is None or seconds < 0:
        return ""
    total = int(math.floor(seconds))
    if seconds > 0 and total < 1:
        total = 1
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


# ---------------------------------------------------------------------------
# Copies without metadata (exports), and playable copies (proxies)

def strip_command(tools, source, target):
    """The ffmpeg command that copies a video's streams into an MP4 without its metadata.

    Only the first video stream and the audio go: data streams (camera
    telemetry, timed GPS), subtitles and chapters are left out, and so are
    the container's and the streams' tags (dates, places, devices). The
    picture and sound are copied as they are (no re-encoding); the rotation
    stays, as it is not a tag.
    """
    return tools.ffmpeg + [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", _input(source),
        "-map", "0:v:0", "-map", "0:a?", "-dn", "-sn", "-map_metadata", "-1",
        "-map_metadata:s", "-1", "-map_chapters", "-1", "-c", "copy",
        "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:v", "+bitexact",
        "-flags:a", "+bitexact", "-f", "mp4", _input(target)]


def _temporary(target):
    # (Exports remove files left with this ending by an interrupted run.)
    return "%s.tmp-%d-%d" % (target, os.getpid(), threading.get_ident())


def _write_with(argv_for, target, timeout, what, source=None):
    """Run argv_for(temporary file), then move the file to target. Raises VideoReadError."""
    common.ensure_dir(os.path.dirname(os.path.abspath(target)))
    temp = _temporary(target)
    try:
        code, _out, err = _run(argv_for(temp), timeout)
        if code != 0 or not os.path.isfile(temp) or os.path.getsize(temp) == 0:
            raise VideoReadError("ffmpeg could not %s (%s)" % (what, _error_line(err, source)))
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return target


def strip_copy(tools, source, target, timeout=REMUX_TIMEOUT):
    """Write target: source re-muxed into an MP4 without metadata (strip_command). Returns target."""
    if tools is None:
        raise ToolsMissing()
    return _write_with(lambda temp: strip_command(tools, source, temp), target, timeout,
                       "copy the video", source)


def proxy_command(tools, source, target, info, height=720, crf=26, tone_map=None):
    """The ffmpeg command that makes a playable copy: H.264 / AAC in an MP4, short side <= height.

    HDR video is tone mapped unless tone_map is False (default:
    Tools.hdr_filter).
    """
    if tone_map is None:
        tone_map = tools.hdr_filter
    chain = []
    if info.get("sar"):
        chain.append("scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1")
    if tone_map and info.get("video", {}).get("hdr"):
        chain.append(hdr_filter(info))
    chain.append("scale=w='if(gt(iw,ih),-2,min(%d,trunc(iw/2)*2))':"
                 "h='if(gt(iw,ih),min(%d,trunc(ih/2)*2),-2)'" % (height, height))
    chain.append("format=yuv420p")
    return tools.ffmpeg + [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", _input(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn", "-map_metadata", "-1",
        "-map_metadata:s", "-1", "-map_chapters", "-1", "-vf", ",".join(chain),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart",
        "-f", "mp4", _input(target)]


def lens_proxy_command(tools, source, target, height=720, crf=26, layout="streams",
                       order=(1, 0), second=None):
    """The ffmpeg command that makes a playable copy of a 360 video's two lenses.

    Its two streams (layout "streams") or its file and second, its second
    lens's ("files"), side by side in order (see _lens_inputs; as its LRV
    has them: the player stitches them as it does an LRV's frames), height
    pixels tall and twice as wide, H.264 / AAC (source's sound) in an MP4,
    without metadata.
    """
    height = max(2, int(height) // 2 * 2)
    inputs, labels = _lens_inputs(source, 0, layout, order, second)
    graph = ("%s%shstack=inputs=2,scale=w=%d:h=%d,format=yuv420p[frame]"
             % (labels[0], labels[1], 2 * height, height))
    return tools.ffmpeg + [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + inputs + [
        "-filter_complex", graph, "-map", "[frame]", "-map", "0:a:0?", "-dn", "-sn",
        "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart",
        "-f", "mp4", _input(target)]


def make_proxy(tools, source, target, height=720, crf=26, timeout=None):
    """Write a playable copy of source to target (proxy_command). Returns target.

    When the tone mapping of an HDR video fails, the copy is made without
    it (as grab_frame does). A 360 video whose lenses are two streams or
    two files gets them side by side (lens_proxy_command); one with both
    in one stream is copied as any video.
    """
    if tools is None:
        raise ToolsMissing()
    data = probe(tools, source)
    info = parse_probe(data, os.path.basename(source))
    if timeout is None:
        # A generous limit: slow computers encode at about real time.
        timeout = max(REMUX_TIMEOUT, int(4 * (info.get("duration") or 0)) + 60)
    # (Its frames are copied, not stitched: no level track.)
    v360 = video_360(tools, source, data, info, level=False) if is_360_video(source) else None
    if v360 is not None and v360.layout in ("streams", "files"):
        return _write_with(lambda temp: lens_proxy_command(
            tools, source, temp, height, crf, v360.layout, v360.camera["order"], v360.second),
            target, timeout, "make a playable copy", source)
    tone_map = tools.hdr_filter and bool(info["video"].get("hdr"))

    def write(tone):
        return _write_with(lambda temp: proxy_command(tools, source, temp, info, height, crf,
                                                      tone),
                           target, timeout, "make a playable copy", source)
    try:
        return write(tone_map)
    except VideoReadError as error:
        if not tone_map or _io_problem(error):
            raise
        if _filter_missing(error):
            tools.hdr_filter = False    # this ffmpeg has no zscale
        return write(False)


def proxy_name(image_id, content_hash):
    """The file name of a video's proxy: it changes when the video does."""
    return "%d-%s.mp4" % (int(image_id), re.sub(r"[^0-9a-f]", "", str(content_hash))[:12])


def proxy_path(library_id, image_id, content_hash, root=None):
    """data/proxies/<library id>/<proxy_name>."""
    if not isinstance(library_id, str) or not common.LIBRARY_ID_RE.match(library_id):
        raise ValueError("invalid library id: %r" % (library_id,))
    return os.path.join(root or common.proxies_dir(), library_id,
                        proxy_name(image_id, content_hash))


def move_proxy(library_id, old_id, new_id):
    """Give a video's proxy to its row's successor (a moved or renamed file). Returns how many."""
    try:
        folder = os.path.dirname(proxy_path(library_id, 0, ""))
        names = os.listdir(folder)
    except (OSError, ValueError):
        return 0
    moved = 0
    prefix = "%d-" % int(old_id)
    for name in names:
        if name.startswith(prefix) and name.endswith(".mp4"):
            try:
                os.replace(os.path.join(folder, name),
                           os.path.join(folder, "%d-%s" % (int(new_id), name[len(prefix):])))
                moved += 1
            except OSError:
                pass
    return moved


def _library_folder(library_id):
    if not isinstance(library_id, str) or not common.LIBRARY_ID_RE.match(library_id):
        raise ValueError("invalid library id: %r" % (library_id,))
    return os.path.join(common.proxies_dir(), library_id)


def delete_library_proxies(library_id):
    """Remove every proxy of a library id (its data was deleted). True if a folder was removed."""
    try:
        folder = _library_folder(library_id)
    except ValueError:
        return False
    if not os.path.isdir(folder):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not os.path.isdir(folder)


def move_library_proxies(source_id, target_id):
    """Move a library id's proxies to another id (the rows keep their ids). Returns how many."""
    try:
        source, target = _library_folder(source_id), _library_folder(target_id)
        names = os.listdir(source)
    except (OSError, ValueError):
        return 0
    moved = 0
    for name in names:
        if not name.endswith(".mp4"):
            continue
        try:
            common.ensure_dir(target)
            os.replace(os.path.join(source, name), os.path.join(target, name))
            moved += 1
        except OSError:
            pass
    shutil.rmtree(source, ignore_errors=True)
    return moved


def existing_proxy(library_id, image_id, content_hash):
    """The path of a video's proxy when there is one for its current content, else None."""
    try:
        path = proxy_path(library_id, image_id, content_hash)
    except (TypeError, ValueError):
        return None
    return path if os.path.isfile(path) else None


def delete_proxies(library_id, image_ids):
    """Remove the proxies of some videos of a library (any content). Returns how many."""
    try:
        folder = os.path.dirname(proxy_path(library_id, 0, ""))
        names = os.listdir(folder)
    except (OSError, ValueError):
        return 0
    wanted = {str(int(image_id)) for image_id in image_ids}
    removed = 0
    for name in names:
        if name.split("-", 1)[0] in wanted and name.endswith(".mp4"):
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    return removed


def proxy_usage():
    """{library id: {"count", "bytes"}} of the proxies in data/proxies."""
    usage = {}
    root = common.proxies_dir()
    try:
        folders = [entry for entry in os.scandir(root) if entry.is_dir()]
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


def video_facts(exif_text):
    """The "video" object of an exif column's JSON ({} when there is none)."""
    try:
        exif = json.loads(exif_text or "{}")
    except (TypeError, ValueError):
        return {}
    facts = exif.get("video") if isinstance(exif, dict) else None
    return facts if isinstance(facts, dict) else {}


def effective_info(info, media):
    """A description as shown and searched: a video's kind of picture is always "video".

    info is the description dict (the model's, with any edit laid over
    it), media the row's media column. The model saw only a few frames,
    so its photo_type is not the video's kind (export.picture_kinds still
    looks at it, see search_index.add_video). Returns info itself for a
    picture (or when it is not a dict), else a copy.
    """
    if media != MEDIA_VIDEO or not isinstance(info, dict):
        return info
    return dict(info, photo_type=MEDIA_VIDEO)


def proxy_candidates(conn, library_id, root=None):
    """Rows (id, path, content_hash, exif, projection) of a library's present videos that
    need a playable copy (see wants_copy). root: the library's folder, to look for 360
    videos' LRVs now (else as the scan found them)."""
    rows = conn.execute(
        "SELECT id, library_id, path, content_hash, exif, projection FROM images "
        "WHERE library_id = ? AND media = 'video' AND missing_since IS NULL ORDER BY id",
        (library_id,)).fetchall()
    found = []
    for row in rows:
        facts = video_facts(row["exif"])
        lrv = None
        if row["projection"] == PROJECTION_360:
            if root is None:
                lrv = facts.get("lrv")
            else:
                lrv = lrv_companion(os.path.join(root, *row["path"].split("/")),
                                    _lrv_name(facts))
        if wants_copy(row["projection"], facts, bool(lrv)):
            found.append(row)
    return found


def wants_copy(projection, facts, has_lrv=False):
    """True when a video needs a playable copy (facts: its exif "video").

    One browsers may not play (not H.264 in an MP4, ...); a 360 video
    (projection) only when there is no LRV (has_lrv) and its original does
    not hold both lenses in one stream browsers play: its lenses' streams
    or files are put side by side. One whose LRV alone holds both lenses
    gets none (a copy of the original would show one lens).
    """
    if projection != PROJECTION_360:
        return playable(facts) != "yes"
    if has_lrv:
        return False
    lenses = facts.get("lenses")
    return lenses in ("streams", "files") or (lenses == "frame" and playable(facts) != "yes")


def _lrv_name(facts):
    lrv = facts.get("lrv")
    return lrv.get("name") if isinstance(lrv, dict) else None


def playable_360(row, full):
    """(path, kind) of what a 360 video row's player gets when it has no playable copy.

    Its LRV, looked for now (lrv_companion of its original at full; kind
    "video-lrv"); else the original when it holds both lenses in one stream
    browsers play (lenses "frame"; kind "video"); else (None, None): no
    browser plays lens streams as they are. Also (None, None) for any other
    row (row: its projection and exif).
    """
    if row["projection"] != PROJECTION_360 or not full:
        return None, None
    facts = video_facts(row["exif"])
    lrv = lrv_companion(full, _lrv_name(facts), cached=True)
    if lrv is not None:
        return lrv, "video-lrv"
    if facts.get("lenses") == "frame" and playable(facts) == "yes":
        return full, "video"
    return None, None


def _remove_stale_proxies(library_id, rows):
    """Remove proxies of videos that are gone or have other content now. Returns how many."""
    try:
        folder = os.path.dirname(proxy_path(library_id, 0, ""))
        names = os.listdir(folder)
    except (OSError, ValueError):
        return 0
    wanted = {proxy_name(row["id"], row["content_hash"]) for row in rows}
    removed = 0
    for name in names:
        # (Also what an interrupted copy left: the job runs one at a time.)
        if name not in wanted and re.match(r"^\d+-[0-9a-f]*\.mp4(\.tmp-\d+-\d+)?$", name):
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    return removed


def make_proxies(conn, config, ctx, library_ids):
    """The proxies job: playable copies of the videos browsers may not play.

    For each library (ids), its present videos that are not H.264 (or not
    in a container browsers take) get an H.264 / AAC copy in
    data/proxies/<library id>/ ("video_proxies": height and crf), unless
    they have one for their current content; copies of videos that are gone
    or changed are removed. A 360 video plays its LRV: only one without
    (there now) gets a copy, of its two lenses side by side (see wants_copy
    and lens_proxy_command). Returns {"made", "kept", "failed", "removed", "stopped"}. Raises
    ToolsMissing without ffmpeg.
    """
    settings = proxy_settings(config)
    tools = find_tools(config)
    if tools is None:
        raise ToolsMissing()
    summary = {"made": 0, "kept": 0, "failed": 0, "removed": 0, "stopped": False}
    work = []
    for library_id in library_ids:
        library = common.get_library(config, library_id) or {}
        root = common.library_root(library)
        rows = proxy_candidates(conn, library_id, root or None)
        summary["removed"] += _remove_stale_proxies(library_id, rows)
        for row in rows:
            if existing_proxy(library_id, row["id"], row["content_hash"]):
                summary["kept"] += 1
            elif root:
                work.append((root, row))
    ctx.stage("proxies", total=len(work))
    if work:
        ctx.log("Making playable copies of %d video(s) (H.264, %dp)" % (len(work),
                                                                       settings["height"]))
    for root, row in work:
        ctx.wait_if_paused()
        if ctx.should_stop():
            summary["stopped"] = True
            break
        source = os.path.join(root, *row["path"].split("/"))
        target = proxy_path(row["library_id"], row["id"], row["content_hash"])
        try:
            make_proxy(tools, source, target, settings["height"], settings["crf"])
            summary["made"] += 1
        except (VideoReadError, OSError) as error:
            summary["failed"] += 1
            ctx.log("Could not make a playable copy of %s: %s" % (row["path"], error), "warn")
        ctx.step(item=row["path"])
    return summary


# ---------------------------------------------------------------------------
# Command line

def main(argv=None):
    """python videos.py FILE...: print what is read from each video (no database is touched)."""
    args = list(sys.argv[1:] if argv is None else argv)
    status = tools_status()
    print("ffmpeg: %s" % (status["ffmpeg"] or "not found"))
    print("ffprobe: %s" % (status["ffprobe"] or "not found"))
    if status["problem"]:
        print(status["problem"])
        return 1
    tools = Tools([status["ffmpeg"]], [status["ffprobe"]])
    code = 0
    for path in args:
        try:
            # (With the file's time, as a scan reads it: see creation_date.)
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = None
        try:
            data = probe(tools, path)
            info = parse_probe(data, os.path.basename(path), mtime)
        except VideoReadError as error:
            print("%s: %s" % (path, error))
            code = 1
            continue
        for key in ("sar", "span", "hdr_input"):
            info.pop(key, None)
        info["length"] = duration_text(info["duration"])
        info["playable"] = playable(info["video"])
        if is_360_video(path):
            # (What a scan reads a 360 video's frames from: "" for a
            # camera whose lenses are not known here, read as one lens.)
            v360 = video_360(tools, path, data, info)
            info["lenses"] = (v360.layout or "lrv") if v360 is not None else ""
            info["lrv"] = (v360.lrv or "") if v360 is not None else ""
            info["lens_file"] = (v360.second or "") if v360 is not None else ""
            model = v360.camera["model"] if v360 is not None else insta360_model(path)
            info["camera"] = info["camera"] or _insta360_camera(model)
        print(path)
        print(json.dumps(info, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
