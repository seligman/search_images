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
  when the file was converted, else a date in the file name, else the
  file's time), the GPS position (ISO 6709) and the camera;
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

Run "python videos.py FILE" to see what is read from one video and where
ffmpeg was found.
"""

import datetime
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import common
import imageinfo

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
# videos then keep their file's time, as photos without a date do.)
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


def creation_date(format_tags, stream_tags, filename="", mtime=None):
    """(taken_at, taken_source, tz_offset) of a video.

    In order: the local date tags (the QuickTime creation date with its UTC
    offset first: phones write it when they record), source "video"; the
    container's (else the video stream's) creation time, which is UTC, in
    this computer's time zone, source "video" -- unless it is more than
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
    taken_at, source, offset = creation_date(format_tags, stream_tags, filename, mtime)
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


def poster(tools, path, info, edge):
    """The poster frame (an RGB image at most edge pixels). Raises VideoReadError when there is none.

    The frame at min(1 s, 10 % of the duration); when it is black (a fade
    in, a hand over the lens), the first later one of LATER_POSTERS that is
    not, else the brightest.
    """
    best, best_level = None, -1.0
    for seconds in poster_times(info.get("duration")):
        frame = grab_frame(tools, path, seconds, info, edge)
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
        best = grab_frame(tools, path, 0.0, info, edge)
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


def make_sheet(tools, path, info, edge, count=DEFAULT_FRAMES, poster_frame=None):
    """(JPEG bytes, frames on it): the contact sheet of a video, count frames, long edge edge.

    The frames are spread over the part of the video that has pictures
    (info's span). A frame that cannot be read (past the last one) is
    looked for once more half way back to the moment before it, else left
    out; with none at all (or no known duration) the sheet is the poster
    alone. The describe step tells the model how many frames the sheet
    really holds.
    """
    _c, _r, cell_w, cell_h, _g, _w, _h = sheet_layout(info["width"], info["height"], count,
                                                    edge)
    frames = []
    before = 0.0
    for seconds in sheet_times(info.get("span") or info.get("duration"), count):
        frame = grab_frame(tools, path, seconds, info, max(cell_w, cell_h))
        if frame is None and seconds > before:
            frame = grab_frame(tools, path, (before + seconds) / 2.0, info,
                               max(cell_w, cell_h))
        if frame is not None:
            frames.append(frame)
        before = seconds
    if not frames:
        frame = poster_frame if poster_frame is not None else poster(
            tools, path, info, max(cell_w, cell_h))
        return contact_sheet([frame], info["width"], info["height"], edge, 1), 1
    return (contact_sheet(frames, info["width"], info["height"], edge, len(frames)),
            len(frames))


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
    info = parse_probe(probe(tools, path), name, mtime)
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


def poster_jpeg(tools, path, edge=None):
    """JPEG bytes of a video's poster at most edge pixels (default: the preview size).

    Raises as read_video does (images.SourceReadError when the file cannot
    be read).
    """
    images = _images()
    if tools is None:
        raise ToolsMissing()
    try:
        info = parse_probe(probe(tools, path), os.path.basename(path))
        frame = poster(tools, path, info, int(edge or images.PREVIEW_EDGE))
    except VideoReadError as error:
        raise _read_failure(error, path)
    return _jpeg(frame, images.PREVIEW_QUALITY)


def sheet_jpeg(tools, path, edge=common.DEFAULT_MAX_IMAGE_EDGE, frames=DEFAULT_FRAMES):
    """(JPEG bytes, frames on it) of a video's contact sheet, made now (make_sheet).

    For when none was kept with the previews. Raises as read_video does.
    """
    if tools is None:
        raise ToolsMissing()
    try:
        info = parse_probe(probe(tools, path), os.path.basename(path))
        return make_sheet(tools, path, info, edge, frames)
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


def make_proxy(tools, source, target, height=720, crf=26, timeout=None):
    """Write a playable copy of source to target (proxy_command). Returns target.

    When the tone mapping of an HDR video fails, the copy is made without
    it (as grab_frame does).
    """
    if tools is None:
        raise ToolsMissing()
    info = parse_probe(probe(tools, source), os.path.basename(source))
    if timeout is None:
        # A generous limit: slow computers encode at about real time.
        timeout = max(REMUX_TIMEOUT, int(4 * (info.get("duration") or 0)) + 60)
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


def proxy_candidates(conn, library_id):
    """Rows (id, path, content_hash, exif) of a library's present videos browsers may not play."""
    rows = conn.execute(
        "SELECT id, library_id, path, content_hash, exif FROM images WHERE library_id = ? "
        "AND media = 'video' AND missing_since IS NULL ORDER BY id", (library_id,)).fetchall()
    return [row for row in rows if playable(video_facts(row["exif"])) != "yes"]


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
    or changed are removed. Returns {"made", "kept", "failed", "removed",
    "stopped"}. Raises ToolsMissing without ffmpeg.
    """
    settings = proxy_settings(config)
    tools = find_tools(config)
    if tools is None:
        raise ToolsMissing()
    summary = {"made": 0, "kept": 0, "failed": 0, "removed": 0, "stopped": False}
    work = []
    for library_id in library_ids:
        library = common.get_library(config, library_id) or {}
        rows = proxy_candidates(conn, library_id)
        summary["removed"] += _remove_stale_proxies(library_id, rows)
        root = common.library_root(library)
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
            info = parse_probe(probe(tools, path), os.path.basename(path), mtime)
        except VideoReadError as error:
            print("%s: %s" % (path, error))
            code = 1
            continue
        for key in ("sar", "span", "hdr_input"):
            info.pop(key, None)
        info["length"] = duration_text(info["duration"])
        info["playable"] = playable(info["video"])
        print(path)
        print(json.dumps(info, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
