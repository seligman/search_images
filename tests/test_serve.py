"""Tests for serve.py: the search server's routes and the "package" command.

The database is filled directly through db/cache (no scanning), inside a
temporary SETTINGS_BASE.
"""

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import warnings
from unittest import mock

import support

import cache
import common
import db
import export
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
# A request from another computer: no admin rights, so no hidden pictures.
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}


quiet = support.quiet


strict_json = support.strict_json


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


app_config_of = support.page_config


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

    def index_records(self, url="/api/index", **kwargs):
        response = self.client.get(url, **kwargs)
        self.assertEqual(response.status_code, 200)
        payload = strict_json(response.get_data(as_text=True))
        return payload, {record["i"]: record for record in payload["records"]}

    def test_index_lists_only_present_visible_images(self):
        payload, records = self.index_records(environ_base=REMOTE)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["libraries"], [{"id": "lib_family1", "name": "Family"},
                                                {"id": "lib_xss0001", "name": XSS_NAME}])
        self.assertEqual(sorted(records), sorted([self.visible, self.xss]))
        record = records[self.visible]
        self.assertEqual(record["l"], "lib_family1")
        self.assertEqual(record["n"], "beach.jpg")
        self.assertEqual(record["f"], "holidays")
        self.assertEqual(record["d"], "A Sunny Beach")
        self.assertEqual(record["s"], ["Dog"])
        self.assertEqual(record["g"], ["sand", "sea"])
        self.assertEqual(record["c"], ["blue"])
        self.assertEqual(record["v4"], 1)
        self.assertNotIn("test-model", json.dumps(payload))
        self.assertEqual(records[self.xss]["l"], "lib_xss0001")
        # The admin (this computer) also gets hidden pictures, marked, to unhide them.
        _payload, records = self.index_records()
        self.assertEqual(sorted(records), sorted([self.visible, self.hidden, self.xss]))
        self.assertEqual(records[self.hidden]["h"], 1)
        self.assertNotIn("h", records[self.visible])

    def test_index_records_cover_version_5_descriptions(self):
        info = {"title": "Toddler blowing out birthday candles", "description": "A party.",
                "photo_type": "photo", "document_type": "none", "setting": "indoor",
                "scene": "dining room", "time_of_day": "unknown", "event": "birthday",
                "people": "3-5", "animals": ["beagle"], "food": ["chocolate cake"],
                "vehicles": [], "people_descriptors": ["toddler in a party hat"],
                "tags": ["cake"], "text": "HAPPY BIRTHDAY", "colors": ["yellow"],
                "_model_name": "test-model"}
        image_id = insert_image(self.conn, "lib_family1", "2021/Party/cake.jpg", described=1,
                                prompt_ver=5, info=json.dumps(info),
                                taken_at="2021-03-07T18:00:00", dhash="")
        _payload, records = self.index_records()
        self.assertEqual(records[image_id], {
            "i": image_id, "l": "lib_family1", "n": "cake.jpg", "f": "2021/Party",
            "t": "Toddler blowing out birthday candles", "d": "A party.", "o": "HAPPY BIRTHDAY",
            "sc": "dining room", "a": ["beagle"], "fo": ["chocolate cake"],
            "pd": ["toddler in a party hat"], "g": ["cake"], "c": ["yellow"], "st": "indoor",
            "ev": "birthday", "ty": "photo", "pp": "3-5", "dk": "2021-03-07",
            "ts": "2021-03-07T18:00:00", "w": 64, "hgt": 48, "ds": 1})

    def test_api_image_shows_public_provenance_only(self):
        provenance = {"model": "model.gguf", "endpoint": "local", "seconds": 2.5,
                      "tokens_in": 1000, "tokens_out": 300, "prompt_ver": 5,
                      "prompt_hash": "0123456789ab", "profile": "core", "cost": 0.001,
                      "at": "2026-01-02T03:04:05Z", "requests": 1}
        image_id = insert_image(self.conn, "lib_family1", "described.jpg", described=1,
                                prompt_ver=5, info=json.dumps({"description": "x"}),
                                provenance=json.dumps(provenance))
        _response, record = self.get_json("/api/image/%d" % image_id)
        self.assertEqual(record["provenance"], {"model": "model.gguf", "endpoint": "local",
                                                "prompt_ver": 5, "profile": "core",
                                                "at": "2026-01-02T03:04:05Z"})
        _response, record = self.get_json("/api/image/%d" % self.visible)
        self.assertIsNone(record["provenance"])

    def test_index_follows_config_changes(self):
        config = common.load_config()
        common.get_library(config, "lib_family1")["enabled"] = False
        common.get_library(config, "lib_disabled")["enabled"] = True
        common.get_library(config, "lib_disabled")["name"] = "Renamed"
        common.save_config(config)
        payload, records = self.index_records()
        self.assertEqual({image_id: record["l"] for image_id, record in records.items()},
                         {self.disabled: "lib_disabled", self.xss: "lib_xss0001"})
        self.assertEqual(payload["libraries"], [{"id": "lib_disabled", "name": "Renamed"},
                                                {"id": "lib_xss0001", "name": XSS_NAME}])

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
        for image_id in (self.missing, self.disabled, self.orphan, 999):
            self.assertEqual(self.client.get("/thumb/%d" % image_id).status_code, 404, image_id)
        # Hidden pictures are only shown to the admin (on this computer).
        self.assertEqual(self.client.get("/thumb/%d" % self.hidden,
                                         environ_base=REMOTE).status_code, 404)
        self.assertEqual(self.client.get("/thumb/%d" % self.hidden).status_code, 200)
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
        for image_id in (self.missing, self.disabled, 999):
            self.assertEqual(self.client.get("/api/image/%d" % image_id).status_code, 404)
        self.assertFalse(record["hidden"])
        self.assertEqual(self.client.get("/api/image/%d" % self.hidden,
                                         environ_base=REMOTE).status_code, 404)
        _response, hidden = self.get_json("/api/image/%d" % self.hidden)
        self.assertTrue(hidden["hidden"])

    def test_app_config_cannot_break_out_of_the_script(self):
        # The page settings block plus the page's scripts (site, shell, the
        # search engine, the maps' ground, the routes, the viewer, the page's own).
        for url, scripts in (("/", 8), ("/detail", 8), ("/map", 8)):
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

    def test_tiles_version_changes_with_the_tile_pyramid(self):
        # Browsers keep a tile a day under its address (TILE_CACHE_SECONDS), and the
        # same address answers with the bundled map's cut before make_tiles.py and
        # with the pyramid's tile after: the pages put the tiles' version in the
        # address (SITE.tileUrl), so that no tile kept from before shows among the
        # new ones (a darker block with a dotted grid).
        def version():
            config = app_config_of(self.client.get("/map").get_data(as_text=True))
            for url in ("/", "/detail", "/albums"):
                other = app_config_of(self.client.get(url).get_data(as_text=True))
                self.assertEqual(other["tilesVersion"], config["tilesVersion"], url)
            return config["tilesVersion"]

        bundled = version()
        self.assertEqual(bundled, str(serve.FALLBACK_TILE_VERSION))
        self.assertEqual(version(), bundled)
        # make_tiles.py has made the pyramid (it writes the manifest last).
        self.base.write_file("data/tiles/0/0/0.jpg", b"\xff\xd8tile")
        manifest = self.base.write_file("data/tiles/manifest.json",
                                        json.dumps({"max_zoom": 0, "tile_size": 256}))
        os.utime(manifest, ns=(int(NOW * 1e9), int(NOW * 1e9)))
        made = version()
        self.assertNotEqual(made, bundled)
        self.assertEqual(version(), made)
        # Made again (with --force, or from another picture of the world).
        os.utime(manifest, ns=(int(NOW * 1e9), int((NOW + 60) * 1e9)))
        again = version()
        self.assertNotIn(again, (bundled, made))
        # A new cut of the bundled map is a new version too.
        with mock.patch.object(serve, "FALLBACK_TILE_VERSION", serve.FALLBACK_TILE_VERSION + 1):
            self.assertNotEqual(version(), again)
        # The tile route answers addresses with the version as those without.
        response = self.client.get("/tile/0/0/0.jpg?v=" + again)
        self.assertEqual(response.data, b"\xff\xd8tile")
        response.close()

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
    """"serve.py package" exports one library with export.py (tested in test_export)."""

    def setUp(self):
        super().setUp()
        if imagegen is not None:
            # A thumbnail an export can use (the fixture's are not JPEGs).
            self.thumbs.put(self.visible, imagegen.jpeg_bytes(imagegen.marker_image((64, 48))))

    def package(self, output, **kwargs):
        return serve.package(output, log=quiet, **kwargs)

    def test_needs_a_library_when_several_are_enabled(self):
        out = self.base.join("site")
        with self.assertRaises(export.ExportError) as caught:
            self.package(out)
        self.assertIn("--library", str(caught.exception))
        with self.assertRaises(export.ExportError):
            self.package(out, library="No such library")
        self.assertFalse(os.path.exists(out))

    def test_refuses_a_non_empty_folder_without_marker(self):
        out = self.base.make_dir("staging")
        keep = self.base.write_file("staging/images/photo.jpg", b"precious")
        with self.assertRaises(export.ExportError) as caught:
            self.package(out, library="Family")
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
            with self.assertRaises(export.ExportError, msg=name):
                export.check_output_dir(os.path.join(common.BASE_DIR, name), config)
        for name in ("site", "site_example", "exports", os.path.join("site", "deeper")):
            export.check_output_dir(os.path.join(common.BASE_DIR, name), config)

    def test_tilde_library_paths_are_expanded(self):
        home = self.base.make_dir("home")
        os.makedirs(os.path.join(home, "Pictures"))
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            config = {"libraries": [support.library("Home", "~/Pictures", id="lib_home0001")]}
            self.assertEqual(serve.library_roots(config),
                             {"lib_home0001": os.path.join(home, "Pictures")})
            # The export guard sees the real folder too.
            with self.assertRaises(export.ExportError):
                export.check_output_dir(os.path.join(home, "Pictures", "site"), config)

    def test_refuses_folders_that_overlap_libraries_or_data(self):
        for out in (os.path.join(self.family_dir, "site"), self.base.path,
                    os.path.dirname(self.family_dir), common.tiles_dir(),
                    common.data_dir(), common.exports_dir()):
            with self.assertRaises(export.ExportError, msg=out):
                self.package(out, library="Family")
        self.assertFalse(os.path.exists(os.path.join(self.family_dir, "site")))

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_exports_the_chosen_library_only(self):
        out = self.base.join("site")
        # The visible picture has an older (version 4) description, which
        # did not look for private content: left out unless asked for.
        result = self.package(out, library="lib_family1")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["excluded"], {"hidden": 1, "unchecked": 1})
        result = self.package(out, library="lib_family1",
                              options={"exclude_undescribed": False})
        # The visible picture; the missing and the hidden one stay out.
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["excluded"], {"hidden": 1})
        self.assertTrue(os.path.isfile(os.path.join(out, export.MARKER)))
        for name in ("index.html", "detail.html", "assets/site.js", "assets/shell.js",
                     "data/manifest.js", "data/index-000.js"):
            self.assertTrue(os.path.isfile(os.path.join(out, *name.split("/"))), name)
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertNotIn(XSS_NAME, html)
        config = app_config_of(html)
        self.assertEqual(config["libraries"], ["Family"])
        self.assertEqual(config["mode"], "static")

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_default_output_is_in_the_data_folder(self):
        result = self.package(None, library="Family")
        expected = os.path.join(common.data_dir(), "exports", "family")
        self.assertEqual(result["output"], expected)
        self.assertTrue(os.path.isfile(os.path.join(expected, "index.html")))

    def test_a_picture_that_cannot_be_read_is_skipped(self):
        insert_image(self.conn, "lib_family1", "broken.tif", described=1, prompt_ver=5,
                     info=json.dumps({"title": "Broken", "photo_type": "photo"}))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")     # Pillow complains about the bad file
            self.write_original("broken.tif", b"II*\x00 not really a tiff")
            result = self.package(self.base.join("site"), library="Family",
                                  options={"exclude_undescribed": False})
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["count"], 1)
        names = os.listdir(self.base.join("site", "data"))
        self.assertEqual([name for name in names if "tmp" in name], [])


