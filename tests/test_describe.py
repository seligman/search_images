"""Tests for describe.py (and the describe step of scan.py) against a fake LLM."""

import contextlib
import io
import json
import os
import socket
import threading
import time
import types
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

from PIL import Image

import fake_llm
import imagegen

import common
import db
import describe
import images
import jobs
import llm
import prompts
import scan

# Not a real key: a made-up value used to check that keys never leak.
SECRET = "sk-test-0123456789abcdefSECRETKEY"
KEY_VARIABLES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                 "OPENROUTER_API_KEY", "AZURE_OPENAI_API_KEY")


def reply(**changes):
    data = dict(fake_llm.DEFAULT_REPLY)
    data.update(changes)
    return json.dumps(data)


def closed_port():
    """A 127.0.0.1 port nothing listens on."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class DescribeTestCase(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        self.folder = self.base.library_folder("Family")
        self.sleeps = []
        for patcher in (mock.patch.object(describe, "UNREACHABLE_WAIT", 0),
                        mock.patch.dict(os.environ)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in KEY_VARIABLES + ("LOG_ERRORS",):
            os.environ.pop(name, None)
        llm._no_stream_options.clear()
        self.addCleanup(llm._no_stream_options.clear)

    def endpoint(self, **settings):
        endpoint = {"kind": "openai", "base_url": self.server.base_url, "api_key": "",
                    "model": "fake-model"}
        endpoint.update(settings)
        return endpoint

    def configure(self, libraries=None, endpoint=None, **extra):
        libraries = libraries if libraries is not None else [
            support.library("Family", self.folder)]
        if "endpoints" not in extra:
            extra["endpoint"] = endpoint or self.endpoint()
        self.base.write_config(libraries, **extra)
        self.config = common.load_config()
        db.init(self.config, log=lambda message: None)
        self.config = common.load_config()
        return self.config

    def index(self):
        ctx = jobs.JobContext("index")
        scan.run(self.config, ctx, describe_images=False)

    def describe(self, helper=None, **kwargs):
        self.ctx = jobs.JobContext("describe", log_limit=5000)
        kwargs.setdefault("sleep", self.sleeps.append)
        conn = db.connect()
        try:
            return describe.describe_pending(conn, self.config, self.ctx, helper=helper,
                                             **kwargs)
        finally:
            conn.close()

    def rows(self):
        conn = db.connect()
        try:
            return {row["path"]: dict(row) for row in conn.execute("SELECT * FROM images")}
        finally:
            conn.close()

    def execute(self, sql, args=()):
        conn = db.connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=5000)["log_tail"])

    def small_images(self, count, folder=None, prefix="photo", first_block=8):
        """count distinct JPEGs (different block sizes from first_block on)."""
        names = []
        for index in range(count):
            name = "%s%d.jpg" % (prefix, index)
            imagegen.jpeg_with_exif(folder or self.folder, name,
                                    image=imagegen.blocks_image(first_block + index),
                                    date="2019:05:%02d 14:23:01" % (index + 1))
            names.append(name)
        return names

    def attempts(self):
        return {path: row["describe_attempts"] for path, row in self.rows().items()}


class DescribeTest(DescribeTestCase):

    def test_reply_is_stored_and_the_image_is_prepared(self):
        imagegen.large_jpeg(self.folder, "rotated.jpg", size=(3000, 2000), orientation=6)
        self.configure()
        self.index()
        summary = self.describe()
        self.assertEqual(summary["described"], 1)
        self.assertEqual((summary["requests"], summary["tokens_in"], summary["tokens_out"]),
                         (1, 10, 20))
        row = self.rows()["rotated.jpg"]
        self.assertEqual((row["described"], row["prompt_ver"]), (1, describe.PROMPT_VER))
        self.assertEqual((row["describe_attempts"], row["describe_error"]), (0, ""))
        info = json.loads(row["info"])
        self.assertEqual(info["description"], fake_llm.DEFAULT_REPLY["description"])
        self.assertEqual(info["title"], fake_llm.DEFAULT_REPLY["title"])
        self.assertEqual(info["tags"], ["test", "red", "blue"])
        self.assertEqual(info["aesthetic"], 2)
        # A local llama.cpp-like server: its /props names the model.
        self.assertEqual(info["_model_name"], "model.gguf")
        provenance = json.loads(row["provenance"])
        self.assertEqual(provenance["model"], "model.gguf")
        self.assertEqual(provenance["endpoint"], "default")
        self.assertEqual(provenance["prompt_ver"], describe.PROMPT_VER)
        self.assertEqual(provenance["prompt_hash"], prompts.prompt_hash("core"))
        self.assertEqual(provenance["profile"], "core")
        self.assertEqual((provenance["tokens_in"], provenance["tokens_out"]), (10, 20))
        self.assertEqual(provenance["requests"], 1)
        self.assertRegex(provenance["at"],
                         r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
        self.assertGreaterEqual(provenance["seconds"], 0)
        self.assertNotIn("cost", provenance)
        # What the model received: the v5 prompt, a JPEG, upright, long edge
        # 1536, and nothing else (no file name or path).
        [request] = self.server.describe_requests()
        self.assertEqual(request["system"], prompts.SYSTEM_PROMPT)
        self.assertEqual(request["prompt"], "Describe this image.")
        self.assertEqual(request["parts"], ["image_url", "text"])
        self.assertEqual(request["payload"]["response_format"]["json_schema"]["schema"],
                         prompts.schema("core"))
        self.assertEqual(request["mime"], "image/jpeg")
        self.assertNotIn("rotated", json.dumps(request["payload"]))
        with Image.open(io.BytesIO(request["image"])) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (1024, 1536))
            image = image.convert("RGB")
            width = image.size[0]
            # The red corner of the stored (sideways) image ends up top right.
            red, _, blue = image.getpixel((width - 20, 20))
            self.assertGreater(red, 150)
            self.assertLess(blue, 100)
            red, _, blue = image.getpixel((20, 20))
            self.assertLess(red, 100)
            self.assertGreater(blue, 150)

    def test_original_is_used_without_a_preview(self):
        imagegen.large_jpeg(self.folder, "rotated.jpg", size=(3000, 2000), orientation=8)
        self.configure([support.library("Family", self.folder, previews=False)])
        self.index()
        self.assertEqual(self.rows()["rotated.jpg"]["has_preview"], 0)
        self.assertEqual(self.describe()["described"], 1)
        with Image.open(io.BytesIO(self.server.images()[0])) as image:
            self.assertEqual(image.size, (1024, 1536))

    def test_endpoint_image_size_and_library_settings(self):
        imagegen.large_jpeg(self.folder, "big.jpg", size=(3000, 2000))
        self.configure([support.library("Family", self.folder, metadata="extended",
                                        context_notes="Pictures from a sailing club.")],
                       endpoint=self.endpoint(max_image_edge=800))
        self.index()
        extended = dict(fake_llm.DEFAULT_REPLY, objects=[{"name": "boat", "count": 2}],
                        activities=["sailing"], landmarks=[], location_guess="",
                        mood="calm")
        self.server.reply = json.dumps(extended)
        self.assertEqual(self.describe()["described"], 1)
        [request] = self.server.describe_requests()
        self.assertEqual(request["system"], prompts.system_prompt("extended"))
        self.assertIn("Pictures from a sailing club.", request["prompt"])
        self.assertIn("objects", request["payload"]["response_format"]["json_schema"]
                      ["schema"]["properties"])
        with Image.open(io.BytesIO(request["image"])) as image:
            self.assertEqual(max(image.size), 1152)     # the short side stays >= 768
        row = self.rows()["big.jpg"]
        self.assertEqual(json.loads(row["info"])["objects"], [{"name": "boat", "count": 2}])
        provenance = json.loads(row["provenance"])
        self.assertEqual((provenance["profile"], provenance["prompt_hash"]),
                         ("extended", prompts.prompt_hash("extended")))

    def test_bad_reply_counts_a_failed_attempt(self):
        self.small_images(2)
        self.configure()
        self.index()
        # The endpoint works (photo1, newest first), then cannot describe photo0.
        self.server.queue(reply())
        self.server.reply = "I cannot do that."
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["failed"]), (1, 1))
        self.assertEqual(len(self.server.describe_requests()), 2 + describe.CONTENT_RETRIES)
        row = self.rows()["photo0.jpg"]
        self.assertEqual((row["described"], row["describe_attempts"]), (0, 1))
        self.assertIn("no JSON object", row["describe_error"])
        # In a run where the endpoint never works, the image is not charged.
        summary = self.describe()
        self.assertEqual((summary["failed"], summary["skipped"]), (0, 1))
        self.assertEqual(self.rows()["photo0.jpg"]["describe_attempts"], 1)

    def test_unusable_replies_before_any_success_do_not_fail_the_images(self):
        self.small_images(5)
        self.configure()
        self.index()
        self.server.reply = fake_llm.Reply("", refusal="No.")
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (0, 0))
        self.assertEqual(summary["retired"], ["default"])
        self.assertIn("gave only unusable replies", self.log_text())
        # Three images, each asked 1 + CONTENT_RETRIES times.
        self.assertEqual(len(self.server.describe_requests()),
                         describe.MAX_ENDPOINT_FAILURES * (1 + describe.CONTENT_RETRIES))
        self.assertEqual(set(self.attempts().values()), {0})

    def test_content_retries_change_the_sampling_of_local_models(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.queue(fake_llm.Reply(reply()[:40], finish="length"),
                          reply(description=""), reply())
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (1, 0))
        first, second, third = self.server.describe_requests()
        self.assertNotIn("repeat_penalty", first["payload"])
        self.assertNotIn("seed", first["payload"])
        self.assertEqual(second["payload"]["repeat_penalty"], 1.1)
        self.assertEqual(third["payload"]["repeat_penalty"], 1.1)
        self.assertNotEqual(second["payload"]["seed"], third["payload"]["seed"])
        row = self.rows()["photo0.jpg"]
        self.assertEqual(row["describe_attempts"], 0)
        self.assertEqual(json.loads(row["provenance"])["requests"], 3)

    def test_content_retries_of_cloud_and_ollama_endpoints(self):
        self.small_images(1)
        # Not a real key: a made-up value used only by this test.
        cloud = self.endpoint(preset="openai", api_key="test-key-0101")
        self.configure(endpoint=cloud)
        self.index()
        self.server.queue(fake_llm.Reply("", refusal="I can't help with that."), reply())
        self.assertEqual(self.describe()["described"], 1)
        retry = self.server.describe_requests()[-1]["payload"]
        self.assertNotIn("repeat_penalty", retry)
        self.assertNotIn("seed", retry)
        self.config["endpoint"] = self.endpoint(kind="ollama", base_url=self.server.root_url)
        self.execute("UPDATE images SET described = 0, info = ''")
        self.server.queue("not json")
        self.assertEqual(self.describe()["described"], 1)
        options = self.server.describe_requests()[-1]["payload"]["options"]
        self.assertEqual(options["repeat_penalty"], 1.1)
        self.assertIn("seed", options)
        self.assertIn("num_predict", options)

    def test_refusals_and_cut_off_replies_fail_after_the_retries(self):
        self.small_images(3)
        self.configure()
        self.index()
        self.server.queue(reply())
        self.server.reply = fake_llm.Reply("", refusal="Not allowed.")
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["failed"]), (1, 2))
        errors = {row["describe_error"] for path, row in self.rows().items()
                  if path != "photo2.jpg"}
        self.assertEqual(len(errors), 1)
        self.assertIn("refused", errors.pop())
        self.execute("UPDATE images SET described = 0 WHERE path = 'photo2.jpg'")
        self.server.queue(reply())
        self.server.reply = fake_llm.Reply(reply()[:50], finish="length")
        self.describe(newest_first=True)
        self.assertIn("cut off", self.rows()["photo0.jpg"]["describe_error"])
        self.assertEqual({path: attempts for path, attempts in self.attempts().items()},
                         {"photo0.jpg": 2, "photo1.jpg": 2, "photo2.jpg": 0})

    def test_looping_replies(self):
        self.small_images(2)
        self.configure()
        self.index()
        # A description that loops is unusable...
        self.server.queue(reply())
        self.server.reply = reply(description="nice " * 200)
        self.assertEqual(self.describe(newest_first=True)["failed"], 1)
        self.assertIn("repeats itself", self.rows()["photo0.jpg"]["describe_error"])
        # ...but a transcription whose end loops is cut, not rejected.
        self.server.reply = reply(text="Menu\nSoup 4.50\n" + "ha " * 200)
        self.assertEqual(self.describe()["described"], 1)
        self.assertEqual(json.loads(self.rows()["photo0.jpg"]["info"])["text"],
                         "Menu\nSoup 4.50\nha")

    def test_images_that_failed_three_times_are_skipped_unless_retried(self):
        self.small_images(2)
        self.configure()
        self.index()
        self.execute("UPDATE images SET describe_attempts = 3, describe_error = 'old' "
                     "WHERE path = 'photo0.jpg'")
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped_failed"]), (1, 1))
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.assertEqual(self.rows()["photo0.jpg"]["described"], 0)
        self.assertEqual(self.describe()["pending"], 0)
        summary = self.describe(retry_failed=True)
        self.assertEqual(summary["described"], 1)
        row = self.rows()["photo0.jpg"]
        self.assertEqual((row["described"], row["describe_attempts"], row["describe_error"]),
                         (1, 0, ""))

    def test_exact_copies_are_described_once(self):
        self.small_images(1)
        os.makedirs(os.path.join(self.folder, "Copy"))
        with open(os.path.join(self.folder, "photo0.jpg"), "rb") as handle:
            data = handle.read()
        with open(os.path.join(self.folder, "Copy", "photo0.jpg"), "wb") as handle:
            handle.write(data)
        self.configure()
        self.index()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["shared"]), (1, 1))
        self.assertEqual(len(self.server.describe_requests()), 1)
        rows = self.rows()
        self.assertEqual(rows["photo0.jpg"]["info"], rows["Copy/photo0.jpg"]["info"])

    def library_rows(self, library_id):
        conn = db.connect()
        try:
            return {row["path"]: dict(row) for row in conn.execute(
                "SELECT * FROM images WHERE library_id = ?", (library_id,))}
        finally:
            conn.close()

    def test_copies_share_only_with_libraries_that_ask_the_same(self):
        self.small_images(1)
        with open(os.path.join(self.folder, "photo0.jpg"), "rb") as handle:
            data = handle.read()
        folders = {}
        for name in ("Twin", "Ext", "Notes"):
            folders[name] = self.base.library_folder(name)
            with open(os.path.join(folders[name], "photo0.jpg"), "wb") as handle:
                handle.write(data)
        self.configure([support.library("Family", self.folder, id="lib_family1"),
                        support.library("Twin", folders["Twin"], id="lib_twin0001"),
                        support.library("Ext", folders["Ext"], id="lib_ext00001",
                                        metadata="extended"),
                        support.library("Notes", folders["Notes"], id="lib_notes001",
                                        context_notes="Pictures from a sailing club.")])
        self.index()
        summary = self.describe()
        # Family's description goes to Twin (same settings); Ext and Notes
        # ask for something else and are described themselves.
        self.assertEqual((summary["described"], summary["shared"]), (3, 1))
        self.assertEqual(len(self.server.describe_requests()), 3)
        twin = self.library_rows("lib_twin0001")["photo0.jpg"]
        self.assertEqual(twin["info"], self.library_rows("lib_family1")["photo0.jpg"]["info"])
        ext = self.library_rows("lib_ext00001")["photo0.jpg"]
        self.assertEqual(json.loads(ext["provenance"])["profile"], "extended")
        self.assertIn("objects", json.loads(ext["info"]))
        # A copy described with an older prompt is only replaced on request.
        self.execute("UPDATE images SET prompt_ver = 4 WHERE library_id = 'lib_twin0001'")
        self.execute("UPDATE images SET described = 0, info = '' "
                     "WHERE library_id = 'lib_family1'")
        summary = self.describe()
        self.assertEqual((summary["described"], summary["shared"], summary["stale"]),
                         (1, 0, 1))
        self.assertEqual(self.library_rows("lib_twin0001")["photo0.jpg"]["prompt_ver"], 4)
        self.execute("UPDATE images SET described = 0, info = '' "
                     "WHERE library_id = 'lib_family1'")
        requests = len(self.server.describe_requests())
        summary = self.describe(redescribe_stale=True)
        self.assertEqual((summary["described"], summary["shared"]), (1, 1))
        self.assertEqual(len(self.server.describe_requests()), requests + 1)
        self.assertEqual(self.library_rows("lib_twin0001")["photo0.jpg"]["prompt_ver"],
                         describe.PROMPT_VER)

    def test_scan_copies_a_description_only_to_a_library_that_fits(self):
        self.small_images(1)
        self.configure([support.library("Family", self.folder, id="lib_family1")])
        self.index()
        self.assertEqual(self.describe()["described"], 1)
        with open(os.path.join(self.folder, "photo0.jpg"), "rb") as handle:
            data = handle.read()
        folders = {}
        for name in ("Twin", "Ext"):
            folders[name] = self.base.library_folder(name)
            with open(os.path.join(folders[name], "copy.jpg"), "wb") as handle:
                handle.write(data)
        self.configure([support.library("Family", self.folder, id="lib_family1"),
                        support.library("Twin", folders["Twin"], id="lib_twin0001"),
                        support.library("Ext", folders["Ext"], id="lib_ext00001",
                                        metadata="extended")])
        self.index()
        self.assertEqual(self.library_rows("lib_twin0001")["copy.jpg"]["described"], 1)
        self.assertEqual(self.library_rows("lib_ext00001")["copy.jpg"]["described"], 0)

    def test_provenance_counts_every_request(self):
        self.small_images(1)
        self.configure(endpoint=self.endpoint(price_in=1.0, price_out=2.0))
        self.index()
        self.server.queue("not json at all", reply())
        summary = self.describe()
        provenance = json.loads(self.rows()["photo0.jpg"]["provenance"])
        self.assertEqual((provenance["tokens_in"], provenance["tokens_out"],
                          provenance["requests"]), (20, 40, 2))
        self.assertEqual((summary["tokens_in"], summary["tokens_out"]), (20, 40))
        self.assertAlmostEqual(provenance["cost"], summary["cost"])
        self.assertAlmostEqual(provenance["cost"], (20 * 1.0 + 40 * 2.0) / 1e6)

    def test_only_enabled_libraries_and_present_files_hidden_included(self):
        other = self.base.library_folder("Other")
        self.small_images(3)
        imagegen.jpeg_with_exif(other, "other.jpg", image=imagegen.blocks_image(20))
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other)])
        self.index()
        self.config["libraries"][1]["enabled"] = False
        family = self.config["libraries"][0]["id"]
        self.execute("UPDATE images SET missing_since = 1 WHERE path = 'photo1.jpg' "
                     "AND library_id = ?", (family,))
        self.execute("UPDATE images SET hidden = 1 WHERE path = 'photo2.jpg'")
        summary = self.describe()
        self.assertEqual(summary["described"], 2)
        described = [(row["library_id"], row["path"]) for row in self.query_described()]
        self.assertEqual(described, [(family, "photo0.jpg"), (family, "photo2.jpg")])

    def query_described(self):
        conn = db.connect()
        try:
            return conn.execute("SELECT library_id, path FROM images WHERE described = 1 "
                                "ORDER BY path").fetchall()
        finally:
            conn.close()

    def test_newest_first_handles_dates_before_1970(self):
        imagegen.jpeg_with_exif(self.folder, "old.jpg", image=imagegen.blocks_image(9),
                                date="1965:07:04 10:00:00")
        imagegen.jpeg_with_exif(self.folder, "new.jpg", image=imagegen.blocks_image(10),
                                date="2021:07:04 10:00:00")
        imagegen.jpeg_with_exif(self.folder, "mid.jpg", image=imagegen.blocks_image(11),
                                date="1999:07:04 10:00:00")
        self.configure()
        self.index()
        # No capture date, and a file time in 1966: sorted between the others.
        self.execute("UPDATE images SET taken_at = '', mtime = -1.0e8 WHERE path = 'mid.jpg'")
        conn = db.connect()
        try:
            newest, _ = describe.pending_rows(conn, self.config["libraries"], newest_first=True)
            oldest, _ = describe.pending_rows(conn, self.config["libraries"], newest_first=False)
        finally:
            conn.close()
        self.assertEqual([row["path"] for row in newest], ["new.jpg", "mid.jpg", "old.jpg"])
        self.assertEqual([row["path"] for row in oldest], ["old.jpg", "mid.jpg", "new.jpg"])
        self.assertEqual(self.describe(newest_first=True)["described"], 3)

    def test_stale_descriptions_only_on_request_and_after_new_ones(self):
        self.small_images(3)
        self.configure()
        self.index()
        old_info = json.dumps(fake_llm.V4_REPLY)
        self.execute("UPDATE images SET described = 1, prompt_ver = 4, info = ? "
                     "WHERE path = 'photo2.jpg'", (old_info,))
        older_info = json.dumps(dict(fake_llm.V4_REPLY, description="Another picture."))
        conn = db.connect()
        try:
            rows, _ = describe.pending_rows(conn, self.config["libraries"])
            self.assertEqual([row["path"] for row in rows], ["photo1.jpg", "photo0.jpg"])
            rows, _ = describe.pending_rows(conn, self.config["libraries"],
                                            redescribe_stale=True)
            self.assertEqual([row["path"] for row in rows],
                             ["photo1.jpg", "photo0.jpg", "photo2.jpg"])
        finally:
            conn.close()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["stale"]), (2, 1))
        self.assertIn("--redescribe-stale", self.log_text())
        self.assertEqual(self.rows()["photo2.jpg"]["info"], old_info)
        # A failed re-describe keeps the older description.
        self.execute("UPDATE images SET prompt_ver = 4, info = ? WHERE path = 'photo1.jpg'",
                     (older_info,))
        self.server.queue(reply())                          # photo2 (newest) works
        self.server.reply = "garbage"
        summary = self.describe(redescribe_stale=True, newest_first=True)
        self.assertEqual((summary["described"], summary["failed"], summary["stale"]),
                         (1, 1, 0))
        row = self.rows()["photo1.jpg"]
        self.assertEqual((row["info"], row["described"], row["describe_attempts"]),
                         (older_info, 1, 1))
        self.assertEqual(row["prompt_ver"], 4)
        self.server.reply = reply()
        self.assertEqual(self.describe(redescribe_stale=True)["described"], 1)
        self.assertEqual({row["prompt_ver"] for row in self.rows().values()},
                         {describe.PROMPT_VER})

    def test_limit(self):
        self.small_images(4)
        self.configure()
        self.index()
        summary = self.describe(limit=2, newest_first=True)
        self.assertEqual((summary["described"], summary["pending"]), (2, 2))
        self.assertEqual(sorted(path for path, row in self.rows().items() if row["described"]),
                         ["photo2.jpg", "photo3.jpg"])
        self.assertEqual(self.describe(limit=0)["described"], 0)
        self.assertEqual(self.describe()["described"], 2)

    def test_stale_preview_file_is_not_sent(self):
        # A preview file without the has_preview flag may show an older
        # picture (its write failed after the content changed): the original
        # is used instead.
        imagegen.large_jpeg(self.folder, "rotated.jpg", size=(3000, 2000), orientation=6)
        self.configure()
        self.index()
        row = self.rows()["rotated.jpg"]
        store = describe.cache.PreviewStore()
        store.write(row["library_id"], row["id"],
                    imagegen.jpeg_bytes(imagegen.marker_image((300, 200))))
        self.execute("UPDATE images SET has_preview = 0")
        self.assertEqual(self.describe()["described"], 1)
        with Image.open(io.BytesIO(self.server.images()[0])) as image:
            self.assertEqual(image.size, (1024, 1536))

    def test_progress_lines(self):
        self.small_images(2)
        self.configure()
        self.index()
        with mock.patch.object(describe, "PROGRESS_SECONDS", 0):
            self.describe()
        self.assertRegex(self.log_text(), r"Progress: 2/2 image\(s\), [\d.]+ images/min, "
                                          r"0 failed")
        snapshot = self.ctx.snapshot()
        self.assertEqual((snapshot["stage"], snapshot["done"], snapshot["total"]),
                         ("describe", 2, 2))


    def test_pictures_in_an_offline_folder_wait_for_it(self):
        away = self.base.library_folder("Away")
        phone = self.base.library_folder("Phone")
        self.small_images(2)
        self.small_images(3, away, prefix="away", first_block=30)
        self.small_images(2, phone, prefix="phone", first_block=50)
        self.configure([support.library("Family", self.folder),
                        support.library("Away", away, previews=False),
                        support.library("Phone", phone)])
        self.index()
        # Both shares go offline: Phone's pictures have previews, Away's do not.
        support.remove_tree(away)
        support.remove_tree(phone)
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"], summary["skipped_offline"]),
                         (4, 0, 3))
        self.assertEqual(len(self.server.describe_requests()), 4)
        attempts = self.attempts()
        self.assertEqual({attempts[name] for name in attempts if name.startswith("away")}, {0})
        self.assertEqual(sum(row["described"] for row in self.rows().values()), 4)
        # One line about the waiting pictures, not one per picture.
        warnings = [entry["message"] for entry in self.ctx.snapshot(log_tail=5000)["log_tail"]
                    if entry["level"] == "warn"]
        self.assertEqual(len([text for text in warnings if "Away" in text]), 1, warnings)
        self.assertIn("3 image(s) without a preview are left for later", "\n".join(warnings))
        # Nothing left that can be described now: no misleading "all done".
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped_offline"]), (0, 3))
        self.assertNotIn("All images already have descriptions", self.log_text())

    def test_time_left_reads_well(self):
        self.assertEqual(describe._duration(0.5), "less than a minute")
        self.small_images(3)
        self.configure()
        self.index()
        with mock.patch.object(describe, "PROGRESS_SECONDS", 0):
            self.describe()
        self.assertNotIn("about less than", self.log_text())
        self.assertNotIn("under a minute", self.log_text())


class EndpointTest(DescribeTestCase):

    def test_claude_and_ollama_kinds(self):
        self.small_images(1)
        self.configure()
        self.index()
        for kind in ("claude", "ollama"):
            with self.subTest(kind=kind):
                # Not a real key: a made-up value used only by this test.
                self.config["endpoint"] = self.endpoint(kind=kind, base_url=self.server.root_url,
                                                        api_key="secret-key-123")
                self.execute("UPDATE images SET described = 0, info = ''")
                self.assertEqual(self.describe()["described"], 1)
                request = self.server.describe_requests()[-1]
                self.assertEqual(request["kind"], kind)
                self.assertEqual(request["system"], prompts.SYSTEM_PROMPT)
                with Image.open(io.BytesIO(request["image"])) as image:
                    self.assertEqual(image.format, "JPEG")
                if kind == "claude":
                    self.assertEqual(request["mime"], "image/jpeg")
                    self.assertEqual(request["headers"].get("x-api-key"), "secret-key-123")
                    self.assertEqual(request["parts"], ["image", "text"])
                info = json.loads(self.rows()["photo0.jpg"]["info"])
                self.assertEqual(info["_model_name"], "fake-model")

    def test_round_robin_endpoints_share_the_work(self):
        self.small_images(6)
        self.configure(endpoint=[self.endpoint(model="model-a"), self.endpoint(model="model-b")])
        self.index()
        summary = self.describe()
        self.assertEqual(summary["described"], 6)
        models = {request["payload"]["model"] for request in self.server.describe_requests()}
        self.assertEqual(models, {"model-a", "model-b"})
        endpoints = {json.loads(row["provenance"])["endpoint"] for row in self.rows().values()}
        self.assertEqual(endpoints, {"default", "default-2"})

    def test_named_endpoints_per_library(self):
        other_folder = self.base.library_folder("Other")
        self.small_images(2)
        self.small_images(2, other_folder, prefix="other", first_block=30)
        other_server = fake_llm.FakeLLM().start()
        self.addCleanup(other_server.stop)
        endpoints = [
            {"name": "local", "preset": "llamacpp", "base_url": self.server.base_url,
             "model": "local-model"},
            {"name": "cloud", "preset": "openai", "base_url": other_server.base_url,
             "model": "cloud-model", "api_key_env": "TEST_CLOUD_KEY", "price_in": 1.0,
             "price_out": 2.0},
        ]
        os.environ["TEST_CLOUD_KEY"] = SECRET
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other_folder, endpoint="cloud")],
                       endpoints=endpoints, default_endpoint="local")
        self.index()
        summary = self.describe()
        self.assertEqual(summary["described"], 4)
        self.assertAlmostEqual(summary["cost"], 2 * (10 * 1.0 + 20 * 2.0) / 1e6)
        self.assertEqual(len(self.server.describe_requests()), 2)
        self.assertEqual(len(other_server.describe_requests()), 2)
        self.assertEqual({r["headers"]["authorization"] for r in other_server.describe_requests()},
                         {"Bearer " + SECRET})
        # Cloud services are never probed.
        self.assertEqual(other_server.other_requests(), [])
        rows = self.rows()
        cloud = json.loads(rows["other0.jpg"]["provenance"])
        self.assertEqual((cloud["endpoint"], cloud["model"]), ("cloud", "cloud-model"))
        self.assertAlmostEqual(cloud["cost"], (10 * 1.0 + 20 * 2.0) / 1e6)
        self.assertEqual(json.loads(rows["photo0.jpg"]["provenance"])["endpoint"], "local")
        # One endpoint for every library, chosen for the run.
        self.execute("UPDATE images SET described = 0, info = ''")
        self.assertEqual(self.describe(endpoint_name="cloud")["described"], 4)
        self.assertEqual(len(other_server.describe_requests()), 6)
        self.execute("UPDATE images SET described = 0, info = ''")
        summary = self.describe(endpoint_name="nowhere")
        self.assertIn("nowhere", summary["error"])
        for text in [self.log_text()] + [json.dumps(row) for row in self.rows().values()]:
            self.assertNotIn(SECRET, text)

    def test_library_with_an_unknown_endpoint_is_skipped(self):
        self.small_images(2)
        self.configure([support.library("Family", self.folder, endpoint="missing")],
                       endpoints=[{"name": "local", "base_url": self.server.base_url,
                                   "model": "m"}])
        self.index()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped"]), (0, 2))
        self.assertIn("'missing'", self.log_text())
        self.assertEqual(self.server.requests, [])
        self.assertEqual(set(self.attempts().values()), {0})

    def test_concurrency_follows_the_server_slots(self):
        self.small_images(8)
        self.server.props_data["total_slots"] = 4
        self.server.reply = fake_llm.Reply(reply(), delay=0.3)
        self.configure()
        self.index()
        summary = self.describe()
        self.assertEqual(summary["described"], 8)
        self.assertEqual(self.server.max_in_flight, 4)
        self.assertIn("4 request(s) at a time", self.log_text())

    def test_concurrency_setting(self):
        self.small_images(6)
        self.server.props = False
        self.server.reply = fake_llm.Reply(reply(), delay=0.3)
        self.configure(endpoint=self.endpoint(preset="vllm", concurrency=3))
        self.index()
        self.assertEqual(self.describe()["described"], 6)
        self.assertEqual(self.server.max_in_flight, 3)

    def test_broken_server_is_not_the_images_fault(self):
        self.small_images(3)
        calls = []
        helper = types.SimpleNamespace(before_launch=lambda: calls.append("before"),
                                       after_launch=lambda: calls.append("after"))
        self.configure()
        self.index()
        self.server.props = False
        self.server.reply = fake_llm.Disconnect()
        summary = self.describe(helper=helper)
        self.assertEqual((summary["described"], summary["failed"]), (0, 0))
        self.assertTrue(summary["error"])
        self.assertEqual(summary["retired"], ["default"])
        # Each request is retried, then the image goes back in the queue,
        # until the endpoint has failed MAX_ENDPOINT_FAILURES times in a row.
        self.assertEqual(len(self.server.describe_requests()),
                         describe.MAX_ENDPOINT_FAILURES * (1 + describe.MAX_RETRIES))
        # Retry pauses happen in short slices (so a stop request is noticed).
        self.assertTrue(self.sleeps)
        self.assertLessEqual(max(self.sleeps), 0.5)
        self.assertEqual(calls, ["before", "after"])
        self.assertEqual(set(self.attempts().values()), {0})

    def test_a_cloud_service_without_a_key_gets_no_picture(self):
        self.small_images(2)
        self.configure(endpoints=[{"name": "cloud", "preset": "openai",
                                   "base_url": self.server.base_url, "model": "m",
                                   "api_key_env": "SEARCH_IMAGES_UNSET_TEST_KEY"}],
                       default_endpoint="cloud")
        self.index()
        summary = self.describe()
        self.assertEqual(self.server.describe_requests(), [])
        self.assertEqual(summary["retired"], ["cloud"])
        self.assertEqual(summary["described"], 0)
        self.assertIn("No usable LLM endpoint", summary["error"])
        self.assertIn("no API key is set (env SEARCH_IMAGES_UNSET_TEST_KEY not set)",
                      self.log_text())
        self.assertEqual(set(self.attempts().values()), {0})

    def test_unreachable_local_server_is_not_used(self):
        self.small_images(2)
        port = closed_port()
        self.configure(endpoint=self.endpoint(base_url="http://127.0.0.1:%d/v1" % port))
        self.index()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"], summary["skipped"]),
                         (0, 0, 2))
        self.assertIn("cannot be reached", self.log_text())
        self.assertEqual(set(self.attempts().values()), {0})

    def test_waits_for_a_loading_server(self):
        self.small_images(1)
        self.server.props_status = 503
        self.server.health_script.extend([503, 503])
        self.configure()
        self.index()
        self.assertEqual(self.describe()["described"], 1)
        self.assertIn("Waiting for endpoint", self.log_text())
        health = [r for r in self.server.other_requests() if r["path"] == "/health"]
        self.assertEqual(len(health), 3)

    def test_retry_after_is_honoured(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.queue(fake_llm.rate_limited(retry_after=7), fake_llm.overloaded())
        summary = self.describe()
        self.assertEqual(summary["described"], 1)
        self.assertEqual(summary["requests"], 3)
        # Waits are cut into short slices so a stop request is noticed.
        self.assertGreaterEqual(sum(self.sleeps), 7.0)
        self.assertLess(sum(self.sleeps), 7.0 + 2.01)
        self.assertEqual(json.loads(self.rows()["photo0.jpg"]["provenance"])["requests"], 3)

    def test_very_long_retry_after_retires_the_endpoint(self):
        self.small_images(2)
        self.configure()
        self.index()
        self.server.reply = fake_llm.rate_limited(retry_after=86400)
        summary = self.describe()
        self.assertEqual(summary["retired"], ["default"])
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.assertIn("1440 minutes", self.log_text())
        self.assertEqual(set(self.attempts().values()), {0})

    def test_endpoint_error_retires_the_endpoint_without_charging_images(self):
        self.small_images(5)
        self.configure()
        self.index()
        self.server.reply = fake_llm.schema_error()
        for _run in range(2):
            summary = self.describe()
            self.assertEqual((summary["described"], summary["failed"], summary["skipped"]),
                             (0, 0, 5))
            self.assertTrue(summary["error"])
            self.assertIn("rejected the request settings", self.log_text())
        self.assertEqual(len(self.server.describe_requests()), 2)
        self.assertEqual(set(self.attempts().values()), {0})
        self.server.reply = reply()
        self.assertEqual(self.describe()["described"], 5)

    def test_group_members_take_over_from_a_retired_one(self):
        self.small_images(4)
        bad_server = fake_llm.FakeLLM().start()
        self.addCleanup(bad_server.stop)
        bad_server.reply = fake_llm.HttpError(404, {"error": {"message": "model not found"}})
        self.configure(endpoints=[
            {"name": "bad", "group": "pool", "base_url": bad_server.base_url, "model": "x"},
            {"name": "good", "group": "pool", "base_url": self.server.base_url, "model": "y"},
        ], default_endpoint="pool")
        self.index()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (4, 0))
        self.assertEqual(summary["retired"], ["bad"])
        self.assertIn("does not know the model", self.log_text())

    def test_image_refusals_before_any_success_do_not_fail_the_images(self):
        self.small_images(5)
        self.configure()
        self.index()
        self.server.reply = fake_llm.image_error()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (0, 0))
        self.assertIn("refused every request", self.log_text())
        self.assertEqual(len(self.server.describe_requests()), describe.MAX_ENDPOINT_FAILURES)
        self.assertEqual(set(self.attempts().values()), {0})

    def test_rejected_image_counts_once_the_endpoint_has_worked(self):
        self.small_images(2)
        self.configure()
        self.index()
        self.server.queue(reply(), fake_llm.image_error())
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["failed"]), (1, 1))
        # Image errors are not retried.
        self.assertEqual(len(self.server.describe_requests()), 2)
        rows = self.rows()
        self.assertEqual(rows["photo0.jpg"]["describe_attempts"], 1)
        self.assertIn("400", rows["photo0.jpg"]["describe_error"])

    def pending_paths(self):
        conn = db.connect()
        try:
            rows, _skipped = describe.pending_rows(
                conn, describe.selected_libraries(self.config), newest_first=True)
        finally:
            conn.close()
        return [row["path"] for row in rows]

    def test_failed_request_is_asked_again_after_the_other_images(self):
        self.small_images(4)
        self.configure()
        self.index()
        broken = [fake_llm.HttpError(500)] * (1 + describe.MAX_RETRIES)
        # photo3 works; photo2 breaks the server; photo1 and photo0 work;
        # then photo2 breaks it again.
        self.server.queue(reply(), *broken, reply(), reply(), *broken)
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["failed"], summary["retired"]),
                         (3, 1, []))
        images = self.server.images()
        tries = 1 + describe.MAX_RETRIES
        # photo2 was not asked again at once, but after the other images.
        self.assertNotEqual(images[1], images[1 + tries])
        self.assertEqual(images[1], images[1 + tries + 2])
        self.assertEqual(self.attempts()["photo2.jpg"], 1)

    def test_images_that_stop_a_working_endpoint_are_charged_once(self):
        self.small_images(5)
        self.configure()
        self.index()
        broken = [fake_llm.HttpError(500)] * (1 + describe.MAX_RETRIES)
        self.server.queue(reply(), *(broken * describe.MAX_ENDPOINT_FAILURES))
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["retired"]), (1, ["default"]))
        # The images that broke a server that had worked are charged once,
        # so the same few cannot end every run; the last one was not tried.
        self.assertEqual(self.attempts(), {"photo4.jpg": 0, "photo3.jpg": 1, "photo2.jpg": 1,
                                           "photo1.jpg": 1, "photo0.jpg": 0})
        # The next run starts with the image that has not failed.
        self.assertEqual(self.pending_paths(),
                         ["photo0.jpg", "photo3.jpg", "photo2.jpg", "photo1.jpg"])

    def test_a_server_that_dies_is_never_the_images_fault(self):
        self.small_images(4)
        self.configure()
        self.index()
        self.server.queue(reply())
        self.server.reply = fake_llm.Disconnect()
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["retired"]), (1, ["default"]))
        self.assertEqual(set(self.attempts().values()), {0})

    def test_content_policy_block_is_the_images_problem(self):
        self.small_images(3)
        flagged = fake_llm.HttpError(403, {"error": {
            "code": 403, "message": 'Your chosen model requires moderation and your input '
                                    'was flagged for "sexual"',
            "metadata": {"reasons": ["sexual"], "flagged_input": "..."}}})
        # Not a real key: a made-up value used only by this test.
        self.configure(endpoint=self.endpoint(preset="openrouter", api_key="test-key-0303",
                                              concurrency=1))
        self.index()
        # photo2 is blocked before the endpoint has worked: not charged, but
        # asked again after the others; blocked again, it is charged.
        self.server.queue(flagged, reply(), reply(), flagged)
        for run in range(2):
            summary = self.describe(newest_first=True)
            self.assertFalse(summary["fatal"])
            self.assertEqual(summary["retired"], [])
            self.assertEqual(self.attempts()["photo2.jpg"], 1)
            self.assertIn("flagged", self.rows()["photo2.jpg"]["describe_error"])
            if run == 0:
                self.assertEqual((summary["described"], summary["failed"]), (2, 1))
                self.server.reply = flagged
        # The second run (only photo2 left, never worked): not charged again.
        self.assertEqual((summary["described"], summary["failed"], summary["skipped"]),
                         (0, 0, 1))

    def test_errors_that_stop_a_working_endpoint_charge_the_image(self):
        self.small_images(3)
        self.configure()
        self.index()
        self.server.queue(reply(), fake_llm.HttpError(404, {"error": {
            "message": "model not found"}}))
        summary = self.describe(newest_first=True)
        self.assertEqual(summary["retired"], ["default"])
        self.assertEqual(self.attempts(), {"photo2.jpg": 0, "photo1.jpg": 1, "photo0.jpg": 0})
        self.assertEqual(self.pending_paths(), ["photo0.jpg", "photo1.jpg"])
        # An error that stops the run names the image and charges it too.
        self.server.queue(reply(), fake_llm.HttpError(403, {"error": {"message": "nope"}}))
        summary = self.describe(newest_first=True)
        self.assertTrue(summary["fatal"])
        self.assertIn("while describing photo1.jpg", self.log_text())
        self.assertEqual(self.attempts(), {"photo2.jpg": 0, "photo1.jpg": 2, "photo0.jpg": 0})

    def test_helper_load_image_runs_one_at_a_time(self):
        self.small_images(6)
        state = {"now": 0, "most": 0, "calls": 0}
        lock = threading.Lock()

        def load_image(path):
            with lock:
                state["now"] += 1
                state["calls"] += 1
                state["most"] = max(state["most"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return None         # read the file as usual

        self.configure([support.library("Family", self.folder, previews=False)],
                       endpoint=self.endpoint(preset="vllm", concurrency=4))
        self.index()
        summary = self.describe(helper=types.SimpleNamespace(load_image=load_image))
        self.assertEqual(summary["described"], 6)
        self.assertEqual((state["calls"], state["most"]), (6, 1))

    def test_unencrypted_endpoint_beyond_the_network_is_reported(self):
        ctx = jobs.JobContext("describe", log_limit=100)
        run = describe._DescribeRun(None, ctx, {"skipped": 0, "error": "", "retired": []},
                                    None, None, self.sleeps.append)
        run.add_endpoint(common.endpoint_settings({
            "name": "far", "preset": "custom", "base_url": "http://llm.example.com/v1",
            "model": "m", "api_key": SECRET}))
        run.add_endpoint(common.endpoint_settings({
            "name": "near", "base_url": self.server.base_url, "model": "m"}))
        run.prepare_endpoints()
        text = "\n".join(entry["message"] for entry in ctx.snapshot(log_tail=100)["log_tail"])
        self.assertIn("Endpoint far (OpenAI-compatible) sends pictures and its API key "
                      "unencrypted", text)
        self.assertEqual(text.count("unencrypted"), 1)
        self.assertNotIn(SECRET, text)
        # The log names where pictures go when that is not this network.
        self.assertIn("pictures are sent to llm.example.com", text)
        self.assertEqual(text.count("pictures are sent to"), 1)

    def test_library_without_any_default_endpoint_is_skipped(self):
        self.small_images(1)
        self.configure(endpoints=[{"name": "cloud", "preset": "openai", "model": "m",
                                   "base_url": "https://api.openai.com/v1"}])
        self.index()
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped"]), (0, 1))
        self.assertIn("no default endpoint is set", self.log_text())
        self.assertEqual(self.server.describe_requests(), [])

    def test_rejected_key_stops_the_whole_run(self):
        other_folder = self.base.library_folder("Other")
        self.small_images(3)
        self.small_images(2, other_folder, prefix="other", first_block=30)
        other_server = fake_llm.FakeLLM().start()
        self.addCleanup(other_server.stop)
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other_folder, endpoint="second")],
                       endpoints=[
                           {"name": "cloud", "preset": "openai", "base_url": self.server.base_url,
                            "model": "m", "api_key": SECRET, "concurrency": 1},
                           {"name": "second", "preset": "custom",
                            "base_url": other_server.base_url, "model": "m",
                            "concurrency": 1},
                       ], default_endpoint="cloud")
        self.index()
        self.server.reply = fake_llm.unauthorized(echo=SECRET)
        other_server.reply = fake_llm.Reply(reply(), delay=0.3)
        summary = self.describe(newest_first=True)
        self.assertTrue(summary["fatal"])
        self.assertIn("'cloud' rejected the API key (401)", summary["error"])
        self.assertEqual(len(self.server.describe_requests()), 1)
        # The other endpoint's request in flight finishes; nothing more is sent.
        self.assertLessEqual(len(other_server.describe_requests()), 1)
        self.assertEqual(set(self.attempts().values()), {0})
        text = self.log_text() + summary["error"]
        self.assertIn("401", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("0123456789abcdef", text)

    def test_keys_never_reach_the_database_or_logs(self):
        self.small_images(2)
        self.configure(endpoint=self.endpoint(api_key=SECRET))
        self.index()
        leaky = fake_llm.HttpError(400, {"error": {
            "message": "image too large for key %s" % SECRET}})
        self.server.queue(reply(), leaky)
        with mock.patch.dict(os.environ, {"LOG_ERRORS": "1"}):
            summary = self.describe(newest_first=True)
        self.assertEqual(summary["failed"], 1)
        texts = [self.log_text()]
        texts += [json.dumps(row) for row in self.rows().values()]
        for name in os.listdir(common.logs_dir()):
            with open(os.path.join(common.logs_dir(), name), encoding="utf-8") as handle:
                texts.append(handle.read())
        for text in texts:
            self.assertNotIn(SECRET, text)
        self.assertIn("image too large", self.rows()["photo0.jpg"]["describe_error"])

    def test_server_error_then_success(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.queue(fake_llm.HttpError(500), "not json")
        self.assertEqual(self.describe()["described"], 1)
        self.assertEqual(len(self.server.describe_requests()), 3)

    def test_model_probe_failure_falls_back_to_the_reply(self):
        self.server.props = False
        self.small_images(1)
        self.configure()
        self.index()
        self.server.queue(fake_llm.Reply(reply(), model=""))
        self.assertEqual(self.describe()["described"], 1)
        info = json.loads(self.rows()["photo0.jpg"]["info"])
        self.assertEqual(info["_model_name"], "fake-model")

    def test_no_model_configured(self):
        self.small_images(1)
        self.configure(endpoint={"kind": "openai", "base_url": "", "model": ""})
        self.index()
        summary = self.describe()
        self.assertEqual(summary["described"], 0)
        self.assertIn("No LLM model configured", self.log_text())
        self.assertEqual(self.server.requests, [])

    def test_error_logs_go_to_the_data_folder(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.reply = "garbage"
        before = set(os.listdir(os.getcwd()))
        with mock.patch.dict(os.environ, {"LOG_ERRORS": "1"}):
            self.describe()
        self.assertEqual(set(os.listdir(os.getcwd())), before)
        logs = os.listdir(common.logs_dir())
        self.assertEqual(len(logs), 1 + describe.CONTENT_RETRIES)
        self.assertTrue(all(name.startswith("llm-error-") for name in logs))
        with open(os.path.join(common.logs_dir(), logs[0]), encoding="utf-8") as handle:
            self.assertIn("garbage", handle.read())


class HelperTest(DescribeTestCase):

    def test_after_launch_runs_when_the_run_fails(self):
        self.small_images(1)
        self.configure()
        self.index()
        calls = []
        helper = types.SimpleNamespace(before_launch=lambda: calls.append("before"),
                                       after_launch=lambda: calls.append("after"))
        with mock.patch.object(describe, "store_description",
                               side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                self.describe(helper=helper)
        self.assertEqual(calls, ["before", "after"])

    def test_after_launch_runs_when_before_launch_fails(self):
        self.small_images(1)
        self.configure()
        self.index()
        calls = []

        def before():
            calls.append("before")
            raise OSError("could not start the model server")

        helper = types.SimpleNamespace(before_launch=before,
                                       after_launch=lambda: calls.append("after"))
        with self.assertRaises(OSError):
            self.describe(helper=helper)
        self.assertEqual(calls, ["before", "after"])

    def test_call_api_gets_a_temporary_jpeg(self):
        imagegen.large_jpeg(self.folder, "big.jpg", size=(2400, 1800))
        self.configure()
        self.index()
        seen = []

        def call_api(prompt, image_path):
            with open(image_path, "rb") as handle:
                data = handle.read()
            seen.append((prompt, image_path, data))
            return None

        helper = types.SimpleNamespace(call_api=call_api)
        self.assertEqual(self.describe(helper=helper)["described"], 1)
        [(prompt, path, data)] = seen
        self.assertEqual(prompt, prompts.user_text(None))
        self.assertEqual(os.path.dirname(path), common.tmp_dir())
        self.assertFalse(os.path.exists(path))
        self.assertEqual(data, self.server.images()[0])
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (1536, 1152))

    def test_call_api_can_ask_for_the_original_path(self):
        self.small_images(1)
        self.configure()
        self.index()
        original = os.path.join(self.folder, "photo0.jpg")
        seen = []

        def by_keyword(prompt, image_path, source_path=None):
            seen.append(("keyword", source_path, os.path.dirname(image_path)))

        def by_position(prompt, image_path, original_path):
            seen.append(("positional", original_path, os.path.dirname(image_path)))

        def with_kwargs(prompt, image_path, **kwargs):
            seen.append(("kwargs", kwargs.get("source_path"), os.path.dirname(image_path)))

        for hook in (by_keyword, by_position, with_kwargs):
            self.execute("UPDATE images SET described = 0, info = ''")
            self.assertEqual(self.describe(helper=types.SimpleNamespace(call_api=hook))
                             ["described"], 1)
        self.assertEqual(seen, [("keyword", original, common.tmp_dir()),
                                ("positional", original, common.tmp_dir()),
                                ("kwargs", original, common.tmp_dir())])

    def test_call_api_can_change_the_prompt_and_image(self):
        self.small_images(1)
        replacement = imagegen.jpeg_named_png(self.base.path, "replacement.png")
        self.configure()
        self.index()
        helper = types.SimpleNamespace(
            call_api=lambda prompt, image_path: ("Short prompt. " + prompt, replacement))
        self.assertEqual(self.describe(helper=helper)["described"], 1)
        [request] = self.server.describe_requests()
        self.assertTrue(request["prompt"].startswith("Short prompt. "))
        self.assertEqual(request["system"], prompts.SYSTEM_PROMPT)
        with open(replacement, "rb") as handle:
            self.assertEqual(request["image"], handle.read())
        self.assertEqual(request["mime"], "image/jpeg")

    def test_failing_call_api_fails_the_image(self):
        self.small_images(1)
        self.configure()
        self.index()

        def call_api(prompt, image_path):
            raise RuntimeError("hook broke")

        summary = self.describe(helper=types.SimpleNamespace(call_api=call_api))
        self.assertEqual(summary["failed"], 1)
        self.assertIn("hook broke", self.rows()["photo0.jpg"]["describe_error"])

    def test_keys_are_masked_before_errors_are_cut(self):
        # A key that straddles the 500-character limit must not leave its
        # first part in the database.
        self.small_images(1)
        self.configure(endpoint=self.endpoint(api_key=SECRET))
        self.index()
        def failing_hook(start):
            def call_api(prompt, image_path):
                raise RuntimeError("q" * start + SECRET)
            return call_api

        for start in (480, 490, 499):
            self.execute("UPDATE images SET describe_attempts = 0, describe_error = ''")
            summary = self.describe(helper=types.SimpleNamespace(call_api=failing_hook(start)))
            self.assertEqual(summary["failed"], 1)
            error = self.rows()["photo0.jpg"]["describe_error"]
            self.assertTrue(error.startswith("qqq"), error)
            self.assertNotIn(SECRET[:3], error)
            self.assertNotIn(SECRET[:3], self.log_text())


class CommandLineTest(DescribeTestCase):

    def run_scan(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = scan.main(list(args))
        return code, out.getvalue()

    def test_scan_then_describe(self):
        self.small_images(2)
        self.configure()
        code, output = self.run_scan("--newest-first")
        self.assertEqual(code, 0, output)
        self.assertIn("Described photo1.jpg", output)
        self.assertEqual({row["described"] for row in self.rows().values()}, {1})

    def test_limit_endpoint_and_stale_flags(self):
        self.small_images(3)
        self.configure(endpoints=[{"name": "one", "base_url": self.server.base_url,
                                   "model": "model-one"},
                                  {"name": "two", "base_url": self.server.base_url,
                                   "model": "model-two"}],
                       default_endpoint="one")
        code, output = self.run_scan("--limit", "1", "--endpoint", "two")
        self.assertEqual(code, 0, output)
        self.assertEqual([r["payload"]["model"] for r in self.server.describe_requests()],
                         ["model-two"])
        self.execute("UPDATE images SET prompt_ver = 4")
        code, output = self.run_scan("--no-scan")
        self.assertIn("--redescribe-stale", output)
        self.assertEqual(len(self.server.describe_requests()), 3)
        code, output = self.run_scan("--no-scan", "--redescribe-stale")
        self.assertEqual(code, 0, output)
        self.assertEqual({row["prompt_ver"] for row in self.rows().values()},
                         {describe.PROMPT_VER})
        code, output = self.run_scan("--limit", "-1")
        self.assertEqual(code, 2)

    def test_fatal_error_exit_code(self):
        self.small_images(1)
        self.configure(endpoint=self.endpoint(api_key=SECRET))
        self.server.reply = fake_llm.unauthorized()
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn("rejected the API key", output)
        self.assertNotIn(SECRET, output)

    def test_runs_that_cannot_describe_because_of_settings_fail(self):
        # Unattended runs must be able to tell these from success.
        self.small_images(1)
        self.configure()
        code, output = self.run_scan("--endpoint", "typo")
        self.assertEqual(code, 2)
        self.assertIn("No enabled endpoint or group is named typo", output)
        self.configure(endpoint=self.endpoint(
            base_url="http://127.0.0.1:%d/v1" % closed_port()))
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn("Stopped early: No usable LLM endpoint is left.", output)
        self.assertNotIn("Done.", output)

    def test_list_endpoints_never_shows_keys(self):
        os.environ["TEST_LIST_KEY"] = SECRET
        key_file = self.base.write_file("keys/cloud.key", SECRET + "\n")
        self.configure(endpoints=[
            {"name": "local", "base_url": self.server.base_url, "model": "m"},
            {"name": "cloud", "preset": "anthropic", "model": "claude-haiku-5-5",
             "api_key_file": key_file},
            {"name": "other", "preset": "openai", "model": "x", "api_key_env": "TEST_LIST_KEY",
             "enabled": False, "group": "spare"},
            {"name": "literal", "preset": "gemini", "model": "y", "api_key": SECRET},
        ], default_endpoint="local")
        code, output = self.run_scan("--list-endpoints")
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, output)
        lines = output.splitlines()
        self.assertTrue(lines[0].split() == ["name", "kind", "preset", "model", "key",
                                             "enabled", "group"])
        self.assertIn("anthropic", output)
        self.assertRegex(output, r"cloud\s+claude\s+anthropic\s+claude-haiku-5-5\s+file\s+yes")
        self.assertRegex(output, r"other\s+openai\s+openai\s+x\s+env TEST_LIST_KEY\s+no\s+spare")
        self.assertRegex(output, r"literal\s+openai\s+gemini\s+y\s+config\s+yes")
        self.assertIn("Default endpoint: local", output)

    def test_test_endpoint(self):
        self.configure(endpoints=[{"name": "local", "base_url": self.server.base_url,
                                   "model": "m", "api_key": SECRET}])
        code, output = self.run_scan("--test-endpoint", "local")
        self.assertEqual(code, 0, output)
        self.assertNotIn(SECRET, output)
        result = json.loads(output[output.index("{"):output.rindex("}") + 1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["key_source"], "config")
        self.assertEqual(result["parsed"]["title"], fake_llm.DEFAULT_REPLY["title"])
        [request] = self.server.describe_requests()
        with open(scan.DEFAULT_TEST_IMAGE, "rb") as handle:
            original = handle.read()
        self.assertNotEqual(request["image"], b"")
        self.assertNotEqual(request["image"], original)       # prepared, not raw
        # Another image, a failing reply, and an unknown endpoint.
        picture = imagegen.jpeg_with_exif(self.base.path, "mine.jpg")
        self.server.queue("not json")
        code, output = self.run_scan("--test-endpoint", "local", "--image", picture)
        self.assertEqual(code, 1)
        self.assertIn('"error_kind": "content"', output)
        code, output = self.run_scan("--test-endpoint", "nope")
        self.assertEqual(code, 2)
        self.assertIn("Unknown endpoint", output)

    def test_stop_keeps_what_was_described(self):
        self.small_images(4)
        self.configure()
        self.index()
        ctx = jobs.JobContext("describe")
        real_store = describe.store_description

        def store_then_stop(*args, **kwargs):
            result = real_store(*args, **kwargs)
            ctx.request_stop()
            return result

        conn = db.connect()
        try:
            with mock.patch.object(describe, "store_description", store_then_stop):
                summary = describe.describe_pending(conn, self.config, ctx,
                                                    sleep=self.sleeps.append)
        finally:
            conn.close()
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["described"], 1)
        self.assertEqual(sum(row["described"] for row in self.rows().values()), 1)


class LlmTest(unittest.TestCase):

    def test_mime_is_sniffed_from_the_bytes(self):
        jpeg = images.prepare_for_llm(imagegen.jpeg_bytes(imagegen.marker_image()))
        self.assertEqual(llm.image_mime(jpeg, "picture.png"), "image/jpeg")
        self.assertEqual(llm.image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 8), "image/png")
        self.assertEqual(llm.image_mime(b"????", "x.gif"), "image/gif")
        self.assertEqual(llm.image_mime(b"????"), "image/jpeg")

    def test_prompt_version_needs_no_pillow(self):
        # progress.py reads PROMPT_VER from prompts.py, which needs no Pillow,
        # so there is no second copy of the number to forget.
        result = support.run_python(
            "import sys; sys.modules['PIL'] = None; sys.modules['PIL.Image'] = None\n"
            "import progress, prompts\n"
            "print(progress.PROMPT_VER, prompts.PROMPT_VER)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [str(describe.PROMPT_VER)] * 2)
        self.assertEqual(describe.PROMPT_VER, 5)


if __name__ == "__main__":
    unittest.main()
