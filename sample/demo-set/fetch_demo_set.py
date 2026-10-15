#!/usr/bin/env python3
"""Download the demo set and make the copies the published demo uses.

The demo set is 39 pictures and 5 short videos under Creative Commons licenses or in
the public domain, listed in manifest.json next to this file (and credited in
CREDITS.md). They are not in the repository: this script downloads each original from
its source, checks it against the manifest (its size and SHA-256) and makes its copy
by applying the item's "copy" instructions:

- max_edge: resized to this many pixels on the long edge (JPEG quality 92), the
  metadata kept;
- exif_date, exif_offset: written as EXIF DateTimeOriginal and OffsetTimeOriginal;
- gps: written as EXIF GPS (lat and lon, WGS-84; accuracy_m as GPSHPositioningError);
- remove_gps_void: the GPSStatus a camera without a position left ("V", void) removed;
- xmp_place: the names written in the XMP location fields (photoshop:City, State and
  Country, Iptc4xmpCore:CountryCode), in place of what those fields held; no position
  is added;
- transcode: the video made into a 720p H.264 / AAC MP4 copy with ffmpeg, video_date
  and video_location as its QuickTime creation date and position.

An item without instructions is copied as it was downloaded. Only the metadata of a
picture changes (its JPEG data stays as it is), unless it is resized.

From the program folder:

    python sample/demo-set/fetch_demo_set.py --list
    python sample/demo-set/fetch_demo_set.py --out C:\\Photos\\DemoSet
    python sample/demo-set/fetch_demo_set.py --out ~/Pictures/demo-set --only trip

The copies go to --out DIR (default: the "files" folder next to this file; a library
cannot be inside the program folder, so copy that folder elsewhere, or name one outside
it). The originals are kept in --originals DIR (default: the "originals" folder next to
this file): a second run downloads only what is missing, or does not match the
manifest, and makes the copies again. Delete that folder when you are done. The two
folders must be different, neither inside the other: most copies have the name of
their original, and a library of the copies would hold the originals too.

The downloads are polite: a User-Agent naming the project, at most one request a
second, and a pause before asking again when a server answers 429 (too many requests)
or 5xx (a passing problem). A file that arrives but does not match the manifest is an
error (it is not kept).

The videos need ffmpeg: --ffmpeg PATH (the program, or its folder), else the "ffmpeg"
setting in config.json (see SETTINGS.md), else ffmpeg on the PATH. Without it the
videos are skipped (and not downloaded).

Ctrl+C stops after the item in progress (what was done is kept). The exit status is 0
when every item was made, 1 when one failed, 2 for options it cannot use, and 130 when
it was stopped: run the same command again to finish (or to retry the failed ones).
"""

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from PIL import Image, TiffImagePlugin    # noqa: E402

import common       # noqa: E402
import jobs         # noqa: E402
import llm          # noqa: E402

TOOL = "sample/demo-set/fetch_demo_set.py"
MANIFEST = os.path.join(HERE, "manifest.json")
DEFAULT_OUT = os.path.join(HERE, "files")
DEFAULT_ORIGINALS = os.path.join(HERE, "originals")

USER_AGENT = ("Search-Images/1.0 (demo set download; "
              "https://github.com/seligman/search_images)")
REQUEST_INTERVAL = 1.0          # seconds from the end of one request to the next
RETRY_WAITS = (30, 60, 120, 240)    # pauses before asking again (429, 5xx, broken off)
MAX_RETRY_AFTER = 900           # the longest Retry-After pause taken (seconds)
TIMEOUT = 60
CHUNK = 1024 * 1024
FFMPEG_TIMEOUT = 3600

COPY_KEYS = {"max_edge", "exif_date", "exif_offset", "gps", "remove_gps_void", "xmp_place",
             "transcode", "video_date", "video_location"}
EXIF_KEYS = ("exif_date", "exif_offset", "gps", "remove_gps_void")
XMP_PLACE_FIELDS = (("City", "photoshop"), ("State", "photoshop"), ("Country", "photoshop"),
                    ("CountryCode", "Iptc4xmpCore"))
