"""Tests for db.py: schema, startup, network detection and the legacy import."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

import support

import cache
import common
import db
import imageinfo

IMAGE_COLUMNS = [
    "id", "library_id", "path", "name", "size", "mtime", "ctime", "width", "height",
    "content_hash", "dhash", "taken_at", "taken_source", "tz_offset", "camera", "lat",
    "lon", "exif", "info", "described", "prompt_ver", "provenance", "describe_attempts",
    "describe_error", "hidden", "has_preview", "missing_since", "added", "updated",
    "projection",
]


def columns(conn, table):
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


strict_json = support.strict_json


class SchemaTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.config = self.base.write_config([
            support.library("Family", "C:\\Photos\\Family", id="lib_aaaa0001"),
            support.library("Vacation", "D:\\Photos\\Vacation", id="lib_bbbb0002"),
        ])
        self.log = []

    def init(self, config=None):
        return db.init(config or common.load_config(), log=self.log.append)

    def test_creates_schema(self):
        report = self.init()
        self.assertTrue(report["created"])
        self.assertIsNone(report["legacy"])
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(db.user_version(conn), db.SCHEMA_VERSION)
        self.assertEqual(columns(conn, "images"), IMAGE_COLUMNS)
        self.assertEqual(columns(conn, "libraries"), ["id", "name", "created"])
        self.assertEqual(columns(conn, "read_failures"),
                         ["library_id", "path", "size", "mtime", "error", "seen"])
        self.assertEqual(columns(conn, "jobs"), [
            "id", "kind", "library_id", "params", "state", "stage", "done", "total",
            "summary", "error", "created", "started", "finished"])
        self.assertEqual(columns(conn, "meta"), ["key", "value"])
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'images_%'")}
        self.assertEqual(indexes, {"images_hash", "images_taken"})
        thumbs = db.connect(common.thumbs_db_path())
        self.addCleanup(thumbs.close)
        self.assertEqual(columns(thumbs, "thumbs"), ["image_id", "jpeg"])
        # The set of thumbnail ids is read from the small index, not the BLOBs.
        plan = " ".join(str(row[-1]) for row in thumbs.execute(
            "EXPLAIN QUERY PLAN SELECT image_id FROM thumbs"))
        self.assertIn("COVERING INDEX", plan)

    def test_connection_settings(self):
        self.init()
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        row = conn.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(row["one"], 1)

    def test_init_is_idempotent_and_registers_libraries(self):
        self.init()
        report = self.init()
        self.assertFalse(report["created"])
        self.assertEqual(report["from_version"], db.SCHEMA_VERSION)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(db.library_names(conn),
                         {"lib_aaaa0001": "Family", "lib_bbbb0002": "Vacation"})
        created = conn.execute("SELECT created FROM libraries WHERE id = 'lib_aaaa0001'").fetchone()[0]
        # A rename only changes the name; the id and created time stay.
        config = common.load_config()
        config["libraries"][0]["name"] = "Family Photos"
        del config["libraries"][1]
        self.init(config)
        self.assertEqual(db.library_names(conn),
                         {"lib_aaaa0001": "Family Photos", "lib_bbbb0002": "Vacation"})
        self.assertEqual(conn.execute(
            "SELECT created FROM libraries WHERE id = 'lib_aaaa0001'").fetchone()[0], created)
        self.assertEqual(db.orphan_library_ids(conn, config), ["lib_bbbb0002"])

    def test_init_assigns_and_saves_missing_ids(self):
        self.base.write_config([support.library("New", "C:\\Photos\\New")])
        self.init()
        saved = common.load_config()["libraries"][0]["id"]
        self.assertRegex(saved, r"^lib_[0-9a-f]{8}$")
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(db.library_names(conn), {saved: "New"})

    def test_stale_config_copies_get_the_saved_ids(self):
        # Two processes load config.json before either has assigned ids (or a
        # tool keeps an old copy): both must end up with the same id.
        self.base.write_config([support.library("Family", "C:\\Photos\\Family"),
                                support.library("Garden", "C:\\Photos\\Garden")])
        first = common.load_config()
        second = common.load_config()
        self.init(first)
        self.init(second)
        saved = [library["id"] for library in common.load_config()["libraries"]]
        self.assertEqual([library["id"] for library in first["libraries"]], saved)
        self.assertEqual([library["id"] for library in second["libraries"]], saved)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(sorted(db.library_names(conn)), sorted(saved))
        self.assertEqual(db.orphan_library_ids(conn, second), [])

    def test_ids_given_by_the_caller_are_kept(self):
        self.base.write_config([support.library("Family", "C:\\Photos\\Family")])
        config = common.load_config()
        config["libraries"][0]["id"] = "lib_caller01"
        self.init(config)
        self.assertEqual(common.load_config()["libraries"][0]["id"], "lib_caller01")

    def test_unchanged_libraries_are_not_written(self):
        # Registering libraries that are already known must not need the
        # write lock (a running scan may hold it).
        self.init()
        writer = db.connect()
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            conn = db.connect()
            self.addCleanup(conn.close)
            conn.execute("PRAGMA busy_timeout=100")
            db.register_libraries(conn, common.load_config())
        finally:
            writer.rollback()

    def test_refuses_newer_database(self):
        self.init()
        conn = db.connect()
        conn.execute("PRAGMA user_version=%d" % (db.SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()
        with self.assertRaises(db.NewerDatabaseError) as caught:
            self.init()
        self.assertIn("newer version", str(caught.exception))

    def test_ids_are_never_reused(self):
        self.init()
        conn = db.connect()
        self.addCleanup(conn.close)
        insert = ("INSERT INTO images (library_id, path, name, size, mtime) "
                  "VALUES ('lib_aaaa0001', ?, ?, 1, 1)")
        conn.execute(insert, ("a.jpg", "a.jpg"))
        conn.execute(insert, ("b.jpg", "b.jpg"))
        last = conn.execute("SELECT MAX(id) FROM images").fetchone()[0]
        conn.execute("DELETE FROM images WHERE id = ?", (last,))
        new_id = conn.execute(insert, ("c.jpg", "c.jpg")).lastrowid
        conn.commit()
        self.assertEqual(new_id, last + 1)

    def test_library_counts(self):
        self.init()
        conn = db.connect()
        self.addCleanup(conn.close)
        rows = [
            # path, described, prompt_ver, attempts, hidden, missing_since
            ("a", 1, 4, 0, 0, None),
            ("b", 1, 3, 0, 0, None),
            ("c", 0, 0, 2, 0, None),
            ("c3", 0, 0, 3, 0, None),
            ("d", 0, 0, 0, 1, None),
            ("e", 1, 4, 0, 0, 12345.0),
        ]
        for path, described, prompt_ver, attempts, hidden, missing in rows:
            conn.execute(
                "INSERT INTO images (library_id, path, name, size, mtime, described, "
                "prompt_ver, describe_attempts, hidden, missing_since) "
                "VALUES ('lib_aaaa0001', ?, ?, 1, 1, ?, ?, ?, ?, ?)",
                (path, path, described, prompt_ver, attempts, hidden, missing))
        conn.execute("INSERT INTO images (library_id, path, name, size, mtime) "
                     "VALUES ('lib_gone0003', 'x', 'x', 1, 1)")
        conn.commit()
        counts = db.library_counts(conn, 4)
        self.assertEqual(counts["lib_aaaa0001"], {"total": 5, "described": 2, "stale": 1,
                                                  "failed": 2, "gave_up": 1, "missing": 1,
                                                  "hidden": 1})
        self.assertEqual(counts["lib_bbbb0002"], dict.fromkeys(
            ("total", "described", "stale", "failed", "gave_up", "missing", "hidden"), 0))
        self.assertEqual(counts["lib_gone0003"]["total"], 1)
        self.assertEqual(db.orphan_library_ids(conn, common.load_config()), ["lib_gone0003"])


class NetworkPathTest(unittest.TestCase):

    def test_unc_paths(self):
        # Written with "/" and converted, so both separator styles are covered.
        for path in ("//server/share/data", "//?/UNC/server/share/data"):
            self.assertTrue(db.is_network_path(path), path)
            self.assertTrue(db.is_network_path(path.replace("/", "\\")), path)
        self.assertFalse(db.is_network_path(""))
        self.assertFalse(db.is_network_path(support.REPO))

    @unittest.skipUnless(os.name == "nt", "Windows drive letters")
    def test_mapped_drive(self):
        mapped = "X:\\data"
        with mock.patch.object(db, "_windows_drive_type", return_value=db.DRIVE_REMOTE) as probe:
            self.assertTrue(db.is_network_path(mapped))
            probe.assert_called_with(mapped[:3])
            self.assertTrue(db.is_network_path("//?/" + mapped))
        with mock.patch.object(db, "_windows_drive_type", return_value=3):
            self.assertFalse(db.is_network_path(mapped))
            self.assertFalse(db.is_network_path(("//?/" + mapped).replace("/", "\\")))

    @unittest.skipIf(os.name == "nt", "POSIX mounts")
    def test_network_mount(self):
        with mock.patch.object(db, "_posix_fs_type", return_value="nfs4"):
            self.assertTrue(db.is_network_path("/mnt/photos"))
        with mock.patch.object(db, "_posix_fs_type", return_value="ext4"):
            self.assertFalse(db.is_network_path("/mnt/photos"))


class NetworkJournalTest(support.TempBaseMixin, unittest.TestCase):

    def test_network_data_dir_uses_rollback_journal_and_warns_once(self):
        stderr = io.StringIO()
        db._warned_network.clear()
        db._network_folders.clear()
        with mock.patch.object(db, "is_network_path", return_value=True), \
                contextlib.redirect_stderr(stderr):
            first = db.connect()
            second = db.connect()
            self.assertEqual(first.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(first.execute("PRAGMA synchronous").fetchone()[0], 2)
            first.close()
            second.close()
        self.assertEqual(stderr.getvalue().count("network drive"), 1)
        db._warned_network.clear()
        db._network_folders.clear()


class ImportWithoutPillowTest(unittest.TestCase):

    def test_modules_import_without_pillow(self):
        result = support.run_python(
            "import sys; sys.modules['PIL'] = None; sys.modules['PIL.Image'] = None\n"
            "import common, db, cache, jobs\n"
            "assert db._load_pillow() is None\n"
            "print('ok')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


def file_state(path):
    with open(path, "rb") as handle:
        data = handle.read()
    stat = os.stat(path)
    return hashlib.sha256(data).hexdigest(), stat.st_size, stat.st_mtime_ns


@unittest.skipUnless(support.pillow_available(), "needs Pillow to build JPEG fixtures")
class LegacyImportTest(support.TempBaseMixin, unittest.TestCase):

    FAMILY = "lib_aaaa0001"

    def setUp(self):
        super().setUp()
        self.base.write_config([
            support.library("Family", "C:\\Photos\\Family", id=self.FAMILY),
            support.library("Vacation", "D:\\Photos\\Vacation", id="lib_bbbb0002"),
        ])
        self.orphan = db.orphan_library_id("Old Stuff")
        original_a = support.gradient_jpeg(640, 480)
        original_b = support.gradient_jpeg(640, 480, descending=True)
        self.dhash_a = support.dhash_of(original_a)
        self.thumb_a = support.thumbnail_jpeg(original_a)
        self.thumb_b = support.thumbnail_jpeg(original_b)
        self.assertGreater(db._hamming(self.dhash_a, support.dhash_of(original_b)), 40)
        builder = support.LegacyBuilder(common.data_dir())
        # Row 5: a good thumbnail; EXIF with NaN values (as old versions wrote
        # them), a rotation, a date, a camera and a position.
        exif = ('{"Make": "Apple", "Model": "iPhone 12", "FNumber": NaN, '
                '"ExposureBiasValue": Infinity, "Orientation": 6, '
                '"DateTimeOriginal": "2019:05:14 14:23:01", '
                '"GPS": {"GPSLatitudeRef": "S", "GPSLatitude": [33, 51, 54], '
                '"GPSLongitudeRef": "W", "GPSLongitude": [151, 12, 36], "GPSAltitude": NaN}}')
        builder.add(5, "Family", "2019/a.jpg", thumb=self.thumb_a, exif=exif,
                    dhash=self.dhash_a, width=64, height=48)
        # Row 9: the shard slice is not a JPEG at all.
        builder.add(9, "Family", "2019/b.jpg", thumb=b"\x00garbage" * 50, dhash=self.dhash_a)
        # Row 12: a valid JPEG, but of a different picture (a mis-pointed thumbnail).
        builder.add(12, "Old Stuff", "c.jpg", thumb=self.thumb_b, dhash=self.dhash_a)
        # Row 20: good thumbnail, no stored dHash, already described.
        self.info = json.dumps({"description": "A ramp.", "_model_name": "vision-model",
                                "score": 0.5})
        builder.add(20, "Old Stuff", "d.png", thumb=self.thumb_a, info=self.info,
                    described=1, prompt_ver=4)
        # Row 21: no thumbnail recorded; no EXIF date, so the file time is used.
        builder.add(21, "Family", "e.jpg", mtime=-86400.0 * 400)
        builder.close()
        self.legacy_files = [builder.db_path,
                             os.path.join(builder.thumb_dir, "thumb-0000.dat")]
        self.before = {path: file_state(path) for path in self.legacy_files}
        self.log = []

    def check_legacy_untouched(self):
        for path in self.legacy_files:
            self.assertEqual(file_state(path), self.before[path], path)
        self.assertEqual(sorted(os.listdir(common.legacy_thumb_dir())), ["thumb-0000.dat"])
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse(os.path.exists(common.legacy_db_path() + suffix), suffix)

    def run_init(self):
        return db.init(common.load_config(), log=self.log.append)

    def test_import(self):
        report = self.run_init()
        counts = report["legacy"]
        self.assertTrue(report["created"])
        self.assertEqual(counts["rows"], 5)
        self.assertEqual(counts["skipped"], 0)
        self.assertEqual((counts["thumbs_ok"], counts["thumbs_rejected"], counts["thumbs_missing"]),
                         (2, 2, 1))
        self.assertEqual(counts["exif_fixed"], 1)
        self.assertEqual(counts["libraries"], {self.FAMILY: 3, self.orphan: 2})
        self.assertEqual(counts["orphans"], [{"id": self.orphan, "name": "Old Stuff", "rows": 2}])
        self.assertTrue(any("Old Stuff" in line for line in self.log))
        self.check_legacy_untouched()
        data = common.data_dir()
        self.assertFalse([name for name in os.listdir(data) if ".tmp" in name])

        conn = db.connect()
        self.addCleanup(conn.close)
        rows = {row["id"]: row for row in conn.execute("SELECT * FROM images")}
        self.assertEqual(sorted(rows), [5, 9, 12, 20, 21])
        self.assertEqual({rows[i]["library_id"] for i in (5, 9, 21)}, {self.FAMILY})
        self.assertEqual({rows[i]["library_id"] for i in (12, 20)}, {self.orphan})
        self.assertEqual(db.library_names(conn)[self.orphan], "Old Stuff")
        self.assertEqual(db.orphan_library_ids(conn, common.load_config()), [self.orphan])

        first = rows[5]
        exif = strict_json(first["exif"])
        self.assertIsNone(exif["FNumber"])
        self.assertIsNone(exif["ExposureBiasValue"])
        self.assertIsNone(exif["GPS"]["GPSAltitude"])
        self.assertEqual((first["width"], first["height"]), (48, 64))
        self.assertEqual(first["taken_at"], "2019-05-14T14:23:01")
        self.assertEqual(first["taken_source"], "exif")
        self.assertEqual(first["camera"], "Apple iPhone 12")
        self.assertAlmostEqual(first["lat"], -(33 + 51 / 60 + 54 / 3600), places=6)
        self.assertAlmostEqual(first["lon"], -(151 + 12 / 60 + 36 / 3600), places=6)
        self.assertEqual(first["dhash"], self.dhash_a)
        self.assertEqual(first["path"], "2019/a.jpg")
        self.assertEqual(first["name"], "a.jpg")
        for row in rows.values():
            self.assertEqual(row["content_hash"], "")
            self.assertEqual(row["has_preview"], 0)
            self.assertIsNone(row["missing_since"])
            strict_json(row["exif"])

        described = rows[20]
        self.assertEqual(described["info"], self.info)
        self.assertEqual((described["described"], described["prompt_ver"]), (1, 4))
        self.assertEqual(json.loads(described["provenance"]), {"model": "vision-model"})
        self.assertEqual(rows[21]["taken_source"], "mtime")
        self.assertTrue(rows[21]["taken_at"].startswith("1968-"))
        self.assertIsNone(rows[21]["lat"])

        with cache.ThumbStore() as thumbs:
            self.assertEqual(thumbs.ids(), {5, 20})
            self.assertEqual(thumbs.get(5), self.thumb_a)

        # New rows continue above the imported ids.
        new_id = conn.execute("INSERT INTO images (library_id, path, name, size, mtime) "
                              "VALUES (?, 'new.jpg', 'new.jpg', 1, 1)", (self.FAMILY,)).lastrowid
        conn.commit()
        self.assertEqual(new_id, 22)

    def test_import_runs_once(self):
        self.run_init()
        report = self.run_init()
        self.assertIsNone(report["legacy"])
        self.assertFalse(report["created"])
        self.check_legacy_untouched()

    def test_without_pillow_only_the_jpeg_marker_is_checked(self):
        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            counts = self.run_init()["legacy"]
        # The mis-pointed JPEG cannot be detected without decoding it.
        self.assertEqual((counts["thumbs_ok"], counts["thumbs_rejected"]), (3, 1))
        with cache.ThumbStore() as thumbs:
            self.assertEqual(thumbs.ids(), {5, 12, 20})

    def test_failed_import_leaves_nothing_behind(self):
        with mock.patch.object(db, "_convert_legacy_row", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_init()
        self.assertFalse(os.path.exists(common.db_path()))
        self.assertFalse([name for name in os.listdir(common.data_dir()) if ".tmp" in name])
        self.check_legacy_untouched()
        # The next start imports normally.
        self.assertEqual(self.run_init()["legacy"]["rows"], 5)

    def test_import_after_the_old_scanner_was_killed(self):
        # The old scanner committed once per library, so a crash or a closed
        # console left images.db with a hot rollback journal. The import must
        # work (from the state before the unfinished transaction) without
        # touching the old files.
        code = (
            "import os, sqlite3, sys\n"
            "conn = sqlite3.connect(sys.argv[1])\n"
            "conn.execute('PRAGMA cache_size=1')\n"
            "conn.execute('BEGIN')\n"
            "conn.execute(\"UPDATE images SET info = 'unfinished', described = 1\")\n"
            "conn.execute(\"INSERT INTO images (id, library, path, name, size, mtime, ctime, "
            "width, height) VALUES (99, 'Family', 'half.jpg', 'half.jpg', 1, 1, 1, 1, 1)\")\n"
            "os._exit(0)\n")
        result = support.run_python(code, common.legacy_db_path())
        self.assertEqual(result.returncode, 0, result.stderr)
        journal = common.legacy_db_path() + "-journal"
        self.assertTrue(os.path.exists(journal))
        before = {path: file_state(path) for path in (common.legacy_db_path(), journal)}
        report = self.run_init()
        self.assertEqual(report["legacy"]["rows"], 5)
        conn = db.connect()
        self.addCleanup(conn.close)
        rows = {row["id"]: row for row in conn.execute("SELECT id, info FROM images")}
        self.assertNotIn(99, rows)
        self.assertEqual(rows[20]["info"], self.info)
        self.assertEqual(rows[5]["info"], "")
        for path, state in before.items():
            self.assertEqual(file_state(path), state, path)
        self.assertEqual(os.listdir(common.tmp_dir()), [])

    def test_unreadable_old_database_is_a_clear_error(self):
        with open(common.legacy_db_path(), "wb") as handle:
            handle.write(b"this is not a database" * 100)
        before = file_state(common.legacy_db_path())
        with self.assertRaises(db.DatabaseError) as caught:
            self.run_init()
        self.assertIn("old database", str(caught.exception))
        self.assertFalse(os.path.exists(common.db_path()))
        self.assertEqual(file_state(common.legacy_db_path()), before)
        # The command line tools exit with that message, not a traceback.
        import scan
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = scan.main(["--no-describe"])
        self.assertEqual(code, 2)
        self.assertIn("Error: Could not import the old database", out.getvalue())

    def test_check_legacy_thumb(self):
        from PIL import Image
        self.assertTrue(db.check_legacy_thumb(self.thumb_a, self.dhash_a, Image))
        self.assertTrue(db.check_legacy_thumb(self.thumb_a, "", Image))
        self.assertFalse(db.check_legacy_thumb(self.thumb_b, self.dhash_a, Image))
        self.assertFalse(db.check_legacy_thumb(self.thumb_a[:200], self.dhash_a, Image))
        self.assertFalse(db.check_legacy_thumb(b"", self.dhash_a, Image))
        self.assertTrue(db.check_legacy_thumb(self.thumb_b, self.dhash_a, None))
        self.assertFalse(db.check_legacy_thumb(b"GIF89a", "", None))


class LegacyHelpersTest(unittest.TestCase):

    def test_imported_rows_use_the_scanner_rules(self):
        # The capture date, camera and position of imported rows come from the
        # same code as for newly scanned files (imageinfo), so both agree.
        cases = [
            {"GPS": {"GPSLatitude": [0, 0, 0], "GPSLatitudeRef": "N",
                     "GPSLongitude": [0, 0, 0], "GPSLongitudeRef": "E"}},
            {"Make": "OLYMPUS IMAGING CORP.", "Model": "E-M10"},
            {"DateTimeDigitized": "2019:05:14 14:23:01"},
            {"DateTimeOriginal": "2019:05:14 14:23:01", "OffsetTimeOriginal": "+02:00"},
            {"DateTime": "2019:02:30 10:00:00"},
        ]
        for exif in cases:
            row = {"id": 1, "path": "IMG_20180101_101010.jpg", "exif": json.dumps(exif),
                   "mtime": 1557843781.0, "size": 1, "width": 1, "height": 1}
            counts = {"exif_fixed": 0}
            values = db._convert_legacy_row(row, "lib_aaaa0001", 0.0, counts)
            expected = imageinfo.exif_metadata(exif, "IMG_20180101_101010.jpg", 1557843781.0)
            # taken_at, taken_source, tz_offset, camera, lat, lon
            self.assertEqual(values[10:16], tuple(expected[key] for key in (
                "taken_at", "taken_source", "tz_offset", "camera", "lat", "lon")), exif)
        self.assertEqual(imageinfo.camera_name(cases[1]), "OLYMPUS E-M10")
        self.assertEqual(imageinfo.exif_gps_to_decimal(cases[0]["GPS"]), (None, None))

    def test_orphan_id_is_stable(self):
        self.assertEqual(db.orphan_library_id("Old Stuff"), db.orphan_library_id("Old Stuff"))
        self.assertRegex(db.orphan_library_id("Old Stuff"), r"^lib_[0-9a-f]{8}$")
        self.assertNotEqual(db.orphan_library_id("A"), db.orphan_library_id("B"))


class MigrationToV2Test(support.TempBaseMixin, unittest.TestCase):
    """Schema 2 adds images.projection; rows that may be 360 photos get '?'."""

    FAMILY = "lib_aaaa0001"

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", "C:\\Photos\\Family",
                                                id=self.FAMILY)])
        self.log = []

    def build_v1(self, rows):
        """index.db as version 1 of the schema wrote it, with the given rows."""
        common.ensure_dir(common.data_dir())
        conn = sqlite3.connect(common.db_path())
        for statement in db.SCHEMA_V1:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO libraries (id, name, created) VALUES (?, 'Family', 1)",
                     (self.FAMILY,))
        for image_id, name, width, height, described in rows:
            conn.execute(
                "INSERT INTO images (id, library_id, path, name, size, mtime, width, height, "
                "content_hash, dhash, info, described) VALUES (?, ?, ?, ?, 10, 1, ?, ?, "
                "'abc', '0123456789abcdef', ?, ?)",
                (image_id, self.FAMILY, "a/" + name, name, width, height,
                 '{"description": "x"}' if described else "", described))
        conn.commit()
        conn.close()

    def test_version_1_database_is_migrated(self):
        self.build_v1([
            (3, "pano.jpg", 2000, 1000, 1), (4, "near.jpg", 1970, 1000, 0),
            (5, "photo.jpg", 4000, 3000, 1), (6, "tall.jpg", 1000, 2000, 0),
            (7, "IMG_1.INSP", 300, 300, 0), (8, "unknown.jpg", 0, 0, 0),
            (9, "wide.jpg", 2100, 1000, 0)])
        report = db.init(common.load_config(), log=self.log.append)
        self.assertEqual((report["from_version"], report["version"]), (1, 2))
        self.assertFalse(report["created"])
        self.assertTrue(any("from version 1 to 2" in line for line in self.log))
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(db.user_version(conn), 2)
        rows = {row["name"]: row for row in conn.execute("SELECT * FROM images")}
        self.assertEqual({name: row["projection"] for name, row in rows.items()}, {
            "pano.jpg": "?", "near.jpg": "?", "photo.jpg": "", "tall.jpg": "",
            "IMG_1.INSP": "?", "unknown.jpg": "", "wide.jpg": ""})
        # Everything else is kept as it was.
        self.assertEqual(rows["pano.jpg"]["id"], 3)
        self.assertEqual(rows["pano.jpg"]["info"], '{"description": "x"}')
        self.assertEqual(rows["pano.jpg"]["described"], 1)
        self.assertEqual(rows["photo.jpg"]["dhash"], "0123456789abcdef")
        # The old version is backed up first.
        backup = common.db_path() + ".bak-v1"
        self.assertTrue(os.path.isfile(backup))
        old = sqlite3.connect(backup)
        try:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("projection", columns(old, "images"))
            self.assertEqual(old.execute("SELECT COUNT(*) FROM images").fetchone()[0], 7)
        finally:
            old.close()
        # Running again changes nothing.
        report = db.init(common.load_config(), log=self.log.append)
        self.assertEqual(report["from_version"], 2)

    def test_new_database_needs_no_backup(self):
        db.init(common.load_config(), log=self.log.append)
        self.assertFalse([name for name in os.listdir(common.data_dir()) if ".bak-" in name])

    @unittest.skipUnless(support.pillow_available(), "needs Pillow to build JPEG fixtures")
    def test_imported_rows_are_marked_too(self):
        builder = support.LegacyBuilder(common.data_dir())
        builder.add(1, "Family", "trip/pano.jpg", width=4000, height=2000)
        builder.add(2, "Family", "trip/photo.jpg", width=4000, height=3000)
        builder.add(3, "Family", "trip/IMG_2.insp", width=10, height=10)
        builder.close()
        report = db.init(common.load_config(), log=self.log.append)
        self.assertEqual(report["legacy"]["rows"], 3)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(db.user_version(conn), db.SCHEMA_VERSION)
        self.assertEqual(dict(conn.execute("SELECT id, projection FROM images")),
                         {1: "?", 2: "", 3: "?"})


if __name__ == "__main__":
    unittest.main()
