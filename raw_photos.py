#!/usr/bin/env python3
"""Camera RAW files (.dng, .arw, .cr2, .cr3, .nef, ...), read without a RAW decoder.

A RAW file holds what the camera's sensor saw, which only a RAW decoder
turns into a picture, and beside it the JPEG previews the camera made of
the shot (a small thumbnail and a larger one; some cameras a full-size
one). This module finds those previews and the file's EXIF facts (date,
camera, lens, exposure, GPS position) by walking its structure, with no
RAW decoder:

  - the TIFF directories (IFDs) most RAW formats are made of: DNG, ARW
    (Sony), CR2 (Canon), NEF and NRW (Nikon), ORF (Olympus), RW2
    (Panasonic), PEF (Pentax) and SRW (Samsung). A preview is a JPEG an
    IFD points to (JPEGInterchangeFormat, or a JPEG-compressed strip), or
    Panasonic's JpgFromRaw tag;
  - Fujifilm's RAF header, which points to a JPEG holding the EXIF;
  - Canon's CR3 boxes (ISO base media, like MP4): the EXIF in its CMT1,
    CMT2 and CMT4 boxes, the previews in its THMB and PRVW boxes and its
    first track.

Only JPEGs a JPEG library decodes count (baseline or progressive): the
sensor data of many RAW files is stored as a lossless JPEG, which is not
a preview. images.read_image() shows a RAW file by its largest preview
(preview_jpeg; the RAW file's Orientation turns it upright); a RAW file
without one cannot be shown (RawError).

Most RAW files have a picture of the same shot beside them, written by
the camera at the same time (a JPEG, or a HEIC): such a RAW file is that
picture's "RAW original" rather than a picture of its own. Pairs are
found by name (pair_names: the same folder and name before the
extension, whatever the case; same_shot: unless their EXIF names other
camera makers, or times more than PAIR_SECONDS apart) and else by camera
and time (capture_key: the same camera, and the same second by its
clock). The picture gets
the EXIF facts only the RAW file has (merge: its lens, say, or a GPS
position), and the RAW file's facts are kept in the picture's exif
column under "raw" (record); scan.py does the pairing, the server shows
the RAW file's name and size with the picture and lets the owner
download it (view). A RAW file with no such picture is a picture of its
own, shown by its preview (alone_record).

Only the standard library is used.
"""

import collections
import datetime
import os
import struct

import common
import imageinfo

RAW_EXTENSIONS = common.RAW_EXTENSIONS

# The settings (config.json "raw"): pairing RAW files with their pictures
# (enabled), and indexing a RAW file that has none as a picture of its own
# (index_alone).
DEFAULTS = {"enabled": True, "index_alone": True}

# The picture a RAW file goes with, among those of the same name: the
# camera's own picture first (a JPEG, or a HEIC), then those made from it.
SIBLING_ORDER = (".jpg", ".jpeg", ".heic", ".heif", ".hif", ".avif", ".insp", ".tif",
                 ".tiff", ".webp", ".png", ".bmp", ".gif")
# A RAW file and a picture of the same name are one shot only when their camera's
# clock dates them at most this far apart (seconds; some cameras date the RAW
# file a few seconds before its picture), see same_shot.
PAIR_SECONDS = 60

# What makes up a RAW file: limits that keep a damaged file from making
# the walk long or large.
MAX_IFDS = 64               # directories looked at in one file
MAX_ENTRIES = 1000          # entries of one directory
MAX_CHAIN = 16              # directories chained one after the other
MAX_DEPTH = 3               # SubIFDs within SubIFDs
MAX_VALUE = 65536           # longest tag value read (longer ones are not needed)
MAX_BOXES = 512             # CR3 boxes looked at
MAX_PREVIEW = 128 * 1024 * 1024     # longest preview JPEG taken
JPEG_HEAD = 262144          # bytes of a JPEG read to find its size
BLOCK = 65536               # a file is read in blocks this large (kept: BLOCKS_KEPT)
BLOCKS_KEPT = 32

# TIFF field types: (struct code, size in bytes).
_TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1),
          7: ("B", 1), 8: ("h", 2), 9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8),
          13: ("I", 4), 16: ("Q", 8), 17: ("q", 8), 18: ("Q", 8)}
_RATIONALS = (5, 10)
_BYTES = (1, 7)

