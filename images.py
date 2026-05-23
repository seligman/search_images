#!/usr/bin/env python3
"""Image helpers built on Pillow (and pillow-heif for HEIC files).

This module is the only place that depends on Pillow, so scripts that do not
touch image pixels do not need it installed.
"""

import io
import tempfile

import common

try:
    from PIL import Image, ImageOps
    from PIL.ExifTags import GPSTAGS, TAGS
except ImportError:
    raise SystemExit(
        "This tool needs Pillow. Install it with:\n"
        "    python -m pip install Pillow")

# EXIF tags worth surfacing on the detail page.
EXIF_FIELDS = (
    "Make", "Model", "LensModel", "DateTimeOriginal", "DateTime",
    "ExposureTime", "FNumber", "ISOSpeedRatings", "FocalLength",
    "FocalLengthIn35mmFilm", "ExposureBiasValue", "Flash", "Orientation",
    "Software", "Artist", "Copyright", "ImageDescription",
)

# pillow-heif is only needed when a library actually contains HEIC images,
# so it is imported on demand rather than at startup.
_heif_state = None


def ensure_heif():
    """Register the HEIC opener on first use; return True if available."""
    global _heif_state
    if _heif_state is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _heif_state = True
        except ImportError:
            print("Warning: HEIC images were found but pillow-heif is not "
                  "installed; skipping them.")
            print("    Install it with: python -m pip install pillow-heif")
            _heif_state = False
    return _heif_state


def _clean_exif_value(value):
    """Convert an EXIF value into something JSON can store."""
    if isinstance(value, bytes):
        return value.decode("ascii", "ignore").replace("\x00", "").strip()
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    if isinstance(value, (tuple, list)):
        return [_clean_exif_value(item) for item in value]
    try:
        number = float(value)
        return int(number) if number.is_integer() else round(number, 6)
    except (TypeError, ValueError):
        return str(value)


def extract_exif(image):
    """Return a JSON-friendly dict of selected EXIF fields, or {} if none."""
    try:
        exif = image.getexif()
    except Exception:
        return {}
    if not exif:
        return {}
    values = {}
    for tag_id, value in exif.items():
        values[TAGS.get(tag_id, str(tag_id))] = value
    try:
        for tag_id, value in exif.get_ifd(0x8769).items():
            values[TAGS.get(tag_id, str(tag_id))] = value
    except Exception:
        pass
    result = {}
    for name in EXIF_FIELDS:
        if name in values:
            cleaned = _clean_exif_value(values[name])
            if cleaned not in (None, "", []):
                result[name] = cleaned
    try:
        gps = exif.get_ifd(0x8825)
        coords = {}
        for tag_id, value in gps.items():
            coords[GPSTAGS.get(tag_id, str(tag_id))] = _clean_exif_value(value)
        if coords:
            result["GPS"] = coords
    except Exception:
        pass
    return result


def compute_dhash(image):
    """Difference hash: resize to 9x8 grayscale, compare horizontal neighbors.

    Returns a 16-character hex string (64 bits). Robust to small changes in
    brightness, contrast, and compression; good for spotting near-duplicates.
    """
    small = image.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return "%016x" % bits


def read_image(path):
    """Return (width, height, jpeg_thumbnail_bytes, exif_dict, dhash) for a file."""
    with Image.open(path) as image:
        width, height = image.size
        exif = extract_exif(image)
        oriented = ImageOps.exif_transpose(image)
        dhash = compute_dhash(oriented)
        thumb = oriented.convert("RGB")
        thumb.thumbnail((common.THUMB_SIZE, common.THUMB_SIZE))
        buffer = io.BytesIO()
        thumb.save(buffer, format="JPEG", quality=85)
        return width, height, buffer.getvalue(), exif, dhash


def read_dhash(path):
    """Open a file and return only its dHash. Used when backfilling."""
    with Image.open(path) as image:
        return compute_dhash(ImageOps.exif_transpose(image))


def heic_to_jpeg_bytes(path):
    """Return JPEG bytes converted from a HEIC file."""
    with Image.open(path) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def heic_to_temp_jpeg(path):
    """Convert a HEIC file to a temporary JPEG file and return its path."""
    handle = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    handle.write(heic_to_jpeg_bytes(path))
    handle.close()
    return handle.name
