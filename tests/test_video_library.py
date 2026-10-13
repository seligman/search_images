"""Videos in a library: scanning, the schema, describing, search records, the
server's routes, the Manage page's routes and the static export.

ffmpeg and ffprobe are played by tests/fake_ffmpeg.py (see videotools.py),
the AI model by tests/fake_llm.py; everything runs in a temporary data root.
"""

import datetime
import io
import json
import os
import shutil
import sqlite3
import time
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

from PIL import Image

import fake_ffmpeg
import fake_llm
import videotools

import admin
import cache
import common
import db
import describe
import export
import jobs
import library_ops
import prompts
import refine
import scan
import search_index
import serve
import videos

try:
    import flask
except ImportError:
    flask = None

SAMPLE = os.path.join(support.REPO, "sample", "images", "01-cat.jpg")
quiet = support.quiet
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}


def described(title, photo_type="photo"):
    return {"title": title, "description": "%s happens." % title, "tags": ["test"],
            "subjects": ["thing"], "photo_type": photo_type, "document_type": "none",
            "setting": "outdoor", "time_of_day": "day", "event": "none", "people": "none",
            "sensitive": "none", "private_info": False, "aesthetic": 3, "sharpness": 4,
            "text": "", "colors": ["blue"]}


class VideoLibraryCase(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):
    """A library with a photo, an H.264 clip and an upright HEVC video."""

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.folder = self.base.library_folder("Family")
        shutil.copyfile(SAMPLE, os.path.join(self.folder, "cat.jpg"))
        self.clip = self.make_video(self.folder, "trip/IMG_0001.MOV", videotools.clip_probe())
        self.upright = self.make_video(self.folder, "IMG_0002.MOV",
                                       videotools.portrait_probe())

    def configure(self, libraries=None, **extra):
        libraries = libraries if libraries is not None else [
            support.library("Family", self.folder, id="lib_family1")]
        self.base.write_config(libraries, **extra)
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        return self.config

    def scan(self, **kwargs):
        self.ctx = jobs.JobContext("test", log_limit=5000)
        summary = scan.run(self.config, self.ctx, describe_images=False, **kwargs)
        return summary["libraries"][0]

    def query(self, sql, args=()):
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def execute(self, sql, args=()):
        conn = db.connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def rows(self):
        return {row["path"]: row for row in self.query("SELECT * FROM images")}

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=5000)["log_tail"])


