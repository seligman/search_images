#!/usr/bin/env python3
"""Tests for images.py: reading originals, EXIF cleaning, capture dates, GPS,
camera names, LLM sizing, browser conversion and the dHash.

Every image is synthesized by tests/imagegen.py in a temporary directory.

Run with:  python -m unittest discover -s tests
"""

import concurrent.futures
import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
for _path in (REPO, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from PIL import Image, ImageDraw                        # noqa: E402
from PIL.TiffImagePlugin import IFDRational             # noqa: E402

import imagegen                                         # noqa: E402
import imageinfo                                        # noqa: E402
import images                                           # noqa: E402

RESULT_KEYS = {
    "width", "height", "thumb", "preview", "exif", "dhash", "content_hash",
    "mime", "taken_at", "taken_source", "tz_offset", "camera", "lat", "lon",
    "projection", "pano",
}

_tmp = None


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory(prefix="search-images-test-")


def tearDownModule():
    _tmp.cleanup()


def new_dir():
    return tempfile.mkdtemp(dir=_tmp.name)


def decode(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def is_red(pixel):
    return pixel[0] > 180 and pixel[1] < 80 and pixel[2] < 80


def is_blue(pixel):
    return pixel[0] < 80 and pixel[1] < 80 and pixel[2] > 180


def is_white(pixel):
    return min(pixel[:3]) > 235


def corners(image):
    """Pixels just inside the four corners: top-left, top-right, bottom-left, bottom-right."""
    width, height = image.size
    image = image.convert("RGB")
    return (image.getpixel((2, 2)), image.getpixel((width - 3, 2)),
            image.getpixel((2, height - 3)), image.getpixel((width - 3, height - 3)))


def reference_dhash(image):
    """The original compute_dhash algorithm (getdata replaced by getpixel)."""
    small = image.convert("L").resize((9, 8), Image.LANCZOS)
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = small.getpixel((col, row)), small.getpixel((col + 1, row))
            bits = (bits << 1) | (1 if left > right else 0)
    return "%016x" % bits


def strict_dumps(value):
    """json.dumps that refuses NaN/Infinity, like the database writers."""
    return json.dumps(value, allow_nan=False)


def local_time(seconds):
    return datetime.datetime.fromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S")


class FakeStat(object):
    def __init__(self, mtime):
        self.st_size = 0
        self.st_mtime = mtime
        self.st_ctime = mtime


class BytesHelper(object):
    """A helper module stand-in that serves bytes for made-up paths."""

    def __init__(self, files, mtime=None):
        self.files = files
        self.mtime = mtime
        self.loaded = []

    def load_image(self, path):
        self.loaded.append(path)
        return self.files.get(path)

    def stat(self, path):
        return FakeStat(self.mtime) if self.mtime is not None else None


class ReadImageJpegTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = new_dir()
        cls.photo = imagegen.jpeg_with_exif(
            cls.dir, "photo.jpg", gps=(37.7749, -122.4194))

    def test_returns_exactly_the_documented_keys(self):
        result = images.read_image(self.photo)
        self.assertEqual(set(result), RESULT_KEYS)

    def test_basic_fields(self):
        with open(self.photo, "rb") as handle:
            data = handle.read()
        result = images.read_image(self.photo)
        self.assertEqual((result["width"], result["height"]), (64, 48))
        self.assertEqual(result["content_hash"], hashlib.sha1(data).hexdigest())
        self.assertEqual(result["mime"], "image/jpeg")
        self.assertRegex(result["dhash"], r"^[0-9a-f]{16}$")

    def test_exif_derived_fields(self):
        result = images.read_image(self.photo)
        self.assertEqual(result["taken_at"], "2019-05-14T14:23:01")
        self.assertEqual(result["taken_source"], "exif")
        self.assertEqual(result["tz_offset"], "+02:00")
        self.assertEqual(result["camera"], "Apple iPhone 12")
        self.assertAlmostEqual(result["lat"], 37.7749, places=4)
        self.assertAlmostEqual(result["lon"], -122.4194, places=4)
        exif = result["exif"]
        self.assertEqual(exif["Make"], "Apple")
        self.assertEqual(exif["DateTimeOriginal"], "2019:05:14 14:23:01")
        self.assertEqual(exif["GPS"]["GPSLatitudeRef"], "N")
        self.assertEqual(exif["GPS"]["GPSLongitudeRef"], "W")
        strict_dumps(exif)

    def test_gps_southern_eastern_hemisphere(self):
        path = imagegen.jpeg_with_exif(self.dir, "south.jpg", gps=(-33.8568, 151.2153))
        result = images.read_image(path)
        self.assertAlmostEqual(result["lat"], -33.8568, places=4)
        self.assertAlmostEqual(result["lon"], 151.2153, places=4)

    def test_thumbnail_and_preview_are_plain_jpegs(self):
        result = images.read_image(self.photo)
        for key in ("thumb", "preview"):
            image = decode(result[key])
            self.assertEqual(image.format, "JPEG", key)
            self.assertEqual(image.mode, "RGB", key)
            self.assertEqual(len(image.getexif()), 0, key + " must not carry EXIF")

    def test_small_images_are_not_upscaled(self):
        result = images.read_image(self.photo)
        self.assertEqual(decode(result["preview"]).size, (64, 48))
        self.assertEqual(decode(result["thumb"]).size, (64, 48))

    def test_orientation_is_applied_and_dimensions_swapped(self):
        # Orientation 6 = rotate 90 degrees clockwise to display: the red
        # top-left marker of the stored pixels ends up top-right.
        # Orientation 8 = rotate counter-clockwise: it ends up bottom-left.
        expected_red = {6: 1, 8: 2, 3: 3, 1: 0}
        for orientation, red_corner in expected_red.items():
            with self.subTest(orientation=orientation):
                path = imagegen.jpeg_with_exif(
                    self.dir, "orient%d.jpg" % orientation, orientation=orientation)
                result = images.read_image(path)
                swapped = orientation in (6, 8)
                self.assertEqual((result["width"], result["height"]),
                                 (48, 64) if swapped else (64, 48))
                for key in ("thumb", "preview"):
                    image = decode(result[key])
                    self.assertEqual(image.size, (result["width"], result["height"]))
                    pixels = corners(image)
                    self.assertTrue(is_red(pixels[red_corner]), (key, pixels))
                    for index, pixel in enumerate(pixels):
                        if index != red_corner:
                            self.assertTrue(is_blue(pixel), (key, index, pixels))
                self.assertEqual(result["exif"]["Orientation"], orientation)

    def test_large_jpeg_preview_and_thumb_sizes(self):
        for orientation, size in ((None, (4000, 3000)), (6, (3000, 4000))):
            with self.subTest(orientation=orientation):
                path = imagegen.large_jpeg(self.dir, "large%s.jpg" % orientation,
                                           orientation=orientation)
                result = images.read_image(path)
                self.assertEqual((result["width"], result["height"]), size)
                preview = decode(result["preview"])
                thumb = decode(result["thumb"])
                if orientation:
                    self.assertEqual(preview.size, (1536, 2048))
                    self.assertEqual(thumb.size, (240, 320))
                    self.assertTrue(is_red(corners(preview)[1]))
                else:
                    self.assertEqual(preview.size, (2048, 1536))
                    self.assertEqual(thumb.size, (320, 240))
                    self.assertTrue(is_red(corners(preview)[0]))

    def test_large_jpeg_dhash_matches_full_decode(self):
        # Draft decoding of big JPEGs must not change the hash noticeably.
        path = imagegen.large_jpeg(self.dir, "large-hash.jpg",
                                   image=imagegen.blocks_image(block=512))
        with Image.open(path) as image:
            full = reference_dhash(image)
        fast = images.read_image(path)["dhash"]
        distance = bin(int(full, 16) ^ int(fast, 16)).count("1")
        self.assertLessEqual(distance, 2)

    def test_custom_preview_edge(self):
        path = imagegen.large_jpeg(self.dir, "edge.jpg", size=(1200, 800))
        result = images.read_image(path, preview_edge=600)
        self.assertEqual(decode(result["preview"]).size, (600, 400))
        self.assertEqual(decode(result["thumb"]).size, (320, 213))

    def test_want_preview_false(self):
        path = imagegen.large_jpeg(self.dir, "nopreview.jpg", size=(3000, 2000))
        with_preview = images.read_image(path)
        without = images.read_image(path, want_preview=False)
        self.assertIsNone(without["preview"])
        self.assertEqual(without["dhash"], with_preview["dhash"])
        self.assertEqual(without["thumb"], with_preview["thumb"])

    def test_mtime_fallback_uses_argument_or_file(self):
        path = imagegen.jpeg_with_exif(self.dir, "IMG_1234.jpg", date=None, offset=None)
        stamp = 1557843781.0
        result = images.read_image(path, mtime=stamp)
        self.assertEqual((result["taken_at"], result["taken_source"], result["tz_offset"]),
                         (local_time(stamp), "mtime", ""))
        os.utime(path, (stamp + 3600, stamp + 3600))
        result = images.read_image(path)
        self.assertEqual(result["taken_at"], local_time(stamp + 3600))
        self.assertEqual(result["taken_source"], "mtime")

    def test_filename_date_beats_mtime(self):
        path = imagegen.jpeg_with_exif(self.dir, "IMG_20190514_142301.jpg",
                                       date=None, offset=None)
        result = images.read_image(path, mtime=0)
        self.assertEqual(result["taken_at"], "2019-05-14T14:23:01")
        self.assertEqual(result["taken_source"], "filename")

    def test_helper_supplies_bytes_and_stat(self):
        with open(self.photo, "rb") as handle:
            data = handle.read()
        fake_path = os.path.join(self.dir, "nowhere", "IMG_9999.jpg")
        helper = BytesHelper({fake_path: imagegen.jpeg_bytes(imagegen.marker_image())},
                             mtime=1557843781.0)
        result = images.read_image(fake_path, helper)
        self.assertEqual(helper.loaded, [fake_path])
        self.assertEqual(result["taken_source"], "mtime")
        self.assertEqual(result["taken_at"], local_time(1557843781.0))
        helper = BytesHelper({fake_path: data})
        self.assertEqual(images.read_image(fake_path, helper)["camera"], "Apple iPhone 12")

    def test_helper_returning_none_reads_the_file(self):
        helper = BytesHelper({})
        result = images.read_image(self.photo, helper)
        self.assertEqual(helper.loaded, [self.photo])
        self.assertEqual(result["camera"], "Apple iPhone 12")


class ReadImageFormatsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = new_dir()

    def test_16bit_png_is_scaled_not_white(self):
        path = imagegen.png16_gray(self.dir, "flat16.png", value=30000)
        result = images.read_image(path)
        thumb = decode(result["thumb"]).convert("L")
        low, high = thumb.getextrema()
        self.assertTrue(105 <= low <= high <= 130, (low, high))   # 30000/257 = 117
        self.assertEqual(result["mime"], "image/png")

    def test_16bit_ramp_keeps_detail(self):
        rising = images.read_image(imagegen.png16_gray(self.dir, "ramp16.png"))
        falling = images.read_image(imagegen.png16_gray(self.dir, "ramp16r.png",
                                                        reverse=True))
        self.assertEqual(rising["dhash"], "0000000000000000")
        self.assertEqual(falling["dhash"], "ffffffffffffffff")
        thumb = decode(rising["thumb"]).convert("L")
        self.assertLess(thumb.getpixel((2, 2)), 30)
        self.assertGreater(thumb.getpixel((thumb.width - 3, 2)), 225)

    def test_rgba_transparency_becomes_white(self):
        result = images.read_image(imagegen.rgba_png(self.dir))
        pixels = corners(decode(result["thumb"]))
        self.assertTrue(is_red(pixels[0]), pixels)
        self.assertTrue(is_white(pixels[1]), pixels)

    def test_palette_transparency_becomes_white(self):
        result = images.read_image(imagegen.palette_png_transparent(self.dir))
        pixels = corners(decode(result["preview"]))
        self.assertTrue(is_red(pixels[0]), pixels)
        self.assertTrue(is_white(pixels[1]), pixels)

    def test_cmyk_jpeg(self):
        result = images.read_image(imagegen.cmyk_jpeg(self.dir))
        thumb = decode(result["thumb"])
        self.assertEqual(thumb.mode, "RGB")
        self.assertTrue(is_red(thumb.getpixel((10, 10))), thumb.getpixel((10, 10)))
        self.assertEqual(result["mime"], "image/jpeg")

    def test_animated_gif_uses_first_frame(self):
        result = images.read_image(imagegen.animated_gif(self.dir))
        self.assertEqual(result["mime"], "image/gif")
        self.assertEqual((result["width"], result["height"]), (48, 48))
        self.assertTrue(is_red(decode(result["thumb"]).getpixel((24, 24))))

    def test_content_decides_mime_not_extension(self):
        result = images.read_image(imagegen.jpeg_named_png(self.dir))
        self.assertEqual(result["mime"], "image/jpeg")
        self.assertEqual((result["width"], result["height"]), (64, 48))

    def test_tall_screenshot(self):
        path = imagegen.screenshot_png(self.dir)
        result = images.read_image(path)
        self.assertEqual((result["width"], result["height"]), (1290, 2796))
        self.assertEqual(decode(result["preview"]).size, (945, 2048))
        self.assertEqual(decode(result["thumb"]).size, (148, 320))
        self.assertEqual(result["taken_at"], "2019-05-14T14:23:01")
        self.assertEqual(result["taken_source"], "filename")
        self.assertEqual(result["exif"], {})

    def test_other_formats(self):
        cases = (("x.webp", "WEBP", "image/webp"), ("x.tif", "TIFF", "image/tiff"),
                 ("x.bmp", "BMP", "image/bmp"), ("x.png", "PNG", "image/png"))
        for name, format_name, mime in cases:
            with self.subTest(format=format_name):
                result = images.read_image(imagegen.save_as(self.dir, name, format_name))
                self.assertEqual(result["mime"], mime)
                self.assertEqual((result["width"], result["height"]), (64, 48))
                self.assertTrue(is_red(corners(decode(result["thumb"]))[0]))

    def test_heic(self):
        path = imagegen.heic_file(self.dir, orientation=6)
        if path is None:
            self.skipTest("pillow-heif cannot write HEIC here")
        result = images.read_image(path)
        self.assertEqual(result["mime"], "image/heic")
        self.assertEqual((result["width"], result["height"]), (48, 64))
        self.assertEqual(result["camera"], "Apple iPhone 12")
        self.assertEqual(result["taken_at"], "2019-05-14T14:23:01")
        pixels = corners(decode(result["thumb"]))
        self.assertTrue(is_red(pixels[1]), pixels)
        strict_dumps(result["exif"])

    def test_avif(self):
        path = imagegen.avif_file(self.dir, orientation=6)
        if path is None:
            self.skipTest("this Pillow cannot write AVIF")
        result = images.read_image(path)
        self.assertEqual(result["mime"], "image/avif")
        self.assertEqual((result["width"], result["height"]), (48, 64))
        self.assertTrue(is_red(corners(decode(result["thumb"]))[1]))


class ReadImageErrorsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = new_dir()

    def assertReadError(self, path, fragment, error_class=images.ImageReadError):
        with self.assertRaises(error_class) as caught:
            images.read_image(path)
        self.assertIn(fragment, str(caught.exception))

    def test_not_an_image(self):
        self.assertReadError(imagegen.corrupt_file(self.dir), "not a supported image")

    def test_truncated(self):
        self.assertReadError(imagegen.truncated_jpeg(self.dir), "truncated")

    def test_empty(self):
        self.assertReadError(imagegen.empty_file(self.dir), "empty")

    def test_missing_file_is_a_source_error(self):
        path = os.path.join(self.dir, "missing.jpg")
        self.assertReadError(path, "cannot read file", images.SourceReadError)
        self.assertTrue(issubclass(images.SourceReadError, images.ImageReadError))

    def test_decompression_bomb(self):
        path = imagegen.jpeg_with_exif(self.dir, "bomb.jpg")    # 64 x 48 = 3072 pixels
        saved = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = 1000                          # error above 2000
        try:
            self.assertReadError(path, "too large")
            with self.assertRaises(images.ImageReadError):
                with open(path, "rb") as handle:
                    images.prepare_for_llm(handle.read())
        finally:
            Image.MAX_IMAGE_PIXELS = saved


class ExifSanitizingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = new_dir()

    def test_zero_denominator_rationals(self):
        result = images.read_image(imagegen.jpeg_zero_rationals(self.dir))
        exif = result["exif"]
        strict_dumps(exif)
        self.assertNotIn("FNumber", exif)
        self.assertNotIn("ExposureBiasValue", exif)
        self.assertNotIn("GPSAltitude", exif["GPS"])
        self.assertNotIn("GPSLatitude", exif["GPS"])
        self.assertEqual((result["lat"], result["lon"]), (None, None))
        self.assertEqual(result["camera"], "Apple iPhone 12")

    def test_clean_exif_value(self):
        cases = (
            (IFDRational(0, 0), None),
            (IFDRational(1, 3), 0.333333),
            (IFDRational(10, 2), 5),
            (float("nan"), None),
            (float("inf"), None),
            (-float("inf"), None),
            (2.5, 2.5),
            (7, 7),
            (True, True),
            (None, None),
            (b"Canon\x00\x00", "Canon"),
            (b"ASCII\x00\x00\x00GPS", "GPS"),
            (b"\x02\x02\x00\x00", [2, 2, 0, 0]),
            (b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09", None),
            ("  Phone\x00 ", "Phone"),
            ("bad \udc80 surrogate", "bad ? surrogate"),
            ((IFDRational(1, 2), IFDRational(0, 0)), [0.5, None]),
            ({"inner": float("nan"), "ok": 1}, {"ok": 1}),
        )
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                cleaned = imageinfo.clean_exif_value(value)
                self.assertEqual(cleaned, expected)
                strict_dumps(cleaned)

    def test_long_text_is_capped(self):
        self.assertEqual(len(imageinfo.clean_exif_value("x" * 10000)), imageinfo.MAX_TEXT)

    def test_sanitize_legacy_json_with_nan(self):
        # Rows written by older versions contain bare NaN tokens.
        legacy = json.loads('{"FNumber": NaN, "Model": "X", "Flash": 16,'
                            ' "GPS": {"GPSAltitude": NaN, "GPSLatitude": [NaN, NaN, NaN],'
                            ' "GPSLongitude": [1, Infinity, 2]}}')
        cleaned = images.sanitize_exif(legacy)
        self.assertEqual(cleaned, {"Model": "X", "Flash": 16,
                                   "GPS": {"GPSLongitude": [1, None, 2]}})
        strict_dumps(cleaned)
        self.assertEqual(images.sanitize_exif(None), {})
        self.assertEqual(images.sanitize_exif("text"), {})

    def test_extract_exif_compat(self):
        path = imagegen.jpeg_with_exif(self.dir, "compat.jpg")
        with Image.open(path) as image:
            exif = images.extract_exif(image)
        self.assertEqual(exif["Model"], "iPhone 12")
        self.assertEqual(exif["OffsetTimeOriginal"], "+02:00")
        with Image.open(imagegen.rgba_png(self.dir)) as image:
            self.assertEqual(images.extract_exif(image), {})

    def test_every_fixture_gives_strict_json(self):
        directory = new_dir()
        paths = [
            imagegen.jpeg_with_exif(directory, gps=(1.5, -2.5), orientation=8),
            imagegen.jpeg_zero_rationals(directory),
            imagegen.png16_gray(directory),
            imagegen.rgba_png(directory),
            imagegen.palette_png_transparent(directory),
            imagegen.cmyk_jpeg(directory),
            imagegen.animated_gif(directory),
            imagegen.jpeg_named_png(directory),
            imagegen.heic_file(directory),
            imagegen.avif_file(directory),
        ]
        for path in paths:
            if path is None:
                continue
            with self.subTest(path=os.path.basename(path)):
                result = images.read_image(path)
                strict_dumps(result["exif"])
                strict_dumps(dict((k, v) for k, v in result.items()
                                 if not isinstance(v, bytes)))


class CaptureDateTest(unittest.TestCase):

    def check(self, exif, filename, mtime, expected):
        self.assertEqual(images.parse_capture_date(exif, filename, mtime), expected,
                         (exif, filename, mtime))

    def test_exif_variants(self):
        stamp = 1557843781.0
        cases = (
            ({"DateTimeOriginal": "2019:05:14 14:23:01", "OffsetTimeOriginal": "+02:00"},
             ("2019-05-14T14:23:01", "exif", "+02:00")),
            ({"DateTimeOriginal": "2019:05:14 14:23:01", "OffsetTimeOriginal": "-0530"},
             ("2019-05-14T14:23:01", "exif", "-05:30")),
            ({"DateTimeOriginal": "2019:05:14 14:23:01", "OffsetTimeOriginal": "junk"},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14 14:23:01", "OffsetTimeOriginal": "+99:00"},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14 14:23:01"},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": "2019-05-14 14:23:01"},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": "2019-05-14T14:23:01"},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14 14:23"},
             ("2019-05-14T14:23:00", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14"},
             ("2019-05-14T00:00:00", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14 14:23:01\x00\x00 "},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": b"2019:05:14 14:23:01\x00"},
             ("2019-05-14T14:23:01", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14 14:23:01.123+02:00"},
             ("2019-05-14T14:23:01", "exif", "+02:00")),
            ({"DateTimeOriginal": "0000:00:00 00:00:00",
              "DateTimeDigitized": "2018:01:02 03:04:05", "OffsetTimeDigitized": "-03:00",
              "OffsetTimeOriginal": "+09:00"},
             ("2018-01-02T03:04:05", "exif", "-03:00")),
            ({"DateTimeOriginal": "    :  :     :  :  ", "DateTime": "2017:06:07 08:09:10",
              "OffsetTime": "+01:00"},
             ("2017-06-07T08:09:10", "exif", "+01:00")),
            ({"DateTimeOriginal": "2019:02:30 10:00:00", "DateTime": "2017:06:07 08:09:10"},
             ("2017-06-07T08:09:10", "exif", "")),
            ({"DateTimeOriginal": "2019:05:14 25:00:00"},
             (local_time(stamp), "mtime", "")),
            ({"DateTimeOriginal": "1900:01:01 00:00:00"},
             ("1900-01-01T00:00:00", "exif", "")),
            ({"DateTimeOriginal": "2100:12:31 23:59:59"},
             ("2100-12-31T23:59:59", "exif", "")),
            ({"DateTimeOriginal": "1850:01:01 00:00:00"},
             (local_time(stamp), "mtime", "")),
            ({"DateTimeOriginal": "2150:01:01 00:00:00"},
             (local_time(stamp), "mtime", "")),
            ({"DateTimeOriginal": 12345}, (local_time(stamp), "mtime", "")),
            ({"DateTimeOriginal": ["2019:05:14 14:23:01"]}, (local_time(stamp), "mtime", "")),
            ({}, (local_time(stamp), "mtime", "")),
            (None, (local_time(stamp), "mtime", "")),
        )
        for exif, expected in cases:
            with self.subTest(exif=exif):
                self.check(exif, "IMG_1234.JPG", stamp, expected)

    def test_rejected_exif_year_falls_back_to_filename(self):
        self.check({"DateTimeOriginal": "1850:01:01 00:00:00"},
                   "IMG_20190514_142301.jpg", 0,
                   ("2019-05-14T14:23:01", "filename", ""))

    def test_filename_patterns(self):
        cases = (
            ("IMG_20190514_142301.jpg", "2019-05-14T14:23:01"),
            ("IMG_20190514_142301_1.jpg", "2019-05-14T14:23:01"),
            ("PXL_20210704_183015123.jpg", "2021-07-04T18:30:15"),
            ("PXL_20210704_183015123.MP.jpg", "2021-07-04T18:30:15"),
            ("2019-05-14 14.23.01.jpg", "2019-05-14T14:23:01"),
            ("2019-05-14 14.23.jpg", "2019-05-14T14:23:00"),
            ("20190514_142301.jpg", "2019-05-14T14:23:01"),
            ("20190514_1423.jpg", "2019-05-14T14:23:00"),
            ("Screenshot_2019-05-14-14-23-01.png", "2019-05-14T14:23:01"),
            ("Screenshot_20190514-142301_Chrome.jpg", "2019-05-14T14:23:01"),
            ("Screenshot 2019-05-14 at 14.23.01.png", "2019-05-14T14:23:01"),
            ("Screen Shot 2019-05-14 at 2.23.01 PM.png", "2019-05-14T14:23:01"),
            ("Screenshot 2023-05-14 at 12.05.09\u202fAM.png", "2023-05-14T00:05:09"),
            ("WhatsApp Image 2019-05-14 at 14.23.01.jpeg", "2019-05-14T14:23:01"),
            ("WhatsApp Image 2019-05-14 at 14.23.01 (1).jpeg", "2019-05-14T14:23:01"),
            ("IMG-20190514-WA0001.jpg", "2019-05-14T00:00:00"),
            ("2019-05-14.jpg", "2019-05-14T00:00:00"),
            ("1999-12-31 23.59.59.jpg", "1999-12-31T23:59:59"),
            ("photo 20190514T142301.jpg", "2019-05-14T14:23:01"),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                self.check({}, name, None, (expected, "filename", ""))
        # Full paths work too; only the file name is looked at.
        self.check({}, os.path.join("2001-01-01 folder", "IMG_20190514_142301.jpg"), None,
                   ("2019-05-14T14:23:01", "filename", ""))

    def test_names_without_a_valid_date(self):
        names = (
            "IMG_1234.JPG", "DSC_0001.jpg", "IMG_20191332_101010.jpg",
            "photo_20190230.jpg", "18500101_120000.jpg", "123456789012345.jpg",
            "Screenshot 2019-05-14 at 13.23.01 PM.png", "", None,
        )
        for name in names:
            with self.subTest(name=name):
                self.check({}, name, None, ("", "", ""))

    def test_mtime_fallback(self):
        stamp = 1557843781.75
        self.check({}, "IMG_1234.JPG", stamp, (local_time(stamp), "mtime", ""))
        self.check({}, "IMG_1234.JPG", int(stamp), (local_time(int(stamp)), "mtime", ""))
        for bad in (None, float("nan"), float("inf"), "soon", 1e12, -1e12):
            with self.subTest(mtime=bad):
                self.check({}, "IMG_1234.JPG", bad, ("", "", ""))

    def test_pre_1970_mtime_does_not_crash(self):
        # Windows cannot convert negative timestamps to local time.
        taken_at, source, offset = images.parse_capture_date({}, "x.jpg", -315619200.0)
        self.assertEqual(source, "mtime")
        self.assertTrue(taken_at.startswith(("1959-12-31", "1960-01-01")), taken_at)
        self.assertEqual(offset, "")

    def test_normalize_offset(self):
        cases = (("+02:00", "+02:00"), ("+0200", "+02:00"), ("-05:30", "-05:30"),
                 ("Z", "+00:00"), (b"+01:00\x00", "+01:00"), ("", ""), (None, ""),
                 ("2:00", ""), ("+15:00", ""), ("+02:60", ""), (3, ""))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(imageinfo.normalize_offset(value), expected)


class GpsTest(unittest.TestCase):

    def test_conversions(self):
        lat_dms = [37, 46, 29.64]
        lon_dms = [122, 25, 9.84]
        cases = (
            ({"GPSLatitudeRef": "N", "GPSLatitude": lat_dms,
              "GPSLongitudeRef": "W", "GPSLongitude": lon_dms}, (37.7749, -122.4194)),
            ({"GPSLatitudeRef": "S", "GPSLatitude": lat_dms,
              "GPSLongitudeRef": "E", "GPSLongitude": lon_dms}, (-37.7749, 122.4194)),
            ({"GPSLatitudeRef": "s", "GPSLatitude": lat_dms,
              "GPSLongitudeRef": b"W\x00", "GPSLongitude": lon_dms}, (-37.7749, -122.4194)),
            ({"GPSLatitude": lat_dms, "GPSLongitude": lon_dms}, (37.7749, 122.4194)),
            ({"GPSLatitudeRef": "N", "GPSLatitude": [37, 46.494],
              "GPSLongitudeRef": "W", "GPSLongitude": [122.4194]}, (37.7749, -122.4194)),
            ({"GPSLatitudeRef": "N", "GPSLatitude": [37, 46.494, None],
              "GPSLongitudeRef": "E", "GPSLongitude": [122, None, None]}, (37.7749, 122.0)),
            ({"GPSLatitudeRef": "S", "GPSLatitude": 12.5,
              "GPSLongitudeRef": "W", "GPSLongitude": "44.25"}, (-12.5, -44.25)),
            ({1: "N", 2: (IFDRational(37, 1), IFDRational(46, 1), IFDRational(2964, 100)),
              3: "W", 4: (IFDRational(122, 1), IFDRational(25, 1), IFDRational(984, 100))},
             (37.7749, -122.4194)),
            ({"GPSLatitudeRef": "N", "GPSLatitude": ((37, 1), (46, 1), (2964, 100)),
              "GPSLongitudeRef": "W", "GPSLongitude": ((122, 1), (25, 1), (984, 100))},
             (37.7749, -122.4194)),
            ({"GPSLatitudeRef": "X", "GPSLatitude": lat_dms,
              "GPSLongitude": lon_dms}, (37.7749, 122.4194)),
        )
        for gps, expected in cases:
            with self.subTest(gps=gps):
                lat, lon = imageinfo.exif_gps_to_decimal(gps)
                self.assertAlmostEqual(lat, expected[0], places=4)
                self.assertAlmostEqual(lon, expected[1], places=4)

    def test_rejections(self):
        cases = (
            None, {}, "N 37", [],
            {"GPSLatitude": [0, 0, 0], "GPSLongitude": [0, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [0, 0, 0],
             "GPSLongitudeRef": "E", "GPSLongitude": [0, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [95, 0, 0],
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [10, 0, 0],
             "GPSLongitudeRef": "E", "GPSLongitude": [190, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [None, None, None],
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": (IFDRational(0, 0), 1, 2),
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [10, -5, 0],
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [1, 2, 3, 4],
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": "north",
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [float("nan"), 0, 0],
             "GPSLongitudeRef": "E", "GPSLongitude": [10, 0, 0]},
            {"GPSLatitudeRef": "N", "GPSLatitude": [10, 0, 0]},
        )
        for gps in cases:
            with self.subTest(gps=gps):
                self.assertEqual(imageinfo.exif_gps_to_decimal(gps), (None, None))


class CameraNameTest(unittest.TestCase):

    def test_names(self):
        cases = (
            ({"Make": "Apple", "Model": "iPhone 12"}, "Apple iPhone 12"),
            ({"Make": "Canon", "Model": "Canon EOS R5"}, "Canon EOS R5"),
            ({"Make": "NIKON CORPORATION", "Model": "NIKON D750"}, "NIKON D750"),
            ({"Make": "OLYMPUS IMAGING CORP.", "Model": "E-M5"}, "OLYMPUS E-M5"),
            ({"Make": "RICOH IMAGING COMPANY, LTD.", "Model": "GR III"}, "RICOH GR III"),
            ({"Make": "EASTMAN KODAK COMPANY", "Model": "KODAK EASYSHARE C813"},
             "KODAK EASYSHARE C813"),
            ({"Make": "samsung", "Model": "SM-G991B"}, "samsung SM-G991B"),
            ({"Make": "Google", "Model": "Pixel 7"}, "Google Pixel 7"),
            ({"Make": "  Apple\x00 ", "Model": "iPhone  12 "}, "Apple iPhone 12"),
            ({"Make": b"Apple\x00", "Model": "iPhone 12"}, "Apple iPhone 12"),
            ({"Model": "iPhone 12"}, "iPhone 12"),
            ({"Make": "Apple"}, "Apple"),
            ({"Make": "", "Model": ""}, ""),
            ({}, ""),
            (None, ""),
        )
        for exif, expected in cases:
            with self.subTest(exif=exif):
                self.assertEqual(imageinfo.camera_name(exif), expected)

    def test_exif_metadata(self):
        exif = {"Make": "Apple", "Model": "iPhone 12",
                "DateTimeOriginal": "2019:05:14 14:23:01", "OffsetTimeOriginal": "+02:00",
                "GPS": {"GPSLatitudeRef": "N", "GPSLatitude": [1, 30, 0],
                        "GPSLongitudeRef": "W", "GPSLongitude": [2, 15, 0]}}
        self.assertEqual(images.exif_metadata(exif, "x.jpg", None), {
            "taken_at": "2019-05-14T14:23:01", "taken_source": "exif",
            "tz_offset": "+02:00", "camera": "Apple iPhone 12",
            "lat": 1.5, "lon": -2.25})
        self.assertEqual(images.exif_metadata(None), {
            "taken_at": "", "taken_source": "", "tz_offset": "", "camera": "",
            "lat": None, "lon": None})


class PrepareForLlmTest(unittest.TestCase):

    def prepared(self, data, **options):
        out = images.prepare_for_llm(data, **options)
        image = decode(out)
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(len(image.getexif()), 0)
        return image

    def test_landscape_photo(self):
        data = imagegen.jpeg_bytes(imagegen.marker_image((4032, 3024)), quality=80)
        self.assertEqual(self.prepared(data).size, (1536, 1152))

    def test_orientation_applied(self):
        exif = imagegen.make_exif(orientation=6, gps=(10.0, 20.0))
        data = imagegen.jpeg_bytes(imagegen.marker_image((4032, 3024)), exif, quality=80)
        image = self.prepared(data)
        self.assertEqual(image.size, (1152, 1536))
        self.assertTrue(is_red(corners(image)[1]))

    def test_tall_screenshot_keeps_text_legible(self):
        path = imagegen.screenshot_png(new_dir())
        with open(path, "rb") as handle:
            image = self.prepared(handle.read())
        width, height = image.size
        self.assertGreaterEqual(min(width, height), 768)
        self.assertLessEqual(width * height, 3200000)
        self.assertEqual(image.size, (768, 1665))

    def test_very_tall_image_is_capped_by_pixels(self):
        data = imagegen.jpeg_bytes(imagegen.marker_image((1290, 12000)), quality=70)
        image = self.prepared(data)
        width, height = image.size
        self.assertLessEqual(width * height, 3200000)
        self.assertGreater(width * height, 3100000)
        self.assertAlmostEqual(float(height) / width, 12000 / 1290.0, delta=0.05)

    def test_small_image_not_upscaled(self):
        data = imagegen.jpeg_bytes(imagegen.marker_image((800, 600)))
        self.assertEqual(self.prepared(data).size, (800, 600))

    def test_alpha_png_is_flattened_on_white(self):
        with open(imagegen.rgba_png(new_dir()), "rb") as handle:
            image = self.prepared(handle.read())
        self.assertTrue(is_white(corners(image)[1]))

    def test_heic_input(self):
        path = imagegen.heic_file(new_dir(), orientation=6)
        if path is None:
            self.skipTest("pillow-heif cannot write HEIC here")
        with open(path, "rb") as handle:
            self.assertEqual(self.prepared(handle.read()).size, (48, 64))

    def test_bad_input(self):
        for data in (b"", b"garbage" * 10):
            with self.subTest(data=data[:10]):
                with self.assertRaises(images.ImageReadError):
                    images.prepare_for_llm(data)

    def test_llm_size_rules(self):
        cases = (
            ((4032, 3024), {}, (1536, 1152)),
            ((3024, 4032), {}, (1152, 1536)),
            ((2000, 2000), {}, (1536, 1536)),
            ((800, 600), {}, (800, 600)),
            ((1290, 2796), {}, (768, 1665)),
            ((2796, 1290), {}, (1665, 768)),
            ((700, 3000), {}, (700, 3000)),
            ((1290, 12000), {}, (586, 5455)),
            ((4032, 3024), {"max_edge": 1024, "min_short": 512}, (1024, 768)),
            ((4000, 3000), {"max_pixels": 1000000}, (1154, 866)),
            ((0, 0), {}, (0, 0)),
        )
        for size, options, expected in cases:
            with self.subTest(size=size, options=options):
                result = images.llm_size(size[0], size[1], **options)
                self.assertEqual(result, expected)
                self.assertLessEqual(result[0] * result[1],
                                     options.get("max_pixels", 3200000))


class BrowserJpegTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = new_dir()

    def test_orientation_and_full_size(self):
        path = imagegen.jpeg_with_exif(self.dir, "o6.jpg", orientation=6)
        image = decode(images.to_browser_jpeg(path))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (48, 64))
        self.assertTrue(is_red(corners(image)[1]))
        self.assertEqual(len(image.getexif()), 0)

    def test_tiff_and_heic(self):
        paths = [imagegen.save_as(self.dir, "x.tif", "TIFF"),
                 imagegen.heic_file(self.dir), imagegen.avif_file(self.dir)]
        for path in paths:
            if path is None:
                continue
            with self.subTest(path=os.path.basename(path)):
                image = decode(images.to_browser_jpeg(path))
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (64, 48))

    def test_helper_bytes(self):
        with open(imagegen.rgba_png(self.dir), "rb") as handle:
            helper = BytesHelper({"virtual.png": handle.read()})
        image = decode(images.to_browser_jpeg("virtual.png", helper))
        self.assertTrue(is_white(corners(image)[1]))

    def test_errors(self):
        with self.assertRaises(images.ImageReadError):
            images.to_browser_jpeg(imagegen.corrupt_file(self.dir))
        with self.assertRaises(images.SourceReadError):
            images.to_browser_jpeg(os.path.join(self.dir, "missing.heic"))


class HeifSupportTest(unittest.TestCase):

    def test_ensure_heif_is_thread_safe(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: images.ensure_heif(warn=False), range(16)))
        self.assertEqual(len(set(results)), 1)
        self.assertIsInstance(results[0], bool)


class DhashTest(unittest.TestCase):

    # compute_dhash at the commit before the rewrite gave this for blocks_image().
    BLOCKS_DHASH = "0884422110884422"

    def test_same_algorithm_as_before(self):
        image = imagegen.blocks_image()
        self.assertEqual(reference_dhash(image), self.BLOCKS_DHASH)
        self.assertEqual(images.compute_dhash(image), self.BLOCKS_DHASH)

    def test_read_image_hash_matches_for_lossless_files(self):
        directory = new_dir()
        image = imagegen.blocks_image()
        path = imagegen.save_as(directory, "blocks.png", "PNG", image=image)
        self.assertEqual(images.read_image(path)["dhash"], self.BLOCKS_DHASH)
        # A mirror image flips the comparisons.
        mirrored = image.transpose(Image.FLIP_LEFT_RIGHT)
        path = imagegen.save_as(directory, "mirror.png", "PNG", image=mirrored)
        self.assertEqual(images.read_image(path)["dhash"], reference_dhash(mirrored))
        self.assertNotEqual(reference_dhash(mirrored), self.BLOCKS_DHASH)

    def test_jpeg_hash_matches_reference_on_decoded_pixels(self):
        path = imagegen.save_as(new_dir(), "blocks.jpg", "JPEG",
                                image=imagegen.blocks_image(block=40), quality=90)
        with Image.open(path) as image:
            expected = reference_dhash(image)
        self.assertEqual(images.read_image(path)["dhash"], expected)

    def test_any_mode(self):
        gray = imagegen.blocks_image().convert("L")
        sixteen = Image.new("I;16", gray.size)
        sixteen.putdata([value * 257 for value in gray.tobytes()])
        for mode in ("L", "RGBA", "P", "CMYK", "1", "I;16"):
            with self.subTest(mode=mode):
                image = sixteen if mode == "I;16" else imagegen.blocks_image().convert(mode)
                self.assertRegex(images.compute_dhash(image), r"^[0-9a-f]{16}$")
        # 16-bit data is scaled to 8 bits, so it hashes like the 8-bit image.
        self.assertEqual(images.compute_dhash(sixteen), images.compute_dhash(gray))


class SniffMimeTest(unittest.TestCase):

    def test_formats(self):
        image = imagegen.marker_image()
        cases = []
        for format_name, mime in (("JPEG", "image/jpeg"), ("PNG", "image/png"),
                                  ("GIF", "image/gif"), ("WEBP", "image/webp"),
                                  ("TIFF", "image/tiff"), ("BMP", "image/bmp")):
            buffer = io.BytesIO()
            image.save(buffer, format_name)
            cases.append((buffer.getvalue(), mime))
        cases += [
            (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", "image/heic"),
            (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic", "image/heic"),
            (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf", "image/avif"),
            (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1avif", "image/avif"),
            (b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00mif1", "image/heif"),
            (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp41", None),
            (b"plain text", None),
            (b"", None),
        ]
        for data, mime in cases:
            with self.subTest(mime=mime, head=data[:12]):
                self.assertEqual(images.sniff_mime(data), mime)
                self.assertEqual(images.sniff_mime(data[:64]), mime)

    def test_real_heic_and_avif(self):
        directory = new_dir()
        for path, mime in ((imagegen.heic_file(directory), "image/heic"),
                           (imagegen.avif_file(directory), "image/avif")):
            if path is None:
                continue
            with open(path, "rb") as handle:
                self.assertEqual(images.sniff_mime(handle.read(64)), mime)

    def test_browser_can_display(self):
        for mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            self.assertTrue(imageinfo.browser_can_display(mime), mime)
        for mime in ("image/heic", "image/heif", "image/avif", "image/tiff", None):
            self.assertFalse(imageinfo.browser_can_display(mime), mime)


class ThreadingTest(unittest.TestCase):

    def test_parallel_reads_match_sequential(self):
        directory = new_dir()
        paths = [
            imagegen.jpeg_with_exif(directory, gps=(1.0, 2.0)),
            imagegen.png16_gray(directory),
            imagegen.rgba_png(directory),
            imagegen.cmyk_jpeg(directory),
            imagegen.animated_gif(directory),
            imagegen.heic_file(directory),
        ]
        paths = [path for path in paths if path is not None] * 4
        sequential = [images.read_image(path) for path in paths]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            parallel = list(pool.map(images.read_image, paths))
        for one, other in zip(sequential, parallel):
            self.assertEqual(one, other)


class DecodeBudgetTest(unittest.TestCase):

    def test_budget_makes_large_decodes_wait(self):
        budget = images._PixelBudget()
        order = []
        with mock.patch.object(images, "DECODE_PIXEL_BUDGET", 100):
            first = threading.Event()

            def second():
                with budget.hold(60):
                    order.append("second")

            with budget.hold(60):
                order.append("first")
                thread = threading.Thread(target=second)
                thread.start()
                first.wait(0.2)
                self.assertEqual(order, ["first"])      # still waiting
            thread.join(5)
            self.assertEqual(order, ["first", "second"])
            # One image larger than the whole budget is still decoded (alone).
            with budget.hold(10 ** 9):
                order.append("huge")
            # Small ones fit next to each other.
            with budget.hold(40), budget.hold(40):
                order.append("both")
        self.assertEqual(order[-2:], ["huge", "both"])

    def test_reads_count_the_pixels_actually_decoded(self):
        directory = new_dir()
        path = imagegen.large_jpeg(directory, size=(4400, 3300))
        held = []
        real_hold = images._decode_budget.hold

        def recording_hold(pixels):
            held.append(pixels)
            return real_hold(pixels)

        with open(path, "rb") as handle:
            data = handle.read()
        with mock.patch.object(images._decode_budget, "hold", recording_hold):
            images.read_image(path)
            images.prepare_for_llm(data)
        # JPEGs are decoded at a reduced scale when the output is small.
        self.assertEqual(len(held), 2)
        self.assertLess(held[0], 4400 * 3300)
        self.assertGreaterEqual(held[0], 2048 * 1536)


class PreviewFormatTest(unittest.TestCase):

    def graphic_png(self, directory, size=(900, 700)):
        image = Image.new("RGB", size, (255, 255, 255))
        draw = ImageDraw.Draw(image)
        for top in range(10, size[1] - 20, 24):
            draw.rectangle([20, top, 20 + (top * 13) % (size[0] - 40), top + 9],
                           fill=(20, 20, 20))
        path = os.path.join(directory, "drawing.png")
        image.save(path, "PNG")
        return path

    def test_graphic_png_gets_a_small_png_preview(self):
        # A JPEG preview of a screenshot or drawing can be many times the
        # size of the PNG original; the smaller format is kept.
        path = self.graphic_png(new_dir())
        result = images.read_image(path)
        preview = result["preview"]
        self.assertEqual(images.sniff_mime(preview), "image/png")
        self.assertLess(len(preview), len(images._encode_jpeg(decode(preview).convert("RGB"),
                                                              images.PREVIEW_QUALITY)))
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.size, (900, 700))
            self.assertEqual(image.mode, "RGB")

    def test_photos_keep_jpeg_previews(self):
        directory = new_dir()
        for path in (imagegen.large_jpeg(directory, size=(2600, 1300)),
                     imagegen.jpeg_with_exif(directory)):
            self.assertEqual(images.sniff_mime(images.read_image(path)["preview"]),
                             "image/jpeg")


class ModuleImportTest(unittest.TestCase):

    def run_python(self, code, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-c", code, REPO] + list(args),
                                cwd=REPO, env=env, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_no_optional_imports_at_import_time(self):
        out = self.run_python(
            "import sys; sys.path.insert(0, sys.argv[1]); import images; "
            "print(','.join(m for m in ('pillow_heif', 'numpy') if m in sys.modules))")
        self.assertEqual(out.strip(), "")

    def test_heic_without_pillow_heif(self):
        path = imagegen.heic_file(new_dir())
        if path is None:
            self.skipTest("pillow-heif cannot write HEIC here")
        out = self.run_python(
            "import sys\n"
            "sys.modules['pillow_heif'] = None\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import images\n"
            "try:\n"
            "    images.read_image(sys.argv[2])\n"
            "except images.ImageReadError as error:\n"
            "    print('error:', error)\n"
            "print('state:', images.ensure_heif(warn=False))\n", path)
        self.assertIn("error: HEIC/HEIF images need pillow-heif", out)
        self.assertIn("state: False", out)

    def test_avif_without_decoder(self):
        path = imagegen.avif_file(new_dir())
        if path is None:
            self.skipTest("this Pillow cannot write AVIF")
        out = self.run_python(
            "import sys\n"
            "sys.modules['PIL.AvifImagePlugin'] = None\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import images\n"
            "try:\n"
            "    images.read_image(sys.argv[2])\n"
            "except images.ImageReadError as error:\n"
            "    print('error:', error)\n", path)
        self.assertIn("error: AVIF images need a Pillow with AVIF support", out)


if __name__ == "__main__":
    unittest.main()
