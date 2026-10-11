#!/usr/bin/env python3
"""Facts about image files that need no image library.

Format sniffing from a file's first bytes, EXIF cleaning, the capture date,
GPS position and camera name worked out from an EXIF dict, and the metadata
clues for 360-degree photos (XMP GPano tags, 360 camera makers). images.py
uses these on freshly read files; db.py uses the same functions on EXIF JSON
stored by older versions, so both give the same answers.

This module uses only the standard library.
"""

import datetime
import math
import os
import re

MAX_TEXT = 2000           # longest EXIF string kept
YEAR_MIN, YEAR_MAX = 1900, 2100


# ---------------------------------------------------------------------------
# Format sniffing

# MIME types every browser can show directly; anything else is converted.
BROWSER_MIME_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp",
                      "image/bmp")

# ISO base media "ftyp" brands. HEIC files need pillow-heif; AVIF files are
# decoded by Pillow itself when it was built with AVIF support.
HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
               b"hevm", b"hevs")
HEIF_BRANDS = HEIC_BRANDS + (b"mif1", b"msf1", b"mif2", b"miaf")
AVIF_BRANDS = (b"avif", b"avis")


def ftyp_brands(data):
    """Return (major_brand, compatible_brands) of an ISO media file, or (None, ())."""
    if len(data) < 12 or bytes(data[4:8]) != b"ftyp":
        return None, ()
    size = int.from_bytes(bytes(data[0:4]), "big")
    end = min(len(data), size if 16 <= size <= 256 else 16)
    compatible = tuple(bytes(data[i:i + 4]) for i in range(16, end - 3, 4))
    return bytes(data[8:12]), compatible


def sniff_mime(data):
    """Return the image MIME type implied by the first bytes of a file, or None.

    Only the leading bytes are needed (64 is plenty), so callers can pass a
    short prefix instead of a whole file.
    """
    head = bytes(data[:64])
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "image/tiff"
    if head.startswith(b"BM") and len(head) >= 26:
        return "image/bmp"
    major, compatible = ftyp_brands(head)
    if major is not None:
        if major in AVIF_BRANDS:
            return "image/avif"
        if major in HEIC_BRANDS or any(b in HEIC_BRANDS for b in compatible):
            return "image/heic"
        if any(b in AVIF_BRANDS for b in compatible):
            return "image/avif"
        if major in HEIF_BRANDS:
            return "image/heif"
    return None


def browser_can_display(mime):
    """True if browsers show this MIME type directly (no conversion needed)."""
    return mime in BROWSER_MIME_TYPES


# ---------------------------------------------------------------------------
# EXIF cleaning

def _is_bad_rational(value):
    """True for a rational number with a zero denominator (Pillow's IFDRational)."""
    try:
        return getattr(value, "denominator", 1) == 0
    except Exception:
        return False


def _clean_text(text):
    """Strip NULs and whitespace, cap the length, drop unencodable characters."""
    text = text.replace("\x00", "").strip()[:MAX_TEXT]
    # Lone surrogates cannot be stored as UTF-8 (SQLite, JSON files).
    return text.encode("utf-8", "replace").decode("utf-8")


def _clean_bytes(value):
    """Decode EXIF bytes: text when it looks like text, else small int lists."""
    if value.startswith(b"ASCII\x00\x00\x00"):    # EXIF character-code prefix
        value = value[8:]
    stripped = value.rstrip(b"\x00")
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError:
        text = stripped.decode("latin-1")
    if all(char.isprintable() or char in "\t\r\n" for char in text):
        return _clean_text(text)
    if len(value) <= 8:      # e.g. GPSVersionID b"\x02\x02\x00\x00"
        return list(value)
    return None


