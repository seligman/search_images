"""Tests for street_maps.py (street-level maps offline) and how the server, the pages
and exports use them.

A made-up "planet" PMTiles archive (tiles of a small made-up area, zoom 0 to 10) is
served by a web server on 127.0.0.1 that answers range requests and writes down every
request, so the size check, the download (also stopped and started again) and the
privacy of what is asked for can be checked; the pictures' places are made up too.
Nothing here uses the network.
"""

import base64
import contextlib
import gzip
import http.server
import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import support

import cache
import common
import db
import export
import jobs
import serve
import street_maps
import trip_export
import test_trip_export as _trip_tests
from test_search_index import insert_image, V5_INFO

try:
    import flask
except ImportError:
    flask = None

NODE = shutil.which("node")
STREETS_TESTS = os.path.join(support.REPO, "tests", "js", "streets_test.js")
# The made-up area of the planet archive, and places in it.
AREA = (9.6, 19.6, 11.4, 21.4)          # south, west, north, east
PLACE = (10.3, 20.4)
OTHER_PLACE = (10.7, 20.8)              # (the same whole-degree square)
TOP_ZOOM = 10


def tile_data(z, x, y):
    """A made-up tile: gzip bytes, its size depending on the tile; the sea tiles of
    zoom 9 and 10 in the area's south-west corner all alike (as a planet dedupes)."""
    south, west, _north, _east = street_maps.tile_bounds(z, x, y)
    if z >= 9 and south < 9.8 and west < 19.8:
        return gzip.compress(b"sea" * 10, mtime=0)
    text = ("tile %d/%d/%d " % (z, x, y)) * (3 + (x * 7 + y * 3) % 11)
    return gzip.compress(text.encode("ascii"), mtime=0)


def make_planet(path, top=TOP_ZOOM):
    """The made-up planet archive: every tile of AREA from zoom 0 to top."""
    levels = street_maps.coverage_levels([AREA], top)
    writer = street_maps.ArchiveWriter(path)
    for tid in street_maps.tile_ids(levels):
        writer.add(tid, tile_data(*street_maps.tile_zxy(tid)))
    writer.finish({"vector_layers": [], "version": "4.9.0", "attribution": "<a>x</a>"},
                  (AREA[1], AREA[0], AREA[3], AREA[2]))
    return levels


class RangeServer:
    """A web server on 127.0.0.1 for a folder, answering range requests; requests
    lists (path, Range header, User-Agent) of each request."""

    def __init__(self, folder, ranges=True):
        self.folder = folder
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Range"),
                                       self.headers.get("User-Agent")))
                name = urllib.parse.urlparse(self.path).path.lstrip("/")
                path = os.path.join(owner.folder, name)
                if not name or not os.path.isfile(path):
                    self.send_error(404)
                    return
                with open(path, "rb") as handle:
                    data = handle.read()
                wanted = self.headers.get("Range")
                match = re.match(r"^bytes=(\d+)-(\d+)$", wanted or "")
                if match and ranges:
                    first, last = int(match.group(1)), min(int(match.group(2)), len(data) - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last,
                                                                          len(data)))
                    self.send_header("ETag", '"planet-1"')
                    body = data[first:last + 1]
                else:
                    self.send_response(200)
                    body = data
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def ranges(self, name):
        return sorted({request[1] for request in self.requests
                       if request[0] == "/" + name and request[1]})


class Ctx(jobs.JobContext):
    """A job context that keeps its log lines and stops after a number of steps."""

    def __init__(self, stop_after=None):
        super().__init__("test")
        self.lines = []
        self.stop_after = stop_after
        self.steps = 0

    def _emit(self, entry):
        self.lines.append(entry["message"])

    def step(self, n=1, item=None, quick=False):
        self.steps += n
        super().step(n, item, quick)

    def should_stop(self):
        return self.stop_after is not None and self.steps >= self.stop_after


class TileMathTest(unittest.TestCase):

    def test_tile_ids_go_both_ways(self):
        rng = random.Random(7)
        for z in range(0, 16):
            for _ in range(40):
                x, y = rng.randrange(1 << z), rng.randrange(1 << z)
                self.assertEqual(street_maps.tile_zxy(street_maps.tile_id(z, x, y)), (z, x, y))
        self.assertEqual(street_maps.tile_id(0, 0, 0), 0)
        self.assertEqual(street_maps.tile_id(1, 0, 0), 1)
        self.assertEqual(street_maps.tile_id(2, 0, 0), 5)

    def test_rows_columns_and_bounds_agree(self):
        for lat, lon in ((10.3, 20.4), (-33.9, 151.2), (64.1, -21.9), (0.0, -180.0)):
            for z in (3, 10, 15):
                x, y = street_maps.lon_column(lon, z), street_maps.lat_row(lat, z)
                south, west, north, east = street_maps.tile_bounds(z, x, y)
                self.assertTrue(south <= lat <= north and west <= lon <= east, (lat, lon, z))
        # Beyond about 85 degrees: the edge rows.
        self.assertEqual(street_maps.lat_row(89.9, 4), 0)
        self.assertEqual(street_maps.lat_row(-89.9, 4), 15)

    def test_boxes_cover_their_tiles_also_across_the_date_line(self):
        found = set()
        street_maps.box_tiles(street_maps.box_around(0.0, 179.99, 20), 6, found)
        columns = sorted({x for x, _y in found})
        self.assertEqual(columns, [0, 63])
        levels = street_maps.with_ancestors(found, 6)
        self.assertEqual(levels[0], {(0, 0)})
        self.assertEqual(set(levels), set(range(7)))
        # A box of 10 km at zoom 15 at 10 degrees: about 9 x 9 tiles.
        tiles = set()
        street_maps.box_tiles(street_maps.box_around(10.3, 20.4, 5), 15, tiles)
        self.assertTrue(64 <= len(tiles) <= 121, len(tiles))

    def test_the_index_lists_runs_of_columns(self):
        rows = street_maps.index_rows({3: {(1, 2), (2, 2), (3, 2), (6, 2), (0, 5)},
                                       0: {(0, 0)}})
        self.assertEqual(rows, {"0": [[0, 0, 0]], "3": [[2, 1, 3, 6, 6], [5, 0, 0]]})

    def test_whole_degree_squares(self):
        squares = street_maps.whole_degree_boxes([street_maps.box_around(10.3, 20.4, 5),
                                                  street_maps.box_around(10.7, 20.8, 5),
                                                  (10.9, 20.9, 11.1, 21.2)])
        self.assertEqual(squares, [(10, 20, 11, 21), (10, 21, 11, 22), (11, 20, 12, 21),
                                   (11, 21, 12, 22)])

    def test_lines_get_a_point_every_kilometre(self):
        points = street_maps.densify([(10.0, 20.0), (10.0, 20.1)], 1.0)
        self.assertEqual(points[0], (10.0, 20.0))
        self.assertEqual(points[-1], (10.0, 20.1))
        self.assertEqual(len(points), 12)       # about 11 km
        merged = street_maps._merge_ranges([(0, 10), (12, 5), (500, 10), (5, 5)], gap=4)
        self.assertEqual(merged, [(0, 17), (500, 10)])

    def test_overlapping_ranges_are_joined_whatever_the_limit(self):
        # (Else the bytes they share would be asked for, and counted, twice.)
        self.assertEqual(street_maps._merge_ranges([(0, 10), (5, 20)], gap=0, limit=12),
                         [(0, 25)])
        # Ranges a gap apart are joined only up to the limit.
        self.assertEqual(street_maps._merge_ranges([(0, 10), (12, 5)], gap=4, limit=12),
                         [(0, 10), (12, 5)])
        self.assertEqual(street_maps._merge_ranges([(0, 10), (12, 5)], gap=4, limit=20),
                         [(0, 17)])