class ScanTest(VideoLibraryCase):

    def test_videos_are_indexed(self):
        self.configure()
        summary = self.scan()
        self.assertEqual((summary["files"], summary["added"]), (3, 3))
        rows = self.rows()
        self.assertEqual(rows["cat.jpg"]["media"], "image")
        self.assertIsNone(rows["cat.jpg"]["duration"])
        clip = rows["trip/IMG_0001.MOV"]
        self.assertEqual((clip["media"], clip["duration"]), ("video", 24.0))
        self.assertEqual((clip["width"], clip["height"]), (1920, 1080))
        self.assertEqual((clip["taken_at"], clip["taken_source"], clip["tz_offset"]),
                         ("2021-07-04T11:30:15", "video", "-07:00"))
        self.assertEqual((clip["lat"], clip["lon"], clip["camera"]),
                         (47.6062, -122.3321, "Acme Phone 9"))
        self.assertEqual(json.loads(clip["exif"])["video"]["codec"], "h264")
        self.assertEqual(clip["content_hash"], videos.quick_hash(self.clip)[0])
        self.assertEqual(clip["has_preview"], 1)
        upright = rows["IMG_0002.MOV"]
        self.assertEqual((upright["width"], upright["height"]), (1080, 1920))
        previews = cache.PreviewStore()
        for row in (clip, upright):
            with Image.open(io.BytesIO(previews.read("lib_family1", row["id"]))) as poster:
                self.assertEqual(poster.size, (row["width"], row["height"]))
            with Image.open(io.BytesIO(previews.read_sheet("lib_family1", row["id"]))) as sheet:
                self.assertEqual(max(sheet.size), common.DEFAULT_MAX_IMAGE_EDGE)
        with cache.ThumbStore() as thumbs:
            self.assertTrue({row["id"] for row in rows.values()} <= thumbs.ids())
        self.assertFalse(previews.read_sheet("lib_family1", rows["cat.jpg"]["id"]))
        # Nothing changed: nothing is read again.
        del self.commands[:]
        again = self.scan()
        self.assertEqual((again["added"], again["unchanged"]), (0, 3))
        self.assertEqual(self.commands, [])

    def log_levels(self, words):
        return [entry["level"] for entry in self.ctx.snapshot(log_tail=5000)["log_tail"]
                if words in entry["message"]]

    def test_without_ffmpeg_videos_wait_and_stay(self):
        self.use_fake_tools(available=False)
        self.configure()
        summary = self.scan()
        self.assertEqual((summary["added"], summary["videos_skipped"]), (1, 2))
        self.assertIn("2 video(s) in Family were not read", self.log_text())
        # (Not a warning on every scan: the Manage page says it once.)
        self.assertEqual(self.log_levels("were not read"), ["info"])
        self.assertEqual(list(self.rows()), ["cat.jpg"])
        # Once ffmpeg is there they are read...
        self.use_fake_tools()
        self.assertEqual(self.scan()["added"], 2)
        # ...and when it is gone again, indexed videos are kept as they are.
        self.use_fake_tools(available=False)
        os.utime(self.clip, (time.time() + 60, time.time() + 60))
        summary = self.scan()
        self.assertEqual((summary["missing"], summary["changed"], summary["videos_skipped"]),
                         (0, 0, 0))
        self.assertTrue(all(row["missing_since"] is None for row in self.rows().values()))

    def test_without_ffmpeg_known_bad_videos_are_not_counted(self):
        self.configure()
        self.base.write_file("libraries/Family/broken.mov", b"not a video at all")
        self.assertEqual(self.scan()["failed"], 1)
        self.use_fake_tools(available=False)
        self.base.write_file("libraries/Family/new.mov", b"not read yet")
        summary = self.scan()
        # Only the new one would be read with ffmpeg; the broken one is known.
        self.assertEqual((summary["videos_skipped"], summary["skipped_failed"]), (1, 1))

    def test_a_share_that_drops_while_a_video_is_read(self):
        self.configure()
        fake_run = videos._run

        def share_drops(argv, timeout):
            if argv[2] == "ffprobe" and argv[-1] == "file:" + self.upright:
                os.rename(self.upright, self.upright + ".away")
                try:
                    return fake_run(argv, timeout)
                finally:
                    os.rename(self.upright + ".away", self.upright)
            return fake_run(argv, timeout)
        with mock.patch.object(videos, "_run", share_drops):
            summary = self.scan()
        # Not a bad file: nothing is recorded, and the next scan reads it.
        self.assertEqual((summary["added"], summary["failed"], summary["read_errors"]),
                         (2, 0, 1))
        self.assertEqual(self.query("SELECT * FROM read_failures"), [])
        self.assertEqual(self.scan()["added"], 1)
        self.assertIn("IMG_0002.MOV", self.rows())

    def test_turning_videos_off_removes_them_like_ignored_files(self):
        self.configure()
        self.scan()
        library = support.library("Family", self.folder, id="lib_family1", videos=False)
        self.configure([library])
        del self.commands[:]
        summary = self.scan()
        self.assertEqual((summary["excluded"], summary["guarded"], summary["files"]),
                         (2, 0, 1))
        rows = self.rows()
        self.assertIsNone(rows["cat.jpg"]["missing_since"])
        self.assertIsNotNone(rows["IMG_0002.MOV"]["missing_since"])
        # Turned off, ffmpeg is not even asked.
        self.assertEqual(self.commands, [])
        self.assertIn("2 video(s) in Family are no longer indexed (videos are off",
                      self.log_text())
        self.assertNotIn("ignore patterns", self.log_text())

    def test_moved_and_copied_videos_keep_their_description(self):
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? WHERE path = ?",
                     (json.dumps(described("Fireworks")), prompts.PROMPT_VER,
                      "trip/IMG_0001.MOV"))
        os.makedirs(os.path.join(self.folder, "moved"))
        os.replace(self.clip, os.path.join(self.folder, "moved", "fireworks.mov"))
        shutil.copyfile(os.path.join(self.folder, "moved", "fireworks.mov"),
                        os.path.join(self.folder, "copy.mov"))
        summary = self.scan()
        # (As for photos: both new paths get the description, then the old
        # row, whose content is at another path now, goes.)
        self.assertEqual((summary["copied"], summary["removed_moved"]), (2, 1))
        rows = self.rows()
        self.assertNotIn("trip/IMG_0001.MOV", rows)
        for path in ("moved/fireworks.mov", "copy.mov"):
            self.assertEqual(json.loads(rows[path]["info"])["title"], "Fireworks", path)

    def test_a_moved_video_keeps_its_playable_copy(self):
        self.configure()
        self.scan()
        old = self.rows()["IMG_0002.MOV"]
        proxy = videos.proxy_path("lib_family1", old["id"], old["content_hash"])
        os.makedirs(os.path.dirname(proxy))
        with open(proxy, "wb") as handle:
            handle.write(b"copy")
        os.makedirs(os.path.join(self.folder, "sub"))
        os.replace(self.upright, os.path.join(self.folder, "sub", "renamed.mov"))
        summary = self.scan()
        self.assertEqual(summary["removed_moved"], 1)
        new = self.rows()["sub/renamed.mov"]
        self.assertNotEqual(new["id"], old["id"])
        self.assertTrue(videos.existing_proxy("lib_family1", new["id"], new["content_hash"]))
        self.assertFalse(os.path.exists(proxy))
        # Away for a scan (missing), then back under another name: moved.
        away = self.base.join("away.mov")
        os.replace(os.path.join(self.folder, "sub", "renamed.mov"), away)
        self.assertEqual(self.scan()["missing"], 1)
        os.replace(away, os.path.join(self.folder, "back.mov"))
        self.assertEqual(self.scan()["moved"], 1)
        back = self.rows()["back.mov"]
        self.assertTrue(videos.existing_proxy("lib_family1", back["id"],
                                              back["content_hash"]))
        self.assertEqual(videos.proxy_usage()["lib_family1"]["count"], 1)

    def test_a_changed_video_is_read_again(self):
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? WHERE path = ?",
                     (json.dumps(described("Old")), prompts.PROMPT_VER, "IMG_0002.MOV"))
        fake_ffmpeg.make_video(self.upright, videotools.portrait_probe(duration=30.0))
        summary = self.scan()
        self.assertEqual(summary["changed"], 1)
        row = self.rows()["IMG_0002.MOV"]
        self.assertEqual((row["duration"], row["described"]), (30.0, 0))

    def test_backfill_makes_a_missing_contact_sheet(self):
        self.configure()
        self.scan()
        clip = self.rows()["trip/IMG_0001.MOV"]
        previews = cache.PreviewStore()
        previews.delete_sheet("lib_family1", clip["id"])
        result = scan.backfill(db.connect(), self.config["libraries"][0],
                               jobs.JobContext("test"), video=videos.setup(self.config))
        self.assertEqual((result["needed"], result["filled"]), (1, 1))
        self.assertIsNotNone(previews.read_sheet("lib_family1", clip["id"]))
        # Without ffmpeg, videos are left alone.
        previews.delete_sheet("lib_family1", clip["id"])
        result = scan.backfill(db.connect(), self.config["libraries"][0],
                               jobs.JobContext("test"),
                               video=videos.Setup(self.config, tools=None, look=False))
        self.assertEqual(result["needed"], 0)

    def test_sheets_follow_the_frames_setting(self):
        self.configure()
        self.scan()
        clip = self.rows()["trip/IMG_0001.MOV"]
        self.assertEqual(videos.sheet_frames(clip["exif"]), (4, 4))
        self.configure(video_frames=2)
        result = scan.backfill(db.connect(), self.config["libraries"][0],
                               jobs.JobContext("test"), video=videos.setup(self.config))
        self.assertEqual((result["needed"], result["filled"]), (2, 2))
        clip = self.rows()["trip/IMG_0001.MOV"]
        self.assertEqual(videos.sheet_frames(clip["exif"]), (2, 2))
        with Image.open(io.BytesIO(cache.PreviewStore().read_sheet("lib_family1",
                                                                   clip["id"]))) as sheet:
            self.assertEqual(sheet.size[0], common.DEFAULT_MAX_IMAGE_EDGE)
            self.assertLess(sheet.size[1], sheet.size[0] / 2)      # two frames side by side
        # Done: nothing more to do.
        result = scan.backfill(db.connect(), self.config["libraries"][0],
                               jobs.JobContext("test"), video=videos.setup(self.config))
        self.assertEqual(result["needed"], 0)
        # A sheet whose frames were not recorded (an earlier version) is made again.
        exif = json.loads(clip["exif"])
        for key in ("sheet_frames", "sheet_asked"):
            del exif["video"][key]
        self.execute("UPDATE images SET exif = ? WHERE id = ?", (json.dumps(exif), clip["id"]))
        result = scan.backfill(db.connect(), self.config["libraries"][0],
                               jobs.JobContext("test"), video=videos.setup(self.config))
        self.assertEqual((result["needed"], result["filled"]), (1, 1))
        self.assertEqual(videos.sheet_frames(self.rows()["trip/IMG_0001.MOV"]["exif"]),
                         (2, 2))

    def test_a_purged_video_takes_its_files_along(self):
        self.configure()
        self.scan()
        clip = self.rows()["trip/IMG_0001.MOV"]
        proxy = videos.proxy_path("lib_family1", clip["id"], clip["content_hash"])
        os.makedirs(os.path.dirname(proxy))
        with open(proxy, "wb") as handle:
            handle.write(b"copy")
        os.remove(self.clip)
        self.scan(now=time.time())
        self.scan(now=time.time() + 40 * 86400)
        self.assertNotIn("trip/IMG_0001.MOV", self.rows())
        previews = cache.PreviewStore()
        self.assertIsNone(previews.read_sheet("lib_family1", clip["id"]))
        self.assertIsNone(previews.read("lib_family1", clip["id"]))
        self.assertFalse(os.path.exists(proxy))


