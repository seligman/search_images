"""Tests for the refine pass (refine.py, describe.refine_pending, the refine job and
the --refine option), against fake LLM servers on 127.0.0.1."""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

import fake_llm
from test_describe import DescribeTestCase, reply

import admin
import common
import db
import describe
import jobs
import library_ops
import prompts
import refine
import scan
import serve
import settings

try:
    import flask
except ImportError:
    flask = None

DOCUMENT = {"photo_type": "document", "document_type": "letter"}
SCREENSHOT = {"photo_type": "screenshot", "document_type": "chat"}
RECEIPT_PHOTO = {"photo_type": "photo", "document_type": "receipt"}
PHOTO = {"photo_type": "photo", "document_type": "none"}


class SettingsTest(unittest.TestCase):

    def test_defaults_and_stored_values(self):
        self.assertEqual(refine.settings({}), {
            "endpoint": "", "photo_types": ["screenshot", "document", "scan"],
            "document_types": ["receipt", "chat", "web", "app", "letter", "form", "ticket",
                               "card_or_id", "book_page", "handwriting", "slide"],
            "max_output_tokens": 8000})
        settings_ = refine.settings({"refine": {
            "endpoint": " big ", "photo_types": ["scan", "bogus", "scan"],
            "document_types": "receipt", "max_output_tokens": 10}})
        self.assertEqual(settings_["endpoint"], "big")
        self.assertEqual(settings_["photo_types"], ["scan"])
        self.assertEqual(sorted(settings_["document_types"]),
                         sorted(refine.DEFAULT_DOCUMENT_TYPES))
        self.assertEqual(settings_["max_output_tokens"], 8000)
        self.assertFalse(refine.enabled({"refine": {"endpoint": ""}}))
        self.assertTrue(refine.enabled({"refine": {"endpoint": "big"}}))
        self.assertNotIn("none", refine.DOCUMENT_TYPE_CHOICES)
        self.assertNotIn("other", refine.PHOTO_TYPE_CHOICES)

    def test_what_the_page_may_send(self):
        config = {"endpoints": [{"name": "Big", "preset": "llamacpp", "model": "m"},
                                {"name": "off", "preset": "llamacpp", "model": "m",
                                 "enabled": False}]}
        self.assertEqual(refine.clean(config, None), ({}, {}))
        self.assertEqual(refine.clean(config, {"endpoint": ""}), ({}, {}))
        self.assertEqual(refine.clean(config, {"endpoint": "big"}), ({"endpoint": "Big"}, {}))
        stored, errors = refine.clean(config, {
            "endpoint": "big", "photo_types": ["screenshot", "document", "scan"],
            "document_types": ["receipt"], "max_output_tokens": "12000"})
        self.assertEqual(errors, {})
        self.assertEqual(stored, {"endpoint": "Big", "document_types": ["receipt"],
                                  "max_output_tokens": 12000})
        # The default kinds in another order are still the defaults.
        stored, errors = refine.clean(config, {
            "endpoint": "big", "photo_types": ["scan", "screenshot", "document"],
            "document_types": list(reversed(refine.DEFAULT_DOCUMENT_TYPES))})
        self.assertEqual((stored, errors), ({"endpoint": "Big"}, {}))
        for bad in ("big", {"endpoint": "off"}, {"endpoint": "nothing"},
                    {"endpoint": 3}, {"endpoint": "big", "colour": 1},
                    {"endpoint": "big", "photo_types": "scan"},
                    {"endpoint": "big", "photo_types": ["selfie"]},
                    {"endpoint": "big", "photo_types": [], "document_types": []},
                    {"endpoint": "big", "max_output_tokens": 100},
                    {"endpoint": "big", "max_output_tokens": True}):
            stored, errors = refine.clean(config, bad)
            self.assertIsNone(stored, bad)
            self.assertIn("refine", errors)

    def test_library_settings_and_renamed_endpoints(self):
        folder = os.path.abspath(os.path.join(os.sep, "photos-for-a-test"))
        config = {"endpoints": [{"name": "big", "preset": "llamacpp", "model": "m"}],
                  "libraries": [{"name": "Family", "path": folder,
                                 "refine": {"endpoint": "big"}}],
                  "local_server": {"endpoint": "big"}}
        changes, _warnings = library_ops.clean_library_fields(
            config, {"refine": {"endpoint": "big", "max_output_tokens": 9000}},
            config["libraries"][0])
        self.assertEqual(changes["refine"], {"endpoint": "big", "max_output_tokens": 9000})
        with self.assertRaises(library_ops.LibraryError) as caught:
            library_ops.clean_library_fields(config, {"refine": {"endpoint": "x"}},
                                             config["libraries"][0])
        self.assertIn("refine", caught.exception.fields)
        settings.rename_endpoint(config, "big", "bigger")
        self.assertEqual(config["libraries"][0]["refine"]["endpoint"], "bigger")
        self.assertEqual(config["local_server"]["endpoint"], "bigger")


