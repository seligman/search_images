"""Tests for camera RAW files (raw_photos.py).

A RAW file is read without a RAW decoder: its largest JPEG preview shows
it, and its EXIF comes from its own TIFF directories (DNG, ARW, CR2, NEF,
ORF, RW2, ...), RAF header or CR3 boxes. One beside a picture of the same
shot (the same name in its folder, or the same camera and second) is that
picture's RAW original: scan.py pairs them, the server shows and (for the
owner) serves it, and an export publishes it only with its raw_files
option. The pages' part runs under Node.js (tests/js/raw_test.js; skipped
when node is not installed). Every RAW file here is made up by
tests/rawgen.py.
"""

import io
import json
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import warnings
from unittest import mock

import support

import common
import db
import imageinfo
import jobs
import raw_photos

try:
    from PIL import Image
    import imagegen
    import images
    import rawgen
except ImportError:                     # (Pillow is not installed)
    Image = imagegen = images = rawgen = None

try:
    import flask
    import serve
    import test_serve
except ImportError:
    flask = serve = test_serve = None

NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "raw_test.js")


def blue_corner(image):
    """Which corner of an upright picture the blue marker square is in ("tl", ...)."""
    width, height = image.size
    corners = {"tl": (2, 2), "tr": (width - 3, 2), "bl": (2, height - 3),
               "br": (width - 3, height - 3)}
    found = [name for name, point in corners.items()
             if image.convert("RGB").getpixel(point)[2] > 150
             and image.convert("RGB").getpixel(point)[0] < 100]
    return found[0] if len(found) == 1 else found


