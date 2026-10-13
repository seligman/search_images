"""GPS tracks of videos (tracks.py): reading dashcams' freeGPS blocks and NMEA
text tracks from made-up MP4 files (tests/gps_fixtures.py), cleaning,
simplifying and keeping tracks, a scan that gives clips their positions and
clocks, the GPS check and the place estimates that use them, the routes of
drives for the server and the pages (tests/js/routes_test.js under Node),
and what an export publishes of them. Every position, time and camera here
is made up.
"""

import datetime
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import support

import gps_fixtures as gpsdata

import common
import db
import drives
import gps_check
import jobs
import place_estimates
import tracks

try:
    import flask
except ImportError:
    flask = None

NODE = shutil.which("node")
quiet = support.quiet
START = gpsdata.utc(2023, 8, 17, 12, 0, 0)
LAT, LON = -41.2, -150.3


def fix(at, utc, lat=LAT, lon=LON, speed=None, course=None):
    return tracks.Fix(float(at), float(utc), lat, lon, speed, course)


class ParsingTest(unittest.TestCase):

    def test_an_rmc_sentence(self):
        sentence = gpsdata.rmc(START + 12.25, LAT, LON, knots=10.0, course=271.5)
        utc, lat, lon, speed, course = tracks.parse_rmc(sentence)
        self.assertAlmostEqual(utc, START + 12.25, places=3)
        self.assertAlmostEqual(lat, LAT, places=6)
        self.assertAlmostEqual(lon, LON, places=6)
        self.assertAlmostEqual(speed, 18.52, places=3)       # knots to km/h
        self.assertEqual(course, 271.5)
        self.assertEqual(tracks.parse_rmc(sentence.encode("ascii"))[:3], (utc, lat, lon))
        # The GPS talker of any system, and no checksum at all.
        self.assertIsNotNone(tracks.parse_rmc(gpsdata.rmc(START, LAT, LON, talker="GP")))
        self.assertIsNotNone(tracks.parse_rmc(gpsdata.rmc(START, LAT, LON, checksum=False)))
        north_east = tracks.parse_rmc(gpsdata.rmc(START, 12.5, 33.75))
        self.assertEqual(north_east[1:3], (12.5, 33.75))

    def test_sentences_that_are_no_fix(self):
        good = gpsdata.rmc(START, LAT, LON)
        for text in (gpsdata.rmc(START, LAT, LON, status="V"),         # no fix
                     good[:-2] + "00",                                # a wrong checksum
                     gpsdata.rmc(START, 0.0, 0.0),                    # zeros
                     gpsdata.rmc(START, LAT, LON).replace("RMC", "GGA"),
                     "$GNRMC,120000.000,A,4112.0000,S", "", "nothing", None, 12):
            self.assertIsNone(tracks.parse_rmc(text), text)
        # A date the clock never had (year 2000 + 99 is past the range).
        self.assertIsNone(tracks.parse_rmc(gpsdata.rmc(START, LAT, LON).replace(
            ",170823,", ",311399,").rsplit("*", 1)[0]))
        # Minutes of 60 or more are no position.
        self.assertIsNone(tracks.parse_rmc(
            "$GNRMC,120000.000,A,4161.0000,S,15018.0000,W,1.0,90.0,170823,,,A"))

    def test_freegps_blocks(self):
        sentence = gpsdata.rmc(START, LAT, LON, knots=20.0)
        for data in (gpsdata.block(sentence),
                     gpsdata.block(binary=(START, LAT, LON, 20.0, 90.0, "A")),
                     gpsdata.block(binary=(START, LAT, LON, 20.0, 90.0, "A"), layout="floats"),
                     # (Both: the sentence counts.)
                     gpsdata.block(sentence, binary=(START + 99, 1.0, 1.0, 1.0, 1.0, "A"))):
            utc, lat, lon, speed, _course = tracks.parse_block(data)
            self.assertEqual(utc, START)
            self.assertAlmostEqual(lat, LAT, places=5)
            self.assertAlmostEqual(lon, LON, places=5)
            self.assertAlmostEqual(speed, 37.04, places=2)
        for data in (gpsdata.block(), gpsdata.block(binary=(START, LAT, LON, 0, 0, "V")),
                     b"\x00\x00\x40\x00freeGPS", b"\x00\x00\x40\x00somethin" + bytes(200)):
            self.assertIsNone(tracks.parse_block(data))

    def test_the_binary_layouts_exiftool_documents(self):
        fields = (START, LAT, LON, 20.0, 271.5, "A")
        for layout in ("doubles", "doubles-30", "floats", "floats-10"):
            for signed in (False, True):
                # (Some cameras write the south and west negative too.)
                found = tracks.parse_block(gpsdata.block(binary=fields, layout=layout,
                                                         signed=signed))
                self.assertIsNotNone(found, layout)
                utc, lat, lon, speed, course = found
                self.assertEqual(utc, START, layout)
                self.assertAlmostEqual(lat, LAT, places=3)
                self.assertAlmostEqual(lon, LON, places=3)
                self.assertAlmostEqual(speed, 37.04, places=2)
                self.assertAlmostEqual(course, 271.5, places=2)
            north_east = tracks.parse_block(gpsdata.block(binary=(START, 12.5, 33.75, 0, 0, "A"),
                                                          layout=layout))
            self.assertAlmostEqual(north_east[1], 12.5, places=3)
            self.assertAlmostEqual(north_east[2], 33.75, places=3)
            self.assertIsNone(tracks.parse_block(gpsdata.block(
                binary=(START, LAT, LON, 0, 0, "V"), layout=layout)), layout)
        # A full year: UTC in most layouts; where the A119 V3 has its date, a
        # camera that writes the full year writes its local time (not read).
        self.assertEqual(tracks.parse_block(gpsdata.block(binary=fields, layout="floats",
                                                          year=2023))[0], START)
        self.assertIsNone(tracks.parse_block(gpsdata.block(binary=fields, layout="floats-10",
                                                           year=2023)))
        # Text where the A119 V3 has its numbers (another camera's: encrypted
        # base64, or decimal digits) is not read as that layout.
        for first, second in ((b"bWFkZSB1cA==", b"bm90IGEgZml4"), (b"4311.5000", b"1030.0000")):
            data = bytearray(gpsdata.block(binary=fields, layout="floats-10"))
            data[0x2c:0x40] = first.ljust(20, b"\x00")
            data[0x40:0x54] = second.ljust(20, b"\x00")
            self.assertIsNone(tracks.parse_block(bytes(data)))
        # (Integers where the Type 3 has its floats: exiftool's IQS variant.)
        data = bytearray(gpsdata.block(binary=fields, layout="floats"))
        data[16:19] = b"IQS"
        self.assertIsNone(tracks.parse_block(bytes(data)))

    def test_days_a_month_does_not_have(self):
        sentence = gpsdata.rmc(START, LAT, LON).rsplit("*", 1)[0]
        for date in ("320823", "310923", "290223", "001323", "170023"):
            self.assertIsNone(tracks.parse_rmc(sentence.replace(",170823,", "," + date + ",")),
                              date)
        self.assertIsNotNone(tracks.parse_rmc(sentence.replace(",170823,", ",290224,")))
        self.assertIsNone(tracks.parse_block(gpsdata.block(
            binary=(gpsdata.utc(2023, 2, 28, 12), LAT, LON, 0, 0, "A"), layout="floats",
            year=23).replace(struct.pack("<3i", 23, 2, 28), struct.pack("<3i", 23, 2, 30))))

    def test_cleaning(self):
        fixes = [fix(0, START), fix(1, START + 1), fix(2, START + 1),         # a repeat
                 fix(3, START + 3, lon=LON + 1.0),                           # a glitch
                 fix(4, START + 4, lon=LON + 0.0004), fix(4, START + 5)]     # the same place
        cleaned = tracks.clean(fixes)
        self.assertEqual([item.at for item in cleaned], [0.0, 1.0, 4.0])
        # A jump that stays is no glitch: the fixes after it agree with it.
        moved = [fix(0, START), fix(1, START + 1), fix(2, START + 2, lon=LON + 1.0),
                 fix(3, START + 3, lon=LON + 1.0)]
        self.assertEqual(len(tracks.clean(moved)), 4)

    def test_cleaning_glitches_anywhere(self):
        steady = [fix(second, START + second, lon=LON + 0.0002 * second) for second in range(60)]
        # A fix whose time is a day late (a date glitch) or an hour off: only it is
        # left out, wherever it is (the others are not dropped after it).
        for index, shift in ((1, 86400), (0, 3600), (30, -3600), (59, 86400), (0, 30)):
            fixes = list(steady)
            fixes[index] = fixes[index]._replace(utc=fixes[index].utc + shift)
            cleaned = tracks.clean(fixes)
            self.assertEqual(len(cleaned), 59, (index, shift))
            self.assertNotIn(float(index), [item.at for item in cleaned])
            self.assertEqual(tracks.Track(cleaned, "freegps").start, START)
        # (A second or two is a GPS's own: kept.)
        fixes = list(steady)
        fixes[10] = fixes[10]._replace(utc=fixes[10].utc + 0.5)
        self.assertEqual(len(tracks.clean(fixes)), 60)
        # The first or the last position far off, while the next two agree: left out
        # (it would draw the route to it, and be taken for where the drive starts).
        for index in (0, 59):
            fixes = list(steady)
            fixes[index] = fixes[index]._replace(lat=LAT + 30.0)
            cleaned = tracks.clean(fixes)
            self.assertEqual(len(cleaned), 59, index)
            self.assertTrue(all(item.lat == LAT for item in cleaned))

    def test_simplifying_keeps_the_place_at_every_moment(self):
        # Along a straight line at a steady speed: the two ends say it all.
        straight = [fix(second, START + second, lon=LON + 0.0003 * second)
                    for second in range(61)]
        self.assertEqual([item.at for item in tracks.simplify(straight)], [0.0, 60.0])
        # The same line with a stop in the middle: where it stopped (and when)
        # is kept, or the moments around it would be placed wrongly.
        stop = [fix(second, START + second, lon=LON + 0.0003 * min(second, 20)
                    + 0.0003 * max(0, second - 40)) for second in range(61)]
        kept = tracks.simplify(stop)
        self.assertEqual([item.at for item in kept], [0.0, 20.0, 40.0, 60.0])
        for item in stop:
            where = tracks.position_at([(point.at, point.lat, point.lon, None)
                                        for point in kept], item.at)
            self.assertLess(abs(where[1] - item.lon) * 111320 * 0.752, tracks.SIMPLIFY_METRES)
        # A corner.
        corner = ([fix(second, START + second, lon=LON + 0.0003 * second) for second in range(30)]
                  + [fix(second, START + second, lat=LAT + 0.0003 * (second - 29),
                         lon=LON + 0.0003 * 29) for second in range(30, 60)])
        self.assertIn(29.0, [item.at for item in tracks.simplify(corner)])
        self.assertEqual(tracks.simplify(straight[:2]), straight[:2])

    def test_encoding(self):
        fixes = [fix(0, START, speed=12.4), fix(12.5, START + 12.5, lat=LAT + 0.00123,
                                                  lon=LON - 0.00456, speed=None),
                 fix(60, START + 60, lat=-LAT, lon=179.99999, speed=101.6)]
        text = tracks.encode(fixes)
        self.assertEqual(text, "[[0,-4120000,-15030000,12],[125,123,-456,-1],"
                               "[475,8239877,33030455,102]]")
        self.assertEqual(tracks.decode(text), [(0.0, -41.2, -150.3, 12),
                                               (12.5, -41.19877, -150.30456, None),
                                               (60.0, 41.2, 179.99999, 102)])
        for bad in ("", "x", "[1]", "[[1,2,3]]", '[[1,2,3,"4"]]', "[[1,2,3,true]]", None):
            self.assertEqual(tracks.decode(bad), [], bad)

    def test_the_clock_by_the_gps_time(self):
        local = gps_check.local_seconds
        # The clock is 8 hours behind UTC, give or take a few seconds.
        self.assertEqual(tracks.clock_offset(START + 3, "2023-08-17T04:00:00", local),
                         (-480, -3.0))
        self.assertEqual(tracks.clock_offset(START - 45, "2023-08-17T17:45:00", local),
                         (345, 45.0))
        # More than a minute off a quarter hour: no time zone.
        minutes, off = tracks.clock_offset(START + 300, "2023-08-17T04:00:00", local)
        self.assertIsNone(minutes)
        self.assertEqual(off, -300.0)
        self.assertEqual(tracks.clock_offset(START, "", local), (None, None))
        self.assertEqual(tracks.offset_text(-480), "-08:00")
        self.assertEqual(tracks.offset_text(345), "+05:45")
        self.assertEqual(tracks.iso_utc(START + 0.9), "2023-08-17T12:00:00Z")

    def test_a_track(self):
        track = tracks.Track([fix(second, START + second + 2, lon=LON + 0.001 * second)
                              for second in range(20, 61)], "freegps")
        self.assertEqual(track.start, START + 2)          # (the fixes' times less their places)
        self.assertEqual(track.end, START + 62)
        self.assertEqual(track.middle(60).at, 30.0)
        self.assertEqual(track.middle().at, 40.0)          # (the middle of the fixes)
        self.assertAlmostEqual(track.extent_km(track.middle(60)), 30 * 0.001 * 111.19 * 0.7524,
                               places=1)
        points = [(0.0, LAT, LON, 10), (10.0, LAT, LON + 0.01, 30)]
        self.assertEqual(tracks.position_at(points, 5.0), (LAT, LON + 0.005, 20.0))
        self.assertEqual(tracks.position_at(points, -3), (LAT, LON, 10))
        self.assertEqual(tracks.position_at(points, 99), (LAT, LON + 0.01, 30))
        self.assertIsNone(tracks.position_at([], 1))
        facts = tracks.Facts("k", 2, "freegps", START, START + 10, 5.0, LAT, LON, 20.0, 0.4,
                             points)
        self.assertEqual([round(item[0] - START) for item in tracks.samples(facts, 4)],
                         [0, 4, 8, 10])
        gps = tracks.gps_facts(facts)
        self.assertEqual(gps, {"GPSStatus": "A", "GPSDateStamp": "2023:08:17",
                               "GPSTimeStamp": [12, 0, 0.0], "GPSHPositioningError": 400.0,
                               "GPSSpeed": 20.0, "GPSSpeedRef": "K"})
        self.assertEqual(gps_check.fix_time(gps), START)
        self.assertTrue(gps_check.checkable(gps))

    def test_settings(self):
        self.assertEqual(tracks.settings({}), {"enabled": True})
        self.assertEqual(tracks.settings({"gps_tracks": {"enabled": False}}), {"enabled": False})
        self.assertEqual(tracks.settings({"gps_tracks": False}), {"enabled": False})
        self.assertEqual(tracks.settings({"gps_tracks": {"enabled": "no"}}), {"enabled": True})


class ReadTrackTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="tracks-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def write(self, name, data):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def blocks(self, count=6, first=0, before=0):
        return ([gpsdata.block() for _ in range(before)]
                + [gpsdata.block(sentence) for sentence in gpsdata.drive_fixes(
                    START + before, count, first=first)])

    def check(self, track, count, kind="freegps", first_at=0.0):
        self.assertIsNotNone(track)
        self.assertEqual((track.kind, len(track.fixes)), (kind, count))
        self.assertEqual(track.fixes[0].at, first_at)
        self.assertEqual(track.start, START)
        self.assertAlmostEqual(track.fixes[1].lon - track.fixes[0].lon, 0.0002, places=6)

    def test_blocks_listed_in_the_movie_header(self):
        for name, data in (("last.mp4", gpsdata.mp4(self.blocks())),
                           ("first.mp4", gpsdata.mp4(self.blocks(), moov_first=True)),
                           ("udta.mp4", gpsdata.mp4(self.blocks(), index_in_udta=True))):
            self.check(tracks.read_track(self.write(name, data)), 6)
        # A camera still looking for satellites writes empty blocks first: the
        # fixes keep their places (seconds) in the video.
        track = tracks.read_track(self.write("cold.mp4", gpsdata.mp4(self.blocks(4, before=3))))
        self.check(track, 4, first_at=3.0)
        self.assertEqual(track.middle(7).at, 3.0)
        # Without any fix: no track.
        self.assertIsNone(tracks.read_track(self.write(
            "none.mp4", gpsdata.mp4([gpsdata.block() for _ in range(4)]))))
        # An empty entry in the index (no block for that second): the fixes after
        # it keep their seconds.
        blocks = self.blocks(6)
        blocks[2] = None
        track = tracks.read_track(self.write("hole.mp4", gpsdata.mp4(blocks)))
        self.assertEqual([item.at for item in track.fixes], [0.0, 1.0, 3.0, 4.0, 5.0])
        self.assertEqual(track.start, START)

    def test_only_the_listed_blocks_are_read(self):
        path = self.write("big.mp4", gpsdata.mp4(self.blocks(), gap=300000))
        reads = []
        real_read = tracks._read

        def read(handle, at, size):
            reads.append(size)
            return real_read(handle, at, size)

        with mock.patch.object(tracks, "_read", read):
            self.check(tracks.read_track(path), 6)
        self.assertLessEqual(sum(reads), 8 * tracks.BLOCK_BYTES)

    def test_blocks_without_an_index_are_searched_for(self):
        data = gpsdata.mp4(self.blocks(8), index=False, gap=777)
        path = self.write("loose.mp4", data)
        self.check(tracks.read_track(path), 8)
        # Read in small pieces: blocks (and their names) split between two
        # reads are found once each.
        for chunk in (1000, 2048, 4099, 16391):
            with mock.patch.object(tracks, "CHUNK_BYTES", chunk):
                self.check(tracks.read_track(path), 8)
        # A block only after the start the reader looks at: no track (the rest
        # of a large video is never read for nothing).
        with mock.patch.object(tracks, "PROBE_BYTES", 500):
            self.assertIsNone(tracks.read_track(path))

    def test_a_text_track_of_sentences(self):
        sentences = [text.encode("ascii") + b"\r\n" for text in gpsdata.drive_fixes(START, 5)]
        path = self.write("text.mp4", gpsdata.text_track_mp4(sentences, timescale=1000,
                                                             delta=1000))
        track = tracks.read_track(path)
        self.check(track, 5, kind="text")
        self.assertEqual([item.at for item in track.fixes], [0.0, 1.0, 2.0, 3.0, 4.0])
        # A subtitle track (sbtl) too; not a video track's samples.
        self.check(tracks.read_track(self.write("sbtl.mp4", gpsdata.text_track_mp4(
            sentences, handler=b"sbtl"))), 5, kind="text")
        self.assertIsNone(tracks.read_track(self.write("vide.mp4", gpsdata.text_track_mp4(
            sentences, handler=b"vide"))))
        # Subtitles of another kind are not read through.
        other = [b"Lens 24mm, ISO 100"] * 40 + sentences
        path = self.write("other.mp4", gpsdata.text_track_mp4(other))
        reads = []
        real_read = tracks._read

        def read(handle, at, size):
            reads.append(size)
            return real_read(handle, at, size)

        with mock.patch.object(tracks, "_read", read):
            self.assertIsNone(tracks.read_track(path))
        self.assertLess(len(reads), 40)

    def test_files_that_are_no_track(self):
        good = gpsdata.mp4(self.blocks())
        for name, data in (("empty.mp4", b""), ("short.mp4", b"\x00\x00"),
                           ("text.mp4", b"This is not a video at all, only words." * 50),
                           ("cut.mp4", good[:200]),
                           ("broken.mp4", gpsdata.FTYP + b"\x00\x00\x00\x05moov")):
            self.assertIsNone(tracks.read_track(self.write(name, data)), name)
        # An index that points outside the file, or at nothing.
        bad = gpsdata.FTYP + gpsdata.box(b"moov", gpsdata.box(
            b"gps ", b"\x00\x00\x01\x01\x00\x00\x00\x02" + b"\x7f\xff\xff\xff\x00\x00\x40\x00"
            + b"\x00\x00\x00\x08\x00\x00\x40\x00"))
        self.assertIsNone(tracks.read_track(self.write("bad.mp4", bad)))
        with self.assertRaises(OSError):
            tracks.read_track(os.path.join(self.folder, "missing.mp4"))

    def test_the_command_line(self):
        path = self.write("drive.mp4", gpsdata.mp4(self.blocks()))
        empty = self.write("empty.mp4", b"")
        lines = []
        with mock.patch("builtins.print", lambda *args, **kwargs: lines.append(
                " ".join(str(arg) for arg in args))):
            self.assertEqual(tracks.main([path, empty, "--fixes"]), 0)
        text = "\n".join(lines)
        self.assertIn("6 fixes (freegps), 2023-08-17 12:00:00 to 12:00:05 UTC", text)
        self.assertIn("empty.mp4: no GPS track", text)
        self.assertIn("-41.200000, -150.299800  74.1 km/h  90", text)


class StorageTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([])
        db.init(common.load_config(), log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def add_video(self, image_id, content_hash, lat=None, lon=None, tz=""):
        self.conn.execute(
            "INSERT INTO images (id, library_id, path, name, size, mtime, content_hash, media, "
            "lat, lon, tz_offset, taken_at, taken_source) VALUES (?, 'lib', ?, ?, 1, 0, ?, "
            "'video', ?, ?, ?, '2023-08-17T04:00:00', 'video')",
            (image_id, "v%d.mp4" % image_id, "v%d.mp4" % image_id, content_hash, lat, lon, tz))

    def test_keeping_tracks(self):
        self.assertFalse(tracks.have_table(self.conn))
        self.assertEqual(tracks.stored(self.conn), {})
        self.assertEqual(tracks.read_keys(self.conn), set())
        track = tracks.Track([fix(second, START + second, lon=LON + 0.0003 * second, speed=40.0)
                              for second in range(61)], "freegps")
        facts = tracks.store(self.conn, "hash-a", track, 60.0, now=5)
        tracks.store(self.conn, "hash-b", None)
        self.conn.commit()
        self.assertTrue(tracks.have_table(self.conn))
        self.assertEqual((facts.fixes, facts.at, facts.lat, facts.start, facts.end, facts.speed),
                         (61, 30.0, LAT, START, START + 60, 40.0))
        self.assertEqual(len(facts.points), 2)
        self.assertEqual(tracks.read_keys(self.conn), {"hash-a", "hash-b"})
        self.assertEqual(set(tracks.stored(self.conn)), {"hash-a"})
        self.assertEqual(tracks.stored(self.conn, ["hash-a", "hash-b", "other"])["hash-a"],
                         facts)
        self.add_video(1, "hash-a", facts.lat, facts.lon)
        self.add_video(2, "hash-b")
        self.add_video(3, "", LAT, LON)
        self.conn.commit()
        rows = self.conn.execute("SELECT * FROM images ORDER BY id").fetchall()
        found = tracks.for_rows(self.conn, rows)
        self.assertEqual(list(found), [1])
        self.assertEqual(tracks.for_rows(self.conn, rows, {"gps_tracks": {"enabled": False}}), {})
        self.assertTrue(tracks.is_position(found[1], rows[0]["lat"], rows[0]["lon"]))
        self.assertFalse(tracks.is_position(found[1], LAT + 0.1, rows[0]["lon"]))
        self.assertFalse(tracks.is_position(None, LAT, LON))
        self.assertEqual([row["id"] for row in tracks.position_rows(self.conn, "lib")], [1])
        self.assertEqual(tracks.content_key(rows[2]), "#3")
        # A track of a content no video has any more is forgotten.
        tracks.store(self.conn, "gone", track)
        self.assertEqual(tracks.prune(self.conn), 1)
        self.assertEqual(tracks.read_keys(self.conn), {"hash-a", "hash-b"})
        # Tracks read by an older version are read again.
        self.conn.execute("UPDATE video_tracks SET format = 0 WHERE content_key = 'hash-a'")
        self.assertEqual(tracks.read_keys(self.conn), {"hash-b"})
        self.assertEqual(tracks.stored(self.conn), {})

    def test_what_a_track_gives_its_video(self):
        track = tracks.Track([fix(second, START + second + 3, lon=LON + 0.0003 * second)
                              for second in range(61)], "freegps")
        result = {"lat": None, "lon": None, "tz_offset": "", "duration": 60.0,
                  "taken_at": "2023-08-17T04:00:00"}
        tracks.apply_to_result(result, track)
        self.assertEqual((result["lat"], round(result["lon"], 6), result["tz_offset"]),
                         (LAT, round(LON + 0.009, 6), "-08:00"))
        self.assertIs(result["track"], track)
        # Its own position and offset stay; no track changes nothing.
        own = {"lat": 1.5, "lon": 2.5, "tz_offset": "+01:00", "taken_at": "2023-08-17T04:00:00"}
        tracks.apply_to_result(own, track)
        self.assertEqual((own["lat"], own["lon"], own["tz_offset"]), (1.5, 2.5, "+01:00"))
        nothing = {"lat": None, "lon": None, "tz_offset": ""}
        tracks.apply_to_result(nothing, None)
        self.assertEqual(nothing, {"lat": None, "lon": None, "tz_offset": "", "track": None})
        # A clock minutes off: the position, but no offset.
        off = {"lat": None, "lon": None, "tz_offset": "", "taken_at": "2023-08-17T04:05:00"}
        tracks.apply_to_result(off, track)
        self.assertEqual(off["tz_offset"], "")
        self.assertIsNotNone(off["lat"])
        facts = tracks.store(self.conn, "k", track, 60.0)
        row = {"lat": None, "lon": None, "tz_offset": "", "taken_at": "2023-08-17T04:00:00"}
        values = tracks.row_values(row, facts)
        self.assertEqual((values["lat"], values["tz_offset"], values["place_km"]),
                         (LAT, "-08:00", None))
        self.assertEqual(tracks.row_values({"lat": 1.0, "lon": 2.0, "tz_offset": "+00:00",
                                            "taken_at": ""}, facts), {})


# The made-up road: east at STEP degrees a second (about 75 km/h there).
STEP = 0.00025
DRIVE_B = 0.5           # (the second drive is this far east)


def road(seconds, drive_b=False):
    """The longitude seconds into the driving of a drive."""
    return LON + (DRIVE_B if drive_b else 0.0) + STEP * seconds


class LibraryCase(support.TempBaseMixin, unittest.TestCase):
    """A library of dashcam clips (fake videos, see videotools) whose tracks a
    stand-in for tracks.read_track gives: two drives on a made-up road, the
    first clip of each without a fix yet (the car still parked where it then
    drove off); a phone video with its own position."""

    def setUp(self):
        super().setUp()
        import fake_ffmpeg
        import videotools
        self.tools = videotools.FakeToolsMixin()
        self.tools.addCleanup = self.addCleanup
        self.tools.use_fake_tools()
        self.folder = self.base.library_folder("Trip")
        # The camera's clock is 8 hours behind UTC and knows no time zone.
        clock = datetime.datetime(2023, 8, 17, 4, 0, 0)
        self.plan = [(1, 0, 60, None), (2, 60, 60, 0), (3, 120, 60, 60), (4, 180, 60, 120),
                     (5, 7200, 60, None), (6, 7260, 60, 0), (7, 7320, 60, 60)]
        self.names = []
        self.tracks = {}
        for number, offset, duration, fixes_from in self.plan:
            moment = clock + datetime.timedelta(seconds=offset)
            name = "%s_%04d_N_A.MP4" % (moment.strftime("%Y%m%d_%H%M%S"), number)
            end = moment + datetime.timedelta(seconds=duration)
            probe = fake_ffmpeg.probe_json(
                width=2560, height=1440, duration=duration, codec="hevc", brand="mp42",
                format_tags={"creation_time": end.strftime("%Y-%m-%dT%H:%M:%S.000000Z")},
                stream_tags={})
            fake_ffmpeg.make_video(os.path.join(self.folder, name), probe)
            self.names.append(name)
            if fixes_from is not None:
                # East along the road, fixes_from seconds into the driving.
                self.tracks[name] = tracks.Track([
                    fix(second, START + offset + second,
                        lon=road(fixes_from + second, offset > 3600), speed=75.0)
                    for second in range(61)], "freegps")
        fake_ffmpeg.make_video(os.path.join(self.folder, "IMG_0001.MOV"),
                               videotools.clip_probe())
        self.reads = []
        patcher = mock.patch.object(tracks, "read_track", self.read_track)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read_track(self, path):
        name = os.path.basename(path)
        self.reads.append(name)
        if getattr(self, "unreadable", None) == name:
            raise OSError("the share went away")
        return self.tracks.get(name)

    def configure(self, **extra):
        self.base.write_config([support.library("Trip", self.folder, id="lib_trip1")], **extra)
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        return self.config

    def scan(self):
        import scan
        self.ctx = jobs.JobContext("test", log_limit=5000)
        return scan.run(self.config, self.ctx, describe_images=False)

    def conn(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        return conn

    def rows(self):
        return {row["name"]: dict(row) for row in self.conn().execute("SELECT * FROM images")}

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=5000)["log_tail"])