class RefineTestCase(DescribeTestCase):
    """A library described by "local", refined by "big" (another fake server)."""

    def setUp(self):
        super().setUp()
        self.big = fake_llm.FakeLLM().start()
        self.addCleanup(self.big.stop)
        self.big.reply = reply(description="A letter from the bank about an account.",
                               title="Letter from the bank", photo_type="document",
                               document_type="letter", text="Dear customer,\nYour account...")

    def setup_library(self, count=5, refine_settings=None, **library):
        self.small_images(count)
        refine_settings = {"endpoint": "big"} if refine_settings is None else refine_settings
        self.configure([support.library("Family", self.folder, refine=refine_settings,
                                        **library)],
                       endpoints=[self.endpoint(name="local"),
                                  self.endpoint(name="big", base_url=self.big.base_url,
                                                model="big-model")],
                       default_endpoint="local")
        self.index()

    def set_info(self, path, kinds, provenance=None, prompt_ver=None, **row):
        info = dict(fake_llm.DEFAULT_REPLY, **kinds)
        provenance = provenance or {"model": "model.gguf", "endpoint": "local",
                                    "at": "2026-01-02T03:04:05Z"}
        values = {"info": json.dumps(info), "described": 1,
                  "prompt_ver": describe.PROMPT_VER if prompt_ver is None else prompt_ver,
                  "provenance": json.dumps(provenance)}
        values.update(row)
        columns = sorted(values)
        self.execute("UPDATE images SET %s WHERE path = ?"
                     % ", ".join("%s = ?" % column for column in columns),
                     [values[column] for column in columns] + [path])

    def refine(self, **kwargs):
        self.ctx = jobs.JobContext("refine", log_limit=5000)
        kwargs.setdefault("sleep", self.sleeps.append)
        conn = db.connect()
        try:
            return describe.refine_pending(conn, self.config, self.ctx, **kwargs)
        finally:
            conn.close()

    def library_row(self):
        conn = db.connect()
        try:
            return self.config["libraries"][0], conn
        except Exception:
            conn.close()
            raise