XMP_NAMESPACES = (("photoshop", "http://ns.adobe.com/photoshop/1.0/"),
                  ("Iptc4xmpCore", "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"))
ORIGINAL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webm", ".ogv", ".mp4", ".mov")

# JPEG markers
SOS, APP0, APP1, APP13, COM = 0xDA, 0xE0, 0xE1, 0xED, 0xFE
EXIF_HEADER = b"Exif\x00\x00"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
MAX_SEGMENT = 65533             # the most a JPEG segment can hold (after its length)

# EXIF tags (and the sub-IFDs of the Exif and GPS tags)
EXIF_IFD, GPS_IFD = 0x8769, 0x8825
DATE_TIME_ORIGINAL, OFFSET_TIME_ORIGINAL = 0x9003, 0x9011
EXIF_VERSION, EXIF_IMAGE_WIDTH, EXIF_IMAGE_HEIGHT = 0x9000, 0xA002, 0xA003
GPS_VERSION_ID, GPS_LATITUDE_REF, GPS_LATITUDE = 0x00, 0x01, 0x02
GPS_LONGITUDE_REF, GPS_LONGITUDE, GPS_STATUS = 0x03, 0x04, 0x09
GPS_MAP_DATUM, GPS_H_POSITIONING_ERROR = 0x12, 0x1F

# The video copy: the app's own playable-copy settings, made on the CPU.
SCALE_720P = ("scale=w='if(gt(iw,ih),-2,min(720,trunc(iw/2)*2))':"
              "h='if(gt(iw,ih),min(720,trunc(ih/2)*2),-2)',format=yuv420p")


class FetchError(Exception):
    """An item could not be downloaded or copied; the message says why."""


