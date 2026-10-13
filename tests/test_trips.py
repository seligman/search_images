"""Tests for trips.py (trips found in the index, the owner's changes) and the Trips pages:
/trips, /trip/<id>, /api/trips (serve.py) and the admin routes (admin.py), and the
page-free parts of static/trips.js under Node (tests/js/trips_test.js).

Every picture here is made up: synthetic rows in a temporary SETTINGS_BASE, at made-up
positions with made-up place names.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import unittest

import support

import common
import db
import looks
import trips
from test_admin import AdminFixture
from test_admin_images import REMOTE
from test_look_groups import DIMS, packed, turned, unit

page_config = support.page_config
NODE = shutil.which("node")
STATIC = os.path.join(support.REPO, "static")

HOME = (40.0, -100.0)
FAR = (40.0, -94.0)              # about 510 km from HOME
NEAR_FAR = (40.1, -94.1)         # about 14 km from FAR
OTHER_FAR = (40.0, -86.0)        # about 680 km from FAR, 1,190 from HOME
LAKE = (35.0, -90.0)
ANTIPODES = (-35.0, 150.0)       # far from everything here
PHONE = "lib_phone001"
LAKES = "lib_lakes001"
HOME_PLACE = ("Hometown", "Region One", "Countryland", "CL")
FAR_PLACE = ("Farville", "Region One", "Countryland", "CL")
NEAR_FAR_PLACE = ("Nearton", "Region One", "Countryland", "CL")
OTHER_PLACE = ("Otherburg", "Region Four", "Countryland", "CL")
OVER_PLACE = ("Overtown", "Region Three", "Countryland", "CL")
LAKE_PLACE = ("Lakeside", "Region Two", "Countryland", "CL")
NOW = 1700000000.0


def add(conn, library_id, path, taken="", source="exif", position=None, place=None, **fields):
    """Insert a picture row; returns its id."""
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1], "size": 100,
           "mtime": NOW, "ctime": NOW, "width": 640, "height": 480, "added": NOW,
           "updated": NOW, "taken_at": taken, "taken_source": source if taken else ""}
    if position is not None:
        row["lat"], row["lon"] = position
    if place is not None:
        row.update(place_city=place[0], place_region=place[1], place_country=place[2],
                   place_cc=place[3], place_km=1.0)
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute("INSERT INTO images (%s) VALUES (%s)" % (
        ",".join(columns), ",".join("?" * len(columns))), [row[key] for key in columns])
    conn.commit()
    return cursor.lastrowid


class TripData:
    """Libraries and pictures: home, an away run, a short run, and a trip library."""

    def make_pictures(self, conn):
        self.ids = {}
        # Ten days at home.
        for day in range(1, 11):
            add(conn, PHONE, "home/%02d.jpg" % day, "2024-01-%02dT10:00:00" % day,
                position=HOME, place=HOME_PLACE)
        # Away from home: 3-6 March (the 5th has no picture).
        self.ids["home_morning"] = add(conn, PHONE, "mar/morning.jpg", "2024-03-03T07:00:00",
                                       position=HOME, place=HOME_PLACE)
        self.ids["away"] = []
        for day, count in ((3, 4), (4, 4), (6, 4)):
            for number in range(count):
                self.ids["away"].append(add(
                    conn, PHONE, "mar/%d-%d.jpg" % (day, number),
                    "2024-03-%02dT%02d:00:00" % (day, 10 + number), position=FAR,
                    place=FAR_PLACE))
        self.ids["unlocated"] = add(conn, PHONE, "mar/no-gps.jpg", "2024-03-04T18:00:00")
        # Two pictures away on 1 April: too few for a trip.
        for number in range(2):
            add(conn, PHONE, "apr/%d.jpg" % number, "2024-04-01T12:0%d:00" % number,
                position=FAR, place=FAR_PLACE)
        # The trip library: 10-12 May, a picture of a camera whose clock was wrong and one
        # dated by its file only.
        self.ids["lakes"] = []
        for day in (10, 11, 12):
            for number in range(3):
                self.ids["lakes"].append(add(
                    conn, LAKES, "%d/%d.jpg" % (day, number),
                    "2024-05-%02dT%02d:00:00" % (day, 9 + number), position=LAKE,
                    place=LAKE_PLACE, content_hash="lake-%d-%d" % (day, number),
                    camera="Maker Cam"))
        self.ids["wrong_clock"] = add(conn, LAKES, "old.jpg", "2023-01-01T12:00:00")
        self.ids["file_date"] = add(conn, LAKES, "copy.jpg", "2024-05-20T12:00:00",
                                    source="mtime")
        # The phone during that trip: away (joins), a copy of a trip picture (left
        # out), at home that morning (left out), no position on a day away (joins).
        self.ids["phone_at_lake"] = add(conn, PHONE, "may/lake.jpg", "2024-05-11T15:00:00",
                                        position=LAKE, place=LAKE_PLACE)
        self.ids["phone_copy"] = add(conn, PHONE, "may/copy.jpg", "2024-05-11T09:00:00",
                                     position=LAKE, place=LAKE_PLACE,
                                     content_hash="lake-11-0")
        self.ids["phone_same_shot"] = add(conn, PHONE, "may/shot.jpg", "2024-05-12T10:00:00",
                                          position=LAKE, place=LAKE_PLACE, camera="Maker Cam")
        self.ids["phone_home"] = add(conn, PHONE, "may/home.jpg", "2024-05-10T06:00:00",
                                     position=HOME, place=HOME_PLACE)
        self.ids["phone_no_gps"] = add(conn, PHONE, "may/no-gps.jpg", "2024-05-11T20:00:00")

    def libraries(self, base):
        return [support.library("Phone", base.library_folder("Phone"), id=PHONE),
                support.library("Lakes", base.library_folder("2024-05-12 - Lakes"), id=LAKES)]


class SettingsTest(unittest.TestCase):

    def test_defaults_and_limits(self):
        options = trips.settings({})
        self.assertEqual(options, dict(trips.DEFAULTS))
        options = trips.settings({"trips": {"away_km": 1, "max_gap_days": 30, "min_pictures": 0,
                                            "best_per_day": 99, "enabled": "no",
                                            "home": "12.5, 7.25"}})
        self.assertEqual((options["away_km"], options["max_gap_days"], options["min_pictures"],
                          options["best_per_day"], options["enabled"], options["home"]),
                         (5.0, 7, 1, 24, True, (12.5, 7.25)))
        self.assertFalse(trips.settings({"trips": False})["enabled"])
        for value in ("north", "100, 0", [1], [True, 2], "0, 0", float("nan")):
            self.assertIsNone(trips.parse_home(value), value)
        self.assertEqual(trips.parse_home([-33.5, 151]), (-33.5, 151.0))

    def test_trip_folders(self):
        for path in ("C:\\Photos\\2024-11-09 - Island", "/home/you/2024-11 Island",
                     "D:\\Photos\\20241109_Island", "\\\\server\\share\\2014 - Trip\\"):
            self.assertTrue(trips.is_trip_folder(path), path)
        for path in ("C:\\Photos\\Family", "C:\\Photos\\2001 Space Odyssey", "",
                     "C:\\Photos\\2024",
                     "D:\\Photos\\1999ers"):
            self.assertFalse(trips.is_trip_folder(path), path)
        config = {"libraries": [{"id": "lib_a", "path": "C:\\Photos\\2024-05-12 - Lakes"},
                                {"id": "lib_b", "path": "C:\\Photos\\Family"}]}
        self.assertEqual(trips.trip_libraries(config), ["lib_a"])
        self.assertEqual(trips.trip_libraries(config, {"lib_b"}), [])
        config["trips"] = {"library_folders": False}
        self.assertEqual(trips.trip_libraries(config), [])

    def test_home_is_where_the_most_days_are(self):
        points = [(HOME[0] + 0.01 * k, HOME[1], "2024-01-%02d" % (k + 1)) for k in range(5)]
        # Many pictures of one day somewhere else count as one day.
        points += [(FAR[0], FAR[1], "2024-03-03")] * 50
        lat, lon = trips.find_home(points)
        self.assertAlmostEqual(lat, HOME[0] + 0.02, places=6)
        self.assertAlmostEqual(lon, HOME[1], places=6)
        self.assertIsNone(trips.find_home(points[:2]))
        self.assertIsNone(trips.find_home([]))

    def test_words(self):
        self.assertEqual(trips.span_words("2024-11-09", "2024-11-15"), "9-15 November 2024")
        self.assertEqual(trips.span_words("2024-10-30", "2024-11-02"),
                         "30 October - 2 November 2024")
        self.assertEqual(trips.span_words("2023-12-30", "2024-01-02"),
                         "30 December 2023 - 2 January 2024")
        self.assertEqual(trips.span_words("2024-11-09", "2024-11-09"), "9 November 2024")
        self.assertEqual(trips.clean_title(" A\tlong\n trip "), "A long trip")
        self.assertEqual(len(trips.clean_title("x" * 500)), trips.TITLE_LENGTH)


class Placed:
    """A stand-in for a picture with a place (auto_title, trip_places)."""

    def __init__(self, city, region, country, cc="CL"):
        self.place = (city, region, country, cc)


class TitleTest(unittest.TestCase):

    def test_titles_from_places(self):
        def title(*groups, home="Countryland"):
            pictures = []
            for count, place in groups:
                pictures.extend(Placed(*place) for _ in range(count))
            return trips.auto_title(pictures, home)
        self.assertEqual(title((8, ("Farville", "Region One", "Countryland"))),
                         "Farville, Region One")
        self.assertEqual(title((5, ("Farville", "Region One", "Countryland")),
                               (5, ("Nearby", "Region One", "Countryland"))), "Region One")
        self.assertEqual(title((5, ("Farville", "Region One", "Abroad")),
                               (5, ("Nearby", "Region One", "Abroad"))), "Region One, Abroad")
        self.assertEqual(title((5, ("A", "Region One", "Countryland")),
                               (5, ("B", "Region Two", "Countryland"))),
                         "Region One and Region Two")
        self.assertEqual(title((5, ("A", "Region One", "Countryland")),
                               (5, ("B", "Region Two", "Abroad"))), "Countryland and Abroad")
        self.assertEqual(trips.auto_title([]), "")

    def test_places_in_order_without_a_bare_region(self):
        pictures = [Placed("", "Region One", "Countryland"), Placed("Farville", "Region One",
                                                                   "Countryland"),
                    Placed("Nearby", "Region One", "Countryland"),
                    Placed("Farville", "Region One", "Countryland"),
                    Placed("", "Region Two", "Countryland")]
        names = [entry["name"] for entry in trips.trip_places(pictures)]
        self.assertEqual(names, ["Farville", "Nearby", "Region Two"])
        many = [Placed("Town %d" % (k % 20), "R", "C") for k in range(100)]
        many += [Placed("Town 3", "R", "C")] * 10
        listed = trips.trip_places(many, limit=5)
        self.assertEqual(len(listed), 5)
        self.assertIn("Town 3", [entry["name"] for entry in listed])


class DetectTest(TripData, support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.config = self.base.write_config(self.libraries(self.base))
        db.init(log=support.quiet)
        self.config = common.load_config()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.make_pictures(self.conn)

    def found(self, **settings):
        if settings:
            self.config["trips"] = settings
        return {trip.id: trip for trip in trips.build(self.conn, self.config, [PHONE, LAKES],
                                                     edits={}).trips}

    def ids_of(self, trip):
        return sorted(picture.id for picture in trip.pictures)

    def test_an_away_run_and_a_trip_library(self):
        found = self.found()
        self.assertEqual(sorted(found), ["away-20240303", LAKES])
        away = found["away-20240303"]
        self.assertEqual((away.kind, away.start, away.end), ("away", "2024-03-03", "2024-03-06"))
        # The picture at home that morning is not part of it; the one without a
        # position on a day away is.
        self.assertEqual(self.ids_of(away), sorted(self.ids["away"] + [self.ids["unlocated"]]))
        self.assertEqual(away.title, "Farville, Region One")
        lakes = found[LAKES]
        self.assertEqual((lakes.kind, lakes.start, lakes.end, lakes.title),
                         ("library", "2024-05-10", "2024-05-12", "Lakes"))
        self.assertEqual(sorted(picture.id for picture in lakes.other),
                         sorted([self.ids["wrong_clock"], self.ids["file_date"]]))
        # The phone's pictures away join; its copies of the trip's pictures (the same
        # file, the same camera and second) and its picture at home do not.
        self.assertEqual(self.ids_of(lakes), sorted(self.ids["lakes"] + [
            self.ids["phone_at_lake"], self.ids["phone_no_gps"]]))
        for name in ("phone_copy", "phone_same_shot", "phone_home"):
            self.assertNotIn(self.ids[name], self.ids_of(lakes), name)

    def test_same_shot_needs_the_same_second(self):
        # A picture by a camera of the same name a few seconds later is another shot; and
        # a date from the file's time is no capture second.
        later = add(self.conn, PHONE, "may/later.jpg", "2024-05-12T10:00:30", position=LAKE,
                    place=LAKE_PLACE, camera="Maker Cam")
        self.assertIn(later, self.ids_of(self.found()[LAKES]))
        self.conn.execute("UPDATE images SET taken_source = 'filename' WHERE id = ?",
                          (self.ids["phone_same_shot"],))
        self.conn.commit()
        self.assertIn(self.ids["phone_same_shot"], self.ids_of(self.found()[LAKES]))

    def test_settings_change_what_is_found(self):
        self.assertEqual(sorted(self.found(min_pictures=2)),
                         ["away-20240303", "away-20240401", LAKES])
        self.assertEqual(sorted(self.found(away_km=1000)), [LAKES])
        self.assertEqual(sorted(self.found(library_folders=False)), ["away-20240303",
                                                                     "away-20240510"])
        self.assertEqual(self.found(enabled=False), {})
        # No gap allowed: the days before and after the 5th still join, as days
        # without pictures in a stay in one place (STAY_KM).
        found = self.found(max_gap_days=0)
        self.assertEqual(sorted(found), ["away-20240303", LAKES])
        self.assertEqual(found["away-20240303"].end, "2024-03-06")
        # A home given in the settings: here the far place is home.
        found = self.found(home=list(FAR))
        self.assertNotIn("away-20240303", found)

    def test_a_day_at_home_ends_a_run(self):
        add(self.conn, PHONE, "mar/home-5th.jpg", "2024-03-05T12:00:00", position=HOME,
            place=HOME_PLACE)
        found = self.found(min_pictures=4)
        self.assertIn("away-20240303", found)
        self.assertIn("away-20240306", found)
        self.assertEqual(found["away-20240303"].end, "2024-03-04")

    def test_days_without_pictures_in_a_stay(self):
        def away_on(day, position, place, count=3):
            for number in range(count):
                add(self.conn, PHONE, "stay/%s-%d.jpg" % (day, number),
                    "%sT1%d:00:00" % (day, number), position=position, place=place)
        # Two days between, the same place: one trip.
        away_on("2024-07-01", FAR, FAR_PLACE)
        away_on("2024-07-04", NEAR_FAR, NEAR_FAR_PLACE)
        # Two days between, far apart: two.
        away_on("2024-08-01", FAR, FAR_PLACE)
        away_on("2024-08-04", OTHER_FAR, OTHER_PLACE)
        # A day at home between: two.
        away_on("2024-09-01", FAR, FAR_PLACE)
        away_on("2024-09-02", HOME, HOME_PLACE, 1)
        away_on("2024-09-04", FAR, FAR_PLACE)
        # Four days between: two (STAY_GAP_DAYS).
        away_on("2024-10-01", FAR, FAR_PLACE)
        away_on("2024-10-06", FAR, FAR_PLACE)
        found = self.found(min_pictures=3)
        self.assertEqual((found["away-20240701"].start, found["away-20240701"].end),
                         ("2024-07-01", "2024-07-04"))
        for first, second in (("20240801", "20240804"), ("20240901", "20240904"),
                              ("20241001", "20241006")):
            self.assertIn("away-" + first, found)
            self.assertIn("away-" + second, found)
        # max_gap_days still joins any two (no day at home between).
        self.assertEqual(self.found(min_pictures=3, max_gap_days=2)["away-20240801"].end,
                         "2024-08-04")

    def test_a_short_trip_of_two_days_or_more(self):
        # Half of min_pictures over two days is a trip; nine on one day are not.
        for number in range(3):
            add(self.conn, PHONE, "jun/a%d.jpg" % number, "2024-06-10T1%d:00:00" % number,
                position=FAR, place=FAR_PLACE)
        for number in range(2):
            add(self.conn, PHONE, "jun/b%d.jpg" % number, "2024-06-11T1%d:00:00" % number,
                position=FAR, place=FAR_PLACE)
        for number in range(9):
            add(self.conn, PHONE, "jul/%d.jpg" % number, "2024-07-20T1%d:00:00" % number,
                position=FAR, place=FAR_PLACE)
        found = self.found()
        self.assertIn("away-20240610", found)
        self.assertNotIn("away-20240720", found)
        self.assertNotIn("away-20240401", found)     # (two pictures, one day)
        self.assertTrue(trips.big_enough([object()] * 10, 10))

    def test_a_date_an_editor_saved_is_no_day_of_the_trip(self):
        # A picture of the trip library dated by EXIF DateTime alone (an editor saved it
        # two days after the trip): not a day of the trip, but one of its other pictures.
        saved = add(self.conn, LAKES, "edited.jpg", "2024-05-14T10:00:00",
                    exif=json.dumps({"DateTime": "2024:05:14 10:00:00", "Software": "Editor"}))
        camera = add(self.conn, LAKES, "camera.jpg", "2024-05-11T13:00:00",
                     exif=json.dumps({"DateTimeOriginal": "2024:05:11 13:00:00",
                                      "DateTime": "2024:05:14 10:00:00"}))
        lakes = self.found()[LAKES]
        self.assertEqual((lakes.start, lakes.end), ("2024-05-10", "2024-05-12"))
        self.assertIn(saved, [picture.id for picture in lakes.other])
        self.assertIn(camera, self.ids_of(lakes))
        self.assertTrue(trips.camera_dated({"DateTimeDigitized": "2024:05:11 13:00:00"}))
        self.assertFalse(trips.camera_dated({"DateTime": "2024:05:11 13:00:00"}))
        self.assertFalse(trips.camera_dated({"DateTimeOriginal": "0000:00:00 00:00:00"}))
        # The same picture with its camera's date: the trip grows to hold it.
        self.conn.execute("UPDATE images SET exif = ? WHERE id = ?", (json.dumps(
            {"DateTimeOriginal": "2024:05:14 10:00:00"}), saved))
        self.conn.commit()
        self.assertEqual(self.found()[LAKES].end, "2024-05-14")

    def test_a_picture_dated_wrongly_far_away_joins_no_trip(self):
        # A phone video located on the other side of the world, dated a day of the lake
        # trip and the day after it: not the trip's, and the trip does not grow.
        during = add(self.conn, PHONE, "may/far.mov", "2024-05-11T08:00:00",
                     source="video", position=ANTIPODES, media="video")
        after = add(self.conn, PHONE, "may/far2.mov", "2024-05-13T08:00:00",
                    source="video", position=ANTIPODES, media="video")
        lakes = self.found()[LAKES]
        self.assertNotIn(during, self.ids_of(lakes))
        self.assertNotIn(after, self.ids_of(lakes))
        self.assertEqual(lakes.end, "2024-05-12")
        # Within JOIN_FACTOR times away_km of the trip's pictures it does join.
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?",
                          (LAKE[0] + 3.0, LAKE[1], during))
        self.conn.commit()
        self.assertIn(during, self.ids_of(self.found()[LAKES]))

    def test_a_place_of_home_without_a_position_is_at_home(self):
        # On a day away, a picture with no position whose place is a town of home (a
        # place estimated without a position) stays home; one with no place joins.
        at_home = add(self.conn, PHONE, "mar/home-town.jpg", "2024-03-04T07:00:00",
                      place=HOME_PLACE)
        away = self.found()["away-20240303"]
        self.assertNotIn(at_home, self.ids_of(away))
        self.assertIn(self.ids["unlocated"], self.ids_of(away))
        self.assertEqual([entry["name"] for entry in trips.trip_places(away.pictures)],
                         ["Farville"])

    def test_pictures_from_a_plane_name_no_place(self):
        over = add(self.conn, PHONE, "mar/window.jpg", "2024-03-04T19:00:00",
                   position=(40.0, -97.0), place=OVER_PLACE,
                   exif=json.dumps({"GPS": {"GPSAltitude": 11000.5, "GPSSpeed": 230.0}}))
        away = self.found()["away-20240303"]
        self.assertIn(over, self.ids_of(away))
        self.assertEqual([entry["name"] for entry in trips.trip_places(away.pictures)],
                         ["Farville"])
        self.assertEqual(away.title, "Farville, Region One")
        for gps, flying in (({"GPSAltitude": 3000.0, "GPSSpeed": 0}, False),
                            ({"GPSAltitude": 3000.0}, True),
                            ({"GPSAltitude": [3000.0], "GPSSpeed": 120}, True),
                            ({"GPSAltitude": 6000.0, "GPSSpeed": 0}, True),
                            ({"GPSAltitude": 9000.0, "GPSAltitudeRef": [1]}, False),
                            ({"GPSAltitude": 1200.0, "GPSSpeed": 300}, False),
                            ({"GPSAltitude": "high"}, False), (None, False)):
            self.assertEqual(trips.flying(gps), flying, gps)

    def test_visitors_see_a_trip_as_its_visible_pictures_tell_it(self):
        # Pictures in another town of the same trip, the day after.
        for number in range(4):
            add(self.conn, PHONE, "mar/near-%d.jpg" % number, "2024-03-04T2%d:00:00" % number,
                position=NEAR_FAR, place=NEAR_FAR_PLACE)
        # Every picture of the town the trip is named after is hidden.
        self.conn.execute("UPDATE images SET hidden = 1 WHERE place_city = 'Farville'")
        self.conn.commit()
        self.config["trips"] = {"min_pictures": 4}
        mine = {trip["id"]: trip for trip in trips.view(
            self.conn, self.config, [PHONE, LAKES], admin=True, edits={})["trips"]}
        self.assertEqual((mine["away-20240303"]["title"], mine["away-20240303"]["start"]),
                         ("Farville, Region One", "2024-03-03"))
        theirs = {trip["id"]: trip for trip in trips.view(
            self.conn, self.config, [PHONE, LAKES], edits={})["trips"]}
        trip = theirs["away-20240303"]
        self.assertEqual((trip["title"], trip["start"], trip["end"]),
                         ("Nearton, Region One", "2024-03-04", "2024-03-04"))
        self.assertNotIn("Farville", json.dumps(trip))
        # The pictures visitors may see make no trip by themselves: no trip for them.
        self.config["trips"] = {}
        theirs = trips.view(self.conn, self.config, [PHONE, LAKES], edits={})
        self.assertNotIn("away-20240303", [trip["id"] for trip in theirs["trips"]])
        self.assertIn("away-20240303", [trip["id"] for trip in trips.view(
            self.conn, self.config, [PHONE, LAKES], admin=True, edits={})["trips"]])
        # A title the owner gave stays.
        self.config["trips"] = {"min_pictures": 4}
        edits = {"format": trips.FORMAT, "trips": {"away-20240303": {"title": "Spring"}}}
        theirs = {trip["id"]: trip for trip in trips.view(
            self.conn, self.config, [PHONE, LAKES], edits=edits)["trips"]}
        self.assertEqual(theirs["away-20240303"]["title"], "Spring")

    def test_look_units(self):
        import random
        rng = random.Random(7)
        view = unit(rng)
        moon = unit(rng)
        # A view taken again over the day; the moon twice half a minute apart, alike but
        # less so, and once more hours later.
        vectors = {"hash-a": (view, "10:30:00"), "hash-b": (turned(view, rng, 0.96), "11:30:00"),
                   "hash-c": (turned(view, rng, 0.95), "12:30:00"),
                   "hash-d": (moon, "20:30:00"), "hash-e": (turned(moon, rng, 0.91), "20:30:30"),
                   "hash-f": (turned(moon, rng, 0.91), "23:30:00")}
        ids = {}
        for number, digest in enumerate(sorted(vectors)):
            ids[digest] = add(self.conn, PHONE, "mar/look-%d.jpg" % number,
                              "2024-03-06T" + vectors[digest][1], position=FAR, place=FAR_PLACE,
                              content_hash=digest)
        store = looks.connect()
        try:
            looks.reset(store, "test-recipe", DIMS)
            looks.write(store, [(digest, looks.KIND_PHOTO, packed(vector))
                                for digest, (vector, _time) in vectors.items()])
        finally:
            store.close()
        trip = {item["id"]: item for item in trips.view(
            self.conn, self.config, [PHONE, LAKES], edits={})["trips"]}["away-20240303"]
        day = {item["date"]: item for item in trip["days"]}["2024-03-06"]
        self.assertEqual(day["looks"], [[ids["hash-a"], ids["hash-b"], ids["hash-c"]],
                                        [ids["hash-d"], ids["hash-e"]]])
        self.assertNotIn("looks", trip["days"][0])
        # No store: no units.
        os.remove(looks.store_path())
        trip = {item["id"]: item for item in trips.view(
            self.conn, self.config, [PHONE, LAKES], edits={})["trips"]}["away-20240303"]
        self.assertTrue(all("looks" not in item for item in trip["days"]))

    def test_no_home_no_away_trips(self):
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL")
        self.conn.commit()
        self.assertEqual(sorted(self.found()), [LAKES])

    def test_the_view_leaves_hidden_things_to_the_admin(self):
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.ids["away"][0],))
        self.conn.commit()
        theirs = trips.view(self.conn, self.config, [PHONE, LAKES], edits={})
        self.assertEqual([trip["id"] for trip in theirs["trips"]], [LAKES, "away-20240303"])
        away = theirs["trips"][1]
        shown = [image_id for day in away["days"] for image_id in day["ids"]]
        self.assertNotIn(self.ids["away"][0], shown)
        self.assertEqual(away["count"], len(self.ids["away"]))
        self.assertNotIn("hidden", away)
        self.assertEqual([day["date"] for day in away["days"]],
                         ["2024-03-03", "2024-03-04", "2024-03-06"])
        self.assertEqual(away["days"][0]["places"], ["Farville"])
        mine = trips.view(self.conn, self.config, [PHONE, LAKES], admin=True, edits={})
        self.assertIn(self.ids["away"][0], mine["trips"][1]["days"][0]["ids"])
        self.assertEqual(mine["trips"][1]["hidden"], False)
        lakes = theirs["trips"][0]
        self.assertEqual(lakes["libraries"], [LAKES, PHONE])
        self.assertEqual(lakes["other"], [self.ids["wrong_clock"], self.ids["file_date"]])
        self.assertEqual(theirs["best_per_day"], trips.DEFAULTS["best_per_day"])
        # Nothing there at all for visitors: no trip.
        self.conn.execute("UPDATE images SET hidden = 1 WHERE library_id = ?", (LAKES,))
        self.conn.execute("UPDATE images SET hidden = 1 WHERE path LIKE 'may/%'")
        self.conn.commit()
        theirs = trips.view(self.conn, self.config, [PHONE, LAKES], edits={})
        self.assertEqual([trip["id"] for trip in theirs["trips"]], ["away-20240303"])

    def test_hidden_pictures_still_place_the_trips(self):
        # Trips are the same for everyone: hidden pictures count when they are found.
        self.conn.execute("UPDATE images SET hidden = 1")
        self.conn.commit()
        self.assertEqual(sorted(self.found()), ["away-20240303", LAKES])

    def test_state_key_follows_the_pictures(self):
        before = trips.state_key(self.conn, [PHONE, LAKES])
        self.conn.execute("UPDATE images SET updated = updated + 1 WHERE id = ?",
                          (self.ids["unlocated"],))
        self.conn.commit()
        self.assertNotEqual(trips.state_key(self.conn, [PHONE, LAKES]), before)


class EditTest(TripData, support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config(self.libraries(self.base), trips={"min_pictures": 4})
        db.init(log=support.quiet)
        self.config = common.load_config()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.make_pictures(self.conn)
        # A day at home on the 5th: two away trips (3-4 and 6 March).
        add(self.conn, PHONE, "mar/home-5th.jpg", "2024-03-05T12:00:00", position=HOME,
            place=HOME_PLACE)
        self.libs = [PHONE, LAKES]

    def trips_now(self):
        return {trip.id: trip for trip in trips.build(self.conn, self.config, self.libs).trips}

    def test_rename_and_hide(self):
        view = trips.set_trip(self.conn, self.config, self.libs, "away-20240303",
                              title="  Spring\nbreak ")
        self.assertEqual((view["title"], view["edited"], view["auto_title"]),
                         ("Spring break", True, "Farville, Region One"))
        trips.set_trip(self.conn, self.config, self.libs, LAKES, hidden=True)
        found = self.trips_now()
        self.assertTrue(found[LAKES].hidden)
        self.assertEqual(found[LAKES].title, "Lakes")
        theirs = trips.view(self.conn, self.config, self.libs)
        self.assertNotIn(LAKES, [trip["id"] for trip in theirs["trips"]])
        trips.set_trip(self.conn, self.config, self.libs, LAKES, hidden=False)
        trips.set_trip(self.conn, self.config, self.libs, "away-20240303", title="")
        found = self.trips_now()
        self.assertFalse(found[LAKES].hidden)
        self.assertEqual(found["away-20240303"].title, "Farville, Region One")
        with open(trips.path(), encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["format"], trips.FORMAT)
        with self.assertRaises(trips.TripError) as raised:
            trips.set_trip(self.conn, self.config, self.libs, "away-19990101", title="x")
        self.assertEqual(raised.exception.status, 404)
        with self.assertRaises(trips.TripError):
            trips.set_trip(self.conn, self.config, self.libs, LAKES, hidden="yes")

    def test_merge_split_and_back(self):
        merged = trips.merge_trips(self.conn, self.config, self.libs,
                                   ["away-20240306", "away-20240303"])
        self.assertEqual(merged, "away-20240303")
        found = self.trips_now()
        self.assertNotIn("away-20240306", found)
        trip = found["away-20240303"]
        self.assertEqual((trip.start, trip.end, trip.merged),
                         ("2024-03-03", "2024-03-06", ["away-20240303", "away-20240306"]))
        # Split it again on another day: two parts, the second "<id>.YYYYMMDD".
        parts = trips.split_trip(self.conn, self.config, self.libs, "away-20240303",
                                 "2024-03-04")
        self.assertEqual(parts, ["away-20240303", "away-20240303.20240304"])
        found = self.trips_now()
        first, second = found["away-20240303"], found["away-20240303.20240304"]
        self.assertEqual((first.start, first.end, second.start, second.end),
                         ("2024-03-03", "2024-03-03", "2024-03-04", "2024-03-06"))
        self.assertEqual(second.base, "away-20240303")
        trips.set_trip(self.conn, self.config, self.libs, second.id, title="Second half")
        self.assertEqual(self.trips_now()[second.id].title, "Second half")
        # A part and another trip cannot merge; the parts can join again.
        with self.assertRaises(trips.TripError) as raised:
            trips.merge_trips(self.conn, self.config, self.libs, [second.id, LAKES])
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(trips.merge_trips(self.conn, self.config, self.libs,
                                           [second.id, first.id]), first.id)
        found = self.trips_now()
        self.assertNotIn(second.id, found)
        self.assertEqual(found[first.id].end, "2024-03-06")
        # As found: the merge goes too.
        self.assertEqual(trips.reset_trip(self.conn, self.config, self.libs, first.id),
                         ["away-20240303", "away-20240306"])
        self.assertEqual(sorted(self.trips_now()), ["away-20240303", "away-20240306", LAKES])

    def test_bad_changes(self):
        for ids in (["away-20240303"], "away-20240303", ["away-20240303", "away-20240303"],
                    ["away-20240303", 5], ["bad id!", "away-20240303"]):
            with self.assertRaises(trips.TripError):
                trips.merge_trips(self.conn, self.config, self.libs, ids)
        for date in ("2024-03-03", "2024-03-07", "March", None, "2024-02-30"):
            with self.assertRaises(trips.TripError):
                trips.split_trip(self.conn, self.config, self.libs, "away-20240303", date)

    def test_a_change_follows_a_trip_whose_first_day_moved(self):
        trips.set_trip(self.conn, self.config, self.libs, "away-20240303", title="Spring")
        trips.split_trip(self.conn, self.config, self.libs, "away-20240303", "2024-03-04")
        # A picture of the day before is found: the trip now starts on the 2nd.
        add(self.conn, PHONE, "mar/earlier.jpg", "2024-03-02T12:00:00", position=FAR,
            place=FAR_PLACE)
        found = self.trips_now()
        self.assertNotIn("away-20240303", found)
        self.assertEqual(found["away-20240302"].title, "Spring")
        self.assertIn("away-20240302.20240304", found)
        # And a new change of it is kept under the id its changes know.
        trips.set_trip(self.conn, self.config, self.libs, "away-20240302", hidden=True)
        with open(trips.path(), encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(sorted(stored["trips"]), ["away-20240303"])
        self.assertTrue(self.trips_now()["away-20240302"].hidden)

    def test_a_broken_file_is_kept_aside(self):
        common.ensure_dir(os.path.dirname(trips.path()))
        with open(trips.path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        told = io.StringIO()
        with contextlib.redirect_stdout(told):
            self.assertEqual(sorted(self.trips_now()), ["away-20240303", "away-20240306", LAKES])
        self.assertIn("cannot be read", told.getvalue())
        trips.set_trip(self.conn, self.config, self.libs, LAKES, title="Lake week")
        names = os.listdir(os.path.dirname(trips.path()))
        self.assertTrue(any(name.startswith("trips.json.broken-") for name in names), names)
        self.assertEqual(self.trips_now()[LAKES].title, "Lake week")

    def test_clean_edits_drops_what_is_not_well_formed(self):
        clean = trips.clean_edits({
            "trips": {"ok": {"title": "Fine", "span": ["2024-01-02", "2024-01-01"]},
                      "bad id": {"title": "x"}, "empty": {"title": "  "},
                      "hidden": {"hidden": True}, "x": "y"},
            "merges": [{"ids": ["a", "b"], "spans": [None]}, {"ids": ["a"]}, "z",
                       {"ids": ["a", "a"]}],
            "splits": {"t": {"dates": ["2024-01-05", "nope", "2024-13-01"]}, "u": {"dates": []}}})
        self.assertEqual(clean["trips"], {"ok": {"title": "Fine"}, "hidden": {"hidden": True}})
        self.assertEqual(clean["merges"], [{"ids": ["a", "b"], "spans": [None, None]}])
        self.assertEqual(clean["splits"], {"t": {"dates": ["2024-01-05"], "span": None}})


class PagesTest(TripData, AdminFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.write_config(self.libraries(self.base))
        db.init(log=support.quiet)
        self.make_pictures(self.conn)

    def test_pages(self):
        response = self.client.get("/trips")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        config = page_config(html)
        self.assertEqual((config["page"], config.get("trip")), ("trips", None))
        self.assertEqual(re.findall(r'<script[^>]*\ssrc="([^"]+)"', html),
                         ["/static/site.js", "/static/shell.js", "/static/search-core.js",
                          "/static/routes.js", "/static/viewer.js", "/static/map.js",
                          "/static/trips.js"])
        self.assertIn('href="/static/trips.css"', html)
        html = self.client.get("/trip/away-20240303").get_data(as_text=True)
        self.assertEqual(page_config(html)["trip"], "away-20240303")
        self.assertEqual(page_config(self.client.get("/trip/lib_x.20240101").get_data(
            as_text=True))["trip"], "lib_x.20240101")
        for bad in ("/trip/a%20b", "/trip/" + "x" * 90, "/trip/a.b"):
            self.assertEqual(self.client.get(bad).status_code, 404, bad)
        response = self.client.get("/trips", environ_base=REMOTE)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(page_config(response.get_data(as_text=True))["capabilities"]["admin"])
        self.assertNotIn("trips", page_config(html)["capabilities"])
        # Trips turned off: the pages say so (no Trips link, see SITE.tripsUrl).
        self.write_config(self.libraries(self.base), trips={"enabled": False})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIs(page_config(html)["capabilities"]["trips"], False)

    def test_api_trips(self):
        response = self.client.get("/api/trips")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([trip["id"] for trip in data["trips"]], [LAKES, "away-20240303"])
        self.assertIn("hidden", data["trips"][0])
        etag = response.headers["ETag"]
        again = self.client.get("/api/trips", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        theirs = self.client.get("/api/trips", environ_base=REMOTE)
        self.assertNotEqual(theirs.headers["ETag"], etag)
        self.assertNotIn("hidden", theirs.get_json()["trips"][0])
        # A change of the owner's: a new answer.
        self.ok(self.post("/trips/away-20240303", {"title": "Spring"}))
        changed = self.client.get("/api/trips", headers={"If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)
        titles = {trip["id"]: trip["title"] for trip in changed.get_json()["trips"]}
        self.assertEqual(titles["away-20240303"], "Spring")
        # Home is never sent.
        self.assertNotIn(str(HOME[1]), changed.get_data(as_text=True))
        self.assertNotIn("Hometown", changed.get_data(as_text=True))

    def test_admin_routes(self):
        self.ok(self.post("/trips/%s" % LAKES, {"hidden": True}))
        theirs = self.client.get("/api/trips", environ_base=REMOTE).get_json()
        self.assertEqual([trip["id"] for trip in theirs["trips"]], ["away-20240303"])
        self.ok(self.post("/trips/%s" % LAKES, {"hidden": False}))
        reply = self.ok(self.post("/trips/away-20240303/split", {"date": "2024-03-06"}))
        self.assertEqual(reply["ids"], ["away-20240303", "away-20240303.20240306"])
        reply = self.ok(self.post("/trips/merge", {"ids": ["away-20240303",
                                                           "away-20240303.20240306"]}))
        self.assertEqual(reply["id"], "away-20240303")
        reply = self.ok(self.post("/trips/merge", {"ids": [LAKES, "away-20240303"]}))
        self.assertEqual(reply["id"], "away-20240303")
        listed = self.client.get("/api/trips").get_json()["trips"]
        self.assertEqual(listed[0]["merged"], ["away-20240303", LAKES])
        reply = self.ok(self.post("/trips/away-20240303/reset", {}))
        self.assertEqual(reply["ids"], ["away-20240303", LAKES])
        self.assertEqual(self.post("/trips/away-19990101", {"title": "x"}).status_code, 404)
        self.assertEqual(self.post("/trips/away-20240303", {}).status_code, 400)
        self.assertEqual(self.post("/trips/away-20240303/split", {"date": "x"}).status_code, 400)
        self.assertEqual(self.post("/trips/merge", {"ids": "x"}).status_code, 400)
        for path, body in (("/trips/%s" % LAKES, {"title": "x"}),
                           ("/trips/merge", {"ids": [LAKES, "away-20240303"]}),
                           ("/trips/%s/split" % LAKES, {"date": "2024-05-11"}),
                           ("/trips/%s/reset" % LAKES, {})):
            self.assertEqual(self.post(path, body, environ_base=REMOTE).status_code, 403)
            response = self.client.post("/api/admin" + path, data=json.dumps(body),
                                        content_type="application/json")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.post(path, body, headers={"Sec-Fetch-Site": "cross-site"})
                             .status_code, 403)
            self.assertEqual(self.get(path).status_code, 405)
        self.assertFalse(os.path.exists(os.path.join(common.data_dir(), "trips.json.tmp")))


class ScriptTest(unittest.TestCase):

    def read(self, name):
        with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
            return handle.read()

    def test_plain_ascii_scripts_built_with_dom_calls(self):
        for name in ("trips.js", "trips.css"):
            self.assertTrue(all(ord(ch) < 128 for ch in self.read(name)), name)
        code = support.code_only(self.read("trips.js"))
        for pattern in (r"=>", r"\blet\s+[A-Za-z_$]", r"\bconst\s+[A-Za-z_$]", r"`",
                        r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML",
                        r"document\.write", r"\beval\s*\(", r"\bnew\s+Function\b"):
            self.assertIsNone(re.search(pattern, code), pattern)
        self.assertIn("var TRIPS = (function () {", self.read("trips.js"))
        self.assertIn("module.exports = TRIPS;", self.read("trips.js"))

    def test_the_map_parts_are_shared(self):
        # The trip map draws with the map page's tiles and pins, and the route lines
        # of routes.js -- one copy of each.
        trips_js = self.read("trips.js")
        for name in ("MAP.TileLayer", "MAP.drawPins", "MAP.cluster", "MAP.fitView",
                     "ROUTES.strokeLines", "ROUTES.lines"):
            self.assertIn(name, trips_js)
        self.assertNotIn("drawImage", trips_js)
        self.assertIn("TileLayer: TileLayer", self.read("map.js"))
        self.assertIn("strokeLines: strokeLines", self.read("routes.js"))

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_page_free_parts(self):
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "trips_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=120)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("passed", output)


if __name__ == "__main__":
    unittest.main()