class CandidateTest(RefineTestCase):

    def test_which_pictures_are_refined(self):
        self.setup_library(count=8)
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", SCREENSHOT)
        self.set_info("photo2.jpg", RECEIPT_PHOTO)
        self.set_info("photo3.jpg", PHOTO)
        # Refined already with this prompt version; and with an older one.
        self.set_info("photo4.jpg", DOCUMENT, provenance={"tier": "refine"})
        self.set_info("photo5.jpg", DOCUMENT, provenance={"tier": "refine"},
                      prompt_ver=describe.PROMPT_VER - 1)
        self.set_info("photo6.jpg", DOCUMENT, missing_since=1.0)
        self.execute("UPDATE images SET info = 'not json', described = 1 "
                     "WHERE path = 'photo7.jpg'")
        library, conn = self.library_row()
        try:
            rows = refine.candidate_rows(conn, library)
            expected = ["photo0.jpg", "photo1.jpg", "photo2.jpg", "photo5.jpg"]
            self.assertEqual(sorted(row["path"] for row in rows), expected)
            self.assertEqual(refine.pending_count(conn, library), 4)
            with mock.patch.object(refine, "has_json1", return_value=False):
                self.assertEqual(sorted(row["path"] for row in refine.candidate_rows(
                    conn, library)), expected)
                self.assertEqual(refine.pending_count(conn, library), 4)
            # Pictures that failed three times are not counted.
            conn.execute("UPDATE images SET describe_attempts = 3 WHERE path = 'photo0.jpg'")
            conn.commit()
            self.assertEqual(refine.pending_count(conn, library), 3)
            # Narrower settings.
            library = dict(library, refine={"endpoint": "big", "photo_types": ["screenshot"],
                                            "document_types": []})
            self.assertEqual([row["path"] for row in refine.candidate_rows(conn, library)],
                             ["photo1.jpg"])
            self.assertEqual(refine.pending_count(conn, dict(library, refine={})), 0)
        finally:
            conn.close()


