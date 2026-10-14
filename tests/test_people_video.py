"""Tests for the faces in videos (people.py: frames, link_faces, video_faces -- one
person's faces linked, two look-alikes kept apart --, record_video and which earlier
face a face takes the place of, the clips of a dashcam's drives skipped, reading that
can be stopped and gives up in time, what goes overwritten in the file; faces.db
version 4) and what the pages are told about them (people_groups: the moments of a
video's people).

The videos are tests/fake_ffmpeg.py's (videotools): their frames show
faces drawn as squares at the times the test gives, which the stand-in
face helper (tests/fake_people_worker.py, see test_people) finds as the
faces of persons 1 to 7. Every test runs in a temporary SETTINGS_BASE.
"""

import json
import math
import os
import sqlite3
import struct
import types
import unittest
from unittest import mock

import support
import test_people
import videotools
from test_people import Context, insert_image

import people
import people_groups
import videos

# (person, box as fractions of the frame, from, to) of the faces of the test video.
EVERYWHERE = {"k": 1, "box": [0.1, 0.2, 0.1, 0.18], "from": 0, "to": 100}
MIDDLE = {"k": 2, "box": [0.5, 0.3, 0.08, 0.14], "from": 8, "to": 14.5}
LATE = {"k": 3, "box": [0.75, 0.5, 0.09, 0.16], "from": 17, "to": 19}


def vector(k, noise=0.0, axis=None):
    """A unit vector mostly along axis k (and a little along another)."""
    values = [0.0] * people.EMBEDDING_DIMS
    values[k] = 1.0
    if noise:
        values[axis if axis is not None else (k + 50) % len(values)] = noise
    length = math.sqrt(sum(value * value for value in values))
    return tuple(value / length for value in values)


def alike(k, other, cosine):
    """A unit vector whose cosine to vector(k) is cosine (the rest along axis other)."""
    values = [0.0] * people.EMBEDDING_DIMS
    values[k] = cosine
    values[other] = math.sqrt(1.0 - cosine * cosine)
    return tuple(values)


def reply_face(k, box, norm=10.0, score=0.95, noise=0.0, values=None):
    """A face of the helper's answer (see people_worker), of person k (or of the vector
    values)."""
    import base64
    x, y, w, h = box
    marks = [[x + fx * w, y + fy * h] for fx, fy in ((0.3, 0.4), (0.7, 0.4), (0.5, 0.6),
                                                       (0.35, 0.8), (0.65, 0.8))]
    return {"box": [x, y, w, h], "landmarks": marks, "score": score, "norm": norm,
            "sharpness": 150.0, "edge": 1000,
            "embedding": base64.b64encode(struct.pack(
                "<128f", *(values or vector(k, noise)))).decode()}


