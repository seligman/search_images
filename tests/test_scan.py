"""Tests for scan.py: walking, indexing, carry-over, missing files and the CLI."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

from PIL import Image, ImageDraw

import imagegen

import cache
import common
import db
import describe
import image_edits
import images
import jobs
import not_duplicates
import scan
import search_index

SAMPLES = os.path.join(support.REPO, "sample", "images")
DAY = 86400.0
INFO = {"description": "A cat on a sofa.", "subjects": ["cat"], "text": "",
        "tags": ["cat"], "colors": ["gray"], "aesthetic_score": 0.5,
        "sharpness": 0.5, "exposure_score": 0.5, "watermark_score": 1.0}


def sample_names(count=None):
    names = sorted(name for name in os.listdir(SAMPLES) if name.endswith(".jpg"))
    return names[:count] if count else names


def write_jpeg(path, seed=0, size=(64, 48)):
    """A small JPEG whose bytes (and dHash) depend on seed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image = Image.new("RGB", size, ((seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256))
    draw = ImageDraw.Draw(image)
    for index in range(4):
        left = (seed * 7 + index * 13) % (size[0] - 8)
        draw.rectangle([left, index * 10, left + 7, index * 10 + 7],
                       fill=((seed * 11 + index * 60) % 256, 255 - (seed * 5) % 256, 40))
    image.save(path, "JPEG", quality=90)
    return path


class ScanTestCase(support.TempBaseMixin, unittest.TestCase):
    """A temporary data root with one library folder, and read counting."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Family")
        self.reads = []
        real_read = images.read_image

        def counting_read(path, *args, **kwargs):
            self.reads.append(path)
            return real_read(path, *args, **kwargs)

        patcher = mock.patch.object(images, "read_image", counting_read)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, libraries=None, **extra):
        libraries = libraries if libraries is not None else [
            support.library("Family", self.folder)]
        self.base.write_config(libraries, **extra)
        self.config = common.load_config()
        db.init(self.config, log=lambda message: None)
        return self.config

    @property
    def library_id(self):
        return self.config["libraries"][0]["id"]

    def copy_samples(self, count=None, subfolder=""):
        target = os.path.join(self.folder, subfolder) if subfolder else self.folder
        os.makedirs(target, exist_ok=True)
        names = sample_names(count)
        for name in names:
            shutil.copy2(os.path.join(SAMPLES, name), os.path.join(target, name))
        return names

    def scan(self, ctx=None, **kwargs):
        """Scan (no describe); returns the first library's summary."""
        del self.reads[:]
        self.ctx = ctx or jobs.JobContext("test")
        summary = scan.run(self.config, self.ctx, describe_images=False, **kwargs)
        self.run_summary = summary
        return summary["libraries"][0] if summary["libraries"] else None

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

    def describe_row(self, path, info=None):
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? "
                     "WHERE path = ?", (json.dumps(info or INFO), describe.PROMPT_VER, path))

    def thumb_ids(self):
        with cache.ThumbStore() as thumbs:
            return thumbs.ids()

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=1000)["log_tail"])


class IndexingTest(ScanTestCase):

    def test_first_scan_indexes_everything(self):
        names = self.copy_samples()
        self.configure()
        summary = self.scan()
        self.assertEqual(summary["added"], len(names))
        self.assertEqual(len(self.reads), len(names))
        self.assertTrue(summary["complete"])
        rows = self.rows()
        self.assertEqual(sorted(rows), names)
        previews = cache.PreviewStore()
        for path, row in rows.items():
            self.assertEqual(row["library_id"], self.library_id)
            self.assertEqual(row["name"], path)
            self.assertRegex(row["content_hash"], "^[0-9a-f]{40}$")
            self.assertRegex(row["dhash"], "^[0-9a-f]{16}$")
            self.assertGreater(row["width"], 0)
            self.assertTrue(row["taken_at"])
            self.assertIsNone(row["missing_since"])
            self.assertEqual(row["has_preview"], 1)
            self.assertTrue(previews.exists(self.library_id, row["id"]))
            json.loads(row["exif"])
        self.assertEqual(self.thumb_ids(), {row["id"] for row in rows.values()})
        with Image.open(os.path.join(SAMPLES, names[0])) as original:
            self.assertEqual((rows[names[0]]["width"], rows[names[0]]["height"]), original.size)

    def test_rescan_reads_nothing(self):
        names = self.copy_samples(6)
        self.configure()
        self.scan()
        before = self.rows()
        summary = self.scan()
        self.assertEqual(self.reads, [])
        self.assertEqual(summary["unchanged"], len(names))
        self.assertEqual((summary["added"], summary["updated"], summary["missing"]), (0, 0, 0))
        self.assertEqual(self.rows(), before)

    def test_modified_file_is_read_again_and_loses_its_description(self):
        self.copy_samples(4)
        self.configure()
        self.scan()
        name = sample_names(1)[0]
        self.describe_row(name)
        old = self.rows()[name]
        path = os.path.join(self.folder, name)
        write_jpeg(path, seed=5, size=(120, 90))
        summary = self.scan()
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(summary["changed"], 1)
        row = self.rows()[name]
        self.assertEqual(row["id"], old["id"])
        self.assertNotEqual(row["content_hash"], old["content_hash"])
        self.assertEqual((row["width"], row["height"]), (120, 90))
        self.assertEqual((row["described"], row["info"]), (0, ""))

    def test_changed_to_a_described_picture_copies_its_description(self):
        names = self.copy_samples(3)
        self.configure()
        self.scan()
        self.describe_row(names[0])
        shutil.copyfile(os.path.join(self.folder, names[0]), os.path.join(self.folder, names[1]))
        summary = self.scan()
        self.assertEqual((summary["changed"], summary["copied"]), (1, 1))
        row = self.rows()[names[1]]
        self.assertEqual(row["described"], 1)
        self.assertEqual(json.loads(row["info"]), INFO)

    def test_touched_file_keeps_its_description(self):
        self.copy_samples(3)
        self.configure()
        self.scan()
        name = sample_names(1)[0]
        self.describe_row(name)
        path = os.path.join(self.folder, name)
        later = os.stat(path).st_mtime + 3600
        os.utime(path, (later, later))
        summary = self.scan()
        self.assertEqual(len(self.reads), 1)
        self.assertEqual((summary["updated"], summary["changed"]), (1, 0))
        row = self.rows()[name]
        self.assertEqual(row["described"], 1)
        self.assertEqual(json.loads(row["info"]), INFO)
        self.assertAlmostEqual(row["mtime"], later, places=3)

    def test_moved_file_keeps_its_description_and_the_old_row_goes(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        self.describe_row(names[0])
        old = self.rows()[names[0]]
        os.makedirs(os.path.join(self.folder, "Moved"))
        os.rename(os.path.join(self.folder, names[0]),
                  os.path.join(self.folder, "Moved", "renamed.jpg"))
        summary = self.scan()
        self.assertEqual(len(self.reads), 1)
        rows = self.rows()
        self.assertNotIn(names[0], rows)
        new = rows["Moved/renamed.jpg"]
        self.assertEqual(new["described"], 1)
        self.assertEqual(json.loads(new["info"]), INFO)
        self.assertEqual(summary["removed_moved"], 1)
        self.assertNotIn(old["id"], self.thumb_ids())
        self.assertFalse(cache.PreviewStore().exists(self.library_id, old["id"]))

    def test_file_moved_after_being_marked_missing(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        self.describe_row(names[0])
        away = os.path.join(self.base.path, "away.jpg")
        os.rename(os.path.join(self.folder, names[0]), away)
        self.assertEqual(self.scan()["missing"], 1)
        os.rename(away, os.path.join(self.folder, "back-again.jpg"))
        summary = self.scan()
        self.assertEqual(summary["moved"], 1)
        rows = self.rows()
        self.assertNotIn(names[0], rows)
        self.assertEqual(json.loads(rows["back-again.jpg"]["info"]), INFO)

    def test_file_back_at_its_old_path_is_not_taken_for_a_move(self):
        names = self.copy_samples(3)
        self.configure(reader_threads=1)
        self.scan()
        self.describe_row(names[0])
        old = self.rows()[names[0]]
        original = os.path.join(self.folder, names[0])
        away = os.path.join(self.base.path, "away.jpg")
        os.rename(original, away)
        self.scan()
        # It comes back (touched, so it is read again) together with a copy
        # that is read first.
        shutil.copy2(away, os.path.join(self.folder, "00-copy.jpg"))
        os.rename(away, original)
        later = os.stat(original).st_mtime + 60
        os.utime(original, (later, later))
        summary = self.scan()
        self.assertEqual((summary["moved"], summary["removed_moved"]), (0, 0))
        rows = self.rows()
        self.assertEqual(rows[names[0]]["id"], old["id"])
        self.assertIsNone(rows[names[0]]["missing_since"])
        self.assertEqual(rows[names[0]]["described"], 1)
        self.assertEqual(json.loads(rows["00-copy.jpg"]["info"]), INFO)
        self.assertEqual(len(rows), 4)

    def test_duplicate_shares_the_description(self):
        names = self.copy_samples(3)
        self.configure()
        self.scan()
        self.describe_row(names[0])
        os.makedirs(os.path.join(self.folder, "Copies"))
        shutil.copy2(os.path.join(self.folder, names[0]),
                     os.path.join(self.folder, "Copies", "copy.jpg"))
        summary = self.scan()
        self.assertEqual((summary["added"], summary["copied"]), (1, 1))
        rows = self.rows()
        self.assertEqual(json.loads(rows["Copies/copy.jpg"]["info"]), INFO)
        self.assertEqual(rows[names[0]]["described"], 1)
        self.assertIsNone(rows[names[0]]["missing_since"])

    def test_previews_follow_the_library_setting(self):
        names = self.copy_samples(3)
        self.configure([support.library("Family", self.folder, previews=False)])
        self.scan()
        self.assertEqual({row["has_preview"] for row in self.rows().values()}, {0})
        self.assertFalse(os.path.exists(common.previews_dir()))
        self.configure([dict(self.config["libraries"][0], previews=True)])
        summary = self.scan()
        self.assertEqual(summary["unchanged"], len(names))
        self.assertEqual(summary["backfill"]["filled"], len(names))
        self.assertEqual(len(self.reads), len(names))
        store = cache.PreviewStore()
        for row in self.rows().values():
            self.assertEqual(row["has_preview"], 1)
            data = store.read(self.library_id, row["id"])
            self.assertTrue(data.startswith(b"\xff\xd8"))
        self.scan()
        self.assertEqual(self.reads, [])

    def test_lost_preview_files_are_rebuilt(self):
        self.copy_samples(2)
        self.configure()
        self.scan()
        shutil.rmtree(common.previews_dir())
        summary = self.scan()
        self.assertEqual(summary["backfill"]["filled"], 2)
        store = cache.PreviewStore()
        for row in self.rows().values():
            self.assertTrue(store.exists(self.library_id, row["id"]))

    def test_rotated_photo_gets_displayed_size(self):
        imagegen.large_jpeg(self.folder, "rotated.jpg", size=(600, 400), orientation=6)
        self.configure()
        self.scan()
        row = self.rows()["rotated.jpg"]
        self.assertEqual((row["width"], row["height"]), (400, 600))
        preview = cache.PreviewStore().read(self.library_id, row["id"])
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.size, (400, 600))

    def test_orphan_and_disabled_libraries_are_left_alone(self):
        self.copy_samples(2)
        other = self.base.library_folder("Other")
        write_jpeg(os.path.join(other, "x.jpg"), seed=1)
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other, enabled=False)])
        self.execute("INSERT INTO images (library_id, path, name, size, mtime) "
                     "VALUES ('lib_orphan01', 'old.jpg', 'old.jpg', 10, 1)")
        self.scan()
        libraries = {row["library_id"] for row in self.query("SELECT library_id FROM images")}
        self.assertEqual(libraries, {self.library_id, "lib_orphan01"})
        orphan = self.query("SELECT * FROM images WHERE library_id = 'lib_orphan01'")[0]
        self.assertIsNone(orphan["missing_since"])

    def test_library_option_limits_the_scan(self):
        other = self.base.library_folder("Other")
        write_jpeg(os.path.join(other, "x.jpg"), seed=1)
        self.copy_samples(2)
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other)])
        other_id = self.config["libraries"][1]["id"]
        self.scan(library_ids=[other_id])
        self.assertEqual({row["library_id"] for row in self.rows().values()}, {other_id})