class ScanTest(LibraryCase):

    def test_clips_get_the_positions_and_clocks_of_their_tracks(self):
        self.configure()
        summary = self.scan()
        rows = self.rows()
        clip = rows[self.names[1]]
        self.assertEqual((clip["lat"], round(clip["lon"], 6), clip["tz_offset"]),
                         (LAT, round(road(30), 6), "-08:00"))
        self.assertEqual(clip["taken_at"], "2023-08-17T04:01:00")    # (the camera's clock)
        # The first clips had no fix yet: no position, no offset.
        for name in (self.names[0], self.names[4]):
            self.assertEqual((rows[name]["lat"], rows[name]["tz_offset"]), (None, ""))
        # The phone keeps its own position (and is not asked for a track it has not).
        phone = rows["IMG_0001.MOV"]
        self.assertEqual((phone["lat"], phone["lon"]), (33.4484, -112.074))
        conn = self.conn()
        kept = conn.execute("SELECT content_key, fixes FROM video_tracks").fetchall()
        self.assertEqual(sorted(row[1] for row in kept), [0, 0, 0, 61, 61, 61, 61, 61])
        self.assertEqual(sorted(set(self.reads)), sorted(self.names + ["IMG_0001.MOV"]))
        # What the tracks say about the clips' clock.
        self.assertIn("GPS tracks: 5 of 8 video(s) read in Trip have one; by the GPS time "
                      "their clocks are at UTC-08:00 (5 videos).", self.log_text())
        # Nothing is read again by the next scan (nor said again).
        self.reads[:] = []
        self.scan()
        self.assertEqual(self.reads, [])
        self.assertNotIn("GPS tracks:", self.log_text())
        # The GPS check counts them (timed by their tracks, though the camera
        # has no name) and sets none aside.
        self.assertEqual(summary["gps_check"]["ignored"], 0)
        self.assertEqual(summary["gps_check"]["checked"], 6)

    def test_videos_indexed_before_get_their_tracks(self):
        self.configure(gps_tracks={"enabled": False})
        self.scan()
        self.assertEqual(self.reads, [])
        self.assertTrue(all(row["lat"] is None for name, row in self.rows().items()
                            if name != "IMG_0001.MOV"))
        self.configure()
        self.unreadable = self.names[2]
        summary = self.scan()
        tracks_summary = summary["libraries"][0]["backfill"]["tracks"]
        self.assertEqual((tracks_summary["checked"], tracks_summary["found"],
                          tracks_summary["placed"], tracks_summary["failed"]), (7, 4, 4, 1))
        self.assertEqual(tracks_summary["offsets"], {"-08:00": 4})
        self.assertIn("GPS tracks: 4 of 7 video(s) read in Trip have one (4 now have its "
                      "position); by the GPS time their clocks are at UTC-08:00 (4 videos); 1 "
                      "could not be read (tried again next time).", self.log_text())
        rows = self.rows()
        self.assertEqual(rows[self.names[1]]["tz_offset"], "-08:00")
        self.assertIsNone(rows[self.names[2]]["lat"])
        # The one that could not be read is read by the next run, and only it.
        self.unreadable = None
        self.reads[:] = []
        self.scan()
        self.assertEqual(self.reads, [self.names[2]])
        self.assertIsNotNone(self.rows()[self.names[2]]["lat"])

    def test_turning_tracks_off_takes_their_positions_away(self):
        self.configure()
        self.scan()
        self.configure(gps_tracks={"enabled": False})
        self.scan()
        rows = self.rows()
        self.assertTrue(all(row["lat"] is None for name, row in rows.items()
                            if name != "IMG_0001.MOV"))
        self.assertEqual(rows["IMG_0001.MOV"]["lat"], 33.4484)
        self.assertIn("5 video(s) in Trip no longer have the position of their GPS track",
                      self.log_text())
        conn = self.conn()
        self.assertEqual(tracks.routes(conn, self.config, ["lib_trip1"]), [])
        # On again: the kept tracks give them back without reading anything.
        self.configure()
        self.reads[:] = []
        self.scan()
        self.assertEqual(self.reads, [])
        self.assertEqual(sum(1 for row in self.rows().values() if row["lat"] is not None), 6)


