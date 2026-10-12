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

360-degree photos are recognised by detect_projection(). Unstitched
dual-fisheye frames (Insta360 .insp) are stitched into an equirectangular
panorama by dual_fisheye_to_equirect() (the two lenses evened out and
blended at the seams, a tripod under a soft fill at the bottom), and
front_view() cuts a natural looking picture from a panorama for the
thumbnail. read_image(want_pano=True) also makes a larger panorama of a
360 photo (PANO_EDGE wide) for the interactive 360 view.

The EXIF helpers (sanitize_exif, parse_capture_date, exif_gps_to_decimal,
camera_name, exif_metadata), sniff_mime() and the colour profile scrubbing
live in imageinfo.py, which needs no Pillow.
"""

import collections
import contextlib
import hashlib
import io
import itertools
import math
import os
import threading

try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    from PIL.ExifTags import GPSTAGS, TAGS
except ImportError:
    raise SystemExit(
        "This tool needs Pillow. Install it with:\n"
        "    python -m pip install Pillow")

import common
import imageinfo
from imageinfo import (
    AVIF_BRANDS, HEIF_BRANDS, exif_metadata, parse_capture_date, sanitize_exif, sniff_mime,
)

THUMB_SIZE = 320          # thumbnails fit in a THUMB_SIZE x THUMB_SIZE box
THUMB_QUALITY = 85
PREVIEW_EDGE = 2048       # long edge of the cached preview
PREVIEW_QUALITY = 85
# A 360-degree photo also gets a larger panorama ("pano") for the 360 view:
# a whole panorama stretched across the screen needs about twice the
# preview's pixels to look sharp. This wide, never upscaled, and only when
# that is larger than the preview (see has_pano). A trade-off rather than
# all the detail there is: an unstitched frame 6000 px wide holds about 15
# pixels per degree in the middle of each lens, against 11 here, but a
# wider pano takes longer to download on a phone, and some phones cannot
# show a picture wider than 4096 px in the 360 view.
PANO_EDGE = 4096
PANO_QUALITY = 85
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

# 360-degree photos (see detect_projection): the projection names stored in
# the index, and how they are shown.
PROJECTION_EQUIRECT = imageinfo.PROJECTION_EQUIRECT
PROJECTION_DUAL_FISHEYE = imageinfo.PROJECTION_DUAL_FISHEYE
PROJECTIONS_360 = imageinfo.PROJECTIONS_360
PROJECTION_UNKNOWN = imageinfo.PROJECTION_UNKNOWN   # not checked yet (older rows)
FRONT_VIEW_FOV = 100.0        # degrees across the thumbnail's front view
FRONT_VIEW_ASPECT = 4.0 / 3.0
MESH_CELL = 16                # pixels per cell of the Image.transform() meshes

# The signature of an unstitched dual-fisheye frame: in each half, the
# corners outside the lens circle are much darker than the circle itself.
# (Lens flare leaves real frames' corners at up to about 15; stitched
# panoramas usually have sky or ground there -- but a night panorama can be
# just as dark, so the signature alone never makes a frame dual-fisheye.)
DUAL_FISHEYE_CORNER_LEVEL = 20      # mean corner luminance (0-255) below this
DUAL_FISHEYE_CORNER_RATIO = 0.25    # ... and below this fraction of the interior
INSP_CORNER_RATIO = 0.5             # the looser test for .insp originals

# Lens model of Insta360 dual-fisheye frames (.insp): one equidistant fisheye
# circle in each half of a 2:1 frame (the distance from the circle's centre
# is proportional to the angle from the lens axis).
#   fov       degrees across a circle of radius R (see find_lens_circles();
#             R is half the frame height unless a dark border shows the circle)
#   front     the half whose lens looks forward: the middle of the panorama
#   rotation  degrees each half (left, right) is turned counter-clockwise
#             from upright: here "up" points to the outer edge of the frame
#   mirror    True if the halves are mirror images (they are not)
#   offsets   lens centres relative to the quarter points, in units of R,
#             (dx, dy) for the left and right half (default circles only)
#   blend     degrees on either side of the plane between the lenses over
#             which their pictures are mixed (0: hard seams); at most what
#             the lenses see beyond 180 degrees, fov / 2 - 90
# Derived empirically from sample frames of an Insta360 ONE R 360 lens
# (6080x3040, circles as tall as the frame): all orientations were rendered
# and the one with level horizons, an upright scene, readable (not mirrored)
# text and halves meeting at the seams was kept; with the forward lens on
# the left the panorama's centre shows the scene rather than the person
# holding the camera. Then fov and the centres were tuned to minimise the
# difference between the two lenses' pictures along the seams. The blend
# was chosen by looking at the seams of such frames: from 2 degrees on the
# line between the lenses is gone, and at 5 nearby things that parallax
# moves (a person beside the camera) show doubled over a wide band; 3 is
# a softer join with little doubling.
INSTA360_PARAMS = {
    "fov": 199.0,
    "front": "left",
    "rotation": (90, 270),
    "mirror": False,
    "offsets": ((-0.009, 0.0), (0.0, 0.0)),
    "blend": 3.0,
}
# Evening out the two lenses (see _lens_gains): only pixels with levels in
# this range in both lenses count (not nearly black, not clipped) ...
SEAM_GAIN_LEVELS = (16, 240)
# ... that differ by at most this much (not a nearby thing parallax moved) ...
SEAM_GAIN_DIFFERENCE = 48
# ... and there must be this many of them (per 2048 px wide panorama).
SEAM_GAIN_MIN_PIXELS = 200
# The most either lens is brightened or darkened, per channel.
SEAM_GAIN_LIMIT = 1.25
# A gain fades out towards white (see _gain_table), so clipped skies stay
# white and nothing new clips; the higher this power, the later it fades.
# (SEAM_GAIN_ROLLOFF * ln(SEAM_GAIN_LIMIT) must stay below 1, or brighter
# levels would come out darker.)
SEAM_GAIN_ROLLOFF = 4
# The soft fill over a pole (see _fill_pole): it fades in over this many
# degrees beyond the size asked for, the view it is made in shows this many
# more round it, and each blur size spreads the colours this many times.
POLE_FEATHER_DEG = 3.0
POLE_MARGIN_DEG = 3.0
POLE_FILL_PASSES = 2
# How many degrees around the bottom (nadir) and top (zenith) of a stitched
# dual-fisheye photo get that fill, unless config.json says otherwise
# ("nadir_fill_deg", "zenith_fill_deg"; 0 = none, at most POLE_FILL_MAX).
# None by default: on clean ground the fill only leaves a grey disc. A
# tripod's legs (or a hand holding the camera) reach about 5 degrees from
# the nadir, so 6 hides them; a hand-held camera's stick lies between the
# lenses and the stitch hides it already. Changing these changes
# stitch_key(), so the stored 360 photos are stitched again.
NADIR_FILL_DEG = 0.0
ZENITH_FILL_DEG = 0.0
POLE_FILL_MAX = 30.0
# Raise when dual_fisheye_to_equirect() makes different pictures: the scan's
# back-fill stage then stitches the 360 photos stored before again (their
# previews, panos, thumbnails and dHashes; never their descriptions). See
# stitch_key().
STITCH_VERSION = 2

HEIF_MISSING = ("HEIC/HEIF images need pillow-heif; install it with: "
                "python -m pip install pillow-heif")
AVIF_MISSING = ("AVIF images need a Pillow with AVIF support; upgrade it with: "
                "python -m pip install --upgrade Pillow")

_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
_BILINEAR = getattr(Image, "Resampling", Image).BILINEAR
_BOX = getattr(Image, "Resampling", Image).BOX
_NEAREST = getattr(Image, "Resampling", Image).NEAREST
_MESH = getattr(Image, "Transform", Image).MESH
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


def has_pano(width, height):
    """True if a 360-degree photo of this (displayed) size gets a pano.

    Only one larger than PREVIEW_EDGE does: for a smaller one the preview
    is already the whole picture.
    """
    return max(int(width or 0), int(height or 0)) > PREVIEW_EDGE


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


def _kept_profile(image, icc):
    """The colour profile to write with an RGB picture made from image, or None.

    A non-sRGB RGB colour profile (Display P3, Adobe RGB, ...) is kept so
    browsers show the colours correctly (sRGB is the default anyway), with
    its free text replaced (imageinfo.scrub_icc): a profile's description,
    copyright and device names can name a person or a computer, and these
    pictures go to the LLM, the browser and the static export.
    """
    if image.mode == "RGB" and _is_wide_gamut_profile(icc):
        return imageinfo.scrub_icc(icc)
    return None


def _encode_jpeg(image, quality, icc=None):
    """JPEG bytes for an RGB image. No EXIF is written; see _kept_profile for the profile."""
    options = {"quality": quality}
    profile = _kept_profile(image, icc)
    if profile:
        options["icc_profile"] = profile
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
def _decoded(image, size_for, resize=True):
    """Decode an opened image upright and flattened to RGB at a wanted size.

    size_for(width, height) gets the displayed (oriented) size of the
    original and returns the output size. Large JPEGs are decoded at 1/2,
    1/4 or 1/8 scale (Image.draft) when the output is much smaller. Yields
    (rgb_image, (displayed_width, displayed_height), icc_profile) while the
    decoded pixels count against DECODE_PIXEL_BUDGET; use it in a with
    statement inside the one that closes the image. With resize=False the
    image is yielded as decoded (at least the wanted size, or the original
    size) instead of being resized to exactly the output size.
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
        if resize and rgb.size != target:
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
    # (Always named: Pillow would otherwise copy the image's own profile, text and all.)
    options = {"icc_profile": _kept_profile(image, icc)}
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


