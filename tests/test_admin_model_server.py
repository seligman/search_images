"""Tests for the Model server routes of admin.py (/api/admin/model-server...).

The server they start is tests/fake_model_server.py on a 127.0.0.1 port;
no model is loaded.
"""

import json
import os
import re
import threading
import time
import unittest
from unittest import mock

import support

import admin
import common
import db
import jobs
import local_server
import serve
from test_local_server import FAKE_SERVER, free_port, touch

try:
    import flask
except ImportError:
    flask = None

wait_for = support.wait_for


@unittest.skipIf(flask is None, "Flask is not installed")
class ModelServerRoutesTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.models = self.base.make_dir("models")
        for name in ("model.gguf", "model.mmproj.gguf", "sub/your-model.gguf",
                     "sub/mmproj-model.gguf"):
            touch(os.path.join(self.models, *name.split("/")))
        self.port = free_port()
        self.marker = self.base.join("marker.txt")
        for name, value in (("GRACE_SECONDS", 3.0), ("START_POLL", 0.1)):
            patcher = mock.patch.object(local_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.end_processes)
        self.write(with_server=True)
        db.init(log=support.quiet)
        self.manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(self.manager.stop, 30)
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def write(self, with_server=True, **server):
        # Not a real key: "hunter2-secret" is a made-up value used only by these tests.
        entry = {"enabled": True, "endpoint": "local", "models_dir": self.models,
                 "model": "model.gguf", "port": self.port, "stop_at": "03:30",
                 "launch_command": ["{python}", FAKE_SERVER, "--port", "{port}", "--marker",
                                    self.marker, "--api-key", "hunter2-secret"]}
        entry.update(server)
        extra = {"local_server": entry} if with_server else {}
        self.base.write_config([], endpoints=[
            {"name": "local", "preset": "llamacpp", "model": "m",
             "base_url": "http://127.0.0.1:%d/v1" % self.port}], default_endpoint="local",
            **extra)

    def end_processes(self):
        for record in local_server.load_state().values():
            if local_server.process_identity(record.get("pid")) is not None:
                local_server.kill_tree(record["pid"])

    def get(self, path, **kwargs):
        return self.client.get(admin.URL_PREFIX + path, headers={"X-Search-Images": "1"},
                               **kwargs)

    def post(self, path, body=None, headers=None):
        sent = {"X-Search-Images": "1"}
        sent.update(headers or {})
        return self.client.post(admin.URL_PREFIX + path, data=json.dumps(body or {}),
                                content_type="application/json", headers=sent)

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertNotIn("hunter2-secret", response.get_data(as_text=True))
        return response.get_json()

    def server(self):
        return self.ok(self.get("/model-server"))["servers"][0]

    def test_without_a_server_the_page_explains_the_setup(self):
        self.write(with_server=False)
        data = self.ok(self.get("/model-server"))
        self.assertEqual(data["servers"], [])
        self.assertEqual(data["setup"]["endpoint"], "local")
        self.assertEqual(data["setup"]["port"], self.port)
        snippet = data["setup"]["snippet"]
        self.assertTrue(snippet.startswith('"local_server": {'))
        example = json.loads(snippet.split(":", 1)[1])
        self.assertEqual((example["endpoint"], example["port"]), ("local", self.port))
        self.assertIn("binary", example)

    def test_the_status_lists_the_models_and_masks_secrets(self):
        server = wait_for(lambda: (lambda view: view if not view["choices"].get("checking")
                                   else None)(self.server()), 10)
        self.assertEqual(server["state"], "stopped")
        self.assertEqual(server["choices"]["models"], ["model.gguf", "sub/your-model.gguf"])
        self.assertEqual(server["choices"]["mmprojs"], ["model.mmproj.gguf",
                                                        "sub/mmproj-model.gguf"])
        self.assertEqual(server["mmproj_auto"], "model.mmproj.gguf")
        self.assertEqual(server["problem"], "")
        self.assertIn("--api-key ***", server["command"])
        self.assertIn("***", server["launch_command"])
        self.assertEqual(server["stop_at"], "03:30")

    def test_start_and_stop(self):
        data = self.ok(self.post("/model-server/start", {"endpoint": "local"}), 202)
        self.assertIn(data["server"]["state"], ("starting", "loading", "ready"))
        self.assertTrue(wait_for(lambda: self.server()["state"] == "ready", 20, 0.1))
        server = self.server()
        self.assertTrue(server["owned"])
        self.assertEqual(server["started_by"], "you")
        self.assertEqual(server["served"]["model"], "model.gguf")
        # A second start changes nothing; the port cannot change while it runs.
        self.ok(self.post("/model-server/start", {"endpoint": "local"}), 202)
        response = self.post("/model-server/settings", {"endpoint": "local",
                                                        "settings": {"port": self.port + 1}})
        self.assertEqual(response.status_code, 409)
        data = self.ok(self.post("/model-server/stop", {"endpoint": "local"}), 202)
        self.assertTrue(wait_for(lambda: self.server()["state"] == "stopped", 20, 0.1))
        self.assertIn("graceful", open(self.marker, encoding="utf-8").read())
        response = self.post("/model-server/stop", {"endpoint": "local"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.post("/model-server/start", {"endpoint": "other"}).status_code,
                         404)

    def test_a_start_that_cannot_work_says_why(self):
        self.write(model="your-model.gguf")
        response = self.post("/model-server/start", {"endpoint": "local"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("does not exist", response.get_json()["error"])

    def test_settings_the_page_may_change(self):
        token = self.ok(self.get("/model-server"))["config_token"]
        data = self.ok(self.post("/model-server/settings", {
            "endpoint": "local", "config_token": token,
            "settings": {"slots": "6", "ctx_per_slot": 4096, "stop_at": "4:05",
                         "auto_start": False, "auto_stop": True,
                         "model": "sub/your-model.gguf", "mmproj": ""}}))
        self.assertEqual(data["server"]["slots"], 6)
        stored = common.load_config()["local_server"]
        self.assertEqual((stored["slots"], stored["ctx_per_slot"], stored["stop_at"],
                          stored["auto_start"], stored["model"], stored["mmproj"]),
                         (6, 4096, "04:05", False, "sub/your-model.gguf", ""))
        # The program and the folders only in config.json.
        for field in ("binary", "models_dir", "launch_command", "extra_args", "endpoint",
                      "enabled"):
            response = self.post("/model-server/settings", {"endpoint": "local",
                                                            "settings": {field: "x"}})
            self.assertEqual(response.status_code, 400, field)
            self.assertIn("only be changed in config.json", response.get_json()["error"])
        # Only files that are in the models folder.
        for name, value in (("model", "../model.gguf"), ("model", "your-model.gguf"),
                            ("mmproj", "model.gguf"), ("slots", 0), ("port", 80),
                            ("ctx_per_slot", "lots"), ("stop_at", "25:00"),
                            ("auto_stop", "yes")):
            response = self.post("/model-server/settings", {"endpoint": "local",
                                                            "settings": {name: value}})
            self.assertEqual(response.status_code, 400, name)
            self.assertIn(name, response.get_json()["fields"])
        self.assertEqual(common.load_config()["local_server"]["model"], "sub/your-model.gguf")
        self.assertEqual(self.post("/model-server/settings",
                                   {"endpoint": "local", "settings": {}}).status_code, 400)

    def test_a_new_port_moves_the_endpoint_with_it(self):
        new_port = free_port()
        data = self.ok(self.post("/model-server/settings", {"endpoint": "local",
                                                            "settings": {"port": new_port}}))
        self.assertIn("now uses port %d" % new_port, data["notes"][0])
        config = common.load_config()
        self.assertEqual(config["local_server"]["port"], new_port)
        self.assertEqual(config["endpoints"][0]["base_url"],
                         "http://127.0.0.1:%d/v1" % new_port)
        self.assertEqual(self.server()["problem"], "")

    def test_the_setup_example_fits_the_computer(self):
        config = common.load_config()
        example = admin.model_server_example(config, windows=False)
        snippet = json.loads(example["snippet"].split(":", 1)[1])
        self.assertEqual((snippet["binary"], snippet["models_dir"]),
                         ("/path/to/llama-server", "/path/to/models"))
        self.assertFalse(example["windows"])
        self.assertEqual(example["config_path"], common.config_path())
        snippet = json.loads(admin.model_server_example(config, windows=True)["snippet"]
                             .split(":", 1)[1])
        self.assertTrue(snippet["binary"].startswith("C:\\path\\to\\"))

    def test_a_new_port_removes_a_typed_api_key(self):
        self.write(with_server=True)
        config = common.load_config()
        # Not a real key: a made-up value used only by this test.
        config["endpoints"][0]["api_key"] = "made-up-local-key-0101"
        common.save_config(config)
        new_port = free_port()
        data = self.ok(self.post("/model-server/settings", {"endpoint": "local",
                                                            "settings": {"port": new_port}}))
        self.assertTrue(any("API key was removed" in note for note in data["notes"]))
        endpoint = common.load_config()["endpoints"][0]
        self.assertNotIn("api_key", endpoint)
        self.assertEqual(endpoint["base_url"], "http://127.0.0.1:%d/v1" % new_port)

    def test_slow_model_files_do_not_hold_up_the_panel(self):
        release = threading.Event()
        self.addCleanup(release.set)
        real = local_server.file_facts

        def slow(settings):
            release.wait(30)            # (a models folder on a share that does not answer)
            return real(settings)
        with mock.patch.object(local_server, "file_facts", side_effect=slow), \
                mock.patch.object(local_server, "model_choices",
                                  side_effect=lambda settings: release.wait(30) and None):
            started = time.monotonic()
            server = self.server()
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(server["checking"])
            self.assertEqual(server["state"], "stopped")
            response = self.post("/model-server/start", {"endpoint": "local"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("still being looked at", response.get_json()["error"])
            release.set()
            self.assertTrue(wait_for(lambda: not self.server()["checking"], 10, 0.1))
        self.assertFalse(os.path.exists(self.marker))

    def test_the_routes_are_admin_only(self):
        response = self.client.get(admin.URL_PREFIX + "/model-server")
        self.assertEqual(response.status_code, 403)
        response = self.get("/model-server/start")
        self.assertEqual(response.status_code, 405)
        response = self.client.post(admin.URL_PREFIX + "/model-server/start",
                                    data=json.dumps({"endpoint": "local"}),
                                    content_type="application/json",
                                    headers={"X-Search-Images": "1",
                                             "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(os.path.exists(self.marker))

    def test_the_panel_script_builds_its_nodes_from_text(self):
        with open(os.path.join(support.REPO, "static", "model-server.js"),
                  encoding="utf-8") as handle:
            code = support.code_only(handle.read())
        for pattern in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML",
                        r"document\.write", r"\beval\s*\(", r"\bnew\s+Function\b",
                        r"=>", r"\blet\s+[A-Za-z_$]", r"\bconst\s+[A-Za-z_$]", r"`",
                        r"\.setAttribute\(\s*\"on", r"javascript:"):
            self.assertIsNone(re.search(pattern, code), pattern)

    def test_the_settings_page_has_the_panel(self):
        response = self.client.get("/settings")
        html = response.get_data(as_text=True)
        self.assertIn('id="model-server"', html)
        self.assertIn("/static/model-server.js", html)


if __name__ == "__main__":
    unittest.main()
