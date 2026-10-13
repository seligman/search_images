"""Levelled 360 pictures: panoramas turned level (images.level_equirect),
unstitched 360 photos levelled with the camera's motion sensor when they
are stitched, 360 videos' level tracks (read with the video, used for its
poster and sheet, sent to the player once, published with the video) and
the "level_horizon" setting.

The trailers are made up (tests/insta360gen.py); ffmpeg is played by
tests/fake_ffmpeg.py; everything runs in a temporary data root.
"""

import io
import json
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

from PIL import Image, ImageDraw

import fake_ffmpeg
import imagegen
import insta360gen as gen
import videotools
from test_panorama import ONE_R_INSP, X4_JPG, exif_of, lens_position, red_centroid
from test_video360 import INSV, LRV, insv_probe, lrv_probe

import common
import db
import export
import images
import insta360
import jobs
import looks
import prompts
import scan
import search_index
import serve
import videos

try:
    import flask
except ImportError:
    flask = None

X4 = insta360.IMU_AXES["insta360x4"]
ONE_R = insta360.IMU_AXES["insta360oner"]


def direction(longitude, latitude):
    lam, phi = math.radians(longitude), math.radians(latitude)
    return (math.cos(phi) * math.sin(lam), math.sin(phi), math.cos(phi) * math.cos(lam))


def lon_lat(vector):
    x, y, z = vector
    return math.degrees(math.atan2(x, z)), math.degrees(math.atan2(y, math.hypot(x, z)))


def at(lon, lat, size):
    width, height = size
    return (lon / 360.0 + 0.5) * width, (0.5 - lat / 180.0) * height


def marked_panorama(size, points):
    """A grey equirectangular panorama with red discs at (longitude, latitude) points."""
    image = Image.new("RGB", size, (90, 110, 90))
    draw = ImageDraw.Draw(image)
    for lon, lat in points:
        x, y = at(lon, lat, size)
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(230, 20, 20))
    return image


def red_near(image, x, y, reach=40):
    left, top = max(0, int(x) - reach), max(0, int(y) - reach)
    found = red_centroid(image.crop((left, top, int(x) + reach, int(y) + reach)))
    return None if found is None else (found[0] + left, found[1] + top)


def photo_trailer(up, model="Insta360 OneR"):
    """A ONE R style photo's trailer: its motion samples say up was up."""
    axes = ONE_R if model == "Insta360 OneR" else X4
    samples = gen.camera_samples(axes, 2.0, 500, up, start=-1.0)
    if model == "Insta360 OneR":
        stamped = gen.stamped(samples, 1e-3, 500000)
        records = [(3, 0, gen.double_motion(stamped)), (1, 1, gen.details(model, 500000))]
    else:
        stamped = gen.stamped(samples, 1e-6, 5000000)
        records = [(3, 0, gen.raw_motion(stamped)),
                   (1, 1, gen.details(model, 5000000, 0.0, (32, 2000)))]
    return gen.trailer(records)


def video_trailer(up, seconds=10.0, model="Insta360 X4", spin=(0.0, 0.0, 0.0)):
    """An X4 style video's trailer with its motion samples (up at time 0, turning by spin)."""
    samples = gen.camera_samples(X4, seconds + 0.4, 200, up, spin=spin, start=-0.2)
    stamped = gen.stamped(samples, 1e-6, 3000000, 1.6)
    records = [(1, 1, gen.details(model, 3000000, 1.6, (32, 2000))),
               (3, 0, gen.raw_motion(stamped))]
    return gen.trailer(records, table=True)


