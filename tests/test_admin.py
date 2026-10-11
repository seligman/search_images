"""Tests for admin.py: the admin API behind the Manage and Settings pages.

Everything runs through Flask's test client inside a temporary
SETTINGS_BASE; the only network traffic goes to the fake LLM server on
127.0.0.1.
"""

import contextlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

import support

import admin
import cache
import common
import db
import jobs
import library_ops
import serve

try:
    import flask
except ImportError:
    flask = None

if support.pillow_available():
    import fake_llm
    import imagegen
    import llm
else:
    fake_llm = imagegen = llm = None

# None of these are real keys: made-up sentinel values used to check that keys never leak.
FILE_KEY = "sk-sentinel-file-key-0123456789abcdef"
LITERAL_KEY = "sk-sentinel-literal-key-abcdef0123456789"
ENV_KEY = "sk-test-sentinel-env-key-9876543210fedcba"
NEW_KEY = "sk-test-sentinel-new-key-55555555555555555"
# Not a real key: a made-up value with the 32 hex digits of older Azure OpenAI keys.
HEX_KEY = "fa1afe1c0ffee0123456789abcdef012"
SENTINELS = (FILE_KEY, LITERAL_KEY, ENV_KEY, NEW_KEY, HEX_KEY)
NOW = 1700000000.0


quiet = support.quiet


wait_for = support.wait_for


