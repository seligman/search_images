"""Tests for videos.py: ffprobe facts, the quick hash, posters, contact sheets, copies.

ffmpeg and ffprobe are played by tests/fake_ffmpeg.py (see videotools.py);
RealFfmpegTest runs the real programs when they are installed (on the PATH,
or named by the environment variable SEARCH_IMAGES_TEST_FFMPEG) and is
skipped otherwise.
"""

import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

from PIL import Image

import fake_ffmpeg
import videotools

import common
import imageinfo
import images
import prompts
import videos


def probe_of(**changes):
    return videotools.clip_probe(**changes)


class ParseTest(unittest.TestCase):
    """parse_probe and its parts, on ffprobe answers shaped like real ones."""

    def test_a_phone_video(self):
        info = videos.parse_probe(probe_of(), "IMG_0001.MOV")
        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertEqual(info["duration"], 24.0)
        # The QuickTime creation date: local time where it was taken, with its offset.
        self.assertEqual((info["taken_at"], info["taken_source"], info["tz_offset"]),
                         ("2021-07-04T11:30:15", "video", "-07:00"))
        self.assertEqual((info["lat"], info["lon"]), (33.4484, -112.074))
        self.assertEqual(info["camera"], "Acme Phone 9")
        self.assertEqual(info["video"], {
            "codec": "h264", "profile": "High", "fps": 30.0, "bitrate": 8000000,
            "audio_codec": "aac", "rotation": 0, "container": "mov", "pix_fmt": "yuv420p"})
        self.assertIsNone(info["sar"])

    def test_rotation_turns_the_size(self):
        info = videos.parse_probe(videotools.portrait_probe(), "IMG_0002.MOV")
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertEqual(info["video"]["rotation"], 90)
        self.assertEqual(info["video"]["hdr"], "hlg")
        upside_down = videos.parse_probe(fake_ffmpeg.probe_json(rotation=-180), "a.mp4")
        self.assertEqual((upside_down["width"], upside_down["height"]), (1920, 1080))
        self.assertEqual(upside_down["video"]["rotation"], 180)
        # Older ffmpeg versions name the turn in a "rotate" tag.
        older = fake_ffmpeg.probe_json(stream_tags={"rotate": "270"})
        info = videos.parse_probe(older, "b.mp4")
        self.assertEqual((info["width"], info["height"], info["video"]["rotation"]),
                         (1080, 1920, 270))

    def test_non_square_pixels(self):
        data = fake_ffmpeg.probe_json(width=720, height=480)
        data["streams"][0]["sample_aspect_ratio"] = "32:27"
        info = videos.parse_probe(data, "tape.mpg")
        self.assertEqual((info["width"], info["height"]), (853, 480))
        self.assertAlmostEqual(info["sar"], 32 / 27.0)
        data["streams"][0]["sample_aspect_ratio"] = "1:1"
        self.assertIsNone(videos.parse_probe(data, "tape.mpg")["sar"])

    def test_utc_creation_time_shows_in_local_time(self):
        data = fake_ffmpeg.probe_json(format_tags={"creation_time": "2023-01-22T20:38:54.000000Z"})
        info = videos.parse_probe(data, "DJI_0001.MP4")
        epoch = (datetime.datetime(2023, 1, 22, 20, 38, 54)
                 - datetime.datetime(1970, 1, 1)).total_seconds()
        self.assertEqual((info["taken_at"], info["taken_source"], info["tz_offset"]),
                         (imageinfo.local_iso(epoch), "video", ""))

    def test_dates_fall_back_like_photos(self):
        # A zero creation time (1904 or 1970) is no date: the file name is next...
        zero = {"creation_time": "1904-01-01T00:00:00.000000Z"}
        data = fake_ffmpeg.probe_json(format_tags=zero, stream_tags=zero)
        info = videos.parse_probe(data, "VID_20190514_142301.mp4", mtime=0)
        self.assertEqual((info["taken_at"], info["taken_source"]),
                         ("2019-05-14T14:23:01", "filename"))
        # ...then the file's time.
        data = fake_ffmpeg.probe_json(format_tags={"creation_time": "1970-01-01T00:00:00Z"},
                                      stream_tags={})
        self.assertEqual(videos.parse_probe(data, "clip.mp4")["taken_source"], "")
        info = videos.parse_probe(data, "clip.mp4", mtime=1600000000)
        self.assertEqual((info["taken_at"], info["taken_source"]),
                         (imageinfo.local_iso(1600000000), "mtime"))
        # The stream's creation time when the container has none.
        data = fake_ffmpeg.probe_json(stream_tags={"creation_time": "2020-02-03T04:05:06Z"})
        del data["format"]["tags"]["creation_time"]
        self.assertEqual(videos.parse_probe(data, "c.mp4")["taken_source"], "video")

    def test_the_creation_time_and_a_date_in_the_name(self):
        def stamp(*when):
            return videos.time.mktime(datetime.datetime(*when).timetuple())

        def utc(seconds):
            # The UTC creation time a camera writes for a moment.
            return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000000Z")

        def utc_name(seconds, pattern):
            return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime(
                pattern)

        def read(name, created, mtime=None):
            data = fake_ffmpeg.probe_json(format_tags={"creation_time": created},
                                          stream_tags={})
            info = videos.parse_probe(data, name, mtime)
            return info["taken_at"], info["taken_source"]

        # A Pixel phone names its videos in UTC: the creation time, in local
        # time, is when it was taken (the name would be hours off).
        taken = stamp(2021, 7, 4, 11, 30, 15)
        name = utc_name(taken, "PXL_%Y%m%d_%H%M%S123.mp4")
        self.assertEqual(read(name, utc(taken), taken + 25)[0], "2021-07-04T11:30:15")
        if utc_name(taken, "%H") != "11":
            self.assertEqual(read(name, utc(taken), taken + 25)[1], "video")
        # A camera that writes when the recording ended: the name (when it
        # started) is at most 15 minutes from it, and more precise.
        ended = stamp(2019, 3, 5, 9, 30, 30)
        self.assertEqual(read("DJI_20190305_093004_456_video_eis.MP4", utc(ended), ended),
                         ("2019-03-05T09:30:04", "filename"))
        # An app's export, created long after the file it came from was
        # written: the name says when it was recorded.
        exported = stamp(2024, 2, 10, 12, 0, 0)
        self.assertEqual(read("DJI_20190305_101500_123_slowmotion.MP4", utc(exported),
                              exported - 50 * 60),
                         ("2019-03-05T10:15:00", "filename"))
        # Otherwise the creation time comes before a date in the name.
        self.assertEqual(read("VID_20190514_142301.mp4", "2020-02-03T04:05:06Z")[1], "video")
        local = videos.parse_tagged_date("2019-05-14T12:00:00Z")[0]
        self.assertEqual(read("VID-%s-WA0001.mp4" % local.strftime("%Y%m%d"),
                              "2019-05-14T12:00:00Z"),
                         (imageinfo.format_datetime(local), "video"))
        self.assertEqual(read("VID-20180101-WA0001.mp4", "2019-05-14T12:00:00Z"),
                         (imageinfo.format_datetime(local), "video"))
        # Without a creation time, the name.
        self.assertEqual(read("VID-20180101-WA0001.mp4", "1970-01-01T00:00:00Z"),
                         ("2018-01-01T00:00:00", "filename"))
        # The QuickTime creation date still comes first.
        tags = {"com.apple.quicktime.creationdate": "2021-07-04T11:30:15-0700"}
        info = videos.parse_probe(fake_ffmpeg.probe_json(format_tags=tags),
                                  "VID_20190514_142301.mp4")
        self.assertEqual(info["taken_source"], "video")

    def test_a_camera_that_writes_its_own_clock_as_utc(self):
        # A dashcam names a clip by its clock (local time) and writes the same
        # clock's time as the creation time, marked as UTC, when the clip
        # ended. The name's time is taken, whatever this computer's time zone.
        def read(name, format_tags, duration=60.0, stream_tags=None):
            data = fake_ffmpeg.probe_json(duration=duration, format_tags=format_tags,
                                          stream_tags=stream_tags or {})
            if "creation_time" not in format_tags:
                del data["format"]["tags"]["creation_time"]
            info = videos.parse_probe(data, name)
            return info["taken_at"], info["taken_source"], info["tz_offset"]

        def utc_offset(when):
            # This computer's UTC offset (seconds) at a local time.
            seconds = videos.time.mktime(when.timetuple())
            return (when - datetime.datetime(1970, 1, 1)).total_seconds() - seconds

        name = "20230817_142005_0412_N_A.MP4"
        start = datetime.datetime(2023, 8, 17, 14, 20, 5)
        # (Where this computer keeps UTC, the usual rules take the name too.)
        source = "video" if utc_offset(start) else "filename"
        ended = {"creation_time": "2023-08-17T14:21:05.000000Z"}
        self.assertEqual(read(name, ended), ("2023-08-17T14:20:05", source, ""))
        # Dated by its start, or a little before it (the clock and the name
        # are a second or two apart): the same.
        for created in ("2023-08-17T14:20:05Z", "2023-08-17T14:18:30Z"):
            self.assertEqual(read(name, {"creation_time": created})[0], "2023-08-17T14:20:05",
                             created)
        # Only the stream says when: the same.
        self.assertEqual(read(name, {}, stream_tags=ended)[0], "2023-08-17T14:20:05")
        # A clip of three minutes dated by its end: still its clock.
        self.assertEqual(read(name, {"creation_time": "2023-08-17T14:23:06Z"}, 181.0)[:2],
                         ("2023-08-17T14:20:05", source))
        if not utc_offset(start):
            return
        # Further from the name than that: an ordinary UTC creation time.
        far = {"creation_time": "2023-08-17T14:26:00Z"}
        self.assertEqual(read(name, far)[1:], ("video", ""))
        self.assertNotEqual(read(name, far)[0], "2023-08-17T14:20:05")
        # A phone writes real UTC: its time converts to the name's, which the
        # usual rules take (from the name, as before).
        utc = datetime.datetime.fromtimestamp(videos.time.mktime(start.timetuple()),
                                              datetime.timezone.utc)
        phone = {"creation_time": utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z")}
        self.assertEqual(read("VID_20230817_142005.mp4", phone),
                         ("2023-08-17T14:20:05", "filename", ""))
        # A phone's own tags, or a name in UTC (Pixel), are never a camera's
        # clock: the creation time stays UTC, as before.
        android = dict(ended, **{"com.android.version": "13"})
        self.assertEqual(read("VID_20230817_142005.mp4", android)[1:], ("video", ""))
        self.assertNotEqual(read("VID_20230817_142005.mp4", android)[0], "2023-08-17T14:20:05")
        self.assertNotEqual(read("PXL_20230817_142005123.mp4", ended)[0],
                            "2023-08-17T14:20:05")
        # A name with a date only says too little.
        self.assertNotEqual(read("VID-20230817-WA0001.mp4", {
            "creation_time": "2023-08-17T00:00:30Z"})[0], "2023-08-17T00:00:00")
        # The QuickTime date still comes first.
        quicktime = dict(ended, **{"com.apple.quicktime.creationdate":
                                   "2023-08-17T09:00:00-0700"})
        self.assertEqual(read(name, quicktime), ("2023-08-17T09:00:00", "video", "-07:00"))

    def test_rows_dated_by_a_camera_clock_read_as_utc_are_found(self):
        # What an older version stored for such a clip: the creation time
        # turned into this computer's time. clock_candidate finds it again.
        name = "20230817_142005_0412_N_A.MP4"
        clock = datetime.datetime(2023, 8, 17, 14, 21, 5)
        stored = videos._utc_to_local(clock)
        if stored == clock:
            self.skipTest("this computer keeps UTC: such dates were never off")
        stored = imageinfo.format_datetime(stored)
        self.assertTrue(videos.clock_candidate(name, stored, 60.0))
        # Not without a time in the name, nor for a name in UTC, nor for a
        # date that already is the name's, nor far from it.
        self.assertFalse(videos.clock_candidate("clip.mp4", stored, 60.0))
        self.assertFalse(videos.clock_candidate("PXL_20230817_142005123.mp4", stored, 60.0))
        self.assertFalse(videos.clock_candidate(name, "2023-08-17T14:20:05", 60.0))
        self.assertFalse(videos.clock_candidate(name, "2023-08-18T02:00:00", 60.0))
        self.assertFalse(videos.clock_candidate(name, "", 60.0))

    def test_a_creation_time_after_the_file_was_written_is_no_recording_time(self):
        # An app that converted the video wrote when it did so.
        created = "2024-02-10T15:20:00Z"
        data = fake_ffmpeg.probe_json(format_tags={"creation_time": created},
                                      stream_tags={"creation_time": created})
        when = videos.parse_tagged_date(created)[0]
        written = videos.time.mktime(when.timetuple()) - 22 * 3600
        info = videos.parse_probe(data, "IMG_0003.MOV", mtime=written)
        self.assertEqual((info["taken_at"], info["taken_source"]),
                         (imageinfo.local_iso(written), "mtime"))
        written = videos.time.mktime(when.timetuple()) - 3600
        info = videos.parse_probe(data, "IMG_0003.MOV", mtime=written)
        self.assertEqual(info["taken_source"], "mtime")
        # A file written as (or after) the recording ended: the creation time counts.
        for later in (0, 60, 3 * 86400):
            written = videos.time.mktime(when.timetuple()) + later
            info = videos.parse_probe(data, "IMG_0003.MOV", mtime=written)
            self.assertEqual(info["taken_source"], "video", later)

    def test_tagged_dates(self):
        parse = videos.parse_tagged_date
        when, offset = parse("2014-03-22T10:15:37-0800")
        self.assertEqual((imageinfo.format_datetime(when), offset),
                         ("2014-03-22T10:15:37", "-08:00"))
        when, offset = parse("2014-03-22 10:15:37.5+05:30")
        self.assertEqual((imageinfo.format_datetime(when), offset),
                         ("2014-03-22T10:15:37", "+05:30"))
        when, offset = parse("2014-03-22T10:15")
        self.assertEqual((imageinfo.format_datetime(when), offset), ("2014-03-22T10:15:00", ""))
        for bad in ("", "yesterday", "2012-13-45T10:00:00", "2312-01-01T00:00:00", None, 5):
            self.assertEqual(parse(bad), (None, ""), bad)

    def test_iso6709_positions(self):
        parse = videos.parse_iso6709
        self.assertEqual(parse("+38.1234-097.5678+150.250/"), (38.1234, -97.5678))
        self.assertEqual(parse("-33.8688+151.2093/"), (-33.8688, 151.2093))
        lat, lon = parse("+3807.404-09734.068/")            # degrees and minutes
        self.assertAlmostEqual(lat, 38 + 7.404 / 60)
        self.assertAlmostEqual(lon, -(97 + 34.068 / 60))
        lat, lon = parse("+402638.5-0795948.9/")            # degrees, minutes, seconds
        self.assertAlmostEqual(lat, 40 + 26 / 60.0 + 38.5 / 3600)
        self.assertAlmostEqual(lon, -(79 + 59 / 60.0 + 48.9 / 3600))
        self.assertEqual(parse("+12.3456-045.6789+005.000CRSWGS_84/"), (12.3456, -45.6789))
        for bad in ("", "nowhere", "+95.0-10.0/", "+10.0-190.0/", "+00.0000+000.0000/",
                    "+123456789+1/", None):
            self.assertEqual(parse(bad), (None, None), bad)
        data = fake_ffmpeg.probe_json(format_tags={"location": "+45.4321-012.3456/"})
        info = videos.parse_probe(data, "a.mp4")
        self.assertEqual((info["lat"], info["lon"]), (45.4321, -12.3456))

    def test_containers(self):
        def container(format_name, brand, name):
            data = fake_ffmpeg.probe_json(brand=brand)
            data["format"]["format_name"] = format_name
            return videos.parse_probe(data, name)["video"]["container"]
        mov = "mov,mp4,m4a,3gp,3g2,mj2"
        self.assertEqual(container(mov, "qt  ", "a.MOV"), "mov")
        self.assertEqual(container(mov, "isom", "a.mp4"), "mp4")
        self.assertEqual(container(mov, "mp42", "a.mp4"), "mp4")
        self.assertEqual(container(mov, "3gp4", "a.3gp"), "3gp")
        self.assertEqual(container(mov, "M4V ", "a.m4v"), "m4v")
        self.assertEqual(container("matroska,webm", "", "a.webm"), "webm")
        self.assertEqual(container("matroska,webm", "", "a.mkv"), "mkv")
        self.assertEqual(container("avi", "", "a.avi"), "avi")
        self.assertEqual(container("asf", "", "a.wmv"), "wmv")
        self.assertEqual(container("mpegts", "", "a.mts"), "mts")

    def test_what_is_not_a_video(self):
        data = fake_ffmpeg.probe_json()
        data["streams"] = [stream for stream in data["streams"]
                           if stream["codec_type"] != "video"]
        with self.assertRaises(videos.VideoReadError):
            videos.parse_probe(data, "song.mp4")
        # A cover picture is not the video.
        cover = fake_ffmpeg.probe_json(codec="mjpeg")
        cover["streams"][0]["disposition"] = {"attached_pic": 1}
        cover["streams"] = [cover["streams"][1], cover["streams"][0]]
        with self.assertRaises(videos.VideoReadError):
            videos.parse_probe(cover, "song.m4a")
        broken = fake_ffmpeg.probe_json(width=0)
        with self.assertRaises(videos.VideoReadError):
            videos.parse_probe(broken, "broken.mp4")
        with self.assertRaises(videos.VideoReadError):
            videos.parse_probe([], "x.mp4")
        # Unknown lengths are None, not errors.
        info = videos.parse_probe(fake_ffmpeg.probe_json(duration=None), "live.mp4")
        self.assertIsNone(info["duration"])

    def test_apple_videos_that_name_no_maker(self):
        tags = {"com.apple.quicktime.model": "iPad Pro"}
        info = videos.parse_probe(fake_ffmpeg.probe_json(format_tags=tags), "clip.MOV")
        # (As its photos say, so the camera is listed once.)
        self.assertEqual(info["camera"], "Apple iPad Pro")
        tags = {"com.apple.quicktime.model": "Model X"}
        info = videos.parse_probe(fake_ffmpeg.probe_json(format_tags=tags), "a.mov")
        self.assertEqual(info["camera"], "Model X")

    def test_hdr_colours_a_file_leaves_out(self):
        untagged = videos.parse_probe(videotools.portrait_probe(), "a.mov")
        self.assertEqual(untagged["hdr_input"], "pin=bt2020:p=bt2020:min=bt2020nc")
        self.assertIn("zscale=t=linear:npl=100:pin=bt2020:p=bt2020:min=bt2020nc,",
                      videos.frame_filters(untagged, 640))
        tagged = videos.parse_probe(videotools.portrait_probe(primaries="bt2020",
                                                              matrix="bt2020nc"), "b.mov")
        self.assertEqual(tagged["hdr_input"], "")
        self.assertIn(videos.HDR_FILTER, videos.frame_filters(tagged, 640))
        no_matrix = videos.parse_probe(videotools.portrait_probe(primaries="bt2020",
                                                                 matrix="unknown"), "c.mov")
        self.assertEqual(no_matrix["hdr_input"], "min=bt2020nc")
        self.assertNotIn("zscale", videos.frame_filters(tagged, 640, tone_map=False))
        self.assertEqual(videos.parse_probe(videotools.clip_probe(), "d.mov")["hdr_input"], "")

    def test_the_part_with_pictures(self):
        # The picture ends before the sound: frames come from the picture's part.
        data = fake_ffmpeg.probe_json(duration=2.345678)
        data["streams"][0]["duration"] = "2.2"
        info = videos.parse_probe(data, "short.mov")
        self.assertEqual((info["duration"], info["span"]), (2.345678, 2.2))
        self.assertEqual(videos.parse_probe(fake_ffmpeg.probe_json(), "a.mov")["span"], 24.0)

    def test_error_lines_leave_the_path_out(self):
        path = os.path.join("C:" + os.sep, "a very", "long", "path", "clip.mov")
        text = "file:%s: Invalid data found when processing input\n" % path
        self.assertEqual(videos._error_line(text, path), "Invalid data found when processing input")
        self.assertEqual(videos._error_line("Error opening %s\n" % path, path),
                         "Error opening clip.mov")
        self.assertEqual(videos._error_line("", path), "no output")

    def test_what_browsers_play(self):
        def facts(codec, container, pix_fmt="yuv420p"):
            return {"codec": codec, "container": container, "pix_fmt": pix_fmt}
        self.assertEqual(videos.playable(facts("h264", "mov")), "yes")
        self.assertEqual(videos.playable(facts("h264", "mp4", "yuvj420p")), "yes")
        self.assertEqual(videos.playable(facts("h264", "mp4", "yuv420p10le")), "no")
        self.assertEqual(videos.playable(facts("h264", "avi")), "no")
        self.assertEqual(videos.playable(facts("hevc", "mov")), "maybe")
        self.assertEqual(videos.playable(facts("vp9", "webm")), "yes")
        self.assertEqual(videos.playable(facts("mpeg4", "avi")), "no")
        self.assertEqual(videos.playable(facts("wmv3", "wmv")), "no")
        self.assertEqual(videos.playable(None), "no")
        self.assertEqual(videos.mime_type("a.MOV"), "video/mp4")
        self.assertEqual(videos.mime_type("a.webm"), "video/webm")
        self.assertEqual(videos.mime_type("a.xyz"), "application/octet-stream")

    def test_words(self):
        # As players show a length: whole seconds, rounded down; at least 0:01.
        self.assertEqual(videos.duration_text(24.4), "0:24")
        self.assertEqual(videos.duration_text(22.97), "0:22")
        self.assertEqual(videos.duration_text(0.07), "0:01")
        self.assertEqual(videos.duration_text(0), "0:00")
        self.assertEqual(videos.duration_text(3723), "1:02:03")
        self.assertEqual(videos.duration_text(None), "")
        self.assertIn("4 frames, in order", prompts.video_note(4, 24.2))
        self.assertIn("a video of 24 seconds", prompts.video_note(4, 24.2))
        self.assertIn("a frame from a video", prompts.video_note(1, None))
        text = prompts.user_text({"context_notes": "Our farm."}, video=(4, 12))
        self.assertTrue(text.startswith(prompts.USER_TEXT), text)
        self.assertIn("4 frames", text)
        self.assertIn("Our farm.", text)
        self.assertNotIn("frames", prompts.user_text({}))

    def test_settings(self):
        self.assertEqual(videos.frame_count({}), 4)
        self.assertEqual(videos.frame_count({"video_frames": 6}), 6)
        self.assertEqual(videos.frame_count({"video_frames": 50}), videos.MAX_FRAMES)
        self.assertEqual(videos.frame_count({"video_frames": "six"}), 4)
        self.assertEqual(videos.proxy_settings({}),
                         {"enabled": False, "height": 720, "crf": 26})
        self.assertEqual(videos.proxy_settings({"video_proxies": {
            "enabled": True, "height": 99999, "crf": 1}}),
            {"enabled": True, "height": 2160, "crf": 14})
        self.assertEqual(videos.proxy_settings({"video_proxies": {"enabled": "yes"}})["enabled"],
                         False)
        self.assertEqual(videos.sheet_edge({}, {}), common.DEFAULT_MAX_IMAGE_EDGE)
        config = {"endpoints": [{"name": "local", "kind": "openai", "model": "m",
                                 "base_url": "http://127.0.0.1:1/v1", "max_image_edge": 1024}],
                  "default_endpoint": "local"}
        self.assertEqual(videos.sheet_edge(config, {}), 1024)


class QuickHashTest(support.TempBaseMixin, unittest.TestCase):

    def write(self, name, data):
        return self.base.write_file(name, data)

    def test_small_files_are_hashed_whole(self):
        path = self.write("a.mp4", b"abc" * 1000)
        digest, size = videos.quick_hash(path)
        self.assertEqual(size, 3000)
        self.assertEqual(digest, hashlib.sha1(b"video:3000\n" + b"abc" * 1000).hexdigest())

    def test_large_files_by_their_ends(self):
        with mock.patch.object(videos, "QUICK_HASH_BYTES", 100):
            data = bytes(range(256)) * 4            # 1024 bytes > 2 x 100
            first = videos.quick_hash(self.write("a.mp4", data))
            self.assertEqual(first, (hashlib.sha1(b"video:1024\n" + data[:100] + data[-100:])
                                     .hexdigest(), 1024))
            # A copy has the same hash; a change at either end gives another.
            self.assertEqual(videos.quick_hash(self.write("copy.mp4", data)), first)
            changed = data[:-1] + b"\x00"
            self.assertNotEqual(videos.quick_hash(self.write("b.mp4", changed))[0], first[0])
            # (A change in the middle only, without a change of size, goes
            # unnoticed: documented.)
            middle = data[:500] + b"\x00" + data[501:]
            self.assertEqual(videos.quick_hash(self.write("c.mp4", middle))[0], first[0])

    def test_unreadable_files(self):
        with self.assertRaises(images.SourceReadError):
            videos.quick_hash(self.base.join("not-there.mp4"))


class LocateTest(support.TempBaseMixin, unittest.TestCase):

    def program(self, folder, name):
        name = name + (".exe" if os.name == "nt" else "")
        path = self.base.write_file(folder + "/" + name, b"#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def test_a_program_and_ffprobe_next_to_it(self):
        ffmpeg = self.program("tools", "ffmpeg")
        ffprobe = self.program("tools", "ffprobe")
        found = videos.locate(ffmpeg)
        self.assertEqual(found, (ffmpeg, ffprobe, ""))
        # The folder works too, and a relative path is relative to config.json.
        self.assertEqual(videos.locate(os.path.dirname(ffmpeg)), (ffmpeg, ffprobe, ""))
        self.assertEqual(videos.locate("tools")[:2], (ffmpeg, ffprobe))

    def test_names_with_a_version(self):
        ffmpeg = self.program("v7", "ffmpeg-7")
        ffprobe = self.program("v7", "ffprobe-7")
        self.assertEqual(videos.locate(ffmpeg)[:2], (ffmpeg, ffprobe))

    def test_what_is_missing_is_said(self):
        ffmpeg, ffprobe, problem = videos.locate(self.base.join("nothing", "ffmpeg"))
        self.assertEqual((ffmpeg, ffprobe), (None, None))
        self.assertIn("was not found", problem)
        alone = self.program("alone", "ffmpeg")
        with mock.patch.object(shutil, "which", return_value=None):
            ffmpeg, ffprobe, problem = videos.locate(alone)
            self.assertEqual((ffmpeg, ffprobe), (alone, None))
            self.assertIn("ffprobe was not found", problem)
            self.assertEqual(videos.locate("")[:2], (None, None))
            self.assertEqual(videos.locate("")[2], videos.TOOLS_MISSING)
            status = videos.tools_status({"ffmpeg": ""})
            self.assertEqual(status, {"available": False, "ffmpeg": "", "ffprobe": "",
                                      "problem": videos.TOOLS_MISSING})

    def test_the_path_and_the_cache(self):
        folder = os.path.dirname(self.program("bin", "ffmpeg"))
        self.program("bin", "ffprobe")
        videos._tools_cache.clear()
        self.addCleanup(videos._tools_cache.clear)
        with mock.patch.dict(os.environ, {"PATH": folder}):
            tools = videos.find_tools({"ffmpeg": ""})
            self.assertIsNotNone(tools)
            self.assertEqual(os.path.dirname(tools.ffmpeg[0]).lower(), folder.lower())
            self.assertIs(videos.find_tools({"ffmpeg": ""}), tools)     # remembered
        self.assertIsNone(videos.find_tools({"ffmpeg": self.base.join("nope.exe")}))


class SheetTest(unittest.TestCase):

    def test_layouts(self):
        # Four frames of a landscape video: 2 x 2, as wide as the edge.
        columns, rows, cell_w, cell_h, gap, width, height = videos.sheet_layout(
            1920, 1080, 4, 1536)
        self.assertEqual((columns, rows, gap), (2, 2, 12))
        self.assertEqual((cell_w, cell_h), (762, 429))
        self.assertEqual((width, height), (1536, 870))
        # An upright video: as tall as the edge.
        layout = videos.sheet_layout(1080, 1920, 4, 1536)
        self.assertEqual(layout[6], 1536)
        self.assertLessEqual(layout[5], 1536)
        self.assertEqual(videos.sheet_layout(1920, 1080, 1, 1000)[:5], (1, 1, 1000, 562, 0))
        self.assertEqual(videos.sheet_layout(1920, 1080, 6, 1536)[:2], (3, 2))
        self.assertEqual(videos.sheet_layout(1920, 1080, 9, 1536)[:2], (3, 3))

    def test_times(self):
        self.assertEqual(videos.sheet_times(24.0, 4), [3.0, 9.0, 15.0, 21.0])
        self.assertEqual(videos.sheet_times(None, 4), [])
        self.assertEqual(videos.poster_times(24.0), [1.0, 6.0, 12.0])
        self.assertAlmostEqual(videos.poster_times(0.4)[0], 0.04)
        self.assertEqual(videos.poster_times(None), [0.0])

    def test_sheet_picture(self):
        frames = [Image.new("RGB", (800, 450), (index * 60, 0, 0)) for index in range(4)]
        data = videos.contact_sheet(frames, 1920, 1080, 1536)
        with Image.open(io.BytesIO(data)) as sheet:
            self.assertEqual(sheet.format, "JPEG")
            self.assertEqual(sheet.size, (1536, 870))
            # Frames in order, left to right and top to bottom.
            self.assertLess(sheet.getpixel((100, 100))[0], sheet.getpixel((1000, 100))[0])
            self.assertLess(sheet.getpixel((1000, 100))[0], sheet.getpixel((100, 600))[0])


class ReadVideoTest(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.library_folder("videos")

    def test_everything_the_index_needs(self):
        path = self.make_video(self.folder, "IMG_0001.MOV", videotools.clip_probe())
        result = videos.read_video(path, self.tools, frames=4, sheet_edge=1024)
        self.assertEqual((result["width"], result["height"]), (1920, 1080))
        self.assertEqual((result["media"], result["duration"]), ("video", 24.0))
        self.assertEqual(result["taken_at"], "2021-07-04T11:30:15")
        self.assertEqual(result["camera"], "Acme Phone 9")
        self.assertEqual(result["exif"]["video"]["codec"], "h264")
        self.assertEqual(result["content_hash"], videos.quick_hash(path)[0])
        self.assertRegex(result["dhash"], r"^[0-9a-f]{16}$")
        self.assertEqual((result["projection"], result["pano"]), ("", None))
        self.assertEqual(result["mime"], "video/mp4")
        for key, size in (("thumb", (320, 180)), ("preview", (1920, 1080)),
                          ("sheet", (1024, 580))):
            with Image.open(io.BytesIO(result[key])) as picture:
                self.assertEqual((picture.format, picture.size), ("JPEG", size), key)
        # A probe, the poster, then the four frames of the sheet; never
        # hardware decoding.
        frames = [argv for argv in self.commands if argv[2] == "ffmpeg"]
        self.assertEqual(len(frames), 5)
        self.assertEqual([argv[argv.index("-ss") + 1] for argv in frames],
                         ["1.000", "3.000", "9.000", "15.000", "21.000"])
        for argv in self.commands:
            self.assertNotIn("-hwaccel", argv)
            self.assertTrue(argv[-1] == "pipe:1" or argv[-1].startswith("file:"), argv)
            self.assertIn("file:" + path, argv)
        # Without previews: no preview, no sheet (made when describing).
        result = videos.read_video(path, self.tools, want_preview=False)
        self.assertIsNone(result["preview"])
        self.assertIsNone(result["sheet"])
        self.assertIsNotNone(result["thumb"])

    def test_a_black_first_frame_is_passed_over(self):
        path = self.make_video(self.folder, "dark.mp4", videotools.clip_probe(), black_until=5)
        result = videos.read_video(path, self.tools)
        posters = [argv[argv.index("-ss") + 1] for argv in self.commands
                   if argv[2] == "ffmpeg" and "-ss" in argv][:2]
        self.assertEqual(posters, ["1.000", "6.000"])
        with Image.open(io.BytesIO(result["preview"])) as poster:
            self.assertGreater(sum(poster.convert("L").getpixel((10, 10)) for _ in (0,)), 20)

    def test_upright_hdr_video_without_zscale(self):
        path = self.make_video(self.folder, "up.MOV", videotools.portrait_probe(),
                               no_zscale=True)
        result = videos.read_video(path, self.tools, sheet_edge=1536)
        self.assertEqual((result["width"], result["height"]), (1080, 1920))
        with Image.open(io.BytesIO(result["sheet"])) as sheet:
            self.assertEqual(sheet.size[1], 1536)
            self.assertLess(sheet.size[0], sheet.size[1])
        # Tone mapping was tried once, then left out.
        self.assertFalse(self.tools.hdr_filter)
        tone_mapped = [argv for argv in self.commands if "zscale" in " ".join(argv)]
        self.assertEqual(len(tone_mapped), 1)

    def test_what_cannot_be_read(self):
        bad = self.base.write_file("libraries/videos/bad.mp4", b"not a video at all")
        with self.assertRaises(videos.VideoReadError) as caught:
            videos.read_video(bad, self.tools)
        self.assertIn("Invalid data", str(caught.exception))
        with self.assertRaises(images.SourceReadError):
            videos.read_video(self.base.join("missing.mp4"), self.tools)
        with self.assertRaises(videos.ToolsMissing):
            videos.read_video(bad, None)
        frameless = self.make_video(self.folder, "frameless.mp4", videotools.clip_probe(),
                                    fail_frames=True)
        with self.assertRaises(videos.VideoReadError):
            videos.read_video(frameless, self.tools)

    def test_a_file_that_goes_away_while_it_is_read_is_no_bad_file(self):
        path = self.make_video(self.folder, "away.mov", videotools.clip_probe())
        fake_run = videos._run

        def share_drops(argv, timeout):
            if argv[2] == "ffprobe":
                # (The share is gone while ffprobe reads, and back after.)
                os.rename(path, path + ".away")
                try:
                    return fake_run(argv, timeout)
                finally:
                    os.rename(path + ".away", path)
            return fake_run(argv, timeout)
        with mock.patch.object(videos, "_run", share_drops):
            with self.assertRaises(images.SourceReadError) as caught:
                videos.read_video(path, self.tools)
        self.assertIn("No such file", str(caught.exception))
        self.assertNotIn(self.folder, str(caught.exception))
        self.assertEqual(videos.read_video(path, self.tools)["duration"], 24.0)

    def test_programs_that_take_too_long(self):
        path = self.make_video(self.folder, "slow.mov", videotools.clip_probe())

        def slow(argv, timeout):
            raise videos.VideoTimeout("ffprobe took longer than %d seconds" % timeout)
        with mock.patch.object(videos, "_run", slow):
            with self.assertRaises(images.SourceReadError):
                videos.read_video(path, self.tools)

    def test_a_bad_file_is_tried_twice_then_fails(self):
        bad = self.base.write_file("libraries/videos/bad.mp4", b"not a video at all")
        with self.assertRaises(videos.VideoReadError) as caught:
            videos.read_video(bad, self.tools)
        self.assertNotIsInstance(caught.exception, images.SourceReadError)
        self.assertEqual(len([argv for argv in self.commands if argv[2] == "ffprobe"]), 2)

    def test_hdr_video_whose_colours_zscale_cannot_convert(self):
        path = self.make_video(self.folder, "odd.MOV", videotools.portrait_probe(),
                               zscale_fails=True)
        result = videos.read_video(path, self.tools, frames=4)
        self.assertEqual(result["exif"]["video"]["sheet_frames"], 4)
        # Each frame without tone mapping, and the next video still gets it.
        self.assertTrue(self.tools.hdr_filter)
        tried = [argv for argv in self.commands if argv[2] == "ffmpeg"]
        self.assertEqual(len([argv for argv in tried if "zscale" in " ".join(argv)]),
                         len(tried) // 2)
        del self.commands[:]
        other = self.make_video(self.folder, "fine.MOV", videotools.portrait_probe(
            primaries="bt2020", matrix="bt2020nc"))
        videos.read_video(other, self.tools)
        self.assertTrue(all("zscale" in " ".join(argv) for argv in self.commands
                            if argv[2] == "ffmpeg"))

    def test_the_sheet_says_how_many_frames_it_holds(self):
        # The picture ends at 12 s of 24: the frame at 15 s is looked for at
        # 12 s instead; the one at 21 s (and 18 s) is not there.
        path = self.make_video(self.folder, "short.mov", videotools.clip_probe(),
                               frames_until=12)
        result = videos.read_video(path, self.tools, frames=4, sheet_edge=1024)
        facts = result["exif"]["video"]
        self.assertEqual((facts["sheet_frames"], facts["sheet_asked"]), (3, 4))
        times = [argv[argv.index("-ss") + 1] for argv in self.commands
                 if argv[2] == "ffmpeg" and "-ss" in argv][1:]
        self.assertEqual(times, ["3.000", "9.000", "15.000", "12.000", "21.000", "18.000"])
        self.assertEqual(videos.sheet_frames(json.dumps(result["exif"])), (3, 4))
        self.assertEqual(videos.sheet_frames(""), (None, None))
        # Without a sheet, nothing is said about one.
        result = videos.read_video(path, self.tools, want_preview=False)
        self.assertNotIn("sheet_frames", result["exif"]["video"])
        # One made now says it too.
        data, count = videos.sheet_jpeg(self.tools, path, edge=800, frames=4)
        self.assertEqual(count, 3)
        with Image.open(io.BytesIO(data)) as sheet:
            self.assertEqual(sheet.size[0], 800)

    def test_the_command_line_reads_dates_as_a_scan_does(self):
        created = "2024-02-10T15:20:00Z"
        path = self.make_video(self.folder, "IMG_0003.MOV", fake_ffmpeg.probe_json(
            format_tags={"creation_time": created}, stream_tags={"creation_time": created}))
        written = videos.time.mktime(videos.parse_tagged_date(created)[0].timetuple()) - 3600
        os.utime(path, (written, written))
        status = {"available": True, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "problem": ""}
        out = io.StringIO()
        with mock.patch.object(videos, "tools_status", return_value=status), \
                mock.patch.object(videos, "Tools", return_value=self.tools), \
                mock.patch("sys.stdout", out):
            self.assertEqual(videos.main([path]), 0)
        text = out.getvalue()
        printed = json.loads(text[text.index("{"):])
        self.assertEqual((printed["taken_at"], printed["taken_source"]),
                         (imageinfo.local_iso(written), "mtime"))
        self.assertEqual(printed["taken_source"],
                         videos.read_video(path, self.tools)["taken_source"])
        self.assertEqual((printed["length"], printed["playable"]), ("0:24", "yes"))

    def test_unknown_length_gives_a_one_frame_sheet(self):
        path = self.make_video(self.folder, "live.mp4", fake_ffmpeg.probe_json(duration=None))
        result = videos.read_video(path, self.tools, sheet_edge=800)
        self.assertIsNone(result["duration"])
        with Image.open(io.BytesIO(result["sheet"])) as sheet:
            self.assertEqual(sheet.size, (800, 450))

    def test_the_programs_run_as_programs(self):
        # The fake as a real program: the subprocess path, with no shell.
        self.use_fake_tools(subprocess=True)
        path = self.make_video(self.folder, "a b & c.mp4", videotools.clip_probe(duration=4))
        result = videos.read_video(path, self.tools, frames=2)
        self.assertEqual(result["duration"], 4.0)
        self.assertIsNotNone(result["sheet"])
        self.assertEqual(videos.parse_probe(videos.probe(self.tools, path))["width"], 1920)

    def test_copies_without_metadata(self):
        source = self.make_video(self.folder, "clip.MOV",
                                 videotools.clip_probe(data_stream=True))
        target = os.path.join(self.base.path, "out", "x.mp4")
        videos.strip_copy(self.tools, source, target)
        argv = self.commands[-1]
        for option in (["-map_metadata", "-1"], ["-map_metadata:s", "-1"],
                       ["-map_chapters", "-1"], ["-c", "copy"], ["-map", "0:v:0"], ["-dn"],
                       ["-sn"], ["-f", "mp4"]):
            self.assertTrue(any(argv[index:index + len(option)] == option
                                for index in range(len(argv))), option)
        self.assertNotIn("-hwaccel", argv)
        # Written to a temporary name, then moved into place.
        self.assertRegex(argv[-1], r"\.tmp-\d+-\d+$")
        self.assertEqual(os.listdir(os.path.dirname(target)), ["x.mp4"])
        copied = videos.probe(self.tools, target)
        self.assertEqual(copied["format"]["tags"], {"major_brand": "isom"})
        self.assertEqual([stream["codec_type"] for stream in copied["streams"]],
                         ["video", "audio"])
        info = videos.parse_probe(copied, "x.mp4")
        self.assertEqual((info["lat"], info["camera"], info["taken_source"]), (None, "", ""))

    def test_playable_copies(self):
        source = self.make_video(self.folder, "up.MOV", videotools.portrait_probe())
        target = videos.proxy_path("lib_00000001", 7, "abcdef0123456789")
        self.assertTrue(target.endswith(os.path.join("proxies", "lib_00000001",
                                                     "7-abcdef012345.mp4")))
        videos.make_proxy(self.tools, source, target, height=480, crf=30)
        argv = self.commands[-1]
        self.assertIn("libx264", argv)
        self.assertEqual(argv[argv.index("-crf") + 1], "30")
        self.assertIn("min(480", argv[argv.index("-vf") + 1])
        self.assertNotIn("-hwaccel", argv)
        info = videos.parse_probe(videos.probe(self.tools, target), "p.mp4")
        self.assertEqual(videos.playable(info["video"]), "yes")
        self.assertEqual(videos.existing_proxy("lib_00000001", 7, "abcdef0123456789"), target)
        self.assertIsNone(videos.existing_proxy("lib_00000001", 7, "0000"))
        self.assertIsNone(videos.existing_proxy("../evil", 7, "abcdef0123456789"))
        self.assertEqual(videos.proxy_usage()["lib_00000001"]["count"], 1)
        # A moved video's row gets its copy; so does a re-attached library.
        self.assertEqual(videos.move_proxy("lib_00000001", 7, 12), 1)
        moved = videos.existing_proxy("lib_00000001", 12, "abcdef0123456789")
        self.assertTrue(moved and moved.endswith("12-abcdef012345.mp4"))
        self.assertEqual(videos.move_library_proxies("lib_00000001", "lib_00000002"), 1)
        self.assertFalse(os.path.exists(os.path.dirname(moved)))
        self.assertTrue(videos.existing_proxy("lib_00000002", 12, "abcdef0123456789"))
        self.assertEqual(videos.move_library_proxies("../evil", "lib_00000002"), 0)
        self.assertEqual(videos.delete_proxies("lib_00000002", [12]), 1)
        self.assertIsNone(videos.existing_proxy("lib_00000002", 12, "abcdef0123456789"))
        self.assertTrue(videos.delete_library_proxies("lib_00000002"))
        self.assertNotIn("lib_00000002", videos.proxy_usage())
        self.assertFalse(videos.delete_library_proxies("../evil"))

    def test_playable_copies_when_tone_mapping_fails(self):
        target = videos.proxy_path("lib_00000001", 8, "0123456789abcdef")
        odd = self.make_video(self.folder, "odd.MOV", videotools.portrait_probe(),
                              zscale_fails=True)
        videos.make_proxy(self.tools, odd, target)
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(self.tools.hdr_filter)      # only this copy went without it
        self.assertNotIn("zscale", " ".join(self.commands[-1]))
        missing = self.make_video(self.folder, "old.MOV", videotools.portrait_probe(),
                                  no_zscale=True)
        videos.make_proxy(self.tools, missing, target)
        self.assertFalse(self.tools.hdr_filter)     # this ffmpeg has no zscale


def _real_tools():
    """Tools for a real ffmpeg (SEARCH_IMAGES_TEST_FFMPEG, else the PATH), or None."""
    setting = os.environ.get("SEARCH_IMAGES_TEST_FFMPEG", "")
    ffmpeg, ffprobe, _problem = videos.locate(setting)
    return videos.Tools([ffmpeg], [ffprobe]) if ffmpeg and ffprobe else None


@unittest.skipIf(_real_tools() is None, "ffmpeg and ffprobe are not installed")
class RealFfmpegTest(support.TempBaseMixin, unittest.TestCase):
    """With the real programs (CPU only): a made-up clip with a position and a date."""

    def setUp(self):
        super().setUp()
        self.tools = _real_tools()
        self.clip = self.base.join("clip.mov")
        argv = self.tools.ffmpeg + [
            "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "testsrc=size=320x240:rate=10:duration=3", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=3", "-c:v", "mpeg4", "-c:a", "aac",
            "-metadata", "location=+33.4484-112.0740/",
            "-metadata", "creation_time=2021-07-04T18:30:15Z",
            "-metadata:s:v:0", "creation_time=2021-07-04T18:30:15Z", "-display_rotation:v:0",
            "90", "-f", "mov", self.clip]
        made = subprocess.run(argv, capture_output=True, timeout=120)
        if made.returncode != 0:
            # (-display_rotation is an input option in newer versions.)
            argv.remove("-display_rotation:v:0")
            argv.remove("90")
            made = subprocess.run(argv, capture_output=True, timeout=120)
        if made.returncode != 0:
            self.skipTest("this ffmpeg cannot make the test clip: %s" % made.stderr[-200:])

    def test_reading_a_real_video(self):
        result = videos.read_video(self.clip, self.tools, sheet_edge=640)
        self.assertEqual(result["duration"], 3.0)
        self.assertIn((result["width"], result["height"]), ((320, 240), (240, 320)))
        self.assertEqual(result["exif"]["video"]["codec"], "mpeg4")
        self.assertEqual(videos.playable(result["exif"]["video"]), "no")
        with Image.open(io.BytesIO(result["sheet"])) as sheet:
            self.assertIn(max(sheet.size), range(636, 641))

    def test_hdr_video_that_names_only_its_transfer(self):
        # HLG without its primaries and matrix: zscale converts it with the
        # BT.2020 ones assumed, and the next HDR video is tone mapped too.
        clip = self.base.join("hlg.mp4")
        made = subprocess.run(self.tools.ffmpeg + [
            "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "testsrc=size=320x240:rate=10:duration=1", "-c:v", "libx265",
            "-x265-params", "log-level=error", "-pix_fmt", "yuv420p10le", "-bsf:v",
            "hevc_metadata=transfer_characteristics=18:colour_primaries=2:"
            "matrix_coefficients=2", "-f", "mp4", clip], capture_output=True, timeout=120)
        if made.returncode != 0:
            self.skipTest("this ffmpeg cannot make an HEVC clip")
        info = videos.parse_probe(videos.probe(self.tools, clip), "hlg.mp4")
        self.assertEqual(info["video"].get("hdr"), "hlg")
        self.assertIn("pin=bt2020", info["hdr_input"])
        frame = videos.grab_frame(self.tools, clip, 0.5, info, 320)
        self.assertEqual(frame.size, (320, 240))
        if self.tools.hdr_filter:
            # (This ffmpeg has zscale: the frame was tone mapped.)
            filters = videos.frame_filters(info, 320)
            argv = self.tools.ffmpeg + ["-nostdin", "-v", "error", "-i", clip, "-frames:v",
                                        "1", "-vf", filters, "-f", "null", "-"]
            self.assertEqual(subprocess.run(argv, capture_output=True,
                                            timeout=120).returncode, 0)

    def test_a_copy_keeps_no_place_or_date(self):
        info = videos.parse_probe(videos.probe(self.tools, self.clip), "clip.mov")
        self.assertIsNotNone(info["lat"])
        target = self.base.join("out.mp4")
        videos.strip_copy(self.tools, self.clip, target)
        data = videos.probe(self.tools, target)
        tags = {}
        tags.update(data["format"].get("tags") or {})
        for stream in data["streams"]:
            tags.update(stream.get("tags") or {})
        lowered = json.dumps(tags).lower()
        for word in ("location", "creation", "iso6709", "33.44", "2021"):
            self.assertNotIn(word, lowered)
        copied = videos.parse_probe(data, "out.mp4")
        self.assertEqual((copied["lat"], copied["taken_source"]), (None, ""))
        self.assertEqual(copied["video"]["rotation"], info["video"]["rotation"])
        with open(target, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"+33.4484", raw)


if __name__ == "__main__":
    unittest.main()
