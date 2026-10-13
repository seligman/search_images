"""Tests for wide pictures (flat panoramas) and their larger picture.

A wide photo (images.WIDE_ASPECT) opens in the viewer's pan view on an
upright phone, zoomed to fill the screen's height; its 2048 px preview is
then too small, so it gets a larger picture (images.wide_pano_size), kept
as its "pano" next to its preview (cache.PreviewStore), served by /pano/<id>
(made from the original when missing) and published by an export with web
pictures as panos/<id>.jpg. The viewer's part runs under Node.js
(tests/js/wide_pano_test.js; skipped when node is not installed). All
pictures here are made up.
"""

import hashlib
import io
import json
import os
import shutil
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import support

import cache
import common
import db
import jobs

try:
    from PIL import Image
    import imagegen
    import images
except ImportError:                     # (Pillow is not installed)
    Image = imagegen = images = None

try:
    import flask
    import serve
    import test_serve
except ImportError:
    flask = serve = test_serve = None

NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "wide_pano_test.js")

# (width, height): the size of its larger picture, or None (none), as
# images.wide_pano_size and static/site.js agree.
SIZES = [
    ((6400, 2000), (5120, 1600)),
    ((16150, 3782), (6832, 1600)),
    ((60000, 2000), (16384, 546)),
    ((4800, 1500), (4800, 1500)),
    ((2200, 1000), None),           # its preview is 931 px tall: not enough taller
    ((2048, 900), None),            # the preview is all of it
    ((2400, 300), None),
    ((4000, 2000), None),           # 2:1: not wide
    ((2199, 1000), None),
    ((3000, 4000), None),
    ((0, 0), None),
]


@unittest.skipIf(images is None, "Pillow is not installed")
class WideSizeTest(unittest.TestCase):

    def test_wide_pictures(self):
        self.assertTrue(images.is_wide(2200, 1000))
        self.assertTrue(images.is_wide(16150, 3782))
        self.assertFalse(images.is_wide(2199, 1000))
        self.assertFalse(images.is_wide(4000, 2000))
        self.assertFalse(images.is_wide(0, 0))
        self.assertFalse(images.is_wide(None, 5))
        self.assertFalse(images.is_wide("x", 5))

    def test_larger_picture_sizes(self):
        for size, expected in SIZES:
            self.assertEqual(images.wide_pano_size(*size), expected, size)

    def test_which_pictures_get_a_pano(self):
        self.assertTrue(images.gets_pano(6400, 2000))
        self.assertTrue(images.gets_pano(6400, 2000, "", "image"))
        self.assertFalse(images.gets_pano(6400, 2000, "", "video"))
        self.assertFalse(images.gets_pano(6400, 2000, "?"))
        self.assertFalse(images.gets_pano(4000, 3000))
        # 360 photos: as before (larger than their preview).
        self.assertTrue(images.gets_pano(4096, 2048, "equirectangular"))
        self.assertTrue(images.gets_pano(6080, 3040, "dual_fisheye"))
        self.assertFalse(images.gets_pano(2048, 1024, "equirectangular"))
        self.assertFalse(images.gets_pano(5760, 2880, "dual_fisheye", "video"))

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_the_viewer_asks_for_the_same_pictures(self):
        # site.js decides on its own (from the record's size) whether a
        # wide photo has a larger picture: it must agree with images.py.
        script = (
            "global.window = {APP_CONFIG: {mode: 'server', api: '/api', page: 'search'},"
            " location: {search: '', protocol: 'http:'}};"
            "var SITE = require(process.argv[1]);"
            "var sizes = JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify(sizes.map(function (s) {"
            " return [SITE.panoUrl({i: 1, w: s[0], hgt: s[1]}) !== null,"
            " SITE.widePanoSize(s[0], s[1])]; })));")
        sizes = [list(size) for size, _ in SIZES] + [
            [width, height] for width in (2200, 2300, 2560, 4096, 5000, 9000, 20000, 40000)
            for height in (240, 512, 800, 931, 1000, 1200, 1700, 1900, 4000)]
        result = subprocess.run([NODE, "-e", script, os.path.join(support.REPO, "static",
                                                                  "site.js"),
                                 json.dumps(sizes)],
                                capture_output=True, timeout=60, cwd=support.REPO)
        self.assertEqual(result.returncode, 0, result.stderr)
        answers = json.loads(result.stdout.decode("ascii"))
        for size, (answer, made) in zip(sizes, answers):
            self.assertEqual(answer, images.gets_pano(*size), size)
            # (And the size the viewer expects it at, for its zoom.)
            expected = images.wide_pano_size(*size)
            self.assertEqual(made, list(expected) if expected else None, size)


