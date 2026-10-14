"""Estimated places (place_estimates.py): where pictures without a usable position were taken.

The rules on made-up pictures (estimate: copies, the pictures taken around
the same time on both sides or one, the file-time rule, a library's area,
the owner's choices, the place a file names in its XMP), the database pass
after the GPS check (update: the place_estimates rows, images.updated,
the log line), the search records of the admin, visitors and an export,
the owner's choices, the server's
/api/image, /api/locations and the admin's routes, the town search of the
place names, and the pages under Node (tests/js/place_estimates_test.js).
Every position, town and camera here is made up.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

import support

import common
import export
import gps_check
import imageinfo
import jobs
import place_estimates
import places
import search_index
import test_gps_check
from test_gps_check import (CAMERA, COAST, FAR, HILLS, OFFSET, PHONE, PassFixture, near, row,
                            taken)

try:
    import flask
except ImportError:
    flask = None

NODE = shutil.which("node")
REMOTE = test_gps_check.REMOTE


def estimate(rows, looks=None, choices=None, across=True, **options):
    """estimate() of rows after the GPS check (options: place_estimates settings)."""
    check = gps_check.evaluate(rows, gps_check.settings({}))
    settings = place_estimates.settings({"place_estimates": options})
    return place_estimates.estimate(rows, check, settings, across=across, looks=looks,
                                    choices=choices)


def placed(found, picture):
    """(source, lat rounded, lon rounded) of a picture's estimate, or None."""
    item = found.get(picture["id"])
    if item is None:
        return None
    if item.lat is None:
        return (item.source, None, None)
    return (item.source, round(item.lat, 5), round(item.lon, 5))


def east(position, metres):
    """A position about metres east of another (at 5 degrees north)."""
    return (position[0], round(position[1] + metres / 110800.0, 6))


# The place names the made-up towns give positions near Coralport.
NAMES = {"place_city": "Coralport", "place_region": "North Coast", "place_country": "Examplia",
         "place_cc": "XA", "place_km": 0.5}


def named(item):
    """A located picture with its place names (as the places job gives them)."""
    item.update(NAMES)
    return item


def gps_with(clock, **values):
    """A phone's fresh GPS data (a picture taken at clock on DAY) with more values
    (GPSHPositioningError in metres; GPSSpeed in km/h)."""
    gps = test_gps_check.fresh_gps(taken(clock))
    gps.update(values)
    if "GPSSpeed" in values:
        gps["GPSSpeedRef"] = "K"
    return gps


class SettingsTest(unittest.TestCase):

    def test_defaults_and_ranges(self):
        self.assertEqual(place_estimates.settings({}), place_estimates.DEFAULTS)
        self.assertEqual(place_estimates.settings(None), place_estimates.DEFAULTS)
        self.assertTrue(place_estimates.DEFAULTS["enabled"])
        self.assertEqual(place_estimates.DEFAULTS["window_minutes"], 30)
        options = place_estimates.settings({"place_estimates": {
            "enabled": False, "window_minutes": 1000, "max_spread_km": 0,
            "library_area": "no", "other": 1}})
        self.assertEqual(options, {"enabled": False, "window_minutes": 240,
                                   "max_spread_km": 0.1, "library_area": True})
        self.assertEqual(place_estimates.settings({"place_estimates": {
            "window_minutes": float("nan"), "max_spread_km": True}}),
            place_estimates.DEFAULTS)

    def test_the_gps_checks_older_settings_still_count(self):
        older = {"gps_check": {"estimate_places": False, "estimate_minutes": 45}}
        self.assertEqual(place_estimates.settings(older)["enabled"], False)
        self.assertEqual(place_estimates.settings(older)["window_minutes"], 45)
        # The new settings win.
        both = dict(older, place_estimates={"enabled": True, "window_minutes": 20})
        self.assertEqual(place_estimates.settings(both)["enabled"], True)
        self.assertEqual(place_estimates.settings(both)["window_minutes"], 20)
        # (gps_check has no such settings of its own any more.)
        self.assertNotIn("estimate_places", gps_check.settings(older))


