"""The GPS check (gps_check.py): which positions are set aside, and what that does.

The rules on made-up pictures (evaluate), the database pass (run: the
columns cleared and given back, the owner's choices, the log line), a
scan that reads the files again, the admin's route and /api/image, the
search records and an export, and the information pane under Node
(tests/js/gps_check_test.js). Every position, town and camera here is
made up.
"""

import datetime
import json
import math
import os
import random
import re
import shutil
import subprocess
import time
import unittest
from unittest import mock

import support

import common
import db
import export
import gps_check
import jobs
import places
import search_index
import test_places

try:
    import flask
except ImportError:
    flask = None

quiet = support.quiet
NODE = shutil.which("node")
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}

# Made-up towns: two near each other, one far away (about 6,800 km). The
# pictures are taken at -10:00, near enough the time zone of the towns'
# longitudes (a time zone far from it is in doubt, see DoubtfulOffsetTest).
COAST = (5.0, -150.0)           # Coralport
HILLS = (5.1, -149.8)           # Hillmere, about 22 km from Coralport
FAR = (50.0, -100.0)            # Farhaven
TOWNS = [
    (101, "Coralport", COAST[0], COAST[1], "PPL", "XA", "01", 12000),
    (102, "Hillmere", HILLS[0], HILLS[1], "PPL", "XA", "01", 3000),
    (103, "Farhaven", FAR[0], FAR[1], "PPL", "XB", "02", 8000),
]
REGIONS = {"XA.01": "North Coast", "XB.02": "Midlands"}
COUNTRIES = {"XA": "Examplia", "XB": "Testland"}

PHONE = "Examplephone 7"
CAMERA = "Examplecam 360"
DAY = "2021-03-04"
OFFSET = "-10:00"