# ---------------------------------------------------------------------------
# 360-degree photos
#
# Directions are (x, y, z) unit vectors: x to the right, y up, z forward.
# An equirectangular panorama maps longitude -180..180 degrees (0 = forward)
# to its columns, left to right, and latitude 90..-90 degrees to its rows.

def _cells(start, stop, cell=MESH_CELL):
    """Cell edges from start to stop (both included), at most cell apart."""
    edges = list(range(start, stop, max(1, cell)))
    return edges + [stop] if stop > start else [start]


def _mesh(columns, rows, point):
    """Image.transform() MESH data: each output cell maps to the source
    quadrilateral that point(x, y) gives for its corners."""
    grid = [[point(x, y) for y in rows] for x in columns]
    mesh = []
    for i in range(len(columns) - 1):
        left, right = grid[i], grid[i + 1]
        for j in range(len(rows) - 1):
            mesh.append(((columns[i], rows[j], columns[i + 1], rows[j + 1]),
                         left[j] + left[j + 1] + right[j + 1] + right[j]))
    return mesh


def _is_insp(filename):
    return os.path.splitext(filename or "")[1].lower() == ".insp"


# Pixel indexes of the corner regions and the interior of a 64x64 half of
# the 128x64 copy used by _corner_levels() (built on first use).
_corner_masks = None


def _corner_levels(image):
    """[(corner means, interior mean)] of the left and right halves of image.

    Measured on a 128x64 grayscale copy: each 64x64 half has a circle
    inscribed; the four corner regions lie well outside it, the interior well
    inside.
    """
    global _corner_masks
    if _corner_masks is None:
        masks = ([[] for _ in range(4)], [])
        for y in range(64):
            for x in range(64):
                dx, dy = x + 0.5 - 32, y + 0.5 - 32
                distance = math.hypot(dx, dy) / 32.0
                if distance > 1.1:
                    masks[0][(dx > 0) + 2 * (dy > 0)].append(y * 128 + x)
                elif distance < 0.8:
                    masks[1].append(y * 128 + x)
        _corner_masks = masks
    corners, interior = _corner_masks
    if image.mode not in ("L", "RGB"):
        image = to_rgb(image)
    pixels = image.resize((128, 64), _BOX).convert("L").tobytes()
    levels = []
    for offset in (0, 64):
        means = [sum(pixels[i + offset] for i in corner) / float(len(corner))
                 for corner in corners]
        levels.append((means, sum(pixels[i + offset] for i in interior) / float(len(interior))))
    return levels


def _fisheye_signature(image):
    """(strict, loose): how well image matches has_dual_fisheye_signature()."""
    strict = loose = True
    for corners, interior in _corner_levels(image):
        if interior < 8:            # a black frame tells nothing
            return False, False
        mean = sum(corners) / len(corners)
        strict = strict and (mean < DUAL_FISHEYE_CORNER_LEVEL
                             and mean < DUAL_FISHEYE_CORNER_RATIO * interior
                             and max(corners) < 2 * DUAL_FISHEYE_CORNER_RATIO * interior)
        loose = loose and max(corners) < INSP_CORNER_RATIO * interior
    return strict, strict or loose


def has_dual_fisheye_signature(image, loose=False):
    """True if image looks like two lens circles side by side on black.

    In each half the corners outside the circle must be much darker than the
    circle's interior: on average below DUAL_FISHEYE_CORNER_LEVEL and below
    DUAL_FISHEYE_CORNER_RATIO of the interior. A stitched panorama has sky
    or ground there instead. loose=True also accepts frames whose corners
    are lit by lens flare (over-exposed shots) as long as every corner stays
    below INSP_CORNER_RATIO of the interior; it is meant for .insp
    originals, which are unstitched unless proven otherwise.
    """
    strict, looser = _fisheye_signature(image)
    return looser if loose else strict


def _is_unstitched_original(exif, filename):
    """True for a frame known to be an unstitched original: a .insp file, or
    one whose Insta360 EXIF says the camera saved it as .insp."""
    return _is_insp(filename) or imageinfo.insta360_file_type(exif) == "insp"