class PanoramaFixture(ServerFixture):
    """Adds a stitched and an unstitched 360 photo, and a row not checked yet."""

    def setUp(self):
        super().setUp()
        self.dual = insert_image(self.conn, "lib_family1", "360/IMG_20190514_142301_00_001.insp",
                                 width=6080, height=3040, projection="dual_fisheye")
        self.stitched = insert_image(self.conn, "lib_family1", "360/pano.jpg",
                                     width=4000, height=2000, projection="equirectangular")
        self.unchecked = insert_image(self.conn, "lib_family1", "old/wide.jpg",
                                      width=4000, height=2000, projection="?")
        for image_id in (self.dual, self.stitched, self.unchecked):
            self.thumbs.put(image_id, b"\xff\xd8thumb-%d" % image_id)


@unittest.skipIf(flask is None, "Flask is not installed")
class PanoramaServerTest(PanoramaFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def get_json(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return strict_json(response.get_data(as_text=True))

    def test_index_marks_360_photos(self):
        records = {r["i"]: r for r in self.get_json("/api/index")["records"]}
        self.assertEqual(records[self.dual]["p"], "360")
        self.assertEqual(records[self.stitched]["p"], "360")
        for image_id in (self.unchecked, self.visible):
            self.assertNotIn("p", records[image_id])

    def test_detail_record_has_the_projection(self):
        self.assertEqual(self.get_json("/api/image/%d" % self.dual)["projection"],
                         "dual_fisheye")
        self.assertEqual(self.get_json("/api/image/%d" % self.stitched)["projection"],
                         "equirectangular")
        # "?" (not checked yet) is not shown as anything.
        self.assertEqual(self.get_json("/api/image/%d" % self.unchecked)["projection"], "")
        self.assertEqual(self.get_json("/api/image/%d" % self.visible)["projection"], "")


@unittest.skipIf(flask is None or Image is None, "Flask or Pillow is not installed")
class PanoRouteTest(PanoramaFixture, unittest.TestCase):
    """/pano/<id>: the large panorama of a 360 photo for the 360 view."""

    def setUp(self):
        super().setUp()
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        self.store = cache.PreviewStore()
        # (Larger than a preview: smaller 360 photos have no pano.)
        self.pano_bytes = imagegen.jpeg_with_xmp(imagegen.equirect_image((2400, 1200)),
                                                 imagegen.gpano_xmp())
        self.write_original("360/pano.jpg", self.pano_bytes)
        frame = imagegen.dual_fisheye_image((2304, 1152))
        exif = imagegen.make_exif(make="Arashi Vision", model="Insta360 OneR", offset=None)
        self.write_original("360/IMG_20190514_142301_00_001.insp",
                            imagegen.jpeg_bytes(frame, exif))
        self.hidden_pano = insert_image(self.conn, "lib_family1", "360/secret.jpg", hidden=1,
                                        width=2400, height=1200, projection="equirectangular")
        self.write_original("360/secret.jpg", self.pano_bytes)

    def set_row(self, image_id, **values):
        columns = sorted(values)
        self.conn.execute("UPDATE images SET %s WHERE id = ?"
                          % ", ".join("%s = ?" % column for column in columns),
                          [values[column] for column in columns] + [image_id])
        self.conn.commit()

    def size_of(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        with Image.open(io.BytesIO(response.data)) as image:
            return image.size

    def test_only_360_photos_have_one(self):
        for image_id in (self.visible, self.unchecked, self.missing, self.disabled,
                         self.orphan, 999999, 2 ** 63):
            self.assertEqual(self.client.get("/pano/%d" % image_id).status_code, 404, image_id)
        # Hidden pictures: for the admin only, as everywhere.
        self.assertEqual(self.client.get("/pano/%d" % self.hidden_pano,
                                         environ_base=REMOTE).status_code, 404)
        self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.hidden_pano)),
                         (2400, 1200))
        # A 360 photo no larger than its preview has none (the preview is
        # all of it), without reading the original.
        self.set_row(self.stitched, width=2048, height=1024)
        with mock.patch.object(serve._images(), "read_image") as read:
            self.assertEqual(self.client.get("/pano/%d" % self.stitched).status_code, 404)
        read.assert_not_called()

    def test_the_stored_pano_is_served(self):
        self.set_row(self.stitched, has_preview=1)
        self.store.write_pano("lib_family1", self.stitched, b"\xff\xd8\xff\xe0stored pano")
        response = self.client.get("/pano/%d" % self.stitched)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"\xff\xd8\xff\xe0stored pano")
        self.assertIn("private", response.headers["Cache-Control"])

    def test_a_missing_pano_is_made_once_and_kept(self):
        digest = hashlib.sha1(self.pano_bytes).hexdigest()
        self.set_row(self.stitched, has_preview=1, content_hash=digest)
        calls = []
        real = serve._images().read_image

        def counting(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)
        with mock.patch.object(serve._images(), "read_image", counting):
            self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.stitched)),
                             (2400, 1200))
            self.assertTrue(self.store.pano_exists("lib_family1", self.stitched))
            self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.stitched)),
                             (2400, 1200))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["want_pano"])
        self.assertFalse(calls[0]["want_preview"])
        # An unstitched original is stitched.
        self.set_row(self.dual, has_preview=1)
        self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.dual)), (2304, 1152))

    def test_a_pano_is_only_kept_for_the_content_the_index_knows(self):
        # The original changed since the last scan (another content hash),
        # or previews are not kept (no flag): served, but not stored.
        self.set_row(self.stitched, has_preview=1, content_hash="0" * 40)
        self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.stitched)), (2400, 1200))
        self.assertFalse(self.store.pano_exists("lib_family1", self.stitched))
        self.set_row(self.stitched, has_preview=0,
                     content_hash=hashlib.sha1(self.pano_bytes).hexdigest())
        # A file without the flag is never trusted.
        self.store.write_pano("lib_family1", self.stitched, b"\xff\xd8\xff\xe0old pano")
        self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.stitched)), (2400, 1200))
        self.assertEqual(self.store.read_pano("lib_family1", self.stitched),
                         b"\xff\xd8\xff\xe0old pano")

    def test_nothing_new_is_stored_while_previews_are_off(self):
        # Previews made while they were on keep their flag; with previews
        # turned off, panos are made each time and never stored.
        self.base.write_config([
            support.library("Family", self.family_dir, id="lib_family1", previews=False),
            support.library("Old", self.other_dir, id="lib_disabled", enabled=False),
        ])
        self.set_row(self.stitched, has_preview=1,
                     content_hash=hashlib.sha1(self.pano_bytes).hexdigest())
        for _ in range(2):
            self.assertEqual(self.size_of(self.client.get("/pano/%d" % self.stitched)),
                             (2400, 1200))
            self.assertFalse(self.store.pano_exists("lib_family1", self.stitched))
        # One stored while they were on is still used.
        self.store.write_pano("lib_family1", self.stitched, b"\xff\xd8\xff\xe0stored pano")
        self.assertEqual(self.client.get("/pano/%d" % self.stitched).data,
                         b"\xff\xd8\xff\xe0stored pano")

    def test_pictures_made_on_the_fly_change_with_the_stitching(self):
        # No stored preview or pano (previews off): made from the original,
        # with an ETag that changes when the stitching does (here the pole fill).
        tags = {}
        for kind in ("preview", "pano"):
            response = self.client.get("/%s/%d" % (kind, self.dual))
            self.assertEqual(response.status_code, 200, kind)
            tags[kind] = response.headers["ETag"].strip('"')
            again = self.client.get("/%s/%d" % (kind, self.dual),
                                    headers={"If-None-Match": tags[kind]})
            self.assertEqual(again.status_code, 304, kind)
        plain = self.client.get("/pano/%d" % self.stitched).headers["ETag"].strip('"')
        config = common.load_config()
        config["nadir_fill_deg"] = 6
        common.save_config(config)
        for kind in ("preview", "pano"):
            response = self.client.get("/%s/%d" % (kind, self.dual),
                                       headers={"If-None-Match": tags[kind]})
            self.assertEqual(response.status_code, 200, kind)
            self.assertNotEqual(response.headers["ETag"].strip('"'), tags[kind], kind)
        # Other pictures do not depend on it.
        self.assertEqual(self.client.get("/pano/%d" % self.stitched).headers["ETag"].strip('"'),
                         plain)

    def test_no_original_no_pano(self):
        os.remove(os.path.join(self.family_dir, "360", "pano.jpg"))
        self.assertEqual(self.client.get("/pano/%d" % self.stitched).status_code, 404)
        self.write_original("360/pano.jpg", b"not an image at all")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(self.client.get("/pano/%d" % self.stitched).status_code, 404)
        self.assertIn("Could not make the 360-degree panorama", printed.call_args[0][0])