class LevelEquirectTest(unittest.TestCase):

    def test_a_tilted_panorama_is_turned_level(self):
        size = (1024, 512)
        up = gen.tilt_up(roll_deg=20, pitch_deg=-25)
        rotation = insta360.up_rotation(up)
        # Where the camera saw what is straight ahead and to the right of
        # the level view (both on the horizon).
        ahead = lon_lat(insta360.turn(rotation, (0.0, 0.0, 1.0)))
        right = lon_lat(insta360.turn(rotation, (1.0, 0.0, 0.0)))
        panorama = marked_panorama(size, [ahead, right])
        level = images.level_equirect(panorama, up)
        self.assertEqual(level.size, size)
        for (x, y) in (at(0, 0, size), at(90, 0, size)):
            found = red_near(level, x, y)
            self.assertIsNotNone(found, (x, y))
            self.assertAlmostEqual(found[0], x, delta=2.5)
            self.assertAlmostEqual(found[1], y, delta=2.5)
        # Another size at once.
        smaller = images.level_equirect(panorama, up, (512, 256))
        self.assertEqual(smaller.size, (512, 256))
        found = red_near(smaller, 256, 128)
        self.assertAlmostEqual(found[0], 256, delta=2)

    def test_across_the_edge_and_the_poles(self):
        # Up far over (75 degrees): the poles move well into the picture and
        # the cells of the mesh near them are split; nothing is left black.
        size = (512, 256)
        up = gen.tilt_up(roll_deg=-75)
        gradient = Image.linear_gradient("L").point(lambda v: 30 + v * 3 // 4)
        gradient = gradient.resize(size).convert("RGB")
        level = images.level_equirect(gradient, up)
        self.assertEqual(level.size, size)
        low = min(level.convert("L").getdata())
        self.assertGreater(low, 0)
        # Turned back, it is close to what it was (away from the poles).
        back = images.level_equirect(level, insta360.turn(
            [list(column) for column in zip(*insta360.up_rotation(up))], (0.0, 1.0, 0.0)))
        band = (0, 64, size[0], 192)
        before = gradient.crop(band).convert("L").getdata()
        after = back.crop(band).convert("L").getdata()
        error = sum(abs(a - b) for a, b in zip(before, after)) / float(len(before))
        self.assertLess(error, 6)

    def test_a_camera_upside_down(self):
        # Hanging upside down, leaning 8 degrees: the middle of the level view
        # is what the camera's front saw (within the lean), not what its side
        # or back did.
        size = (1024, 512)
        t, b = math.radians(172), math.radians(45)
        up = (math.sin(t) * math.cos(b), math.cos(t), math.sin(t) * math.sin(b))
        ahead = lon_lat(insta360.turn(insta360.up_rotation(up), (0.0, 0.0, 1.0)))
        self.assertLess(math.hypot(ahead[0], ahead[1]), 8.5)
        panorama = marked_panorama(size, [ahead])
        level = images.level_equirect(panorama, up)
        found = red_near(level, *at(0, 0, size))
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found[0], size[0] / 2.0, delta=2.5)
        self.assertAlmostEqual(found[1], size[1] / 2.0, delta=2.5)
        # (Marked so by a track: the same; marked upright: the smallest turn.)
        self.assertEqual(images.level_equirect(panorama, up, inverted=True).tobytes(),
                         level.tobytes())
        self.assertNotEqual(images.level_equirect(panorama, up, inverted=False).tobytes(),
                            level.tobytes())

    def test_level_already_is_left_alone(self):
        panorama = marked_panorama((256, 128), [(0, 0)])
        self.assertIs(images.level_equirect(panorama, (0.0, 1.0, 0.0)), panorama)
        self.assertIs(images.level_equirect(panorama, None), panorama)
        self.assertIs(images.level_equirect(panorama, (float("nan"), 1.0, 0.0)), panorama)
        self.assertEqual(images.level_equirect(panorama, (0.0, 1.0, 0.0), (128, 64)).size,
                         (128, 64))

    def test_the_stitch_levels_after_the_pole_fills(self):
        params = images.INSTA360_PARAMS
        x, y = lens_position(0, 0, (1024, 512), params)
        frame = imagegen.dual_fisheye_image((1024, 512), offsets=params["offsets"],
                                            markers=[(x, y, 6)])
        up = gen.tilt_up(roll_deg=15)
        plain = images.dual_fisheye_to_equirect(frame, 1024, nadir_fill=0, zenith_fill=0)
        level = images.dual_fisheye_to_equirect(frame, 1024, nadir_fill=0, zenith_fill=0,
                                                level=up)
        self.assertEqual(level.tobytes(), images.level_equirect(plain, up).tobytes())


