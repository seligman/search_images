#!/usr/bin/env python3
"""Image helpers built on Pillow (plus pillow-heif for HEIC/HEIF files).

This module is the only place that depends on Pillow, so scripts that do not
touch image pixels do not need it installed. pillow-heif is optional and is
only imported the first time a HEIC/HEIF file is met.

The main entry point is read_image(): it reads an original file once and
returns everything the index needs (displayed size, thumbnail, preview,
cleaned EXIF, dHash, content hash, capture date, camera and GPS position).
prepare_for_llm() shrinks an image for a vision model, and to_browser_jpeg()
converts originals a browser cannot show (HEIC, TIFF, ...) to JPEG.

The EXIF helpers (sanitize_exif, parse_capture_date, exif_gps_to_decimal,
camera_name, exif_metadata) and sniff_mime() live in imageinfo.py, which needs
no Pillow; they are re-exported here for convenience.
"""

import contextlib
import hashlib
import io
import math
import os
import threading

try:
    from PIL import Image
    from PIL.ExifTags import GPSTAGS, TAGS
except ImportError:
    raise SystemExit(
        "This tool needs Pillow. Install it with:\n"
        "    python -m pip install Pillow")

import imageinfo
from imageinfo import (
    AVIF_BRANDS, HEIF_BRANDS, exif_metadata, parse_capture_date, sanitize_exif, sniff_mime,
)

# The rest of imageinfo's helpers, available through this module as before.
BROWSER_MIME_TYPES = imageinfo.BROWSER_MIME_TYPES
MAX_TEXT = imageinfo.MAX_TEXT
browser_can_display = imageinfo.browser_can_display
camera_name = imageinfo.camera_name
clean_exif_value = imageinfo.clean_exif_value
exif_gps_to_decimal = imageinfo.exif_gps_to_decimal
normalize_offset = imageinfo.normalize_offset

THUMB_SIZE = 320          # thumbnails fit in a THUMB_SIZE x THUMB_SIZE box
THUMB_QUALITY = 85
PREVIEW_EDGE = 2048       # long edge of the cached preview
PREVIEW_QUALITY = 85
BROWSER_QUALITY = 90      # full-size JPEGs made for formats browsers cannot show
# At most this many pixels (summed over all threads) are decoded at full size
# at the same time. Decoding takes several bytes per pixel, so this bounds the
# memory used by parallel readers (large panoramas, HEIC photos) at roughly
# 1 GB whatever the number of threads. One image larger than this is still
# decoded, on its own.
DECODE_PIXEL_BUDGET = 100000000

# EXIF tags worth surfacing on the detail page (GPS is stored separately).
EXIF_FIELDS = (
    "Make", "Model", "LensMake", "LensModel",
    "DateTimeOriginal", "OffsetTimeOriginal",
    "DateTimeDigitized", "OffsetTimeDigitized",
    "DateTime", "OffsetTime",
    "ExposureTime", "FNumber", "ISOSpeedRatings", "PhotographicSensitivity",
    "FocalLength", "FocalLengthIn35mmFilm", "ExposureBiasValue", "Flash",
    "Orientation", "Software", "Artist", "Copyright", "ImageDescription",
)

HEIF_MISSING = ("HEIC/HEIF images need pillow-heif; install it with: "
                "python -m pip install pillow-heif")
AVIF_MISSING = ("AVIF images need a Pillow with AVIF support; upgrade it with: "
                "python -m pip install --upgrade Pillow")

_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
_TRANSPOSE = getattr(Image, "Transpose", Image)

# EXIF Orientation value -> the transpose that shows the image upright.
_ORIENTATION_METHODS = {
    2: _TRANSPOSE.FLIP_LEFT_RIGHT,
    3: _TRANSPOSE.ROTATE_180,
    4: _TRANSPOSE.FLIP_TOP_BOTTOM,
    5: _TRANSPOSE.TRANSPOSE,
    6: _TRANSPOSE.ROTATE_270,
    7: _TRANSPOSE.TRANSVERSE,
    8: _TRANSPOSE.ROTATE_90,
}
# Transposes that swap width and height.
_SWAPPING_METHODS = (_TRANSPOSE.TRANSPOSE, _TRANSPOSE.ROTATE_270,
                     _TRANSPOSE.TRANSVERSE, _TRANSPOSE.ROTATE_90)

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_ORIENTATION_TAG = 0x0112