# Tags of the image directories.
TAG_NEW_SUBFILE = 0x00FE
TAG_WIDTH, TAG_HEIGHT = 0x0100, 0x0101
TAG_COMPRESSION = 0x0103
TAG_PHOTOMETRIC = 0x0106
TAG_STRIP_OFFSETS, TAG_STRIP_BYTES = 0x0111, 0x0117
TAG_ORIENTATION = 0x0112
TAG_SUBIFDS = 0x014A
TAG_JPEG_OFFSET, TAG_JPEG_LENGTH = 0x0201, 0x0202
TAG_JPG_FROM_RAW = 0x002E       # Panasonic RW2: the preview's bytes, in IFD0
TAG_EXIF_IFD, TAG_GPS_IFD = 0x8769, 0x8825
TAG_DNG_VERSION = 0xC612
PHOTOMETRIC_CFA, PHOTOMETRIC_LINEAR_RAW = 32803, 34892
JPEG_COMPRESSIONS = (6, 7)      # old-style and new-style JPEG

# The EXIF tags kept, by the names Pillow gives them (images.EXIF_FIELDS).
EXIF_TAGS = {
    0x010F: "Make", 0x0110: "Model", 0xA433: "LensMake", 0xA434: "LensModel",
    0x9003: "DateTimeOriginal", 0x9011: "OffsetTimeOriginal",
    0x9004: "DateTimeDigitized", 0x9012: "OffsetTimeDigitized",
    0x0132: "DateTime", 0x9010: "OffsetTime",
    0x829A: "ExposureTime", 0x829D: "FNumber", 0x8827: "ISOSpeedRatings",
    0x920A: "FocalLength", 0xA405: "FocalLengthIn35mmFilm", 0x9204: "ExposureBiasValue",
    0x9209: "Flash", 0x0112: "Orientation", 0x0131: "Software", 0x013B: "Artist",
    0x8298: "Copyright", 0x010E: "ImageDescription",
}
GPS_TAGS = {
    0: "GPSVersionID", 1: "GPSLatitudeRef", 2: "GPSLatitude", 3: "GPSLongitudeRef",
    4: "GPSLongitude", 5: "GPSAltitudeRef", 6: "GPSAltitude", 7: "GPSTimeStamp",
    8: "GPSSatellites", 9: "GPSStatus", 10: "GPSMeasureMode", 11: "GPSDOP",
    12: "GPSSpeedRef", 13: "GPSSpeed", 14: "GPSTrackRef", 15: "GPSTrack",
    16: "GPSImgDirectionRef", 17: "GPSImgDirection", 18: "GPSMapDatum",
    19: "GPSDestLatitudeRef", 20: "GPSDestLatitude", 21: "GPSDestLongitudeRef",
    22: "GPSDestLongitude", 23: "GPSDestBearingRef", 24: "GPSDestBearing",
    25: "GPSDestDistanceRef", 26: "GPSDestDistance", 27: "GPSProcessingMethod",
    28: "GPSAreaInformation", 29: "GPSDateStamp", 30: "GPSDifferential",
    31: "GPSHPositioningError",
}
# EXIF facts a picture never takes from its RAW file: its own pixels decide
# which way is up.
NOT_MERGED = ("Orientation",)

RAF_MAGIC = b"FUJIFILMCCD-RAW "
CR3_BRAND = b"crx "
# The CR3 box holding the EXIF (CMT1-CMT4) and the thumbnail (THMB), and
# the one holding the preview (PRVW).
CR3_CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
CR3_PREVIEW_UUID = bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16")
# TIFF-like headers: (bytes 0-3, format) of the formats that change TIFF's
# magic number 42.
_TIFF_HEADS = {b"II*\x00": "", b"MM\x00*": "", b"IIRO": "ORF", b"IIRS": "ORF",
               b"MMOR": "ORF", b"IIU\x00": "RW2"}
# JPEG start-of-frame markers a JPEG library decodes (baseline, extended,
# progressive); the other SOF markers (lossless, hierarchical, arithmetic)
# are sensor data or rare.
_SOF_DECODED = (0xC0, 0xC1, 0xC2)
_SOF_OTHER = (0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)

MIME_TYPE = "image/x-raw"


class RawError(ValueError):
    """A RAW file that cannot be read: damaged, of a kind not known, or with no preview."""


Preview = collections.namedtuple("Preview", "offset length width height")


class RawInfo:
    """What read() found in a RAW file.

    format       "ARW", "DNG", ... (by the extension when it is a RAW one,
                 else by the structure)
    exif         {name: value} as Pillow gives them (not cleaned): the
                 EXIF_TAGS found, and "GPS": {GPS_TAGS name: value}
    orientation  the EXIF Orientation of the shot (1-8), or None
    previews     [Preview(offset, length, width, height)] of the decodable
                 JPEG previews, largest first
    sensor       (width, height) of the sensor data, or None
    """

    def __init__(self, format_name):
        self.format = format_name
        self.exif = {}
        self.orientation = None
        self.previews = []
        self.sensor = None

    def largest(self):
        """The largest preview, or None."""
        return self.previews[0] if self.previews else None


# ---------------------------------------------------------------------------
# Settings and names

