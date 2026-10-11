"""Tests for common.py: paths, config, library helpers, ignore rules, redact."""

import contextlib
import io
import locale
import os
import tempfile
import unittest
from unittest import mock

import support

import common


class PathTest(support.TempBaseMixin, unittest.TestCase):

    def test_paths_follow_settings_base_at_call_time(self):
        self.assertEqual(common.settings_base(), os.path.abspath(self.base.path))
        data = os.path.join(os.path.abspath(self.base.path), "data")
        expected = {
            common.config_path: os.path.join(os.path.abspath(self.base.path), "config.json"),
            common.data_dir: data,
            common.db_path: os.path.join(data, "index.db"),
            common.legacy_db_path: os.path.join(data, "images.db"),
            common.legacy_thumb_dir: os.path.join(data, "thumbs"),
            common.thumbs_db_path: os.path.join(data, "thumbs.db"),
            common.previews_dir: os.path.join(data, "previews"),
            common.logs_dir: os.path.join(data, "logs"),
            common.tmp_dir: os.path.join(data, "tmp"),
            common.exports_dir: os.path.join(data, "exports"),
            common.reports_dir: os.path.join(data, "reports"),
            common.tiles_dir: os.path.join(data, "tiles"),
            common.abort_path: os.path.join(data, "abort"),
            common.lock_path: os.path.join(data, "worker.lock"),
        }
        for func, path in expected.items():
            self.assertEqual(func(), path, func.__name__)
        with support.TempBase() as other:
            self.assertEqual(common.data_dir(), os.path.join(os.path.abspath(other.path), "data"))
        self.assertEqual(common.data_dir(), data)

    def test_old_writers_are_gone(self):
        # The legacy images.db and shard files are never written again.
        for name in ("open_db", "ThumbStore", "LegacyShardStore", "ensure_data_dirs",
                     "DATA_DIR", "DB_PATH"):
            self.assertFalse(hasattr(common, name), name)

    def test_ensure_dir_and_atomic_write(self):
        folder = common.ensure_dir(self.base.join("a", "b"))
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(common.ensure_dir(folder), folder)
        target = os.path.join(folder, "file.bin")
        common.write_file_atomic(target, b"one")
        common.write_file_atomic(target, "two \u00e9")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), "two \u00e9".encode("utf-8"))
        self.assertEqual(os.listdir(folder), ["file.bin"])