class PanoramaPackageTest(PanoramaFixture, unittest.TestCase):

    def test_insp_originals_become_jpg_files(self):
        if imagegen is None:
            self.skipTest("Pillow is not installed")
        data = imagegen.jpeg_bytes(imagegen.marker_image((64, 48)))
        self.assertEqual(export.original_picture(data)[1], "jpg")

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_records_carry_the_projection(self):
        thumb = imagegen.jpeg_bytes(imagegen.marker_image((64, 32)))
        for image_id in (self.dual, self.stitched, self.unchecked):
            self.thumbs.put(image_id, thumb)
        out = self.base.join("site")
        serve.package(out, library="Family", log=quiet,
                      options={"images": "none", "exclude_undescribed": False})
        with open(os.path.join(out, "data", "index-000.js"), encoding="utf-8") as handle:
            text = handle.read()
        records = strict_json(text[text.index("["):text.rindex("]") + 1])
        marked = sorted(record.get("p", "") for record in records)
        # The dual-fisheye and the stitched one are 360 photos; the
        # unchecked row is not. (The beach's thumbnail is no JPEG: skipped.)
        self.assertEqual(marked, ["", "360", "360"])


class CommandLineTest(support.TempBaseMixin, unittest.TestCase):
    """serve.py --host / --port work without the "serve" command; a busy port is said so."""

    def test_options_without_a_command_mean_serve(self):
        self.assertEqual(serve._command_line(["--port", "8080"]), ["serve", "--port", "8080"])
        self.assertEqual(serve._command_line(["--host", "0.0.0.0"]),
                         ["serve", "--host", "0.0.0.0"])
        for argv in ([], ["serve", "--port", "1"], ["package", "out"], ["-h"], ["--help"]):
            self.assertEqual(serve._command_line(argv), argv)
        # The serve command's own help, reached with an option first.
        result = subprocess.run([sys.executable, "serve.py", "--port", "8080", "--help"],
                                cwd=support.REPO, env=support.child_env(),
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"--host", result.stdout)

    def test_a_port_another_server_listens_on_is_refused(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        # As Werkzeug binds its socket (which lets a second one share it on Windows).
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        problem = serve.port_problem("127.0.0.1", port)
        if os.name == "nt":
            self.assertIn("Port %d is already in use" % port, problem)
        else:
            self.assertIsNone(problem)      # the server's own start says so there
        listener.close()
        self.assertIsNone(serve.port_problem("127.0.0.1", port))

    def test_requests_read_originals_through_the_shared_helper_lock(self):
        helper = mock.Mock(spec=["load_image", "stat", "__file__"],
                           __file__=os.path.join(self.base.path, "helper.py"))
        state = serve.ServerState(helper)
        self.addCleanup(state.close)
        self.assertIs(state.module, helper)
        self.assertIsNot(state.helper, helper)
        state.helper.load_image("a.jpg")
        helper.load_image.assert_called_once_with("a.jpg")
        self.assertIsNone(serve.ServerState(None).helper)


if __name__ == "__main__":
    unittest.main()
