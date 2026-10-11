"""Tests for serve.py: the search server's routes and the static site packager.

The database is filled directly through db/cache (no scanning), inside a
temporary SETTINGS_BASE.
"""

import io
import json
import os
import re
import sys
import time
import unittest
import warnings
from unittest import mock

import support

import cache
import common
import db
import serve

try:
    import flask
except ImportError:
    flask = None

if support.pillow_available():
    import imagegen
    from PIL import Image
else:
    imagegen = Image = None

XSS_NAME = "</script><script>alert(1)</script>"
NOW = 1700000000.0


def quiet(*_args, **_kwargs):
    pass


def strict_json(text):
    """json.loads that fails on NaN/Infinity, like a browser's JSON.parse."""
    def reject(name):
        raise ValueError("non-finite number in JSON: " + name)
    return json.loads(text, parse_constant=reject)


def insert_image(conn, library_id, path, **fields):
    """Insert one images row (sensible defaults) and return its id."""
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 64, "height": 48,
           "dhash": "0884422110884422", "added": NOW, "updated": NOW}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute(
        "INSERT INTO images (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
        [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


def app_config_of(html):
    """The APP_CONFIG object injected into a page."""
    match = re.search(r"window\.APP_CONFIG = (.*?);</script>", html)
    return strict_json(match.group(1))


class ServerFixture(support.TempBaseMixin):
    """A config with three kinds of library and a few rows of each kind.

    Family (enabled) has a visible image plus a missing and a hidden one;
    Old is disabled; the third library's name tries to break out of the
    page's <script>; one row belongs to a library that is not configured.
    """

    def setUp(self):
        super().setUp()
        self.family_dir = self.base.library_folder("family")
        self.other_dir = self.base.library_folder("other")
        self.base.write_config([
            support.library("Family", self.family_dir, id="lib_family1"),
            support.library("Old", self.other_dir, id="lib_disabled", enabled=False),
            support.library(XSS_NAME, self.other_dir, id="lib_xss0001"),
        ])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.thumbs = cache.ThumbStore()
        self.addCleanup(self.thumbs.close)
        info = {"description": "A Sunny Beach", "text": "", "subjects": ["Dog"],
                "tags": ["Sand", "sea"], "colors": ["blue"], "_model_name": "test-model"}
        self.visible = insert_image(
            self.conn, "lib_family1", "holidays/beach.jpg", described=1, prompt_ver=4,
            info=json.dumps(info), exif='{"FNumber": NaN, "Make": "Acme", '
            '"GPS": {"GPSAltitude": Infinity}}', taken_at="2019-05-14T14:23:01",
            taken_source="exif", tz_offset="+02:00", camera="Acme One",
            lat=48.85, lon=2.35)
        self.missing = insert_image(self.conn, "lib_family1", "gone.jpg",
                                    missing_since=NOW, lat=10.0, lon=10.0)
        self.hidden = insert_image(self.conn, "lib_family1", "secret.jpg", hidden=1,
                                   lat=11.0, lon=11.0)
        self.disabled = insert_image(self.conn, "lib_disabled", "old.jpg", lat=12.0, lon=12.0)
        self.orphan = insert_image(self.conn, "lib_orphan01", "lost.jpg", lat=13.0, lon=13.0)
        self.xss = insert_image(self.conn, "lib_xss0001", "odd.jpg", lat=0.0, lon=0.0)
        for image_id in (self.visible, self.missing, self.hidden, self.disabled,
                         self.orphan, self.xss):
            self.thumbs.put(image_id, b"\xff\xd8thumb-%d" % image_id)
        if imagegen is not None:
            self.write_original("holidays/beach.jpg",
                                imagegen.jpeg_bytes(imagegen.marker_image((64, 48))))

    def write_original(self, rel_path, data, folder=None):
        path = os.path.join(folder or self.family_dir, *rel_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(ServerFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def get_json(self, url):
        response = self.client.get(url)
        return response, strict_json(response.get_data(as_text=True))

    def test_index_lists_only_present_visible_images(self):
        response, records = self.get_json("/api/index")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(r["id"] for r in records), sorted([self.visible, self.xss]))
        record = [r for r in records if r["id"] == self.visible][0]
        self.assertEqual(set(record), {"id", "name", "library", "text", "dhash"})
        self.assertEqual(record["library"], "Family")
        self.assertEqual(record["name"], "beach.jpg")
        self.assertEqual(record["dhash"], "0884422110884422")
        text = record["text"]
        self.assertEqual(text, text.lower())
        for word in ("beach.jpg", "holidays/beach.jpg", "a sunny beach", "dog", "sand", "sea", "blue"):
            self.assertIn(word, text)
        self.assertNotIn("test-model", text)
        xss = [r for r in records if r["id"] == self.xss][0]
        self.assertEqual(xss["library"], XSS_NAME)

    def test_index_follows_config_changes(self):
        config = common.load_config()
        common.get_library(config, "lib_family1")["enabled"] = False
        common.get_library(config, "lib_disabled")["enabled"] = True
        common.get_library(config, "lib_disabled")["name"] = "Renamed"
        common.save_config(config)
        _response, records = self.get_json("/api/index")
        by_id = {r["id"]: r["library"] for r in records}
        self.assertEqual(by_id, {self.disabled: "Renamed", self.xss: XSS_NAME})

    def test_thumb_has_etag_and_answers_304(self):
        response = self.client.get("/thumb/%d" % self.visible)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"\xff\xd8thumb-%d" % self.visible)
        etag = response.headers["ETag"]
        self.assertIn("max-age=", response.headers["Cache-Control"])
        again = self.client.get("/thumb/%d" % self.visible, headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.data, b"")
        for image_id in (self.missing, self.hidden, self.disabled, self.orphan, 999):
            self.assertEqual(self.client.get("/thumb/%d" % image_id).status_code, 404, image_id)
        self.assertEqual(self.client.get("/thumb/%d" % (2 ** 70)).status_code, 404)

    def test_preview_serves_the_stored_file(self):
        cache.PreviewStore().write("lib_family1", self.visible, b"\xff\xd8\xffstored-preview")
        self.conn.execute("UPDATE images SET has_preview = 1 WHERE id = ?", (self.visible,))
        self.conn.commit()
        response = self.client.get("/preview/%d" % self.visible)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"\xff\xd8\xffstored-preview")
        self.assertIn("private", response.headers["Cache-Control"])
        response.close()
        self.assertEqual(self.client.get("/preview/%d" % self.hidden).status_code, 404)
        # Previews of screenshots may be PNGs; the type comes from the bytes.
        cache.PreviewStore().write("lib_family1", self.visible, b"\x89PNG\r\n\x1a\nrest")
        response = self.client.get("/preview/%d" % self.visible)
        self.assertEqual(response.mimetype, "image/png")
        response.close()

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_preview_file_without_the_flag_is_not_served(self):
        # A leftover file (its replacement could not be written after the
        # picture changed) may show the old picture: the original is used.
        cache.PreviewStore().write("lib_family1", self.visible, b"\xff\xd8\xffold-picture")
        response = self.client.get("/preview/%d" % self.visible)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"old-picture", response.data)
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual(image.size, (64, 48))

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_originals_in_a_tilde_library_are_found(self):
        home = self.base.make_dir("home")
        imagegen.save_as(self.base.make_dir("home", "Pictures"), "pic.png", "PNG")
        config = common.load_config()
        config["libraries"].append(support.library("Home", "~/Pictures", id="lib_home0001"))
        common.save_config(config)
        image_id = insert_image(self.conn, "lib_home0001", "pic.png")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            response = self.client.get("/image/%d" % image_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            response.close()
            self.assertEqual(self.client.get("/preview/%d" % image_id).status_code, 200)

    def test_requests_for_other_host_names_are_refused(self):
        # DNS rebinding: a page on another site that points its name at this
        # machine must not be able to read the index.
        for host in ("attacker.example:5000", "attacker.example", "192.0.2.1:5000"):
            response = self.client.get("/api/index", headers={"Host": host})
            self.assertEqual(response.status_code, 400, host)
            self.assertEqual(self.client.get("/image/%d" % self.visible,
                                             headers={"Host": host}).status_code, 400)
        for host in ("localhost:5000", "127.0.0.1:5000", "[::1]:5000", "LOCALHOST"):
            self.assertEqual(self.client.get("/api/index", headers={"Host": host}).status_code,
                             200, host)
        self.assertEqual(serve.host_name("[::1]:80"), "::1")
        self.assertEqual(serve.host_name("Example.COM:8080"), "example.com")
        self.assertIn("192.0.2.7", serve.trusted_hosts("192.0.2.7"))
        self.assertNotIn("192.0.2.7", serve.trusted_hosts("127.0.0.1"))
        everywhere = serve.trusted_hosts("0.0.0.0")
        self.assertIn(serve.socket.gethostname().lower(), everywhere)

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_preview_falls_back_to_the_original(self):
        # No stored preview: one is made from the (rotated) original.
        image_id = insert_image(self.conn, "lib_family1", "big.jpg")
        imagegen.large_jpeg(self.family_dir, "big.jpg", size=(2600, 1300), orientation=6)
        response = self.client.get("/preview/%d" % image_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual(image.size, (1024, 2048))
        etag = response.headers["ETag"]
        with mock.patch("images.read_image", side_effect=AssertionError("re-read")):
            again = self.client.get("/preview/%d" % image_id, headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        # An original that cannot be reached gives 404 (the page shows the thumbnail).
        os.remove(os.path.join(self.family_dir, "big.jpg"))
        self.assertEqual(self.client.get("/preview/%d" % image_id).status_code, 404)

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_image_mimetype_is_sniffed_not_guessed(self):
        jpeg_id = insert_image(self.conn, "lib_family1", "really-a-jpeg.png")
        path = imagegen.jpeg_named_png(self.family_dir)
        response = self.client.get("/image/%d" % jpeg_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        with open(path, "rb") as handle:
            self.assertEqual(response.data, handle.read())
        response.close()
        png_id = insert_image(self.conn, "lib_family1", "real.png")
        imagegen.save_as(self.family_dir, "real.png", "PNG")
        response = self.client.get("/image/%d" % png_id)
        self.assertEqual(response.mimetype, "image/png")
        response.close()
        self.assertEqual(self.client.get("/image/%d" % self.missing).status_code, 404)
        self.assertEqual(self.client.get("/image/%d" % self.orphan).status_code, 404)

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_image_converts_formats_browsers_cannot_show(self):
        image_id = insert_image(self.conn, "lib_family1", "scan.tif")
        imagegen.save_as(self.family_dir, "scan.tif", "TIFF")
        response = self.client.get("/image/%d" % image_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertTrue(response.data.startswith(b"\xff\xd8"))
        etag = response.headers["ETag"]
        self.assertIn("max-age=", response.headers["Cache-Control"])
        # A revalidation is answered without converting again.
        with mock.patch("images.to_browser_jpeg", side_effect=AssertionError("converted")):
            again = self.client.get("/image/%d" % image_id, headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_image_converts_heic(self):
        path = imagegen.heic_file(self.family_dir, "photo.heic")
        if path is None:
            self.skipTest("pillow-heif cannot write HEIC here")
        image_id = insert_image(self.conn, "lib_family1", "photo.heic")
        response = self.client.get("/image/%d" % image_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertTrue(response.data.startswith(b"\xff\xd8"))

    def test_api_image_is_finite_json(self):
        response, record = self.get_json("/api/image/%d" % self.visible)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(record["library"], "Family")
        self.assertEqual(record["library_id"], "lib_family1")
        self.assertEqual(record["exif"]["Make"], "Acme")
        self.assertIsNone(record["exif"]["FNumber"])
        self.assertIsNone(record["exif"]["GPS"]["GPSAltitude"])
        self.assertEqual(record["taken_at"], "2019-05-14T14:23:01")
        self.assertEqual(record["tz_offset"], "+02:00")
        self.assertEqual(record["camera"], "Acme One")
        self.assertEqual((record["lat"], record["lon"]), (48.85, 2.35))
        self.assertEqual(record["info"]["tags"], ["Sand", "sea"])
        for image_id in (self.missing, self.hidden, self.disabled, 999):
            self.assertEqual(self.client.get("/api/image/%d" % image_id).status_code, 404)

    def test_app_config_cannot_break_out_of_the_script(self):
        for url, scripts in (("/", 3), ("/detail", 3), ("/map", 3)):
            html = self.client.get(url).get_data(as_text=True)
            self.assertNotIn(XSS_NAME, html)
            self.assertNotIn("<script>alert", html)
            self.assertEqual(html.count("<script"), scripts, url)
            self.assertEqual(html.count("</script>"), scripts, url)
            config = app_config_of(html)
            self.assertEqual(config["libraries"], ["Family", XSS_NAME])
            self.assertEqual(config["mode"], "server")

    def test_script_json_escapes_html_specials(self):
        text = serve.script_json({"a": "<b>&</b>", "b": "  "})
        for character in "<>&  ":
            self.assertNotIn(character, text)
        self.assertEqual(json.loads(text), {"a": "<b>&</b>", "b": "  "})

    def test_locations_come_from_lat_lon_columns(self):
        _response, points = self.get_json("/api/locations")
        # Missing, hidden, disabled, orphan and 0,0 positions are left out.
        self.assertEqual(points, [{"id": self.visible, "lat": 48.85, "lon": 2.35}])

    def test_tile_zoom_is_limited(self):
        started = time.time()
        self.assertEqual(self.client.get("/tile/1000000000/0/0.jpg").status_code, 404)
        self.assertLess(time.time() - started, 2)
        self.assertEqual(self.client.get("/tile/4/0/0.jpg").status_code, 404)
        self.assertEqual(self.client.get("/tile/1/4/0.jpg").status_code, 404)
        if imagegen is not None:
            response = self.client.get("/tile/0/1/0.jpg")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/jpeg")
        # With a generated pyramid, its own depth is allowed.
        tiles = common.ensure_dir(common.tiles_dir())
        with open(os.path.join(tiles, "manifest.json"), "w") as handle:
            json.dump({"max_zoom": 5}, handle)
        self.base.write_file("data/tiles/5/3/2.jpg", b"\xff\xd8tile")
        response = self.client.get("/tile/5/3/2.jpg")
        self.assertEqual(response.data, b"\xff\xd8tile")
        response.close()
        self.assertEqual(self.client.get("/tile/6/0/0.jpg").status_code, 404)

    def test_database_is_prepared_once(self):
        with mock.patch.object(db, "init", wraps=db.init) as init:
            app = serve.build_app()
            self.addCleanup(app.extensions["search_images"].close)
            client = app.test_client()
            for url in ("/", "/api/index", "/api/image/%d" % self.visible,
                        "/thumb/%d" % self.visible, "/api/locations"):
                self.assertEqual(client.get(url).status_code, 200, url)
        self.assertEqual(init.call_count, 1)

    def test_main_runs_without_the_reloader(self):
        with mock.patch.object(flask.Flask, "run") as run, \
                mock.patch.object(sys, "argv", ["serve.py", "serve", "--port", "5123"]), \
                mock.patch("builtins.print"):
            serve.main()
        self.assertEqual(run.call_count, 1)
        self.assertIs(run.call_args.kwargs["use_reloader"], False)
        self.assertEqual(run.call_args.kwargs["port"], 5123)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")


class PackageTest(ServerFixture, unittest.TestCase):

    def package(self, output, **kwargs):
        return serve.package(output, log=quiet, **kwargs)

    def test_refuses_a_non_empty_folder_without_marker(self):
        out = self.base.make_dir("staging")
        keep = self.base.write_file("staging/images/photo.jpg", b"precious")
        with self.assertRaises(serve.ExportError) as caught:
            self.package(out)
        self.assertIn("not empty", str(caught.exception))
        self.assertEqual(sorted(os.listdir(out)), ["images"])
        with open(keep, "rb") as handle:
            self.assertEqual(handle.read(), b"precious")

    def test_refuses_folders_inside_the_program_folder(self):
        # Only folders that git ignores may hold an export inside a checkout,
        # so private photos never show up as files to commit. (Checks only;
        # nothing is written.)
        config = common.load_config()
        for name in ("export", "docs", "photos_site", os.path.join("static", "site")):
            with self.assertRaises(serve.ExportError, msg=name):
                serve.check_export_dir(os.path.join(common.BASE_DIR, name), config)
        for name in ("site", "site_example", "exports", os.path.join("site", "deeper")):
            serve.check_export_dir(os.path.join(common.BASE_DIR, name), config)

    def test_tilde_library_paths_are_expanded(self):
        home = self.base.make_dir("home")
        os.makedirs(os.path.join(home, "Pictures"))
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            config = {"libraries": [support.library("Home", "~/Pictures", id="lib_home0001")]}
            self.assertEqual(serve.library_roots(config),
                             {"lib_home0001": os.path.join(home, "Pictures")})
            # The export guard sees the real folder too.
            with self.assertRaises(serve.ExportError):
                serve.check_export_dir(os.path.join(home, "Pictures", "site"), config)

    def test_refuses_folders_that_overlap_libraries_or_data(self):
        for out in (os.path.join(self.family_dir, "site"), self.base.path,
                    os.path.dirname(self.family_dir), common.tiles_dir(),
                    common.data_dir()):
            with self.assertRaises(serve.ExportError, msg=out):
                self.package(out)
        self.assertFalse(os.path.exists(os.path.join(self.family_dir, "site")))

    def test_force_accepts_an_older_site(self):
        out = self.base.make_dir("old-site")
        self.base.write_file("old-site/index.html", "old")
        result = self.package(out, force=True)
        self.assertEqual(result["count"], 2)
        self.assertTrue(os.path.isfile(os.path.join(out, serve.EXPORT_MARKER)))

    def test_packages_into_an_empty_folder_and_updates_it(self):
        out = self.base.join("site")
        result = self.package(out)
        self.assertEqual(result["count"], 2)
        self.assertTrue(os.path.isfile(os.path.join(out, serve.EXPORT_MARKER)))
        for name in ("index.html", "detail.html", "map.html", "site.js", "detail.js",
                     "data/manifest.json", "data/locations.json", "data/data-0000.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, *name.split("/"))), name)

        with open(os.path.join(out, "data", "manifest.json"), encoding="utf-8") as handle:
            manifest = strict_json(handle.read())
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(manifest["libraries"], ["Family", XSS_NAME])
        with open(os.path.join(out, "data", "data-0000.json"), encoding="utf-8") as handle:
            records = {r["id"]: r for r in strict_json(handle.read())}
        self.assertEqual(sorted(records), sorted([self.visible, self.xss]))
        record = records[self.visible]
        self.assertEqual(record["library"], "Family")
        self.assertIsNone(record["exif"]["FNumber"])
        self.assertEqual(record["taken_at"], "2019-05-14T14:23:01")
        self.assertIn("a sunny beach", record["text"])
        self.assertTrue(record["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(record["image"], "images/%d.jpg" % self.visible)
        with open(os.path.join(out, "data", "locations.json"), encoding="utf-8") as handle:
            self.assertEqual(strict_json(handle.read()),
                             [{"id": self.visible, "lat": 48.85, "lon": 2.35}])
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertNotIn(XSS_NAME, html)
        self.assertEqual(app_config_of(html)["libraries"], ["Family", XSS_NAME])
        if imagegen is not None:
            copied = os.path.join(out, "images", "%d.jpg" % self.visible)
            with open(copied, "rb") as handle, \
                    open(os.path.join(self.family_dir, "holidays", "beach.jpg"), "rb") as original:
                self.assertEqual(handle.read(), original.read())
            self.assertEqual(result["copies"]["copied"], 1)
        # The xss row has no original on disk: reported, not fatal.
        self.assertEqual(result["copies"]["missing"], 1)

        # A second run into the (now marked) folder only removes what is stale.
        self.base.write_file("site/images/99999.jpg", b"stale")
        self.base.write_file("site/notes.txt", b"mine")
        again = self.package(out)
        self.assertEqual(again["written"], 0)
        self.assertFalse(os.path.exists(os.path.join(out, "images", "99999.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(out, "notes.txt")))

    def test_default_output_is_in_the_data_folder(self):
        result = self.package(None)
        expected = os.path.join(common.data_dir(), "exports", "site")
        self.assertEqual(result["output"], expected)
        self.assertTrue(os.path.isfile(os.path.join(expected, "index.html")))

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_failed_copy_leaves_no_partial_file(self):
        image_id = insert_image(self.conn, "lib_family1", "broken.tif")
        self.write_original("broken.tif", b"II*\x00 not really a tiff")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")     # Pillow complains about the bad file
            result = self.package(self.base.join("site"))
        self.assertEqual(result["copies"]["failed"], 1)
        images_dir = self.base.join("site", "images")
        self.assertNotIn("%d.jpg" % image_id, os.listdir(images_dir))
        self.assertEqual([name for name in os.listdir(images_dir) if "tmp" in name], [])


if __name__ == "__main__":
    unittest.main()