class TimeRuleTest(unittest.TestCase):

    def test_between_two_pictures_that_agree(self):
        before = row(COAST, "14:00")
        after = row(east(COAST, 600), "14:20")
        camera = row(None, "14:05", camera=CAMERA, offset="")
        found = estimate([before, after, camera])
        item = found[camera["id"]]
        self.assertEqual(item.source, "time")
        # A quarter of the way (5 of 20 minutes).
        self.assertAlmostEqual(item.lon, COAST[1] + 150 / 110800.0, places=5)
        self.assertEqual(item.detail["ids"], [before["id"], after["id"]])
        self.assertEqual(item.detail["seconds"], [-300, 900])
        self.assertAlmostEqual(item.detail["km"], 0.6, places=2)
        # The radius: how far apart (0.6 km) and 5 km/h for 5 minutes.
        self.assertAlmostEqual(item.radius_km, 0.6 + 5 * 5 / 60.0, places=2)

    def test_two_sides_far_apart_give_nothing(self):
        # A drive: 20 km in 20 minutes; the picture could be anywhere between.
        rows = [row(COAST, "14:00"), row(HILLS, "14:20"),
                row(None, "14:10", camera=CAMERA, offset="")]
        self.assertEqual(estimate(rows), {})
        # A wider max_spread_km lets it through.
        self.assertEqual(placed(estimate(rows, max_spread_km=30), rows[2])[0], "time")

    def test_the_window(self):
        rows = [row(COAST, "14:00"), row(near(COAST, 1), "14:50"),
                row(None, "14:25", camera=CAMERA, offset="")]
        self.assertEqual(placed(estimate(rows), rows[2])[0], "time")
        self.assertEqual(estimate(rows, window_minutes=20), {})

    def test_one_side_needs_two_shots_that_stood_still(self):
        first = row(COAST, "14:00")
        second = row(near(COAST, 2), "14:03")
        camera = row(None, "14:05", camera=CAMERA, offset="")
        found = estimate([first, second, camera])
        self.assertEqual(placed(found, camera), ("time", second["lat"], second["lon"]))
        self.assertEqual(found[camera["id"]].detail["seconds"], [-300, -120])
        # (How far apart they are, at least 100 m, and walking for 2 minutes.)
        self.assertAlmostEqual(found[camera["id"]].radius_km, 0.1 + 5 * 2 / 60.0, places=3)
        # One shot alone says nothing about where the camera went next.
        self.assertEqual(estimate([second, camera]), {})
        # Copies of one shot count once.
        twin = dict(row(near(COAST, 2), "14:03"), content_hash=second["content_hash"])
        self.assertEqual(estimate([second, twin, camera]), {})
        # Two shots that do not agree: the camera was on its way.
        self.assertEqual(estimate([row(HILLS, "14:00"), second, camera]), {})

    def test_one_side_only_within_half_the_window(self):
        rows = [named(row(COAST, "14:00")), named(row(near(COAST, 1), "14:01")),
                row(None, "14:17", camera=CAMERA, offset="")]
        self.assertEqual(estimate(rows), {})            # 16 minutes after: too far
        rows[2] = row(None, "14:14", camera=CAMERA, offset="")
        self.assertEqual(placed(estimate(rows), rows[2]), ("time", None, None))

    def test_a_camera_may_have_boarded_a_vehicle(self):
        # Two shots seconds apart where a helicopter stood, and a picture ten
        # minutes later: it may be many kilometres away by then.
        rows = [named(row(COAST, "14:21:42")), named(row(near(COAST, 1), "14:21:49")),
                row(None, "14:31:21", camera=CAMERA, offset="")]
        item = estimate(rows)[rows[2]["id"]]
        self.assertEqual((item.source, item.lat, item.lon), ("time", None, None))
        self.assertEqual(item.area, ("North Coast", "Examplia", "XA", "Coralport"))
        self.assertEqual(item.detail["coarse"], 1)
        # (Walking for 2 minutes after the nearer shot, then 50 km/h.)
        moved = (5.0 * 120 + 50.0 * (572 - 120)) / 3600.0
        self.assertAlmostEqual(item.radius_km, 0.1 + moved, places=2)
        self.assertIn("so there is no position", place_estimates.reason_text("time", item.detail))
        # Without place names: nothing to say.
        bare = [row(COAST, "14:21:42"), row(near(COAST, 1), "14:21:49"), rows[2]]
        self.assertEqual(estimate(bare), {})
        # A minute later: a position, a few hundred metres off at most.
        rows[2] = row(None, "14:22:49", camera=CAMERA, offset="")
        self.assertEqual(placed(estimate(rows), rows[2]), ("time", rows[1]["lat"], rows[1]["lon"]))
        self.assertLess(estimate(rows)[rows[2]["id"]].radius_km, 0.2)
        # Further than TOWN_KM: the region only.
        rows[2] = row(None, "14:35:30", camera=CAMERA, offset="")
        item = estimate(rows)[rows[2]["id"]]
        self.assertEqual(item.area, ("North Coast", "Examplia", "XA", ""))
        self.assertGreater(item.radius_km, place_estimates.TOWN_KM)

    def test_the_speed_the_shots_state(self):
        # Shots that stood still by their positions, but whose GPS said they
        # moved at 60 km/h (a car in a queue): a minute on, 1 km away.
        speed = {"GPSSpeed": 60.0}
        rows = [named(row(COAST, "14:00:00", gps=gps_with("14:00:00", **speed))),
                named(row(near(COAST, 1), "14:00:10", gps=gps_with("14:00:10", **speed))),
                row(None, "14:01:10", camera=CAMERA, offset="")]
        item = estimate(rows)[rows[2]["id"]]
        self.assertAlmostEqual(item.radius_km, 0.1 + 60.0 * 60 / 3600.0, places=2)
        rows[2] = row(None, "14:02:40", camera=CAMERA, offset="")
        self.assertEqual(placed(estimate(rows), rows[2]), ("time", None, None))

    def test_positions_that_say_they_are_coarse(self):
        # A phone on an aeroplane with fixes 13 km off by their own account:
        # they place nothing.
        coarse = {"GPSHPositioningError": 13000.0}
        rows = [row(FAR, "14:00:35", gps=gps_with("14:00:35", **coarse)),
                row(near(FAR, 1), "14:00:41", gps=gps_with("14:00:41", **coarse)),
                row(None, "14:00:00", camera=CAMERA, offset="")]
        self.assertEqual(estimate(rows), {})
        # Fixes that may be 1.5 km off: the radius says so.
        fair = {"GPSHPositioningError": 1500.0}
        rows[:2] = [row(FAR, "14:00:35", gps=gps_with("14:00:35", **fair)),
                    row(near(FAR, 1), "14:00:41", gps=gps_with("14:00:41", **fair))]
        self.assertGreaterEqual(estimate(rows)[rows[2]["id"]].radius_km, 1.5)
        # On both sides too.
        rows = [row(COAST, "14:00", gps=gps_with("14:00", **fair)), row(near(COAST, 2), "14:20"),
                row(None, "14:05", camera=CAMERA, offset="")]
        self.assertGreaterEqual(estimate(rows)[rows[2]["id"]].radius_km, 1.5)
        rows[0] = row(COAST, "14:00", gps=gps_with("14:00", **coarse))
        self.assertEqual(estimate(rows), {})            # (one shot after it only)

    def test_shots_taken_seconds_apart_on_the_move(self):
        # From an aeroplane: two shots 3 seconds and 600 m apart are near
        # each other, but the camera was 50 km away four minutes before.
        rows = [row(COAST, "14:04:00"), row(east(COAST, 600), "14:04:03"),
                row(None, "14:00", camera=CAMERA, offset="")]
        self.assertEqual(estimate(rows), {})
        # The same shots a minute apart: walking pace.
        rows[1] = row(east(COAST, 60), "14:05:00")
        self.assertEqual(placed(estimate(rows), rows[2])[0], "time")

    def test_other_libraries_and_clocks(self):
        rows = [row(COAST, "14:00", library="lib_phone0001"),
                row(near(COAST, 1), "14:20", library="lib_phone0001"),
                row(None, "14:05", camera=CAMERA, offset="")]
        self.assertEqual(placed(estimate(rows), rows[2])[0], "time")
        self.assertEqual(estimate(rows, across=False), {})

    def test_a_time_zone_in_doubt_widens_the_time(self):
        # The library's pictures had two time zones that day (a flight):
        # a picture without its own could be 12 hours either way.
        rows = [row(COAST, "14:00"), row(near(COAST, 1), "14:20"),
                row(None, "14:30", offset="+02:00", camera="Otherphone"),
                row(None, "14:05", camera=CAMERA, offset="")]
        self.assertNotIn(rows[3]["id"], estimate(rows))
        # Its own offset: timed exactly.
        rows[3] = row(None, "14:05", camera=CAMERA, offset=OFFSET)
        self.assertIn(rows[3]["id"], estimate(rows))

    def test_dates_that_cannot_time_a_picture(self):
        rows = [row(COAST, "14:00"), row(near(COAST, 1), "14:20")]
        for extra in ({"taken_source": "mtime"}, {"media": "video", "camera": ""},
                      {"taken_source": "filename", "software": "Some App"}):
            target = row(None, "14:05", camera=CAMERA, offset="")
            target.update(extra)
            self.assertEqual(estimate(rows + [target]), {}, extra)


class CopyRuleTest(unittest.TestCase):

    def test_the_same_file_whatever_its_date(self):
        located = row(COAST, "14:00")
        copy = row(None, "10:00", camera="", offset="", day="2022-01-01",
                   content_hash=located["content_hash"], taken_source="mtime")
        found = estimate([located, copy])
        self.assertEqual(placed(found, copy), ("copy", COAST[0], COAST[1]))
        self.assertEqual(found[copy["id"]].detail["kind"], "file")
        self.assertEqual(found[copy["id"]].radius_km, place_estimates.RADIUS_MIN_KM)

    def test_the_same_camera_and_second(self):
        located = row(COAST, "14:00:07")
        other = row(None, "14:00:07", gps={})
        found = estimate([located, other])
        self.assertEqual(found[other["id"]].detail["kind"], "second")

    def test_the_same_dhash_within_two_minutes(self):
        located = row(COAST, "14:00", dhash="0123456789abcdef")
        copy = row(None, "14:01:30", camera=CAMERA, offset="", dhash="0123456789abcdef")
        found = estimate([located, copy])
        self.assertEqual(found[copy["id"]].detail["kind"], "dhash")
        later = row(None, "14:05", camera=CAMERA, offset="", dhash="0123456789abcdef")
        self.assertNotIn(later["id"], estimate([located, later]))

    def test_a_picture_that_looks_the_same(self):
        located = row(COAST, "14:00")
        copy = row(None, "14:06", camera=CAMERA, offset="")
        other = row(None, "14:07", camera=CAMERA, offset="")
        same = ((10, 20, 30, 40), 55.0)
        looks = {located["id"]: same, copy["id"]: same,
                 other["id"]: ((40, -30, 20, -10), 55.0)}
        found = estimate([located, copy, other], looks=looks)
        self.assertEqual(found[copy["id"]].detail["kind"], "look")
        self.assertEqual(found[copy["id"]].detail["seconds"], [-360])
        self.assertNotIn(other["id"], found)
        # More than COPY_LOOK_SECONDS apart: no copy.
        far = row(None, "14:11", camera=CAMERA, offset="")
        looks[far["id"]] = same
        self.assertNotIn(far["id"], estimate([located, far], looks=looks))
        # (Walking for the 6 minutes between them.)
        self.assertAlmostEqual(found[copy["id"]].radius_km, 0.1 + 5 * 6 / 60.0, places=3)

    def test_a_look_alike_from_a_moving_camera(self):
        # Shots of the clouds from an aeroplane look alike while far apart: a
        # look copy goes only as far as the camera may have gone between them.
        same = ((10, 20, 30, 40), 55.0)
        fast = {"GPSSpeed": 715.0}
        located = row(COAST, "14:00:00", gps=gps_with("14:00:00", **fast))
        later = row(None, "14:00:28", camera=CAMERA, offset="")
        looks = {located["id"]: same, later["id"]: same}
        self.assertNotIn(later["id"], estimate([located, later], looks=looks))
        sooner = row(None, "14:00:07", camera=CAMERA, offset="")
        looks[sooner["id"]] = same
        item = estimate([located, sooner], looks=looks)[sooner["id"]]
        self.assertEqual(item.detail["kind"], "look")
        self.assertAlmostEqual(item.radius_km, 0.1 + 715.0 * 7 / 3600.0, places=2)
        # Without a stated speed, the same camera's shots before and after say
        # how fast it went (600 m in 3 seconds).
        located = row(COAST, "14:00:00")
        rows = [located, row(east(COAST, 600), "14:00:03"), later]
        looks = {located["id"]: same, later["id"]: same}
        self.assertNotIn(later["id"], estimate(rows, looks=looks))
        # And a copy by dHash with a coarse fix is none.
        coarse = row(COAST, "14:00", dhash="0123456789abcdef",
                     gps=gps_with("14:00", GPSHPositioningError=13000.0))
        copy = row(None, "14:00:30", camera=CAMERA, offset="", dhash="0123456789abcdef")
        self.assertNotIn(copy["id"], estimate([coarse, copy]))