class LevelledPhotoTest(support.TempBaseMixin, unittest.TestCase):
    """A ONE R style .insp whose trailer says the camera leaned when it was taken."""

    def setUp(self):
        super().setUp()
        self.up = gen.tilt_up(roll_deg=18, pitch_deg=10)
        rotation = insta360.up_rotation(self.up)
        # A marker where the camera saw what is straight ahead of the level view.
        self.lon, self.lat = lon_lat(insta360.turn(rotation, (0.0, 0.0, 1.0)))
        params = images.INSTA360_PARAMS
        x, y = lens_position(self.lon, self.lat, (1024, 512), params)
        self.frame = imagegen.dual_fisheye_image((1024, 512), offsets=params["offsets"],
                                                 markers=[(x, y, 6)])
        self.folder = self.base.make_dir("photos")

    def write(self, name, trailer, make="Arashi Vision", model="Insta360 OneR"):
        exif = exif_of(make, model, ONE_R_INSP["ImageDescription"])
        return imagegen.write_bytes(self.folder, name,
                                    imagegen.jpeg_bytes(self.frame, exif, quality=95) + trailer)

    def marker(self, data):
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return red_centroid(image), image.size

    def test_levelled_when_stitched(self):
        path = self.write("IMG_20190514_142301_00_001.insp", photo_trailer(self.up))
        result = images.read_image(path)
        self.assertEqual(result["projection"], "dual_fisheye")
        (x, y), size = self.marker(result["preview"])
        self.assertAlmostEqual(x, size[0] / 2.0, delta=4)
        self.assertAlmostEqual(y, size[1] / 2.0, delta=4)
        # The thumbnail (the front view) has it in the middle too.
        (x, y), size = self.marker(result["thumb"])
        self.assertAlmostEqual(x, size[0] / 2.0, delta=4)
        # What the model is sent (an original stitched as its preview).
        with open(path, "rb") as handle:
            sent = images.prepare_for_llm(handle.read(), projection="dual_fisheye")
        (x, y), size = self.marker(sent)
        self.assertAlmostEqual(x, size[0] / 2.0, delta=4)
        self.assertAlmostEqual(y, size[1] / 2.0, delta=4)

    def test_as_the_camera_saw_it_when_levelling_is_off(self):
        self.base.write_config([], level_horizon=False)
        path = self.write("IMG_20190514_142301_00_002.insp", photo_trailer(self.up))
        (x, y), size = self.marker(images.read_image(path)["preview"])
        want = at(self.lon, self.lat, size)
        self.assertAlmostEqual(x, want[0], delta=4)
        self.assertAlmostEqual(y, want[1], delta=4)

    def test_only_photos_of_cameras_whose_photos_are_levelled(self):
        # (An X4 levels the 360 photos it stitches itself; one without
        # motion samples is stitched as it is.)
        for name, trailer in (("IMG_20190514_142301_00_003.insp",
                               photo_trailer(self.up, "Insta360 X4")),
                              ("IMG_20190514_142301_00_004.insp", b"")):
            path = self.write(name, trailer)
            (x, y), size = self.marker(images.read_image(path)["preview"])
            want = at(self.lon, self.lat, size)
            self.assertAlmostEqual(x, want[0], delta=4, msg=name)
            self.assertAlmostEqual(y, want[1], delta=4, msg=name)


