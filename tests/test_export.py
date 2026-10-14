"""Tests for export.py: the static export of one library.

A temporary data root gets a library of synthesized pictures and copies of
the bundled sample pictures (a JPEG with GPS, XMP, a maker note and data
after the image; a PNG with text chunks; a screenshot; a document; private,
sensitive, undescribed and hidden pictures; exact copies; a 360 photo; a
HEIC when pillow-heif can write one), a second library whose name must
never show up, a real scan, and made-up descriptions. The exports are
checked file by file and byte by byte; tests/js/export_test.js (Node.js, a
development tool; skipped when it is missing) loads them the way the pages
do and searches them.
"""

import collections
import errno
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from unittest import mock

import support

import common
import db
import export
import image_edits
import jobs

if support.pillow_available():
    import imagegen
    from PIL import Image, PngImagePlugin
    from PIL.TiffImagePlugin import IFDRational
else:
    imagegen = Image = PngImagePlugin = IFDRational = None

SAMPLES = os.path.join(support.REPO, "sample", "images")
STATIC = os.path.join(support.REPO, "static")
NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "export_test.js")
XID = re.compile(r"^[a-z2-7]{12}$")

# Strings planted in the pictures' metadata and the database that no
# export may contain (made up for this test).
SECRETS = (b"MAKERNOTE-SECRET-0123", b"XMP-SECRET-4567", b"TRAILER-SECRET-89ab",
           b"PNG-SECRET-cdef", b"ITXT-SECRET-0246", b"ZTXT-SECRET-1357")
FOLDER = "SecretFolderName"
FILE_PREFIX = "IMG_PRIVATE_NAME"
OTHER_LIBRARY = "OtherLibraryName"
MODEL = "secret-model-name-xyz"
ENDPOINT = "secret-endpoint-name"
PLACE_GUESS = "Secret Location Guess"
LANDMARK = "Secret Landmark"
GPS = (48.8566, 2.3522)


quiet = support.quiet


def info(title, tags, photo_type="photo", **fields):
    """A made-up version 5 description."""
    value = {"title": title, "description": "A picture of %s." % title.lower(),
             "tags": list(tags), "subjects": list(tags[:1]), "photo_type": photo_type,
             "document_type": "none", "setting": "outdoor", "time_of_day": "day",
             "event": "none", "people": "none", "sensitive": "none", "private_info": False,
             "aesthetic": 3, "sharpness": 4, "text": "", "colors": ["blue"]}
    value.update(fields)
    return value


