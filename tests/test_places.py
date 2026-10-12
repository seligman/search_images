"""Tests for places.py and make_places.py: place names from GPS positions.

The GeoNames files are made up here: a few dozen real towns with their
positions typed in (write_geonames). Downloads come from a fake server on
127.0.0.1; nothing goes over the network.
"""

import http.server
import io
import math
import os
import random
import socket
import sqlite3
import threading
import time
import unittest
import zipfile
from unittest import mock

import support

import common
import db
import jobs
import make_places
import places
import search_index

quiet = support.quiet

# (geonameid, name, lat, lon, feature code, cc, admin1, population)
TOWNS = [
    (1, "Kahului", 20.8893, -156.4729, "PPL", "US", "HI", 26337),
    (2, "Wailuku", 20.8911, -156.5047, "PPLA2", "US", "HI", 15313),
    (3, "Honolulu", 21.3069, -157.8583, "PPLA", "US", "HI", 350964),
    (4, "Hilo", 19.7297, -155.0900, "PPL", "US", "HI", 43263),
    (5, "San Francisco", 37.7749, -122.4194, "PPLA2", "US", "CA", 864816),
    (6, "Oakland", 37.8044, -122.2712, "PPLA2", "US", "CA", 419267),
    (7, "Los Angeles", 34.0522, -118.2437, "PPLA2", "US", "CA", 3971883),
    (8, "New York City", 40.7143, -74.0060, "PPL", "US", "NY", 8804190),
    (9, "London", 51.5085, -0.1257, "PPLC", "GB", "ENG", 8961989),
    (10, "Edinburgh", 55.9521, -3.1965, "PPLA", "GB", "SCT", 506520),
    (11, "Paris", 48.8534, 2.3488, "PPLC", "FR", "11", 2138551),
    (12, "Paris 06 Luxembourg", 48.8491, 2.3328, "PPLX", "FR", "11", 40916),
    (13, "Nice", 43.7031, 7.2661, "PPLA2", "FR", "93", 342669),
    (14, "Z\u00fcrich", 47.3667, 8.5500, "PPLA", "CH", "ZH", 341730),
    (15, "Berlin", 52.5244, 13.4105, "PPLC", "DE", "16", 3426354),
    (16, "M\u00fcnchen", 48.1374, 11.5755, "PPLA", "DE", "02", 1260391),
    (17, "Tokyo", 35.6895, 139.6917, "PPLC", "JP", "40", 8336599),
    (18, "Sydney", -33.8679, 151.2073, "PPLA", "AU", "02", 4627345),
    (19, "Reykjav\u00edk", 64.1355, -21.8954, "PPLC", "IS", "39", 118918),
    (20, "Suva", -18.1416, 178.4415, "PPLC", "FJ", "C", 77366),
    (21, "Labasa", -16.4167, 179.3833, "PPLA", "FJ", "N", 27949),
    (22, "Somosomo", -16.7731, -179.9681, "PPL", "FJ", "N", 1500),
    (23, "Longyearbyen", 78.2186, 15.6401, "PPLA", "SJ", "21", 2060),
    (24, "Cape Town", -33.9258, 18.4232, "PPLA", "ZA", "11", 3433441),
    (25, "Rio de Janeiro", -22.9028, -43.2075, "PPLA", "BR", "21", 6023699),
    (26, "Quito", -0.2299, -78.5250, "PPLC", "EC", "18", 1399814),
    (27, "Singapore", 1.2897, 103.8501, "PPLC", "SG", "00", 3547809),
    (28, "Monaco", 43.7333, 7.4167, "PPLC", "MC", "", 32965),
    (29, "Old Kahului", 20.8800, -156.4600, "PPLH", "US", "HI", 5000),
    (30, "Ushuaia", -54.8019, -68.3030, "PPLA", "AR", "23", 58028),
]

REGIONS = {"US.HI": "Hawaii", "US.CA": "California", "US.NY": "New York",
           "GB.ENG": "England", "GB.SCT": "Scotland", "FR.11": "\u00cele-de-France",
           "FR.93": "Provence-Alpes-C\u00f4te d'Azur", "CH.ZH": "Zurich", "DE.16": "Berlin",
           "DE.02": "Bavaria", "JP.40": "Tokyo", "AU.02": "New South Wales",
           "IS.39": "Capital Region", "FJ.C": "Central", "FJ.N": "Northern",
           "SJ.21": "Svalbard", "ZA.11": "Western Cape", "BR.21": "Rio de Janeiro",
           "EC.18": "Pichincha", "AR.23": "Tierra del Fuego"}