class ConfigTest(support.TempBaseMixin, unittest.TestCase):

    def test_missing_config_gives_defaults(self):
        config = common.load_config()
        self.assertEqual(config["libraries"], [])
        self.assertEqual(config["reader_threads"], 6)
        self.assertEqual(config["endpoint"]["kind"], "openai")
        self.assertFalse(os.path.exists(common.config_path()))

    def test_round_trip_keeps_unicode(self):
        folder = "D:\\Photos\\A\u00f1o \u00c1lbum \u65e5\u672c"
        config = common.load_config()
        config["libraries"].append(support.library("A\u00f1o", folder))
        common.save_config(config)
        with open(common.config_path(), "rb") as handle:
            raw = handle.read()
        # Plain ASCII (\u escapes), so older versions that read the file in
        # the system's encoding still see the right names.
        raw.decode("ascii")
        self.assertIn(b"A\\u00f1o", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\n"))
        loaded = common.load_config()
        self.assertEqual(loaded["libraries"][0]["path"], folder)
        self.assertEqual(loaded["libraries"][0]["name"], "A\u00f1o")
        # No temporary files are left next to the config.
        self.assertEqual(sorted(os.listdir(self.base.path)), ["config.json"])

    def test_reads_utf8_with_bom_and_bytes_undefined_in_cp1252(self):
        # "\u00c1" is 0xC3 0x81 in UTF-8; 0x81 is undefined in cp1252.
        self.base.write_config([support.library("\u00c1lbum", "C:\\Photos\\\u00c1lbum")], bom=True)
        config = common.load_config()
        self.assertEqual(config["libraries"][0]["name"], "\u00c1lbum")
        self.assertEqual(config["libraries"][0]["path"], "C:\\Photos\\\u00c1lbum")

    def test_reads_a_config_saved_in_the_system_encoding(self):
        # Older versions read config.json in the system's encoding, so files
        # saved that way by hand ("ANSI") must keep working.
        text = '{"libraries": [{"name": "Z\u00fcrich", "path": "C:\\\\Photos\\\\Z\u00fcrich"}]}'
        self.base.write_file("config.json", text.encode("cp1252"))
        stderr = io.StringIO()
        with mock.patch.object(locale, "getpreferredencoding", return_value="cp1252"), \
                contextlib.redirect_stderr(stderr):
            config = common.load_config()
            common.load_config()
        self.assertEqual(config["libraries"][0]["name"], "Z\u00fcrich")
        self.assertEqual(config["libraries"][0]["path"], "C:\\Photos\\Z\u00fcrich")
        self.assertEqual(stderr.getvalue().count("not UTF-8"), 1)
        # A system encoding that cannot decode it either is a clear error.
        with mock.patch.object(locale, "getpreferredencoding", return_value="utf-8"):
            with self.assertRaises(common.ConfigError):
                common.load_config()

    def test_library_defaults_filled(self):
        self.base.write_config([{"path": "C:\\Photos\\2014-10-24 - Garden"},
                                {"name": "Off", "path": "D:\\Photos\\Vacation",
                                 "enabled": False, "previews": False}])
        first, second = common.load_config()["libraries"]
        self.assertEqual(first["name"], "Garden")
        self.assertTrue(first["enabled"])
        self.assertTrue(first["previews"])
        self.assertTrue(first["default_ignores"])
        self.assertEqual(first["ignore"], [])
        self.assertNotIn("id", first)
        self.assertFalse(second["enabled"])
        self.assertFalse(second["previews"])

    def test_endpoint_list_is_merged(self):
        self.base.write_config([], endpoint=[{"model": "a"}, {"kind": "ollama", "model": "b"}])
        endpoints = common.load_config()["endpoint"]
        self.assertEqual([e["kind"] for e in endpoints], ["openai", "ollama"])
        self.assertEqual(endpoints[0]["base_url"], "")

    def test_invalid_json_is_a_clear_error(self):
        self.base.write_file("config.json", "{not json")
        with self.assertRaises(common.ConfigError) as caught:
            common.load_config()
        self.assertIn("config.json", str(caught.exception))

    def test_ensure_library_ids(self):
        self.base.write_config([support.library("A", "C:\\Photos\\A"),
                                support.library("B", "C:\\Photos\\B", id="lib_keepme01"),
                                support.library("C", "C:\\Photos\\C", id="lib_keepme01"),
                                support.library("D", "C:\\Photos\\D", id="bad id/..")])
        config = common.load_config()
        self.assertTrue(common.ensure_library_ids(config))
        ids = [library["id"] for library in config["libraries"]]
        self.assertEqual(ids[1], "lib_keepme01")
        self.assertEqual(len(set(ids)), 4)
        for library_id in ids:
            self.assertRegex(library_id, r"^lib_[0-9a-z]{8}$")
        self.assertFalse(common.ensure_library_ids(config))
        common.save_config(config)
        self.assertEqual([library["id"] for library in common.load_config()["libraries"]], ids)

    def test_copy_library_ids(self):
        saved = {"libraries": [
            {"id": "lib_saved001", "name": "Family", "path": "C:/Photos/Family"},
            {"id": "lib_saved002", "name": "Garden", "path": "C:/Photos/Garden"},
        ]}
        # A stale copy: no ids yet (loaded before another process saved them),
        # one library since renamed, one matched only by name.
        stale = {"libraries": [
            {"name": "Relatives", "path": "C:/Photos/Family/"},
            {"name": "garden", "path": "D:\\Photos\\Moved\\Garden"},
            {"name": "New", "path": "D:\\Photos\\New", "id": "lib_new00001"},
        ]}
        self.assertTrue(common.copy_library_ids(stale, saved, keep={"lib_new00001"}))
        self.assertEqual([library["id"] for library in stale["libraries"]],
                         ["lib_saved001", "lib_saved002", "lib_new00001"])
        # only_missing leaves valid ids alone.
        target = {"libraries": [{"name": "Family", "path": "C:/Photos/Family",
                                 "id": "lib_mine0001"}, {"name": "Garden", "path": ""}]}
        common.copy_library_ids(target, saved, only_missing=True)
        self.assertEqual([library["id"] for library in target["libraries"]],
                         ["lib_mine0001", "lib_saved002"])
        # An id is never handed to two libraries.
        twins = {"libraries": [{"name": "Family", "path": "C:/Photos/Family"},
                               {"name": "Family", "path": "C:/Photos/Family"}]}
        common.copy_library_ids(twins, saved)
        self.assertEqual([library.get("id") for library in twins["libraries"]],
                         ["lib_saved001", None])

    def test_library_root(self):
        home = self.base.make_dir("home")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            self.assertEqual(common.library_root({"path": "~/Pictures"}),
                             os.path.join(home, "Pictures"))
        self.assertEqual(common.library_root({"path": ""}), "")
        self.assertEqual(common.library_root({}), "")
        self.assertEqual(common.library_root({"path": "C:/Photos/Family/"}),
                         os.path.normpath("C:/Photos/Family"))


class LibraryNameTest(unittest.TestCase):

    def test_default_library_name(self):
        cases = {
            "2014-10-24 - Garden": "Garden",
            "2014-10 Garden": "Garden",
            "20141024_Garden": "Garden",
            "2014.10.24 Garden": "Garden",
            "2014_10_24_Garden": "Garden",
            "C:\\Photos\\2014-10-24 - Garden": "Garden",
            "\\\\server\\share\\2014-10-24 - Garden\\": "Garden",
            "/home/you/Pictures/2019-05-14 Beach/": "Beach",
            "2014-10-24 - 2014-10-28 Road Trip": "Road Trip",
            "2014 - Trip": "Trip",
            "Family": "Family",
            "C:\\Photos\\Family": "Family",
            "2014": "2014",
            "2014-10-24": "2014-10-24",
            "2001 A Space Odyssey": "2001 A Space Odyssey",
            "1999ers": "1999ers",
            "1000 Islands": "1000 Islands",
            "": "",
        }
        for folder, expected in cases.items():
            self.assertEqual(common.default_library_name(folder), expected, folder)

    def test_library_lookup_helpers(self):
        config = {"libraries": [
            {"id": "lib_00000001", "name": "Family", "enabled": True},
            {"id": "lib_00000002", "name": "Vacation", "enabled": False},
            {"id": "lib_00000003", "name": "", "path": "C:\\Photos\\2014-10-24 - Garden"},
        ]}
        self.assertEqual(common.get_library(config, "lib_00000002")["name"], "Vacation")
        self.assertEqual(common.get_library(config, "family")["id"], "lib_00000001")
        self.assertIsNone(common.get_library(config, "nope"))
        self.assertIsNone(common.get_library(config, ""))
        self.assertEqual([lib["id"] for lib in common.enabled_libraries(config)],
                         ["lib_00000001", "lib_00000003"])
        self.assertEqual(common.library_label(config["libraries"][0]), "Family")
        self.assertEqual(common.library_label(config["libraries"][2]), "Garden")
        self.assertEqual(common.library_label({"id": "lib_00000009"}), "lib_00000009")


class FileTypeTest(unittest.TestCase):

    def test_extensions(self):
        for name in ("a.jpg", "b.JPEG", "c.png", "d.gif", "e.heic", "f.HEIF", "g.hif",
                     "h.webp", "i.tif", "j.TIFF", "k.avif", "l.bmp"):
            self.assertTrue(common.is_image_file(name), name)
        for name in ("a.mov", "b.mp4", "c.txt", "Thumbs.db", "jpg"):
            self.assertFalse(common.is_image_file(name), name)
        for name in ("a.heic", "b.HEIF", "c.hif", "d.avif"):
            self.assertTrue(common.is_heic_like(name), name)
        self.assertFalse(common.is_heic_like("a.jpg"))


class IgnoreTest(unittest.TestCase):

    def test_normalize_ignore(self):
        self.assertEqual(
            common.normalize_ignore(["Documents\\*", " ./Private/* ", "/Root/", "", None,
                                     5, "a//b"]),
            ["Documents/*", "Private/*", "Root", "a/b"])
        self.assertEqual(common.normalize_ignore("*.tmp"), ["*.tmp"])
        self.assertEqual(common.normalize_ignore(None), [])

    def test_probe_patterns_do_not_prune_unrelated_dirs(self):
        # Regression: the old scanner pruned a folder when a made-up file "_"
        # inside it matched, so these patterns pruned every folder and the
        # scan then deleted all of their rows.
        for pattern in ("*/_*", "*_*", "*/?"):
            patterns = common.normalize_ignore([pattern])
            for folder in ("Vacation", "2020", "2020/Trip", "Documents"):
                self.assertFalse(common.should_prune_dir(folder, patterns),
                                 "%s pruned %s" % (pattern, folder))
            for path in ("Vacation/IMG001.jpg", "2020/Trip/IMG002.jpg"):
                self.assertFalse(common.matches_ignore(path, patterns), path)

    def test_pruning_rules(self):
        prune = common.should_prune_dir
        self.assertTrue(prune("Documents", ["Documents/*"]))
        self.assertFalse(prune("Documents2", ["Documents/*"]))
        self.assertFalse(prune("Other/Documents", ["Documents/*"]))
        self.assertTrue(prune("a/cache", ["*/cache/*"]))
        self.assertFalse(prune("cache", ["*/cache/*"]))
        self.assertTrue(prune("Originals", ["Originals"]))
        self.assertTrue(prune("2020/_private", ["*/_*"]))
        self.assertTrue(common.matches_ignore("2020/_private/x.jpg", ["*/_*"]))

    def test_prefix_pruning_is_sound(self):
        # When a "/*" pattern prunes a folder, every file inside really is ignored.
        patterns = ["Documents/*", "*/cache/*", "a*/*", "*_x/*", "20??/raw/*"]
        folders = ["Documents", "Docs", "b/cache", "abc", "abc/def", "q_x", "2019/raw",
                   "2019/raw/deeper", "x/y/z"]
        for pattern in patterns:
            for folder in folders:
                if common.should_prune_dir(folder, [pattern]):
                    for inner in ("f.jpg", "sub/g.jpg"):
                        self.assertTrue(common.matches_ignore(folder + "/" + inner, [pattern]),
                                        "%s pruned %s but not %s" % (pattern, folder, inner))

    def test_default_ignores(self):
        patterns = common.ignore_patterns({"ignore": ["Private/*"]})
        for folder in ("@eaDir", "a/@eaDir", "#recycle", "x/y/.@__thumb", "$RECYCLE.BIN",
                       ".Trashes", "a/.thumbnails", "Private"):
            self.assertTrue(common.should_prune_dir(folder, patterns), folder)
        for path in ("._IMG_0001.jpg", "a/._IMG_0001.jpg", "Thumbs.db", "a/b/Thumbs.db"):
            self.assertTrue(common.matches_ignore(path, patterns), path)
        for path in ("IMG_0001.jpg", "a/IMG_0001.jpg", "eaDir/x.jpg"):
            self.assertFalse(common.matches_ignore(path, patterns), path)
        self.assertFalse(common.should_prune_dir("a/b", patterns))
        self.assertEqual(common.ignore_patterns({"ignore": ["x"], "default_ignores": False}),
                         ["x"])

    def test_walk_with_pruning(self):
        root = tempfile.mkdtemp(prefix="search-images-walk-")
        self.addCleanup(support.remove_tree, root)
        files = ["a.jpg", "_skip.jpg", "Vacation/b.jpg", "Vacation/_c.jpg", "2020/Trip/d.jpg",
                 "2020/_hidden/e.jpg", "@eaDir/f.jpg", "x/@eaDir/g.jpg", "x/._h.jpg",
                 "Documents/i.jpg"]
        for rel in files:
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()
        patterns = common.normalize_ignore(["*/_*", "_*", "Documents/*"]) + list(common.DEFAULT_IGNORES)
        found = []
        for current, dirs, names in os.walk(root):
            rel_dir = os.path.relpath(current, root).replace(os.sep, "/")
            prefix = "" if rel_dir == "." else rel_dir + "/"
            dirs[:] = [d for d in dirs if not common.should_prune_dir(prefix + d, patterns)]
            for name in names:
                if not common.matches_ignore(prefix + name, patterns):
                    found.append(prefix + name)
        self.assertEqual(sorted(found), ["2020/Trip/d.jpg", "Vacation/b.jpg", "a.jpg"])


class HelperTest(support.TempBaseMixin, unittest.TestCase):

    def test_relative_helper_resolves_against_config_folder(self):
        self.base.write_file("my_test_helper.py", "VALUE = 42\n")
        elsewhere = tempfile.mkdtemp(prefix="search-images-cwd-")
        self.addCleanup(support.remove_tree, elsewhere)
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        helper = common.load_helper("my_test_helper.py")
        self.assertEqual(helper.VALUE, 42)
        self.assertIsNone(common.load_helper(""))
        with self.assertRaises(SystemExit) as caught:
            common.load_helper("missing_helper.py")
        # Every place that was looked at is named.
        message = str(caught.exception)
        self.assertIn(os.path.join(os.path.abspath(self.base.path), "missing_helper.py"),
                      message)
        self.assertIn(os.path.join(os.getcwd(), "missing_helper.py"), message)

    def test_relative_helper_in_the_current_directory_still_loads(self):
        # Older versions took a relative helper path from the current
        # directory; such setups keep working, with a warning.
        cwd_folder = tempfile.mkdtemp(prefix="search-images-cwd-")
        self.addCleanup(support.remove_tree, cwd_folder)
        with open(os.path.join(cwd_folder, "old_place_helper.py"), "w") as handle:
            handle.write("VALUE = 7\n")
        cwd = os.getcwd()
        os.chdir(cwd_folder)
        self.addCleanup(os.chdir, cwd)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            helper = common.load_helper("old_place_helper.py")
        self.assertEqual(helper.VALUE, 7)
        self.assertIn("not next to config.json", stderr.getvalue())
        # Next to config.json wins when both exist.
        self.base.write_file("old_place_helper.py", "VALUE = 8\n")
        self.assertEqual(common.load_helper("old_place_helper.py").VALUE, 8)

    def test_stat_path_and_load_image_bytes(self):
        path = self.base.write_file("x.bin", b"abc")
        self.assertEqual(common.stat_path(None, path).st_size, 3)
        self.assertIsNone(common.stat_path(None, self.base.join("missing")))
        self.assertEqual(common.load_image_bytes(None, path), b"abc")

        class Helper:
            @staticmethod
            def load_image(path):
                return b"from helper"
        self.assertEqual(common.load_image_bytes(Helper, path), b"from helper")


class RedactTest(unittest.TestCase):

    def test_masks_credential_shapes(self):
        cases = [
            ("Authorization: Bearer abc.DEF-123_xyz", "abc.DEF-123_xyz"),
            ("x-api-key: k-123456789", "k-123456789"),
            ('{"api_key": "secret-value-1"}', "secret-value-1"),
            ("GET https://example.com/v1/models?key=AIzaSecret123&alt=json", "AIzaSecret123"),
            ("https://example.com/x?a=1&api-key=Secret987", "Secret987"),
            ("api-key=Secret654", "Secret654"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("error from sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
            ("https://bob:hunter2pass@example.com/v1", "hunter2pass"),
        ]
        for text, secret in cases:
            result = common.redact(text)
            self.assertNotIn(secret, result, text)
            self.assertIn(common.REDACTED, result, text)
        self.assertIn("&alt=json", common.redact(cases[3][0]))

    def test_masks_given_secrets(self):
        key = "my-very-secret-key"
        text = "request failed (key %s) at http://127.0.0.1:8080/" % key
        result = common.redact(text, [key, None, "", "ab"])
        self.assertNotIn(key, result)
        self.assertIn("http://127.0.0.1:8080/", result)
        self.assertEqual(common.redact("x " + key, key), "x " + common.REDACTED)
        # Very short values are not treated as secrets.
        self.assertEqual(common.redact("a basic idea", ["a"]), "a basic idea")

    def test_leaves_normal_text_alone(self):
        for text in ("a basic idea", "monkey=banana", "keys: 3", "Scanned 12 files", ""):
            self.assertEqual(common.redact(text), text)
        self.assertEqual(common.redact(None), "")
        self.assertEqual(common.redact(ValueError("boom")), "boom")


if __name__ == "__main__":
    unittest.main()
