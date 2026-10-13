"""360-degree videos (Insta360 .insv): pairing with the LRV and the second
lens's file, the cameras whose lenses are known, reading and stitching
their frames, the dates and the camera, playable copies, scans (companions
that come and go), search records, the server's routes, looks, the prompt,
the static export and the player's lens model (the shader's own sums run
under Node.js against images.py's).

ffmpeg and ffprobe are played by tests/fake_ffmpeg.py (see videotools.py);
everything runs in a temporary data root. The files are made up: an X4
style .insv holds two square lens streams (its LRV one 2:1 stream), a ONE R
style recording is two files of one square stream each and an LRV named
.insv. RealFfmpegTest runs the real programs on made-up files when they
are installed (on the PATH, or named by SEARCH_IMAGES_TEST_FFMPEG) and is
skipped otherwise.
"""

import io
import json
import math
import os
import shutil
import struct
import subprocess
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

from PIL import Image

import fake_ffmpeg
import fake_llm
import videotools

import cache
import common
import db
import describe
import export
import images
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

NODE = shutil.which("node")
VIEWER = os.path.join(support.REPO, "static", "viewer.js")
GLSL = os.path.join(support.REPO, "tests", "js", "glsl.js")

# A made-up X4 style camera's names: VID_<date>_<time>_00_<number>.insv and
# its LRV_<date>_<time>_01_<number>.lrv. The camera's clock said 14:23:01;
# the creation time (UTC) says 12:23:01: two hours east of Greenwich.
INSV = "VID_20190514_142301_00_007.insv"
LRV = "LRV_20190514_142301_01_007.lrv"
LENS = 960                  # a made-up lens stream size (square)
# A ONE R style recording: one file per lens and an LRV named .insv; the
# camera writes its own clock's time as the creation time.
ONE_A = "VID_20190514_150000_00_012.insv"
ONE_B = "VID_20190514_150000_10_012.insv"
ONE_LRV = "LRV_20190514_150000_11_012.insv"


def insv_probe(duration=10.0, codec="hevc", **changes):
    """An .insv: two square lens streams and AAC, the creation time in UTC."""
    utc = {"creation_time": "2019-05-14T12:23:01.000000Z"}
    changes.setdefault("lens_streams", True)
    changes.setdefault("width", LENS)
    changes.setdefault("height", LENS)
    return fake_ffmpeg.probe_json(duration=duration, codec=codec, pix_fmt="yuvj420p",
                                  brand="avc1", format_tags=utc, stream_tags=dict(utc),
                                  **changes)


def lrv_probe(duration=10.0, width=832, height=416, codec="h264"):
    """An LRV: both lenses side by side in one H.264 stream."""
    utc = {"creation_time": "2019-05-14T12:23:01.000000Z"}
    return fake_ffmpeg.probe_json(width=width, height=height, duration=duration, codec=codec,
                                  pix_fmt="yuvj420p", brand="avc1", format_tags=utc,
                                  stream_tags=dict(utc))


def lens_file_probe(duration=8.0):
    """One lens of a ONE R style recording: one square H.264 stream (and sound)."""
    local = {"creation_time": "2019-05-14T15:00:00.000000Z"}
    return fake_ffmpeg.probe_json(width=LENS, height=LENS, duration=duration, codec="h264",
                                  pix_fmt="yuvj420p", brand="avc1", format_tags=local,
                                  stream_tags=dict(local))