@unittest.skipIf(images is None, "Pillow is not installed")
class ReadWideTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wide-")
        self.addCleanup(support.remove_tree, self.dir)

    def open(self, data):
        image = Image.open(io.BytesIO(data))
        image.load()
        return image

    def test_a_wide_photo_gets_a_taller_picture(self):
        path = imagegen.large_jpeg(self.dir, "wide.jpg", size=(6400, 2000))
        result = images.read_image(path, want_pano=True)
        self.assertEqual((result["width"], result["height"]), (6400, 2000))
        self.assertEqual(result["projection"], "")
        pano = self.open(result["pano"])
        self.assertEqual(pano.size, (5120, 1600))
        self.assertEqual(pano.format, "JPEG")
        # Upright, and the same picture: the red corner top left.
        red = pano.getpixel((20, 20))
        self.assertGreater(red[0], 200)
        self.assertLess(red[2], 80)
        self.assertEqual(self.open(result["preview"]).size, (2048, 640))
        self.assertEqual(self.open(result["thumb"]).size, (320, 100))
        # Only when asked for.
        self.assertIsNone(images.read_image(path)["pano"])

    def test_a_turned_wide_photo(self):
        # Stored upright with EXIF orientation 6: it is wide as displayed.
        path = imagegen.large_jpeg(self.dir, "turned.jpg", size=(2000, 6400), orientation=6)
        result = images.read_image(path, want_pano=True)
        self.assertEqual((result["width"], result["height"]), (6400, 2000))
        self.assertEqual(self.open(result["pano"]).size, (5120, 1600))

    def test_other_photos_get_none(self):
        for name, size in (("photo.jpg", (4000, 3000)), ("two-to-one.jpg", (4000, 2000)),
                           ("small-wide.jpg", (2048, 900))):
            path = imagegen.large_jpeg(self.dir, name, size=size)
            self.assertIsNone(images.read_image(path, want_pano=True)["pano"], name)


