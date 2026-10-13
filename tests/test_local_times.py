"""Times in the local time of the place (local_times.py), and the time zones of places.py.

The rules on made-up pictures with made-up time zones (decide), the
database pass on made-up rows (run: the times written and given back, the
camera's clock kept, a file read again, local times turned off, no place
names, no time zones, no time zone database), the time zones in places.db
(the second format, the first one still read and built again), drives that
go by their camera's clock, /api/image's "clock", a scan of real files, and
the pages under Node (tests/js/local_times_test.js). Every town, camera and
picture here is made up; the time zones are real IANA ids (the tests that
need their rules are skipped without a time zone database).
"""

import datetime
import io
import json
import os
import shutil
import sqlite3
import subprocess
import time
import unittest
import zipfile
from unittest import mock

import support

import common
import db
import drives
import gps_check
import jobs
import local_times
import make_places
import places
import tracks
import test_places

try:
    import flask
except ImportError:
    flask = None

quiet = support.quiet
NODE = shutil.which("node")
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}
ZONES_THERE = local_times.Zones().available()

# Made-up towns with real time zones: two far apart in one zone with daylight
# saving, one in a zone without it.
BRIGHTWATER = (48.0, 2.0)
WESTMERE = (49.0, 0.4)          # about 150 km from Brightwater
EASTHOLM = (35.0, 139.0)
TOWNS = [
    (401, "Brightwater", BRIGHTWATER[0], BRIGHTWATER[1], "PPL", "FR", "11", 20000),
    (402, "Eastholm", EASTHOLM[0], EASTHOLM[1], "PPL", "JP", "40", 50000),
    (403, "Westmere", WESTMERE[0], WESTMERE[1], "PPL", "FR", "11", 5000),
]
ZONES = {401: "Europe/Paris", 402: "Asia/Tokyo", 403: "Europe/London"}
PHONE = "Examplephone 7"
LIBRARY = "lib_times0001"


def install_towns(zones=ZONES, towns=TOWNS):
    """data/places (the GeoNames files and places.db) of the current SETTINGS_BASE from
    the made-up towns."""
    folder = places.places_dir()
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(os.path.join(folder, places.CITIES_FILE), "w") as archive:
        archive.writestr(places.CITIES_TEXT, test_places.cities_text(towns, zones))
    for name, text in ((places.REGIONS_FILE, test_places.regions_text()),
                       (places.COUNTRIES_FILE, test_places.countries_text())):
        with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    places.build(ctx=jobs.JobContext("test"))
    places.release()


def make_format_one():
    """Turn data/places/places.db into one of the first format (no time zones)."""
    conn = sqlite3.connect(places.places_db_path())
    try:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'format'")
        conn.execute("UPDATE meta SET value = '12345' WHERE key = 'built'")
        conn.execute("DROP TABLE zones")
        conn.execute("ALTER TABLE cities DROP COLUMN zone")
        conn.commit()
    finally:
        conn.close()
    places.release()


def utc_seconds(text):
    """Seconds since 1970 of a UTC 'YYYY-MM-DDTHH:MM:SS'."""
    return (datetime.datetime.fromisoformat(text) - datetime.datetime(1970, 1, 1)).total_seconds()


def gps_of(utc_text):
    """EXIF GPS facts of a fix at a UTC moment."""
    when = datetime.datetime.fromisoformat(utc_text)
    return {"GPSDateStamp": when.strftime("%Y:%m:%d"),
            "GPSTimeStamp": [when.hour, when.minute, when.second]}


def track_of(start_utc_text, position, seconds=60):
    """A tracks.Track: a fix a second, starting at a UTC moment, standing at a position."""
    start = utc_seconds(start_utc_text)
    return tracks.Track([tracks.Fix(float(at), start + at, position[0], position[1] + at * 1e-5,
                                    30.0, 90.0) for at in range(seconds)], "freegps")


# ---------------------------------------------------------------------------
# The rules (pure)

class FakeZones:
    """Made-up time zones: "Test/Summer" is UTC+02:00 from April to October and
    UTC+01:00 the rest of the year (by the UTC month), "Test/Nine" UTC+09:00,
    "Test/One" UTC+01:00 all year."""

    def available(self):
        return True

    def offset(self, name, utc):
        if name == "Test/Nine":
            return 540
        if name == "Test/One":
            return 60
        if name == "Test/Summer":
            month = time.gmtime(utc).tm_mon
            return 120 if 4 <= month <= 10 else 60
        return None


class FakeIndex:
    """Towns [(lat, lon, zone)]: the nearest one's zone (any distance), and the zones of
    those within km."""

    def __init__(self, towns):
        self.towns = towns

    def zone_of(self, lat, lon):
        best = min(self.towns, key=lambda town: places.distance_km(lat, lon, town[0], town[1]))
        return best[2], places.distance_km(lat, lon, best[0], best[1])

    def zones_near(self, lat, lon, km):
        return {town[2] for town in self.towns
                if places.distance_km(lat, lon, town[0], town[1]) <= km}


INDEX = FakeIndex([(48.0, 2.0, "Test/Summer"), (35.0, 139.0, "Test/Nine")])


def item(image_id, clock, recorded="", position=None, gps=None, track=None, key=PHONE):
    return local_times.Item(image_id, LIBRARY, key, clock, recorded,
                            position[0] if position else None,
                            position[1] if position else None, gps, track)


def decide(items, index=INDEX, **kwargs):
    return local_times.decide(items, index, FakeZones(), **kwargs)