def insert_image(conn, library_id, path, **fields):
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 64, "height": 48,
           "added": NOW, "updated": NOW}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute(
        "INSERT INTO images (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
        [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


page_config = support.page_config


def gate_runner(started, release):
    """A stand-in job runner that works until release is set (honouring pause and cancel)."""
    def run(ctx, conn, config, params, helper=None):
        ctx.stage("scan", total=10)
        started.set()
        while not release.is_set() and not ctx.should_stop():
            ctx.wait_if_paused()
            time.sleep(0.01)
        return {}
    return run


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminFixture(support.TempBaseMixin):
    """A library with a few rows, endpoints with keys from a file, config and env."""

    def setUp(self):
        super().setUp()
        # Key files live outside the program folder and the data folder.
        self.keys = tempfile.mkdtemp(prefix="search-images-keys-")
        self.addCleanup(support.remove_tree, self.keys)
        self.key_file = os.path.join(self.keys, "cloud.key")
        with open(self.key_file, "w", encoding="utf-8") as handle:
            handle.write(FILE_KEY + "\n")
        patcher = mock.patch.dict(os.environ, {"SEARCH_IMAGES_TEST_KEY": ENV_KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.family = self.base.library_folder("Family")
        self.server = None
        self.endpoints = [
            {"name": "local", "preset": "llamacpp", "base_url": "http://127.0.0.1:9/v1",
             "model": "model.gguf", "api_key": LITERAL_KEY},
            {"name": "cloud", "preset": "openai", "model": "gpt-test",
             "api_key_file": self.key_file},
            {"name": "env", "preset": "custom", "base_url": "https://llm.example.com/v1",
             "model": "m", "api_key_env": "SEARCH_IMAGES_TEST_KEY"},
        ]
        self.write_config()
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(self.manager.stop, 30)
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        self.responses = []

    def write_config(self, libraries=None, **extra):
        if libraries is None:
            libraries = [support.library("Family", self.family, id="lib_family1")]
        extra.setdefault("endpoints", self.endpoints)
        extra.setdefault("default_endpoint", "local")
        self.base.write_config(libraries, **extra)

    def remember(self, response):
        self.responses.append(response.get_data(as_text=True))
        return response

    def get(self, path, headers=None, **kwargs):
        sent = {"X-Search-Images": "1"}
        sent.update(headers or {})
        return self.remember(self.client.get(admin.URL_PREFIX + path, headers=sent, **kwargs))

    def post(self, path, body=None, headers=None, **kwargs):
        sent = {"X-Search-Images": "1"}
        sent.update(headers or {})
        return self.remember(self.client.post(
            admin.URL_PREFIX + path, data=json.dumps({} if body is None else body),
            content_type="application/json", headers=sent, **kwargs))

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        return response.get_json()

    def config(self):
        return common.load_config()

    def library(self, library_id):
        return next(item for item in self.config()["libraries"] if item["id"] == library_id)

    def tearDown(self):
        # No answer of any admin route ever contains an API key.
        for text in self.responses:
            for key in SENTINELS:
                self.assertNotIn(key, text)
        super().tearDown()


class SecurityTest(AdminFixture, unittest.TestCase):

    def test_requests_must_come_from_this_computer_with_the_header(self):
        self.ok(self.get("/state"))
        response = self.client.get("/api/admin/state")
        self.assertEqual(response.status_code, 403)
        self.assertIn("header", response.get_json()["error"])
        response = self.get("/state", environ_base={"REMOTE_ADDR": "192.0.2.5"})
        self.assertEqual(response.status_code, 403)
        self.ok(self.get("/state", environ_base={"REMOTE_ADDR": "::1"}))
        self.ok(self.get("/state", environ_base={"REMOTE_ADDR": "::ffff:127.0.0.1"}))
        response = self.post("/paths/check", {"path": self.family},
                             environ_base={"REMOTE_ADDR": "192.0.2.5"})
        self.assertEqual(response.status_code, 403)

    def test_cross_site_requests_are_refused(self):
        for site, status in (("cross-site", 403), ("same-site", 403), ("same-origin", 200),
                             ("none", 200)):
            response = self.get("/state", headers={"Sec-Fetch-Site": site})
            self.assertEqual(response.status_code, status, site)
        # Only the page's own origin: the same host name and port as the request.
        for origin, status in (("http://attacker.example", 403), ("null", 403),
                               ("http://localhost", 200), ("https://localhost", 200),
                               ("http://localhost:9999", 403), ("http://127.0.0.1", 403)):
            response = self.get("/state", headers={"Origin": origin})
            self.assertEqual(response.status_code, status, origin)
        for host, origin, status in (("127.0.0.1:5000", "http://127.0.0.1:5000", 200),
                                     ("127.0.0.1:5000", "http://127.0.0.1:1", 403),
                                     ("[::1]:5000", "http://[::1]:5000", 200)):
            response = self.post("/fs/list", {"path": ""},
                                 headers={"Origin": origin, "Host": host})
            self.assertEqual(response.status_code, status, (host, origin))
        response = self.get("/state", headers={"Host": "attacker.example"})
        self.assertEqual(response.status_code, 400)

    def test_bodies_must_be_json_and_paths_are_post_only(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data"):
            response = self.client.post("/api/admin/paths/check",
                                        data='{"path": "x"}', content_type=content_type,
                                        headers={"X-Search-Images": "1"})
            self.assertEqual(response.status_code, 415, content_type)
        for path in ("/paths/check", "/fs/list", "/ignore-preview", "/libraries",
                     "/abort-other", "/endpoints/local/test", "/failures/retry"):
            response = self.get(path, query_string={"path": self.family})
            self.assertGreaterEqual(response.status_code, 400, path)
            self.assertLess(response.status_code, 500, path)
        self.assertEqual(self.post("/paths/check", ["not", "an", "object"]).status_code, 400)

    def test_responses_are_not_cached(self):
        response = self.get("/state")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = self.post("/paths/check", {"path": ""})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_turned_off_when_listening_beyond_this_computer(self):
        app = serve.build_app(init_db=False, bind_host="0.0.0.0",
                              job_manager=jobs.JobManager())
        self.addCleanup(app.extensions["search_images"].close)
        client = app.test_client()
        response = client.get("/api/admin/state", headers={"X-Search-Images": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/manage").status_code, 403)
        self.assertEqual(client.get("/settings").status_code, 403)
        config = page_config(client.get("/").get_data(as_text=True))
        self.assertFalse(config["capabilities"]["admin"])
        self.assertTrue(admin.is_loopback_host("127.0.0.1"))
        self.assertTrue(admin.is_loopback_host("localhost"))
        self.assertTrue(admin.is_loopback_host("::1"))
        self.assertFalse(admin.is_loopback_host("0.0.0.0"))
        self.assertFalse(admin.is_loopback_host("192.0.2.1"))


class PageTest(AdminFixture, unittest.TestCase):

    def test_pages_carry_the_config_block_csp_and_the_shell(self):
        for url, name in (("/", "search"), ("/detail", "detail"), ("/map", "map"),
                          ("/manage", "manage"), ("/settings", "settings")):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            html = response.get_data(as_text=True)
            policy = response.headers["Content-Security-Policy"]
            for directive in ("default-src 'self'", "frame-ancestors 'none'",
                              "img-src 'self' data: blob:", "base-uri 'none'",
                              "form-action 'self'", "object-src 'none'"):
                self.assertIn(directive, policy)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn('<script src="/static/shell.js"></script>', html, url)
            self.assertNotIn("window.APP_CONFIG", html)
            # Every script is either a file or the JSON block (CSP allows no inline code).
            for tag in re.findall(r"<script[^>]*>", html):
                self.assertTrue('src="/static/' in tag or 'type="application/json"' in tag, tag)
            config = page_config(html)
            self.assertEqual(config["page"], name)
            self.assertTrue(config["capabilities"]["admin"])
            self.assertFalse(config["capabilities"]["static"])
            self.assertEqual(config["libraryList"], [{"id": "lib_family1", "name": "Family"}])
            self.assertEqual(config["adminApi"], "/api/admin")
        for asset in ("shell.js", "site.js"):
            self.assertEqual(self.client.get("/static/" + asset).status_code, 200)

    def test_links_from_other_sites_open_the_bare_admin_pages(self):
        # A link (or redirect) from another site may name a dialog in the
        # address; it gets the page without it, so nothing starts by itself.
        for url in ("/manage?dialog=browse&path=%5C%5Cattacker.invalid%5Cshare",
                    "/settings?dialog=test&name=local"):
            for site in ("cross-site", "same-site"):
                response = self.client.get(url, headers={"Sec-Fetch-Site": site})
                self.assertEqual(response.status_code, 303, (url, site))
                self.assertEqual(response.headers["Location"].split("?")[0],
                                 url.split("?")[0])
                self.assertNotIn("?", response.headers["Location"])
            for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}):
                self.assertEqual(self.client.get(url, headers=headers).status_code, 200)
        # Without a query string there is nothing to strip.
        response = self.client.get("/manage", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 200)

    def test_other_computers_get_search_but_not_admin_pages(self):
        remote = {"REMOTE_ADDR": "192.0.2.5"}
        self.assertEqual(self.client.get("/manage", environ_base=remote).status_code, 403)
        self.assertEqual(self.client.get("/settings", environ_base=remote).status_code, 403)
        html = self.client.get("/", environ_base=remote).get_data(as_text=True)
        config = page_config(html)
        self.assertFalse(config["capabilities"]["admin"])
        self.assertNotIn("adminApi", config)

    def test_static_export_pages_have_the_block_and_no_admin(self):
        output = self.base.join("data", "exports", "site")
        serve.package(output, log=quiet)
        for name in ("index.html", "detail.html"):
            with open(os.path.join(output, name), encoding="utf-8") as handle:
                config = page_config(handle.read())
            self.assertEqual(config["mode"], "static")
            self.assertFalse(config["capabilities"]["admin"])
            self.assertTrue(config["capabilities"]["static"])
            self.assertNotIn("adminApi", config)
        self.assertTrue(os.path.isfile(os.path.join(output, "assets", "shell.js")))
        # No key, key file, endpoint or model of the settings anywhere in it.
        secrets = [LITERAL_KEY, FILE_KEY, ENV_KEY, self.key_file, "llm.example.com",
                   "127.0.0.1:9", "gpt-test", "SEARCH_IMAGES_TEST_KEY"]
        for folder, _dirs, names in os.walk(output):
            for name in names:
                with open(os.path.join(folder, name), "rb") as handle:
                    data = handle.read()
                for secret in secrets:
                    self.assertNotIn(secret.encode("utf-8"), data, name)


class StateTest(AdminFixture, unittest.TestCase):

    def test_state_lists_libraries_orphans_endpoints_and_jobs(self):
        insert_image(self.conn, "lib_family1", "a.jpg", described=1, prompt_ver=1)
        insert_image(self.conn, "lib_family1", "b.jpg", describe_attempts=3,
                     describe_error="boom")
        insert_image(self.conn, "lib_family1", "c.jpg", describe_attempts=1)
        insert_image(self.conn, "lib_family1", "d.jpg", missing_since=NOW)
        insert_image(self.conn, "lib_family1", "e.jpg", hidden=1)
        insert_image(self.conn, "lib_orphan01", "x.jpg")
        state = self.ok(self.get("/state"))
        library = state["libraries"][0]
        self.assertEqual(library["id"], "lib_family1")
        self.assertEqual(library["path"], self.family)
        self.assertEqual(library["counts"], {
            "total": 5, "present": 4, "missing": 1, "hidden": 1, "described": 1, "stale": 1,
            "failed": 2, "gave_up": 1, "pending": 2})
        self.assertIn(library["status"]["reachable"], (None, True))
        self.assertEqual(library["endpoint_info"]["name"], "local")
        self.assertFalse(library["endpoint_info"]["cloud"])
        self.assertEqual([orphan["id"] for orphan in state["orphans"]], ["lib_orphan01"])
        self.assertEqual(state["orphans"][0]["counts"]["total"], 1)
        self.assertEqual(state["default_endpoint"], "local")
        self.assertIn("anthropic", state["presets"])
        self.assertTrue(state["presets"]["anthropic"]["cloud"])
        self.assertEqual(set(state["jobs"]), {"current", "queue", "recent", "log", "seq",
                                              "errors", "lock", "manager"})
        self.assertTrue(state["config_token"])
        # The folder check runs in the background and is then remembered.
        status = wait_for(lambda: self.ok(self.get("/state"))["libraries"][0]["status"]
                          if self.ok(self.get("/state"))["libraries"][0]["status"]["reachable"]
                          else None)
        self.assertTrue(status["reachable"])

    def test_cloud_endpoints_without_a_key_cannot_describe(self):
        self.endpoints[1]["api_key_file"] = os.path.join(self.keys, "missing.key")
        self.endpoints.append({"name": "unset", "preset": "anthropic", "model": "m",
                               "api_key_env": "SEARCH_IMAGES_UNSET_TEST_KEY"})
        trips = self.base.library_folder("Trips")
        away = self.base.library_folder("Away")
        self.write_config([support.library("Family", self.family, id="lib_family1",
                                           endpoint="cloud"),
                           support.library("Trips", trips, id="lib_trips01", endpoint="unset"),
                           support.library("Away", away, id="lib_away001", endpoint="env")])
        libraries = {item["id"]: item for item in self.ok(self.get("/state"))["libraries"]}
        info = libraries["lib_family1"]["endpoint_info"]
        self.assertFalse(info["ready"])
        self.assertIn("key file", info["problem"])
        self.assertTrue(info["cloud"])
        info = libraries["lib_trips01"]["endpoint_info"]
        self.assertFalse(info["ready"])
        self.assertIn("SEARCH_IMAGES_UNSET_TEST_KEY is not set", info["problem"])
        # A custom service with a key from the environment is fine.
        self.assertTrue(libraries["lib_away001"]["endpoint_info"]["ready"])
        self.assertEqual(libraries["lib_away001"]["endpoint_info"]["problem"], "")

    def test_a_slow_key_file_does_not_hold_up_the_pages(self):
        real = common.resolve_api_key
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(endpoint):
            if endpoint.get("api_key_file"):
                release.wait(10)        # a key file on a share that does not answer
            return real(endpoint)
        self.write_config([support.library("Family", self.family, id="lib_family1",
                                           endpoint="cloud")])
        with mock.patch.object(common, "resolve_api_key", slow):
            started = time.monotonic()
            state = self.ok(self.get("/state"))
            listing = self.ok(self.get("/endpoints"))
            self.assertLess(time.monotonic() - started, 3)
        for endpoints in (state["endpoints"], listing["endpoints"]):
            cloud = next(item for item in endpoints if item["name"] == "cloud")
            self.assertEqual((cloud["key_source"], cloud["key_set"]), ("file checking", False))
        # Still reading: the library is not refused for it.
        self.assertTrue(state["libraries"][0]["endpoint_info"]["ready"])
        release.set()
        self.assertTrue(wait_for(lambda: next(
            item for item in self.ok(self.get("/endpoints"))["endpoints"]
            if item["name"] == "cloud")["key_source"] == "file"))

    def test_a_key_typed_into_a_name_field_by_hand_is_never_shown(self):
        self.endpoints[2]["api_key_env"] = HEX_KEY
        self.endpoints.append({"name": "pasted", "preset": "custom",
                               "base_url": "https://llm.example.com/v1", "model": "m",
                               "api_key_file": HEX_KEY})
        self.write_config()
        endpoints = {item["name"]: item for item in self.ok(self.get("/state"))["endpoints"]}
        self.assertEqual(endpoints["env"]["key_source"], "env (invalid name) not set")
        self.assertEqual(endpoints["env"]["api_key_env"], common.REDACTED)
        self.assertEqual(endpoints["env"]["stored"]["api_key_env"], common.REDACTED)
        self.assertEqual(endpoints["pasted"]["stored"]["api_key_file"], common.REDACTED)

    def test_endpoints_never_show_keys(self):
        endpoints = {item["name"]: item for item in self.ok(self.get("/state"))["endpoints"]}
        self.assertEqual(endpoints["cloud"]["key_source"], "file")
        self.assertTrue(endpoints["cloud"]["key_set"])
        self.assertFalse(endpoints["cloud"]["local"])
        self.assertEqual(endpoints["cloud"]["host"], "api.openai.com")
        self.assertEqual(endpoints["local"]["key_source"], "config")
        self.assertTrue(endpoints["local"]["stored_key"])
        self.assertTrue(endpoints["local"]["local"])
        self.assertIn(endpoints["local"]["status"]["state"],
                      ("checking", "not running", "unknown"))
        self.assertEqual(endpoints["env"]["key_source"], "env SEARCH_IMAGES_TEST_KEY")
        self.assertTrue(endpoints["local"]["default"])
        self.assertEqual(endpoints["local"]["used_by"], ["lib_family1"])
        for item in endpoints.values():
            self.assertNotIn("api_key", item)
            self.assertNotIn("api_key", item["stored"])
        listing = self.ok(self.get("/endpoints"))
        self.assertEqual({item["name"] for item in listing["endpoints"]},
                         {"local", "cloud", "env"})


class LibraryRoutesTest(AdminFixture, unittest.TestCase):

    def test_add_names_from_the_folder_and_registers_it(self):
        folder = self.base.library_folder("2014-10-24 - Garden")
        imagegen_free = os.path.join(folder, "a.jpg")
        with open(imagegen_free, "wb") as handle:
            handle.write(b"\xff\xd8not really")
        answer = self.ok(self.post("/libraries", {"path": folder, "ignore": "Private/*\n\n"}),
                         201)
        library = answer["library"]
        self.assertEqual(library["name"], "Garden")
        self.assertRegex(library["id"], r"^lib_[0-9a-f]{8}$")
        self.assertEqual(library["ignore"], ["Private/*"])
        self.assertEqual(answer["warnings"], [])
        saved = self.library(library["id"])
        self.assertEqual(saved["path"], folder)
        self.assertTrue(saved["enabled"])
        self.assertEqual(db.library_names(self.conn)[library["id"]], "Garden")

    def test_add_validates_fields(self):
        cases = (
            ({"path": ""}, "path"),
            ({"path": "relative/folder"}, "path"),
            ({"path": self.base.join("data", "photos")}, "path"),
            ({"path": os.path.join(support.REPO, "sample")}, "path"),
            ({"path": self.base.library_folder("Other"), "name": "FAMILY"}, "name"),
            ({"path": self.base.library_folder("Other"), "metadata": "huge"}, "metadata"),
            ({"path": self.base.library_folder("Other"), "endpoint": "nope"}, "endpoint"),
            ({"path": self.base.library_folder("Other"), "enabled": "yes"}, "enabled"),
            ({"path": self.base.library_folder("Other"), "context_notes": "x" * 5000},
             "context_notes"),
        )
        for body, field in cases:
            response = self.post("/libraries", body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn(field, response.get_json()["fields"], body)
        response = self.post("/libraries", {"path": self.family, "colour": "red"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.config()["libraries"]), 1)

    def test_add_warns_about_overlap_and_unreachable_folders(self):
        inner = self.base.library_folder(os.path.join("Family", "Inner"))
        answer = self.ok(self.post("/libraries", {"path": inner}), 201)
        self.assertTrue(any("overlaps" in warning for warning in answer["warnings"]))
        missing = self.base.join("libraries", "not-there")
        answer = self.ok(self.post("/libraries", {"path": missing, "name": "Away"}), 201)
        # (Its drive is there, so on Windows it is said to be missing, not offline.)
        self.assertTrue(any("cannot be reached" in warning or "does not exist" in warning
                            for warning in answer["warnings"]), answer["warnings"])
        self.assertEqual(len(self.config()["libraries"]), 3)

    def test_edit_rename_and_relocate_keep_the_id(self):
        image_id = insert_image(self.conn, "lib_family1", "a.jpg", described=1)
        answer = self.ok(self.post("/libraries/lib_family1",
                                   {"name": "Relatives", "enabled": False,
                                    "newest_first": False, "metadata": "extended",
                                    "context_notes": " Coastal town ", "endpoint": "CLOUD"}))
        self.assertEqual(answer["library"]["id"], "lib_family1")
        saved = self.library("lib_family1")
        self.assertEqual((saved["name"], saved["enabled"], saved["newest_first"],
                          saved["metadata"], saved["context_notes"], saved["endpoint"]),
                         ("Relatives", False, False, "extended", "Coastal town", "cloud"))
        self.assertTrue(answer["library"]["endpoint_info"]["cloud"])
        self.assertEqual(answer["library"]["endpoint_info"]["cloud_hosts"], ["api.openai.com"])
        self.assertEqual(db.library_names(self.conn)["lib_family1"], "Relatives")
        moved = self.base.library_folder("Moved")
        self.ok(self.post("/libraries/lib_family1", {"path": moved}))
        self.assertEqual(self.library("lib_family1")["path"], moved)
        row = self.conn.execute("SELECT library_id, described FROM images WHERE id = ?",
                                (image_id,)).fetchone()
        self.assertEqual(tuple(row), ("lib_family1", 1))
        self.assertEqual(self.post("/libraries/lib_nothere", {"name": "x"}).status_code, 404)
        response = self.post("/libraries/lib_family1", {"id": "lib_other"})
        self.assertEqual(response.status_code, 400)

    def test_rename_refuses_a_taken_name(self):
        other = self.base.library_folder("Trips")
        self.ok(self.post("/libraries", {"path": other}), 201)
        response = self.post("/libraries/lib_family1", {"name": "trips"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.get_json()["fields"])
        # Its own name in another case is fine.
        self.ok(self.post("/libraries/lib_family1", {"name": "FAMILY"}))

    def test_concurrent_hand_edit_gives_409(self):
        token = self.ok(self.get("/state"))["config_token"]
        # Someone edits config.json by hand meanwhile.
        config = json.loads(open(common.config_path(), encoding="utf-8").read())
        config["reader_threads"] = 3
        with open(common.config_path(), "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        stat = os.stat(common.config_path())
        os.utime(common.config_path(), ns=(stat.st_atime_ns, stat.st_mtime_ns + 10 ** 9))
        response = self.post("/libraries/lib_family1", {"name": "Kin", "config_token": token})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["reload"])
        self.assertEqual(self.library("lib_family1")["name"], "Family")
        response = self.post("/libraries/lib_family1", {"name": "Kin"},
                             headers={"X-Config-Token": token})
        self.assertEqual(response.status_code, 409)
        fresh = self.ok(self.get("/state"))["config_token"]
        answer = self.ok(self.post("/libraries/lib_family1",
                                   {"name": "Kin", "config_token": fresh}))
        self.assertNotEqual(answer["config_token"], fresh)
        # The hand edit was kept.
        self.assertEqual(self.config()["reader_threads"], 3)
        self.assertEqual(self.library("lib_family1")["name"], "Kin")

    def test_remove_keeping_the_data_leaves_an_orphan_to_reattach(self):
        image_id = insert_image(self.conn, "lib_family1", "a.jpg", described=1, has_preview=1)
        previews = cache.PreviewStore()
        previews.write("lib_family1", image_id, b"\xff\xd8preview")
        response = self.post("/libraries/lib_family1/remove",
                             {"keep_data": True, "confirm_name": "wrong"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm_name", response.get_json()["fields"])
        self.assertEqual(self.post("/libraries/lib_family1/remove",
                                   {"confirm_name": "Family"}).status_code, 400)
        answer = self.ok(self.post("/libraries/lib_family1/remove",
                                   {"keep_data": True, "confirm_name": " family "}))
        self.assertIsNone(answer["job"])
        self.assertEqual(self.config()["libraries"], [])
        orphans = self.ok(self.get("/state"))["orphans"]
        self.assertEqual([(item["id"], item["name"]) for item in orphans],
                         [("lib_family1", "Family")])
        # Added again (a new id), the old data can be re-attached to it.
        library = self.ok(self.post("/libraries", {"path": self.family}), 201)["library"]
        new_id = library["id"]
        self.assertNotEqual(new_id, "lib_family1")
        answer = self.ok(self.post("/orphans/lib_family1/reattach", {"library_id": new_id}))
        self.assertEqual((answer["moved"], answer["previews_moved"]), (1, 1))
        row = self.conn.execute("SELECT library_id, described FROM images WHERE id = ?",
                                (image_id,)).fetchone()
        self.assertEqual(tuple(row), (new_id, 1))
        self.assertEqual(previews.read(new_id, image_id), b"\xff\xd8preview")
        self.assertFalse(os.path.isdir(previews.library_dir("lib_family1")))
        self.assertEqual(self.ok(self.get("/state"))["orphans"], [])

    def test_a_busy_database_never_turns_a_saved_change_into_an_error(self):
        # A running scan keeps index.db's write lock; config.json is saved
        # anyway, and the answer carries the new token.
        writer = db.connect()
        self.addCleanup(writer.close)
        errors = io.StringIO()
        with mock.patch.object(admin, "REGISTER_WAIT", 0.2), \
                mock.patch.object(admin, "REGISTER_RETRY", 0.05), \
                contextlib.redirect_stderr(errors):
            writer.execute("BEGIN IMMEDIATE")
            try:
                answer = self.ok(self.post("/libraries",
                                           {"path": self.base.library_folder("Trips")}), 201)
                library_id = answer["library"]["id"]
                # The page's next change is not taken for a hand edit.
                answer = self.ok(self.post("/libraries/" + library_id,
                                           {"name": "Journeys",
                                            "config_token": answer["config_token"]}))
                self.assertEqual(self.library(library_id)["name"], "Journeys")
                self.assertNotIn(library_id, db.library_names(self.conn))
            finally:
                writer.rollback()
            # The names are recorded once the database is free.
            self.assertTrue(wait_for(lambda: db.library_names(self.conn).get(library_id)
                                     == "Journeys"))
            writer.execute("BEGIN IMMEDIATE")
            try:
                answer = self.ok(self.post("/libraries/%s/remove" % library_id,
                                           {"keep_data": False, "confirm_name": "journeys",
                                            "config_token": answer["config_token"]}))
                self.assertTrue(answer["config_token"])
                self.assertEqual(answer["warnings"], [])
                self.assertEqual((answer["job"]["kind"], answer["job"]["params"]),
                                 ("delete_library", {"confirm_name": "Journeys"}))
            finally:
                writer.rollback()
            # Let the background retries finish while the retry interval is
            # still short and this test's data folder still exists; a later
            # try would otherwise open index.db in whatever folder
            # SETTINGS_BASE names by then (the program folder, or another
            # test's).
            self.assertTrue(wait_for(lambda: not any(
                thread.name == "register-libraries" and thread.is_alive()
                for thread in threading.enumerate())))
        self.assertIn("trying again", errors.getvalue())
        self.assertEqual([item["id"] for item in self.config()["libraries"]], ["lib_family1"])

    def test_a_library_removed_before_anything_was_indexed_leaves_nothing_behind(self):
        answer = self.ok(self.post("/libraries", {"path": self.base.library_folder("Typo")}),
                         201)
        library_id = answer["library"]["id"]
        self.ok(self.post("/libraries/%s/remove" % library_id,
                          {"keep_data": True, "confirm_name": "Typo"}))
        self.assertEqual(self.ok(self.get("/state"))["orphans"], [])

    def test_export_settings_are_checked(self):
        response = self.post("/libraries/lib_family1", {"export": {"output": self.family}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("export", response.get_json()["fields"])
        answer = self.ok(self.post("/libraries/lib_family1",
                                   {"export": {"images": "none", "location": False}}))
        self.assertEqual(self.library("lib_family1")["export"], {"images": "none"})
        self.assertEqual(answer["library"]["export_info"]["options"]["images"], "none")
        self.ok(self.post("/libraries/lib_family1", {"export": {}}))
        self.assertEqual(self.library("lib_family1")["export"], {})

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_device_paths_are_refused(self):
        device = "\\\\?\\" + common.data_dir()
        response = self.post("/libraries", {"path": device})
        self.assertEqual(response.status_code, 400)
        self.assertIn("path", response.get_json()["fields"])
        self.assertEqual(self.post("/fs/list", {"path": device}).status_code, 400)

    def test_reattach_refuses_clashing_paths_and_unknown_ids(self):
        insert_image(self.conn, "lib_orphan01", "a.jpg")
        insert_image(self.conn, "lib_family1", "a.jpg")
        response = self.post("/orphans/lib_orphan01/reattach", {"library_id": "lib_family1"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.post("/orphans/lib_orphan01/reattach",
                                   {"library_id": "lib_nothere"}).status_code, 400)
        self.assertEqual(self.post("/orphans/lib_family1/reattach",
                                   {"library_id": "lib_family1"}).status_code, 404)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM images WHERE library_id = 'lib_orphan01'").fetchone()[0], 1)

    def test_reattach_waits_for_a_running_job(self):
        insert_image(self.conn, "lib_orphan01", "b.jpg")
        lock = jobs.WorkerLock()
        self.assertTrue(lock.acquire("scan.py"))
        try:
            response = self.post("/orphans/lib_orphan01/reattach",
                                 {"library_id": "lib_family1"})
        finally:
            lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertIn("scan.py", response.get_json()["error"])

    def test_remove_and_delete_the_data(self):
        image_id = insert_image(self.conn, "lib_family1", "a.jpg")
        with cache.ThumbStore() as thumbs:
            thumbs.put(image_id, b"\xff\xd8thumb")
        cache.PreviewStore().write("lib_family1", image_id, b"\xff\xd8preview")
        queued, _added = self.manager.enqueue("scan", "lib_family1", {})
        answer = self.ok(self.post("/libraries/lib_family1/remove",
                                   {"keep_data": False, "confirm_name": "Family"}))
        job = answer["job"]
        self.assertEqual((job["kind"], job["library_id"], job["params"]),
                         ("delete_library", "lib_family1", {"confirm_name": "Family"}))
        # The library's queued jobs are dropped with it.
        self.assertEqual(self.manager.get(queued["id"])["state"], "cancelled")
        self.assertTrue(self.manager.start())
        self.assertTrue(self.manager.wait_idle(20))
        self.assertEqual(self.manager.get(job["id"])["state"], "done")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)
        with cache.ThumbStore() as thumbs:
            self.assertIsNone(thumbs.get(image_id))
        self.assertFalse(os.path.isdir(cache.PreviewStore().library_dir("lib_family1")))
        self.assertNotIn("lib_family1", db.library_names(self.conn))

    def test_orphan_delete_checks_the_name(self):
        insert_image(self.conn, "lib_orphan01", "a.jpg")
        with self.conn:
            self.conn.execute("INSERT INTO libraries (id, name, created) VALUES (?, ?, ?)",
                              ("lib_orphan01", "Old Phone", NOW))
        response = self.post("/orphans/lib_orphan01/delete", {"confirm_name": "Old"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post("/orphans/lib_family1/delete",
                                   {"confirm_name": "Family"}).status_code, 404)
        answer = self.ok(self.post("/orphans/lib_orphan01/delete",
                                   {"confirm_name": "old phone"}), 202)
        self.assertTrue(answer["added"])
        self.assertTrue(self.manager.start())
        self.assertTrue(self.manager.wait_idle(20))
        self.assertEqual(self.manager.get(answer["job"]["id"])["state"], "done")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_the_delete_job_never_touches_configured_libraries(self):
        insert_image(self.conn, "lib_family1", "a.jpg")
        job, _added = self.manager.enqueue("delete_library", "lib_family1",
                                           {"confirm_name": "Family"})
        response = self.post("/jobs", {"kind": "delete_library", "library_id": "lib_family1",
                                       "params": {"confirm_name": "Family"}})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.manager.start())
        self.assertTrue(self.manager.wait_idle(20))
        job = self.manager.get(job["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("in the settings", job["error"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)


class FolderRoutesTest(AdminFixture, unittest.TestCase):

    def write(self, rel, data=b"\xff\xd8"):
        path = os.path.join(self.family, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def test_paths_check(self):
        for rel in ("a.jpg", "sub/b.png", "@eaDir/c.jpg", "notes.txt"):
            self.write(rel)
        answer = self.ok(self.post("/paths/check", {"path": self.family}))
        self.assertTrue(answer["exists"] and answer["is_dir"] and answer["readable"])
        self.assertEqual(answer["sample_images"], 2)
        self.assertFalse(answer["more"])
        self.assertEqual(answer["problem"], "")
        answer = self.ok(self.post("/paths/check", {"path": self.base.join("nothing")}))
        self.assertFalse(answer["exists"])
        self.assertIn("does not exist", answer["message"])
        answer = self.ok(self.post("/paths/check",
                                   {"path": os.path.join(self.family, "a.jpg")}))
        self.assertTrue(answer["exists"])
        self.assertFalse(answer["is_dir"])
        answer = self.ok(self.post("/paths/check", {"path": self.base.join("data")}))
        self.assertIn("data folder", answer["problem"])
        self.assertEqual(self.post("/paths/check", {}).status_code, 400)

    def test_paths_check_suggests_a_library_name(self):
        # The Add dialog fills in the name a library in this folder would get.
        folder = self.base.library_folder("2014-10-24 - Garden")
        answer = self.ok(self.post("/paths/check", {"path": folder}))
        self.assertEqual(answer["name"], "Garden")
        self.assertEqual(answer["name"], common.default_library_name(folder))
        answer = self.ok(self.post("/paths/check", {"path": self.base.join("nothing here")}))
        self.assertEqual(answer["name"], "nothing here")

    def test_paths_check_times_out_on_a_hanging_share(self):
        real_isdir = os.path.isdir
        slow = self.base.join("slow-share")

        def hanging(path):
            if os.path.normcase(str(path)) == os.path.normcase(slow):
                time.sleep(1.5)
            return real_isdir(path)

        with mock.patch.object(library_ops, "PATH_CHECK_TIMEOUT", 0.2), \
                mock.patch("os.path.isdir", side_effect=hanging):
            started = time.monotonic()
            answer = self.ok(self.post("/paths/check", {"path": slow}))
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertTrue(answer["timed_out"])
        self.assertIn("did not answer", answer["message"])

    def test_fs_list(self):
        for rel in ("b/x.jpg", "A/y.jpg", "c.jpg", ".hidden/z.jpg", "$RECYCLE.BIN/q",
                    "@eaDir/t.jpg"):
            self.write(rel)
        answer = self.ok(self.post("/fs/list", {"path": self.family}))
        self.assertEqual(answer["dirs"], ["A", "b"])
        self.assertEqual(answer["path"], self.family)
        self.assertEqual(os.path.normcase(answer["parent"]),
                         os.path.normcase(os.path.dirname(self.family)))
        self.assertFalse(answer["truncated"])
        roots = self.ok(self.post("/fs/list", {"path": ""}))
        self.assertTrue(roots["roots"])
        self.assertTrue(roots["dirs"])
        self.assertIn("\\\\server\\share", roots["hint"])
        top = self.ok(self.post("/fs/list", {"path": roots["dirs"][0]}))
        self.assertEqual(top["parent"], "")
        self.assertEqual(self.post("/fs/list", {"path": self.base.join("nothing")}).status_code,
                         400)
        self.assertEqual(self.post("/fs/list", {"path": "relative"}).status_code, 400)
        self.assertEqual(self.post("/fs/list", {"path": os.path.join(self.family, "c.jpg")})
                         .status_code, 400)
        with mock.patch.object(library_ops, "LIST_LIMIT", 1):
            self.assertTrue(self.ok(self.post("/fs/list", {"path": self.family}))["truncated"])

    def test_fs_list_times_out(self):
        real_scandir = os.scandir

        def hanging(path="."):
            time.sleep(1.5)
            return real_scandir(path)

        with mock.patch.object(library_ops, "LIST_TIMEOUT", 0.2), \
                mock.patch.object(library_ops.os, "scandir", side_effect=hanging):
            response = self.post("/fs/list", {"path": self.family})
        self.assertEqual(response.status_code, 504)
        self.assertIn("did not answer", response.get_json()["error"])

    def test_ignore_preview(self):
        for rel in ("keep.jpg", "x_1.jpg", "skip/a.jpg", "skip/deep/b.jpg", "@eaDir/t.jpg",
                    "._junk.jpg", "doc.txt"):
            self.write(rel)
        answer = self.ok(self.post("/ignore-preview", {"library_id": "lib_family1",
                                                       "ignore": "skip/*",
                                                       "default_ignores": True}))
        self.assertEqual(answer["scanned"], 6)
        self.assertEqual(answer["skipped"], 4)
        self.assertEqual(sorted(answer["examples"]), ["._junk.jpg", "@eaDir/", "skip/"])
        self.assertFalse(answer["truncated"])
        answer = self.ok(self.post("/ignore-preview", {"path": self.family,
                                                       "ignore": ["*_1.jpg"],
                                                       "default_ignores": False}))
        self.assertEqual((answer["scanned"], answer["skipped"], answer["examples"]),
                         (6, 1, ["x_1.jpg"]))
        response = self.post("/ignore-preview", {"path": self.family, "ignore": 5})
        self.assertEqual(response.status_code, 400)
        response = self.post("/ignore-preview", {"path": self.base.join("data")})
        self.assertEqual(response.status_code, 400)


class JobRoutesTest(AdminFixture, unittest.TestCase):

    def test_queue_validate_and_control_jobs(self):
        for body in ({"kind": "export"}, {"kind": "scan", "params": {"turbo": True}},
                     {"kind": "describe", "params": {"limit": -1}},
                     {"kind": "describe", "params": {"limit": "5"}},
                     {"kind": "describe", "params": {"endpoint": "nope"}},
                     {"kind": "scan", "params": {"allow_mass_removal": "yes"}},
                     {"kind": "scan", "library_id": "lib_nothere"}):
            response = self.post("/jobs", body)
            self.assertIn(response.status_code, (400, 404), body)
        answer = self.ok(self.post("/jobs", {"kind": "process", "library_id": "lib_family1",
                                             "params": {"retry_failed": False, "limit": 5,
                                                        "endpoint": "local"}}), 202)
        self.assertTrue(answer["added"])
        job = answer["job"]
        self.assertEqual(job["params"], {"limit": 5, "endpoint": "local"})
        self.assertEqual(answer["jobs"]["queue"][0]["library"], "Family")
        again = self.ok(self.post("/jobs", {"kind": "process", "library_id": "lib_family1",
                                            "params": {"endpoint": "local", "limit": 5}}), 202)
        self.assertFalse(again["added"])
        self.assertEqual(again["job"]["id"], job["id"])
        everyone = self.ok(self.post("/jobs", {"kind": "process", "library_id": None}), 202)
        self.assertIsNone(everyone["job"]["library_id"])
        self.assertEqual(self.post("/jobs/%d/pause" % job["id"]).status_code, 409)
        self.assertEqual(self.post("/jobs/%d/explode" % job["id"]).status_code, 404)
        self.ok(self.post("/jobs/%d/cancel" % job["id"]))
        self.assertEqual(self.manager.get(job["id"])["state"], "cancelled")
        self.assertEqual(self.post("/jobs/%d/cancel" % job["id"]).status_code, 409)
        snap = self.ok(self.get("/jobs", query_string={"since": "0"}))
        self.assertEqual([item["id"] for item in snap["queue"]], [everyone["job"]["id"]])
        self.assertEqual(self.ok(self.get("/jobs", query_string={"since": "junk"}))["queue"],
                         snap["queue"])

    def test_job_indicator_can_leave_out_the_log(self):
        self.manager.buffer.append({"time": NOW, "level": "warn", "job": None, "stage": "",
                                    "message": "something to show in the log"})
        self.ok(self.post("/jobs", {"kind": "scan", "library_id": "lib_family1"}), 202)
        full = self.ok(self.get("/jobs"))
        self.assertTrue(full["log"])
        short = self.ok(self.get("/jobs", query_string={"log": "0"}))
        self.assertEqual((short["log"], short["errors"]), ([], []))
        self.assertEqual(short["seq"], full["seq"])
        self.assertEqual([job["id"] for job in short["queue"]],
                         [job["id"] for job in full["queue"]])

    def test_jobs_never_wait_for_a_scan_writing_index_db(self):
        writer = db.connect()
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            answer = self.ok(self.post("/jobs", {"kind": "scan", "library_id": "lib_family1"}),
                             202)
            self.ok(self.get("/jobs", query_string={"log": "0"}))
            self.ok(self.post("/jobs/%d/cancel" % answer["job"]["id"]))
            self.assertLess(time.monotonic() - started, 3)
        finally:
            writer.rollback()

    def test_a_running_job_is_not_started_again(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.manager.runners["scan"] = gate_runner(started, release)
        self.assertTrue(self.manager.start())
        first = self.ok(self.post("/jobs", {"kind": "scan", "library_id": "lib_family1"}), 202)
        self.assertTrue(started.wait(10))
        again = self.ok(self.post("/jobs", {"kind": "scan", "library_id": "lib_family1"}), 202)
        self.assertFalse(again["added"])
        self.assertEqual((again["job"]["id"], again["job"]["state"]),
                         (first["job"]["id"], "running"))
        release.set()
        self.assertTrue(self.manager.wait_idle(10))

    def test_jobs_another_server_runs_are_shown_but_not_controlled_here(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        other = jobs.JobManager(runners={"scan": gate_runner(started, release)},
                                poll_interval=0.05, idle_interval=0.05)
        self.assertTrue(other.start())
        self.addCleanup(other.stop, 10)
        self.assertFalse(self.manager.start())
        job = self.ok(self.post("/jobs", {"kind": "scan", "library_id": "lib_family1"}),
                      202)["job"]
        self.assertTrue(started.wait(10))
        snap = wait_for(lambda: self.ok(self.get("/jobs")) if (
            self.ok(self.get("/jobs"))["current"] or {}).get("remote") else None)
        self.assertTrue(snap)
        self.assertEqual((snap["current"]["id"], snap["current"]["library"]),
                         (job["id"], "Family"))
        self.assertEqual(snap["lock"]["kind"], "server")
        self.assertFalse(snap["manager"]["running"])
        for action in ("pause", "cancel"):
            response = self.post("/jobs/%d/%s" % (job["id"], action))
            self.assertEqual(response.status_code, 409, action)
            self.assertIn("another server", response.get_json()["error"])
        answer = self.ok(self.post("/abort-other"))
        self.assertFalse(answer["requested"])
        self.assertFalse(os.path.exists(common.abort_path()))
        self.assertEqual(self.post("/jobs/999999/cancel").status_code, 404)
        release.set()
        self.assertTrue(other.wait_idle(10))

    def test_disabled_libraries_are_not_queued(self):
        self.ok(self.post("/libraries/lib_family1", {"enabled": False}))
        response = self.post("/jobs", {"kind": "scan", "library_id": "lib_family1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("disabled", response.get_json()["error"])

    def test_failures_and_retry(self):
        insert_image(self.conn, "lib_family1", "a.jpg", describe_attempts=3,
                     describe_error="HTTP 401 with key %s" % LITERAL_KEY)
        # Not a real key: a made-up value used only by this test.
        insert_image(self.conn, "lib_family1", "b.jpg", describe_attempts=1,
                     describe_error="Bearer abcdefghijklmnop1234")
        insert_image(self.conn, "lib_family1", "c.jpg", described=1)
        insert_image(self.conn, "lib_family1", "d.jpg", describe_attempts=2,
                     missing_since=NOW)
        failures = self.ok(self.get("/failures", query_string={"library": "lib_family1"}))
        self.assertEqual([(item["path"], item["attempts"]) for item in failures],
                         [("a.jpg", 3), ("b.jpg", 1)])
        self.assertNotIn("abcdefghijklmnop1234", json.dumps(failures))
        self.assertEqual(len(self.ok(self.get("/failures", query_string={"limit": "1"}))), 1)
        answer = self.ok(self.post("/failures/retry", {"library_id": "lib_family1"}), 202)
        self.assertEqual((answer["job"]["kind"], answer["job"]["params"]),
                         ("describe", {"retry_failed": True}))

    def test_abort_other(self):
        answer = self.ok(self.post("/abort-other"))
        self.assertFalse(answer["requested"])
        self.assertFalse(os.path.exists(common.abort_path()))
        lock = jobs.WorkerLock()
        self.assertTrue(lock.acquire("scan.py"))
        try:
            answer = self.ok(self.post("/abort-other"))
            self.assertTrue(answer["requested"])
            self.assertIn("scan.py", answer["holder"])
            self.assertTrue(os.path.exists(common.abort_path()))
            snap = self.ok(self.get("/jobs"))
            self.assertTrue(snap["lock"]["other"])
        finally:
            lock.release()
        # Once the other process is gone the file is removed (scan.py would refuse to start).
        self.ok(self.get("/jobs"))
        self.assertFalse(os.path.exists(common.abort_path()))


class EndpointRoutesTest(AdminFixture, unittest.TestCase):

    def stored(self, name):
        config = json.loads(open(common.config_path(), encoding="utf-8").read())
        return next(item for item in config["endpoints"] if item["name"] == name)

    def test_add_edit_keep_and_clear_the_key(self):
        answer = self.ok(self.post("/endpoints", {"endpoint": {
            "name": "studio", "preset": "lmstudio", "model": "vision-model",
            "api_key": NEW_KEY, "concurrency": "2", "timeout": 90, "extra": '{"top_k": 20}',
            "price_in": 0.5}}))
        view = answer["endpoint"]
        self.assertEqual((view["key_source"], view["key_set"]), ("config", True))
        self.assertEqual((view["concurrency"], view["timeout"], view["extra"]),
                         (2, 90.0, {"top_k": 20}))
        self.assertEqual(self.stored("studio")["api_key"], NEW_KEY)
        # A blank key keeps the stored one.
        answer = self.ok(self.post("/endpoints", {"endpoint": {"name": "studio", "api_key": "",
                                                               "model": "other-model"},
                                                  "original_name": "studio"}))
        self.assertFalse(answer["key_cleared"])
        self.assertEqual(self.stored("studio")["api_key"], NEW_KEY)
        self.assertEqual(self.stored("studio")["model"], "other-model")
        # Same host, another path: still the same service.
        self.ok(self.post("/endpoints", {"endpoint": {"name": "studio",
                                                      "base_url": "http://127.0.0.1:1234/v2"},
                                         "original_name": "studio"}))
        self.assertEqual(self.stored("studio")["api_key"], NEW_KEY)
        # Another host without a new key: the key is not sent there. (Another
        # loopback address: the server checks it, and tests stay on this computer.)
        answer = self.ok(self.post("/endpoints", {"endpoint": {
            "name": "studio", "base_url": "http://127.0.0.2:1234/v1"},
            "original_name": "studio"}))
        self.assertTrue(answer["key_cleared"])
        self.assertNotIn("api_key", self.stored("studio"))
        self.assertTrue(any("removed" in warning for warning in answer["warnings"]))
        # Moving with a new key keeps that key ("replace" edits by name, too).
        self.ok(self.post("/endpoints", {"endpoint": {"name": "studio", "api_key": NEW_KEY,
                                                      "base_url": "http://127.0.0.1:1234/v1"},
                                         "replace": True}))
        self.assertEqual(self.stored("studio")["api_key"], NEW_KEY)
        self.ok(self.post("/endpoints", {"endpoint": {"clear_api_key": True},
                                         "original_name": "studio"}))
        self.assertNotIn("api_key", self.stored("studio"))

    def test_invalid_endpoint_settings(self):
        cases = (
            ({"name": ""}, "name"),
            ({"name": "a,b"}, "name"),
            ({"name": "x", "preset": "warp"}, "preset"),
            ({"name": "x", "kind": "gopher"}, "kind"),
            ({"name": "x", "concurrency": 0}, "concurrency"),
            ({"name": "x", "max_image_edge": 10}, "max_image_edge"),
            ({"name": "x", "timeout": "soon"}, "timeout"),
            ({"name": "x", "extra": "{not json"}, "extra"),
            ({"name": "x", "extra": "[1, 2]"}, "extra"),
            ({"name": "x", "enabled": "no"}, "enabled"),
        )
        for endpoint, field in cases:
            response = self.post("/endpoints", {"endpoint": endpoint})
            self.assertEqual(response.status_code, 400, endpoint)
            self.assertIn(field, response.get_json()["fields"], endpoint)
        # A key pasted into the variable name field is refused and never echoed.
        response = self.post("/endpoints", {"endpoint": {"name": "x",
                                                         "api_key_env": NEW_KEY}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("looks like an API key", response.get_json()["fields"]["api_key_env"])
        response = self.post("/endpoints", {"endpoint": {"name": "x",
                                                         "api_key_file": NEW_KEY}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("api_key_file", response.get_json()["fields"])
        self.assertEqual(self.post("/endpoints", {"endpoint": {"name": "x", "bogus": 1}})
                         .status_code, 400)
        self.assertEqual(self.post("/endpoints", {"endpoint": {"name": "y"},
                                                  "original_name": "missing"}).status_code, 404)
        names = [item["name"] for item in self.config()["endpoints"]]
        self.assertEqual(names, ["local", "cloud", "env"])

    def test_adding_a_name_that_is_taken_is_refused(self):
        response = self.post("/endpoints", {"endpoint": {"name": "Cloud", "preset": "openai",
                                                         "model": "gpt-other"},
                                            "original_name": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("already called", response.get_json()["fields"]["name"])
        stored = self.stored("cloud")
        self.assertEqual((stored["name"], stored["model"]), ("cloud", "gpt-test"))
        self.assertEqual([item["name"] for item in self.config()["endpoints"]],
                         ["local", "cloud", "env"])

    def test_a_model_is_required(self):
        response = self.post("/endpoints", {"endpoint": {"name": "studio",
                                                         "preset": "llamacpp"}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("List models", response.get_json()["fields"]["model"])
        response = self.post("/endpoints", {"endpoint": {"model": " "},
                                            "original_name": "local"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("model", response.get_json()["fields"])
        self.assertEqual(self.stored("local")["model"], "model.gguf")

    def test_a_hex_key_typed_as_a_variable_or_file_name_is_refused(self):
        for field in ("api_key_env", "api_key_file"):
            response = self.post("/endpoints", {"endpoint": {"name": "azure2", "preset": "azure",
                                                             "model": "m", field: HEX_KEY}})
            self.assertEqual(response.status_code, 400, field)
            self.assertIn("looks like an API key", response.get_json()["fields"][field])
        self.assertNotIn("azure2", [item["name"] for item in self.config()["endpoints"]])

    def test_rename_default_and_remove(self):
        self.ok(self.post("/endpoints", {"endpoint": {"name": "workstation"},
                                         "original_name": "local"}))
        config = self.config()
        self.assertEqual(config["default_endpoint"], "workstation")
        self.assertEqual(self.stored("workstation")["api_key"], LITERAL_KEY)
        self.ok(self.post("/libraries/lib_family1", {"endpoint": "env"}))
        answer = self.ok(self.post("/endpoints/default", {"name": "env"}))
        self.assertEqual(answer["default_endpoint"], "env")
        self.assertEqual(self.post("/endpoints/default", {"name": "nope"}).status_code, 400)
        answer = self.ok(self.post("/endpoints/env/remove"))
        self.assertTrue(any("Family" in warning for warning in answer["warnings"]))
        self.assertEqual(answer["default_endpoint"], "workstation")
        self.assertEqual(self.post("/endpoints/env/remove").status_code, 404)

    def test_new_endpoints_and_legacy_configs(self):
        self.write_config(endpoints=None, default_endpoint=None)
        config = json.loads(open(common.config_path(), encoding="utf-8").read())
        config.pop("endpoints")
        config.pop("default_endpoint")
        config["endpoint"] = {"kind": "openai", "base_url": "http://127.0.0.1:8080/v1",
                              "model": "old-model"}
        with open(common.config_path(), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        state = self.ok(self.get("/state"))
        self.assertEqual([item["name"] for item in state["endpoints"]], ["default"])
        self.ok(self.post("/endpoints", {"endpoint": {"name": "second", "preset": "ollama",
                                                      "model": "llava"}}))
        saved = self.config()
        self.assertNotIn("endpoint", json.loads(open(common.config_path()).read()))
        self.assertEqual([item["name"] for item in saved["endpoints"]], ["default", "second"])
        self.assertEqual(saved["default_endpoint"], "default")

    @unittest.skipIf(fake_llm is None, "Pillow is not installed")
    def test_test_route_describes_the_sample_picture(self):
        server = fake_llm.FakeLLM().start()
        self.addCleanup(server.stop)
        llm._no_stream_options.clear()
        self.addCleanup(llm._no_stream_options.clear)
        self.ok(self.post("/endpoints", {"endpoint": {
            "base_url": server.base_url, "api_key": LITERAL_KEY,
            "price_in": 1.0, "price_out": 2.0}, "original_name": "local"}))
        insert_image(self.conn, "lib_family1", "a.jpg")
        insert_image(self.conn, "lib_family1", "b.jpg")
        answer = self.ok(self.post("/endpoints/local/test"))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["parsed"]["title"], fake_llm.DEFAULT_REPLY["title"])
        self.assertEqual(answer["image"], "sample picture")
        self.assertEqual(answer["pending_images"], 2)
        self.assertAlmostEqual(answer["est_cost_pending"],
                               2 * answer["est_cost_per_image"], places=6)
        self.assertEqual(answer["key_source"], "config")
        self.assertEqual(len(server.describe_requests()), 1)
        self.assertEqual(self.post("/endpoints/nope/test").status_code, 404)

    def test_a_cloud_endpoint_only_gets_the_sample_picture(self):
        image_id = insert_image(self.conn, "lib_family1", "a.jpg")
        response = self.post("/endpoints/cloud/test", {"image_id": image_id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("sample picture", response.get_json()["error"])

    @unittest.skipIf(fake_llm is None, "Pillow is not installed")
    def test_list_models(self):
        server = fake_llm.FakeLLM().start()
        self.addCleanup(server.stop)
        answer = self.ok(self.post("/endpoints/models", {"endpoint": {
            "preset": "custom", "base_url": server.base_url}}))
        self.assertTrue(answer["models"])
        # Listing never saves anything.
        self.assertEqual([item["name"] for item in self.config()["endpoints"]],
                         ["local", "cloud", "env"])


class SettingsConsoleTest(support.TempBaseMixin, unittest.TestCase):
    """settings.py uses the same library rules as the web UI."""

    def test_folders_inside_the_data_or_program_folder_are_refused(self):
        import settings
        config = {"libraries": []}
        for folder in (os.path.join(common.data_dir(), "pictures"),
                       os.path.join(support.REPO, "sample")):
            with self.assertRaises(ValueError):
                settings.add_library(config, folder)
        library = settings.add_library(config, self.base.library_folder("Family"))
        self.assertEqual(library["name"], "Family")
        with self.assertRaises(ValueError):
            settings.set_library_folder(config, 0, common.data_dir())
        # Relative folders are still accepted on the console, as before.
        settings.add_library(config, "relative-folder", "Relative")


class FirstRunTest(AdminFixture, unittest.TestCase):
    """A new install where a library is added before any endpoint."""

    def test_no_phantom_endpoint_before_the_first_one_is_added(self):
        os.remove(common.config_path())
        answer = self.ok(self.post("/libraries", {"path": self.family}), 201)
        info = answer["library"]["endpoint_info"]
        # Nothing is said to go to a cloud service: there is no endpoint.
        self.assertFalse(info["cloud"])
        self.assertEqual(info["cloud_hosts"], [])
        self.assertEqual(info["endpoints"], [])
        with open(common.config_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertNotIn("endpoint", saved)
        self.assertEqual(saved["endpoints"], [])
        state = self.ok(self.get("/state"))
        self.assertEqual(state["endpoints"], [])
        self.assertEqual(state["default_endpoint"], "")

    def test_the_placeholder_of_older_versions_is_no_endpoint(self):
        self.write_config(endpoints=None, default_endpoint=None)
        self.base.write_config([support.library("Family", self.family, id="lib_family1")],
                               endpoint={"kind": "openai", "base_url": "", "api_key": "",
                                         "model": ""})
        state = self.ok(self.get("/state"))
        self.assertEqual(state["endpoints"], [])
        self.assertEqual(state["default_endpoint"], "")
        info = state["libraries"][0]["endpoint_info"]
        self.assertFalse(info["cloud"])
        self.assertIn("No AI model", info["problem"])

    def test_an_endpoint_without_a_model_sends_nothing_anywhere(self):
        self.endpoints.append({"name": "nomodel", "preset": "openai"})
        self.write_config([support.library("Family", self.family, id="lib_family1",
                                           endpoint="nomodel")])
        info = self.ok(self.get("/state"))["libraries"][0]["endpoint_info"]
        self.assertFalse(info["ready"])
        self.assertFalse(info["cloud"])
        self.assertIn("no model", info["problem"])

    def test_quoted_library_folder(self):
        folder = self.base.library_folder("Quoted")
        answer = self.ok(self.post("/libraries", {"path": '"%s"' % folder}), 201)
        self.assertEqual(answer["library"]["path"], folder)
        self.assertEqual(answer["library"]["name"], "Quoted")
        checked = self.ok(self.post("/paths/check", {"path": '"%s"' % folder}))
        self.assertEqual(checked["problem"], "")
        self.assertEqual(checked["path"], folder)


@unittest.skipIf(fake_llm is None, "Pillow is not installed")
class UnsavedKeyTest(AdminFixture, unittest.TestCase):
    """Listing models never sends a key from a file or variable to a new address."""

    def setUp(self):
        super().setUp()
        self.fake = fake_llm.FakeLLM().start()
        self.addCleanup(self.fake.stop)

    def keys_sent(self):
        return [entry["headers"].get("authorization") for entry in self.fake.requests
                if "headers" in entry]

    def test_a_key_file_or_variable_needs_a_saved_endpoint(self):
        for source in ({"api_key_file": self.key_file},
                       {"api_key_env": "SEARCH_IMAGES_TEST_KEY"}):
            fields = dict(source, preset="custom", base_url=self.fake.base_url)
            response = self.post("/endpoints/models", {"endpoint": fields})
            self.assertEqual(response.status_code, 400, source)
            self.assertIn("Save the endpoint first", response.get_json()["error"])
            # A saved endpoint moved elsewhere in the request: the same.
            response = self.post("/endpoints/models", {
                "endpoint": {"base_url": self.fake.base_url}, "original_name": "env"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake.requests, [])
        # A key typed in the request is the caller's own: fine.
        # Not a real key: a made-up value used only by this test.
        typed = "sk-test-typed-in-request-0000"
        self.ok(self.post("/endpoints/models", {"endpoint": {
            "preset": "custom", "base_url": self.fake.base_url, "api_key": typed}}))
        self.assertIn("Bearer " + typed, self.keys_sent())

    def test_a_saved_endpoint_lists_its_models_with_its_key(self):
        self.ok(self.post("/endpoints", {"endpoint": {
            "name": "gateway", "preset": "custom", "base_url": self.fake.base_url,
            "model": "m", "api_key_file": self.key_file}}))
        answer = self.ok(self.post("/endpoints/models", {
            "endpoint": {"api_key_file": self.key_file}, "original_name": "gateway"}))
        self.assertTrue(answer["models"])
        self.assertIn("Bearer " + FILE_KEY, self.keys_sent())


if __name__ == "__main__":
    unittest.main()