class LevelledVideoTest(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.make_dir("cam")
        self.up = gen.tilt_up(roll_deg=30)
        self.spin = (math.radians(4), 0.0, 0.0)
        self.video = self.make_video(self.folder, INSV, insv_probe(),
                                     trailer=video_trailer(self.up, spin=self.spin))
        self.lrv = self.make_video(self.folder, LRV, lrv_probe())

    def read(self, marks=None):
        calls = []
        real = images.dual_fisheye_to_equirect

        def spy(*args, **kwargs):
            calls.append(kwargs.get("level"))
            if marks is not None:
                marks.append(kwargs.get("level_inverted"))
            return real(*args, **kwargs)
        with mock.patch.object(images, "dual_fisheye_to_equirect", spy):
            result = videos.read_video(self.video, self.tools, sheet_edge=768)
        return result, calls

    def test_the_track_is_read_and_used(self):
        result, calls = self.read()
        track = result["exif"]["video"]["level"]
        self.assertIs(insta360.checked_track(track), track)
        self.assertEqual(track[0][0], 0.0)
        self.assertAlmostEqual(track[-1][0], 10.0)
        self.assertAlmostEqual(insta360.tilt_degrees(track[0][1:]), 30, delta=1)
        # Each stitch (the poster, the sheet's frames) is turned for its time.
        self.assertGreaterEqual(len(calls), 5)
        self.assertTrue(all(call is not None for call in calls))
        tilts = sorted(round(insta360.tilt_degrees(call)) for call in calls)
        self.assertGreater(tilts[-1] - tilts[0], 5)       # (it turns over time)
        # The facts survive JSON (the exif column).
        self.assertEqual(json.loads(json.dumps(result["exif"]))["video"]["level"], track)

    def test_a_camera_upside_down(self):
        os.remove(self.video)
        self.video = self.make_video(self.folder, INSV, insv_probe(),
                                     trailer=video_trailer(gen.tilt_up(roll_deg=172)))
        marks = []
        result, calls = self.read(marks)
        track = result["exif"]["video"]["level"]
        self.assertTrue(all(sample[4:] == [1] for sample in track))
        self.assertTrue(marks)
        self.assertTrue(all(mark is True for mark in marks))

    def test_a_kept_track_is_not_read_again(self):
        kept = [[0.0, 0.0, 1.0, 0.0], [10.0, 0.0, 1.0, 0.0]]
        calls = []
        real = images.dual_fisheye_to_equirect

        def spy(*args, **kwargs):
            calls.append(kwargs.get("level"))
            return real(*args, **kwargs)
        reading = mock.patch.object(insta360, "level_track",
                                    side_effect=AssertionError("the trailer was read again"))
        with reading, mock.patch.object(images, "dual_fisheye_to_equirect", spy):
            videos.poster_jpeg(self.tools, self.video, edge=512, level=kept)
            videos.sheet_jpeg(self.tools, self.video, edge=768, level=kept)
            # A playable copy only copies the lenses: no track at all.
            videos.make_proxy(self.tools, self.video, self.base.join("proxy.mp4"), height=480)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(call == (0.0, 1.0, 0.0) for call in calls), calls)
        # (Kept in a row's facts: stored_level.)
        self.assertEqual(videos.stored_level(json.dumps({"video": {"level": kept}})), kept)
        self.assertIsNone(videos.stored_level(json.dumps({"video": {"level": "x"}})))
        self.assertIsNone(videos.stored_level(""))

    def test_kept_but_not_used_when_levelling_is_off(self):
        self.base.write_config([], level_horizon=False)
        result, calls = self.read()
        self.assertTrue(result["exif"]["video"]["level"])
        self.assertTrue(calls)
        self.assertTrue(all(call is None for call in calls))
        self.assertIsNone(videos.level_of(result["exif"]["video"]))

    def test_from_the_lrv_when_the_video_has_none(self):
        os.remove(self.video)
        os.remove(self.lrv)
        self.video = self.make_video(self.folder, INSV, insv_probe(),
                                     trailer=fake_ffmpeg.insta360_trailer())
        self.lrv = self.make_video(self.folder, LRV, lrv_probe(),
                                   trailer=video_trailer(self.up))
        result, calls = self.read()
        self.assertTrue(result["exif"]["video"]["level"])
        # And none at all: as before.
        os.remove(self.lrv)
        self.lrv = self.make_video(self.folder, LRV, lrv_probe())
        result, calls = self.read()
        self.assertNotIn("level", result["exif"]["video"])
        self.assertTrue(all(call is None for call in calls))