def settings(config):
    """config.json "raw" checked: {"enabled", "index_alone"} (both on by default; a bare
    true or false is taken as "enabled")."""
    stored = config.get("raw") if isinstance(config, dict) else None
    result = dict(DEFAULTS)
    if isinstance(stored, bool):
        result["enabled"] = stored
    elif isinstance(stored, dict):
        for key in DEFAULTS:
            if isinstance(stored.get(key), bool):
                result[key] = stored[key]
    return result


def is_raw_name(name):
    """True for a file name with a RAW extension (RAW_EXTENSIONS)."""
    return common.is_raw_file(name or "")


def format_of(name):
    """"ARW", "DNG", ... for a RAW file name; "" for another name."""
    extension = os.path.splitext(name or "")[1].lower()
    return extension[1:].upper() if extension in RAW_EXTENSIONS else ""


def stem(name):
    """A file name without its extension, folded for comparing ("DSC0001.ARW" -> "dsc0001")."""
    return os.path.splitext(name or "")[0].casefold()


def _sibling_rank(name):
    extension = os.path.splitext(name)[1].lower()
    return (SIBLING_ORDER.index(extension) if extension in SIBLING_ORDER
            else len(SIBLING_ORDER), name)


def pair_names(raw_names, image_names):
    """{RAW name: the picture's name} of the RAW files of one folder with a picture of
    the same name (stem, whatever the case; the picture's kind by SIBLING_ORDER).

    raw_names and image_names are the names of the folder's RAW files and
    pictures (image_names may hold RAW names too: they are skipped). A
    picture goes with one RAW file: of two RAW files of the same name (a
    .ARW and a .DNG) the first by name gets it.
    """
    pictures = collections.defaultdict(list)
    for name in image_names:
        if not is_raw_name(name):
            pictures[stem(name)].append(name)
    pairs, taken = {}, set()
    for name in sorted(raw_names):
        found = sorted(pictures.get(stem(name), ()), key=_sibling_rank)
        for picture in found:
            if picture not in taken:
                pairs[name] = picture
                taken.add(picture)
                break
    return pairs


def capture_key(exif):
    """(camera, "YYYY-MM-DDTHH:MM:SS") of a shot by its EXIF (cleaned or not), for pairing
    a RAW file with its picture by camera and time; None without both.

    The time is the camera's clock (DateTimeOriginal, else
    DateTimeDigitized), to the second; the camera its make and model
    (imageinfo.camera_name), whatever the case.
    """
    if not isinstance(exif, dict):
        return None
    camera = imageinfo.camera_name(exif).casefold()
    taken_at = _capture_time(exif)
    return (camera, taken_at) if camera and taken_at else None


def _capture_time(exif):
    """'YYYY-MM-DDTHH:MM:SS' of a shot by the camera's clock (DateTimeOriginal, else
    DateTimeDigitized), or ""."""
    for tag in ("DateTimeOriginal", "DateTimeDigitized"):
        taken_at, _source, _offset = imageinfo.parse_capture_date({tag: exif.get(tag)}, "",
                                                                   None)
        if taken_at:
            return taken_at
    return ""


def _maker(exif):
    """The first word of a shot's camera maker (EXIF Make), whatever the case ("nikon"
    of "NIKON CORPORATION"), or ""."""
    make = exif.get("Make") if isinstance(exif, dict) else None
    words = make.casefold().split() if isinstance(make, str) else []
    return words[0] if words else ""


def same_shot(picture_exif, raw_exif):
    """False when a picture and a RAW file of the same name (their EXIF; the picture's
    own, see own_exif) cannot be one shot: both name their camera's maker and those
    differ, or both say when they were taken and that is more than PAIR_SECONDS
    apart (two cameras that both name their files IMG_1234, say). True when they
    may be (or they do not say: a picture without EXIF is taken at its name)."""
    picture_exif = picture_exif if isinstance(picture_exif, dict) else {}
    raw_exif = raw_exif if isinstance(raw_exif, dict) else {}
    first, second = _maker(picture_exif), _maker(raw_exif)
    if first and second and first != second:
        return False
    first, second = _capture_time(picture_exif), _capture_time(raw_exif)
    if first and second:
        apart = datetime.datetime.fromisoformat(first) - datetime.datetime.fromisoformat(second)
        if abs(apart.total_seconds()) > PAIR_SECONDS:
            return False
    return True


# ---------------------------------------------------------------------------
# Reading bytes

