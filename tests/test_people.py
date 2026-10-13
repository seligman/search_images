"""Tests for People (people.py): settings, the face store (data/faces.db), the helper
program it runs, the faces job and scan step, the Manage page's counts and actions,
and the command line.

The helper is tests/fake_people_worker.py, run with this Python: it speaks
the real protocol and "finds" faces drawn as solid squares (no numpy, no
OpenCV, no photos of anyone). Every test runs in a temporary SETTINGS_BASE.
"""

import ast
import base64
import io
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

import common
import db
import jobs
import people

try:
    import flask
except ImportError:
    flask = None

if support.pillow_available():
    from PIL import Image, ImageDraw
    import fake_people_worker
else:
    Image = ImageDraw = fake_people_worker = None

NOW = 1700000000.0
FAKE_WORKER = os.path.join(support.REPO, "tests", "fake_people_worker.py")
JS_TESTS = os.path.join(support.REPO, "tests", "js", "people_test.js")
JS_PAGE_TESTS = os.path.join(support.REPO, "tests", "js", "people_pages_test.js")
quiet = support.quiet


def insert_image(conn, library_id, path, **fields):
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 400, "height": 300,
           "added": NOW, "updated": NOW, "content_hash": "hash-" + path}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute("INSERT INTO images (%s) VALUES (%s)"
                          % (",".join(columns), ",".join("?" * len(columns))),
                          [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


def picture(faces=(), size=(400, 300), corner=None, kind="PNG"):
    """Bytes of a picture with faces [(person 1..7, x, y, side)] drawn as squares."""
    image = Image.new("RGB", size, (200, 200, 200))
    draw = ImageDraw.Draw(image)
    for person, x, y, side in faces:
        draw.rectangle([x, y, x + side - 1, y + side - 1],
                       fill=fake_people_worker.face_colour(person))
    if corner is not None:
        draw.rectangle([0, 0, 3, 3], fill=corner)
    out = io.BytesIO()
    image.save(out, kind, **({"quality": 95} if kind == "JPEG" else {}))
    return out.getvalue()


CRASH = (0, 255, 0)
FAIL = (0, 0, 255)
SLOW = (255, 255, 0)


class Context(jobs.JobContext):
    """A job context that keeps its log lines (and can be told to stop after n steps)."""

    def __init__(self, stop_after=None):
        jobs.JobContext.__init__(self, "test")
        self.lines = []
        self.stop_after = stop_after

    def _emit(self, entry):
        self.lines.append((entry["level"], entry["message"]))

    def _progress(self):
        if self.stop_after is not None and self.done >= self.stop_after:
            self.request_stop()

    def text(self):
        return "\n".join(message for _level, message in self.lines)


# ---------------------------------------------------------------------------

class SettingsTest(support.TempBaseMixin, unittest.TestCase):

    def test_defaults_and_values_that_cannot_be_used(self):
        self.assertEqual(people.settings({}), people.DEFAULTS)
        self.assertFalse(people.DEFAULTS["enabled"])
        options = people.settings({"people": {
            "enabled": "yes", "after_scan": 0, "workers": 0, "threads": 2.5, "max_edge": 99999,
            "min_score": 2, "min_size": True, "max_faces": "9", "python": " \"py\" ",
            "detector": "", "recognizer": " r.onnx "}})
        self.assertFalse(options["enabled"])                 # (only true turns it on)
        self.assertTrue(options["after_scan"])               # (only false turns it off)
        for key in ("workers", "threads", "max_edge", "min_score", "min_size", "max_faces"):
            self.assertEqual(options[key], people.DEFAULTS[key], key)
        self.assertEqual((options["python"], options["detector"], options["recognizer"]),
                         ("py", people.DETECTOR_FILE, "r.onnx"))
        options = people.settings({"people": {"enabled": True, "after_scan": False,
                                              "workers": 3, "threads": 0, "max_edge": 1024,
                                              "min_score": 0.8, "min_size": 40,
                                              "max_faces": 10}})
        self.assertEqual((options["enabled"], options["after_scan"], options["workers"],
                          options["threads"], options["max_edge"], options["min_score"],
                          options["min_size"], options["max_faces"]),
                         (True, False, 3, 0, 1024, 0.8, 40, 10))
        self.assertTrue(people.enabled({"people": {"enabled": True}}))
        self.assertFalse(people.enabled({"people": True}))

    def test_paths_and_what_is_missing(self):
        options = people.settings({"people": {"enabled": True}})
        self.assertIn("Set \"python\"", people.setup_problem(options))
        self.assertIn("People is off", people.setup_problem(people.settings({})))
        # An environment's folder stands for its python.
        env = self.base.make_dir("env")
        script_dir = self.base.make_dir("env", "Scripts" if os.name == "nt" else "bin")
        python = os.path.join(script_dir, "python.exe" if os.name == "nt" else "python3")
        with open(python, "wb") as handle:
            handle.write(b"")
        options = people.settings({"people": {"enabled": True, "python": "env",
                                              "models_dir": "models"}})
        self.assertEqual(people.python_path(options), python)
        self.assertIn("face detector model", people.setup_problem(options))
        self.assertEqual(people.model_path(options, "detector"),
                         self.base.join("models", people.DETECTOR_FILE))
        self.base.write_file("models/" + people.DETECTOR_FILE, b"d")
        self.assertIn("face recogniser model", people.setup_problem(options))
        self.base.write_file("other/r.onnx", b"r")
        options = people.settings({"people": {"enabled": True, "python": python,
                                              "models_dir": "models",
                                              "recognizer": "other/r.onnx"}})
        self.assertEqual(people.model_path(options, "recognizer"),
                         self.base.join("other", "r.onnx"))
        self.assertEqual(people.setup_problem(options), "")
        options["python"] = self.base.join("nothing", "python.exe")
        self.assertIn("is not there", people.setup_problem(options))
        command = people.worker_command(dict(options, python=python))
        self.assertEqual(command, [python, "-I", "-B", people.WORKER_SCRIPT])

    def test_the_recipe_follows_the_model_files_and_what_is_found(self):
        self.base.write_file("models/" + people.DETECTOR_FILE, b"detector")
        self.base.write_file("models/" + people.RECOGNIZER_FILE, b"recognizer")
        options = people.settings({"people": {"enabled": True, "models_dir": "models"}})
        first = people.recipe(options)
        self.assertEqual(people.recipe(dict(options, workers=8, threads=3)), first)
        for change in ({"min_score": 0.8}, {"max_edge": 1024}, {"min_size": 30},
                       {"max_faces": 5}):
            self.assertNotEqual(people.recipe(dict(options, **change)), first, change)
        time.sleep(0.01)
        self.base.write_file("models/" + people.DETECTOR_FILE, b"another detector")
        self.assertNotEqual(people.recipe(options), first)
        os.remove(self.base.join("models", people.RECOGNIZER_FILE))
        with self.assertRaises(people.PeopleError):
            people.recipe(options)


class FaceTest(unittest.TestCase):

    def test_pose_of_faces(self):
        straight = [[30, 40], [70, 40], [50, 60], [35, 80], [65, 80]]
        turn, tilt, roll = people.pose(straight)
        self.assertAlmostEqual(turn, 0.0)
        self.assertAlmostEqual(tilt, 0.5)
        self.assertAlmostEqual(roll, 0.0)
        turned = [[30, 40], [70, 40], [68, 60], [45, 80], [75, 80]]
        self.assertAlmostEqual(people.pose(turned)[0], 0.45)
        # Rolled by 90 degrees (lying on its side): the same turn and tilt.
        rolled = [[-y, x] for x, y in turned]
        turn, tilt, roll = people.pose(rolled)
        self.assertAlmostEqual(turn, 0.45)
        self.assertAlmostEqual(roll, 90.0)
        self.assertEqual(people.pose([[1, 1]] * 5), (0.0, 0.0, 0.0))

    def test_quality(self):
        self.assertEqual(people.quality(0.95, 120, 500, 12, 0.05), 1.0)
        self.assertEqual(people.quality(0.6, 120, 500, 12, 0.05), 0.0)       # unsure
        self.assertEqual(people.quality(0.95, 16, 500, 12, 0.05), 0.0)       # tiny
        self.assertEqual(people.quality(0.95, 120, 4, 12, 0.05), 0.0)        # blurred
        self.assertEqual(people.quality(0.95, 120, 500, 4, 0.05), 0.0)       # no face
        self.assertEqual(people.quality(0.95, 120, 500, 12, 0.7), 0.0)       # side on
        middle = people.quality(0.75, 40, 30, 6, 0.3)
        self.assertTrue(0 < middle < 0.2, middle)

    def test_face_rows(self):
        vector = struct.pack("<128f", *([1.0] + [0.0] * 127))
        face = {"box": [100, 50, 40, 60], "score": 0.9, "norm": 10, "sharpness": 99,
                "landmarks": [[110, 70], [130, 70], [120, 80], [112, 95], [128, 95]],
                "embedding": base64.b64encode(vector).decode("ascii")}
        row = people.face_row(face, 400, 200)
        self.assertEqual((row["x"], row["y"], row["w"], row["h"]), (0.25, 0.25, 0.1, 0.3))
        self.assertEqual(json.loads(row["landmarks"])[0], [0.275, 0.35])
        self.assertEqual((row["size"], row["embedding"], row["turn"]), (40.0, vector, 0.0))
        self.assertGreater(row["quality"], 0)
        for broken in ({"embedding": "AAAA"}, {"box": [1, 2, 3]}, {"box": [1, 2, 0, 4]},
                       {"score": float("nan")}, {"landmarks": [[1, 2]] * 4},
                       {"embedding": base64.b64encode(struct.pack(
                           "<128f", *([float("inf")] + [0.0] * 127))).decode("ascii")}):
            self.assertIsNone(people.face_row(dict(face, **broken), 400, 200), broken)
        self.assertIsNone(people.face_row(face, 0, 200))


def reply(*faces, width=400, height=300):
    """A helper's answer with faces [(person, x, y, side)] (pixels)."""
    found = []
    for person, x, y, side in faces:
        found.append({"box": [x, y, side, side], "score": 0.95, "norm": 10.0,
                      "sharpness": 150.0, "edge": max(width, height),
                      "landmarks": [[x + 0.3 * side, y + 0.4 * side],
                                    [x + 0.7 * side, y + 0.4 * side],
                                    [x + 0.5 * side, y + 0.6 * side],
                                    [x + 0.35 * side, y + 0.8 * side],
                                    [x + 0.65 * side, y + 0.8 * side]],
                      "embedding": base64.b64encode(
                          fake_people_worker.vector(person)).decode("ascii")})
    return {"width": width, "height": height, "faces": found, "seconds": 0.1}


@unittest.skipIf(Image is None, "Pillow is not installed")
class StoreTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", self.base.library_folder("Family"),
                                                id="lib_family1")])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def faces(self, store, key):
        return [tuple(row) for row in store.execute(
            "SELECT id, x, y, w, h FROM faces WHERE content_hash = ? AND kind = ? ORDER BY id",
            key)]

    def test_versions_of_the_store(self):
        self.assertIsNone(people.connect(create=False))
        self.assertIsNone(people.read_meta())
        store = people.connect()
        self.assertEqual(people.meta(store)["version"], people.FACES_VERSION)
        people.record_picture(store, ("h", "photo"), "r1", reply((1, 10, 10, 50)))
        store.commit()
        store.close()
        self.assertEqual(people.read_meta()["faces"], 1)
        # An older layout is emptied by a run that writes; a newer one is left alone.
        store = sqlite3.connect(people.store_path())
        store.execute("UPDATE meta SET value = '0' WHERE key = 'version'")
        store.commit()
        store.close()
        self.assertIsNone(people.connect(create=False))
        store = people.connect()
        self.assertEqual(people.meta(store)["faces"], 0)
        store.execute("UPDATE meta SET value = '99' WHERE key = 'version'")
        store.commit()
        store.close()
        with self.assertRaises(people.StoreError):
            people.connect()
        self.assertIsNone(people.read_meta())

    def test_faces_found_again_keep_their_ids(self):
        store = people.connect()
        self.addCleanup(store.close)
        key = ("hash-a", people.KIND_PHOTO)
        self.assertEqual(people.record_picture(store, key, "r1", reply(
            (1, 10, 10, 60), (2, 200, 100, 80))), 2)
        store.commit()
        first = self.faces(store, key)
        self.assertEqual(len(first), 2)
        self.assertAlmostEqual(first[0][1], 10 / 400.0)
        # Looked at again: the first face moved a little (same id), the second is gone,
        # a third is new.
        people.record_picture(store, key, "r2", reply((1, 12, 11, 60), (3, 300, 200, 40)))
        store.commit()
        second = self.faces(store, key)
        self.assertEqual(second[0][0], first[0][0])
        self.assertNotIn(first[1][0], [row[0] for row in second])
        self.assertEqual(len(second), 2)
        done, failed = people.picture_states(store)
        self.assertEqual((done, failed), ({key: "r2"}, {}))
        # Faces that are not usable are left out.
        broken = reply((1, 10, 10, 60))
        broken["faces"].append({"box": "nonsense"})
        self.assertEqual(people.record_picture(store, ("hash-b", "photo"), "r2", broken), 1)

    def test_failures_count_per_recipe(self):
        store = people.connect()
        self.addCleanup(store.close)
        key = ("hash-a", people.KIND_PHOTO)
        people.record_picture(store, key, "r1", reply((1, 10, 10, 60)))
        self.assertEqual(people.record_failure(store, key, "r2", "broken"), 1)
        self.assertEqual(people.record_failure(store, key, "r2", "broken"), 2)
        self.assertEqual(people.record_failure(store, key, "r3", "broken"), 1)
        store.commit()
        # The faces it had stay; a success clears the failure.
        self.assertEqual(len(self.faces(store, key)), 1)
        self.assertEqual(people.picture_states(store)[1], {key: ("r3", 1)})
        people.record_picture(store, key, "r3", reply())
        self.assertEqual(people.picture_states(store)[1], {})
        self.assertEqual(self.faces(store, key), [])

    def test_purge_and_delete_all(self):
        kept = insert_image(self.conn, "lib_family1", "a.jpg")
        insert_image(self.conn, "lib_family1", "b.jpg", missing_since=NOW)
        gone = insert_image(self.conn, "lib_family1", "c.jpg")
        store = people.connect()
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            people.record_picture(store, ("hash-" + name, "photo"), "r", reply((1, 5, 5, 40)))
        people.record_failure(store, ("hash-d.jpg", "photo"), "r", "broken")
        # (A row that became a video now has another kind of picture.)
        people.record_picture(store, ("hash-a.jpg", "video-poster"), "r", reply())
        store.commit()
        self.assertEqual(people.purge(store, self.conn), 2)
        self.assertEqual(sorted(people.picture_states(store)[0]),
                         [("hash-a.jpg", "photo"), ("hash-b.jpg", "photo"),
                          ("hash-c.jpg", "photo")])
        store.close()
        with self.conn:
            self.conn.execute("DELETE FROM images WHERE id = ?", (gone,))
        self.assertEqual(people.forget_unreferenced(self.conn), 1)
        self.assertEqual(people.forget_unreferenced(self.conn), 0)
        self.assertTrue(kept)
        result = people.delete_all()
        self.assertEqual(result, {"pictures": 2, "faces": 2, "people": 0, "named": 0,
                                  "removed": True, "error": ""})
        self.assertFalse(os.path.exists(people.store_path()))
        self.assertEqual(people.delete_all(), {"pictures": 0, "faces": 0, "people": 0,
                                               "named": 0, "removed": True, "error": ""})
        self.assertEqual(people.forget_unreferenced(self.conn), 0)

    @unittest.skipUnless(os.name == "nt", "open files can be removed on this system")
    def test_delete_all_wipes_a_file_that_is_open(self):
        store = people.connect()
        people.record_picture(store, ("h", "photo"), "r", reply((1, 5, 5, 40)))
        store.commit()
        try:
            result = people.delete_all()
            self.assertEqual((result["faces"], result["removed"]), (1, False))
            self.assertEqual(store.execute("SELECT COUNT(*) FROM faces").fetchone()[0], 0)
        finally:
            store.close()
        self.assertTrue(people.delete_all()["removed"])
        self.assertFalse(os.path.exists(people.store_path()))

    def named_store(self):
        """A store with a face and a named person; closed."""
        import people_groups
        store = people.connect()
        people.record_picture(store, ("h", "photo"), "r", reply((1, 5, 5, 40)))
        store.execute("INSERT INTO people (name, hidden, cover, created, updated) VALUES "
                      "('Zelda Quorn', 0, NULL, 0, 0)")
        store.execute("INSERT INTO face_people (face_id, person_id, owner, updated) SELECT "
                      "f.id, p.id, 1, 0 FROM faces f, people p")
        store.commit()
        self.assertEqual(people_groups.status(store, {})["named"], 1)
        store.close()

    def left_in_store(self):
        check = sqlite3.connect("file:%s?mode=ro" % people.store_path().replace("\\", "/"),
                                uri=True)
        try:
            return (check.execute("SELECT COUNT(*) FROM faces").fetchone()[0],
                    check.execute("SELECT COUNT(*) FROM people WHERE name != ''").fetchone()[0])
        finally:
            check.close()

    @unittest.skipUnless(os.name == "nt", "open and read-only files can be removed here")
    def test_delete_all_says_when_the_face_data_stays(self):
        import stat
        self.named_store()
        # A read-only file: neither removed nor emptied, and said so.
        os.chmod(people.store_path(), stat.S_IREAD)
        try:
            with mock.patch.object(people, "DELETE_TRIES", 2):
                result = people.delete_all()
        finally:
            os.chmod(people.store_path(), stat.S_IREAD | stat.S_IWRITE)
        self.assertFalse(result["removed"])
        self.assertIn("readonly", result["error"].replace(" ", "").replace("-", ""))
        self.assertEqual(self.left_in_store(), (1, 1))
        # Another program writing to it all the time: the same.
        other = sqlite3.connect(people.store_path())
        other.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(people, "DELETE_TRIES", 2), \
                    mock.patch.object(people, "WIPE_TIMEOUT", 0.2):
                result = people.delete_all()
                self.assertIn("locked", result["error"])
                with self.assertRaises(jobs.JobError) as caught:
                    jobs.run_delete_faces(Context(), None, {}, {})
                self.assertIn("NOT deleted", str(caught.exception))
        finally:
            other.rollback()
            other.close()
        self.assertEqual(self.left_in_store(), (1, 1))
        # Once nothing holds it, it goes.
        result = people.delete_all()
        self.assertEqual((result["removed"], result["error"], result["named"]), (True, "", 1))
        self.assertFalse(os.path.exists(people.store_path()))

    def test_people_without_faces_go_with_their_pictures(self):
        self.named_store()
        store = people.connect()
        try:
            # (No row of index.db has its picture: the face, the person and its name go.)
            self.assertEqual(people.purge(store, self.conn), 1)
            self.assertEqual(store.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)
            self.assertEqual(people.meta(store)["named"], 0)
        finally:
            store.close()

    def test_the_pictures_looked_at(self):
        photo = insert_image(self.conn, "lib_family1", "a.jpg")
        video = insert_image(self.conn, "lib_family1", "v.mp4", media="video", has_preview=1)
        insert_image(self.conn, "lib_family1", "w.mp4", media="video")          # no poster
        insert_image(self.conn, "lib_family1", "p.jpg", projection="equirectangular")
        pano_video = insert_image(self.conn, "lib_family1", "f.insv", media="video",
                                  projection="dual_fisheye", has_preview=1)
        insert_image(self.conn, "lib_family1", "n.jpg", content_hash="")
        insert_image(self.conn, "lib_family1", "m.jpg", missing_since=NOW)
        hidden = insert_image(self.conn, "lib_family1", "h.jpg", hidden=1)
        rows = people.library_rows(self.conn, ["lib_family1"])
        # (360 pictures in their stored preview only: the photo p.jpg has none here.)
        self.assertEqual([row["id"] for row in rows], [photo, video, pano_video, hidden])
        self.assertEqual([people.kind_of(row) for row in rows],
                         [people.KIND_PHOTO, people.KIND_POSTER, people.KIND_360,
                          people.KIND_PHOTO])

    def test_the_probe_face_is_drawn(self):
        data = people.probe_picture()
        image = Image.open(io.BytesIO(data))
        self.assertEqual((image.format, image.size), ("PNG", (320, 320)))
        self.assertEqual(image.convert("RGB").getpixel((1, 1)),
                         fake_people_worker.PROBE_BACKGROUND)