class CheckAndEstimatesTest(LibraryCase):

    def test_tracks_are_strong_positions_and_references(self):
        self.configure()
        self.scan()
        conn = self.conn()
        rows = gps_check._load(conn, ["lib_trip1"], self.config)
        by_name = {row["path"]: row for row in rows}
        clip = by_name[self.names[1]]
        self.assertTrue(gps_check.timed_row(clip))
        self.assertEqual(clip["gps"]["GPSTimeStamp"], [12, 1, 0.0])
        self.assertEqual(clip["gps"]["GPSSpeed"], 75.0)
        # A clip of the folder without a fix: its clock is the others'.
        self.assertTrue(by_name[self.names[0]]["gps_clock"])
        self.assertTrue(gps_check.timed_row(by_name[self.names[0]]))
        check = gps_check.evaluate(rows, gps_check.settings(self.config))
        references = check.references()
        clip_id = clip["id"]
        own = [item for item in references if item.id == clip_id and not
               isinstance(item.group, tuple) or (item.id == clip_id and item.group[0] != "track")]
        self.assertEqual(len(own), 1)
        self.assertTrue(own[0].track)
        # (Its middle fix: where the camera was at that moment.)
        self.assertEqual((own[0].lo, own[0].hi, own[0].mid), (START + 90,) * 3)
        self.assertEqual(own[0].tolerance, gps_check.BASE_TOLERANCE_KM)
        points = sorted(item.mid for item in references
                        if item.id == clip_id and item.group[0] == "track")
        self.assertEqual([point - START for point in points],
                         [60, 70, 80, 100, 110, 120])
        self.assertEqual(len({item.group for item in references}), len(references))
        # The phone video is no track.
        phone = [item for item in references if item.id == by_name["IMG_0001.MOV"]["id"]]
        self.assertEqual([item.track for item in phone], [False])
        # The clips without a fix are placed where the car stood (the next
        # clip's start), from its track.
        found = place_estimates.estimate(rows, check, place_estimates.settings(self.config))
        first = found[by_name[self.names[0]]["id"]]
        self.assertEqual(first.source, "time")
        self.assertEqual(first.detail["track"], "all")
        self.assertEqual(first.detail["ids"], [clip_id])
        self.assertAlmostEqual(first.lon, LON, places=5)
        self.assertEqual(place_estimates.reason_text("time", first.detail),
                         "estimated from the GPS track of a video recorded around the same "
                         "time: 3 points of the track after it, the nearest 60 seconds later, "
                         "420 m apart")

    def test_a_dashcams_clock_speaks_for_its_camera_only(self):
        # A dashcam whose clock ignores daylight saving: its track says -08:00
        # while the place is at -07:00. A photo of another camera in the same
        # library, with no offset of its own, does not borrow it.
        rows = []
        for number in range(5):
            start = START + 60 * number
            track = tracks.Facts("k%d" % number, 61, "freegps", start, start + 60, 30.0, LAT,
                                 LON + 0.001 * number, 75.0, 0.6,
                                 [(0.0, LAT, LON, 75), (60.0, LAT, LON + 0.002, 75)])
            local = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=start - 8 * 3600)
            rows.append({"id": number + 1, "library_id": "lib", "taken_at": local.isoformat(),
                         "taken_source": "video", "tz_offset": "-08:00", "camera": "",
                         "media": "video", "content_hash": "h%d" % number, "dhash": "",
                         "lat": LAT, "lon": LON + 0.001 * number, "path": "cam/%d.mp4" % number,
                         "gps": tracks.gps_facts(track), "software": "", "choice": None,
                         "track": track})
        photo = {"id": 99, "library_id": "lib", "taken_at": "2023-08-17T05:02:00",
                 "taken_source": "exif", "tz_offset": "", "camera": "Acme Camera",
                 "media": "image", "content_hash": "photo", "dhash": "", "lat": None,
                 "lon": None, "gps": None, "software": "", "choice": None}
        check = gps_check.evaluate(rows + [photo], gps_check.settings({}))
        self.assertTrue(check.timing(photo)[3])         # (no time zone known: its clock)
        # A clip of the dashcam's folder without a fix borrows it.
        fixless = dict(rows[0], id=50, track=None, gps_clock=True, lat=None, lon=None,
                       gps=None, tz_offset="", taken_at="2023-08-17T04:05:00",
                       path="cam/50.mp4")
        del fixless["track"]
        lo, hi, mid, clock_only = check.timing(fixless)
        self.assertEqual((lo, hi, clock_only), (START + 300, START + 300, False))
        # The same offset from a phone's own clock is the place's: lent.
        phone = [dict(row, track=None, camera="Acme Phone", path="") for row in rows]
        for row in phone:
            del row["track"]
        check = gps_check.evaluate(phone + [photo], gps_check.settings({}))
        self.assertFalse(check.timing(photo)[3])

    def test_two_drives_on_one_road_are_no_frozen_fix(self):
        # The same place at the same speed two hours apart: a commute, not a
        # fix that stood still (each is a fix of its own time).
        rows = []
        for number, start in ((1, START), (2, START + 7200)):
            track = tracks.Facts("k%d" % number, 61, "freegps", start, start + 60, 30.0, LAT,
                                 LON, 75.0, 0.6, [(0.0, LAT, LON - 0.006, 75),
                                                  (60.0, LAT, LON + 0.006, 75)])
            local = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=start - 8 * 3600)
            rows.append({"id": number, "library_id": "lib", "taken_at": local.isoformat(),
                         "taken_source": "video", "tz_offset": "-08:00",
                         "camera": "Acme Dashcam", "media": "video",
                         "content_hash": "h%d" % number,
                         "dhash": "", "lat": LAT, "lon": LON, "gps": tracks.gps_facts(track),
                         "software": "", "choice": None, "track": track})
        check = gps_check.evaluate(rows, gps_check.settings({}))
        self.assertEqual([verdict.ignored for verdict in check.verdicts.values()],
                         [False, False])
        # The same GPS facts as a camera's own (no track): set aside.
        for row in rows:
            del row["track"]
        check = gps_check.evaluate(rows, gps_check.settings({}))
        self.assertEqual([verdict.finding and verdict.finding["kind"]
                          for verdict in check.verdicts.values()], [None, "frozen"])

    def test_a_picture_taken_on_the_way_is_placed_by_the_track(self):
        self.configure()
        self.scan()
        conn = self.conn()
        rows = gps_check._load(conn, ["lib_trip1"], self.config)
        # A made-up photo taken 2:35 into the drive (UTC-08:00), without a position.
        photo = {"id": 999, "library_id": "lib_trip1", "taken_at": "2023-08-17T04:02:35",
                 "taken_source": "exif", "tz_offset": "-08:00", "camera": "Acme Phone",
                 "media": "image", "content_hash": "photo", "dhash": "", "lat": None,
                 "lon": None, "gps": None, "software": "", "choice": None, "path": "p.jpg",
                 "mtime": 0, "duration": None, "place_country": ""}
        rows.append(photo)
        check = gps_check.evaluate(rows, gps_check.settings(self.config))
        found = place_estimates.estimate(rows, check, place_estimates.settings(self.config))
        estimate = found[999]
        self.assertEqual((estimate.source, estimate.detail["track"]), ("time", "all"))
        # (2:35 into the drive is 0:35 into its third clip, which starts
        # where the second ended: 95 seconds of driving.)
        self.assertAlmostEqual(estimate.lon, road(95), places=4)
        self.assertLess(estimate.radius_km, 0.5)