class _TryAgain(Exception):
    """A download failed in a way that may pass (429, 5xx, a broken connection)."""

    def __init__(self, message, retry_after=None):
        Exception.__init__(self, message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# The manifest

def load_manifest(path=None):
    with open(path or MANIFEST, "r", encoding="utf-8") as handle:
        return json.load(handle)


def select(manifest, groups=None):
    """The manifest's items, only those of groups when given."""
    return [item for item in manifest["items"] if not groups or item["group"] in groups]


def megabytes(count):
    return "%.1f MB" % (count / 1e6)


def original_name(item):
    """The file name of an item's original: its copy's name with the original's extension."""
    extension = os.path.splitext(urllib.parse.urlparse(item["file_url"]).path)[1].lower()
    if extension not in ORIGINAL_EXTENSIONS:
        raise FetchError("%s: unexpected file type %r" % (item["file"], extension))
    return os.path.splitext(item["file"])[0] + extension


def is_video(item):
    return bool((item.get("copy") or {}).get("transcode"))


def describe_copy(copy):
    """What making the copy does, in words."""
    if not copy:
        return "as downloaded"
    if copy.get("transcode"):
        parts = ["720p H.264 copy"]
        if copy.get("video_date"):
            parts.append("date %s" % copy["video_date"])
        if copy.get("video_location"):
            parts.append("position %s" % copy["video_location"])
        return ", ".join(parts)
    parts = []
    if copy.get("max_edge"):
        parts.append("resized to %d px" % copy["max_edge"])
    if copy.get("exif_date"):
        parts.append("date %s%s" % (copy["exif_date"], copy.get("exif_offset", "")))
    if copy.get("gps"):
        gps = copy["gps"]
        parts.append("GPS %s, %s%s" % (gps["lat"], gps["lon"], " (about %d m)" % gps["accuracy_m"]
                                       if gps.get("accuracy_m") else ""))
    if copy.get("remove_gps_void"):
        parts.append("GPS void status removed")
    if copy.get("xmp_place"):
        parts.append("place %s" % ", ".join(value for _name, value in place_items(
            copy["xmp_place"])))
    return "; ".join(parts)


def place_items(place):
    return [(name, place[name]) for name, _prefix in XMP_PLACE_FIELDS if place.get(name)]


def list_items(manifest, items, stream=None):
    """--list: every item, its download size, license and creator, and the totals."""
    stream = stream or sys.stdout
    groups = manifest.get("groups") or {}
    for group in groups:
        mine = [item for item in items if item["group"] == group]
        if not mine:
            continue
        stream.write("%s (%d)\n" % (groups[group], len(mine)))
        for item in mine:
            stream.write("  %-42s %9s  %-22s %s\n" % (item["file"], megabytes(item["bytes"]),
                                                      item["licence"], item["credit"]))
    total = sum(item["bytes"] for item in items)
    copies = sum(item.get("copy_bytes") or item["bytes"] for item in items)
    videos = sum(1 for item in items if is_video(item))
    stream.write("%d items (%d pictures, %d videos): %s to download; the copies take about "
                 "%s.\n" % (len(items), len(items) - videos, videos, megabytes(total),
                            megabytes(copies)))


# ---------------------------------------------------------------------------
# Downloading

def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def tls_context():
    """The TLS settings of the downloads: Python's defaults (certificates verified), without
    the TLS 1.3 post-handshake authentication urllib offers by default, which Flickr's
    image servers answer with 429 Too Many Requests every time."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth:
        context.post_handshake_auth = False
    return context


def urlopen(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout, context=tls_context())


class Downloader:
    """Polite downloads: one request at a time, at most one a second, and pauses before
    asking again. opener, sleep and clock are urlopen (above), time.sleep and
    time.monotonic (tests give stand-ins); log(message) reports the pauses."""

    def __init__(self, log=None, opener=None, sleep=time.sleep, clock=time.monotonic,
                 interval=REQUEST_INTERVAL, waits=RETRY_WAITS):
        self.log = log or (lambda message: None)
        self.opener = opener or urlopen
        self.sleep = sleep
        self.clock = clock
        self.interval = interval
        self.waits = tuple(waits)
        self.requests = 0
        self._last = None

    def _pause(self):
        if self._last is not None:
            wait = self.interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)

    def download(self, url, target, size, sha256):
        """Download url to target (through target.part), checking its size and SHA-256;
        raises FetchError when it cannot be downloaded or does not match."""
        part = target + ".part"
        try:
            for wait in self.waits + (None,):
                self._pause()
                try:
                    got_size, got_sha256 = self._get(url, part, size)
                    break
                except _TryAgain as error:
                    if wait is None:
                        raise FetchError("%s (gave up after %d tries)"
                                         % (error, len(self.waits) + 1))
                    if error.retry_after is not None:
                        wait = min(max(error.retry_after, 1.0), MAX_RETRY_AFTER)
                    self.log("%s; asking again in %d s" % (error, round(wait)))
                    self.sleep(wait)
            if got_size != size or got_sha256 != sha256:
                raise FetchError(
                    "%s does not match the manifest: %d bytes, SHA-256 %s (expected %d bytes, "
                    "SHA-256 %s); the file at its source may have changed"
                    % (url, got_size, got_sha256, size, sha256))
            os.replace(part, target)
        finally:
            if os.path.exists(part):
                os.remove(part)

    def _get(self, url, part, size):
        """One request: url into part; returns (bytes, sha256 hex)."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "*/*"})
        self.requests += 1
        digest, got = hashlib.sha256(), 0
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                length = (response.headers.get("Content-Length") or "").strip()
                with open(part, "wb") as handle:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        got += len(block)
                        if got > size:
                            raise FetchError("%s is larger than the manifest says (over %d "
                                             "bytes)" % (url, size))
                        digest.update(block)
                        handle.write(block)
        except urllib.error.HTTPError as error:
            if error.code == 429 or 500 <= error.code <= 599:
                raise _TryAgain("%s answered %d %s" % (url, error.code, error.reason),
                                llm.retry_after_seconds(error.headers))
            raise FetchError("%s answered %d %s" % (url, error.code, error.reason))
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
                TimeoutError, socket.timeout, ssl.SSLError) as error:
            # (A transfer broken off by the server often arrives as an SSLError, "EOF
            # occurred in violation of protocol"; a read that times out is a
            # socket.timeout before Python 3.10.)
            reason = getattr(error, "reason", None) or error
            raise _TryAgain("%s: the connection failed (%s)" % (url, reason))
        finally:
            self._last = self.clock()
        if length.isdigit() and got != int(length):
            raise _TryAgain("%s arrived incomplete (%d of %s bytes)" % (url, got, length))
        return got, digest.hexdigest()


def ensure_original(item, path, downloader, log=None):
    """The item's original at path, downloaded unless it is there already (and matches the
    manifest). Returns "downloaded" or "already there"."""
    if os.path.isfile(path):
        if os.path.getsize(path) == item["bytes"] and file_sha256(path) == item["sha256"]:
            return "already there"
        if log:
            log("%s does not match the manifest (damaged, or an older file?); downloading it "
                "again" % path)
    downloader.download(item["file_url"], path, item["bytes"], item["sha256"])
    return "downloaded"


# ---------------------------------------------------------------------------
# JPEG segments

def split_jpeg(data):
    """([(marker, payload)] of the segments before the image data, the rest from SOS on).
    Raises FetchError when data is not a JPEG."""
    if data[:2] != b"\xff\xd8":
        raise FetchError("not a JPEG file")
    segments, index = [], 2
    while True:
        if index + 4 > len(data) or data[index] != 0xFF:
            raise FetchError("damaged JPEG file (no image data)")
        marker = data[index + 1]
        if marker == 0xFF:                  # (fill byte)
            index += 1
            continue
        if marker == SOS:
            return segments, data[index:]
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            index += 2                      # (no length)
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2 or index + 2 + length > len(data):
            raise FetchError("damaged JPEG file (segment length)")
        segments.append((marker, data[index + 4:index + 2 + length]))
        index += 2 + length


def join_jpeg(segments, rest):
    parts = [b"\xff\xd8"]
    for marker, payload in segments:
        if len(payload) > MAX_SEGMENT:
            raise FetchError("a metadata segment would be too large (%d bytes)" % len(payload))
        parts.append(bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload)
    parts.append(rest)
    return b"".join(parts)


def _find(segments, marker, header):
    for index, (mark, payload) in enumerate(segments):
        if mark == marker and payload.startswith(header):
            return index
    return None


def _insert_position(segments):
    """Where a new metadata segment goes: after APP0 (JFIF) and an EXIF segment."""
    position = 0
    if segments and segments[0][0] == APP0:
        position = 1
    exif = _find(segments, APP1, EXIF_HEADER)
    if exif is not None:
        position = max(position, exif + 1)
    return position


# ---------------------------------------------------------------------------
# EXIF (Pillow's Exif)

def _ifd(exif, tag):
    """The sub-IFD tag (Exif, GPS) of exif, made when the file has none."""
    ifd = exif.get_ifd(tag)
    if tag not in exif:
        exif[tag] = ifd
    return ifd


def _keep_text_bytes(ifd):
    """Pillow reads EXIF text as Latin-1 and writes it as ASCII: text with other bytes
    (UTF-8 written by most programs) goes back as the bytes it was read from."""
    for tag, value in list(ifd.items()):
        if isinstance(value, str) and not value.isascii():
            ifd[tag] = value.encode("latin-1", "replace")


def _dms(value):
    """Degrees, minutes and seconds (EXIF rationals) of abs(value), exactly as written."""
    value = abs(Fraction(repr(float(value))))
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = ((value - degrees) * 60 - minutes) * 60
    seconds = seconds.limit_denominator(10 ** 7)
    return (TiffImagePlugin.IFDRational(degrees, 1), TiffImagePlugin.IFDRational(minutes, 1),
            TiffImagePlugin.IFDRational(seconds.numerator, seconds.denominator))


def apply_exif(exif, copy, size=None):
    """Write the EXIF instructions of copy (and size, (width, height), as
    ExifImageWidth/Height) into exif, a PIL.Image.Exif."""
    exif_ifd = None
    if copy.get("exif_date") or copy.get("exif_offset") or size:
        new = EXIF_IFD not in exif
        exif_ifd = _ifd(exif, EXIF_IFD)
        if new:
            exif_ifd[EXIF_VERSION] = b"0232"
        if copy.get("exif_date"):
            exif_ifd[DATE_TIME_ORIGINAL] = copy["exif_date"]
        if copy.get("exif_offset"):
            exif_ifd[OFFSET_TIME_ORIGINAL] = copy["exif_offset"]
        if size:
            exif_ifd[EXIF_IMAGE_WIDTH], exif_ifd[EXIF_IMAGE_HEIGHT] = size
    gps = None
    if copy.get("gps") or copy.get("remove_gps_void"):
        gps = _ifd(exif, GPS_IFD)
    if copy.get("gps"):
        lat, lon = float(copy["gps"]["lat"]), float(copy["gps"]["lon"])
        gps[GPS_VERSION_ID] = b"\x02\x03\x00\x00"
        gps[GPS_LATITUDE_REF] = "N" if lat >= 0 else "S"
        gps[GPS_LATITUDE] = _dms(lat)
        gps[GPS_LONGITUDE_REF] = "E" if lon >= 0 else "W"
        gps[GPS_LONGITUDE] = _dms(lon)
        gps[GPS_MAP_DATUM] = "WGS-84"
        if copy["gps"].get("accuracy_m"):
            gps[GPS_H_POSITIONING_ERROR] = TiffImagePlugin.IFDRational(
                int(copy["gps"]["accuracy_m"]), 1)
    if copy.get("remove_gps_void"):
        gps.pop(GPS_STATUS, None)
    for ifd in (exif, exif_ifd, gps):
        if ifd is not None:
            _keep_text_bytes(ifd)


def _check_exif(payload, copy, size):
    """Read payload back and raise FetchError when what apply_exif wrote is not there."""
    exif = Image.Exif()
    exif.load(payload)
    exif_ifd, gps = exif.get_ifd(EXIF_IFD), exif.get_ifd(GPS_IFD)
    wanted = []
    if copy.get("exif_date"):
        wanted.append(exif_ifd.get(DATE_TIME_ORIGINAL) == copy["exif_date"])
    if copy.get("exif_offset"):
        wanted.append(exif_ifd.get(OFFSET_TIME_ORIGINAL) == copy["exif_offset"])
    if size:
        wanted.append((exif_ifd.get(EXIF_IMAGE_WIDTH), exif_ifd.get(EXIF_IMAGE_HEIGHT)) == size)
    if copy.get("gps"):
        wanted.append(GPS_LATITUDE in gps and GPS_LONGITUDE in gps)
    if copy.get("remove_gps_void"):
        wanted.append(GPS_STATUS not in gps)
    if not all(wanted):
        raise FetchError("the EXIF written could not be read back (Pillow %s)"
                         % Image.__version__)


def with_exif(segments, copy, size=None):
    """segments with their EXIF segment changed (or made) as copy says."""
    segments = list(segments)
    index = _find(segments, APP1, EXIF_HEADER)
    exif = Image.Exif()
    if index is not None:
        exif.load(segments[index][1])
    apply_exif(exif, copy, size)
    try:
        payload = exif.tobytes()
    except Exception as error:
        raise FetchError("could not write the EXIF (%s: %s)" % (type(error).__name__, error))
    if len(payload) > MAX_SEGMENT:
        raise FetchError("the EXIF would be too large for a JPEG (%d bytes)" % len(payload))
    _check_exif(payload, copy, size)
    if index is None:
        segments.insert(_insert_position(segments), (APP1, payload))
    else:
        segments[index] = (APP1, payload)
    return segments


# ---------------------------------------------------------------------------
# XMP place names

def _xml_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def place_description(place):
    """An rdf:Description holding the place names (attributes, as exiftool writes them)."""
    lines = ['  <rdf:Description rdf:about=""']
    lines += ['    xmlns:%s="%s"' % pair for pair in XMP_NAMESPACES]
    for name, prefix in XMP_PLACE_FIELDS:
        if place.get(name):
            lines.append('    %s:%s="%s"' % (prefix, name, _xml_escape(str(place[name]))))
    return "\n".join(lines) + "/>\n"


def new_xmp_packet(place):
    return ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            + place_description(place) +
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
            '<?xpacket end="w"?>').encode("utf-8")


def xmp_with_place(packet, place):
    """An XMP packet (bytes) with the place names in it: the fields written lose any value
    they had, and a description holding the new ones is added (other fields stay)."""
    try:
        text = packet.decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError("the XMP is not UTF-8")
    for name, prefix in XMP_PLACE_FIELDS:
        if not place.get(name):
            continue
        qname = re.escape("%s:%s" % (prefix, name))
        text = re.sub(r"\s%s\s*=\s*(?:\"[^\"]*\"|'[^']*')" % qname, "", text)
        text = re.sub(r"<%s\s*/>|<%s(?:\s[^>]*)?>.*?</%s\s*>" % (qname, qname, qname), "", text,
                      flags=re.DOTALL)
    end = text.rfind("</rdf:RDF>")
    if end < 0:
        raise FetchError("the XMP has no rdf:RDF element")
    return (text[:end] + place_description(place).lstrip(" ") + " " + text[end:]).encode("utf-8")