class FileTimeTest(unittest.TestCase):
    """A date from the file's time counts when the folder's files kept their times."""

    def folder(self, kept):
        rows = [row(COAST, "13:55"), row(near(COAST, 1), "14:20")]
        for clock in ("13:58", "14:02", "14:06"):
            camera = row(None, clock, camera="Examplecam 5", offset="", path="trip/c.jpg")
            written = gps_check.local_seconds(camera["taken_at"])
            camera["mtime"] = self.epoch(written if kept else written + 86400 * 3)
            rows.append(camera)
        clip = row(None, "14:04", camera="", offset="", taken_source="mtime",
                   media="video", path="trip/00012.MTS")
        clip["mtime"] = self.epoch(gps_check.local_seconds(clip["taken_at"]))
        rows.append(clip)
        return rows, clip

    @staticmethod
    def epoch(local):
        """The file time (seconds since 1970) whose local clock reads local."""
        moment = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=local)
        return time.mktime(moment.timetuple())

    def test_a_folder_that_kept_its_file_times(self):
        rows, clip = self.folder(kept=True)
        self.assertEqual(placed(estimate(rows), clip)[0], "time")
        self.assertEqual(place_estimates._file_times(rows), {clip["id"]})

    def test_a_folder_of_copies(self):
        rows, clip = self.folder(kept=False)
        self.assertNotIn(clip["id"], estimate(rows))
        self.assertEqual(place_estimates._file_times(rows), set())

    def test_one_vouching_file_is_not_enough(self):
        rows, clip = self.folder(kept=True)
        rows = [item for item in rows if item.get("path") != "trip/c.jpg"] + [
            next(item for item in rows if item.get("path") == "trip/c.jpg")]
        self.assertEqual(place_estimates._file_times(rows), set())


class AreaTest(unittest.TestCase):
    """A library whose located pictures are all in one region."""

    NAMES = NAMES

    def library(self, count=place_estimates.AREA_MIN, region="North Coast", **values):
        rows = []
        for step in range(count):
            hour = 8 + step // 6
            item = row(near(COAST, step * 30), "%02d:%02d" % (hour, step % 6 * 10), **values)
            item.update(self.NAMES, place_region=region if step % 2 else "North Coast")
            rows.append(item)
        return rows

    def test_an_undated_picture_gets_the_region(self):
        rows = self.library()
        target = row(None, "10:00", camera="", offset="", taken_source="mtime",
                     day="2021-03-09")
        found = estimate(rows + [target])
        self.assertEqual(found[target["id"]].source, "area")
        self.assertIsNone(found[target["id"]].lat)
        self.assertEqual(found[target["id"]].area, ("North Coast", "Examplia", "XA"))
        self.assertEqual(estimate(rows + [target], library_area=False), {})

    def test_only_pictures_taken_near_the_located_ones(self):
        rows = self.library()               # (taken from 08:00 to 11:10)
        times = {("2021-03-04", "13:30"): True, ("2021-03-04", "06:00"): True,
                 ("2021-03-04", "15:00"): False, ("2021-03-05", "12:00"): False,
                 ("2021-03-02", "12:00"): False}
        for (day, clock), wanted in times.items():
            target = row(None, clock, camera=CAMERA, offset=OFFSET, day=day)
            self.assertEqual(target["id"] in estimate(rows + [target]), wanted, (day, clock))

    def test_not_on_the_way_there(self):
        # A picture at the airport an hour before the trip's first located
        # picture -- and a located picture taken nearer in time, at home.
        rows = self.library()
        target = row(None, "07:00", camera=CAMERA, offset=OFFSET)
        self.assertIn(target["id"], estimate(rows + [target]))
        home = row((5.0, -147.3), "06:45", library="lib_phone0001")     # (300 km away)
        self.assertNotIn(target["id"], estimate(rows + [home, target]))
        # (Only other libraries' that count: across_libraries.)
        self.assertIn(target["id"], estimate(rows + [home, target], across=False))

    def test_a_clock_in_no_known_time_zone(self):
        # A camera without a time zone, days from any picture with one: its
        # clock may be 14 hours ahead of UTC or 12 behind, no more.
        rows = self.library()
        for day, wanted in (("2021-03-04", True), ("2021-03-14", False), ("2014-07-02", False)):
            target = row(None, "12:00", camera="Othercam 2", offset="", day=day)
            check = gps_check.evaluate(rows + [target], gps_check.settings({}))
            if day != "2021-03-04":
                self.assertTrue(check.timing(target)[3], day)       # (clock only)
            self.assertEqual(target["id"] in estimate(rows + [target]), wanted, day)
        # A library whose located pictures have no time zone either: their
        # clocks are compared as they are.
        rows = self.library(offset="", camera="Examplecam 9", gps={"GPSHPositioningError": 5.0})
        near_them = row(None, "12:00", camera="Othercam 2", offset="")
        days_later = row(None, "12:00", camera="Othercam 2", offset="", day="2021-03-09")
        found = estimate(rows + [near_them, days_later])
        self.assertIn(near_them["id"], found)
        self.assertNotIn(days_later["id"], found)

    def test_a_few_located_pictures_in_a_large_library(self):
        # 20 located pictures of one outing in a library of 400 others: the
        # undated ones may be from anywhere.
        rows = self.library()
        others = [row(None, "10:00", camera="", offset="", source="mtime", day="2019-05-%02d"
                      % (1 + number % 28)) for number in range(400)]
        target = others[0]
        self.assertNotIn(target["id"], estimate(rows + others))
        # A fifth of them: the library's area holds.
        self.assertIn(target["id"], estimate(rows + others[:80]))

    def test_a_programs_export(self):
        # Dated when it was saved (EXIF DateTime alone, a Software tag): taken
        # then or before -- days after the trip it may be one of its pictures.
        rows = self.library()
        saved = row(None, "12:00", camera="", offset=OFFSET, day="2021-03-08",
                    software="Some Editor 2.1", saved_date=True)
        before = dict(saved, id=saved["id"] + 10 ** 6, taken_at="2021-03-01T12:00:00")
        found = estimate(rows + [saved, before])
        self.assertIn(saved["id"], found)
        self.assertNotIn(before["id"], found)

    def test_a_library_that_is_not_in_one_place(self):
        target = row(None, "10:00", camera="", offset="", taken_source="mtime")
        rows = self.library()
        for item in rows[:3]:
            item.update(lat=FAR[0], lon=FAR[1])
        rows += [row(FAR, "16:%02d" % minute) for minute in range(0, 30, 10)]
        self.assertNotIn(target["id"], estimate(rows + [target]))
        # Too few located pictures.
        self.assertEqual(estimate(self.library(count=place_estimates.AREA_MIN - 1)
                                  + [target]), {})
        # Two regions: the country only.
        found = estimate(self.library(region="South Coast") + [target])
        self.assertEqual(found[target["id"]].area, ("", "Examplia", "XA"))
        # No place names yet: nothing.
        rows = self.library()
        for item in rows:
            item.update(place_country="")
        self.assertEqual(estimate(rows + [target]), {})

    def test_area_names(self):
        self.assertEqual(place_estimates.area_name(("North Coast", "Examplia", "XA")),
                         "North Coast, Examplia")
        self.assertEqual(place_estimates.area_name(("", "Examplia", "XA")), "Examplia")