@unittest.skipIf(Image is None, "Pillow is not installed")
class PeopleFixture(support.TempBaseMixin):
    """Two libraries, previews with faces drawn as squares, and the stand-in helper."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(people, "WORKER_SCRIPT", FAKE_WORKER)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"FAKE_PEOPLE_SYS_PATH": json.dumps(sys.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # (The helper gets only some of the environment: the stand-in's settings too.)
        patcher = mock.patch.object(people, "HELPER_ENV_PREFIXES",
                                    people.HELPER_ENV_PREFIXES + ("FAKE_PEOPLE_",))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base.write_file("models/" + people.DETECTOR_FILE, b"detector")
        self.base.write_file("models/" + people.RECOGNIZER_FILE, b"recognizer")
        self.family = self.base.library_folder("Family")
        self.trips = self.base.library_folder("Trips")
        self.configure()
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        import cache
        self.previews = cache.PreviewStore()
        self.two = self.add("lib_family1", "two.png", picture([(1, 40, 40, 80),
                                                               (2, 200, 120, 60)]))
        # (A copy without a preview: its file is read when it is the only copy looked at.)
        two = self.previews.read("lib_family1", self.two)
        with open(os.path.join(self.trips, "copy.png"), "wb") as handle:
            handle.write(two)
        self.copy = self.add("lib_trips01", "copy.png", None, content_hash="hash-two.png")
        self.none = self.add("lib_family1", "empty.png", picture([]))
        self.hidden = self.add("lib_family1", "hidden.png", picture([(3, 100, 100, 100)]),
                               hidden=1)
        self.video = self.add("lib_trips01", "clip.mp4", picture([(1, 150, 60, 90)]),
                              media="video")
        # (A 360 photo is looked at too, but this face, far above its horizon, is not
        # kept: see test_faces_of_360_pictures.)
        self.pano = self.add("lib_trips01", "pano.jpg", picture([(4, 10, 10, 50)]),
                             projection="equirectangular")

    def configure(self, people_settings=None, libraries=None, **extra):
        values = {"enabled": True, "python": sys.executable, "models_dir": "models",
                  "workers": 1}
        values.update(people_settings or {})
        if libraries is None:
            libraries = [support.library("Family", self.family, id="lib_family1"),
                         support.library("Trips", self.trips, id="lib_trips01")]
        self.config = self.base.write_config(libraries, people=values, **extra)
        self.config = common.load_config()
        return self.config

    def add(self, library_id, path, preview, **fields):
        if preview is not None:
            fields.setdefault("has_preview", 1)
        image_id = insert_image(self.conn, library_id, path, **fields)
        if preview is not None:
            self.previews.write(library_id, image_id, preview)
        return image_id

    def update(self, ctx=None, **kwargs):
        return people.update(self.conn, self.config, ctx or Context(), **kwargs)

    def stored(self):
        store = people.connect(create=False)
        if store is None:
            return {}
        try:
            found = {}
            for row in store.execute("SELECT content_hash, kind, x, y, w, h, embedding, id "
                                     "FROM faces ORDER BY id"):
                found.setdefault((row[0], row[1]), []).append(
                    (round(row[2], 4), round(row[3], 4), round(row[4], 4), round(row[5], 4),
                     struct.unpack("<128f", row[6]).index(max(struct.unpack("<128f", row[6]))),
                     row[7]))
            return found
        finally:
            store.close()


class UpdateTest(PeopleFixture, unittest.TestCase):

    def test_every_picture_once_per_content(self):
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["configured"], summary["looked"], summary["faces"],
                          summary["with_faces"], summary["failed"], summary["pending"],
                          summary["workers"], summary["error"]), (True, 5, 4, 3, 0, 5, 1, ""))
        found = self.stored()
        self.assertEqual(sorted(found), [("hash-clip.mp4", "video-poster"),
                                         ("hash-hidden.png", "photo"),
                                         ("hash-two.png", "photo")])
        two = found[("hash-two.png", "photo")]
        # Fractions of the picture (400 x 300), and who it is.
        self.assertEqual([face[:5] for face in two], [(0.1, 0.1333, 0.2, 0.2667, 1),
                                                      (0.5, 0.4, 0.15, 0.2, 2)])
        self.assertEqual(found[("hash-hidden.png", "photo")][0][4], 3)
        self.assertIn("Finding faces in 5 picture(s) with 1 helper(s)", ctx.text())
        self.assertIn("4 face(s) in 3 of them", ctx.text())
        # Nothing to do: no helper is started.
        summary = self.update()
        self.assertEqual((summary["looked"], summary["up_to_date"], summary["pending"],
                          summary["workers"]), (0, 6, 0, 0))
        # A new picture is looked at alone.
        self.add("lib_family1", "new.png", picture([(5, 0, 150, 120)]))
        summary = self.update()
        self.assertEqual((summary["looked"], summary["faces"]), (1, 1))

    def test_faces_of_360_pictures(self):
        # Only the faces near the horizon of a 360 photo (its preview) are kept.
        pano = self.add("lib_family1", "sphere.jpg",
                        picture([(5, 100, 20, 60), (6, 500, 170, 60)], size=(800, 400)),
                        projection="equirectangular")
        self.add("lib_family1", "sphere.insv", picture([(7, 300, 120, 60)], size=(800, 400)),
                 media="video", projection="dual_fisheye")
        self.add("lib_family1", "unstitched.insp", None, projection="dual_fisheye")
        self.update()
        found = self.stored()
        self.assertEqual([face[4] for face in found[("hash-sphere.jpg", people.KIND_360)]], [6])
        self.assertEqual([face[4] for face in found[("hash-sphere.insv", people.KIND_360)]], [7])
        store = people.connect(create=False)
        try:
            # (A 360 picture without a stored preview is not looked at.)
            self.assertNotIn(("hash-unstitched.insp", people.KIND_360),
                             people.picture_states(store)[0])
        finally:
            store.close()
        row = self.conn.execute("SELECT * FROM images WHERE id = ?", (pano,)).fetchone()
        self.assertEqual(people.kind_of(row), people.KIND_360)
        self.assertTrue(people.in_band_360({"y": 0.4, "h": 0.1}))
        self.assertFalse(people.in_band_360({"y": 0.05, "h": 0.1}))

    def test_the_helper_gets_only_what_it_needs(self):
        # Not real keys: made-up values used only by this test.
        secrets_ = {"SEARCH_IMAGES_TEST_API_KEY": "not-a-real-key",
                    "ANTHROPIC_API_KEY": "not-a-real-key-either", "OMP_NUM_THREADS": "3"}
        env = people.helper_environment(dict(secrets_, PATH="/bin", SystemRoot="X:\\Windows"))
        self.assertEqual(env, {"PATH": "/bin", "SystemRoot": "X:\\Windows",
                               "OMP_NUM_THREADS": "3", "OPENBLAS_NUM_THREADS": "1",
                               "MKL_NUM_THREADS": "1", "OPENCV_LOG_LEVEL": "ERROR"})
        with mock.patch.dict(os.environ, secrets_):
            helper = people.Helper(people.settings(self.config)).start()
            try:
                names = helper.info["environment"]
            finally:
                helper.stop()
        self.assertNotIn("SEARCH_IMAGES_TEST_API_KEY", names)
        self.assertNotIn("ANTHROPIC_API_KEY", names)
        self.assertIn("OPENBLAS_NUM_THREADS", names)

    def test_a_run_says_what_it_runs_on(self):
        ctx = Context()
        self.update(ctx)
        self.assertEqual(people.read_meta()["runtime"], "OpenCV fake, numpy fake")
        self.assertNotIn("were found with", ctx.text())
        store = people.connect()
        store.execute("UPDATE meta SET value = 'OpenCV 4.14.0, numpy 2.5.4' WHERE key = "
                      "'runtime'")
        store.commit()
        store.close()
        self.add("lib_family1", "later.png", picture([(5, 0, 150, 120)]))
        ctx = Context()
        self.update(ctx)
        self.assertIn("faces kept were found with OpenCV 4.14.0, numpy 2.5.4; this run uses "
                      "OpenCV fake", ctx.text())
        self.assertEqual(people.status(self.config)["runtime"], "OpenCV fake, numpy fake")

    def test_libraries_limits_and_originals(self):
        summary = self.update(library_ids=["lib_trips01"])
        self.assertEqual((summary["looked"], summary["pending"]), (3, 3))
        summary = self.update(limit=1)
        self.assertEqual(summary["looked"], 1)
        # (Newest first.)
        self.assertIn(("hash-hidden.png", "photo"), self.stored())
        # A picture without a preview is read from its library's folder.
        with open(os.path.join(self.family, "original.jpg"), "wb") as handle:
            handle.write(picture([(6, 60, 60, 120)], size=(800, 600), kind="JPEG"))
        self.add("lib_family1", "original.jpg", None)
        summary = self.update()
        self.assertEqual((summary["looked"], summary["failed"]), (2, 0))
        face = self.stored()[("hash-original.jpg", "photo")][0]
        self.assertEqual(face[4], 6)
        self.assertAlmostEqual(face[0], 0.075, places=2)
        # One whose file is gone fails (and is tried again by later runs).
        self.add("lib_family1", "lost.jpg", None)
        summary = self.update()
        self.assertEqual((summary["looked"], summary["failed"]), (0, 1))

    def test_offline_library_folders_are_not_failures(self):
        self.configure(libraries=[support.library("Family", self.base.join("gone"),
                                                  id="lib_family1"),
                                  support.library("Trips", self.trips, id="lib_trips01")])
        self.add("lib_family1", "original.jpg", None)
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["offline"], summary["failed"], summary["looked"]), (1, 0, 5))
        self.assertIn("cannot be reached", ctx.text())

    def test_pictures_that_fail_are_tried_three_times(self):
        bad = self.add("lib_family1", "bad.png", picture([(1, 10, 10, 50)], corner=FAIL))
        ctx = Context()
        for attempt in range(1, people.MAX_ATTEMPTS + 1):
            summary = self.update(ctx)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["gave_up"], 1 if attempt == people.MAX_ATTEMPTS else 0)
        self.assertIn("Picture %d: no faces looked for: this test picture fails" % bad,
                      ctx.text())
        summary = self.update()
        self.assertEqual((summary["pending"], summary["failed"]), (0, 0))
        counted = people.counts(self.conn, self.config, ["lib_family1"])["lib_family1"]
        self.assertEqual((counted["failed"], counted["pending"]), (1, 0))
        summary = self.update(retry_failed=True)
        self.assertEqual((summary["pending"], summary["failed"]), (1, 1))

    def test_a_helper_that_stops_is_started_again(self):
        crash = self.add("lib_family1", "crash.png", picture([], corner=CRASH))
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["looked"], summary["failed"], summary["restarts"],
                          summary["error"]), (5, 1, 1, ""))
        self.assertIn("started again", ctx.text())
        self.assertIn("Picture %d" % crash, ctx.text())
        self.assertIn("exit code 3", ctx.text())

    def test_a_helper_that_does_not_answer_is_stopped(self):
        self.add("lib_family1", "slow.png", picture([], corner=SLOW))
        with mock.patch.object(people, "PICTURE_TIMEOUT", 1.0):
            summary = self.update()
        self.assertEqual((summary["looked"], summary["failed"], summary["restarts"]),
                         (5, 1, 1))

    def test_a_helper_that_keeps_stopping_ends_the_run(self):
        for index in range(3):
            self.add("lib_family1", "crash%d.png" % index, picture([], corner=CRASH))
        with mock.patch.object(people, "MAX_RESTARTS", 1):
            summary = self.update()
        self.assertIn("it stopped 2 times", summary["error"])
        self.assertFalse(summary["stopped"])

    def test_two_helpers_share_the_work(self):
        for index in range(6):
            self.add("lib_family1", "more%d.png" % index,
                     picture([(1 + index % 7, 20 * index, 10, 60)]))
        self.configure({"workers": 2})
        summary = self.update()
        self.assertEqual((summary["workers"], summary["looked"], summary["failed"]),
                         (2, 11, 0))

    def test_a_stopped_run_keeps_what_it_found(self):
        for index in range(5):
            self.add("lib_family1", "more%d.png" % index, picture([(1, 10, 10, 40)]))
        summary = self.update(Context(stop_after=3))
        self.assertTrue(summary["stopped"])
        self.assertLess(summary["looked"], 10)
        done = summary["looked"]
        summary = self.update()
        self.assertEqual(summary["looked"], 10 - done)

    def test_other_settings_look_again_and_keep_face_ids(self):
        self.update()
        before = self.stored()[("hash-two.png", "photo")]
        self.configure({"min_score": 0.8})
        counted = people.counts(self.conn, self.config, ["lib_family1"])["lib_family1"]
        self.assertEqual((counted["done"], counted["changed"], counted["pending"]), (0, 3, 3))
        summary = self.update()
        self.assertEqual(summary["looked"], 5)
        after = self.stored()[("hash-two.png", "photo")]
        self.assertEqual([face[5] for face in after], [face[5] for face in before])

    def test_setups_that_do_not_work(self):
        cases = [("problem", "no cv2"), ("version", "another version"),
                 ("noprobe", "no face in the test face")]
        for mode, words in cases:
            with mock.patch.dict(os.environ, {"FAKE_PEOPLE_MODE": mode}):
                summary = self.update()
            self.assertIn(words, summary["error"], mode)
            self.assertEqual(summary["looked"], 0)
        self.configure({"python": self.base.join("nothing.exe")})
        summary = self.update()
        self.assertIn("is not there", summary["error"])
        self.configure({"enabled": False})
        self.assertEqual(self.update(), people._new_summary())
        # (The fake's models must be there too.)
        self.configure({"detector": "other.onnx"})
        self.assertIn("face detector model", self.update()["error"])

    def test_check(self):
        result = people.check(self.config)
        self.assertEqual((result["ok"], result["opencv"], result["problem"]),
                         (True, "fake", ""))
        self.assertEqual(result["python"], "%d.%d.%d" % sys.version_info[:3])
        with mock.patch.dict(os.environ, {"FAKE_PEOPLE_MODE": "noprobe"}):
            result = people.check(self.config)
        self.assertFalse(result["ok"])
        self.assertIn("test face", result["problem"])
        self.assertIn("People is off", people.check({})["problem"])

    def test_counts_and_status(self):
        status = people.status(self.config)
        self.assertEqual((status["enabled"], status["ready"], status["stored"], status["faces"]),
                         (True, True, False, 0))
        self.update(library_ids=["lib_family1"])
        counted = people.counts(self.conn, self.config, ["lib_family1", "lib_trips01"])
        self.assertEqual(counted["lib_family1"], {"pictures": 3, "done": 3, "faces": 3,
                                                  "with_faces": 2, "pending": 0, "failed": 0,
                                                  "changed": 0})
        # The copy in the other library shares the faces of its content.
        self.assertEqual(counted["lib_trips01"], {"pictures": 3, "done": 1, "faces": 2,
                                                  "with_faces": 1, "pending": 2, "failed": 0,
                                                  "changed": 0})
        status = people.status(self.config)
        self.assertEqual((status["stored"], status["pictures"], status["faces"]), (True, 3, 3))
        self.configure({"python": ""})
        status = people.status(self.config)
        self.assertEqual((status["ready"], "Set \"python\"" in status["problem"]), (False, True))
        self.configure({"enabled": False})
        self.assertEqual(people.status(self.config)["problem"], "")

    def test_after_a_scan(self):
        ctx = Context()
        summary = people.after_run(self.conn, self.config, ctx, ["lib_family1"])
        self.assertEqual(summary["looked"], 3)
        self.configure({"after_scan": False})
        self.assertIsNone(people.after_run(self.conn, self.config, ctx))
        self.configure({"python": ""})
        self.assertIsNone(people.after_run(self.conn, self.config, ctx))
        self.assertIn("Faces were not looked for", ctx.text())
        self.configure()
        with mock.patch.dict(os.environ, {"FAKE_PEOPLE_MODE": "noprobe"}):
            summary = people.after_run(self.conn, self.config, ctx)
        # (A setup that does not work is a warning of the scan, not its error.)
        self.assertEqual(summary["error"], "")
        self.assertIn("test face", summary["warning"])
        self.configure({"enabled": False})
        self.assertIsNone(people.after_run(self.conn, self.config, ctx))


class JobAndScanTest(PeopleFixture, unittest.TestCase):

    def test_the_jobs(self):
        summary = jobs.RUNNERS["faces"](Context(), self.conn, self.config,
                                        {"library_id": "lib_trips01", "limit": 5})
        self.assertEqual(summary["looked"], 3)
        ctx = Context()
        summary = jobs.RUNNERS["faces"](ctx, self.conn, self.config, {})
        self.assertEqual(summary["looked"], 2)
        summary = jobs.RUNNERS["faces"](ctx, self.conn, self.config, {})
        self.assertIn("Faces are up to date (6 picture(s))", ctx.text())
        result = jobs.RUNNERS["delete_faces"](ctx, self.conn, self.config, {})
        self.assertEqual((result["pictures"], result["faces"], result["removed"],
                          result["error"]), (5, 4, True, ""))
        self.assertIsNone(people.read_meta())
        self.configure({"enabled": False})
        with self.assertRaises(jobs.JobError):
            jobs.RUNNERS["faces"](Context(), self.conn, self.config, {})

    def test_a_scan_looks_for_the_faces_of_new_pictures(self):
        import scan
        with open(os.path.join(self.family, "scanned.png"), "wb") as handle:
            handle.write(picture([(7, 100, 80, 120)], size=(600, 400)))
        ctx = Context()
        result = scan.run(self.config, ctx, library_ids=["lib_family1"], describe_images=False,
                          conn=self.conn)
        self.assertEqual(result["faces"]["looked"], 1)
        row = self.conn.execute("SELECT content_hash FROM images WHERE path = 'scanned.png'"
                                ).fetchone()
        face = self.stored()[(row[0], "photo")][0]
        self.assertEqual(face[4], 7)
        self.assertAlmostEqual(face[0], 100 / 600.0, places=2)
        summary = jobs._scan_summary(result, {}, offline_fails=False)
        self.assertEqual(summary["faces"]["looked"], 1)
        # Deleting a library's indexed data takes its faces along.
        import library_ops
        library_ops.delete_library_data(self.conn, "lib_family1")
        self.assertNotIn((row[0], "photo"), self.stored())


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(PeopleFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import serve
        self.manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(self.manager.stop, 30)
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        self.headers = {"X-Search-Images": "1"}

    def state(self):
        response = self.client.get("/api/admin/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def post(self, path, body):
        return self.client.post("/api/admin" + path, headers=self.headers, json=body)

    def test_the_manage_page_counts_faces_and_queues_the_jobs(self):
        state = self.state()
        self.assertEqual((state["people"]["enabled"], state["people"]["ready"]), (True, True))
        views = {view["id"]: view for view in state["libraries"]}
        self.assertEqual(views["lib_family1"]["faces"]["pending"], 3)
        self.update()
        views = {view["id"]: view for view in self.state()["libraries"]}
        self.assertEqual((views["lib_family1"]["faces"]["faces"],
                          views["lib_family1"]["faces"]["pending"]), (3, 0))
        response = self.post("/jobs", {"kind": "faces", "library_id": "lib_family1",
                                       "params": {"limit": 3, "retry_failed": True}})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["job"]["params"], {"limit": 3, "retry_failed": True})
        response = self.post("/jobs", {"kind": "delete_faces", "params": {}})
        self.assertEqual(response.status_code, 400)
        response = self.post("/jobs", {"kind": "delete_faces", "library_id": "lib_family1",
                                       "params": {"confirm": True}})
        self.assertEqual(response.status_code, 400)
        response = self.post("/jobs", {"kind": "delete_faces", "params": {"confirm": True}})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.manager.start()
        job_id = response.get_json()["job"]["id"]
        self.assertTrue(support.wait_for(
            lambda: (self.manager.get(job_id) or {}).get("state") in jobs.FINISHED_STATES,
            timeout=60), [self.manager.get(job_id), self.manager.snapshot(0)])
        self.assertEqual(self.manager.get(job_id)["state"], "done")
        self.assertIsNone(people.read_meta())
        # (Not a library's last job: it is no library's.)
        views = {view["id"]: view for view in self.state()["libraries"]}
        self.assertNotEqual((views["lib_trips01"]["last_job"] or {}).get("kind"),
                            "delete_faces")
        self.configure({"python": ""})
        response = self.post("/jobs", {"kind": "faces"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("python", response.get_json()["error"])

    def test_check_the_helper(self):
        response = self.post("/people/check", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.get_json()["ok"], response.get_json()["opencv"]),
                         (True, "fake"))
        # Only for this computer's admin.
        response = self.client.post("/api/admin/people/check", json={})
        self.assertEqual(response.status_code, 403)

    def test_with_people_off_the_page_shows_nothing(self):
        self.configure({"enabled": False})
        state = self.state()
        self.assertEqual((state["people"]["enabled"], state["people"]["stored"]), (False, False))
        self.assertNotIn("faces", state["libraries"][0])
        # Face data kept from before is still counted (so it can be deleted).
        self.configure()
        self.update()
        self.configure({"enabled": False})
        state = self.state()
        self.assertEqual(state["people"]["faces"], 4)
        self.assertIn("faces", state["libraries"][0])


@unittest.skipIf(Image is None, "Pillow is not installed")
class CommandLineTest(PeopleFixture, unittest.TestCase):

    def run_main(self, *args, answer=None):
        out = io.StringIO()
        with redirect_stdout(out):
            if answer is not None:
                with mock.patch("builtins.input", return_value=answer):
                    code = people.main(list(args), out=out)
            else:
                code = people.main(list(args), out=out)
        return code, out.getvalue()

    def test_find_status_check_and_delete(self):
        code, text = self.run_main("--check")
        self.assertEqual(code, 0, text)
        self.assertIn("The face helper works", text)
        code, text = self.run_main("--library", "Trips", "--limit", "5")
        self.assertEqual(code, 0, text)
        self.assertIn("Looked for faces in 3 picture(s)", text)
        code, text = self.run_main()
        self.assertEqual(code, 0, text)
        code, text = self.run_main("--status")
        self.assertEqual(code, 0, text)
        self.assertIn("4 face(s) in 5 picture(s) looked at (last run: OpenCV fake", text)
        self.assertIn("Family: 3 of 3 picture(s) looked at", text)
        code, text = self.run_main("--delete-all", answer="no")
        self.assertEqual(code, 1)
        self.assertIsNotNone(people.read_meta())
        code, text = self.run_main("--delete-all", answer="yes")
        self.assertEqual(code, 0, text)
        self.assertIn("Deleted the face data: 4 face(s) of 5 picture(s).", text)
        self.assertIsNone(people.read_meta())
        self.update()
        with mock.patch.object(people, "delete_all", return_value={
                "pictures": 1, "faces": 1, "people": 0, "named": 0, "removed": False,
                "error": "faces.db is locked"}):
            code, text = self.run_main("--delete-all", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("NOT deleted", text)
        self.assertEqual(self.run_main("--library", "Nowhere")[0], 2)
        self.assertEqual(self.run_main("--limit", "-1")[0], 2)
        self.assertEqual(self.run_main("--check", "--status")[0], 2)
        self.configure({"enabled": False})
        code, text = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("People is off", text)
        code, text = self.run_main("--check")
        self.assertEqual(code, 1)


@unittest.skipIf(shutil.which("node") is None, "Node.js is not installed")
class NodeTest(unittest.TestCase):

    def test_the_job_panel_names_and_sums_up_the_jobs(self):
        result = subprocess.run([shutil.which("node"), JS_TESTS], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)

    def test_people_on_the_albums_and_people_pages(self):
        # (The real page scripts on the made-up DOM of tests/js/app_meaning_test.js.)
        result = subprocess.run([shutil.which("node"), JS_PAGE_TESTS], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)


class PrivacyTest(unittest.TestCase):
    """Faces never reach a published site, the search records or an AI model."""

    @staticmethod
    def read(name):
        with open(os.path.join(support.REPO, name), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_publishing_code_never_reads_face_data(self):
        # (serve.py reads it for the admin's pages, see test_people_groups.)
        for name in ("export.py", "search_index.py", "static/site.js", "static/search-core.js"):
            self.assertNotIn("faces.db", self.read(name), name)
        # The search records know nothing of People; an export only the names, and
        # only with its people_names option (people_groups.export_names).
        self.assertFalse(self.imports("search_index.py") & {"people", "people_groups"})
        self.assertEqual(self.imports("export.py") & {"people", "people_groups"},
                         {"people_groups"})
        used = [node.attr for node in ast.walk(ast.parse(self.read("export.py")))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in ("people", "people_groups")]
        self.assertEqual(used, ["export_names"])

    def imports(self, name):
        found = set()
        for node in ast.walk(ast.parse(self.read(name))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    def test_what_goes_to_ai_models_never_holds_people(self):
        # (The describe, refine, meaning and looks steps send pictures and words to
        # models, which may be cloud endpoints: none of them reads People.)
        for name in ("describe.py", "llm.py", "refine.py", "sheet_talk.py", "embeddings.py",
                     "looks.py", "prompts.py", "image_edits.py", "local_server.py"):
            self.assertFalse(self.imports(name) & {"people", "people_groups"}, name)
            self.assertNotIn("faces.db", self.read(name), name)


if __name__ == "__main__":
    unittest.main()