class RuleTest(unittest.TestCase):

    def test_a_dashcam_clock_that_ignores_daylight_saving(self):
        # The clock is UTC+01:00 all year; in July the place is at UTC+02:00.
        clip = item(1, "2023-07-01T10:15:30", "+01:00", BRIGHTWATER,
                    track=track_of("2023-07-01T09:15:30", BRIGHTWATER), key=("folder", "x", ""))
        decisions, kept = decide([clip])
        self.assertEqual(decisions[1].taken_at, "2023-07-01T11:15:30")
        self.assertEqual(decisions[1].tz_offset, "+02:00")
        self.assertEqual((decisions[1].kind, decisions[1].minutes, decisions[1].zone),
                         ("track", 60, "Test/Summer"))
        # In January the clock is right: nothing changes.
        winter = item(2, "2023-01-10T10:15:30", "+01:00", BRIGHTWATER,
                      track=track_of("2023-01-10T09:15:30", BRIGHTWATER))
        decisions, kept = decide([winter])
        self.assertEqual(decisions, {})
        self.assertEqual(kept, {2: "right"})

    def test_a_phone_on_the_time_of_the_airport_it_left(self):
        # No offset in the file; the GPS time says the clock was at UTC+02:00
        # where the place is at UTC+09:00.
        photo = item(1, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        decisions, _kept = decide([photo])
        self.assertEqual((decisions[1].taken_at, decisions[1].tz_offset, decisions[1].kind),
                         ("2023-07-01T15:00:10", "+09:00", "gps"))
        # A GPS time a few minutes old is no evidence.
        stale = item(2, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T05:57:10"))
        self.assertEqual(decide([stale]), ({}, {2: "no_facts"}))
        # Nor is a GPS time that is the clock's own (a camera that writes its
        # local time as GPS time).
        own = item(3, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T08:00:10"))
        self.assertEqual(decide([own]), ({}, {3: "no_facts"}))

    def test_an_offset_that_fits_the_place_is_never_changed(self):
        # The file's offset is the place's: right, whatever the GPS time says.
        photo = item(1, "2023-07-01T08:00:10", "+09:00", EASTHOLM,
                     gps_of("2023-07-01T08:00:10"))
        self.assertEqual(decide([photo]), ({}, {1: "right"}))
        photo = item(2, "2023-07-01T08:00:10", "+09:00", EASTHOLM,
                     gps_of("2023-07-01T03:00:10"))
        self.assertEqual(decide([photo]), ({}, {2: "right"}))

    def test_a_phone_that_kept_the_offset_it_came_with(self):
        # The clock shows the local time; the file's offset is the one of
        # home, and the GPS date says it cannot be: only the offset changes.
        photo = item(1, "2023-07-01T08:30:00", "+02:00", EASTHOLM,
                     {"GPSDateStamp": "2023:06:30"})
        decisions, _kept = decide([photo])
        self.assertEqual((decisions[1].taken_at, decisions[1].tz_offset, decisions[1].kind),
                         ("2023-07-01T08:30:00", "+09:00", "gps_date"))
        # A GPS date both offsets fit says nothing.
        later = item(2, "2023-07-01T20:30:00", "+02:00", EASTHOLM,
                     {"GPSDateStamp": "2023:07:01"})
        self.assertEqual(decide([later]), ({}, {2: "unsure"}))

    def test_pictures_without_facts_go_by_their_camera(self):
        anchor = item(1, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        # No position, four minutes before: the same camera's clock, and the
        # time zone of where that picture was.
        before = item(2, "2023-07-01T07:56:10")
        # A copy without its offset, in the same second.
        copy = item(3, "2023-07-01T08:00:10", "+02:00")
        # Another camera, an offset of another time zone, too long before.
        other = item(4, "2023-07-01T07:58:00", key="another camera")
        offset = item(5, "2023-07-01T07:57:00", "+05:00")
        early = item(6, "2023-07-01T04:59:00")
        decisions, kept = decide([anchor, before, copy, other, offset, early])
        self.assertEqual((decisions[2].taken_at, decisions[2].tz_offset, decisions[2].kind,
                          decisions[2].source, decisions[2].seconds, decisions[2].zone),
                         ("2023-07-01T14:56:10", "+09:00", "like", 1, 240.0, "Test/Nine"))
        self.assertEqual(decisions[3].taken_at, "2023-07-01T15:00:10")
        self.assertEqual(sorted(decisions), [1, 2, 3])
        self.assertEqual((kept[4], kept[5], kept[6]), ("no_facts", "no_facts", "no_facts"))
        # Three hours is the most (and for the time zone of a picture without
        # a position, the window of an estimated place).
        self.assertEqual(sorted(decide([anchor, early], window=4 * 3600,
                                       zone_window=4 * 3600)[0]), [1, 6])

    def test_the_time_zone_of_a_picture_without_a_position_comes_from_near_in_time(self):
        anchor = item(1, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        # Two hours before, without a position: the camera's clock is known,
        # where it was is not (it may have been elsewhere by then).
        early = item(2, "2023-07-01T06:00:00")
        decisions, kept = decide([anchor, early])
        self.assertEqual(sorted(decisions), [1])
        self.assertEqual(kept[2], "no_zone")
        # 29 minutes is near enough; the window is the caller's.
        near = item(3, "2023-07-01T07:31:10")
        self.assertEqual(decide([anchor, near])[0][3].zone, "Test/Nine")
        self.assertEqual(decide([anchor, near], zone_window=600)[1][3], "no_zone")
        # So for a clip whose position was set aside.
        key = ("folder", LIBRARY, "Dashcam")
        clip = item(4, "2023-07-01T10:15:30", "+01:00", BRIGHTWATER,
                    track=track_of("2023-07-01T09:15:30", BRIGHTWATER), key=key)
        aside = item(5, "2023-07-01T11:15:30", "+01:00",
                     track=track_of("2023-07-01T10:15:30", BRIGHTWATER), key=key)
        self.assertEqual(decide([clip, aside])[1][5], "no_zone")
        self.assertEqual(decide([clip, aside], zone_window=3600)[0][5].zone, "Test/Summer")

    def test_a_borrowed_clock_never_puts_a_picture_before_its_own_fix(self):
        anchor = item(1, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        # Ten minutes later, its position set aside, with a GPS fix of 07:00
        # UTC: by the anchor's clock it was taken at 06:10 UTC, before its own
        # fix. A fix may be old; never from after the picture.
        later = item(2, "2023-07-01T08:10:00", gps=gps_of("2023-07-01T07:00:00"))
        decisions, kept = decide([anchor, later])
        self.assertEqual(sorted(decisions), [1])
        self.assertEqual(kept[2], "unsure")
        # An old fix (05:00 UTC) is no contradiction; nor a fix a minute after.
        for fix in ("2023-07-01T05:00:00", "2023-07-01T06:11:00"):
            older = item(3, "2023-07-01T08:10:00", gps=gps_of(fix))
            self.assertEqual(decide([anchor, older])[0][3].taken_at, "2023-07-01T15:10:00")
        # A GPS date after the picture's UTC day says the same.
        dated = item(4, "2023-07-01T08:10:00", gps={"GPSDateStamp": "2023:07:02"})
        self.assertEqual(decide([anchor, dated])[1][4], "unsure")

    def test_a_camera_that_went_to_another_time_zone_does_not_borrow(self):
        # The anchor says the clock was at UTC+02:00, at Brightwater (Test/Summer,
        # UTC+02:00 in July). An hour later the camera is at Eastholm (UTC+09:00):
        # its clock may have been set to the local time in between.
        anchor = item(1, "2023-07-01T08:00:10", "", BRIGHTWATER, gps_of("2023-07-01T06:00:10"))
        away = item(2, "2023-07-01T09:00:00", "", EASTHOLM)
        decisions, kept = decide([anchor, away])
        self.assertEqual(decisions, {})
        self.assertEqual(kept, {1: "right", 2: "unsure"})
        # A picture taken there later is the one it goes by: its clock was set
        # to the local time (UTC+09:00), so this one shows it.
        there = item(3, "2023-07-01T10:30:00", "", EASTHOLM, gps_of("2023-07-01T01:30:00"))
        decisions, kept = decide([anchor, away, there])
        self.assertEqual(decisions, {})
        self.assertEqual(kept, {1: "right", 2: "right", 3: "right"})
        # In the same time zone it does.
        home = item(4, "2023-07-01T09:00:00", "", WESTMERE)
        index = FakeIndex([(48.0, 2.0, "Test/Summer"), (49.0, 0.4, "Test/Summer")])
        self.assertEqual(decide([anchor, home], index)[1][4], "right")

    def test_a_clock_set_to_utc_is_known_by_its_track(self):
        clip = item(1, "2023-07-01T09:15:30", "+00:00", BRIGHTWATER,
                    track=track_of("2023-07-01T09:15:30", BRIGHTWATER), key=("folder", "x", ""))
        decisions, _kept = decide([clip])
        self.assertEqual((decisions[1].kind, decisions[1].minutes, decisions[1].taken_at,
                          decisions[1].tz_offset), ("track", 0, "2023-07-01T11:15:30", "+02:00"))

    def test_which_side_of_a_time_zone_border_is_not_known(self):
        # The nearest town (10 km) is in Test/Summer, another 25 km away in
        # Test/One; the dashcam's clock (UTC-08:00) is neither's.
        index = FakeIndex([(48.09, 2.0, "Test/Summer"), (47.775, 2.0, "Test/One")])
        clip = item(1, "2023-07-01T10:00:00", "-08:00", (48.0, 2.0),
                    track=track_of("2023-07-01T18:00:00", (48.0, 2.0)), key=("folder", "x", ""))
        self.assertEqual(decide([clip], index), ({}, {1: "border"}))
        # Far enough from the other time zone, the nearest town's.
        index = FakeIndex([(48.09, 2.0, "Test/Summer"), (47.2, 2.0, "Test/One")])
        decisions, _kept = decide([clip], index)
        self.assertEqual((decisions[1].zone, decisions[1].taken_at, decisions[1].tz_offset),
                         ("Test/Summer", "2023-07-01T20:00:00", "+02:00"))

    def test_a_picture_that_fits_its_place_does_not_follow_its_camera(self):
        anchor = item(1, "2023-07-01T08:00:10", "+02:00", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        home = item(2, "2023-07-01T07:00:00", "+02:00", BRIGHTWATER)
        decisions, kept = decide([anchor, home])
        self.assertEqual(sorted(decisions), [1])
        self.assertEqual(kept[2], "right")

    def test_the_clips_of_a_drive_without_a_track(self):
        key = ("folder", LIBRARY, "Dashcam")
        clips = [item(1, "2023-07-01T10:14:30", "", key=key),
                 item(2, "2023-07-01T10:15:30", "+01:00", BRIGHTWATER,
                      track=track_of("2023-07-01T09:15:30", BRIGHTWATER), key=key),
                 item(3, "2023-07-01T10:16:30", "", key=key)]
        decisions, _kept = decide(clips)
        self.assertEqual([decisions[number].taken_at for number in (1, 2, 3)],
                         ["2023-07-01T11:14:30", "2023-07-01T11:15:30", "2023-07-01T11:16:30"])
        self.assertEqual(decisions[1].kind, "like")
        self.assertEqual(decisions[1].tz_offset, "+02:00")
        # A clip whose track says when, but whose position was set aside: the
        # time zone of the clip next to it.
        aside = item(4, "2023-07-01T10:17:30", "+01:00",
                     track=track_of("2023-07-01T09:17:30", BRIGHTWATER), key=key)
        decisions, _kept = decide(clips[1:2] + [aside])
        self.assertEqual((decisions[4].taken_at, decisions[4].kind, decisions[4].zone),
                         ("2023-07-01T11:17:30", "track", "Test/Summer"))
        self.assertEqual(decide([aside]), ({}, {4: "no_zone"}))

    def test_near_a_time_zone_border(self):
        # A town of another time zone near: the clock may well be right.
        index = FakeIndex([(48.0, 2.0, "Test/Summer"), (48.3, 2.0, "Test/One")])
        photo = item(1, "2023-07-01T08:00:10", "", (48.01, 2.0), gps_of("2023-07-01T07:00:10"))
        self.assertEqual(decide([photo], index), ({}, {1: "border"}))
        # With a wrong offset in its file, that one is given.
        photo = item(2, "2023-07-01T08:00:10", "+05:00", (48.01, 2.0),
                     gps_of("2023-07-01T07:00:10"))
        decisions, _kept = decide([photo], index)
        self.assertEqual((decisions[2].taken_at, decisions[2].tz_offset, decisions[2].zone),
                         ("2023-07-01T08:00:10", "+01:00", "Test/One"))
        # Farther than ZONE_BORDER_KM more than the nearest town: no.
        index = FakeIndex([(48.0, 2.0, "Test/Summer"), (49.0, 2.0, "Test/One")])
        decisions, _kept = decide([item(3, "2023-07-01T08:00:10", "", (48.01, 2.0),
                                        gps_of("2023-07-01T07:00:10"))], index)
        self.assertEqual(decisions[3].tz_offset, "+02:00")

    def test_without_a_time_zone(self):
        class Nowhere:
            def zone_of(self, lat, lon):
                return None

            def zones_near(self, lat, lon, km):
                return set()
        photo = item(1, "2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.assertEqual(decide([photo], Nowhere()), ({}, {1: "no_zone"}))

    def test_settings(self):
        self.assertEqual(local_times.settings({}), {"enabled": True})
        self.assertEqual(local_times.settings({"local_times": {"enabled": False}}),
                         {"enabled": False})
        self.assertEqual(local_times.settings({"local_times": False}), {"enabled": False})
        self.assertEqual(local_times.settings({"local_times": {"enabled": "no"}}),
                         {"enabled": True})

    def test_facts(self):
        self.assertEqual(local_times.gps_day({"GPSDateStamp": "1970:01:02"}), 1)
        self.assertIsNone(local_times.gps_day({"GPSDateStamp": "2023:02:30"}))
        self.assertEqual(local_times.local_text(86400 + 3661), "1970-01-02T01:01:01")
        self.assertEqual(local_times.clock_key({"camera": " Example  Cam "}), "example cam")
        self.assertEqual(local_times.clock_key({"media": "video", "library_id": "l",
                                                "path": "a/b/c.mp4"}), ("folder", "l", "a/b"))
        self.assertIsNone(local_times.clock_key({"media": "image"}))
        self.assertEqual(local_times.gps_minutes(gps_of("2023-07-01T06:00:40"),
                                                 utc_seconds("2023-07-01T08:01:20")), 120)
        self.assertIsNone(local_times.gps_minutes(gps_of("2023-07-01T06:00:00"),
                                                  utc_seconds("2023-07-01T08:01:20")))

    def test_without_a_time_zone_database(self):
        def missing(name):
            raise LookupError(name)
        with mock.patch("zoneinfo.ZoneInfo", side_effect=missing):
            zones = local_times.Zones()
            self.assertFalse(zones.available())
            self.assertIsNone(zones.offset("Europe/Paris", 0))

    @unittest.skipUnless(ZONES_THERE, "no time zone database")
    def test_real_time_zones(self):
        zones = local_times.Zones()
        self.assertEqual(zones.offset("Europe/Paris", utc_seconds("2023-07-01T12:00:00")), 120)
        self.assertEqual(zones.offset("Europe/Paris", utc_seconds("2023-01-01T12:00:00")), 60)
        # (Daylight saving starts at 01:00 UTC on the last Sunday of March.)
        self.assertEqual(zones.offset("Europe/Paris", utc_seconds("2023-03-26T00:59:59")), 60)
        self.assertEqual(zones.offset("Europe/Paris", utc_seconds("2023-03-26T01:00:00")), 120)
        self.assertEqual(zones.offset("Asia/Kolkata", 0), 330)
        self.assertEqual(local_times.local_offset(zones, "Europe/Paris",
                                                  utc_seconds("2023-07-01T12:00:00")), 120)
        self.assertIsNone(zones.offset("Nowhere/Atall", 0))


# ---------------------------------------------------------------------------
# Time zones in places.db

class PlaceZonesTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([])
        self.addCleanup(places.release)
        install_towns()

    def test_the_towns_time_zones(self):
        index = places.shared()
        self.assertTrue(index.has_zones)
        self.assertEqual(index.zone_of(*EASTHOLM)[0], "Asia/Tokyo")
        self.assertAlmostEqual(index.zone_of(48.01, 2.0)[1], 1.1, places=1)
        self.assertIsNone(index.zone_of(-60.0, -120.0))       # (no town near)
        self.assertEqual(index.zones_near(48.5, 1.2, 100), {"Europe/Paris", "Europe/London"})
        self.assertEqual(index.zones_near(48.0, 2.0, 10), {"Europe/Paris"})
        self.assertEqual(index.town(index.nearest(*EASTHOLM)[0])["zone"], "Asia/Tokyo")
        meta = places.read_meta()
        self.assertEqual((meta["format"], meta["zones"]), ("2", "3"))
        self.assertIsNone(places.rebuild_if_needed())
        state = places.status()
        self.assertTrue(state["zones"])
        self.assertFalse(state["rebuild"])

    def test_a_town_without_a_time_zone(self):
        install_towns({401: "Europe/Paris", 402: "not a zone!"})
        index = places.shared()
        self.assertIsNone(index.zone_of(*EASTHOLM))
        self.assertEqual(index.zone_of(*BRIGHTWATER)[0], "Europe/Paris")
        self.assertEqual(index.town(index.nearest(*EASTHOLM)[0])["zone"], "")

    def test_an_index_of_the_first_format_is_read_and_built_again(self):
        make_format_one()
        index = places.shared()
        self.assertIsNotNone(index)
        self.assertFalse(index.has_zones)
        self.assertIsNone(index.zone_of(*EASTHOLM))
        self.assertEqual(index.zones_near(*EASTHOLM, 50), set())
        self.assertEqual(index.columns(*EASTHOLM)["place_city"], "Eastholm")
        state = places.status()
        self.assertTrue(state["available"])
        self.assertFalse(state["zones"])
        self.assertTrue(state["rebuild"])
        ctx = jobs.JobContext("t")
        summary = places.rebuild_if_needed(ctx)
        self.assertEqual(summary["zones"], 3)
        meta = places.read_meta()
        # (Built again from the same files: when they were downloaded stays.)
        self.assertEqual((meta["format"], meta["built"]), ("2", "12345"))
        self.assertTrue(any("no time zones yet" in entry["message"]
                            for entry in ctx.snapshot(log_tail=50)["log_tail"]))
        places.release()
        self.assertTrue(places.shared().has_zones)

    def test_the_lookup_command_says_the_time_zone(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(make_places.main(["--lookup", "35.01", "139.0"]), 0)
        self.assertIn("Time zone: Asia/Tokyo", output.getvalue())


# ---------------------------------------------------------------------------
# The database pass

class PassFixture(support.TempBaseMixin):
    """A library in a temporary SETTINGS_BASE, with the made-up towns."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", self.folder, id=LIBRARY)])
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(places.release)
        install_towns()
        self.count = 0

    def insert(self, clock, offset="", position=None, gps=None, camera=PHONE, media="image",
               source="exif", name=None, **extra):
        self.count += 1
        name = name or "p%d.%s" % (self.count, "mp4" if media == "video" else "jpg")
        values = {"library_id": LIBRARY, "path": "f/" + name, "name": name, "size": 100,
                  "mtime": 0, "ctime": 0, "taken_at": clock, "taken_source": source,
                  "tz_offset": offset, "camera": camera, "media": media,
                  "lat": position[0] if position else None,
                  "lon": position[1] if position else None,
                  "exif": json.dumps({"GPS": gps} if gps else {}),
                  "content_hash": "hash%d" % self.count, "added": 0, "updated": 0}
        values.update(places.place_columns(values["lat"], values["lon"]))
        values.update(extra)
        columns = sorted(values)
        image_id = self.conn.execute(
            "INSERT INTO images (%s) VALUES (%s)" % (", ".join(columns),
                                                     ", ".join("?" * len(columns))),
            [values[column] for column in columns]).lastrowid
        self.conn.commit()
        return image_id

    def clip(self, clock, start_utc, position, name, duration=60.0):
        image_id = self.insert(clock, "", position, camera="", media="video", source="video",
                               name=name, duration=duration)
        track = track_of(start_utc, position, int(duration))
        middle = track.middle(duration)
        self.conn.execute("UPDATE images SET lat = ?, lon = ?, tz_offset = ? WHERE id = ?",
                          (middle.lat, middle.lon, tracks.offset_text(
                              tracks.clock_offset(track.start, clock,
                                                  gps_check.local_seconds)[0]), image_id))
        tracks.store(self.conn, tracks.content_key(self.picture(image_id)), track, duration)
        self.conn.commit()
        return image_id

    def picture(self, image_id):
        return self.conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()

    def times(self, image_id):
        row = self.picture(image_id)
        return row["taken_at"], row["tz_offset"]

    def localise(self, config=None, **kwargs):
        return local_times.run(self.conn, config or self.config, **kwargs)


@unittest.skipUnless(ZONES_THERE, "no time zone database")
class PassTest(PassFixture, unittest.TestCase):

    def test_local_times_are_written_and_the_camera_clock_kept(self):
        clips = [self.clip("2023-07-01T10:%02d:30" % minute,
                           "2023-07-01T09:%02d:30" % minute, BRIGHTWATER,
                           "20230701_10%02d30_%04d.MP4" % (minute, minute))
                 for minute in (15, 16, 17)]
        lost = self.insert("2023-07-01T10:18:30", "", camera="", media="video", source="video",
                           name="20230701_101830_0018.MP4", duration=60.0)
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        home = self.insert("2023-07-01T07:00:00", "+02:00", BRIGHTWATER,
                           gps_of("2023-07-01T05:00:00"))
        undated = self.insert("2023-07-01T08:00:10", "", EASTHOLM,
                              gps_of("2023-07-01T06:00:10"), source="mtime")
        ctx = jobs.JobContext("t")
        summary = self.localise(ctx=ctx)
        self.assertEqual(summary["state"], "done")
        self.assertEqual((summary["local"], summary["shifted"], summary["offset_only"],
                          summary["changed"]), (5, 5, 0, 5))
        self.assertEqual(summary["kinds"], {"track": 3, "like": 1, "gps": 1})
        self.assertEqual(summary["libraries"][LIBRARY]["shifted"], 5)
        self.assertEqual(self.times(clips[0]), ("2023-07-01T11:15:30", "+02:00"))
        self.assertEqual(self.times(lost), ("2023-07-01T11:18:30", "+02:00"))
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        self.assertEqual(self.times(home), ("2023-07-01T07:00:00", "+02:00"))
        self.assertEqual(self.times(undated), ("2023-07-01T08:00:10", ""))
        self.assertIn("Local times: 5 pictures show the local time of their place (3 by their "
                      "GPS track, 1 by their GPS time, 1 by another picture of the same "
                      "camera): 5 at another time than their camera's clock; 5 changed now.",
                      [entry["message"] for entry in ctx.snapshot(log_tail=20)["log_tail"]])
        side = local_times.for_picture(self.conn, self.picture(phone))
        self.assertEqual((side["clock"], side["clock_offset"], side["zone"], side["kind"]),
                         ("2023-07-01T08:00:10", "", "Asia/Tokyo", "gps"))
        view = local_times.view(self.conn, self.picture(lost), admin=True)
        self.assertEqual(view, {"taken_at": "2023-07-01T10:18:30", "tz_offset": "",
                                "kind": "like", "zone": "Europe/Paris",
                                "source": clips[2], "seconds": 60.0})
        # Visitors: the zone only with a position of its own.
        self.assertEqual(local_times.view(self.conn, self.picture(lost)),
                         {"taken_at": "2023-07-01T10:18:30", "tz_offset": "", "kind": "like"})
        self.assertEqual(local_times.view(self.conn, self.picture(clips[0]))["tz_offset"],
                         "+01:00")
        self.assertIsNone(local_times.view(self.conn, self.picture(home)))
        self.assertEqual(local_times.camera_times(self.conn, [clips[0], home]),
                         {clips[0]: ("2023-07-01T10:15:30", "+01:00")})
        status = local_times.status(self.conn, self.config)
        self.assertEqual((status["state"], status["local"], status["shifted"], status["kinds"]),
                         ("done", 5, 5, {"track": 3, "like": 1, "gps": 1}))
        # Once more: nothing changes.
        again = self.localise()
        self.assertEqual((again["local"], again["changed"], again["restored"]), (5, 0, 0))
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))

    def test_a_file_read_again_is_decided_again(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.localise()
        # (A scan writes the camera's time back when it reads the file again.)
        self.conn.execute("UPDATE images SET taken_at = '2023-07-01T08:00:10', tz_offset = '' "
                          "WHERE id = ?", (phone,))
        self.conn.commit()
        self.assertIsNone(local_times.for_picture(self.conn, self.picture(phone)))
        summary = self.localise()
        self.assertEqual(summary["changed"], 1)
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        # Read again with another clock (the owner set the file's date): the
        # scanner says so (scan._update_row).
        self.conn.execute("UPDATE images SET taken_at = '2023-07-01T15:00:10', "
                          "tz_offset = '+09:00' WHERE id = ?", (phone,))
        local_times.file_read_again(self.conn, phone)
        self.conn.commit()
        summary = self.localise()
        self.assertEqual((summary["local"], summary["changed"]), (0, 0))
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        self.assertEqual(local_times.stored(self.conn), {})

    def test_a_picture_whose_facts_are_gone_gets_its_camera_time_back(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.localise()
        # (The GPS check set its position aside.)
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL WHERE id = ?", (phone,))
        self.conn.commit()
        summary = self.localise()
        self.assertEqual((summary["local"], summary["restored"]), (0, 1))
        self.assertEqual(self.times(phone), ("2023-07-01T08:00:10", ""))

    def test_turned_off_every_camera_time_comes_back(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        offset = self.insert("2023-07-01T08:30:00", "+02:00", EASTHOLM,
                             {"GPSDateStamp": "2023:06:30"})
        summary = self.localise()
        self.assertEqual((summary["shifted"], summary["offset_only"]), (1, 1))
        self.assertEqual(self.times(offset), ("2023-07-01T08:30:00", "+09:00"))
        off = dict(self.config, local_times={"enabled": False})
        ctx = jobs.JobContext("t")
        summary = self.localise(off, ctx=ctx)
        self.assertEqual((summary["state"], summary["restored"]), ("off", 2))
        self.assertEqual(self.times(phone), ("2023-07-01T08:00:10", ""))
        self.assertEqual(self.times(offset), ("2023-07-01T08:30:00", "+02:00"))
        self.assertEqual(local_times.stored(self.conn), {})
        self.assertIn("Local times are off: 2 picture(s) have their camera's time again.",
                      [entry["message"] for entry in ctx.snapshot(log_tail=20)["log_tail"]])
        self.assertEqual(local_times.status(self.conn, off)["state"], "off")
        # And on again.
        self.assertEqual(self.localise()["changed"], 2)

    def test_without_what_it_needs_nothing_changes(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.localise()
        # No time zone database: kept as they are.
        unavailable = mock.Mock(available=mock.Mock(return_value=False))
        summary = self.localise(zones=unavailable)
        self.assertEqual((summary["state"], summary["changed"], summary["restored"]),
                         ("no_tzdata", 0, 0))
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        with mock.patch.object(local_times, "Zones", return_value=unavailable):
            ctx = jobs.JobContext("t")
            self.assertEqual(local_times.run_safely(self.conn, self.config, ctx)["state"],
                             "no_tzdata")
            self.assertTrue(any("pip install tzdata" in entry["message"] for entry
                                in ctx.snapshot(log_tail=20)["log_tail"]))
            self.assertEqual(local_times.status(self.conn, self.config)["state"], "no_tzdata")
        # No place names.
        os.remove(places.places_db_path())
        places.release()
        self.assertEqual(self.localise()["state"], "no_places")
        self.assertEqual(local_times.status(self.conn, self.config)["state"], "no_places")
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))

    def test_a_place_index_without_time_zones_is_built_again(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        make_format_one()
        self.assertEqual(self.localise()["state"], "no_zones")
        self.assertEqual(local_times.status(self.conn, self.config)["state"], "no_zones")
        self.assertEqual(self.times(phone), ("2023-07-01T08:00:10", ""))
        ctx = jobs.JobContext("t")
        summary = local_times.run_safely(self.conn, self.config, ctx)
        self.assertEqual((summary["state"], summary["changed"]), ("done", 1))
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        self.assertTrue(places.has_zones(places.read_meta()))

    def test_the_gps_check_runs_again_when_times_change(self):
        self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        with mock.patch.object(gps_check, "run_safely") as checked:
            local_times.run_safely(self.conn, self.config, jobs.JobContext("t"))
            self.assertEqual(checked.call_count, 1)
            local_times.run_safely(self.conn, self.config, jobs.JobContext("t"))
            self.assertEqual(checked.call_count, 1)

    def test_the_places_job_works_them_out(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        make_format_one()
        summary = jobs.run_places(jobs.JobContext("t"), self.conn, self.config, {})
        self.assertEqual(summary["local_times"]["changed"], 1)
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        state = places.status(self.conn)
        self.assertEqual((state["zones"], state["local_times"]["local"]), (True, 1))

    def test_drives_go_by_the_camera_clock(self):
        # Three clips of a drive across a time zone border: by the local time
        # the last one starts before the others; by the clock it follows them.
        clips = [self.clip("2023-07-01T10:%02d:30" % minute, "2023-07-01T09:%02d:30" % minute,
                           BRIGHTWATER if minute < 17 else WESTMERE,
                           "20230701_10%02d30_%04d.MP4" % (minute, minute))
                 for minute in (15, 16, 17)]
        self.localise()
        self.assertEqual([self.times(clip)[0] for clip in clips],
                         ["2023-07-01T11:15:30", "2023-07-01T11:16:30", "2023-07-01T10:17:30"])
        found = drives.library_drives(self.conn, LIBRARY, self.config)
        self.assertEqual([drive.ids for drive in found], [clips])
        # (Without the camera's clock they would make no drive.)
        with mock.patch.object(local_times, "camera_times", return_value={}):
            drives._memo.clear()
            self.assertEqual(drives.library_drives(self.conn, LIBRARY, self.config), [])
        drives._memo.clear()


@unittest.skipUnless(ZONES_THERE, "no time zone database")
@unittest.skipIf(flask is None, "Flask is not installed")
class RouteTest(PassFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import serve
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def test_the_camera_clock_in_the_details(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        copy = self.insert("2023-07-01T08:00:10", "")
        right = self.insert("2023-07-01T07:00:00", "+02:00", BRIGHTWATER)
        self.localise()
        detail = self.client.get("/api/image/%d" % phone).get_json()
        self.assertEqual((detail["taken_at"], detail["tz_offset"]),
                         ("2023-07-01T15:00:10", "+09:00"))
        self.assertEqual(detail["clock"], {"taken_at": "2023-07-01T08:00:10", "tz_offset": "",
                                           "kind": "gps", "zone": "Asia/Tokyo"})
        theirs = self.client.get("/api/image/%d" % copy, environ_base=REMOTE).get_json()
        self.assertEqual(theirs["clock"], {"taken_at": "2023-07-01T08:00:10", "tz_offset": "",
                                           "kind": "like"})
        mine = self.client.get("/api/image/%d" % copy).get_json()
        self.assertEqual((mine["clock"]["zone"], mine["clock"]["source"]),
                         ("Asia/Tokyo", phone))
        self.assertNotIn("clock", self.client.get("/api/image/%d" % right).get_json())

    def admin_post(self, route, body):
        import admin
        return self.client.post(admin.URL_PREFIX + route, data=json.dumps(body),
                                content_type="application/json",
                                headers={"X-Search-Images": "1"})

    def test_a_position_ignored_takes_its_local_time_with_it_at_once(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        # (The same camera, a minute later, no position: it goes by that clock.)
        later = self.insert("2023-07-01T08:01:10", "")
        self.localise()
        self.assertEqual(self.times(later), ("2023-07-01T15:01:10", "+09:00"))
        answer = self.admin_post("/images/%d/location" % phone, {"use": False}).get_json()
        self.assertEqual(self.times(phone), ("2023-07-01T08:00:10", ""))
        self.assertEqual(self.times(later), ("2023-07-01T08:01:10", ""))
        self.assertEqual(answer["time"], {"taken_at": "2023-07-01T08:00:10", "tz_offset": "",
                                          "clock": None})
        self.assertEqual((answer["record"]["ts"], answer["record"]["gx"]),
                         ("2023-07-01T08:00:10", 1))
        self.assertEqual(answer["others"][str(later)]["ts"], "2023-07-01T08:01:10")
        detail = self.client.get("/api/image/%d" % phone).get_json()
        self.assertNotIn("clock", detail)
        # Used again: the local time of its place again.
        answer = self.admin_post("/images/%d/location" % phone, {"use": True}).get_json()
        self.assertEqual(self.times(phone), ("2023-07-01T15:00:10", "+09:00"))
        self.assertEqual(self.times(later), ("2023-07-01T15:01:10", "+09:00"))
        self.assertEqual((answer["time"]["taken_at"], answer["time"]["clock"]["kind"]),
                         ("2023-07-01T15:00:10", "gps"))
        self.assertEqual(answer["record"]["dk"], "2023-07-01")

    def test_a_place_chosen_works_the_local_times_out_again(self):
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.localise()
        # (Its position set aside by a check that the local times did not
        # follow yet.)
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL WHERE id = ?", (phone,))
        self.conn.commit()
        answer = self.admin_post("/images/%d/place" % phone, {"choice": "reject"})
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        self.assertEqual(self.times(phone), ("2023-07-01T08:00:10", ""))
        self.assertEqual(answer.get_json()["time"]["taken_at"], "2023-07-01T08:00:10")
        self.assertEqual(answer.get_json()["record"]["ts"], "2023-07-01T08:00:10")

    def test_an_export_with_the_location_follows_its_gps_check(self):
        import export
        phone = self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.localise()
        # The owner ignored the position; the check has not run since.
        with self.conn:
            gps_check.set_choice(self.conn, self.config, phone, False)
        with mock.patch.object(export._Export, "run", return_value={}):
            export.export_library(jobs.JobContext("t"), self.conn, self.config, LIBRARY,
                                  self.base.join("site"), {"location": True})
        self.assertIsNone(self.picture(phone)["lat"])
        self.assertEqual(self.times(phone), ("2023-07-01T08:00:10", ""))

    def test_the_manage_page_counts_them(self):
        self.insert("2023-07-01T08:00:10", "", EASTHOLM, gps_of("2023-07-01T06:00:10"))
        self.localise()
        import admin
        answer = self.client.get(admin.URL_PREFIX + "/places",
                                 headers={"X-Search-Images": "1"}).get_json()
        self.assertEqual(answer["local_times"], {"enabled": True, "state": "done", "local": 1,
                                                 "shifted": 1, "offset_only": 0,
                                                 "kinds": {"gps": 1}})
        self.assertTrue(answer["zones"])


@unittest.skipUnless(ZONES_THERE, "no time zone database")
@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class ScanTest(PassFixture, unittest.TestCase):
    """Real files: a scan gives them the local time, and again after reading them again."""

    def photo(self, name, clock, offset, position, fix_utc, seed=0):
        import imagegen
        from PIL import Image
        image = Image.new("RGB", (48, 32), (seed * 40 % 256, 90, 150))
        fix = datetime.datetime.fromisoformat(fix_utc)
        gps_tags = {7: (imagegen.IFDRational(fix.hour, 1), imagegen.IFDRational(fix.minute, 1),
                        imagegen.IFDRational(fix.second, 1)),
                    29: fix.strftime("%Y:%m:%d")}
        return imagegen.jpeg_with_exif(self.folder, name, image=image,
                                       date=clock.replace("-", ":").replace("T", " "),
                                       offset=offset, make="Examplephone", model="7",
                                       gps=position, gps_tags=gps_tags)

    def scan(self):
        import scan
        ctx = jobs.JobContext("t")
        return scan.run(self.config, ctx, describe_images=False, embed_vectors=False), ctx

    def test_a_scan(self):
        # A phone that kept the time zone of home, abroad.
        path = self.photo("away.jpg", "2023-07-01T08:00:10", "+02:00", EASTHOLM,
                          "2023-07-01T06:00:10")
        self.photo("home.jpg", "2023-06-20T09:00:00", "+02:00", BRIGHTWATER,
                   "2023-06-20T07:00:00", seed=1)
        result, ctx = self.scan()
        self.assertEqual(result["local_times"]["changed"], 1)
        rows = {row["name"]: row for row in self.conn.execute("SELECT * FROM images")}
        self.assertEqual((rows["away.jpg"]["taken_at"], rows["away.jpg"]["tz_offset"]),
                         ("2023-07-01T15:00:10", "+09:00"))
        self.assertEqual((rows["home.jpg"]["taken_at"], rows["home.jpg"]["tz_offset"]),
                         ("2023-06-20T09:00:00", "+02:00"))
        self.assertTrue(any(entry["message"].startswith("Local times: 1 picture show")
                            for entry in ctx.snapshot(log_tail=200)["log_tail"]))
        # A scan that finds nothing new changes nothing.
        result, _ctx = self.scan()
        self.assertEqual(result["local_times"]["changed"], 0)
        # The file changed: read again, and given the local time again.
        stat = os.stat(path)
        self.photo("away.jpg", "2023-07-01T08:00:20", "+02:00", EASTHOLM, "2023-07-01T06:00:20",
                   seed=3)
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))
        result, _ctx = self.scan()
        self.assertEqual(result["local_times"]["changed"], 1)
        self.assertEqual(self.times(rows["away.jpg"]["id"]), ("2023-07-01T15:00:20", "+09:00"))
        self.assertEqual(local_times.for_picture(self.conn, self.picture(
            rows["away.jpg"]["id"]))["clock"], "2023-07-01T08:00:20")


class PageScriptTest(unittest.TestCase):

    @unittest.skipIf(NODE is None, "Node is not installed")
    def test_the_pages(self):
        # tests/js/local_times_test.js: the real page scripts on a made-up DOM.
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "local_times_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