class RedateTest(VideoLibraryCase):
    """Videos an older version dated by a camera's clock read as UTC get the time of
    their names on the next scan (scan.redate_videos)."""

    def probes(self, name):
        return [argv for argv in self.commands
                if len(argv) > 2 and argv[2] == "ffprobe" and argv[-1].endswith(name)]

    def test_dashcam_clips_get_their_clock_times(self):
        clock = datetime.datetime(2023, 8, 17, 14, 21, 5)
        if videos._utc_to_local(clock) == clock:
            self.skipTest("this computer keeps UTC: such dates were never off")
        self.configure()
        dashcam = "cam/20230817_142005_0412_N_A.MP4"
        self.make_video(self.folder, dashcam, fake_ffmpeg.probe_json(
            duration=60.0, codec="hevc", audio=False,
            format_tags={"creation_time": "2023-08-17T14:21:05.000000Z"},
            stream_tags={"creation_time": "2023-08-17T14:21:05.000000Z"}))
        # A phone that names its videos in UTC (its own tags say it is one):
        # read again once, and kept as it is.
        phone = "cam/VID_20230817_212005.mp4"
        self.make_video(self.folder, phone, fake_ffmpeg.probe_json(
            duration=20.0, format_tags={"creation_time": "2023-08-17T21:20:05.000000Z",
                                        "com.android.version": "13"}))
        # The scan of an older version: the creation times as UTC (and no
        # dates read again).
        older = mock.patch.object(videos, "local_clock_time", lambda *args: None)
        unread = mock.patch.object(scan, "redate_videos", lambda *args: None)
        with older, unread:
            self.scan()
        old = self.rows()[dashcam]
        self.assertEqual((old["taken_source"], old["tz_offset"]), ("video", ""))
        self.assertNotEqual(old["taken_at"], "2023-08-17T14:20:05")
        phone_date = self.rows()[phone]["taken_at"]
        self.assertTrue(videos.clock_candidate(phone.split("/")[1], phone_date, 20.0))
        # The next scan reads the dates of those two again (ffprobe only).
        del self.commands[:]
        summary = self.scan()
        self.assertEqual(summary["backfill"]["redated"],
                         {"checked": 2, "changed": 1, "failed": 0})
        rows = self.rows()
        self.assertEqual((rows[dashcam]["taken_at"], rows[dashcam]["taken_source"],
                          rows[dashcam]["tz_offset"]), ("2023-08-17T14:20:05", "video", ""))
        self.assertGreater(rows[dashcam]["updated"], old["updated"])
        self.assertEqual(rows[phone]["taken_at"], phone_date)
        self.assertEqual(len(self.probes("20230817_142005_0412_N_A.MP4")), 1)
        self.assertFalse(any(argv[2] == "ffmpeg" for argv in self.commands
                             if len(argv) > 2), self.commands)
        self.assertIn("now have the time of their camera's clock", self.log_text())
        # And never again: the phone's date stands, the clip's is right now.
        del self.commands[:]
        summary = self.scan()
        self.assertEqual(summary["backfill"]["redated"],
                         {"checked": 0, "changed": 0, "failed": 0})
        self.assertEqual(self.probes(".mp4") + self.probes(".MP4"), [])

    def test_without_ffmpeg_nothing_is_read_again(self):
        self.configure()
        summary = self.scan()
        self.assertEqual(summary["backfill"]["redated"]["checked"], 0)
        self.use_fake_tools(available=False)
        summary = self.scan()
        self.assertEqual(summary["backfill"]["redated"],
                         {"checked": 0, "changed": 0, "failed": 0})


