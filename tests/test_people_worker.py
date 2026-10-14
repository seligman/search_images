"""Tests for people_worker.py, the face helper People runs in its own Python environment.

Its box arithmetic (the image pyramid, merging the faces seen at several
sizes) and its protocol are tested here with a stand-in backend, so
neither numpy nor OpenCV is needed; the real detector and recogniser are
only run by tests/test_people_live.py, when a people environment is
named (see there).
"""

import base64
import io
import json
import math
import os
import struct
import subprocess
import sys
import unittest
from unittest import mock

import support

import people_worker


class Stub:
    """A backend whose detector finds the faces given per size (width, height)."""

    def __init__(self, width, height, rows_by_size=None, fail_decode=False):
        self.width, self.height = width, height
        self.rows_by_size = rows_by_size or {}
        self.fail_decode = fail_decode
        self.loaded = None
        self.seen = []

    def versions(self):
        return {"opencv": "stub", "numpy": "stub"}

    def load(self, detector, recognizer, threads=0):
        if detector == "missing":
            raise people_worker.WorkerError("There is no model file missing.")
        self.loaded = (detector, recognizer, threads)

    def decode(self, data):
        if self.fail_decode:
            raise people_worker.WorkerError("The picture cannot be decoded.")
        return "image", self.width, self.height

    def detect(self, image, width, height):
        self.seen.append((width, height))
        return [list(row) for row in self.rows_by_size.get((width, height), [])]

    def embed(self, image, face):
        values = [0.0] * people_worker.EMBEDDING_DIMS
        values[0] = 1.0
        return struct.pack("<%df" % len(values), *values), 9.5, 120.0


def row(x, y, w, h, score):
    marks = []
    for fx, fy in ((0.3, 0.4), (0.7, 0.4), (0.5, 0.6), (0.35, 0.8), (0.65, 0.8)):
        marks += [x + fx * w, y + fy * h]
    return [x, y, w, h] + marks + [score]