class TargetsAndChoicesTest(unittest.TestCase):

    def test_which_pictures_get_an_estimate(self):
        rows = [row(COAST, "14:00"), row(near(COAST, 1), "14:20")]
        # A position the check keeps: none.
        kept = row(near(COAST, 2), "14:10", camera=CAMERA, offset="")
        # One it sets aside (6,800 km off): estimated.
        home = row(FAR, "14:05", camera=CAMERA, offset="")
        found = estimate(rows + [kept, home])
        self.assertNotIn(kept["id"], found)
        self.assertEqual(placed(found, home)[0], "time")
        self.assertEqual({item["id"] for item in place_estimates.targets(
            rows + [kept, home], gps_check.evaluate(rows + [kept, home], {
                **gps_check.DEFAULTS}))}, {home["id"]})

    def test_the_owners_choices(self):
        rows = [row(COAST, "14:00"), row(near(COAST, 1), "14:20")]
        camera = row(None, "14:05", camera=CAMERA, offset="")
        key = gps_check.content_key(camera["content_hash"], camera["id"])
        found = estimate(rows + [camera], choices={key: {"choice": "reject"}})
        self.assertEqual(found, {})
        found = estimate(rows + [camera], choices={key: {"choice": "set", "lat": FAR[0],
                                                         "lon": FAR[1]}})
        self.assertEqual(placed(found, camera), ("owner", FAR[0], FAR[1]))
        found = estimate(rows + [camera], choices={key: {
            "choice": "set", "lat": None, "lon": None, "place_region": "Midlands",
            "place_country": "Testland", "place_cc": "XB"}})
        self.assertEqual(found[camera["id"]].area, ("Midlands", "Testland", "XB"))
        # Turned off: only the owner's.
        found = estimate(rows + [camera], enabled=False)
        self.assertEqual(found, {})
        found = estimate(rows + [camera], enabled=False, choices={key: {
            "choice": "set", "lat": FAR[0], "lon": FAR[1]}})
        self.assertEqual(placed(found, camera)[0], "owner")


def xmp_place_packet(**names):
    """An XMP packet with location fields in attribute form (city=..., region=...,
    country=..., cc=...)."""
    properties = dict((key, name) for key, name in imageinfo.XMP_PLACE_FIELDS)
    return ("<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF><rdf:Description rdf:about='' %s/>"
            "</rdf:RDF></x:xmpmeta>" % " ".join(
                '%s="%s"' % (properties[key], value) for key, value in names.items())
            ).encode("utf-8")


class FilePlaceTest(unittest.TestCase):
    """The place a file names (XMP location fields): read, named, and the rule."""

    def test_the_xmp_fields(self):
        attribute = xmp_place_packet(city="Coralport", region="North Coast",
                                     country="Examplia", cc="xa")
        self.assertEqual(imageinfo.xmp_place(attribute),
                         {"city": "Coralport", "region": "North Coast", "country": "Examplia",
                          "cc": "XA"})
        element = ("<rdf:Description><photoshop:City>Port\tSaint-Élise &amp; Bay"
                   "</photoshop:City><photoshop:State>\n North  Coast </photoshop:State>"
                   "<photoshop:Country></photoshop:Country><Iptc4xmpCore:CountryCode>XAX"
                   "</Iptc4xmpCore:CountryCode></rdf:Description>").encode("utf-8")
        # Entities, control characters and spaces; an empty name and a code that is
        # not two letters are left out.
        self.assertEqual(imageinfo.xmp_place(element),
                         {"city": "Port Saint-Élise & Bay", "region": "North Coast"})
        self.assertEqual(imageinfo.xmp_place(element.decode("utf-8")),
                         imageinfo.xmp_place(element))
        long_name = imageinfo.xmp_place(xmp_place_packet(country="E" * 500))["country"]
        self.assertEqual(len(long_name), imageinfo.XMP_PLACE_MAX)
        self.assertEqual(imageinfo.xmp_place(b"<x:xmpmeta/>"), {})
        self.assertEqual(imageinfo.xmp_place(b""), {})
        self.assertEqual(imageinfo.xmp_place(None), {})

    def test_read_with_the_picture(self):
        from PIL import Image
        import imagegen
        import images
        folder = tempfile.mkdtemp(prefix="search-images-test-")
        self.addCleanup(support.remove_tree, folder)
        image = Image.new("RGB", (64, 48), (90, 140, 200))
        named = imagegen.write_bytes(folder, "named.jpg", imagegen.jpeg_with_xmp(
            image, xmp_place_packet(region="North Coast", country="Examplia", cc="XA")))
        plain = imagegen.write_bytes(folder, "plain.jpg", imagegen.jpeg_bytes(image))
        result = images.read_image(named, want_preview=False)
        self.assertEqual(result["exif"]["XMPPlace"], {"region": "North Coast",
                                                      "country": "Examplia", "cc": "XA"})
        self.assertIsNone(result["lat"])
        self.assertNotIn("XMPPlace", images.read_image(plain, want_preview=False)["exif"])

    def test_the_names(self):
        countries = {"XA": "Examplia", "XB": "Testland"}

        def place(**names):
            return place_estimates.file_place({"XMPPlace": names}, countries)

        self.assertEqual(place(city="Coralport", region="North Coast", country="Examplia",
                               cc="XA"), ("North Coast", "Examplia", "XA", "Coralport"))
        # The code names the country as the place names do; a name alone gets its code.
        self.assertEqual(place(country="Republic of Examplia", cc="XA"),
                         ("", "Examplia", "XA", ""))
        self.assertEqual(place(country="testland"), ("", "Testland", "XB", ""))
        self.assertEqual(place(cc="XB"), ("", "Testland", "XB", ""))
        # Not known to the place names: as the file says.
        self.assertEqual(place(region="Far Isles", country="Nowhereland", cc="XZ"),
                         ("Far Isles", "Nowhereland", "XZ", ""))
        self.assertEqual(place_estimates.file_place({"XMPPlace": {"country": "Examplia"}}),
                         ("", "Examplia", "", ""))
        # No country: no place.
        self.assertIsNone(place(city="Coralport", region="North Coast"))
        self.assertIsNone(place(cc="XZ"))
        self.assertIsNone(place_estimates.file_place({}))
        self.assertIsNone(place_estimates.file_place({"XMPPlace": "Examplia"}))
        self.assertIsNone(place_estimates.file_place(None))

    def test_the_rule(self):
        rows = [row(COAST, "14:00"), row(near(COAST, 1), "14:20")]
        named = row(None, "14:05", camera=CAMERA, offset="")
        # A position the GPS check sets aside (6,800 km off): estimated, not named.
        aside = row(FAR, "14:05", camera=CAMERA, offset="")
        names = ("Midlands", "Testland", "XB", "")
        files = {named["id"]: names, aside["id"]: names}
        check = gps_check.evaluate(rows + [named, aside], gps_check.settings({}))
        found = place_estimates.estimate(rows + [named, aside], check,
                                         place_estimates.settings({}), file_places=files)
        # The file's place, though the pictures around it would place it.
        self.assertEqual(placed(found, named), ("file", None, None))
        self.assertEqual(found[named["id"]].area, names)
        self.assertIsNone(found[named["id"]].radius_km)
        self.assertEqual(placed(found, aside)[0], "time")
        # Also with the estimates turned off; the owner's choice comes first.
        off = place_estimates.settings({"place_estimates": {"enabled": False}})
        self.assertEqual(placed(place_estimates.estimate(
            rows + [named], check, off, file_places=files), named), ("file", None, None))
        key = gps_check.content_key(named["content_hash"], named["id"])
        for choice, wanted in (({"choice": "reject"}, None),
                               ({"choice": "set", "lat": FAR[0], "lon": FAR[1]},
                                ("owner", FAR[0], FAR[1]))):
            found = place_estimates.estimate(rows + [named], check,
                                             place_estimates.settings({}),
                                             choices={key: choice}, file_places=files)
            self.assertEqual(placed(found, named), wanted)
        # Its record: the picture's own place (ps "f", not pe).
        self.assertEqual(place_estimates.place_record({
            "source": "file", "place_city": "", "place_region": "Midlands",
            "place_country": "Testland", "place_cc": "XB"}),
            {"rg": "Midlands", "co": "Testland", "cc": "XB", "ps": "f"})