@unittest.skipIf(rawgen is None, "Pillow is not installed")
class ReadTest(unittest.TestCase):
    """The structure of each kind of RAW file: previews, EXIF, orientation."""

    def test_arw(self):
        data = rawgen.arw(orientation=6, gps=(48.8584, -2.2945))
        info = raw_photos.read(data, "DSC0001.ARW")
        self.assertEqual(info.format, "ARW")
        self.assertEqual(info.orientation, 6)
        self.assertEqual(info.sensor, (120, 80))
        # The largest preview first; the thumbnail after it.
        self.assertEqual([(found.width, found.height) for found in info.previews],
                         [(96, 64), (32, 24)])
        preview = raw_photos.preview_jpeg(data, info)
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.size, (96, 64))
        exif = raw_photos.exif_dict(info)
        self.assertEqual((exif["Make"], exif["Model"], exif["LensModel"]),
                         ("SONY", "ILCE-X", "E 18-55mm"))
        self.assertEqual(exif["DateTimeOriginal"], "2021:06:13 13:36:50")
        self.assertEqual((exif["ExposureTime"], exif["FNumber"], exif["ISOSpeedRatings"],
                          exif["FocalLength"]), (0.004, 5.6, 200, 35))
        lat, lon = imageinfo.exif_gps_to_decimal(exif["GPS"])
        self.assertAlmostEqual(lat, 48.8584, places=4)
        self.assertAlmostEqual(lon, -2.2945, places=4)
        # The same facts a scan works out for any picture.
        meta = imageinfo.exif_metadata(exif, "DSC0001.ARW")
        self.assertEqual((meta["taken_at"], meta["taken_source"], meta["camera"]),
                         ("2021-06-13T13:36:50", "exif", "SONY ILCE-X"))
        self.assertTrue(raw_photos.is_raw_data(data))
        json.dumps(exif, allow_nan=False)

    def test_dng(self):
        # Big-endian; an uncompressed thumbnail in IFD0 (no JPEG: no preview), the
        # sensor data a lossless JPEG (never a preview) and the preview in SubIFDs.
        data = rawgen.dng(gps=(-33.86, 151.21))
        info = raw_photos.read(data, "IMG_0001.DNG")
        self.assertEqual(info.format, "DNG")
        self.assertEqual([(found.width, found.height) for found in info.previews],
                         [(128, 96)])
        self.assertEqual(info.sensor, (200, 100))
        lat, lon = imageinfo.exif_gps_to_decimal(raw_photos.exif_dict(info)["GPS"])
        self.assertAlmostEqual(lat, -33.86, places=4)
        self.assertAlmostEqual(lon, 151.21, places=4)
        # Known as a DNG by its structure too, whatever its name.
        self.assertEqual(raw_photos.read(data, "").format, "DNG")

    def test_nef_and_others_by_name(self):
        info = raw_photos.read(rawgen.nef(), "a.nef")
        self.assertEqual((info.format, info.largest()[2:]), ("NEF", (160, 106)))
        self.assertEqual(imageinfo.camera_name(info.exif), "NIKON D9")
        for name in ("b.NRW", "c.pef", "d.SRW"):
            self.assertEqual(raw_photos.read(rawgen.nef(), name).format,
                             name.rsplit(".", 1)[1].upper())

    def test_cr2(self):
        data = rawgen.cr2()
        info = raw_photos.read(data, "")
        self.assertEqual(info.format, "CR2")
        # The old-style JPEG strip of IFD0; IFD3's lossless sensor data is no preview.
        self.assertEqual([(found.width, found.height) for found in info.previews],
                         [(120, 80)])
        self.assertEqual(info.exif["DateTimeOriginal"], "2020:05:06 07:08:09")

    def test_rw2_and_orf_headers(self):
        info = raw_photos.read(rawgen.rw2(), "")
        self.assertEqual((info.format, info.largest()[2:]), ("RW2", (100, 75)))
        info = raw_photos.read(rawgen.orf(), "")
        self.assertEqual((info.format, info.largest()[2:]), ("ORF", (80, 60)))
        self.assertEqual(info.exif["Model"], "E-M9")

    def test_raf(self):
        data = rawgen.raf(orientation=8)
        info = raw_photos.read(data, "DSCF0001.RAF")
        self.assertEqual(info.format, "RAF")
        self.assertEqual(info.largest()[2:], (96, 64))
        self.assertEqual(info.orientation, 8)
        self.assertEqual((info.exif["Make"], info.exif["DateTimeOriginal"]),
                         ("FUJIFILM", "2019:09:09 09:09:09"))
        self.assertTrue(raw_photos.is_raw_data(data))

    def test_cr3(self):
        data = rawgen.cr3(gps=(10.5, 20.25), orientation=3)
        info = raw_photos.read(data, "IMG_0001.CR3")
        self.assertEqual(info.format, "CR3")
        # The first track's full-size JPEG, PRVW, THMB.
        self.assertEqual([(found.width, found.height) for found in info.previews],
                         [(200, 133), (160, 106), (40, 30)])
        self.assertEqual(info.orientation, 3)
        exif = raw_photos.exif_dict(info)
        self.assertEqual((exif["Model"], exif["DateTimeOriginal"]),
                         ("Canon EOS R9", "2023:03:04 05:06:07"))
        self.assertEqual(imageinfo.exif_gps_to_decimal(exif["GPS"]), (10.5, 20.25))
        self.assertTrue(raw_photos.is_raw_data(data))
        # Without the track: the PRVW preview is the largest.
        info = raw_photos.read(rawgen.cr3(full=None), "x.cr3")
        self.assertEqual(info.largest()[2:], (160, 106))

    def test_no_preview(self):
        # Some cameras write none (only a RAW converter can show such a file).
        info = raw_photos.read(rawgen.dng(preview=None), "x.dng")
        self.assertEqual(info.previews, [])
        self.assertIsNone(info.largest())
        self.assertIsNone(raw_photos.preview_jpeg(b"", info))
        self.assertEqual(info.exif["Model"], "Model D")

    def test_which_jpegs_are_previews(self):
        # A progressive JPEG is one; one that would run past the end of the file,
        # or a lossless one (sensor data), is not.
        data = rawgen.jpeg((30, 20), progressive=True)
        self.assertIsNone(raw_photos._jpeg_size(raw_photos._Source(data=data), 0,
                                                len(data) + 1))
        self.assertEqual(raw_photos._jpeg_size(raw_photos._Source(data=data), 0, len(data)),
                         (30, 20))
        lossless = rawgen.lossless_jpeg()
        self.assertIsNone(raw_photos._jpeg_size(raw_photos._Source(data=lossless), 0,
                                                len(lossless)))

    def test_read_file_reads_only_what_it_needs(self):
        # A large RAW file: walking it reads its directories, not its sensor data.
        folder = tempfile.mkdtemp(prefix="raw-")
        self.addCleanup(support.remove_tree, folder)
        data = rawgen.arw()
        path = os.path.join(folder, "big.arw")
        with open(path, "wb") as handle:
            handle.write(data + b"\x00" * (8 * 1024 * 1024))
        real_open = open
        read = []

        class Counting(io.FileIO):
            def read(self, size=-1):
                chunk = super().read(size)
                read.append(len(chunk))
                return chunk

        def counting_open(name, mode="r", *args, **kwargs):
            if name == path:
                return Counting(name, "rb")
            return real_open(name, mode, *args, **kwargs)

        with mock.patch("builtins.open", counting_open):
            info = raw_photos.read_file(path)
        self.assertEqual(info.largest()[2:], (96, 64))
        self.assertLess(sum(read), 512 * 1024)
        self.assertEqual(raw_photos.exif_dict(info),
                         raw_photos.exif_dict(raw_photos.read(data, "big.arw")))

    def test_a_helper_that_loads_files(self):
        data = rawgen.arw()

        class Helper:
            def load_image(self, path):
                return data if path.endswith("x.arw") else None

        info = raw_photos.read_file(os.path.join("nowhere", "x.arw"), Helper())
        self.assertEqual(info.largest()[2:], (96, 64))

    def test_damaged_files(self):
        for data in (b"", b"II*\x00", b"II*\x00\xff\xff\xff\x7f",
                     b"MM\x00*\x00\x00\x00\x08\x00\x00", b"XX*\x00\x08\x00\x00\x00",
                     b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16):
            with self.assertRaises(raw_photos.RawError, msg=data):
                raw_photos.read(data, "x.arw")
        # A directory that points back at itself is walked once.
        data = bytearray(rawgen.arw(thumb=None))
        first = struct.unpack("<I", data[4:8])[0]
        count = struct.unpack("<H", data[first:first + 2])[0]
        data[first + 2 + 12 * count:first + 6 + 12 * count] = struct.pack("<I", first)
        self.assertEqual(raw_photos.read(bytes(data), "x.arw").largest()[2:], (96, 64))
        # Damaged anywhere: a RawError (or what could be read), never anything else.
        good = rawgen.arw(gps=(1.0, 2.0))
        generator = random.Random(7)
        for _ in range(300):
            damaged = bytearray(good)
            for _ in range(generator.randint(1, 8)):
                damaged[generator.randrange(len(damaged))] = generator.randrange(256)
            try:
                info = raw_photos.read(bytes(damaged), "x.arw")
            except raw_photos.RawError:
                continue
            json.dumps(raw_photos.exif_dict(info), allow_nan=False)
            for found in info.previews:
                self.assertLessEqual(found.offset + found.length, len(damaged))
        for cut in (10, 100, 1000, len(good) // 2):
            try:
                raw_photos.read(good[:cut], "x.arw")
            except raw_photos.RawError:
                pass
        self.assertFalse(raw_photos.is_raw_data(good[:10]))

    def test_raw_files_and_ordinary_tiffs(self):
        buffer = io.BytesIO()
        imagegen.marker_image().save(buffer, "TIFF")
        tiff = buffer.getvalue()
        self.assertTrue(raw_photos.maybe_raw(tiff[:16]))
        self.assertFalse(raw_photos.is_raw_data(tiff))
        self.assertFalse(raw_photos.maybe_raw(rawgen.jpeg()[:16]))
        self.assertFalse(raw_photos.is_raw_data(rawgen.jpeg()))
        for data in (rawgen.arw(), rawgen.dng(), rawgen.nef(), rawgen.cr2(), rawgen.rw2(),
                     rawgen.orf(), rawgen.raf(), rawgen.cr3()):
            self.assertTrue(raw_photos.maybe_raw(data[:16]))
            self.assertTrue(raw_photos.is_raw_data(data))

    def test_names(self):
        self.assertEqual(raw_photos.format_of("a/DSC0001.arw"), "ARW")
        self.assertEqual(raw_photos.format_of("photo.jpg"), "")
        for extension in common.RAW_EXTENSIONS:
            self.assertTrue(common.is_raw_file("X" + extension.upper()))
            self.assertTrue(common.is_media_file("x" + extension, raws=True))
            self.assertFalse(common.is_media_file("x" + extension))
            self.assertFalse(common.is_image_file("x" + extension))
        self.assertFalse(common.is_raw_file("x.tif"))


@unittest.skipIf(rawgen is None, "Pillow is not installed")
class PairingRulesTest(unittest.TestCase):

    def test_pair_names(self):
        self.assertEqual(raw_photos.pair_names(
            ["DSC0001.ARW", "DSC0002.ARW", "dsc0003.arw", "IMG_9.DNG", "B.ARW", "B.DNG"],
            ["dsc0001.jpg", "DSC0002.HEIC", "DSC0002.JPG", "DSC0003.png", "other.jpg",
             "IMG_9.DNG", "b.jpg"]),
            {"DSC0001.ARW": "dsc0001.jpg", "DSC0002.ARW": "DSC0002.JPG",
             "dsc0003.arw": "DSC0003.png", "B.ARW": "b.jpg"})
        self.assertEqual(raw_photos.pair_names(["x.arw"], []), {})

    def test_capture_key(self):
        exif = {"Make": "SONY", "Model": "ILCE-X", "DateTimeOriginal": "2021:06:13 13:36:50"}
        self.assertEqual(raw_photos.capture_key(exif), ("sony ilce-x", "2021-06-13T13:36:50"))
        self.assertEqual(raw_photos.capture_key(dict(exif, Make="sony")),
                         raw_photos.capture_key(exif))
        self.assertEqual(raw_photos.capture_key({"Model": "X", "DateTimeDigitized":
                                                 "2021:06:13 13:36:50"}),
                         ("x", "2021-06-13T13:36:50"))
        # No camera, or no time of the shot (DateTime is when the file changed): none.
        self.assertIsNone(raw_photos.capture_key({"DateTimeOriginal": "2021:06:13 13:36:50"}))
        self.assertIsNone(raw_photos.capture_key({"Model": "X", "DateTime":
                                                  "2021:06:13 13:36:50"}))
        self.assertIsNone(raw_photos.capture_key(None))

    def test_merge_and_take_back(self):
        picture = {"Make": "Camco", "Model": "R1", "DateTimeOriginal": "2021:06:13 13:36:50",
                   "Orientation": 1, "GPS": {"GPSVersionID": [2, 3, 0, 0]}}
        raw = {"Make": "Camco", "Model": "R1", "DateTimeOriginal": "2021:06:13 13:36:49",
               "LensModel": "Zoom", "Orientation": 6, "OffsetTimeOriginal": "+02:00",
               "GPS": {"GPSLatitudeRef": "N", "GPSLatitude": [10, 0, 0],
                       "GPSLongitudeRef": "E", "GPSLongitude": [20, 0, 0]}}
        merged, filled, replaced = raw_photos.merge(picture, raw)
        # The picture's own facts win; it gets what it lacks, never the orientation.
        self.assertEqual(merged["DateTimeOriginal"], "2021:06:13 13:36:50")
        self.assertEqual(merged["Orientation"], 1)
        self.assertEqual((merged["LensModel"], merged["OffsetTimeOriginal"]),
                         ("Zoom", "+02:00"))
        self.assertEqual(imageinfo.exif_gps_to_decimal(merged["GPS"]), (10.0, 20.0))
        self.assertEqual(filled, ["GPS", "LensModel", "OffsetTimeOriginal"])
        self.assertEqual(replaced, {"GPSVersionID": [2, 3, 0, 0]})
        merged["raw"] = raw_photos.record("a/x.ARW", 10, 5.0, "name", raw, filled, replaced)
        self.assertEqual(raw_photos.own_exif(merged), picture)
        # Merged again (a picture read again), nothing piles up.
        again, filled_again, _ = raw_photos.merge(merged, raw)
        self.assertEqual(filled_again, filled)
        self.assertNotIn("raw", again)
        # A picture with a position keeps it.
        merged, filled, replaced = raw_photos.merge(
            dict(picture, GPS=raw["GPS"]), dict(raw, GPS={"GPSLatitude": [1, 0, 0],
                                                          "GPSLatitudeRef": "S",
                                                          "GPSLongitude": [2, 0, 0],
                                                          "GPSLongitudeRef": "W"}))
        self.assertNotIn("GPS", filled)
        self.assertIsNone(replaced)
        self.assertEqual(imageinfo.exif_gps_to_decimal(merged["GPS"]), (10.0, 20.0))

    def test_records_and_views(self):
        entry = raw_photos.record("trip/DSC0001.ARW", 24000000, 1700000000.5, "time",
                                  {"Make": "Camco"}, ["LensModel"])
        exif = {"Make": "Camco", "raw": entry}
        self.assertIs(raw_photos.paired_record(exif), entry)
        self.assertTrue(raw_photos.same_file(entry, "trip/DSC0001.ARW", 24000000, 1700000000.9))
        self.assertFalse(raw_photos.same_file(entry, "trip/DSC0001.ARW", 24000001,
                                              1700000000.5))
        self.assertFalse(raw_photos.same_file(entry, "trip/DSC0002.ARW", 24000000,
                                              1700000000.5))
        self.assertEqual(raw_photos.view(exif), {"name": "DSC0001.ARW", "size": 24000000,
                                                 "format": "ARW", "how": "time"})
        alone = {"raw": {"how": "alone", "format": "NEF", "preview": [1067, 1600],
                         "sensor": [6000, 4000]}}
        self.assertIsNone(raw_photos.paired_record(alone))
        self.assertEqual(raw_photos.view(alone, "x.NEF", 17),
                         {"name": "x.NEF", "size": 17, "format": "NEF", "how": "alone",
                          "preview": [1067, 1600], "sensor": [6000, 4000]})
        self.assertIsNone(raw_photos.view({"Make": "Camco"}))
        self.assertIsNone(raw_photos.view({"raw": "odd"}))

    def test_safe_paths(self):
        for path in ("a/b.ARW", "x.dng", "deep/er/x.cr3"):
            self.assertTrue(raw_photos.safe_path(path), path)
        for path in ("", None, "../x.arw", "a/../../x.arw", "/x.arw", "C:/x.arw", "a\\x.arw",
                     "a/x.jpg", "a//x.arw", "./x.arw", 5):
            self.assertFalse(raw_photos.safe_path(path), path)

    def test_settings(self):
        self.assertEqual(raw_photos.settings({}), {"enabled": True, "index_alone": True})
        self.assertEqual(raw_photos.settings({"raw": False}),
                         {"enabled": False, "index_alone": True})
        self.assertEqual(raw_photos.settings({"raw": {"index_alone": False, "enabled": "no"}}),
                         {"enabled": True, "index_alone": False})
        self.assertEqual(raw_photos.settings(None), {"enabled": True, "index_alone": True})


@unittest.skipIf(images is None, "Pillow is not installed")
class ReadImageTest(unittest.TestCase):
    """images.read_image shows a RAW file by its largest preview, turned upright."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="raw-")
        self.addCleanup(support.remove_tree, self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_the_largest_preview_upright(self):
        path = self.write("DSC0001.ARW", rawgen.arw(orientation=6, gps=(1.5, 2.5)))
        result = images.read_image(path, want_pano=True)
        # Orientation 6: turned a quarter clockwise (the corner top left goes top right).
        self.assertEqual((result["width"], result["height"]), (64, 96))
        with Image.open(io.BytesIO(result["preview"])) as preview:
            self.assertEqual(preview.size, (64, 96))
            self.assertEqual(blue_corner(preview), "tr")
        with Image.open(io.BytesIO(result["thumb"])) as thumb:
            self.assertEqual(thumb.size, (64, 96))
        self.assertEqual(result["mime"], raw_photos.MIME_TYPE)
        self.assertEqual((result["taken_at"], result["camera"], result["lat"], result["lon"]),
                         ("2021-06-13T13:36:50", "SONY ILCE-X", 1.5, 2.5))
        self.assertEqual(result["exif"]["LensModel"], "E 18-55mm")
        self.assertEqual(result["exif"]["raw"], {"how": "alone", "format": "ARW",
                                                 "preview": [64, 96], "sensor": [120, 80]})
        self.assertEqual(len(result["content_hash"]), 40)
        self.assertIsNone(result["pano"])
        json.dumps(result["exif"], allow_nan=False)

    def test_the_preview_s_own_orientation_wins(self):
        # A RAF's JPEG says which way is up itself.
        path = self.write("DSCF0001.RAF", rawgen.raf(orientation=6))
        result = images.read_image(path)
        self.assertEqual((result["width"], result["height"]), (64, 96))
        self.assertEqual(result["exif"]["raw"]["format"], "RAF")

    def test_every_kind(self):
        for name, data, size in (("a.dng", rawgen.dng(), (128, 96)),
                                 ("b.nef", rawgen.nef(), (160, 106)),
                                 ("c.cr2", rawgen.cr2(), (120, 80)),
                                 ("d.rw2", rawgen.rw2(), (100, 75)),
                                 ("e.orf", rawgen.orf(), (80, 60)),
                                 ("f.cr3", rawgen.cr3(orientation=3), (200, 133))):
            result = images.read_image(self.write(name, data))
            self.assertEqual((result["width"], result["height"]), size, name)
            self.assertEqual(result["exif"]["raw"]["format"],
                             name.rsplit(".", 1)[1].upper(), name)

    def test_no_preview(self):
        path = self.write("x.dng", rawgen.dng(preview=None))
        with self.assertRaises(images.ImageReadError) as caught:
            images.read_image(path)
        self.assertIn("no preview", str(caught.exception))
        with self.assertRaises(images.ImageReadError), warnings.catch_warnings():
            warnings.simplefilter("ignore")         # (Pillow's, about the damaged TIFF)
            images.read_image(self.write("y.arw", b"II*\x00\x08\x00\x00\x00\xff\xff"))

    def test_for_the_browser(self):
        path = self.write("x.arw", rawgen.arw(orientation=8))
        with Image.open(io.BytesIO(images.to_browser_jpeg(path))) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (64, 96)))
            self.assertEqual(blue_corner(image), "bl")
        with open(path, "rb") as handle:
            data = handle.read()
        with Image.open(io.BytesIO(images.jpeg_from_bytes(data))) as image:
            self.assertEqual(image.size, (64, 96))

    def test_ordinary_tiffs_are_read_as_before(self):
        buffer = io.BytesIO()
        imagegen.marker_image((40, 30)).save(buffer, "TIFF")
        result = images.read_image(self.write("scan.tif", buffer.getvalue()))
        self.assertEqual((result["width"], result["height"], result["mime"]),
                         (40, 30, "image/tiff"))
        self.assertNotIn("raw", result["exif"])

    def test_a_picture_s_own_text_is_no_raw_file(self):
        # (A PNG's text chunks go into Pillow's image.info: they say nothing of
        # a RAW file.)
        from PIL import PngImagePlugin
        for key, value in (("raw", "hello"), ("raw_orientation", "6")):
            meta = PngImagePlugin.PngInfo()
            meta.add_text(key, value)
            buffer = io.BytesIO()
            imagegen.marker_image((60, 30)).save(buffer, "PNG", pnginfo=meta)
            result = images.read_image(self.write("%s.png" % key, buffer.getvalue()))
            self.assertEqual((result["width"], result["height"], result["mime"]),
                             (60, 30, "image/png"), key)
            self.assertNotIn("raw", result["exif"], key)


def picture_exif(**options):
    """A Pillow Exif for a camera's JPEG of the shot (by default without a lens or GPS)."""
    defaults = {"date": "2021:06:13 13:36:50", "offset": None, "make": "SONY",
                "model": "ILCE-X", "gps": None}
    defaults.update(options)
    return imagegen.make_exif(**defaults)


@unittest.skipIf(images is None, "Pillow is not installed")
class ScanTest(support.TempBaseMixin, unittest.TestCase):
    """The scan pairs RAW files with their pictures, and reads the others on their own."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Camera")
        self.raw_reads = []
        real = raw_photos.read_file

        def counting(path, *args, **kwargs):
            self.raw_reads.append(os.path.basename(path))
            return real(path, *args, **kwargs)
        patcher = mock.patch.object(raw_photos, "read_file", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.configure()

    def configure(self, **extra):
        self.base.write_config([support.library("Camera", self.folder, id="lib_raw00001")],
                               **extra)
        self.config = common.load_config()
        db.init(self.config, log=lambda message: None)

    def write(self, rel, data):
        path = os.path.join(self.folder, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def jpeg(self, rel, **options):
        return self.write(rel, imagegen.jpeg_bytes(imagegen.marker_image((48, 32)),
                                                   picture_exif(**options)))

    def scan(self, **options):
        import scan
        del self.raw_reads[:]
        self.ctx = jobs.JobContext("test")
        return scan.run(self.config, self.ctx, describe_images=False, embed_vectors=False,
                        **options)["libraries"][0]

    def rows(self, present=True):
        conn = db.connect()
        try:
            rows = {}
            for row in conn.execute("SELECT * FROM images ORDER BY path"):
                if present and row["missing_since"] is not None:
                    continue
                found = dict(row)
                found["exif"] = json.loads(row["exif"])
                rows[row["path"]] = found
            return rows
        finally:
            conn.close()

    def execute(self, sql, params=()):
        conn = db.connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=1000)["log_tail"])

    def make_library(self):
        # By name (whatever the case), with the lens and position only the RAW has.
        self.write("a/DSC0001.ARW", rawgen.arw(gps=(48.85, 2.29)))
        self.jpeg("a/dsc0001.jpg")
        # By camera and time: another name, in another folder.
        self.write("raw/DSC0002.ARW", rawgen.arw(date="2021:06:13 14:00:00"))
        self.jpeg("edited/holiday.jpg", date="2021:06:13 14:00:00")
        # On its own.
        self.write("a/DSC0003.ARW", rawgen.arw(date="2021:06:13 15:00:00", orientation=6))
        # With no preview: it cannot be shown.
        self.write("a/X.dng", rawgen.dng(preview=None))
        # An ordinary picture.
        self.jpeg("a/other.jpg", date="2021:06:13 16:00:00", model="Other")

    def test_pairs_and_pictures_of_their_own(self):
        self.make_library()
        summary = self.scan()
        rows = self.rows()
        self.assertEqual(sorted(rows), ["a/DSC0003.ARW", "a/dsc0001.jpg", "a/other.jpg",
                                        "edited/holiday.jpg"])
        pair = rows["a/dsc0001.jpg"]
        entry = pair["exif"]["raw"]
        self.assertEqual((entry["path"], entry["name"], entry["how"], entry["format"]),
                         ("a/DSC0001.ARW", "DSC0001.ARW", "name", "ARW"))
        self.assertEqual(entry["size"], os.path.getsize(os.path.join(self.folder, "a",
                                                                     "DSC0001.ARW")))
        # (What the picture lacks: its lens, position and exposure.)
        self.assertEqual(entry["filled"], ["DateTime", "DateTimeDigitized", "ExposureTime",
                                           "FNumber", "FocalLength", "GPS", "ISOSpeedRatings",
                                           "LensModel"])
        # The picture has what only its RAW file had: the lens, the position.
        self.assertEqual(pair["exif"]["LensModel"], "E 18-55mm")
        self.assertEqual((round(pair["lat"], 2), round(pair["lon"], 2)), (48.85, 2.29))
        self.assertEqual(pair["taken_at"], "2021-06-13T13:36:50")
        timed = rows["edited/holiday.jpg"]["exif"]["raw"]
        self.assertEqual((timed["path"], timed["how"]), ("raw/DSC0002.ARW", "time"))
        alone = rows["a/DSC0003.ARW"]
        self.assertEqual((alone["width"], alone["height"]), (64, 96))
        self.assertEqual(alone["exif"]["raw"]["how"], "alone")
        self.assertEqual((alone["camera"], alone["taken_at"]),
                         ("SONY ILCE-X", "2021-06-13T15:00:00"))
        self.assertEqual(rows["a/other.jpg"]["exif"].get("raw"), None)
        self.assertEqual((summary["raw_paired"], summary["raw_alone"], summary["failed"]),
                         (2, 1, 1))
        self.assertEqual(summary["files"], 6)
        text = self.log_text()
        self.assertIn("a/DSC0001.ARW goes with a/dsc0001.jpg (by name)", text)
        self.assertIn("raw/DSC0002.ARW goes with edited/holiday.jpg (by camera and time)",
                      text)
        self.assertIn("RAW files in Camera: 2 now with their picture, 1 read as pictures of "
                      "their own.", text)
        self.assertIn("this RAW file holds no preview picture", text)
        # Nothing to do the next time: no RAW file is read again.
        summary = self.scan()
        self.assertEqual(self.raw_reads, [])
        self.assertEqual((summary["raw_paired"], summary["raw_alone"], summary["added"],
                          summary["missing"], summary["excluded"]), (0, 0, 0, 0, 0))
        self.assertEqual(self.rows(), rows)

    def test_a_picture_read_again_keeps_its_raw_file(self):
        self.make_library()
        self.scan()
        before = self.rows()["a/dsc0001.jpg"]
        path = os.path.join(self.folder, "a", "dsc0001.jpg")
        os.utime(path, (time.time() + 60, time.time() + 60))
        summary = self.scan()
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(self.raw_reads, [])
        after = self.rows()["a/dsc0001.jpg"]
        self.assertEqual(after["exif"], before["exif"])
        self.assertEqual((after["lat"], after["taken_at"]), (before["lat"], before["taken_at"]))
        # The backfill (here: a preview to make again) keeps it too.
        import cache
        cache.PreviewStore().delete_both("lib_raw00001", after["id"])
        summary = self.scan()
        self.assertEqual(summary["backfill"]["filled"], 1)
        self.assertEqual(self.rows()["a/dsc0001.jpg"]["exif"], before["exif"])

    def test_a_raw_file_that_goes_away(self):
        self.make_library()
        self.scan()
        os.remove(os.path.join(self.folder, "a", "DSC0001.ARW"))
        os.remove(os.path.join(self.folder, "raw", "DSC0002.ARW"))
        summary = self.scan()
        self.assertEqual(summary["raw_unpaired"], 2)
        rows = self.rows()
        picture = rows["a/dsc0001.jpg"]
        # Its own EXIF again: no lens, no position.
        self.assertNotIn("raw", picture["exif"])
        self.assertNotIn("LensModel", picture["exif"])
        self.assertNotIn("GPS", picture["exif"])
        self.assertEqual((picture["lat"], picture["lon"], picture["place_country"]),
                         (None, None, ""))
        self.assertNotIn("raw", rows["edited/holiday.jpg"]["exif"])
        self.assertEqual(summary["missing"], 0)

    def test_a_picture_comes_next_to_a_raw_file_of_its_own(self):
        self.make_library()
        self.scan()
        alone = self.rows()["a/DSC0003.ARW"]
        info = {"title": "Tower", "description": "A tower.", "tags": ["tower"]}
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = 5 WHERE id = ?",
                     (json.dumps(info), alone["id"]))
        self.jpeg("a/DSC0003.JPG", date="2021:06:13 15:00:00")
        summary = self.scan()
        rows = self.rows()
        self.assertNotIn("a/DSC0003.ARW", rows)
        picture = rows["a/DSC0003.JPG"]
        self.assertEqual(picture["exif"]["raw"]["path"], "a/DSC0003.ARW")
        # The picture has the description it had as a RAW file.
        self.assertEqual((picture["described"], json.loads(picture["info"])["title"]),
                         (1, "Tower"))
        self.assertEqual((summary["excluded"], summary["missing"]), (1, 0))
        self.assertIn("1 RAW file(s) in Camera are no longer pictures of their own",
                      self.log_text())
        # The picture goes away again: the RAW file is a picture of its own again,
        # as it was.
        os.remove(os.path.join(self.folder, "a", "DSC0003.JPG"))
        summary = self.scan()
        rows = self.rows()
        self.assertEqual(rows["a/DSC0003.ARW"]["id"], alone["id"])
        self.assertEqual(rows["a/DSC0003.ARW"]["described"], 1)
        self.assertEqual((summary["restored"], summary["missing"]), (1, 1))

    def test_a_picture_of_the_same_second_twice(self):
        # Two pictures of the camera in the same second: which one is not known.
        self.write("raw/DSC0002.ARW", rawgen.arw(date="2021:06:13 14:00:00"))
        self.jpeg("one.jpg", date="2021:06:13 14:00:00")
        self.jpeg("two.jpg", date="2021:06:13 14:00:00")
        self.scan()
        rows = self.rows()
        self.assertEqual(sorted(rows), ["one.jpg", "raw/DSC0002.ARW", "two.jpg"])
        self.assertNotIn("raw", rows["one.jpg"]["exif"])

    def test_a_raw_file_whose_picture_is_ignored(self):
        self.write("a/DSC0001.ARW", rawgen.arw())
        self.jpeg("a/DSC0001.jpg", date="2020:01:01 00:00:00")
        self.base.write_config([support.library("Camera", self.folder, id="lib_raw00001",
                                                ignore=["*.jpg"])])
        self.config = common.load_config()
        self.scan()
        self.assertEqual(sorted(self.rows()), ["a/DSC0001.ARW"])

    def test_raw_files_alone_off(self):
        self.make_library()
        self.scan()
        self.configure(raw={"index_alone": False})
        summary = self.scan()
        rows = self.rows()
        self.assertNotIn("a/DSC0003.ARW", rows)
        self.assertEqual(rows["a/dsc0001.jpg"]["exif"]["raw"]["path"], "a/DSC0001.ARW")
        self.assertEqual((summary["excluded"], summary["missing"]), (1, 0))
        # A new one is not read at all.
        self.write("b/DSC0009.ARW", rawgen.arw(date="2022:01:01 10:00:00"))
        summary = self.scan()
        # (With the DNG that cannot be shown.)
        self.assertEqual(summary["raw_skipped"], 2)
        self.assertNotIn("b/DSC0009.ARW", self.rows())
        self.assertIn("2 without a picture left out", self.log_text())

    def test_raw_files_off(self):
        self.make_library()
        self.scan()
        self.configure(raw={"enabled": False})
        summary = self.scan()
        rows = self.rows()
        self.assertEqual(sorted(rows), ["a/dsc0001.jpg", "a/other.jpg", "edited/holiday.jpg"])
        self.assertNotIn("raw", rows["a/dsc0001.jpg"]["exif"])
        self.assertNotIn("LensModel", rows["a/dsc0001.jpg"]["exif"])
        self.assertEqual((summary["raw_unpaired"], summary["excluded"], summary["missing"],
                          summary["files"]), (2, 1, 0, 3))
        self.assertEqual(self.raw_reads, [])
        # On again: paired again, and the RAW file on its own is back.
        self.configure()
        self.scan()
        rows = self.rows()
        self.assertIn("a/DSC0003.ARW", rows)
        self.assertEqual(rows["a/dsc0001.jpg"]["exif"]["raw"]["path"], "a/DSC0001.ARW")

    def test_a_name_two_cameras_give_their_files_is_no_pair(self):
        # Both name their files IMG_1234: another maker, another day.
        self.write("trip/IMG_1234.CR3", rawgen.cr3(date="2023:03:04 05:06:07"))
        self.jpeg("trip/IMG_1234.JPG", date="2023:07:01 10:00:00", make="Apple",
                  model="iPhone 99")
        # The same maker, a day apart (one camera's numbers came round again).
        self.write("b/DSC0001.ARW", rawgen.arw(date="2021:06:14 13:36:50"))
        self.jpeg("b/DSC0001.JPG")
        # A RAW file dated a few seconds before its picture is one shot.
        self.write("c/IMG_0007.DNG", rawgen.dng(make="Camco", date="2022:01:02 10:20:24"))
        self.jpeg("c/IMG_0007.JPG", date="2022:01:02 10:20:30", make="Camco", model="Model D")
        summary = self.scan()
        rows = self.rows()
        self.assertEqual(sorted(rows), ["b/DSC0001.ARW", "b/DSC0001.JPG", "c/IMG_0007.JPG",
                                        "trip/IMG_1234.CR3", "trip/IMG_1234.JPG"])
        phone = rows["trip/IMG_1234.JPG"]
        self.assertNotIn("raw", phone["exif"])
        self.assertNotIn("LensModel", phone["exif"])
        self.assertEqual(phone["camera"], "Apple iPhone 99")
        self.assertEqual(rows["trip/IMG_1234.CR3"]["exif"]["raw"]["how"], "alone")
        self.assertEqual(rows["trip/IMG_1234.CR3"]["taken_at"], "2023-03-04T05:06:07")
        self.assertNotIn("raw", rows["b/DSC0001.JPG"]["exif"])
        self.assertEqual(rows["c/IMG_0007.JPG"]["exif"]["raw"]["path"], "c/IMG_0007.DNG")
        self.assertEqual((summary["raw_paired"], summary["raw_alone"]), (1, 2))
        self.assertIn("trip/IMG_1234.CR3 does not go with trip/IMG_1234.JPG (another camera, "
                      "or another time)", self.log_text())
        # Nothing changes the next time.
        summary = self.scan()
        self.assertEqual((summary["raw_paired"], summary["raw_alone"], summary["excluded"],
                          summary["missing"]), (0, 0, 0, 0))
        self.assertEqual(self.rows(), rows)

    def test_a_picture_that_is_another_shot_now(self):
        self.write("a/DSC0001.ARW", rawgen.arw(gps=(48.85, 2.29)))
        self.jpeg("a/DSC0001.JPG")
        self.scan()
        self.assertEqual(self.rows()["a/DSC0001.JPG"]["exif"]["raw"]["how"], "name")
        # The picture is replaced by another shot of the same name (a day later).
        path = self.jpeg("a/DSC0001.JPG", date="2021:06:14 09:00:00", model="Other")
        os.utime(path, (time.time() + 60, time.time() + 60))
        summary = self.scan()
        rows = self.rows()
        self.assertEqual(sorted(rows), ["a/DSC0001.ARW", "a/DSC0001.JPG"])
        picture = rows["a/DSC0001.JPG"]
        self.assertNotIn("raw", picture["exif"])
        self.assertNotIn("LensModel", picture["exif"])
        self.assertEqual((picture["lat"], picture["taken_at"]), (None, "2021-06-14T09:00:00"))
        self.assertEqual(rows["a/DSC0001.ARW"]["exif"]["raw"]["how"], "alone")
        self.assertEqual(summary["raw_alone"], 1)

    def others(self, count=8):
        """Pictures of another camera (so that a scan's guard against mass removal
        does not hold back the removal of one)."""
        for number in range(count):
            self.jpeg("other/%d.jpg" % number, date="2020:01:01 00:00:%02d" % number,
                      make="Else", model="E%d" % number)

    def test_a_picture_renamed_or_moved_keeps_its_raw_file(self):
        self.others()
        self.write("a/DSC0001.ARW", rawgen.arw())
        self.jpeg("a/DSC0001.JPG")
        self.scan()
        # Renamed: by camera and time, at once.
        os.rename(os.path.join(self.folder, "a", "DSC0001.JPG"),
                  os.path.join(self.folder, "a", "Sunset.JPG"))
        summary = self.scan()
        rows = self.rows()
        self.assertNotIn("a/DSC0001.ARW", rows)
        self.assertEqual(rows["a/Sunset.JPG"]["exif"]["raw"]["path"], "a/DSC0001.ARW")
        self.assertEqual(rows["a/Sunset.JPG"]["exif"]["raw"]["how"], "time")
        self.assertEqual(summary["raw_alone"], 0)
        # Moved to another folder: the same.
        os.makedirs(os.path.join(self.folder, "best"))
        os.rename(os.path.join(self.folder, "a", "Sunset.JPG"),
                  os.path.join(self.folder, "best", "Sunset.JPG"))
        summary = self.scan()
        rows = self.rows()
        self.assertNotIn("a/DSC0001.ARW", rows)
        self.assertEqual(rows["best/Sunset.JPG"]["exif"]["raw"]["path"], "a/DSC0001.ARW")
        self.assertEqual(summary["raw_alone"], 0)
        self.assertNotIn("a/DSC0001.ARW", self.rows(present=False))

    def test_a_picture_deleted_leaves_its_raw_file_on_its_own_at_once(self):
        self.others()
        self.write("a/DSC0001.ARW", rawgen.arw())
        self.jpeg("a/DSC0001.JPG")
        self.scan()
        os.remove(os.path.join(self.folder, "a", "DSC0001.JPG"))
        summary = self.scan()
        rows = self.rows()
        self.assertEqual(rows["a/DSC0001.ARW"]["exif"]["raw"]["how"], "alone")
        self.assertEqual((summary["raw_alone"], summary["raw_paired"]), (1, 0))
        self.assertNotIn("goes with", self.log_text())

    def test_a_hidden_raw_file_keeps_its_shot_hidden(self):
        self.write("a/DSC0003.ARW", rawgen.arw(date="2021:06:13 15:00:00"))
        self.scan()
        self.execute("UPDATE images SET hidden = 1 WHERE path = 'a/DSC0003.ARW'")
        self.jpeg("a/DSC0003.JPG", date="2021:06:13 15:00:00")
        self.scan()
        rows = self.rows()
        self.assertEqual(sorted(rows), ["a/DSC0003.JPG"])
        self.assertEqual(rows["a/DSC0003.JPG"]["hidden"], 1)
        # (A shown one leaves its picture shown.)
        self.write("a/DSC0004.ARW", rawgen.arw(date="2021:06:13 15:30:00"))
        self.scan()
        self.jpeg("a/DSC0004.JPG", date="2021:06:13 15:30:00")
        self.scan()
        self.assertEqual(self.rows()["a/DSC0004.JPG"]["hidden"], 0)

    def test_a_picture_without_a_date_gets_the_raw_file_s(self):
        self.write("DSC0005.ARW", rawgen.arw(date="2018:02:03 04:05:06"))
        self.write("DSC0005.jpg", imagegen.jpeg_bytes(imagegen.marker_image()))
        os.utime(os.path.join(self.folder, "DSC0005.jpg"), (1600000000, 1600000000))
        self.scan()
        picture = self.rows()["DSC0005.jpg"]
        self.assertEqual((picture["taken_at"], picture["taken_source"], picture["camera"]),
                         ("2018-02-03T04:05:06", "exif", "SONY ILCE-X"))
        os.remove(os.path.join(self.folder, "DSC0005.ARW"))
        self.scan()
        picture = self.rows()["DSC0005.jpg"]
        self.assertEqual((picture["taken_source"], picture["camera"]), ("mtime", ""))

    def test_a_helper_s_listing_pairs_them_too(self):
        import scan
        self.write("a/DSC0001.ARW", rawgen.arw())
        self.jpeg("a/DSC0001.JPG")
        self.write("a/DSC0002.CR3", rawgen.cr3())

        class Helper:
            def walk_library(self, root):
                return os.walk(root)

        walk = scan.walk_library(self.folder, [], Helper(), raws=True)
        self.assertEqual(sorted(entry[0] for entry in walk), ["a/DSC0001.JPG", "a/DSC0002.CR3"])
        self.assertEqual(list(walk.raw_pairs), ["a/DSC0001.JPG"])
        self.assertEqual(walk.raw_pairs["a/DSC0001.JPG"][0], "a/DSC0001.ARW")
        # Without raws they are not listed at all.
        walk = scan.walk_library(self.folder, [], raws=False)
        self.assertEqual(sorted(entry[0] for entry in walk), ["a/DSC0001.JPG"])
        self.assertEqual(walk.raw_pairs, {})


@unittest.skipIf(flask is None or images is None, "Flask or Pillow is not installed")
class RawRouteTest(test_serve.ServerFixture if test_serve else object, unittest.TestCase):
    """The server: a picture's RAW file in its details, downloaded by the owner only."""

    def setUp(self):
        super().setUp()
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        self.raw_data = rawgen.arw(gps=(1.0, 2.0))
        self.write_original("trip/DSC0001.ARW", self.raw_data)
        self.write_original("trip/DSC0001.JPG", imagegen.jpeg_bytes(imagegen.marker_image()))
        entry = raw_photos.record("trip/DSC0001.ARW", len(self.raw_data), 1.0, "name",
                                  {"Make": "SONY", "GPS": {"GPSLatitude": [1, 0, 0]}},
                                  ["LensModel"])
        self.paired = test_serve.insert_image(
            self.conn, "lib_family1", "trip/DSC0001.JPG",
            exif=json.dumps({"Make": "SONY", "LensModel": "Zoom", "raw": entry}))
        self.alone_data = rawgen.arw(orientation=6)
        self.write_original("trip/DSC0002.ARW", self.alone_data)
        self.alone = test_serve.insert_image(
            self.conn, "lib_family1", "trip/DSC0002.ARW", size=len(self.alone_data),
            exif=json.dumps({"Make": "SONY", "raw": {"how": "alone", "format": "ARW",
                                                     "preview": [64, 96]}}))
        bad = raw_photos.record("../../outside.ARW", 5, 1.0, "name", {}, [])
        self.unsafe = test_serve.insert_image(self.conn, "lib_family1", "trip/x.jpg",
                                              exif=json.dumps({"raw": bad}))

    def detail(self, image_id, **kwargs):
        response = self.client.get("/api/image/%d" % image_id, **kwargs)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.get_data(as_text=True))

    def test_the_details_say_so(self):
        record = self.detail(self.paired)
        self.assertEqual(record["raw"], {"name": "DSC0001.ARW", "size": len(self.raw_data),
                                         "format": "ARW", "how": "name",
                                         "url": "/raw/%d" % self.paired})
        # The RAW file's own facts stay on the server.
        self.assertNotIn("raw", record["exif"])
        self.assertEqual(record["exif"]["LensModel"], "Zoom")
        record = self.detail(self.alone)
        self.assertEqual(record["raw"], {"name": "DSC0002.ARW", "size": len(self.alone_data),
                                         "format": "ARW", "how": "alone",
                                         "preview": [64, 96], "url": "/raw/%d" % self.alone})
        self.assertNotIn("raw", self.detail(self.visible))
        # Visitors see it, but get no link.
        record = self.detail(self.paired, environ_base=test_serve.REMOTE)
        self.assertEqual(record["raw"]["name"], "DSC0001.ARW")
        self.assertNotIn("url", record["raw"])

    def test_the_owner_downloads_it(self):
        response = self.client.get("/raw/%d" % self.paired)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.raw_data)
        self.assertEqual(response.mimetype, "application/octet-stream")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("DSC0001.ARW", response.headers["Content-Disposition"])
        self.assertIn("private", response.headers["Cache-Control"])
        response.close()
        response = self.client.get("/raw/%d" % self.alone)
        self.assertEqual(response.data, self.alone_data)
        response.close()

    def test_nobody_else_does(self):
        for image_id in (self.paired, self.alone):
            self.assertEqual(self.client.get("/raw/%d" % image_id,
                                             environ_base=test_serve.REMOTE).status_code, 404)
            self.assertEqual(self.client.get("/raw/%d" % image_id, headers={
                "Sec-Fetch-Site": "cross-site"}).status_code, 404)
        for image_id in (self.visible, self.unsafe, self.missing, 999999):
            self.assertEqual(self.client.get("/raw/%d" % image_id).status_code, 404)

    def test_a_raw_file_of_its_own_is_shown_by_its_preview(self):
        response = self.client.get("/image/%d" % self.alone)
        self.assertEqual((response.status_code, response.mimetype), (200, "image/jpeg"))
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual(image.size, (64, 96))
        response = self.client.get("/preview/%d" % self.alone)
        self.assertEqual(response.status_code, 200)