class RefinePassTest(RefineTestCase):

    def test_documents_are_described_again_with_the_refine_endpoint(self):
        self.setup_library(count=3)
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", PHOTO)
        self.set_info("photo2.jpg", SCREENSHOT)
        summary = self.refine()
        self.assertEqual((summary["described"], summary["failed"]), (2, 0), self.log_text())
        requests = self.big.describe_requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.server.describe_requests(), [])
        for request in requests:
            self.assertEqual(request["payload"]["max_tokens"], 8000)
            self.assertEqual(request["payload"]["response_format"]["json_schema"]["schema"],
                             prompts.schema("core"))
            self.assertEqual(request["system"], prompts.SYSTEM_PROMPT)
        rows = self.rows()
        info = json.loads(rows["photo0.jpg"]["info"])
        self.assertEqual(info["title"], "Letter from the bank")
        self.assertEqual(info["_model_name"], "model.gguf")
        provenance = json.loads(rows["photo0.jpg"]["provenance"])
        self.assertEqual(provenance["tier"], "refine")
        self.assertEqual(provenance["endpoint"], "big")
        self.assertEqual(provenance["previous"], {"model": "model.gguf", "endpoint": "local",
                                                  "at": "2026-01-02T03:04:05Z"})
        self.assertEqual(rows["photo0.jpg"]["prompt_ver"], describe.PROMPT_VER)
        self.assertEqual(json.loads(rows["photo1.jpg"]["info"])["photo_type"], "photo")
        self.assertIn("Refined photo0.jpg", self.log_text())
        self.assertIn("Refined 2 image(s)", self.log_text())
        # Nothing is left to refine; regular describing leaves refined rows alone.
        self.assertEqual(self.refine()["pending"], 0)
        self.assertIn("Nothing to refine", self.log_text())
        self.assertEqual(self.describe(redescribe_stale=True)["pending"], 0)
        self.assertEqual(json.loads(self.rows()["photo0.jpg"]["provenance"])["tier"], "refine")
        # Describing one picture again (on request) does replace it.
        self.server.reply = reply(photo_type="document", document_type="letter")
        self.assertEqual(self.describe(ids=[rows["photo0.jpg"]["id"]], force=True)["described"],
                         1)
        self.assertNotIn("tier", json.loads(self.rows()["photo0.jpg"]["provenance"]))
        self.assertEqual(self.refine()["described"], 1)

    def test_settings_limit_and_order(self):
        self.setup_library(count=3, refine_settings={
            "endpoint": "big", "max_output_tokens": 12000}, newest_first=False)
        for path in ("photo0.jpg", "photo1.jpg", "photo2.jpg"):
            self.set_info(path, DOCUMENT)
        summary = self.refine(limit=2)
        self.assertEqual(summary["described"], 2)
        self.assertIn("Refining 2 of 3 picture(s) (limit)", self.log_text())
        self.assertEqual({request["payload"]["max_tokens"]
                          for request in self.big.describe_requests()}, {12000})
        refined = sorted(path for path, row in self.rows().items()
                         if json.loads(row["provenance"]).get("tier") == "refine")
        self.assertEqual(refined, ["photo0.jpg", "photo1.jpg"])       # oldest first

    def test_failures_are_counted_and_skipped_later(self):
        self.setup_library(count=1)
        self.set_info("photo0.jpg", DOCUMENT)
        self.big.reply = fake_llm.Reply("", refusal="No.")
        summary = self.refine()
        self.assertEqual(summary["described"], 0)
        row = self.rows()["photo0.jpg"]
        self.assertEqual(row["described"], 1)                   # its description is kept
        self.assertEqual(json.loads(row["info"])["photo_type"], "document")
        self.execute("UPDATE images SET describe_attempts = 3")
        summary = self.refine()
        self.assertEqual((summary["pending"], summary["skipped_failed"]), (0, 1))
        self.big.reply = reply(title="Letter", description="A letter.")
        self.assertEqual(self.refine(retry_failed=True)["described"], 1)

    def test_exact_copies_are_refined_once(self):
        self.setup_library(count=2)
        self.execute("UPDATE images SET content_hash = 'same'")
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", DOCUMENT)
        summary = self.refine()
        self.assertEqual((summary["described"], summary["shared"]), (1, 1))
        self.assertEqual(len(self.big.describe_requests()), 1)
        for row in self.rows().values():
            self.assertEqual(json.loads(row["provenance"])["tier"], "refine")

    def test_libraries_without_a_refine_endpoint(self):
        self.setup_library(count=1, refine_settings={})
        self.set_info("photo0.jpg", DOCUMENT)
        summary = self.refine()
        self.assertEqual((summary["pending"], summary["error"]), (0, ""))
        self.assertIn("No library names a refine endpoint", self.log_text())
        summary = self.refine(library_ids=[self.config["libraries"][0]["id"]],
                              only_library=True)
        self.assertIn("has no refine endpoint", summary["error"])
        self.assertEqual(self.big.describe_requests(), [])

    def test_a_missing_refine_endpoint_skips_the_library(self):
        self.setup_library(count=1)
        self.set_info("photo0.jpg", DOCUMENT)
        self.config["endpoints"][1]["enabled"] = False
        summary = self.refine()
        self.assertEqual((summary["described"], summary["skipped"]), (0, 1))
        self.assertIn("its refine endpoint", self.log_text())

    def test_truncated_replies_are_kept_and_flagged(self):
        self.setup_library(count=1)
        self.set_info("photo0.jpg", DOCUMENT)
        text = reply(title="Letter", description="A long letter.", text="x" * 300)
        self.big.reply = fake_llm.Reply(text[:text.index('"x') + 100], finish="length")
        self.assertEqual(self.refine()["described"], 1)
        row = self.rows()["photo0.jpg"]
        self.assertTrue(json.loads(row["provenance"])["truncated"])
        self.assertEqual(json.loads(row["info"])["title"], "Letter")

    def test_a_cut_off_refine_reply_keeps_the_old_values_and_privacy_flags(self):
        self.setup_library(count=1)
        self.set_info("photo0.jpg", dict(DOCUMENT, private_info=True, sensitive="suggestive",
                                         tags=["bank", "letter"], subjects=["Letter"]))
        text = reply(title="Letter from the bank", description="A long letter.",
                     photo_type="document", document_type="letter",
                     text="".join("Line %d of the letter.\n" % number for number in range(400)),
                     private_info=False, sensitive="none", tags=["other"])
        self.big.reply = fake_llm.Reply(text[:text.index('"text"') + 3000], finish="length")
        self.assertEqual(self.refine()["described"], 1)
        self.assertEqual(len(self.big.describe_requests()), 1)
        row = self.rows()["photo0.jpg"]
        info = json.loads(row["info"])
        self.assertEqual(info["title"], "Letter from the bank")         # the new answer
        self.assertTrue(info["text"].startswith("Line 0 of the letter."))
        self.assertEqual(info["tags"], ["bank", "letter"])              # not reached: the old
        self.assertEqual(info["subjects"], ["Letter"])
        self.assertIs(info["private_info"], True)
        self.assertEqual(info["sensitive"], "suggestive")
        provenance = json.loads(row["provenance"])
        self.assertEqual((provenance["tier"], provenance["truncated"]), ("refine", True))
        self.assertIn("tags", provenance["missing_fields"])
        # Reached fields never weaken the old privacy flags either.
        self.assertEqual(describe.fill_missing(
            {"private_info": False, "sensitive": "none", "tags": []}, [],
            {"private_info": True, "sensitive": "explicit", "tags": ["x"]}),
            {"private_info": True, "sensitive": "explicit", "tags": []})

    def test_the_endpoints_own_larger_limit_is_kept(self):
        self.setup_library(count=1)
        self.config["endpoints"][1]["max_output_tokens"] = 32000
        self.set_info("photo0.jpg", DOCUMENT)
        self.assertEqual(self.refine()["described"], 1)
        self.assertEqual(self.big.describe_requests()[0]["payload"]["max_tokens"], 32000)

    def test_counts_for_the_manage_page(self):
        self.setup_library(count=4)
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", DOCUMENT, hidden=1)
        self.set_info("photo2.jpg", DOCUMENT, describe_attempts=3)
        self.set_info("photo3.jpg", DOCUMENT, hidden=1, describe_attempts=3)
        library, conn = self.library_row()
        try:
            self.assertEqual(refine.counts(conn, library),
                             {"pending": 2, "hidden": 0, "failed": 2})
            self.assertEqual(refine.counts(conn, library, include_hidden=False),
                             {"pending": 1, "hidden": 2, "failed": 1})
            with mock.patch.object(refine, "has_json1", return_value=False):
                self.assertEqual(refine.counts(conn, library, include_hidden=False),
                                 {"pending": 1, "hidden": 2, "failed": 1})
            self.assertEqual(refine.pending_count(conn, library, include_hidden=False), 1)
            self.assertEqual(refine.counts(conn, dict(library, refine={})),
                             {"pending": 0, "hidden": 0, "failed": 0})
        finally:
            conn.close()