class LibraryDataTest(VideoLibraryCase):
    """Deleting or re-attaching a library's data takes the playable copies along."""

    def proxies(self):
        rows = self.rows()
        paths = []
        for path in ("IMG_0002.MOV", "trip/IMG_0001.MOV"):
            row = rows[path]
            proxy = videos.proxy_path(row["library_id"], row["id"], row["content_hash"])
            common.ensure_dir(os.path.dirname(proxy))
            with open(proxy, "wb") as handle:
                handle.write(b"copy")
            paths.append(proxy)
        return paths

    def test_deleting_a_library_deletes_its_copies(self):
        self.configure()
        self.scan()
        made = self.proxies()
        self.configure([])
        conn = db.connect()
        try:
            result = library_ops.delete_library_data(conn, "lib_family1")
        finally:
            conn.close()
        self.assertEqual(result["deleted"], 3)
        self.assertFalse(any(os.path.exists(path) for path in made))
        self.assertNotIn("lib_family1", videos.proxy_usage())

    def test_reattaching_a_library_moves_its_copies(self):
        self.configure()
        self.scan()
        self.proxies()
        library = support.library("Family", self.folder, id="lib_family2")
        self.configure([library])
        conn = db.connect()
        try:
            library_ops.reattach_orphan(conn, "lib_family1", "lib_family2", self.config)
        finally:
            conn.close()
        rows = self.rows()
        for path in ("IMG_0002.MOV", "trip/IMG_0001.MOV"):
            row = rows[path]
            self.assertEqual(row["library_id"], "lib_family2")
            self.assertTrue(videos.existing_proxy("lib_family2", row["id"],
                                                  row["content_hash"]), path)
        self.assertNotIn("lib_family1", videos.proxy_usage())


class MigrationTest(support.TempBaseMixin, unittest.TestCase):

    def test_version_4_becomes_5_with_a_backup(self):
        path = common.db_path()
        common.ensure_dir(os.path.dirname(path))
        conn = sqlite3.connect(path)
        for version, func, _rewrites in db.MIGRATIONS:
            if version <= 4:
                func(conn)
        conn.execute("PRAGMA user_version=4")
        conn.execute("INSERT INTO images (library_id, path, name, size, mtime) "
                     "VALUES ('lib_00000001', 'a.jpg', 'a.jpg', 1, 1)")
        conn.commit()
        conn.close()
        lines = []
        report = db.init({"libraries": []}, log=lines.append)
        self.assertEqual((report["from_version"], report["version"]), (4, db.SCHEMA_VERSION))
        self.assertTrue(os.path.isfile(path + ".bak-v4"))
        conn = db.connect()
        try:
            row = conn.execute("SELECT media, duration FROM images").fetchone()
            self.assertEqual((row["media"], row["duration"]), ("image", None))
        finally:
            conn.close()