class RoutesTest(LibraryCase):

    def test_the_routes_of_drives(self):
        self.configure()
        self.scan()
        conn = self.conn()
        ids = {row["name"]: row["id"] for row in self.rows().values()}
        found = tracks.routes(conn, self.config, ["lib_trip1"])
        self.assertEqual([route["d"] for route in found], [ids[self.names[1]],
                                                            ids[self.names[5]]])
        first = found[0]
        self.assertEqual(first["l"], "lib_trip1")
        self.assertEqual([clip["i"] for clip in first["c"]],
                         [ids[name] for name in self.names[1:4]])
        self.assertEqual([clip["o"] for clip in first["c"]], [0.0, 60.0, 120.0])
        self.assertEqual(tracks.decode(first["c"][0]["p"])[0], (0.0, LAT, LON, 75))
        # A hidden clip is left out (its route has a gap there).
        conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (ids[self.names[2]],))
        conn.commit()
        found = tracks.routes(conn, self.config, ["lib_trip1"])
        self.assertEqual([clip["i"] for clip in found[0]["c"]],
                         [ids[self.names[1]], ids[self.names[3]]])
        self.assertEqual(len(tracks.routes(conn, self.config, ["lib_trip1"],
                                           ids={ids[self.names[6]]})), 1)
        # A clip with a track in no drive has a route of its own.
        conn.execute("DELETE FROM images WHERE id IN (?, ?)", (ids[self.names[4]],
                                                               ids[self.names[5]]))
        conn.commit()
        lone = tracks.routes(conn, self.config, ["lib_trip1"])[-1]
        self.assertEqual((lone["d"], [clip["o"] for clip in lone["c"]]),
                         (ids[self.names[6]], [0.0]))

    def test_where_drives_start_and_end_is_left_out(self):
        self.configure()
        self.scan()
        conn = self.conn()
        zones = tracks.privacy_zones(conn, self.config, ["lib_trip1"])
        self.assertEqual([(lat, round(lon, 5)) for lat, lon in zones],
                         [(LAT, LON), (LAT, round(road(180), 5)),
                          (LAT, round(road(0, True), 5)), (LAT, round(road(120, True), 5))])
        route = tracks.routes(conn, self.config, ["lib_trip1"])[0]
        # The simplified tracks are straight lines: two points a clip, 1.26 km apart.
        self.assertEqual([len(tracks.decode(clip["p"])) for clip in route["c"]], [2, 2, 2])
        trimmed = tracks.trim_route(route, zones, 500)
        points = [point for clip in trimmed["c"] for point in tracks.decode(clip["p"])]
        for point in points:
            self.assertTrue(tracks.outside(point[1], point[2], zones, 500))
        # The first clip lost its first point, the last its last: they are cut.
        self.assertEqual([(clip["i"], len(tracks.decode(clip["p"])), clip.get("t"))
                          for clip in trimmed["c"]],
                         [(route["c"][0]["i"], 1, 1), (route["c"][1]["i"], 2, None),
                          (route["c"][2]["i"], 1, 1)])
        # A zone in the middle of a clip cuts it in two pieces (its line passes
        # the zone, though its two points are 628 m from it).
        middle = tracks.trim_route(route, [(LAT, road(90))], 500)
        self.assertEqual([(clip["i"], clip.get("t")) for clip in middle["c"]],
                         [(route["c"][0]["i"], None), (route["c"][1]["i"], 1),
                          (route["c"][1]["i"], 1), (route["c"][2]["i"], None)])
        self.assertIsNone(tracks.trim_route(route, zones, 5000))
        self.assertEqual(tracks.trim_route(route, zones, 0), route)
        self.assertTrue(tracks.outside(LAT, LON, zones, 0))


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(LibraryCase):

    def client(self):
        import serve
        app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        app.testing = True
        self.addCleanup(app.extensions["search_images"].close)
        return app.test_client()

    def test_routes_and_the_track_of_a_clip(self):
        self.configure()
        self.scan()
        ids = {row["name"]: row["id"] for row in self.rows().values()}
        client = self.client()
        answer = client.get("/api/routes")
        self.assertEqual(answer.status_code, 200)
        routes = answer.get_json()["routes"]
        self.assertEqual(len(routes), 2)
        self.assertEqual(set(routes[0]), {"d", "l", "c"})
        self.assertEqual(set(routes[0]["c"][0]), {"i", "o", "p"})
        detail = client.get("/api/image/%d" % ids[self.names[1]]).get_json()
        self.assertEqual(detail["track"], {"fixes": 61, "start": "2023-08-17T12:01:00Z",
                                           "end": "2023-08-17T12:02:00Z", "position": True})
        self.assertIsNone(client.get("/api/image/%d" % ids[self.names[0]]).get_json()["track"])
        # Visitors see no hidden clip of a route (nor does the admin: as on the map).
        conn = self.conn()
        conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (ids[self.names[1]],))
        conn.commit()
        visitor = client.get("/api/routes", environ_base={"REMOTE_ADDR": "192.0.2.5"})
        listed = [clip["i"] for route in visitor.get_json()["routes"] for clip in route["c"]]
        self.assertNotIn(ids[self.names[1]], listed)
        self.assertEqual(len(listed), 4)


