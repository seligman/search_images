"""Tests for describe.py (and the describe step of scan.py) against a fake LLM."""

import contextlib
import io
import json
import os
import socket
import types
import unittest
import urllib.error
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
import scan


class DescribeTestCase(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        self.folder = self.base.library_folder("Family")
        # No real waiting between /props retries.
        patcher = mock.patch.object(llm, "PROPS_RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self, **settings):
        endpoint = {"kind": "openai", "base_url": self.server.base_url, "api_key": "",
                    "model": "fake-model"}
        endpoint.update(settings)
        return endpoint

    def configure(self, libraries=None, endpoint=None, **extra):
        libraries = libraries if libraries is not None else [
            support.library("Family", self.folder)]
        self.base.write_config(libraries, endpoint=endpoint or self.endpoint(), **extra)
        self.config = common.load_config()
        db.init(self.config, log=lambda message: None)
        return self.config

    def index(self):
        ctx = jobs.JobContext("index")
        scan.run(self.config, ctx, describe_images=False)

    def describe(self, helper=None, **kwargs):
        self.ctx = jobs.JobContext("describe")
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
                         for entry in self.ctx.snapshot(log_tail=1000)["log_tail"])

    def small_images(self, count, folder=None):
        names = []
        for index in range(count):
            name = "photo%d.jpg" % index
            imagegen.jpeg_with_exif(folder or self.folder, name,
                                    image=imagegen.blocks_image(8 + index),
                                    date="2019:05:%02d 14:23:01" % (index + 1))
            names.append(name)
        return names


class DescribeTest(DescribeTestCase):

    def test_reply_is_stored_and_the_image_is_prepared(self):
        imagegen.large_jpeg(self.folder, "rotated.jpg", size=(3000, 2000), orientation=6)
        self.configure()
        self.index()
        summary = self.describe()
        self.assertEqual(summary["described"], 1)
        row = self.rows()["rotated.jpg"]
        self.assertEqual((row["described"], row["prompt_ver"]), (1, describe.PROMPT_VER))
        self.assertEqual((row["describe_attempts"], row["describe_error"]), (0, ""))
        info = json.loads(row["info"])
        self.assertEqual(info["description"], fake_llm.DEFAULT_REPLY["description"])
        self.assertEqual(info["tags"], ["test", "red", "blue"])
        self.assertEqual(info["_model_name"], "model.gguf")
        provenance = json.loads(row["provenance"])
        self.assertEqual(provenance["model"], "model.gguf")
        self.assertEqual(provenance["prompt_ver"], describe.PROMPT_VER)
        # What the model received: a JPEG, upright, long edge 1536, and
        # nothing but the prompt (no file name or path).
        [request] = self.server.describe_requests()
        self.assertEqual(request["mime"], "image/jpeg")
        self.assertEqual(request["prompt"], describe.DESCRIBE_PROMPT)
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

    def test_bad_reply_counts_a_failed_attempt(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.reply = "I cannot do that."
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (0, 1))
        self.assertEqual(len(self.server.describe_requests()), describe.REPLY_ATTEMPTS)
        row = self.rows()["photo0.jpg"]
        self.assertEqual((row["described"], row["describe_attempts"]), (0, 1))
        self.assertIn("no JSON object", row["describe_error"])
        self.describe()
        self.assertEqual(self.rows()["photo0.jpg"]["describe_attempts"], 2)

    def test_score_out_of_type_is_a_failure(self):
        self.small_images(1)
        self.configure()
        self.index()
        reply = dict(fake_llm.DEFAULT_REPLY, sharpness="very")
        self.server.reply = json.dumps(reply)
        self.assertEqual(self.describe()["failed"], 1)
        self.assertIn("sharpness", self.rows()["photo0.jpg"]["describe_error"])

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

    def test_only_enabled_libraries_and_present_files(self):
        other = self.base.library_folder("Other")
        self.small_images(2)
        imagegen.jpeg_with_exif(other, "other.jpg", image=imagegen.blocks_image(20))
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other)])
        self.index()
        self.config["libraries"][1]["enabled"] = False
        self.execute("UPDATE images SET missing_since = 1 WHERE path = 'photo1.jpg' "
                     "AND library_id = ?", (self.config["libraries"][0]["id"],))
        summary = self.describe()
        self.assertEqual(summary["described"], 1)
        described = [(row["library_id"], row["path"]) for row in self.query_described()]
        self.assertEqual(described, [(self.config["libraries"][0]["id"], "photo0.jpg")])

    def query_described(self):
        conn = db.connect()
        try:
            return conn.execute("SELECT library_id, path FROM images WHERE described = 1 "
                                "ORDER BY id").fetchall()
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

    def test_stale_descriptions_come_after_new_ones(self):
        self.small_images(3)
        self.configure()
        self.index()
        self.execute("UPDATE images SET described = 1, prompt_ver = 3, info = '{}' "
                     "WHERE path = 'photo2.jpg'")
        conn = db.connect()
        try:
            rows, _ = describe.pending_rows(conn, self.config["libraries"])
        finally:
            conn.close()
        self.assertEqual([row["path"] for row in rows], ["photo1.jpg", "photo0.jpg", "photo2.jpg"])

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