class WordsTest(unittest.TestCase):

    def test_reasons(self):
        self.assertEqual(place_estimates.reason_text(
            "time", {"seconds": [-240, 120], "km": 0.31}),
            "estimated from pictures taken around the same time: one taken 4 minutes "
            "earlier, one 2 minutes later, 310 m apart")
        self.assertEqual(place_estimates.reason_text(
            "time", {"seconds": [-600, -420], "km": 0.02}),
            "estimated from pictures taken around the same time: 2 taken before it, the "
            "nearest 7 minutes earlier, 20 m apart")
        self.assertEqual(place_estimates.reason_text("copy", {"kind": "file"}),
                         "estimated from a copy of this picture with a position (the same "
                         "file)")
        # (The same camera model in the same second: maybe another camera.)
        self.assertEqual(place_estimates.reason_text("copy", {"kind": "second"}),
                         "estimated from a picture with a position taken in the same second "
                         "by a camera of the same model")
        self.assertEqual(place_estimates.reason_text(
            "time", {"seconds": [-600, -593], "km": 0.002, "coarse": 1}),
            "estimated from pictures taken around the same time: 2 taken before it, the "
            "nearest 10 minutes earlier, 10 m apart (it may have been taken some way off, so "
            "there is no position)")
        self.assertEqual(place_estimates.reason_text("copy", {"kind": "look",
                                                              "seconds": [65]}),
                         "estimated from a copy of this picture with a position (a picture "
                         "that looks the same taken 65 seconds later)")
        self.assertEqual(place_estimates.reason_text("area", {}, "North Coast, Examplia"),
                         "estimated from the library: nearly all of its pictures with a "
                         "position were taken in North Coast, Examplia")
        self.assertEqual(place_estimates.reason_text("owner"), "set by you")
        # A video's GPS track: its points, not pictures taken.
        self.assertEqual(place_estimates.reason_text(
            "time", {"seconds": [-240, 120], "km": 0.31, "track": "all"}),
            "estimated from the GPS track of a video recorded around the same time: one "
            "point of the track 4 minutes earlier, one 2 minutes later, 310 m apart")
        self.assertEqual(place_estimates.reason_text(
            "time", {"seconds": [0], "track": "all"}),
            "estimated from the GPS track of a video recorded around the same time: 1 point "
            "of the track before it, the nearest at the same time")
        self.assertEqual(place_estimates.reason_text(
            "time", {"seconds": [-600, -420], "km": 0.02, "track": "some"}),
            "estimated from pictures taken around the same time (and a video's GPS track): "
            "2 taken before it, the nearest 7 minutes earlier, 20 m apart")
        self.assertEqual(place_estimates.summary_text(
            {"estimated": 3, "by_source": {"time": 2, "area": 1, "owner": 1}, "changed": 4}),
            "Estimated places: 3 pictures without a position (2 from pictures taken around "
            "the same time, 1 from the library's area); 1 with a place you set; 4 changed. "
            "The admin finds them with is:place-estimated.")
        self.assertEqual(place_estimates.summary_text(
            {"estimated": 0, "by_source": {"file": 2}, "changed": 2}),
            "Estimated places: none; 2 with the place their file names; 2 changed.")

    def test_positions_typed(self):
        self.assertEqual(place_estimates.parse_position("5.05, -150.2"), (5.05, -150.2))
        self.assertEqual(place_estimates.parse_position(" -33.9 151.2 "), (-33.9, 151.2))
        for bad in ("", "Coralport", "95, 10", "0, 0", "5.0", "5,0,1", None):
            self.assertIsNone(place_estimates.parse_position(bad), bad)