def install_towns():
    """data/places/places.db of the current SETTINGS_BASE from the made-up towns."""
    folder = places.places_dir()
    os.makedirs(folder, exist_ok=True)
    import zipfile
    with zipfile.ZipFile(os.path.join(folder, places.CITIES_FILE), "w") as archive:
        archive.writestr(places.CITIES_TEXT, test_places.cities_text(TOWNS))
    with open(os.path.join(folder, places.REGIONS_FILE), "w", encoding="utf-8",
              newline="\n") as handle:
        handle.write("".join("%s\t%s\t%s\t1\n" % (code, name, name)
                             for code, name in REGIONS.items()))
    lines = ["#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital"]
    for cc, name in COUNTRIES.items():
        lines.append("\t".join([cc, cc + "X", "0", cc, name, "", "0", "0", "XX", "", "", "",
                                "", "", "", "", "0", "", ""]))
    with open(os.path.join(folder, places.COUNTRIES_FILE), "w", encoding="utf-8",
              newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    places.build(ctx=jobs.JobContext("test"))
    places.release()


def near(position, step):
    """A position step * 10 m east of another (a picture taken nearby)."""
    return (position[0], round(position[1] + step * 0.0001, 6))


def taken(clock, day=DAY):
    return "%sT%s" % (day, clock if len(clock) > 5 else clock + ":00")


def fresh_gps(when, offset=OFFSET, age=0):
    """A phone's GPS data: a fix age seconds before the picture (local time when)."""
    local = datetime.datetime.fromisoformat(when)
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    utc = local - sign * datetime.timedelta(hours=hours, minutes=minutes)
    utc -= datetime.timedelta(seconds=age)
    return {"GPSDateStamp": utc.strftime("%Y:%m:%d"),
            "GPSTimeStamp": [utc.hour, utc.minute, utc.second], "GPSHPositioningError": 5.0}


_ids = iter(range(1, 10 ** 6))


def row(position, clock, camera=PHONE, offset=OFFSET, gps=None, library="lib_trips001",
        day=DAY, source="exif", **extra):
    """A picture as evaluate() gets it. A phone's GPS data is fresh unless gps says else."""
    when = taken(clock, day)
    if gps is None and camera == PHONE and position is not None:
        gps = fresh_gps(when, offset or OFFSET)
    item = {"id": next(_ids), "library_id": library, "taken_at": when, "taken_source": source,
            "tz_offset": offset, "camera": camera, "media": "image",
            "content_hash": "h%d" % random.getrandbits(48), "dhash": "",
            "lat": position[0] if position else None, "lon": position[1] if position else None,
            "gps": gps or {}, "choice": None}
    item.update(extra)
    return item


def log_lines(ctx):
    return [entry["message"] for entry in ctx.snapshot(log_tail=1000)["log_tail"]]


def check(rows, **options):
    return gps_check.evaluate(rows, gps_check.settings({"gps_check": options}))


def findings(rows, **options):
    """{id: reason kind} of the positions set aside."""
    result = check(rows, **options)
    return {image_id: (verdict.finding or {"kind": "owner"})["kind"]
            for image_id, verdict in result.verdicts.items() if verdict.ignored}


def day_with_phone(steps=(("13:50", 0), ("14:05", 1), ("14:30", 2), ("15:10", 3),
                          ("16:20", 4))):
    """The phone's pictures of a day at the coast, a few metres apart."""
    return [row(near(COAST, step), clock) for clock, step in steps]


class SettingsTest(unittest.TestCase):

    def test_defaults(self):
        self.assertEqual(gps_check.settings({}), gps_check.DEFAULTS)
        self.assertEqual(gps_check.settings(None), gps_check.DEFAULTS)
        self.assertEqual(gps_check.settings({"gps_check": "on"}), gps_check.DEFAULTS)
        self.assertTrue(gps_check.DEFAULTS["enabled"])
        # (Estimated places have settings of their own: test_place_estimates.)
        self.assertNotIn("estimate_places", gps_check.DEFAULTS)

    def test_values_are_checked_and_held_to_their_ranges(self):
        options = gps_check.settings({"gps_check": {
            "enabled": False, "max_speed": 50, "window_hours": 1000, "neighbours": 3.4,
            "stale_minutes": "10", "repeat_hours": 0.5, "across_libraries": 0,
            "estimate_places": True, "estimate_minutes": float("nan"), "other": 1}})
        self.assertEqual(options, {
            "enabled": False, "max_speed": 300, "window_hours": 72, "neighbours": 3,
            "stale_minutes": 5, "repeat_hours": 0.5, "across_libraries": True})
        self.assertEqual(gps_check.settings({"gps_check": {"enabled": "false"}})["enabled"],
                         True)
        self.assertEqual(gps_check.settings({"gps_check": {"neighbours": True}})["neighbours"],
                         4)


class FactsTest(unittest.TestCase):

    def test_fix_time(self):
        local = gps_check.local_seconds("2021-03-04T14:00:00")
        utc = gps_check.local_seconds("2021-03-05T00:00:00")
        gps = {"GPSDateStamp": "2021:03:05", "GPSTimeStamp": [0, 0, 0]}
        self.assertEqual(gps_check.fix_time(gps, local), utc)
        # Without a day: the one nearest the picture's clock.
        self.assertEqual(gps_check.fix_time({"GPSTimeStamp": [0, 0, 0]}, local), utc)
        # Hundredths of a second written as whole seconds (some older phones).
        self.assertEqual(gps_check.fix_time({"GPSTimeStamp": [23, 59, 5930]}, local),
                         utc - 0.7)
        for bad in ({}, {"GPSTimeStamp": [25, 0, 0]}, {"GPSTimeStamp": "noon"},
                    {"GPSTimeStamp": [1, 2]}, None):
            self.assertIsNone(gps_check.fix_time(bad, local))
        self.assertIsNone(gps_check.fix_time({"GPSTimeStamp": [0, 0, 0]}))

    def test_small_facts(self):
        self.assertEqual(gps_check.offset_minutes("-10:00"), -600)
        self.assertEqual(gps_check.offset_minutes("+05:45"), 345)
        self.assertIsNone(gps_check.offset_minutes(""))
        self.assertAlmostEqual(gps_check.speed_kmh({"GPSSpeed": 10, "GPSSpeedRef": "N"}), 18.52)
        self.assertEqual(gps_check.speed_kmh({"GPSSpeed": 50}), 50)
        self.assertIsNone(gps_check.speed_kmh({}))
        # iPhones write metres per second under "K".
        self.assertAlmostEqual(gps_check.speed_kmh({"GPSSpeed": 10, "GPSSpeedRef": "K"},
                                                   "Apple iPhone 99"), 36.0)
        self.assertEqual(gps_check.speed_kmh({"GPSSpeed": 10, "GPSSpeedRef": "K"}, PHONE), 10)
        self.assertEqual(gps_check.accuracy_km({"GPSHPositioningError": 2500}), 2.5)
        self.assertFalse(gps_check.checkable({"GPSLatitude": [1, 2, 3]}))
        self.assertTrue(gps_check.checkable({"GPSHPositioningError": 5}))
        self.assertTrue(gps_check.void_fix({"GPSStatus": "V"}))
        self.assertEqual(gps_check.camera_key(" Examplecam  360 "),
                         gps_check.camera_key("examplecam 360"))

    def test_reasons_in_words(self):
        self.assertEqual(gps_check.reason_text(
            {"kind": "travel", "seconds": 180, "later": True, "km": 4276.0, "count": 1}),
            "a picture taken 3 minutes later is 4,300 km away")
        self.assertEqual(gps_check.reason_text(
            {"kind": "travel", "seconds": 11, "later": False, "km": 165.2, "count": 3}),
            "3 pictures taken around the same time are far away: one taken 11 seconds "
            "earlier is 170 km away")
        self.assertEqual(gps_check.reason_text({"kind": "stale", "minutes": 84}),
                         "its GPS position was measured 84 minutes before the picture was "
                         "taken")
        self.assertIn("earlier than the picture",
                      gps_check.reason_text({"kind": "stale", "minutes": None}))
        self.assertEqual(gps_check.reason_text(
            {"kind": "frozen", "minutes": 13, "speed": 91, "count": 5}),
            "the camera gave 5 pictures taken over 13 minutes the same position while it "
            "said it was moving at 91 km/h")
        self.assertEqual(gps_check.reason_text({"kind": "stale", "minutes": 26 * 60,
                                                "least": True}),
                         "its GPS position was measured at least 26 hours before the picture "
                         "was taken")
        self.assertEqual(gps_check.reason_text({"kind": "owner"}), "you chose to ignore it")
        self.assertEqual(gps_check.reason_text({"kind": "owner", "copy": True}),
                         "you chose to ignore a copy of it")
        self.assertEqual(gps_check.duration_words(5 * 3600), "5 hours")

    def test_summary_text(self):
        summary = {"checked": 1924, "ignored": 0, "ignored_by": {}, "newly_ignored": 0,
                   "restored": 0, "estimated": 0}
        self.assertEqual(gps_check.summary_text(summary),
                         "GPS check of 1,924 pictures with a position: none ignored.")
        summary.update(ignored=3, ignored_by={"stale": 3}, newly_ignored=3)
        self.assertEqual(gps_check.summary_text(summary),
                         "GPS check of 1,924 pictures with a position: ignored 3 (3 with a GPS "
                         "fix older than the picture); 3 newly ignored. The admin finds them "
                         "with is:gps-ignored.")
        summary.update(checked=1, ignored=1, ignored_by={"travel": 1}, newly_ignored=0,
                       restored=2)
        self.assertEqual(gps_check.summary_text(summary),
                         "GPS check of 1 picture with a position: ignored 1 (1 that does not "
                         "match pictures taken around the same time); 2 used again. The admin "
                         "finds them with is:gps-ignored.")

    def test_doubtful_offsets(self):
        local = gps_check.local_seconds("2021-03-04T14:00:00")
        utc = local + 600 * 60
        # -10:00 fits a longitude of -150; +01:00 does not.
        self.assertFalse(gps_check.doubtful_offset(-600, -150.0, {}, local, None, 300))
        self.assertTrue(gps_check.doubtful_offset(60, -150.0, {}, local, None, 300))
        # A GPS time after the picture, or a GPS date of another UTC day.
        self.assertTrue(gps_check.doubtful_offset(-600, -150.0, {}, local, utc + 3600, 300))
        self.assertFalse(gps_check.doubtful_offset(-600, -150.0, {}, local, utc - 30, 300))
        self.assertTrue(gps_check.doubtful_offset(-600, -150.0, {"GPSDateStamp": "2021:03:06"},
                                                  local, None, 300))
        self.assertFalse(gps_check.doubtful_offset(-600, -150.0, {"GPSDateStamp": "2021:03:05"},
                                                   local, None, 300))
        offsets = gps_check.longitude_offsets(-150.0)
        self.assertEqual((min(offsets), max(offsets), len(offsets)), (-720, -480, 17))
        self.assertEqual(max(gps_check.longitude_offsets(179.0)), 14 * 60)


class TravelRuleTest(unittest.TestCase):

    def test_a_flight_day_is_kept(self):
        # Pictures every 15 minutes from a plane at about 800 km/h, then
        # at the far end; fresh fixes all the way.
        rows = [row(COAST, "07:40"), row(near(COAST, 3), "07:55")]
        for leg in range(16):
            minutes = 8 * 60 + leg * 15
            rows.append(row((COAST[0], round(COAST[1] + 1.8 * (leg + 1), 4)),
                            "%02d:%02d" % (minutes // 60, minutes % 60)))
        end = (COAST[0], round(COAST[1] + 1.8 * 16, 4))
        rows += [row(near(end, 1), "12:20"), row(near(end, 2), "12:40")]
        self.assertEqual(findings(rows), {})

    def test_a_cached_home_position_contradicted_by_a_phone_is_ignored(self):
        rows = day_with_phone()
        # A camera without a fix of its own: its shots near the phone, and
        # one with the position it had at home, 2 minutes before the phone's.
        rows += [row(near(COAST, 10), "14:20", camera=CAMERA, offset=""),
                 row(near(COAST, 11), "14:35", camera=CAMERA, offset="")]
        home = row(FAR, "14:03", camera=CAMERA, offset="")
        rows.append(home)
        result = check(rows)
        self.assertEqual(findings(rows), {home["id"]: "travel"})
        finding = result.verdicts[home["id"]].finding
        self.assertTrue(finding["later"])
        self.assertEqual(finding["seconds"], 120)
        self.assertGreater(finding["km"], 6000)
        self.assertEqual(finding["count"], 4)
        self.assertEqual(gps_check.reason_text(finding),
                         "4 pictures taken around the same time are far away: one taken 2 "
                         "minutes later is 6,800 km away")

    def test_repeated_coordinates_are_weak_and_go_on_one_contradiction(self):
        # The camera gave one position to shots two hours apart: its last
        # known one. A single picture of the phone contradicts it.
        phone = row(COAST, "14:05")
        first = row(FAR, "14:02", camera=CAMERA, offset="")
        again = row(FAR, "16:00", camera=CAMERA, offset="")
        result = check([phone, first, again])
        self.assertTrue(result.verdicts[first["id"]].weak)
        self.assertEqual(findings([phone, first, again]),
                         {first["id"]: "travel", again["id"]: "travel"})
        # Once only, it is no more than one opinion against another: kept.
        lone = row(FAR, "14:02", camera=CAMERA, offset="")
        self.assertEqual(findings([row(COAST, "14:05"), lone]), {})
        # Nor does a phone's repeated position (it says when it measured it).
        rows = [row(COAST, "10:00"), row(COAST, "13:00")]
        self.assertFalse(any(verdict.weak for verdict in check(rows).verdicts.values()))

    def test_weak_positions_never_vote(self):
        rows = day_with_phone()
        stuck = [row(FAR, clock, camera=CAMERA, offset="") for clock in ("13:40", "14:10",
                                                                        "15:30", "16:00")]
        result = check(rows + stuck)
        for item in stuck:
            self.assertTrue(result.verdicts[item["id"]].ignored)
        for item in rows:
            self.assertFalse(result.verdicts[item["id"]].ignored)

    def test_no_neighbours_keeps_the_position(self):
        rows = [row(FAR, "14:00", camera=CAMERA, offset=""),
                row(COAST, "14:00", day="2021-03-09")]
        self.assertEqual(findings(rows), {})
        # Neighbours further away in time than the window do not count.
        rows = day_with_phone() + [row(FAR, "19:30", camera=CAMERA, offset="")]
        self.assertEqual(findings(rows, window_hours=3), {})
        self.assertEqual(len(findings(rows)), 1)

    def test_unknown_offsets_give_slack(self):
        # The camera has no time zone; the library's pictures were taken at
        # -10:00 and -08:00 that day, so its clock may be either: two hours
        # are enough for 1,500 km.
        far_enough = (COAST[0] + 13.5, COAST[1])
        library = [row(None, "09:00", offset="-08:00", camera="Other camera"),
                   row(None, "18:00", offset="-10:00", camera="Other camera")]
        phone = [row(COAST, "14:05"), row(near(COAST, 1), "14:10")]
        camera = row(far_enough, "14:00", camera=CAMERA, offset="")
        self.assertEqual(findings(library + phone + [camera]), {})
        # With only -10:00 known, it is 5 minutes from the phone's pictures.
        library = [row(None, "09:00", offset="-10:00", camera="Other camera")]
        self.assertEqual(findings(library + phone + [camera]), {camera["id"]: "travel"})
        # With no time zone anywhere, the clocks are compared as they are.
        rows = [row(COAST, "14:05", camera="Old camera", offset="", gps={}),
                row(near(COAST, 1), "14:10", camera="Old camera", offset="", gps={}),
                row(far_enough, "14:00", camera=CAMERA, offset="")]
        self.assertEqual(findings(rows), {rows[2]["id"]: "travel"})

    def test_copies_of_one_shot_vote_once(self):
        agree = row(near(COAST, 1), "14:10")
        item = row(COAST, "14:00", camera=CAMERA, offset="")
        wrong = row(FAR, "14:20", camera="Third camera", offset=OFFSET, gps={})
        copies = [dict(wrong, id=next(_ids), library_id=library)
                  for library in ("lib_trips001", "lib_other01")]
        resaved = dict(wrong, id=next(_ids), content_hash="other", dhash="00ff00ff00ff00ff",
                       camera="", taken_at=taken("14:21"))
        wrong_dhash = dict(wrong, dhash="00ff00ff00ff00ff")
        rows = [agree, item, wrong_dhash, resaved] + copies
        # For the picture: one for, one against (the copies count once), no
        # majority. (The far shot itself has both against it.)
        far = {wrong_dhash["id"], resaved["id"]} | {copy["id"] for copy in copies}
        self.assertEqual(set(findings(rows)), far)
        # Pictures at exactly the picture's own position are the same fix:
        # no second opinion either.
        same = row(COAST, "14:12", camera="Third camera", offset=OFFSET, gps={})
        self.assertNotIn(item["id"], findings([item, wrong, same]))
        self.assertIn(item["id"], findings([item, wrong, row(near(COAST, 1), "14:12",
                                                             camera="Third camera",
                                                             offset=OFFSET, gps={}),
                                            row(FAR, "14:30", camera="Fourth camera",
                                                offset=OFFSET, gps={})]))

    def test_libraries_vouch_for_each_other_unless_told_not_to(self):
        rows = [dict(item, library_id="lib_phone001") for item in day_with_phone()]
        home = row(FAR, "14:03", camera=CAMERA, offset="")
        self.assertEqual(findings(rows + [home]), {home["id"]: "travel"})
        self.assertEqual(findings(rows + [home], across_libraries=False), {})

    def test_owner_choices(self):
        rows = day_with_phone()
        home = row(FAR, "14:03", camera=CAMERA, offset="", choice="use")
        result = check(rows + [home])
        self.assertFalse(result.verdicts[home["id"]].ignored)
        self.assertEqual(result.verdicts[home["id"]].finding["kind"], "travel")
        wrong = row(near(COAST, 7), "14:15", camera=CAMERA, offset="", choice="ignore")
        result = check(rows + [wrong])
        self.assertTrue(result.verdicts[wrong["id"]].ignored)
        self.assertIsNone(result.verdicts[wrong["id"]].finding)
        # Turned off, the check finds nothing; the owner's choice still counts.
        self.assertEqual(findings(rows + [wrong, row(FAR, "14:03", camera=CAMERA, offset="")],
                                  enabled=False), {wrong["id"]: "owner"})

    def test_the_clocks_and_references_for_estimates(self):
        # (The estimates themselves: test_place_estimates.)
        rows = day_with_phone()
        home = row(FAR, "14:03", camera=CAMERA, offset="")
        lost = row(None, "14:10", camera=CAMERA, offset="")
        result = check(rows + [home, lost])
        references = {reference.id for reference in result.references()}
        self.assertEqual(references, {item["id"] for item in rows})
        # A picture without a position is timed by the library's offsets.
        lo, hi, mid, clock_only = result.timing(lost)
        self.assertEqual((lo, hi, clock_only), (mid, mid, False))
        self.assertEqual(mid, gps_check.local_seconds(taken("14:10")) + 600 * 60)
        self.assertIsNone(result.timing(dict(lost, taken_source="mtime")))
        self.assertIsNotNone(result.timing(dict(lost, taken_source="mtime"), trusted=True))
        self.assertIsNone(result.group_of(lost["id"]))
        self.assertIsNotNone(result.group_of(home["id"]))


class IntrinsicRuleTest(unittest.TestCase):

    def test_a_stale_fix_is_ignored(self):
        rows = day_with_phone()
        old = row(COAST, "14:40", gps=fresh_gps(taken("14:40"), age=40 * 60))
        result = check(rows + [old])
        self.assertEqual(result.verdicts[old["id"]].finding, {"kind": "stale", "minutes": 40})
        # Under stale_minutes: kept.
        recent = row(COAST, "14:40", gps=fresh_gps(taken("14:40"), age=3 * 60))
        self.assertEqual(findings(rows + [recent]), {})

    def test_a_stale_fix_without_a_time_zone(self):
        def phone(clock, age=0, **extra):
            return row(COAST, clock, offset="", gps=fresh_gps(taken(clock), age=age), **extra)
        rows = [phone("14:00"), phone("14:30"), phone("15:00")]
        # 2 hours 7 minutes old: no whole quarter hour from the clock; the
        # same camera's time zone (from its fresh fixes) tells the age.
        old = phone("15:20", age=127 * 60)
        self.assertEqual(check(rows + [old]).verdicts[old["id"]].finding,
                         {"kind": "stale", "minutes": 127})
        # Without other fixes of that camera within the hour, the library's
        # time zones within 36 hours (here the GPS times') tell it.
        lone = phone("19:20", age=127 * 60)
        self.assertEqual(check(rows + [lone]).verdicts[lone["id"]].finding,
                         {"kind": "stale", "minutes": 127})
        # With two time zones that day, every one must make it stale; the
        # youngest age is told ("at least").
        other = row(None, "09:00", offset="-09:00", camera="Other camera")
        self.assertEqual(check(rows + [other, lone]).verdicts[lone["id"]].finding,
                         {"kind": "stale", "minutes": 67, "least": True})
        # With no time zone anywhere, only a fix older than any offset allows.
        def old_phone(clock, age, day=DAY):
            return row(COAST, clock, offset="", day=day,
                       gps=fresh_gps(taken(clock, day), age=age))
        far_days = [old_phone("10:00", 0, "2021-02-%02d" % day) for day in (1, 2, 3)]
        young = old_phone("12:00", 20 * 60)
        ancient = old_phone("12:00", 30 * 3600)
        result = check(far_days + [young, ancient])
        self.assertIsNone(result.verdicts[young["id"]].finding)
        self.assertEqual(result.verdicts[ancient["id"]].finding,
                         {"kind": "stale", "minutes": 360, "least": True})

    def test_a_fix_a_few_minutes_old_without_a_time_zone_is_kept(self):
        # GPS times a few minutes off a whole quarter hour, either way, could
        # be fresh fixes in some time zone: kept (with -07:00 too).
        def fix(clock, age, offset=""):
            return row(COAST, clock, offset=offset,
                       gps=fresh_gps(taken(clock), offset="-07:00", age=age))
        rows = [fix("09:00", 0), fix("10:00", 0), fix("11:00", 0)]
        for age in (-120, 90, 180, 290):
            for offset in ("", "-07:00"):
                item = fix("12:00", age, offset)
                self.assertEqual(findings(rows + [item]), {}, (age, offset))

    def test_a_camera_whose_clock_is_off_keeps_its_positions(self):
        # Every GPS time is 3 minutes before the camera's clock: the clock
        # is off, not the fixes.
        rows = [row(near(COAST, step), clock, offset="",
                    gps=fresh_gps(taken(clock), age=180))
                for step, clock in enumerate(("10:00", "11:00", "12:00", "13:00"))]
        self.assertEqual(findings(rows), {})

    def test_a_frozen_fix_is_ignored(self):
        def moving(clock):
            gps = dict(fresh_gps(taken(clock)), GPSSpeed=91.0, GPSSpeedRef="K")
            return row((COAST[0], COAST[1] + 0.5), clock, gps=gps)
        frozen = [moving("18:48"), moving("18:50"), moving("19:01")]
        result = check(day_with_phone() + frozen)
        # The first shot may have been near the fix; the later ones cannot.
        self.assertIsNone(result.verdicts[frozen[0]["id"]].finding)
        for item in frozen[1:]:
            self.assertEqual(result.verdicts[item["id"]].finding,
                             {"kind": "frozen", "minutes": 13, "speed": 91, "count": 3})
        # A burst seconds apart is no frozen fix, at any speed.
        burst = [moving("18:48:00"), moving("18:48:20")]
        self.assertEqual(findings(day_with_phone() + burst), {})
        # An iPhone's speed is in metres per second: 25 is 90 km/h.
        def iphone(clock):
            gps = dict(fresh_gps(taken(clock)), GPSSpeed=25.0, GPSSpeedRef="K")
            return row((COAST[0], COAST[1] + 0.5), clock, camera="Apple iPhone 99", gps=gps)
        frozen = [iphone("18:48"), iphone("19:01")]
        result = check(day_with_phone() + frozen)
        self.assertEqual(result.verdicts[frozen[1]["id"]].finding["speed"], 90)
        # Standing still at one spot is no frozen fix.
        still = [row(COAST, clock, gps=dict(fresh_gps(taken(clock)), GPSSpeed=0))
                 for clock in ("10:00", "10:30")]
        self.assertEqual(findings(still), {})

    def test_a_void_fix_is_ignored(self):
        item = row(COAST, "10:00", camera=CAMERA, offset="", gps={"GPSStatus": "V"})
        self.assertEqual(findings([item]), {item["id"]: "void"})

    def test_untrusted_dates_are_left_alone(self):
        # A date from the file's time, or a video without a camera (a copy a
        # program made, dated when it was made), times nothing.
        rows = day_with_phone()
        rows += [row(FAR, "14:03", camera=CAMERA, offset="", source="mtime"),
                 row(FAR, "14:04", camera="", offset="", media="video")]
        self.assertEqual(findings(rows), {})


class PassFixture(support.TempBaseMixin):
    """A library in a temporary SETTINGS_BASE, with the made-up towns."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips001")])
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(places.release)
        install_towns()

    def insert(self, position, clock, camera=PHONE, offset=OFFSET, gps=None, **extra):
        item = row(position, clock, camera=camera, offset=offset, gps=gps)
        exif = {"GPS": item["gps"]} if position else {}
        values = {"library_id": "lib_trips001", "path": "p%d.jpg" % item["id"],
                  "name": "p%d.jpg" % item["id"], "size": 100, "mtime": 0, "ctime": 0,
                  "taken_at": item["taken_at"], "taken_source": "exif",
                  "tz_offset": offset, "camera": camera, "lat": item["lat"],
                  "lon": item["lon"], "exif": json.dumps(exif),
                  "content_hash": item["content_hash"], "added": 0, "updated": 0}
        values.update(places.place_columns(item["lat"], item["lon"]))
        values.update(extra)
        columns = sorted(values)
        image_id = self.conn.execute(
            "INSERT INTO images (%s) VALUES (%s)" % (", ".join(columns),
                                                     ", ".join("?" * len(columns))),
            [values[column] for column in columns]).lastrowid
        self.conn.commit()
        return image_id

    def day(self):
        return [self.insert(near(COAST, step), clock) for step, clock in enumerate(
            ("13:50", "14:05", "14:30", "15:10", "16:20"))]

    def picture(self, image_id):
        return self.conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()

    def check_all(self, ctx=None, config=None):
        return gps_check.run(self.conn, config or self.config, ctx)


class PassTest(PassFixture, unittest.TestCase):

    def test_the_rows_say_which_dates_are_an_editors(self):
        saved = self.insert(None, "15:00", camera="", offset="", exif=json.dumps(
            {"DateTime": "2021:03:04 15:00:00", "Software": "Some Editor 2.1"}))
        shot = self.insert(None, "15:00", camera="", offset="", exif=json.dumps(
            {"DateTime": "2021:03:04 15:00:00", "DateTimeOriginal": "2021:03:04 14:00:00",
             "Software": "Some Editor 2.1"}))
        rows = {item["id"]: item for item in gps_check._load(self.conn, ["lib_trips001"])}
        self.assertTrue(rows[saved]["saved_date"])
        self.assertEqual(rows[saved]["software"], "Some Editor 2.1")
        self.assertTrue(gps_check.export_date(rows[saved]))
        # (A picture without a position that has its capture date: its EXIF is
        # not even read.)
        self.assertFalse(rows[shot]["saved_date"])
        self.assertIsNone(rows[shot]["exif"])

    def test_an_ignored_position_is_cleared_and_kept_aside(self):
        phone = self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.assertEqual(self.picture(home)["place_city"], "Farhaven")
        ctx = jobs.JobContext("t")
        summary = self.check_all(ctx)
        self.assertEqual(summary["ignored"], 1)
        self.assertEqual(summary["newly_ignored"], 1)
        self.assertEqual(summary["ignored_by"], {"travel": 1})
        picture = self.picture(home)
        self.assertIsNone(picture["lat"])
        self.assertIsNone(picture["lon"])
        self.assertEqual((picture["place_city"], picture["place_country"]), ("", ""))
        self.assertIsNone(picture["place_km"])
        # The file's own GPS data stays in exif; the position in gps_checks.
        self.assertIn("GPS", json.loads(picture["exif"]))
        kept = self.conn.execute("SELECT lat, lon, ignored, reason FROM gps_checks").fetchall()
        self.assertEqual([tuple(item)[:3] for item in kept], [(FAR[0], FAR[1], 1)])
        self.assertEqual(json.loads(kept[0]["reason"])["kind"], "travel")
        self.assertEqual(gps_check.ignored_ids(self.conn), {home})
        self.assertTrue(gps_check.is_ignored(self.conn, home))
        self.assertFalse(gps_check.is_ignored(self.conn, phone[0]))
        # One line in the log.
        lines = [entry for entry in log_lines(ctx) if "GPS check" in entry]
        self.assertEqual(len(lines), 1)
        self.assertIn("ignored 1 (1 that does not match pictures taken around the same time)",
                      lines[0])
        self.assertIn("is:gps-ignored", lines[0])
        # Again: nothing changes.
        before = self.picture(home)["updated"]
        summary = self.check_all()
        self.assertEqual((summary["ignored"], summary["newly_ignored"], summary["restored"]),
                         (1, 0, 0))
        self.assertEqual(self.picture(home)["updated"], before)
        # The place names' count.
        status = places.status(self.conn)
        self.assertEqual(status["gps_ignored"], 1)
        self.assertEqual(status["pictures"]["located"], 5)

    def test_a_decision_changes_when_the_neighbours_do(self):
        phone = self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.check_all()
        self.assertIsNone(self.picture(home)["lat"])
        self.conn.execute("DELETE FROM images WHERE id IN (%s)" % ",".join(map(str, phone)))
        self.conn.commit()
        summary = self.check_all()
        self.assertEqual(summary["restored"], 1)
        picture = self.picture(home)
        self.assertEqual((picture["lat"], picture["lon"]), FAR)
        self.assertEqual(picture["place_city"], "Farhaven")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gps_checks").fetchone()[0], 0)

    def test_the_owners_choice_both_ways(self):
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        near_ = self.insert(near(COAST, 9), "14:15", camera=CAMERA, offset="")
        self.check_all()
        # Use it anyway: kept by content, and the position comes back.
        self.assertEqual(gps_check.set_choice(self.conn, self.config, home, True), [home])
        self.conn.commit()
        self.check_all()
        picture = self.picture(home)
        self.assertEqual((picture["lat"], picture["place_city"]), (FAR[0], "Farhaven"))
        view = gps_check.picture_view(self.conn, picture)
        self.assertEqual((view["ignored"], view["choice"]), (False, "use"))
        self.assertIn("6,800 km away", view["reason"])
        # Ignore a position the check keeps.
        gps_check.set_choice(self.conn, self.config, near_, False)
        self.conn.commit()
        summary = self.check_all()
        self.assertEqual(summary["ignored_by"], {"owner": 1})
        view = gps_check.picture_view(self.conn, self.picture(near_))
        # (Its place is estimated from the phone's pictures around it.)
        self.assertEqual(view, {"ignored": True, "reason": "you chose to ignore it",
                                "choice": "ignore", "place": "Coralport, North Coast, Examplia",
                                "estimated": True})
        # Choosing what the check decides anyway removes the choice.
        gps_check.set_choice(self.conn, self.config, near_, True)
        gps_check.set_choice(self.conn, self.config, home, False)
        self.conn.commit()
        self.assertEqual(gps_check.choices(self.conn), {})
        self.check_all()
        self.assertIsNotNone(self.picture(near_)["lat"])
        self.assertIsNone(self.picture(home)["lat"])
        self.assertEqual(gps_check.set_choice(self.conn, self.config, 999, True), [])

    def test_turned_off_gives_the_positions_back(self):
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.check_all()
        self.assertIsNone(self.picture(home)["lat"])
        summary = self.check_all(config=dict(self.config, gps_check={"enabled": False}))
        self.assertEqual((summary["ignored"], summary["restored"]), (0, 1))
        self.assertEqual(self.picture(home)["place_city"], "Farhaven")

    def test_estimated_places(self):
        # A position set aside: the place estimated from the pictures around it
        # is kept apart (place_estimates.py), never in the picture's columns.
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        summary = self.check_all()
        self.assertEqual(summary["estimates"]["by_source"], {"time": 1})
        picture = self.picture(home)
        self.assertIsNone(picture["lat"])
        self.assertEqual((picture["place_city"], picture["place_country"]), ("", ""))
        record = search_index.picture_record(self.conn, home)
        self.assertEqual((record["ci"], record["pe"], record["ps"], record["gx"]),
                         ("Coralport", 1, "t", 1))
        self.assertNotIn("gp", record)
        self.assertNotIn("km", record)
        self.assertTrue(gps_check.picture_view(self.conn, picture)["estimated"])
        # The older setting turns them off: the estimate goes.
        summary = self.check_all(config=dict(self.config,
                                             gps_check={"estimate_places": False}))
        self.assertIsNone(search_index.picture_record(self.conn, home).get("ci"))
        self.assertFalse(gps_check.picture_view(self.conn, self.picture(home))["estimated"])

    def test_a_file_read_again_is_checked_again(self):
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.check_all()
        # The scanner wrote the position of the file again (it changed).
        moved = (FAR[0] + 1, FAR[1])
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?", moved + (home,))
        self.conn.commit()
        self.assertIsNone(gps_check.picture_view(self.conn, self.picture(home))["reason"]
                          or None)
        self.check_all()
        kept = self.conn.execute("SELECT lat, lon FROM gps_checks WHERE image_id = ?",
                                 (home,)).fetchone()
        self.assertEqual(tuple(kept), moved)
        self.assertIsNone(self.picture(home)["lat"])

    def test_admin_records_and_the_search(self):
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.check_all()
        admin_records = search_index.build_records(self.conn, self.config, ["lib_trips001"])
        visitor_records = search_index.build_records(self.conn, self.config, ["lib_trips001"],
                                                     include_hidden=False)
        (mine,) = [record for record in admin_records if record["i"] == home]
        (theirs,) = [record for record in visitor_records if record["i"] == home]
        self.assertEqual(mine.get("gx"), 1)
        for record in (mine, theirs):
            # Not the position set aside, nor its place: only the place
            # estimated from the pictures around it, marked as such.
            self.assertFalse({"gp", "km"} & set(record), record)
            self.assertEqual((record["co"], record["pe"]), ("Examplia", 1))
        self.assertNotIn("gx", theirs)
        self.assertEqual(sum(1 for record in admin_records if record.get("gx")), 1)


class SchemaTest(support.TempBaseMixin, unittest.TestCase):

    def test_a_version_6_database_made_before_the_check_gets_its_tables(self):
        folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", folder, id="lib_trips001")])
        db.init(log=quiet)
        conn = db.connect()
        try:
            conn.execute("DROP TABLE gps_checks")
            conn.execute("DROP TABLE gps_choices")
            conn.commit()
        finally:
            conn.close()
        db.init(log=quiet)
        conn = db.connect()
        try:
            self.assertEqual(db.user_version(conn), db.SCHEMA_VERSION)
            self.assertEqual(gps_check.ignored_ids(conn), set())
            self.assertEqual(gps_check.choices(conn), {})
        finally:
            conn.close()


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class ScanTest(support.TempBaseMixin, unittest.TestCase):
    """Real files: the scan checks the positions, and a choice survives a rescan."""

    def setUp(self):
        super().setUp()
        import imagegen
        self.imagegen = imagegen
        self.folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips001")])
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        self.addCleanup(places.release)
        install_towns()

    def jpeg(self, name, clock, position, phone=True, seed=0):
        from PIL import Image
        rng = random.Random(seed)
        image = Image.new("RGB", (64, 48))
        image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                       for _ in range(64 * 48)])
        gps_tags = {}
        if phone:
            utc = datetime.datetime.fromisoformat(taken(clock)) + datetime.timedelta(hours=10)
            gps_tags = {7: (self.imagegen.IFDRational(utc.hour, 1),
                            self.imagegen.IFDRational(utc.minute, 1),
                            self.imagegen.IFDRational(0, 1)),
                        29: utc.strftime("%Y:%m:%d")}
        return self.imagegen.jpeg_with_exif(
            self.folder, name, image=image, date=taken(clock).replace("-", ":").replace(
                "T", " "), offset=OFFSET if phone else None,
            make="Examplephone" if phone else "Examplecam", model="7" if phone else "360",
            gps=position, gps_tags=gps_tags)

    def scan(self):
        import scan
        ctx = jobs.JobContext("t")
        result = scan.run(self.config, ctx, describe_images=False)
        return result, ctx

    def rows(self):
        conn = db.connect()
        try:
            return {row["name"]: row for row in conn.execute("SELECT * FROM images")}
        finally:
            conn.close()

    def test_scan_checks_and_a_choice_survives_a_rescan(self):
        for number, clock in enumerate(("13:50", "14:05", "14:30", "15:10")):
            self.jpeg("phone%d.jpg" % number, clock, near(COAST, number), seed=number)
        home = self.jpeg("cam-home.jpg", "14:03", FAR, phone=False, seed=9)
        result, ctx = self.scan()
        self.assertEqual(result["gps_check"]["ignored"], 1)
        self.assertTrue(any("ignored 1 (1 that does not match" in line
                            for line in log_lines(ctx)), log_lines(ctx))
        rows = self.rows()
        self.assertIsNone(rows["cam-home.jpg"]["lat"])
        self.assertEqual(rows["phone1.jpg"]["place_city"], "Coralport")
        image_id = rows["cam-home.jpg"]["id"]
        conn = db.connect()
        try:
            gps_check.set_choice(conn, self.config, image_id, True)
            conn.commit()
        finally:
            conn.close()
        # The file is touched (read again by the next scan) and renamed: the
        # choice goes with its content.
        stat = os.stat(home)
        os.utime(home, (stat.st_atime, stat.st_mtime + 60))
        os.replace(home, os.path.join(self.folder, "renamed.jpg"))
        result, _ctx = self.scan()
        rows = self.rows()
        self.assertNotIn("cam-home.jpg", rows)
        self.assertEqual(rows["renamed.jpg"]["place_city"], "Farhaven")
        self.assertEqual(result["gps_check"]["ignored"], 0)
        # And the other way: ignore a phone picture, then scan again.
        conn = db.connect()
        try:
            gps_check.set_choice(conn, self.config, rows["phone2.jpg"]["id"], False)
            conn.commit()
        finally:
            conn.close()
        result, _ctx = self.scan()
        self.assertEqual(result["gps_check"]["ignored_by"], {"owner": 1})
        self.assertIsNone(self.rows()["phone2.jpg"]["lat"])


class ExportTest(PassFixture, unittest.TestCase):

    def test_an_export_never_holds_an_ignored_position(self):
        self.export_and_look(checked=True)

    def test_an_export_with_locations_checks_first(self):
        # (A scan that read the far position was stopped before checking it.)
        self.export_and_look(checked=False)

    def export_and_look(self, checked):
        from PIL import Image
        ids = self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        if checked:
            self.check_all()
        info = {"title": "Beach", "description": "A beach.", "photo_type": "photo",
                "document_type": "none", "tags": ["beach"], "people": "none",
                "sensitive": "none", "private_info": False}
        for image_id in ids + [home]:
            name = self.picture(image_id)["name"]
            Image.new("RGB", (40, 30), (image_id * 20 % 256, 120, 200)).save(
                os.path.join(self.folder, name), "JPEG")
            self.conn.execute("UPDATE images SET described = 1, prompt_ver = 5, info = ? "
                              "WHERE id = ?", (json.dumps(info), image_id))
        self.conn.commit()
        out = self.base.join("site")
        export.export_library(export.LogContext(quiet), self.conn, self.config, "lib_trips001",
                              out, {"location": True, "images": "none"})
        records = []
        texts = []
        for name in sorted(os.listdir(os.path.join(out, "data"))):
            with open(os.path.join(out, "data", name), encoding="utf-8") as handle:
                text = handle.read()
            texts.append(text)
            if name.startswith("index-"):
                records.extend(json.loads(text[text.index("["):text.rindex("]") + 1]))
        self.assertEqual(len(records), 6)
        self.assertEqual(sum(1 for record in records if record.get("gp")), 5)
        self.assertFalse(any(record.get("co") == "Testland" for record in records))
        self.assertFalse(any("gx" in record or "pe" in record for record in records))
        everything = "\n".join(texts)
        self.assertNotIn("Farhaven", everything)
        self.assertNotIn(str(FAR[1]), everything)
        locations = re.findall(r"\"lat\":", everything)
        self.assertEqual(len(locations), 5)


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminRouteTest(PassFixture, unittest.TestCase):

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
        return self.client.post("%s/images/%d/location" % (self.prefix, image_id),
                                data=json.dumps(body), content_type="application/json",
                                headers={"X-Search-Images": "1"}, **kwargs)

    def test_use_and_ignore_from_the_viewer(self):
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.check_all()
        detail = self.client.get("/api/image/%d" % home).get_json()
        self.assertTrue(detail["location_check"]["ignored"])
        self.assertIn("6,800 km away", detail["location_check"]["reason"])
        self.assertEqual(detail["location_check"]["place"], "Farhaven, Midlands, Testland")
        self.assertIsNone(detail["lat"])
        # (Its place: only the one estimated from the pictures around it.)
        self.assertEqual((detail["place"]["city"], detail["place"]["estimated"]),
                         ("Coralport", True))
        # A visitor sees nothing of it, not even the file's GPS data.
        theirs = self.client.get("/api/image/%d" % home, environ_base=REMOTE)
        self.assertEqual(theirs.status_code, 200, theirs.get_data(as_text=True))
        theirs = theirs.get_json()
        self.assertNotIn("location_check", theirs)
        self.assertNotIn("GPS", theirs["exif"])
        self.assertIn("GPS", json.loads(self.picture(home)["exif"]))
        answer = self.post(home, {"use": True})
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        answer = answer.get_json()
        self.assertEqual(answer["location_check"]["choice"], "use")
        self.assertFalse(answer["location_check"]["ignored"])
        self.assertEqual(answer["record"], {"gp": 1, "ci": "Farhaven", "rg": "Midlands",
                                            "co": "Testland", "cc": "XB", "km": 0})
        self.assertEqual(self.picture(home)["lat"], FAR[0])
        answer = self.post(home, {"use": False}).get_json()
        self.assertTrue(answer["location_check"]["ignored"])
        self.assertEqual(answer["record"], {"gx": 1, "ci": "Coralport", "rg": "North Coast",
                                            "co": "Examplia", "cc": "XA", "pe": 1, "ps": "t"})
        self.assertEqual(gps_check.choices(self.conn), {})
        self.assertEqual((answer["copies"], answer["others"], answer["more"]), (0, {}, 0))
        # A located picture's own pane: only the choice to ignore it.
        detail = self.client.get("/api/image/%d" % (home - 1)).get_json()
        self.assertEqual(detail["location_check"], {"ignored": False, "reason": "",
                                                    "choice": "", "place": "",
                                                    "estimated": False})

    def test_the_answer_holds_the_other_pictures_that_changed(self):
        # A camera's last known position, contradicted by one phone picture:
        # set aside. When the owner ignores the phone picture, the camera's
        # pictures have nothing against them any more.
        phone = self.insert(COAST, "14:05")
        first = self.insert(FAR, "14:02", camera=CAMERA, offset="")
        again = self.insert(FAR, "16:00", camera=CAMERA, offset="")
        self.check_all()
        self.assertEqual(gps_check.ignored_ids(self.conn), {first, again})
        answer = self.post(phone, {"use": False}).get_json()
        self.assertEqual(answer["record"], {"gx": 1})
        self.assertEqual(answer["others"], {
            str(first): {"gp": 1, "ci": "Farhaven", "rg": "Midlands", "co": "Testland",
                         "cc": "XB", "km": 0},
            str(again): {"gp": 1, "ci": "Farhaven", "rg": "Midlands", "co": "Testland",
                         "cc": "XB", "km": 0}})
        self.assertEqual(gps_check.ignored_ids(self.conn), {phone})

    def test_bad_requests(self):
        (first, *_rest) = self.day()
        self.assertEqual(self.post(first, {"use": "yes"}).status_code, 400)
        self.assertEqual(self.post(first, {}).status_code, 400)
        self.assertEqual(self.post(9999, {"use": True}).status_code, 404)
        no_position = self.insert(None, "10:00", camera=CAMERA, offset="")
        self.assertEqual(self.post(no_position, {"use": False}).status_code, 404)
        self.assertEqual(self.post(first, {"use": False}, environ_base=REMOTE).status_code,
                         403)
        self.assertIsNotNone(self.picture(first)["lat"])


class FalsePositiveTest(unittest.TestCase):
    """Real positions the check must keep."""

    def test_a_stale_fix_of_a_phone_standing_still(self):
        rows = day_with_phone()

        def aged(clock, minutes, speed=None, camera=PHONE):
            gps = fresh_gps(taken(clock), age=minutes * 60)
            if speed is not None:
                gps.update(GPSSpeed=speed, GPSSpeedRef="K")
            return row(near(COAST, 8), clock, camera=camera, gps=gps)
        # Six minutes old, standing still (or saying nothing): kept.
        for item in (aged("14:40", 6, speed=0), aged("14:40", 6)):
            self.assertEqual(findings(rows + [item]), {})
        # Six minutes old at 50 km/h: 5 km away by now.
        moving = aged("14:40", 6, speed=50)
        self.assertEqual(check(rows + [moving]).verdicts[moving["id"]].finding,
                         {"kind": "stale", "minutes": 6})
        # 10 m/s from an iPhone is 36 km/h: 3.6 km in six minutes. The same
        # number from another camera is 10 km/h: 1 km, kept.
        phone_rows = [dict(item, camera="Apple iPhone 99") for item in rows]
        iphone = aged("14:40", 6, speed=10, camera="Apple iPhone 99")
        self.assertEqual(set(findings(phone_rows + [iphone])), {iphone["id"]})
        self.assertEqual(findings(rows + [aged("14:40", 6, speed=10)]), {})
        # Older than 15 minutes, whatever it says.
        old = aged("14:40", 20, speed=0)
        self.assertEqual(set(findings(rows + [old])), {old["id"]})

    def test_a_flight_with_the_jet_stream_behind_it(self):
        # Window seat photos 45 minutes apart at 1,080 km/h over the ground,
        # with fresh fixes: kept at the default speed, not at 1,000 km/h.
        rows = []
        for leg in range(5):
            minutes = 18 * 60 + 45 * leg
            lon = -150.0 + 1080 * 0.75 * leg / (111.32 * math.cos(math.radians(5.0)))
            rows.append(row((5.0, round(lon, 4)), "%02d:%02d" % (minutes // 60, minutes % 60)))
        self.assertEqual(findings(rows), {})
        self.assertEqual(len(findings(rows, max_speed=1000)), 5)

    def test_copies_of_a_moving_fix_at_first(self):
        # (See test_a_frozen_fix_is_ignored: the first shots keep theirs.)
        def moving(clock):
            gps = dict(fresh_gps(taken(clock)), GPSSpeed=300.0, GPSSpeedRef="K")
            return row((COAST[0], COAST[1] + 0.5), clock, gps=gps)
        shots = [moving("18:48:20"), moving("18:48:23"), moving("18:50:05")]
        result = check(day_with_phone() + shots)
        self.assertEqual([result.verdicts[item["id"]].ignored for item in shots],
                         [False, False, True])

    def test_videos_of_a_phone_at_home_are_not_weak(self):
        # A phone's videos have no GPS data of their own to check, and their
        # positions are rounded: two at home months apart share one. The
        # phone has a GPS receiver (its photos have GPS times).
        home = (COAST[0], round(COAST[1], 4))
        videos = [row(home, "18:00", day="2021-01-10", media="video", gps={}),
                  row(home, "18:00", day="2021-06-10", media="video", gps={})]
        photos = [row(near(COAST, 30), "12:00", day="2021-03-%02d" % day) for day in (1, 2)]
        result = check(videos + photos)
        self.assertFalse(any(verdict.weak for verdict in result.verdicts.values()))
        # A camera without a receiver: weak, but only for one run at one
        # position, with nothing far in between and no week without a picture.
        stuck = [row(FAR, clock, camera=CAMERA, offset="") for clock in ("10:00", "13:00")]
        self.assertTrue(all(verdict.weak for verdict in check(stuck).verdicts.values()))
        apart = [row(FAR, "10:00", camera=CAMERA, offset=""),
                 row(COAST, "11:00", camera=CAMERA, offset=""),
                 row(FAR, "13:00", camera=CAMERA, offset="")]
        self.assertFalse(any(verdict.weak for verdict in check(apart).verdicts.values()))
        nearby = [row(FAR, "10:00", camera=CAMERA, offset=""),
                  row(near(FAR, 3), "11:00", camera=CAMERA, offset=""),
                  row(FAR, "13:00", camera=CAMERA, offset="")]
        self.assertTrue(check(nearby).verdicts[nearby[2]["id"]].weak)
        weeks = [row(FAR, "10:00", day="2021-03-01", camera=CAMERA, offset=""),
                 row(FAR, "10:00", day="2021-03-20", camera=CAMERA, offset="")]
        self.assertFalse(any(verdict.weak for verdict in check(weeks).verdicts.values()))

    def test_an_export_named_when_it_was_exported(self):
        # An app's export, dated by its name (written an hour after the
        # shot, so its own time says nothing), 1,500 km from the phone.
        trip = (COAST[0] + 13.5, COAST[1])
        exported = row(trip, "15:00", camera=CAMERA, offset="", source="filename",
                       software="Example Studio")
        self.assertEqual(findings(day_with_phone() + [exported]), {})
        # A camera's own file named by the time it was taken is not.
        named = dict(exported, id=next(_ids), software="")
        self.assertEqual(set(findings(day_with_phone() + [named])), {named["id"]})
        # Nor is an editor's export dated only by EXIF DateTime (when it was
        # saved: no DateTimeOriginal) with a Software tag.
        saved = dict(exported, id=next(_ids), taken_source="exif",
                     saved_date=True)
        self.assertEqual(findings(day_with_phone() + [saved]), {})
        self.assertEqual(set(findings(day_with_phone() + [dict(saved, saved_date=False)])),
                         {saved["id"]})
        # Without a position: its interval reaches 2 hours back too.
        result = check(day_with_phone())
        lo, hi, _mid, _clock_only = result.timing(dict(saved, lat=None, lon=None))
        self.assertEqual(hi - lo, gps_check.EXPORT_SECONDS)
        self.assertTrue(gps_check.export_date(saved))
        self.assertFalse(gps_check.export_date(dict(saved, software="")))

    def test_which_dates_are_an_editors(self):
        facts = gps_check._facts_of(json.dumps({"DateTime": "2021:03:04 15:00:00",
                                                "Software": "Some Editor 2.1"}))
        self.assertEqual((facts["software"], facts["saved_date"]), ("Some Editor 2.1", True))
        for exif in ({"DateTime": "2021:03:04 15:00:00",
                      "DateTimeOriginal": "2021:03:04 14:00:00"},
                     {"DateTime": "2021:03:04 15:00:00",
                      "DateTimeDigitized": "2021:03:04 14:00:00"},
                     {"DateTimeOriginal": "2021:03:04 14:00:00"}):
            self.assertFalse(gps_check._facts_of(json.dumps(exif))["saved_date"], exif)
        # (A blank DateTimeOriginal is none.)
        self.assertTrue(gps_check._facts_of(json.dumps({
            "DateTime": "2021:03:04 15:00:00",
            "DateTimeOriginal": "0000:00:00 00:00:00"}))["saved_date"])

    def test_many_pictures_at_one_position_are_quick(self):
        # A time-lapse: thousands of shots seconds apart at one position
        # (or wandering within a metre).
        def lapse(count, jitter):
            rng = random.Random(7)
            start = datetime.datetime(2021, 3, 4, 6, 0, 0)
            rows = []
            for number in range(count):
                when = start + datetime.timedelta(seconds=2 * number)
                rows.append(row((COAST[0] + rng.uniform(-jitter, jitter),
                                 COAST[1] + rng.uniform(-jitter, jitter)),
                                when.strftime("%H:%M:%S")))
            return rows
        for jitter in (0.0, 0.000002):
            rows = lapse(4000, jitter)
            started = time.perf_counter()
            self.assertEqual(findings(rows), {})
            self.assertLess(time.perf_counter() - started, 5.0)


class DoubtfulOffsetTest(unittest.TestCase):
    """A phone that kept the time zone of the day before after a flight."""

    EAST = (48.0, 10.0)         # (+01:00)
    WEST = (40.0, -110.0)       # (-07:00)

    def test_a_lone_stopover_picture_keeps_its_position(self):
        rows = []
        for clock in ("08:00", "08:30", "09:00", "09:30"):
            gps = fresh_gps(taken(clock), offset="+01:00")
            rows.append(row(near(self.EAST, len(rows)), clock, offset="+01:00", gps=gps))
        # Ten hours later at the far end (11:30 on the clock there, 18:30
        # UTC), still marked +01:00 (as if 10:30 UTC); only a GPS date.
        stopover = row(self.WEST, "11:30", offset="+01:00", gps={"GPSDateStamp": "2021:03:04"})
        self.assertEqual(findings(rows + [stopover]), {})
        # (Trusted as it is, its offset would put it 2 hours from the
        # pictures 8,800 km away.)
        with mock.patch.object(gps_check, "DOUBTFUL_HOURS", 24):
            self.assertEqual(findings(rows + [stopover]), {stopover["id"]: "travel"})

    def test_gps_times_time_the_pictures_without_a_time_zone(self):
        # A phone's pictures dated from their names (no time zone) but with
        # GPS times: those times give the library its time zone, so a
        # camera without one is timed by it, and contradicted.
        phone = [row(near(COAST, step), clock, offset="", gps=fresh_gps(taken(clock)))
                 for step, clock in enumerate(("13:50", "14:05", "14:30", "15:10"))]
        home = row(FAR, "14:03", camera=CAMERA, offset="")
        self.assertEqual(findings(phone + [home]), {home["id"]: "travel"})


class CopyChoiceTest(PassFixture, unittest.TestCase):
    """The owner's choice goes with every copy of the shot."""

    def test_a_choice_for_the_copies(self):
        self.day()
        original = self.insert(near(COAST, 20), "14:40", camera=CAMERA, offset="")
        export_ = self.insert(near(COAST, 21), "14:40", camera=CAMERA, offset="")
        self.check_all()
        ids = gps_check.set_choice(self.conn, self.config, original, False)
        self.conn.commit()
        self.assertEqual(ids, [original, export_])
        changed = set()
        summary = self.check_all_changed(changed)
        self.assertEqual(summary["ignored_by"], {"owner": 2})
        self.assertEqual(changed, {original, export_})
        view = gps_check.picture_view(self.conn, self.picture(export_))
        self.assertEqual((view["ignored"], view["choice"]), (True, "ignore"))
        self.assertEqual(view["reason"], "you chose to ignore it")
        # A copy that comes later follows the others.
        later = self.insert(near(COAST, 22), "14:40", camera=CAMERA, offset="")
        self.check_all()
        self.assertIsNone(self.picture(later)["lat"])
        view = gps_check.picture_view(self.conn, self.picture(later))
        self.assertEqual((view["ignored"], view["choice"], view["reason"]),
                         (True, "ignore", "you chose to ignore a copy of it"))
        # "Use this location" on the copy uses all of them again.
        self.assertEqual(gps_check.set_choice(self.conn, self.config, later, True),
                         [later, original, export_])
        self.conn.commit()
        self.assertEqual(gps_check.choices(self.conn), {})
        self.check_all()
        self.assertEqual(gps_check.ignored_ids(self.conn), set())

    def check_all_changed(self, changed):
        return gps_check.run(self.conn, self.config, changed=changed)


class ReadAgainTest(PassFixture, unittest.TestCase):
    """A file read again: its gps_checks row follows the position it gives now."""

    def ignored_home(self):
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        self.check_all()
        self.assertTrue(gps_check.is_ignored(self.conn, home))
        return home

    def test_the_same_position_stays_aside(self):
        home = self.ignored_home()
        values = dict(places.place_columns(*FAR), lat=FAR[0], lon=FAR[1])
        gps_check.file_read_again(self.conn, home, values)
        self.assertEqual((values["lat"], values["lon"], values["place_city"]), (None, None, ""))
        self.assertTrue(gps_check.is_ignored(self.conn, home))

    def test_a_file_without_its_position_any_more(self):
        home = self.ignored_home()
        values = dict(places.empty_columns(), lat=None, lon=None)
        gps_check.file_read_again(self.conn, home, values)
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL, exif = '{}', "
                          "content_hash = 'new' WHERE id = ?", (home,))
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gps_checks").fetchone()[0], 0)
        summary = self.check_all()
        self.assertEqual((summary["ignored"], summary["restored"]), (0, 0))
        self.assertIsNone(self.picture(home)["lat"])
        self.assertIsNone(gps_check.picture_view(self.conn, self.picture(home)))
        self.assertEqual(gps_check.set_choice(self.conn, self.config, home, True), [])
        self.assertFalse(gps_check.is_ignored(self.conn, home))

    def test_rows_left_from_before_count_for_nothing(self):
        home = self.ignored_home()
        # (As if written by an older version: the position is back, the row not.)
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?", FAR + (home,))
        self.conn.commit()
        self.assertFalse(gps_check.is_ignored(self.conn, home))
        self.assertEqual(gps_check.ignored_ids(self.conn), set())
        self.assertEqual(places.status(self.conn)["gps_ignored"], 0)
        # And a row of a picture whose position is gone, not set aside.
        other = self.insert(None, "10:00", camera=CAMERA, offset="")
        self.conn.execute("INSERT INTO gps_checks (image_id, lat, lon, ignored, reason) "
                          "VALUES (?, 1, 2, 0, '{}')", (other,))
        self.conn.commit()
        self.check_all()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM gps_checks WHERE image_id = ?", (other,)).fetchone()[0], 0)


class StoppedJobTest(PassFixture, unittest.TestCase):
    """A stopped job still checks what it read; an export with locations checks first."""

    def test_a_stopped_scan_and_previews_job(self):
        import scan
        self.day()
        home = self.insert(FAR, "14:03", camera=CAMERA, offset="")
        ctx = jobs.JobContext("t")
        ctx.request_stop()
        result = scan.run(self.config, ctx, describe_images=False)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["gps_check"]["ignored"], 1)
        self.assertIsNone(self.picture(home)["lat"])
        self.conn.execute("DELETE FROM gps_checks")
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?", FAR + (home,))
        self.conn.commit()
        ctx = jobs.JobContext("t")
        ctx.request_stop()
        summary = jobs.run_previews(ctx, self.conn, self.config, {})
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["gps_check"]["ignored"], 1)

    def test_a_busy_database_is_only_a_warning(self):
        ctx = jobs.JobContext("t")
        other = db.connect()
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        self.conn.execute("PRAGMA busy_timeout = 0")
        try:
            self.assertIsNone(gps_check.run_safely(self.conn, self.config, ctx))
        finally:
            other.rollback()
        self.assertTrue(any("could not run" in line for line in log_lines(ctx)))


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PaneScriptTest(unittest.TestCase):

    def test_the_information_pane(self):
        # tests/js/gps_check_test.js: the real page scripts on a made-up DOM.
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "gps_check_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
