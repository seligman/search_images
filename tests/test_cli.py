"""Tests for the small command line tools: progress.py, settings.py,
world_map.py and make_tiles.py."""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import support

import common
import db
import progress
import settings
import world_map

SECRET = "sk-test-0123456789abcdefSECRET"
NOW = 1700000000.0


def quiet(*_args, **_kwargs):
    pass


def insert_image(conn, library_id, path, **fields):
    """Insert one images row (sensible defaults) and return its id."""
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "added": NOW, "updated": NOW}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute(
        "INSERT INTO images (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
        [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


def output_of(func, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        func(*args, **kwargs)
    return buffer.getvalue()


def run_script(name, *args):
    """Run one of the repo's scripts in a child process (same SETTINGS_BASE)."""
    return subprocess.run([sys.executable, name] + list(args), cwd=support.REPO,
                          env=support.child_env(), capture_output=True, timeout=120)


def table_row(text, label):
    """The cells of the progress table row that starts with label."""
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("\u2502")]
        if cells and cells[0] == label:
            return cells
    raise AssertionError("no row %r in:\n%s" % (label, text))


class ProgressTest(support.TempBaseMixin, unittest.TestCase):

    def open_db(self):
        db.init(log=quiet)
        conn = db.connect()
        self.addCleanup(conn.close)
        return conn

    def test_counts_on_an_empty_database(self):
        conn = self.open_db()
        text = output_of(progress.show_counts, conn)
        cells = table_row(text, "(all)")
        self.assertEqual(cells[1:], ["-"] * 7)
        self.assertEqual(output_of(progress.show_files, conn), "")

    def test_counts_per_library(self):
        self.base.write_config([
            support.library("Family", self.base.library_folder("a"), id="lib_family1"),
            support.library("Garden", self.base.library_folder("b"), id="lib_garden1",
                            enabled=False),
        ])
        conn = self.open_db()
        current = progress.PROMPT_VER
        insert_image(conn, "lib_family1", "done.jpg", described=1, prompt_ver=current)
        insert_image(conn, "lib_family1", "stale.jpg", described=1, prompt_ver=current - 1)
        insert_image(conn, "lib_family1", "new.jpg")
        insert_image(conn, "lib_family1", "bad.jpg", describe_attempts=3,
                     describe_error="timed out")
        insert_image(conn, "lib_family1", "gone.jpg", missing_since=NOW)
        insert_image(conn, "lib_garden1", "rose.jpg", described=1, prompt_ver=current)
        insert_image(conn, "lib_orphan01", "lost.jpg")

        text = output_of(progress.show_counts, conn)
        # described, at current version, left, failed, missing, total, done
        self.assertEqual(table_row(text, "Family")[1:],
                         ["2", "1", "3", "1", "1", "4", "25.00%"])
        self.assertEqual(table_row(text, "Garden (disabled)")[1:],
                         ["1", "1", "-", "-", "-", "1", "100.00%"])
        self.assertEqual(table_row(text, "lib_orphan01 (orphan)")[1:],
                         ["-", "-", "1", "-", "-", "1", "0.00%"])
        self.assertEqual(table_row(text, "(all)")[1:],
                         ["3", "2", "4", "1", "1", "6", "33.33%"])

        lines = output_of(progress.show_files, conn).splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(lines[0].endswith("Family: new.jpg"), lines)
        self.assertIn("Family: bad.jpg  (failed 3x: timed out)", lines[1])
        self.assertTrue(lines[2].endswith("Family: stale.jpg"), lines)

    def test_command_line(self):
        for command in ("counts", "files"):
            result = run_script("progress.py", command)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"(all)", run_script("progress.py", "counts").stdout)
        self.assertTrue(os.path.isfile(common.db_path()))

    def test_broken_config_is_reported_without_a_traceback(self):
        self.base.write_file("config.json", "{not json")
        for script, args in (("progress.py", ["counts"]), ("serve.py", ["package"]),
                             ("world_map.py", [])):
            result = run_script(script, *args)
            self.assertNotEqual(result.returncode, 0, script)
            self.assertIn(b"Error: Could not read", result.stderr, script)
            self.assertNotIn(b"Traceback", result.stderr, script)


class SettingsTest(support.TempBaseMixin, unittest.TestCase):

    def config_with_two_endpoints(self):
        config = common.default_config()
        config["endpoint"] = [
            {"kind": "openai", "base_url": "http://127.0.0.1:8080/v1", "api_key": SECRET,
             "model": "first"},
            {"kind": "claude", "base_url": "", "api_key": "", "model": "second"},
        ]
        return config

    def test_list_endpoints_are_shown_without_keys(self):
        config = self.config_with_two_endpoints()
        lines = settings.settings_lines(config)
        text = "\n".join(lines)
        self.assertIn("1. kind=openai, model=first", text)
        self.assertIn("2. kind=claude, model=second", text)
        self.assertIn("API key set", text)
        self.assertIn("API key (empty)", text)
        self.assertNotIn(SECRET, text)
        with mock.patch("builtins.input", return_value=""):
            shown = output_of(settings.show_settings, config)
        self.assertNotIn(SECRET, shown)
        self.assertNotIn("0123456789", shown)

    def test_base_url_secrets_are_masked(self):
        endpoint = {"kind": "openai", "base_url": "https://example.com/v1?key=abcdef123456",
                    "api_key": "", "model": "m"}
        self.assertNotIn("abcdef123456", settings.endpoint_summary(endpoint))
        # Nor in the prompt that edits it; Enter keeps the value.
        prompts = []
        answers = iter(["3", "", "0"])

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        with mock.patch("builtins.input", fake_input):
            output = output_of(settings.edit_endpoint, endpoint)
        self.assertNotIn("abcdef123456", "".join(prompts) + output)
        self.assertTrue(any("Base URL" in prompt for prompt in prompts))
        self.assertEqual(endpoint["base_url"], "https://example.com/v1?key=abcdef123456")

    def test_add_and_remove_endpoints(self):
        config = common.default_config()
        self.assertIsInstance(config["endpoint"], dict)
        first = settings.endpoint_list(config)[0]
        added = settings.add_endpoint(config)
        self.assertEqual(config["endpoint"], [first, added])
        self.assertIs(settings.endpoint_list(config)[1], added)
        settings.remove_endpoint(config, 0)
        self.assertEqual(config["endpoint"], [added])
        with self.assertRaises(ValueError):
            settings.remove_endpoint(config, 0)

    def test_edit_endpoint_by_index(self):
        config = self.config_with_two_endpoints()
        answers = iter(["2", "2", "new-model", "0", "0"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output_of(settings.configure_endpoints, config)
        self.assertEqual(config["endpoint"][1]["model"], "new-model")
        self.assertEqual(config["endpoint"][0]["model"], "first")

    def test_answers_keep_or_clear(self):
        self.assertEqual(settings.apply_answer("", "old"), "old")
        self.assertEqual(settings.apply_answer("  ", "old"), "old")
        self.assertEqual(settings.apply_answer("-", "old"), "")
        self.assertEqual(settings.apply_answer(" new ", "old"), "new")

    def test_api_key_prompt_never_shows_the_key(self):
        prompts = []

        def fake_getpass(prompt=""):
            prompts.append(prompt)
            return answers.pop(0)

        answers = ["", "-", "sk-new-key-value-1234"]
        with mock.patch("getpass.getpass", fake_getpass), \
                mock.patch("builtins.input", side_effect=AssertionError("echoed input")):
            printed = output_of(lambda: self.assertEqual(
                settings.prompt_secret("API key", SECRET), SECRET))
            self.assertEqual(settings.prompt_secret("API key", SECRET), "")
            self.assertEqual(settings.prompt_secret("API key", ""), "sk-new-key-value-1234")
        for prompt in prompts:
            self.assertNotIn(SECRET, prompt)
        self.assertNotIn(SECRET, printed)
        self.assertIn("set", prompts[0])
        self.assertIn("(empty)", prompts[2])

    def test_add_library_assigns_id_and_suggests_name(self):
        config = common.default_config()
        library = settings.add_library(config, "C:\\Photos\\2014-10-24 - Garden")
        self.assertEqual(library["name"], "Garden")
        self.assertRegex(library["id"], common.LIBRARY_ID_RE)
        self.assertTrue(library["enabled"])
        other = settings.add_library(config, "D:\\Photos\\Vacation", "Holidays")
        self.assertEqual(other["name"], "Holidays")
        self.assertNotEqual(other["id"], library["id"])
        with self.assertRaises(ValueError):
            settings.add_library(config, "D:\\Photos\\Garden")      # same name, any case
        with self.assertRaises(ValueError):
            settings.add_library(config, "D:\\Photos\\x", "holidays")
        with self.assertRaises(ValueError):
            settings.add_library(config, "  ")

    def test_rename_keeps_id_and_names_stay_unique(self):
        config = common.default_config()
        first = settings.add_library(config, "C:\\Photos\\Family")
        settings.add_library(config, "C:\\Photos\\Garden")
        library_id = first["id"]
        settings.rename_library(config, 0, "Relatives")
        self.assertEqual(config["libraries"][0]["id"], library_id)
        self.assertEqual(config["libraries"][0]["name"], "Relatives")
        settings.rename_library(config, 0, "RELATIVES")       # its own name, new case
        with self.assertRaises(ValueError):
            settings.rename_library(config, 0, "garden")
        with self.assertRaises(ValueError):
            settings.rename_library(config, 0, "")
        settings.set_library_folder(config, 0, "D:\\Photos\\Family")
        self.assertEqual(config["libraries"][0]["id"], library_id)

    def test_remove_asks_and_keeps_the_database(self):
        folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", folder, id="lib_family1")])
        db.init(log=quiet)
        conn = db.connect()
        self.addCleanup(conn.close)
        insert_image(conn, "lib_family1", "a.jpg", described=1)

        def run(answers):
            answers = iter(answers)
            with mock.patch("builtins.input", lambda _prompt="": next(answers)):
                return output_of(settings.main)

        # Libraries menu, Remove, library 1, answer no, Back, Save.
        run(["3", "3", "1", "n", "0", "5"])
        self.assertEqual(len(common.load_config()["libraries"]), 1)
        prompts = []
        answers = iter(["3", "3", "1", "y", "0", "5"])

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        with mock.patch("builtins.input", fake_input):
            output_of(settings.main)
        self.assertEqual(common.load_config()["libraries"], [])
        self.assertTrue(any("orphan" in prompt for prompt in prompts), prompts)
        # Nothing was deleted: the rows are now an orphan library.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)
        self.assertEqual(db.orphan_library_ids(conn, common.load_config()), ["lib_family1"])

    def test_rename_before_the_first_run_keeps_the_old_index(self):
        # The old database matches libraries by name. settings.py imports it
        # before anything can be renamed, so a rename never orphans it.
        folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", folder)])
        builder = support.LegacyBuilder(common.data_dir())
        builder.add(7, "Family", "a.jpg", info='{"description": "A cat."}', described=1,
                    prompt_ver=4)
        builder.close()
        # Libraries menu, Edit, library 1, Rename, new name, Back, Back, Save.
        answers = iter(["3", "2", "1", "1", "Family Photos", "0", "0", "5"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output_of(settings.main)
        (library,) = common.load_config()["libraries"]
        self.assertEqual(library["name"], "Family Photos")
        conn = db.connect()
        self.addCleanup(conn.close)
        row = conn.execute("SELECT library_id, described FROM images WHERE id = 7").fetchone()
        self.assertEqual((row["library_id"], row["described"]), (library["id"], 1))
        db.init(log=quiet)
        self.assertEqual(db.orphan_library_ids(conn, common.load_config()), [])

    def test_save_keeps_ids_saved_by_another_process(self):
        folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", folder)])
        config = common.load_config()
        common.ensure_library_ids(config)          # this session's own idea of the id
        start_ids = {library["id"] for library in config["libraries"]}
        db.init(log=quiet)                          # another process saves the real id
        saved_id = common.load_config()["libraries"][0]["id"]
        self.assertNotIn(saved_id, start_ids)
        settings.rename_library(config, 0, "Relatives")
        added = settings.add_library(config, "D:\\Photos\\New")
        settings.save(config, start_ids)
        saved = common.load_config()["libraries"]
        self.assertEqual([library["id"] for library in saved], [saved_id, added["id"]])
        self.assertEqual(saved[0]["name"], "Relatives")

    def test_main_adds_a_library_with_the_suggested_name(self):
        folder = self.base.library_folder("2019-07 Vacation")
        # Libraries menu, Add, folder, accept the suggested name, Back, Save.
        answers = iter(["3", "1", folder, "", "0", "5"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output_of(settings.main)
        with open(common.config_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        (library,) = saved["libraries"]
        self.assertEqual(library["name"], "Vacation")
        self.assertEqual(library["path"], folder)
        self.assertRegex(library["id"], common.LIBRARY_ID_RE)


class WorldMapTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([
            support.library("Family", self.base.library_folder("a"), id="lib_family1"),
            support.library("Garden", self.base.library_folder("b"), id="lib_garden1",
                            enabled=False),
        ])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.shown = insert_image(self.conn, "lib_family1", "a.jpg", lat=-33.9, lon=151.2)
        insert_image(self.conn, "lib_family1", "no-gps.jpg")
        insert_image(self.conn, "lib_family1", "gone.jpg", lat=1.0, lon=1.0, missing_since=NOW)
        insert_image(self.conn, "lib_family1", "hidden.jpg", lat=2.0, lon=2.0, hidden=1)
        insert_image(self.conn, "lib_family1", "null-island.jpg", lat=0.0, lon=0.0)
        insert_image(self.conn, "lib_family1", "bad.jpg", lat=95.0, lon=10.0)
        insert_image(self.conn, "lib_family1", "inf.jpg", lat=float("inf"), lon=10.0)
        self.disabled = insert_image(self.conn, "lib_garden1", "b.jpg", lat=40.7, lon=-74.0)

    def test_collects_present_visible_positions(self):
        self.assertEqual(world_map.collect_located_images(self.conn),
                         [(self.shown, -33.9, 151.2)])
        self.assertEqual(world_map.collect_located_images(),
                         [(self.shown, -33.9, 151.2)])
        self.assertEqual(world_map.collect_located_images(self.conn, ["lib_garden1"]),
                         [(self.disabled, 40.7, -74.0)])
        self.assertEqual(world_map.collect_located_images(self.conn, []), [])
        self.assertEqual(world_map.collect_locations(self.conn), [(-33.9, 151.2)])

    @unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
    def test_command_line_writes_into_the_data_folder(self):
        result = run_script("world_map.py", "--width", "64", "--height", "32")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(common.data_dir(), "world_map.png")))
        self.assertFalse(os.path.exists(os.path.join(support.REPO, "world_map.png")))


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class MakeTilesTest(support.TempBaseMixin, unittest.TestCase):

    def test_tiles_go_to_the_current_data_folder(self):
        import make_tiles
        import serve
        from PIL import Image
        self.assertEqual(make_tiles.tile_path(1, 2, 3),
                         os.path.join(self.base.path, "data", "tiles", "1", "2", "3.jpg"))
        self.assertEqual(make_tiles.default_source(),
                         os.path.join(self.base.path, "data", "blue_marble.jpg"))
        source = Image.new("RGB", (512, 256), (10, 80, 160))
        with contextlib.redirect_stdout(io.StringIO()):
            make_tiles.slice_zoom(source, 1, 80, False)
        make_tiles.write_manifest(1)
        tiles = os.path.join(self.base.path, "data", "tiles")
        self.assertEqual(sorted(os.listdir(os.path.join(tiles, "1"))), ["0", "1", "2", "3"])
        self.assertEqual(sorted(os.listdir(os.path.join(tiles, "1", "3"))), ["0.jpg", "1.jpg"])
        self.assertEqual(serve.tiles_max_zoom(), 1)


if __name__ == "__main__":
    unittest.main()
