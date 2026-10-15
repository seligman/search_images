"""Tests for trip_export.py: a trip (trips.py) exported as a static website that opens on
the trip's page, with the export's privacy rules and home kept out (HomeArea).

Everything here is made up: pictures synthesized into temporary libraries at made-up
positions with made-up place names, a made-up home (the "home" setting of "trips"), and
dashcam clips whose GPS tracks are written straight into the index.
"""

import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import support

import cache
import common
import db
import drives
import export
import jobs
import places
import tracks
import trip_export
import trips
from test_admin import AdminFixture
from test_trips import TripData

if support.pillow_available():
    import imagegen
    from test_export import files_of, info, read_manifest, read_records
else:
    imagegen = None

HOME = (40.0, -100.0)
NEAR_HOME = (40.02, -99.97)         # about 3.5 km from HOME
LAKE = (40.0, -98.0)                # about 170 km away
FAR = (40.0, -94.0)
PHONE = "lib_phone002"
LAKES = "lib_lakes002"
SHELF = "lib_shelf002"
HOME_PLACE = ("Hometown", "Region One", "Countryland", "CL")
LAKE_PLACE = ("Lakeside", "Region Two", "Countryland", "CL")
FAR_PLACE = ("Farville", "Region Three", "Countryland", "CL")
NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "export_test.js")
SHELF_NAME = "SecretShelfName"
PHONE_NAME = "PhoneLibraryName"


def quiet(*_args, **_kwargs):
    pass


def trip_data(out):
    """data/trips.js of an export, parsed."""
    with open(os.path.join(out, "data", "trips.js"), encoding="ascii") as handle:
        text = handle.read()
    prefix, suffix = "SEARCH_DATA.trips(", ");\n"
    assert text.startswith(prefix) and text.endswith(suffix)
    return json.loads(text[len(prefix):-len(suffix)])


def points_of(out, name):
    """[(lat, lon)] of data/locations.js or the routes of data/routes.js (none: [])."""
    path = os.path.join(out, "data", name)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="ascii") as handle:
        text = handle.read()
    value = json.loads(text[text.index("(") + 1:text.rindex(")")])
    if name == "locations.js":
        return [(point["lat"], point["lon"]) for point in value]
    return [(lat, lon) for route in value for clip in route["c"]
            for _at, lat, lon, _speed in tracks.decode(clip["p"])]


def page_settings(out, name):
    with open(os.path.join(out, name), encoding="utf-8") as handle:
        return support.page_config(handle.read())


def fix(second, lat, lon):
    return tracks.Fix(float(second), 1715331600.0 + second, lat, lon, 25.0, 90.0)


class HomeAreaTest(unittest.TestCase):

    def test_squares_around_home(self):
        area = trip_export.HomeArea(HOME, 10)
        self.assertTrue(area.contains(*HOME))
        self.assertTrue(area.contains(*NEAR_HOME))
        self.assertFalse(area.contains(*LAKE))
        self.assertFalse(area.contains(None, None))
        # At least 10 km from home every way, at most 2 squares (3 across).
        for bearing in range(0, 360, 15):
            for km, inside in ((9.95, True), (2 * 10 * 1.5, False)):
                lat = HOME[0] + km * math.cos(math.radians(bearing)) / trip_export.KM_PER_DEGREE
                lon = HOME[1] + km * math.sin(math.radians(bearing)) / (
                    trip_export.KM_PER_DEGREE * math.cos(math.radians(HOME[0])))
                self.assertEqual(area.contains(lat, lon), inside, (bearing, km))
        # The squares do not move with home within its square: the edges do not tell
        # where home is.
        other = trip_export.HomeArea((HOME[0] + 0.01, HOME[1] + 0.01), 10)
        self.assertEqual((other.south, other.north, other.west, other.width),
                         (area.south, area.north, area.west, area.width))
        # Across the 180th meridian.
        east = trip_export.HomeArea((10.0, 179.99), 10)
        self.assertTrue(east.contains(10.0, -179.99))
        self.assertFalse(east.contains(10.0, 170.0))

    def test_the_grids_of_the_sizes_nest(self):
        # The middle square (the one holding home) of each size lies within the one of
        # the next size: exports made with different sizes tell no more together than
        # the one of the smallest size.
        def middle(area):
            return (area.south + area.lat_step, area.south + 2 * area.lat_step,
                    area.west + area.lon_step, area.west + 2 * area.lon_step)
        for home in (HOME, (46.83, -107.42), (-33.87, 151.21), (64.1, -21.9), (0.01, 0.01),
                     (52.71, 9.13)):
            squares = [middle(trip_export.HomeArea(home, km))
                       for km in trip_export.HOME_KM_STEPS]
            for small, large in zip(squares, squares[1:]):
                self.assertGreaterEqual(small[0], large[0] - 1e-9, home)
                self.assertLessEqual(small[1], large[1] + 1e-9, home)
                self.assertGreaterEqual(small[2], large[2] - 1e-9, home)
                self.assertLessEqual(small[3], large[3] + 1e-9, home)
            for km, square in zip(trip_export.HOME_KM_STEPS, squares):
                self.assertTrue(square[0] <= home[0] < square[1], (home, km))
                self.assertTrue(square[2] <= home[1] < square[3], (home, km))
                # At least km wide at home.
                width = (square[3] - square[2]) * trip_export.KM_PER_DEGREE * math.cos(
                    math.radians(home[0]))
                self.assertGreaterEqual(width, km - 1e-6, (home, km))

    def test_routes_are_cut_at_its_edge(self):
        area = trip_export.HomeArea(HOME, 10)
        # A drive from home to the lake, and a line passing through a corner of the area
        # with no point in it.
        points = [(second * 10.0, HOME[0], HOME[1] + second * 0.02, 20.0)
                  for second in range(0, 101)]
        route = {"d": 7, "l": "lib", "c": [{"i": 7, "o": 0.0,
                                            "p": tracks.encode_points(points)}]}
        trimmed = area.trim(route)
        kept = [point for clip in trimmed["c"] for point in tracks.decode(clip["p"])]
        self.assertTrue(kept)
        self.assertFalse(any(area.contains(point[1], point[2]) for point in kept))
        self.assertEqual(trimmed["c"][0].get("t"), 1)
        self.assertGreater(min(places.distance_km(point[1], point[2], *HOME) for point in kept),
                           10.0)
        corner = [(0.0, area.south - 0.02, area.west + 0.5 * area.width, 0.0),
                  (10.0, area.south + 0.1, area.west - 0.2 * area.width, 0.0)]
        self.assertTrue(area.crosses(corner[0][1:3], corner[1][1:3]))
        cut = area.trim({"d": 1, "l": "lib", "c": [
            {"i": 1, "o": 0.0, "p": tracks.encode_points(corner)}]})
        self.assertEqual([len(tracks.decode(clip["p"])) for clip in cut["c"]], [1, 1])
        inside = [(0.0, HOME[0], HOME[1], 0.0), (5.0, NEAR_HOME[0], NEAR_HOME[1], 0.0)]
        self.assertIsNone(area.trim({"d": 2, "l": "lib", "c": [
            {"i": 2, "o": 0.0, "p": tracks.encode_points(inside)}]}))
        far = [(0.0, LAKE[0], LAKE[1], 0.0), (5.0, LAKE[0], LAKE[1] + 0.01, 0.0)]
        same = area.trim({"d": 3, "l": "lib", "c": [
            {"i": 3, "o": 0.0, "p": tracks.encode_points(far)}]})
        self.assertNotIn("t", same["c"][0])


