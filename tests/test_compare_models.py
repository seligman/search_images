"""Tests for compare_models.py against fake LLM servers (the database is never written)."""

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

import fake_llm
import imagegen

import common
import compare_models
import db
import describe
import jobs
import llm
import prompts
import scan

# Not a real key: a made-up value used to check that keys never leak.
SECRET = "sk-test-compare-0123456789abcdef"


def row(name, camera="Apple iPhone 12", library_id="lib_a"):
    return {"id": hash((name, library_id)) & 0xFFFF, "name": name, "camera": camera,
            "library_id": library_id}


class SampleTest(unittest.TestCase):

    def test_likely_documents(self):
        self.assertTrue(compare_models.likely_document(row("Screenshot.PNG")))
        self.assertTrue(compare_models.likely_document(row("scan.jpg", camera="")))
        self.assertFalse(compare_models.likely_document(row("photo.jpg")))

    def test_every_prefix_is_balanced(self):
        rows = [row("a%d.png" % n, library_id="lib_a") for n in range(10)]
        rows += [row("a%d.jpg" % n, library_id="lib_a") for n in range(30)]
        rows += [row("b%d.jpg" % n, library_id="lib_b") for n in range(30)]
        rows += [row("b%d.png" % n, library_id="lib_b") for n in range(5)]
        order = compare_models.stratified_order(rows, ["lib_a", "lib_b"], seed=7)
        self.assertEqual(len(order), len(rows))
        self.assertEqual(len({id(item) for item in order}), len(rows))
        for size in (2, 4, 8):
            prefix = order[:size]
            libraries = [item["library_id"] for item in prefix]
            strata = [compare_models.stratum(item) for item in prefix]
            self.assertEqual(libraries.count("lib_a"), size // 2, size)
            self.assertEqual(strata.count("document"), size // 2, size)
        # Documents are oversampled: half of the first 20 although they are
        # 15 of 75 images.
        self.assertEqual([compare_models.stratum(item) for item in order[:20]].count("document"),
                         10)
        # The same seed gives the same order; another seed another one.
        again = compare_models.stratified_order(rows, ["lib_a", "lib_b"], seed=7)
        self.assertEqual([item["name"] for item in again], [item["name"] for item in order])
        other = compare_models.stratified_order(rows, ["lib_a", "lib_b"], seed=8)
        self.assertNotEqual([item["name"] for item in other], [item["name"] for item in order])

    def test_limits(self):
        self.assertEqual(compare_models.parse_limits(["cloud=20", " Local = 3"]),
                         {"cloud": 20, "local": 3})
        for bad in ("cloud", "cloud=x", "=3", "cloud=-1"):
            with self.assertRaises(SystemExit):
                compare_models.parse_limits([bad])


class CompareTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LOG_ERRORS", None)
        llm._no_stream_options.clear()
        self.addCleanup(llm._no_stream_options.clear)
        self.local = fake_llm.FakeLLM().start()
        self.addCleanup(self.local.stop)
        self.cloud = fake_llm.FakeLLM().start()
        self.addCleanup(self.cloud.stop)
        self.cloud.props = False
        self.folder = self.base.library_folder("Family")
        self.other = self.base.library_folder("Other")
        for index in range(3):
            imagegen.jpeg_with_exif(self.folder, "photo%d.jpg" % index,
                                    image=imagegen.blocks_image(8 + index))
        imagegen.screenshot_png(self.folder, "Screenshot_1.png", size=(300, 600))
        imagegen.screenshot_png(self.folder, "Screenshot_2.png", size=(320, 640))
        imagegen.jpeg_with_exif(self.other, "other.jpg", image=imagegen.blocks_image(20),
                                make="", model="")
        os.environ["TEST_COMPARE_KEY"] = SECRET
        self.base.write_config(
            [support.library("Family", self.folder),
             support.library("Other", self.other, context_notes="Notes for the other library.")],
            endpoints=[
                {"name": "local", "preset": "llamacpp", "base_url": self.local.base_url,
                 "model": "local-model"},
                {"name": "cloud", "preset": "openai", "base_url": self.cloud.base_url,
                 "model": "cloud-model", "api_key_env": "TEST_COMPARE_KEY",
                 "price_in": 1.0, "price_out": 2.0, "concurrency": 2},
            ], default_endpoint="local")
        config = common.load_config()
        db.init(config, log=lambda message: None)
        self.config = common.load_config()
        scan.run(self.config, jobs.JobContext("index"), describe_images=False)

    def database_state(self):
        conn = db.connect()
        try:
            rows = [tuple(row) for row in conn.execute("SELECT * FROM images ORDER BY id")]
        finally:
            conn.close()
        stats = {name: os.stat(os.path.join(common.data_dir(), name)).st_mtime_ns
                 for name in ("index.db", "thumbs.db")}
        return rows, stats

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                mock.patch.object(compare_models.time, "sleep", lambda seconds: None):
            code = compare_models.main(list(args))
        return code, out.getvalue()

    def test_report_and_raw_results(self):
        before = self.database_state()
        self.local.queue(fake_llm.HttpError(500))      # one retry on the local server
        self.cloud.queue("not json")                   # one unusable reply in the cloud
        out = self.base.join("reports", "compare.html")
        code, output = self.run_main("--endpoints", "local,cloud", "--sample", "5",
                                     "--seed", "3", "--endpoint-limit", "cloud=2",
                                     "--out", out)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.database_state(), before)        # nothing was written
        self.assertNotIn(SECRET, output)
        with open(self.base.join("reports", "compare.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["format"], "search-images-compare")
        self.assertEqual((report["prompt_ver"], report["prompt_hash"], report["profile"]),
                         (prompts.PROMPT_VER, prompts.prompt_hash("core"), "core"))
        images = report["images"]
        self.assertEqual(len(images), 5)
        self.assertTrue(all(image["thumb"].startswith("data:image/jpeg;base64,")
                            for image in images))
        # Libraries and strata take turns.
        self.assertEqual({image["library"] for image in images[:2]}, {"Family", "Other"})
        self.assertIn("document", [image["stratum"] for image in images[:2]])
        local, cloud = report["endpoints"]
        self.assertEqual((local["name"], cloud["name"]), ("local", "cloud"))
        self.assertEqual(local["served_model"], "model.gguf")
        self.assertEqual(len(local["results"]), 5)
        self.assertEqual(len(cloud["results"]), 2)
        self.assertEqual(sorted(cloud["results"]), sorted(str(image["id"])
                                                          for image in images[:2]))
        self.assertEqual(local["summary"]["described"], 5)
        self.assertEqual(local["summary"]["requests"], 6)
        self.assertEqual(sorted(record["requests"] for record in local["results"].values()),
                         [1, 1, 1, 1, 2])
        self.assertEqual(cloud["summary"]["requests"], 3)
        self.assertEqual(cloud["summary"]["content_retries"], 1)
        self.assertEqual(cloud["summary"]["described"], 2)
        self.assertEqual(len(self.cloud.describe_requests()), 3)
        self.assertAlmostEqual(cloud["summary"]["cost_per_10k"],
                               round(3 * (10 * 1.0 + 20 * 2.0) / 1e6 / 2 * 10000, 2))
        self.assertIsNone(local["summary"]["cost_per_10k"])
        self.assertIsNotNone(local["summary"]["median_seconds"])
        self.assertGreater(local["summary"]["images_per_minute"], 0)
        # The library's context notes went with its images.
        prompts_sent = [request["prompt"] for request in self.local.describe_requests()]
        self.assertTrue(any("Notes for the other library." in text for text in prompts_sent))
        with open(out, encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn("<h1>Model comparison</h1>", page)
        self.assertIn(fake_llm.DEFAULT_REPLY["title"], page)
        self.assertIn("data:image/jpeg;base64,", page)
        self.assertNotIn("<script", page)
        self.assertNotIn(SECRET, page)
        self.assertIn("not sent", page)
        self.assertIn("$0.75", page)

    def test_ids_profile_and_default_output(self):
        conn = db.connect()
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM images ORDER BY id DESC")][:2]
        finally:
            conn.close()
        extended = dict(fake_llm.DEFAULT_REPLY, objects=[], activities=[], landmarks=[],
                        location_guess="", mood="calm")
        self.local.reply = json.dumps(extended)
        code, output = self.run_main("--endpoints", "local", "--ids",
                                     ",".join(str(i) for i in ids) + ",99999",
                                     "--profile", "extended")
        self.assertEqual(code, 0, output)
        reports = os.listdir(common.reports_dir())
        self.assertEqual(sorted(os.path.splitext(name)[1] for name in reports),
                         [".html", ".json"])
        with open(os.path.join(common.reports_dir(),
                               [n for n in reports if n.endswith(".json")][0]),
                  encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual([image["id"] for image in report["images"]], ids)
        self.assertEqual(report["profile"], "extended")
        record = list(report["endpoints"][0]["results"].values())[0]
        self.assertEqual(record["info"]["mood"], "calm")
        self.assertEqual(self.local.describe_requests()[0]["system"],
                         prompts.system_prompt("extended"))

    def test_fatal_error_stops_only_that_endpoint(self):
        self.cloud.reply = fake_llm.unauthorized(echo=SECRET)
        out = self.base.join("r.html")
        code, output = self.run_main("--endpoints", "cloud,local", "--sample", "4",
                                     "--out", out)
        self.assertEqual(code, 0, output)
        with open(self.base.join("r.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        cloud, local = report["endpoints"]
        self.assertIn("rejected the API key", cloud["stopped"])
        self.assertLessEqual(len(self.cloud.describe_requests()), 2)    # concurrency 2
        self.assertEqual(cloud["summary"]["not_run"] + cloud["summary"]["failures"], 4)
        self.assertEqual(local["summary"]["described"], 4)
        with open(out, encoding="utf-8") as handle:
            page = handle.read()
        self.assertNotIn(SECRET, page + output + json.dumps(report))

    def test_merge(self):
        first, second = self.base.join("a.html"), self.base.join("b.html")
        self.assertEqual(self.run_main("--endpoints", "local", "--sample", "3",
                                       "--out", first)[0], 0)
        self.assertEqual(self.run_main("--endpoints", "local", "--sample", "3",
                                       "--endpoint-limit", "local=2", "--out", second)[0], 0)
        merged = self.base.join("merged.html")
        code, output = self.run_main("--merge", self.base.join("a.json"),
                                     self.base.join("b.json"), "--out", merged)
        self.assertEqual(code, 0, output)
        with open(self.base.join("merged.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual([entry["name"] for entry in report["endpoints"]],
                         ["local", "local (2)"])
        self.assertEqual(len(report["images"]), 3)
        self.assertEqual(len(report["endpoints"][1]["results"]), 2)
        with open(merged, encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn("local (2)", page)
        not_a_report = self.base.write_file("x.json", "{}")
        code, output = self.run_main("--merge", not_a_report)
        self.assertEqual(code, 2)
        self.assertIn("not a compare_models.py result file", output)

    def test_unusable_endpoint_is_not_sent_the_whole_sample(self):
        # A cloud endpoint whose replies are always cut off (say, too few
        # output tokens) stops after a few images instead of costing three
        # requests for every image of the sample.
        self.cloud.reply = fake_llm.Reply(finish="length")
        report = compare_models.compare(self.config, "cloud", sample=6,
                                        ctx=jobs.JobContext("compare"),
                                        sleep=lambda seconds: None)
        [cloud] = report["endpoints"]
        self.assertIn("in a row failed", cloud["stopped"])
        self.assertEqual(cloud["summary"]["failures"], 3)
        self.assertEqual(cloud["summary"]["not_run"], 3)
        tries = 1 + describe.CONTENT_RETRIES
        self.assertLessEqual(len(self.cloud.describe_requests()), 4 * tries)

    def test_requests_cut_short_by_a_stop_are_not_failures(self):
        self.cloud.queue(fake_llm.HttpError(503), fake_llm.unauthorized())
        report = compare_models.compare(self.config, "cloud", sample=4,
                                        ctx=jobs.JobContext("compare"),
                                        sleep=lambda seconds: time.sleep(0.3))
        [cloud] = report["endpoints"]
        kinds = sorted(record["kind"] for record in cloud["results"].values())
        self.assertEqual(kinds, ["fatal", "not_run", "not_run", "not_run"])
        self.assertEqual(cloud["summary"]["failures"], 1)

    def test_libraries_kept_local_are_not_sent_to_the_cloud(self):
        config = common.load_config()
        config["libraries"][1]["endpoint"] = "local"
        report = compare_models.compare(config, "local,cloud", sample=6,
                                        ctx=jobs.JobContext("compare"),
                                        sleep=lambda seconds: None)
        local, cloud = report["endpoints"]
        other_ids = {str(image["id"]) for image in report["images"]
                     if image["library"] == "Other"}
        self.assertTrue(other_ids)
        self.assertTrue(all(local["results"][key]["ok"] for key in other_ids))
        for key in other_ids:
            self.assertEqual(cloud["results"][key]["kind"], "not_run")
            self.assertIn("kept on local endpoints", cloud["results"][key]["error"])
        self.assertEqual(cloud["summary"]["described"], len(report["images"]) - len(other_ids))
        sent = [request["prompt"] for request in self.cloud.describe_requests()]
        self.assertFalse(any("Notes for the other library." in text for text in sent))
        # Named with --library, it is sent.
        report = compare_models.compare(config, "cloud", ["Other"], sample=6,
                                        ctx=jobs.JobContext("compare"),
                                        sleep=lambda seconds: None)
        self.assertEqual(report["endpoints"][0]["summary"]["described"], 1)

    def test_a_library_named_twice_is_sampled_once(self):
        libraries = compare_models.choose_libraries(self.config, ["Family", "family"])
        self.assertEqual(len(libraries), 1)
        rows = [row("a%d.jpg" % n) for n in range(3)]
        order = compare_models.stratified_order(rows, ["lib_a", "lib_a"], seed=1)
        self.assertEqual(len(order), 3)

    def test_errors(self):
        code, output = self.run_main("--endpoints", "nothing")
        self.assertEqual(code, 2)
        self.assertIn("Unknown endpoint", output)
        code, output = self.run_main("--endpoints", "local", "--library", "Nope")
        self.assertEqual(code, 2)
        self.assertIn("Unknown library", output)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                compare_models.main([])


if __name__ == "__main__":
    unittest.main()