@unittest.skipIf(images is None, "Pillow is not installed")
class WideScanTest(support.TempBaseMixin, unittest.TestCase):
    """The scan keeps a wide photo's larger picture next to its preview."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Family")
        imagegen.large_jpeg(self.folder, "wide.jpg", size=(6400, 2000))
        imagegen.large_jpeg(self.folder, "photo.jpg", size=(4000, 3000))
        self.base.write_config([support.library("Family", self.folder, id="lib_wide0001")])
        self.config = common.load_config()
        db.init(self.config, log=lambda message: None)
        self.reads = []
        real = images.read_image

        def counting(path, *args, **kwargs):
            self.reads.append(os.path.basename(path))
            return real(path, *args, **kwargs)
        patcher = mock.patch.object(images, "read_image", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self):
        import scan
        del self.reads[:]
        self.ctx = jobs.JobContext("test")
        return scan.run(self.config, self.ctx, describe_images=False)["libraries"][0]

    def ids(self):
        conn = db.connect()
        try:
            return {row["path"]: row["id"] for row in conn.execute("SELECT id, path FROM images")}
        finally:
            conn.close()

    def panos(self):
        store = cache.PreviewStore()
        found = {}
        for image_id in store.stored_ids("lib_wide0001")[1]:
            with Image.open(io.BytesIO(store.read_pano("lib_wide0001", image_id))) as image:
                found[image_id] = image.size
        return found

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=1000)["log_tail"])

    def test_the_scan_stores_it_and_the_backfill_makes_a_missing_one(self):
        self.scan()
        ids = self.ids()
        self.assertEqual(self.panos(), {ids["wide.jpg"]: (5120, 1600)})
        self.scan()
        self.assertEqual(self.reads, [])
        # Previews made before wide photos got one: only the wide photo is
        # read again, and only its larger picture is stored (its preview,
        # thumbnail and dHash, made by an older version, stay as they are: made
        # again from this read they would differ a little).
        store = cache.PreviewStore()
        store.delete_pano("lib_wide0001", ids["wide.jpg"])
        older = imagegen.jpeg_bytes(imagegen.textured_image((2048, 640)))
        store.write("lib_wide0001", ids["wide.jpg"], older)
        with cache.ThumbStore() as thumbs:
            thumbs.put(ids["wide.jpg"], b"older thumbnail")
        conn = db.connect()
        try:
            with conn:
                conn.execute("UPDATE images SET dhash = '0123456789abcdef', updated = 5 "
                             "WHERE id = ?", (ids["wide.jpg"],))
        finally:
            conn.close()
        summary = self.scan()
        self.assertEqual(self.reads, ["wide.jpg"])
        self.assertEqual(summary["backfill"]["panos"], 1)
        self.assertEqual(summary["backfill"]["wide"], 1)
        self.assertIn("1 wide photo(s) in", self.log_text())
        self.assertNotIn("360-degree", self.log_text())
        self.assertEqual(self.panos(), {ids["wide.jpg"]: (5120, 1600)})
        self.assertEqual(store.read("lib_wide0001", ids["wide.jpg"]), older)
        with cache.ThumbStore() as thumbs:
            self.assertEqual(thumbs.get(ids["wide.jpg"]), b"older thumbnail")
        conn = db.connect()
        try:
            row = conn.execute("SELECT dhash, updated, has_preview FROM images WHERE id = ?",
                               (ids["wide.jpg"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), ("0123456789abcdef", 5, 1))
        self.scan()
        self.assertEqual(self.reads, [])

    def test_no_larger_picture_without_previews(self):
        self.base.write_config([support.library("Family", self.folder, id="lib_wide0001",
                                                previews=False)])
        self.config = common.load_config()
        self.scan()
        self.assertEqual(self.panos(), {})
        self.assertEqual(self.scan()["backfill"]["needed"], 0)


@unittest.skipIf(flask is None or images is None, "Flask or Pillow is not installed")
class WidePanoRouteTest(test_serve.ServerFixture if test_serve else object, unittest.TestCase):
    """/pano/<id> serves a wide photo's larger picture too."""

    def setUp(self):
        super().setUp()
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        self.store = cache.PreviewStore()
        self.data = imagegen.jpeg_bytes(imagegen.marker_image((6400, 2000)), quality=80)
        self.write_original("trip/wide.jpg", self.data)
        self.wide = test_serve.insert_image(
            self.conn, "lib_family1", "trip/wide.jpg", width=6400, height=2000,
            has_preview=1, content_hash=hashlib.sha1(self.data).hexdigest())
        self.short = test_serve.insert_image(self.conn, "lib_family1", "trip/short.jpg",
                                             width=2400, height=300)

    def size_of(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        with Image.open(io.BytesIO(response.data)) as image:
            return image.size

    def test_made_once_and_kept(self):
        self.assertFalse(self.store.pano_exists("lib_family1", self.wide))
        self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.wide)), (5120, 1600))
        self.assertTrue(self.store.pano_exists("lib_family1", self.wide))
        with mock.patch.object(serve._images(), "read_image") as read:
            self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.wide)),
                             (5120, 1600))
        read.assert_not_called()
        # Its search record is no 360 photo's.
        records = {record["i"]: record for record in
                   json.loads(self.client.get("/api/index").get_data(as_text=True))["records"]}
        self.assertNotIn("p", records[self.wide])
        self.assertEqual((records[self.wide]["w"], records[self.wide]["hgt"]), (6400, 2000))

    def test_other_pictures_have_none(self):
        with mock.patch.object(serve._images(), "read_image") as read:
            for image_id in (self.short, self.visible):
                self.assertEqual(self.client.get("/pano/%d" % image_id).status_code, 404)
        read.assert_not_called()

    def test_a_wide_picture_that_cannot_be_read(self):
        self.write_original("trip/wide.jpg", b"not an image at all")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(self.client.get("/pano/%d" % self.wide).status_code, 404)
        self.assertIn("Could not make the larger picture", printed.call_args[0][0])