def with_xmp_place(segments, place):
    """segments with the place names in their XMP (made when there is none)."""
    segments = list(segments)
    index = _find(segments, APP1, XMP_HEADER)
    if index is None:
        segments.insert(_insert_position(segments), (APP1, XMP_HEADER + new_xmp_packet(place)))
    else:
        packet = segments[index][1][len(XMP_HEADER):]
        segments[index] = (APP1, XMP_HEADER + xmp_with_place(packet, place))
    return segments


# ---------------------------------------------------------------------------
# Pictures and videos

def resize_jpeg(data, max_edge):
    """(JPEG bytes, (width, height)) of data resized to max_edge on the long edge (never
    enlarged), with the original's EXIF, XMP, IPTC and comment; size None when it is
    small enough already."""
    segments, _rest = split_jpeg(data)
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if max(width, height) <= max_edge:
            return data, None
        scale = max_edge / float(max(width, height))
        size = (round(width * scale), round(height * scale))
        small = image.resize(size, getattr(Image, "Resampling", Image).LANCZOS)
        buffer = io.BytesIO()
        small.save(buffer, "JPEG", quality=92, subsampling=0,
                   icc_profile=image.info.get("icc_profile"))
    new_segments, new_rest = split_jpeg(buffer.getvalue())
    # (The encoder's own segments, JFIF, ICC profile, tables, stay; the original's
    # metadata segments come after its JFIF.)
    carried = [(marker, payload) for marker, payload in segments
               if marker in (APP1, APP13, COM)]
    position = 1 if new_segments and new_segments[0][0] == APP0 else 0
    return join_jpeg(new_segments[:position] + carried + new_segments[position:], new_rest), size