# Formats whose originals may be smaller as PNG than as a JPEG preview
# (screenshots, drawings); see _encode_preview().
_LOSSLESS_MIME_TYPES = ("image/png", "image/gif", "image/webp", "image/bmp", "image/tiff")


class ImageReadError(Exception):
    """An image could not be read: unreadable, unsupported, corrupt or too big."""


class SourceReadError(ImageReadError):
    """The original's bytes could not be read at all (an I/O error).

    A subclass of ImageReadError, so callers that do not care can catch
    ImageReadError alone; a scanner may treat this one as possibly transient
    (for example a network share that went away).
    """


# ---------------------------------------------------------------------------
# HEIF support

# pillow-heif is only needed when a library actually contains HEIC images,
# so it is imported on demand rather than at startup.
_heif_lock = threading.Lock()
_heif_state = None        # None = not tried yet, True = registered, False = missing
_heif_warned = False


def ensure_heif(warn=True):
    """Register the HEIC/HEIF opener on first use; return True if available.

    Thread-safe; the opener is registered at most once per process. With
    warn=True a one-time install hint is printed when pillow-heif is missing.
    """
    global _heif_state, _heif_warned
    if _heif_state is None:
        with _heif_lock:
            if _heif_state is None:
                try:
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                    _heif_state = True
                except Exception:   # ImportError, or a broken native library
                    _heif_state = False
    if not _heif_state and warn and not _heif_warned:
        _heif_warned = True
        print("Warning: " + HEIF_MISSING)
    return _heif_state


def _image_mime(image, data):
    """MIME type from the format Pillow detected (never from the extension)."""
    if image.format == "MPO":   # multi-picture JPEGs (many phone photos)
        return "image/jpeg"
    try:
        mime = image.get_format_mimetype()
    except Exception:
        mime = None
    return mime or sniff_mime(data) or "application/octet-stream"


# ---------------------------------------------------------------------------
# Reading and decoding

def _error_text(error):
    """Short text for an exception, naming its type unless that is obvious."""
    text = str(error)
    if not text:
        text = type(error).__name__
    elif not isinstance(error, (OSError, ValueError)):
        text = "%s: %s" % (type(error).__name__, text)
    return text[:300]


def read_source_bytes(path, helper=None):
    """Return the original bytes of an image file.

    The helper module may supply load_image(path) returning the bytes, or
    None to fall back to reading the file. I/O errors raise SourceReadError.
    """
    loader = getattr(helper, "load_image", None) if helper is not None else None
    try:
        if loader is not None:
            data = loader(path)
            if data is not None:
                return data if isinstance(data, bytes) else bytes(data)
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise SourceReadError("cannot read file: %s" % _error_text(error)) from error


def _source_mtime(path, helper):
    """Best-effort modification time of a source file, or None."""
    stat = getattr(helper, "stat", None) if helper is not None else None
    try:
        result = stat(path) if stat is not None else None
        if result is None:
            result = os.stat(path)
        return result.st_mtime
    except Exception:
        return None


def _open_image(data):
    """Open image bytes with Pillow, registering HEIF support when needed."""
    if not data:
        raise ImageReadError("file is empty")
    major, _ = imageinfo.ftyp_brands(data)
    heif = major in HEIF_BRANDS
    if heif:
        ensure_heif(warn=False)
    try:
        return Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as error:
        raise ImageReadError("image is too large to decode safely (%s)"
                             % _error_text(error)) from error
    except Exception as error:
        if heif and not _heif_state:
            raise ImageReadError(HEIF_MISSING) from error
        if major in AVIF_BRANDS:
            raise ImageReadError(AVIF_MISSING) from error
        if isinstance(error, Image.UnidentifiedImageError):
            # Pillow's message names a BytesIO object; keep only its hints
            # such as "AVIF support not installed".
            text = str(error)
            hint = text.split(" because ", 1)[1] if " because " in text else ""
            raise ImageReadError("not a supported image format" +
                                 (" (%s)" % hint if hint else "")) from error
        raise ImageReadError("not a supported image (%s)"
                             % _error_text(error)) from error


def _orientation_method(image):
    """The transpose that makes this image upright, or None."""
    try:
        value = image.getexif().get(_ORIENTATION_TAG)
        return _ORIENTATION_METHODS.get(int(value))
    except Exception:
        return None