class MissingFilesTest(ScanTestCase):

    def test_deleted_file_is_marked_missing_not_deleted(self):
        names = self.copy_samples(5)
        self.configure()
        self.scan()
        os.remove(os.path.join(self.folder, names[1]))
        start = time.time()
        summary = self.scan()
        self.assertEqual(summary["missing"], 1)
        row = self.rows()[names[1]]
        self.assertGreaterEqual(row["missing_since"], start - 1)
        conn = db.connect()
        try:
            counts = db.library_counts(conn)[self.library_id]
            pending, _ = describe.pending_rows(conn, self.config["libraries"])
        finally:
            conn.close()
        self.assertEqual((counts["total"], counts["missing"]), (4, 1))
        self.assertNotIn(names[1], [row["path"] for row in pending])
        self.assertEqual(len(pending), 4)
        # A second scan leaves the first missing time alone.
        self.scan()
        self.assertEqual(self.rows()[names[1]]["missing_since"], row["missing_since"])

    def test_file_that_comes_back_is_restored_without_reading(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        self.describe_row(names[2])
        away = os.path.join(self.base.path, "away.jpg")
        shutil.move(os.path.join(self.folder, names[2]), away)
        self.scan()
        self.assertIsNotNone(self.rows()[names[2]]["missing_since"])
        shutil.move(away, os.path.join(self.folder, names[2]))
        summary = self.scan()
        self.assertEqual(self.reads, [])
        self.assertEqual(summary["restored"], 1)
        row = self.rows()[names[2]]
        self.assertIsNone(row["missing_since"])
        self.assertEqual(row["described"], 1)

    def test_offline_root_changes_nothing(self):
        self.copy_samples(4)
        self.configure()
        self.scan()
        before = self.rows()
        os.rename(self.folder, self.folder + "-away")
        try:
            summary = self.scan()
        finally:
            os.rename(self.folder + "-away", self.folder)
        self.assertTrue(summary["offline"])
        self.assertFalse(summary["complete"])
        self.assertEqual(self.rows(), before)

    def test_helper_walk_returning_none_still_checks_the_root(self):
        self.base.write_file("none_walker.py", "def walk_library(root):\n    return None\n")
        self.copy_samples(4)
        self.configure(helper="none_walker.py")
        helper = common.load_helper(self.config["helper"])
        self.scan(helper=helper)
        self.assertEqual(len(self.rows()), 4)
        before = self.rows()
        os.rename(self.folder, self.folder + "-away")
        try:
            summary = self.scan(helper=helper)
        finally:
            os.rename(self.folder + "-away", self.folder)
        self.assertTrue(summary["offline"])
        self.assertEqual(self.rows(), before)

    def test_helper_walk_iterator_is_used(self):
        self.base.write_file("os_walker.py", (
            "import os\n"
            "calls = []\n"
            "def walk_library(root):\n"
            "    calls.append(root)\n"
            "    return os.walk(root)\n"))
        self.copy_samples(2)
        self.copy_samples(2, subfolder="Skip")
        self.configure([support.library("Family", self.folder, ignore=["Skip/*"])],
                       helper="os_walker.py")
        helper = common.load_helper(self.config["helper"])
        self.scan(helper=helper)
        self.assertEqual(helper.calls, [self.folder])
        self.assertEqual(sorted(self.rows()), sample_names(2))
        # A helper walk that finds nothing (an unreachable share) marks nothing.
        helper.walk_library = lambda root: iter(())
        summary = self.scan(helper=helper)
        self.assertEqual(summary["missing"], 0)
        self.assertEqual(summary["guarded"], 2)
        self.assertEqual({row["missing_since"] for row in self.rows().values()}, {None})

    def test_helper_walk_that_fails_marks_nothing(self):
        self.copy_samples(3)
        self.configure()
        self.scan()

        def broken_walk(root):
            yield root, [], [sample_names(1)[0]]
            raise OSError("the network name is no longer available")

        helper = mock.Mock(spec=["walk_library"])
        helper.walk_library = broken_walk
        summary = self.scan(helper=helper)
        self.assertFalse(summary["complete"])
        self.assertEqual({row["missing_since"] for row in self.rows().values()}, {None})

    def test_unreadable_folder_keeps_its_rows(self):
        names = self.copy_samples(3)
        locked = ["one.jpg", "two.jpg", "three.jpg"]
        for index, name in enumerate(locked):
            write_jpeg(os.path.join(self.folder, "Locked", name), seed=index)
        self.configure()
        self.scan()
        self.assertEqual(len(self.rows()), 6)
        os.remove(os.path.join(self.folder, names[0]))
        real_scandir = os.scandir

        def flaky_scandir(path="."):
            if os.path.basename(os.fspath(path)) == "Locked":
                raise PermissionError(13, "Access is denied", os.fspath(path))
            return real_scandir(path)

        with mock.patch("os.scandir", flaky_scandir):
            summary = self.scan()
        self.assertEqual(summary["errors"][0][0], "Locked")
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["protected"], 3)
        rows = self.rows()
        self.assertIsNotNone(rows[names[0]]["missing_since"])
        for name in locked:
            self.assertIsNone(rows["Locked/" + name]["missing_since"])

    def test_mass_removal_is_refused_unless_allowed(self):
        for index in range(24):
            write_jpeg(os.path.join(self.folder, "img%02d.jpg" % index), seed=index)
        self.configure()
        self.scan()
        for index in range(22):
            os.remove(os.path.join(self.folder, "img%02d.jpg" % index))
        summary = self.scan()
        self.assertEqual((summary["missing"], summary["guarded"]), (0, 22))
        self.assertEqual({row["missing_since"] for row in self.rows().values()}, {None})
        summary = self.scan(allow_mass_removal=True)
        self.assertEqual(summary["missing"], 22)

    def test_empty_folder_marks_nothing(self):
        names = self.copy_samples(3)
        self.configure()
        self.scan()
        for name in names:
            os.remove(os.path.join(self.folder, name))
        summary = self.scan()
        self.assertEqual((summary["missing"], summary["guarded"]), (0, 3))
        self.assertEqual({row["missing_since"] for row in self.rows().values()}, {None})

    def test_missing_rows_are_purged_after_the_configured_days(self):
        names = self.copy_samples(4)
        self.configure(purge_after_days=30)
        self.scan()
        gone = self.rows()[names[0]]
        os.remove(os.path.join(self.folder, names[0]))
        start = time.time()
        self.assertEqual(self.scan(now=start)["missing"], 1)
        self.assertEqual(self.rows()[names[0]]["missing_since"], start)
        self.assertEqual(self.scan(now=start + 29 * DAY)["purged"], 0)
        self.assertIn(names[0], self.rows())
        self.assertEqual(self.scan(now=start + 31 * DAY)["purged"], 1)
        self.assertNotIn(names[0], self.rows())
        self.assertNotIn(gone["id"], self.thumb_ids())
        self.assertFalse(cache.PreviewStore().exists(self.library_id, gone["id"]))
        self.assertEqual(len(self.rows()), 3)


class IgnoreAndFailureTest(ScanTestCase):

    def test_ignore_patterns_and_default_ignores(self):
        files = ["Vacation/a.jpg", "2020/Trip/b.jpg", "sub/g.jpg", "x_y.jpg",
                 "Private/c.jpg", "Private/deep/d.jpg", "@eaDir/e.jpg",
                 "sub/@eaDir/f.jpg", "._h.jpg", "sub/._i.jpg", "notes.txt"]
        for index, rel in enumerate(files):
            write_jpeg(os.path.join(self.folder, *rel.split("/")), seed=index)
        self.configure([support.library("Family", self.folder, ignore=["*_*", "Private/*"])])
        listed = []
        real_scandir = os.scandir

        def recording_scandir(path="."):
            listed.append(os.path.relpath(os.fspath(path), self.folder).replace(os.sep, "/"))
            return real_scandir(path)

        with mock.patch("os.scandir", recording_scandir):
            self.scan()
        self.assertEqual(sorted(self.rows()), ["2020/Trip/b.jpg", "Vacation/a.jpg", "sub/g.jpg"])
        self.assertNotIn("Private", listed)
        self.assertNotIn("@eaDir", listed)
        self.assertNotIn("sub/@eaDir", listed)
        # The '*_*' placeholder regression: ordinary folders are still entered.
        for folder in ("Vacation", "2020", "2020/Trip", "sub"):
            self.assertIn(folder, listed)

    def test_default_ignores_can_be_turned_off(self):
        write_jpeg(os.path.join(self.folder, "@eaDir", "e.jpg"), seed=1)
        self.configure([support.library("Family", self.folder, default_ignores=False)])
        self.scan()
        self.assertEqual(sorted(self.rows()), ["@eaDir/e.jpg"])

    def test_read_failure_is_recorded_and_not_retried_until_changed(self):
        self.copy_samples(2)
        bad = imagegen.corrupt_file(self.folder, "bad.jpg")
        self.configure()
        summary = self.scan()
        self.assertEqual(summary["failed"], 1)
        self.assertNotIn("bad.jpg", self.rows())
        failures = self.query("SELECT * FROM read_failures")
        self.assertEqual([row["path"] for row in failures], ["bad.jpg"])
        self.assertIn("not a supported image", failures[0]["error"])
        summary = self.scan()
        self.assertEqual(self.reads, [])
        self.assertEqual(summary["skipped_failed"], 1)
        write_jpeg(bad, seed=3)
        summary = self.scan()
        self.assertEqual(len(self.reads), 1)
        self.assertIn("bad.jpg", self.rows())
        self.assertEqual(self.query("SELECT * FROM read_failures"), [])

    def test_read_failure_of_a_removed_file_is_forgotten(self):
        self.copy_samples(2)
        bad = imagegen.corrupt_file(self.folder, "bad.jpg")
        self.configure()
        self.scan()
        os.remove(bad)
        self.scan()
        self.assertEqual(self.query("SELECT * FROM read_failures"), [])

    def test_unicode_names(self):
        name = "写真 été \U0001f600.jpg"
        write_jpeg(os.path.join(self.folder, name), seed=2)
        self.configure()
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        self.scan(ctx=jobs.ConsoleContext(stream=stream))
        self.assertEqual(list(self.rows()), [name])


