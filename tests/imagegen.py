#!/usr/bin/env python3
"""Synthetic test images, generated in code.

Every builder writes one small file into a directory (normally a temporary
one) and returns its path. Nothing here is ever copied from a real photo
library. Builders for formats this Pillow cannot write (HEIC without
pillow-heif, AVIF without libavif) return None so tests can skip.
"""

import io
import os
import struct

from PIL import Image, ImageDraw
from PIL.TiffImagePlugin import IFDRational

RED = (220, 20, 20)
BLUE = (20, 20, 220)

# EXIF tag ids used below.
TAG_MAKE, TAG_MODEL, TAG_ORIENTATION, TAG_DATETIME = 0x010F, 0x0110, 0x0112, 0x0132
TAG_EXIF_IFD, TAG_GPS_IFD = 0x8769, 0x8825
TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED = 0x9003, 0x9004
TAG_OFFSET_TIME, TAG_OFFSET_TIME_ORIGINAL = 0x9010, 0x9011
TAG_FNUMBER, TAG_EXPOSURE_BIAS = 0x829D, 0x9204
GPS_LAT_REF, GPS_LAT, GPS_LON_REF, GPS_LON, GPS_ALT = 1, 2, 3, 4, 6


def block_levels():
    """8 rows x 9 columns of gray levels; horizontal neighbours always differ a lot."""
    return [[30 + ((row * 7 + col * 11) % 9) * 25 for col in range(9)]
            for row in range(8)]


def blocks_image(block=10):
    """A deterministic 9x8-block RGB image whose dHash has no borderline bits."""
    image = Image.new("RGB", (9 * block, 8 * block))
    draw = ImageDraw.Draw(image)
    for row, levels in enumerate(block_levels()):
        for col, level in enumerate(levels):
            color = (level, max(0, level - 20), min(255, level + 20))
            draw.rectangle([col * block, row * block,
                            (col + 1) * block - 1, (row + 1) * block - 1], fill=color)
    return image