def detect_projection(image, exif, xmp_bytes, filename=""):
    """What kind of 360-degree photo an image is.

    image is the decoded, upright picture (only its aspect and a small copy
    are looked at), exif the dict from extract_exif(), xmp_bytes the raw XMP
    packet (b"" if none), filename the file's name ("" if unknown). Returns:
        "dual_fisheye"     an unstitched frame of two fisheye circles: a .insp
                           file, or one whose Insta360 EXIF says the camera
                           saved it as .insp (see imageinfo.insta360_file_type),
                           2:1, with the dark corners of
                           has_dual_fisheye_signature(loose=True). Dark corners
                           alone are not enough: night panoramas have them too.
        "equirectangular"  a stitched 2:1 panorama: XMP GPano:ProjectionType
                           equirectangular (covering the whole sphere), an
                           Insta360 photo the camera stitched itself (its EXIF
                           says .jpg), or another known 360 camera's 2:1 photo
                           that does not even loosely look like two circles
        ""                 anything else (also a camera's frame that might be
                           either: it is shown as it is)
    """
    width, height = image.size
    if not imageinfo.is_panorama_aspect(width, height):
        return ""
    gpano = imageinfo.xmp_projection(xmp_bytes)
    if gpano:
        if gpano == PROJECTION_EQUIRECT and imageinfo.xmp_covers_sphere(xmp_bytes):
            return PROJECTION_EQUIRECT
        return ""
    unstitched = _is_unstitched_original(exif, filename)
    camera_360 = imageinfo.is_360_camera(exif)
    if not unstitched and not camera_360:
        return ""
    if not unstitched and imageinfo.insta360_file_type(exif) == "jpg":
        return PROJECTION_EQUIRECT          # stitched in the camera
    loose = _fisheye_signature(image)[1]
    if unstitched and loose:
        return PROJECTION_DUAL_FISHEYE
    if camera_360 and not loose:
        return PROJECTION_EQUIRECT
    return ""


# find_lens_circles(): rays walked in from the edge of each half, and how
# many of them must find the circle's edge (and agree on it) to measure it.
_CIRCLE_RAYS = 24
_CIRCLE_MIN_RAYS = 12


def _circle_edge(pixels, width, centre, angle, bounds, threshold):
    """Where a lens circle's picture starts along one ray, walking inward.

    The ray runs from centre at angle (radians) to the edge of bounds
    (x0, y0, x1, y1). Returns ("edge", (x, y)) at the first pixel brighter
    than threshold, ("open", None) when the picture reaches the bounds
    there, or ("dark", None) when nothing on the ray is bright.
    """
    x0, y0, x1, y1 = bounds
    cx, cy = centre
    dx, dy = math.cos(angle), math.sin(angle)
    reach = []
    if abs(dx) > 1e-9:
        reach.append(((x1 if dx > 0 else x0) - cx) / dx)
    if abs(dy) > 1e-9:
        reach.append(((y1 if dy > 0 else y0) - cy) / dy)
    far = min(reach) - 0.5
    steps = int(far)
    for index in range(steps + 1):
        distance = far - index
        x = min(x1 - 1, max(x0, int(math.floor(cx + distance * dx))))
        y = min(y1 - 1, max(y0, int(math.floor(cy + distance * dy))))
        if pixels[y * width + x] > threshold:
            if index == 0:
                return "open", None
            edge = distance + 0.5           # between this pixel and the last dark one
            return "edge", (cx + edge * dx, cy + edge * dy)
    return "dark", None


def _fit_circle(points):
    """The least-squares circle (cx, cy, r) through points, or None."""
    count = len(points)
    if count < 3:
        return None
    mean_x = sum(point[0] for point in points) / count
    mean_y = sum(point[1] for point in points) / count
    suu = svv = suv = suz = svz = sz = 0.0
    for x, y in points:
        u, v = x - mean_x, y - mean_y
        z = u * u + v * v
        suu += u * u
        svv += v * v
        suv += u * v
        suz += u * z
        svz += v * z
        sz += z
    # u^2 + v^2 + D u + E v + F = 0 in coordinates centred on the points'
    # mean, where the sums of u and v vanish.
    det = suu * svv - suv * suv
    if abs(det) < 1e-9:
        return None
    d = -(suz * svv - svz * suv) / det
    e = -(svz * suu - suz * suv) / det
    f = -sz / count
    a, b = -d / 2.0, -e / 2.0
    if a * a + b * b - f <= 0:
        return None
    return mean_x + a, mean_y + b, math.sqrt(a * a + b * b - f)