class BackfillTest(ScanTestCase):

    def test_rows_without_content_hash_or_preview_are_filled_in(self):
        for index in range(3):
            imagegen.jpeg_with_exif(self.folder, "photo%d.jpg" % index,
                                    image=imagegen.blocks_image(10 + index),
                                    date="2019:05:1%d 14:23:01" % index)
        self.configure()
        self.scan()
        good = self.rows()
        # Make the rows look like ones imported from the old database.
        self.execute("UPDATE images SET content_hash = '', has_preview = 0, "
                     "taken_at = '2001-01-01T00:00:00', taken_source = 'mtime', camera = ''")
        shutil.rmtree(common.previews_dir())
        self.describe_row("photo1.jpg")
        summary = self.scan()
        self.assertEqual(summary["unchanged"], 3)
        self.assertEqual(summary["backfill"]["filled"], 3)
        self.assertEqual(len(self.reads), 3)
        for path, row in self.rows().items():
            self.assertEqual(row["content_hash"], good[path]["content_hash"])
            self.assertEqual(row["has_preview"], 1)
            self.assertEqual(row["taken_source"], "exif")
            self.assertEqual(row["taken_at"], good[path]["taken_at"])
            self.assertEqual(row["camera"], "Apple iPhone 12")
        self.assertEqual(self.rows()["photo1.jpg"]["described"], 1)
        self.scan()
        self.assertEqual(self.reads, [])

    def test_imported_row_without_hash_is_matched_by_size_and_dhash(self):
        for index in range(3):
            imagegen.jpeg_with_exif(self.folder, "photo%d.jpg" % index,
                                    image=imagegen.blocks_image(10 + index))
        self.configure()
        self.scan()
        self.execute("UPDATE images SET content_hash = ''")
        self.describe_row("photo0.jpg")
        os.makedirs(os.path.join(self.folder, "Sorted"))
        os.rename(os.path.join(self.folder, "photo0.jpg"),
                  os.path.join(self.folder, "Sorted", "photo0.jpg"))
        self.scan()
        rows = self.rows()
        self.assertNotIn("photo0.jpg", rows)
        self.assertEqual(json.loads(rows["Sorted/photo0.jpg"]["info"]), INFO)