def _fit_size(size, edge):
    """Size that fits in an edge x edge box, keeping the aspect; never upscales."""
    width, height = size
    if width <= edge and height <= edge:
        return (width, height)
    if width >= height:
        return (edge, max(1, int(round(height * float(edge) / width))))
    return (max(1, int(round(width * float(edge) / height))), edge)


def _to_8bit_gray(image):
    """Scale a 16/32-bit integer or float grayscale image down to mode L."""
    if image.mode not in ("I", "F"):
        image = image.convert("I")
    low, high = image.getextrema()
    if image.mode == "I" and low >= 0 and high <= 255:
        scale, offset = 1.0, 0.0
    elif image.mode == "I" and low >= 0 and high <= 65535:
        scale, offset = 255.0 / 65535, 0.0      # ordinary 16-bit data
    elif image.mode == "F" and low >= 0 and high <= 1.0:
        scale, offset = 255.0, 0.0              # normalised floats
    elif high > low:
        scale = 255.0 / (high - low)
        offset = -low * scale
    else:
        scale, offset = 1.0, 0.0
    if scale != 1.0 or offset:
        image = image.point(lambda value: value * scale + offset)
    return image.convert("L")


def to_rgb(image, background=(255, 255, 255)):
    """Return an 8-bit RGB version of image, ready for JPEG encoding.

    16-bit and 32-bit grayscale is scaled (not clipped) to 8 bits, any
    transparency (alpha channel, palette or tRNS) is composited onto a white
    background, and CMYK/YCbCr/LAB/... are converted. RGB images without
    transparency are returned unchanged.
    """
    mode = image.mode
    if mode == "RGB" and "transparency" not in image.info:
        return image
    if mode.startswith("I") or mode == "F":
        return _to_8bit_gray(image).convert("RGB")
    if mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, background)
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def _is_wide_gamut_profile(icc):
    """True for an RGB ICC profile worth keeping (not sRGB, not huge)."""
    if not icc or len(icc) > 65536 or icc[16:20] != b"RGB ":
        return False
    return b"sRGB" not in icc and "sRGB".encode("utf-16-be") not in icc


def _encode_jpeg(image, quality, icc=None):
    """JPEG bytes for an RGB image. No EXIF is written.

    A non-sRGB RGB colour profile (Display P3, Adobe RGB, ...) is kept so
    browsers show the colours correctly; sRGB is the default anyway.
    """
    options = {"quality": quality}
    if image.mode == "RGB" and _is_wide_gamut_profile(icc):
        options["icc_profile"] = icc
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", **options)
    return buffer.getvalue()


class _PixelBudget:
    """Limits how many pixels are decoded at full size at the same time.

    hold(pixels) waits until the pixels fit in DECODE_PIXEL_BUDGET next to
    what other threads hold (an image larger than the whole budget waits
    until nothing else is held).
    """

    def __init__(self):
        self._used = 0
        self._condition = threading.Condition()

    @contextlib.contextmanager
    def hold(self, pixels):
        total = max(1, int(DECODE_PIXEL_BUDGET))
        pixels = max(1, min(int(pixels), total))
        with self._condition:
            while self._used and self._used + pixels > total:
                self._condition.wait()
            self._used += pixels
        try:
            yield
        finally:
            with self._condition:
                self._used -= pixels
                self._condition.notify_all()


_decode_budget = _PixelBudget()


@contextlib.contextmanager
def _decoded(image, size_for):
    """Decode an opened image upright and flattened to RGB at a wanted size.

    size_for(width, height) gets the displayed (oriented) size of the
    original and returns the output size. Large JPEGs are decoded at 1/2,
    1/4 or 1/8 scale (Image.draft) when the output is much smaller. Yields
    (rgb_image, (displayed_width, displayed_height), icc_profile) while the
    decoded pixels count against DECODE_PIXEL_BUDGET; use it in a with
    statement inside the one that closes the image.
    """
    method = _orientation_method(image)
    raw_size = image.size
    swap = method in _SWAPPING_METHODS
    displayed = (raw_size[1], raw_size[0]) if swap else raw_size
    target = size_for(displayed[0], displayed[1])
    raw_target = (target[1], target[0]) if swap else target
    if (image.format in ("JPEG", "MPO") and raw_target[0] * 2 <= raw_size[0]
            and raw_target[1] * 2 <= raw_size[1]):
        try:
            image.draft(None, raw_target)
        except Exception:
            pass
    # After draft() the size is what will actually be decoded.
    with _decode_budget.hold(image.size[0] * image.size[1]):
        image.load()
        icc = image.info.get("icc_profile")
        frame = image.transpose(method) if method is not None else image
        rgb = to_rgb(frame)
        if rgb.size != target:
            rgb = rgb.resize(target, _LANCZOS, reducing_gap=2.0)
        yield rgb, displayed, icc