COUNTRIES = {"US": "United States", "GB": "United Kingdom", "FR": "France",
             "CH": "Switzerland", "DE": "Germany", "JP": "Japan", "AU": "Australia",
             "IS": "Iceland", "FJ": "Fiji", "SJ": "Svalbard and Jan Mayen",
             "ZA": "South Africa", "BR": "Brazil", "EC": "Ecuador", "SG": "Singapore",
             "MC": "Monaco", "AR": "Argentina"}


def cities_text(towns=TOWNS):
    """cities1000.txt as GeoNames writes it (19 tab-separated columns)."""
    lines = []
    for geonameid, name, lat, lon, code, cc, admin1, population in towns:
        ascii_name = name.encode("ascii", "replace").decode("ascii")
        lines.append("\t".join([str(geonameid), name, ascii_name, "", "%.5f" % lat,
                                "%.5f" % lon, "P", code, cc, "", admin1, "", "", "",
                                str(population), "", "10", "Etc/UTC", "2024-01-01"]))
    # A line that is not a town, and a broken one.
    lines.append("\t".join(["99", "Mount Nowhere", "Mount Nowhere", "", "10.0", "10.0", "T",
                            "MT", "US", "", "HI", "", "", "", "0", "", "", "", ""]))
    lines.append("not a town")
    return "\n".join(lines) + "\n"


def regions_text():
    return "".join("%s\t%s\t%s\t1\n" % (code, name, name) for code, name in REGIONS.items())


def countries_text():
    lines = ["# GeoNames country information (made up for a test)",
             "#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital"]
    for cc, name in COUNTRIES.items():
        lines.append("\t".join([cc, cc + "X", "0", cc, name, "", "0", "0", "XX", "", "", "",
                                "", "", "", "", "0", "", ""]))
    return "\n".join(lines) + "\n"