def edit_picture(data, copy):
    """The bytes of a picture's copy: data (a JPEG) changed as copy says."""
    size = None
    if copy.get("max_edge"):
        data, size = resize_jpeg(data, int(copy["max_edge"]))
    segments, rest = split_jpeg(data)
    if size or any(copy.get(key) for key in EXIF_KEYS):
        segments = with_exif(segments, copy, size)
    if copy.get("xmp_place"):
        segments = with_xmp_place(segments, copy["xmp_place"])
    return join_jpeg(segments, rest)


def transcode_argv(ffmpeg, source, target, copy):
    """The ffmpeg command line of a video's 720p H.264 copy."""
    argv = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", source,
            "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn", "-map_metadata", "-1",
            "-map_metadata:s", "-1", "-map_chapters", "-1", "-vf", SCALE_720P,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart+use_metadata_tags"]
    if copy.get("video_date"):
        argv += ["-metadata", "com.apple.quicktime.creationdate=%s" % copy["video_date"]]
    if copy.get("video_location"):
        argv += ["-metadata", "com.apple.quicktime.location.ISO6709=%s" % copy["video_location"]]
    return argv + ["-f", "mp4", target]


def run_program(argv):
    """Run a program (no shell); returns (exit code, the end of what it said)."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        return -1, str(error)
    return result.returncode, (result.stderr or "")[-2000:]


def make_copy(item, original, target, ffmpeg=None):
    """Make an item's copy at target (through target.part) from its original. Returns what
    was done, in words."""
    copy = item.get("copy") or {}
    unknown = sorted(set(copy) - COPY_KEYS)
    if unknown:
        raise FetchError("unknown copy instructions %s (a newer manifest? update the program)"
                         % ", ".join(unknown))
    if not copy and item.get("copy_sha256") and os.path.isfile(target) \
            and file_sha256(target) == item["copy_sha256"]:
        return "copy already there"
    part = target + ".part"
    try:
        if copy.get("transcode"):
            if not ffmpeg:
                raise FetchError("ffmpeg is needed for a video")
            code, said = run_program(transcode_argv(ffmpeg, original, part, copy))
            if code != 0 or not os.path.isfile(part):
                raise FetchError("ffmpeg failed (exit code %s): %s" % (code, said.strip()))
        elif not copy:
            shutil.copyfile(original, part)
            if item.get("copy_sha256") and file_sha256(part) != item["copy_sha256"]:
                raise FetchError("the copy does not match the manifest's SHA-256")
        else:
            with open(original, "rb") as handle:
                data = handle.read()
            data = edit_picture(data, copy)
            with open(part, "wb") as handle:
                handle.write(data)
        os.replace(part, target)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return describe_copy(copy)


def find_ffmpeg(option=None):
    """(ffmpeg path or None, what is wrong): --ffmpeg PATH, else config.json's "ffmpeg"
    setting, else the PATH (videos.locate)."""
    import videos
    if option:
        setting = os.path.abspath(os.path.expanduser(option))
    else:
        try:
            setting = common.load_config().get("ffmpeg") or ""
        except common.ConfigError:
            setting = ""
    ffmpeg, _ffprobe, problem = videos.locate(setting if isinstance(setting, str) else "")
    if ffmpeg:
        return ffmpeg, ""
    return None, problem or "ffmpeg was not found."


def _inside(path, folder):
    path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(
        os.path.abspath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# The command line

def fetch(items, out, originals, ctx, downloader, ffmpeg=None, dry_run=False):
    """Download and copy items; returns (the number of failures, whether ctx stopped it
    before the end)."""
    failures, made, counts = [], 0, {"downloaded": 0, "already there": 0}
    stopped = False
    pending = []
    for item in items:
        if is_video(item) and not ffmpeg:
            continue
        pending.append(item)
    if not dry_run:
        os.makedirs(out, exist_ok=True)
        os.makedirs(originals, exist_ok=True)
    for number, item in enumerate(pending, 1):
        if ctx.should_stop():
            ctx.log("Stopped: %d of %d items done." % (number - 1, len(pending)), "warn")
            stopped = True
            break
        label = "%s (%d/%d)" % (item["file"], number, len(pending))
        try:
            original = os.path.join(originals, original_name(item))
            if dry_run:
                there = (os.path.isfile(original) and os.path.getsize(original) == item["bytes"]
                         and file_sha256(original) == item["sha256"])
                state = "already there" if there else "would download %s from %s" % (
                    megabytes(item["bytes"]), item["file_url"])
                counts["already there" if there else "downloaded"] += 1
                ctx.log("%s: %s; copy: %s" % (label, state, describe_copy(item.get("copy"))))
                continue
            state = ensure_original(item, original, downloader, ctx.log)
            counts[state] += 1
            done = make_copy(item, original, os.path.join(out, item["file"]), ffmpeg)
            made += 1
            ctx.log("%s: %s; %s" % (label, state if state == "already there"
                                    else "%s (%s)" % (state, megabytes(item["bytes"])), done))
        except (FetchError, OSError) as error:
            failures.append(item["file"])
            ctx.log("%s: %s" % (label, error), "error")
    if dry_run:
        ctx.log("Dry run: %d to download, %d already there; nothing was written."
                % (counts["downloaded"], counts["already there"]))
    else:
        ctx.log("Copies in %s: %d made, %d failed (%d downloaded, %d already there)."
                % (out, made, len(failures), counts["downloaded"], counts["already there"]))
    if failures:
        ctx.log("Failed: %s. Run the same command again to retry them." % ", ".join(failures),
                "error")
    if stopped:
        ctx.log("The set is not complete: run the same command again to finish it.", "warn")
    return len(failures), stopped


def _overlap(out, originals):
    """What is wrong with these folders for the copies and the originals, or ""."""
    if _inside(out, originals) or _inside(originals, out):
        return ("--out %s and --originals %s: the copies and the originals need folders of "
                "their own, neither inside the other (most copies have the name of their "
                "original, and a library of the copies would hold the originals too)."
                % (out, originals))
    return ""


def main(argv=None, downloader=None):
    manifest = load_manifest()
    groups = list(manifest.get("groups") or {})
    parser = argparse.ArgumentParser(
        description="Download the demo set (sample/demo-set/manifest.json: openly licensed "
                    "pictures and videos, credited in sample/demo-set/CREDITS.md), check "
                    "every file against the manifest, and make the copies the demo uses.")
    parser.add_argument("--out", metavar="DIR", default=DEFAULT_OUT,
                        help="the folder for the copies (default: sample/demo-set/files; a "
                             "library cannot be inside the program folder)")
    parser.add_argument("--originals", metavar="DIR", default=DEFAULT_ORIGINALS,
                        help="the folder for the downloaded originals, kept so a second run "
                             "downloads only what is missing (default: "
                             "sample/demo-set/originals)")
    parser.add_argument("--only", metavar="GROUP", action="append", choices=groups,
                        help="only this group (%s); may be given more than once"
                             % ", ".join(groups))
    parser.add_argument("--list", action="store_true",
                        help="list the items and the download size, and download nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be downloaded and copied, without doing it")
    parser.add_argument("--ffmpeg", metavar="PATH",
                        help="the ffmpeg program (or its folder) for the videos (default: the "
                             "\"ffmpeg\" setting in config.json, else the PATH)")
    jobs.add_settings_base_argument(parser)
    args = parser.parse_args(argv)
    jobs.use_settings_base(parser, args)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args.out = os.path.expanduser(args.out)
    args.originals = os.path.expanduser(args.originals)
    items = select(manifest, args.only)
    if args.list:
        list_items(manifest, items)
        return 0

    ctx = jobs.ConsoleContext("demo-set")
    jobs.log_tool_command(ctx, TOOL, argv)
    problem = _overlap(args.out, args.originals)
    if problem:
        ctx.log(problem, "error")
        return 2
    ffmpeg = None
    if any(is_video(item) for item in items):
        ffmpeg, problem = find_ffmpeg(args.ffmpeg)
        if not ffmpeg and args.ffmpeg:
            ctx.log("--ffmpeg %s: %s" % (args.ffmpeg, problem), "error")
            return 2
        if not ffmpeg:
            ctx.log("The videos (%d) are skipped, and not downloaded: they need ffmpeg. %s "
                    "Set it up (\"ffmpeg\" in config.json, see SETTINGS.md), or give --ffmpeg "
                    "PATH."
                    % (sum(1 for item in items if is_video(item)), problem), "warn")
    wanted = [item for item in items if ffmpeg or not is_video(item)]
    ctx.log("%d items, %s of originals into %s; the copies (about %s) into %s."
            % (len(wanted), megabytes(sum(item["bytes"] for item in wanted)),
               os.path.abspath(args.originals),
               megabytes(sum(item.get("copy_bytes") or item["bytes"] for item in wanted)),
               os.path.abspath(args.out)))
    downloader = downloader or Downloader(log=lambda message: ctx.log(message, "warn"))
    with ctx.stop_on_ctrl_c():
        failed, stopped = fetch(items, args.out, args.originals, ctx, downloader, ffmpeg,
                                args.dry_run)
    if stopped:
        return 130
    if not args.dry_run and not failed and _inside(args.out, REPO):
        ctx.log("A library cannot be inside the program folder: copy %s to a folder of its "
                "own (or run again with --out DIR outside the program folder), then add it "
                "as a library." % os.path.abspath(args.out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
