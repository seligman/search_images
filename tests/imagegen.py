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


# ---------------------------------------------------------------------------
# 360-degree photos

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


def dual_fisheye_image(size=(1024, 512), radius=None, offsets=((0.0, 0.0), (0.0, 0.0)),
                       markers=(), background=(0, 0, 0)):
    """An unstitched dual-fisheye frame: two bright, differently striped
    circles on black, one per half.

    radius defaults to half the height (circles as tall as the frame);
    offsets move the centres from the quarter points, in units of radius.
    markers are (x, y, r) red discs drawn on top.
    """
    width, height = size
    radius = float(radius or height / 2.0)
    frame = Image.new("RGB", size, background)
    for half, (dx, dy) in enumerate(offsets):
        cx = (2 * half + 1) * width / 4.0 + dx * radius
        cy = height / 2.0 + dy * radius
        pattern = Image.new("RGB", size, (90, 160, 90) if half == 0 else (90, 120, 200))
        draw = ImageDraw.Draw(pattern)
        step = max(4, height // 16)
        for start in range(-height, width + height, step * 2):
            if half == 0:
                draw.line([(start, 0), (start + height, height)], fill=(200, 220, 120),
                          width=max(1, step // 2))
            else:
                draw.line([(start, height), (start + height // 2, 0)], fill=(230, 230, 250),
                          width=max(1, step // 3))
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                                     fill=255)
        frame.paste(pattern, (0, 0), mask)
    draw = ImageDraw.Draw(frame)
    for x, y, r in markers:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=RED)
    return frame


def equirect_image(size=(512, 256), marker=False):
    """A stitched-panorama-like 2:1 picture: sky above, ground below, posts.

    With marker=True a red disc sits in the middle (the forward direction).
    """
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        if y < height // 2:
            shade = 150 + 90 * y // max(1, height // 2)
            draw.line([(0, y), (width, y)], fill=(shade - 40, shade - 10, 255))
        else:
            shade = 120 - 60 * (y - height // 2) // max(1, height // 2)
            draw.line([(0, y), (width, y)], fill=(shade, shade + 30, 40))
    for index in range(12):
        left = (index * 37 * width) // 400 % width
        draw.rectangle([left, height * 3 // 8 - (index % 3) * height // 16,
                        left + max(2, width // 60), height * 5 // 8], fill=(70, 50, 30))
    if marker:
        r = max(3, width // 64)
        draw.ellipse([width / 2.0 - r, height / 2.0 - r, width / 2.0 + r, height / 2.0 + r],
                     fill=RED)
    return image


def night_panorama(size=(1024, 512), marker=False):
    """A stitched 2:1 night panorama: near-black sky and ground, a lit band
    of buildings along the horizon. Its corners are as dark as a
    dual-fisheye frame's. With marker=True a red disc sits in the middle.
    """
    width, height = size
    image = Image.new("RGB", size, (12, 12, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, height * 5 // 16, width, height * 11 // 16], fill=(150, 130, 90))
    for index in range(24):
        left = index * width // 24
        draw.rectangle([left, height * 6 // 16, left + width // 60, height * 10 // 16],
                       fill=(230, 210, 120) if index % 2 else (60, 50, 40))
    if marker:
        r = max(3, width // 64)
        draw.ellipse([width / 2.0 - r, height / 2.0 - r, width / 2.0 + r, height / 2.0 + r],
                     fill=RED)
    return image


def gpano_xmp(projection="equirectangular", form="element", cropped=None, full=None):
    """An XMP packet with GPano tags, in element or attribute form.

    cropped and full are (width, height) for the CroppedAreaImage*/FullPano*
    pixel counts (left out when None).
    """
    values = [("ProjectionType", projection), ("UsePanoramaViewer", "True")]
    if cropped:
        values += [("CroppedAreaImageWidthPixels", cropped[0]),
                   ("CroppedAreaImageHeightPixels", cropped[1])]
    if full:
        values += [("FullPanoWidthPixels", full[0]), ("FullPanoHeightPixels", full[1])]
    if form == "attribute":
        body = ("<rdf:Description rdf:about='' "
                "xmlns:GPano='http://ns.google.com/photos/1.0/panorama/' %s/>"
                % " ".join("GPano:%s=\"%s\"" % pair for pair in values))
    else:
        body = ("<rdf:Description rdf:about='' "
                "xmlns:GPano='http://ns.google.com/photos/1.0/panorama/'>%s</rdf:Description>"
                % "".join("<GPano:%s>%s</GPano:%s>" % (name, value, name)
                          for name, value in values))
    return ("<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>"
            "<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF "
            "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>%s</rdf:RDF>"
            "</x:xmpmeta><?xpacket end='w'?>" % body).encode("utf-8")


def jpeg_with_xmp(image, xmp, exif=None, quality=90):
    """JPEG bytes with an XMP packet in its own APP1 segment (and optional EXIF)."""
    data = jpeg_bytes(image, exif, quality)
    payload = XMP_HEADER + xmp
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return data[:2] + segment + data[2:]


def write_bytes(directory, name, data):
    """Write bytes to directory/name; returns the path."""
    return _write(directory, name, data)