class UpdateTest(PassFixture, unittest.TestCase):
    """The pass after the GPS check (gps_check.run calls place_estimates.update)."""

    def test_estimates_are_kept_apart_and_worked_out_again(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        before = self.picture(camera)["updated"]
        ctx = jobs.JobContext("t")
        summary = self.check_all(ctx)
        self.assertEqual(summary["estimates"]["by_source"], {"time": 2})
        self.assertEqual(summary["estimates"]["changed"], 2)
        rows = {row_["image_id"]: row_ for row_ in self.conn.execute(
            "SELECT * FROM place_estimates")}
        self.assertEqual(set(rows), {camera, home})
        self.assertEqual((rows[camera]["source"], rows[camera]["place_city"],
                          rows[camera]["place_country"]), ("time", "Coralport", "Examplia"))
        self.assertIsNotNone(rows[camera]["lat"])
        # The pictures' own columns say nothing of it; their records do.
        picture = self.picture(camera)
        self.assertIsNone(picture["lat"])
        self.assertEqual(picture["place_country"], "")
        self.assertGreater(picture["updated"], before)
        self.assertEqual(self.picture(home)["place_country"], "")
        lines = [line for line in test_gps_check.log_lines(ctx) if "Estimated places" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("2 pictures without a position (2 from pictures taken around the same "
                      "time)", lines[0])
        # Again: nothing changes.
        changed = set()
        summary = gps_check.run(self.conn, self.config, changed=changed)
        self.assertEqual(summary["estimates"]["changed"], 0)
        self.assertEqual(changed, set())
        self.assertEqual(self.picture(camera)["updated"], picture["updated"])
        # The Manage page's counts.
        status = places.status(self.conn)
        self.assertEqual(status["estimates"]["time"], 2)
        self.assertEqual(status["estimates"]["estimated"], 2)

    def test_a_new_located_picture_improves_an_estimate(self):
        first = self.insert(COAST, "14:00")
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        self.check_all()
        self.assertIsNone(place_estimates.for_picture(self.conn, camera))
        self.insert(near(COAST, 3), "14:20")
        changed = set()
        gps_check.run(self.conn, self.config, changed=changed)
        self.assertIn(camera, changed)
        self.assertEqual(place_estimates.for_picture(self.conn, camera)["source"], "time")
        # And when the pictures around it go, so does the estimate.
        self.conn.execute("DELETE FROM images WHERE id = ?", (first,))
        self.conn.commit()
        gps_check.run(self.conn, self.config)
        self.assertIsNone(place_estimates.for_picture(self.conn, camera))

    def test_a_copy_by_look(self):
        # (The looks of the pictures near in time come from data/image_vectors.db.)
        import looks
        import struct
        phone = self.insert(COAST, "14:00")
        camera = self.insert(None, "14:06", camera=CAMERA, offset="")
        other = self.insert(None, "18:00", camera=CAMERA, offset="")
        vector = struct.pack("16b", *range(1, 17))
        store = looks.connect(create=True)
        try:
            looks.reset(store, "test-recipe", 16)
            looks.write(store, [(self.picture(image_id)["content_hash"], looks.KIND_PHOTO, vector)
                                for image_id in (phone, camera, other)])
        finally:
            store.close()
        self.check_all()
        found = place_estimates.for_picture(self.conn, camera)
        self.assertEqual((found["source"], json.loads(found["detail"])["kind"]), ("copy", "look"))
        # Hours later the same look is no copy.
        self.assertIsNone(place_estimates.for_picture(self.conn, other))

    def test_turned_off(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        self.check_all()
        self.assertIsNotNone(place_estimates.for_picture(self.conn, camera))
        summary = self.check_all(config=dict(self.config,
                                             place_estimates={"enabled": False}))
        self.assertEqual(summary["estimates"]["estimated"], 0)
        self.assertIsNone(place_estimates.for_picture(self.conn, camera))

    def test_without_place_names(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        os.remove(places.places_db_path())
        places.release()
        self.check_all()
        found = place_estimates.for_picture(self.conn, camera)
        self.assertIsNotNone(found["lat"])
        self.assertEqual(found["place_country"], "")
        self.assertIsNone(found["place_km"])
        # Not usable as a place yet (no names), but on the map.
        self.assertFalse(search_index.picture_record(self.conn, camera).get("co"))
        self.assertEqual([item[0] for item in place_estimates.located(
            self.conn, ["lib_trips001"])], [camera])

    def test_records(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        self.check_all()
        mine = search_index.picture_record(self.conn, camera)
        self.assertEqual({key: mine.get(key) for key in ("ci", "co", "pe", "ps", "gp", "km")},
                         {"ci": "Coralport", "co": "Examplia", "pe": 1, "ps": "t",
                          "gp": None, "km": None})
        theirs = [record for record in search_index.build_records(
            self.conn, self.config, ["lib_trips001"], include_hidden=False)
            if record["i"] == camera][0]
        self.assertEqual((theirs["ci"], theirs["pe"], theirs["ps"]), ("Coralport", 1, "t"))
        # An export: only with location and estimated_places.
        for options, wanted in (({"location": True}, False),
                                ({"location": False, "estimated_places": True}, False),
                                ({"location": True, "estimated_places": True}, True)):
            record = [record for record in search_index.build_records(
                self.conn, self.config, ["lib_trips001"], for_export=True,
                export_options=options) if record["i"] == camera][0]
            self.assertEqual("co" in record and record.get("pe") == 1, wanted, options)
            if not wanted:
                self.assertFalse({"ci", "rg", "co", "cc", "pe", "ps"} & set(record))

    def test_the_owners_choices(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        located = self.insert(near(COAST, 8), "15:00")
        self.check_all()
        estimated = place_estimates.for_picture(self.conn, camera)
        self.assertGreater(estimated["radius_km"], 0)
        # Accept: the estimate becomes the owner's place, as approximate as it was.
        place_estimates.set_choice(self.conn, camera, "accept")
        self.conn.commit()
        self.check_all()
        found = place_estimates.for_picture(self.conn, camera)
        self.assertEqual((found["source"], found["place_city"]), ("owner", "Coralport"))
        self.assertEqual((found["lat"], found["lon"]), (estimated["lat"], estimated["lon"]))
        self.assertAlmostEqual(found["radius_km"], estimated["radius_km"], places=3)
        self.assertEqual(place_estimates.located(self.conn, ["lib_trips001"]),
                         [(camera, found["lat"], found["lon"], found["radius_km"], True)])
        record = search_index.picture_record(self.conn, camera)
        self.assertEqual((record.get("pe"), record["ps"], record["co"]), (None, "o", "Examplia"))
        export_record = [record for record in search_index.build_records(
            self.conn, self.config, ["lib_trips001"], for_export=True,
            export_options={"location": True}) if record["i"] == camera][0]
        self.assertEqual(export_record["ps"], "o")
        # Set: another position (a town picked from the place names: its middle,
        # TOWN_PICK_KM off).
        place_estimates.set_choice(self.conn, camera, "set", FAR[0], FAR[1])
        self.conn.commit()
        self.check_all()
        found = place_estimates.for_picture(self.conn, camera)
        self.assertEqual((found["lat"], found["place_city"]), (FAR[0], "Farhaven"))
        self.assertEqual(place_estimates.located(self.conn, ["lib_trips001"]),
                         [(camera, FAR[0], FAR[1], place_estimates.TOWN_PICK_KM, True)])
        # A position typed (not a town's own): exact.
        place_estimates.set_choice(self.conn, camera, "set", FAR[0] + 0.01, FAR[1])
        self.conn.commit()
        self.check_all()
        self.assertEqual(place_estimates.located(self.conn, ["lib_trips001"]),
                         [(camera, FAR[0] + 0.01, FAR[1], None, True)])
        place_estimates.set_choice(self.conn, camera, "set", FAR[0], FAR[1])
        self.conn.commit()
        self.check_all()
        view = place_estimates.view(self.conn, self.picture(camera), admin=True)
        self.assertEqual((view["source"], view["reason"], view["choice"]),
                         ("owner", "set by you", "set"))
        # Reject: no place at all, across scans.
        place_estimates.set_choice(self.conn, camera, "reject")
        self.conn.commit()
        self.check_all()
        self.assertIsNone(place_estimates.for_picture(self.conn, camera))
        view = place_estimates.view(self.conn, self.picture(camera), admin=True)
        self.assertEqual((view["source"], view["choice"]), ("", "reject"))
        self.assertIsNone(place_estimates.view(self.conn, self.picture(camera)))
        # Clear: estimated again.
        place_estimates.set_choice(self.conn, camera, "clear")
        self.conn.commit()
        self.check_all()
        self.assertEqual(place_estimates.for_picture(self.conn, camera)["source"], "time")
        # Choices that cannot be made.
        with self.assertRaises(place_estimates.ChoiceError) as caught:
            place_estimates.set_choice(self.conn, located, "set", FAR[0], FAR[1])
        self.assertEqual(caught.exception.status, 409)
        lone = self.insert(None, "03:00", camera=CAMERA, offset="",
                           taken_at="2019-01-01T03:00:00")
        self.check_all()
        with self.assertRaises(place_estimates.ChoiceError) as caught:
            place_estimates.set_choice(self.conn, lone, "accept")
        self.assertEqual(caught.exception.status, 409)
        for args, status in (((lone, "set", 95.0, 0.0), 400), ((lone, "maybe"), 400),
                             ((99999, "reject"), 404)):
            with self.assertRaises(place_estimates.ChoiceError) as caught:
                place_estimates.set_choice(self.conn, *args)
            self.assertEqual(caught.exception.status, status, args)

    def test_a_place_its_file_names(self):
        self.day()
        named = self.insert(None, "14:10", camera=CAMERA, offset="", exif=json.dumps(
            {"XMPPlace": {"city": "Coralport", "region": "North Coast",
                          "country": "Republic of Examplia", "cc": "XA"}}))
        undated = self.insert(None, "03:00", camera=CAMERA, offset="",
                              taken_at="2019-01-01T03:00:00", exif=json.dumps(
                                  {"XMPPlace": {"region": "Midlands", "country": "Testland"}}))
        # A position the GPS check sets aside (6,800 km off) is not replaced by the names.
        aside = self.insert(FAR, "14:03", camera=CAMERA, offset="", exif=json.dumps(
            {"GPS": {}, "XMPPlace": {"region": "Midlands", "country": "Testland"}}))
        ctx = jobs.JobContext("t")
        summary = self.check_all(ctx)
        self.assertEqual(summary["estimates"]["by_source"], {"file": 2, "time": 1})
        self.assertEqual(summary["estimates"]["estimated"], 1)
        lines = [line for line in test_gps_check.log_lines(ctx) if "Estimated places" in line]
        self.assertIn("1 picture without a position (1 from pictures taken around the same "
                      "time); 2 with the place their file names", lines[0])
        rows = {row_["image_id"]: row_ for row_ in self.conn.execute(
            "SELECT * FROM place_estimates")}
        self.assertEqual({key: rows[named][key] for key in (
            "source", "lat", "lon", "radius_km", "place_city", "place_region",
            "place_country", "place_cc", "place_km")},
            {"source": "file", "lat": None, "lon": None, "radius_km": None,
             "place_city": "Coralport", "place_region": "North Coast",
             "place_country": "Examplia", "place_cc": "XA", "place_km": None})
        self.assertEqual((rows[undated]["source"], rows[undated]["place_region"],
                          rows[undated]["place_country"], rows[undated]["place_cc"]),
                         ("file", "Midlands", "Testland", "XB"))
        self.assertEqual(rows[aside]["source"], "time")
        # The picture's own place (not estimated), without a pin.
        record = search_index.picture_record(self.conn, named)
        self.assertEqual({key: record.get(key) for key in ("ci", "rg", "co", "cc", "pe", "ps",
                                                          "gp", "km")},
                         {"ci": "Coralport", "rg": "North Coast", "co": "Examplia",
                          "cc": "XA", "pe": None, "ps": "f", "gp": None, "km": None})
        self.assertEqual([item[0] for item in place_estimates.located(
            self.conn, ["lib_trips001"])], [aside])
        view = place_estimates.view(self.conn, self.picture(named))
        self.assertEqual((view["source"], view["reason"], view["radius_km"], view["place"]),
                         ("file", "named in its file", None,
                          "Coralport, North Coast, Examplia"))
        status = places.status(self.conn)["estimates"]
        self.assertEqual((status["estimated"], status["time"], status["file"],
                          status["owner"]), (1, 1, 2, 0))
        # An export: with location (as the owner's own places), not without.
        for options, wanted in (({"location": True}, True), ({"location": False}, False)):
            record = [record for record in search_index.build_records(
                self.conn, self.config, ["lib_trips001"], for_export=True,
                export_options=options) if record["i"] == named][0]
            self.assertEqual(record.get("ps") == "f" and record.get("co") == "Examplia",
                             wanted, options)
            if not wanted:
                self.assertFalse({"ci", "rg", "co", "cc", "pe", "ps"} & set(record))
        # Nothing to accept; again: nothing changes.
        with self.assertRaises(place_estimates.ChoiceError) as caught:
            place_estimates.set_choice(self.conn, named, "accept")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.check_all()["estimates"]["changed"], 0)

    def test_hidden_pictures_are_not_on_the_map(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        self.check_all()
        self.assertEqual(len(place_estimates.located(self.conn, ["lib_trips001"])), 1)
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (camera,))
        self.conn.commit()
        self.assertEqual(place_estimates.located(self.conn, ["lib_trips001"]), [])

    def test_an_export_with_estimated_places(self):
        from PIL import Image
        ids = self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        info = {"title": "Beach", "description": "A beach.", "photo_type": "photo",
                "document_type": "none", "tags": ["beach"], "people": "none",
                "sensitive": "none", "private_info": False}
        for image_id in ids + [camera]:
            name = self.picture(image_id)["name"]
            Image.new("RGB", (40, 30), (image_id * 20 % 256, 120, 200)).save(
                os.path.join(self.folder, name), "JPEG")
            self.conn.execute("UPDATE images SET described = 1, prompt_ver = 5, info = ? "
                              "WHERE id = ?", (json.dumps(info), image_id))
        self.conn.commit()
        for estimated in (False, True):
            out = self.base.join("site-%s" % estimated)
            export.export_library(export.LogContext(test_gps_check.quiet), self.conn,
                                  self.config, "lib_trips001", out,
                                  {"location": True, "images": "none",
                                   "estimated_places": estimated})
            with open(os.path.join(out, "data", "locations.js"), encoding="ascii") as handle:
                points = json.loads(re.search(r"\[.*\]", handle.read(), re.S).group(0))
            hollow = [point for point in points if point.get("e")]
            self.assertEqual(len(points), 6 if estimated else 5)
            self.assertEqual(len(hollow), 1 if estimated else 0)
            if estimated:
                self.assertGreater(hollow[0]["r"], 0)
            with open(os.path.join(out, "data", "manifest.js"), encoding="utf-8") as handle:
                text = handle.read()
            manifest = json.loads(text[text.index("{"):text.rindex("}") + 1])
            self.assertEqual(manifest["options"].get("estimated_places", False), estimated)

    def test_export_options(self):
        options = export.validate_options({"estimated_places": True})
        self.assertTrue(options["estimated_places"])
        self.assertFalse(export.DEFAULT_OPTIONS["estimated_places"])
        with self.assertRaises(export.OptionsError):
            export.validate_options({"estimated_places": "yes"})
        # Without location it says nothing (nor is anything published).
        self.assertNotIn("estimated_places", export.options_summary(
            export.validate_options({"estimated_places": True})))
        self.assertFalse(search_index.export_settings(
            {"estimated_places": True})["estimated_places"])


class TownSearchTest(support.TempBaseMixin, unittest.TestCase):

    TOWNS = [
        (201, "Sa\u00efnt-\u00c9lan", 20.0, -56.0, "PPL", "XA", "01", 20000),
        (202, "Saint-Elan Heights", 20.1, -56.1, "PPL", "XA", "01", 100),
        (203, "Saint-Ela", 30.0, -10.0, "PPL", "XB", "02", 50000),
        (204, "Paris", 48.0, 2.0, "PPLC", "XB", "02", 2000000),
        (205, "Paris", 33.0, -95.0, "PPL", "XA", "01", 25000),
    ]

    def setUp(self):
        super().setUp()
        self.base.write_config([])
        saved = test_gps_check.TOWNS
        test_gps_check.TOWNS = self.TOWNS
        try:
            test_gps_check.install_towns()
        finally:
            test_gps_check.TOWNS = saved
        self.addCleanup(places.release)
        self.index = places.shared()

    def names(self, text, **kwargs):
        return [(town["name"], town["region"]) for town in self.index.find_towns(text, **kwargs)]

    def test_find_towns(self):
        # Accents and case do not matter; an exact name comes first, then
        # the most people.
        self.assertEqual(self.names("saint-elan"), [("Sa\u00efnt-\u00c9lan", "North Coast"),
                                                    ("Saint-Elan Heights", "North Coast")])
        self.assertEqual(self.names("SAINT-EL"), [("Saint-Ela", "Midlands"),
                                                  ("Sa\u00efnt-\u00c9lan", "North Coast"),
                                                  ("Saint-Elan Heights", "North Coast")])
        self.assertEqual(self.names("paris"), [("Paris", "Midlands"), ("Paris", "North Coast")])
        # After a comma: the region, the country or its code.
        self.assertEqual(self.names("paris, north"), [("Paris", "North Coast")])
        self.assertEqual(self.names("paris, testland"), [("Paris", "Midlands")])
        self.assertEqual(self.names("paris, xa"), [("Paris", "North Coast")])
        self.assertEqual(self.names("paris", limit=1), [("Paris", "Midlands")])
        self.assertEqual(self.names("k"), [])
        self.assertEqual(self.names("nowhere"), [])
        town = self.index.find_towns("Saint-Elan H")[0]
        self.assertEqual((town["country"], town["cc"], town["population"]),
                         ("Examplia", "XA", 100))
        self.assertAlmostEqual(town["lat"], 20.1)
        self.assertEqual(places.fold("  SA\u00cfNT-\u00c9LAN   Town "), "saint-elan town")


@unittest.skipIf(flask is None, "Flask is not installed")
class RouteTest(PassFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import admin
        import serve
        self.prefix = admin.URL_PREFIX
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def post(self, image_id, body, **kwargs):
        return self.client.post("%s/images/%d/place" % (self.prefix, image_id),
                                data=json.dumps(body), content_type="application/json",
                                headers={"X-Search-Images": "1"}, **kwargs)

    def towns(self, text, **kwargs):
        return self.client.get("%s/places/towns" % self.prefix, query_string={"q": text},
                               headers={"X-Search-Images": "1"}, **kwargs)

    def test_the_viewer_and_the_map(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        self.check_all()
        detail = self.client.get("/api/image/%d" % camera).get_json()
        self.assertEqual(detail["place"], {"city": "Coralport", "region": "North Coast",
                                           "country": "Examplia", "cc": "XA",
                                           "source": "time", "estimated": True})
        estimate = detail["place_estimate"]
        self.assertEqual(estimate["source"], "time")
        self.assertIn("taken around the same time", estimate["reason"])
        self.assertEqual(len(estimate["ids"]), 2)
        self.assertEqual(estimate["choice"], "")
        self.assertIsNone(detail["lat"])
        theirs = self.client.get("/api/image/%d" % camera, environ_base=REMOTE).get_json()
        self.assertEqual(theirs["place"]["city"], "Coralport")
        self.assertEqual(set(theirs["place_estimate"]), {"source", "reason", "radius_km",
                                                         "place"})
        points = self.client.get("/api/locations").get_json()
        hollow = [point for point in points if point.get("e")]
        self.assertEqual(len(points), 6)
        self.assertEqual([point["id"] for point in hollow], [camera])
        self.assertGreater(hollow[0]["r"], 0)
        # A located picture says nothing of it.
        located = self.client.get("/api/image/%d" % (camera - 1)).get_json()
        self.assertIsNone(located["place_estimate"])

    def test_the_owners_choices(self):
        self.day()
        camera = self.insert(None, "14:10", camera=CAMERA, offset="")
        self.check_all()
        radius = [point for point in self.client.get("/api/locations").get_json()
                  if point["id"] == camera][0]["r"]
        answer = self.post(camera, {"choice": "accept"})
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        answer = answer.get_json()
        self.assertEqual(answer["record"], {"ci": "Coralport", "rg": "North Coast",
                                            "co": "Examplia", "cc": "XA", "ps": "o"})
        self.assertEqual(answer["place"]["source"], "owner")
        self.assertNotIn("estimated", answer["place"])
        self.assertEqual(answer["place_estimate"]["choice"], "set")
        # (Accepted, as approximate as it was: its radius goes with it.)
        point = [point for point in self.client.get("/api/locations").get_json()
                 if point["id"] == camera][0]
        self.assertEqual((point.get("o"), point.get("e"), point["r"]), (1, None, radius))
        answer = self.post(camera, {"choice": "set", "lat": FAR[0], "lon": FAR[1]}).get_json()
        self.assertEqual(answer["record"]["ci"], "Farhaven")
        points = self.client.get("/api/locations").get_json()
        # (A town chosen: its middle, as far off as TOWN_PICK_KM.)
        self.assertIn({"id": camera, "lat": FAR[0], "lon": FAR[1], "o": 1,
                       "r": place_estimates.TOWN_PICK_KM}, points)
        answer = self.post(camera, {"choice": "reject"}).get_json()
        self.assertEqual(answer["record"], {})
        self.assertIsNone(answer["place"])
        self.assertEqual(answer["place_estimate"]["choice"], "reject")
        answer = self.post(camera, {"choice": "clear"}).get_json()
        self.assertEqual(answer["record"]["ps"], "t")
        # Bad requests.
        self.assertEqual(self.post(camera, {"choice": "maybe"}).status_code, 400)
        self.assertEqual(self.post(camera, {"choice": "set", "lat": "5"}).status_code, 400)
        self.assertEqual(self.post(camera, {"choice": "set", "lat": 0, "lon": 0}).status_code,
                         400)
        self.assertEqual(self.post(99999, {"choice": "reject"}).status_code, 404)
        self.assertEqual(self.post(camera - 1, {"choice": "reject"}).status_code, 409)
        self.assertEqual(self.post(camera, {"choice": "reject"},
                                   environ_base=REMOTE).status_code, 403)

    def test_a_place_without_a_position(self):
        # Two shots seconds apart, and a picture ten minutes later: the town,
        # no pin. Accepted, it stays the town.
        self.insert(COAST, "17:00:00")
        self.insert(near(COAST, 1), "17:00:07")
        camera = self.insert(None, "17:10:00", camera=CAMERA, offset="")
        self.check_all()
        answer = self.client.get("/api/image/%d" % camera).get_json()
        self.assertEqual(answer["place_estimate"]["place"], "Coralport, North Coast, Examplia")
        self.assertIsNone(answer["place_estimate"]["lat"])
        self.assertIn("so there is no position", answer["place_estimate"]["reason"])
        self.assertGreater(answer["place_estimate"]["radius_km"], 2)
        points = self.client.get("/api/locations").get_json()
        self.assertNotIn(camera, [point["id"] for point in points])
        answer = self.post(camera, {"choice": "accept"}).get_json()
        self.assertEqual(answer["record"], {"ci": "Coralport", "rg": "North Coast",
                                            "co": "Examplia", "cc": "XA", "ps": "o"})
        points = self.client.get("/api/locations").get_json()
        self.assertNotIn(camera, [point["id"] for point in points])

    def test_a_position_whose_file_names_its_place(self):
        # Taken at Coralport (Examplia), its file naming a village of Testland: the
        # names are the file's (the pin its position), without a distance; the nearest
        # town's are not even searched.
        import serve
        import trips
        names = {"city": "Little Ford", "region": "Midlands", "country": "Testland",
                 "cc": "XB"}
        picture = self.insert(COAST, "14:00", exif=json.dumps(
            {"GPS": {}, "XMPPlace": names}))
        plain = self.insert(near(COAST, 1), "14:05")
        self.check_all()
        record = search_index.picture_record(self.conn, picture)
        self.assertEqual({key: record.get(key) for key in ("ci", "rg", "co", "cc", "km", "ps",
                                                          "gp", "pe")},
                         {"ci": "Little Ford", "rg": "Midlands", "co": "Testland", "cc": "XB",
                          "km": None, "ps": "f", "gp": 1, "pe": None})
        other = search_index.picture_record(self.conn, plain)
        self.assertEqual((other["ci"], other.get("ps")), ("Coralport", None))
        # In the records of the pages and of an export with location; not without.
        records = {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, ["lib_trips001"])}
        self.assertEqual(records[picture]["co"], "Testland")
        exported = {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, ["lib_trips001"], for_export=True,
            export_options={"location": True})}
        self.assertEqual((exported[picture]["ci"], exported[picture]["ps"]),
                         ("Little Ford", "f"))
        hidden = {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, ["lib_trips001"], for_export=True)}
        self.assertFalse({"ci", "rg", "co", "cc", "ps", "km"} & set(hidden[picture]))
        # The viewer's details, and a trip's places.
        row = self.picture(picture)
        self.assertEqual(serve.place_of(row), {"city": "Little Ford", "region": "Midlands",
                                               "country": "Testland", "cc": "XB",
                                               "source": "file"})
        self.assertEqual(trips._Picture(row, None).place,
                         ("Little Ford", "Midlands", "Testland", "XB"))
        # No estimate is made for it (it has a position), and a country code alone
        # names the country as the place names do.
        self.assertIsNone(place_estimates.for_picture(self.conn, picture))
        self.conn.execute("UPDATE images SET exif = ? WHERE id = ?",
                          (json.dumps({"XMPPlace": {"cc": "XB"}}), picture))
        self.conn.commit()
        record = search_index.picture_record(self.conn, picture)
        self.assertEqual((record.get("ci"), record["co"], record["cc"]), (None, "Testland", "XB"))
        # A position the GPS check sets aside does not make way for the names.
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL WHERE id = ?", (picture,))
        self.conn.commit()
        self.assertIsNone(place_estimates.gps_file_columns(self.picture(picture)))

    def test_a_place_its_file_names(self):
        names = {"region": "North Coast", "country": "Examplia", "cc": "XA"}
        self.day()
        named = self.insert(None, "14:10", camera=CAMERA, offset="",
                            exif=json.dumps({"XMPPlace": names}))
        aside = self.insert(FAR, "14:03", camera=CAMERA, offset="",
                            exif=json.dumps({"GPS": {"GPSLatitudeRef": "N"},
                                             "XMPPlace": names}))
        self.check_all()
        detail = self.client.get("/api/image/%d" % named).get_json()
        self.assertEqual(detail["place"], {"city": "", "region": "North Coast",
                                           "country": "Examplia", "cc": "XA",
                                           "source": "file"})
        self.assertEqual(detail["place_estimate"]["reason"], "named in its file")
        self.assertNotIn(named, [point["id"] for point in
                                 self.client.get("/api/locations").get_json()])
        # The owner's place comes first; cleared, the file's again.
        answer = self.post(named, {"choice": "set", "lat": FAR[0], "lon": FAR[1]}).get_json()
        self.assertEqual((answer["record"]["ci"], answer["record"]["ps"]), ("Farhaven", "o"))
        answer = self.post(named, {"choice": "clear"}).get_json()
        self.assertEqual(answer["record"], {"rg": "North Coast", "co": "Examplia", "cc": "XA",
                                            "ps": "f"})
        self.assertEqual(self.post(named, {"choice": "accept"}).status_code, 409)
        # Visitors never see the names a set-aside position's file gives (nor its GPS).
        theirs = self.client.get("/api/image/%d" % aside, environ_base=REMOTE).get_json()
        self.assertEqual(theirs["exif"], {})
        mine = self.client.get("/api/image/%d" % aside).get_json()
        self.assertEqual(mine["exif"]["XMPPlace"], names)

    def test_towns(self):
        answer = self.towns("coral").get_json()
        self.assertTrue(answer["available"])
        self.assertEqual([town["name"] for town in answer["towns"]], ["Coralport"])
        self.assertIsNone(answer["position"])
        answer = self.towns("5.0, -150.0").get_json()
        self.assertEqual(answer["towns"], [])
        self.assertEqual(answer["position"], {"lat": 5.0, "lon": -150.0,
                                              "place": "Coralport, North Coast, Examplia"})
        self.assertEqual(self.towns("x" * 500).status_code, 200)
        self.assertEqual(self.towns("coral", environ_base=REMOTE).status_code, 403)
        os.remove(places.places_db_path())
        places.release()
        answer = self.towns("coral").get_json()
        self.assertEqual((answer["available"], answer["towns"]), (False, []))


class PaneScriptTest(unittest.TestCase):

    @unittest.skipIf(NODE is None, "Node is not installed")
    def test_the_pages(self):
        # tests/js/place_estimates_test.js: the real page scripts on a made-up DOM.
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "place_estimates_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