def marker_image(size=(64, 48)):
    """Blue image with a red square in the top-left corner (orientation checks)."""
    image = Image.new("RGB", size, BLUE)
    edge = max(4, min(size) // 3)
    image.paste(RED, (0, 0, edge, edge))
    return image


def textured_image(size=(256, 256)):
    """Busy deterministic content, so JPEG data is large enough to truncate."""
    width, height = size
    image = Image.new("L", size)
    image.putdata(bytes(((x * 31) ^ (y * 17) ^ (x * y)) & 0xFF
                        for y in range(height) for x in range(width)))
    return image.convert("RGB")


def dms(value):
    """Degrees/minutes/seconds rationals for an absolute decimal coordinate."""
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = (value - degrees - minutes / 60.0) * 3600
    return (IFDRational(degrees, 1), IFDRational(minutes, 1),
            IFDRational(int(round(seconds * 100)), 100))


def make_exif(date="2019:05:14 14:23:01", offset="+02:00", make="Apple",
              model="iPhone 12", orientation=None, gps=None, digitized=None,
              modified=None, modified_offset=None, exif_tags=None, gps_tags=None):
    """Build a Pillow Exif object. gps is (lat, lon) in decimal degrees.

    exif_tags/gps_tags are extra {tag_id: value} items for the Exif and GPS
    sub-IFDs (for example zero-denominator rationals).
    """
    exif = Image.Exif()
    if make:
        exif[TAG_MAKE] = make
    if model:
        exif[TAG_MODEL] = model
    if orientation:
        exif[TAG_ORIENTATION] = orientation
    if modified:
        exif[TAG_DATETIME] = modified
    # Sub-IFDs are assigned as plain dicts: that is how every Pillow
    # version since 8 writes them (get_ifd() edits are lost before 11.1).
    sub = {}
    if date:
        sub[TAG_DATETIME_ORIGINAL] = date
    if offset:
        sub[TAG_OFFSET_TIME_ORIGINAL] = offset
    if digitized:
        sub[TAG_DATETIME_DIGITIZED] = digitized
    if modified_offset:
        sub[TAG_OFFSET_TIME] = modified_offset
    sub.update(exif_tags or {})
    if sub:
        exif[TAG_EXIF_IFD] = sub
    if gps is not None or gps_tags:
        gps_ifd = {}
        if gps is not None:
            lat, lon = gps
            gps_ifd[GPS_LAT_REF] = "S" if lat < 0 else "N"
            gps_ifd[GPS_LAT] = dms(lat)
            gps_ifd[GPS_LON_REF] = "W" if lon < 0 else "E"
            gps_ifd[GPS_LON] = dms(lon)
        gps_ifd.update(gps_tags or {})
        exif[TAG_GPS_IFD] = gps_ifd
    return exif


def jpeg_bytes(image, exif=None, quality=90):
    """JPEG bytes for image; exif is a Pillow Exif object or raw EXIF bytes."""
    buffer = io.BytesIO()
    options = {"quality": quality}
    if exif is not None:
        options["exif"] = exif if isinstance(exif, bytes) else exif.tobytes()
    image.save(buffer, "JPEG", **options)
    return buffer.getvalue()


def _write(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def jpeg_with_exif(directory, name="exif.jpg", image=None, **exif_options):
    """JPEG with EXIF from make_exif(**exif_options); the marker image by default."""
    image = image if image is not None else marker_image()
    return _write(directory, name, jpeg_bytes(image, make_exif(**exif_options)))


# A rational no real tag uses; patched to 0/0 after Pillow serialised it,
# because some Pillow versions cannot write a zero denominator themselves.
_SENTINEL = (1000003, 999983)


def jpeg_zero_rationals(directory, name="zero-rationals.jpg"):
    """JPEG whose FNumber, ExposureBiasValue, GPSAltitude and latitude are 0/0."""
    marker = IFDRational(*_SENTINEL)
    exif = make_exif(
        exif_tags={TAG_FNUMBER: marker, TAG_EXPOSURE_BIAS: marker},
        gps_tags={GPS_LAT_REF: "N", GPS_LAT: (marker, marker, marker),
                  GPS_LON_REF: "E", GPS_LON: dms(10.5), GPS_ALT: marker})
    data = exif.tobytes()
    order = ">" if data[6:8] == b"MM" else "<"
    sentinel = struct.pack(order + "LL", *_SENTINEL)
    assert data.count(sentinel) == 6, "unexpected EXIF layout"
    data = data.replace(sentinel, struct.pack(order + "LL", 0, 0))
    return _write(directory, name, jpeg_bytes(marker_image(), data))


def png16_gray(directory, name="gray16.png", size=(256, 32), value=None,
               reverse=False):
    """16-bit grayscale PNG: a constant value, or a ramp 0..65535.

    The ramp gets brighter to the right, or to the left with reverse=True.
    """
    width, height = size
    image = Image.new("I;16", size)
    if value is None:
        row = [x * 65535 // (width - 1) for x in range(width)]
        if reverse:
            row.reverse()
        image.putdata(row * height)
    else:
        image.putdata([value] * (width * height))
    path = os.path.join(directory, name)
    image.save(path, "PNG")
    return path


def rgba_png(directory, name="alpha.png", size=(64, 32)):
    """RGBA PNG: left half opaque red, right half fully transparent black."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, size[0] // 2, size[1]))
    path = os.path.join(directory, name)
    image.save(path, "PNG")
    return path


def palette_png_transparent(directory, name="palette.png", size=(64, 32)):
    """Palette PNG: index 1 (red) on the left, transparent index 0 (black) on the right."""
    image = Image.new("P", size, 0)
    image.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
    image.paste(1, (0, 0, size[0] // 2, size[1]))
    path = os.path.join(directory, name)
    image.save(path, "PNG", transparency=0)
    return path


def cmyk_jpeg(directory, name="cmyk.jpg", size=(64, 48)):
    """CMYK JPEG that is pure red (C=0, M=Y=100 %, K=0)."""
    path = os.path.join(directory, name)
    Image.new("CMYK", size, (0, 255, 255, 0)).save(path, "JPEG", quality=95)
    return path


def animated_gif(directory, name="animated.gif", size=(48, 48)):
    """Three-frame GIF: red, then green, then blue."""
    frames = [Image.new("RGB", size, color).convert("P")
              for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    path = os.path.join(directory, name)
    frames[0].save(path, "GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    return path


def jpeg_named_png(directory, name="really-a-jpeg.png"):
    """A '.png' file that actually holds JPEG data."""
    return _write(directory, name, jpeg_bytes(marker_image()))


def screenshot_png(directory, name="Screenshot_2019-05-14-14-23-01.png",
                   size=(1290, 2796)):
    """A tall phone-screenshot-like PNG with text-like bars."""
    image = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0] - 1, 140], fill=(30, 30, 30))
    for top in range(220, size[1] - 60, 90):
        draw.rectangle([60, top, 60 + (top * 7) % (size[0] - 160), top + 28],
                       fill=(60, 60, 60))
    path = os.path.join(directory, name)
    image.save(path, "PNG")
    return path


def large_jpeg(directory, name="large.jpg", size=(4000, 3000), orientation=None,
               image=None):
    """A big JPEG (exercises draft decoding): the marker image, or image."""
    exif = make_exif(orientation=orientation) if orientation else None
    image = image if image is not None else marker_image(size)
    return _write(directory, name, jpeg_bytes(image, exif, quality=80))


def heic_file(directory, name="photo.heic", orientation=None, size=(64, 48)):
    """HEIC marker image with EXIF, or None if pillow-heif cannot write HEIC."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        buffer = io.BytesIO()
        exif = make_exif(orientation=orientation)
        marker_image(size).save(buffer, "HEIF", exif=exif.tobytes(), quality=90)
    except Exception:
        return None
    return _write(directory, name, buffer.getvalue())


def avif_file(directory, name="photo.avif", orientation=None, size=(64, 48)):
    """AVIF marker image, or None if this Pillow cannot write AVIF."""
    try:
        buffer = io.BytesIO()
        exif = make_exif(orientation=orientation)
        marker_image(size).save(buffer, "AVIF", exif=exif.tobytes(), quality=90)
    except Exception:
        return None
    return _write(directory, name, buffer.getvalue())


def save_as(directory, name, format_name, image=None, **options):
    """Save the marker image (or image) in any format Pillow can write."""
    image = image if image is not None else marker_image()
    path = os.path.join(directory, name)
    image.save(path, format_name, **options)
    return path


def corrupt_file(directory, name="corrupt.jpg"):
    """Bytes that are not an image at all."""
    return _write(directory, name, b"not an image\n" + bytes(range(256)) * 4)


def truncated_jpeg(directory, name="truncated.jpg"):
    """A JPEG cut off part-way through its image data."""
    data = jpeg_bytes(textured_image(), quality=95)
    return _write(directory, name, data[:len(data) * 6 // 10])


def empty_file(directory, name="empty.jpg"):
    return _write(directory, name, b"")