class DescribeVideoTest(VideoLibraryCase):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(describe, "UNREACHABLE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self):
        return {"name": "local", "kind": "openai", "base_url": self.server.base_url,
                "api_key": "", "model": "fake-model"}

    def describe(self, **kwargs):
        self.ctx = jobs.JobContext("describe", log_limit=5000)
        conn = db.connect()
        try:
            return describe.describe_pending(conn, self.config, self.ctx,
                                             sleep=lambda seconds: None, **kwargs)
        finally:
            conn.close()

    def test_the_model_sees_the_contact_sheet(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local",
                       video_frames=4)
        self.scan()
        summary = self.describe()
        self.assertEqual(summary["described"], 3)
        rows = self.rows()
        previews = cache.PreviewStore()
        by_size = {}
        for entry in self.server.describe_requests():
            with Image.open(io.BytesIO(entry["image"])) as sent:
                by_size[sent.size] = entry["prompt"]
        clip = rows["trip/IMG_0001.MOV"]
        with Image.open(io.BytesIO(previews.read_sheet("lib_family1", clip["id"]))) as sheet:
            prompt = by_size[sheet.size]
        self.assertIn("4 frames, in order", prompt)
        self.assertIn("a video of 24 seconds", prompt)
        photo_prompts = [text for text in by_size.values() if "frames" not in text]
        self.assertEqual(len(photo_prompts), 1)
        # The kind of picture is "video" in the search record, whatever the model said.
        conn = db.connect()
        try:
            records = {record["i"]: record for record in search_index.build_records(
                conn, self.config, ["lib_family1"])}
        finally:
            conn.close()
        self.assertEqual(records[clip["id"]]["ty"], "video")
        # The model's own kind of picture stays in vt (unless it said "photo").
        kind = json.loads(self.rows()["trip/IMG_0001.MOV"]["info"])["photo_type"]
        self.assertEqual(records[clip["id"]].get("vt"), None if kind == "photo" else kind)
        self.assertNotEqual(records[rows["cat.jpg"]["id"]].get("ty"), "video")
        self.assertNotIn("mv", records[rows["cat.jpg"]["id"]])
        self.assertNotIn("vt", records[rows["cat.jpg"]["id"]])

    def test_without_kept_previews_the_sheet_is_made_now(self):
        library = support.library("Family", self.folder, id="lib_family1", previews=False)
        self.configure([library], endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        self.assertFalse(any(row["has_preview"] for row in self.rows().values()))
        summary = self.describe()
        self.assertEqual(summary["described"], 3)
        sheets = [argv for argv in self.commands if argv[2] == "ffmpeg"]
        self.assertTrue(sheets)

    def video_prompts(self):
        return [entry["prompt"] for entry in self.server.describe_requests()
                if "frames" in entry["prompt"] or "a frame from" in entry["prompt"]]

    def test_the_model_is_told_the_frames_the_sheet_holds(self):
        # The picture ends half way through: three frames (of four) on the sheet.
        fake_ffmpeg.make_video(self.clip, videotools.clip_probe(), frames_until=12)
        os.remove(self.upright)
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        self.describe()
        prompt, = self.video_prompts()
        self.assertIn("This picture holds 3 frames, in order", prompt)
        self.assertNotIn("4 frames", prompt)

    def test_a_sheet_made_now_says_its_frames_too(self):
        fake_ffmpeg.make_video(self.clip, videotools.clip_probe(), frames_until=12)
        os.remove(self.upright)
        library = support.library("Family", self.folder, id="lib_family1", previews=False)
        self.configure([library], endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        self.describe()
        prompt, = self.video_prompts()
        self.assertIn("This picture holds 3 frames, in order", prompt)

    def test_without_ffmpeg_videos_are_skipped_not_failed(self):
        library = support.library("Family", self.folder, id="lib_family1", previews=False)
        self.configure([library], endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        self.use_fake_tools(available=False)
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (1, 0))
        self.assertTrue(all(row["describe_attempts"] == 0 for row in self.rows().values()))

    def test_hidden_videos_never_go_to_a_cloud_service(self):
        cloud = dict(self.endpoint(), name="cloud", preset="openai",
                     base_url="https://api.example.com/v1")
        self.configure(endpoints=[cloud], default_endpoint="cloud")
        self.scan()
        self.execute("UPDATE images SET hidden = 1 WHERE media = 'video'")
        with mock.patch.object(common, "is_local_endpoint", lambda endpoint: False):
            summary = self.describe(limit=0)
        self.assertEqual(summary["skipped_hidden"], 2)

    def test_refine_never_picks_videos(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps(described("Screen", "screenshot")), prompts.PROMPT_VER))
        library = dict(self.config["libraries"][0],
                       refine={"endpoint": "local", "photo_types": ["screenshot"]})
        conn = db.connect()
        try:
            found = refine.candidate_rows(conn, library)
        finally:
            conn.close()
        self.assertEqual([row["path"] for row in found], ["cat.jpg"])


class RecordTest(VideoLibraryCase):

    def test_search_records_and_stacks(self):
        self.configure()
        shutil.copyfile(self.clip, os.path.join(self.folder, "same.mov"))
        self.scan()
        rows = self.rows()
        conn = db.connect()
        try:
            # A photo with the same dHash as a video never stacks with it.
            conn.execute("UPDATE images SET dhash = ?, taken_at = ?, taken_source = 'exif' "
                         "WHERE path = 'cat.jpg'", (rows["IMG_0002.MOV"]["dhash"],
                                                    rows["IMG_0002.MOV"]["taken_at"]))
            conn.commit()
            records = {record["i"]: record for record in search_index.build_records(
                conn, self.config, ["lib_family1"], use_cache=False)}
            exported = search_index.build_records(conn, self.config, ["lib_family1"],
                                                  for_export=True)
        finally:
            conn.close()
        clip = records[rows["trip/IMG_0001.MOV"]["id"]]
        self.assertEqual((clip["mv"], clip["ty"], clip["dr"], clip["vc"]),
                         (1, "video", 24.0, "h264"))
        # Lengths are rounded down, as players show them (at least 0.1 s).
        for seconds, kept in ((22.97, 22.9), (0.04, 0.1)):
            record = {}
            search_index.add_video(record, {"duration": seconds, "vexif": ""})
            self.assertEqual(record["dr"], kept)
        # The model's kind of picture stays in vt (a screen recording).
        record = {"ty": "screenshot"}
        search_index.add_video(record, {"duration": 5.0, "vexif": ""})
        self.assertEqual((record["ty"], record["vt"]), ("video", "screenshot"))
        upright = records[rows["IMG_0002.MOV"]["id"]]
        self.assertEqual((upright["vc"], upright["w"], upright["hgt"]), ("hevc", 1080, 1920))
        cat = records[rows["cat.jpg"]["id"]]
        self.assertNotIn("mv", cat)
        self.assertNotIn("dg", cat)
        self.assertNotIn("dg", upright)
        # Identical files stack.
        same = records[rows["same.mov"]["id"]]
        self.assertEqual(same["dg"], clip["dg"])
        self.assertTrue(all(record.get("mv") in (None, 1) for record in exported))
        self.assertTrue(search_index.exact_copies_only({"mv": 1}))


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(VideoLibraryCase):

    def setUp(self):
        super().setUp()
        self.configure()
        self.scan()
        self.ids = {path: row["id"] for path, row in self.rows().items()}
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def test_the_video_route_sends_byte_ranges(self):
        url = "/video/%d" % self.ids["trip/IMG_0001.MOV"]
        with open(self.clip, "rb") as handle:
            data = handle.read()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "video/mp4")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(response.get_data(), data)
        etag = response.headers["ETag"]
        response.close()
        response = self.client.get(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.get_data(), data[10:20])
        self.assertEqual(response.headers["Content-Range"], "bytes 10-19/%d" % len(data))
        response.close()
        response = self.client.get(url, headers={"Range": "bytes=-5"})
        self.assertEqual((response.status_code, response.get_data()), (206, data[-5:]))
        response.close()
        response = self.client.get(url, headers={"Range": "bytes=%d-" % (len(data) + 10)})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], "bytes */%d" % len(data))
        response.close()
        response = self.client.get(url, headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 304)
        response.close()
        self.assertIn("private", self.client.get(url).headers["Cache-Control"])

    def test_who_may_see_a_video(self):
        url = "/video/%d" % self.ids["IMG_0002.MOV"]
        self.assertEqual(self.client.get(url, environ_base=REMOTE).status_code, 200)
        self.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.ids["IMG_0002.MOV"],))
        self.assertEqual(self.client.get(url, environ_base=REMOTE).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 200)              # the admin
        self.assertEqual(self.client.get("/video/%d" % self.ids["cat.jpg"]).status_code, 404)
        self.assertEqual(self.client.get("/video/999999").status_code, 404)
        os.remove(self.upright)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_playable_copy_is_sent_instead(self):
        row = self.rows()["IMG_0002.MOV"]
        detail = self.client.get("/api/image/%d" % row["id"]).get_json()
        self.assertFalse(detail["playable_copy"])
        proxy = videos.proxy_path("lib_family1", row["id"], row["content_hash"])
        os.makedirs(os.path.dirname(proxy))
        with open(proxy, "wb") as handle:
            handle.write(b"playable copy")
        response = self.client.get("/video/%d" % row["id"])
        self.assertEqual((response.mimetype, response.get_data()),
                         ("video/mp4", b"playable copy"))
        response.close()
        detail = self.client.get("/api/image/%d" % row["id"]).get_json()
        self.assertTrue(detail["playable_copy"])

    def test_the_full_record_of_a_video(self):
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? WHERE id = ?",
                     (json.dumps(described("Fireworks")), prompts.PROMPT_VER,
                      self.ids["trip/IMG_0001.MOV"]))
        detail = self.client.get("/api/image/%d" % self.ids["trip/IMG_0001.MOV"]).get_json()
        self.assertEqual((detail["media"], detail["duration"]), ("video", 24.0))
        self.assertEqual(detail["video"]["codec"], "h264")
        self.assertEqual(detail["info"]["photo_type"], "video")
        self.assertEqual(detail["taken_source"], "video")
        photo = self.client.get("/api/image/%d" % self.ids["cat.jpg"]).get_json()
        self.assertEqual(photo["media"], "image")
        self.assertNotIn("video", photo)
        payload = json.loads(self.client.get("/api/index").get_data(as_text=True))
        by_id = {record["i"]: record for record in payload["records"]}
        self.assertEqual(by_id[self.ids["IMG_0002.MOV"]]["mv"], 1)

    def test_posters_and_originals(self):
        clip_id = self.ids["trip/IMG_0001.MOV"]
        stored = self.client.get("/preview/%d" % clip_id)
        self.assertEqual(stored.mimetype, "image/jpeg")
        stored.close()
        # Without a kept preview the poster is made now.
        self.execute("UPDATE images SET has_preview = 0 WHERE id = ?", (clip_id,))
        made = self.client.get("/preview/%d" % clip_id)
        self.assertEqual(made.status_code, 200)
        with Image.open(io.BytesIO(made.get_data())) as poster:
            self.assertEqual(poster.size, (1920, 1080))
        original = self.client.get("/image/%d" % clip_id)
        with open(self.clip, "rb") as handle:
            self.assertEqual(original.get_data(), handle.read())
        self.assertEqual(original.mimetype, "video/mp4")
        # A download: a tab that cannot decode it would play only its sound.
        self.assertEqual(original.headers["Content-Disposition"],
                         "attachment; filename=IMG_0001.MOV")
        original.close()
        photo = self.client.get("/image/%d" % self.ids["cat.jpg"])
        self.assertNotIn("attachment", photo.headers.get("Content-Disposition", ""))
        photo.close()


    @unittest.skipIf(shutil.which("node") is None, "Node.js is not installed")
    def test_site_js_makes_the_records_the_server_sends(self):
        import test_ui_scripts
        self.execute("UPDATE images SET duration = 24.25 WHERE path = 'IMG_0002.MOV'")
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? "
                     "WHERE path = 'trip/IMG_0001.MOV'",
                     (json.dumps(described("Fireworks")), prompts.PROMPT_VER))
        records = self.client.get("/api/index").get_json()["records"]
        pairs = [{"record": record,
                  "detail": self.client.get("/api/image/%d" % record["i"]).get_json()}
                 for record in records if record.get("mv")]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(sorted(pair["record"]["dr"] for pair in pairs), [24.0, 24.2])
        path = self.base.join("pairs.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"pairs": pairs}, handle)
        code, output = test_ui_scripts.run_node("--compare", path)
        self.assertEqual(code, 0, "\n" + output)
        self.assertIn("2 of 2 passed", output)


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminTest(VideoLibraryCase):

    def setUp(self):
        super().setUp()
        self.configure()
        self.scan()
        self.manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(self.manager.stop, 30)
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def get(self, path):
        return self.client.get(admin.URL_PREFIX + path, headers={"X-Search-Images": "1"})

    def post(self, path, body):
        return self.client.post(admin.URL_PREFIX + path, data=json.dumps(body),
                                content_type="application/json",
                                headers={"X-Search-Images": "1"})

    def test_the_manage_page_counts_videos(self):
        with mock.patch.object(videos, "tools_status", return_value={
                "available": False, "ffmpeg": "", "ffprobe": "",
                "problem": videos.TOOLS_MISSING}):
            state = self.get("/state").get_json()
        self.assertEqual(state["videos"]["available"], False)
        self.assertEqual(state["videos"]["problem"], videos.TOOLS_MISSING)
        self.assertEqual(state["videos"]["proxies"]["enabled"], False)
        library = state["libraries"][0]
        self.assertEqual(library["video_info"]["count"], 2)
        self.assertEqual(library["video_info"]["proxies"],
                         {"count": 0, "bytes": 0, "pending": None})
        self.assertIs(library["videos"], True)

    def test_videos_can_be_turned_off_for_a_library(self):
        answer = self.post("/libraries/lib_family1", {"videos": False})
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        self.assertIs(common.load_config()["libraries"][0]["videos"], False)
        answer = self.post("/libraries/lib_family1", {"videos": "no"})
        self.assertEqual(answer.status_code, 400)

    def test_the_proxies_job(self):
        refused = self.post("/jobs", {"kind": "proxies", "library_id": "lib_family1"})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("video_proxies", refused.get_json()["error"])
        self.configure(video_proxies={"enabled": True, "height": 480})
        state = self.get("/state").get_json()
        # The HEVC video needs a copy; the H.264 one plays as it is.
        self.assertEqual(state["libraries"][0]["video_info"]["proxies"]["pending"], 1)
        answer = self.post("/jobs", {"kind": "proxies", "library_id": "lib_family1"})
        self.assertEqual(answer.status_code, 202, answer.get_data(as_text=True))
        self.assertTrue(self.manager.start())
        self.assertTrue(self.manager.wait_idle(30))
        job = self.manager.get(answer.get_json()["job"]["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["summary"]["made"], 1)
        row = self.rows()["IMG_0002.MOV"]
        self.assertIsNotNone(videos.existing_proxy("lib_family1", row["id"],
                                                   row["content_hash"]))
        state = self.get("/state").get_json()
        proxies = state["libraries"][0]["video_info"]["proxies"]
        self.assertEqual((proxies["count"], proxies["pending"]), (1, 0))


class ExportTest(VideoLibraryCase):

    def setUp(self):
        super().setUp()
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps(described("Something")), prompts.PROMPT_VER))
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def export(self, options=None):
        lines = []
        summary = export.export_library(export.LogContext(lines.append), self.conn,
                                        common.load_config(), "lib_family1", options=options)
        return summary, lines

    def records(self, out):
        records = []
        data = os.path.join(out, "data")
        for name in sorted(os.listdir(data)):
            if name.startswith("index-"):
                with open(os.path.join(data, name), encoding="ascii") as handle:
                    text = handle.read()
                records.extend(json.loads(text[text.index("(") + 1:text.rindex(")")]))
        return records

    def test_videos_go_out_as_posters_by_default(self):
        summary, _lines = self.export()
        self.assertEqual((summary["count"], summary["videos"]), (3, 0))
        out = summary["output"]
        self.assertFalse(os.path.exists(os.path.join(out, "videos")))
        records = self.records(out)
        videos_out = [record for record in records if record.get("mv")]
        self.assertEqual(len(videos_out), 2)
        for record in videos_out:
            self.assertEqual(record["ty"], "video")
            self.assertNotIn("vf", record)
            self.assertTrue(os.path.isfile(os.path.join(out, "images", record["i"] + ".jpg")))
        with open(os.path.join(out, "data", "manifest.js"), encoding="ascii") as handle:
            manifest = handle.read()
        self.assertNotIn('"videos":true', manifest)
        # (An export without videos says nothing about them.)
        self.assertNotIn('"videos":"skip"', manifest)
        self.assertNotIn("videos", export.options_summary(export.validate_options({})))
        self.assertEqual(export.options_summary(export.validate_options(
            {"videos": "include"}))["videos"], "include")

    def test_a_screen_recording_is_left_out_like_a_screenshot(self):
        self.execute("UPDATE images SET info = ? WHERE path = 'IMG_0002.MOV'",
                     (json.dumps(described("Chat", "screenshot")),))
        preview = export.preview_export(self.conn, common.load_config(), "lib_family1")
        self.assertEqual(preview["counts"]["by_reason"], {"screenshot": 1})
        flagged = {item["id"]: item for item in preview["flagged"]}
        row = self.rows()["IMG_0002.MOV"]
        self.assertEqual(flagged[row["id"]]["reasons"], ["screenshot"])
        self.assertTrue(flagged[row["id"]]["excluded"])
        # (How it would go out if published, for the review's counts.)
        self.assertEqual(flagged[row["id"]]["video"], "poster")
        # "video" leaves every video out.
        options = export.validate_options({"exclude_types": ["video", "screenshot"]})
        self.assertEqual(options["exclude_types"], ["screenshot", "video"])
        preview = export.preview_export(self.conn, common.load_config(), "lib_family1",
                                        {"exclude_types": ["video"]})
        self.assertEqual(preview["counts"]["by_reason"], {"video": 2})
        preview = export.preview_export(self.conn, common.load_config(), "lib_family1",
                                        {"exclude_types": ["video"], "videos": "include"})
        self.assertEqual(preview["counts"]["videos"],
                         {"total": 0, "included": 0, "not_playable": 0, "too_large": 0})
        flagged = {item["id"]: item for item in preview["flagged"]}
        self.assertEqual(flagged[row["id"]]["video"], "not_playable")        # HEVC
        self.assertEqual(flagged[self.rows()["trip/IMG_0001.MOV"]["id"]]["video"], "included")
        self.assertEqual(flagged[self.rows()["trip/IMG_0001.MOV"]["id"]]["parts"]["videos"],
                         self.rows()["trip/IMG_0001.MOV"]["size"])
        summary, _lines = self.export({"exclude_types": ["video"]})
        self.assertEqual(summary["count"], 1)
        with self.assertRaises(export.OptionsError):
            export.validate_options({"exclude_types": ["movie"]})

    def test_playable_videos_go_out_without_their_metadata(self):
        preview = export.preview_export(self.conn, common.load_config(), "lib_family1",
                                        {"videos": "include"})
        self.assertEqual(preview["counts"]["videos"],
                         {"total": 2, "included": 1, "not_playable": 1, "too_large": 0})
        self.assertGreater(preview["size_parts"]["videos"], 0)
        summary, _lines = self.export({"videos": "include"})
        self.assertEqual(summary["videos"], 1)
        out = summary["output"]
        files = os.listdir(os.path.join(out, "videos"))
        self.assertEqual(len(files), 1)
        records = {record["i"]: record for record in self.records(out)}
        xid = files[0][:-len(".mp4")]
        self.assertEqual(records[xid].get("vf"), 1)
        self.assertEqual(records[xid].get("vc"), "h264")
        copied = videos.probe(self.tools, os.path.join(out, "videos", files[0]))
        self.assertEqual(copied["format"]["tags"], {"major_brand": "isom"})
        self.assertTrue(all(not stream["tags"] for stream in copied["streams"]))
        with open(os.path.join(out, "data", "manifest.js"), encoding="ascii") as handle:
            self.assertIn('"videos":true', handle.read())
        # A second export copies nothing again.
        del self.commands[:]
        again, _lines = self.export({"videos": "include"})
        self.assertEqual(again["written"], 0)
        self.assertFalse([argv for argv in self.commands if "-c" in argv])
        # A playable copy of the HEVC video goes out too.
        row = self.rows()["IMG_0002.MOV"]
        videos.make_proxy(self.tools, self.upright,
                          videos.proxy_path("lib_family1", row["id"], row["content_hash"]))
        third, _lines = self.export({"videos": "include"})
        self.assertEqual(third["videos"], 2)
        # Turned off again: the videos are removed from the site.
        last, _lines = self.export({"videos": "skip"})
        self.assertEqual(last["videos"], 0)
        self.assertFalse(os.path.isdir(os.path.join(out, "videos")))

    def test_a_video_that_cannot_be_read_is_not_kept_once_turned_off(self):
        summary, _lines = self.export({"videos": "include"})
        out = summary["output"]
        self.assertEqual(len(os.listdir(os.path.join(out, "videos"))), 1)
        # The video cannot be read now (its preview is gone, the file moved
        # away): the earlier export's files are kept -- but not the video
        # itself once videos are turned off.
        row = self.rows()["trip/IMG_0001.MOV"]
        cache.PreviewStore().delete_both("lib_family1", row["id"])
        self.execute("UPDATE images SET mtime = mtime + 100 WHERE id = ?", (row["id"],))
        os.replace(self.clip, self.clip + ".away")
        again, lines = self.export({"videos": "skip"})
        self.assertTrue(any("cannot be read" in line for line in lines), lines)
        self.assertEqual(again["videos"], 0)
        self.assertFalse(os.path.isdir(os.path.join(out, "videos")))
        self.assertFalse(any(record.get("vf") for record in self.records(out)))

    def test_large_videos_keep_their_poster(self):
        with mock.patch.object(export, "_file_size", return_value=None):
            self.execute("UPDATE images SET size = ? WHERE media = 'video'", (300 * 1024 * 1024,))
            preview = export.preview_export(self.conn, common.load_config(), "lib_family1",
                                            {"videos": "include", "max_video_mb": 200})
        self.assertEqual(preview["counts"]["videos"]["too_large"], 1)
        self.assertEqual(preview["counts"]["videos"]["included"], 0)

    def test_options(self):
        self.assertEqual(export.validate_options({"videos": "include"})["videos"], "include")
        with self.assertRaises(export.OptionsError):
            export.validate_options({"videos": "all"})
        with self.assertRaises(export.OptionsError):
            export.validate_options({"max_video_mb": 0})
        self.assertEqual(export.validate_options({"max_video_mb": 50})["max_video_mb"], 50)


if __name__ == "__main__":
    unittest.main()