def touch(folder, name, data=b"x"):
    path = os.path.join(folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def make_one_r(case, folder, lrv=True):
    """A ONE R style recording in folder (case: a FakeToolsMixin test): (first, second, lrv)."""
    first = case.make_video(folder, ONE_A, lens_file_probe(),
                            trailer=fake_ffmpeg.insta360_trailer("Insta360 OneR"))
    second = case.make_video(folder, ONE_B, lens_file_probe())
    copy = case.make_video(folder, ONE_LRV, lrv_probe(duration=8.0, width=736, height=368),
                           trailer=fake_ffmpeg.insta360_trailer("Insta360 OneR")) if lrv else None
    return first, second, copy


class PairingTest(support.TempBaseMixin, unittest.TestCase):

    def test_the_lrv_with_the_same_date_time_and_number(self):
        folder = self.base.make_dir("cam")
        video = touch(folder, INSV)
        self.assertIsNone(videos.lrv_companion(video))
        lrv = touch(folder, LRV)
        self.assertEqual(os.path.normcase(videos.lrv_companion(video)), os.path.normcase(lrv))
        # Another number, time or date is another video's.
        other = touch(folder, "VID_20190514_142301_00_008.insv")
        touch(folder, "LRV_20190514_142302_01_008.lrv")
        touch(folder, "LRV_20190515_142301_01_008.lrv")
        touch(folder, "LRV_20190514_142301_01_009.lrv")
        self.assertIsNone(videos.lrv_companion(other))
        # Whatever the case of the letters and the middle field; also an
        # LRV named .insv (as older cameras write it).
        upper = touch(folder, "VID_20190514_150000_00_010.INSV")
        touch(folder, "lrv_20190514_150000_11_010.LRV")
        self.assertEqual(os.path.basename(videos.lrv_companion(upper)).lower(),
                         "lrv_20190514_150000_11_010.lrv")
        named = touch(folder, "VID_20190514_150500_00_013.insv")
        touch(folder, "LRV_20190514_150500_11_013.insv")
        self.assertEqual(os.path.basename(videos.lrv_companion(named)),
                         "LRV_20190514_150500_11_013.insv")
        # The name a scan found is looked at first.
        self.assertEqual(os.path.basename(videos.lrv_companion(named, "LRV_20190514_150500_11_"
                                                                      "013.insv")),
                         "LRV_20190514_150500_11_013.insv")
        self.assertEqual(os.path.basename(videos.lrv_companion(video, "gone.lrv")), LRV)
        # Only the video's own folder.
        alone = touch(folder, "VID_20190514_160000_00_011.insv")
        touch(folder, "sub/LRV_20190514_160000_01_011.lrv")
        self.assertIsNone(videos.lrv_companion(alone))
        # Names that are not the camera's, and flat .mp4 videos, have none.
        touch(folder, "VID_20190514_142301_00_007.mp4")
        for name in ("clip.insv", "VID_20190514_142301.insv", LRV, "VID_2019_x_00_1.insv",
                     "VID_20190514_142301_00_007.mp4"):
            self.assertIsNone(videos.lrv_companion(os.path.join(folder, name)), name)
        self.assertIsNone(videos.lrv_companion(""))
        self.assertIsNone(videos.lrv_companion(os.path.join(folder, "gone", INSV)))

    def test_a_video_recorded_as_two_files(self):
        folder = self.base.make_dir("cam")
        first, second = touch(folder, ONE_A), touch(folder, ONE_B)
        self.assertEqual(os.path.basename(videos.second_lens_file(first)), ONE_B)
        self.assertIsNone(videos.second_lens_file(second))
        self.assertIsNone(videos.second_lens_file(os.path.join(folder, INSV)))
        touch(folder, ONE_LRV)
        self.assertEqual(os.path.basename(videos.lrv_companion(first)), ONE_LRV)
        # The second lens's file is part of the first's video: no LRV of its own.
        self.assertIsNone(videos.lrv_companion(second))
        # Alone (its first file gone), it is a video of its own.
        os.remove(first)
        self.assertEqual(os.path.basename(videos.lrv_companion(second)), ONE_LRV)

    def test_companions_are_never_indexed_on_their_own(self):
        self.assertTrue(common.is_video_file(INSV))
        self.assertTrue(common.is_video_file(INSV.upper()))
        self.assertFalse(common.is_media_file(LRV, videos=True))
        self.assertFalse(common.is_media_file(INSV, videos=False))
        self.assertTrue(videos.is_360_video(INSV))
        self.assertFalse(videos.is_360_video("clip.mp4"))
        self.assertFalse(videos.is_360_video(None))
        names = [INSV, LRV, ONE_A, ONE_B, ONE_LRV, "clip.mp4"]
        self.assertEqual([name for name in names if videos.is_companion(name, names)],
                         [LRV, ONE_B, ONE_LRV])
        self.assertTrue(videos.is_companion(ONE_B.upper(), [ONE_A.lower()]))
        # A second lens's file without its first file is a video.
        self.assertFalse(videos.is_companion(ONE_B, [ONE_B]))
        self.assertFalse(videos.is_companion("VID_20190514_150000_10_099.insv", names))

    def test_the_servers_lookups_use_a_recent_listing(self):
        folder = self.base.make_dir("cam")
        video = touch(folder, INSV)
        listed = []
        real = os.listdir

        def listdir(path):
            listed.append(path)
            return real(path)
        with mock.patch.object(videos.os, "listdir", listdir):
            for _ in range(3):
                self.assertIsNone(videos.lrv_companion(video, cached=True))
            self.assertEqual(len(listed), 1)
            # A scan (not cached) always lists the folder, and finds what is new.
            touch(folder, LRV)
            self.assertIsNotNone(videos.lrv_companion(video))
            self.assertEqual(len(listed), 2)
            self.assertIsNotNone(videos.lrv_companion(video, cached=True))
            self.assertEqual(len(listed), 2)
            # The name the scan found needs no listing; a listing that went
            # stale is made again.
            self.assertIsNotNone(videos.lrv_companion(video, LRV, cached=True))
            with mock.patch.object(videos, "LISTING_SECONDS", -1.0):
                self.assertIsNotNone(videos.lrv_companion(video, cached=True))
            self.assertEqual(len(listed), 3)
            # A gone LRV is never named, however recent the listing.
            os.remove(os.path.join(folder, LRV))
            self.assertIsNone(videos.lrv_companion(video, LRV, cached=True))

    def test_companions_as_a_scan_compares_them(self):
        folder = self.base.make_dir("cam")
        video = touch(folder, INSV)
        self.assertEqual(videos.companions(video), "")
        touch(folder, LRV, b"12345")
        self.assertEqual(videos.companions(video), "lrv:%s:5" % LRV)
        first, _second = touch(folder, ONE_A), touch(folder, ONE_B)
        self.assertEqual(videos.companions(first), "lens:%s" % ONE_B)
        exif = json.dumps({"video": {"companions": "lrv:%s:5" % LRV}})
        self.assertFalse(videos.companions_changed(video, exif))
        touch(folder, LRV, b"123")
        self.assertTrue(videos.companions_changed(video, exif))
        # Read before companions were kept: read again.
        self.assertTrue(videos.companions_changed(video, json.dumps({"video": {}})))


class DateAndCameraTest(unittest.TestCase):

    def test_the_name_is_the_cameras_local_time(self):
        utc = {"creation_time": "2019-05-14T12:23:01.000000Z"}
        self.assertEqual(videos.named_local_time(INSV, utc, {}), ("2019-05-14T14:23:01", "+02:00"))
        # West of Greenwich, across midnight; half hours; the stream's tag.
        self.assertEqual(videos.named_local_time(
            "VID_20190514_201500_00_001.insv", {}, {"creation_time": "2019-05-15T03:15:00Z"}),
            ("2019-05-14T20:15:00", "-07:00"))
        self.assertEqual(videos.named_local_time(
            "VID_20190514_201500_00_001.mp4", {"creation_time": "2019-05-14T14:45:01Z"}, {}),
            ("2019-05-14T20:15:00", "+05:30"))
        # No difference says nothing of the zone: a camera that writes its
        # own clock's time as UTC.
        self.assertEqual(videos.named_local_time(
            ONE_A, {"creation_time": "2019-05-14T14:59:50Z"}, {}), ("2019-05-14T15:00:00", ""))
        # Not a whole number of quarter hours, too far, no UTC time, other names.
        for name, tags in ((INSV, {"creation_time": "2019-05-14T12:16:01Z"}),
                           (INSV, {"creation_time": "2019-05-13T12:23:01Z"}),
                           (INSV, {"creation_time": "2019-05-14T12:23:01-0700"}),
                           (INSV, {}),
                           ("VID_20190514_142301.mp4", utc),
                           ("PXL_20190514_142301123.mp4", utc)):
            self.assertIsNone(videos.named_local_time(name, tags, {}), (name, tags))

    def test_creation_date_uses_it_for_insta360_names(self):
        utc = {"creation_time": "2019-05-14T12:23:01.000000Z"}
        self.assertEqual(videos.creation_date(utc, {}, INSV),
                         ("2019-05-14T14:23:01", "video", "+02:00"))
        # A phone's QuickTime date still comes first.
        tags = dict(utc, **{"com.apple.quicktime.creationdate": "2019-05-14T11:30:15-0700"})
        self.assertEqual(videos.creation_date(tags, {}, INSV),
                         ("2019-05-14T11:30:15", "video", "-07:00"))

    def test_the_trailer_names_the_camera(self):
        with support.TempBase() as base:
            path = fake_ffmpeg.make_video(base.join(INSV), insv_probe(),
                                          trailer=fake_ffmpeg.insta360_trailer("Insta360 X9"))
            self.assertEqual(videos.insta360_model(path), "Insta360 X9")
            plain = fake_ffmpeg.make_video(base.join("plain.insv"), insv_probe())
            self.assertEqual(videos.insta360_model(plain), "")
            # The mark must end the file; a broken length names nothing.
            broken = base.join("broken.insv")
            with open(broken, "wb") as handle:
                handle.write(b"\x12\x30Insta360 X9\x00" + b"8db42d694ccc418790edff439fe026bf")
            self.assertEqual(videos.insta360_model(broken), "")
            self.assertEqual(videos.insta360_model(base.join("missing.insv")), "")

    def test_the_cameras_whose_lenses_are_known(self):
        x4 = videos.camera_360("Insta360 X4")
        self.assertEqual((x4["params"], x4["order"], x4["model"]),
                         ("INSTA360_VIDEO_PARAMS", (1, 0), "Insta360 X4"))
        one_r = videos.camera_360(" Insta360 ONE R ")
        self.assertEqual((one_r["params"], one_r["order"]), ("INSTA360_ONE_R_VIDEO_PARAMS", (0, 1)))
        self.assertEqual(videos.camera_360("Insta360 OneR")["params"],
                         "INSTA360_ONE_R_VIDEO_PARAMS")
        for model in ("Insta360 X3", "Insta360 ONE X2", "", None):
            self.assertIsNone(videos.camera_360(model), model)
        for entry in videos.CAMERAS_360.values():
            self.assertIn(getattr(images, entry["params"])["front"], ("left", "right"))

    def test_lens_layouts(self):
        self.assertEqual(videos.lens_layout(insv_probe()), "streams")
        self.assertEqual(videos.lens_layout(insv_probe(lens_streams=False)), "")
        self.assertEqual(videos.lens_layout(lrv_probe()), "frame")
        self.assertEqual(videos.lens_layout(fake_ffmpeg.probe_json()), "")
        self.assertEqual(videos.lens_layout({}), "")
        # One square stream, and a second lens's file like it.
        self.assertEqual(videos.lens_layout(lens_file_probe(), lens_file_probe()), "files")
        self.assertEqual(videos.lens_layout(lens_file_probe(), lrv_probe()), "")
        self.assertEqual(videos.lens_layout(lens_file_probe()), "")
        self.assertEqual(videos.lens_filter(1000),
                         "[0:v:1][0:v:0]hstack=inputs=2,scale=w=1000:h=500[frame]")
        self.assertEqual(videos.lens_filter(1000, ("[0:v:0]", "[1:v:0]")),
                         "[0:v:0][1:v:0]hstack=inputs=2,scale=w=1000:h=500[frame]")

    def test_the_media_data_of_an_mp4(self):
        def box(kind, body):
            return struct.pack(">I4s", 8 + len(body), kind) + body
        with support.TempBase() as base:
            path = base.join("copy.lrv")
            body = (box(b"ftyp", b"isom" * 3) + box(b"mdat", b"\x01" * 1000)
                    + box(b"moov", b"\x02" * 300))
            with open(path, "wb") as handle:
                handle.write(body + fake_ffmpeg.insta360_trailer())
            self.assertEqual(videos.media_data_size(path), 1008)
            # A 64-bit size, and more than one mdat.
            large = struct.pack(">I4sQ", 1, b"mdat", 16 + 500) + b"\x03" * 500
            with open(path, "wb") as handle:
                handle.write(box(b"ftyp", b"isom") + large + box(b"mdat", b"\x04" * 10))
            self.assertEqual(videos.media_data_size(path), 516 + 18)
            # Not an MP4.
            fake_ffmpeg.make_video(path, lrv_probe())
            self.assertIsNone(videos.media_data_size(path))
            self.assertIsNone(videos.media_data_size(base.join("missing.lrv")))


class ReadTest(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.make_dir("cam")
        self.video = self.make_video(self.folder, INSV, insv_probe(),
                                     trailer=fake_ffmpeg.insta360_trailer())

    def read(self, path=None, **kwargs):
        return videos.read_video(path or self.video, self.tools, sheet_edge=768, **kwargs)

    def frame_commands(self):
        return [argv for argv in self.commands if argv[-1] == "pipe:1"]

    def test_frames_come_from_the_lrv(self):
        lrv = self.make_video(self.folder, LRV, lrv_probe())
        result = self.read()
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual(result["media"], "video")
        self.assertIsNone(result["pano"])
        # The full panorama's size: two lenses wide.
        self.assertEqual((result["width"], result["height"]), (2 * LENS, LENS))
        self.assertEqual(result["duration"], 10.0)
        self.assertEqual((result["taken_at"], result["tz_offset"], result["taken_source"]),
                         ("2019-05-14T14:23:01", "+02:00", "video"))
        self.assertEqual(result["camera"], "Arashi Vision Insta360 X4")
        self.assertEqual(result["mime"], "video/mp4")
        # The preview is the stitched poster (as wide as the LRV), the
        # thumbnail its front view.
        with Image.open(io.BytesIO(result["preview"])) as preview:
            self.assertEqual(preview.size, (832, 416))
        with Image.open(io.BytesIO(result["thumb"])) as thumb:
            self.assertEqual(thumb.size, (320, 240))
        with Image.open(io.BytesIO(result["sheet"])) as sheet:
            self.assertEqual(sheet.size[0], 768)
            self.assertLess(sheet.size[1], sheet.size[0])
        facts = result["exif"]["video"]
        self.assertEqual(facts["codec"], "hevc")
        self.assertEqual(facts["lenses"], "streams")
        self.assertNotIn("lens_file", facts)
        size = os.path.getsize(lrv)
        self.assertEqual(facts["lrv"], {"name": LRV, "codec": "h264", "pix_fmt": "yuvj420p",
                                        "container": "mp4", "width": 832, "height": 416,
                                        "size": size, "media_size": None})
        self.assertEqual(videos.playable(facts["lrv"]), "yes")
        self.assertEqual(facts["companions"], "lrv:%s:%d" % (LRV, size))
        self.assertEqual((facts["sheet_frames"], facts["sheet_asked"]), (4, 4))
        geometry = facts["fisheye"]
        params = images.INSTA360_VIDEO_PARAMS
        self.assertEqual(geometry["front"], 1)
        self.assertEqual(geometry["fov"], params["fov"])
        self.assertEqual(geometry["turn"], list(params["rotation"]))
        self.assertEqual(geometry["blend"], params["blend"])
        self.assertEqual(geometry["fill"], [0.0, 0.0])
        # (Circles as tall as the frame, centred on its quarter points moved
        # by the offsets, in units of their radius.)
        left, right = geometry["lens"]
        self.assertAlmostEqual(left[0], 0.25 + params["offsets"][0][0] * 0.25, places=4)
        self.assertAlmostEqual(left[1], 0.5 + params["offsets"][0][1] * 0.5, places=4)
        self.assertEqual(left[2], 0.5)
        self.assertEqual(right, [0.75, 0.5, 0.5])
        self.assertEqual(len(geometry["gain"]), 2)
        # Every frame was the LRV's; the lens streams were never decoded.
        frames = self.frame_commands()
        self.assertTrue(frames)
        self.assertTrue(all("file:" + lrv in argv for argv in frames))
        self.assertFalse([argv for argv in self.commands if "-filter_complex" in argv])

    def test_without_an_lrv_the_lens_streams_go_side_by_side(self):
        result = self.read()
        self.assertEqual(result["projection"], "dual_fisheye")
        facts = result["exif"]["video"]
        self.assertNotIn("lrv", facts)
        self.assertEqual(facts["lenses"], "streams")
        self.assertEqual(facts["companions"], "")
        frames = self.frame_commands()
        self.assertTrue(frames)
        for argv in frames:
            self.assertIn("-filter_complex", argv)
            graph = argv[argv.index("-filter_complex") + 1]
            self.assertTrue(graph.startswith("[0:v:1][0:v:0]hstack=inputs=2,"), graph)
            self.assertEqual(argv[argv.index("-map") + 1], "[frame]")
            self.assertNotIn("-hwaccel", argv)
        # The preview: the panorama at most the preview's size, never wider
        # than the two streams.
        with Image.open(io.BytesIO(result["preview"])) as preview:
            self.assertEqual(preview.size, (2 * LENS, LENS))

    def test_a_flat_lrv_is_not_used(self):
        self.make_video(self.folder, LRV, lrv_probe(width=1920, height=1080))
        result = self.read()
        self.assertNotIn("lrv", result["exif"]["video"])
        self.assertTrue([argv for argv in self.commands if "-filter_complex" in argv])

    def test_a_camera_whose_lenses_are_not_known_is_an_ordinary_video(self):
        for trailer in (fake_ffmpeg.insta360_trailer("Insta360 X9"), b""):
            other = self.make_video(self.folder, "VID_20190514_150000_00_014.insv", insv_probe(),
                                    trailer=trailer)
            self.make_video(self.folder, "LRV_20190514_150000_01_014.lrv", lrv_probe())
            result = self.read(other)
            self.assertEqual(result["projection"], "")
            self.assertEqual((result["width"], result["height"]), (LENS, LENS))
            facts = result["exif"]["video"]
            self.assertNotIn("fisheye", facts)
            self.assertNotIn("lrv", facts)
            # (Read again when its companions change, as any .insv.)
            self.assertTrue(facts["companions"].startswith("lrv:"))
            self.assertEqual(result["camera"],
                             "Arashi Vision Insta360 X9" if trailer else "")

    def test_the_lrvs_trailer_names_the_camera_when_the_video_has_none(self):
        plain = self.make_video(self.folder, "VID_20190514_150000_00_015.insv", insv_probe())
        self.make_video(self.folder, "LRV_20190514_150000_01_015.lrv", lrv_probe(),
                        trailer=fake_ffmpeg.insta360_trailer())
        result = self.read(plain)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual(result["camera"], "Arashi Vision Insta360 X4")

    def test_one_square_stream_and_no_lrv_is_an_ordinary_video(self):
        single = self.make_video(self.folder, "VID_20190514_150000_00_012.insv",
                                 insv_probe(lens_streams=False),
                                 trailer=fake_ffmpeg.insta360_trailer())
        result = self.read(single)
        self.assertEqual(result["projection"], "")
        self.assertEqual((result["width"], result["height"]), (LENS, LENS))
        self.assertNotIn("fisheye", result["exif"]["video"])

    def test_one_square_stream_with_an_lrv_plays_the_lrv(self):
        single = self.make_video(self.folder, "VID_20190514_150000_00_012.insv",
                                 insv_probe(lens_streams=False),
                                 trailer=fake_ffmpeg.insta360_trailer())
        self.make_video(self.folder, "LRV_20190514_150000_01_012.lrv", lrv_probe())
        result = self.read(single)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual(result["exif"]["video"]["lenses"], "lrv")
        self.assertEqual((result["width"], result["height"]), (2 * LENS, LENS))

    def test_posters_and_sheets_made_later_are_stitched_too(self):
        self.make_video(self.folder, LRV, lrv_probe())
        with Image.open(io.BytesIO(videos.poster_jpeg(self.tools, self.video))) as poster:
            self.assertEqual(poster.size, (832, 416))
        sheet, count = videos.sheet_jpeg(self.tools, self.video, edge=600)
        self.assertEqual(count, 4)
        with Image.open(io.BytesIO(sheet)) as picture:
            self.assertEqual(picture.size[0], 600)

    def test_a_playable_copy_holds_both_lenses(self):
        target = self.base.join("proxy.mp4")
        videos.make_proxy(self.tools, self.video, target, height=480)
        argv = [argv for argv in self.commands if "libx264" in argv][-1]
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertEqual(graph, "[0:v:1][0:v:0]hstack=inputs=2,scale=w=960:h=480,"
                                "format=yuv420p[frame]")
        for option in ("-map_metadata", "-dn", "-sn"):
            self.assertIn(option, argv)
        copied = videos.probe(self.tools, target)
        streams = [stream for stream in copied["streams"] if stream["codec_type"] == "video"]
        self.assertEqual([(stream["codec_name"], stream["width"], stream["height"])
                          for stream in streams], [("h264", 960, 480)])


class OneFilePerLensTest(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):
    """A ONE R style recording: one file per lens, and an LRV named .insv."""

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.make_dir("cam")

    def read(self, path):
        return videos.read_video(path, self.tools, sheet_edge=768)

    def test_with_its_lrv(self):
        first, _second, lrv = make_one_r(self, self.folder)
        result = self.read(first)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual((result["width"], result["height"]), (2 * LENS, LENS))
        self.assertEqual(result["camera"], "Arashi Vision Insta360 OneR")
        # (Its clock's time, written as UTC: no time zone.)
        self.assertEqual((result["taken_at"], result["tz_offset"]), ("2019-05-14T15:00:00", ""))
        facts = result["exif"]["video"]
        self.assertEqual((facts["lenses"], facts["lens_file"], facts["lrv"]["name"]),
                         ("files", ONE_B, ONE_LRV))
        self.assertEqual(facts["companions"],
                         "lrv:%s:%d lens:%s" % (ONE_LRV, os.path.getsize(lrv), ONE_B))
        # Its own lens model: the left lens (the _00_ file's) in front,
        # both turned a quarter.
        params = images.INSTA360_ONE_R_VIDEO_PARAMS
        geometry = facts["fisheye"]
        self.assertEqual((geometry["front"], geometry["turn"], geometry["fov"]),
                         (0, [90.0, 270.0], params["fov"]))
        frames = [argv for argv in self.commands if argv[-1] == "pipe:1"]
        self.assertTrue(frames)
        self.assertTrue(all("file:" + lrv in argv for argv in frames))

    def test_without_an_lrv_the_two_files_go_side_by_side(self):
        first, second, _lrv = make_one_r(self, self.folder, lrv=False)
        result = self.read(first)
        self.assertEqual(result["projection"], "dual_fisheye")
        facts = result["exif"]["video"]
        self.assertNotIn("lrv", facts)
        self.assertEqual(facts["lenses"], "files")
        frames = [argv for argv in self.commands if argv[-1] == "pipe:1"]
        self.assertTrue(frames)
        for argv in frames:
            inputs = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-i"]
            self.assertEqual(inputs, ["file:" + first, "file:" + second])
            graph = argv[argv.index("-filter_complex") + 1]
            self.assertTrue(graph.startswith("[0:v:0][1:v:0]hstack=inputs=2,"), graph)
        # A playable copy: both files side by side, the first one's sound.
        target = self.base.join("proxy.mp4")
        videos.make_proxy(self.tools, first, target, height=480)
        argv = [argv for argv in self.commands if "libx264" in argv][-1]
        self.assertEqual([argv[index + 1] for index, arg in enumerate(argv) if arg == "-i"],
                         ["file:" + first, "file:" + second])
        self.assertEqual(argv[argv.index("-filter_complex") + 1],
                         "[0:v:0][1:v:0]hstack=inputs=2,scale=w=960:h=480,format=yuv420p[frame]")
        self.assertIn("0:a:0?", argv)

    def test_a_second_lens_that_cannot_be_read_is_left_out(self):
        first, second, _lrv = make_one_r(self, self.folder)
        with open(second, "wb") as handle:
            handle.write(b"not a video")
        result = self.read(first)
        facts = result["exif"]["video"]
        # (The LRV still holds both lenses.)
        self.assertEqual(facts["lenses"], "lrv")
        self.assertNotIn("lens_file", facts)
        os.remove(os.path.join(self.folder, ONE_LRV))
        self.assertEqual(self.read(first)["projection"], "")


class Library360Case(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):
    """A library with an X4 style 360 video and its LRV, one without (in solo/), and a
    ONE R style recording with its LRV (in oner/)."""

    SOLO = "solo/VID_20190514_150000_00_008.insv"
    ONE_R = "oner/" + ONE_A

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.library_folder("Trips")
        self.video = self.make_video(self.folder, INSV, insv_probe(),
                                     trailer=fake_ffmpeg.insta360_trailer())
        self.lrv = self.make_video(self.folder, LRV, lrv_probe())
        self.solo = self.make_video(self.folder, self.SOLO, insv_probe(duration=6.0),
                                    trailer=fake_ffmpeg.insta360_trailer())
        self.one_r = make_one_r(self, os.path.join(self.folder, "oner"))

    def configure(self, **extra):
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips1")],
                               **extra)
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        return self.config

    def scan(self):
        self.ctx = jobs.JobContext("test", log_limit=5000)
        return scan.run(self.config, self.ctx, describe_images=False)["libraries"][0]

    def log(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=500)["log_tail"])

    def rows(self):
        conn = db.connect()
        try:
            return {row["path"]: dict(row) for row in conn.execute("SELECT * FROM images")}
        finally:
            conn.close()

    def execute(self, sql, args=()):
        conn = db.connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def records(self, **options):
        conn = db.connect()
        try:
            return {record["i"]: record for record in search_index.build_records(
                conn, self.config, ["lib_trips1"], use_cache=False, **options)}
        finally:
            conn.close()

    def candidates(self, root=None):
        conn = db.connect()
        try:
            return [row["path"] for row in videos.proxy_candidates(conn, "lib_trips1", root)]
        finally:
            conn.close()