class RulesTest(unittest.TestCase):

    def test_settings(self):
        options = people.settings({})
        self.assertEqual((options["frames"], options["frame_seconds"], options["drives"]),
                         (12, 4.0, False))
        options = people.settings({"people": {"frames": 0, "frame_seconds": 10,
                                              "drives": True}})
        self.assertEqual((options["frames"], options["frame_seconds"], options["drives"]),
                         (0, 10.0, True))
        options = people.settings({"people": {"frames": 99, "frame_seconds": 0.1,
                                              "drives": "yes"}})
        self.assertEqual((options["frames"], options["frame_seconds"], options["drives"]),
                         (12, 4.0, False))

    def test_frame_times(self):
        options = people.settings({})
        # About one every 4 s, spread evenly, besides the poster frame (not one within
        # a quarter of that of it).
        self.assertEqual(people.frame_times(20.0, 0.0, options), [2.0, 6.0, 10.0, 14.0, 18.0])
        self.assertEqual(people.frame_times(20.0, 1.0, options), [6.0, 10.0, 14.0, 18.0])
        # A long video: at most 11 besides the poster frame.
        times = people.frame_times(600.0, 1.0, options)
        self.assertEqual(len(times), 11)
        self.assertAlmostEqual(times[0], 600.0 / 22, places=2)
        # A short one: one, and none that would be the poster frame again.
        self.assertEqual(people.frame_times(3.0, 0.3, options), [1.5])
        self.assertEqual(people.frame_times(1.0, 0.5, options), [])
        for length in (None, 0, -5, float("nan"), "x"):
            self.assertEqual(people.frame_times(length, 0.0, options), [])
        self.assertEqual(people.frame_times(60.0, 1.0, dict(options, frames=1)), [])

    def test_kinds_and_recipes(self):
        options = people.settings({"people": {"enabled": True}})
        video = {"media": "video", "projection": "", "width": 1920, "height": 1080}
        pano_video = dict(video, projection="dual_fisheye")
        photo = {"media": "image", "projection": "", "width": 400, "height": 300}
        self.assertEqual(people.recipe_kind(video, options), people.RECIPE_VIDEO)
        self.assertEqual(people.recipe_kind(pano_video, options), people.RECIPE_VIDEO)
        self.assertEqual(people.kind_of(pano_video), people.KIND_360)
        self.assertEqual(people.recipe_kind(photo, options), people.KIND_PHOTO)
        self.assertTrue(people.in_frames(video, options))
        self.assertFalse(people.in_frames(video, dict(options, frames=0)))
        self.assertFalse(people.in_frames(photo, options))
        digests = ["a" * 64, "b" * 64]
        with mock.patch.object(people, "_model_digests", lambda options: digests):
            made = people.recipes(options)
            # The pictures keep the recipe they had: only the videos are looked at again.
            self.assertEqual({made[people.KIND_PHOTO], made[people.KIND_POSTER],
                              made[people.KIND_360]}, {people.recipe(options, digests)})
            self.assertNotEqual(made[people.RECIPE_VIDEO], made[people.KIND_POSTER])
            other = people.recipes(dict(options, frame_seconds=2.0))
            self.assertNotEqual(other[people.RECIPE_VIDEO], made[people.RECIPE_VIDEO])
            self.assertEqual(other[people.KIND_PHOTO], made[people.KIND_PHOTO])
            self.assertEqual(other[people.RECIPE_FULL], made[people.RECIPE_FULL])
            # "frames": 0, the poster alone: the recipe of before.
            self.assertEqual(people.recipes(dict(options, frames=0))[
                people.RECIPE_VIDEO], made[people.KIND_POSTER])

    def test_linking_the_faces_of_the_frames(self):
        # Person 1 in frames 0-2, person 2 in frames 0-1, both a little different each
        # time; in frame 2 a second face like person 1's (a print of it), and in frame
        # 3 a thing (a short vector) that looks like person 2.
        frames = [0, 0, 1, 1, 2, 2, 3]
        vectors = [vector(1, 0.3, 60), vector(2, 0.3, 61), vector(1, 0.5, 62),
                   vector(2, 0.2, 63), vector(1, 0.1, 64), vector(1, 0.9, 65),
                   vector(2, 0.4, 66)]
        norms = [10, 10, 9, 9, 10, 10, 5]
        groups = people.link_faces(frames, vectors, norms)
        self.assertIn([0, 2, 4], groups)
        # (Never two faces of one frame: the other face in frame 2 is alone; the thing
        # joins person 2, which has no face in its frame.)
        self.assertIn([5], groups)
        self.assertIn([1, 3, 6], groups)
        self.assertEqual(sorted(index for group in groups for index in group), list(range(7)))
        # Faces less alike than TRACK_MATCH stay apart; a thing alike nothing, alone.
        groups = people.link_faces([0, 1, 2], [vector(1), vector(2), vector(3)], [10, 10, 3])
        self.assertEqual(sorted(groups), [[0], [1], [2]])

    def test_the_faces_kept_of_a_videos_frames(self):
        options = people.settings({})
        size = {"width": 1000, "height": 500}
        frames = [
            (1.0, dict(size, faces=[reply_face(1, (100, 200, 60, 60), score=0.8),
                                    reply_face(4, (800, 20, 40, 40))])),
            (6.0, dict(size, faces=[reply_face(1, (110, 210, 64, 64), score=0.99),
                                    reply_face(2, (500, 200, 50, 50)),
                                    reply_face(5, (300, 200, 50, 50), norm=4.0)])),
            (10.0, dict(size, faces=[reply_face(2, (520, 220, 40, 40))])),
        ]
        faces, width, height = people.video_faces(frames, people.KIND_POSTER, options)
        self.assertEqual((width, height), (1000, 500))
        # One face per person (the best of its frames, and when it was), and none for
        # the thing whose vector is short in every frame.
        self.assertEqual(len(faces), 3)
        self.assertEqual(sorted(face["at"] for face in faces), [1.0, 6.0, 6.0])
        one = [face for face in faces if json.loads(face["seen"])[0][0] == 1.0
               and face["x"] < 0.2][0]
        self.assertEqual(one["at"], 6.0)            # (its surest face)
        self.assertEqual([item[0] for item in json.loads(one["seen"])], [1.0, 6.0])
        self.assertAlmostEqual(json.loads(one["seen"])[0][1], 0.1)
        vector_one = struct.unpack("<128f", one["embedding"])
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in vector_one)), 1.0,
                               places=5)
        self.assertGreater(vector_one[1], 0.9)
        two = [face for face in faces if face["x"] >= 0.5 and face["at"] in (6.0, 10.0)][0]
        self.assertEqual([item[0] for item in json.loads(two["seen"])], [6.0, 10.0])
        # Of a 360 video, only the faces near the horizon.
        faces, _w, _h = people.video_faces(frames, people.KIND_360, options)
        self.assertNotIn(4, [struct.unpack("<128f", face["embedding"]).index(
            max(struct.unpack("<128f", face["embedding"]))) for face in faces])
        self.assertEqual(people.video_faces([], people.KIND_POSTER, options), ([], 0, 0))

    def test_two_look_alike_people_of_a_video_are_two_faces(self):
        # Ann, clearly, at 1 s and 6 s; at 10 s and 14 s someone else who looks a little
        # like her (two women in sunglasses: a cosine of 0.33), whom the linking of the
        # frames (average linkage, TRACK_MATCH) takes in. Not as alike to Ann's best face
        # as VIDEO_SAME asks: she is a face of her own, with her own moments.
        options = people.settings({})
        size = {"width": 1000, "height": 500}
        ann = [vector(1, 0.2, 60), vector(1, 0.25, 61)]
        other = alike(1, 2, 0.33)
        frames = [(1.0, dict(size, faces=[reply_face(1, (100, 100, 60, 60), score=0.99,
                                                     values=ann[0])])),
                  (6.0, dict(size, faces=[reply_face(1, (120, 100, 60, 60), score=0.97,
                                                     values=ann[1])])),
                  (10.0, dict(size, faces=[reply_face(0, (600, 100, 50, 50), score=0.85,
                                                      values=other)])),
                  (14.0, dict(size, faces=[reply_face(0, (620, 100, 50, 50), score=0.85,
                                                      values=other)]))]
        self.assertEqual(sorted(people.link_faces(
            [0, 1, 2, 3], ann + [other, other], [10, 10, 10, 10])), [[0, 1, 2, 3]])
        faces, _w, _h = people.video_faces(frames, people.KIND_POSTER, options)
        self.assertEqual(sorted([item[0] for item in json.loads(face["seen"])]
                                for face in faces), [[1.0, 6.0], [10.0, 14.0]])
        first = [face for face in faces if face["at"] in (1.0, 6.0)][0]
        # (Her vector is made of her own faces only.)
        self.assertLess(struct.unpack("<128f", first["embedding"])[2], 1e-6)
        # A face as alike as VIDEO_SAME stays with her.
        frames[2] = (10.0, dict(size, faces=[reply_face(0, (600, 100, 50, 50), score=0.85,
                                                        values=alike(1, 2, 0.45))]))
        faces, _w, _h = people.video_faces(frames[:3], people.KIND_POSTER, options)
        self.assertEqual([[item[0] for item in json.loads(face["seen"])] for face in faces],
                         [[1.0, 6.0, 10.0]])

    def test_a_face_seen_once_too_blurred_to_tell_is_left_out(self):
        options = people.settings({})
        size = {"width": 1000, "height": 500}
        # 18 pixels: a quality of about 0.04 (see people.quality); 40 pixels: 0.5.
        frames = [(1.0, dict(size, faces=[reply_face(1, (100, 100, 18, 18)),
                                          reply_face(2, (300, 100, 40, 40)),
                                          reply_face(3, (500, 100, 18, 18))])),
                  (6.0, dict(size, faces=[reply_face(3, (510, 100, 18, 18))]))]
        faces, _w, _h = people.video_faces(frames, people.KIND_POSTER, options)
        kept = sorted(struct.unpack("<128f", face["embedding"]).index(
            max(struct.unpack("<128f", face["embedding"]))) for face in faces)
        # (Person 1, seen once and blurred, goes; person 2, sharp, and person 3, seen
        # twice, stay.)
        self.assertEqual(kept, [2, 3])
        self.assertTrue(all(face["quality"] < people.VIDEO_LONE_QUALITY for face in faces
                            if face["x"] > 0.45))

    def test_a_crowd_keeps_its_best_faces(self):
        # 40 strangers in a frame of a stadium: the 32 best (VIDEO_MAX_FACES).
        options = people.settings({})
        frame = {"width": 4000, "height": 500,
                 "faces": [reply_face(k, (k * 90, 100, 30 + k, 30 + k)) for k in range(1, 41)]}
        faces, _w, _h = people.video_faces([(1.0, frame)], people.KIND_POSTER, options)
        self.assertEqual(len(faces), people.VIDEO_MAX_FACES)
        self.assertEqual(min(face["size"] for face in faces), 39)
        faces, _w, _h = people.video_faces([(1.0, frame)], people.KIND_POSTER,
                                           dict(options, max_faces=10))
        self.assertEqual(len(faces), 10)