class OptionsTest(unittest.TestCase):

    def test_options(self):
        options = trip_export.trip_options()
        self.assertEqual(options["home_km"], trip_export.HOME_KM)
        self.assertIsNone(options["album"])
        self.assertFalse(options["location"])
        options = trip_export.trip_options({"location": True, "home_km": 20, "album": 4,
                                            "colour": "red"}, {"routes": True})
        self.assertEqual((options["location"], options["routes"], options["home_km"],
                          options["album"]), (True, True, 20, None))
        self.assertEqual(trip_export.options_to_save(options),
                         {"location": True, "routes": True, "home_km": 20})
        # A size kept before that is not one of the sizes: the default.
        for saved in (-3, 25, 200):
            self.assertEqual(trip_export.trip_options({"home_km": saved})["home_km"],
                             trip_export.HOME_KM)
        with self.assertRaises(export.OptionsError) as caught:
            trip_export.trip_options(None, {"home_km": 500, "album": 3, "images": "huge"})
        self.assertEqual(sorted(caught.exception.errors), ["album", "home_km", "images"])
        for bad in (True, 2.5, "10", "", None, 15, 200):
            with self.assertRaises(export.OptionsError):
                trip_export.trip_options(None, {"home_km": bad})
        self.assertEqual(trip_export.trip_options(None, {"home_km": 0})["home_km"], 0)
        for km in trip_export.HOME_KM_STEPS:
            self.assertEqual(trip_export.trip_options(None, {"home_km": km})["home_km"], km)

    def test_kept_options_that_cannot_be_used_are_named(self):
        # As a library's saved options: left at their defaults, never silently.
        kept = {"location": True, "home_km": 25, "colour": "red", "album": 4, "story": True}
        self.assertEqual(trip_export.option_warnings(kept), [
            'The saved export option "home_km" is ignored (left at its default): it must be '
            'one of 5, 10, 20, 40, 80, 160 kilometres, or 0 (home is not kept out).',
            'The saved export option "colour" is ignored: it is not an export option.'])
        # (In the words of the error an export given the same values gets.)
        with self.assertRaises(export.OptionsError) as caught:
            trip_export.trip_options(None, {"home_km": 25, "story": "yes"})
        self.assertEqual(trip_export.option_warnings({"home_km": 25, "story": "yes"}), [
            'The saved export option "%s" is ignored (left at its default): it %s.'
            % (key, caught.exception.errors[key]) for key in ("home_km", "story")])
        self.assertEqual(trip_export.option_warnings(kept, {"home_km": 10}),
                         ['The saved export option "colour" is ignored: it is not an export '
                          'option.'])
        self.assertEqual(trip_export.option_warnings(None), [])
        self.assertEqual(trip_export.option_warnings({"home_km": 0, "story": True}), [])
        # A story that is not true or false is read as off: said too.
        self.assertFalse(trip_export.trip_options({"story": "yes"})["story"])
        self.assertEqual(trip_export.option_warnings({"story": "yes"}), [
            'The saved export option "story" is ignored (left at its default): it must be '
            'true or false.'])
        self.assertEqual(trip_export.option_warnings({"story": "yes"}, {"story": True}), [])

    def test_the_dialog_offers_the_sizes(self):
        # The Export dialog asks for one of the sizes by name (no number field an empty
        # value would turn into 0, which is off).
        with open(os.path.join(support.REPO, "static", "manage.js"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("var HOME_KM_STEPS = [%s];" % ", ".join(
            str(km) for km in trip_export.HOME_KM_STEPS), text)
        self.assertIn("var HOME_KM_DEFAULT = %d;" % trip_export.HOME_KM, text)
        self.assertIn('homeKm = el("select", {className: "input export-home-km"});', text)
        self.assertNotIn("Number(homeKm.value) || 0", text)

    def test_gap_classes(self):
        self.assertEqual(trips.gap_classes([]), "")
        self.assertEqual(trips.gap_classes(["2024-05-11T10:00:00"]), "")
        self.assertEqual(trips.gap_classes([
            "2024-05-11T10:00:00", "2024-05-11T10:00:05", "2024-05-11T10:01:00",
            "2024-05-11T10:05:00", "", "2024-05-11T11:00:00", "2024-05-11T11:00:19"]),
            "012220")
        self.assertEqual((trips.SPREAD_SECONDS, trips.SAME_MOMENT_SECONDS), (120, 20))
        with open(os.path.join(support.REPO, "static", "trips.js"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("var SPREAD_SECONDS = %d;" % trips.SPREAD_SECONDS, text)
        self.assertIn("var SAME_MOMENT_SECONDS = %d;" % trips.SAME_MOMENT_SECONDS, text)


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class TripExportTest(support.TempBaseMixin, unittest.TestCase):
    """A phone, a trip library and another library; home; dashcam clips."""

    def setUp(self):
        super().setUp()
        base = self.base
        folders = {PHONE: base.library_folder(PHONE_NAME),
                   LAKES: base.library_folder("2024-05-12 - Lakes"),
                   SHELF: base.library_folder(SHELF_NAME)}
        self.write_config(folders)
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        names = {}

        def picture(library, name, date, position, place, **fields):
            folder = folders[library]
            imagegen.jpeg_with_exif(folder, name, image=imagegen.textured_image((160, 120)),
                                    date=date, offset="+00:00", gps=position)
            names[(library, name)] = (place, fields)

        # Home (ten days), an away trip in March (the phone), the lake trip in May.
        for day in range(1, 11):
            picture(PHONE, "home-%02d.jpg" % day, "2024:01:%02d 10:00:00" % day, HOME,
                    HOME_PLACE)
        for day in (3, 4, 6):
            for number in range(4):
                picture(PHONE, "mar-%d-%d.jpg" % (day, number),
                        "2024:03:%02d %02d:00:00" % (day, 10 + number), FAR, FAR_PLACE)
        picture(PHONE, "may-lake.jpg", "2024:05:11 15:00:00", LAKE, LAKE_PLACE)
        for day in (10, 11, 12):
            for number in range(3):
                picture(LAKES, "%d-%d.jpg" % (day, number),
                        "2024:05:%02d %02d:%02d:00" % (day, 12 + number, number), LAKE,
                        LAKE_PLACE)
        # Leaving home that morning: in the trip library, near home.
        picture(LAKES, "leaving-1.jpg", "2024:05:10 06:00:00", NEAR_HOME, HOME_PLACE)
        picture(LAKES, "leaving-2.jpg", "2024:05:10 06:00:10", NEAR_HOME, HOME_PLACE)
        picture(LAKES, "card.jpg", "2024:05:11 13:30:00", LAKE, LAKE_PLACE, private=True)
        picture(LAKES, "hidden.jpg", "2024:05:11 13:40:00", LAKE, LAKE_PLACE, hidden=True)
        picture(SHELF, "shelf.jpg", "2023:01:01 14:00:00", LAKE, LAKE_PLACE)
        import scan
        scan.run(common.load_config(), jobs.JobContext("test"), describe_images=False)
        self.ids = {}
        for (library, name), (place, fields) in names.items():
            row = self.conn.execute("SELECT id FROM images WHERE library_id = ? AND path = ?",
                                    (library, name)).fetchone()
            self.ids[name] = row["id"]
            extra = {}
            if name == "leaving-1.jpg":
                # (The AI model's words of where it is: the town of home.)
                extra = {"location_guess": "Main street of Hometown",
                         "landmarks": ["Hometown clock tower"],
                         "objects": [{"name": "bicycle"}]}
            value = info("Picture %s" % name.split(".")[0], ["view"],
                         private_info=bool(fields.get("private")), **extra)
            self.conn.execute(
                "UPDATE images SET info = ?, described = 1, prompt_ver = 5, place_city = ?, "
                "place_region = ?, place_country = ?, place_cc = ?, place_km = 1.0, "
                "hidden = ? WHERE id = ?",
                (json.dumps(value), place[0], place[1], place[2], place[3],
                 1 if fields.get("hidden") else 0, row["id"]))
        self.conn.commit()
        self.add_dashcam()

    def write_config(self, folders):
        self.base.write_config(
            [support.library(PHONE_NAME, folders[PHONE], id=PHONE),
             support.library("Lakes", folders[LAKES], id=LAKES),
             support.library(SHELF_NAME, folders[SHELF], id=SHELF)],
            trips={"home": "%s, %s" % HOME})

    def add_dashcam(self, clips=None, first_number=0):
        """Three clips of a dashcam in the trip library: one leaving home, one at the lake,
        one without a fix (with a thumbnail and a preview each, as a scan keeps them);
        or the clips given ({path: fixes or None})."""
        thumb = imagegen.jpeg_bytes(imagegen.textured_image((160, 90)))
        store = cache.PreviewStore()
        clips = clips or {
            "dash/20240510_070000.MP4": [fix(second, HOME[0] + 0.001, HOME[1] + second * 0.004)
                                         for second in range(0, 61)],
            "dash/20240511_120000.MP4": [fix(second, LAKE[0], LAKE[1] + second * 0.001)
                                         for second in range(0, 61)],
            "dash/20240512_080000.MP4": None,
        }
        with cache.ThumbStore() as thumbs:
            for number, (path, fixes) in enumerate(sorted(clips.items()), first_number):
                track = tracks.Track(fixes, "freegps") if fixes else None
                middle = track.middle(60.0) if track else None
                stamp = path.split("/")[1]
                taken = "%s-%s-%sT%s:%s:%s" % (stamp[0:4], stamp[4:6], stamp[6:8], stamp[9:11],
                                               stamp[11:13], stamp[13:15])
                cursor = self.conn.execute(
                    "INSERT INTO images (library_id, path, name, size, mtime, ctime, width, "
                    "height, added, updated, taken_at, taken_source, media, duration, "
                    "content_hash, camera, lat, lon, has_preview, info, described, prompt_ver) "
                    "VALUES (?, ?, ?, 1000, 1, 1, 160, 90, 1, 1, ?, 'video', 'video', 60.0, ?, "
                    "'DashCo', ?, ?, 1, ?, 1, 5)",
                    (LAKES, path, path.split("/")[1], taken, "dash-%d" % number,
                     middle.lat if middle else None, middle.lon if middle else None,
                     json.dumps(info("Drive %d" % number, ["road"]))))
                image_id = cursor.lastrowid
                self.ids[path] = image_id
                thumbs.put(image_id, thumb)
                store.write(LAKES, image_id, thumb)
                if track is not None:
                    tracks.store(self.conn, tracks.content_key({"content_hash": "dash-%d" % number,
                                                                "id": image_id}), track, 60.0)
        self.conn.commit()

    def export(self, name="trip", options=None, trip_id=LAKES):
        out = os.path.join(common.exports_dir(), name)
        summary = trip_export.export_trip(jobs.JobContext("test"), self.conn,
                                          common.load_config(), trip_id, out, options)
        return out, summary

    def texts(self, out):
        found = {}
        for rel in files_of(out):
            if rel.endswith((".html", ".js", ".css", ".txt")):
                with open(os.path.join(out, *rel.split("/")), encoding="utf-8") as handle:
                    found[rel] = handle.read()
        return found

    def test_trip_export(self):
        out, summary = self.export(options={"location": True, "routes": True,
                                            "routes_trim": 0})
        self.assertEqual(summary["trip"], LAKES)
        self.assertEqual(summary["title"], "Lakes")
        # The pages: the trip's first.
        files = set(files_of(out))
        for name in ("index.html", "search.html", "detail.html", "map.html", "data/trips.js",
                     "assets/trips.js", "assets/trips.css", "assets/map.js"):
            self.assertIn(name, files)
        config = page_settings(out, "index.html")
        self.assertEqual((config["page"], config["trip"], config["tripsPage"],
                          config["searchPage"], config["mode"]),
                         ("trips", "trip", "index.html", "search.html", "static"))
        self.assertEqual(page_settings(out, "search.html")["page"], "search")
        self.assertEqual(read_manifest(out)["trips"], "trips.js")
        records = {record["i"]: record for record in read_records(out)}
        titles = {record.get("t") for record in records.values()}
        # The trip's pictures of both libraries; not the private, hidden or other ones,
        # nor the dashcam's clip leaving home or the one without a fix.
        self.assertEqual(titles, {"Picture %d-%d" % (day, number) for day in (10, 11, 12)
                                  for number in range(3)}
                         | {"Picture leaving-1", "Picture leaving-2", "Picture may-lake",
                            "Drive 1"})
        self.assertEqual(summary["excluded"], {"home": 2, "private": 1, "hidden": 1})
        data = trip_data(out)
        self.assertEqual(data["best_per_day"], trips.DEFAULTS["best_per_day"])
        trip = data["trips"][0]
        self.assertEqual((trip["id"], trip["title"], trip["kind"], trip["start"], trip["end"]),
                         ("trip", "Lakes", "library", "2024-05-10", "2024-05-12"))
        self.assertEqual(trip["libraries"], [summary["export_id"]])
        self.assertEqual(trip["count"], len(records))
        self.assertEqual([day["date"] for day in trip["days"]],
                         ["2024-05-10", "2024-05-11", "2024-05-12"])
        listed = [xid for day in trip["days"] for xid in day["ids"]] + trip["other"]
        self.assertEqual(sorted(listed), sorted(records))
        first = trip["days"][0]
        # In the order taken: the two pictures leaving home 10 s apart (the same moment).
        self.assertEqual([records[xid]["t"] for xid in first["ids"]],
                         ["Picture leaving-1", "Picture leaving-2", "Picture 10-0",
                          "Picture 10-1", "Picture 10-2"])
        self.assertEqual(first["gaps"], "0222")
        self.assertEqual([place["name"] for place in trip["places"]], ["Lakeside"])
        self.assertEqual(first["places"], ["Lakeside"])
        # Home: no position, route or town near it; region and country stay.
        area = trip_export.HomeArea(HOME, trip_export.HOME_KM)
        located = points_of(out, "locations.js")
        route = points_of(out, "routes.js")
        self.assertTrue(located and route)
        self.assertFalse(any(area.contains(lat, lon) for lat, lon in located + route))
        leaving = [records[xid] for xid in first["ids"][:2]]
        for record in leaving:
            self.assertNotIn("gp", record)
            self.assertNotIn("ci", record)
            self.assertNotIn("km", record)
            self.assertEqual((record["rg"], record["co"]), HOME_PLACE[1:3])
        # Its words without those of where it is (the town of home).
        self.assertEqual(leaving[0]["ex"], "bicycle")
        for rel, text in self.texts(out).items():
            for secret in ("Hometown", SHELF_NAME, PHONE_NAME, PHONE, LAKES, SHELF,
                           "40.02,", "-99.97", "home-0", "dash/", "taken_at\":\"2024-05-10T"):
                self.assertNotIn(secret, text, "%s in %s" % (secret, rel))
        # A second export writes nothing.
        _out, again = self.export(options={"location": True, "routes": True,
                                           "routes_trim": 0})
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["export_id"], summary["export_id"])
        # The trip's key and dates are kept (its options only when asked to).
        edits = trips.load_edits()
        self.assertEqual(edits["exports"][LAKES], {"options": {},
                                                   "span": ["2024-05-10", "2024-05-12"]})

    def run_node(self, out, expected):
        """tests/js/export_test.js on an export, with these expectations."""
        folder = tempfile.mkdtemp(prefix="trip-export-node-")
        self.addCleanup(support.remove_tree, folder)
        path = os.path.join(folder, "expected.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(expected, handle)
        result = subprocess.run([NODE, NODE_TESTS, out, path], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_loaded_like_the_pages(self):
        out, summary = self.export(options={"location": True})
        for page in ("index.html", "search.html"):
            self.run_node(out, {"page": page, "count": summary["count"],
                                "trip": {"title": "Lakes"},
                                "queries": [{"query": "leaving", "count": 2}]})

    def test_a_private_picture_published_by_choice_is_never_featured(self):
        # The records of an export never say a picture shows private information
        # (export.PRIVATE_FLAG_KEYS), so the trip lists the pictures the owner's page
        # never features: the published page leaves out a private one the owner chose
        # to publish as the owner's own does -- not among a day's best, nor as a day's
        # only picture or on the cover (a favorite, it would be among the best of its day
        # and of the trip without the list).
        import albums
        card = self.ids["card.jpg"]
        albums.set_marks(self.conn, [card], favorite=True)
        self.conn.commit()
        out, summary = self.export("private", {"include_ids": [card], "marks": True})
        self.assertNotIn("private", summary["excluded"])
        records = {record.get("t"): record for record in read_records(out)}
        self.assertNotIn("pr", records["Picture card"])
        trip = trip_data(out)["trips"][0]
        self.assertEqual(trip["unfeatured"], [records["Picture card"]["i"]])
        if NODE is not None:
            self.run_node(out, {"page": "index.html", "count": summary["count"],
                                "trip": {"title": "Lakes", "unfeatured": ["Picture card"]},
                                "queries": [{"query": "is:private", "count": 0}]})
        # A trip without one has no list.
        out, _summary = self.export("not-private")
        self.assertNotIn("unfeatured", trip_data(out)["trips"][0])

    def test_choices_and_home_off(self):
        clip = self.ids["dash/20240510_070000.MP4"]
        # The clip leaving home, published by choice: its route is cut at the area.
        out, summary = self.export(options={"location": True, "routes": True,
                                            "routes_trim": 0,
                                            "include_ids": [[clip, ["home"]]]})
        self.assertEqual(summary["excluded"].get("home"), 1)
        area = trip_export.HomeArea(HOME, trip_export.HOME_KM)
        route = points_of(out, "routes.js")
        self.assertGreater(len(route), 2)
        self.assertFalse(any(area.contains(lat, lon) for lat, lon in route))
        # home_km 0: home is not kept out.
        out, summary = self.export("home-off", {"location": True, "routes": True,
                                                "routes_trim": 0, "home_km": 0})
        self.assertNotIn("home", summary["excluded"])
        self.assertTrue(any(area.contains(lat, lon)
                            for lat, lon in points_of(out, "locations.js")))
        self.assertIn("Hometown", json.dumps(trip_data(out)))
        # Without the location: no positions, no places, no map; the flags stay.
        out, summary = self.export("no-location")
        self.assertEqual(summary["excluded"].get("home"), 2)
        self.assertFalse(os.path.exists(os.path.join(out, "map.html")))
        self.assertEqual(points_of(out, "locations.js"), [])
        trip = trip_data(out)["trips"][0]
        self.assertEqual(trip["places"], [])
        self.assertTrue(all(day["places"] == [] for day in trip["days"]))
        for text in self.texts(out).values():
            for secret in ("Lakeside", "Hometown", "Region Two"):
                self.assertNotIn(secret, text)

    def test_away_trip_and_folders(self):
        out, summary = self.export("march", {"title": "Spring trip", "location": True},
                                   trip_id="away-20240303")
        trip = trip_data(out)["trips"][0]
        self.assertEqual((trip["title"], trip["kind"], trip["count"]),
                         ("Spring trip", "away", 12))
        self.assertEqual(summary["count"], 12)
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            self.assertIn("<title>Spring trip</title>", handle.read())
        # Each trip needs a folder of its own; a library's export folder is refused too.
        with self.assertRaises(export.ExportError) as caught:
            self.export("march")
        self.assertIn("holds the export of the trip Farville, Region Three", str(caught.exception))
        library_out = os.path.join(common.exports_dir(), "shelf")
        export.export_library(jobs.JobContext("test"), self.conn, common.load_config(), SHELF,
                              library_out)
        with self.assertRaises(export.ExportError):
            trip_export.export_trip(jobs.JobContext("test"), self.conn, common.load_config(),
                                    LAKES, library_out)
        # The default folder: data/exports/trip-<first day>-<name>.
        summary = trip_export.export_trip(jobs.JobContext("test"), self.conn,
                                          common.load_config(), LAKES)
        self.assertEqual(os.path.basename(summary["output"]), "trip-2024-05-10-lakes")
        with self.assertRaises(export.ExportError):
            trip_export.export_trip(jobs.JobContext("test"), self.conn, common.load_config(),
                                    "away-19990101")

    def test_the_words_of_a_drive_filmed_near_home_stay_home(self):
        # Three clips before the one at the lake make a drive of four: its first was
        # filmed near home (left out), the others at the lake.
        self.add_dashcam({
            "dash/20240511_115700.MP4": [fix(second, NEAR_HOME[0], NEAR_HOME[1] + second * 1e-4)
                                         for second in range(0, 61)],
            "dash/20240511_115800.MP4": [fix(second, LAKE[0], LAKE[1] - 0.2 + second * 1e-3)
                                         for second in range(0, 61)],
            "dash/20240511_115900.MP4": [fix(second, LAKE[0], LAKE[1] - 0.1 + second * 1e-3)
                                         for second in range(0, 61)]}, first_number=3)
        found = [drive for drive in drives.library_drives(self.conn, LAKES, common.load_config())
                 if len(drive.clips) == 4]
        self.assertEqual(len(found), 1)
        drives.save_description(LAKES, found[0], {
            "title": "Drive from the garage", "description": "Out of the garage at home.",
            "tags": ["driveway"]}, {"model": "fake"})
        options = {"location": True, "routes": True, "routes_trim": 0}
        preview = trip_export.preview_trip(self.conn, common.load_config(), LAKES, options)
        self.assertEqual(preview["counts"]["drives"]["withheld"], 1)
        self.assertEqual(preview["counts"]["drives"]["described"], 0)
        out, summary = self.export("drive", options)
        records = read_records(out)
        clips = [record for record in records if record.get("dv")]
        self.assertEqual(len(clips), 3)
        for record in records:
            for key in ("dvt", "dvd", "dvg"):
                self.assertNotIn(key, record)
        for rel, text in self.texts(out).items():
            self.assertNotIn("Drive from the garage", text, rel)
        # The clip near home published by choice: its drive's words go too.
        first = self.ids["dash/20240511_115700.MP4"]
        out, summary = self.export("drive-chosen", dict(options, include_ids=[[first, ["home"]]]))
        self.assertIn("Drive from the garage", json.dumps(read_records(out)))
        # And with home not kept out.
        out, summary = self.export("drive-home-off", dict(options, home_km=0))
        self.assertIn("Drive from the garage", json.dumps(read_records(out)))

    def test_a_folder_holds_one_trip_or_library(self):
        out, _summary = self.export("lakes", {"location": True})
        config = common.load_config()
        # A library's export into the trip's folder is refused while the trip is found.
        with self.assertRaises(export.ExportError) as caught:
            export.check_output_dir(out, config, SHELF)
        self.assertIn("holds the export of the trip Lakes", str(caught.exception))
        with self.assertRaises(export.ExportError):
            export.export_library(jobs.JobContext("test"), self.conn, config, SHELF, out)
        self.assertTrue(os.path.isfile(os.path.join(out, "data", "trips.js")))
        # So is another trip's.
        with self.assertRaises(export.ExportError) as caught:
            export.check_output_dir(out, config, trip_export.KEY_PREFIX + "away-20240303")
        self.assertIn("the trip Lakes", str(caught.exception))
        export.check_output_dir(out, config, trip_export.KEY_PREFIX + LAKES)
        # Once the trip is not found any more, its folder may be used again.
        self.base.write_config(
            [support.library(PHONE_NAME, self.base.library_folder(PHONE_NAME), id=PHONE),
             support.library("Lakes", self.base.library_folder("2024-05-12 - Lakes"), id=LAKES,
                             enabled=False),
             support.library(SHELF_NAME, self.base.library_folder(SHELF_NAME), id=SHELF)],
            trips={"home": "%s, %s" % HOME})
        config = common.load_config()
        export.check_output_dir(out, config, SHELF)
        ctx = jobs.JobContext("test")
        logged = []
        ctx.log = lambda message, level="info": logged.append(message)
        export.export_library(ctx, self.conn, config, SHELF, out)
        self.assertTrue(any("an export of a trip that is no longer found" in message
                            for message in logged), logged)

    def test_the_title_names_no_hidden_town_nor_home(self):
        # The March trip is named after its town; with all of its pictures there hidden
        # but one day's, moved near home, the title comes from what is published.
        out, summary = self.export("march", {"location": True}, trip_id="away-20240303")
        self.assertEqual(summary["title"], "Farville, Region Three")
        march = [self.ids[name] for name in sorted(self.ids) if name.startswith("mar-")]
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id IN (%s)"
                          % ",".join("?" * 8), march[:8])
        self.conn.execute("UPDATE images SET place_city = 'Otherplace' WHERE id IN (%s)"
                          % ",".join("?" * 4), march[8:])
        self.conn.commit()
        out, summary = self.export("march-2", {"location": True}, trip_id="away-20240303")
        self.assertEqual(summary["title"], "Otherplace, Region Three")
        data = trip_data(out)["trips"][0]
        self.assertEqual((data["title"], data["start"], data["end"]),
                         ("Otherplace, Region Three", "2024-03-06", "2024-03-06"))
        self.assertNotIn("Farville", json.dumps(trip_data(out)))

    def test_preview_and_command_line(self):
        result = trip_export.preview_trip(self.conn, common.load_config(), LAKES,
                                          {"location": True})
        counts = result["counts"]
        self.assertEqual(counts["trip"], {"title": "Lakes"})
        self.assertEqual(counts["by_reason"], {"home": 2, "private": 1, "hidden": 1})
        flagged = {item["id"]: item["reasons"] for item in result["flagged"]}
        self.assertEqual(flagged[self.ids["dash/20240510_070000.MP4"]], ["home"])
        self.assertEqual(flagged[self.ids["dash/20240512_080000.MP4"]], ["home"])
        self.assertTrue(result["home"])
        self.assertEqual(result["trip"]["count"], 16)
        out = os.path.join(common.exports_dir(), "from-the-command-line")
        summary = export.main(["--trip", LAKES, "--out", out, "--location", "--home-km", "20"])
        self.assertEqual(trips.load_edits()["exports"][LAKES]["span"], ["2024-05-10", "2024-05-12"])
        self.assertEqual(summary["trip"], LAKES)
        self.assertTrue(os.path.isfile(os.path.join(out, "data", "locations.js")))
        with self.assertRaises(SystemExit):
            export.main(["--trip", "no-such-trip"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                export.main(["--trip", LAKES, "--album", "Best"])
        # --home-km is a trip's: a library's export has no area around home.
        told = io.StringIO()
        with contextlib.redirect_stderr(told):
            with self.assertRaises(SystemExit):
                export.main(["--library", SHELF, "--home-km", "5", "--location"])
        self.assertIn("--home-km only applies with --trip", told.getvalue())
        with self.assertRaises(SystemExit) as caught:
            export.main(["--trip", LAKES, "--out", out, "--home-km", "15"])
        self.assertIn("home_km", str(caught.exception))

    def add_raw_files(self):
        """The RAW originals of three of the trip's pictures, with GPS positions: leaving
        home (near home), at the lake, and one at the lake whose RAW file says it was
        near home (the same camera and second as their pictures: one shot)."""
        import rawgen
        import scan
        folder = self.base.library_folder("2024-05-12 - Lakes")
        for name, position, date in (("leaving-1.ARW", NEAR_HOME, "2024:05:10 06:00:00"),
                                     ("10-0.ARW", LAKE, "2024:05:10 12:00:00"),
                                     ("11-0.ARW", NEAR_HOME, "2024:05:11 12:00:00")):
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(rawgen.arw(gps=position, date=date, make="Apple",
                                        model="iPhone 12"))
        scan.run(common.load_config(), jobs.JobContext("test"), describe_images=False)

    def raw_positions(self, out):
        """[(lat, lon)] of the GPS positions in the RAW files of an export (rounded)."""
        import imageinfo
        import raw_photos
        found = []
        for rel in files_of(out):
            if rel.startswith("raws/"):
                info = raw_photos.read_file(os.path.join(out, *rel.split("/")))
                lat, lon = imageinfo.exif_gps_to_decimal(raw_photos.exif_dict(info).get("GPS"))
                found.append((round(lat, 3), round(lon, 3)))
        return found

    def test_a_raw_file_near_home_stays_home(self):
        self.add_raw_files()
        options = {"location": True, "routes": True, "routes_trim": 0, "raw_files": True}
        preview = trip_export.preview_trip(self.conn, common.load_config(), LAKES, options)
        self.assertEqual(preview["counts"]["raws"], {"total": 3, "included": 1, "location": 2})
        ctx = jobs.JobContext("test")
        out = os.path.join(common.exports_dir(), "raws")
        summary = trip_export.export_trip(ctx, self.conn, common.load_config(), LAKES, out,
                                          options)
        # Only the lake's goes (its picture's position goes too); the one leaving home
        # stays home, as its picture's position does, and so does the one whose own
        # position is near home.
        self.assertEqual(self.raw_positions(out), [LAKE])
        self.assertEqual((summary["raws"], summary["raw_located"], summary["raw_failed"]),
                         (1, 2, 0))
        log = "\n".join(entry["message"] for entry in ctx.snapshot(log_tail=200)["log_tail"])
        self.assertIn("the RAW files of 2 picture(s) stay home: they hold a GPS position, and "
                      "the picture's own position is not published or is near home", log)
        # Without the location none goes, and the summary and the log say why.
        ctx = jobs.JobContext("test")
        summary = trip_export.export_trip(ctx, self.conn, common.load_config(), LAKES, out,
                                          {"location": False, "raw_files": True})
        self.assertEqual(self.raw_positions(out), [])
        self.assertEqual((summary["raws"], summary["raw_located"]), (0, 3))
        log = "\n".join(entry["message"] for entry in ctx.snapshot(log_tail=200)["log_tail"])
        self.assertIn("the RAW files of 3 picture(s) stay home", log)

    def test_the_local_times_follow_the_gps_check_before_an_export(self):
        # (As a library export's: a position the check sets aside gives no local time.)
        import gps_check
        import local_times
        calls = []
        with mock.patch.object(gps_check, "run_safely", return_value={"ignored": 1}), \
                mock.patch.object(local_times, "after_check_safely",
                                  side_effect=lambda *args, **kwargs: calls.append(args)):
            self.export(options={"location": True})
        self.assertEqual(len(calls), 1)
        calls = []
        with mock.patch.object(gps_check, "run_safely", return_value=None), \
                mock.patch.object(local_times, "after_check_safely",
                                  side_effect=lambda *args, **kwargs: calls.append(args)):
            self.export(name="again", options={"location": True})
        self.assertEqual(calls, [])


class AdminTripExportTest(TripData, AdminFixture, unittest.TestCase):
    """The admin's routes and the export job of a trip (pictures without files)."""

    def setUp(self):
        super().setUp()
        self.write_config(self.libraries(self.base))
        db.init(log=support.quiet)
        self.make_pictures(self.conn)

    def test_routes(self):
        info = self.ok(self.get("/trips/away-20240303/export"))
        self.assertEqual(info["trip"]["title"], "Farville, Region One")
        self.assertEqual(info["options"]["home_km"], trip_export.HOME_KM)
        self.assertIsNone(info["last"])
        self.assertTrue(info["home"])
        self.assertTrue(info["default_output"].endswith("trip-2024-03-03-farville-region-one"))
        self.assertEqual(self.get("/trips/no-such/export").status_code, 404)
        preview = self.ok(self.post("/trips/away-20240303/export/preview",
                                    {"options": {"location": True}, "out_dir": ""}))
        self.assertEqual(preview["counts"]["total"], 13)
        self.assertEqual(preview["counts"]["trip"]["title"], "Farville, Region One")
        self.assertIn("free_bytes", preview)
        response = self.post("/trips/away-20240303/export/preview",
                             {"options": {"home_km": 1000}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("home_km", response.get_json()["fields"])
        self.assertEqual(self.post("/trips/away-20240303/export/open-folder").status_code, 404)
        # Not for visitors.
        self.assertEqual(self.client.get("/api/admin/trips/away-20240303/export",
                                         headers={"X-Search-Images": "1"},
                                         environ_base={"REMOTE_ADDR": "192.0.2.5"}
                                         ).status_code, 403)

    def test_export_job(self):
        bad = self.post("/jobs", {"kind": "export", "params": {"trip": "no-such"}})
        self.assertEqual(bad.status_code, 404)
        bad = self.post("/jobs", {"kind": "export", "library_id": "lib_phone001",
                                  "params": {"trip": "away-20240303"}})
        self.assertEqual(bad.status_code, 400, bad.get_data(as_text=True))
        bad = self.post("/jobs", {"kind": "export", "params": {
            "trip": "away-20240303", "options": {"album": 3}}})
        self.assertEqual(bad.status_code, 400)
        self.manager.stop(30)
        # A job of every library first: the cards keep showing it after a trip's export.
        everyone, added = self.manager.enqueue("scan", None, {})
        self.assertTrue(added)
        reply = self.ok(self.post("/jobs", {"kind": "export", "params": {
            "trip": "away-20240303", "options": {"location": True, "home_km": 20},
            "save_options": True}}), 202)
        job = reply["job"]
        self.assertIsNone(job["library_id"])
        self.assertEqual(job["params"], {"trip": "away-20240303",
                                         "trip_title": "Farville, Region One"})
        # No library's job: the libraries' cards do not show it as theirs.
        state = self.ok(self.get("/state"))
        self.assertTrue(state["libraries"])
        for library in state["libraries"]:
            self.assertEqual(library["last_job"]["id"], everyone["id"], library["id"])
        self.assertNotIn(None, [job["library_id"] for job in
                                self.manager.latest_by_library().values()
                                if job["kind"] == "export"])
        # The options were saved with the trip (data/trips.json).
        info = self.ok(self.get("/trips/away-20240303/export"))
        self.assertEqual((info["options"]["location"], info["options"]["home_km"]), (True, 20))
        with open(trips.path(), encoding="utf-8") as handle:
            saved = json.load(handle)["exports"]["away-20240303"]
        self.assertEqual(saved["options"], {"location": True, "home_km": 20})
        # A trip's export job into a folder that holds another trip's export is refused
        # when it is made (not only once it runs).
        folder = os.path.join(common.exports_dir(), "lakes-site")
        os.makedirs(os.path.join(folder, "data"))
        with open(os.path.join(folder, export.MARKER), "w", encoding="utf-8") as handle:
            json.dump({"export_id": "abcdefabcdef"}, handle)
        export.save_state({"export_id": "abcdefabcdef", "secret": "0" * 64, "files": {},
                           "library_id": trip_export.KEY_PREFIX + "lib_lakes001"})
        bad = self.post("/jobs", {"kind": "export", "params": {
            "trip": "away-20240303", "out_dir": folder}})
        self.assertEqual(bad.status_code, 400, bad.get_data(as_text=True))
        self.assertIn("holds the export of the trip Lakes", bad.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
