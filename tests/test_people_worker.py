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
                                   "min_score": 0.7, "max_faces": 64})
        options = people_worker.options_of({"max_edge": 99999, "min_edge": 99999,
                                            "min_score": float("nan"), "min_size": "x",
                                            "max_faces": 0})
        self.assertEqual((options["max_edge"], options["min_edge"], options["min_score"],
                          options["min_size"], options["max_faces"]),
                         (people_worker.LARGEST_EDGE, people_worker.LARGEST_EDGE, 0.7, 20, 1))


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