class ReplaceFacesTest(support.TempBaseMixin, unittest.TestCase):
    """Which earlier face of a video a face found in its frames takes the place of."""

    def setUp(self):
        super().setUp()
        self.store = people.connect()
        self.addCleanup(self.store.close)
        self.key = ("hash-v.mp4", people.KIND_POSTER)
        self.options = people.settings({})

    def poster_face(self, k, box):
        """The id of a face found before in the poster frame (at 1 s) alone."""
        people.record_picture(self.store, self.key, "posters", {
            "width": 1000, "height": 500, "faces": [reply_face(k, box)]})
        return self.store.execute("SELECT MAX(id) FROM faces").fetchone()[0]

    def ids(self):
        return {row[0] for row in self.store.execute("SELECT id FROM faces")}

    def test_a_face_keeps_the_id_of_the_same_person_only(self):
        bo = self.poster_face(2, (100, 100, 50, 50))
        # Now Ann's face is kept of the frames (her best at 6 s), and the frames saw her
        # at 1 s too, where Bo's face was before: Bo's id is not hers.
        ann = dict(people.face_row(reply_face(1, (500, 100, 50, 50)), 1000, 500), at=6.0,
                   seen=json.dumps([[1.0, 0.1, 0.2, 0.05, 0.1], [6.0, 0.5, 0.2, 0.05, 0.1]]))
        ids = people.replace_faces(self.store, self.key, [ann], 2.0, poster_at=1.0)
        self.assertNotEqual(ids, [bo])
        self.assertEqual(self.ids(), set(ids))

    def test_the_same_person_keeps_the_id(self):
        # Seen at the poster frame by another of the person's moments, with a vector
        # alike...
        bo = self.poster_face(2, (100, 100, 50, 50))
        again = dict(people.face_row(reply_face(0, (500, 100, 50, 50),
                                                values=vector(2, 0.4, 70)), 1000, 500),
                     at=6.0, seen=json.dumps([[1.0, 0.1, 0.2, 0.05, 0.1],
                                              [6.0, 0.5, 0.2, 0.05, 0.1]]))
        self.assertEqual(people.replace_faces(self.store, self.key, [again], 2.0,
                                              poster_at=1.0), [bo])
        # ... or there by its own best face (the same face of the same frame).
        own = dict(people.face_row(reply_face(0, (100, 100, 50, 50),
                                              values=alike(2, 3, 0.2)), 1000, 500),
                   at=1.0, seen=json.dumps([[1.0, 0.1, 0.2, 0.05, 0.1]]))
        self.assertEqual(people.replace_faces(self.store, self.key, [own], 3.0,
                                              poster_at=1.0), [bo])
        # (A photo's faces: by their place alone, as before.)
        photo = ("hash-p.jpg", people.KIND_PHOTO)
        people.record_picture(self.store, photo, "r", {"width": 1000, "height": 500,
                                                       "faces": [reply_face(2, (100, 100,
                                                                                50, 50))]})
        old = self.store.execute("SELECT id FROM faces WHERE content_hash = ?",
                                 (photo[0],)).fetchone()[0]
        other = people.face_row(reply_face(5, (102, 100, 50, 50)), 1000, 500)
        self.assertEqual(people.replace_faces(self.store, photo, [other], 4.0), [old])