class ScanTest(Library360Case):

    def test_the_insv_files_are_indexed_and_their_companions_are_not(self):
        self.configure()
        summary = self.scan()
        self.assertEqual(summary["added"], 3)
        rows = self.rows()
        self.assertEqual(sorted(rows), sorted([self.SOLO, INSV, self.ONE_R]))
        row = rows[INSV]
        self.assertEqual((row["media"], row["projection"]), ("video", "dual_fisheye"))
        self.assertEqual((row["width"], row["height"]), (2 * LENS, LENS))
        self.assertEqual(row["camera"], "Arashi Vision Insta360 X4")
        self.assertEqual((row["taken_at"], row["tz_offset"]), ("2019-05-14T14:23:01", "+02:00"))
        facts = json.loads(row["exif"])["video"]
        self.assertEqual(facts["lrv"]["name"], LRV)
        previews = cache.PreviewStore()
        with Image.open(io.BytesIO(previews.read("lib_trips1", row["id"]))) as preview:
            self.assertEqual(preview.size, (832, 416))
        self.assertIsNotNone(previews.read_sheet("lib_trips1", row["id"]))
        self.assertIsNone(previews.read_pano("lib_trips1", row["id"]))
        solo = rows[self.SOLO]
        self.assertEqual(solo["projection"], "dual_fisheye")
        self.assertNotIn("lrv", json.loads(solo["exif"])["video"])
        one_r = rows[self.ONE_R]
        self.assertEqual((one_r["projection"], one_r["camera"]),
                         ("dual_fisheye", "Arashi Vision Insta360 OneR"))
        self.assertEqual(json.loads(one_r["exif"])["video"]["lrv"]["name"], ONE_LRV)
        # Nothing is read again: a 360 video has no pano to wait for.
        del self.commands[:]
        again = self.scan()
        self.assertEqual(again["added"], 0)
        self.assertEqual(self.commands, [])
        self.assertNotIn("360-degree photo", self.log())

    def test_companions_that_come_and_go_read_the_video_again(self):
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1",
                     (json.dumps({"title": "Lawn"}),))
        before = self.rows()
        self.assertEqual(self.candidates(), [self.SOLO])
        # The X4 video's LRV is deleted, the solo one gets one, and the
        # ONE R recording loses its second lens's file.
        os.remove(self.lrv)
        self.make_video(self.folder, "solo/LRV_20190514_150000_01_008.lrv", lrv_probe(6.0))
        os.remove(self.one_r[1])
        del self.commands[:]
        summary = self.scan()
        self.assertEqual((summary["updated"], summary["added"], summary["changed"]), (3, 0, 0))
        rows = self.rows()
        for path in (INSV, self.SOLO, self.ONE_R):
            self.assertEqual(rows[path]["id"], before[path]["id"])
            self.assertEqual(rows[path]["described"], 1, path)       # (description kept)
        facts = {path: json.loads(rows[path]["exif"])["video"] for path in rows}
        self.assertNotIn("lrv", facts[INSV])
        self.assertEqual(facts[self.SOLO]["lrv"]["name"], "LRV_20190514_150000_01_008.lrv")
        self.assertEqual((facts[self.ONE_R]["lenses"], facts[self.ONE_R]["lrv"]["name"]),
                         ("lrv", ONE_LRV))
        self.assertNotIn("lens_file", facts[self.ONE_R])
        # What plays changed with them.
        self.assertEqual(self.candidates(), [INSV])
        records = {record["n"]: record for record in self.records().values()}
        self.assertNotIn("vc", records[INSV])
        self.assertEqual(records[os.path.basename(self.SOLO)]["vc"], "h264")
        # Nothing changed since: nothing is read.
        del self.commands[:]
        self.assertEqual(self.scan()["updated"], 0)
        self.assertEqual(self.commands, [])
        # Without its LRV the ONE R file is one lens: an ordinary video.
        os.remove(self.one_r[2])
        self.scan()
        self.assertEqual(self.rows()[self.ONE_R]["projection"], "")

    def test_new_pole_fills_stitch_the_posters_again(self):
        self.configure()
        self.scan()
        self.configure(nadir_fill_deg=20)
        del self.commands[:]
        self.scan()
        self.assertTrue(self.commands)
        row = self.rows()[INSV]
        self.assertEqual(json.loads(row["exif"])["video"]["fisheye"]["fill"], [20.0, 0.0])
        self.assertIn("Stitching 3 360-degree video(s) in Trips again", self.log())
        self.assertNotIn("photo(s)", self.log())

    def test_the_proxies_job_copies_only_videos_with_nothing_else_to_play(self):
        self.configure(video_proxies={"enabled": True, "height": 240})
        self.scan()
        rows = self.rows()
        # (What the Manage page counts as waiting for a copy.)
        self.assertEqual(self.candidates(), [self.SOLO])
        conn = db.connect()
        try:
            # A stale copy of the video with an LRV is removed.
            stale = videos.proxy_path("lib_trips1", rows[INSV]["id"], rows[INSV]["content_hash"])
            os.makedirs(os.path.dirname(stale), exist_ok=True)
            with open(stale, "wb") as handle:
                handle.write(b"old")
            summary = videos.make_proxies(conn, self.config, jobs.JobContext("proxies"),
                                          ["lib_trips1"])
        finally:
            conn.close()
        self.assertEqual((summary["made"], summary["removed"], summary["failed"]), (1, 1, 0))
        self.assertFalse(os.path.exists(stale))
        solo = rows[self.SOLO]
        copy = videos.existing_proxy("lib_trips1", solo["id"], solo["content_hash"])
        self.assertIsNotNone(copy)
        streams = videos.probe(self.tools, copy)["streams"]
        self.assertEqual([(stream["width"], stream["height"]) for stream in streams
                          if stream["codec_type"] == "video"], [(480, 240)])
        # An LRV deleted since the scan: the job looks for it now.
        os.remove(self.lrv)
        self.assertEqual(self.candidates(), [self.SOLO])
        self.assertEqual(self.candidates(self.folder), [INSV, self.SOLO])

    def test_lens_streams_browsers_play_still_need_a_copy(self):
        # (H.264 streams: no browser plays a video of two of them.)
        self.make_video(self.folder, "h264/VID_20190514_170000_00_020.insv",
                        insv_probe(codec="h264"), trailer=fake_ffmpeg.insta360_trailer())
        # One 2:1 stream with both lenses: H.264 plays as it is; HEVC needs a copy.
        self.make_video(self.folder, "frame/VID_20190514_180000_00_021.insv", lrv_probe(),
                        trailer=fake_ffmpeg.insta360_trailer())
        self.make_video(self.folder, "frame/VID_20190514_180100_00_022.insv",
                        lrv_probe(codec="hevc"), trailer=fake_ffmpeg.insta360_trailer())
        self.configure()
        self.scan()
        self.assertEqual(sorted(self.candidates()),
                         ["frame/VID_20190514_180100_00_022.insv",
                          "h264/VID_20190514_170000_00_020.insv", self.SOLO])
        records = {record["n"]: record for record in self.records().values()}
        self.assertNotIn("vc", records["VID_20190514_170000_00_020.insv"])
        self.assertNotIn("vc", records[os.path.basename(self.SOLO)])
        self.assertEqual(records["VID_20190514_180000_00_021.insv"]["vc"], "h264")
        self.assertEqual(records["VID_20190514_180100_00_022.insv"]["vc"], "hevc")
        self.assertTrue(all("fx" in record for record in records.values()))

    def test_looks_see_the_front_view_of_the_poster(self):
        self.configure()
        self.scan()
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM images WHERE path = ?", (INSV,)).fetchone()
            self.assertEqual(looks.kind_of(row), looks.KIND_POSTER)
            data = looks.picture_jpeg(row, {"max_edge": 1024}, cache.PreviewStore(),
                                      self.config, root=self.folder)
        finally:
            conn.close()
        with Image.open(io.BytesIO(data)) as picture:
            self.assertAlmostEqual(picture.size[0] / float(picture.size[1]), 4.0 / 3.0, places=2)

    def test_search_records(self):
        self.configure()
        self.scan()
        rows = self.rows()
        records = self.records()
        exported = self.records(for_export=True)
        record = records[rows[INSV]["id"]]
        self.assertEqual((record["p"], record["mv"], record["ty"], record["vc"]),
                         ("360", 1, "video", "h264"))
        self.assertEqual(record["fx"], search_index.fisheye_record(
            json.loads(rows[INSV]["exif"])["video"]["fisheye"]))
        self.assertEqual(record["fx"]["front"], 1)
        self.assertEqual(records[rows[self.ONE_R]["id"]]["fx"]["turn"], [90.0, 270.0])
        # Without an LRV only a playable copy plays: no codec to ask about.
        solo = records[rows[self.SOLO]["id"]]
        self.assertNotIn("vc", solo)
        self.assertIn("fx", solo)
        self.assertTrue(all("fx" in item for item in exported.values()))

    def test_fisheye_records_take_only_the_expected_shapes(self):
        good = {"lens": [[0.25, 0.5, 0.5], [0.75, 0.5, 0.5]], "fov": 190.8, "turn": [-0.4, 0],
                "front": 1, "mirror": False, "blend": 3.0, "gain": [[1, 1, 1], [1, 1, 1]],
                "fill": [0, 0]}
        self.assertEqual(search_index.fisheye_record(good)["fov"], 190.8)
        for change in ({"lens": [[0.25, 0.5]]}, {"fov": 80}, {"front": 2}, {"fov": "x"},
                       {"gain": [[1, 1, float("nan")], [1, 1, 1]]}, {"turn": None},
                       {"blend": True}, {"fill": [0]}):
            self.assertIsNone(search_index.fisheye_record(dict(good, **change)), change)
        self.assertIsNone(search_index.fisheye_record(None))