def write_geonames(folder, as_zip=True, towns=TOWNS):
    """The three GeoNames files (made up) in folder; returns folder."""
    os.makedirs(folder, exist_ok=True)
    if as_zip:
        with zipfile.ZipFile(os.path.join(folder, places.CITIES_FILE), "w",
                             zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(places.CITIES_TEXT, cities_text(towns))
    else:
        with open(os.path.join(folder, places.CITIES_TEXT), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write(cities_text(towns))
    for name, text in ((places.REGIONS_FILE, regions_text()),
                       (places.COUNTRIES_FILE, countries_text())):
        with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return folder


def install_places(towns=TOWNS):
    """Build data/places/places.db of the current SETTINGS_BASE from the made-up towns."""
    write_geonames(places.places_dir(), towns=towns)
    places.build(ctx=jobs.JobContext("test"))
    places.release()


def brute_nearest(lat, lon, towns):
    best = None
    for town in towns:
        km = places.distance_km(lat, lon, town[2], town[3])
        if best is None or km < best[1]:
            best = (town, km)
    return best


USED = [town for town in TOWNS if town[4] not in places.SKIPPED_FEATURES]


class GeometryTest(unittest.TestCase):

    def test_distance(self):
        # Paris to London: about 344 km.
        self.assertAlmostEqual(places.distance_km(48.8534, 2.3488, 51.5085, -0.1257), 343.9,
                               delta=1.5)
        self.assertEqual(places.distance_km(10, 20, 10, 20), 0)
        # Across the date line, the short way round.
        self.assertLess(places.distance_km(0, 179.9, 0, -179.9), 23)

    def test_cells_cover_the_circle(self):
        rng = random.Random(7)
        for _ in range(300):
            lat = rng.uniform(-89.9, 89.9)
            lon = rng.uniform(-180, 180)
            km = rng.choice((5, 25, 100, 250))
            cells = set(places.cells_near(lat, lon, km))
            for _ in range(20):
                # A point at most km away, in some direction.
                bearing = rng.uniform(0, 2 * math.pi)
                angle = rng.uniform(0, km) / places.EARTH_KM
                phi, lam = math.radians(lat), math.radians(lon)
                phi2 = math.asin(math.sin(phi) * math.cos(angle)
                                 + math.cos(phi) * math.sin(angle) * math.cos(bearing))
                lam2 = lam + math.atan2(math.sin(bearing) * math.sin(angle) * math.cos(phi),
                                        math.cos(angle) - math.sin(phi) * math.sin(phi2))
                lat2 = math.degrees(phi2)
                lon2 = (math.degrees(lam2) + 540) % 360 - 180
                self.assertIn(places.cell_of(lat2, lon2), cells, (lat, lon, km, lat2, lon2))

    def test_cell_edges(self):
        self.assertEqual(places.cell_of(90, 180), places.cell_of(90, -180))
        self.assertEqual(places.cell_of(-90, 0) // places.COLUMNS, 0)
        self.assertEqual(places.cell_of(90, 0) // places.COLUMNS, places.ROWS - 1)
        # Near a pole the circle takes in every longitude.
        self.assertEqual(len(places.cells_near(89.9, 10, 50)) % places.COLUMNS, 0)


class BuildTest(support.TempBaseMixin, unittest.TestCase):

    def test_build_from_the_zip_and_from_the_text(self):
        for as_zip in (True, False):
            folder = write_geonames(self.base.join("geo-%s" % as_zip), as_zip=as_zip)
            out = self.base.join("places-%s.db" % as_zip)
            summary = places.build(folder, out, ctx=jobs.JobContext("t"))
            self.assertEqual(summary["cities"], len(USED))
            index = places.PlaceIndex(out)
            self.assertEqual(len(index), len(USED))
            names = set(index.names)
            # Parts of towns and places that are no more are left out.
            self.assertNotIn("Paris 06 Luxembourg", names)
            self.assertNotIn("Old Kahului", names)
            self.assertNotIn("Mount Nowhere", names)
            self.assertIn("Z\u00fcrich", names)
            self.assertEqual(places.read_meta(out)["format"], str(places.PLACES_FORMAT))

    def test_missing_files_and_no_towns(self):
        folder = self.base.make_dir("empty")
        ctx = jobs.JobContext("t")
        with self.assertRaises(places.PlacesError) as caught:
            places.build(folder, self.base.join("x.db"), ctx)
        self.assertIn("missing", str(caught.exception))
        write_geonames(folder, towns=[])
        with self.assertRaises(places.PlacesError):
            places.build(folder, self.base.join("x.db"), ctx)
        self.assertFalse(os.path.exists(self.base.join("x.db")))

    def test_a_broken_zip_is_reported(self):
        folder = write_geonames(self.base.join("geo"))
        with open(os.path.join(folder, places.CITIES_FILE), "wb") as handle:
            handle.write(b"PK not really")
        with self.assertRaises(places.PlacesError):
            places.build(folder, self.base.join("x.db"), jobs.JobContext("t"))

    def test_rebuild_replaces_the_file(self):
        install_places()
        first = places.shared()
        self.assertEqual(len(first), len(USED))
        install_places(TOWNS[:5])
        # (shared() looks at the file again only now and then.)
        places.release()
        self.assertEqual(len(places.shared()), 5)
        places.release()

    def test_an_unreadable_index_is_no_index(self):
        common.ensure_dir(places.places_dir())
        with open(places.places_db_path(), "wb") as handle:
            handle.write(b"not a database")
        places.release()
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertIsNone(places.shared())
        self.assertEqual(places.place_columns(20.9, -156.5)["place_km"], None)
        state = places.status()
        self.assertIn("cannot be used", state["error"])
        # Without the downloaded files it cannot be built again here.
        self.assertFalse(state["rebuild"])
        self.assertIn("download the place names again", state["error"])
        places.release()

    def test_files_that_look_cut_off_are_not_used(self):
        # A region file that arrived in part leaves most towns without
        # their region; a country file in part, without their country.
        for name, keep, what in ((places.REGIONS_FILE, 7, "region"),
                                 (places.COUNTRIES_FILE, 4, "country")):
            folder = write_geonames(self.base.join("cut-" + what))
            path = os.path.join(folder, name)
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines(True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.writelines(lines[:keep])
            out = self.base.join("cut-%s.db" % what)
            with self.assertRaises(places.PlacesError) as caught:
                places.build(folder, out, jobs.JobContext("t"))
            self.assertIn("looks incomplete", str(caught.exception))
            self.assertIn("in a %s it does not name" % what, str(caught.exception))
            self.assertFalse(os.path.exists(out))
        # A few towns in a region the file does not list are fine (the real
        # files have some).
        self.assertIsNone(places.check_names(
            [("A", 0, 0, "US", "HI", 1)] * 9 + [("B", 0, 0, "US", "XX", 1)],
            {"US": "United States"}, {"US.HI": "Hawaii"}))

    def test_an_index_that_cannot_be_written_is_a_places_error(self):
        folder = write_geonames(self.base.join("geo"))
        blocker = self.base.join("blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("a file where a folder should be")
        with self.assertRaises(places.PlacesError) as caught:
            places.build(folder, os.path.join(blocker, "places.db"), jobs.JobContext("t"))
        self.assertIn("Could not write the place index", str(caught.exception))


class LookupTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        install_places()
        self.index = places.shared()
        self.addCleanup(places.release)

    def test_nearest_is_the_true_nearest(self):
        rng = random.Random(11)
        points = [(rng.uniform(-80, 80), rng.uniform(-180, 180)) for _ in range(400)]
        # And points near the towns, where it matters most.
        for town in USED:
            for _ in range(5):
                points.append((town[2] + rng.uniform(-1.5, 1.5),
                               (town[3] + rng.uniform(-1.5, 1.5) + 540) % 360 - 180))
        for lat, lon in points:
            expected = brute_nearest(lat, lon, USED)
            found = self.index.nearest(lat, lon)
            if expected[1] > places.MAX_KM:
                self.assertIsNone(found, (lat, lon))
                continue
            self.assertIsNotNone(found, (lat, lon))
            self.assertEqual(self.index.names[found[0]], expected[0][1], (lat, lon))
            self.assertAlmostEqual(found[1], expected[1], places=6)

    def test_a_town_names_the_place(self):
        columns = places.place_columns(20.8800, -156.4600)
        self.assertEqual(columns["place_city"], "Kahului")
        self.assertEqual(columns["place_region"], "Hawaii")
        self.assertEqual(columns["place_country"], "United States")
        self.assertEqual(columns["place_cc"], "US")
        self.assertAlmostEqual(columns["place_km"], 1.6, delta=0.2)

    def test_far_from_a_town_only_region_and_country(self):
        # About 90 km off the coast of Hawaii's Big Island, nearest Hilo.
        columns = places.place_columns(20.3, -154.6)
        self.assertEqual(columns["place_city"], "")
        self.assertEqual(columns["place_region"], "Hawaii")
        self.assertEqual(columns["place_country"], "United States")
        self.assertGreater(columns["place_km"], places.CITY_KM)

    def test_no_town_anywhere_near(self):
        for lat, lon in ((0.0, -140.0), (89.9, 0.0), (-89.0, 120.0)):
            columns = places.place_columns(lat, lon)
            self.assertEqual(columns, places.far_columns(), (lat, lon))
            self.assertEqual(columns["place_km"], -1)

    def test_across_the_date_line_and_near_the_poles(self):
        columns = places.place_columns(-16.78, 179.99)
        self.assertEqual(columns["place_city"], "Somosomo")
        self.assertLess(columns["place_km"], 5)
        self.assertEqual(places.place_columns(78.0, 15.0)["place_city"], "Longyearbyen")

    def test_regions_that_are_not_known(self):
        columns = places.place_columns(1.30, 103.85)
        self.assertEqual((columns["place_city"], columns["place_region"],
                          columns["place_country"]), ("Singapore", "", "Singapore"))
        self.assertEqual(places.place_columns(43.73, 7.42)["place_region"], "")

    def test_positions_that_cannot_be_used(self):
        self.assertEqual(places.place_columns(None, None), places.empty_columns())
        self.assertEqual(places.place_columns(0.0, 0.0)["place_km"], -1)
        self.assertEqual(places.place_columns(95.0, 10.0)["place_km"], -1)

    def test_without_place_names_nothing_is_looked_up(self):
        os.remove(places.places_db_path())
        places.release()
        self.assertIsNone(places.shared())
        self.assertEqual(places.place_columns(20.88, -156.46), places.empty_columns())

    def test_lookup_speed(self):
        rng = random.Random(3)
        towns = [(n, "Town %d" % n, rng.uniform(35, 60), rng.uniform(-10, 30), "PPL", "DE",
                  "02", 2000) for n in range(1, 30001)]
        folder = write_geonames(self.base.join("dense"), towns=towns)
        out = self.base.join("dense.db")
        places.build(folder, out, jobs.JobContext("t"))
        index = places.PlaceIndex(out)
        import time
        started = time.perf_counter()
        for _ in range(2000):
            index.nearest(rng.uniform(36, 59), rng.uniform(-9, 29))
        per_lookup = (time.perf_counter() - started) / 2000
        self.assertLess(per_lookup, 0.01)


class FakeGeoNames:
    """A web server on 127.0.0.1 serving made-up GeoNames files."""

    def __init__(self, folder, missing=()):
        files = {}
        for name in places.FILES:
            if name in missing:
                continue
            with open(os.path.join(folder, name), "rb") as handle:
                files["/dump/" + name] = handle.read()
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("User-Agent")))
                data = files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/dump/" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BrokenGeoNames:
    """A server on 127.0.0.1 whose reply for one file goes wrong (mode):

    "short" -- says how long the file is, sends a third, then hangs up;
    "chunked" -- a chunked reply that breaks off; "garbage" -- not HTTP;
    "cut" -- a third of the file, said to be all of it (a file that was
    cut off at the source).
    """

    def __init__(self, folder, mode, broken):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                with open(os.path.join(folder, name), "rb") as handle:
                    data = handle.read()
                if name == broken and mode == "garbage":
                    self.wfile.write(b"garbage\r\n\r\n")
                    self.close_connection = True
                    return
                if name == broken and mode == "chunked":
                    self.protocol_version = "HTTP/1.1"
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"%x\r\n%s\r\n" % (100, data[:100]))
                    self.wfile.write(b"%x\r\n%s" % (500, data[100:150]))
                    self.hang_up()
                    return
                if name == broken and mode == "cut":
                    data = data[:len(data) // 3]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if name == broken and mode == "short":
                    self.wfile.write(data[:len(data) // 3])
                    self.hang_up()
                    return
                self.wfile.write(data)

            def hang_up(self):
                self.wfile.flush()
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.close_connection = True

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=owner.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/dump/" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DownloadTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.source = write_geonames(self.base.join("source"))
        self.addCleanup(places.release)

    def serve(self, missing=()):
        server = FakeGeoNames(self.source, missing)
        self.addCleanup(server.close)
        return server

    def test_download_and_build(self):
        server = self.serve()
        ctx = jobs.JobContext("t")
        summary = places.update(ctx, base_url=server.url)
        self.assertEqual(summary["cities"], len(USED))
        self.assertEqual(sorted(path for path, _agent in server.requests),
                         sorted("/dump/" + name for name in places.FILES))
        self.assertTrue(all(agent == places.USER_AGENT for _path, agent in server.requests))
        self.assertTrue(places.have_files())
        self.assertFalse([name for name in os.listdir(places.places_dir())
                          if name.endswith(".part") or ".tmp" in name])
        self.assertEqual(places.read_meta()["source"], server.url)

    def test_a_missing_file_keeps_what_was_there(self):
        install_places()
        before = os.path.getsize(places.places_db_path())
        server = self.serve(missing=(places.REGIONS_FILE,))
        with self.assertRaises(places.PlacesError) as caught:
            places.update(jobs.JobContext("t"), base_url=server.url)
        self.assertIn("404", str(caught.exception))
        self.assertEqual(os.path.getsize(places.places_db_path()), before)

    def test_nothing_listening(self):
        with self.assertRaises(places.PlacesError):
            places.download(self.base.join("x"), jobs.JobContext("t"),
                            base_url="http://127.0.0.1:9/", timeout=5)

    def test_stopping_a_download(self):
        server = self.serve()
        ctx = jobs.JobContext("t")
        ctx.request_stop()
        with self.assertRaises(places.PlacesError):
            places.download(self.base.join("x"), ctx, base_url=server.url)
        self.assertFalse(os.path.exists(self.base.join("x", places.CITIES_FILE)))

    def test_a_reply_that_breaks_off_keeps_what_was_there(self):
        install_places()
        before = places.read_meta()["built"]
        with open(os.path.join(places.places_dir(), places.REGIONS_FILE), "rb") as handle:
            regions = handle.read()
        cases = (("short", places.REGIONS_FILE, "arrived incomplete"),
                 ("chunked", places.COUNTRIES_FILE, "connection broke off"),
                 ("garbage", places.COUNTRIES_FILE, "connection broke off"),
                 ("cut", places.REGIONS_FILE, "looks incomplete"))
        for mode, broken, words in cases:
            server = BrokenGeoNames(self.source, mode, broken)
            self.addCleanup(server.close)
            with self.assertRaises(places.PlacesError) as caught:
                places.update(jobs.JobContext("t"), base_url=server.url)
            self.assertIn(words, str(caught.exception), mode)
            # The place index is the one from before.
            self.assertEqual(places.read_meta()["built"], before, mode)
            self.assertFalse([name for name in os.listdir(places.places_dir())
                              if name.endswith(".part") or ".tmp" in name], mode)
            if mode != "cut":
                # (A file that broke off is not kept either.)
                with open(os.path.join(places.places_dir(), places.REGIONS_FILE),
                          "rb") as handle:
                    self.assertEqual(handle.read(), regions, mode)

    def test_the_command_line_reports_a_broken_download(self):
        server = BrokenGeoNames(self.source, "garbage", places.COUNTRIES_FILE)
        self.addCleanup(server.close)
        output = io.StringIO()
        with mock.patch.object(places, "GEONAMES_URL", server.url), \
                mock.patch("sys.stdout", output):
            self.assertEqual(make_places.main(["--download", "--no-label"]), 1)
        self.assertIn("Error: Could not download", output.getvalue())


class LabelTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips001"),
                                support.library("Other", self.folder, id="lib_other001")])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(places.release)

    def add(self, path, lat, lon, library_id="lib_trips001", **fields):
        values = {"library_id": library_id, "path": path, "name": path, "size": 1,
                  "mtime": 1, "lat": lat, "lon": lon, "updated": 1.0}
        values.update(fields)
        columns = sorted(values)
        image_id = self.conn.execute(
            "INSERT INTO images (%s) VALUES (%s)" % (",".join(columns),
                                                     ",".join("?" * len(columns))),
            [values[column] for column in columns]).lastrowid
        self.conn.commit()
        return image_id

    def row(self, image_id):
        return self.conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()

    def test_without_place_names(self):
        self.add("a.jpg", 20.88, -156.46)
        self.assertIsNone(places.label_pictures(self.conn, jobs.JobContext("t")))

    def test_labels_pictures_not_looked_up(self):
        install_places()
        maui = self.add("maui.jpg", 20.88, -156.46)
        sea = self.add("sea.jpg", 0.0, -140.0)
        coast = self.add("coast.jpg", 20.3, -154.6)
        nowhere = self.add("no-gps.jpg", None, None)
        gone = self.add("gone.jpg", 48.85, 2.35, missing_since=5.0)
        other = self.add("paris.jpg", 48.85, 2.35, library_id="lib_other001")
        ctx = jobs.JobContext("t")
        summary = places.label_pictures(self.conn, ctx, ["lib_trips001"])
        self.assertEqual(summary, {"checked": 3, "named": 1, "region_only": 1, "far": 1,
                                   "aborted": False})
        row = self.row(maui)
        self.assertEqual((row["place_city"], row["place_region"], row["place_country"],
                          row["place_cc"]), ("Kahului", "Hawaii", "United States", "US"))
        self.assertGreater(row["updated"], 1.0)
        self.assertEqual(self.row(sea)["place_km"], -1)
        self.assertEqual(self.row(coast)["place_city"], "")
        self.assertIsNone(self.row(nowhere)["place_km"])
        self.assertIsNone(self.row(gone)["place_km"])
        self.assertIsNone(self.row(other)["place_km"])
        # Done once: nothing left to do.
        self.assertEqual(places.label_pictures(self.conn, ctx, ["lib_trips001"])["checked"], 0)
        # redo looks at every one again.
        self.conn.execute("UPDATE images SET place_city = 'Nowhere' WHERE id = ?", (maui,))
        self.conn.commit()
        self.assertEqual(places.label_pictures(self.conn, ctx, redo=True)["checked"], 4)
        self.assertEqual(self.row(maui)["place_city"], "Kahului")
        self.assertEqual(self.row(other)["place_city"], "Paris")
        self.assertEqual(places.status(self.conn)["pictures"],
                         {"located": 4, "named": 2, "region_only": 1, "far": 1, "pending": 0})

    def test_a_pause_does_not_lock_other_writers_out(self):
        install_places()
        for n in range(50):
            self.add("%d.jpg" % n, 20.88, -156.46 + n * 0.001)

        class Pausing(jobs.JobContext):
            steps = 0

            def step(self, n=1, item=None):
                super().step(n, item)
                Pausing.steps += 1
                if Pausing.steps == 5:
                    self.pause()

        ctx = Pausing("t")
        conn = db.connect(check_same_thread=False)
        self.addCleanup(conn.close)
        worker = threading.Thread(target=places.label_pictures, args=(conn, ctx))
        worker.start()
        try:
            self.assertTrue(support.wait_for(lambda: ctx.paused and Pausing.steps == 5))
            time.sleep(0.2)
            other = sqlite3.connect(common.db_path(), timeout=0.5)
            try:
                other.execute("UPDATE images SET hidden = 1 WHERE path = '40.jpg'")
                other.commit()
                # What was looked up before the pause is saved.
                self.assertEqual(other.execute(
                    "SELECT COUNT(*) FROM images WHERE place_km IS NOT NULL").fetchone()[0], 5)
            finally:
                other.close()
        finally:
            ctx.resume()
            worker.join(10)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM images WHERE place_km IS NULL").fetchone()[0], 0)

    def test_the_towns_are_loaded_only_when_needed(self):
        self.add("a.jpg", 20.88, -156.46)
        # No place names: nothing to do, and nothing is loaded.
        with mock.patch.object(places, "shared") as shared:
            self.assertIsNone(places.label_pictures(self.conn, jobs.JobContext("t")))
        shared.assert_not_called()
        install_places()
        self.assertEqual(places.label_pictures(self.conn, jobs.JobContext("t"))["checked"], 1)
        places.release()
        with mock.patch.object(places, "shared") as shared:
            summary = places.label_pictures(self.conn, jobs.JobContext("t"))
        shared.assert_not_called()
        self.assertEqual(summary["checked"], 0)

    def test_stopping(self):
        install_places()
        for n in range(5):
            self.add("%d.jpg" % n, 20.88, -156.46)
        ctx = jobs.JobContext("t")
        ctx.request_stop()
        summary = places.label_pictures(self.conn, ctx)
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["checked"], 0)

    def test_the_search_records_have_the_place(self):
        install_places()
        maui = self.add("maui.jpg", 20.88, -156.46)
        coast = self.add("coast.jpg", 20.3, -154.6)
        sea = self.add("sea.jpg", 0.0, -140.0)
        places.label_pictures(self.conn, jobs.JobContext("t"))
        config = common.load_config()
        records = {record["i"]: record for record in search_index.build_records(
            self.conn, config, ["lib_trips001"])}
        self.assertEqual({key: records[maui][key] for key in ("ci", "rg", "co", "cc", "km")},
                         {"ci": "Kahului", "rg": "Hawaii", "co": "United States", "cc": "US",
                          "km": 2})
        self.assertNotIn("ci", records[coast])
        self.assertNotIn("km", records[coast])
        self.assertEqual(records[coast]["co"], "United States")
        self.assertFalse({"ci", "rg", "co", "cc", "km"} & set(records[sea]))
        # An export: only with its Location option.
        for location in (False, True):
            exported = {record["i"]: record for record in search_index.build_records(
                self.conn, config, ["lib_trips001"], for_export=True,
                export_options={"location": location})}
            self.assertEqual("co" in exported[maui], location)
            self.assertEqual("ci" in exported[maui], location)

    def test_the_job(self):
        source = write_geonames(self.base.join("source"))
        server = FakeGeoNames(source)
        self.addCleanup(server.close)
        maui = self.add("maui.jpg", 20.88, -156.46)
        config = common.load_config()
        ctx = jobs.JobContext("t")
        with self.assertRaises(jobs.JobError):
            jobs.run_places(ctx, self.conn, config, {})
        with mock.patch.object(places, "GEONAMES_URL", server.url):
            summary = jobs.run_places(ctx, self.conn, config, {"download": True})
        self.assertEqual(summary["index"]["cities"], len(USED))
        self.assertEqual(summary["places"]["named"], 1)
        self.assertEqual(self.row(maui)["place_city"], "Kahului")
        self.assertIsNone(places._shared)
        self.conn.execute("UPDATE images SET place_city = 'X' WHERE id = ?", (maui,))
        self.conn.commit()
        self.assertEqual(jobs.run_places(ctx, self.conn, config, {})["places"]["checked"], 0)
        self.assertEqual(jobs.run_places(ctx, self.conn, config, {"redo": True})
                         ["places"]["checked"], 1)
        self.assertEqual(self.row(maui)["place_city"], "Kahului")
        self.assertIn("places", jobs.RUNNERS)

    def test_the_job_looks_up_every_library(self):
        # (The Manage page counts the pictures of every library; the job
        # does them all, so its count goes to nothing.)
        install_places()
        config = common.load_config()
        config["libraries"][1]["enabled"] = False
        trips = self.add("maui.jpg", 20.88, -156.46)
        other = self.add("paris.jpg", 48.85, 2.35, library_id="lib_other001")
        gone = self.add("old.jpg", 48.85, 2.35, library_id="lib_gone0001")
        self.assertEqual(places.status(self.conn)["pictures"]["pending"], 3)
        summary = jobs.run_places(jobs.JobContext("t"), self.conn, config, {})
        self.assertEqual(summary["places"]["checked"], 3)
        self.assertEqual(places.status(self.conn)["pictures"]["pending"], 0)
        self.assertEqual([self.row(image_id)["place_city"] for image_id in (trips, other, gone)],
                         ["Kahului", "Paris", "Paris"])

    def test_an_index_of_another_version_is_built_again(self):
        install_places()
        maui = self.add("maui.jpg", 20.88, -156.46)
        self.conn.execute("UPDATE images SET place_km = 5, place_city = 'Old' WHERE id = ?",
                          (maui,))
        self.conn.commit()
        outdated = sqlite3.connect(places.places_db_path())
        outdated.execute("UPDATE meta SET value = '0' WHERE key = 'format'")
        outdated.commit()
        outdated.close()
        places.release()
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertIsNone(places.shared())
        state = places.status(self.conn)
        self.assertFalse(state["available"])
        self.assertTrue(state["rebuild"])
        self.assertIn("built again from the downloaded files", state["error"])
        summary = jobs.run_places(jobs.JobContext("t"), self.conn, common.load_config(), {})
        self.assertEqual(summary["index"]["cities"], len(USED))
        self.assertEqual(places.read_meta()["format"], str(places.PLACES_FORMAT))
        # Every picture is looked up again with the new index.
        self.assertEqual(summary["places"]["checked"], 1)
        self.assertEqual(self.row(maui)["place_city"], "Kahului")
        # Nothing to do when it is fine.
        self.assertIsNone(places.rebuild_if_needed())

    def test_a_cancelled_download_is_no_failure(self):
        source = write_geonames(self.base.join("source"))
        server = FakeGeoNames(source)
        self.addCleanup(server.close)
        ctx = jobs.JobContext("t")
        ctx.request_stop()
        with mock.patch.object(places, "GEONAMES_URL", server.url):
            self.assertEqual(jobs.run_places(ctx, self.conn, common.load_config(),
                                             {"download": True}), {})
        self.assertFalse(os.path.exists(places.places_db_path()))


class CommandLineTest(support.TempBaseMixin, unittest.TestCase):

    def test_build_from_a_folder_and_label(self):
        folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", folder, id="lib_trips001")])
        db.init(log=quiet)
        conn = db.connect()
        conn.execute("INSERT INTO images (library_id, path, name, size, mtime, lat, lon) "
                     "VALUES ('lib_trips001', 'a.jpg', 'a.jpg', 1, 1, 48.86, 2.35)")
        conn.commit()
        conn.close()
        source = write_geonames(self.base.join("source"), as_zip=False)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(make_places.main(["--from", source]), 0)
            self.assertEqual(make_places.main(["--lookup", "20.88", "-156.46"]), 0)
        self.assertIn("Kahului, Hawaii, United States", output.getvalue())
        conn = db.connect()
        try:
            row = conn.execute("SELECT place_city, place_country FROM images").fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), ("Paris", "France"))
        places.release()

    def test_an_index_of_another_version_is_built_again(self):
        self.base.write_config([])
        install_places()
        outdated = sqlite3.connect(places.places_db_path())
        outdated.execute("UPDATE meta SET value = '0' WHERE key = 'format'")
        outdated.commit()
        outdated.close()
        places.release()
        with mock.patch.object(places, "download") as download, \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(make_places.main(["--no-label"]), 0)
        download.assert_not_called()
        self.assertEqual(places.read_meta()["format"], str(places.PLACES_FORMAT))
        places.release()

    def test_nothing_is_downloaded_with_from(self):
        with mock.patch.object(places, "download") as download, \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(make_places.main(["--from", self.base.join("nothing"),
                                               "--no-label"]), 1)
        download.assert_not_called()


class SchemaTest(support.TempBaseMixin, unittest.TestCase):

    def test_version_3_database_gets_the_place_columns(self):
        common.ensure_dir(common.data_dir())
        path = common.db_path()
        conn = sqlite3.connect(path)
        for version, func, _rewrites in db.MIGRATIONS:
            if version <= 3:
                func(conn)
        conn.execute("PRAGMA user_version=3")
        conn.execute("INSERT INTO images (library_id, path, name, size, mtime, lat, lon) "
                     "VALUES ('lib_x', 'a.jpg', 'a.jpg', 1, 1, 20.88, -156.46)")
        conn.commit()
        conn.close()
        self.base.write_config([])
        report = db.init(log=quiet)
        self.assertEqual(report["from_version"], 3)
        self.assertTrue(os.path.exists(path + ".bak-v3"))
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM images").fetchone()
            self.assertEqual(db.user_version(conn), db.SCHEMA_VERSION)
        finally:
            conn.close()
        self.assertEqual((row["place_city"], row["place_region"], row["place_country"],
                          row["place_cc"], row["place_km"]), ("", "", "", "", None))


if __name__ == "__main__":
    unittest.main()