class _Source:
    """Random access to the bytes of a file: bytes, or an open binary file read in
    BLOCK sized pieces (the last BLOCKS_KEPT kept), so walking a RAW file's
    directories reads only small parts of it, also on a network share."""

    def __init__(self, data=None, handle=None):
        self.data = data
        self.handle = handle
        self.blocks = collections.OrderedDict()
        if data is not None:
            self.size = len(data)
        else:
            handle.seek(0, os.SEEK_END)
            self.size = handle.tell()

    def _block(self, index):
        found = self.blocks.get(index)
        if found is None:
            self.handle.seek(index * BLOCK)
            found = self.handle.read(BLOCK)
            self.blocks[index] = found
            while len(self.blocks) > BLOCKS_KEPT:
                self.blocks.popitem(last=False)
        else:
            self.blocks.move_to_end(index)
        return found

    def read(self, offset, length):
        """Up to length bytes at offset (fewer at the end of the file, b"" past it)."""
        if offset < 0 or length <= 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        if self.data is not None:
            return bytes(self.data[offset:offset + length])
        if length > BLOCK * 4:
            self.handle.seek(offset)
            return self.handle.read(length)
        parts = []
        position, end = offset, offset + length
        while position < end:
            index = position // BLOCK
            block = self._block(index)
            start = position - index * BLOCK
            piece = block[start:start + end - position]
            if not piece:
                break
            parts.append(piece)
            position += len(piece)
        return b"".join(parts)


# ---------------------------------------------------------------------------
# TIFF directories

class _Tiff:
    """The TIFF structure of a file (or of a part of one: base is where its header is).
    Offsets in it are from base."""

    def __init__(self, source, base=0):
        self.source = source
        self.base = base
        head = source.read(base, 8)
        if len(head) < 8 or head[:2] not in (b"II", b"MM"):
            raise RawError("not a TIFF structure")
        self.endian = "<" if head[:2] == b"II" else ">"
        magic = struct.unpack(self.endian + "H", head[2:4])[0]
        if magic not in (42, 0x4F52, 0x5352, 0x55):
            raise RawError("not a TIFF structure")
        self.kind = _TIFF_HEADS.get(head[:4], "")
        self.first = struct.unpack(self.endian + "I", head[4:8])[0]

    def ifd(self, offset):
        """({tag: (type, count, value field, absolute value offset or None)}, next offset)
        of the directory at offset; RawError for one that cannot be."""
        start = self.base + offset
        head = self.source.read(start, 2)
        if len(head) < 2:
            raise RawError("a directory lies past the end of the file")
        count = struct.unpack(self.endian + "H", head)[0]
        if not 0 < count <= MAX_ENTRIES:
            raise RawError("a directory has %d entries" % count)
        block = self.source.read(start + 2, 12 * count + 4)
        if len(block) < 12 * count:
            raise RawError("a directory is cut short")
        entries = {}
        for index in range(count):
            tag, kind, number = struct.unpack(self.endian + "HHI",
                                              block[12 * index:12 * index + 8])
            field = block[12 * index + 8:12 * index + 12]
            size = _TYPES.get(kind, (None, 0))[1] * number
            where = None
            if size > 4:
                where = self.base + struct.unpack(self.endian + "I", field)[0]
            entries.setdefault(tag, (kind, number, field, where))
        tail = block[12 * count:12 * count + 4]
        following = struct.unpack(self.endian + "I", tail)[0] if len(tail) == 4 else 0
        return entries, following

    def _payload(self, entry, limit=MAX_VALUE):
        """The bytes of an entry's value (None when longer than limit or unreadable)."""
        kind, number, field, where = entry
        size = _TYPES.get(kind, (None, 0))[1] * number
        if size > limit or kind not in _TYPES:
            return None
        if where is None:
            return field[:size]
        data = self.source.read(where, size)
        return data if len(data) == size else None

    def value(self, entry):
        """An entry's value as Pillow gives it: text, bytes, a number, a float for a
        rational (None when its denominator is 0), or a tuple of them; None when it
        cannot be read."""
        kind, number = entry[0], entry[1]
        data = self._payload(entry)
        if data is None or not number:
            return None
        if kind == 2:
            text = data.split(b"\x00", 1)[0]
            try:
                return text.decode("utf-8")
            except UnicodeDecodeError:
                return text.decode("latin-1")
        if kind in _BYTES:
            return bytes(data)
        code = _TYPES[kind][0]
        if kind in _RATIONALS:
            pairs = struct.unpack(self.endian + code[0] * (2 * number), data)
            values = tuple(pairs[2 * i] / float(pairs[2 * i + 1]) if pairs[2 * i + 1] else None
                           for i in range(number))
        else:
            values = struct.unpack(self.endian + code * number, data)
        return values[0] if number == 1 else values

    def numbers(self, entry):
        """An entry's value as a tuple of integers (offsets, counts); () when it is not."""
        if entry is None or entry[0] not in (1, 3, 4, 8, 9, 13, 16, 17, 18):
            return ()
        value = self.value(entry)
        if isinstance(value, bytes):
            return tuple(value)
        if isinstance(value, int):
            return (value,)
        return tuple(item for item in value or () if isinstance(item, int))

    def number(self, entries, tag):
        found = self.numbers(entries.get(tag))
        return found[0] if found else None

    def directories(self):
        """The image directories: IFD0, those chained after it and their SubIFDs (IFD0
        first); RawError when not even IFD0 can be read."""
        found, seen = [], set()

        def visit(offset, depth):
            if not offset or offset in seen or len(seen) >= MAX_IFDS:
                return 0
            seen.add(offset)
            try:
                entries, following = self.ifd(offset)
            except RawError:
                if not found:
                    raise
                return 0
            found.append(entries)
            if depth < MAX_DEPTH:
                for child in self.numbers(entries.get(TAG_SUBIFDS)):
                    visit(child, depth + 1)
            return following

        offset, chained = self.first, 0
        while offset and chained < MAX_CHAIN:
            offset = visit(offset, 0)
            chained += 1
        if not found:
            raise RawError("the file has no image directory")
        return found

    def tags(self, entries, names):
        """{name: value} of the entries named in names ({tag: name}) that can be read."""
        values = {}
        for tag, name in names.items():
            entry = entries.get(tag)
            if entry is None:
                continue
            value = self.value(entry)
            if value is not None:
                values[name] = value
        return values

    def sub_directory(self, entries, tag):
        """The entries of the sub-directory a tag points to (Exif, GPS), or {}."""
        offset = self.number(entries, tag)
        if not offset:
            return {}
        try:
            return self.ifd(offset)[0]
        except RawError:
            return {}