def clean_exif_value(value):
    """Convert one EXIF value into something JSON can always store.

    Rationals with a zero denominator, NaN and infinities become None, bytes
    are decoded, tuples become lists and other objects become strings.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (bytes, bytearray)):
        return _clean_bytes(bytes(value))
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return sanitize_exif(value)
    if isinstance(value, (tuple, list)):
        return [clean_exif_value(item) for item in value]
    if _is_bad_rational(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return _clean_text(str(value))
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else round(number, 6)


def _is_empty(value):
    if value is None or value == "" or value == {}:
        return True
    if isinstance(value, list):
        return all(_is_empty(item) for item in value)
    return False


def sanitize_exif(exif):
    """Return a cleaned copy of an EXIF dict (raw or parsed from JSON).

    Every value goes through clean_exif_value() and empty values are dropped,
    so json.dumps(result, allow_nan=False) never fails. Non-dict input gives {}.
    """
    if not isinstance(exif, dict):
        return {}
    result = {}
    for key, value in exif.items():
        cleaned = clean_exif_value(value)
        if not _is_empty(cleaned):
            result[str(key)] = cleaned
    return result


# ---------------------------------------------------------------------------
# Capture date

# EXIF date tags in order of preference, each with its offset tag.
_DATE_TAGS = (
    ("DateTimeOriginal", "OffsetTimeOriginal"),
    ("DateTimeDigitized", "OffsetTimeDigitized"),
    ("DateTime", "OffsetTime"),
)

# "2019:05:14 14:23:01", "2019-05-14T14:23", "2019:05:14 14:23:01.123+02:00"
_EXIF_DATE_RE = re.compile(
    r"^\s*(\d{4})[:\-/.](\d{1,2})[:\-/.](\d{1,2})"
    r"(?:(?:\s+|T)(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?(?:[.,]\d+)?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?\s*$")

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")

# File name patterns; the first valid match wins. Times are optional.
_NAME_PATTERNS = (
    # 2019-05-14 14.23.01, Screenshot_2019-05-14-14-23-01,
    # Screenshot 2019-05-14 at 14.23.01, WhatsApp Image 2019-05-14 at 2.23.01 PM
    re.compile(
        r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})[-_.](\d{2})"
        r"(?:(?:\s+at\s+|[ _T-]+)(\d{1,2})[-_.:](\d{2})(?:[-_.:](\d{2}))?"
        r"(?:\s*([AaPp])\.?[Mm]\.?(?![A-Za-z]))?)?(?!\d)"),
    # IMG_20190514_142301, PXL_20210704_183015123, 20190514_142301,
    # Screenshot_20190514-142301, IMG-20190514-WA0001 (date only)
    re.compile(
        r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})"
        r"(?:[_T-](\d{2})(\d{2})(\d{2})?\d{0,3})?(?!\d)"),
)


def _as_text(value):
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("latin-1")
    if not isinstance(value, str):
        return None
    return value.replace("\x00", "").strip()


def _make_datetime(year, month, day, hour=0, minute=0, second=0):
    """A datetime from strings/ints, or None if invalid or outside YEAR_MIN..MAX."""
    try:
        parts = [int(part or 0) for part in (year, month, day, hour, minute, second)]
        if not YEAR_MIN <= parts[0] <= YEAR_MAX:
            return None
        return datetime.datetime(*parts)
    except (TypeError, ValueError, OverflowError):
        return None


def format_datetime(when):
    """'YYYY-MM-DDTHH:MM:SS' for a datetime (any year, unlike strftime on Windows)."""
    return "%04d-%02d-%02dT%02d:%02d:%02d" % (
        when.year, when.month, when.day, when.hour, when.minute, when.second)


def normalize_offset(value):
    """'+0200', '+02:00', 'Z' -> '+02:00' / '+00:00'; anything else -> ''."""
    text = _as_text(value)
    if not text:
        return ""
    if text.upper() == "Z":
        return "+00:00"
    match = _OFFSET_RE.match(text)
    if not match or int(match.group(2)) > 14 or int(match.group(3)) > 59:
        return ""
    return "%s%s:%s" % match.groups()


def _parse_exif_datetime(value):
    """Return (datetime, inline_offset) for an EXIF date string, or (None, '')."""
    text = _as_text(value)
    match = _EXIF_DATE_RE.match(text) if text else None
    if not match:
        return None, ""
    year, month, day, hour, minute, second, offset = match.groups()
    return _make_datetime(year, month, day, hour, minute, second), normalize_offset(offset)


def _parse_filename_datetime(filename):
    """Capture time encoded in a file name (phone and app conventions), or None."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    for pattern in _NAME_PATTERNS:
        for match in pattern.finditer(stem):
            groups = match.groups()
            year, month, day, hour, minute, second = groups[:6]
            ampm = groups[6].upper() if len(groups) > 6 and groups[6] else ""
            if ampm and hour is not None:
                hour = int(hour)
                if not 1 <= hour <= 12:
                    continue
                hour = hour % 12 + (12 if ampm == "P" else 0)
            when = _make_datetime(year, month, day, hour, minute, second)
            if when is not None:
                return when
    return None