class BoxesTest(unittest.TestCase):

    def test_the_pyramid_halves_down_to_the_smallest_size(self):
        sizes = people_worker.pyramid(4000, 3000, 2048, 256)
        self.assertEqual([(w, h) for _scale, w, h in sizes],
                         [(2048, 1536), (1024, 768), (512, 384), (256, 192)])
        self.assertAlmostEqual(sizes[0][0], 2048 / 4000.0)
        # A picture smaller than max_edge starts at its own size, and a small one
        # is looked at once.
        self.assertEqual([(w, h) for _s, w, h in people_worker.pyramid(1200, 900, 2048, 256)],
                         [(1200, 900), (600, 450), (300, 225)])
        self.assertEqual([(w, h) for _s, w, h in people_worker.pyramid(300, 200, 2048, 256)],
                         [(300, 200)])
        self.assertEqual(people_worker.pyramid(0, 10), [])
        # min_edge is never above max_edge; neither goes below SMALLEST_EDGE.
        self.assertEqual(len(people_worker.pyramid(2000, 1000, 640, 4000)), 1)
        self.assertEqual(people_worker.pyramid(2000, 1000, 1, 1)[0][1:],
                         (people_worker.SMALLEST_EDGE, people_worker.SMALLEST_EDGE // 2))

    def test_each_size_keeps_the_faces_of_its_band(self):
        low, high = people_worker.BAND_LOW, people_worker.BAND_HIGH
        self.assertTrue(people_worker.in_band(100, 1, 3))
        self.assertFalse(people_worker.in_band(low - 1, 1, 3))
        self.assertTrue(people_worker.in_band(low - 1, 0, 3))       # the largest size
        self.assertFalse(people_worker.in_band(high + 1, 1, 3))
        self.assertTrue(people_worker.in_band(high + 1, 2, 3))      # the smallest size
        self.assertTrue(people_worker.in_band(high + 1, 0, 1))

    def test_overlap_and_merge(self):
        iou, inside = people_worker.overlap([0, 0, 10, 10], [5, 0, 10, 10])
        self.assertAlmostEqual(iou, 50 / 150.0)
        self.assertAlmostEqual(inside, 0.5)
        self.assertEqual(people_worker.overlap([0, 0, 10, 10], [20, 20, 5, 5]), (0.0, 0.0))
        self.assertEqual(people_worker.overlap([0, 0, 10, 10], [0, 0, 10, 10])[0], 1.0)
        faces = [
            {"box": [100, 100, 50, 50], "score": 0.8},      # the same face, less sure
            {"box": [102, 98, 52, 50], "score": 0.95},
            {"box": [110, 110, 10, 10], "score": 0.9},      # inside it: a part of it
            {"box": [300, 100, 40, 40], "score": 0.75},     # another face
        ]
        kept = people_worker.merge(faces)
        self.assertEqual([face["score"] for face in kept], [0.95, 0.75])

    def test_request_numbers_are_checked(self):
        options = people_worker.options_of({})
        self.assertEqual(options, {"max_edge": 2048, "min_edge": 256, "min_size": 20,
                                   "min_score": 0.7, "max_faces": 64, "tile": 0, "overlap": 0,
                                   "crop_edge": 0, "crop_margin": 0.0})
        options = people_worker.options_of({"max_edge": 99999, "min_edge": 99999,
                                            "min_score": float("nan"), "min_size": "x",
                                            "max_faces": 0})
        # (Without tiles a picture is looked at whole: at most UNTILED_MAX_EDGE.)
        self.assertEqual((options["max_edge"], options["min_edge"], options["min_score"],
                          options["min_size"], options["max_faces"]),
                         (people_worker.UNTILED_MAX_EDGE, people_worker.UNTILED_MAX_EDGE, 0.7,
                          20, 1))
        # Tiles let it be looked at in full size; they overlap by at most half a tile.
        options = people_worker.options_of({"max_edge": 99999, "tile": 100, "overlap": 9999,
                                            "crop_edge": 5, "crop_margin": 9})
        self.assertEqual((options["max_edge"], options["tile"], options["overlap"],
                          options["crop_edge"], options["crop_margin"]),
                         (people_worker.LARGEST_EDGE, people_worker.SMALLEST_TILE,
                          people_worker.SMALLEST_TILE // 2, people_worker.SMALLEST_CROP, 2.0))
        options = people_worker.options_of({"tile": 2048, "overlap": 416, "crop_edge": 160,
                                            "crop_margin": 0.45, "max_edge": 30000})
        self.assertEqual((options["max_edge"], options["tile"], options["overlap"],
                          options["crop_edge"], options["crop_margin"]),
                         (30000, 2048, 416, 160, 0.45))


class FindFacesTest(unittest.TestCase):

    def test_faces_of_every_size_in_picture_pixels(self):
        stub = Stub(4096, 3072, {
            # At 2048 x 1536 (scale 0.5): a small face, and a part of a large one.
            (2048, 1536): [row(100, 100, 30, 30, 0.9), row(1000, 600, 100, 120, 0.72)],
            # At 1024 x 768: the small face again (too small here: outside the band),
            # and the large face.
            (1024, 768): [row(50, 50, 15, 15, 0.95), row(400, 250, 300, 300, 0.93)],
            # At 256 x 192: the large face again, less sure.
            (256, 192): [row(100, 62, 75, 75, 0.85)],
            # At 512 x 384: something not sure enough.
            (512, 384): [row(10, 10, 40, 40, 0.5)],
        })
        found = people_worker.find_faces(stub, b"x", people_worker.options_of({}))
        self.assertEqual(stub.seen, [(2048, 1536), (1024, 768), (512, 384), (256, 192)])
        self.assertEqual((found["width"], found["height"]), (4096, 3072))
        faces = found["faces"]
        # The large face (outside the band at 1024: 300 > 288, so the one at 256
        # is kept) and the small face; the part inside the large face goes.
        self.assertEqual([face["score"] for face in faces], [0.9, 0.85])
        self.assertEqual(faces[0]["box"], [200.0, 200.0, 60.0, 60.0])
        self.assertEqual(faces[0]["edge"], 2048)
        self.assertEqual(faces[1]["box"], [1600.0, 992.0, 1200.0, 1200.0])
        self.assertEqual(faces[1]["edge"], 256)
        self.assertEqual(faces[1]["landmarks"][0], [1600 + 0.3 * 1200, 992 + 0.4 * 1200])
        vector = struct.unpack("<128f", base64.b64decode(faces[0]["embedding"]))
        self.assertEqual(vector[0], 1.0)
        self.assertEqual((faces[0]["norm"], faces[0]["sharpness"]), (9.5, 120.0))

    def test_size_score_and_count_limits(self):
        rows = [row(10 + 100 * k, 10, 20 + 10 * k, 20 + 10 * k, 0.8 + 0.01 * k)
                for k in range(6)]
        stub = Stub(800, 600, {(800, 600): rows})
        options = people_worker.options_of({"min_size": 35, "max_faces": 3, "min_edge": 800})
        faces = people_worker.find_faces(stub, b"x", options)["faces"]
        # 2..5 are large enough; the 3 surest of them are kept.
        self.assertEqual([face["box"][2] for face in faces], [70.0, 60.0, 50.0])
        options = people_worker.options_of({"min_score": 0.84, "min_edge": 800})
        self.assertEqual(len(people_worker.find_faces(stub, b"x", options)["faces"]), 2)

    def test_a_picture_that_cannot_be_read(self):
        with self.assertRaises(people_worker.WorkerError):
            people_worker.find_faces(Stub(10, 10, fail_decode=True), b"x",
                                     people_worker.options_of({}))


class Scene:
    """A backend whose picture holds faces at places given in its own pixels (x, y, side,
    score): the detector sees each face that lies in what it looks at (a size of the
    pyramid, or a tile of one), whole or cut off by the tile's sides, as a square
    (cut: less sure, as a real detector is of half a face), when it is at least
    SMALLEST pixels there (YuNet sees no smaller faces)."""

    SMALLEST = 10

    def __init__(self, width, height, faces):
        self.width, self.height = width, height
        self.faces = faces
        self.whole = []         # (width, height) of the sizes looked at whole
        self.parts = []         # (level width, level height, x, y, w, h) of the tiles
        self.crops = []

    def decode(self, data):
        return ("picture", self.width, self.height), self.width, self.height

    def scaled(self, image, width, height):
        return ("level", width, height)

    def detect(self, image, width, height):
        self.whole.append((width, height))
        return self._seen(width, height, (0, 0, width, height))

    def detect_part(self, level, x, y, width, height):
        self.parts.append((level[1], level[2], x, y, width, height))
        return self._seen(level[1], level[2], (x, y, width, height))

    def _seen(self, width, height, part):
        scale = width / float(self.width)
        left, top, part_width, part_height = part
        rows = []
        for x, y, side, score in self.faces:
            if side * scale < self.SMALLEST:
                continue
            x0, y0 = max(left, x * scale), max(top, y * scale)
            x1 = min(left + part_width, (x + side) * scale)
            y1 = min(top + part_height, (y + side) * scale)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            cut = (x1 - x0, y1 - y0) != (side * scale, side * scale)
            rows.append(row(x0 - left, y0 - top, x1 - x0, y1 - y0, score - (0.2 if cut else 0)))
        return rows

    def embed(self, image, face):
        return Stub.embed(None, image, face)

    def crop(self, image, box, edge, margin):
        self.crops.append((tuple(box), edge, margin))
        return b"\xff\xd8\xff-face-%d" % len(self.crops)


class TilesTest(unittest.TestCase):

    def test_tiles_cover_the_picture_and_overlap(self):
        for length in (1, 512, 2048, 2049, 3000, 4095, 16382, 30000):
            for tile, overlap in ((2048, 416), (512, 256), (1024, 0)):
                spans = people_worker.spans(length, tile, overlap)
                self.assertEqual(spans[0][0], 0, (length, tile))
                self.assertEqual(spans[-1][0] + spans[-1][1], length, (length, tile))
                self.assertTrue(all(size <= tile for _start, size in spans), (length, tile))
                self.assertEqual(len({size for _start, size in spans}), 1)
                for (start, size), (after, _size) in zip(spans, spans[1:]):
                    self.assertGreaterEqual(start + size - after, overlap - 1, (length, tile))
                # (As few as can be: one fewer could not cover it.)
                if len(spans) > 1:
                    self.assertLess((len(spans) - 1) * (tile - overlap) + overlap, length)
        self.assertEqual(people_worker.spans(1500, 2048, 416), [(0, 1500)])
        self.assertEqual(people_worker.spans(3600, 2048, 416), [(0, 2008), (1592, 2008)])
        self.assertEqual(people_worker.spans(4000, 2048, 416),
                         [(0, 1611), (1194, 1611), (2389, 1611)])
        found = people_worker.tiles(5000, 3000, 2048, 416)
        self.assertEqual(len(found), 3 * 2)
        self.assertEqual(found[0][:2], (0, 0))
        self.assertEqual((found[-1][0] + found[-1][2], found[-1][1] + found[-1][3]),
                         (5000, 3000))
        self.assertEqual(people_worker.tiles(800, 600, 2048, 416), [(0, 0, 800, 600)])

    def test_a_face_cut_by_a_side_of_its_tile(self):
        part = (1000, 0, 2048, 2048)        # inside the picture on the left and right
        cut = people_worker.cut_by_tile
        self.assertTrue(cut((0, 100, 50, 50), part, 5000, 2048))
        self.assertTrue(cut((2000, 100, 47, 50), part, 5000, 2048))
        self.assertFalse(cut((10, 100, 50, 50), part, 5000, 2048))
        # (Its top and bottom are those of the picture: a face there is whole.)
        self.assertFalse(cut((10, 0, 50, 50), part, 5000, 2048))
        self.assertFalse(cut((10, 2000, 50, 48), part, 5000, 2048))
        self.assertFalse(cut((0, 100, 50, 50), (0, 0, 2048, 2048), 5000, 2048))


class FullSizeTest(unittest.TestCase):

    def test_a_wide_picture_is_looked_at_in_tiles(self):
        # A panorama 16000 x 3800 with small faces (40 to 120 pixels), one on the
        # boundary between two tiles, one at the picture's own edge, and a large one.
        faces = [(500, 1000, 40, 0.9), (1900, 2000, 120, 0.91), (15960, 3000, 40, 0.92),
                 (7000, 400, 1200, 0.95), (3000, 3700, 60, 0.93)]
        scene = Scene(16000, 3800, faces)
        options = people_worker.options_of({"max_edge": 99999, "tile": 2048, "overlap": 416,
                                            "crop_edge": 160, "crop_margin": 0.45})
        found = people_worker.find_faces(scene, b"x", options)
        boxes = sorted(tuple(int(round(value)) for value in face["box"])
                       for face in found["faces"])
        # Every face once, whole (the parts tiles saw of the one on a boundary went).
        self.assertEqual(boxes, [(500, 1000, 40, 40), (1900, 2000, 120, 120),
                                 (3000, 3700, 60, 60), (7000, 400, 1200, 1200),
                                 (15960, 3000, 40, 40)])
        self.assertTrue(all(face["score"] >= 0.9 for face in found["faces"]))
        # The full size and the next ones were looked at in tiles of at most 2048 pixels;
        # the small sizes whole.
        levels = sorted({(width, height) for width, height, _x, _y, _w, _h in scene.parts},
                        reverse=True)
        self.assertEqual(levels[0], (16000, 3800))
        self.assertTrue(all(w <= 2048 and h <= 2048 for _lw, _lh, _x, _y, w, h in scene.parts))
        self.assertTrue(all(max(size) <= 2048 for size in scene.whole))
        self.assertEqual(len(scene.parts) + len(scene.whole),
                         sum(len(people_worker.tiles(w, h, 2048, 416))
                             for _scale, w, h in people_worker.pyramid(16000, 3800, 99999, 256)))
        # A picture of each face, cut from the picture as sent.
        self.assertEqual(len(scene.crops), 5)
        self.assertEqual({(edge, margin) for _box, edge, margin in scene.crops}, {(160, 0.45)})
        self.assertTrue(all(base64.b64decode(face["crop"]).startswith(b"\xff\xd8\xff")
                            for face in found["faces"]))

    def test_without_tiles_nothing_changes(self):
        # (The pictures looked at in their preview are looked at as before: every size
        # whole, and no faces' pictures.)
        scene = Scene(4000, 3000, [(400, 300, 100, 0.9)])
        found = people_worker.find_faces(scene, b"x", people_worker.options_of({}))
        self.assertEqual(scene.parts, [])
        self.assertEqual(scene.whole, [(w, h) for _s, w, h in people_worker.pyramid(4000, 3000)])
        self.assertEqual(len(found["faces"]), 1)
        self.assertNotIn("crop", found["faces"][0])
        self.assertEqual(scene.crops, [])
        # A face missed in a preview (too small there) is found in full size.
        small = Scene(16000, 3800, [(8000, 1900, 60, 0.9)])
        self.assertEqual(people_worker.find_faces(small, b"x",
                                                  people_worker.options_of({}))["faces"], [])
        found = people_worker.find_faces(small, b"x", people_worker.options_of(
            {"max_edge": 99999, "tile": 2048, "overlap": 416}))["faces"]
        self.assertEqual([face["box"] for face in found], [[8000.0, 1900.0, 60.0, 60.0]])


class ProtocolTest(unittest.TestCase):

    def talk(self, requests, factory):
        reader = io.BytesIO(b"".join(
            (item if isinstance(item, bytes) else json.dumps(item).encode("utf-8")) + b"\n"
            for item in requests))
        writer = io.BytesIO()
        people_worker.serve(reader, writer, factory)
        return [json.loads(line) for line in writer.getvalue().splitlines()]

    def test_a_session(self):
        stub = Stub(640, 480, {(640, 480): [row(100, 100, 80, 80, 0.93)]})
        picture = base64.b64encode(b"picture").decode("ascii")
        answers = self.talk([
            {"id": 1, "op": "hello"},
            {"id": 2, "op": "faces", "image": picture},
            {"id": 3, "op": "load", "detector": "missing", "recognizer": "r"},
            {"id": 4, "op": "load", "detector": "d", "recognizer": "r", "threads": 2},
            {"id": 5, "op": "faces", "image": picture},
            {"id": 6, "op": "faces", "image": "not base64!"},
            {"id": 7, "op": "faces", "image": ""},
            {"id": 8, "op": "dance"},
            b"this is not JSON",
            b"[1, 2]",
            {"id": 9, "op": "quit"},
            {"id": 10, "op": "hello"},         # (never read: the helper quit)
        ], lambda: stub)
        self.assertEqual([answer["id"] for answer in answers], [1, 2, 3, 4, 5, 6, 7, 8, None,
                                                                None, 9])
        hello = answers[0]
        self.assertEqual((hello["ok"], hello["worker"], hello["opencv"]),
                         (True, people_worker.WORKER_VERSION, "stub"))
        self.assertEqual(answers[1], {"ok": False, "error": "The models are not loaded.", "id": 2})
        self.assertIn("missing", answers[2]["error"])
        self.assertEqual(stub.loaded, ("d", "r", 2))
        faces = answers[4]["faces"]
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0]["box"], [100.0, 100.0, 80.0, 80.0])
        self.assertIn("seconds", answers[4])
        self.assertIn("base64", answers[5]["error"])
        self.assertIn("no picture", answers[6]["error"])
        self.assertIn("Unknown request", answers[7]["error"])
        self.assertIn("cannot be read", answers[8]["error"])
        self.assertIn("JSON object", answers[9]["error"])
        self.assertTrue(answers[10]["ok"])

    def test_without_opencv_hello_says_so(self):
        def factory():
            error = ImportError("No module named 'cv2'")
            error.name = "cv2"
            raise error
        answers = self.talk([{"id": 1, "op": "hello"}, {"id": 2, "op": "load", "detector": "d",
                                                        "recognizer": "r"}], factory)
        self.assertTrue(answers[0]["ok"])
        self.assertIn("no cv2", answers[0]["problem"])
        self.assertFalse(answers[1]["ok"])

    def test_anything_a_backend_raises_is_one_failed_request(self):
        class Broken(Stub):
            def detect(self, image, width, height):
                raise RuntimeError("cv2.error: something inside OpenCV")
        stub = Broken(64, 64)
        picture = base64.b64encode(b"x").decode("ascii")
        answers = self.talk([{"id": 1, "op": "load", "detector": "d", "recognizer": "r"},
                             {"id": 2, "op": "faces", "image": picture},
                             {"id": 3, "op": "hello"}], lambda: stub)
        self.assertIn("RuntimeError", answers[1]["error"])
        self.assertTrue(answers[2]["ok"])

    def test_a_request_too_long_is_skipped_whole(self):
        with mock.patch.object(people_worker, "MAX_LINE", 50):
            answers = self.talk([{"id": 1, "op": "hello", "padding": "x" * 200},
                                 {"id": 2, "op": "hello"}], lambda: Stub(1, 1))
        self.assertEqual(len(answers), 2)
        self.assertIn("too long", answers[0]["error"])
        self.assertTrue(answers[1]["ok"])


class ProgramTest(unittest.TestCase):
    """The real program, run with this Python (which may lack OpenCV)."""

    def test_it_answers_on_its_standard_output_only(self):
        script = os.path.join(support.REPO, "people_worker.py")
        requests = [{"id": 1, "op": "hello"}, {"id": 2, "op": "faces", "image": "eA=="},
                    {"id": 3, "op": "quit"}]
        completed = subprocess.run(
            [sys.executable, "-I", "-B", script],
            input=b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in requests),
            capture_output=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        answers = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([answer["id"] for answer in answers], [1, 2, 3])
        hello = answers[0]
        self.assertTrue(hello["ok"])
        self.assertEqual(hello["python"], "%d.%d.%d" % sys.version_info[:3])
        # Without OpenCV hello says what is missing; with it, its version.
        self.assertTrue(hello.get("problem") or hello.get("opencv"), hello)
        self.assertEqual(answers[1]["error"], "The models are not loaded.")


if __name__ == "__main__":
    unittest.main()