@unittest.skipIf(images is None, "Pillow is not installed")
class WideExportTest(support.TempBaseMixin, unittest.TestCase):
    """An export with web pictures publishes the larger picture as panos/<id>.jpg."""

    def setUp(self):
        super().setUp()
        folder = self.base.library_folder("Family")
        imagegen.large_jpeg(folder, "wide.jpg", size=(6400, 2000))
        imagegen.large_jpeg(folder, "photo.jpg", size=(4000, 3000))
        self.base.write_config([support.library("Family", folder, id="lib_wide0001")])
        self.config = common.load_config()
        db.init(self.config, log=lambda message: None)
        import scan
        scan.run(self.config, jobs.JobContext("test"), describe_images=False)
        info = {"title": "A wide view", "description": "Hills.", "tags": ["hills"],
                "photo_type": "photo", "private_info": False, "sensitive": "none"}
        conn = db.connect()
        try:
            conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = 5",
                         (json.dumps(info),))
            conn.commit()
        finally:
            conn.close()

    def export(self, name, images_option):
        import export
        out = os.path.join(self.base.path, "exports", name)
        conn = db.connect()
        try:
            export.export_library(jobs.JobContext("test"), conn, common.load_config(),
                                  "lib_wide0001", out, {"images": images_option})
        finally:
            conn.close()
        return out

    def records(self, out):
        import test_export
        return test_export.read_records(out)

    def test_web_pictures_come_with_the_larger_one(self):
        out = self.export("web", "web")
        records = self.records(out)
        wide = [record for record in records if record.get("w") == 6400]
        other = [record for record in records if record.get("w") == 4000]
        self.assertEqual(len(wide), 1)
        self.assertEqual(wide[0].get("pn"), 1)
        self.assertNotIn("p", wide[0])
        self.assertNotIn("pn", other[0])
        panos = sorted(os.listdir(os.path.join(out, "panos")))
        self.assertEqual(panos, ["%s.jpg" % wide[0]["i"]])
        with Image.open(os.path.join(out, "panos", panos[0])) as image:
            self.assertEqual(image.size, (5120, 1600))
            self.assertFalse(image.getexif())
        self.assertIs(page_settings(out)["capabilities"]["panos"], True)

    def test_originals_need_none(self):
        out = self.export("originals", "original")
        self.assertFalse(os.path.isdir(os.path.join(out, "panos")) and
                         os.listdir(os.path.join(out, "panos")))
        records = self.records(out)
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertNotIn("pn", record)


def page_settings(out):
    """The page settings of an export (its index.html's app-config)."""
    with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
        page = handle.read()
    found = re.search(r'<script type="application/json" id="app-config">(.*?)</script>',
                      page, re.S)
    return json.loads(found.group(1))


class MinimapStyleTest(unittest.TestCase):

    def test_the_minimap_lies_above_the_face_boxes(self):
        # (A face low in a panorama: its name, which takes taps, would cover the
        # minimap and take its taps.)
        with open(os.path.join(support.REPO, "static", "style.css"), encoding="utf-8") as handle:
            css = handle.read()

        def layer(selector):
            found = re.search(r"^%s \{([^}]*)\}" % re.escape(selector), css, re.M)
            self.assertIsNotNone(found, selector)
            return int(re.search(r"z-index: (\d+);", found.group(1)).group(1))
        self.assertGreater(layer(".stage-minimap"), layer(".face-layer"))


@unittest.skipIf(NODE is None, "Node.js is not installed")
class ViewerScriptTest(unittest.TestCase):

    def test_the_pan_view(self):
        # The pure helpers, SITE.panoUrl, and the stage on a made-up page:
        # the pan view, a resized stage, swipes at the end, the minimap.
        result = subprocess.run([NODE, NODE_TESTS], cwd=support.REPO, capture_output=True,
                                timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