def _jpeg_size(source, offset, length):
    """(width, height) of a JPEG a JPEG library decodes at offset (length bytes long), or
    None for anything else (not a JPEG, a lossless one, a damaged one)."""
    if length < 4 or length > MAX_PREVIEW or offset < 0 or offset + length > source.size:
        return None
    head = source.read(offset, min(length, JPEG_HEAD))
    if head[:3] != b"\xff\xd8\xff":
        return None
    position = 2
    while position + 4 <= len(head):
        if head[position] != 0xFF:
            return None
        marker = head[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            position += 2
            continue
        if marker in (0xD9, 0xDA):
            return None
        segment = struct.unpack(">H", head[position + 2:position + 4])[0]
        if marker in _SOF_DECODED:
            if position + 9 > len(head):
                return None
            height, width = struct.unpack(">HH", head[position + 5:position + 9])
            return (width, height) if width and height else None
        if marker in _SOF_OTHER:
            return None
        position += 2 + segment
    return None


def _add_preview(info, source, offset, length, seen):
    if offset in seen:
        return
    seen.add(offset)
    size = _jpeg_size(source, offset, length)
    if size is not None:
        info.previews.append(Preview(offset, length, size[0], size[1]))


def _sort_previews(info):
    info.previews.sort(key=lambda found: (-found.width * found.height, -found.length,
                                          found.offset))


def _orientation(value):
    return value if isinstance(value, int) and 1 <= value <= 8 else None


def _read_tiff(source, name, base=0):
    tiff = _Tiff(source, base)
    directories = tiff.directories()
    first = directories[0]
    raw_format = format_of(name)
    if not raw_format:
        if tiff.kind:
            raw_format = tiff.kind
        elif source.read(base + 8, 2) == b"CR":
            raw_format = "CR2"
        elif TAG_DNG_VERSION in first:
            raw_format = "DNG"
        else:
            raw_format = "RAW"
    info = RawInfo(raw_format)
    info.exif = tiff.tags(first, EXIF_TAGS)
    info.exif.update(tiff.tags(tiff.sub_directory(first, TAG_EXIF_IFD), EXIF_TAGS))
    gps = tiff.tags(tiff.sub_directory(first, TAG_GPS_IFD), GPS_TAGS)
    if gps:
        info.exif["GPS"] = gps
    info.orientation = _orientation(info.exif.get("Orientation"))
    seen = set()
    sensor = None
    for entries in directories:
        photometric = tiff.number(entries, TAG_PHOTOMETRIC)
        width, height = tiff.number(entries, TAG_WIDTH), tiff.number(entries, TAG_HEIGHT)
        if photometric in (PHOTOMETRIC_CFA, PHOTOMETRIC_LINEAR_RAW) and width and height:
            if sensor is None or width * height > sensor[0] * sensor[1]:
                sensor = (width, height)
        start, length = (tiff.number(entries, TAG_JPEG_OFFSET),
                         tiff.number(entries, TAG_JPEG_LENGTH))
        if start and length:
            _add_preview(info, source, base + start, length, seen)
        if (tiff.number(entries, TAG_COMPRESSION) in JPEG_COMPRESSIONS
                and photometric not in (PHOTOMETRIC_CFA, PHOTOMETRIC_LINEAR_RAW)):
            strips = tiff.numbers(entries.get(TAG_STRIP_OFFSETS))
            counts = tiff.numbers(entries.get(TAG_STRIP_BYTES))
            if len(strips) == 1 and len(counts) == 1:
                _add_preview(info, source, base + strips[0], counts[0], seen)
    embedded = first.get(TAG_JPG_FROM_RAW)
    if embedded is not None and embedded[0] in _BYTES and embedded[3] is not None:
        # (Panasonic: the preview's bytes are the tag's value.)
        _add_preview(info, source, embedded[3], embedded[1], seen)
    info.sensor = sensor
    _sort_previews(info)
    return info


def _exif_base(source, offset, length):
    """Where the TIFF structure of a JPEG's EXIF (APP1 "Exif") starts, or None."""
    head = source.read(offset, min(length, JPEG_HEAD))
    position = 2
    while position + 4 <= len(head) and head[position] == 0xFF:
        marker = head[position + 1]
        segment = struct.unpack(">H", head[position + 2:position + 4])[0]
        if marker == 0xE1 and head[position + 4:position + 10] == b"Exif\x00\x00":
            return offset + position + 10
        if marker in (0xDA, 0xD9) or marker in _SOF_DECODED:
            return None
        position += 2 + segment
    return None


def _read_raf(source, name):
    head = source.read(0, 100)
    if len(head) < 92:
        raise RawError("the RAF header is cut short")
    start, length = struct.unpack(">II", head[84:92])
    info = RawInfo(format_of(name) or "RAF")
    base = _exif_base(source, start, length) if start and length else None
    if base is not None:
        try:
            inner = _read_tiff(source, "x.raf", base)
        except RawError:
            inner = None
        if inner is not None:
            info.exif = inner.exif
            info.orientation = inner.orientation
            info.previews = list(inner.previews)
    _add_preview(info, source, start, length, {found.offset for found in info.previews})
    _sort_previews(info)
    return info


def _boxes(source, start, end, budget):
    """[(type, payload start, end)] of the ISO media boxes between start and end."""
    found = []
    position = start
    while position + 8 <= end and budget[0] > 0:
        budget[0] -= 1
        head = source.read(position, 16)
        if len(head) < 8:
            break
        size, kind = struct.unpack(">I", head[:4])[0], head[4:8]
        payload = position + 8
        if size == 1:
            if len(head) < 16:
                break
            size = struct.unpack(">Q", head[8:16])[0]
            payload = position + 16
        elif size == 0:
            size = end - position
        if size < payload - position or position + size > end:
            break
        found.append((kind, payload, position + size))
        position += size
    return found


def _box_jpeg(info, source, start, end, seen):
    """A JPEG within the first bytes of a box's payload (THMB, PRVW) as a preview."""
    head = source.read(start, 64)
    at = head.find(b"\xff\xd8\xff")
    if at >= 0:
        _add_preview(info, source, start + at, end - start - at, seen)


def _first_sample(source, stbl, budget):
    """(offset, size) of the first sample of a track (its stbl box), or None."""
    sizes = offsets = None
    for kind, start, end in _boxes(source, stbl[0], stbl[1], budget):
        data = source.read(start, 24)
        if kind == b"stsz" and len(data) >= 12:
            sample_size, count = struct.unpack(">II", data[4:12])
            if sample_size:
                sizes = sample_size
            elif count and len(data) >= 16:
                sizes = struct.unpack(">I", data[12:16])[0]
        elif kind == b"co64" and len(data) >= 16:
            if struct.unpack(">I", data[4:8])[0]:
                offsets = struct.unpack(">Q", data[8:16])[0]
        elif kind == b"stco" and len(data) >= 12:
            if struct.unpack(">I", data[4:8])[0]:
                offsets = struct.unpack(">I", data[8:12])[0]
    if sizes and offsets:
        return offsets, sizes
    return None


def _child(source, box, path, budget):
    """The (start, end) of the box at path (types) below box (start, end), or None."""
    for kind in path:
        for found, start, end in _boxes(source, box[0], box[1], budget):
            if found == kind:
                box = (start, end)
                break
        else:
            return None
    return box


def _read_cr3(source, name):
    info = RawInfo(format_of(name) or "CR3")
    budget = [MAX_BOXES]
    seen = set()
    top = _boxes(source, 0, source.size, budget)
    for kind, start, end in top:
        if kind == b"moov":
            for inner, inner_start, inner_end in _boxes(source, start, end, budget):
                if inner == b"uuid" and source.read(inner_start, 16) == CR3_CANON_UUID:
                    _read_cr3_metadata(info, source, inner_start + 16, inner_end, budget, seen)
                elif inner == b"trak":
                    stbl = _child(source, (inner_start, inner_end),
                                  (b"mdia", b"minf", b"stbl"), budget)
                    sample = _first_sample(source, stbl, budget) if stbl else None
                    if sample is not None:
                        _add_preview(info, source, sample[0], sample[1], seen)
        elif kind == b"uuid" and source.read(start, 16) == CR3_PREVIEW_UUID:
            for inner, inner_start, inner_end in _boxes(source, start + 24, end, budget):
                if inner == b"PRVW":
                    _box_jpeg(info, source, inner_start, inner_end, seen)
    info.orientation = _orientation(info.exif.get("Orientation"))
    _sort_previews(info)
    return info


def _read_cr3_metadata(info, source, start, end, budget, seen):
    """The EXIF in a CR3's CMT boxes (TIFF structures: CMT1 the main directory, CMT2 the
    Exif one, CMT4 the GPS one) and its THMB thumbnail."""
    for kind, inner_start, inner_end in _boxes(source, start, end, budget):
        if kind == b"THMB":
            _box_jpeg(info, source, inner_start, inner_end, seen)
            continue
        if kind not in (b"CMT1", b"CMT2", b"CMT4"):
            continue
        try:
            tiff = _Tiff(source, inner_start)
            entries = tiff.ifd(tiff.first)[0]
        except RawError:
            continue
        if kind == b"CMT4":
            gps = tiff.tags(entries, GPS_TAGS)
            if gps:
                info.exif["GPS"] = gps
        else:
            info.exif.update(tiff.tags(entries, EXIF_TAGS))


def _read(source, name):
    head = source.read(0, 16)
    try:
        if head.startswith(RAF_MAGIC):
            return _read_raf(source, name)
        if head[4:8] == b"ftyp":
            if head[8:12] != CR3_BRAND:
                raise RawError("not a RAW file")
            return _read_cr3(source, name)
        return _read_tiff(source, name)
    except (struct.error, IndexError, OverflowError) as error:
        # (A damaged file that slipped past the checks.)
        raise RawError("the file is damaged (%s)" % error) from error


def read(data, name=""):
    """RawInfo of a RAW file's bytes (name: its file name, for its format); RawError when
    it is no RAW file this can read."""
    return _read(_Source(data=data), name)


def read_file(path, helper=None):
    """RawInfo of a RAW file, reading only the parts needed (RawError when it cannot be
    read; OSError when the file cannot be opened). With a helper that loads files
    (helper.load_image), its bytes are used."""
    loader = getattr(helper, "load_image", None) if helper is not None else None
    if loader is not None:
        data = loader(path)
        if data is not None:
            return read(bytes(data), os.path.basename(path))
    with open(path, "rb") as handle:
        return _read(_Source(handle=handle), os.path.basename(path))


def preview_jpeg(data, info):
    """The bytes of the largest preview of a RAW file's bytes (info: read(data)), or None."""
    found = info.largest() if info is not None else None
    if found is None:
        return None
    return bytes(data[found.offset:found.offset + found.length])


def is_raw_data(data):
    """True when bytes are a RAW file this module reads (a camera's RAW: not an ordinary
    TIFF), by their structure rather than a name."""
    head = bytes(data[:16])
    if head.startswith(RAF_MAGIC) or (head[4:8] == b"ftyp" and head[8:12] == CR3_BRAND):
        return True
    if head[:4] in (b"IIRO", b"IIRS", b"MMOR", b"IIU\x00"):
        return True
    if head[:4] not in (b"II*\x00", b"MM\x00*"):
        return False
    if head[8:10] == b"CR":
        return True
    try:
        tiff = _Tiff(_Source(data=data))
        directories = tiff.directories()
    except (RawError, struct.error):
        return False
    if TAG_DNG_VERSION in directories[0]:
        return True
    return any(tiff.number(entries, TAG_PHOTOMETRIC) in (PHOTOMETRIC_CFA,
                                                         PHOTOMETRIC_LINEAR_RAW)
               for entries in directories)


def maybe_raw(head):
    """True when a file's first bytes (16 are enough) may be a RAW file (is_raw_data
    then decides): a TIFF-like header, RAF's or a CR3's."""
    head = bytes(head[:16])
    return (head[:4] in _TIFF_HEADS or head.startswith(RAF_MAGIC)
            or (head[4:8] == b"ftyp" and head[8:12] == CR3_BRAND))


def exif_dict(info):
    """The cleaned EXIF dict of a RAW file (imageinfo.sanitize_exif of info.exif)."""
    return imageinfo.sanitize_exif(info.exif if info is not None else {})


# ---------------------------------------------------------------------------
# A RAW file with its picture: what the picture's exif column keeps

def _position(gps):
    return imageinfo.exif_gps_to_decimal(gps) != (None, None)


def own_exif(exif):
    """A picture's EXIF without what it took from its RAW file (and without the RAW
    file's record): as its own file gives it."""
    if not isinstance(exif, dict):
        return {}
    record = exif.get("raw")
    own = {key: value for key, value in exif.items() if key != "raw"}
    if isinstance(record, dict) and record.get("path"):
        for key in record.get("filled") or ():
            own.pop(key, None)
        if isinstance(record.get("own_gps"), dict):
            own["GPS"] = record["own_gps"]
    return own


def merge(picture_exif, raw_exif):
    """(merged EXIF, names filled, the picture's own GPS dict it replaced or None).

    The picture keeps its own EXIF; it gets the facts only its RAW file has
    (but never its Orientation, see NOT_MERGED), and the RAW file's GPS
    position when it has none of its own.
    """
    merged = dict(own_exif(picture_exif))
    raw_exif = raw_exif if isinstance(raw_exif, dict) else {}
    filled, replaced = [], None
    for key, value in raw_exif.items():
        if key in NOT_MERGED or key == "GPS" or key == "raw":
            continue
        if key not in merged:
            merged[key] = value
            filled.append(key)
    gps = raw_exif.get("GPS")
    if isinstance(gps, dict) and _position(gps) and not _position(merged.get("GPS")):
        if isinstance(merged.get("GPS"), dict):
            replaced = merged["GPS"]
        merged["GPS"] = gps
        filled.append("GPS")
    return merged, sorted(filled), replaced


def record(path, size, mtime, how, raw_exif, filled, own_gps=None, raw_format=""):
    """The "raw" entry of a picture's exif column for its RAW original.

    path is the RAW file's (relative to the library), size and mtime its
    file's, how "name" or "time" (how it was paired), raw_exif its cleaned
    EXIF, filled the names the picture took from it (merge), own_gps the
    GPS dict of the picture's own it replaced.
    """
    entry = {"path": path, "name": path.rsplit("/", 1)[-1], "size": int(size or 0),
             "mtime": float(mtime or 0), "format": raw_format or format_of(path),
             "how": how, "exif": raw_exif if isinstance(raw_exif, dict) else {},
             "filled": list(filled or ())}
    if isinstance(own_gps, dict):
        entry["own_gps"] = own_gps
    return entry


def alone_record(info, preview_size):
    """The "raw" entry of the exif column of a RAW file indexed as a picture of its own:
    its format, the size of the preview that shows it (preview_size, upright) and of
    its sensor data."""
    entry = {"how": "alone", "format": info.format, "preview": list(preview_size)}
    if info.sensor:
        entry["sensor"] = list(info.sensor)
    return entry


def paired_record(exif):
    """The "raw" entry of a picture with a RAW original, or None (also for a RAW file
    indexed on its own)."""
    entry = exif.get("raw") if isinstance(exif, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]:
        return entry
    return None


def same_file(entry, path, size, mtime):
    """True when a "raw" entry is for this RAW file, as it is now (size, mtime)."""
    return (isinstance(entry, dict) and entry.get("path") == path
            and entry.get("size") == size
            and abs(float(entry.get("mtime") or 0) - float(mtime or 0)) < 1.0)


def safe_path(path):
    """True for a library-relative RAW path that stays inside its library (no "..",
    no drive or root), so the server may open it."""
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        return False
    parts = path.split("/")
    return (all(part not in ("", ".", "..") for part in parts)
            and is_raw_name(parts[-1]))


def view(exif, alone_name="", alone_size=0):
    """What the server says about a picture's RAW file (None without one):
    {name, size, format, how ("name", "time" or "alone"), preview, sensor}.

    For a RAW file indexed on its own (exif's "raw" says "alone"), its own
    name and size are alone_name and alone_size, and preview the size of
    the preview that shows it.
    """
    entry = exif.get("raw") if isinstance(exif, dict) else None
    if not isinstance(entry, dict):
        return None
    found = paired_record(exif)
    if found is not None:
        return {"name": found.get("name") or found["path"].rsplit("/", 1)[-1],
                "size": found.get("size") or 0, "format": found.get("format") or "",
                "how": found.get("how") or "name"}
    if entry.get("how") != "alone":
        return None
    result = {"name": alone_name, "size": alone_size or 0,
              "format": entry.get("format") or format_of(alone_name), "how": "alone"}
    for key in ("preview", "sensor"):
        value = entry.get(key)
        if (isinstance(value, list) and len(value) == 2
                and all(isinstance(item, int) and item > 0 for item in value)):
            result[key] = value
    return result
