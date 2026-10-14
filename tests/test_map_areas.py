"""Tests for the search by drawing on the map on the server's side: map_areas.py reads
an area's text as the pages make it (static/search-core.js), a smart album keeps an area
(albums.py, the admin route), an export publishes one only with its location option (and a
trip's export never one near home), and the request log never shows one (serve.py).

The pages themselves are tested by tests/js/map_areas_test.js (the area's text, the
search, the search page and the map page) and tests/js/streets_test.js (the streets and
places of the street maps), run here and by test_street_maps.py. Every place here is made
up.
"""

import json
import os
import re
import shutil
import subprocess
import types
import unittest

import support

import albums
import common
import db
import export
import jobs
import map_areas
import serve
import trip_export
from test_admin_images import PictureFixture

try:
    import flask
except ImportError:
    flask = None

NODE = shutil.which("node")
# The texts the pages make of these areas (SearchCore.areas.encode), around 45 N 10 E:
# a rectangle, a road with 50 m either side, 100 m around a point, a triangle, and a
# rectangle across the date line.
BOX = "b.mZpo8Wdzy1Wz3Ww7"
ROAD = "l50.WYro8mb_y10Wz3"
SPOT = "l100.WYro8WaXz1"
SHAPE = "p.mZpo8mb_y1Wz30l-1mx5"
ACROSS = "b._fud3WtWgY1WnJWYd1"
# Places to try, and which of them each area holds (as the pages answer).
PLACES = [(45, 10), (45.02, 10), (45 + 40 / 111195, 10), (45 + 60 / 111195, 10),
          (-16.95, -179.95), (45, 10.015)]
HOLDS = {BOX: "101101", ROAD: "101000", SPOT: "101100", SHAPE: "101101", ACROSS: "000010"}


class AreaTextTest(unittest.TestCase):

    def test_the_pages_texts_read_as_the_pages_read_them(self):
        for text, holds in HOLDS.items():
            area = map_areas.parse(text)
            self.assertIsNotNone(area, text)
            self.assertEqual("".join("1" if area.contains(lat, lon) else "0"
                                     for lat, lon in PLACES), holds, text)
        road = map_areas.parse(ROAD)
        self.assertEqual((road.kind, road.metres, road.parts), ("l", 50, [[(45.0, 9.99),
                                                                           (45.0, 10.01)]]))
        self.assertEqual(map_areas.parse(BOX).parts, [[(44.99, 9.98), (45.01, 9.98),
                                                        (45.01, 10.02), (44.99, 10.02)]])

    def test_encoded_as_the_pages_encode(self):
        self.assertEqual(map_areas.encode("b", [[(44.99, 9.98), (45.01, 10.02)]]), BOX)
        self.assertEqual(map_areas.encode("l", [[(45.0, 9.99), (45.0, 10.01)]], 50), ROAD)
        self.assertEqual(map_areas.encode("l", [[(45.0, 10.0)]], 100), SPOT)
        self.assertEqual(map_areas.encode("p", [[(44.99, 9.99), (45.01, 9.99),
                                                 (45.0, 10.02)]]), SHAPE)
        self.assertEqual(map_areas.encode("b", [[(-17, 179.9), (-16.9, 180.1)]]), ACROSS)
        for kind, parts, metres in (("b", [[(45, 10), (44, 11)]], 0),
                                    ("p", [[(45, 10), (46, 11)]], 0),
                                    ("l", [[(95, 10)]], 50), ("l", [[(45, 10)]], 0),
                                    ("q", [[(45, 10)]], 50)):
            with self.assertRaises(ValueError):
                map_areas.encode(kind, parts, metres)

    def test_text_that_is_no_area(self):
        for bad in ("", None, 5, "b", "x.AA", "b.AAAA.AAAA", "p.AAAA", "l.AAAA", "l0.AAAA",
                    "l6000.AAAA", "b20.AAAA", "p.A A", "l50.AAAA!", "l50.A_", "l050.AAAA",
                    "l50." + "A" * map_areas.MAX_TEXT):
            self.assertIsNone(map_areas.parse(bad), bad)

    def test_deep_enough_inside_and_bounds(self):
        spot = map_areas.parse(SPOT)
        self.assertTrue(spot.contains(45 + 60 / 111195, 10, 30))
        self.assertFalse(spot.contains(45 + 80 / 111195, 10, 30))
        self.assertFalse(spot.contains(float("nan"), 10))
        self.assertFalse(spot.contains(None, 10))
        south, west, north, east = map_areas.parse(BOX).bounds()
        self.assertAlmostEqual(south, 44.99)
        self.assertAlmostEqual(east, 10.02)
        # A line's metres around it; an area across the date line goes past 180.
        south, _west, north, _east = map_areas.parse(ROAD).bounds()
        self.assertLess(south, 44.9996)
        self.assertGreater(north, 45.0004)
        self.assertAlmostEqual(map_areas.parse(ACROSS).bounds()[3], 180.1)

    def test_names(self):
        self.assertEqual(map_areas.clean_name("  Test\tRoad\n"), "Test Road")
        self.assertEqual(len(map_areas.clean_name("x" * 300)), map_areas.NAME_LENGTH)
        self.assertEqual(map_areas.clean_name(None), "")


class AlbumOptionsTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([])
        db.init(common.load_config(), log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def test_a_smart_album_keeps_an_area_and_its_name(self):
        self.assertEqual(albums.clean_options({"area": ROAD, "aname": " Test Road "}),
                         {"area": ROAD, "aname": "Test Road"})
        # A name alone is not kept; an area of no known form is refused.
        self.assertEqual(albums.clean_options({"aname": "Test Road"}), {})
        for bad in ({"area": "l50.!!"}, {"area": 5}, {"area": ROAD, "aname": 5},
                    {"area": ROAD, "aname": "x" * 101}):
            with self.assertRaises(albums.AlbumError):
                albums.clean_options(bad)

    def test_an_area_alone_is_a_search(self):
        album = albums.create_album(self.conn, "By the road", kind="smart",
                                    options={"area": ROAD, "aname": "Test Road"})
        self.assertEqual(album["options"], {"area": ROAD, "aname": "Test Road"})
        changed = albums.update_album(self.conn, album["id"], {"options": {"area": BOX}})
        self.assertEqual(changed["options"], {"area": BOX})
        with self.assertRaises(albums.AlbumError):
            albums.update_album(self.conn, album["id"], {"options": {}})


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminRouteTest(PictureFixture, unittest.TestCase):
    """The admin's route makes a smart album of an area (and refuses a bad one)."""

    def test_save_an_area_as_a_smart_album(self):
        album = self.ok(self.post("/albums", {"name": "Old town", "kind": "smart", "query": "",
                                              "options": {"area": BOX, "aname": "Old town"}}),
                        201)["album"]
        self.assertEqual(album["options"], {"area": BOX, "aname": "Old town"})
        bad = self.post("/albums", {"name": "Bad", "kind": "smart", "query": "",
                                    "options": {"area": "b.!!"}})
        self.assertEqual(bad.status_code, 400)
        listed = [entry for entry in albums.index_albums(self.conn, ["lib_family1"], True)
                  if entry["name"] == "Old town"]
        self.assertEqual(listed[0]["options"]["area"], BOX)


@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class ExportTest(support.TempBaseMixin, unittest.TestCase):
    """A smart album's area goes out with the location option only."""

    def setUp(self):
        super().setUp()
        import imagegen
        import scan
        self.family = self.base.library_folder("family")
        self.base.write_config([support.library("Family", self.family, id="lib_family1")])
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        for name, where in (("road.jpg", (45.0001, 10.0)), ("far.jpg", (-30.0, 140.0))):
            exif = imagegen.make_exif(gps=where)
            with open(os.path.join(self.family, name), "wb") as handle:
                handle.write(imagegen.jpeg_bytes(imagegen.textured_image((64, 48)), exif))
        scan.run(self.config, jobs.JobContext("scan"), describe_images=False)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        for name, title in (("road.jpg", "By the road"), ("far.jpg", "Far away")):
            info = {"title": title, "description": "A road.", "tags": ["road"],
                    "photo_type": "photo", "sensitive": "none", "private_info": False}
            self.conn.execute("UPDATE images SET described = 1, prompt_ver = 6, info = ? "
                              "WHERE name = ?", (json.dumps(info), name))
        albums.create_album(self.conn, "By the road", kind="smart",
                            options={"area": ROAD, "aname": "Test Road"})
        albums.create_album(self.conn, "Roads", kind="smart", query="road")
        self.conn.commit()

    def export(self, **options):
        from test_export import read_manifest
        options = dict({"images": "none", "similar": False, "meaning": False, "looks": False,
                        "marks": True}, **options)
        self.sites = getattr(self, "sites", 0) + 1
        out = self.base.join("site-%d" % self.sites)
        export.export_library(export.LogContext(support.quiet), self.conn, self.config,
                              "lib_family1", out, options)
        return out, read_manifest(out)

    def all_text(self, folder):
        texts = []
        for root, _dirs, files in os.walk(folder):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    texts.append(handle.read())
        return b"\n".join(texts)

    def site_finds(self, out, area):
        """tests/js/export_test.js on a site: its search of an area (Node)."""
        if NODE is None:
            return
        path = os.path.join(out, "..", os.path.basename(out) + "-expected.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"area": area}, handle)
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                     "export_test.js"), out, path],
                                cwd=support.REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_with_the_location(self):
        out, manifest = self.export(location=True)
        published = {album["name"]: album for album in manifest["albums"]}
        self.assertEqual(published["By the road"]["options"],
                         {"area": ROAD, "aname": "Test Road"})
        # The published site searches an area (and the album of one) by its own files.
        self.site_finds(out, {"text": ROAD, "titles": ["By the road"]})
        self.site_finds(out, {"text": BOX, "query": "album:\"By the road\"",
                              "titles": ["By the road"]})
        out, manifest = self.export(location=False)
        self.assertEqual([album["name"] for album in manifest["albums"]], ["Roads"])
        text = self.all_text(out)
        self.assertNotIn(ROAD.encode("ascii"), text)
        self.assertNotIn(b"Test Road", text)
        # A site without locations has no areas.
        self.site_finds(out, {"text": ROAD})