def _render_jpeg(data, quality, size_for):
    """Decode image bytes, orient, flatten, resize and re-encode as JPEG."""
    image = _open_image(data)
    try:
        with image:
            with _decoded(image, size_for) as (rgb, _, icc):
                return _encode_jpeg(rgb, quality, icc)
    except ImageReadError:
        raise
    except Image.DecompressionBombError as error:
        raise ImageReadError("image is too large to decode safely (%s)"
                             % _error_text(error)) from error
    except Exception as error:
        raise ImageReadError("cannot decode image (%s)"
                             % _error_text(error)) from error


def _encode_preview(image, icc, mime, original_size):
    """Preview bytes: JPEG, or PNG when that is smaller for a lossless original.

    Screenshots and drawings saved as PNG can be several times larger as a
    JPEG than the original file. For such originals (only when the JPEG
    would be larger than the original) a PNG is made too and the smaller of
    the two is kept. Readers tell them apart with sniff_mime().
    """
    jpeg = _encode_jpeg(image, PREVIEW_QUALITY, icc)
    if mime not in _LOSSLESS_MIME_TYPES or len(jpeg) <= original_size:
        return jpeg
    options = {}
    if image.mode == "RGB" and _is_wide_gamut_profile(icc):
        options["icc_profile"] = icc
    buffer = io.BytesIO()
    image.save(buffer, "PNG", **options)
    png = buffer.getvalue()
    return png if len(png) < len(jpeg) else jpeg


def compute_dhash(image):
    """Difference hash: resize to 9x8 grayscale, compare horizontal neighbors.

    Returns a 16-character hex string (64 bits), row by row, a bit being 1
    when a pixel is brighter than its right neighbor. Robust to small changes
    in brightness, contrast and compression; good for spotting
    near-duplicates. Images with transparency or more than 8 bits per sample
    are flattened with to_rgb() first.
    """
    if image.mode not in ("1", "L", "RGB"):
        image = to_rgb(image)
    pixels = image.convert("L").resize((9, 8), _LANCZOS).tobytes()
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return "%016x" % bits


def read_image(path, helper=None, preview_edge=PREVIEW_EDGE, want_preview=True,
               mtime=None):
    """Read an original once and return everything the index stores about it.

    The bytes come from helper.load_image(path) when the helper provides
    them, else from the file. Returns a dict with:
        width, height   displayed size (after EXIF orientation)
        thumb           JPEG bytes fitting THUMB_SIZE x THUMB_SIZE
        preview         JPEG bytes, long edge <= preview_edge, or None when
                        want_preview is False. For PNG/GIF/WebP/BMP/TIFF
                        originals it is a PNG when that is smaller (see
                        _encode_preview); check with sniff_mime().
        exif            cleaned EXIF dict (json.dumps(allow_nan=False) safe)
        dhash           16 hex chars (see compute_dhash)
        content_hash    sha1 hex of the original bytes
        mime            sniffed from the content, not the extension
        taken_at, taken_source, tz_offset, camera, lat, lon
                        see exif_metadata()
    mtime (the file's modification time) is only used when neither EXIF nor
    the file name gives a capture date; when omitted it is looked up.
    Raises ImageReadError (SourceReadError for I/O errors) when the file
    cannot be read or decoded, is not a supported image, or is too large.
    """
    data = read_source_bytes(path, helper)
    content_hash = hashlib.sha1(data).hexdigest()
    image = _open_image(data)
    working_edge = max(int(preview_edge), THUMB_SIZE)
    try:
        with image:
            mime = _image_mime(image, data)
            exif = extract_exif(image)
            with _decoded(image, lambda w, h: _fit_size((w, h), working_edge)) as (
                    working, (width, height), icc):
                dhash = compute_dhash(working)
                thumb_image = working
                thumb_size = _fit_size((width, height), THUMB_SIZE)
                if thumb_image.size != thumb_size:
                    thumb_image = working.resize(thumb_size, _LANCZOS, reducing_gap=2.0)
                thumb = _encode_jpeg(thumb_image, THUMB_QUALITY, icc)
                preview = None
                if want_preview:
                    preview_image = working
                    preview_size = _fit_size((width, height), int(preview_edge))
                    if preview_image.size != preview_size:
                        preview_image = working.resize(preview_size, _LANCZOS,
                                                       reducing_gap=2.0)
                    preview = _encode_preview(preview_image, icc, mime, len(data))
    except ImageReadError:
        raise
    except Image.DecompressionBombError as error:
        raise ImageReadError("image is too large to decode safely (%s)"
                             % _error_text(error)) from error
    except Exception as error:
        raise ImageReadError("cannot decode image (%s)"
                             % _error_text(error)) from error

    name = os.path.basename(path)
    meta = exif_metadata(exif, name, mtime)
    if not meta["taken_source"] and mtime is None:
        meta.update(zip(("taken_at", "taken_source", "tz_offset"),
                        parse_capture_date(exif, name, _source_mtime(path, helper))))
    result = {
        "width": width, "height": height, "thumb": thumb, "preview": preview,
        "exif": exif, "dhash": dhash, "content_hash": content_hash,
        "mime": mime,
    }
    result.update(meta)
    return result