class VideoFixture(videotools.FakeToolsMixin, test_people.PeopleFixture):
    """The People fixture, with a fake ffmpeg and a video whose frames show faces."""

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        # (The fixture's video has no file: this one has.)
        self.conn.execute("DELETE FROM images WHERE id = ?", (self.video,))
        self.conn.commit()
        self.configure({"frames": 12, "frame_seconds": 4, "min_faces": 1})
        self.clip = self.add_video("lib_trips01", "party.mp4", [EVERYWHERE, MIDDLE, LATE])
        self.key = ("hash-party.mp4", people.KIND_POSTER)

    def add_video(self, library_id, name, faces, **fields):
        folder = self.trips if library_id == "lib_trips01" else self.family
        path = self.make_video(folder, name, videotools.clip_probe(duration=20.0),
                               faces=faces)
        poster = videos.poster_jpeg(self.tools, path)
        return self.add(library_id, name, poster, media="video", width=1920, height=1080,
                        **fields)

    def video_faces(self, key=None):
        store = people.connect(create=False)
        try:
            return [dict(zip(("id", "at", "seen", "x", "quality", "person"), row))
                    for row in store.execute(
                        "SELECT f.id, f.at, f.seen, f.x, f.quality, fp.person_id FROM faces f "
                        "LEFT JOIN face_people fp ON fp.face_id = f.id WHERE f.content_hash = "
                        "? AND f.kind = ? ORDER BY f.x", key or self.key)]
        finally:
            store.close()


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class VideoRunTest(VideoFixture, unittest.TestCase):

    def test_a_video_is_looked_at_in_frames(self):
        ctx = Context()
        del self.commands[:]
        summary = self.update(ctx)
        self.assertEqual((summary["videos"], summary["frames"], summary["failed"]), (1, 5, 0))
        self.assertIn("1 of them are videos, looked at in up to 12 frames", ctx.text())
        self.assertIn("1 video(s) in 5 frame(s)", ctx.text())
        faces = self.video_faces()
        # One face per person seen, with the moments it was seen at (the poster frame
        # at 1 s, then one every 4 s).
        self.assertEqual([[item[0] for item in json.loads(face["seen"])] for face in faces],
                         [[1.0, 6.0, 10.0, 14.0, 18.0], [10.0, 14.0], [18.0]])
        self.assertEqual([face["at"] in [item[0] for item in json.loads(face["seen"])]
                          for face in faces], [True] * 3)
        self.assertAlmostEqual(faces[0]["x"], 0.1, places=2)
        # Each has a picture of its own (the poster shows another moment).
        store = people.connect(create=False)
        try:
            crops = {row[0] for row in store.execute("SELECT face_id FROM face_crops")}
            self.assertTrue({face["id"] for face in faces} <= crops)
            frames = store.execute("SELECT frames, width, height FROM face_pictures WHERE "
                                   "content_hash = ? AND kind = ?", self.key).fetchone()
            self.assertEqual(tuple(frames), (5, 1920, 1080))
        finally:
            store.close()
        # Every frame from the video's file, by ffmpeg on the processor.
        grabs = [command for command in self.commands if command[2] == "ffmpeg"]
        self.assertEqual(len(grabs), 5)
        self.assertFalse(any("-hwaccel" in command for command in grabs))
        # Up to date: nothing is looked at again.
        summary = self.update()
        self.assertEqual((summary["looked"], summary["pending"]), (0, 0))
        counted = people.counts(self.conn, self.config, ["lib_trips01"])["lib_trips01"]
        self.assertEqual((counted["videos"], counted["video_pending"]), (1, 0))

    def test_the_poster_faces_keep_their_ids_names_and_moves(self):
        # Found before by the poster alone (as Milestone 6 did), named and moved.
        self.configure({"frames": 0, "min_faces": 1})
        self.update()
        poster = self.video_faces()
        self.assertEqual(len(poster), 1)
        self.assertIsNone(poster[0]["at"])
        store = people.connect()
        try:
            person = poster[0]["person"]
            people_groups.update_person(store, person, {"name": "Ada"})
            # (A face of a photo moved to Ada by the owner.)
            moved = store.execute("SELECT id FROM faces WHERE content_hash = 'hash-two.png' "
                                  "ORDER BY id").fetchone()[0]
            people_groups.move_faces(store, [moved], person)
        finally:
            store.close()
        photos = {key: value for key, value in self.stored().items() if key != self.key}
        # Then the videos are looked at in frames: only they.
        self.configure({"frames": 12, "frame_seconds": 4, "min_faces": 1})
        counted = people.counts(self.conn, self.config, ["lib_trips01"])["lib_trips01"]
        self.assertEqual((counted["pending"], counted["video_pending"]), (1, 1))
        summary = self.update()
        self.assertEqual((summary["looked"], summary["videos"]), (1, 1))
        faces = self.video_faces()
        self.assertEqual(faces[0]["id"], poster[0]["id"])
        self.assertEqual(faces[0]["person"], person)
        store = people.connect(create=False)
        try:
            self.assertEqual(store.execute("SELECT name FROM people WHERE id = ?",
                                           (person,)).fetchone()[0], "Ada")
            self.assertEqual(tuple(store.execute("SELECT person_id, owner FROM face_people "
                                                 "WHERE face_id = ?", (moved,)).fetchone()),
                             (person, 1))
        finally:
            store.close()
        self.assertEqual({key: value for key, value in self.stored().items()
                          if key != self.key}, photos)

    def test_a_face_the_owner_placed_stays_when_the_frames_do_not_show_it(self):
        self.update()
        store = people.connect()
        try:
            late = [face for face in self.video_faces() if face["x"] > 0.7][0]
            people_groups.move_faces(store, [late["id"]], None, "Lee")
            other = [face for face in self.video_faces() if 0.4 < face["x"] < 0.7][0]
            # Looked at again, the frames show neither.
            frames = [(1.0, {"width": 1920, "height": 1080, "faces": []})]
            people.record_video(store, self.key, "again", frames, people.settings(self.config),
                                poster_at=1.0)
            store.commit()
            ids = {row[0] for row in store.execute("SELECT id FROM faces WHERE content_hash = "
                                                   "? AND kind = ?", self.key)}
        finally:
            store.close()
        self.assertIn(late["id"], ids)
        self.assertNotIn(other["id"], ids)

    def test_a_360_video_keeps_the_faces_near_its_horizon(self):
        with mock.patch.object(people, "video_frames") as frames:
            frames.return_value = (1.0, [(1.0, test_people.picture(
                [(5, 100, 20, 60), (6, 500, 170, 60)], size=(800, 400)))])
            self.add("lib_family1", "sphere.insv", test_people.picture([], size=(800, 400)),
                     media="video", projection="dual_fisheye")
            summary = self.update(library_ids=["lib_family1"])
        self.assertEqual(summary["videos"], 1)
        found = self.stored()[("hash-sphere.insv", people.KIND_360)]
        self.assertEqual([face[4] for face in found], [6])
        self.assertEqual(frames.call_args[0][0]["path"], "sphere.insv")

    def test_without_ffmpeg_the_videos_wait(self):
        self.use_fake_tools(available=False)
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["no_tools"], summary["videos"]), (1, 0))
        self.assertIn("1 video(s) wait", ctx.text())
        self.assertEqual(self.video_faces(), [])
        self.assertGreater(summary["looked"], 0)            # (the photos are looked at)

    def test_an_offline_library_keeps_its_videos_waiting(self):
        self.update(library_ids=["lib_family1"])
        support.remove_tree(self.trips)
        ctx = Context()
        summary = self.update(ctx, library_ids=["lib_trips01"])
        self.assertGreaterEqual(summary["offline"], 1)
        self.assertEqual(summary["videos"], 0)
        self.assertIn("wait for a later run", ctx.text())

    def test_a_video_that_cannot_be_read_fails_and_is_tried_again(self):
        with open(os.path.join(self.trips, "party.mp4"), "wb") as handle:
            handle.write(b"not a video")
        summary = self.update(library_ids=["lib_trips01"])
        self.assertEqual((summary["failed"], summary["videos"]), (1, 0))
        self.assertEqual(self.attempts(), 1)

    def attempts(self):
        """The failed tries at the test video (0 for none)."""
        store = people.connect(create=False)
        try:
            row = store.execute("SELECT attempts FROM face_failures WHERE content_hash = ?",
                                (self.key[0],)).fetchone()
            return row[0] if row is not None else 0
        finally:
            store.close()

    def test_a_stop_while_the_frames_are_read_is_soon_heard(self):
        ctx = Context()
        real = videos.frame_source

        def frame_source(tools, path, level=None):
            info, grab = real(tools, path, level)

            def stopping(seconds, edge):
                ctx.request_stop()          # (Stop, while the first frame is read)
                return grab(seconds, edge)
            return info, stopping
        del self.commands[:]
        with mock.patch.object(videos, "frame_source", frame_source):
            summary = self.update(ctx, library_ids=["lib_trips01"])
        self.assertTrue(summary["stopped"])
        self.assertEqual((summary["failed"], summary["videos"]), (0, 0))
        # (No other frame is read after it; the video waits for the next run.)
        self.assertEqual(len([command for command in self.commands
                              if command[2] == "ffmpeg"]), 1)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.update(library_ids=["lib_trips01"])["videos"], 1)

    def test_frames_that_take_too_long(self):
        # The folder is there: the video failed (a try counted).
        with mock.patch.object(people, "VIDEO_READ_TIMEOUT", -1.0):
            summary = self.update(library_ids=["lib_trips01"])
        self.assertEqual((summary["failed"], summary["videos"], summary["offline"]), (1, 0, 0))
        self.assertEqual(self.attempts(), 1)
        # Its folder went away while it was read: it waits for its folder (no try counted).
        import library_ops
        answers = iter([{"reachable": True}])

        def folder_status(root, timeout=None):
            return next(answers, {"reachable": False})
        ctx = Context()
        with mock.patch.object(people, "VIDEO_READ_TIMEOUT", -1.0), \
                mock.patch.object(library_ops, "folder_status", folder_status):
            summary = self.update(ctx, library_ids=["lib_trips01"], retry_failed=True)
        self.assertEqual((summary["failed"], summary["videos"], summary["offline"]), (0, 0, 1))
        self.assertIn("cannot be reached any more", ctx.text())
        self.assertEqual(self.attempts(), 1)
        # (And the next run, with its folder back, looks at it.)
        self.assertEqual(self.update(library_ids=["lib_trips01"], retry_failed=True)["videos"],
                         1)

    def file_holds(self, *parts):
        """Whether data/faces.db (once its journal is written back) holds any of parts."""
        conn = sqlite3.connect(people.store_path())
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        with open(people.store_path(), "rb") as handle:
            data = handle.read()
        return [part in data for part in parts]

    def test_the_faces_that_go_are_wiped_from_the_file(self):
        stranger = {"k": 6, "box": [0.4, 0.4, 0.1, 0.18], "from": 0, "to": 100}
        _clip, key, drives = self.drive_of("cam0003.mp4", [stranger], 60)
        with drives:
            self.configure({"frames": 12, "drives": True})
            self.update()
            store = people.connect(create=False)
            try:
                crop, vector_bytes, seen = store.execute(
                    "SELECT c.jpeg, f.embedding, f.seen FROM faces f JOIN face_crops c ON "
                    "c.face_id = f.id WHERE f.content_hash = ?", (key[0],)).fetchone()
                party = [bytes(row[0]) for row in store.execute(
                    "SELECT c.jpeg FROM faces f JOIN face_crops c ON c.face_id = f.id WHERE "
                    "f.content_hash = ?", (self.key[0],))]
            finally:
                store.close()
            crop, vector_bytes = bytes(crop), bytes(vector_bytes)
            self.assertEqual(self.file_holds(crop, vector_bytes), [True, True])
            # A dashcam's clip, skipped now: its stranger's face goes, and what it was
            # is overwritten in the file.
            self.configure({"frames": 12})
            self.assertEqual(self.update()["dropped"], 1)
            self.assertEqual(self.file_holds(crop, vector_bytes, seen.encode("ascii")),
                             [False, False, False])
        # So are the faces of a picture deleted from the index.
        self.assertTrue(party)
        self.conn.execute("DELETE FROM images WHERE id = ?", (self.clip,))
        self.conn.commit()
        self.assertEqual(people.forget_unreferenced(self.conn), 1)
        self.assertEqual(self.file_holds(*party), [False] * len(party))

    def drive_of(self, name, faces, length):
        """(a clip's id, its key, a patch of drives.library_drives that makes it a drive
        of Family whose camera cut its clips length seconds long)."""
        clip = self.add_video("lib_family1", name, faces)
        drive = types.SimpleNamespace(ids=[clip], clips=[
            types.SimpleNamespace(id=clip, duration=float(length))] * 3)
        return clip, ("hash-" + name, people.KIND_POSTER), mock.patch(
            "drives.library_drives", lambda conn, library_id, config=None: [drive]
            if library_id == "lib_family1" else [])

    def test_the_clips_of_a_dashcams_drives_are_skipped_and_their_faces_go(self):
        # (A stranger, person 6, seen in this clip alone; min_faces 3: in no person.)
        stranger = {"k": 6, "box": [0.4, 0.4, 0.1, 0.18], "from": 0, "to": 100}
        _clip, key, drives = self.drive_of("cam0001.mp4", [EVERYWHERE, stranger], 60)
        with drives:
            # Looked at while "drives" is on; then off, the default.
            self.configure({"frames": 12, "drives": True})
            self.update()
            before = {face["x"] > 0.3: face for face in self.video_faces(key)}
            self.assertEqual(len(before), 2)
            self.assertIsNone(before[True]["person"])
            self.assertIsNotNone(before[False]["person"])       # (person 1, seen elsewhere)
            self.configure({"frames": 12})
            ctx = Context()
            summary = self.update(ctx)
            self.assertEqual((summary["skipped"], summary["dropped"], summary["looked"]),
                             (1, 1, 0))
            self.assertIn("clips of dashcam drives", ctx.text())
            # The stranger's face goes; one in a person (the grouping's, or the
            # owner's) stays.
            self.assertEqual([face["id"] for face in self.video_faces(key)],
                             [before[False]["id"]])
            counted = people.counts(self.conn, self.config, ["lib_family1"])["lib_family1"]
            self.assertEqual((counted["skipped"], counted["pending"]), (1, 0))
            self.assertEqual(self.update()["dropped"], 0)

    def test_the_chapters_of_a_long_recording_are_looked_at(self):
        # An action camera's recording in chapters of about 9 minutes: no dashcam's.
        _clip, key, drives = self.drive_of("chapter01.mp4", [EVERYWHERE], 546)
        with drives:
            summary = self.update()
            self.assertEqual((summary["skipped"], summary["dropped"]), (0, 0))
            self.assertEqual(len(self.video_faces(key)), 1)
            self.assertTrue(self.video_faces(key)[0]["seen"])      # (in frames)

    def test_posters_alone_skip_no_clip(self):
        # "frames": 0 looks at every video's poster, as before.
        _clip, key, drives = self.drive_of("cam0002.mp4", [EVERYWHERE], 60)
        with drives:
            self.configure({"frames": 0})
            summary = self.update()
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual(len(self.video_faces(key)), 1)
            self.assertEqual(people.skipped_ids(self.conn, self.config, ["lib_family1"]),
                             set())

    def test_the_status_line_says_so(self):
        self.update()
        lines = []

        class Out:
            def write(self, text):
                lines.append(text)

            def flush(self):
                pass
        people._print_status(self.config, Out())
        text = "".join(lines)
        self.assertIn("1 video(s) in frames", text)


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class PagesTest(VideoFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.update()
        self.row = self.conn.execute("SELECT * FROM images WHERE id = ?",
                                     (self.clip,)).fetchone()
        store = people.connect()
        try:
            self.everywhere = self.video_faces()[0]
            people_groups.update_person(store, self.everywhere["person"], {"name": "Ada"})
        finally:
            store.close()

    def test_the_viewer_is_told_the_moments(self):
        store = people.connect(create=False)
        try:
            view = people_groups.picture_view(store, self.conn, self.row, self.config, True)
            ada = [item for item in view["names"] if item["label"] == "Ada"][0]
            self.assertEqual(ada["at"], [1.0, 6.0, 10.0, 14.0, 18.0])
            self.assertTrue(all(isinstance(face.get("at"), float) for face in view["faces"]))
            # Visitors (when allowed) see the named people's moments only.
            self.configure({"frames": 12, "min_faces": 1, "visitors": True})
            view = people_groups.picture_view(store, self.conn, self.row, self.config, False)
            self.assertEqual([item["label"] for item in view["names"]], ["Ada"])
            self.assertNotIn("faces", view)
            # A photo's names have no moments.
            photo = self.conn.execute("SELECT * FROM images WHERE id = ?",
                                      (self.two,)).fetchone()
            view = people_groups.picture_view(store, self.conn, photo, self.config, True)
            self.assertTrue(all("at" not in item for item in view["names"]))
        finally:
            store.close()

    def test_the_people_page_is_told_the_videos(self):
        store = people.connect(create=False)
        try:
            listed = people_groups.search_data(store, self.conn, self.config,
                                               ["lib_family1", "lib_trips01"], True)
            ada = [item for item in listed["people"] if item["name"] == "Ada"][0]
            self.assertIn(self.clip, ada["ids"])
            self.assertEqual(ada["videos"], 1)
            view = people_groups.person_view(store, self.conn, self.config,
                                             self.everywhere["person"],
                                             ["lib_family1", "lib_trips01"])
            face = [item for item in view["faces"] if item["id"] == self.everywhere["id"]][0]
            self.assertEqual((face["image"], face["at"]), (self.clip, self.everywhere["at"]))
            self.assertTrue(all("at" not in item for item in view["faces"]
                                if item["image"] != self.clip))
            other = people_groups.other_faces(store, self.conn, ["lib_family1", "lib_trips01"])
            self.assertTrue(any("at" in item for item in other["faces"]) or
                            not any(item["image"] == self.clip for item in other["faces"]))
            self.assertEqual(people_groups.moments(None, ""), [])
            self.assertEqual(people_groups.moments(2.5, ""), [2.5])
            self.assertEqual(people_groups.moments(2.5, "[[1,0,0,0.1,0.1],[3.25,0,0,1,1]]"),
                             [1.0, 3.25])
        finally:
            store.close()

    @unittest.skipIf(test_people.flask is None, "Flask is not installed")
    def test_a_face_of_a_video_shows_its_own_picture(self):
        import serve
        app = serve.build_app(init_db=False)
        app.testing = True
        self.addCleanup(app.extensions["search_images"].close)
        client = app.test_client()
        late = self.video_faces()[-1]
        store = people.connect(create=False)
        try:
            kept = bytes(store.execute("SELECT jpeg FROM face_crops WHERE face_id = ?",
                                       (late["id"],)).fetchone()[0])
        finally:
            store.close()
        response = client.get("/face/%d" % late["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, kept)
        detail = client.get("/api/image/%d" % self.clip).get_json()
        ada = [item for item in detail["people"]["names"] if item["label"] == "Ada"][0]
        self.assertEqual(ada["at"][0], 1.0)


# The layout of data/faces.db version 3 (Milestone 7's faces in full size), before
# the moments of videos: the upgrade adds the columns, and keeps everything.
VERSION_3 = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE face_pictures (content_hash TEXT NOT NULL, kind TEXT NOT NULL, "
    "recipe TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, "
    "faces INTEGER NOT NULL, seconds REAL NOT NULL DEFAULT 0, updated REAL NOT NULL, "
    "PRIMARY KEY (content_hash, kind))",
    "CREATE TABLE faces (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "content_hash TEXT NOT NULL, kind TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, "
    "w REAL NOT NULL, h REAL NOT NULL, landmarks TEXT NOT NULL, score REAL NOT NULL, "
    "size REAL NOT NULL, sharpness REAL NOT NULL, norm REAL NOT NULL, turn REAL NOT NULL, "
    "tilt REAL NOT NULL, quality REAL NOT NULL, embedding BLOB NOT NULL, "
    "updated REAL NOT NULL)",
    "CREATE INDEX faces_picture ON faces (content_hash, kind)",
    "CREATE TABLE face_failures (content_hash TEXT NOT NULL, kind TEXT NOT NULL, "
    "recipe TEXT NOT NULL, error TEXT NOT NULL, attempts INTEGER NOT NULL, "
    "updated REAL NOT NULL, PRIMARY KEY (content_hash, kind))",
    "CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0, cover INTEGER, "
    "created REAL NOT NULL, updated REAL NOT NULL)",
    "CREATE TABLE face_people (face_id INTEGER PRIMARY KEY REFERENCES faces (id) ON DELETE "
    "CASCADE, person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE, "
    "owner INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)",
    "CREATE INDEX face_people_person ON face_people (person_id)",
    "CREATE TABLE face_rejects (face_id INTEGER NOT NULL REFERENCES faces (id) ON DELETE "
    "CASCADE, person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE, "
    "updated REAL NOT NULL, PRIMARY KEY (face_id, person_id))",
    "CREATE TABLE face_marks (face_id INTEGER PRIMARY KEY REFERENCES faces (id) ON DELETE "
    "CASCADE, not_face INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL)",
    "CREATE TABLE face_crops (face_id INTEGER PRIMARY KEY REFERENCES faces (id) ON DELETE "
    "CASCADE, jpeg BLOB NOT NULL, updated REAL NOT NULL)",
)


class UpgradeTest(support.TempBaseMixin, unittest.TestCase):

    def old_store(self, version=3):
        """A data/faces.db of an older version: faces of a photo and of a video's poster,
        two people (one named, one hidden, merged), a move, a "not this person" and a
        "not a face"."""
        path = people.store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        statements = VERSION_3 if version == 3 else VERSION_3[:-1]
        for statement in statements:
            conn.execute(statement)
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("version", str(version)), ("epoch", "0123456789abcdef"), ("generation", "7"),
            ("people_version", "3")])
        blob = struct.pack("<128f", *vector(1))
        for face_id, key in ((1, ("hash-a.jpg", "photo")), (2, ("hash-v.mp4", "video-poster")),
                             (3, ("hash-b.jpg", "photo")), (4, ("hash-c.jpg", "photo"))):
            conn.execute("INSERT INTO faces VALUES (?, ?, ?, 0.1, 0.1, 0.2, 0.2, '[]', 0.9, 40, "
                         "100, 10, 0, 0.5, 0.8, ?, 1.0)", (face_id,) + key + (blob,))
        conn.execute("INSERT INTO face_pictures VALUES ('hash-v.mp4', 'video-poster', 'r', 400, "
                     "300, 1, 0.2, 1.0)")
        conn.executemany("INSERT INTO people VALUES (?, ?, ?, NULL, 1.0, 2.0)",
                         [(10, "Ada", 0), (11, "", 1)])
        conn.executemany("INSERT INTO face_people VALUES (?, ?, ?, 2.0)",
                         [(1, 10, 1), (2, 10, 1), (3, 11, 0)])
        conn.execute("INSERT INTO face_rejects VALUES (3, 10, 2.0)")
        conn.execute("INSERT INTO face_marks VALUES (4, 1, 2.0)")
        conn.commit()
        conn.close()
        return path

    def snapshot(self, store):
        return {table: sorted(tuple(row)[:17] for row in store.execute(
            "SELECT * FROM %s" % table)) for table in ("people", "face_people", "face_rejects",
                                                       "face_marks", "meta")}

    def test_names_merges_moves_and_marks_stay(self):
        for version in (3, 2):
            path = self.old_store(version)
            conn = sqlite3.connect(path)
            before = self.snapshot(conn)
            faces = sorted(tuple(row) for row in conn.execute("SELECT * FROM faces"))
            conn.close()
            # Reading it (also without create) brings it up to date.
            store = people.connect(create=False)
            try:
                self.assertEqual(people.meta(store)["version"], people.FACES_VERSION)
                after = self.snapshot(store)
                before["meta"] = [row if row[0] != "version" else ("version", "4")
                                  for row in before["meta"]]
                self.assertEqual(after, before)
                # The faces are as they were, with no moments (a poster's face).
                self.assertEqual(sorted(tuple(row)[:17] for row in store.execute(
                    "SELECT * FROM faces")), faces)
                self.assertEqual({tuple(row) for row in store.execute(
                    "SELECT at, seen FROM faces")}, {(None, "")})
                self.assertEqual(store.execute("SELECT frames FROM face_pictures").fetchone()[0],
                                 0)
                # (Asked again, nothing more to do.)
                people._upgrade(store)
                self.assertEqual(self.snapshot(store), after)
            finally:
                store.close()
            os.remove(path)

    def test_an_older_program_leaves_a_newer_file_alone(self):
        self.old_store()
        people.connect(create=False).close()
        with mock.patch.object(people, "FACES_VERSION", 3):
            self.assertIsNone(people.connect(create=False))
            with self.assertRaises(people.StoreError):
                people.connect()
        store = people.connect(create=False)
        try:
            self.assertEqual(people.meta(store)["named"], 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