class TemplateTest(unittest.TestCase):
    """The map page's area tools: each button's name says what it shows first (a voice
    command of its words reaches it)."""

    def test_the_names_of_the_tools_hold_their_words(self):
        with open(os.path.join(support.REPO, "templates", "map.html"), encoding="utf-8") as handle:
            text = handle.read()
        tools = text[text.index('<div id="map-area-tools"'):text.index('<div id="map-area-menu"')]
        found = re.findall(r'<button[^>]*aria-label="([^"]+)"[^>]*>([^<&]+)</button>', tools)
        self.assertEqual(len(found), 4, found)
        for label, shown in found:
            self.assertIn(shown.strip().lower(), label.lower())
        self.assertIn('aria-label="This view: search what the map shows"', tools)


class TripExportTest(unittest.TestCase):
    """A trip's export never publishes an area that comes near home."""

    def test_the_home_area_meets_boxes(self):
        home = trip_export.HomeArea((45.0, 10.0), 10)
        self.assertTrue(home.meets(44.99, 9.99, 45.01, 10.01))
        self.assertFalse(home.meets(46.0, 9.99, 46.1, 10.01))
        self.assertFalse(home.meets(44.99, 11.0, 45.01, 11.1))
        # Round the world and across the date line.
        self.assertTrue(home.meets(44.9, -180.0, 45.1, 180.0))
        far = trip_export.HomeArea((-16.95, 179.95), 10)
        self.assertTrue(far.meets(-17.0, 179.9, -16.9, 180.1))
        self.assertTrue(far.meets(-17.0, -180.2, -16.9, -179.9))
        self.assertFalse(far.meets(-17.0, -170.0, -16.9, -169.0))

    def test_an_area_near_home_is_not_published(self):
        near = trip_export.HomeArea((45.0, 10.0), 5)
        away = trip_export.HomeArea((50.0, 20.0), 5)
        for home, location, expected in ((near, True, False), (away, True, True),
                                         (None, True, True), (None, False, False)):
            stub = types.SimpleNamespace(options={"location": location}, home=home)
            self.assertEqual(trip_export._TripExport.area_published(stub, ROAD), expected)
            self.assertFalse(trip_export._TripExport.area_published(stub, "l50.!!"))


class RequestLogTest(unittest.TestCase):

    def test_an_area_never_shows_in_the_request_log(self):
        line = ('127.0.0.1 - - [01/Jan/2026 10:00:00] "GET /map?view=45,10,12&area=%s'
                '&aname=Test%%20Road&pin=45,10 HTTP/1.1" 200 -' % ROAD)
        quiet = serve.quiet_request_line(line)
        self.assertIn("GET /map?view=...&area=...&aname=...&pin=... HTTP/1.1", quiet)
        self.assertNotIn(ROAD, quiet)
        self.assertNotIn("Test", quiet)


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PagesTest(unittest.TestCase):
    """The area's text, the search, the search page and the map page (Node)."""

    def test_node(self):
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                     "map_areas_test.js")],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