def gps_jpeg():
    """A JPEG with GPS, camera settings, a maker note, XMP and data after the image."""
    exif = imagegen.make_exif(
        gps=GPS, orientation=6,
        exif_tags={0x927C: SECRETS[0] + b"\x00" * 8, 0x829D: IFDRational(28, 10),
                   0x829A: IFDRational(1, 125), 0x8827: 100})
    xmp = (b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>" + SECRETS[1] + b"</x:xmpmeta>")
    data = imagegen.jpeg_with_xmp(imagegen.textured_image((400, 300)), xmp, exif)
    return data + SECRETS[2] + b"\x00" * 16


def text_png():
    """A PNG with tEXt, iTXt and zTXt chunks."""
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Comment", SECRETS[3].decode("ascii"))
    meta.add_itxt("Description", SECRETS[4].decode("ascii"))
    meta.add_text("Other", SECRETS[5].decode("ascii"), zip=True)
    image = imagegen.blocks_image(12).resize((216, 192))
    buffer = io.BytesIO()
    image.save(buffer, "PNG", pnginfo=meta)
    return buffer.getvalue()


def heic_bytes():
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        buffer = io.BytesIO()
        imagegen.marker_image((96, 64)).save(buffer, "HEIF", quality=90)
        return buffer.getvalue()
    except Exception:
        return None


# Made up for these tests: a colour profile (of a calibrated screen, say)
# whose texts name a person and a computer.
ICC_SECRETS = ("ICC-DESC-JaneDoe-MacBook", "ICC-CPRT-JaneDoe", "ICC-DMDD-JaneDoe")


def personal_icc(padding=0):
    """A small ICC profile with a desc, a text and a mluc tag holding ICC_SECRETS.

    padding adds a tag of that many bytes (a large profile is split over
    several JPEG segments).
    """
    def desc(text):
        plain = text.encode("ascii") + b"\x00"
        return (b"desc" + bytes(4) + struct.pack(">I", len(plain)) + plain
                + struct.pack(">II", 0, len(text)) + text.encode("utf-16-be")
                + struct.pack(">HB", 0, 0) + bytes(67))

    def mluc(text):
        wide = text.encode("utf-16-be")
        return (b"mluc" + bytes(4) + struct.pack(">II", 1, 12) + b"enUS"
                + struct.pack(">II", len(wide), 28) + wide)

    tags = [(b"desc", desc(ICC_SECRETS[0])),
            (b"cprt", b"text" + bytes(4) + ICC_SECRETS[1].encode("ascii") + b"\x00"),
            (b"dmdd", mluc(ICC_SECRETS[2]))]
    if padding:
        tags.append((b"zzzz", b"zzzz" + bytes(4) + bytes(range(256)) * (padding // 256)))
    start = 128 + 4 + 12 * len(tags)
    table, body = b"", b""
    for signature, data in tags:
        body += bytes(-(start + len(body)) % 4)
        table += struct.pack(">4sII", signature, start + len(body), len(data))
        body += data
    header = bytearray(128)
    header[0:4] = struct.pack(">I", start + len(body))
    header[4:20] = b"lcms\x02\x10\x00\x00mntrRGB "
    header[20:24] = b"XYZ "
    header[36:40] = b"acsp"
    header[84:100] = b"PROFILE-ID-12345"
    return bytes(header) + struct.pack(">I", len(tags)) + table + body


def has_icc_secret(data):
    return any(secret.encode("ascii") in data or secret.encode("utf-16-be") in data
               for secret in ICC_SECRETS)


def read_records(out):
    """The records of an export's index shards."""
    records = []
    data = os.path.join(out, "data")
    for name in sorted(os.listdir(data)):
        if re.match(r"^index-\d+\.js$", name):
            with open(os.path.join(data, name), encoding="ascii") as handle:
                text = handle.read()
            prefix, suffix = "SEARCH_DATA.push([\n", "\n]);\n"
            assert text.startswith(prefix) and text.endswith(suffix), name
            body = text[len(prefix):-len(suffix)]
            if body:
                records.extend(json.loads("[" + body + "]"))
    return records


def read_details(out):
    """{picture id: details} of an export's detail shards."""
    details = {}
    data = os.path.join(out, "data")
    for name in sorted(os.listdir(data)):
        if re.match(r"^detail-\d+\.js$", name):
            with open(os.path.join(data, name), encoding="ascii") as handle:
                text = handle.read()
            prefix, suffix = "SEARCH_DATA.detail({\n", "\n});\n"
            assert text.startswith(prefix) and text.endswith(suffix), name
            body = text[len(prefix):-len(suffix)]
            if body:
                details.update(json.loads("{" + body + "}"))
    return details


def read_manifest(out):
    with open(os.path.join(out, "data", "manifest.js"), encoding="ascii") as handle:
        text = handle.read()
    prefix = "window.SEARCH_DATA_MANIFEST = "
    assert text.startswith(prefix) and text.endswith(";\n")
    return json.loads(text[len(prefix):-2])


def files_of(out):
    """{relative path: (size, mtime_ns)} of every file in a folder."""
    found = {}
    for folder, _dirs, names in os.walk(out):
        for name in names:
            path = os.path.join(folder, name)
            stat = os.stat(path)
            found[os.path.relpath(path, out).replace(os.sep, "/")] = (stat.st_size,
                                                                       stat.st_mtime_ns)
    return found


def jpeg_segments(data):
    """[(marker, payload)] of a JPEG up to its end, and the bytes after it."""
    segments = []
    pos = 2
    while pos < len(data):
        assert data[pos] == 0xFF, "marker expected at %d" % pos
        marker = data[pos + 1]
        pos += 2
        if marker == 0xD9:
            return segments, data[pos:]
        if 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        segments.append((marker, data[pos + 2:pos + length]))
        pos += length
        if marker == 0xDA:
            while True:
                found = data.index(b"\xff", pos)
                following = data[found + 1]
                if following == 0 or 0xD0 <= following <= 0xD7 or following == 0xFF:
                    pos = found + 1 if following == 0xFF else found + 2
                    continue
                pos = found
                break
    raise AssertionError("no end of image")


def png_chunks(data):
    chunks = []
    pos = 8
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunks.append(kind)
        pos += 12 + length
    return chunks


@unittest.skipIf(imagegen is None, "Pillow is not installed")
class ExportFixture(unittest.TestCase):
    """One data root for the whole class: scanned and described once."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = support.TempBase()
        cls.base.__enter__()
        try:
            cls.build()
        except BaseException:
            cls.base.__exit__(None, None, None)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.__exit__(None, None, None)
        super().tearDownClass()

    @classmethod
    def build(cls):
        base = cls.base
        cls.family = base.library_folder("Family")
        cls.other = base.library_folder(OTHER_LIBRARY)
        trip = os.path.join(cls.family, FOLDER, "Trip")
        os.makedirs(trip)

        def sample(name, target):
            shutil.copyfile(os.path.join(SAMPLES, name), target)

        sample("13-lighthouse.jpg", os.path.join(trip, FILE_PREFIX + "_0001.jpg"))
        sample("01-cat.jpg", os.path.join(trip, FILE_PREFIX + "_0002.jpg"))
        sample("01-cat.jpg", os.path.join(trip, FILE_PREFIX + "_0002 copy.jpg"))
        sample("05-coffee.jpg", os.path.join(cls.family, "private.jpg"))
        sample("04-beach.jpg", os.path.join(cls.family, "sensitive.jpg"))
        # An exact copy of the lighthouse, not described: it is left out,
        # and so the lighthouse is no stack in the export.
        sample("13-lighthouse.jpg", os.path.join(cls.family, "undescribed.jpg"))
        sample("16-penguin.jpg", os.path.join(cls.family, "hidden.jpg"))
        sample("18-chalkboard-menu.jpg", os.path.join(cls.family, "document.jpg"))
        sample("14-train.jpg", os.path.join(cls.other, "train.jpg"))
        imagegen.write_bytes(cls.family, "gps.jpg", gps_jpeg())
        imagegen.write_bytes(cls.family, "text.png", text_png())
        imagegen.screenshot_png(cls.family)
        pano = imagegen.jpeg_with_xmp(imagegen.equirect_image((4096, 2048)),
                                      imagegen.gpano_xmp(), quality=80)
        imagegen.write_bytes(cls.family, "pano.jpg", pano)
        cls.heic = heic_bytes()
        if cls.heic:
            imagegen.write_bytes(cls.family, "flower.heic", cls.heic)

        base.write_config([support.library("Family", cls.family),
                           support.library(OTHER_LIBRARY, cls.other)])
        db.init(log=quiet)
        cls.config = common.load_config()
        cls.library_id = cls.config["libraries"][0]["id"]
        cls.other_id = cls.config["libraries"][1]["id"]
        import scan
        scan.run(cls.config, jobs.JobContext("test"), describe_images=False)

        descriptions = {
            FOLDER + "/Trip/" + FILE_PREFIX + "_0001.jpg": info(
                "Lighthouse at dusk", ["lighthouse", "coast"]),
            FOLDER + "/Trip/" + FILE_PREFIX + "_0002.jpg": info("Cat on a sofa", ["cat"]),
            FOLDER + "/Trip/" + FILE_PREFIX + "_0002 copy.jpg": info(
                "Cat on a sofa again", ["cat"]),
            "private.jpg": info("Bank card", ["card"], private_info=True),
            "sensitive.jpg": info("Beach outfit", ["beach"], sensitive="suggestive"),
            "hidden.jpg": info("Hidden penguin", ["penguin"]),
            "document.jpg": info("Tax letter", ["letter"], photo_type="document"),
            "gps.jpg": info("Eiffel picnic", ["picnic"], location_guess=PLACE_GUESS,
                            landmarks=[LANDMARK]),
            "text.png": info("Blue drawing", ["drawing"], photo_type="artwork"),
            "Screenshot_2019-05-14-14-23-01.png": info("Phone screenshot", ["phone"],
                                                       photo_type="screenshot"),
            "pano.jpg": info("Mountain panorama", ["mountain"]),
            "flower.heic": info("Heic flower", ["flower"]),
        }
        provenance = json.dumps({"model": MODEL, "endpoint": ENDPOINT, "seconds": 1.5,
                                 "cost": 0.001, "prompt_ver": 5})
        conn = db.connect()
        try:
            for path, value in descriptions.items():
                conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = 5, "
                             "provenance = ? WHERE library_id = ? AND path = ?",
                             (json.dumps(value), provenance, cls.library_id, path))
            conn.execute("UPDATE images SET hidden = 1 WHERE path = 'hidden.jpg'")
            conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = 5 "
                         "WHERE library_id = ?", (json.dumps(info("Old train", ["train"])),
                                                  cls.other_id))
            conn.commit()
            cls.ids = {row["path"]: row["id"] for row in conn.execute(
                "SELECT id, path FROM images WHERE library_id = ?", (cls.library_id,))}
            cls.hashes = [row[0] for row in conn.execute(
                "SELECT content_hash FROM images WHERE content_hash != ''")]
            # File times in seconds (also the start of one with decimals).
            cls.times = sorted({"%d" % value for row in conn.execute(
                "SELECT mtime, ctime FROM images") for value in row if value})
        finally:
            conn.close()
        expected = 13 if cls.heic else 12
        assert len(cls.ids) == expected, sorted(cls.ids)

    # -- helpers ---------------------------------------------------------------

    def setUp(self):
        super().setUp()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def out(self, name):
        return os.path.join(common.exports_dir(), name)

    def export(self, out=None, options=None, ctx=None, **kwargs):
        config = common.load_config()
        return export.export_library(ctx or jobs.JobContext("test"), self.conn, config,
                                     self.library_id, out, options, **kwargs)

    def titles(self, records):
        return sorted(record.get("t", "") for record in records)

    def has_trusted_date(self, record):
        """True when the database dates the record's picture from the camera or a file name."""
        title = record.get("t", "")
        for row in self.conn.execute("SELECT info, taken_source FROM images WHERE info != ''"):
            if json.loads(row["info"]).get("title") == title:
                return row["taken_source"] in ("exif", "filename")
        return False

    def by_title(self, out):
        return {record.get("t", ""): record for record in read_records(out)}

    def run_node(self, out, expectations):
        folder = tempfile.mkdtemp(prefix="export-node-")
        self.addCleanup(support.remove_tree, folder)
        path = os.path.join(folder, "expected.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(expectations, handle)
        result = subprocess.run([NODE, NODE_TESTS, out, path], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")

    def assert_private(self, out, allowed_text=(), originals=False):
        """Scan every byte of an export for what must never be in one.

        originals: original JPEGs may keep their orientation (only that).
        """
        texts = [self.family, self.family.replace("\\", "/"), self.base.path,
                 self.base.path.replace("\\", "/"), FOLDER, FILE_PREFIX, OTHER_LIBRARY,
                 MODEL, ENDPOINT, "provenance", PLACE_GUESS, LANDMARK, "Hidden penguin",
                 "Bank card", "Beach outfit", "Phone screenshot", "Tax letter", "ctime",
                 "content_hash", "dhash", "48.85", "2.352", "gps", "exports",
                 self.library_id, self.other_id, "Old train"] + self.hashes + self.times
        texts = [text for text in texts if text not in allowed_text]
        binary = list(SECRETS) + [b"http://ns.adobe.com/xap/1.0/", FOLDER.encode("ascii"),
                                  FILE_PREFIX.encode("ascii"), MODEL.encode("ascii"),
                                  self.library_id.encode("ascii")]
        checked = 0
        for rel in sorted(files_of(out)):
            with open(os.path.join(out, *rel.split("/")), "rb") as handle:
                data = handle.read()
            checked += 1
            if rel.startswith("assets/"):
                # The page scripts, exactly as the program has them.
                with open(os.path.join(STATIC, rel[len("assets/"):]), "rb") as handle:
                    self.assertEqual(data, handle.read(), rel)
                continue
            for secret in binary:
                self.assertNotIn(secret, data, rel)
            if rel.endswith((".html", ".js", ".css", ".txt")) or rel == export.MARKER:
                text = data.decode("utf-8").lower()
                for value in texts:
                    value = value.lower()
                    if re.match(r"^[a-z_]+$", value):
                        # A plain word must not appear as a word; random picture
                        # ids (such as "y4ngpszba747") may contain its letters.
                        pattern = r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(value)
                        self.assertIsNone(re.search(pattern, text), "%s in %s" % (value, rel))
                    else:
                        self.assertNotIn(value, text, "%s in %s" % (value, rel))
                self.assertIsNone(re.search(r"\b[a-z]:(\\\\|\\|/)", text), rel)
                self.assertNotIn("\\\\\\\\", text, rel)
            elif rel.endswith(".jpg"):
                self.assert_clean_jpeg(data, rel, "any" if originals else None)
            elif rel.endswith(".png"):
                self.assertLessEqual(set(png_chunks(data)),
                                     {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"pHYs",
                                      b"sRGB", b"gAMA", b"cHRM", b"iCCP"}, rel)
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    self.assertEqual(getattr(image, "text", {}), {}, rel)
                    self.assertFalse(image.getexif(), rel)
            else:
                self.assertIn(rel, (export.MARKER,), rel)
        return checked

    def assert_clean_jpeg(self, data, rel, orientation=None):
        segments, after = jpeg_segments(data)
        self.assertEqual(after, b"", rel)
        for marker, payload in segments:
            if marker == 0xE0:
                self.assertTrue(payload.startswith(b"JFIF\x00"), rel)
            elif marker == 0xE1:
                # Only the orientation of an original, in a minimal block.
                self.assertTrue(payload.startswith(b"Exif\x00\x00MM"), rel)
                self.assertEqual(len(payload), 6 + 8 + 2 + 12 + 4, rel)
            elif marker == 0xE2:
                self.assertTrue(payload.startswith(b"ICC_PROFILE\x00"), rel)
            elif marker == 0xEE:
                self.assertTrue(payload.startswith(b"Adobe"), rel)
            else:
                self.assertFalse(0xE0 <= marker <= 0xEF or marker == 0xFE,
                                 "%s: marker %x" % (rel, marker))
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            exif = dict(image.getexif())
            if orientation == "any":
                self.assertLessEqual(set(exif), {274}, rel)
            else:
                self.assertEqual(exif, {274: orientation} if orientation else {}, rel)
            self.assertNotIn("xmp", image.info, rel)
            self.assertNotIn("comment", image.info, rel)
            self.assertNotIn("exif", image.info if not orientation else {}, rel)


class ExportTest(ExportFixture):

    def test_the_owners_edits_are_published_and_the_text_they_replace_is_not(self):
        cat = self.ids[FOLDER + "/Trip/" + FILE_PREFIX + "_0002.jpg"]
        image_edits.save(self.conn, cat, {"title": "Tom asleep", "description": "Our Tom.",
                                          "tags_add": ["tom"], "tags_remove": ["cat"]})
        self.conn.commit()

        def undo():
            image_edits.save(self.conn, cat, {"title": None, "description": None,
                                              "tags_add": [], "tags_remove": []})
            self.conn.commit()
        self.addCleanup(undo)
        out = self.out("edited")
        self.export(out)
        records = self.by_title(out)
        self.assertNotIn("Cat on a sofa", records)
        record = records["Tom asleep"]
        self.assertEqual((record["d"], record["g"]), ("Our Tom.", ["tom"]))
        # That the owner edited it is no business of an export.
        self.assertFalse([item for item in read_records(out) if "ed" in item])
        for name in files_of(out):
            if name.startswith("data/"):
                with open(os.path.join(out, *name.split("/")), encoding="utf-8") as handle:
                    text = handle.read()
                self.assertNotIn("A picture of cat on a sofa.", text, name)
                self.assertNotIn("model_info", text, name)

    def test_default_export_holds_only_what_may_be_published(self):
        out = self.out("default")
        summary = self.export(out)
        expected = ["Blue drawing", "Cat on a sofa", "Cat on a sofa again", "Eiffel picnic",
                    "Lighthouse at dusk", "Mountain panorama"]
        if self.heic:
            expected.append("Heic flower")
        records = read_records(out)
        self.assertEqual(self.titles(records), sorted(expected))
        self.assertEqual(summary["count"], len(expected))
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["excluded"], {
            "hidden": 1, "undescribed": 1, "screenshot": 1, "document": 1, "private": 1,
            "sensitive": 1})
        self.assertEqual(summary["output"], out)

        # The files: pages, scripts, data, and per picture a thumbnail and
        # an image (and the 360 photo's panorama); no map without location.
        files = set(files_of(out))
        xids = sorted(record["i"] for record in records)
        expected_files = {export.MARKER, "index.html", "detail.html", "data/manifest.js",
                          "data/index-000.js", "data/detail-000.js", "robots.txt"}
        expected_files.update("assets/" + name for name in export.ASSETS)
        expected_files.update("thumbs/%s.jpg" % xid for xid in xids)
        expected_files.update("images/%s.jpg" % xid for xid in xids)
        pano = self.by_title(out)["Mountain panorama"]
        expected_files.add("panos/%s.jpg" % pano["i"])
        self.assertEqual(files, expected_files)

        with open(os.path.join(out, export.MARKER), "rb") as handle:
            marker = json.loads(handle.read().decode("ascii"))
        self.assertEqual(sorted(marker), ["export_id", "format"])
        self.assertEqual(marker["format"], 1)
        self.assertEqual(marker["export_id"], summary["export_id"])
        # The state is in the data root, not in the export.
        self.assertTrue(os.path.isfile(export.state_path(summary["export_id"])))

        manifest = read_manifest(out)
        self.assertEqual(manifest["count"], len(expected))
        self.assertEqual(manifest["library"], {"id": summary["export_id"], "name": "Family"})
        self.assertEqual(manifest["capabilities"], {
            "admin": False, "static": True, "map": False, "images": True,
            "originals": False, "panos": True, "file_names": False, "folders": False})
        self.assertRegex(manifest["generated"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotIn("locations", manifest)

        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('<meta name="robots" content="noindex, noimageindex">', html)
        with open(os.path.join(out, "robots.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"User-agent: *\nDisallow: /\n")
        self.assertIn("<title>Family</title>", html)
        # Every script and the style sheet with the hash of its contents.
        sources = [source for source in re.findall(
            r'(?:<script[^>]*\ssrc|<link[^>]*\shref)="([^"]+)"', html)
            if not source.startswith("data:")]
        self.assertEqual([source.split("?")[0] for source in sources],
                         ["assets/style.css", "assets/site.js", "assets/shell.js",
                          "assets/search-core.js", "assets/streets.js", "assets/routes.js",
                          "assets/viewer.js", "assets/app.js"])
        for source in sources:
            rel, version = source.split("?v=")
            with open(os.path.join(out, rel), "rb") as handle:
                self.assertEqual(version, hashlib.sha1(handle.read()).hexdigest()[:10], rel)

        # Dates only from the camera or a file name, never a file's time.
        rows = {row["id"]: row for row in self.conn.execute(
            "SELECT id, taken_source FROM images")}
        for record in records:
            if "dk" in record:
                self.assertTrue(self.has_trusted_date(record), record.get("t"))
        for rel in ("data/index-000.js", "data/detail-000.js"):
            with open(os.path.join(out, rel), encoding="ascii") as handle:
                self.assertNotIn("mtime", handle.read())
        self.assertIn("mtime", {row["taken_source"] for row in rows.values()})
        self.assertGreater(self.assert_private(out), 20)

    def test_records_use_export_ids_and_keep_stacks_of_exported_pictures(self):
        out = self.out("ids")
        summary = self.export(out)
        records = self.by_title(out)
        row_ids = {str(value) for value in self.ids.values()}
        for record in records.values():
            self.assertRegex(record["i"], XID)
            self.assertNotIn(record["i"], row_ids)
            self.assertEqual(record["l"], summary["export_id"])
            for key in ("n", "f", "ts", "h", "gp"):
                self.assertNotIn(key, record)
        # The two cats are exact copies: one stack, its cover an export id.
        cats = [records["Cat on a sofa"], records["Cat on a sofa again"]]
        self.assertEqual(cats[0]["dg"], cats[1]["dg"])
        self.assertIn(cats[0]["dg"], (cats[0]["i"], cats[1]["i"]))
        # The lighthouse's copy is not exported (not described): no stack.
        self.assertNotIn("dg", records["Lighthouse at dusk"])
        # Without location there are no place words.
        self.assertNotIn("ex", records["Eiffel picnic"])
        self.assertEqual(records["Mountain panorama"]["p"], "360")

        # The ids stay the same from one export of the folder to the next,
        # also after the folder was emptied; another folder gets others.
        again = self.export(out)
        self.assertEqual(again["export_id"], summary["export_id"])
        self.assertEqual(self.by_title(out), records)
        for name in os.listdir(out):
            path = os.path.join(out, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        self.export(out)
        self.assertEqual(self.by_title(out), records)
        elsewhere = self.out("ids-elsewhere")
        other = self.export(elsewhere)
        self.assertNotEqual(other["export_id"], summary["export_id"])
        moved = self.by_title(elsewhere)
        self.assertEqual(sorted(moved), sorted(records))
        before_ids = {record["i"] for record in records.values()}
        self.assertFalse({record["i"] for record in moved.values()} & before_ids)

    def test_xids_are_keyed_hashes(self):
        secret = "ab" * 32
        xid = export.make_xid(secret, "0123456789abcdef", 42)
        self.assertRegex(xid, XID)
        self.assertEqual(xid, export.make_xid(secret, "0123456789abcdef", 42))
        self.assertNotEqual(xid, export.make_xid(secret, "0123456789abcdef", 43))
        self.assertNotEqual(xid, export.make_xid("cd" * 32, "0123456789abcdef", 42))
        # The bucket sum the pages repeat (site.js detailShard).
        self.assertEqual(export.bucket_of("baaa", 1 << 20), 32 ** 3)
        self.assertEqual(export.bucket_of("abcd", 7), (0 * 32 ** 3 + 1 * 32 ** 2 + 2 * 32 + 3) % 7)

    def test_second_export_writes_nothing_and_option_changes_rewrite(self):
        out = self.out("again")
        first = self.export(out, {"zip": True})
        self.assertGreater(first["written"], 10)
        before = files_of(out)
        zip_path = export.zip_path_for(out)
        zip_before = os.stat(zip_path).st_mtime_ns
        time.sleep(0.02)
        second = self.export(out, {"zip": True})
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["removed"], 0)
        self.assertEqual(files_of(out), before)
        self.assertEqual(os.stat(zip_path).st_mtime_ns, zip_before)

        # File names on: the data and pages change, the pictures do not.
        third = self.export(out, {"file_names": True, "zip": True})
        self.assertGreater(third["written"], 0)
        after = files_of(out)
        changed = sorted(rel for rel in after if after[rel] != before.get(rel))
        self.assertTrue(changed)
        for rel in changed:
            self.assertTrue(rel.startswith("data/") or rel.endswith(".html"), rel)
        names = {record.get("n") for record in read_records(out)}
        self.assertIn(FILE_PREFIX + "_0001.jpg", names)
        self.assertNotEqual(os.stat(zip_path).st_mtime_ns, zip_before)
        # And off again: back to the first bytes.
        self.export(out, {"zip": True})
        self.assertEqual({rel: value[0] for rel, value in files_of(out).items()},
                         {rel: value[0] for rel, value in before.items()})
        self.assertNotIn("n", read_records(out)[0])

    def test_zip_next_to_the_folder(self):
        out = self.out("zipped")
        summary = self.export(out, {"zip": True})
        self.assertEqual(summary["zip"], out + ".zip")
        with zipfile.ZipFile(summary["zip"]) as archive:
            entries = {entry.filename: entry for entry in archive.infolist()}
            self.assertEqual(sorted(entries),
                             sorted(rel for rel in files_of(out) if rel != export.MARKER))
            self.assertEqual(entries["data/manifest.js"].compress_type, zipfile.ZIP_DEFLATED)
            thumb = [name for name in entries if name.startswith("thumbs/")][0]
            self.assertEqual(entries[thumb].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(entries["index.html"].date_time, (1980, 1, 1, 0, 0, 0))
            with open(os.path.join(out, "index.html"), "rb") as handle:
                self.assertEqual(archive.read("index.html"), handle.read())
            self.assertIsNone(archive.testzip())
        # Without zip, the zip it wrote goes (it could hold pictures left out now).
        self.export(out)
        self.assertFalse(os.path.exists(summary["zip"]))

    def test_stale_files_are_removed_and_others_kept(self):
        out = self.out("stale")
        self.export(out)
        lighthouse = self.by_title(out)["Lighthouse at dusk"]["i"]
        mine = [os.path.join(out, "notes.txt"), os.path.join(out, "thumbs", "mine.txt")]
        for path in mine:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("mine")
        summary = self.export(out, {"exclude_ids": [self.ids[FOLDER + "/Trip/" + FILE_PREFIX
                                                             + "_0001.jpg"]]})
        self.assertEqual(summary["excluded"]["excluded"], 1)
        self.assertEqual(summary["removed"], 2)
        for rel in ("thumbs/%s.jpg" % lighthouse, "images/%s.jpg" % lighthouse):
            self.assertFalse(os.path.exists(os.path.join(out, *rel.split("/"))), rel)
        for path in mine:
            self.assertTrue(os.path.isfile(path), path)
        self.assertNotIn("Lighthouse at dusk", self.by_title(out))

    def test_refuses_folders_it_must_not_write_to(self):
        foreign = self.out("foreign")
        os.makedirs(foreign)
        with open(os.path.join(foreign, "photo.jpg"), "wb") as handle:
            handle.write(b"precious")
        for out in (foreign, self.family, os.path.join(self.family, "site"), self.other,
                    self.base.path, common.data_dir(), common.exports_dir(), export.state_dir(),
                    os.path.join(export.state_dir(), "x"), common.previews_dir(),
                    os.path.join(common.BASE_DIR, "docs"), common.BASE_DIR,
                    os.path.dirname(self.family)):
            with self.assertRaises(export.ExportError, msg=out):
                self.export(out)
        self.assertEqual(os.listdir(foreign), ["photo.jpg"])
        self.assertFalse(os.path.exists(os.path.join(self.family, "site")))
        # A file is no folder.
        with self.assertRaises(export.ExportError):
            self.export(os.path.join(foreign, "photo.jpg"))
        # A damaged marker is not trusted.
        damaged = self.out("damaged")
        os.makedirs(damaged)
        with open(os.path.join(damaged, export.MARKER), "w", encoding="utf-8") as handle:
            handle.write("{\"export_id\": \"../../x\"}")
        with self.assertRaises(export.ExportError):
            self.export(damaged)
        # A zip inside the program folder only where git ignores it.
        config = common.load_config()
        with self.assertRaises(export.ExportError):
            export.check_zip_path(os.path.join(common.BASE_DIR, "docs.zip"), config)
        export.check_zip_path(os.path.join(common.BASE_DIR, "site.zip"), config)
        with self.assertRaises(export.ExportError):
            export.check_zip_path(os.path.join(self.family, "x.zip"), config)

    def test_site_of_an_older_version_is_replaced(self):
        out = self.out("older")
        for rel, data in (("index.html", b"old"), ("site.js", b"old"), ("app.js", b"old"),
                          ("images/12.jpg", b"old"), ("data/data-0000.json", b"[]"),
                          ("data/manifest.json", b"{}"), ("data/locations.json", b"[]"),
                          ("tiles/0/0/0.jpg", b"old"), ("notes.txt", b"mine")):
            path = os.path.join(out, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        with open(os.path.join(out, export.MARKER), "w", encoding="utf-8") as handle:
            json.dump({"format": 1, "generated": 1700000000, "shards": [[[12, "x"]]]}, handle)
        self.export(out)
        files = set(files_of(out))
        for rel in ("site.js", "app.js", "images/12.jpg", "data/data-0000.json",
                    "data/manifest.json", "data/locations.json", "tiles/0/0/0.jpg"):
            self.assertNotIn(rel, files)
        self.assertIn("notes.txt", files)
        self.assertIn("index.html", files)
        self.assertFalse(os.path.exists(os.path.join(out, "tiles")))

    def test_flagged_pictures_can_be_included(self):
        out = self.out("included")
        ids = [self.ids["Screenshot_2019-05-14-14-23-01.png"], self.ids["undescribed.jpg"],
               self.ids["hidden.jpg"]]
        summary = self.export(out, {"include_ids": ids})
        titles = self.titles(read_records(out))
        self.assertIn("Phone screenshot", titles)
        self.assertNotIn("Hidden penguin", titles)        # hidden: never
        self.assertEqual(titles.count(""), 1)              # the undescribed one
        records = read_records(out)
        # The lighthouse and its (now exported) copy are a stack again.
        stacked = [record for record in records if "dg" in record and record.get("t") in
                   ("Lighthouse at dusk", None)]
        self.assertEqual(len(stacked), 2)
        self.assertEqual(summary["excluded"].get("hidden"), 1)
        self.assertNotIn("screenshot", summary["excluded"])
        # Options can also switch a whole kind on.
        summary = self.export(out, {"exclude_types": ["document"], "exclude_private": False,
                                    "exclude_sensitive": False})
        titles = self.titles(read_records(out))
        for title in ("Phone screenshot", "Bank card", "Beach outfit"):
            self.assertIn(title, titles)
        self.assertNotIn("Tax letter", titles)

    def test_preview_counts_flags_and_size(self):
        config = common.load_config()
        preview = export.preview_export(self.conn, config, self.library_id)
        counts = preview["counts"]
        self.assertEqual(counts["total"], len(self.ids))
        self.assertEqual(counts["by_reason"], {
            "hidden": 1, "undescribed": 1, "screenshot": 1, "document": 1, "private": 1,
            "sensitive": 1})
        self.assertEqual(counts["exported"], len(self.ids) - 6)
        flagged = {item["id"]: item for item in preview["flagged"]}
        self.assertEqual(flagged[self.ids["private.jpg"]]["reason"], "private")
        self.assertEqual(flagged[self.ids["private.jpg"]]["title"], "Bank card")
        self.assertTrue(flagged[self.ids["private.jpg"]]["excluded"])
        self.assertNotIn(self.ids["hidden.jpg"], flagged)
        self.assertGreater(preview["size_estimate"], 100000)
        self.assertEqual(preview["size_estimate"], sum(preview["size_parts"].values()))
        # What each flagged picture would add (the dialog counts choices itself).
        card = flagged[self.ids["private.jpg"]]
        self.assertGreater(card["bytes"], 1000)
        self.assertEqual(card["bytes"], sum(card["parts"].values()))
        self.assertFalse(card["needs_folder"])
        self.assertEqual((counts["needs_folder"], counts["unavailable"]), (0, 0))
        # Including one by id leaves it flagged but no longer excluded.
        preview = export.preview_export(self.conn, config, self.library_id,
                                        {"include_ids": [self.ids["private.jpg"]],
                                         "images": "none"})
        flagged = {item["id"]: item for item in preview["flagged"]}
        self.assertFalse(flagged[self.ids["private.jpg"]]["excluded"])
        self.assertEqual(preview["counts"]["exported"], len(self.ids) - 5)
        self.assertEqual(preview["size_parts"]["images"], 0)
        with self.assertRaises(export.ExportError):
            export.preview_export(self.conn, config, "lib_nonexist")
        # Originals are read from the library's folder: when it cannot be
        # reached, the review says how many pictures need it.
        preview = export.preview_export(self.conn, config, self.library_id,
                                        {"images": "original"}, reachable=False)
        exported = preview["counts"]["exported"]
        self.assertEqual(preview["counts"]["needs_folder"], exported)
        self.assertEqual(preview["counts"]["unavailable"], exported)
        preview = export.preview_export(self.conn, config, self.library_id,
                                        {"images": "original"}, reachable=True)
        self.assertEqual(preview["counts"]["unavailable"], 0)

    def test_older_descriptions_are_not_checked(self):
        # Described by the version 4 prompt: no photo type, no private or
        # sensitive flag. It might be an ID card, so it is left out (and
        # shown in the review) until it is described again.
        path = "text.png"
        old = {"description": "photo page of a travel document with name and number",
               "subjects": ["passport"], "text": "P<UTODOE<<JANE", "tags": ["document"],
               "colors": ["blue"]}
        self.set_info(path, old, prompt_ver=4)
        config = common.load_config()
        preview = export.preview_export(self.conn, config, self.library_id)
        flagged = {item["id"]: item for item in preview["flagged"]}
        item = flagged[self.ids[path]]
        self.assertEqual((item["reasons"], item["excluded"]), (["unchecked"], True))
        self.assertEqual(preview["counts"]["by_reason"]["unchecked"], 1)
        out = self.out("older")
        summary = self.export(out)
        self.assertEqual(summary["excluded"]["unchecked"], 1)
        for name in os.listdir(os.path.join(out, "data")):
            with open(os.path.join(out, "data", name), encoding="ascii") as handle:
                self.assertNotIn("travel document", handle.read(), name)
        # Published when pictures that were not checked may be.
        summary = self.export(out, {"exclude_undescribed": False})
        self.assertNotIn("unchecked", summary["excluded"])
        self.assertNotIn("undescribed", summary["excluded"])

    def set_info(self, path, value, prompt_ver=5):
        """Give a picture another description for one test (the old one comes back after)."""
        row = self.conn.execute("SELECT info, prompt_ver FROM images WHERE library_id = ? AND "
                                "path = ?", (self.library_id, path)).fetchone()

        def restore():
            self.conn.execute("UPDATE images SET info = ?, prompt_ver = ? WHERE library_id = ? "
                              "AND path = ?", (row["info"], row["prompt_ver"],
                                               self.library_id, path))
            self.conn.commit()
        self.addCleanup(restore)
        self.conn.execute("UPDATE images SET info = ?, prompt_ver = ? WHERE library_id = ? AND "
                          "path = ?", (json.dumps(value), prompt_ver, self.library_id, path))
        self.conn.commit()

    def test_document_types_count_whatever_the_photo_type(self):
        cases = {
            # path: (photo type, document type, the flags, published by default)
            "text.png": ("scan", "letter", ["document", "scan"], False),
            FOLDER + "/Trip/" + FILE_PREFIX + "_0002.jpg": ("photo", "receipt", ["document"],
                                                             False),
            FOLDER + "/Trip/" + FILE_PREFIX + "_0002 copy.jpg": ("photo", "chat",
                                                                  ["screenshot"], False),
            "pano.jpg": ("photo", "menu", ["writing"], True),
            FOLDER + "/Trip/" + FILE_PREFIX + "_0001.jpg": ("other", "card_or_id",
                                                             ["private"], False),
        }
        for path, (kind, document_type, _flags, _published) in cases.items():
            self.set_info(path, info("Paper " + path[-6:], ["paper"], photo_type=kind,
                                     document_type=document_type))
        config = common.load_config()
        preview = export.preview_export(self.conn, config, self.library_id)
        flagged = {item["id"]: item for item in preview["flagged"]}
        for path, (_kind, _type, flags, published) in cases.items():
            item = flagged[self.ids[path]]
            self.assertEqual(item["reasons"], flags, path)
            self.assertEqual(item["excluded"], not published, path)
        # With documents and screenshots allowed, the letter, receipt and
        # chat are published; the ID card is still private.
        preview = export.preview_export(self.conn, config, self.library_id,
                                        {"exclude_types": []})
        flagged = {item["id"]: item for item in preview["flagged"]}
        for path, (_kind, _type, flags, _published) in cases.items():
            self.assertEqual(flagged[self.ids[path]]["excluded"], "private" in flags, path)

    def test_a_choice_to_publish_ends_with_a_new_flag(self):
        config = common.load_config()
        blank = self.ids["undescribed.jpg"]
        options = {"include_ids": [[blank, ["undescribed"]]]}
        preview = export.preview_export(self.conn, config, self.library_id, options)
        item = {entry["id"]: entry for entry in preview["flagged"]}[blank]
        self.assertFalse(item["excluded"])
        self.assertNotIn("new_flags", item)
        # Described later: it shows private information.
        self.set_info("undescribed.jpg", info("Bank statement", ["bank"], private_info=True))
        self.conn.execute("UPDATE images SET described = 1 WHERE id = ?", (blank,))
        self.conn.commit()
        self.addCleanup(self.conn.commit)
        self.addCleanup(self.conn.execute, "UPDATE images SET described = 0 WHERE id = ?",
                        (blank,))
        preview = export.preview_export(self.conn, config, self.library_id, options)
        item = {entry["id"]: entry for entry in preview["flagged"]}[blank]
        self.assertTrue(item["excluded"])
        self.assertEqual(item["new_flags"], ["private"])
        out = self.out("new-flag")
        summary = self.export(out, options)
        self.assertNotIn("Bank statement", self.titles(read_records(out)))
        self.assertEqual(summary["excluded"]["private"], 2)
        # Chosen again with its flags of now; or a plain id (whatever the flags).
        for again in ([[blank, ["private"]]], [blank]):
            self.export(out, {"include_ids": again})
            self.assertIn("Bank statement", self.titles(read_records(out)), again)

    def test_pictures_of_one_day_keep_the_order_they_were_taken_in(self):
        first = FOLDER + "/Trip/" + FILE_PREFIX + "_0002.jpg"
        second = FOLDER + "/Trip/" + FILE_PREFIX + "_0002 copy.jpg"
        before = {row["path"]: (row["taken_at"], row["taken_source"]) for row in
                  self.conn.execute("SELECT path, taken_at, taken_source FROM images")}

        def restore():
            for path in (first, second):
                self.conn.execute("UPDATE images SET taken_at = ?, taken_source = ? "
                                  "WHERE library_id = ? AND path = ?",
                                  before[path] + (self.library_id, path))
            self.conn.commit()
        self.addCleanup(restore)
        # The picture with the higher id was taken first.
        if self.ids[first] > self.ids[second]:
            first, second = second, first
        for path, taken in ((first, "2020-07-01T09:00:01"), (second, "2020-07-01T09:00:00")):
            self.conn.execute("UPDATE images SET taken_at = ?, taken_source = 'exif' "
                              "WHERE library_id = ? AND path = ?",
                              (taken, self.library_id, path))
        self.conn.commit()
        out = self.out("order")
        self.export(out)
        records = {record["i"]: record for record in read_records(out)}
        titles = {path: info_title for path, info_title in (
            (FOLDER + "/Trip/" + FILE_PREFIX + "_0002.jpg", "Cat on a sofa"),
            (FOLDER + "/Trip/" + FILE_PREFIX + "_0002 copy.jpg", "Cat on a sofa again"))}
        by_title = {record.get("t"): record for record in records.values()}
        self.assertEqual((by_title[titles[second]]["tn"], by_title[titles[first]]["tn"]),
                         (1, 2))
        for record in records.values():
            self.assertNotIn("ts", record)
            if record.get("dk") != "2020-07-01":
                self.assertNotIn("tn", record)

    def test_a_folder_of_another_library_is_refused(self):
        out = self.out("shared")
        self.export(out)
        config = common.load_config()
        with self.assertRaises(export.ExportError) as caught:
            export.export_library(jobs.JobContext("test"), self.conn, config, self.other_id,
                                  out)
        self.assertIn("holds the export of library Family", str(caught.exception))
        self.assertIn("Lighthouse at dusk", self.titles(read_records(out)))
        with self.assertRaises(export.ExportError):
            export.check_output_dir(out, config, self.other_id)
        export.check_output_dir(out, config, self.library_id)

    def test_write_errors_stop_the_export_in_plain_words(self):
        out = self.out("full")
        real = export._write_replacing
        calls = []

        def full(path, data):
            calls.append(path)
            if len(calls) > 3:
                raise OSError(errno.ENOSPC, "No space left on device", path)
            return real(path, data)
        with mock.patch.object(export, "_write_replacing", full):
            with self.assertRaises(export.ExportError) as caught:
                self.export(out)
        message = str(caught.exception)
        self.assertIn("is full", message)
        self.assertNotIn("Errno", message)
        # It stopped at once: not every picture was tried.
        self.assertLess(len(calls), 12)
        # Run again with room: it finishes.
        summary = self.export(out)
        self.assertEqual(summary["failed"], 0)

    def test_not_enough_free_space_is_refused_before_writing_pictures(self):
        out = self.out("tight")
        usage = collections.namedtuple("usage", "total used free")
        with mock.patch.object(export.shutil, "disk_usage",
                               return_value=usage(10 ** 9, 10 ** 9, 1000)):
            with self.assertRaises(export.ExportError) as caught:
                self.export(out)
        self.assertIn("not enough free space", str(caught.exception))
        self.assertFalse(os.path.exists(out))

    def test_originals_lose_their_metadata(self):
        out = self.out("originals")
        summary = self.export(out, {"images": "original"})
        self.assertEqual(summary["failed"], 0)
        records = self.by_title(out)
        manifest = read_manifest(out)
        self.assertTrue(manifest["capabilities"]["originals"])
        gps = records["Eiffel picnic"]
        path = os.path.join(out, "images", gps["i"] + ".jpg")
        with open(path, "rb") as handle:
            data = handle.read()
        # Lossless: the same JPEG image data, only the metadata is gone
        # (the orientation stays, so the picture is shown upright).
        self.assert_clean_jpeg(data, "gps.jpg", orientation=6)
        with open(os.path.join(self.family, "gps.jpg"), "rb") as handle:
            original = handle.read()
        scan = original.index(b"\xff\xda")
        self.assertIn(original[scan:original.index(b"\xff\xd9", scan)], data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (400, 300))
        # A PNG stays a PNG, without its text chunks.
        drawing = records["Blue drawing"]
        self.assertEqual(drawing["ix"], "png")
        self.assertTrue(os.path.isfile(os.path.join(out, "images", drawing["i"] + ".png")))
        if self.heic:
            flower = records["Heic flower"]
            self.assertNotIn("ix", flower)
            with Image.open(os.path.join(out, "images", flower["i"] + ".jpg")) as image:
                self.assertEqual(image.format, "JPEG")
        self.assert_private(out, originals=True)

    def test_thumbnails_only(self):
        out = self.out("thumbs-only")
        self.export(out, {"images": "none"})
        files = set(files_of(out))
        self.assertFalse([rel for rel in files if rel.startswith(("images/", "panos/"))])
        self.assertFalse(read_manifest(out)["capabilities"]["images"])

    def test_location_adds_the_map(self):
        out = self.out("located")
        self.export(out, {"location": True, "credits": "Photos <by> me & co"})
        records = self.by_title(out)
        picnic = records["Eiffel picnic"]
        self.assertEqual(picnic["gp"], 1)
        self.assertIn(LANDMARK, picnic["ex"])
        self.assertIn(PLACE_GUESS, picnic["ex"])
        files = set(files_of(out))
        for rel in ("map.html", "assets/map.js", "data/locations.js", "tiles/0/0/0.jpg"):
            self.assertIn(rel, files)
        with open(os.path.join(out, "data", "locations.js"), encoding="ascii") as handle:
            text = handle.read()
        points = json.loads(text[text.index("["):text.rindex("]") + 1])
        self.assertEqual(points, [{"id": picnic["i"], "lat": round(GPS[0], 4),
                                   "lon": round(GPS[1], 4)}])
        manifest = read_manifest(out)
        self.assertEqual(manifest["locations"], "locations.js")
        self.assertTrue(manifest["capabilities"]["map"])
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('<footer class="export-credits">Photos &lt;by&gt; me &amp; co</footer>',
                      html)
        # Still no GPS in the pictures themselves.
        with open(os.path.join(out, "images", picnic["i"] + ".jpg"), "rb") as handle:
            self.assert_clean_jpeg(handle.read(), "picnic")
        # Turning location off again removes all of it.
        self.export(out)
        files = set(files_of(out))
        self.assertFalse([rel for rel in files if rel.startswith("tiles/")
                          or rel in ("map.html", "assets/map.js", "data/locations.js")])
        self.assert_private(out)

    def tile_writes(self):
        """Counts the tiles the export writes (or reads to compare) while patched."""
        written = []
        real = export.Writer.write

        def spy(writer, rel, data, sig=None):
            if rel.startswith("tiles/"):
                written.append(rel)
            return real(writer, rel, data, sig)

        return written, mock.patch.object(export.Writer, "write", spy)

    def test_map_tiles_are_copied_only_when_they_changed(self):
        # A small pyramid (zoom 0 and 1) in the data folder, one tile cut short.
        tiles = common.tiles_dir()
        self.addCleanup(support.remove_tree, tiles)
        sources = {}
        for zoom in (0, 1):
            for x in range(2 ** (zoom + 1)):
                for y in range(2 ** zoom):
                    buffer = io.BytesIO()
                    Image.new("RGB", (256, 256), (zoom * 90, x * 40, y * 90)).save(
                        buffer, format="JPEG", quality=70)
                    rel = "%d/%d/%d.jpg" % (zoom, x, y)
                    sources[rel] = buffer.getvalue()
        sources["1/3/1.jpg"] = sources["1/3/1.jpg"][:200]
        for rel, data in sources.items():
            path = os.path.join(tiles, *rel.split("/"))
            common.ensure_dir(os.path.dirname(path))
            with open(path, "wb") as handle:
                handle.write(data)
        with open(os.path.join(tiles, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"max_zoom": 1, "tile_size": 256}, handle)
        out = self.out("tiled")
        warnings = []
        ctx = jobs.JobContext("test")
        real_log = ctx.log
        ctx.log = lambda message, level="info": (warnings.append(message) if level == "warn"
                                                 else None, real_log(message, level))
        written, patch = self.tile_writes()
        with patch:
            self.export(out, {"location": True}, ctx=ctx)
        files = files_of(out)
        copied = sorted(rel for rel in files if rel.startswith("tiles/"))
        self.assertEqual(copied, sorted("tiles/" + rel for rel in sources if rel != "1/3/1.jpg"))
        self.assertEqual(len(written), 9)
        for rel in copied:
            with open(os.path.join(out, *rel.split("/")), "rb") as handle:
                self.assertEqual(handle.read(), sources[rel[len("tiles/"):]], rel)
        self.assertTrue(any("1 map tile(s)" in text and "make_tiles.py" in text
                            for text in warnings), warnings)
        config = json.loads(re.search(r'id="app-config">(.*?)</script>',
                                      open(os.path.join(out, "map.html"),
                                           encoding="utf-8").read()).group(1))
        self.assertEqual(config["tilesMax"], 1)
        self.assertEqual(config["tiles"], "tiles/")
        # Again: not one tile is read or written.
        del written[:]
        with patch:
            summary = self.export(out, {"location": True})
        self.assertEqual(written, [])
        self.assertEqual(summary["written"], 0)
        self.assertEqual({rel: value for rel, value in files_of(out).items()
                          if rel.startswith("tiles/")},
                         {rel: files[rel] for rel in copied})
        # One tile changed, one repaired (make_tiles.py again), one gone:
        # just those change in the export.
        changed = io.BytesIO()
        Image.new("RGB", (256, 256), (250, 250, 0)).save(changed, format="JPEG")
        repaired = io.BytesIO()
        Image.new("RGB", (256, 256), (0, 250, 250)).save(repaired, format="JPEG")
        time.sleep(0.02)
        with open(os.path.join(tiles, "1", "0", "0.jpg"), "wb") as handle:
            handle.write(changed.getvalue())
        with open(os.path.join(tiles, "1", "3", "1.jpg"), "wb") as handle:
            handle.write(repaired.getvalue())
        os.remove(os.path.join(tiles, "1", "2", "1.jpg"))
        with patch:
            summary = self.export(out, {"location": True})
        self.assertEqual(sorted(written), ["tiles/1/0/0.jpg", "tiles/1/3/1.jpg"])
        after = files_of(out)
        self.assertNotIn("tiles/1/2/1.jpg", after)
        with open(os.path.join(out, "tiles", "1", "3", "1.jpg"), "rb") as handle:
            self.assertEqual(handle.read(), repaired.getvalue())
        for rel in copied:
            if rel not in ("tiles/1/0/0.jpg", "tiles/1/3/1.jpg", "tiles/1/2/1.jpg"):
                self.assertEqual(after[rel], files[rel], rel)

    def test_the_small_map_is_cut_into_tiles_once(self):
        import serve
        out = self.out("small-map")
        self.export(out, {"location": True})
        tiles = sorted(rel for rel in files_of(out) if rel.startswith("tiles/"))
        # Zoom 0 to 3 of the bundled map: 2 + 8 + 32 + 128 tiles.
        self.assertEqual(len(tiles), 170)
        written, patch = self.tile_writes()
        with patch, mock.patch.object(serve, "_fallback_tile_bytes",
                                      side_effect=AssertionError("cut again")):
            summary = self.export(out, {"location": True})
        self.assertEqual(written, [])
        self.assertEqual(summary["written"], 0)
        # Once the cutting changes (its version is raised), they are cut again.
        written, patch = self.tile_writes()
        with patch, mock.patch.object(serve, "FALLBACK_TILE_VERSION",
                                      serve.FALLBACK_TILE_VERSION + 1):
            self.export(out, {"location": True})
        self.assertEqual(len(written), 170)

    def test_title_credits_and_indexing(self):
        out = self.out("titled")
        self.export(out, {"title": "Our <Holidays>", "allow_indexing": True})
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("<title>Our &lt;Holidays&gt;</title>", html)
        self.assertNotIn("robots", html)
        self.assertNotIn("<Holidays>", html)
        config = json.loads(re.search(r'id="app-config">(.*?)</script>', html).group(1))
        self.assertEqual(config["title"], "Our <Holidays>")
        self.assertEqual(config["libraryList"][0]["name"], "Our <Holidays>")
        self.assertEqual(read_manifest(out)["library"]["name"], "Our <Holidays>")
        # The library's own name is not used when a title is given.
        self.assertNotIn("Family", html)

    def test_a_stop_keeps_track_of_what_was_written(self):
        out = self.out("stopped")
        ctx = jobs.JobContext("test")
        steps = []
        original = ctx.step

        def step(*args, **kwargs):
            original(*args, **kwargs)
            steps.append(1)
            if len(steps) == 2:
                ctx.request_stop()

        ctx.step = step
        summary = self.export(out, ctx=ctx)
        self.assertTrue(summary["stopped"])
        state = export.load_state(summary["export_id"])
        written = [rel for rel in state["files"] if rel.startswith("thumbs/")]
        self.assertTrue(written)
        self.assertFalse(os.path.exists(os.path.join(out, "index.html")))
        summary = self.export(out)
        self.assertFalse(summary["stopped"])
        self.assertTrue(os.path.isfile(os.path.join(out, "index.html")))

    def run_script(self, *args):
        """Run a program script in a child process (with this class's data root)."""
        return subprocess.run([sys.executable] + list(args), cwd=support.REPO,
                              env=support.child_env(), capture_output=True, text=True,
                              timeout=120)

    def test_its_own_server_answers_range_requests(self):
        # Browsers move in a video with range requests; python -m http.server
        # answers them with the whole file, so seeking fails there.
        import http.client
        import threading
        folder = self.out("served")
        os.makedirs(os.path.join(folder, "videos"))
        body = bytes(range(256)) * 40
        with open(os.path.join(folder, "videos", "clip.mp4"), "wb") as handle:
            handle.write(body)
        with open(os.path.join(folder, "index.html"), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write("<!doctype html><title>Site</title>\n")
        server = export.make_server(folder, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.server_address[0], "127.0.0.1")

        def get(path, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                              timeout=10)
            try:
                conn.request("GET", path, headers=headers or {})
                answer = conn.getresponse()
                return answer.status, dict(answer.getheaders()), answer.read()
            finally:
                conn.close()
        status, headers, data = get("/videos/clip.mp4", {"Range": "bytes=100-199"})
        self.assertEqual((status, data), (206, body[100:200]))
        self.assertEqual(headers["Content-Range"], "bytes 100-199/%d" % len(body))
        self.assertEqual(headers["Content-Length"], "100")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        # Open-ended and from the end.
        self.assertEqual(get("/videos/clip.mp4", {"Range": "bytes=10200-"})[2], body[10200:])
        self.assertEqual(get("/videos/clip.mp4", {"Range": "bytes=-5"})[2], body[-5:])
        # Past the end: 416. Without a range, the whole file (ranges offered).
        self.assertEqual(get("/videos/clip.mp4", {"Range": "bytes=99999-"})[0], 416)
        status, headers, data = get("/videos/clip.mp4")
        self.assertEqual((status, data, headers["Accept-Ranges"]), (200, body, "bytes"))
        self.assertEqual(get("/")[0], 200)
        self.assertEqual(get("/nothing.js", {"Range": "bytes=0-1"})[0], 404)
        # A folder without a site is refused.
        with self.assertRaises(export.ExportError):
            export.serve_folder(os.path.join(folder, "videos"))

    def test_command_lines(self):
        out = self.out("cli")
        result = self.run_script("export.py", "--library", "Family", "--out", out,
                                 "--images", "none", "--include-screenshots", "--zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        # (Its own server, which answers range requests: videos can be moved in.)
        self.assertIn("export.py\" --serve \"%s\"" % out, result.stdout)
        # What was left out, and why.
        self.assertIn("Left out: ", result.stdout)
        self.assertIn("1 picture with private information", result.stdout)
        titles = self.titles(read_records(out))
        self.assertIn("Phone screenshot", titles)
        self.assertNotIn("Tax letter", titles)
        self.assertTrue(os.path.isfile(out + ".zip"))
        self.assertFalse(read_manifest(out)["capabilities"]["images"])
        result = self.run_script("export.py", "--library", "No such library")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # "serve.py package" needs --library: two libraries are enabled.
        packaged = self.out("cli-package")
        result = self.run_script("serve.py", "package", packaged)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--library", result.stderr)
        self.assertFalse(os.path.exists(packaged))
        result = self.run_script("serve.py", "package", "--library", "Family", packaged)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(packaged, "index.html")))
        # The folder the library went to last (for "Open folder").
        self.assertEqual(export.last_export_dir(common.load_config(), self.library_id),
                         (packaged, False))
        self.assertEqual(export.last_export_dir(common.load_config(), self.other_id),
                         (None, False))
        # More switches: pictures that were not checked, private, sensitive.
        result = self.run_script("export.py", "--library", "Family", "--out", out,
                                 "--images", "none", "--include-undescribed",
                                 "--include-private", "--include-sensitive")
        self.assertEqual(result.returncode, 0, result.stderr)
        titles = self.titles(read_records(out))
        for title in ("Bank card", "Beach outfit", ""):
            self.assertIn(title, titles)
        self.assertNotIn("Phone screenshot", titles)
        # Stopped (an abort file): status 2, and no "Open" line.
        abort = common.abort_path()
        common.ensure_dir(os.path.dirname(abort))
        with open(abort, "w", encoding="utf-8") as handle:
            handle.write("stop")
        try:
            result = self.run_script("export.py", "--library", "Family", "--out",
                                     self.out("cli-stopped"))
        finally:
            os.remove(abort)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("stopped", result.stdout)
        self.assertNotIn("Open ", result.stdout)
        # serve.py package shows its progress like export.py.
        result = self.run_script("serve.py", "package", "--library", "Family", packaged)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\[[0-9]{2}:[0-9]{2}:[0-9]{2}\] export")
        self.assertIn("Left out: ", result.stdout)
        # The folder of the last export was moved away.
        moved = packaged + "-moved"
        os.rename(packaged, moved)
        self.addCleanup(support.remove_tree, moved)
        self.assertEqual(export.last_export_dir(common.load_config(), self.library_id),
                         (packaged, True))

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_the_pages_load_and_search_the_export(self):
        out = self.out("node")
        # Small data files, so the records and details are spread over
        # several of them (the pages must find each picture's details).
        with mock.patch.object(export, "INDEX_SHARD_BYTES", 1000), \
                mock.patch.object(export, "DETAIL_SHARD_BYTES", 40):
            self.export(out)
        manifest = read_manifest(out)
        self.assertGreater(len(manifest["shards"]), 1)
        self.assertGreater(len(manifest["details"]), 1)
        details = read_details(out)
        titles = {record["i"]: record.get("t", "") for record in read_records(out)}
        expected = {title: details.get(xid) for xid, title in titles.items()}
        self.assertEqual(expected["Eiffel picnic"], {
            "taken_at": "2019-05-14", "taken_source": "exif",
            "exif": {"ExposureTime": 0.008, "FNumber": 2.8, "ISOSpeedRatings": 100}})
        cats = ["Cat on a sofa", "Cat on a sofa again"]
        self.run_node(out, {
            "page": "index.html", "count": 7 if self.heic else 6,
            "queries": [
                {"query": "lighthouse", "titles": ["Lighthouse at dusk"]},
                {"query": "cat", "titles": cats},
                {"query": "cat", "options": {"group": True}, "count": 1},
                {"query": "screenshot", "titles": []},
                {"query": "type:artwork", "titles": ["Blue drawing"]},
                {"query": "landmark", "titles": []},
                {"query": "is:360", "titles": ["Mountain panorama"]},
                {"query": "year:2019 picnic", "titles": ["Eiffel picnic"]},
            ],
            "details": expected,
        })
        # One data file fails to load (a slow host, say): the page loads
        # the index when it tries again, and never looks for an older
        # package's files.
        self.run_node(out, {"page": "index.html", "count": 7 if self.heic else 6,
                            "fail_once": manifest["shards"][-1]})

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_the_pages_load_an_export_with_location_and_originals(self):
        out = self.out("node-located")
        self.export(out, {"location": True, "images": "original", "file_names": True,
                          "folders": True})
        self.run_node(out, {
            "page": "map.html", "allow": ["n", "f"], "locations": 1,
            "queries": [{"query": "landmark", "titles": ["Eiffel picnic"]},
                        {"query": "has:gps", "titles": ["Eiffel picnic"]},
                        {"query": "folder:trip", "titles": ["Cat on a sofa",
                                                            "Cat on a sofa again",
                                                            "Lighthouse at dusk"]}],
        })


@unittest.skipIf(imagegen is None, "Pillow is not installed")
class BadPictureTest(support.TempBaseMixin, unittest.TestCase):
    """Pictures that cannot be read: skipped, or kept from the last export."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Family")
        shutil.copyfile(os.path.join(SAMPLES, "01-cat.jpg"), os.path.join(self.folder, "cat.jpg"))
        self.base.write_config([support.library("Family", self.folder)])
        db.init(log=quiet)
        self.config = common.load_config()
        import scan
        scan.run(self.config, jobs.JobContext("test"), describe_images=False)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.library_id = self.config["libraries"][0]["id"]

    def describe_all(self):
        self.conn.execute("UPDATE images SET described = 1, prompt_ver = 5, info = ?",
                          (json.dumps(info("Something", ["thing"])),))
        self.conn.commit()

    def add_ghosts(self, count):
        """Rows whose file and thumbnail are gone (the scan has not noticed)."""
        for number in range(count):
            self.conn.execute(
                "INSERT INTO images (library_id, path, name, size, mtime, added, updated) "
                "VALUES (?, ?, ?, 10, 1, 1, 1)",
                (self.library_id, "ghost%d.jpg" % number, "ghost%d.jpg" % number))
        self.conn.commit()

    def export(self, lines, options=None):
        return export.export_library(export.LogContext(lines.append), self.conn, self.config,
                                     self.library_id, options=options)

    def test_one_bad_picture_never_stops_the_export(self):
        self.add_ghosts(1)
        self.describe_all()
        lines = []
        summary = self.export(lines)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["excluded"], {"failed": 1})
        self.assertTrue(any("ghost0.jpg" in line for line in lines), lines)
        self.assertEqual(len(read_records(summary["output"])), 1)
        self.assertEqual(summary["output"], os.path.join(common.exports_dir(), "family"))

    def test_many_bad_pictures_are_counted_not_all_listed(self):
        self.add_ghosts(export.LOGGED_FAILURES + 5)
        self.describe_all()
        lines = []
        summary = self.export(lines)
        self.assertEqual(summary["failed"], export.LOGGED_FAILURES + 5)
        listed = [line for line in lines if "ghost" in line]
        self.assertEqual(len(listed), export.LOGGED_FAILURES)
        self.assertTrue(any("counted, not listed" in line for line in lines))

    def test_an_unreadable_picture_keeps_its_earlier_files(self):
        self.describe_all()
        lines = []
        first = self.export(lines, {"images": "original"})
        self.assertEqual(first["count"], 1)
        before = files_of(first["output"])
        # The library goes offline, and the original seems to have changed
        # (an unchanged one is not even read again).
        os.rename(self.folder, self.folder + "-away")
        self.addCleanup(os.rename, self.folder + "-away", self.folder)
        self.conn.execute("UPDATE images SET mtime = mtime + 10")
        self.conn.commit()
        lines = []
        second = self.export(lines, {"images": "original"})
        self.assertEqual((second["count"], second["kept"], second["failed"]), (1, 1, 0))
        self.assertEqual(second["removed"], 0)
        self.assertEqual(files_of(second["output"]), before)
        self.assertTrue(any("not available" in line for line in lines), lines)
        self.assertTrue(any("are kept" in line for line in lines), lines)
        # Thumbnails only: the thumbnail is in the data folder, nothing to keep.
        third = self.export(lines, {"images": "none"})
        self.assertEqual((third["count"], third["kept"]), (1, 0))
        self.assertFalse(os.path.isdir(os.path.join(third["output"], "images")))
        # Back to the originals: there is nothing earlier to keep now, so
        # nothing can be exported; the folder is left as it was.
        before = files_of(third["output"])
        with self.assertRaises(export.ExportError) as caught:
            self.export(lines, {"images": "original"})
        self.assertIn("not available", str(caught.exception))
        self.assertIn("Web size", str(caught.exception))
        self.assertEqual(files_of(third["output"]), before)

    def test_an_offline_library_is_refused_before_anything_is_written(self):
        self.describe_all()
        os.rename(self.folder, self.folder + "-away")
        self.addCleanup(os.rename, self.folder + "-away", self.folder)
        lines = []
        with self.assertRaises(export.ExportError) as caught:
            self.export(lines, {"images": "original"})
        self.assertIn("not available", str(caught.exception))
        # Nothing was written: no folder, no record of an export.
        out = os.path.join(common.exports_dir(), "family")
        self.assertFalse(os.path.exists(out))
        self.assertIsNone(export.last_export(self.library_id))
        # Web size uses the stored previews: that works offline.
        summary = self.export(lines)
        self.assertEqual((summary["count"], summary["failed"]), (1, 0))

    def test_a_picture_that_fails_leaves_no_files_behind(self):
        self.add_ghosts(1)
        self.describe_all()
        lines = []
        with mock.patch.object(export._Export, "_thumb",
                               side_effect=ValueError("cannot make the thumbnail")):
            with self.assertRaises(export.ExportError) as caught:
                self.export(lines)
        self.assertIn("None of the 2 pictures", str(caught.exception))
        out = os.path.join(common.exports_dir(), "family")
        # The web-size picture written before its thumbnail failed is gone.
        self.assertFalse(os.path.isdir(os.path.join(out, "images")) and
                         os.listdir(os.path.join(out, "images")))
        # A normal export: its count is what it published, not its thumbnails.
        summary = self.export(lines)
        self.assertEqual((summary["count"], summary["failed"]), (1, 1))
        self.assertEqual(sorted(os.listdir(os.path.join(out, "images"))),
                         sorted(os.listdir(os.path.join(out, "thumbs"))))
        self.assertEqual(export.last_export(self.library_id)["pictures"], 1)
        self.assertEqual(export.load_state(summary["export_id"])["count"], 1)


class OptionsTest(unittest.TestCase):

    def test_defaults(self):
        options = export.validate_options(None)
        self.assertEqual(options, export.DEFAULT_OPTIONS)
        self.assertIsNot(options["exclude_types"], export.DEFAULT_OPTIONS["exclude_types"])
        self.assertEqual(options["images"], "web")
        self.assertFalse(options["location"] or options["file_names"] or options["folders"])
        self.assertEqual(options["exclude_types"], ["screenshot", "document"])

    def test_bad_options_are_named(self):
        with self.assertRaises(export.OptionsError) as caught:
            export.validate_options({"images": "huge", "location": "yes", "colour": 1,
                                     "include_ids": [1, "2"], "exclude_types": ["selfie"],
                                     "title": "x" * 500})
        self.assertEqual(sorted(caught.exception.errors),
                         ["colour", "exclude_types", "images", "include_ids", "location",
                          "title"])
        with self.assertRaises(export.OptionsError):
            export.validate_options(["images"])
        with self.assertRaises(export.OptionsError):
            export.validate_options({"exclude_ids": [True]})

    def test_values_are_cleaned(self):
        options = export.validate_options({"include_ids": [5, 3, 5], "title": "  A \n B ",
                                           "exclude_types": ["document", "scan"]})
        self.assertEqual(options["include_ids"], [3, 5])
        self.assertEqual(options["title"], "A B")
        self.assertEqual(options["exclude_types"], ["document", "scan"])

    def test_pictures_published_despite_their_flags(self):
        # [id, flags]: published while it has no other flag that leaves it out.
        options = export.validate_options({"include_ids": [
            [8, ["undescribed", "private", "private"]], 3, [5, []]]})
        self.assertEqual(options["include_ids"], [3, [5, []], [8, ["private", "undescribed"]]])
        self.assertEqual(export.include_choices(options),
                         {3: None, 5: frozenset(), 8: frozenset(["private", "undescribed"])})
        for bad in ([[8, ["selfie"]]], [[8, "private"]], [["8", []]], [[8]], [[8, [], 1]]):
            with self.assertRaises(export.OptionsError, msg=bad) as caught:
                export.validate_options({"include_ids": bad})
            self.assertEqual(list(caught.exception.errors), ["include_ids"])
        # Saved like that (only options that differ from the defaults).
        self.assertEqual(export.options_to_save(options), {"include_ids": options["include_ids"]})

    def test_flags(self):
        default = export.default_options()

        def reasons(**record):
            return export._reasons(dict(record, ds=1), default)
        self.assertEqual(reasons(ty="photo"), ([], []))
        self.assertEqual(reasons(ty="photo", dt="receipt"), (["document"], ["document"]))
        self.assertEqual(reasons(ty="scan", dt="form"),
                         (["document", "scan"], ["document"]))
        self.assertEqual(reasons(ty="photo", dt="chat"), (["screenshot"], ["screenshot"]))
        self.assertEqual(reasons(ty="photo", dt="sign"), (["writing"], []))
        self.assertEqual(reasons(ty="scan", dt="book_page"), (["scan"], []))
        self.assertEqual(reasons(ty="other", dt="card_or_id"), (["private"], ["private"]))
        self.assertEqual(reasons(ty="photo", pr=1, se="suggestive"),
                         (["private", "sensitive"], ["private", "sensitive"]))
        self.assertEqual(export._reasons({"v4": 1, "ds": 1}, default),
                         (["unchecked"], ["unchecked"]))
        self.assertEqual(export._reasons({}, default), (["undescribed"], ["undescribed"]))
        loose = dict(default, exclude_undescribed=False, exclude_types=[])
        self.assertEqual(export._reasons({"v4": 1, "ds": 1}, loose), (["unchecked"], []))
        self.assertEqual(export._reasons({"ty": "photo", "dt": "letter", "ds": 1}, loose),
                         (["document"], []))
        # Every flag has a name the options know.
        self.assertEqual(len(set(export.FLAGS)), len(export.FLAGS))

    def test_folder_errors_in_plain_words(self):
        full = OSError(errno.ENOSPC, "No space left on device")
        self.assertIn("The disk holding X is full", export.os_error_text(full, "X"))
        denied = PermissionError(errno.EACCES, "Permission denied", "Y")
        self.assertIn("Y cannot be written or read: access is denied",
                      export.os_error_text(denied))
        gone = FileNotFoundError(errno.ENOENT, "No such file", "Z")
        self.assertIn("is its drive connected", export.os_error_text(gone))
        self.assertEqual(export.human_bytes(512), "512 bytes")
        self.assertEqual(export.human_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(export.human_bytes(300 * 1024 ** 3), "300 GB")
        folder = tempfile.mkdtemp(prefix="export-writable-")
        self.addCleanup(support.remove_tree, folder)
        export.check_writable(folder)
        export.check_writable(os.path.join(folder, "new", "deeper"))
        self.assertEqual(os.listdir(folder), [])            # nothing left behind
        with open(os.path.join(folder, "a-file"), "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(export.ExportError) as caught:
            export.check_writable(os.path.join(folder, "a-file", "site"))
        self.assertIn("is a file", str(caught.exception))
        with mock.patch.object(export.os, "mkdir", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(export.ExportError) as caught:
                export.check_writable(os.path.join(folder, "new"))
        self.assertIn("access is denied", str(caught.exception))
        self.assertNotIn("Errno", str(caught.exception))

    def test_saved_options_are_the_base(self):
        library = {"export": {"images": "original", "location": "bad", "zip": True}}
        options = export.library_options(library)
        self.assertEqual(options["images"], "original")
        self.assertIs(options["location"], False)        # a bad saved value: the default
        self.assertTrue(options["zip"])
        options = export.library_options(library, {"images": "none"})
        self.assertEqual(options["images"], "none")
        self.assertTrue(options["zip"])

    def test_default_folders(self):
        with support.TempBase():
            config = {"libraries": [{"id": "lib_a", "name": "Fam\u00edlia 2019!"},
                                    {"id": "lib_b", "name": "State"},
                                    {"id": "lib_c", "name": "Trips"},
                                    {"id": "lib_d", "name": "trips?"}]}
            exports = common.exports_dir()
            folders = [export.default_output_dir(config, library)
                       for library in config["libraries"]]
            self.assertEqual(folders, [os.path.join(exports, "familia-2019"),
                                       os.path.join(exports, "state-library"),
                                       os.path.join(exports, "trips-lib-c"),
                                       os.path.join(exports, "trips-lib-d")])
            self.assertEqual(export.library_slug("\u65e5\u672c"), "")


@unittest.skipIf(imagegen is None, "Pillow is not installed")
class StripTest(unittest.TestCase):

    def pixels(self, data):
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("RGBA").tobytes()

    def test_jpeg(self):
        data = gps_jpeg()
        data = data[:2] + b"\xff\xfe\x00\x09comment" + data[2:]
        clean = export.strip_jpeg(data)
        self.assertEqual(self.pixels(clean), self.pixels(data))
        for secret in SECRETS[:3] + (b"comment",):
            self.assertNotIn(secret, clean)
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual(dict(image.getexif()), {274: 6})
        self.assertNotIn(b"Exif", export.strip_jpeg(data, keep_orientation=False))
        with self.assertRaises(export.StripError):
            export.strip_jpeg(b"not a jpeg")
        # A JPEG cut off in its image data still ends properly.
        cut = imagegen.jpeg_bytes(imagegen.textured_image())[:3000]
        self.assertTrue(export.strip_jpeg(cut).endswith(b"\xff\xd9"))

    def test_png(self):
        data = text_png()
        clean = export.strip_png(data)
        self.assertEqual(self.pixels(clean), self.pixels(data))
        for secret in SECRETS[3:]:
            self.assertNotIn(secret, clean)
        self.assertEqual(png_chunks(clean)[0], b"IHDR")
        with self.assertRaises(export.StripError):
            export.strip_png(data[:40])

    def test_png_palettes_and_profile_names(self):
        # A suggested palette and a profile can carry names (paths, people).
        data = text_png()
        icc = personal_icc()
        extra = (export._png_chunk(b"iCCP", b"PROFILE-NAME-JaneDoe\x00\x00"
                                   + zlib.compress(icc))
                 + export._png_chunk(b"sPLT", b"SPLT-NAME-secret\x00\x08" + bytes(6))
                 + export._png_chunk(b"hIST", bytes(2)))
        end_of_header = 8 + 12 + 13
        data = data[:end_of_header] + extra + data[end_of_header:]
        clean = export.strip_png(data)
        for secret in (b"JaneDoe", b"PROFILE-NAME", b"SPLT-NAME"):
            self.assertNotIn(secret, clean)
        chunks = png_chunks(clean)
        self.assertNotIn(b"sPLT", chunks)
        self.assertNotIn(b"hIST", chunks)
        self.assertIn(b"iCCP", chunks)
        with Image.open(io.BytesIO(clean)) as image:
            image.load()
            self.assertEqual(image.info["icc_profile"], export.scrub_icc(icc))
        self.assertEqual(self.pixels(clean), self.pixels(text_png()))

    def test_colour_profiles_lose_their_texts(self):
        icc = personal_icc()
        clean = export.scrub_icc(icc)
        self.assertEqual(len(clean), len(icc))
        self.assertTrue(has_icc_secret(icc))
        self.assertFalse(has_icc_secret(clean))
        self.assertEqual(clean[84:100], bytes(16))          # the profile id
        self.assertEqual(clean[:84], icc[:84])
        # Not a profile this can read: dropped.
        self.assertIsNone(export.scrub_icc(b"short"))
        self.assertIsNone(export.scrub_icc(icc[:36] + b"xxxx" + icc[40:]))
        broken = bytearray(icc)
        broken[128:132] = struct.pack(">I", 5000)
        self.assertIsNone(export.scrub_icc(bytes(broken)))
        buffer = io.BytesIO()
        imagegen.marker_image((32, 24)).save(buffer, "JPEG", icc_profile=bytes(broken))
        dropped = export.strip_jpeg(buffer.getvalue())
        self.assertNotIn(b"ICC_PROFILE", dropped)
        self.assertFalse(has_icc_secret(dropped))
        # A real profile still works after.
        try:
            from PIL import ImageCms
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        except (ImportError, OSError):
            srgb = None
        if srgb:
            ImageCms.ImageCmsProfile(io.BytesIO(export.scrub_icc(srgb)))
        # In a JPEG (a large profile is split over several segments).
        for size in (0, 100000):
            icc = personal_icc(size)
            buffer = io.BytesIO()
            imagegen.marker_image((64, 48)).save(buffer, "JPEG", icc_profile=icc)
            data = buffer.getvalue()
            self.assertEqual(data.count(b"ICC_PROFILE\x00"), 2 if size else 1)
            clean = export.strip_jpeg(data)
            self.assertFalse(has_icc_secret(clean))
            with Image.open(io.BytesIO(clean)) as image:
                image.load()
                self.assertEqual(image.info["icc_profile"], export.scrub_icc(icc))
            self.assertEqual(self.pixels(clean), self.pixels(data))
        # In a WebP.
        buffer = io.BytesIO()
        try:
            imagegen.marker_image((64, 48)).save(buffer, "WEBP", lossless=True,
                                                 icc_profile=personal_icc())
        except (KeyError, OSError, ValueError):
            return
        data = buffer.getvalue()
        if has_icc_secret(data):
            clean = export.strip_webp(data)
            self.assertFalse(has_icc_secret(clean))
            self.assertEqual(self.pixels(clean), self.pixels(data))

    def test_gif(self):
        frames = [imagegen.marker_image((32, 24)), imagegen.blocks_image(3).resize((32, 24))]
        buffer = io.BytesIO()
        frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], loop=0,
                       duration=100, comment=b"GIF-SECRET")
        data = buffer.getvalue()
        self.assertIn(b"GIF-SECRET", data)
        clean = export.strip_gif(data)
        self.assertNotIn(b"GIF-SECRET", clean)
        self.assertIn(b"NETSCAPE2.0", clean)
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual(image.n_frames, 2)
            self.assertEqual(self.pixels(clean), self.pixels(data))

    def test_webp(self):
        buffer = io.BytesIO()
        exif = imagegen.make_exif(gps=GPS)
        try:
            imagegen.marker_image((64, 48)).save(buffer, "WEBP", lossless=True,
                                                 exif=exif.tobytes(), xmp=SECRETS[1])
        except (KeyError, OSError, ValueError):
            self.skipTest("this Pillow cannot write WebP")
        data = buffer.getvalue()
        clean = export.strip_webp(data)
        self.assertNotIn(b"EXIF", clean)
        self.assertNotIn(SECRETS[1], clean)
        self.assertEqual(self.pixels(clean), self.pixels(data))
        with Image.open(io.BytesIO(clean)) as image:
            self.assertFalse(image.getexif())

    def test_original_picture_formats(self):
        jpeg = imagegen.jpeg_bytes(imagegen.marker_image())
        self.assertEqual(export.original_picture(jpeg)[1], "jpg")
        self.assertEqual(export.original_picture(text_png())[1], "png")
        # A PNG that needs turning upright becomes a JPEG.
        self.assertEqual(export.original_picture(text_png(), orientation=6)[1], "jpg")
        tiff = io.BytesIO()
        imagegen.marker_image().save(tiff, "TIFF")
        clean, ext = export.original_picture(tiff.getvalue())
        self.assertEqual(ext, "jpg")
        self.assertTrue(clean.startswith(b"\xff\xd8"))
        heic = heic_bytes()
        if heic:
            self.assertEqual(export.original_picture(heic)[1], "jpg")
        # A BMP becomes a PNG of the same pixels: nothing after them, and
        # no linked colour profile (a file path) survives.
        bmp = io.BytesIO()
        imagegen.marker_image((40, 30)).save(bmp, "BMP")
        data = bmp.getvalue() + b"BMP-TRAILER-SECRET"
        clean, ext = export.original_picture(data)
        self.assertEqual(ext, "png")
        self.assertNotIn(b"BMP-TRAILER-SECRET", clean)
        self.assertEqual(self.pixels(clean), self.pixels(bmp.getvalue()))


@unittest.skipIf(imagegen is None, "Pillow is not installed")
class FolderAndZipTest(support.TempBaseMixin, unittest.TestCase):
    """The default folder does not move; a zip someone else made is never replaced;
    a site of the first versions is replaced; a dual-fisheye original is stitched."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Vacation")
        shutil.copyfile(os.path.join(SAMPLES, "01-cat.jpg"), os.path.join(self.folder, "cat.jpg"))
        self.base.write_config([support.library("Vacation Zürich", self.folder,
                                                id="lib_zurich01")])
        db.init(log=quiet)
        self.library_id = "lib_zurich01"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.scan_and_describe()

    def scan_and_describe(self):
        import scan
        scan.run(common.load_config(), jobs.JobContext("test"), describe_images=False)
        self.conn.execute("UPDATE images SET described = 1, prompt_ver = 5, info = ?",
                          (json.dumps(info("Something", ["thing"])),))
        self.conn.commit()

    def export(self, out=None, options=None):
        return export.export_library(export.LogContext(quiet), self.conn, common.load_config(),
                                     self.library_id, out, options)

    def test_the_default_folder_stays_when_a_similar_name_is_added(self):
        plain = os.path.join(common.exports_dir(), "vacation-zurich")
        first = self.export()
        self.assertEqual(first["output"], plain)
        marker = export.read_marker(plain)
        # A library whose name gives the same folder name is added later.
        other = self.base.library_folder("Other")
        config = common.load_config()
        config["libraries"].append(support.library("Vacation Zurich", other, id="lib_other001"))
        common.save_config(config)
        config = common.load_config()
        self.assertEqual(export.default_output_dir(config, config["libraries"][0]), plain)
        self.assertEqual(export.default_output_dir(config, config["libraries"][1]),
                         plain + "-lib-other001")
        again = self.export()
        self.assertEqual(again["output"], plain)
        self.assertEqual(export.read_marker(plain), marker)      # same ids, same links

    def test_a_zip_this_program_did_not_make_is_never_replaced(self):
        out = os.path.join(self.base.path, "Site")
        zip_path = out + ".zip"
        with open(zip_path, "wb") as handle:
            handle.write(b"my own archive")
        with self.assertRaises(export.ExportError) as caught:
            self.export(out, {"zip": True})
        self.assertIn("was not made by an export", str(caught.exception))
        with open(zip_path, "rb") as handle:
            self.assertEqual(handle.read(), b"my own archive")
        self.assertFalse(os.path.exists(out))         # refused before anything was written
        os.remove(zip_path)
        self.export(out, {"zip": True})
        self.assertTrue(zipfile.is_zipfile(zip_path))
        # Its own zip, as it left it: made again (or kept) without a word.
        self.export(out, {"zip": True, "title": "Another title"})
        self.assertTrue(zipfile.is_zipfile(zip_path))
        # Changed since: left alone.
        with open(zip_path, "ab") as handle:
            handle.write(b"more")
        with self.assertRaises(export.ExportError):
            self.export(out, {"zip": True})

    def test_a_site_of_the_first_versions_is_replaced(self):
        out = os.path.join(self.base.path, "OldSite")
        files = {"index.html": b"<script>window.APP_CONFIG = {};</script>",
                 "site.js": b"old", "app.js": b"old", "style.css": b"old",
                 "images/12.jpg": b"old", "data/data-0000.json": b"[]",
                 "data/manifest.json": b'{"shards": ["data-0000.json"], "generated": 1}',
                 "data/locations.json": b"[]", "tiles/0/0/0.jpg": b"old",
                 "notes.txt": b"mine"}
        for rel, data in files.items():
            path = os.path.join(out, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        self.export(out)
        left = set(files_of(out))
        for rel in ("site.js", "app.js", "images/12.jpg", "data/data-0000.json",
                    "data/manifest.json", "data/locations.json", "tiles/0/0/0.jpg"):
            self.assertNotIn(rel, left)
        self.assertIn("notes.txt", left)
        self.assertIn(export.MARKER, left)
        # A folder that only looks a little like one is still refused.
        other = os.path.join(self.base.path, "NotASite")
        os.makedirs(os.path.join(other, "data"))
        for name in ("index.html", "site.js", "app.js"):
            with open(os.path.join(other, name), "wb") as handle:
                handle.write(b"x")
        with self.assertRaises(export.ExportError):
            self.export(other)

    def test_a_dual_fisheye_original_is_exported_stitched(self):
        exif = imagegen.make_exif(make="Arashi Vision", model="Insta360 OneR",
                                  date="2019:05:14 14:23:01", offset=None)
        exif[0x010E] = "IMG_20190514_142301_00_001.insp"
        import images
        frame = imagegen.dual_fisheye_image((1024, 512), offsets=images.INSTA360_PARAMS["offsets"])
        imagegen.write_bytes(self.folder, "IMG_20190514_142301_00_001.insp",
                             imagegen.jpeg_bytes(frame, exif))
        os.remove(os.path.join(self.folder, "cat.jpg"))
        self.scan_and_describe()
        row = self.conn.execute("SELECT projection FROM images WHERE missing_since IS NULL"
                                ).fetchone()
        self.assertEqual(row["projection"], "dual_fisheye")
        shipped = {}
        for mode in ("web", "original"):
            out = os.path.join(self.base.path, "site-" + mode)
            self.export(out, {"images": mode})
            (name,) = os.listdir(os.path.join(out, "images"))
            self.assertTrue(name.endswith(".jpg"), name)
            with open(os.path.join(out, "images", name), "rb") as handle:
                shipped[mode] = handle.read()
            (record,) = read_records(out)
            self.assertNotIn("ix", record)
        # The pages show the stitched panorama, never the two raw circles.
        self.assertEqual(shipped["original"], shipped["web"])

    def test_a_new_stitch_is_exported_again_without_previews(self):
        # A library without previews: the export stitches the original, and
        # stitches it again once the stitching changes (here the pole fill).
        exif = imagegen.make_exif(make="Arashi Vision", model="Insta360 OneR",
                                  date="2019:05:14 14:23:01", offset=None)
        exif[0x010E] = "IMG_20190514_142301_00_001.insp"
        import images
        frame = imagegen.dual_fisheye_image((4400, 2200),
                                            offsets=images.INSTA360_PARAMS["offsets"])
        imagegen.write_bytes(self.folder, "IMG_20190514_142301_00_001.insp",
                             imagegen.jpeg_bytes(frame, exif))
        os.remove(os.path.join(self.folder, "cat.jpg"))
        library = support.library("Vacation Z\u00fcrich", self.folder, id="lib_zurich01",
                                  previews=False)
        self.base.write_config([library])
        self.scan_and_describe()
        out = os.path.join(self.base.path, "site")

        def shipped():
            found = {}
            for folder in ("images", "panos"):
                (name,) = os.listdir(os.path.join(out, folder))
                with open(os.path.join(out, folder, name), "rb") as handle:
                    found[folder] = handle.read()
            return found
        self.export(out, {"images": "web"})
        before = shipped()
        self.assertEqual(self.export(out, {"images": "web"})["written"], 0)
        self.base.write_config([library], nadir_fill_deg=6)
        second = self.export(out, {"images": "web"})
        after = shipped()
        self.assertGreaterEqual(second["written"], 2)
        for folder in ("images", "panos"):
            self.assertNotEqual(after[folder], before[folder], folder)
        fresh = images.read_image(os.path.join(self.folder, "IMG_20190514_142301_00_001.insp"),
                                  None, want_pano=True)
        self.assertEqual(after["panos"], export.clean_jpeg(fresh["pano"]))


@unittest.skipIf(imagegen is None, "Pillow is not installed")
class ReencodedProfileTest(unittest.TestCase):
    """Pictures made anew (not stripped in place) keep their profile, not its texts."""

    def picture(self, kind):
        image = imagegen.blocks_image(12).resize((96, 64)).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, kind, icc_profile=personal_icc())
        data = buffer.getvalue()
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.info.get("icc_profile"), personal_icc(), kind)
        return data

    def assert_scrubbed(self, data, what):
        self.assertFalse(has_icc_secret(data), what)
        # (A PNG keeps its profile compressed: it is looked at decoded.)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.info.get("icc_profile"), export.scrub_icc(personal_icc()),
                             what)

    def test_no_profile_text_survives_a_new_encoding(self):
        import images
        png, tiff, jpeg = self.picture("PNG"), self.picture("TIFF"), self.picture("JPEG")
        for data, orientation in ((tiff, None), (png, 6)):
            clean, extension = export.original_picture(data, orientation)
            self.assertEqual(extension, "jpg")
            self.assert_scrubbed(clean, "original %s" % orientation)
        # A PNG preview (screenshots, drawings) becomes a JPEG.
        self.assert_scrubbed(export.web_jpeg(png), "web")
        self.assert_scrubbed(export.clean_jpeg(png), "clean")
        self.assert_scrubbed(images.png_from_bytes(png), "png")
        # What a vision model is sent.
        self.assert_scrubbed(images.prepare_for_llm(jpeg), "llm")
        if heic_bytes() is not None:        # (pillow-heif can write one)
            buffer = io.BytesIO()
            imagegen.marker_image((96, 64)).save(buffer, "HEIF", quality=90,
                                                 icc_profile=personal_icc())
            clean, _extension = export.original_picture(buffer.getvalue())
            self.assertFalse(has_icc_secret(clean), "heic")


if __name__ == "__main__":
    unittest.main()