def _measure_circle(pixels, size, half, threshold):
    """The lens circle (cx, cy, r) of one half of a small frame, or None.

    _CIRCLE_RAYS rays from the quarter point find where the picture starts.
    The circle must have a dark border all round (no ray finds the picture
    at the frame's edge); edge points that disagree with the rest (a dark
    object at the circle's rim) are dropped, and at least _CIRCLE_MIN_RAYS
    must remain.
    """
    width, height = size
    bounds = (half * width // 2, 0, (half + 1) * width // 2, height)
    centre = ((bounds[0] + bounds[2]) / 2.0, height / 2.0)
    points = []
    for index in range(_CIRCLE_RAYS):
        found, point = _circle_edge(pixels, width, centre,
                                    2.0 * math.pi * index / _CIRCLE_RAYS, bounds, threshold)
        if found == "open":
            return None
        if found == "edge":
            points.append(point)
    circle = None
    while len(points) >= _CIRCLE_MIN_RAYS:
        circle = _fit_circle(points)
        if circle is None:
            return None
        cx, cy, radius = circle
        tolerance = max(1.0, 0.02 * radius)
        kept = [point for point in points
                if abs(math.hypot(point[0] - cx, point[1] - cy) - radius) <= tolerance]
        if len(kept) == len(points):
            break
        points, circle = kept, None
    if circle is None:
        return None
    cx, cy, radius = circle
    if not (0.25 * height <= radius <= 0.5 * height + 1
            and bounds[0] <= cx - radius + 1 and cx + radius - 1 <= bounds[2]
            and -1 <= cy - radius and cy + radius <= height + 1):
        return None
    return circle


def find_lens_circles(image, params=INSTA360_PARAMS):
    """Centre and radius of the two lens circles of a dual-fisheye frame.

    Returns ((cx, cy, r), (cx, cy, r)) for the left and right halves, in
    pixels of image. A circle with a dark border all round is measured from
    where that border ends along rays from its half's centre (see
    _measure_circle). When only one half can be measured, the other lens is
    taken to be the same size and placed alike in its half. When neither
    can -- the circles reach the frame's edges, as on cameras whose circles
    are cropped, or the picture is dark at their rims -- both use the
    default: radius H/2, centred on the quarter points moved by
    params["offsets"].
    """
    width, height = image.size
    radius = height / 2.0
    offsets = params.get("offsets") or ((0.0, 0.0), (0.0, 0.0))
    scale = min(1.0, 512.0 / max(1, width))
    small_size = (max(4, int(round(width * scale))), max(2, int(round(height * scale))))
    small = image if image.mode in ("L", "RGB") else to_rgb(image)
    if small.size != small_size:
        small = small.resize(small_size, _BOX)
    pixels = small.convert("L").tobytes()
    measured = []
    for half, interior in enumerate(level for _, level in _corner_levels(image)):
        threshold = max(DUAL_FISHEYE_CORNER_LEVEL, DUAL_FISHEYE_CORNER_RATIO * interior)
        circle = _measure_circle(pixels, small_size, half, threshold)
        if circle is not None:
            circle = (circle[0] / scale, circle[1] / scale, circle[2] / scale)
        measured.append(circle)
    if measured[0] is None and measured[1] is None:
        return tuple(((half * 2 + 1) * width / 4.0 + offsets[half][0] * radius,
                      height / 2.0 + offsets[half][1] * radius, radius) for half in (0, 1))
    shift = width / 2.0
    if measured[0] is None:
        measured[0] = (measured[1][0] - shift, measured[1][1], measured[1][2])
    if measured[1] is None:
        measured[1] = (measured[0][0] + shift, measured[0][1], measured[0][2])
    return tuple(measured)


def _fisheye_point(circle, back, rotation, mirror, half_fov):
    """A function mapping a direction to its position in one lens circle."""
    cx, cy, radius = circle
    turn = math.radians(rotation)
    scale = radius / half_fov

    def point(x, y, z):
        if back:                # the back lens looks the other way
            x, z = -x, -z
        theta = math.acos(max(-1.0, min(1.0, z)))      # angle from the lens axis
        angle = math.atan2(y, x)
        if mirror:
            angle = math.pi - angle
        angle += turn
        distance = scale * theta                        # equidistant fisheye
        return (cx + distance * math.cos(angle), cy - distance * math.sin(angle))
    return point


def stitch_options(config):
    """{"nadir_fill_deg": .., "zenith_fill_deg": ..} from a config dict.

    Missing or unusable values (not numbers, negative) take the defaults
    (NADIR_FILL_DEG, ZENITH_FILL_DEG); larger ones are cut to POLE_FILL_MAX.
    """
    options = {}
    for key, default in (("nadir_fill_deg", NADIR_FILL_DEG), ("zenith_fill_deg", ZENITH_FILL_DEG)):
        value = config.get(key, default) if isinstance(config, dict) else default
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            value = default
        options[key] = min(float(value), POLE_FILL_MAX)
    return options


_stitch_lock = threading.Lock()
_stitch_memo = [None, None]         # [config.json's (path, mtime, size), options]


def stitch_settings():
    """stitch_options() of config.json, read again only when the file changes."""
    path = common.config_path()
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (path, None, None)
    with _stitch_lock:
        if _stitch_memo[0] == key:
            return dict(_stitch_memo[1])
    try:
        config = common.load_config()
    except common.ConfigError:
        config = {}
    options = stitch_options(config)
    with _stitch_lock:
        _stitch_memo[0], _stitch_memo[1] = key, options
    return dict(options)


def stitch_key(options=None):
    """What the panoramas stitched from dual-fisheye frames depend on, as text.

    STITCH_VERSION and the pole fills (options, default stitch_settings()):
    when it changes, the scan's back-fill stage stitches the stored 360
    photos again (see scan.backfill).
    """
    options = options or stitch_settings()
    return "%d n%g z%g" % (STITCH_VERSION, options["nadir_fill_deg"], options["zenith_fill_deg"])


def _fisheye_source_edge(out_width, params=INSTA360_PARAMS):
    """Frame width at which a dual-fisheye frame has the detail of an
    equirectangular panorama out_width wide (the long edge to decode it at)."""
    return int(math.ceil(out_width * float(params["fov"]) / 180.0))


def _seam_reach(cos_lat, blend):
    """Degrees of longitude, either side of a seam, that the blend band covers
    at a latitude with this cosine (90 or more: the whole row)."""
    if cos_lat <= 1e-9:
        return 180.0
    ratio = math.sin(blend) / cos_lat
    return 180.0 if ratio >= 1.0 else math.degrees(math.asin(ratio))


_seam_masks = collections.OrderedDict()     # (width, height, blend) -> mask image
_seam_masks_lock = threading.Lock()
_SEAM_MASKS_KEPT = 4
_SEAM_MASK_EDGE = 2048          # masks are worked out at most this wide, then scaled


def _seam_mask(width, height, blend):
    """The mask (mode L) that mixes the two lenses' layers: 255 where the front
    lens alone shows, 0 where the back lens does, and a smooth ramp across
    the band of blend radians either side of the plane between the lenses.

    A direction's angle from that plane is asin(cos(latitude) * cos(longitude)),
    so the band is narrow at the horizon and covers whole rows near the poles.
    The same for every frame of a size, so it is kept (a few sizes).
    """
    key = (width, height, round(blend, 6))
    with _seam_masks_lock:
        mask = _seam_masks.get(key)
        if mask is not None:
            _seam_masks.move_to_end(key)
            return mask
    scale = min(1.0, float(_SEAM_MASK_EDGE) / width)
    small_w = max(8, int(round(width * scale)) // 4 * 4)
    small_h = max(4, small_w // 2)
    quarter = small_w // 4
    data = bytearray(small_w * small_h)
    cos_lon = [math.cos(((x + 0.5) / small_w - 0.5) * 2.0 * math.pi) for x in range(small_w)]
    full = b"\xff" * (2 * quarter)
    for y in range(small_h):
        cos_lat = math.cos((0.5 - (y + 0.5) / small_h) * math.pi)
        base = y * small_w
        data[base + quarter:base + 3 * quarter] = full
        reach = _seam_reach(cos_lat, blend)
        if reach >= 90.0:
            columns = range(small_w)
        else:
            span = int(math.ceil(reach * small_w / 360.0)) + 1
            columns = itertools.chain(
                range(max(0, quarter - span), min(small_w, quarter + span)),
                range(max(0, 3 * quarter - span), min(small_w, 3 * quarter + span)))
        for x in columns:
            # Signed angle from the seam plane, towards the front lens.
            angle = math.asin(max(-1.0, min(1.0, cos_lat * cos_lon[x])))
            t = min(1.0, max(0.0, 0.5 + 0.5 * angle / blend))
            data[base + x] = int(round(255.0 * t * t * (3.0 - 2.0 * t)))
    mask = Image.frombytes("L", (small_w, small_h), bytes(data))
    if mask.size != (width, height):
        mask = mask.resize((width, height), _BILINEAR)
    with _seam_masks_lock:
        _seam_masks[key] = mask
        while len(_seam_masks) > _SEAM_MASKS_KEPT:
            _seam_masks.popitem(last=False)
    return mask


def _level_mask(value_low, value_high):
    return [255 if value_low <= value <= value_high else 0 for value in range(256)]


def _gain_estimate(strips, masks, width):
    """Per-channel square roots of back / front over the masked pixels of
    strips [(front strip, back strip)], or None when too few pixels count."""
    sums_front = [0.0, 0.0, 0.0]
    sums_back = [0.0, 0.0, 0.0]
    count = 0.0
    for (strip_front, strip_back), mask in zip(strips, masks):
        pixels = ImageStat.Stat(mask).sum[0] / 255.0
        if pixels < 1:
            continue
        for sums, strip in ((sums_front, strip_front), (sums_back, strip_back)):
            for channel, value in enumerate(ImageStat.Stat(strip, mask).sum):
                sums[channel] += value
        count += pixels
    if count < SEAM_GAIN_MIN_PIXELS * (width / 2048.0) ** 2:
        return None
    if min(sums_front) <= 0 or min(sums_back) <= 0:
        return None
    return tuple(math.sqrt(back / front) for front, back in zip(sums_front, sums_back))


def _lens_gains(front_layer, back_layer, blend):
    """Per-channel gains ((front r, g, b), (back r, g, b)) that give the two
    lenses the same brightness and colour where they overlap.

    Compared along both seams within 60 degrees of the horizon (the poles
    hold the camera's stick and the sky's glare), on pixels that are
    neither nearly black nor clipped in either lens (SEAM_GAIN_LEVELS) and,
    once a first estimate has evened the lenses out, that the two lenses
    see about alike (SEAM_GAIN_DIFFERENCE; not a nearby thing that parallax
    moved). Each lens gets half the correction, at most SEAM_GAIN_LIMIT.
    """
    width, height = front_layer.size
    quarter = width // 4
    span = max(2, int(round(math.degrees(blend) * width / 360.0)))
    top, bottom = height // 6, height - height // 6
    usable_levels = _level_mask(SEAM_GAIN_LEVELS[0], SEAM_GAIN_LEVELS[1])
    alike = [255 if value <= SEAM_GAIN_DIFFERENCE else 0 for value in range(256)]
    strips, usable = [], []
    for seam in (quarter, 3 * quarter):
        box = (seam - span, top, seam + span, bottom)
        strip_front, strip_back = front_layer.crop(box), back_layer.crop(box)
        strips.append((strip_front, strip_back))
        usable.append(ImageChops.multiply(strip_front.convert("L").point(usable_levels),
                                          strip_back.convert("L").point(usable_levels)))
    first = _gain_estimate(strips, usable, width)
    if first is None:
        return (1.0, 1.0, 1.0), (1.0, 1.0, 1.0)
    masks = []
    for (strip_front, strip_back), mask in zip(strips, usable):
        evened_front = _apply_gains(strip_front, first).convert("L")
        evened_back = _apply_gains(strip_back, [1.0 / gain for gain in first]).convert("L")
        masks.append(ImageChops.multiply(
            mask, ImageChops.difference(evened_front, evened_back).point(alike)))
    gains = _gain_estimate(strips, masks, width) or first
    gains = [min(SEAM_GAIN_LIMIT, max(1.0 / SEAM_GAIN_LIMIT, gain)) for gain in gains]
    return tuple(gains), tuple(1.0 / gain for gain in gains)


def _gain_table(gain):
    """The 256 levels of a channel brightened or darkened by gain.

    Dark and middle levels are multiplied by about gain; towards white the
    gain fades out (v * gain ** (1 - (v / 255) ** SEAM_GAIN_ROLLOFF)), so
    0 and 255 stay where they are: a clipped sky stays neutral white in
    both lenses, and no level below 255 becomes 255. Brighter levels never
    come out darker.
    """
    table = []
    for value in range(255):
        level = value * gain ** (1.0 - (value / 255.0) ** SEAM_GAIN_ROLLOFF)
        table.append(min(254, int(round(level))))
    table.append(255)
    return table


def _apply_gains(image, gains):
    if all(abs(gain - 1.0) < 0.002 for gain in gains):
        return image
    table = []
    for gain in gains:
        table.extend(_gain_table(gain))
    return image.point(table)


def _row_mask(width, weights):
    """A mode L mask width wide whose rows have the given weights (0.0-1.0), top to bottom."""
    data = bytes(int(round(255.0 * min(1.0, max(0.0, weight)))) for weight in weights)
    return Image.frombytes("L", (1, len(data)), data).resize((width, len(data)), _NEAREST)


def _pole_view_point(size, focal, bottom):
    """A function mapping an equirectangular panorama position (longitude,
    latitude in radians) to its place in a view of size x size pixels
    looking straight down (bottom) or up, as equirect_view() draws it."""
    half = size / 2.0

    def point(longitude, latitude):
        across = math.cos(latitude) * math.sin(longitude)
        ahead = math.cos(latitude) * math.cos(longitude)
        height = max(1e-6, -math.sin(latitude) if bottom else math.sin(latitude))
        scale = focal / height
        if bottom:
            return (half + across * scale, half - ahead * scale)
        return (half + across * scale, half + ahead * scale)
    return point


def _fill_disc(view, radius):
    """view (RGB, square) with the disc of radius pixels round its centre filled in.

    The disc is painted from its surroundings inwards: it starts as their
    mean colour and is blurred again and again, from coarse to fine, with
    the pixels outside kept as they are. That spreads the colours at its
    edge smoothly over it, the way heat spreads (no spokes, no copied
    shapes).
    """
    size = view.size[0]
    centre = size / 2.0
    inside = bytearray(size * size)
    for y in range(size):
        dy = y + 0.5 - centre
        reach = radius * radius - dy * dy
        if reach <= 0:
            continue
        span = math.sqrt(reach)
        left = max(0, int(math.floor(centre - span)))
        right = min(size, int(math.ceil(centre + span)))
        inside[y * size + left:y * size + right] = b"\xff" * (right - left)
    mask = Image.frombytes("L", (size, size), bytes(inside))
    outside = ImageChops.invert(mask)
    if not ImageStat.Stat(outside).sum[0]:
        return view
    mean = tuple(int(round(value)) for value in ImageStat.Stat(view, outside).mean)
    filled = Image.composite(Image.new("RGB", view.size, mean), view, mask)
    blur = max(1.0, radius / 2.0)
    while True:
        for _ in range(POLE_FILL_PASSES):
            filled = Image.composite(filled.filter(ImageFilter.GaussianBlur(blur)), view, mask)
        if blur <= 1.0:
            return filled
        blur = max(1.0, blur / 2.0)


def _fill_pole(panorama, degrees, bottom=True):
    """Cover one pole of a panorama with a soft fill made from the ring around it.

    The rows within degrees of the pole (the nadir when bottom, else the
    zenith) are painted over, which hides a tripod's legs (a hand-held
    stick lies between the lenses, where the stitch already hides it) and
    what blending makes of them. The fill is made in a view looking straight
    at the pole, where it is a round disc: the colours round its edge are
    spread smoothly over it (_fill_disc), and the result is drawn back into
    the panorama, fading in over POLE_FEATHER_DEG so it has no hard edge.
    Returns the panorama (changed in place).
    """
    width, height = panorama.size
    degrees = min(float(degrees), 45.0)
    feather = POLE_FEATHER_DEG
    rows = int(round(height * (degrees + feather) / 180.0))
    if int(round(height * degrees / 180.0)) < 1:
        return panorama
    # The view covers the disc and a margin round it.
    view_degrees = degrees + feather + POLE_MARGIN_DEG
    size = min(1024, max(32, int(round(width * 2.0 * view_degrees / 360.0))))
    focal = (size / 2.0) / math.tan(math.radians(view_degrees))
    view = equirect_view(panorama, (size, size), fov=2.0 * view_degrees,
                         pitch=-90.0 if bottom else 90.0)
    view = _fill_disc(view, focal * math.tan(math.radians(degrees + feather)))
    point = _pole_view_point(size, focal, bottom)
    top = height - rows if bottom else 0

    def mesh_point(x, y):
        longitude = (float(x) / width - 0.5) * 2.0 * math.pi
        latitude = (0.5 - float(top + y) / height) * math.pi
        return point(longitude, latitude)
    strip = view.transform((width, rows), _MESH,
                           _mesh(_cells(0, width), _cells(0, rows), mesh_point), _BILINEAR)
    # Rows of the strip fade in from the outer edge over the feather.
    feather_rows = max(1.0, height * feather / 180.0)
    weights = []
    for row in range(rows):
        distance = (row + 0.5) if bottom else (rows - row - 0.5)     # from the outer edge
        t = min(1.0, distance / feather_rows)
        weights.append(t * t * (3.0 - 2.0 * t))
    panorama.paste(strip, (0, top), _row_mask(width, weights))
    return panorama


def dual_fisheye_to_equirect(image, out_width=PREVIEW_EDGE, params=INSTA360_PARAMS,
                             nadir_fill=None, zenith_fill=None):
    """Stitch a dual-fisheye frame into an equirectangular panorama.

    image holds the two lens circles side by side (see find_lens_circles()
    and INSTA360_PARAMS for the lens model). Returns an RGB image
    out_width x out_width/2 (out_width rounded down to a multiple of 4): the
    front lens fills the middle half, the back lens the outer quarters.

    The seams (longitude -90 and 90 degrees, and the poles, where the plane
    between the lenses cuts the sphere) are blended: each lens is drawn into
    a layer of its own a little beyond its half (params["blend"] degrees
    past the plane; the lenses see 199 degrees, so both see that band), the
    two layers are given the same brightness and colour where they overlap
    (_lens_gains), and they are mixed with a smooth ramp across the band
    (_seam_mask). blend 0 joins them in hard lines instead.

    nadir_fill and zenith_fill are degrees around the bottom and top pole to
    cover with a soft fill (_fill_pole; 0 = none); None takes the settings
    of config.json (stitch_settings()).
    """
    if image.mode != "RGB":
        image = to_rgb(image)
    out_width = max(8, int(out_width) // 4 * 4)
    out_height = out_width // 2
    if nadir_fill is None or zenith_fill is None:
        settings = stitch_settings()
        nadir_fill = settings["nadir_fill_deg"] if nadir_fill is None else nadir_fill
        zenith_fill = settings["zenith_fill_deg"] if zenith_fill is None else zenith_fill
    circles = find_lens_circles(image, params)
    half_fov = math.radians(float(params["fov"]) / 2.0)
    front = 0 if params.get("front", "left") == "left" else 1
    rotation = params.get("rotation") or (0, 0)
    mirror = bool(params.get("mirror"))
    # The band may not reach past the edge of what the lenses see.
    blend = math.radians(min(max(0.0, float(params.get("blend", 0.0))),
                             max(0.0, float(params["fov"]) / 2.0 - 90.0)))
    lenses = [_fisheye_point(circles[half], half != front, rotation[half], mirror, half_fov)
              for half in (0, 1)]
    seam_left, seam_right = out_width // 4, out_width * 3 // 4
    rows = _cells(0, out_height)
    sin_lat = {}
    cos_lat = {}
    for y in rows:
        latitude = (0.5 - float(y) / out_height) * math.pi
        sin_lat[y], cos_lat[y] = math.sin(latitude), math.cos(latitude)

    def lens_point(half):
        lens = lenses[half]
        known = {}          # (Neighbouring row bands share their edge points.)

        def point(x, y):
            found = known.get((x, y))
            if found is None:
                longitude = (float(x) / out_width - 0.5) * 2.0 * math.pi
                found = known[x, y] = lens(cos_lat[y] * math.sin(longitude), sin_lat[y],
                                           cos_lat[y] * math.cos(longitude))
            return found
        return point

    if blend <= 0:
        mesh = []
        for start, stop, half in ((0, seam_left, 1 - front), (seam_left, seam_right, front),
                                  (seam_right, out_width, 1 - front)):
            mesh.extend(_mesh(_cells(start, stop), rows, lens_point(half)))
        panorama = image.transform((out_width, out_height), _MESH, mesh, _BILINEAR)
    else:
        layers = []
        for half, is_front in ((front, True), (1 - front, False)):
            point = lens_point(half)
            mesh = []
            for index in range(len(rows) - 1):
                top, bottom = rows[index], rows[index + 1]
                reach = _seam_reach(min(cos_lat[top], cos_lat[bottom]), blend)
                extra = int(math.ceil(reach * out_width / 360.0)) + MESH_CELL
                if extra >= seam_left:
                    spans = [(0, out_width)]
                elif is_front:
                    spans = [(seam_left - extra, seam_right + extra)]
                else:
                    spans = [(0, seam_left + extra), (seam_right - extra, out_width)]
                for start, stop in spans:
                    mesh.extend(_mesh(_cells(start, stop), (top, bottom), point))
            layers.append(image.transform((out_width, out_height), _MESH, mesh, _BILINEAR))
        gains = _lens_gains(layers[0], layers[1], blend)
        layers = [_apply_gains(layer, gain) for layer, gain in zip(layers, gains)]
        panorama = Image.composite(layers[0], layers[1], _seam_mask(out_width, out_height, blend))
    if nadir_fill:
        _fill_pole(panorama, nadir_fill, bottom=True)
    if zenith_fill:
        _fill_pole(panorama, zenith_fill, bottom=False)
    return panorama


# equirect_view(): a mesh cell whose corners lie further apart than this in
# longitude (near a pole) is split, down to single pixels.
_VIEW_CELL_TURN = math.radians(20.0)


def equirect_view(image, size, fov=FRONT_VIEW_FOV, yaw=0.0, pitch=0.0):
    """A perspective (rectilinear) picture cut from an equirectangular panorama.

    size is the output (width, height); fov the horizontal field of view in
    degrees; yaw and pitch (degrees, right and up) the viewing direction,
    0, 0 being the centre of the panorama. Any direction works: cells of the
    mesh that cross the panorama's left/right edge sample it wrapped round,
    and cells near a pole are split until they are small.
    """
    if image.mode not in ("L", "RGB"):
        image = to_rgb(image)
    out_width, out_height = max(1, int(size[0])), max(1, int(size[1]))
    src_width, src_height = image.size
    focal = (out_width / 2.0) / math.tan(math.radians(min(max(fov, 1.0), 170.0)) / 2.0)
    cos_yaw, sin_yaw = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cos_pitch, sin_pitch = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    directions = {}

    def direction(u, v):
        """(longitude, latitude) in radians seen at output pixel corner u, v."""
        if (u, v) not in directions:
            x, y, z = u - out_width / 2.0, out_height / 2.0 - v, focal
            y, z = y * cos_pitch + z * sin_pitch, z * cos_pitch - y * sin_pitch
            x, z = x * cos_yaw + z * sin_yaw, z * cos_yaw - x * sin_yaw
            directions[u, v] = (math.atan2(x, z), math.atan2(y, math.hypot(x, z)))
        return directions[u, v]

    cells = []

    def add_cell(x0, y0, x1, y1):
        corners = [direction(x0, y0), direction(x0, y1), direction(x1, y1), direction(x1, y0)]
        # Longitudes as one continuous run (they may cross +-180 degrees).
        first = corners[0][0]
        longitudes = [lon + 2.0 * math.pi * round((first - lon) / (2.0 * math.pi))
                      for lon, _ in corners]
        if max(longitudes) - min(longitudes) > _VIEW_CELL_TURN:
            if x1 - x0 > 1 or y1 - y0 > 1:
                xm, ym = (x0 + x1 + 1) // 2, (y0 + y1 + 1) // 2
                for left, top, right, bottom in ((x0, y0, xm, ym), (xm, y0, x1, ym),
                                                 (x0, ym, xm, y1), (xm, ym, x1, y1)):
                    if right > left and bottom > top:
                        add_cell(left, top, right, bottom)
                return
            longitudes = [first] * 4        # a pixel at the pole: any longitude will do
        quad = []
        for longitude, (_, latitude) in zip(longitudes, corners):
            # (Pillow samples nothing at exactly the bottom edge.)
            quad += [(longitude / (2.0 * math.pi) + 0.5) * src_width,
                     min(src_height - 0.01, (0.5 - latitude / math.pi) * src_height)]
        cells.append([(x0, y0, x1, y1), quad])

    columns, rows = _cells(0, out_width), _cells(0, out_height)
    for i in range(len(columns) - 1):
        for j in range(len(rows) - 1):
            add_cell(columns[i], rows[j], columns[i + 1], rows[j + 1])
    # Cells across the left/right edge reach a little beyond the panorama:
    # sample from a copy with the opposite edge's columns added there.
    xs = [quad[index] for _, quad in cells for index in range(0, 8, 2)]
    lowest, highest = min(xs), max(xs)
    pad_left = min(src_width, int(math.ceil(-lowest)) + 1) if lowest < 0 else 0
    pad_right = (min(src_width, int(math.ceil(highest - src_width)) + 1)
                 if highest >= src_width else 0)
    source = image
    if pad_left or pad_right:
        source = Image.new(image.mode, (src_width + pad_left + pad_right, src_height))
        source.paste(image, (pad_left, 0))
        if pad_left:
            source.paste(image.crop((src_width - pad_left, 0, src_width, src_height)), (0, 0))
        if pad_right:
            source.paste(image.crop((0, 0, pad_right, src_height)), (pad_left + src_width, 0))
        for _, quad in cells:
            for index in range(0, 8, 2):
                quad[index] += pad_left
    mesh = [(box, tuple(quad)) for box, quad in cells]
    return source.transform((out_width, out_height), _MESH, mesh, _BILINEAR)


def front_view(equirect, width=THUMB_SIZE):
    """The thumbnail picture of a 360 photo: a FRONT_VIEW_FOV degree wide,
    FRONT_VIEW_ASPECT perspective view of the panorama's centre, width
    pixels wide. It is rendered at twice the size and scaled down, which
    keeps fine detail from aliasing."""
    size = (int(width), max(1, int(round(width / FRONT_VIEW_ASPECT))))
    view = equirect_view(equirect, (size[0] * 2, size[1] * 2))
    return view.resize(size, _LANCZOS, reducing_gap=2.0)


def _image_xmp(image, data):
    """The raw XMP packet of an opened image, or b""."""
    xmp = image.info.get("xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "replace")
    if not xmp and image.format in ("JPEG", "MPO"):
        xmp = imageinfo.jpeg_xmp(data)
    return bytes(xmp or b"")


def _may_be_360(image, exif, xmp, filename):
    """True if an opened (not yet decoded) image could be a 360 photo.

    Only these get the closer look of detect_projection(), which needs the
    decoded pixels.
    """
    width, height = image.size
    if _orientation_method(image) in _SWAPPING_METHODS:
        width, height = height, width
    if not imageinfo.is_panorama_aspect(width, height):
        return False
    return (imageinfo.xmp_projection(xmp) == PROJECTION_EQUIRECT or _is_insp(filename)
            or imageinfo.is_360_camera(exif))


def _stitched_original(data, size_for, known=False):
    """(panorama, icc) for an unstitched dual-fisheye original, else None.

    known=True: the caller knows the original is one (the index says so).
    Otherwise there is no file name to go on, so only an original whose
    Insta360 EXIF says the camera saved it as .insp is decoded and checked
    with detect_projection(); anything else returns None before decoding.
    The panorama is the one a preview would show, then sized by
    size_for(width, height).
    """
    image = _open_image(data)
    with image:
        exif = xmp = None
        if not known:
            exif = extract_exif(image)
            if imageinfo.insta360_file_type(exif) != "insp":
                return None
            xmp = _image_xmp(image, data)
            if not _may_be_360(image, exif, xmp, ""):
                return None
        edge = _fisheye_source_edge(PREVIEW_EDGE)
        with _decoded(image, lambda w, h: _fit_size((w, h), edge), resize=False) as (
                rgb, (width, height), icc):
            if known:
                if not imageinfo.is_panorama_aspect(width, height):
                    return None             # not the frame the index saw
            elif detect_projection(rgb, exif, xmp, "") != PROJECTION_DUAL_FISHEYE:
                return None
            panorama = dual_fisheye_to_equirect(rgb, min(PREVIEW_EDGE, width))
            target = size_for(*panorama.size)
            if panorama.size != target:
                panorama = panorama.resize(target, _LANCZOS, reducing_gap=2.0)
            return panorama, icc


def read_image(path, helper=None, preview_edge=PREVIEW_EDGE, want_preview=True,
               mtime=None, want_pano=False):
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
        projection      "" for an ordinary photo, "equirectangular" or
                        "dual_fisheye" for a 360-degree photo (see
                        detect_projection()). Then the preview is the
                        equirectangular panorama (stitched from the two
                        lenses for dual_fisheye), the thumbnail and dHash
                        show its front view (front_view()) and width x
                        height is the panorama's 2:1 size.
        pano            for a 360-degree photo larger than the preview
                        (has_pano) when want_pano is set: JPEG bytes of
                        the equirectangular panorama, PANO_EDGE wide
                        (never upscaled; no EXIF), for the 360 view; else
                        None. A dual-fisheye frame is then decoded
                        at full size and stitched at that width (the
                        preview is that panorama made smaller), so the
                        pano holds the detail of the original.
    mtime (the file's modification time) is only used when neither EXIF nor
    the file name gives a capture date; when omitted it is looked up.
    Raises ImageReadError (SourceReadError for I/O errors) when the file
    cannot be read or decoded, is not a supported image, or is too large.
    """
    data = read_source_bytes(path, helper)
    content_hash = hashlib.sha1(data).hexdigest()
    image = _open_image(data)
    name = os.path.basename(path)
    working_edge = max(int(preview_edge), THUMB_SIZE)
    try:
        with image:
            mime = _image_mime(image, data)
            exif = extract_exif(image)
            xmp = _image_xmp(image, data)
            maybe_360 = _may_be_360(image, exif, xmp, name)
            # A possible 360 photo is decoded large enough for its pano.
            pano_edge = int(PANO_EDGE) if want_pano and maybe_360 else 0
            stitch_edge = max(working_edge, pano_edge)
            # A dual-fisheye frame is decoded larger: its circles are
            # stretched over the whole panorama.
            maybe_dual = maybe_360 and _is_unstitched_original(exif, name)
            decode_edge = _fisheye_source_edge(stitch_edge) if maybe_dual else stitch_edge
            with _decoded(image, lambda w, h: _fit_size((w, h), decode_edge),
                          resize=not maybe_dual) as (working, (width, height), icc):
                projection = detect_projection(working, exif, xmp, name) if maybe_360 else ""
                if projection == PROJECTION_DUAL_FISHEYE:
                    # The panorama is as wide as the frame (never upscaled).
                    working = dual_fisheye_to_equirect(working, min(stitch_edge, width))
                    height = max(1, int(round(width / 2.0)))
                pano = None
                if projection and pano_edge and has_pano(width, height):
                    pano_size = _fit_size((width, height), pano_edge)
                    pano_image = working
                    if pano_image.size != pano_size:
                        pano_image = working.resize(pano_size, _LANCZOS, reducing_gap=2.0)
                    pano = _encode_jpeg(pano_image, PANO_QUALITY, icc)
                working_size = _fit_size((width, height), working_edge)
                if working.size != working_size:
                    working = working.resize(working_size, _LANCZOS, reducing_gap=2.0)
                if projection:
                    thumb_image = front_view(working)
                    dhash = compute_dhash(thumb_image)
                else:
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

    meta = exif_metadata(exif, name, mtime)
    if not meta["taken_source"] and mtime is None:
        meta.update(zip(("taken_at", "taken_source", "tz_offset"),
                        parse_capture_date(exif, name, _source_mtime(path, helper))))
    result = {
        "width": width, "height": height, "thumb": thumb, "preview": preview,
        "exif": exif, "dhash": dhash, "content_hash": content_hash,
        "mime": mime, "projection": projection, "pano": pano,
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
                    quality=88, projection=None):
    """Return JPEG bytes sized for a vision model (see llm_size()).

    Accepts the bytes of a preview or of any original Pillow can read (HEIC
    too). The result is upright, flattened to RGB, never upscaled, and
    carries no EXIF. An unstitched dual-fisheye original is sent as its
    equirectangular panorama, as its preview shows it. projection is what
    the index knows about an original (its projection column):
    "dual_fisheye" stitches it, any other checked value ("",
    "equirectangular") sends it as it is. When it is None (or "?", not
    checked yet), an original whose Insta360 EXIF says it was saved as .insp
    is stitched if detect_projection() agrees. A preview is stitched already
    and carries no EXIF: leave projection None for it.
    Raises ImageReadError if the bytes cannot be decoded.
    """
    def size_for(width, height):
        return llm_size(width, height, max_edge, min_short, max_pixels)
    if projection in (None, PROJECTION_UNKNOWN, PROJECTION_DUAL_FISHEYE):
        try:
            stitched = _stitched_original(jpeg_bytes, size_for,
                                          known=projection == PROJECTION_DUAL_FISHEYE)
        except Exception:
            stitched = None         # _render_jpeg() reports any real problem
        if stitched is not None:
            return _encode_jpeg(stitched[0], quality, stitched[1])
    return _render_jpeg(jpeg_bytes, quality, size_for)


def to_browser_jpeg(path, helper=None, quality=BROWSER_QUALITY):
    """Full-size, upright JPEG bytes for an original browsers cannot show.

    Used for HEIC/HEIF/AVIF/TIFF originals. No EXIF is copied.
    Raises ImageReadError if the file cannot be read or decoded.
    """
    return _render_jpeg(read_source_bytes(path, helper), quality,
                        lambda w, h: (w, h))


def jpeg_from_bytes(data, quality=BROWSER_QUALITY):
    """Full-size, upright JPEG bytes of image bytes in any format this can read.

    No EXIF is copied (a non-sRGB colour profile is kept). Used by the
    static export for pictures whose metadata cannot be removed in place.
    Raises ImageReadError if the bytes cannot be decoded.
    """
    return _render_jpeg(bytes(data), quality, lambda w, h: (w, h))


def png_from_bytes(data):
    """PNG bytes of image bytes (a BMP, ...): the same pixels, with no metadata.

    Used by the static export for lossless originals in formats whose
    metadata cannot be removed in place (a BMP can name a colour profile
    file, and keep bytes after its pixels). Raises ImageReadError if the
    bytes cannot be decoded.
    """
    image = _open_image(bytes(data))
    try:
        with image:
            with _decode_budget.hold(image.size[0] * image.size[1]):
                image.load()
                frame = image
                if frame.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
                    frame = to_rgb(frame)
                # The colour profile stays, without its texts (see _kept_profile).
                icc = image.info.get("icc_profile")
                buffer = io.BytesIO()
                frame.save(buffer, "PNG", icc_profile=imageinfo.scrub_icc(icc) if icc else None)
                return buffer.getvalue()
    except ImageReadError:
        raise
    except Image.DecompressionBombError as error:
        raise ImageReadError("image is too large to decode safely (%s)"
                             % _error_text(error)) from error
    except Exception as error:
        raise ImageReadError("cannot decode image (%s)" % _error_text(error)) from error


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