class ExportTest(LibraryCase):

    def export(self, **options):
        import export
        conn = self.conn()
        summary = export.export_library(export.LogContext(lambda line: None), conn,
                                        common.load_config(), "lib_trip1", options=options)
        data = os.path.join(summary["output"], "data")
        records = []
        for name in sorted(os.listdir(data)):
            if name.startswith("index-"):
                with open(os.path.join(data, name), encoding="ascii") as handle:
                    text = handle.read()
                records.extend(json.loads(text[text.index("(") + 1:text.rindex(")")]))

        def script(name, call):
            path = os.path.join(data, name)
            if not os.path.exists(path):
                return None
            with open(path, encoding="ascii") as handle:
                text = handle.read()
            return json.loads(text[len(call) + 1:text.rindex(")")])
        with open(os.path.join(data, "manifest.js"), encoding="ascii") as handle:
            manifest_text = handle.read()
        manifest = json.loads(manifest_text[manifest_text.index("{"):manifest_text.rindex("}")
                                            + 1])
        return (records, script("locations.js", "SEARCH_DATA.locations"),
                script("routes.js", "SEARCH_DATA.routes"), manifest, summary)

    def prepare(self):
        import places
        import test_places
        self.configure()
        self.addCleanup(places.release)
        # A made-up town near the made-up road, for place names.
        test_places.install_places(towns=[(1, "Seaview", LAT - 0.01, LON + 0.02, "PPL", "FJ",
                                           "N", 1500)])
        self.addCleanup(places.release)
        self.scan()
        import prompts
        conn = self.conn()
        conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps({"title": "Road", "description": "A road.", "tags": [],
                                  "photo_type": "photo", "sensitive": "none",
                                  "private_info": False}), prompts.PROMPT_VER))
        conn.execute("UPDATE images SET info = ? WHERE name = ?", (json.dumps(
            {"title": "Road", "description": "A road.", "tags": [], "photo_type": "photo",
             "sensitive": "none", "private_info": False,
             "text": "DASHCAM 2023/08/17 04:02:35 S:41.20000 W:150.28100 47MPH"}),
            self.names[2]))
        conn.commit()

    def test_options(self):
        import export
        self.assertEqual(export.validate_options({})["routes"], False)
        self.assertEqual(export.validate_options({})["routes_trim"], 500)
        self.assertEqual(export.validate_options({"routes": True, "routes_trim": 0})
                         ["routes_trim"], 0)
        for bad in (-1, 5001, 1.5, True, "500"):
            with self.assertRaises(export.OptionsError):
                export.validate_options({"routes_trim": bad})
        with self.assertRaises(export.OptionsError):
            export.validate_options({"routes": 1})
        self.assertNotIn("routes", export.options_summary(export.validate_options(
            {"routes": True})))
        self.assertTrue(export.options_summary(export.validate_options(
            {"routes": True, "location": True}))["routes"])

    def test_no_routes_without_the_option(self):
        self.prepare()
        # (What the model read off the overlay, see prepare, and the drive's.)
        conn = self.conn()
        found = drives.library_drives(conn, "lib_trip1", self.config)
        drives.save_description("lib_trip1", found[0], {
            "title": "Out of town",
            "description": "The overlay reads S:41.20000 W:150.29000 at first",
            "tags": []}, {})
        records, points, routes, manifest, _summary = self.export(location=True)
        texts = [record["o"] for record in records if record.get("o")]
        self.assertEqual(texts, ["DASHCAM 2023/08/17 04:02:35 47MPH"])
        self.assertEqual([record["dvd"] for record in records if record.get("dvd")],
                         ["The overlay reads at first"])
        self.assertIsNone(routes)
        self.assertNotIn("routes", manifest)
        # The clips keep their place names but not their positions (nor how far
        # the town is); the phone video keeps its own.
        with_position = [record for record in records if record.get("gp")]
        self.assertEqual(len(with_position), 1)
        self.assertEqual(len(points), 1)
        clips = [record for record in records if record.get("ci") == "Seaview"]
        self.assertEqual(len(clips), 5)
        self.assertFalse(any("km" in record or "gp" in record for record in clips))
        # (The 7 clips are a dashcam's -- the phone video beside them is not --
        # and 6 of them may show where a drive starts or ends: the 2 without a
        # fix, the first and last with one of each drive.)
        self.assertEqual(self.review(location=True)["routes"],
                         {"on": False, "positions": 5, "withheld": 5, "routes": 0,
                          "overlay": 7, "near": 6})

    def review(self, **options):
        import export
        return export.preview_export(self.conn(), common.load_config(), "lib_trip1",
                                    options=options)["counts"]

    def test_routes_without_where_drives_start_and_end(self):
        self.prepare()
        records, points, routes, manifest, summary = self.export(location=True, routes=True)
        self.assertEqual(manifest["routes"], "routes.js")
        self.assertTrue(manifest["options"]["routes"])
        by_id = {record["i"]: record for record in records}
        self.assertEqual(len(routes), 2)
        zones = [(LAT, road(0)), (LAT, road(180)), (LAT, road(0, True)),
                 (LAT, road(120, True))]
        for route in routes:
            self.assertIn(route["d"], by_id)                       # export ids
            self.assertEqual(route["l"], manifest["libraries"][0]["id"])
            for clip in route["c"]:
                self.assertIn(clip["i"], by_id)
                for _at, lat, lon, _speed in tracks.decode(clip["p"]):
                    self.assertTrue(tracks.outside(lat, lon, zones, 500))
                    self.assertEqual(round(lon, 4), lon)            # (rounded to 4 decimals)
        # Their positions (the middle of each clip: 628 m from where the clip
        # began) are all further than 500 m from where the drives start and end.
        positioned = [record for record in records if record.get("gp")]
        self.assertEqual(len(positioned), 1 + 5)
        self.assertEqual(len(points), 1 + 5)
        # (Their texts without positions: the route shows where they were.)
        self.assertEqual([record["o"] for record in records if record.get("o")],
                         ["DASHCAM 2023/08/17 04:02:35 47MPH"])
        counts = self.review(location=True, routes=True)
        self.assertEqual(counts["routes"], {"on": True, "positions": 5, "withheld": 0,
                                            "routes": 2, "overlay": 7, "near": 6})
        # 700 m: only the middle clip of the first drive keeps its position.
        records, points, routes, _manifest, _summary = self.export(
            location=True, routes=True, routes_trim=700)
        self.assertEqual(len([record for record in records if record.get("gp")]), 1 + 1)
        self.assertEqual(len(points), 1 + 1)
        self.assertEqual(self.review(location=True, routes=True, routes_trim=700)["routes"],
                         {"on": True, "positions": 5, "withheld": 4, "routes": 2,
                          "overlay": 7, "near": 6})
        del summary

    def test_estimates_from_tracks_go_with_the_routes(self):
        self.prepare()
        conn = self.conn()
        gps_check.run(conn, self.config)
        estimated = conn.execute("SELECT image_id, detail FROM place_estimates").fetchall()
        self.assertEqual(len(estimated), 2)
        self.assertTrue(all(json.loads(row[1])["track"] == "all" for row in estimated))
        # The viewer is told so (its label: "estimated from a video's GPS track").
        row = conn.execute("SELECT * FROM images WHERE id = ?", (estimated[0][0],)).fetchone()
        view = place_estimates.view(conn, row)
        self.assertEqual((view["source"], view["track"]), ("time", "all"))
        self.assertTrue(view["reason"].startswith("estimated from the GPS track of a video"),
                        view["reason"])
        _records, points, _routes, _manifest, _summary = self.export(
            location=True, estimated_places=True)
        self.assertEqual([point for point in points if point.get("e")], [])
        # With the routes, still not within 500 m of where the drives start.
        _records, points, _routes, _manifest, _summary = self.export(
            location=True, estimated_places=True, routes=True)
        self.assertEqual([point for point in points if point.get("e")], [])
        _records, points, _routes, _manifest, _summary = self.export(
            location=True, estimated_places=True, routes=True, routes_trim=0)
        self.assertEqual(len([point for point in points if point.get("e")]), 2)

    def set_text(self, name, text):
        conn = self.conn()
        conn.execute("UPDATE images SET info = ? WHERE name = ?", (json.dumps(
            {"title": "Road", "description": "A road.", "tags": [], "photo_type": "photo",
             "sensitive": "none", "private_info": False, "text": text}), name))
        conn.commit()

    def test_the_texts_of_a_dashcams_clips_never_hold_positions(self):
        self.prepare()
        # The first clip had no fix: its overlay prints the last position the
        # camera had -- where the drive starts. The second's first frames are
        # there too, though its middle (its position) is further.
        home = "S:41.20000 W:150.30000"
        self.set_text(self.names[0], "DASHCAM 2023/08/17 04:00:30 %s 0MPH" % home)
        self.set_text(self.names[1], "DASHCAM 2023/08/17 04:01:00 %s 0MPH" % home)
        # (A phone video beside them keeps its words: it is no dashcam.)
        self.set_text("IMG_0001.MOV", "A sign: 41.20000, -150.30000")
        for options in ({"location": True}, {"location": True, "routes": True},
                        {"location": True, "routes": True, "routes_trim": 0}):
            records, _points, _routes, _manifest, _summary = self.export(**options)
            texts = sorted(record["o"] for record in records if record.get("o"))
            self.assertEqual(texts, ["A sign: 41.20000, -150.30000",
                                     "DASHCAM 2023/08/17 04:00:30 0MPH",
                                     "DASHCAM 2023/08/17 04:01:00 0MPH",
                                     "DASHCAM 2023/08/17 04:02:35 47MPH"], options)

    def test_an_accepted_place_from_a_track_goes_as_the_track(self):
        self.prepare()
        conn = self.conn()
        gps_check.run(conn, self.config)
        first = conn.execute("SELECT id FROM images WHERE name = ?",
                             (self.names[0],)).fetchone()[0]
        self.assertEqual(json.loads(place_estimates.for_picture(conn, first)["detail"])["track"],
                         "all")
        with conn:
            place_estimates.set_choice(conn, first, "accept")
        gps_check.run(conn, self.config)
        owner = place_estimates.for_picture(conn, first)
        self.assertEqual((owner["source"], json.loads(owner["detail"])), ("owner", {"track": "all"}))
        # Where the car stood when the drive began: not with Location alone, nor
        # with the routes near where a drive starts.
        for options in ({"location": True}, {"location": True, "routes": True}):
            _records, points, _routes, _manifest, _summary = self.export(**options)
            self.assertEqual([point for point in points if point.get("o")], [], options)
        _records, points, _routes, _manifest, _summary = self.export(
            location=True, routes=True, routes_trim=0)
        self.assertEqual(len([point for point in points if point.get("o")]), 1)
        # A place the owner set by hand is the owner's to publish.
        with conn:
            place_estimates.set_choice(conn, first, "set", LAT - 0.05, LON)
        gps_check.run(conn, self.config)
        _records, points, _routes, _manifest, _summary = self.export(location=True)
        self.assertEqual(len([point for point in points if point.get("o")]), 1)

    def test_where_another_librarys_drives_start_is_left_out_too(self):
        self.prepare()
        phone_folder = self.base.library_folder("Phone")
        self.base.write_config([support.library("Trip", self.folder, id="lib_trip1"),
                                support.library("Phone", phone_folder, id="lib_phone1")])
        self.config = common.load_config()
        conn = self.conn()
        import places
        import prompts
        from PIL import Image
        Image.new("RGB", (40, 30), (20, 120, 200)).save(os.path.join(phone_folder, "a.jpg"),
                                                         "JPEG")
        # A photo without a position in another library, taken 5 seconds into
        # the first drive's driving: placed by the track, 100 m from its start.
        values = {"library_id": "lib_phone1", "path": "a.jpg", "name": "a.jpg", "size": 100,
                  "mtime": 0, "ctime": 0, "taken_at": "2023-08-17T05:01:05",
                  "taken_source": "exif", "tz_offset": "-07:00", "camera": "Acme Phone",
                  "lat": None, "lon": None, "exif": "{}", "content_hash": "phone1",
                  "added": 0, "updated": 0, "described": 1, "prompt_ver": prompts.PROMPT_VER,
                  "info": json.dumps({"title": "Driveway", "description": "A car.",
                                      "photo_type": "photo", "tags": ["car"],
                                      "sensitive": "none", "private_info": False})}
        values.update(places.empty_columns())
        columns = sorted(values)
        photo = conn.execute("INSERT INTO images (%s) VALUES (%s)" % (
            ", ".join(columns), ",".join("?" * len(columns))),
            [values[column] for column in columns]).lastrowid
        conn.commit()
        gps_check.run(conn, self.config)
        found = place_estimates.for_picture(conn, photo)
        self.assertEqual(json.loads(found["detail"])["track"], "all")
        self.assertLess(places.distance_km(found["lat"], found["lon"], LAT, road(0)), 0.5)
        import export
        for options, expected in (({"routes": True}, 0), ({"routes": True, "routes_trim": 0}, 1),
                                  ({}, 0)):
            options = dict(options, location=True, estimated_places=True, images="none")
            summary = export.export_library(export.LogContext(lambda line: None), conn,
                                            common.load_config(), "lib_phone1", options=options)
            with open(os.path.join(summary["output"], "data", "locations.js"),
                      encoding="ascii") as handle:
                points = json.loads(handle.read()[len("SEARCH_DATA.locations("):].rsplit(
                    ")", 1)[0])
            self.assertEqual(len([point for point in points if point.get("e")]), expected,
                             options)


@unittest.skipIf(NODE is None, "Node.js is not installed")
class NodeTest(unittest.TestCase):

    def test_the_pages(self):
        # tests/js/routes_test.js: routes.js, the map's lines and the viewer's map.
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "routes_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
