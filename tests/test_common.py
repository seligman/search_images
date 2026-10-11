"""Tests for common.py: paths, config, library helpers, ignore rules, redact."""

import contextlib
import io
import json
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


KEY_VARIABLES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                 "OPENROUTER_API_KEY", "AZURE_OPENAI_API_KEY", "TEST_SEARCH_IMAGES_KEY")


def private_address(*parts):
    """A private-network IPv4 address, built at run time (the repo holds no literal ones)."""
    return ".".join(str(part) for part in parts)


class EndpointConfigTest(support.TempBaseMixin, unittest.TestCase):

    def test_legacy_single_endpoint(self):
        self.base.write_config([support.library("Family", "C:\\Photos\\Family")],
                               endpoint={"kind": "openai", "base_url": "http://127.0.0.1:8080/v1",
                                         "model": "m"})
        config = common.load_config()
        [endpoint] = common.endpoint_list(config)
        self.assertEqual((endpoint["name"], endpoint["group"], endpoint["preset"],
                          endpoint["kind"]), ("default", "", "local", "openai"))
        self.assertEqual((endpoint["max_image_edge"], endpoint["timeout"],
                          endpoint["max_output_tokens"], endpoint["concurrency"],
                          endpoint["enabled"], endpoint["extra"]),
                         (1536, 180, 4000, None, True, {}))
        self.assertEqual(common.default_endpoint_name(config), "default")
        self.assertEqual(common.endpoints_for_library(config, config["libraries"][0]),
                         [endpoint])
        self.assertEqual(common.endpoints_for_library(config), [endpoint])
        self.assertEqual(common.get_endpoint(config, "DEFAULT"), endpoint)
        self.assertIsNone(common.get_endpoint(config, "other"))

    def test_legacy_round_robin_list(self):
        self.base.write_config([], endpoint=[{"model": "a", "base_url": "http://127.0.0.1:1/v1"},
                                             {"model": "b", "base_url": "http://127.0.0.1:2/v1"},
                                             {"kind": "ollama", "model": "c"}])
        config = common.load_config()
        endpoints = common.endpoint_list(config)
        self.assertEqual([e["name"] for e in endpoints], ["default", "default-2", "default-3"])
        self.assertEqual({e["group"] for e in endpoints}, {"default"})
        self.assertEqual([e["preset"] for e in endpoints], ["local", "local", "ollama"])
        # Libraries share the work between all of them, as before.
        self.assertEqual([e["model"] for e in common.endpoints_for_library(config, {})],
                         ["a", "b", "c"])
        # A single member can still be picked by name.
        self.assertEqual([e["model"] for e in common.resolve_endpoints(config, "default-2")],
                         ["b"])
        # The config itself is not changed.
        self.assertNotIn("name", config["endpoint"][0])

    def test_endpoints_list_groups_and_names(self):
        config = {
            "endpoints": [
                {"name": "local", "base_url": "http://127.0.0.1:8080/v1", "model": "m"},
                {"name": "cloud", "preset": "anthropic", "model": "claude-haiku-5-5",
                 "enabled": False},
                {"name": "gpu-a", "group": "gpus", "base_url": "http://gpu-a:8080/v1"},
                {"name": "gpu-b", "group": "GPUs", "base_url": "http://gpu-b:8080/v1"},
                {"name": "gpu-c", "group": "gpus", "enabled": False},
                {"model": "unnamed"},
                {"name": "LOCAL", "model": "twin"},
                "not an endpoint",
            ],
            "default_endpoint": "gpus",
        }
        names = [e["name"] for e in common.endpoint_list(config)]
        self.assertEqual(names, ["local", "cloud", "gpu-a", "gpu-b", "gpu-c", "endpoint-6",
                                 "LOCAL-2"])

        def picked(library):
            return [e["name"] for e in common.endpoints_for_library(config, library)]

        self.assertEqual(picked({}), ["gpu-a", "gpu-b"])
        self.assertEqual(picked({"endpoint": ""}), ["gpu-a", "gpu-b"])
        self.assertEqual(picked({"endpoint": "Local"}), ["local"])
        self.assertEqual(picked({"endpoint": "LOCAL-2"}), ["LOCAL-2"])
        # Disabled or unknown endpoints give nothing: never some other endpoint.
        self.assertEqual(picked({"endpoint": "cloud"}), [])
        self.assertEqual(picked({"endpoint": "renamed"}), [])
        config["default_endpoint"] = ""
        self.assertEqual(common.default_endpoint_name(config), "local")
        config["endpoints"][0]["enabled"] = False
        self.assertEqual(common.default_endpoint_name(config), "gpu-a")
        self.assertEqual(common.endpoint_list({"endpoints": []}), [])
        self.assertEqual(common.endpoints_for_library({"endpoints": []}, {}), [])

    def test_saving_keeps_the_form(self):
        self.base.write_config([], endpoint={"kind": "ollama", "model": "m"})
        config = common.load_config()
        common.endpoint_list(config)
        common.save_config(config)
        with open(common.config_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertNotIn("endpoints", saved)
        self.assertEqual(saved["endpoint"]["model"], "m")
        self.base.write_config([], endpoints=[{"name": "local", "model": "m"}],
                               default_endpoint="local")
        config = common.load_config()
        self.assertNotIn("endpoint", config)
        common.endpoint_list(config)
        common.save_config(config)
        with open(common.config_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertNotIn("endpoint", saved)
        self.assertEqual(saved["endpoints"], [{"name": "local", "model": "m"}])
        self.assertEqual(saved["default_endpoint"], "local")

    def test_converting_to_the_endpoints_list(self):
        single = {"endpoint": {"kind": "openai", "base_url": "http://127.0.0.1:8080/v1",
                               "api_key": "", "model": "m"}, "libraries": []}
        before = [e["model"] for e in common.endpoints_for_library(single, {})]
        self.assertTrue(common.convert_legacy_endpoints(single))
        self.assertNotIn("endpoint", single)
        self.assertEqual(single["endpoints"], [{"kind": "openai", "name": "default",
                                                "base_url": "http://127.0.0.1:8080/v1",
                                                "api_key": "", "model": "m"}])
        self.assertEqual(single["default_endpoint"], "default")
        self.assertEqual([e["model"] for e in common.endpoints_for_library(single, {})], before)
        self.assertFalse(common.convert_legacy_endpoints(single))
        # A round-robin list stays one group; the unused placeholder goes.
        listed = {"endpoint": [{"model": "a"}, {"model": "", "base_url": "", "api_key": ""},
                               {"kind": "ollama", "model": "c"}], "default_endpoint": ""}
        before = [e["name"] for e in common.endpoints_for_library(listed, {})
                  if e["model"]]
        common.convert_legacy_endpoints(listed)
        self.assertEqual([(e["name"], e["group"]) for e in listed["endpoints"]],
                         [("default", "default"), ("default-3", "default")])
        self.assertEqual([e["name"] for e in common.endpoints_for_library(listed, {})], before)
        placeholder = common.default_config()
        common.convert_legacy_endpoints(placeholder)
        self.assertEqual(placeholder["endpoints"], [])

    def test_presets_are_inferred(self):
        cases = [
            ({"kind": "claude"}, "anthropic"),
            ({"kind": "ollama", "base_url": "http://127.0.0.1:11434"}, "ollama"),
            ({"kind": "openai", "base_url": ""}, "openai"),
            ({}, "openai"),
            ({"base_url": "http://127.0.0.1:8080/v1"}, "local"),
            ({"base_url": "http://localhost:8080/v1"}, "local"),
            ({"base_url": "http://[::1]:8080/v1"}, "local"),
            ({"base_url": "http://%s:8080/v1" % private_address(192, 168, 0, 9)}, "local"),
            ({"base_url": "http://%s:8080" % private_address(10, 1, 2, 3)}, "local"),
            ({"base_url": "http://gpu-box:8080/v1"}, "local"),
            ({"base_url": "http://server.local:8080/v1"}, "local"),
            ({"base_url": "https://api.openai.com/v1"}, "openai"),
            ({"base_url": "https://example.openai.azure.com/openai/v1"}, "azure"),
            ({"base_url": "https://generativelanguage.googleapis.com/v1beta/openai"}, "gemini"),
            ({"base_url": "https://openrouter.ai/api/v1"}, "openrouter"),
            ({"base_url": "https://api.anthropic.com"}, "anthropic"),
            ({"base_url": "https://llm.example.com/v1"}, "custom"),
            ({"preset": "vllm", "base_url": "https://llm.example.com/v1"}, "vllm"),
            ({"preset": "LMStudio"}, "lmstudio"),
            ({"preset": "no-such-preset", "kind": "claude"}, "anthropic"),
        ]
        for endpoint, preset in cases:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(common.infer_preset(endpoint), preset)

    def test_settings_are_filled_and_checked(self):
        settings = common.endpoint_settings({"preset": "anthropic", "kind": "openai",
                                             "model": "claude-opus-5-5"})
        # An explicit preset decides the kind and fills the base URL.
        self.assertEqual((settings["kind"], settings["base_url"]),
                         ("claude", "https://api.anthropic.com"))
        self.assertEqual(settings["max_output_tokens"], 8000)
        self.assertEqual(settings["concurrency"], 4)
        self.assertEqual(common.endpoint_settings(settings), settings)
        self.assertEqual(common.endpoint_settings({"kind": "gemini"})["kind"], "openai")
        self.assertEqual(common.endpoint_settings({"base_url": "https://api.anthropic.com/"})
                         ["kind"], "claude")
        bad = common.endpoint_settings({
            "name": 5, "max_image_edge": "big", "timeout": -1, "max_output_tokens": 0,
            "concurrency": True, "price_in": "cheap", "price_out": float("nan"), "extra": [],
            "enabled": 0, "base_url": "http://127.0.0.1:8080/v1/"})
        self.assertEqual((bad["name"], bad["max_image_edge"], bad["timeout"],
                          bad["max_output_tokens"], bad["concurrency"], bad["price_in"],
                          bad["price_out"], bad["extra"], bad["enabled"], bad["base_url"]),
                         ("", 1536, 180, 4000, None, None, None, {}, False,
                          "http://127.0.0.1:8080/v1"))
        good = common.endpoint_settings({"max_image_edge": 1024, "timeout": 30.5,
                                         "price_in": 0.1, "price_out": 1, "label": "Mine"})
        self.assertEqual((good["max_image_edge"], good["timeout"], good["price_in"],
                          good["price_out"], good["label"]), (1024, 30.5, 0.1, 1.0, "Mine"))
        typed = common.endpoint_settings({"base_url": " localhost:8080/v1/ "})
        self.assertEqual((typed["base_url"], typed["preset"]),
                         ("http://localhost:8080/v1", "local"))

    def test_scheme_of_a_typed_base_url(self):
        # Without a scheme, only hosts on the local network get http://: the
        # key and the pictures never cross the internet unencrypted.
        cases = [
            ({"base_url": "localhost:8080/v1"}, "http://localhost:8080/v1"),
            ({"base_url": "gpu-box:8080/v1"}, "http://gpu-box:8080/v1"),
            ({"base_url": "%s:8080/v1" % private_address(10, 0, 0, 5)},
             "http://%s:8080/v1" % private_address(10, 0, 0, 5)),
            ({"base_url": "openrouter.ai/api/v1"}, "https://openrouter.ai/api/v1"),
            ({"base_url": "api.openai.com/v1"}, "https://api.openai.com/v1"),
            ({"kind": "claude", "base_url": "api.anthropic.com"}, "https://api.anthropic.com"),
            ({"preset": "azure", "base_url": "res.openai.azure.com/openai/v1"},
             "https://res.openai.azure.com/openai/v1"),
            ({"base_url": "llm.example.com/v1"}, "https://llm.example.com/v1"),
            # The cloud services' own hosts only serve https.
            ({"base_url": "http://api.openai.com/v1"}, "https://api.openai.com/v1"),
            ({"kind": "claude", "base_url": "HTTP://api.anthropic.com"},
             "https://api.anthropic.com"),
            # An explicit http:// elsewhere is kept (and reported as insecure).
            ({"base_url": "http://llm.example.com/v1"}, "http://llm.example.com/v1"),
        ]
        for endpoint, url in cases:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(common.endpoint_settings(endpoint)["base_url"], url)
        self.assertTrue(common.insecure_endpoint({"base_url": "http://llm.example.com/v1"}))
        for endpoint in ({"base_url": "https://llm.example.com/v1"},
                         {"base_url": "http://127.0.0.1:8080/v1"}, {"preset": "llamacpp"},
                         {"base_url": "http://nas.lan:8080"}, {"preset": "openai"}):
            self.assertFalse(common.insecure_endpoint(endpoint), endpoint)

    def test_default_endpoint_is_never_a_cloud_service_by_accident(self):
        cloud = {"name": "cloud", "preset": "anthropic", "model": "m"}
        local = {"name": "local", "preset": "llamacpp", "model": "m"}
        # Without a default_endpoint, the first local endpoint is used...
        self.assertEqual(common.default_endpoint_name({"endpoints": [cloud, local]}), "local")
        # ...and never a cloud service nobody chose.
        self.assertEqual(common.default_endpoint_name({"endpoints": [cloud]}), "")
        self.assertEqual(common.endpoints_for_library({"endpoints": [cloud]}, {}), [])
        self.assertEqual(common.default_endpoint_name(
            {"endpoints": [cloud, local], "default_endpoint": "cloud"}), "cloud")

    def test_local_endpoints(self):
        local = ["http://127.0.0.1:8080/v1", "http://localhost:11434",
                 "http://%s:1234/v1" % private_address(172, 16, 4, 2), "http://gpu-box:8000/v1",
                 "http://nas.lan:8080"]
        for url in local:
            self.assertTrue(common.is_local_endpoint({"base_url": url}), url)
            self.assertTrue(common.is_local_endpoint({"preset": "vllm", "base_url": url}), url)
        self.assertTrue(common.is_local_endpoint({"preset": "llamacpp"}))
        self.assertTrue(common.is_local_endpoint({"kind": "ollama"}))
        for endpoint in ({"kind": "claude"}, {}, {"preset": "gemini"},
                         {"base_url": "https://llm.example.com/v1"},
                         {"preset": "llamacpp", "base_url": "https://gpu.example.com/v1"},
                         {"preset": "openai", "base_url": "http://127.0.0.1:8080/v1"}):
            self.assertFalse(common.is_local_endpoint(endpoint), endpoint)

    def test_concurrency(self):
        concurrency = common.endpoint_concurrency
        self.assertEqual(concurrency({"preset": "llamacpp"}), 1)
        self.assertEqual(concurrency({"preset": "llamacpp"}, slots=4), 4)
        self.assertEqual(concurrency({"base_url": "http://127.0.0.1:8080/v1"}, slots=2), 2)
        self.assertEqual(concurrency({"preset": "llamacpp", "concurrency": 3}, slots=4), 3)
        self.assertEqual(concurrency({"preset": "openai"}), 4)
        self.assertEqual(concurrency({"kind": "claude"}, slots=9), 4)
        self.assertEqual(concurrency({"kind": "ollama"}), 1)
        self.assertEqual(concurrency({"preset": "lmstudio"}, slots=4), 1)
        self.assertEqual(concurrency({"concurrency": 1000}), common.MAX_CONCURRENCY)
        self.assertEqual(concurrency({"preset": "llamacpp"}, slots=1000), common.MAX_CONCURRENCY)

    def test_claude_thinking(self):
        cases = {"claude-haiku-5-5": "disabled", "Claude-Haiku-5-5-20260101": "disabled",
                 "claude-sonnet-5-5": "between_tools", "claude-opus-5-5": "effort",
                 "claude-fable-5-1": "effort", "claude-haiku-5-50": "", "": "", None: ""}
        for model, mode in cases.items():
            self.assertEqual(common.claude_thinking(model), mode, model)


class ApiKeyTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in KEY_VARIABLES:
            os.environ.pop(name, None)

    def test_lookup_order(self):
        key_file = self.base.write_file("keys/llm.key", "file-key-value\n")
        endpoint = {"preset": "openai", "api_key_env": "TEST_SEARCH_IMAGES_KEY",
                    "api_key_file": key_file, "api_key": "config-key-value"}
        os.environ["TEST_SEARCH_IMAGES_KEY"] = "env-key-value"
        os.environ["OPENAI_API_KEY"] = "preset-key-value"
        resolve = common.resolve_api_key
        self.assertEqual(resolve(endpoint), ("env-key-value", "env TEST_SEARCH_IMAGES_KEY"))
        os.environ["TEST_SEARCH_IMAGES_KEY"] = "  "
        self.assertEqual(resolve(endpoint), ("file-key-value", "file"))
        os.remove(key_file)
        # A missing key file is never replaced by another key.
        self.assertEqual(resolve(endpoint), ("", "file missing"))
        del endpoint["api_key_file"]
        self.assertEqual(resolve(endpoint), ("config-key-value", "config"))
        del endpoint["api_key"]
        # A named variable that is not set is reported, not swapped for the
        # preset's usual variable.
        self.assertEqual(resolve(endpoint), ("", "env TEST_SEARCH_IMAGES_KEY not set"))
        del endpoint["api_key_env"]
        self.assertEqual(resolve(endpoint), ("preset-key-value", "env OPENAI_API_KEY"))
        self.assertEqual(resolve({"kind": "claude"}), ("", "none"))
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-key-value"
        self.assertEqual(resolve({"kind": "claude"}), ("anthropic-key-value",
                                                       "env ANTHROPIC_API_KEY"))
        # Local servers have no usual variable.
        self.assertEqual(resolve({"base_url": "http://127.0.0.1:8080/v1"}), ("", "none"))
        # An empty endpoint is OpenAI's.
        self.assertEqual(resolve(None), ("preset-key-value", "env OPENAI_API_KEY"))
        del os.environ["OPENAI_API_KEY"]
        self.assertEqual(resolve(None), ("", "none"))

    def test_usual_variable_only_for_the_presets_own_service(self):
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-key-value"
        os.environ["OPENAI_API_KEY"] = "openai-key-value"
        os.environ["AZURE_OPENAI_API_KEY"] = "azure-key-value"
        resolve = common.resolve_api_key
        # A gateway or proxy at another address never gets the user's key.
        for endpoint in ({"kind": "claude", "base_url": "http://%s:4000"
                          % private_address(192, 168, 1, 50)},
                         {"kind": "claude", "base_url": "https://gateway.example.com",
                          "api_key": ""},
                         {"preset": "openai", "base_url": "https://llm.example.com/v1"},
                         {"preset": "anthropic", "base_url": "https://api.anthropic.com.evil.test"},
                         {"preset": "azure"}):
            self.assertEqual(resolve(endpoint), ("", "none"), endpoint)
        self.assertEqual(resolve({"kind": "claude", "base_url": "https://api.anthropic.com/"}),
                         ("anthropic-key-value", "env ANTHROPIC_API_KEY"))
        self.assertEqual(resolve({"preset": "openai", "base_url": "api.openai.com/v1"}),
                         ("openai-key-value", "env OPENAI_API_KEY"))
        self.assertEqual(resolve({"preset": "azure",
                                  "base_url": "https://res.openai.azure.com/openai/v1"}),
                         ("azure-key-value", "env AZURE_OPENAI_API_KEY"))
        # A gateway can still name the variable itself.
        self.assertEqual(resolve({"kind": "claude", "base_url": "https://gateway.example.com",
                                  "api_key_env": "ANTHROPIC_API_KEY"}),
                         ("anthropic-key-value", "env ANTHROPIC_API_KEY"))

    def test_a_key_pasted_as_a_variable_name_is_never_shown(self):
        for pasted in ("AIzaSyFAKE-pasted-key-0123456789abcdefg", "sk-proj-pastedvalue",
                       "AIzaSyFAKEpastedkey0123456789abcdefgh", "Xy7kQ2mR9pL4vN8wT3zB6cD1",
                       "has space", "1STARTS_WITH_DIGIT"):
            with self.subTest(pasted=pasted[:6]):
                os.environ[pasted.replace(" ", "_")] = "should-not-be-used"
                self.assertEqual(common.resolve_api_key({"api_key_env": pasted}),
                                 ("", "env (invalid name) not set"))
                self.assertFalse(common.valid_env_name(pasted))
        for name in ("OPENAI_API_KEY", "my_key", "_X", "KEY2"):
            self.assertTrue(common.valid_env_name(name), name)

    def test_key_files(self):
        self.base.write_file("secret.key", b"\xef\xbb\xbf  file-key-value  \r\n\r\nsecond line\n")
        # A relative path is next to config.json, wherever the current folder is.
        self.assertEqual(common.resolve_api_key({"api_key_file": "secret.key"}),
                         ("file-key-value", "file"))
        self.base.write_file("empty.key", "\n  \n")
        self.assertEqual(common.resolve_api_key({"api_key_file": "empty.key"}),
                         ("", "file empty"))
        self.base.write_file("binary.key", b"\xff\xfe\x00\x01")
        self.assertEqual(common.resolve_api_key({"api_key_file": "binary.key"}),
                         ("", "file missing"))
        self.assertEqual(common.resolve_api_key({"api_key_file": self.base.path}),
                         ("", "file missing"))

    def test_sources_never_contain_the_key(self):
        key = "do-not-show-this-key"
        os.environ["TEST_SEARCH_IMAGES_KEY"] = key
        self.base.write_file("k.txt", key)
        for endpoint in ({"api_key_env": "TEST_SEARCH_IMAGES_KEY"}, {"api_key_file": "k.txt"},
                         {"api_key": key}):
            found, source = common.resolve_api_key(endpoint)
            self.assertEqual(found, key)
            self.assertNotIn(key, source)
            self.assertEqual(common.redact("rejected key %s" % found, [found]),
                             "rejected key " + common.REDACTED)


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

    def test_thread_safe_helper(self):
        import threading
        import time as time_module
        state = {"now": 0, "most": 0}
        lock = threading.Lock()

        class Helper:
            @staticmethod
            def load_image(path):
                with lock:
                    state["now"] += 1
                    state["most"] = max(state["most"], state["now"])
                time_module.sleep(0.02)
                with lock:
                    state["now"] -= 1
                return path.encode()

            @staticmethod
            def call_api(prompt, image_path):
                return None

        wrapped = common.thread_safe_helper(Helper)
        threads = [threading.Thread(target=wrapped.load_image, args=("p%d" % n,))
                   for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(state["most"], 1)
        self.assertEqual(wrapped.load_image("x"), b"x")
        # Only the reading hooks are wrapped.
        self.assertFalse(hasattr(wrapped, "call_api"))
        self.assertFalse(hasattr(wrapped, "stat"))
        Helper.THREAD_SAFE = True
        self.assertIs(common.thread_safe_helper(Helper), Helper)
        self.assertIsNone(common.thread_safe_helper(None))

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