class AbortTest(ScanTestCase):

    def test_abort_file_stops_and_the_next_scan_resumes(self):
        names = self.copy_samples(10)
        self.configure(reader_threads=1)
        abort = common.abort_path()
        real_read = images.read_image

        def read_then_abort(path, *args, **kwargs):
            self.reads.append(path)
            if len(self.reads) == 3:
                common.ensure_dir(os.path.dirname(abort))
                open(abort, "w").close()
            return real_read(path, *args, **kwargs)

        ctx = jobs.ConsoleContext(stream=io.StringIO(), check_interval=0)
        with mock.patch.object(images, "read_image", read_then_abort):
            summary = self.scan(ctx=ctx)
        self.assertTrue(summary["aborted"])
        self.assertTrue(self.run_summary["stopped"])
        stored = len(self.rows())
        self.assertGreaterEqual(stored, 3)
        self.assertLess(stored, len(names))
        os.remove(abort)
        summary = self.scan()
        self.assertEqual(len(self.reads), len(names) - stored)
        self.assertEqual(len(self.rows()), len(names))

    def test_stopped_scan_marks_nothing(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        os.remove(os.path.join(self.folder, names[0]))
        ctx = jobs.JobContext("test")
        ctx.request_stop()
        conn = db.connect()
        try:
            summary = scan.scan_library(conn, self.config["libraries"][0], ctx)
        finally:
            conn.close()
        self.assertTrue(summary["aborted"])
        self.assertEqual({row["missing_since"] for row in self.rows().values()}, {None})


class CommandLineTest(ScanTestCase):

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = scan.main(list(args))
        return code, out.getvalue()

    def test_scan_from_the_command_line(self):
        self.copy_samples(3)
        self.configure()
        code, output = self.run_main("--no-describe")
        self.assertEqual(code, 0, output)
        self.assertIn("Added", output)
        self.assertIn("Done.", output)
        self.assertEqual(len(self.rows()), 3)
        self.assertFalse(jobs.WorkerLock().holder())

    def test_busy_lock_exits_cleanly(self):
        self.copy_samples(2)
        self.configure()
        lock = jobs.WorkerLock()
        self.assertTrue(lock.acquire("server job"))
        self.addCleanup(lock.release)
        code, output = self.run_main("--no-describe")
        self.assertEqual(code, 1)
        self.assertIn("Another job is running", output)
        self.assertIn("server job", output)
        self.assertEqual(self.rows(), {})

    def test_abort_file_present_at_start(self):
        self.copy_samples(1)
        self.configure()
        common.ensure_dir(common.data_dir())
        open(common.abort_path(), "w").close()
        code, output = self.run_main("--no-describe")
        self.assertEqual(code, 1)
        self.assertIn("Abort file present", output)
        self.assertEqual(self.rows(), {})

    def test_unknown_library(self):
        self.copy_samples(1)
        self.configure()
        code, output = self.run_main("--no-describe", "--library", "Nope")
        self.assertEqual(code, 2)
        self.assertIn("Unknown library: Nope", output)
        code, output = self.run_main("--no-describe", "--library", "family")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.rows()), 1)

    def test_the_place_names_of_the_files_read_again(self):
        # A picture whose file names its place, indexed by a version that did not read
        # the names (no "XMPPlace" in its exif): --read-place-names reads them, and
        # nothing else of it changes.
        packet = ("<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF><rdf:Description "
                  "rdf:about='' photoshop:State=\"North Coast\" photoshop:Country=\"Examplia\" "
                  "Iptc4xmpCore:CountryCode=\"XA\"/></rdf:RDF></x:xmpmeta>").encode("utf-8")
        names = {"region": "North Coast", "country": "Examplia", "cc": "XA"}
        imagegen.write_bytes(self.folder, "named.jpg", imagegen.jpeg_with_xmp(
            Image.new("RGB", (64, 48), (90, 140, 200)), packet))
        write_jpeg(os.path.join(self.folder, "plain.jpg"), seed=5)
        self.configure()
        self.scan()
        named = self.rows()["named.jpg"]
        exif = json.loads(named["exif"])
        self.assertEqual(exif.pop("XMPPlace"), names)
        self.execute("UPDATE images SET exif = ? WHERE id = ?", (json.dumps(exif), named["id"]))
        self.execute("DELETE FROM place_estimates WHERE image_id = ?", (named["id"],))
        before = self.rows()
        del self.reads[:]
        code, output = self.run_main("--read-place-names")
        self.assertEqual(code, 0, output)
        self.assertIn("2 read, 1 name a place, 1 changed.", output)
        after = self.rows()
        self.assertEqual(json.loads(after["named.jpg"]["exif"])["XMPPlace"], names)
        self.assertEqual(after["plain.jpg"], before["plain.jpg"])
        self.assertEqual({key: value for key, value in after["named.jpg"].items()
                          if key not in ("exif", "updated")},
                         {key: value for key, value in before["named.jpg"].items()
                          if key not in ("exif", "updated")})
        self.assertGreater(after["named.jpg"]["updated"], before["named.jpg"]["updated"])
        self.assertEqual(self.reads, [])        # (only the start of each file)
        # It has the place its file names now (no position: see place_estimates).
        conn = db.connect()
        try:
            import place_estimates
            self.assertEqual(place_estimates.for_picture(conn, named["id"])["source"], "file")
        finally:
            conn.close()
        # Again: nothing to change.
        code, output = self.run_main("--read-place-names", "--library", "Family")
        self.assertIn("2 read, 1 name a place, 0 changed.", output)
        # Nothing else goes with it.
        code, output = self.run_main("--read-place-names", "--no-describe")
        self.assertEqual(code, 2)
        self.assertIn("--read-place-names only", output)

    def test_piped_output_with_names_the_console_cannot_encode(self):
        name = "写真-été.jpg"
        write_jpeg(os.path.join(self.folder, name), seed=4)
        self.configure()
        env = support.child_env(SETTINGS_BASE=self.base.path, PYTHONIOENCODING="cp1252")
        env.pop("PYTHONUTF8", None)
        result = subprocess.run(
            [sys.executable, os.path.join(support.REPO, "scan.py"), "--no-describe"],
            cwd=self.base.path, env=env, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn(b"Added", result.stdout)
        self.assertEqual(list(self.rows()), [name])


def other_info(text):
    return dict(INFO, description=text)


class CarryOverTest(ScanTestCase):
    """Descriptions, previews and the hidden flag follow the content."""

    def description_of(self, path):
        row = self.rows()[path]
        return json.loads(row["info"])["description"] if row["info"] else ""

    def test_swapped_names_keep_both_descriptions(self):
        write_jpeg(os.path.join(self.folder, "a.jpg"), seed=1)
        write_jpeg(os.path.join(self.folder, "b.jpg"), seed=2)
        self.configure(reader_threads=1)
        self.scan()
        self.describe_row("a.jpg", other_info("content A"))
        self.describe_row("b.jpg", other_info("content B"))
        a, b = (os.path.join(self.folder, name) for name in ("a.jpg", "b.jpg"))
        os.replace(a, a + ".tmp")
        os.replace(b, a)
        os.replace(a + ".tmp", b)
        later = time.time() + 10
        os.utime(a, (later, later))
        os.utime(b, (later + 5, later + 5))
        summary = self.scan()
        self.assertEqual((summary["changed"], summary["copied"]), (2, 2))
        self.assertEqual(self.description_of("a.jpg"), "content B")
        self.assertEqual(self.description_of("b.jpg"), "content A")

    def test_edited_file_whose_original_is_kept_under_a_new_name(self):
        path = write_jpeg(os.path.join(self.folder, "IMG_1.jpg"), seed=1)
        self.configure(reader_threads=1)
        self.scan()
        self.describe_row("IMG_1.jpg", other_info("original content"))
        shutil.copy2(path, os.path.join(self.folder, "IMG_1_original.jpg"))
        write_jpeg(path, seed=99)
        self.scan()
        self.assertEqual(self.description_of("IMG_1_original.jpg"), "original content")
        self.assertEqual(self.description_of("IMG_1.jpg"), "")

    def test_failed_preview_write_never_leaves_the_old_picture(self):
        path = write_jpeg(os.path.join(self.folder, "a.jpg"), seed=1, size=(400, 300))
        self.configure()
        self.scan()
        image_id = self.rows()["a.jpg"]["id"]
        store = cache.PreviewStore()
        self.assertTrue(store.exists(self.library_id, image_id))
        write_jpeg(path, seed=2, size=(120, 90))
        later = os.stat(path).st_mtime + 60
        os.utime(path, (later, later))
        real_write = cache.PreviewStore.write

        def failing_write(store_self, library_id, written_id, data):
            if written_id == image_id:
                raise PermissionError(13, "The file is in use")
            return real_write(store_self, library_id, written_id, data)

        # The scan stage alone (no previews pass afterwards).
        conn = db.connect()
        try:
            with mock.patch.object(cache.PreviewStore, "write", failing_write):
                summary = scan.scan_library(conn, self.config["libraries"][0],
                                            jobs.JobContext("test"))
        finally:
            conn.close()
        self.assertEqual(summary["changed"], 1)
        self.assertEqual(self.rows()["a.jpg"]["has_preview"], 0)
        # The previous picture's preview is gone, so nothing can show it.
        self.assertFalse(store.exists(self.library_id, image_id))
        # The previews pass that fails the same way does not bring one back.
        store.write(self.library_id, image_id,
                    imagegen.jpeg_bytes(imagegen.marker_image((400, 300))))
        with mock.patch.object(cache.PreviewStore, "write", failing_write):
            self.scan()
        self.assertEqual(self.rows()["a.jpg"]["has_preview"], 0)
        self.assertFalse(store.exists(self.library_id, image_id))
        # Even a leftover file is not taken for a valid preview by the
        # next run; that run builds a new one from the current content.
        store.write(self.library_id, image_id,
                    imagegen.jpeg_bytes(imagegen.marker_image((400, 300))))
        summary = self.scan()
        self.assertEqual(summary["backfill"]["filled"], 1)
        self.assertEqual(self.rows()["a.jpg"]["has_preview"], 1)
        with Image.open(io.BytesIO(store.read(self.library_id, image_id))) as image:
            self.assertEqual(image.size, (120, 90))

    def test_renamed_folder_holding_most_of_the_library_is_a_move(self):
        for index in range(30):
            write_jpeg(os.path.join(self.folder, "2014-10-24 - Trip", "p%02d.jpg" % index),
                       seed=index + 1)
        self.configure()
        self.scan()
        for index in range(30):
            self.describe_row("2014-10-24 - Trip/p%02d.jpg" % index, other_info("pic %d" % index))
        os.rename(os.path.join(self.folder, "2014-10-24 - Trip"),
                  os.path.join(self.folder, "Trip"))
        summary = self.scan()
        self.assertEqual((summary["guarded"], summary["missing"]), (0, 0))
        self.assertEqual(summary["removed_moved"], 30)
        rows = self.rows()
        self.assertEqual(sorted(rows), ["Trip/p%02d.jpg" % index for index in range(30)])
        self.assertEqual(self.description_of("Trip/p07.jpg"), "pic 7")
        self.assertEqual(self.scan()["guarded"], 0)

    def test_hidden_images_stay_hidden_when_moved_or_copied(self):
        for index in range(4):
            write_jpeg(os.path.join(self.folder, "Private", "p%d.jpg" % index), seed=index + 1)
        write_jpeg(os.path.join(self.folder, "open.jpg"), seed=50)
        self.configure()
        self.scan()
        self.execute("UPDATE images SET hidden = 1 WHERE path LIKE 'Private/%'")
        # Undescribed, so no description carries the flag along.
        os.rename(os.path.join(self.folder, "Private"), os.path.join(self.folder, "Private2"))
        shutil.copy2(os.path.join(self.folder, "Private2", "p0.jpg"),
                     os.path.join(self.folder, "copy-of-p0.jpg"))
        self.scan()
        rows = self.rows()
        self.assertNotIn("Private/p0.jpg", rows)
        for index in range(4):
            self.assertEqual(rows["Private2/p%d.jpg" % index]["hidden"], 1)
        self.assertEqual(rows["copy-of-p0.jpg"]["hidden"], 1)
        self.assertEqual(rows["open.jpg"]["hidden"], 0)

    def test_copy_of_a_hidden_image_is_hidden_as_soon_as_it_is_added(self):
        # Even when the scan stops before the end-of-scan clean-up.
        write_jpeg(os.path.join(self.folder, "secret.jpg"), seed=5)
        self.configure()
        self.scan()
        self.execute("UPDATE images SET hidden = 1")
        shutil.copy2(os.path.join(self.folder, "secret.jpg"),
                     os.path.join(self.folder, "copy.jpg"))
        with mock.patch.object(scan.LibraryScan, "_finish", lambda *args: None):
            self.scan()
        self.assertEqual(self.rows()["copy.jpg"]["hidden"], 1)

    def keep_one_of_two_copies(self, keep, hide, describe=False):
        """Two identical files; the Duplicates page kept one and hid the other."""
        path = write_jpeg(os.path.join(self.folder, keep), seed=7)
        os.makedirs(os.path.dirname(os.path.join(self.folder, hide)), exist_ok=True)
        shutil.copy2(path, os.path.join(self.folder, hide))
        self.configure()
        self.scan()
        if describe:
            self.describe_row(keep)
            self.describe_row(hide)
        self.execute("UPDATE images SET hidden = 1 WHERE path = ?", (hide,))

    def shown(self):
        """{path: shown} of the files present."""
        return {path: row["hidden"] == 0 for path, row in self.rows().items()
                if row["missing_since"] is None}

    def test_the_kept_copy_stays_shown_when_it_moves(self):
        self.keep_one_of_two_copies("a/x.jpg", "b/x.jpg")
        os.makedirs(os.path.join(self.folder, "c"))
        os.replace(os.path.join(self.folder, "a", "x.jpg"),
                   os.path.join(self.folder, "c", "x.jpg"))
        self.scan()
        self.assertEqual(self.shown(), {"b/x.jpg": False, "c/x.jpg": True})

    def test_the_kept_described_copy_stays_shown_when_it_moves(self):
        # The moved file takes the description of a copy (the hidden one,
        # found first, which has the lower id): never its hidden flag.
        path = write_jpeg(os.path.join(self.folder, "d", "y.jpg"), seed=7)
        os.makedirs(os.path.join(self.folder, "e"))
        shutil.copy2(path, os.path.join(self.folder, "e", "y.jpg"))
        self.configure()
        self.scan()
        self.describe_row("d/y.jpg")
        self.describe_row("e/y.jpg")
        rows = self.rows()
        hide, keep = sorted(("d", "e"), key=lambda name: rows[name + "/y.jpg"]["id"])
        self.execute("UPDATE images SET hidden = 1 WHERE path = ?", (hide + "/y.jpg",))
        os.replace(os.path.join(self.folder, keep), os.path.join(self.folder, "f"))
        self.scan()
        self.assertEqual(self.shown(), {hide + "/y.jpg": False, "f/y.jpg": True})
        self.assertEqual(self.rows()["f/y.jpg"]["described"], 1)

    def test_the_kept_copy_stays_shown_when_it_moves_over_two_scans(self):
        # Moved out of the library first (missing), then back at another path.
        self.keep_one_of_two_copies("a/x.jpg", "b/x.jpg")
        outside = os.path.join(self.base.path, "x.jpg")
        os.replace(os.path.join(self.folder, "a", "x.jpg"), outside)
        self.scan()
        os.makedirs(os.path.join(self.folder, "c"))
        os.replace(outside, os.path.join(self.folder, "c", "x.jpg"))
        summary = self.scan()
        self.assertEqual(summary["moved"], 1)
        self.assertEqual(self.shown(), {"b/x.jpg": False, "c/x.jpg": True})

    def test_deleting_the_hidden_copy_keeps_the_kept_one_shown(self):
        self.keep_one_of_two_copies("a/x.jpg", "b/x.jpg")
        os.remove(os.path.join(self.folder, "b", "x.jpg"))
        self.scan()
        self.assertEqual(self.shown(), {"a/x.jpg": True})

    def test_both_copies_moved_keep_their_flags(self):
        self.keep_one_of_two_copies("a/x.jpg", "b/x.jpg")
        os.makedirs(os.path.join(self.folder, "2020"))
        for name in ("a", "b"):
            os.replace(os.path.join(self.folder, name), os.path.join(self.folder, "2020", name))
        self.scan()
        self.assertEqual(self.shown(), {"2020/a/x.jpg": True, "2020/b/x.jpg": False})
        # The hidden one moved alone (another name) stays hidden.
        os.replace(os.path.join(self.folder, "2020", "b", "x.jpg"),
                   os.path.join(self.folder, "2020", "b", "renamed.jpg"))
        self.scan()
        self.assertEqual(self.shown(), {"2020/a/x.jpg": True, "2020/b/renamed.jpg": False})

    def test_a_new_copy_of_a_kept_picture_is_shown(self):
        # The picture is shown anyway: a new copy is shown too (and stacks
        # with the kept one on the Duplicates page).
        self.keep_one_of_two_copies("a/x.jpg", "b/x.jpg")
        shutil.copy2(os.path.join(self.folder, "a", "x.jpg"), os.path.join(self.folder, "d.jpg"))
        self.scan()
        self.assertEqual(self.shown(), {"a/x.jpg": True, "b/x.jpg": False, "d.jpg": True})

    def test_hidden_flag_moves_with_imported_rows(self):
        # Rows without a content hash are matched by size and dHash when the
        # old row is removed; the new row inherits the flag then.
        for index in range(3):
            write_jpeg(os.path.join(self.folder, "Private", "p%d.jpg" % index), seed=index + 1)
        self.configure()
        self.scan()
        self.execute("UPDATE images SET hidden = 1, content_hash = ''")
        os.rename(os.path.join(self.folder, "Private"), os.path.join(self.folder, "Moved"))
        summary = self.scan()
        self.assertEqual(summary["removed_moved"], 3)
        self.assertEqual({row["hidden"] for row in self.rows().values()}, {1})

    def test_moved_described_row_gives_its_description_to_the_new_path(self):
        # A move found only after the fact (the new row got no description,
        # because the old one was not described when it was read) still
        # keeps the description when the old row goes.
        path = write_jpeg(os.path.join(self.folder, "old.jpg"), seed=3)
        write_jpeg(os.path.join(self.folder, "keep.jpg"), seed=4)
        self.configure()
        self.scan()
        self.describe_row("old.jpg", other_info("described late"))
        os.rename(path, os.path.join(self.folder, "new.jpg"))
        conn = db.connect()
        try:
            library = self.config["libraries"][0]
            with mock.patch.object(scan.LibraryScan, "_find_source",
                                   lambda *args: (None, False)):
                scan.scan_library(conn, library, jobs.JobContext("test"))
        finally:
            conn.close()
        rows = self.rows()
        self.assertNotIn("old.jpg", rows)
        self.assertEqual(self.description_of("new.jpg"), "described late")


class NotDuplicatesFollowTest(ScanTestCase):
    """Pictures kept apart as not duplicates stay apart when their files change."""

    def picture(self):
        image = Image.new("RGB", (640, 480), (30, 120, 200))
        draw = ImageDraw.Draw(image)
        for left in range(0, 640, 40):
            draw.rectangle([left, (left * 7) % 400, left + 20, (left * 7) % 400 + 60],
                           fill=(200, left % 255, 50))
        return image

    def groups(self):
        conn = db.connect()
        try:
            records = search_index.build_records(conn, self.config, [self.library_id])
        finally:
            conn.close()
        return {record["n"]: record.get("dg") for record in records}

    def keys(self):
        return {path: not_duplicates.unit_key(row["id"], row["content_hash"])
                for path, row in self.rows().items()}

    def test_a_file_changed_in_place_stays_apart(self):
        image = self.picture()
        image.save(os.path.join(self.folder, "one.jpg"), quality=95)
        image.save(os.path.join(self.folder, "two.jpg"), quality=70)
        self.configure()
        self.scan()
        groups = self.groups()
        self.assertTrue(groups["one.jpg"] and groups["one.jpg"] == groups["two.jpg"], groups)
        old_keys = self.keys()
        not_duplicates.change(not_duplicates.pairs_among(old_keys.values()))
        self.assertEqual(self.groups(), {"one.jpg": None, "two.jpg": None})
        # Re-saved in place (rotated, or a rating written into it elsewhere).
        image.save(os.path.join(self.folder, "two.jpg"), quality=72)
        later = time.time() + 10
        os.utime(os.path.join(self.folder, "two.jpg"), (later, later))
        summary = self.scan()
        self.assertEqual(summary["changed"], 1)
        new_keys = self.keys()
        self.assertNotEqual(new_keys["two.jpg"], old_keys["two.jpg"])
        self.assertIn(tuple(sorted((new_keys["one.jpg"], new_keys["two.jpg"]))),
                      not_duplicates.pairs())
        self.assertEqual(self.groups(), {"one.jpg": None, "two.jpg": None})
        self.assertIn("Kept 1 pair(s) of pictures apart", self.log_text())

    def test_imported_rows_keep_their_pairs_when_their_hash_is_found(self):
        image = self.picture()
        image.save(os.path.join(self.folder, "one.jpg"), quality=95)
        image.save(os.path.join(self.folder, "two.jpg"), quality=70)
        self.configure()
        self.scan()
        # As rows imported from the old database: no content hash yet.
        self.execute("UPDATE images SET content_hash = ''")
        old_keys = self.keys()
        self.assertEqual(sorted(old_keys.values()), sorted("#%d" % row["id"]
                                                           for row in self.rows().values()))
        not_duplicates.change(not_duplicates.pairs_among(old_keys.values()))
        self.scan()
        new_keys = self.keys()
        self.assertTrue(all(not key.startswith("#") for key in new_keys.values()), new_keys)
        self.assertIn(tuple(sorted(new_keys.values())), not_duplicates.pairs())
        self.assertEqual(self.groups(), {"one.jpg": None, "two.jpg": None})


class OwnersEditTest(ScanTestCase):
    """The owner's edits (image_edits) follow moved files and go with removed ones."""

    def save_edit(self, path, **changes):
        conn = db.connect()
        try:
            image_id = conn.execute("SELECT id FROM images WHERE path = ?",
                                    (path,)).fetchone()[0]
            image_edits.save(conn, image_id, changes)
            conn.commit()
        finally:
            conn.close()
        return image_id

    def edit_of(self, path):
        conn = db.connect()
        try:
            row = conn.execute("SELECT id FROM images WHERE path = ?", (path,)).fetchone()
            return image_edits.load(conn, row[0]) if row else None
        finally:
            conn.close()

    def edit_count(self):
        return self.query("SELECT COUNT(*) AS n FROM image_edits")[0]["n"]

    def test_a_moved_file_keeps_its_edit(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        self.describe_row(names[0])
        self.save_edit(names[0], title="Our cat", tags_add=["tom"])
        os.makedirs(os.path.join(self.folder, "Moved"))
        os.rename(os.path.join(self.folder, names[0]),
                  os.path.join(self.folder, "Moved", "renamed.jpg"))
        self.scan()
        self.assertNotIn(names[0], self.rows())
        edit = self.edit_of("Moved/renamed.jpg")
        self.assertEqual((edit["title"], edit["tags_add"]), ("Our cat", ["tom"]))
        self.assertEqual(self.edit_count(), 1)

    def test_a_move_found_after_the_fact_keeps_the_edit(self):
        path = write_jpeg(os.path.join(self.folder, "old.jpg"), seed=3)
        write_jpeg(os.path.join(self.folder, "keep.jpg"), seed=4)
        self.configure()
        self.scan()
        self.save_edit("old.jpg", tags_add=["porch"])
        os.rename(path, os.path.join(self.folder, "new.jpg"))
        conn = db.connect()
        try:
            with mock.patch.object(scan.LibraryScan, "_find_source",
                                   lambda *args: (None, False)):
                scan.scan_library(conn, self.config["libraries"][0], jobs.JobContext("test"))
        finally:
            conn.close()
        self.assertNotIn("old.jpg", self.rows())
        self.assertEqual(self.edit_of("new.jpg")["tags_add"], ["porch"])

    def test_a_changed_file_keeps_its_edit_and_a_purged_one_loses_it(self):
        names = self.copy_samples(4)
        self.configure(purge_after_days=30)
        self.scan()
        self.save_edit(names[0], title="First")
        self.save_edit(names[1], title="Second")
        # New content at the same path: the same picture to its owner.
        shutil.copy2(os.path.join(SAMPLES, names[3]), os.path.join(self.folder, names[1]))
        os.remove(os.path.join(self.folder, names[0]))
        start = time.time()
        self.scan(now=start)
        self.assertEqual(self.edit_of(names[1])["title"], "Second")
        self.assertEqual(self.edit_of(names[0])["title"], "First")      # only missing
        self.scan(now=start + 31 * DAY)
        self.assertNotIn(names[0], self.rows())
        self.assertEqual(self.edit_count(), 1)

    def titles(self):
        return {row["path"]: row["title"] for row in self.query(
            "SELECT i.path, e.title FROM images AS i LEFT JOIN image_edits AS e "
            "ON e.image_id = i.id WHERE i.missing_since IS NULL")}

    def test_a_reused_name_leaves_the_edit_with_the_moved_picture(self):
        # Cameras number their files: IMG_0001.jpg moves and a new photo is
        # saved as IMG_0001.jpg. Either may be read first (a folder's files
        # are listed before its subfolders).
        for old, moved in (("IMG_0001.jpg", "Archive/IMG_0001.jpg"),
                           ("Camera/IMG_0001.jpg", "IMG_0001-old.jpg")):
            with self.subTest(moved=moved):
                support.remove_tree(self.folder)
                path = write_jpeg(os.path.join(self.folder, *old.split("/")), seed=3)
                write_jpeg(os.path.join(self.folder, "keep.jpg"), seed=4)
                self.configure(reader_threads=1)
                self.execute("DELETE FROM images")
                self.scan()
                self.describe_row(old)
                self.save_edit(old, title="Rex", tags_add=["rex"])
                os.makedirs(os.path.dirname(os.path.join(self.folder, *moved.split("/"))),
                            exist_ok=True)
                os.rename(path, os.path.join(self.folder, *moved.split("/")))
                write_jpeg(path, seed=9)
                self.scan()
                self.assertEqual(self.edit_of(moved)["tags_add"], ["rex"])
                self.assertIsNone(self.edit_of(old))
                self.assertEqual(self.rows()[moved]["described"], 1)
                self.assertEqual(self.edit_count(), 1)
                self.assertIn("Your edit of %s went with its picture to %s" % (old, moved),
                              self.log_text())

    def test_swapped_names_swap_the_edits(self):
        a = write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        b = write_jpeg(os.path.join(self.folder, "b.jpg"), seed=4)
        write_jpeg(os.path.join(self.folder, "c.jpg"), seed=5)
        self.configure(reader_threads=1)
        self.scan()
        self.save_edit("a.jpg", title="Rex")
        self.save_edit("b.jpg", title="Tom")

        def swap():
            os.replace(a, a + ".tmp")
            os.replace(b, a)
            os.replace(a + ".tmp", b)
            later = time.time() + 10
            os.utime(a, (later, later))
            os.utime(b, (later, later))

        swap()
        self.scan()
        self.assertEqual(self.titles(), {"a.jpg": "Tom", "b.jpg": "Rex", "c.jpg": None})
        # With only one of them edited, that edit moves.
        self.save_edit("a.jpg", title=None)
        swap()
        self.scan()
        self.assertEqual(self.titles(), {"a.jpg": "Rex", "b.jpg": None, "c.jpg": None})

    def test_a_retouched_file_keeps_its_edit_and_the_kept_original_gets_it_too(self):
        path = write_jpeg(os.path.join(self.folder, "IMG_1.jpg"), seed=3)
        self.configure(reader_threads=1)
        self.scan()
        self.save_edit("IMG_1.jpg", title="Rex")
        shutil.copy2(path, os.path.join(self.folder, "IMG_1_original.jpg"))
        # The same picture saved again: other bytes, the same look.
        with Image.open(path) as image:
            image.load()
            image.save(path, "JPEG", quality=60)
        later = time.time() + 10
        os.utime(path, (later, later))
        self.scan()
        self.assertEqual(self.titles(), {"IMG_1.jpg": "Rex", "IMG_1_original.jpg": "Rex"})

    def test_a_file_changed_in_place_keeps_its_edit_next_to_an_old_copy(self):
        # A copy of the old content that was there before is not where the
        # file went: the edit stays.
        path = write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        shutil.copy2(path, os.path.join(self.folder, "copy.jpg"))
        self.configure()
        self.scan()
        self.save_edit("a.jpg", title="Rex")
        write_jpeg(path, seed=9)
        later = time.time() + 10
        os.utime(path, (later, later))
        self.scan()
        self.assertEqual(self.titles(), {"a.jpg": "Rex", "copy.jpg": None})

    def test_a_move_next_to_a_copy_gives_the_edit_to_the_moved_file(self):
        write_jpeg(os.path.join(self.folder, "copy.jpg"), seed=3)
        write_jpeg(os.path.join(self.folder, "orig.jpg"), seed=3)
        write_jpeg(os.path.join(self.folder, "other.jpg"), seed=5)
        self.configure()
        self.scan()
        self.save_edit("orig.jpg", title="Original", tags_add=["mine"])
        os.makedirs(os.path.join(self.folder, "Moved"))
        os.rename(os.path.join(self.folder, "orig.jpg"),
                  os.path.join(self.folder, "Moved", "orig.jpg"))
        self.scan()
        self.assertEqual(self.titles(), {"copy.jpg": None, "other.jpg": None,
                                         "Moved/orig.jpg": "Original"})
        # And when the copy has an edit of its own, both are kept.
        self.save_edit("copy.jpg", title="Copy")
        os.rename(os.path.join(self.folder, "Moved", "orig.jpg"),
                  os.path.join(self.folder, "orig.jpg"))
        self.scan()
        self.assertEqual(self.titles(), {"copy.jpg": "Copy", "other.jpg": None,
                                         "orig.jpg": "Original"})
        self.assertEqual(self.edit_of("orig.jpg")["tags_add"], ["mine"])
        self.assertEqual(self.edit_count(), 2)

    def test_an_edit_no_copy_can_take_is_logged(self):
        write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        write_jpeg(os.path.join(self.folder, "b.jpg"), seed=3)
        for index in range(3):
            write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
        self.configure()
        self.scan()
        self.save_edit("a.jpg", title="First")
        self.save_edit("b.jpg", title="Second")
        os.makedirs(os.path.join(self.folder, "Moved"))
        os.rename(os.path.join(self.folder, "a.jpg"), os.path.join(self.folder, "Moved", "c.jpg"))
        os.remove(os.path.join(self.folder, "b.jpg"))
        self.scan()
        titles = self.titles()
        self.assertIn(titles.pop("Moved/c.jpg"), ("First", "Second"))
        self.assertEqual(set(titles.values()), {None})
        self.assertEqual(self.edit_count(), 1)
        self.assertRegex(self.log_text(), r"Your edit of [ab]\.jpg \((First|Second)\) was "
                                          r"removed with it: every copy")

    def test_a_move_to_another_library_keeps_the_edit(self):
        other = self.base.library_folder("Other")
        family = support.library("Family", self.folder)
        second = support.library("Other", other)
        for order in ((family, second), (second, family)):
            with self.subTest(first=order[0]["name"]):
                for folder in (self.folder, other):
                    support.remove_tree(folder)
                path = write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
                for index in range(3):
                    write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
                self.configure(list(order), purge_after_days=1)
                self.execute("DELETE FROM images")
                self.scan()
                self.describe_row("a.jpg")
                self.save_edit("a.jpg", title="Rex")
                os.makedirs(other, exist_ok=True)
                os.rename(path, os.path.join(other, "a.jpg"))
                start = time.time()
                self.scan(now=start)
                # Read first, the old library hands the edit on as the file goes
                # missing; read last, the new file takes it from the missing one.
                self.assertIn("Kept your edit of a.jpg for "
                              + ("a.jpg" if order[0] is family else
                                 "its copy in another library"), self.log_text())
                rows = self.query(
                    "SELECT i.library_id, i.missing_since, e.title FROM images AS i "
                    "JOIN image_edits AS e ON e.image_id = i.id WHERE i.path = 'a.jpg'")
                present = [row for row in rows if row["missing_since"] is None]
                self.assertEqual([(row["library_id"], row["title"]) for row in present],
                                 [(self.config["libraries"][[item["name"] for item in order]
                                                            .index("Other")]["id"], "Rex")])
                # The old row goes after purge_after_days; the edit stays.
                self.scan(now=start + 2 * DAY)
                self.assertEqual(self.titles(), {"a.jpg": "Rex", "k0.jpg": None,
                                                 "k1.jpg": None, "k2.jpg": None})
                self.assertEqual(self.edit_count(), 1)


class TrustTest(ScanTestCase):
    """Nothing is removed on the word of a walk that cannot be trusted."""

    def test_file_back_but_unreadable_is_not_purged(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        self.describe_row(names[0])
        path = os.path.join(self.folder, names[0])
        with open(path, "rb") as handle:
            data = handle.read()
        os.remove(path)
        start = time.time()
        self.assertEqual(self.scan(now=start)["missing"], 1)
        with open(path, "wb") as handle:
            handle.write(data[:300])            # back, but truncated
        summary = self.scan(now=start + 31 * DAY)
        self.assertEqual((summary["failed"], summary["purged"]), (1, 0))
        row = self.rows()[names[0]]
        self.assertIsNone(row["missing_since"])
        self.assertEqual(row["described"], 1)

    def test_untrusted_walk_purges_nothing(self):
        names = self.copy_samples(4)
        self.configure()
        self.scan()
        os.remove(os.path.join(self.folder, names[0]))
        start = time.time()
        self.scan(now=start)
        # Everything vanishes (an unmounted share leaving an empty folder).
        for name in names[1:]:
            os.remove(os.path.join(self.folder, name))
        summary = self.scan(now=start + 31 * DAY)
        self.assertEqual((summary["guarded"], summary["purged"]), (3, 0))
        self.assertIn(names[0], self.rows())

    def test_files_now_ignored_are_excluded_not_guarded(self):
        # The old scanner indexed NAS thumbnail folders that the default
        # ignore patterns now skip: they leave without a mass-removal alarm,
        # and the previews pass never reads them.
        for index in range(4):
            write_jpeg(os.path.join(self.folder, "p%d.jpg" % index), seed=index)
        for index in range(24):
            write_jpeg(os.path.join(self.folder, "@eaDir", "p%d.jpg" % (index % 4),
                                    "SYNOPHOTO_THUMB_%d.jpg" % index), seed=index + 10)
        self.configure([support.library("Family", self.folder, default_ignores=False)])
        self.scan()
        self.assertEqual(len(self.rows()), 28)
        self.execute("UPDATE images SET content_hash = '', has_preview = 0")
        self.configure([dict(self.config["libraries"][0], default_ignores=True)])
        summary = self.scan()
        self.assertEqual((summary["guarded"], summary["excluded"]), (0, 24))
        self.assertNotIn("too many", self.log_text())
        self.assertFalse([path for path in self.reads if "@eaDir" in path])
        rows = self.rows()
        self.assertTrue(all(row["missing_since"] is not None
                            for path, row in rows.items() if path.startswith("@eaDir/")))
        self.assertEqual(summary["backfill"]["filled"], 4)

    def test_previews_pass_skips_ignored_files(self):
        write_jpeg(os.path.join(self.folder, "p.jpg"), seed=1)
        write_jpeg(os.path.join(self.folder, "@eaDir", "thumb.jpg"), seed=2)
        self.configure([support.library("Family", self.folder, default_ignores=False)])
        self.scan()
        self.execute("UPDATE images SET content_hash = '', has_preview = 0")
        library = dict(self.config["libraries"][0], default_ignores=True)
        del self.reads[:]
        conn = db.connect()
        try:
            summary = scan.backfill(conn, library, jobs.JobContext("test"))
        finally:
            conn.close()
        self.assertEqual(summary["filled"], 1)
        self.assertEqual([os.path.basename(path) for path in self.reads], ["p.jpg"])

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_rows_under_a_junction_are_kept(self):
        # The old scanner followed junctions; the new one does not, but what
        # it indexed there must not look deleted.
        self.copy_samples(2)
        outside = self.base.make_dir("outside")
        write_jpeg(os.path.join(outside, "s0.jpg"), seed=7)
        link = os.path.join(self.folder, "Scans")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                                capture_output=True)
        if result.returncode != 0 or not os.path.isdir(link):
            self.skipTest("cannot create a junction here")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        self.configure(purge_after_days=30)
        self.execute("INSERT INTO images (library_id, path, name, size, mtime, info, described) "
                     "VALUES (?, 'Scans/s0.jpg', 's0.jpg', 10, 1, '{}', 1)", (self.library_id,))
        summary = self.scan(now=time.time() + 31 * DAY)
        self.assertEqual((summary["links"], summary["missing"], summary["purged"]), (1, 0, 0))
        self.assertEqual(summary["protected"], 1)
        self.assertIsNone(self.rows()["Scans/s0.jpg"]["missing_since"])
        self.assertIn("not followed", self.log_text())
        # Nothing behind the junction was listed (the previews pass may
        # still fill in the row it already had).
        self.assertEqual((summary["files"], summary["added"]), (2, 2))

    def test_file_symlinks_are_not_followed(self):
        outside = write_jpeg(os.path.join(self.base.path, "outside.jpg"), seed=8)
        self.copy_samples(1)
        try:
            os.symlink(outside, os.path.join(self.folder, "linked.jpg"))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.configure()
        summary = self.scan()
        self.assertEqual(summary["links"], 1)
        self.assertNotIn("linked.jpg", self.rows())


class WritingTest(ScanTestCase):
    """How and when the scan writes."""

    def test_progress_is_committed_during_the_scan(self):
        for index in range(8):
            write_jpeg(os.path.join(self.folder, "p%d.jpg" % index), seed=index)
        self.configure(reader_threads=1)
        seen = []
        calls = []
        real_read = images.read_image

        def read_and_count(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 6:
                conn = db.connect()
                try:
                    seen.append(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
                finally:
                    conn.close()
            return real_read(path, *args, **kwargs)

        with mock.patch.object(scan, "COMMIT_ROWS", 2), \
                mock.patch.object(images, "read_image", read_and_count):
            self.scan()
        self.assertGreaterEqual(seen[0], 2)

    def test_a_paused_scan_does_not_lock_out_other_writers(self):
        for index in range(6):
            write_jpeg(os.path.join(self.folder, "p%d.jpg" % index), seed=index)
        self.configure(reader_threads=1)
        ctx = jobs.JobContext("test")
        paused = threading.Event()
        real_read = images.read_image

        def read_then_pause(path, *args, **kwargs):
            result = real_read(path, *args, **kwargs)
            self.reads.append(path)
            if len(self.reads) == 2:
                ctx.pause()
                paused.set()
            return result

        worker = threading.Thread(target=lambda: scan.run(self.config, ctx,
                                                          describe_images=False))
        with mock.patch.object(images, "read_image", read_then_pause):
            worker.start()
            try:
                self.assertTrue(paused.wait(30))
                got_lock = False
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not got_lock:
                    other = sqlite3.connect(common.db_path(), timeout=0.2)
                    try:
                        other.execute("BEGIN IMMEDIATE")
                        other.rollback()
                        got_lock = True
                    except sqlite3.OperationalError:
                        pass
                    finally:
                        other.close()
                self.assertTrue(got_lock, "the paused scan kept the database locked")
                self.assertGreaterEqual(len(self.rows()), 1)
            finally:
                ctx.resume()
                worker.join(60)
        self.assertEqual(len(self.rows()), 6)

    def test_reads_in_flight_are_bounded(self):
        state = {"open": 0, "peak": 0}
        lock = threading.Lock()

        def items():
            for index in range(40):
                with lock:
                    state["open"] += 1
                    state["peak"] = max(state["peak"], state["open"])
                yield index

        def read(item):
            time.sleep(0.002)
            return item

        def handle(item, result):
            with lock:
                state["open"] -= 1

        scan._parallel_reads(3, items(), read, handle, lambda: False, jobs.JobContext())
        self.assertEqual(state["open"], 0)
        # 2 x threads submitted, plus the one taken from the list next.
        self.assertLessEqual(state["peak"], 2 * 3 + 1)

    def test_per_file_lines_are_limited_unless_verbose(self):
        for index in range(5):
            write_jpeg(os.path.join(self.folder, "p%d.jpg" % index), seed=index)
        self.configure()
        with mock.patch.object(scan, "FILE_LOG_LIMIT", 2):
            self.scan()
            text = self.log_text()
            self.assertEqual(text.count("Added p"), 2)
            self.assertIn("use --verbose", text)
            self.assertIn("Library Family: 5 files, 5 added.", text)
            self.execute("DELETE FROM images")
            self.scan(verbose=True)
            self.assertEqual(self.log_text().count("Added p"), 5)

    def test_helper_stat_is_used_when_listing(self):
        self.copy_samples(2)
        self.base.write_file("stat_helper.py", (
            "import os\n"
            "calls = []\n"
            "def stat(path):\n"
            "    calls.append(path)\n"
            "    return os.stat(path)\n"))
        self.configure(helper="stat_helper.py")
        helper = common.load_helper(self.config["helper"])
        self.scan(helper=helper)
        self.assertEqual(sorted(os.path.basename(path) for path in helper.calls),
                         sample_names(2))

    def test_library_wide_queries_run_once_per_scan(self):
        # Looking up copies and moves must use indexes, not go through the
        # whole library for every file (that took minutes at 100k images).
        for index in range(30):
            write_jpeg(os.path.join(self.folder, "Old", "p%02d.jpg" % index), seed=index + 1)
        self.configure()
        self.scan()
        # Imported rows have no content hash, then the folder is renamed and
        # a few files disappear.
        self.execute("UPDATE images SET content_hash = ''")
        os.rename(os.path.join(self.folder, "Old"), os.path.join(self.folder, "New"))
        for index in range(3):
            os.remove(os.path.join(self.folder, "New", "p%02d.jpg" % index))
        statements = []
        real_connect = db.connect

        def tracing_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(db, "connect", tracing_connect):
            summary = self.scan()
        self.assertEqual((summary["added"], summary["removed_moved"]), (27, 27))
        library_wide = [sql for sql in statements
                        if "FROM images WHERE library_id" in " ".join(sql.split())]
        # (Eight: the last ones find the videos whose dates may be read again,
        # and those whose GPS track was never read.)
        self.assertLessEqual(len(library_wide), 8, library_wide)
        conn = real_connect()
        try:
            plan = " ".join(str(row[-1]) for row in conn.execute(
                "EXPLAIN QUERY PLAN " + scan._SOURCE_BY_HASH_SQL, ("x", 0)))
        finally:
            conn.close()
        self.assertIn("images_hash", plan)


class WalkTest(support.TempBaseMixin, unittest.TestCase):

    def test_walk_reports_offline_root(self):
        walk = scan.walk_library(os.path.join(self.base.path, "nothing-here"), [])
        self.assertEqual(list(walk), [])
        self.assertFalse(walk.reachable)
        self.assertFalse(walk.complete)

    def test_long_listings_report_progress(self):
        folder = self.base.library_folder("Walk")
        for index in range(3):
            write_jpeg(os.path.join(folder, "F%d" % index, "a.jpg"), seed=index)
        ctx = jobs.JobContext("test")
        with mock.patch.object(scan, "LIST_LOG_SECONDS", 0):
            entries = list(scan.walk_library(folder, [], ctx=ctx, label="Walk"))
        self.assertEqual(len(entries), 3)
        lines = [entry["message"] for entry in ctx.snapshot()["log_tail"]]
        self.assertIn("Listing Walk: 4 folders, 3 image files so far", lines)

    def test_a_paused_job_stops_listing_folders(self):
        folder = self.base.library_folder("Walk")
        for index in range(3):
            write_jpeg(os.path.join(folder, "F%d" % index, "a.jpg"), seed=index)
        ctx = jobs.JobContext("test")
        ctx.pause()
        entries = []
        thread = threading.Thread(target=lambda: entries.extend(
            scan.walk_library(folder, [], ctx=ctx, label="Walk")), daemon=True)
        thread.start()
        thread.join(0.3)
        # Paused: the share is left alone until the job is resumed.
        self.assertTrue(thread.is_alive())
        self.assertEqual(entries, [])
        ctx.resume()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(entries), 3)

    def test_unchanged_files_do_not_count_towards_the_rate(self):
        folder = self.base.library_folder("Family")
        for index in range(3):
            write_jpeg(os.path.join(folder, "p%d.jpg" % index), seed=index)
        self.base.write_config([support.library("Family", folder)])
        config = common.load_config()
        db.init(config, log=lambda message: None)
        scan.run(config, jobs.JobContext("first"), describe_images=False)
        ctx = jobs.UIContext(1, "scan")
        scan.run(config, ctx, describe_images=False)
        # A re-scan finds every file unchanged: all done, but no real work.
        self.assertEqual((ctx.stage_name, ctx.done, ctx.quick), ("scan", 3, 3))

    def test_walk_yields_sizes_and_times(self):
        folder = self.base.library_folder("Walk")
        path = write_jpeg(os.path.join(folder, "A", "one.jpg"), seed=1)
        write_jpeg(os.path.join(folder, "two.JPG"), seed=2)
        with open(os.path.join(folder, "three.txt"), "w") as handle:
            handle.write("not an image")
        walk = scan.walk_library(folder, common.DEFAULT_IGNORES)
        entries = list(walk)
        self.assertTrue(walk.complete)
        self.assertEqual([entry[0] for entry in entries], ["two.JPG", "A/one.jpg"])
        stat = os.stat(path)
        self.assertEqual(entries[1][1:3], (stat.st_size, stat.st_mtime))


def insta360_exif(name="IMG_20190514_142301_00_001.insp"):
    """EXIF as an Insta360 camera writes it: the original file name is the
    ImageDescription (0x010E)."""
    exif = imagegen.make_exif(make="Arashi Vision", model="Insta360 OneR",
                              date="2019:05:14 14:23:01", offset=None)
    exif[0x010E] = name
    return exif


def dual_fisheye_jpeg(exif=None, size=(1024, 512)):
    frame = imagegen.dual_fisheye_image(size, offsets=images.INSTA360_PARAMS["offsets"])
    return imagegen.jpeg_bytes(frame, exif)


class PanoramaScanTest(ScanTestCase):
    """360-degree photos: stored projection, previews, thumbnails, re-checks."""

    LIBRARY = "lib_pano0001"

    def write(self, name, data):
        return imagegen.write_bytes(self.folder, name, data)

    def thumb_size(self, image_id):
        with cache.ThumbStore() as thumbs:
            data = thumbs.get(image_id)
        with Image.open(io.BytesIO(data)) as image:
            return image.size

    def preview_size(self, image_id):
        data = cache.PreviewStore().read(self.LIBRARY, image_id)
        with Image.open(io.BytesIO(data)) as image:
            return image.size

    def test_scan_stores_the_projection(self):
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif()))
        self.write("pano.jpg", imagegen.jpeg_with_xmp(imagegen.equirect_image((800, 400)),
                                                      imagegen.gpano_xmp()))
        self.write("wide.jpg", imagegen.jpeg_bytes(imagegen.equirect_image((800, 400))))
        # RAW files are not indexed, and the .insv is a video that cannot be
        # read (64 bytes; 360 videos are read in test_video360).
        self.write("IMG_20190514_142301_00_002.dng", b"II*\x00" + b"\x00" * 64)
        self.write("VID_20190514_142301_00_003.insv", b"\x00" * 64)
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        summary = self.scan()
        self.assertEqual(summary["added"], 3)
        rows = self.rows()
        self.assertEqual(sorted(rows), ["IMG_20190514_142301_00_001.insp", "pano.jpg",
                                        "wide.jpg"])
        insp = rows["IMG_20190514_142301_00_001.insp"]
        self.assertEqual(insp["projection"], "dual_fisheye")
        self.assertEqual((insp["width"], insp["height"]), (1024, 512))
        self.assertEqual(insp["camera"], "Arashi Vision Insta360 OneR")
        self.assertEqual(insp["taken_at"], "2019-05-14T14:23:01")
        self.assertEqual(self.thumb_size(insp["id"]), (320, 240))
        self.assertEqual(self.preview_size(insp["id"]), (1024, 512))
        self.assertEqual(rows["pano.jpg"]["projection"], "equirectangular")
        self.assertEqual(self.thumb_size(rows["pano.jpg"]["id"]), (320, 240))
        self.assertEqual(self.preview_size(rows["pano.jpg"]["id"]), (800, 400))
        self.assertEqual(rows["wide.jpg"]["projection"], "")
        self.assertEqual(self.thumb_size(rows["wide.jpg"]["id"]), (320, 160))
        self.scan()
        self.assertEqual(self.reads, [])

    def panos(self):
        """{image id: pano size} of the library's pano files."""
        store = cache.PreviewStore()
        found = {}
        for image_id in store.stored_ids(self.LIBRARY)[1]:
            with Image.open(io.BytesIO(store.read_pano(self.LIBRARY, image_id))) as image:
                found[image_id] = image.size
        return found

    def write_360_set(self, size=(2400, 1200)):
        """Two 360 photos (larger than a preview by default) and a wide photo."""
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif(), size))
        self.write("pano.jpg", imagegen.jpeg_with_xmp(imagegen.equirect_image(size),
                                                      imagegen.gpano_xmp()))
        self.write("wide.jpg", imagegen.jpeg_bytes(imagegen.equirect_image((800, 400))))

    def test_360_photos_get_a_pano_next_to_their_preview(self):
        self.write_360_set()
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        rows = self.rows()
        self.assertEqual(self.panos(),
                         {rows["IMG_20190514_142301_00_001.insp"]["id"]: (2400, 1200),
                          rows["pano.jpg"]["id"]: (2400, 1200)})
        self.assertEqual(self.preview_size(rows["pano.jpg"]["id"]), (2048, 1024))
        self.assertEqual(cache.PreviewStore().stored_ids(self.LIBRARY)[0],
                         {row["id"] for row in rows.values()})
        self.scan()
        self.assertEqual(self.reads, [])

    def test_360_photos_no_larger_than_a_preview_need_no_pano(self):
        # Their preview is already all of them; the backfill does not ask
        # for a pano either.
        self.write_360_set(size=(1024, 512))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        rows = self.rows()
        self.assertEqual(rows["IMG_20190514_142301_00_001.insp"]["projection"], "dual_fisheye")
        self.assertEqual(rows["pano.jpg"]["projection"], "equirectangular")
        self.assertEqual(self.panos(), {})
        self.assertEqual(self.scan()["backfill"]["needed"], 0)
        self.assertEqual(self.reads, [])

    def test_no_panos_without_previews(self):
        self.write_360_set()
        self.configure([support.library("Family", self.folder, id=self.LIBRARY,
                                        previews=False)])
        self.scan()
        self.assertEqual(self.panos(), {})
        self.assertEqual(self.scan()["backfill"]["needed"], 0)

    def test_previews_turned_off_later_do_not_ask_for_panos(self):
        self.write_360_set()
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        for image_id in self.panos():
            cache.PreviewStore().delete_pano(self.LIBRARY, image_id)
        self.configure([support.library("Family", self.folder, id=self.LIBRARY,
                                        previews=False)])
        self.assertEqual(self.scan()["backfill"]["needed"], 0)
        self.assertEqual(self.reads, [])

    def test_panos_follow_their_pictures(self):
        self.write_360_set()
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        rows = self.rows()
        insp_id = rows["IMG_20190514_142301_00_001.insp"]["id"]
        pano_id = rows["pano.jpg"]["id"]
        # New content at the same path that is no 360 photo: no pano.
        path = self.write("pano.jpg", imagegen.jpeg_bytes(imagegen.equirect_image((800, 400))))
        os.utime(path, (time.time() + 5, time.time() + 5))
        summary = self.scan()
        self.assertEqual(summary["changed"], 1)
        self.assertEqual(self.rows()["pano.jpg"]["projection"], "")
        self.assertEqual(set(self.panos()), {insp_id})
        self.assertTrue(cache.PreviewStore().exists(self.LIBRARY, pano_id))
        # A purged picture takes its pano along.
        os.remove(os.path.join(self.folder, "IMG_20190514_142301_00_001.insp"))
        self.scan()
        self.assertEqual(set(self.panos()), {insp_id})
        self.scan(now=time.time() + 31 * DAY)
        self.assertNotIn("IMG_20190514_142301_00_001.insp", self.rows())
        self.assertEqual(self.panos(), {})
        self.assertFalse(cache.PreviewStore().exists(self.LIBRARY, insp_id))

    def test_backfill_makes_missing_panos(self):
        self.write_360_set()
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        rows = self.rows()
        store = cache.PreviewStore()
        # Previews made before panos existed.
        for image_id in self.panos():
            store.delete_pano(self.LIBRARY, image_id)
        summary = self.scan()
        self.assertEqual(sorted(os.path.basename(path) for path in self.reads),
                         ["IMG_20190514_142301_00_001.insp", "pano.jpg"])
        self.assertEqual(summary["backfill"]["panos"], 2)
        self.assertEqual(set(self.panos()), {rows["IMG_20190514_142301_00_001.insp"]["id"],
                                             rows["pano.jpg"]["id"]})
        self.assertIn("(2 with a large panorama for the 360 view)", self.log_text())
        self.scan()
        self.assertEqual(self.reads, [])

    def stitch_state(self):
        conn = db.connect()
        try:
            return scan.stitch_state(conn, self.LIBRARY)
        finally:
            conn.close()

    def set_stitch_state(self, key, pending=()):
        conn = db.connect()
        try:
            scan.save_stitch_state(conn, self.LIBRARY, key, pending)
            conn.commit()
        finally:
            conn.close()

    def test_a_new_library_is_stitched_once(self):
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif()))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        summary = self.scan()
        self.assertEqual([os.path.basename(path) for path in self.reads],
                         ["IMG_20190514_142301_00_001.insp"])
        self.assertEqual(summary["backfill"]["restitched"], 0)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))
        self.scan()
        self.assertEqual(self.reads, [])

    def test_photos_stitched_by_an_older_version_are_stitched_again(self):
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif()))
        write_jpeg(os.path.join(self.folder, "photo.jpg"), seed=3)
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        rows = self.rows()
        insp_id = rows["IMG_20190514_142301_00_001.insp"]["id"]
        self.describe_row("IMG_20190514_142301_00_001.insp")
        # As an older version left it: a hard-seamed preview, no record.
        store = cache.PreviewStore()
        old_preview = imagegen.jpeg_bytes(Image.new("RGB", (1024, 512), (200, 0, 0)))
        store.write(self.LIBRARY, insp_id, old_preview)
        self.execute("DELETE FROM meta")
        summary = self.scan()
        self.assertEqual([os.path.basename(path) for path in self.reads],
                         ["IMG_20190514_142301_00_001.insp"])
        self.assertEqual(summary["backfill"]["restitched"], 1)
        self.assertNotEqual(store.read(self.LIBRARY, insp_id), old_preview)
        self.assertIn("Stitching 1 360-degree photo(s) in", self.log_text())
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))
        row = self.rows()["IMG_20190514_142301_00_001.insp"]
        self.assertEqual(row["described"], 1)                 # the description is kept
        self.assertEqual(json.loads(row["info"]), INFO)
        self.assertEqual(row["projection"], "dual_fisheye")
        self.assertNotIn("describe them again", self.log_text())
        self.scan()
        self.assertEqual(self.reads, [])
        # Other pole fill settings make another round.
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)],
                       nadir_fill_deg=5)
        self.assertEqual(self.scan()["backfill"]["restitched"], 1)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))

    def test_photos_stitched_with_the_old_default_fill_are_stitched_again(self):
        # An earlier version filled 6 degrees round the nadir by default;
        # without that setting the photos it stitched are stitched again.
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif()))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        self.set_stitch_state(images.stitch_key({"nadir_fill_deg": 6.0,
                                                 "zenith_fill_deg": 0.0}), [])
        summary = self.scan()
        self.assertEqual(summary["backfill"]["restitched"], 1)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))
        self.assertEqual(self.scan()["backfill"]["restitched"], 0)

    def test_a_stopped_round_carries_on(self):
        for number in (1, 2, 3):
            name = "IMG_20190514_14230%d_00_00%d.insp" % (number, number)
            self.write(name, dual_fisheye_jpeg(insta360_exif(name)))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        ids = sorted(row["id"] for row in self.rows().values())
        # Two still pending from a run that was stopped.
        self.set_stitch_state(images.stitch_key(), ids[1:])
        summary = self.scan()
        self.assertEqual(summary["backfill"]["restitched"], 2)
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))
        # A pending row that is gone is dropped; one ignored now (so
        # missing) waits until it is back.
        ignored = self.rows()["IMG_20190514_142301_00_001.insp"]["id"]
        self.set_stitch_state(images.stitch_key(), [ignored, 999999])
        self.configure([support.library("Family", self.folder, id=self.LIBRARY,
                                        ignore=["IMG_20190514_142301_00_001.insp"])])
        summary = self.scan()
        self.assertEqual(summary["backfill"]["restitched"], 0)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), [ignored]))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        summary = self.scan()
        self.assertEqual(summary["restored"], 1)
        self.assertEqual(summary["backfill"]["restitched"], 1)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))

    def test_a_photo_that_cannot_be_read_for_now_stays_pending(self):
        # A share that went away during the round: tried again next time.
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif()))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        insp_id = self.rows()["IMG_20190514_142301_00_001.insp"]["id"]
        self.execute("DELETE FROM meta")            # as an older version left it

        def unreachable(path, *args, **kwargs):
            raise images.SourceReadError("cannot read the file: the share went away")
        with mock.patch.object(images, "read_image", unreachable):
            summary = self.scan()
        self.assertEqual((summary["backfill"]["failed"], summary["backfill"]["restitched"]),
                         (1, 0))
        self.assertEqual(self.stitch_state(), (images.stitch_key(), [insp_id]))
        summary = self.scan()
        self.assertEqual(summary["backfill"]["restitched"], 1)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))

    def test_a_photo_missing_at_the_upgrade_is_stitched_when_it_is_back(self):
        name = "IMG_20190514_142301_00_001.insp"
        self.write(name, dual_fisheye_jpeg(insta360_exif()))
        for seed in range(5):
            write_jpeg(os.path.join(self.folder, "photo%d.jpg" % seed), seed=seed)
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        insp_id = self.rows()[name]["id"]
        away = os.path.join(self.base.path, name)
        os.replace(os.path.join(self.folder, name), away)
        self.scan()
        self.assertIsNotNone(self.rows()[name]["missing_since"])
        self.execute("DELETE FROM meta")            # the upgrade
        self.assertEqual(self.scan()["backfill"]["restitched"], 0)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), [insp_id]))
        os.replace(away, os.path.join(self.folder, name))
        summary = self.scan()
        self.assertEqual(summary["restored"], 1)
        self.assertEqual(summary["backfill"]["restitched"], 1)
        self.assertEqual(self.stitch_state(), (images.stitch_key(), []))

    def test_a_broken_record_counts_as_none(self):
        self.write("IMG_20190514_142301_00_001.insp", dual_fisheye_jpeg(insta360_exif()))
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        self.execute("UPDATE meta SET value = '[1, 2' WHERE key = ?",
                     (scan.STITCH_META + self.LIBRARY,))
        self.assertEqual(self.stitch_state(), (None, []))
        self.assertEqual(self.scan()["backfill"]["restitched"], 1)

    def test_a_pano_that_cannot_be_written_is_not_left_behind(self):
        self.write_360_set()
        self.configure([support.library("Family", self.folder, id=self.LIBRARY)])
        self.scan()
        pano_id = self.rows()["pano.jpg"]["id"]
        path = self.write("pano.jpg", imagegen.jpeg_with_xmp(
            imagegen.equirect_image((2400, 1200), marker=True), imagegen.gpano_xmp()))
        os.utime(path, (time.time() + 5, time.time() + 5))
        with mock.patch.object(cache.PreviewStore, "write_pano",
                               side_effect=OSError("disk full")):
            self.scan()
        self.assertNotIn(pano_id, self.panos())
        self.assertTrue(cache.PreviewStore().exists(self.LIBRARY, pano_id))
        self.assertEqual(self.rows()["pano.jpg"]["has_preview"], 1)
        self.assertIn("Could not save the 360-degree panorama of pano.jpg: disk full",
                      self.log_text())

    def build_v1_database(self, files):
        """index.db and thumbs.db as version 1 left them, one row per file."""
        common.ensure_dir(common.data_dir())
        conn = sqlite3.connect(common.db_path())
        for statement in db.SCHEMA_V1:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO libraries (id, name, created) VALUES (?, 'Family', 1)",
                     (self.LIBRARY,))
        thumbs = sqlite3.connect(common.thumbs_db_path())
        db.create_thumbs_schema(thumbs)
        for image_id, (name, described) in enumerate(files, 1):
            path = os.path.join(self.folder, name)
            stat = os.stat(path)
            with Image.open(path) as image:
                size = image.size
            with open(path, "rb") as handle:
                digest = hashlib.sha1(handle.read()).hexdigest()
            conn.execute(
                "INSERT INTO images (id, library_id, path, name, size, mtime, ctime, width, "
                "height, content_hash, dhash, info, described, prompt_ver) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '00000000000000ff', ?, ?, ?)",
                (image_id, self.LIBRARY, name, name, stat.st_size, stat.st_mtime,
                 stat.st_ctime, size[0], size[1], digest,
                 json.dumps(INFO) if described else "", described,
                 describe.PROMPT_VER if described else 0))
            thumbs.execute("INSERT INTO thumbs (image_id, jpeg) VALUES (?, ?)",
                           (image_id, imagegen.jpeg_bytes(imagegen.marker_image())))
        conn.commit()
        conn.close()
        thumbs.commit()
        thumbs.close()

    def test_rows_from_version_1_are_checked_again(self):
        # A .insp copied under another name: its EXIF still says what it is.
        self.write("unstitched.jpg",
                   dual_fisheye_jpeg(insta360_exif("IMG_20190514_142301_00_004.insp")))
        self.write("pano.jpg", imagegen.jpeg_with_xmp(imagegen.equirect_image((800, 400)),
                                                      imagegen.gpano_xmp(form="attribute")))
        self.write("wide.jpg", imagegen.jpeg_bytes(imagegen.equirect_image((800, 400))))
        write_jpeg(os.path.join(self.folder, "photo.jpg"), seed=3)
        files = [("unstitched.jpg", 1), ("pano.jpg", 0), ("wide.jpg", 0), ("photo.jpg", 1)]
        self.build_v1_database(files)
        # Previews off: only the projection check makes rows worth reading.
        self.configure([support.library("Family", self.folder, id=self.LIBRARY,
                                        previews=False)])
        self.assertEqual({row["path"]: row["projection"] for row in self.rows().values()},
                         {"unstitched.jpg": "?", "pano.jpg": "?", "wide.jpg": "?",
                          "photo.jpg": ""})
        summary = self.scan()
        self.assertEqual(summary["unchanged"], 4)
        self.assertEqual(sorted(os.path.basename(path) for path in self.reads),
                         ["pano.jpg", "unstitched.jpg", "wide.jpg"])
        self.assertEqual(summary["backfill"]["filled"], 3)
        self.assertEqual(summary["backfill"]["panoramas"], 2)
        self.assertIn("2 360-degree photo(s) recognised in", self.log_text())
        rows = self.rows()
        self.assertEqual({path: row["projection"] for path, row in rows.items()},
                         {"unstitched.jpg": "dual_fisheye", "pano.jpg": "equirectangular",
                          "wide.jpg": "", "photo.jpg": ""})
        for path in ("unstitched.jpg", "pano.jpg"):
            self.assertEqual(self.thumb_size(rows[path]["id"]), (320, 240))
            self.assertNotEqual(rows[path]["dhash"], "00000000000000ff")
        self.assertEqual(rows["photo.jpg"]["dhash"], "00000000000000ff")    # not read
        # Descriptions are kept; a hint says which ones were made from the
        # unstitched picture.
        self.assertEqual(rows["unstitched.jpg"]["described"], 1)
        self.assertEqual(json.loads(rows["unstitched.jpg"]["info"]), INFO)
        self.assertIn("unstitched 360-degree photos (unstitched.jpg)", self.log_text())
        self.scan()
        self.assertEqual(self.reads, [])

    def test_unreadable_marked_row_is_not_read_every_time(self):
        self.write("wide.jpg", imagegen.jpeg_bytes(imagegen.equirect_image((800, 400))))
        self.build_v1_database([("wide.jpg", 0)])
        self.configure([support.library("Family", self.folder, id=self.LIBRARY,
                                        previews=False)])
        # The file is damaged after it was indexed (same size and time).
        path = os.path.join(self.folder, "wide.jpg")
        stat = os.stat(path)
        with open(path, "r+b") as handle:
            handle.seek(2)
            handle.write(b"\x00" * 64)
        os.utime(path, (stat.st_atime, stat.st_mtime))
        summary = self.scan()
        self.assertEqual(summary["backfill"]["failed"], 1)
        self.scan()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.rows()["wide.jpg"]["projection"], "?")


if __name__ == "__main__":
    unittest.main()