class DescribeTest(Library360Case):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(describe, "UNREACHABLE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_model_sees_stitched_frames_and_is_told_so(self):
        os.remove(self.solo)
        shutil.rmtree(os.path.join(self.folder, "oner"))
        self.configure(endpoints=[{"name": "local", "kind": "openai",
                                   "base_url": self.server.base_url, "api_key": "",
                                   "model": "fake-model"}], default_endpoint="local")
        self.scan()
        conn = db.connect()
        try:
            summary = describe.describe_pending(conn, self.config,
                                                jobs.JobContext("describe", log_limit=500),
                                                sleep=lambda seconds: None)
        finally:
            conn.close()
        self.assertEqual(summary["described"], 1)
        request, = self.server.describe_requests()
        self.assertIn("4 frames, in order", request["prompt"])
        self.assertIn("It is a 360-degree video: each of its frames here is a 360-degree view",
                      request["prompt"])
        row = self.rows()[INSV]
        sheet = cache.PreviewStore().read_sheet("lib_trips1", row["id"])
        with Image.open(io.BytesIO(request["image"])) as sent, \
                Image.open(io.BytesIO(sheet)) as kept:
            self.assertEqual(sent.size, kept.size)


class PromptTest(unittest.TestCase):

    def test_a_360_videos_sheet_is_frames_of_panoramas(self):
        text = prompts.user_text(None, projection="dual_fisheye", video=(4, 11))
        self.assertIn("4 frames, in order", text)
        self.assertIn("each of its frames here is a 360-degree view", text)
        self.assertIn("never mention frames, panels, a grid or a collage", text)
        self.assertNotIn("360-degree photo", text)
        self.assertLess(text.index("4 frames"), text.index("360-degree view"))
        # A 360 photo's note is as it was.
        self.assertIn("This is a 360-degree photo",
                      prompts.user_text(None, projection="dual_fisheye"))


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(Library360Case):

    def setUp(self):
        super().setUp()
        self.frame = self.make_video(self.folder, "frame/VID_20190514_180000_00_021.insv",
                                     lrv_probe(), trailer=fake_ffmpeg.insta360_trailer())
        self.configure()
        self.scan()
        self.ids = {path: row["id"] for path, row in self.rows().items()}
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def detail(self, path):
        return self.client.get("/api/image/%d" % self.ids[path]).get_json()

    def test_the_player_gets_the_lrv(self):
        url = "/video/%d" % self.ids[INSV]
        with open(self.lrv, "rb") as handle:
            data = handle.read()
        response = self.client.get(url)
        self.assertEqual((response.status_code, response.mimetype), (200, "video/mp4"))
        self.assertEqual(response.get_data(), data)
        response.close()
        response = self.client.get(url, headers={"Range": "bytes=2-5"})
        self.assertEqual((response.status_code, response.get_data()), (206, data[2:6]))
        response.close()
        detail = self.detail(INSV)
        self.assertTrue(detail["playable_copy"])
        self.assertEqual(detail["projection"], "dual_fisheye")
        # No pano: it plays.
        self.assertEqual(self.client.get("/pano/%d" % self.ids[INSV]).status_code, 404)
        # Also a ONE R recording's LRV (named .insv).
        response = self.client.get("/video/%d" % self.ids[self.ONE_R])
        with open(self.one_r[2], "rb") as handle:
            self.assertEqual(response.get_data(), handle.read())
        response.close()
        # Without its LRV (and without a copy) nothing plays: never lens streams.
        os.remove(self.lrv)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(self.detail(INSV)["playable_copy"])

    def test_an_original_with_both_lenses_in_a_stream_browsers_play_plays(self):
        url = "/video/%d" % self.ids["frame/VID_20190514_180000_00_021.insv"]
        response = self.client.get(url)
        self.assertEqual((response.status_code, response.mimetype), (200, "video/mp4"))
        with open(self.frame, "rb") as handle:
            self.assertEqual(response.get_data(), handle.read())
        response.close()
        self.assertTrue(self.detail("frame/VID_20190514_180000_00_021.insv")["playable_copy"])

    def test_a_playable_copy_comes_first(self):
        row = self.rows()[self.SOLO]
        url = "/video/%d" % row["id"]
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(self.detail(self.SOLO)["playable_copy"])
        proxy = videos.proxy_path("lib_trips1", row["id"], row["content_hash"])
        os.makedirs(os.path.dirname(proxy), exist_ok=True)
        with open(proxy, "wb") as handle:
            handle.write(b"both lenses")
        response = self.client.get(url)
        self.assertEqual(response.get_data(), b"both lenses")
        response.close()
        self.assertTrue(self.detail(self.SOLO)["playable_copy"])

    def test_poster_and_original(self):
        video_id = self.ids[INSV]
        self.execute("UPDATE images SET has_preview = 0 WHERE id = ?", (video_id,))
        made = self.client.get("/preview/%d" % video_id)
        self.assertEqual(made.status_code, 200)
        with Image.open(io.BytesIO(made.get_data())) as poster:
            self.assertEqual(poster.size, (832, 416))
        original = self.client.get("/image/%d" % video_id)
        self.assertEqual(original.headers["Content-Disposition"], "attachment; filename=" + INSV)
        original.close()

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_site_js_makes_the_records_the_server_sends(self):
        import test_ui_scripts
        records = self.client.get("/api/index").get_json()["records"]
        pairs = [{"record": record,
                  "detail": self.client.get("/api/image/%d" % record["i"]).get_json()}
                 for record in records]
        self.assertEqual(len(pairs), 4)
        self.assertTrue(all("fx" in pair["record"] for pair in pairs))
        self.assertEqual(sorted(pair["record"].get("vc", "") for pair in pairs),
                         ["", "h264", "h264", "h264"])
        path = self.base.join("pairs.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"pairs": pairs}, handle)
        code, output = test_ui_scripts.run_node("--compare", path)
        self.assertEqual(code, 0, "\n" + output)
        self.assertIn("4 of 4 passed", output)


class ExportTest(Library360Case):

    def setUp(self):
        super().setUp()
        self.configure()
        self.scan()
        self.describe_all()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def describe_all(self):
        description = {"title": "Lawn", "description": "A lawn.", "tags": ["lawn"],
                       "subjects": ["lawn"], "photo_type": "photo", "document_type": "none",
                       "setting": "outdoor", "time_of_day": "day", "event": "none",
                       "people": "none", "sensitive": "none", "private_info": False,
                       "aesthetic": 3, "sharpness": 4, "text": "", "colors": ["green"]}
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps(description), prompts.PROMPT_VER))

    def export(self, options=None):
        lines = []
        summary = export.export_library(export.LogContext(lines.append), self.conn,
                                        common.load_config(), "lib_trips1", options=options)
        return summary, lines

    def published(self, out):
        records = []
        data = os.path.join(out, "data")
        for name in sorted(os.listdir(data)):
            if name.startswith("index-"):
                with open(os.path.join(data, name), encoding="ascii") as handle:
                    text = handle.read()
                records.extend(json.loads(text[text.index("(") + 1:text.rindex(")")]))
        return {record["i"]: record for record in records}

    def test_the_lrv_goes_out_without_its_metadata(self):
        options = {"videos": "include"}
        preview = export.preview_export(self.conn, common.load_config(), "lib_trips1", options)
        self.assertEqual(preview["counts"]["videos"],
                         {"total": 3, "included": 2, "not_playable": 1, "too_large": 0})
        self.assertEqual(preview["size_parts"]["videos"],
                         os.path.getsize(self.lrv) + os.path.getsize(self.one_r[2]))
        self.assertEqual(preview["size_parts"]["panos"], 0)
        summary, lines = self.export(options)
        self.assertEqual(summary["videos"], 2, lines)
        out = summary["output"]
        self.assertFalse(os.path.isdir(os.path.join(out, "panos")))
        files = sorted(os.listdir(os.path.join(out, "videos")))
        self.assertEqual(len(files), 2)
        records = self.published(out)
        widths = set()
        for name in files:
            copied = videos.probe(self.tools, os.path.join(out, "videos", name))
            self.assertEqual(copied["format"]["tags"], {"major_brand": "isom"})
            widths.add(copied["streams"][0]["width"])          # the LRVs'
            record = records[name[:-len(".mp4")]]
            self.assertEqual((record["p"], record["mv"], record.get("vf"), record.get("vc")),
                             ("360", 1, 1, "h264"))
            self.assertIn(record["fx"]["front"], (0, 1))
            self.assertNotIn("pn", record)
            with Image.open(os.path.join(out, "images", record["i"] + ".jpg")) as poster:
                self.assertEqual(poster.size[0], 2 * poster.size[1])
        self.assertEqual(widths, {832, 736})
        # Too large: the poster only.
        real = videos.lrv_facts

        def large(exif_text):
            found = real(exif_text)
            return dict(found, size=300 << 20, media_size=None) if found else found
        with mock.patch.object(videos, "lrv_facts", large):
            preview = export.preview_export(self.conn, common.load_config(), "lib_trips1",
                                            dict(options, max_video_mb=200))
        self.assertEqual(preview["counts"]["videos"]["too_large"], 2)
        self.assertEqual(preview["counts"]["videos"]["included"], 0)

    def test_an_lrvs_size_is_what_its_copy_keeps(self):
        row = self.rows()[INSV]
        exif = json.loads(row["exif"])
        exif["video"]["lrv"]["media_size"] = 1234
        self.execute("UPDATE images SET exif = ? WHERE id = ?", (json.dumps(exif), row["id"]))
        preview = export.preview_export(self.conn, common.load_config(), "lib_trips1",
                                        {"videos": "include"})
        self.assertEqual(preview["size_parts"]["videos"], 1234 + os.path.getsize(self.one_r[2]))
        # An LRV browsers do not play is not published.
        exif["video"]["lrv"]["codec"] = "hevc"
        self.execute("UPDATE images SET exif = ? WHERE id = ?", (json.dumps(exif), row["id"]))
        preview = export.preview_export(self.conn, common.load_config(), "lib_trips1",
                                        {"videos": "include"})
        self.assertEqual(preview["counts"]["videos"]["included"], 1)

    def test_an_lrv_that_went_away_keeps_the_poster(self):
        os.remove(self.lrv)
        os.remove(self.one_r[2])
        summary, lines = self.export({"videos": "include"})
        self.assertEqual(summary["videos"], 0)
        self.assertTrue(any(".lrv" in line for line in lines), lines)

    def test_a_published_copy_says_h264(self):
        # (An HEVC video published as its playable copy: the site's player
        # must not ask the browser about HEVC.)
        self.make_video(self.folder, "clip.mp4",
                        fake_ffmpeg.probe_json(codec="hevc", duration=5.0))
        self.scan()
        self.describe_all()
        row = self.rows()["clip.mp4"]
        videos.make_proxy(self.tools, os.path.join(self.folder, "clip.mp4"),
                          videos.proxy_path("lib_trips1", row["id"], row["content_hash"]))
        summary, lines = self.export({"videos": "include"})
        clip, = [record for record in self.published(summary["output"]).values()
                 if "p" not in record]
        self.assertEqual((clip.get("vf"), clip.get("vc")), (1, "h264"))
        self.assertEqual(export.published_codec(self.rows()["clip.mp4"], None), "hevc")