class CommandLineRefineTest(RefineTestCase):

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = scan.main(list(args))
        return code, out.getvalue()

    def test_scan_py_refine(self):
        self.setup_library(count=2)
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", PHOTO)
        code, output = self.run_main("--refine", "--library", "Family", "--limit", "5")
        self.assertEqual(code, 0, output)
        self.assertIn("Refined 1 image(s)", output)
        self.assertEqual(len(self.big.describe_requests()), 1)
        self.assertEqual(self.server.describe_requests(), [])       # no regular describing
        code, output = self.run_main("--refine", "--endpoint", "big")
        self.assertEqual(code, 2)
        self.assertIn("--refine uses each library's refine endpoint", output)


@unittest.skipIf(flask is None, "Flask is not installed")
class RefineJobTest(RefineTestCase):

    def setUp(self):
        super().setUp()
        self.setup_library(count=2)
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", PHOTO)
        self.manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(self.manager.stop, 30)
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()
        self.library_id = self.config["libraries"][0]["id"]

    def request(self, path, body=None):
        headers = {"X-Search-Images": "1"}
        if body is None:
            return self.client.get(admin.URL_PREFIX + path, headers=headers)
        return self.client.post(admin.URL_PREFIX + path, data=json.dumps(body),
                                content_type="application/json", headers=headers)

    def test_the_manage_page_counts_and_runs_the_refine_pass(self):
        state = self.request("/state").get_json()
        [library] = state["libraries"]
        info = library["refine_info"]
        self.assertEqual((info["enabled"], info["ready"], info["pending"], info["cloud"]),
                         (True, True, 1, False))
        self.assertEqual((info["hidden"], info["failed"], info["local"]), (0, 0, True))
        self.assertEqual(info["settings"]["endpoint"], "big")
        self.assertIn("screenshot", state["refine_choices"]["photo_types"])
        # Settings lists the library under the refine endpoint's "Used by".
        endpoints = {item["name"]: item for item in state["endpoints"]}
        self.assertEqual(endpoints["big"]["refined_by"], [self.library_id])
        self.assertNotIn(self.library_id, endpoints["big"]["used_by"])
        response = self.request("/jobs", {"kind": "refine", "library_id": self.library_id,
                                          "params": {"limit": 10}})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertTrue(self.manager.start())
        self.assertTrue(self.manager.wait_idle(60))
        job = self.manager.get(response.get_json()["job"]["id"])
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(job["summary"]["described"], 1)
        self.assertEqual(self.request("/state").get_json()["libraries"][0]["refine_info"][
            "pending"], 0)
        response = self.request("/jobs", {"kind": "refine", "params": {"force": True}})
        self.assertEqual(response.status_code, 400)

    def test_a_cloud_refine_endpoint_needs_a_key_and_never_counts_hidden_pictures(self):
        self.set_info("photo1.jpg", DOCUMENT, hidden=1)
        config = common.load_config()
        config["endpoints"].append({"name": "cloud", "preset": "openai", "model": "gpt",
                                    "api_key_env": "SEARCH_IMAGES_TEST_NO_SUCH_KEY"})
        config["libraries"][0]["refine"] = {"endpoint": "cloud"}
        common.save_config(config)
        info = self.request("/state").get_json()["libraries"][0]["refine_info"]
        self.assertFalse(info["ready"])
        self.assertIn("The refine endpoint cloud has no API key", info["problem"])
        self.assertEqual((info["pending"], info["hidden"], info["cloud"]), (1, 1, False))
        response = self.request("/jobs", {"kind": "refine", "library_id": self.library_id})
        self.assertEqual(response.status_code, 202)     # (the run itself sends nothing)
        # With a key it is ready; the hidden document still is not counted.
        # Not a real key: a made-up value used only by this test.
        config["endpoints"][-1]["api_key"] = "test-key-refine-0101"
        common.save_config(config)
        info = self.request("/state").get_json()["libraries"][0]["refine_info"]
        self.assertEqual((info["ready"], info["cloud"], info["local"]), (True, True, False))
        self.assertEqual((info["pending"], info["hidden"]), (1, 1))
        self.assertEqual(info["cloud_hosts"], ["api.openai.com"])

    def test_a_library_without_a_refine_endpoint_cannot_be_refined(self):
        response = self.request("/libraries/%s" % self.library_id, {"refine": {}})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(common.load_config()["libraries"][0]["refine"], {})
        response = self.request("/jobs", {"kind": "refine", "library_id": self.library_id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("has no refine endpoint", response.get_json()["error"])
        response = self.request("/jobs", {"kind": "refine"})
        self.assertEqual(response.status_code, 400)
        response = self.request("/libraries/%s" % self.library_id,
                                {"refine": {"endpoint": "nothing"}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("refine", response.get_json()["fields"])
        response = self.request("/libraries/%s" % self.library_id,
                                {"refine": {"endpoint": "big", "document_types": ["receipt"]}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(common.load_config()["libraries"][0]["refine"],
                         {"endpoint": "big", "document_types": ["receipt"]})


if __name__ == "__main__":
    unittest.main()
