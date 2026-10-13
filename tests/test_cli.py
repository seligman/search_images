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

# Not a real key: a made-up value used to check that keys never leak.
SECRET = "sk-test-0123456789abcdefSECRET"
NOW = 1700000000.0


quiet = support.quiet


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
        self.assertEqual(cells[1:], ["-"] * 9)
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
        # (Out of date for a picture: before prompts.CURRENT_SINCE.)
        insert_image(conn, "lib_family1", "stale.jpg", described=1,
                     prompt_ver=progress.prompts.CURRENT_SINCE["image"] - 1)
        insert_image(conn, "lib_family1", "new.jpg")
        insert_image(conn, "lib_family1", "hidden.jpg", hidden=1)
        insert_image(conn, "lib_family1", "once.jpg", describe_attempts=1,
                     describe_error="cut off")
        insert_image(conn, "lib_family1", "bad.jpg", describe_attempts=3,
                     describe_error="timed out")
        insert_image(conn, "lib_family1", "gone.jpg", missing_since=NOW)
        insert_image(conn, "lib_garden1", "rose.jpg", described=1, prompt_ver=current)
        insert_image(conn, "lib_orphan01", "lost.jpg")

        text = output_of(progress.show_counts, conn)
        # described, at current version, older prompt, left (what the next
        # run describes: hidden images too, not those given up on), failed,
        # gave up, missing, total, done
        self.assertEqual(table_row(text, "Family")[1:],
                         ["2", "1", "1", "3", "2", "1", "1", "6", "16.67%"])
        self.assertEqual(table_row(text, "Garden (disabled)")[1:],
                         ["1", "1", "-", "-", "-", "-", "-", "1", "100.00%"])
        self.assertEqual(table_row(text, "lib_orphan01 (orphan)")[1:],
                         ["-", "-", "-", "1", "-", "-", "-", "1", "0.00%"])
        self.assertEqual(table_row(text, "(all)")[1:],
                         ["3", "2", "1", "4", "2", "1", "1", "8", "25.00%"])

        lines = output_of(progress.show_files, conn).splitlines()
        self.assertEqual(len(lines), 5, lines)
        self.assertTrue(lines[0].endswith("Family: new.jpg"), lines)
        self.assertTrue(lines[1].endswith("Family: hidden.jpg"), lines)
        self.assertIn("Family: once.jpg  (failed 1x: cut off)", lines[2])
        self.assertIn("Family: bad.jpg  (skipped after 3 failures: timed out; run scan.py "
                      "--retry-failed)", lines[3])
        self.assertIn("Family: stale.jpg  (older prompt; run scan.py --redescribe-stale)",
                      lines[4])

    def test_command_line(self):
        for command in ("counts", "files"):
            result = run_script("progress.py", command)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"(all)", run_script("progress.py", "counts").stdout)
        # With no command: the counts too.
        self.assertIn(b"(all)", run_script("progress.py").stdout)
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

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TEST_SETTINGS_KEY"):
            os.environ.pop(name, None)

    def config_with_two_endpoints(self):
        """A config with the older round-robin "endpoint" list (and no "endpoints")."""
        config = common.default_config()
        del config["endpoints"]
        config["endpoint"] = [
            {"kind": "openai", "base_url": "http://127.0.0.1:8080/v1", "api_key": SECRET,
             "model": "first"},
            {"kind": "claude", "base_url": "", "api_key": "", "model": "second"},
        ]
        return config

    def test_endpoints_are_shown_without_keys(self):
        config = self.config_with_two_endpoints()
        text = "\n".join(settings.settings_lines(config))
        self.assertIn("LLM endpoints (default: default):", text)
        self.assertIn("1. default: Local server, model=first", text)
        self.assertIn("2. default-2: Claude, model=second", text)
        self.assertIn("API key: config", text)
        self.assertIn("API key: none", text)
        self.assertIn("group=default", text)
        self.assertNotIn(SECRET, text)
        # Showing does not convert the older form.
        self.assertIn("endpoint", config)
        self.assertNotIn("endpoints", config)
        with mock.patch("builtins.input", return_value=""):
            shown = output_of(settings.show_settings, config)
        self.assertNotIn(SECRET, shown)
        self.assertNotIn("0123456789", shown)

    def test_base_url_secrets_are_masked(self):
        config = {"endpoints": [{"name": "web", "kind": "openai", "model": "m",
                                 "base_url": "https://example.com/v1?key=abcdef123456"}]}
        endpoint = config["endpoints"][0]
        self.assertNotIn("abcdef123456", settings.endpoint_summary(endpoint))
        # Nor in the prompt that edits it; Enter keeps the value.
        prompts = []
        answers = iter(["4", "", "0"])

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        with mock.patch("builtins.input", fake_input):
            output = output_of(settings.edit_endpoint, config, endpoint)
        self.assertNotIn("abcdef123456", "".join(prompts) + output)
        self.assertTrue(any("Base URL" in prompt for prompt in prompts))
        self.assertEqual(endpoint["base_url"], "https://example.com/v1?key=abcdef123456")

    def test_add_remove_rename_and_default(self):
        config = common.default_config()
        self.assertEqual(settings.endpoint_entries(config), [])     # the placeholder is dropped
        self.assertNotIn("endpoint", config)
        local = settings.add_endpoint(config, "llamacpp")
        self.assertEqual(local, {"name": "llamacpp", "preset": "llamacpp", "model": ""})
        self.assertEqual(config["default_endpoint"], "llamacpp")
        cloud = settings.add_endpoint(config, "openai")
        again = settings.add_endpoint(config, "openai")
        self.assertEqual((cloud["name"], again["name"]), ("openai", "openai-2"))
        self.assertEqual(config["default_endpoint"], "llamacpp")
        for bad in ("OPENAI", "a,b", "a=b", " "):
            with self.assertRaises(ValueError):
                settings.add_endpoint(config, "openai", name=bad)
        with self.assertRaises(ValueError):
            settings.add_endpoint(config, "no-such-preset")
        config["libraries"] = [{"name": "Family", "path": "C:\\Photos\\Family",
                                "endpoint": "OpenAI"}]
        settings.rename_endpoint(config, "openai", "cloud")
        self.assertEqual(config["libraries"][0]["endpoint"], "cloud")
        settings.set_default_endpoint(config, "CLOUD")
        self.assertEqual(config["default_endpoint"], "cloud")
        with self.assertRaises(ValueError):
            settings.set_default_endpoint(config, "nothing")
        self.assertEqual([library["name"] for library in
                          settings.libraries_using(config, "cloud")], ["Family"])
        settings.remove_endpoint(config, "cloud")
        self.assertEqual(config["default_endpoint"], "llamacpp")
        # The library keeps naming it: its images are never sent elsewhere.
        self.assertEqual(config["libraries"][0]["endpoint"], "cloud")
        self.assertEqual(common.endpoints_for_library(config, config["libraries"][0]), [])
        with self.assertRaises(ValueError):
            settings.remove_endpoint(config, "cloud")

    def test_older_forms_are_converted_when_endpoints_are_edited(self):
        config = self.config_with_two_endpoints()
        entries = settings.endpoint_entries(config)
        self.assertNotIn("endpoint", config)
        self.assertEqual([entry["name"] for entry in entries], ["default", "default-2"])
        self.assertEqual({entry["group"] for entry in entries}, {"default"})
        self.assertEqual(config["default_endpoint"], "default")
        self.assertEqual(len(common.endpoints_for_library(config, None)), 2)
        self.base.write_config([])
        settings.save(config)
        saved = common.load_config()
        self.assertNotIn("endpoint", saved)
        self.assertEqual([entry["model"] for entry in saved["endpoints"]], ["first", "second"])
        # A single older endpoint keeps working the same way.
        single = common.default_config()
        del single["endpoints"]
        single["endpoint"] = {"kind": "openai", "base_url": "http://127.0.0.1:8080/",
                              "model": "only"}
        self.assertEqual([entry["name"] for entry in settings.endpoint_entries(single)],
                         ["default"])
        self.assertEqual(common.endpoints_for_library(single, None)[0]["model"], "only")

    def test_base_url_change_removes_a_typed_key(self):
        endpoint = {"name": "x", "preset": "openai", "model": "m", "api_key": SECRET}
        self.assertFalse(settings.set_base_url(endpoint, "https://api.openai.com/v1/"))
        self.assertEqual(endpoint["api_key"], SECRET)
        self.assertTrue(settings.set_base_url(endpoint, "https://elsewhere.example/v1"))
        self.assertNotIn("api_key", endpoint)
        endpoint.update(api_key_env="TEST_SETTINGS_KEY")
        self.assertFalse(settings.set_base_url(endpoint, ""))
        self.assertNotIn("base_url", endpoint)
        self.assertEqual(endpoint["api_key_env"], "TEST_SETTINGS_KEY")

    def test_preset_change_removes_a_typed_key(self):
        # A key typed for one service is never sent to another one.
        config = {"endpoints": [{"name": "cloud", "preset": "openai", "model": "m",
                                 "api_key": SECRET}]}
        endpoint = config["endpoints"][0]
        gemini = str(list(common.ENDPOINT_PRESETS).index("gemini") + 1)
        answers = iter(["2", gemini, "0"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output = output_of(settings.edit_endpoint, config, endpoint)
        self.assertEqual(endpoint["preset"], "gemini")
        self.assertNotIn("api_key", endpoint)
        self.assertIn("stored API key was removed", output)
        self.assertNotIn(SECRET, output)
        # The same address keeps it (a base URL of its own stays put).
        endpoint.update(api_key=SECRET, base_url="https://llm.example.com/v1")
        self.assertFalse(settings.set_preset(endpoint, "custom"))
        self.assertEqual(endpoint["api_key"], SECRET)
        with self.assertRaises(ValueError):
            settings.set_preset(endpoint, "no-such-preset")

    def test_removing_the_default_never_moves_libraries_to_the_cloud(self):
        config = {"endpoints": [{"name": "local", "preset": "llamacpp", "model": "m"},
                                {"name": "cloud", "preset": "anthropic", "model": "m"}],
                  "default_endpoint": "local",
                  "libraries": [{"name": "Family", "path": "C:\\Photos\\Family"}]}
        family = config["libraries"][0]
        self.assertEqual([e["name"] for e in common.endpoints_for_library(config, family)],
                         ["local"])
        # Endpoints menu: Remove, the first one, yes, Back.
        answers = iter(["4", "1", "y", "0"])
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        with mock.patch("builtins.input", fake_input):
            output_of(settings.configure_endpoints, config)
        question = [prompt for prompt in prompts if "Remove endpoint local?" in prompt][0]
        self.assertIn("It is the default endpoint (used by Family); no endpoint will be the "
                      "default", question)
        self.assertEqual(config["default_endpoint"], "")
        self.assertEqual(common.endpoints_for_library(config, family), [])
        # A cloud endpoint added now does not become the default by itself...
        settings.add_endpoint(config, "openai", name="cloud-2")
        self.assertEqual(config["default_endpoint"], "")
        # ...a local one does, and a local one takes over from a removed default.
        settings.add_endpoint(config, "llamacpp", name="gpu-a")
        settings.add_endpoint(config, "vllm", name="gpu-b")
        self.assertEqual(config["default_endpoint"], "gpu-a")
        settings.remove_endpoint(config, "gpu-a")
        self.assertEqual(config["default_endpoint"], "gpu-b")

    def test_a_key_typed_as_a_variable_name_is_not_saved(self):
        # Not a real key: a made-up value used only by this test.
        pasted = "AIzaSyFAKE-pasted-key-0123456789abcdefg"
        config = {"endpoints": [{"name": "g", "preset": "gemini", "model": "m"}]}
        endpoint = config["endpoints"][0]
        answers = iter(["5", "1", pasted, "0"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output = output_of(settings.edit_endpoint, config, endpoint)
        self.assertIn("looks like a key", output)
        self.assertNotIn("api_key_env", endpoint)
        # One saved by an older version is never shown.
        endpoint["api_key_env"] = pasted
        prompts = []
        answers = iter(["5", "1", "", "0"])

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        with mock.patch("builtins.input", fake_input):
            output = output_of(settings.edit_endpoint, config, endpoint)
        self.assertNotIn(pasted, output + "".join(prompts))
        self.assertIn("(not a valid name)", "".join(prompts))

    def test_key_file_in_the_program_folder_and_plain_http_are_warned_about(self):
        config = {"endpoints": [{"name": "c", "preset": "custom", "model": "m"}]}
        endpoint = config["endpoints"][0]
        inside = os.path.join(common.BASE_DIR, "my-test-key.txt")
        self.assertFalse(os.path.exists(inside))
        answers = iter(["5", "2", inside, "4", "http://llm.example.com/v1", "0"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output = output_of(settings.edit_endpoint, config, endpoint)
        self.assertIn("inside the program folder", output)
        self.assertIn("is not encrypted", output)
        self.assertFalse(os.path.exists(inside))

    def test_key_options_never_echo_the_key(self):
        config = {"endpoints": [{"name": "cloud", "preset": "openai", "model": "m"}]}
        endpoint = config["endpoints"][0]
        printed = []
        getpass_prompts = []

        def run(answers, secret=None):
            answers = iter(answers)

            def fake_getpass(prompt=""):
                getpass_prompts.append(prompt)
                return secret

            with mock.patch("builtins.input", lambda _prompt="": next(answers)), \
                    mock.patch("getpass.getpass", fake_getpass):
                printed.append(output_of(settings.edit_endpoint, config, endpoint))

        # API key, type it in, back.
        run(["5", "3", "0"], secret=SECRET)
        self.assertEqual(endpoint["api_key"], SECRET)
        self.assertEqual(settings.key_status(endpoint), "config")
        # API key, environment variable (the preset's is suggested), back.
        run(["5", "1", "", "0"])
        self.assertEqual(endpoint["api_key_env"], "OPENAI_API_KEY")
        self.assertNotIn("api_key", endpoint)
        self.assertEqual(settings.key_status(endpoint), "env OPENAI_API_KEY not set")
        os.environ["OPENAI_API_KEY"] = SECRET
        self.assertEqual(settings.key_status(endpoint), "env OPENAI_API_KEY")
        # API key, key file, back.
        key_file = self.base.write_file("cloud.key", SECRET)
        run(["5", "2", key_file, "0"])
        self.assertEqual((endpoint["api_key_file"], settings.key_status(endpoint)),
                         (key_file, "file"))
        self.assertNotIn("api_key_env", endpoint)
        run(["5", "4", "0"])
        self.assertEqual(settings.key_status(endpoint), "env OPENAI_API_KEY")
        for text in printed + getpass_prompts:
            self.assertNotIn(SECRET, text)

    def test_edit_endpoint_settings(self):
        config = {"endpoints": [{"name": "local", "preset": "llamacpp", "model": "m"}]}
        endpoint = config["endpoints"][0]
        # Concurrency 3, image edge 1024, timeout 60, tokens 1500, prices,
        # group, disable, then clear the concurrency again.
        answers = iter(["6", "3", "7", "1024", "8", "60", "9", "1500", "10", "0.1", "0.5",
                        "11", "pool", "12", "6", "-", "0"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output_of(settings.edit_endpoint, config, endpoint)
        self.assertEqual(endpoint, {"name": "local", "preset": "llamacpp", "model": "m",
                                    "max_image_edge": 1024, "timeout": 60.0,
                                    "max_output_tokens": 1500, "price_in": 0.1,
                                    "price_out": 0.5, "group": "pool", "enabled": False})
        self.assertEqual(settings.parse_number(" 7 "), 7)
        self.assertIsNone(settings.parse_number(""))
        with self.assertRaises(ValueError):
            settings.parse_number("x")
        with self.assertRaises(ValueError):
            settings.parse_number("0", minimum=1)

    def test_edit_endpoint_from_the_main_menu(self):
        self.base.write_config([], endpoint=self.config_with_two_endpoints()["endpoint"])
        # Endpoints, edit the second, Model, new name, Back, Back, Save.
        answers = iter(["1", "2", "3", "new-model", "0", "0", "5"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output_of(settings.main)
        with open(common.config_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertNotIn("endpoint", saved)
        self.assertEqual([entry["model"] for entry in saved["endpoints"]],
                         ["first", "new-model"])
        self.assertEqual(saved["default_endpoint"], "default")
        # Endpoints, Add, preset 1 (llama.cpp), Model, name, Back, Back, Save.
        answers = iter(["1", "3", "1", "3", "added-model", "0", "0", "5"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output_of(settings.main)
        added = common.get_endpoint(common.load_config(), "llamacpp")
        self.assertEqual((added["preset"], added["model"]), ("llamacpp", "added-model"))

    def test_library_endpoint_metadata_and_notes(self):
        config = common.default_config()
        settings.add_endpoint(config, "llamacpp", name="local")
        settings.add_library(config, "C:\\Photos\\Family")
        settings.set_library_endpoint(config, 0, "LOCAL")
        self.assertEqual(config["libraries"][0]["endpoint"], "local")
        with self.assertRaises(ValueError):
            settings.set_library_endpoint(config, 0, "elsewhere")
        settings.set_library_endpoint(config, 0, "")
        self.assertEqual(config["libraries"][0]["endpoint"], "")
        # Metadata: extended; notes; back.
        answers = iter(["5", "2", "6", "Photos from a sailing club.", "0"])
        with mock.patch("builtins.input", lambda _prompt="": next(answers)):
            output = output_of(settings.edit_library, config, 0)
        self.assertEqual(config["libraries"][0]["metadata"], "extended")
        self.assertEqual(config["libraries"][0]["context_notes"], "Photos from a sailing club.")
        self.assertIn("sent to the LLM service", output)

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

        # Not a real key: a made-up value used only by this test.
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

    def gradient(self):
        from PIL import Image
        source = Image.new("RGB", (1024, 512))
        source.putdata([(x % 256, y % 256, (x + y) % 256)
                        for y in range(512) for x in range(1024)])
        return source

    def test_an_interrupted_run_leaves_no_half_written_tile(self):
        import make_tiles
        source = self.gradient()
        real_replace = os.replace
        calls = []

        def interrupted(src, dst):
            calls.append(dst)
            if len(calls) == 3:
                raise KeyboardInterrupt()
            real_replace(src, dst)

        with mock.patch.object(make_tiles.os, "replace", side_effect=interrupted):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    make_tiles.slice_zoom(source, 1, 80, False)
        # The tile being written when it stopped is not there under its name
        # (its temporary file was removed on the way out); the ones before it
        # are whole.
        stopped = calls[2]
        self.assertFalse(os.path.exists(stopped))
        self.assertFalse(os.path.exists(stopped + ".part"))
        import imageinfo
        for done in calls[:2]:
            self.assertTrue(imageinfo.jpeg_file_complete(done), done)
        # A process that was killed outright leaves the temporary file: the
        # next run removes it and fills the gaps.
        with open(stopped + ".part", "wb") as handle:
            handle.write(b"\xff\xd8 cut short")
        self.assertEqual(make_tiles.remove_partial_files(), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            counts = make_tiles.slice_zoom(source, 1, 80, False)
        self.assertEqual(counts, {"written": 6, "repaired": 0, "kept": 2})
        self.assertFalse(os.path.exists(stopped + ".part"))
        for x in range(4):
            for y in range(2):
                self.assertTrue(imageinfo.jpeg_file_complete(make_tiles.tile_path(1, x, y)))

    def test_a_rerun_repairs_tiles_cut_short(self):
        import imageinfo
        import make_tiles
        source = self.gradient()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(make_tiles.slice_zoom(source, 1, 80, False),
                             {"written": 8, "repaired": 0, "kept": 0})
        # One tile cut short (as an older version left it when stopped while
        # writing in place), one emptied, one filled with zeros by the disk.
        paths = [make_tiles.tile_path(1, x, y) for x in range(4) for y in range(2)]
        with open(paths[0], "rb") as handle:
            whole = handle.read()
        with open(paths[0], "wb") as handle:
            handle.write(whole[:len(whole) // 2])
        open(paths[3], "wb").close()
        with open(paths[5], "wb") as handle:
            handle.write(b"\0" * len(whole))
        kept = {path: os.stat(path).st_mtime_ns for path in paths}
        self.assertFalse(imageinfo.jpeg_file_complete(paths[0]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            counts = make_tiles.slice_zoom(source, 1, 80, False)
        self.assertEqual(counts, {"written": 0, "repaired": 3, "kept": 5})
        self.assertIn("3 repaired", output.getvalue())
        for index, path in enumerate(paths):
            self.assertTrue(imageinfo.jpeg_file_complete(path), path)
            if index not in (0, 3, 5):
                self.assertEqual(os.stat(path).st_mtime_ns, kept[path], path)
        with open(paths[0], "rb") as handle:
            self.assertEqual(handle.read(), whole)
        # --force writes every tile again.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(make_tiles.slice_zoom(source, 1, 80, True)["written"], 8)

    def test_only_its_own_temporary_files_are_removed(self):
        import make_tiles
        tiles = common.ensure_dir(os.path.join(common.tiles_dir(), "2", "1"))
        for name in ("3.jpg.part", "notes.part", "3.jpg"):
            with open(os.path.join(tiles, name), "wb") as handle:
                handle.write(b"x")
        self.assertEqual(make_tiles.remove_partial_files(), 1)
        self.assertEqual(sorted(os.listdir(tiles)), ["3.jpg", "notes.part"])


class HelpTest(support.TempBaseMixin, unittest.TestCase):
    """--help says what a tool takes, and never starts the work."""

    def run_closed(self, name, *args):
        """Run a script with no input at all (stdin closed); help lines are not wrapped."""
        return subprocess.run([sys.executable, name] + list(args), cwd=support.REPO,
                              env=support.child_env(COLUMNS="200"), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_settings_help_prints_usage_without_the_menu(self):
        result = self.run_closed("settings.py", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"usage: settings.py", result.stdout)
        self.assertNotIn(b"Image database settings", result.stdout)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertFalse(os.path.exists(common.config_path()))
        # An option it does not know is an error, not a menu.
        result = self.run_closed("settings.py", "--library", "x")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b"Image database settings", result.stdout)

    def test_settings_menu_without_input_stops_cleanly(self):
        result = self.run_closed("settings.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"nothing was saved", result.stdout)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertFalse(os.path.exists(common.config_path()))

    def test_map_tools_name_their_formats_and_defaults(self):
        result = self.run_closed("world_map.py", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"JPEG, PNG", result.stdout)
        if support.pillow_available():
            result = self.run_closed("make_tiles.py", "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"(default: 82)", result.stdout)


class SettingsSaveCheckTest(support.TempBaseMixin, unittest.TestCase):
    """settings.py never silently undoes a change made elsewhere while it was open."""

    def setUp(self):
        super().setUp()
        self.family = self.base.library_folder("Family")
        self.base.write_config([support.library("Family", self.family, id="lib_family1")])

    def change_elsewhere(self):
        """What the Manage page does while settings.py waits at a prompt."""
        config = common.load_config()
        config["libraries"].append(support.library("AddedOnManagePage",
                                                   self.base.library_folder("Other"),
                                                   id="lib_other001"))
        config["libraries"][0]["export"] = {"location": True}
        common.save_config(config)

    def run_settings(self, answers):
        """settings.main with these answers; a callable answer runs, then answers its value."""
        answers = iter(answers)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            answer = next(answers)
            return answer() if callable(answer) else answer

        with mock.patch("builtins.input", fake_input):
            return output_of(settings.main), prompts

    def saved_names(self):
        return [library["name"] for library in common.load_config()["libraries"]]

    def elsewhere_then(self, answer):
        def step():
            self.change_elsewhere()
            return answer
        return step

    def test_nothing_changed_here_keeps_the_other_change(self):
        output, _prompts = self.run_settings([self.elsewhere_then("5")])
        self.assertIn("changed by something else", output)
        self.assertIn("left as it is", output)
        self.assertEqual(self.saved_names(), ["Family", "AddedOnManagePage"])
        self.assertEqual(common.load_config()["libraries"][0]["export"], {"location": True})

    def test_a_change_here_asks_first(self):
        new = self.base.library_folder("New")
        # Add a library here; meanwhile one is added elsewhere; Save and exit,
        # then "Exit without saving".
        output, _prompts = self.run_settings(["3", "1", new, "", "0",
                                              self.elsewhere_then("5"), "3"])
        self.assertIn("Nothing was saved", output)
        self.assertEqual(self.saved_names(), ["Family", "AddedOnManagePage"])
        # Back (0) returns to the menu; Reload (2) drops the change made
        # here, which is then made again and saved over the new file.
        self.base.write_config([support.library("Family", self.family, id="lib_family1")])
        output, _prompts = self.run_settings(["3", "1", new, "", "0",
                                              self.elsewhere_then("5"), "0",
                                              "5", "2", "3", "1", new, "", "0", "5"])
        self.assertIn("Reloaded", output)
        self.assertEqual(self.saved_names(), ["Family", "AddedOnManagePage", "New"])
        self.assertEqual(common.load_config()["libraries"][0]["export"], {"location": True})
        # Saving anyway (1) is a choice, never a surprise.
        self.base.write_config([support.library("Family", self.family, id="lib_family1")])
        self.run_settings(["3", "1", new, "", "0", self.elsewhere_then("5"), "1"])
        self.assertEqual(self.saved_names(), ["Family", "New"])

    def test_unchanged_file_saves_as_before(self):
        new = self.base.library_folder("New")
        output, _prompts = self.run_settings(["3", "1", new, "", "0", "5"])
        self.assertIn("Saved to", output)
        self.assertEqual(self.saved_names(), ["Family", "New"])

    def test_ctrl_c_at_a_prompt_saves_nothing_and_shows_no_traceback(self):
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as caught:
                output_of(settings.main)
        self.assertEqual(caught.exception.code, 130)


class SettingsInputTest(support.TempBaseMixin, unittest.TestCase):

    def test_quoted_folders_and_files(self):
        folder = self.base.library_folder("lib2")
        config = common.default_config()
        # Windows Explorer's "Copy as path" puts quotes around the path.
        library = settings.add_library(config, '"%s"' % folder)
        self.assertEqual(library["path"], folder)
        self.assertEqual(library["name"], "lib2")
        self.assertIsNone(settings.library_folder_error('"%s"' % folder))
        settings.set_library_folder(config, 0, "'%s'" % folder)
        self.assertEqual(config["libraries"][0]["path"], folder)
        # The same for the key file and the helper module.
        endpoint = {"name": "x", "preset": "custom", "model": "m"}
        key_file = self.base.write_file("cloud.key", SECRET)
        with mock.patch("builtins.input", side_effect=["2", '"%s"' % key_file]):
            output_of(settings.configure_key, endpoint)
        self.assertEqual(endpoint["api_key_file"], key_file)
        helper = self.base.write_file("helper.py", "")
        with mock.patch("builtins.input", return_value='"%s"' % helper):
            output_of(settings.configure_helper, config)
        self.assertEqual(config["helper"], helper)

    def test_testing_without_pillow_keeps_the_session(self):
        with mock.patch.dict(sys.modules, {"scan": None}):
            output = output_of(settings.test_endpoint_now, {"name": "x", "model": "m"})
        self.assertIn("Pillow", output)


class TileWriteTest(support.TempBaseMixin, unittest.TestCase):

    def test_a_tile_the_server_is_sending_is_replaced_after_a_moment(self):
        import make_tiles
        path = os.path.join(self.base.path, "0.jpg")
        with open(path, "wb") as handle:
            handle.write(b"old")
        real_replace = os.replace
        calls = []

        def busy_twice(src, dst):
            # Windows refuses to replace a file another process has open.
            calls.append(dst)
            if len(calls) <= 2:
                raise PermissionError(13, "Access is denied", dst)
            real_replace(src, dst)

        with mock.patch.object(make_tiles.os, "replace", side_effect=busy_twice):
            make_tiles.write_tile(path, b"new")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"new")
        self.assertEqual(len(calls), 3)
        self.assertFalse(os.path.exists(path + ".part"))


if __name__ == "__main__":
    unittest.main()