def _real_tools():
    """Tools for a real ffmpeg (SEARCH_IMAGES_TEST_FFMPEG, else the PATH), or None."""
    ffmpeg, ffprobe, _problem = videos.locate(os.environ.get("SEARCH_IMAGES_TEST_FFMPEG", ""))
    return videos.Tools([ffmpeg], [ffprobe]) if ffmpeg and ffprobe else None


@unittest.skipIf(_real_tools() is None, "ffmpeg and ffprobe are not installed")
class RealFfmpegTest(support.TempBaseMixin, unittest.TestCase):
    """With the real programs (CPU only): made-up .insv files (two lens streams in one,
    or one lens per file) with an Insta360 trailer, and LRVs."""

    def make(self, name, inputs, maps, model=None):
        argv = self.tools.ffmpeg + ["-nostdin", "-v", "error"]
        for source in inputs:
            argv += ["-f", "lavfi", "-i", source]
        for index in maps:
            argv += ["-map", index]
        path = os.path.join(self.folder, name)
        argv += ["-c:v", "mpeg4", "-c:a", "aac", "-metadata",
                 "creation_time=2019-05-14T12:23:01Z", "-f", "mp4", path]
        made = subprocess.run(argv, capture_output=True, timeout=120)
        if made.returncode != 0:
            self.skipTest("this ffmpeg cannot make the test files: %s" % made.stderr[-200:])
        if model:
            with open(path, "ab") as handle:
                handle.write(fake_ffmpeg.insta360_trailer(model))
        return path

    def setUp(self):
        super().setUp()
        self.tools = _real_tools()
        self.folder = self.base.make_dir("cam")

    def lens(self, name, source="testsrc", model=None):
        return self.make(name, ["%s=size=256x256:rate=10:duration=2" % source,
                                "sine=frequency=440:duration=2"], ["0:v", "1:a"], model)

    def copy_or_skip(self, source, target):
        try:
            videos.make_proxy(self.tools, source, target, height=128)
        except videos.VideoReadError as error:
            if "libx264" in str(error) or "Unknown encoder" in str(error):
                self.skipTest("this ffmpeg has no libx264")
            raise
        return [(stream["codec_name"], stream["width"], stream["height"])
                for stream in videos.probe(self.tools, target)["streams"]
                if stream["codec_type"] == "video"]

    def test_both_ways_of_reading_the_frames(self):
        video = self.make(INSV, ["testsrc=size=256x256:rate=10:duration=2",
                                 "testsrc2=size=256x256:rate=10:duration=2",
                                 "sine=frequency=440:duration=2"],
                          ["0:v", "1:v", "2:a"], "Insta360 X4")
        # From the lens streams side by side.
        result = videos.read_video(video, self.tools, sheet_edge=512)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual((result["width"], result["height"]), (512, 256))
        self.assertEqual((result["taken_at"], result["tz_offset"]),
                         ("2019-05-14T14:23:01", "+02:00"))
        self.assertEqual(result["camera"], "Arashi Vision Insta360 X4")
        self.assertEqual(result["exif"]["video"]["lenses"], "streams")
        with Image.open(io.BytesIO(result["preview"])) as preview:
            self.assertEqual(preview.size, (512, 256))
        self.assertEqual(result["exif"]["video"]["sheet_frames"], 4)
        # From the LRV: its media data is what a copy keeps.
        lrv = self.make(LRV, ["testsrc=size=384x192:rate=10:duration=2",
                              "sine=frequency=440:duration=2"], ["0:v", "1:a"], "Insta360 X4")
        result = videos.read_video(video, self.tools, sheet_edge=512)
        facts = result["exif"]["video"]["lrv"]
        self.assertEqual(facts["codec"], "mpeg4")
        self.assertLess(0, facts["media_size"])
        self.assertLess(facts["media_size"], facts["size"])
        with Image.open(io.BytesIO(result["preview"])) as preview:
            self.assertEqual(preview.size, (384, 192))
        stripped = self.base.join("stripped.mp4")
        videos.strip_copy(self.tools, lrv, stripped)
        self.assertLess(abs(os.path.getsize(stripped) - facts["media_size"]),
                        facts["media_size"] // 2 + 4096)
        self.assertEqual(videos.insta360_model(stripped), "")

    def test_one_file_per_lens(self):
        first = self.lens(ONE_A, "testsrc", "Insta360 OneR")
        second = self.lens(ONE_B, "testsrc2")
        result = videos.read_video(first, self.tools, sheet_edge=512)
        self.assertEqual(result["projection"], "dual_fisheye")
        self.assertEqual(result["exif"]["video"]["lenses"], "files")
        self.assertEqual((result["width"], result["height"]), (512, 256))
        self.assertEqual(result["exif"]["video"]["fisheye"]["turn"], [90.0, 270.0])
        self.assertTrue(videos.is_companion(os.path.basename(second), os.listdir(self.folder)))
        self.assertEqual(self.copy_or_skip(first, self.base.join("copy.mp4")),
                         [("h264", 256, 128)])

    def test_a_copy_with_both_lenses(self):
        video = self.make(INSV, ["testsrc=size=256x256:rate=10:duration=2",
                                 "testsrc2=size=256x256:rate=10:duration=2",
                                 "sine=frequency=440:duration=2"],
                          ["0:v", "1:v", "2:a"], "Insta360 X4")
        self.assertEqual(self.copy_or_skip(video, self.base.join("copy.mp4")),
                         [("h264", 256, 128)])


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PlayerLensModelTest(unittest.TestCase):
    """The player's shader (viewer.js FISHEYE_SHADER, its own text run by
    tests/js/glsl.js) samples where images.py's stitch does, for each camera."""

    def run_shader(self, fx, frame_size, directions, levels, gain):
        script = (
            "var V = require(process.argv[1]); var G = require(process.argv[2]);"
            "var data = JSON.parse(process.argv[3]);"
            "var p = V.format.fisheyeParams(data.fx, data.size[0], data.size[1]);"
            "function v(c) { return {x: c[0], y: c[1], z: c[2]}; }"
            "var f = G.functions(V.format.fisheyeShader,"
            " ['lens', 'frontPoint', 'backPoint', 'frontWeight', 'gained'],"
            " {u_front: v(p.front), u_back: v(p.back), u_turn: {x: p.frontTurn, y: p.backTurn},"
            "  u_aspect: p.aspect, u_scale: p.scale, u_mirror: p.mirror ? 1 : 0,"
            "  u_blend: p.blend});"
            "console.log(JSON.stringify({points: data.directions.map(function (d) {"
            " var at = {x: d[0], y: d[1], z: d[2]};"
            " var front = f.frontPoint(at); var back = f.backPoint(at);"
            " return [[front.x, front.y], [back.x, back.y], f.frontWeight(at)]; }),"
            " gained: data.levels.map(function (level) {"
            " return 255 * f.gained(level / 255, data.gain); })}));")
        result = subprocess.run(
            [NODE, "-e", script, VIEWER, GLSL,
             json.dumps({"fx": fx, "size": frame_size, "directions": directions,
                         "levels": levels, "gain": gain})],
            capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def check(self, camera, frame_size, layout):
        params = getattr(images, videos.camera_360(camera)["params"])
        frame = Image.new("RGB", frame_size, (128, 128, 128))
        circles = images.find_lens_circles(frame, params)
        v360 = videos.Video360(None, INSV, {"width": LENS, "height": LENS, "video": {}},
                               layout, videos.camera_360(camera), stream_width=LENS)
        v360.frame_size, v360.circles = frame.size, circles
        with mock.patch.object(images, "stitch_settings",
                               return_value={"nadir_fill_deg": 0.0, "zenith_fill_deg": 0.0}):
            fx = search_index.fisheye_record(v360.fisheye())
        front = 0 if params["front"] == "left" else 1
        half_fov = math.radians(params["fov"] / 2.0)
        lenses = [images._fisheye_point(circles[half], half != front, params["rotation"][half],
                                        params["mirror"], half_fov) for half in (0, 1)]
        blend = math.radians(params["blend"])
        directions, expected = [], []
        for latitude in range(-80, 81, 20):
            for longitude in range(-180, 180, 15):
                for nudge in (0.0, 1.3):
                    lat, lon = math.radians(latitude + nudge), math.radians(longitude + nudge)
                    x = math.cos(lat) * math.sin(lon)
                    y = math.sin(lat)
                    z = math.cos(lat) * math.cos(lon)
                    directions.append([x, y, z])
                    places = []
                    for half in (front, 1 - front):
                        px, py = lenses[half](x, y, z)
                        places.append([px / float(frame_size[0]), py / float(frame_size[1])])
                    t = min(1.0, max(0.0, 0.5 + 0.5 * math.asin(max(-1.0, min(1.0, z))) / blend))
                    expected.append(places + [t * t * (3.0 - 2.0 * t)])
        levels = list(range(0, 255, 5))
        found = self.run_shader(fx, frame_size, directions, levels, 1.15)
        self.assertEqual(len(found["points"]), len(expected))
        for direction, got, want in zip(directions, found["points"], expected):
            for lens in (0, 1):
                for axis in (0, 1):
                    self.assertAlmostEqual(got[lens][axis], want[lens][axis], places=4,
                                           msg=(camera, direction, lens))
            self.assertAlmostEqual(got[2], want[2], places=6, msg=(camera, direction))
        # The lenses evened out as images.py does (to the nearest level).
        table = images._gain_table(1.15)
        for level, got in zip(levels, found["gained"]):
            self.assertLessEqual(abs(got - table[level]), 0.51, level)

    def test_the_shader_samples_where_the_stitch_does(self):
        self.check("Insta360 X4", (1664, 832), "streams")
        self.check("Insta360 OneR", (736, 368), "files")


if __name__ == "__main__":
    unittest.main()