class EndpointTest(DescribeTestCase):

    def test_claude_and_ollama_kinds(self):
        self.small_images(1)
        self.configure()
        self.index()
        for kind in ("claude", "ollama"):
            with self.subTest(kind=kind):
                self.config["endpoint"] = self.endpoint(kind=kind, base_url=self.server.root_url,
                                                        api_key="secret-key-123")
                self.execute("UPDATE images SET described = 0, info = ''")
                self.assertEqual(self.describe()["described"], 1)
                request = self.server.describe_requests()[-1]
                self.assertEqual(request["kind"], kind)
                with Image.open(io.BytesIO(request["image"])) as image:
                    self.assertEqual(image.format, "JPEG")
                if kind == "claude":
                    self.assertEqual(request["mime"], "image/jpeg")
                    self.assertEqual(request["headers"].get("x-api-key"), "secret-key-123")
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
        self.assertEqual(len(self.server.describe_requests()), describe.MAX_ENDPOINT_FAILURES)
        self.assertEqual(calls, ["before", "after"])
        self.assertEqual({row["describe_attempts"] for row in self.rows().values()}, {0})

    def test_error_classification(self):
        def http(code):
            return urllib.error.HTTPError("http://127.0.0.1/", code, "error", {}, None)

        cases = [
            (http(401), "fatal"), (http(403), "fatal"), (http(404), "fatal"),
            (http(400), "image"), (http(413), "image"), (http(429), "endpoint"),
            (http(500), "retry"), (http(503), "retry"),
            (urllib.error.URLError(ConnectionRefusedError()), "endpoint"),
            (urllib.error.URLError(socket.timeout("timed out")), "image"),
            (socket.timeout("timed out"), "image"),
            (ConnectionResetError(), "endpoint"),
            (ValueError("no JSON object found in the response"), "retry"),
            (KeyError("text"), "retry"),
        ]
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                self.assertEqual(describe._classify(error), expected)

    def test_endpoint_refusing_every_request_does_not_fail_the_images(self):
        # A 400 for every request (say, a parameter the service does not
        # accept) is the endpoint's fault until it has worked once.
        self.small_images(5)
        self.configure()
        self.index()
        self.server.reply = fake_llm.HttpError(400)
        for _run in range(3):
            summary = self.describe()
            self.assertEqual((summary["described"], summary["failed"]), (0, 0))
            self.assertTrue(summary["error"])
            self.assertIn("refused every request", self.log_text())
        self.assertEqual({row["describe_attempts"] for row in self.rows().values()}, {0})
        self.server.reply = json.dumps(fake_llm.DEFAULT_REPLY)
        self.assertEqual(self.describe()["described"], 5)

    def test_rejected_image_counts_once_the_endpoint_has_worked(self):
        self.small_images(2)
        self.configure(endpoint=self.endpoint())
        self.index()
        self.server.queue(json.dumps(fake_llm.DEFAULT_REPLY), fake_llm.HttpError(400))
        summary = self.describe(newest_first=True)
        self.assertEqual((summary["described"], summary["failed"]), (1, 1))
        rows = self.rows()
        self.assertEqual(rows["photo0.jpg"]["describe_attempts"], 1)
        self.assertIn("400", rows["photo0.jpg"]["describe_error"])

    def test_rejected_key_stops_at_once(self):
        self.small_images(3)
        self.configure(endpoint=self.endpoint(api_key="sk-abcdefghijklmnopqrstu"))
        self.index()
        self.server.reply = fake_llm.HttpError(401)
        summary = self.describe()
        self.assertEqual(summary["described"], 0)
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.assertIn("401", summary["error"] + self.log_text())
        self.assertNotIn("abcdefghijklmnopqrstu", self.log_text())
        self.assertEqual({row["describe_attempts"] for row in self.rows().values()}, {0})

    def test_server_error_then_success(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.queue(fake_llm.HttpError(500), "not json")
        self.assertEqual(self.describe()["described"], 1)
        self.assertEqual(len(self.server.describe_requests()), 3)

    def test_model_probe_failure_falls_back_to_the_configured_name(self):
        self.server.props = False
        self.small_images(1)
        self.configure()
        self.index()
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
        self.assertEqual(len(logs), describe.REPLY_ATTEMPTS)
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
        self.assertEqual(prompt, describe.DESCRIBE_PROMPT)
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
        with open(replacement, "rb") as handle:
            self.assertEqual(request["image"], handle.read())
        self.assertEqual(request["mime"], "image/jpeg")


class CommandLineTest(DescribeTestCase):

    def test_scan_then_describe(self):
        self.small_images(2)
        self.configure()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = scan.main(["--newest-first"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Described photo1.jpg", out.getvalue())
        self.assertEqual({row["described"] for row in self.rows().values()}, {1})

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
                summary = describe.describe_pending(conn, self.config, ctx)
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


if __name__ == "__main__":
    unittest.main()