@unittest.skipIf(images is None, "Pillow is not installed")
class RawExportTest(support.TempBaseMixin, unittest.TestCase):
    """An export publishes RAW files only with its raw_files option."""

    def setUp(self):
        super().setUp()
        folder = self.base.library_folder("Camera")
        self.raw_data = rawgen.arw(gps=(48.85, 2.29))
        self.alone_data = rawgen.arw(date="2021:06:13 15:00:00")

        def write(name, data):
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(data)
        write("DSC0001.ARW", self.raw_data)
        write("DSC0001.jpg", imagegen.jpeg_bytes(imagegen.marker_image((48, 32)),
                                                 picture_exif()))
        write("DSC0003.ARW", self.alone_data)
        self.base.write_config([support.library("Camera", folder, id="lib_raw00001")])
        db.init(common.load_config(), log=lambda message: None)
        import scan
        scan.run(common.load_config(), jobs.JobContext("test"), describe_images=False,
                 embed_vectors=False)
        info = {"title": "A shot", "description": "A shot.", "tags": ["shot"],
                "photo_type": "photo", "private_info": False, "sensitive": "none"}
        conn = db.connect()
        try:
            conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = 5",
                         (json.dumps(info),))
            conn.commit()
            self.ids = {row["name"]: row["id"] for row in conn.execute(
                "SELECT id, name FROM images")}
        finally:
            conn.close()

    def export(self, name, **options):
        import export
        out = os.path.join(self.base.path, "exports", name)
        conn = db.connect()
        try:
            summary = export.export_library(jobs.JobContext("test"), conn, common.load_config(),
                                            "lib_raw00001", out, options)
        finally:
            conn.close()
        return out, summary

    def preview(self, **options):
        import export
        conn = db.connect()
        try:
            return export.preview_export(conn, common.load_config(), "lib_raw00001", options,
                                         reachable=True)
        finally:
            conn.close()

    def raws(self, out):
        folder = os.path.join(out, "raws")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def test_never_by_default(self):
        import test_export
        out, summary = self.export("plain")
        self.assertEqual(self.raws(out), [])
        self.assertEqual(summary["raws"], 0)
        for detail in test_export.read_details(out).values():
            self.assertNotIn("raw", detail)
        counts = self.preview()["counts"]
        self.assertEqual(counts["raws"], {"total": 2, "included": 0, "location": 0})

    def test_published_as_they_are(self):
        import test_export
        out, summary = self.export("raw", raw_files=True, location=True)
        found = self.raws(out)
        self.assertEqual(len(found), 2)
        self.assertEqual(summary["raws"], 2)
        details = test_export.read_details(out)
        contents = []
        for xid, detail in details.items():
            raw = detail["raw"]
            self.assertEqual(raw["url"], "raws/%s.arw" % xid)
            self.assertNotIn("name", raw)            # (no file names)
            with open(os.path.join(out, raw["url"]), "rb") as handle:
                contents.append(handle.read())
            self.assertEqual(raw["size"], len(contents[-1]))
            self.assertEqual(raw["format"], "ARW")
        self.assertEqual(sorted(contents), sorted([self.raw_data, self.alone_data]))
        self.assertEqual(sorted(detail["raw"]["how"] for detail in details.values()),
                         ["alone", "name"])
        # The same again: nothing written; with file names: their names.
        again, summary = self.export("raw", raw_files=True, location=True)
        self.assertEqual(summary["written"], 0)
        out, _ = self.export("named", raw_files=True, location=True, file_names=True)
        names = sorted(detail["raw"]["name"]
                       for detail in test_export.read_details(out).values())
        self.assertEqual(names, ["DSC0001.ARW", "DSC0003.ARW"])
        # Off again: they are taken out of the folder.
        out, summary = self.export("raw")
        self.assertEqual(self.raws(out), [])

    def test_one_with_a_position_needs_the_location(self):
        import test_export
        out, summary = self.export("raw", raw_files=True)
        self.assertEqual(summary["raws"], 1)
        details = list(test_export.read_details(out).values())
        self.assertEqual([detail["raw"]["how"] for detail in details if "raw" in detail],
                         ["alone"])
        preview = self.preview(raw_files=True)
        self.assertEqual(preview["counts"]["raws"], {"total": 2, "included": 1, "location": 1})
        self.assertEqual(preview["size_parts"]["raws"], len(self.alone_data))
        flagged = self.preview(raw_files=True, exclude_ids=[self.ids["DSC0003.ARW"]])
        self.assertEqual(flagged["counts"]["raws"]["included"], 0)
        self.assertEqual([item.get("raw") for item in flagged["flagged"]], ["included"])

    def test_one_whose_picture_s_position_is_set_aside_stays_home(self):
        import gps_check
        import test_export
        conn = db.connect()
        try:
            with conn:
                gps_check.set_choice(conn, common.load_config(), self.ids["DSC0001.jpg"], False)
            gps_check.run(conn, common.load_config())
            self.assertIsNone(conn.execute("SELECT lat FROM images WHERE id = ?",
                                           (self.ids["DSC0001.jpg"],)).fetchone()[0])
        finally:
            conn.close()
        out, summary = self.export("raw", raw_files=True, location=True)
        self.assertEqual((summary["raws"], summary["raw_located"]), (1, 1))
        self.assertEqual(sorted(detail["raw"]["how"] for detail
                                in test_export.read_details(out).values() if "raw" in detail),
                         ["alone"])
        self.assertEqual(self.preview(raw_files=True, location=True)["counts"]["raws"],
                         {"total": 2, "included": 1, "location": 1})

    def test_one_geotagged_since_the_scan_is_looked_at_again(self):
        import test_export
        folder = self.base.library_folder("Camera")
        path = os.path.join(folder, "DSC0003.ARW")
        with open(path, "wb") as handle:
            handle.write(rawgen.arw(date="2021:06:13 15:00:00", gps=(40.5, 3.5)))
        os.utime(path, (time.time() + 60, time.time() + 60))
        out, summary = self.export("raw", raw_files=True)
        # (Without the location: neither goes now.)
        self.assertEqual((summary["raws"], summary["raw_located"]), (0, 2))
        self.assertEqual(self.raws(out), [])
        # With it, the one on its own still stays home: its own position (the
        # scan's) is none.
        out, summary = self.export("raw", raw_files=True, location=True)
        self.assertEqual((summary["raws"], summary["raw_located"]), (1, 1))
        self.assertEqual([detail["raw"]["how"] for detail
                          in test_export.read_details(out).values() if "raw" in detail],
                         ["name"])

    def test_a_folder_without_its_record_loses_them_too(self):
        out, _summary = self.export("raw", raw_files=True, location=True)
        self.assertEqual(len(self.raws(out)), 2)
        # The record of the last export is lost (another computer, say).
        state = os.path.join(self.base.path, "data", "exports", "state")
        for name in os.listdir(state):
            os.remove(os.path.join(state, name))
        out, _summary = self.export("raw")
        self.assertEqual(self.raws(out), [])

    def test_the_option(self):
        import export
        self.assertIs(export.default_options()["raw_files"], False)
        with self.assertRaises(export.OptionsError):
            export.validate_options({"raw_files": "yes"})
        self.assertEqual(export.options_summary(export.validate_options({"raw_files": True}))
                         .get("raw_files"), True)
        self.assertNotIn("raw_files", export.options_summary(export.validate_options({})))


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PageScriptTest(unittest.TestCase):

    def test_the_pages(self):
        # VIEWER.format.rawText/rawUrl, the information pane's RAW row, and the
        # export dialog's words (static/export-review.js).
        result = subprocess.run([NODE, NODE_TESTS], cwd=support.REPO, capture_output=True,
                                timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