class ArchiveTest(support.TempBaseMixin, unittest.TestCase):

    def test_written_archives_read_back_with_runs_and_alike_tiles_once(self):
        path = self.base.join("small.pmtiles")
        writer = street_maps.ArchiveWriter(path)
        first = street_maps.tile_id(5, 10, 12)
        for offset in range(6):
            data = b"same" if offset in (1, 2, 3) else b"tile %d" % offset
            writer.add(first + offset, data)
        size = writer.finish({"name": "test"}, (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(os.path.getsize(path), size)
        self.assertFalse(os.path.exists(path + ".data"))
        archive = street_maps.open_local(path)
        try:
            header = archive.header
            self.assertEqual((header["addressed"], header["entries"], header["contents"]),
                             (6, 4, 4))
            self.assertEqual((header["min_zoom"], header["max_zoom"]), (5, 5))
            self.assertEqual(header["bounds"], [1.0, 2.0, 3.0, 4.0])
            self.assertEqual(archive.metadata(), {"name": "test"})
            for offset in range(6):
                z, x, y = street_maps.tile_zxy(first + offset)
                self.assertEqual(archive.get(z, x, y),
                                 b"same" if offset in (1, 2, 3) else b"tile %d" % offset)
            self.assertIsNone(archive.get(5, 0, 0))
            self.assertEqual(archive.entries()[1][:2], (first + 1, 3))
        finally:
            archive.read.close()
        with self.assertRaises(ValueError):
            writer = street_maps.ArchiveWriter(self.base.join("bad.pmtiles"))
            writer.add(10, b"a")
            try:
                writer.add(9, b"b")
            finally:
                writer.discard()

    def test_large_archives_get_leaf_directories(self):
        path = self.base.join("large.pmtiles")
        rng = random.Random(3)
        writer = street_maps.ArchiveWriter(path)
        tid = street_maps.tile_id(12, 0, 0)
        ids = []
        for _ in range(12000):
            tid += rng.randrange(1, 40)
            ids.append(tid)
            writer.add(tid, os.urandom(rng.randrange(5, 60)))
        writer.finish({}, (0.0, 0.0, 1.0, 1.0))
        archive = street_maps.open_local(path)
        try:
            self.assertGreater(archive.header["leaf_length"], 0)
            self.assertLessEqual(archive.header["root_length"], street_maps.ROOT_LIMIT)
            self.assertEqual([entry[0] for entry in archive.entries()], ids)
            for tid in ids[::997]:
                self.assertIsNotNone(archive.find(tid))
            self.assertIsNone(archive.find(ids[5] + 1) if ids[6] != ids[5] + 1 else None)
        finally:
            archive.read.close()

    def test_other_files_are_refused(self):
        with self.assertRaises(street_maps.MapsError):
            street_maps.parse_header(b"PK\x03\x04" + b"\0" * 200)
        with self.assertRaises(street_maps.MapsError):
            street_maps.parse_header(b"PMTiles\x02" + b"\0" * 200)


class SettingsTest(unittest.TestCase):

    def test_defaults_and_checked_values(self):
        self.assertEqual(street_maps.settings({}), street_maps.DEFAULTS)
        found = street_maps.settings({"street_maps": {
            "max_zoom": 14, "radius_km": 2.5, "route_km": 0, "whole_degrees": False,
            "source": " https://example.invalid/x.pmtiles "}})
        self.assertEqual(found, {"max_zoom": 14, "radius_km": 2.5, "route_km": 0.0,
                                 "whole_degrees": False,
                                 "source": "https://example.invalid/x.pmtiles"})
        bad = street_maps.settings({"street_maps": {"max_zoom": 20, "radius_km": -1,
                                                    "route_km": "5", "whole_degrees": 1,
                                                    "source": "file:///etc"}})
        self.assertEqual(bad, street_maps.DEFAULTS)


class PlanAndDownloadTest(support.TempBaseMixin, unittest.TestCase):
    """The size check and the download, against the made-up planet on 127.0.0.1."""

    def setUp(self):
        super().setUp()
        self.site = self.base.make_dir("site")
        self.planet_levels = make_planet(os.path.join(self.site, "20990101.pmtiles"))
        self.server = RangeServer(self.site)
        self.addCleanup(self.server.close)
        self.source = self.server.url + "20990101.pmtiles"
        self.family = self.base.library_folder("family")
        self.write_config()
        db.init(log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(street_maps.shared.forget)
        insert_image(self.conn, "lib_family1", "a.jpg", lat=PLACE[0], lon=PLACE[1])

    def write_config(self, **street):
        settings = {"max_zoom": TOP_ZOOM, "radius_km": 5, "route_km": 1,
                    "whole_degrees": False, "source": self.source}
        settings.update(street)
        self.base.write_config([support.library("Family", self.family, id="lib_family1")],
                               street_maps=settings)
        self.config = common.load_config()

    def expected_tiles(self, places, km=5):
        boxes = [street_maps.box_around(lat, lon, km) for lat, lon in places]
        levels = street_maps.coverage_levels(boxes, TOP_ZOOM)
        return {street_maps.tile_id(z, x, y) for z, tiles in levels.items() for x, y in tiles}

    def test_the_size_check_reads_directories_only_and_counts_the_tiles(self):
        found = street_maps.plan(self.conn, self.config, Ctx())
        self.assertEqual(found["mode"], "exact")
        self.assertEqual(found["spots"], 1)
        self.assertEqual(found["tiles"], len(self.expected_tiles([PLACE])))
        self.assertEqual(found["max_zoom"], TOP_ZOOM)
        self.assertGreater(found["download_bytes"], 0)
        self.assertGreaterEqual(found["download_bytes"], found["keep_bytes"])
        self.assertFalse(found["large"])
        # Only the header and directories were read: no tile data.
        archive = found["_archive"]
        data_start = archive.header["data_offset"]
        for _path, ranges, agent in self.server.requests:
            first = int(re.match(r"^bytes=(\d+)-", ranges).group(1))
            self.assertLess(first, data_start)
            self.assertEqual(agent, street_maps.USER_AGENT)
        # What is kept of the check says nothing of where the pictures are.
        street_maps.save_plan(found)
        kept = json.dumps(street_maps.status()["plan"])
        self.assertNotIn("10.3", kept)
        self.assertNotIn("20.4", kept)
        self.assertNotIn("bounds", kept)
        self.assertIn(found["build"], street_maps.plan_words(found))

    def test_a_download_keeps_exactly_the_planned_tiles(self):
        ctx = Ctx()
        result = street_maps.download(self.conn, self.config, ctx)
        self.assertFalse(result["stopped"])
        archive = street_maps.open_local(street_maps.archive_path())
        try:
            have = {entry[0] + k for entry in archive.entries() for k in range(entry[1])}
            self.assertEqual(have, self.expected_tiles([PLACE]))
            for tid in sorted(have)[::7]:
                z, x, y = street_maps.tile_zxy(tid)
                self.assertEqual(archive.get(z, x, y), tile_data(z, x, y))
            self.assertEqual(archive.metadata()["attribution"], street_maps.ATTRIBUTION)
        finally:
            archive.read.close()
        manifest = street_maps.status()["manifest"]
        self.assertEqual(manifest["build"], "20990101.pmtiles")
        self.assertEqual(manifest["tiles"], len(have))
        # The temporary files are gone; the status says it all.
        left = sorted(os.listdir(street_maps.maps_dir()))
        self.assertEqual(left, ["streets.json", "streets.pmtiles"])
        status = street_maps.status(self.conn, self.config)
        self.assertTrue(status["available"])
        self.assertEqual(status["outside"], 0)
        self.assertEqual(status["max_zoom"], TOP_ZOOM)
        # A picture somewhere else has no street map yet.
        insert_image(self.conn, "lib_family1", "b.jpg", lat=-40.0, lon=100.0)
        self.assertEqual(street_maps.status(self.conn, self.config)["outside"], 1)
        self.assertEqual(street_maps.remove(), 2)
        self.assertFalse(street_maps.status()["available"])

    def test_a_download_much_larger_than_agreed_to_is_refused(self):
        saved = street_maps.GROWTH_SLACK
        street_maps.GROWTH_SLACK = 0
        self.addCleanup(setattr, street_maps, "GROWTH_SLACK", saved)
        with self.assertRaises(street_maps.MapsError):
            street_maps.download(self.conn, self.config, Ctx(), expect_bytes=10)
        self.assertFalse(os.path.exists(street_maps.archive_path()))

    def test_a_stopped_download_goes_on_where_it_stopped(self):
        self.write_config()
        street_maps.MERGE_GAP, saved = 0, street_maps.MERGE_GAP
        self.addCleanup(setattr, street_maps, "MERGE_GAP", saved)
        found = street_maps.plan(self.conn, self.config, Ctx())
        self.assertGreater(found["requests"], 3)
        stopped = street_maps.download(self.conn, self.config, Ctx(stop_after=2), found=found)
        self.assertTrue(stopped["stopped"])
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        self.assertTrue(street_maps.status()["downloading"])
        first = len(self.server.requests)
        ctx = Ctx()
        result = street_maps.download(self.conn, self.config, ctx)
        self.assertFalse(result["stopped"])
        self.assertTrue(any("Continuing" in line for line in ctx.lines), ctx.lines)
        # The two ranges downloaded before were not asked for again.
        data_start = found["_archive"].header["data_offset"]
        later = [request[1] for request in self.server.requests[first:]
                 if int(re.match(r"^bytes=(\d+)-", request[1]).group(1)) >= data_start]
        self.assertEqual(len(later), found["requests"] - 2)
        archive = street_maps.open_local(street_maps.archive_path())
        try:
            have = {entry[0] + k for entry in archive.entries() for k in range(entry[1])}
        finally:
            archive.read.close()
        self.assertEqual(have, self.expected_tiles([PLACE]))

    def test_whole_degrees_ask_only_for_whole_degree_squares(self):
        # Two places in the same whole-degree square: the same requests (the map's host
        # cannot tell them apart), and more bytes than asking for the tiles alone.
        self.write_config(whole_degrees=True)
        first = street_maps.plan(self.conn, self.config, Ctx())
        street_maps.download(self.conn, self.config, Ctx(), found=first)
        asked_first = self.server.ranges("20990101.pmtiles")
        self.assertEqual(first["mode"], "whole_degrees")
        self.assertEqual(first["squares"], 1)
        street_maps.remove()
        self.conn.execute("UPDATE images SET lat = ?, lon = ?", OTHER_PLACE)
        self.conn.commit()
        self.server.requests[:] = []
        second = street_maps.plan(self.conn, self.config, Ctx())
        street_maps.download(self.conn, self.config, Ctx(), found=second)
        self.assertEqual(self.server.ranges("20990101.pmtiles"), asked_first)
        self.assertNotEqual(first["tiles"], 0)
        self.assertEqual(second["download_bytes"], first["download_bytes"])
        self.write_config(whole_degrees=False)
        exact = street_maps.plan(self.conn, self.config, Ctx())
        self.assertLess(exact["download_bytes"], second["download_bytes"])
        # Each picture's own tiles are kept, whatever was asked for.
        archive = street_maps.open_local(street_maps.archive_path())
        try:
            have = {entry[0] + k for entry in archive.entries() for k in range(entry[1])}
        finally:
            archive.read.close()
        self.assertEqual(have, self.expected_tiles([OTHER_PLACE]))

    def test_whole_degrees_read_the_same_directories_in_the_same_order(self):
        # A planet whose directory is split in many small leaf directories: the size
        # check reads them square by square, so two places in the same whole-degree
        # square make the very same requests, in the same order.
        self.addCleanup(setattr, street_maps, "ROOT_LIMIT", street_maps.ROOT_LIMIT)
        self.addCleanup(setattr, street_maps, "LEAF_ENTRIES", street_maps.LEAF_ENTRIES)
        street_maps.ROOT_LIMIT = 60
        street_maps.LEAF_ENTRIES = 4
        make_planet(os.path.join(self.site, "20990102.pmtiles"))
        self.write_config(whole_degrees=True, source=self.server.url + "20990102.pmtiles")
        self.server.requests[:] = []
        first = street_maps.plan(self.conn, self.config, Ctx())
        asked_first = [request[:2] for request in self.server.requests]
        self.assertGreater(len(asked_first), 3)          # (several leaf directories)
        shutil.rmtree(street_maps.cache_dir(), ignore_errors=True)
        self.conn.execute("UPDATE images SET lat = ?, lon = ?", OTHER_PLACE)
        self.conn.commit()
        self.server.requests[:] = []
        second = street_maps.plan(self.conn, self.config, Ctx())
        self.assertEqual([request[:2] for request in self.server.requests], asked_first)
        self.assertNotEqual(set(first["_keep"]), set(second["_keep"]))
        self.assertEqual(first["_ranges"], second["_ranges"])

    def test_a_large_download_is_refused_unless_allowed(self):
        # Large: to keep, or (with whole degrees) only to download.
        self.write_config(whole_degrees=True)
        found = street_maps.plan(self.conn, self.config, Ctx())
        self.assertLess(found["keep_bytes"], found["download_bytes"])
        self.addCleanup(setattr, street_maps, "LARGE_BYTES", street_maps.LARGE_BYTES)
        street_maps.LARGE_BYTES = found["keep_bytes"] + 1
        found = street_maps.plan(self.conn, self.config, Ctx())
        self.assertTrue(found["large"])
        self.assertIn("not downloaded unless", street_maps.plan_words(found))
        with self.assertRaises(street_maps.MapsError):
            street_maps.download(self.conn, self.config, Ctx(), found=found)
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        with self.assertRaises(jobs.JobError):
            jobs.run_maps(Ctx(), self.conn, self.config, {
                "download": True, "source": self.source,
                "expect_bytes": found["download_bytes"]})
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        result = street_maps.download(self.conn, self.config, Ctx(), found=found,
                                      allow_large=True)
        self.assertFalse(result["stopped"])
        self.assertTrue(os.path.isfile(street_maps.archive_path()))

    def test_too_little_free_space_is_refused_before_anything_is_downloaded(self):
        found = street_maps.plan(self.conn, self.config, Ctx())
        asked = len(self.server.requests)
        # (The tiles, then the map file made of them: about twice what is kept.)
        need = 2 * found["keep_bytes"] + street_maps.SPACE_MARGIN
        with mock.patch.object(street_maps, "free_bytes", return_value=need // 2):
            with self.assertRaises(street_maps.MapsError) as caught:
                street_maps.download(self.conn, self.config, Ctx(), found=found)
        self.assertIn("free disk space", str(caught.exception))
        self.assertEqual(len(self.server.requests), asked)
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        with mock.patch.object(street_maps, "free_bytes", return_value=need + 10 ** 6):
            street_maps.download(self.conn, self.config, Ctx(), found=found)
        self.assertTrue(os.path.isfile(street_maps.archive_path()))

    def test_the_map_file_is_made_of_the_downloaded_tiles_without_a_copy_of_them(self):
        def no_copy(*_args, **_kwargs):
            raise AssertionError("the downloaded tiles were copied into another spool")
        with mock.patch.object(street_maps, "ArchiveWriter", no_copy):
            result = street_maps.download(self.conn, self.config, Ctx())
        self.assertFalse(result["stopped"])
        archive = street_maps.open_local(street_maps.archive_path())
        try:
            have = {entry[0] + k for entry in archive.entries() for k in range(entry[1])}
            self.assertEqual(have, self.expected_tiles([PLACE]))
            for tid in sorted(have):
                self.assertEqual(archive.get(*street_maps.tile_zxy(tid)),
                                 tile_data(*street_maps.tile_zxy(tid)))
            self.assertEqual(archive.header["clustered"], 1)
        finally:
            archive.read.close()

    def test_a_stopped_download_goes_on_with_its_build_when_a_newer_one_comes(self):
        street_maps.MERGE_GAP, saved = 0, street_maps.MERGE_GAP
        self.addCleanup(setattr, street_maps, "MERGE_GAP", saved)
        self.write_config(source="")
        builds = {"key": "20990101.pmtiles"}

        def newest(builds_url=None):
            return {"url": self.server.url + builds["key"], "key": builds["key"],
                    "size": None, "version": "4.9.0"}
        self.addCleanup(setattr, street_maps, "newest_build", street_maps.newest_build)
        street_maps.newest_build = newest
        found = street_maps.plan(self.conn, self.config, Ctx())
        self.assertEqual(found["build"], "20990101.pmtiles")
        stopped = street_maps.download(self.conn, self.config, Ctx(stop_after=2), found=found)
        self.assertTrue(stopped["stopped"])
        downloading = street_maps.status()["downloading"]
        self.assertEqual((downloading["build"], downloading["done"], downloading["total"]),
                         ("20990101.pmtiles", 2, found["requests"]))
        # A newer build is out: the stopped download goes on with its own.
        shutil.copyfile(os.path.join(self.site, "20990101.pmtiles"),
                        os.path.join(self.site, "20990102.pmtiles"))
        builds["key"] = "20990102.pmtiles"
        first = len(self.server.requests)
        ctx = Ctx()
        again = street_maps.plan(self.conn, self.config, ctx)
        self.assertEqual(again["build"], "20990101.pmtiles")
        self.assertEqual(street_maps.resume_state(again)[0], "continues")
        result = street_maps.download(self.conn, self.config, ctx, found=again)
        self.assertFalse(result["stopped"])
        self.assertTrue(any("Continuing" in line for line in ctx.lines), ctx.lines)
        data_start = found["_archive"].header["data_offset"]
        later = [request for request in self.server.requests[first:]
                 if int(re.match(r"^bytes=(\d+)-", request[1]).group(1)) >= data_start]
        self.assertEqual(len(later), found["requests"] - 2)
        self.assertTrue(all(request[0] == "/20990101.pmtiles" for request in later))
        self.assertEqual(street_maps.status()["manifest"]["build"], "20990101.pmtiles")

    def test_a_stopped_download_of_a_build_that_is_gone_starts_over_and_says_so(self):
        street_maps.MERGE_GAP, saved = 0, street_maps.MERGE_GAP
        self.addCleanup(setattr, street_maps, "MERGE_GAP", saved)
        self.addCleanup(setattr, street_maps, "TRIES", street_maps.TRIES)
        street_maps.TRIES = 1
        found = street_maps.plan(self.conn, self.config, Ctx(),
                                 source=self.server.url + "20990101.pmtiles")
        street_maps.download(self.conn, self.config, Ctx(stop_after=1), found=found)
        shutil.copyfile(os.path.join(self.site, "20990101.pmtiles"),
                        os.path.join(self.site, "20990102.pmtiles"))
        os.remove(os.path.join(self.site, "20990101.pmtiles"))
        self.write_config(source="")
        self.addCleanup(setattr, street_maps, "newest_build", street_maps.newest_build)
        street_maps.newest_build = lambda builds_url=None: {
            "url": self.server.url + "20990102.pmtiles", "key": "20990102.pmtiles",
            "size": None, "version": "4.9.0"}
        ctx = Ctx()
        newer = street_maps.plan(self.conn, self.config, ctx)
        self.assertEqual(newer["build"], "20990102.pmtiles")
        self.assertTrue(any("cannot be read any more" in line for line in ctx.lines))
        self.assertEqual(street_maps.resume_state(newer)[0], "discarded")
        ctx = Ctx()
        street_maps.download(self.conn, self.config, ctx, found=newer)
        self.assertTrue(any("starts over" in line for line in ctx.lines), ctx.lines)
        self.assertEqual(street_maps.status()["manifest"]["build"], "20990102.pmtiles")

    def test_what_a_cut_short_download_left_is_removed(self):
        os.makedirs(street_maps.maps_dir(), exist_ok=True)
        names = ["write.data", "streets.pmtiles.tmp", "plan.json.tmp", "download.json.tmp"]
        for name in names:
            with open(os.path.join(street_maps.maps_dir(), name), "wb") as handle:
                handle.write(b"left")
        street_maps.remove()
        self.assertEqual(sorted(os.listdir(street_maps.maps_dir())), [])
        for name in names:
            with open(os.path.join(street_maps.maps_dir(), name), "wb") as handle:
                handle.write(b"left")
        street_maps.download(self.conn, self.config, Ctx())
        self.assertEqual(sorted(os.listdir(street_maps.maps_dir())),
                         ["streets.json", "streets.pmtiles"])

    def test_hidden_pictures_are_left_out(self):
        # (Other computers see the area the street maps cover.)
        insert_image(self.conn, "lib_family1", "hidden.jpg", lat=-40.0, lon=100.0, hidden=1)
        spots, _routes = street_maps.coverage_points(self.conn, self.config)
        self.assertEqual(spots, [PLACE])

    def test_the_words_say_what_the_host_learns(self):
        exact = street_maps.plan(self.conn, self.config, Ctx())
        self.assertIn("can see which areas", street_maps.plan_words(exact))
        self.write_config(whole_degrees=True)
        whole = street_maps.plan(self.conn, self.config, Ctx())
        self.assertIn("no more than whole degrees", street_maps.plan_words(whole))

    def test_the_command_line_of_a_job_reads_back_to_its_params(self):
        for params in ({}, {"download": True, "expect_bytes": 1234567,
                            "source": "https://build.example/20990101.pmtiles"},
                       {"download": True, "expect_bytes": 99, "allow_large": True,
                        "source": "http://127.0.0.1:9/p.pmtiles"},
                       {"download": True}):
            args = street_maps.command_args(params)
            self.assertEqual(street_maps.job_params(street_maps.parser().parse_args(args)),
                             params, args)
            self.assertEqual(street_maps.command_line(params),
                             " ".join(["python", "street_maps.py"] + args))
        self.assertIn("--expect-bytes 1234567", street_maps.command_line(
            {"download": True, "expect_bytes": 1234567}))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            street_maps.parser().parse_args(["--download", "--expect-bytes", "0"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            street_maps.parser().parse_args(["--plan", "--source", "file:///x"])

    def test_the_command_line_guards_the_size(self):
        self.conn.close()
        self.addCleanup(setattr, street_maps, "LARGE_BYTES", street_maps.LARGE_BYTES)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(street_maps.main(["--plan"]), 0)
        street_maps.LARGE_BYTES = 1
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(street_maps.main(["--download", "--yes"]), 1)
        self.assertIn("Nothing was downloaded", out.getvalue())
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        street_maps.LARGE_BYTES = 10 ** 12
        # A download well past the size agreed to (--expect-bytes) is refused.
        saved = street_maps.GROWTH_SLACK
        street_maps.GROWTH_SLACK = 0
        self.addCleanup(setattr, street_maps, "GROWTH_SLACK", saved)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(street_maps.main(["--download", "--yes", "--expect-bytes", "10"]),
                             1)
        self.assertIn("more than the", out.getvalue())
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        street_maps.LARGE_BYTES = 1
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(street_maps.main(["--download", "--yes", "--allow-large"]), 0)
        self.assertTrue(os.path.isfile(street_maps.archive_path()))

    def test_the_status_says_what_is_there(self):
        self.conn.close()
        # The hints run as they are: the settings are not in the program folder here.
        command = jobs.command_text("street_maps.py", ["--download", "--settings-base",
                                                       self.base.path])
        self.assertEqual(street_maps.status()["command"], command)
        # --status is what no switch does (as people.py --status), alone.
        for args in ([], ["--status"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(street_maps.main(args), 0)
            self.assertIn("No street maps yet. %s downloads them" % command, out.getvalue())
        self.assertIsNone(street_maps.job_params(street_maps.parser().parse_args(
            ["--status"])))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            street_maps.main(["--status", "--download"])
        # A map file without its streets.json (put there by hand): no made-up date.
        write_streets(street_maps.coverage_levels([street_maps.box_around(*PLACE, 1)], 8))
        self.addCleanup(street_maps.shared.forget)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(street_maps.main(["--status"]), 0)
        first = [line for line in out.getvalue().splitlines()
                 if line.startswith("Street maps:")][0]
        self.assertTrue(re.match(r"^Street maps: .*, \d+ tiles, zoom 0-8\.$", first), first)
        # With it: the build and the day it was downloaded.
        found = street_maps.status()
        found["manifest"] = {"build": "20990101", "downloaded": time.mktime(
            (2099, 1, 2, 12, 0, 0, 0, 0, -1))}
        self.assertTrue(street_maps._status_words(found).endswith(
            ", of the build 20990101 (downloaded 2099-01-02)."))

    def test_removing_waits_for_a_running_job(self):
        street_maps.download(self.conn, self.config, Ctx())
        lock = jobs.WorkerLock()
        self.assertTrue(lock.acquire("a test job"))
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(street_maps.main(["--remove"]), 1)
            self.assertIn("Another job is running", out.getvalue())
            self.assertTrue(os.path.isfile(street_maps.archive_path()))
        finally:
            lock.release()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(street_maps.main(["--remove"]), 0)
        self.assertFalse(os.path.exists(street_maps.archive_path()))

    def test_the_newest_drawable_build_is_chosen(self):
        builds = [{"key": "20990101.pmtiles", "size": 1, "version": "4.1.0"},
                  {"key": "20990105.pmtiles", "size": 1, "version": "3.9.0"},
                  {"key": "20990103.pmtiles", "size": 1, "version": "4.2.0"},
                  {"key": "notes.txt", "version": "4.2.0"}]
        with open(os.path.join(self.site, "builds.json"), "w", encoding="utf-8") as handle:
            json.dump(builds, handle)
        found = street_maps.newest_build(self.server.url + "builds.json")
        self.assertEqual(found["key"], "20990103.pmtiles")
        self.assertEqual(found["url"], self.server.url + "20990103.pmtiles")
        with open(os.path.join(self.site, "builds.json"), "w", encoding="utf-8") as handle:
            json.dump(builds[1:2], handle)
        with self.assertRaises(street_maps.MapsError):
            street_maps.newest_build(self.server.url + "builds.json")

    def test_a_server_without_range_requests_is_refused(self):
        plain = RangeServer(self.site, ranges=False)
        self.addCleanup(plain.close)
        self.write_config(source=plain.url + "20990101.pmtiles")
        with self.assertRaises(street_maps.MapsError):
            street_maps.plan(self.conn, self.config, Ctx())

    def test_nothing_to_download_without_places(self):
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL")
        self.conn.commit()
        with self.assertRaises(street_maps.MapsError):
            street_maps.plan(self.conn, self.config, Ctx())
        self.assertEqual(self.server.requests, [])

    def test_the_job_logs_its_command_line_and_keeps_the_plan(self):
        ctx = Ctx()
        summary = jobs.run_maps(ctx, self.conn, self.config, {})
        self.assertTrue(jobs.command_message("maps", {}, self.config).startswith(
            "Command line: python street_maps.py --plan"))
        self.assertEqual(summary["plan"]["tiles"], len(self.expected_tiles([PLACE])))
        self.assertEqual(street_maps.status()["plan"]["tiles"], summary["plan"]["tiles"])
        ctx = Ctx()
        agreed = summary["plan"]["download_bytes"]
        summary = jobs.run_maps(ctx, self.conn, self.config, {
            "download": True, "source": self.source, "expect_bytes": agreed})
        # (With the size agreed to: the command line refuses what the job refuses.)
        self.assertTrue(jobs.command_message("maps", {
            "download": True, "source": self.source, "expect_bytes": agreed},
            self.config).startswith("Command line: python street_maps.py --download --yes "
                                    "--expect-bytes %d --source %s" % (agreed, self.source)))
        self.assertTrue(os.path.isfile(street_maps.archive_path()))
        self.assertGreater(summary["tiles"], 0)
        saved = street_maps.GROWTH_SLACK
        street_maps.GROWTH_SLACK = 0
        self.addCleanup(setattr, street_maps, "GROWTH_SLACK", saved)
        with self.assertRaises(jobs.JobError):
            jobs.run_maps(Ctx(), self.conn, self.config, {"download": True,
                                                          "source": self.source,
                                                          "expect_bytes": 5})

    def test_the_command_line_checks_the_size_then_downloads(self):
        env = support.child_env(SETTINGS_BASE=self.base.path)
        self.conn.close()
        result = subprocess.run([sys.executable, "street_maps.py", "--plan"], cwd=support.REPO,
                                env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Command line: python street_maps.py --plan", result.stdout)
        self.assertIn("Download ", result.stdout)
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        result = subprocess.run([sys.executable, "street_maps.py", "--download"],
                                cwd=support.REPO, env=env, capture_output=True, text=True,
                                input="n\n", timeout=120)
        self.assertIn("Nothing was downloaded", result.stdout)
        self.assertFalse(os.path.exists(street_maps.archive_path()))
        result = subprocess.run([sys.executable, "street_maps.py", "--download", "--yes"],
                                cwd=support.REPO, env=env, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(street_maps.archive_path()))
        result = subprocess.run([sys.executable, "street_maps.py"], cwd=support.REPO, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertIn("Street maps: ", result.stdout)


def write_streets(levels):
    """data/maps/streets.pmtiles with the made-up tiles of levels."""
    os.makedirs(street_maps.maps_dir(), exist_ok=True)
    writer = street_maps.ArchiveWriter(street_maps.archive_path())
    for tid in street_maps.tile_ids(levels):
        writer.add(tid, tile_data(*street_maps.tile_zxy(tid)))
    writer.finish({}, (AREA[1], AREA[0], AREA[3], AREA[2]))
    street_maps.shared.forget()


# Absolute addresses a page may hold that are not requests: the SVG namespace, and
# the placeholder of an empty field.
NOT_REQUESTS = ("http://www.w3.org/2000/svg", "https://...")
URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>)]*", re.I)


def other_hosts(text):
    """The addresses in text that name a host other than this computer."""
    found = []
    for url in URL_RE.findall(text):
        if url in NOT_REQUESTS or any(url.startswith(allowed) for allowed in NOT_REQUESTS):
            continue
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            found.append(url)
    return found


class NoCalls:
    """Within it, any attempt to open a network connection fails the test."""

    def __init__(self, test):
        self.test = test
        self.tries = []

    def __enter__(self):
        self.saved = (socket.socket.connect, socket.create_connection,
                      urllib.request.urlopen)
        tries = self.tries

        def refuse(*args, **_kwargs):
            tries.append(args[1:] if args and isinstance(args[0], socket.socket) else args)
            raise OSError("no network in this test")
        socket.socket.connect = refuse
        socket.create_connection = refuse
        urllib.request.urlopen = refuse
        return self

    def __exit__(self, *exc):
        socket.socket.connect, socket.create_connection, urllib.request.urlopen = self.saved
        self.test.assertEqual(self.tries, [], "a network call was tried")
        return False


@unittest.skipIf(flask is None, "Flask is not installed")
class ServedMapsTest(support.TempBaseMixin, unittest.TestCase):
    """The server's street map routes, and no call to another host from its pages."""

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", self.base.library_folder("family"),
                                                id="lib_family1")])
        db.init(log=support.quiet)
        self.addCleanup(street_maps.shared.forget)
        self.levels = street_maps.coverage_levels([street_maps.box_around(*PLACE, 3)], 12)
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        conn = db.connect()
        insert_image(conn, "lib_family1", "a.jpg", lat=PLACE[0], lon=PLACE[1], described=1,
                     prompt_ver=5, info=V5_INFO)
        conn.close()

    def config_of(self, url):
        return support.page_config(self.client.get(url).get_data(as_text=True))

    def test_the_licence_the_credit_links_to(self):
        response = self.client.get("/maps/LICENSE.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        text = response.get_data(as_text=True)
        self.assertIn("OpenStreetMap contributors", text)
        self.assertIn("Open Database License", text)
        self.assertIn("openstreetmap.org/copyright", text)
        # The map page's credit links to it (the address is set by map.js from the
        # page settings: streets.url + LICENSE.txt).
        page = self.client.get("/map").get_data(as_text=True)
        self.assertIn('id="map-streets-licence" rel="license"', page)

    def test_without_street_maps_the_pages_have_none(self):
        self.assertNotIn("streets", self.config_of("/map"))
        self.assertEqual(self.client.get("/maps/index.js").status_code, 404)
        self.assertEqual(self.client.get("/maps/3/4/3.js").status_code, 404)

    def test_the_index_and_the_tiles_are_scripts(self):
        write_streets(self.levels)
        for url in ("/map", "/", "/detail", "/trips"):
            streets = self.config_of(url)["streets"]
            self.assertEqual(streets["url"], "/maps/")
            self.assertRegex(streets["v"], r"^[0-9a-f]{10}$")
        response = self.client.get("/maps/index.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/javascript")
        text = response.get_data(as_text=True)
        self.assertTrue(text.startswith("STREETS_DATA.index({"))
        index = json.loads(text[len("STREETS_DATA.index("):-3])
        self.assertEqual(index["maxzoom"], 12)
        self.assertEqual(index["rows"], json.loads(json.dumps(
            street_maps.index_rows(self.levels))))
        x, y = sorted(self.levels[12])[0]
        response = self.client.get("/maps/12/%d/%d.js" % (x, y))
        self.assertEqual(response.status_code, 200)
        match = re.match(r'^STREETS_DATA\.tile\(12,%d,%d,"([A-Za-z0-9+/=]+)"\);\n$' % (x, y),
                         response.get_data(as_text=True))
        self.assertIsNotNone(match)
        self.assertEqual(base64.b64decode(match.group(1)), tile_data(12, x, y))
        etag = response.headers["ETag"]
        again = self.client.get("/maps/12/%d/%d.js" % (x, y), headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        # Tiles that are not there, and zooms and columns that cannot be.
        for url in ("/maps/12/0/0.js", "/maps/16/0/0.js", "/maps/3/8/0.js", "/maps/3/0/9.js"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        # Visitors (another computer) get them too, like the map.
        visitor = self.client.get("/maps/12/%d/%d.js" % (x, y),
                                  environ_base={"REMOTE_ADDR": "192.0.2.7"})
        self.assertEqual(visitor.status_code, 200)

    def test_pages_assets_and_data_name_no_other_host_and_ask_none(self):
        write_streets(self.levels)
        with NoCalls(self):
            pages = ["/", "/detail?id=1", "/map", "/trips", "/albums", "/manage", "/settings",
                     "/duplicates"]
            assets = set()
            for url in pages:
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                csp = response.headers["Content-Security-Policy"]
                self.assertIn("default-src 'self'", csp)
                self.assertIn("connect-src 'self'", csp)
                html = response.get_data(as_text=True)
                self.assertEqual(other_hosts(html), [], url)
                assets.update(re.findall(r'(?:src|href)="(/static/[^"?]+)', html))
            for name in sorted(os.listdir(os.path.join(support.REPO, "static"))):
                if name.endswith((".js", ".css")):
                    assets.add("/static/" + name)
            for url in sorted(assets):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertEqual(other_hosts(response.get_data(as_text=True)), [], url)
                response.close()
            x, y = sorted(self.levels[12])[0]
            for url in ("/api/index", "/api/locations", "/api/routes", "/api/trips",
                        "/maps/index.js", "/maps/12/%d/%d.js" % (x, y)):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertEqual(other_hosts(response.get_data(as_text=True)), [], url)
        # The scripts never fetch from an address of their own making: streets.js loads
        # scripts from the page settings' address only.
        code = support.code_only(open(os.path.join(support.REPO, "static", "streets.js"),
                                      encoding="utf-8").read())
        self.assertNotRegex(code, r"\bfetch\s*\(|XMLHttpRequest|\bWorker\b|\bimport\s*\(")


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminMapsTest(support.TempBaseMixin, unittest.TestCase):

    HEADERS = {"X-Search-Images": "1", "Sec-Fetch-Site": "same-origin"}

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", self.base.library_folder("family"),
                                                id="lib_family1")])
        db.init(log=support.quiet)
        self.addCleanup(street_maps.shared.forget)
        self.manager = jobs.JobManager()
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def post(self, path, body=None):
        return self.client.post("/api/admin" + path, json=body or {}, headers=self.HEADERS)

    def test_status_size_check_download_and_delete(self):
        state = self.client.get("/api/admin/maps?full=1", headers=self.HEADERS).get_json()
        self.assertFalse(state["available"])
        self.assertIsNone(state["plan"])
        self.assertEqual(state["attribution"], street_maps.ATTRIBUTION)
        self.assertEqual(state["settings"], street_maps.DEFAULTS)
        # (The panel's hint: the command line runs as it is, from any folder.)
        self.assertEqual(state["command"], jobs.command_text(
            "street_maps.py", ["--download", "--settings-base", self.base.path]))
        # A download is only taken after a size check, of the file it checked.
        self.assertEqual(self.post("/maps/download", {"expect_bytes": 5,
                                                      "source": "x"}).status_code, 409)
        reply = self.post("/maps/plan")
        self.assertEqual(reply.status_code, 202)
        self.assertEqual(reply.get_json()["job"]["kind"], "maps")
        self.assertEqual(reply.get_json()["maps"]["job"]["state"], "queued")
        os.makedirs(street_maps.maps_dir(), exist_ok=True)
        street_maps.save_plan({"source": "http://127.0.0.1:9/p.pmtiles", "download_bytes": 7})
        self.assertEqual(self.post("/maps/download", {"expect_bytes": 7,
                                                      "source": "http://elsewhere/"})
                         .status_code, 409)
        self.assertEqual(self.post("/maps/download", {"expect_bytes": True,
                                                      "source": "http://127.0.0.1:9/p.pmtiles"})
                         .status_code, 400)
        reply = self.post("/maps/download", {"expect_bytes": 7,
                                              "source": "http://127.0.0.1:9/p.pmtiles"})
        self.assertEqual(reply.status_code, 202)
        self.assertEqual(reply.get_json()["job"]["params"],
                         {"download": True, "expect_bytes": 7,
                          "source": "http://127.0.0.1:9/p.pmtiles"})
        # While a maps job waits, the maps are not deleted.
        self.assertEqual(self.post("/maps/remove").status_code, 409)
        self.assertEqual(self.post("/maps/nothing").status_code, 404)

    def test_a_large_download_needs_allowing(self):
        os.makedirs(street_maps.maps_dir(), exist_ok=True)
        source = "http://127.0.0.1:9/p.pmtiles"
        street_maps.save_plan({"source": source, "download_bytes": 7, "large": True})
        reply = self.post("/maps/download", {"expect_bytes": 7, "source": source})
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(self.post("/maps/download", {"expect_bytes": 7, "source": source,
                                                      "allow_large": "yes"}).status_code, 400)
        reply = self.post("/maps/download", {"expect_bytes": 7, "source": source,
                                             "allow_large": True})
        self.assertEqual(reply.status_code, 202)
        self.assertEqual(reply.get_json()["job"]["params"],
                         {"download": True, "expect_bytes": 7, "source": source,
                          "allow_large": True})

    def test_no_delete_while_another_job_or_tool_holds_the_lock(self):
        write_streets(street_maps.coverage_levels([street_maps.box_around(*PLACE, 1)], 8))
        lock = jobs.WorkerLock()
        self.assertTrue(lock.acquire("street_maps.py"))
        try:
            self.assertEqual(self.post("/maps/remove").status_code, 409)
        finally:
            lock.release()
        self.assertTrue(os.path.isfile(street_maps.archive_path()))
        self.assertEqual(self.post("/maps/remove").status_code, 200)

    def test_delete(self):
        write_streets(street_maps.coverage_levels([street_maps.box_around(*PLACE, 1)], 8))
        self.assertTrue(self.client.get("/api/admin/maps", headers=self.HEADERS)
                        .get_json()["available"])
        reply = self.post("/maps/remove")
        self.assertEqual(reply.status_code, 200)
        self.assertGreaterEqual(reply.get_json()["removed"], 1)
        self.assertFalse(reply.get_json()["maps"]["available"])

    def test_only_this_computer(self):
        response = self.client.get("/api/admin/maps", headers=self.HEADERS,
                                   environ_base={"REMOTE_ADDR": "192.0.2.7"})
        self.assertNotEqual(response.status_code, 200)


class ExportPlanTest(unittest.TestCase):

    def wanted(self, boxes, top):
        """coverage_levels from the zoom the pages draw street tiles at."""
        return {z: tiles for z, tiles in street_maps.coverage_levels(boxes, top).items()
                if z >= street_maps.DRAWN_FROM}

    def test_tiles_around_places_and_routes_of_those_there_are(self):
        have = street_maps.coverage_levels([AREA], 12)
        levels = street_maps.export_plan([PLACE], [[(10.0, 20.0), (10.0, 20.1)]], 3, 12,
                                         levels=have)
        self.assertEqual(max(levels), 12)
        # (None of the zooms the pages never draw street tiles at.)
        self.assertEqual(min(levels), street_maps.DRAWN_FROM)
        wanted = self.wanted(
            [street_maps.box_around(PLACE[0], PLACE[1], 3)]
            + [street_maps.box_around(lat, lon, 2.0)
               for lat, lon in street_maps.densify([(10.0, 20.0), (10.0, 20.1)], 1.0)], 12)
        self.assertEqual(levels, wanted)
        # Not deeper than asked, nor than there are; nothing without places or maps.
        self.assertEqual(max(street_maps.export_plan([PLACE], [], 3, 10, levels=have)), 10)
        self.assertEqual(max(street_maps.export_plan([PLACE], [], 3, 15, levels=have)), 12)
        self.assertEqual(street_maps.export_plan([], [], 3, 12, levels=have), {})
        self.assertEqual(street_maps.export_plan([PLACE], [], 3, 12, levels={}), {})
        # Outside what was downloaded: nothing (the world tiles of the coarse zooms that
        # hold it are not drawn).
        self.assertEqual(street_maps.export_plan([(-40.0, 100.0)], [], 3, 12, levels=have),
                         {})

    def test_no_tile_that_touches_home(self):
        have = street_maps.coverage_levels([AREA], 12)
        home = trip_export.HomeArea(PLACE, 10)
        # A place just east of the home area, whose 20 km reach well into it.
        place = (PLACE[0], home.west + home.width + 0.05)
        levels = street_maps.export_plan([place], [], 20, 12, home=home, levels=have)
        self.assertTrue(levels)
        everything = street_maps.export_plan([place], [], 20, 12, levels=have)
        self.assertGreater(sum(map(len, everything.values())),
                           sum(map(len, levels.values())))
        for z, tiles in levels.items():
            self.assertGreaterEqual(z, street_maps.DRAWN_FROM)
            for x, y in tiles:
                south, west, north, east = street_maps.tile_bounds(z, x, y)
                inside = (south < home.north and north > home.south and
                          west < home.west + home.width and east > home.west)
                self.assertFalse(inside, (z, x, y))


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class ExportTest(support.TempBaseMixin, unittest.TestCase):
    """A published site with street maps: only the tiles around what it shows, the
    credit, and no other host."""

    def setUp(self):
        super().setUp()
        self.family = self.base.library_folder("family")
        self.base.write_config([support.library("Family", self.family, id="lib_family1")])
        db.init(log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(street_maps.shared.forget)
        self.config = common.load_config()
        image_id = insert_image(self.conn, "lib_family1", "a.jpg", lat=PLACE[0],
                                lon=PLACE[1], described=1, prompt_ver=5, info=V5_INFO,
                                has_preview=0, taken_at="2020-06-01T12:00:00",
                                taken_source="exif")
        with cache.ThumbStore() as thumbs:
            thumbs.put(image_id, support.gradient_jpeg(64, 48))
        write_streets(street_maps.coverage_levels([AREA], 12))

    def export(self, **options):
        out = self.base.join("site")
        options.setdefault("images", "none")
        ctx = export.LogContext(log=support.quiet)
        with NoCalls(self):
            summary = export.export_library(ctx, self.conn, self.config, "lib_family1",
                                            out_dir=out, options=options)
        return out, summary

    def files(self, out):
        found = []
        for folder, _dirs, names in os.walk(out):
            for name in names:
                found.append(os.path.relpath(os.path.join(folder, name), out)
                             .replace(os.sep, "/"))
        return sorted(found)

    def test_only_the_tiles_around_the_published_places_go(self):
        out, _summary = self.export(location=True, street_maps=True, street_km=2,
                                    street_zoom=11)
        files = self.files(out)
        tiles = {tuple(int(part) for part in re.match(r"^maps/(\d+)/(\d+)/(\d+)\.js$", rel)
                       .groups()) for rel in files if re.match(r"^maps/\d+/", rel)}
        wanted = street_maps.coverage_levels([street_maps.box_around(PLACE[0], PLACE[1], 2)], 11)
        self.assertEqual(tiles, {(z, x, y) for z, level in wanted.items() for x, y in level
                                 if z >= street_maps.DRAWN_FROM})
        self.assertIn("maps/index.js", files)
        self.assertIn("maps/LICENSE.txt", files)
        self.assertIn("assets/streets.js", files)
        index_text = open(os.path.join(out, "maps", "index.js"), encoding="ascii").read()
        self.assertEqual(json.loads(index_text[len("STREETS_DATA.index("):-3])["maxzoom"], 11)
        page = open(os.path.join(out, "index.html"), encoding="utf-8").read()
        config = support.page_config(page)
        self.assertEqual(config["streets"]["url"], "maps/")
        self.assertTrue(config["capabilities"]["streets"])
        self.assertIn("OpenStreetMap contributors", page)
        self.assertIn('href="maps/LICENSE.txt"', page)
        map_page = open(os.path.join(out, "map.html"), encoding="utf-8").read()
        self.assertEqual(support.page_config(map_page)["streets"]["url"], "maps/")
        # The map page's corner credit links to the licence (map.js sets the address).
        self.assertIn('id="map-streets-licence" rel="license"', map_page)
        licence = open(os.path.join(out, "maps", "LICENSE.txt"), encoding="ascii").read()
        self.assertIn("openstreetmap.org/copyright", licence)
        # Every page says in its Content-Security-Policy that it asks no other host.
        for rel in files:
            if rel.endswith(".html"):
                text = open(os.path.join(out, *rel.split("/")), encoding="utf-8").read()
                match = re.search(r'<meta http-equiv="Content-Security-Policy" '
                                  r'content="([^"]+)">', text)
                self.assertIsNotNone(match, rel)
                policy = match.group(1).replace("&#x27;", "'")
                self.assertIn("default-src 'self'", policy)
                self.assertIn("connect-src 'self'", policy)
                self.assertEqual(policy, export.EXPORT_CSP)
        # Nothing the site loads names another host.
        for rel in files:
            if rel.endswith((".html", ".js", ".css", ".json")):
                with open(os.path.join(out, *rel.split("/")), encoding="utf-8") as handle:
                    self.assertEqual(other_hosts(handle.read()), [], rel)
        # A second export writes no tile again; one without street maps removes them.
        _out, again = self.export(location=True, street_maps=True, street_km=2,
                                  street_zoom=11)
        self.assertEqual(again["written"], 0)
        _out, plain = self.export(location=True)
        self.assertFalse([rel for rel in self.files(out) if rel.startswith("maps/")])
        self.assertNotIn("streets", support.page_config(
            open(os.path.join(out, "index.html"), encoding="utf-8").read()))

    def test_the_free_space_check_counts_the_street_maps(self):
        options = {"location": True, "street_maps": True, "street_km": 2, "street_zoom": 11}
        preview = export.preview_export(self.conn, self.config, "lib_family1",
                                        export.validate_options(options), reachable=True)
        counted = []
        real = export._Export.check_space

        def check_space(job, chosen, rows, sizes, extra=0):
            counted.append(extra)
            return real(job, chosen, rows, sizes, extra=extra)
        with mock.patch.object(export._Export, "check_space", check_space):
            self.export(**options)
        self.assertEqual(counted, [preview["counts"]["street_maps"]["bytes"]])
        self.assertGreater(counted[0], 0)
        # Street maps larger than the free space: refused before anything is written.
        with mock.patch.object(export._Export, "street_bytes", return_value=10 ** 18):
            with self.assertRaises(export.ExportError) as caught:
                self.export(**options)
        self.assertIn("not enough free space", str(caught.exception))

    def test_the_export_server_sends_the_policy_too(self):
        out, _summary = self.export(location=True, street_maps=True)
        server = export.make_server(out, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/index.html" % server.server_address[1]
            with urllib.request.urlopen(url, timeout=30) as response:
                self.assertEqual(response.headers["Content-Security-Policy"],
                                 export.EXPORT_CSP)
        finally:
            server.shutdown()
            server.server_close()

    def test_no_street_maps_without_location(self):
        out, _summary = self.export(location=False, street_maps=True)
        self.assertFalse([rel for rel in self.files(out) if rel.startswith("maps/")])

    def test_the_review_counts_them(self):
        options = export.validate_options({"location": True, "street_maps": True,
                                           "street_km": 2, "street_zoom": 11})
        preview = export.preview_export(self.conn, self.config, "lib_family1", options,
                                        reachable=True)
        counts = preview["counts"]["street_maps"]
        self.assertEqual((counts["available"], counts["on"]), (True, True))
        wanted = street_maps.coverage_levels([street_maps.box_around(PLACE[0], PLACE[1], 2)], 11)
        self.assertEqual(counts["tiles"], sum(len(level) for z, level in wanted.items()
                                              if z >= street_maps.DRAWN_FROM))
        self.assertGreater(counts["bytes"], 0)
        self.assertEqual(preview["size_parts"]["maps"], counts["bytes"])
        off = export.preview_export(self.conn, self.config, "lib_family1",
                                    export.validate_options({"location": True}), reachable=True)
        self.assertEqual(off["counts"]["street_maps"]["on"], False)
        self.assertEqual(off["size_parts"]["maps"], 0)
        street_maps.remove()
        gone = export.preview_export(self.conn, self.config, "lib_family1", options,
                                     reachable=True)
        self.assertFalse(gone["counts"]["street_maps"]["available"])

    def test_options_are_checked(self):
        self.assertEqual(export.validate_options({})["street_maps"], False)
        for bad in ({"street_maps": 1}, {"street_km": 0}, {"street_km": 51},
                    {"street_zoom": 9}, {"street_zoom": 16}, {"street_zoom": True}):
            with self.assertRaises(export.OptionsError):
                export.validate_options(bad)
        summary = export.options_summary(export.validate_options(
            {"location": True, "street_maps": True}))
        self.assertTrue(summary["street_maps"])


class TripExportTest(_trip_tests.TripExportTest):
    """A trip's export with street maps and home kept out: the trip of test_trip_export's
    made-up libraries, home and dashcam (its setUp; none of its tests: see below), and
    one more picture of the trip just outside the home area, whose street maps reach
    into it."""

    def setUp(self):
        super().setUp()
        self.trips = _trip_tests
        test_trip_export = _trip_tests
        self.addCleanup(street_maps.shared.forget)
        self.area = trip_export.HomeArea(test_trip_export.HOME, trip_export.HOME_KM)
        self.near = (test_trip_export.HOME[0], self.area.west + self.area.width + 0.05)
        near_id = insert_image(self.conn, test_trip_export.LAKES, "near.jpg",
                               lat=self.near[0], lon=self.near[1], described=1,
                               prompt_ver=5, info=V5_INFO, has_preview=0,
                               taken_at="2024-05-11T09:00:00", taken_source="exif")
        with cache.ThumbStore() as thumbs:
            thumbs.put(near_id, support.gradient_jpeg(64, 48))
        # Street maps around home (and the near picture) and the lake, zoom 0 to 13.
        write_streets(street_maps.coverage_levels(
            [street_maps.box_around(test_trip_export.HOME[0], test_trip_export.HOME[1], 60),
             street_maps.box_around(test_trip_export.LAKE[0], test_trip_export.LAKE[1], 30)],
            13))

    def test_no_street_map_of_home_goes_and_the_review_counts_what_does(self):
        options = {"location": True, "routes": True, "routes_trim": 0, "images": "none",
                   "street_maps": True, "street_km": 20, "street_zoom": 13}
        preview = trip_export.preview_trip(self.conn, common.load_config(),
                                           self.trips.LAKES, options, reachable=True)
        out = os.path.join(common.exports_dir(), "trip-streets")
        with NoCalls(self):
            trip_export.export_trip(jobs.JobContext("test"), self.conn, common.load_config(),
                                    self.trips.LAKES, out, options)
        tiles = []
        for folder, _dirs, names in os.walk(os.path.join(out, "maps")):
            for name in names:
                rel = os.path.relpath(os.path.join(folder, name), out).replace(os.sep, "/")
                match = re.match(r"^maps/(\d+)/(\d+)/(\d+)\.js$", rel)
                if match:
                    tiles.append(tuple(int(part) for part in match.groups()))
        self.assertTrue(tiles)
        # The near picture's street maps go, but none that touches home, at any zoom
        # the pages draw (and none of the coarser ones, which they do not draw).
        self.assertTrue(any(z == 13 and street_maps.tile_bounds(z, x, y)[1] <
                            self.near[1] + 0.2 for z, x, y in tiles))
        for z, x, y in tiles:
            self.assertGreaterEqual(z, street_maps.DRAWN_FROM, (z, x, y))
            self.assertFalse(street_maps._touches_home(self.area, z, x, y), (z, x, y))
        # The review counted exactly those; the index lists exactly those.
        self.assertEqual(preview["counts"]["street_maps"]["tiles"], len(tiles))
        text = open(os.path.join(out, "maps", "index.js"), encoding="ascii").read()
        index = json.loads(text[len("STREETS_DATA.index("):-3])
        listed = {(int(z), x, row[0]) for z, rows in index["rows"].items() for row in rows
                  for start, end in zip(row[1::2], row[2::2]) for x in range(start, end + 1)}
        self.assertEqual(listed, set(tiles))
        self.assertEqual(index["minzoom"], street_maps.DRAWN_FROM)
        # Its pages credit the street maps and say they ask no other host.
        page = open(os.path.join(out, "index.html"), encoding="utf-8").read()
        self.assertIn("OpenStreetMap contributors", page)
        self.assertIn("Content-Security-Policy", page)


# (TripExportTest runs only its own test, not those of the fixture it borrows.)
for _name in dir(_trip_tests.TripExportTest):
    if _name.startswith("test") and _name not in TripExportTest.__dict__:
        setattr(TripExportTest, _name, None)


@unittest.skipIf(NODE is None, "Node.js is not installed")
class StreetsScriptTest(unittest.TestCase):

    def test_the_renderer(self):
        # tests/js/streets_test.js: gzip, vector tiles, the projection, the world image
        # in Web Mercator, which street tiles a view asks for, drawing them with names.
        result = subprocess.run([NODE, STREETS_TESTS], cwd=support.REPO, capture_output=True,
                                timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