def llm_size(width, height, max_edge=1536, min_short=768, max_pixels=3200000):
    """Output size used by prepare_for_llm() for a width x height image.

    The long edge is brought down to max_edge, unless that would make the
    short side smaller than min_short (tall screenshots, panoramas); then the
    short side is kept at min_short instead. The result never exceeds
    max_pixels and is never larger than the original.
    """
    if width <= 0 or height <= 0:
        return (width, height)
    scale = min(1.0, float(max_edge) / max(width, height))
    if min(width, height) * scale < min_short:
        # Keep text in tall screenshots legible.
        scale = min(1.0, float(min_short) / min(width, height))
    if width * height * scale * scale > max_pixels:
        scale = math.sqrt(float(max_pixels) / (width * height))
        return (max(1, int(width * scale)), max(1, int(height * scale)))
    return (max(1, int(round(width * scale))), max(1, int(round(height * scale))))


def prepare_for_llm(jpeg_bytes, max_edge=1536, min_short=768, max_pixels=3200000,
                    quality=88):
    """Return JPEG bytes sized for a vision model (see llm_size()).

    Accepts the bytes of a preview or of any original Pillow can read (HEIC
    too). The result is upright, flattened to RGB, never upscaled, and
    carries no EXIF. Raises ImageReadError if the bytes cannot be decoded.
    """
    return _render_jpeg(
        jpeg_bytes, quality,
        lambda w, h: llm_size(w, h, max_edge, min_short, max_pixels))


def to_browser_jpeg(path, helper=None, quality=BROWSER_QUALITY):
    """Full-size, upright JPEG bytes for an original browsers cannot show.

    Used for HEIC/HEIF/AVIF/TIFF originals. No EXIF is copied.
    Raises ImageReadError if the file cannot be read or decoded.
    """
    return _render_jpeg(read_source_bytes(path, helper), quality,
                        lambda w, h: (w, h))


# ---------------------------------------------------------------------------
# EXIF

def extract_exif(image):
    """Return a JSON-safe dict of selected EXIF fields plus "GPS", or {}.

    Reads the main IFD and the Exif sub-IFD for EXIF_FIELDS, and every tag of
    the GPS IFD (named as in PIL.ExifTags.GPSTAGS). Call it before rotating
    the image. Damaged EXIF never raises; whatever was readable is returned.
    """
    try:
        exif = image.getexif()
    except Exception:
        return {}
    if not exif:
        return {}
    values = {}
    try:
        for tag_id, value in exif.items():
            values[TAGS.get(tag_id, str(tag_id))] = value
    except Exception:
        pass
    try:
        for tag_id, value in exif.get_ifd(_EXIF_IFD).items():
            values[TAGS.get(tag_id, str(tag_id))] = value
    except Exception:
        pass
    result = sanitize_exif({name: values[name] for name in EXIF_FIELDS if name in values})
    try:
        gps = exif.get_ifd(_GPS_IFD)
        coords = sanitize_exif(dict(
            (GPSTAGS.get(tag_id, str(tag_id)), value) for tag_id, value in gps.items()))
        if coords:
            result["GPS"] = coords
    except Exception:
        pass
    return result