@unittest.skipIf(flask is None, "Flask is not installed")
class LevelledLibraryTest(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.library_folder("Trips")
        self.video = self.make_video(self.folder, INSV, insv_probe(),
                                     trailer=video_trailer(gen.tilt_up(roll_deg=30)))
        self.lrv = self.make_video(self.folder, LRV, lrv_probe())
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips1")])
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        scan.run(self.config, jobs.JobContext("test", log_limit=500), describe_images=False)
        conn = db.connect()
        try:
            self.row = dict(conn.execute("SELECT * FROM images").fetchone())
        finally:
            conn.close()
        self.track = json.loads(self.row["exif"])["video"]["level"]

    def client(self):
        app = serve.build_app(init_db=False)
        app.testing = True
        self.addCleanup(app.extensions["search_images"].close)
        return app.test_client()

    def records(self):
        conn = db.connect()
        try:
            return search_index.build_records(conn, common.load_config(), ["lib_trips1"],
                                              use_cache=False)
        finally:
            conn.close()

    def test_the_player_gets_the_track_once(self):
        client = self.client()
        detail = client.get("/api/image/%d" % self.row["id"]).get_json()
        self.assertEqual(detail["level"], self.track)
        self.assertNotIn("level", detail["video"])
        self.assertNotIn("level", detail["exif"]["video"])
        self.assertIn("fisheye", detail["video"])
        record = self.records()[0]
        self.assertEqual(record["lv"], 1)
        self.assertIn("fx", record)
        # Off: neither.
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips1")],
                               level_horizon=False)
        detail = client.get("/api/image/%d" % self.row["id"]).get_json()
        self.assertNotIn("level", detail)
        self.assertNotIn("level", detail["video"])
        self.assertNotIn("lv", self.records()[0])

    @unittest.skipIf(shutil.which("node") is None, "Node.js is not installed")
    def test_site_js_makes_the_record_the_server_sends(self):
        import test_ui_scripts
        client = self.client()
        records = client.get("/api/index").get_json()["records"]
        self.assertEqual(records[0].get("lv"), 1)
        pairs = [{"record": record,
                  "detail": client.get("/api/image/%d" % record["i"]).get_json()}
                 for record in records]
        path = self.base.join("pairs.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"pairs": pairs}, handle)
        code, output = test_ui_scripts.run_node("--compare", path)
        self.assertEqual(code, 0, "\n" + output)
        self.assertIn("1 of 1 passed", output)

    def test_turning_levelling_off_changes_the_index(self):
        client = self.client()
        first = client.get("/api/index")
        self.assertEqual(first.get_json()["records"][0].get("lv"), 1)
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips1")],
                               level_horizon=False)
        again = client.get("/api/index", headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(again.status_code, 200)
        self.assertNotEqual(again.headers["ETag"], first.headers["ETag"])
        self.assertNotIn("lv", again.get_json()["records"][0])

    def test_a_poster_made_now_uses_the_kept_track(self):
        # No stored preview: the poster is made from the original, turned with
        # the track the scan kept (not read from the file again).
        conn = db.connect()
        try:
            conn.execute("UPDATE images SET has_preview = 0")
            conn.commit()
        finally:
            conn.close()
        client = self.client()
        with mock.patch.object(insta360, "level_track",
                               side_effect=AssertionError("the trailer was read again")):
            answer = client.get("/preview/%d" % self.row["id"])
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.mimetype, "image/jpeg")

    def test_stitched_again_its_looks_are_made_again(self):
        key = (self.row["content_hash"], looks.KIND_POSTER)
        store = looks.connect()
        try:
            looks.write(store, [key + (b"\x00" * 64,)])
        finally:
            store.close()
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips1")],
                               level_horizon=False)
        ctx = jobs.JobContext("test", log_limit=500)
        scan.run(common.load_config(), ctx, describe_images=False)
        store = looks.connect(create=False)
        try:
            self.assertNotIn(key, looks.stored_keys(store))
        finally:
            store.close()

    def test_turning_levelling_off_stitches_them_again(self):
        conn = db.connect()
        try:
            self.assertEqual(scan._restitch_ids(conn, "lib_trips1")[1], set())
            self.base.write_config([support.library("Trips", self.folder, id="lib_trips1")],
                                   level_horizon=False)
            self.assertEqual(scan._restitch_ids(conn, "lib_trips1")[1], {self.row["id"]})
        finally:
            conn.close()

    def export(self, options):
        self.execute_describe()
        conn = db.connect()
        try:
            summary = export.export_library(export.LogContext(lambda line: None), conn,
                                            common.load_config(), "lib_trips1",
                                            options=options)
        finally:
            conn.close()
        data = os.path.join(summary["output"], "data")
        records, details = [], {}
        for name in sorted(os.listdir(data)):
            with open(os.path.join(data, name), encoding="ascii") as handle:
                text = handle.read()
            if name.startswith("index-"):
                records.extend(json.loads(text[text.index("(") + 1:text.rindex(")")]))
            elif name.startswith("detail-"):
                details.update(json.loads(text[text.index("(") + 1:text.rindex(")")]))
        return records, details

    def execute_describe(self):
        description = {"title": "Lawn", "description": "A lawn.", "tags": ["lawn"],
                       "subjects": ["lawn"], "photo_type": "photo", "document_type": "none",
                       "setting": "outdoor", "time_of_day": "day", "event": "none",
                       "people": "none", "sensitive": "none", "private_info": False,
                       "aesthetic": 3, "sharpness": 4, "text": "", "colors": ["green"]}
        conn = db.connect()
        try:
            conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                         (json.dumps(description), prompts.PROMPT_VER))
            conn.commit()
        finally:
            conn.close()

    def test_published_with_the_video(self):
        records, details = self.export({"videos": "include"})
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record.get("vf"), record.get("lv")), (1, 1))
        self.assertEqual(details[str(record["i"])]["level"], self.track)

    def test_not_without_the_video(self):
        records, details = self.export({})
        record = records[0]
        self.assertNotIn("vf", record)
        self.assertNotIn("lv", record)
        self.assertNotIn("level", details.get(str(record["i"]), {}))


if __name__ == "__main__":
    unittest.main()