def local_datetime(seconds):
    """Local time for a POSIX timestamp (a file time), or None.

    Windows cannot convert times before 1970 to local time; those fall back
    to UTC. Years outside YEAR_MIN..YEAR_MAX give None.
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds):
        return None
    try:
        when = datetime.datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        try:
            when = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=seconds)
        except (OverflowError, ValueError):
            return None
    return when if YEAR_MIN <= when.year <= YEAR_MAX else None


def local_iso(seconds):
    """'YYYY-MM-DDTHH:MM:SS' local time for a POSIX timestamp, or '' (see local_datetime)."""
    when = local_datetime(seconds)
    return format_datetime(when) if when is not None else ""


def parse_capture_date(exif_dict, filename, mtime):
    """Work out when a photo was taken. Returns (taken_at, source, tz_offset).

    taken_at is 'YYYY-MM-DDTHH:MM:SS' local capture time and source is
    'exif', 'filename' or 'mtime'; ('', '', '') when nothing usable is found.
    Order: DateTimeOriginal (+OffsetTimeOriginal), DateTimeDigitized,
    DateTime, then file name patterns, then mtime (local time). Years outside
    1900..2100 and impossible dates are rejected. tz_offset ('+02:00') only
    comes from EXIF.
    """
    exif = exif_dict if isinstance(exif_dict, dict) else {}
    for date_tag, offset_tag in _DATE_TAGS:
        when, inline_offset = _parse_exif_datetime(exif.get(date_tag))
        if when is not None:
            offset = normalize_offset(exif.get(offset_tag)) or inline_offset
            return format_datetime(when), "exif", offset
    when = _parse_filename_datetime(filename)
    if when is not None:
        return format_datetime(when), "filename", ""
    when = local_datetime(mtime)
    if when is not None:
        return format_datetime(when), "mtime", ""
    return "", "", ""


# ---------------------------------------------------------------------------
# GPS position and camera

# GPS tag ids, for dicts straight from Image.Exif.get_ifd(0x8825).
_GPS_TAG_IDS = {"GPSLatitudeRef": 1, "GPSLatitude": 2,
                "GPSLongitudeRef": 3, "GPSLongitude": 4}


def _gps_value(gps, name):
    if name in gps:
        return gps[name]
    return gps.get(_GPS_TAG_IDS[name])


def _to_number(value):
    """A finite float from an int/float/rational/(num, den) pair, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return None
        numerator, denominator = _to_number(value[0]), _to_number(value[1])
        if numerator is None or not denominator:
            return None
        value = numerator / denominator
    if _is_bad_rational(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


def _gps_ref(value):
    text = _as_text(value)
    return text[:1].upper() if text else ""


def _gps_coordinate(value, ref, positive, negative, limit):
    """Decimal degrees from EXIF degrees/minutes/seconds and a ref, or None."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if not 1 <= len(value) <= 3:
            return None
        parts = [_to_number(part) for part in value]
        if parts[0] is None:
            return None
        minutes = parts[1] if len(parts) > 1 and parts[1] is not None else 0.0
        seconds = parts[2] if len(parts) > 2 and parts[2] is not None else 0.0
        if minutes < 0 or seconds < 0:
            return None
        degrees = abs(parts[0]) + minutes / 60.0 + seconds / 3600.0
        number = -degrees if parts[0] < 0 else degrees
    else:
        number = _to_number(value)
        if number is None:
            return None
    ref = _gps_ref(ref)
    if ref == negative:
        number = -abs(number)
    elif ref == positive:
        number = abs(number)
    if abs(number) > limit:
        return None
    return number


def exif_gps_to_decimal(gps_dict):
    """Return (lat, lon) in decimal degrees from an EXIF GPS dict, or (None, None).

    Accepts the cleaned dict stored in exif["GPS"] (lists of numbers) as well
    as raw Pillow values (IFDRational, (numerator, denominator) pairs, tag-id
    keys). S and W refs make the value negative. Out-of-range values,
    unreadable values and the 0,0 "no fix" position are rejected.
    """
    if not isinstance(gps_dict, dict):
        return None, None
    lat = _gps_coordinate(_gps_value(gps_dict, "GPSLatitude"),
                          _gps_value(gps_dict, "GPSLatitudeRef"), "N", "S", 90.0)
    lon = _gps_coordinate(_gps_value(gps_dict, "GPSLongitude"),
                          _gps_value(gps_dict, "GPSLongitudeRef"), "E", "W", 180.0)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None, None
    return round(lat, 7), round(lon, 7)


# Trailing words dropped from a camera maker ("NIKON CORPORATION" -> "NIKON").
_MAKER_NOISE = {
    "co", "co.", "corp", "corp.", "corporation", "company", "inc", "inc.",
    "ltd", "ltd.", "limited", "imaging", "optical", "ag", "gmbh",
}


def camera_name(exif_dict):
    """'Make Model' without repeating the maker, or '' if unknown.

    'Apple' + 'iPhone 12' -> 'Apple iPhone 12'; 'Canon' + 'Canon EOS R5' ->
    'Canon EOS R5'; 'NIKON CORPORATION' + 'NIKON D750' -> 'NIKON D750'.
    """
    exif = exif_dict if isinstance(exif_dict, dict) else {}
    make = " ".join((_as_text(exif.get("Make")) or "").split())
    model = " ".join((_as_text(exif.get("Model")) or "").split())
    words = make.replace(",", " ").split()
    while len(words) > 1 and words[-1].lower() in _MAKER_NOISE:
        words.pop()
    make = " ".join(words)
    if not make or not model:
        return (make or model)[:100]
    lowered = model.lower()
    for word in words:
        word = word.lower()
        # The model already names the maker ("Canon EOS", "KODAK EASYSHARE").
        if lowered.startswith(word) and (len(lowered) == len(word) or
                                         not lowered[len(word)].isalnum()):
            return model[:100]
    return ("%s %s" % (make, model))[:100]


def exif_metadata(exif_dict, filename="", mtime=None):
    """The indexed fields derived from a cleaned EXIF dict.

    Returns {taken_at, taken_source, tz_offset, camera, lat, lon}; see
    parse_capture_date(), camera_name() and exif_gps_to_decimal().
    """
    exif = exif_dict if isinstance(exif_dict, dict) else {}
    taken_at, source, offset = parse_capture_date(exif, filename, mtime)
    lat, lon = exif_gps_to_decimal(exif.get("GPS"))
    return {
        "taken_at": taken_at, "taken_source": source, "tz_offset": offset,
        "camera": camera_name(exif), "lat": lat, "lon": lon,
    }


# ---------------------------------------------------------------------------
# 360-degree photos

# Values of the images.projection column. "" is an ordinary photo and "?"
# marks a row stored before projections were detected that may be a 360
# photo (the scanner reads it again to find out).
PROJECTION_EQUIRECT = "equirectangular"     # stitched 360 x 180 degree panorama
PROJECTION_DUAL_FISHEYE = "dual_fisheye"    # two unstitched fisheye circles
PROJECTIONS_360 = (PROJECTION_EQUIRECT, PROJECTION_DUAL_FISHEYE)
PROJECTION_UNKNOWN = "?"

# A full panorama is twice as wide as it is high, give or take this much.
PANORAMA_ASPECT_TOLERANCE = 0.02

_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_GPANO_PROJECTION_RE = re.compile(
    rb"GPano:ProjectionType\s*(?:=\s*[\"']([^\"']*)[\"']|>\s*([^<\s]*)\s*<)")
_GPANO_NUMBER_RE = rb"GPano:%s\s*(?:=\s*[\"']\s*(\d+)\s*[\"']|>\s*(\d+)\s*<)"


def is_panorama_aspect(width, height):
    """True for a 2:1 frame (within PANORAMA_ASPECT_TOLERANCE)."""
    try:
        width, height = float(width), float(height)
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    return abs(width / (2.0 * height) - 1.0) <= PANORAMA_ASPECT_TOLERANCE + 1e-9


def jpeg_xmp(data):
    """The XMP packet of JPEG bytes (from its APP1 segment), or b"".

    Only the segments before the image data are looked at, so this is quick
    even for large files. Anything that is not a JPEG gives b"".
    """
    if bytes(data[:3]) != b"\xff\xd8\xff":
        return b""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return b""
        marker = data[pos + 1]
        if marker == 0xFF:              # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xDA, 0xD9):      # start of scan / end of image
            return b""
        length = int.from_bytes(bytes(data[pos + 2:pos + 4]), "big")
        if length < 2:
            return b""
        segment = bytes(data[pos + 4:pos + 2 + length])
        if marker == 0xE1 and segment.startswith(_XMP_HEADER):
            return segment[len(_XMP_HEADER):]
        pos += 2 + length
    return b""


def _xmp_text(xmp):
    if isinstance(xmp, str):
        return xmp.encode("utf-8", "replace")
    return bytes(xmp or b"")


def xmp_projection(xmp):
    """The GPano:ProjectionType of an XMP packet, lowercased ("" if absent).

    Both the attribute form (GPano:ProjectionType="equirectangular") and the
    element form (<GPano:ProjectionType>equirectangular</...>) are found.
    """
    match = _GPANO_PROJECTION_RE.search(_xmp_text(xmp))
    if not match:
        return ""
    value = match.group(1) if match.group(1) is not None else match.group(2)
    return value.decode("latin-1").strip().lower()


def _xmp_number(text, name):
    match = re.search(_GPANO_NUMBER_RE % name.encode("ascii"), text)
    if not match:
        return None
    return int(match.group(1) if match.group(1) is not None else match.group(2))


def xmp_covers_sphere(xmp):
    """False when GPano says the image is only part of a panorama.

    Phones write GPano for wide but partial panoramas too: the cropped area
    is then smaller than the full panorama. Without those fields the image
    is taken to be complete.
    """
    text = _xmp_text(xmp)
    for cropped, full in (("CroppedAreaImageWidthPixels", "FullPanoWidthPixels"),
                          ("CroppedAreaImageHeightPixels", "FullPanoHeightPixels")):
        part, whole = _xmp_number(text, cropped), _xmp_number(text, full)
        if part and whole and part < whole * (1.0 - PANORAMA_ASPECT_TOLERANCE):
            return False
    return True


def _exif_text(exif, key):
    return " ".join((_as_text(exif.get(key)) or "").split()).lower()


def is_insta360(exif_dict):
    """True for photos from an Insta360 camera (maker Insta360 / Arashi Vision)."""
    exif = exif_dict if isinstance(exif_dict, dict) else {}
    make = _exif_text(exif, "Make")
    return "insta360" in make or "arashi vision" in make


# Insta360 cameras write the file's own name into EXIF ImageDescription,
# cut short on some models ("DCIM\Camera01\IMG_..._00_005.jp"): .insp for an
# unstitched dual-fisheye photo, .jpg for one the camera stitched itself.
# Copies exported from the camera's app keep the name (and add GPano XMP).
_INSTA360_FILE_TYPES = (
    ("insp", (".insp", ".ins")),
    ("jpg", (".jpg", ".jpeg", ".jp")),
)


def insta360_file_type(exif_dict):
    """The type of file an Insta360 camera saved a photo as, from its EXIF.

    "insp" for an unstitched original, "jpg" for a photo stitched in the
    camera, "" when the EXIF does not say (or the photo is not from an
    Insta360 camera). Unlike the file's extension, this survives renaming.
    """
    exif = exif_dict if isinstance(exif_dict, dict) else {}
    if not is_insta360(exif):
        return ""
    name = _exif_text(exif, "ImageDescription")
    for file_type, endings in _INSTA360_FILE_TYPES:
        if name.endswith(endings):
            return file_type
    return ""


def is_360_camera(exif_dict):
    """True for photos from a known 360-degree camera (by EXIF Make and Model)."""
    exif = exif_dict if isinstance(exif_dict, dict) else {}
    if is_insta360(exif):
        return True
    make, model = _exif_text(exif, "Make"), _exif_text(exif, "Model")
    return (("ricoh" in make and "theta" in model)
            or ("gopro" in make and "max" in model)
            or ("samsung" in make and "gear 360" in model))
