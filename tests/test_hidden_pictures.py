"""Hidden pictures are never sent to an endpoint outside this computer and network
(describe, refine and compare_models), but local endpoints still describe them."""

import json
import unittest

import support

if not support.pillow_available():
    raise unittest.SkipTest("Pillow is not installed")

import fake_llm
from test_describe import DescribeTestCase, reply
from test_refine import DOCUMENT, RefineTestCase

import admin
import common
import compare_models
import jobs
import serve

try:
    import flask
except ImportError:
    flask = None

# Not a real key: a made-up value used only by these tests.
CLOUD_KEY = "test-key-hidden-0101"


class HiddenDescribeTest(DescribeTestCase):

    def setUp(self):
        super().setUp()
        self.cloud = fake_llm.FakeLLM().start()
        self.addCleanup(self.cloud.stop)
        self.cloud.props = False

    def cloud_endpoint(self, **settings):
        return self.endpoint(name="cloud", preset="openai", base_url=self.cloud.base_url,
                             api_key=CLOUD_KEY, model="cloud-model", **settings)

    def test_hidden_pictures_stay_away_from_cloud_services(self):
        self.small_images(3)
        self.configure([support.library("Family", self.folder, endpoint="cloud")],
                       endpoints=[self.endpoint(name="local"), self.cloud_endpoint()],
                       default_endpoint="local")
        self.index()
        self.execute("UPDATE images SET hidden = 1 WHERE path = 'photo1.jpg'")
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped_hidden"], summary["skipped"]),
                         (2, 1, 1))
        self.assertEqual(len(self.cloud.describe_requests()), 2)
        self.assertEqual(self.server.describe_requests(), [])
        self.assertEqual(self.rows()["photo1.jpg"]["described"], 0)
        self.assertIn("1 hidden picture(s) were not sent to 127.0.0.1", self.log_text())
        # Not even when it is asked for by id.
        row_id = self.rows()["photo1.jpg"]["id"]
        summary = self.describe(ids=[row_id], force=True)
        self.assertEqual((summary["described"], summary["skipped_hidden"]), (0, 1))
        self.assertEqual(len(self.cloud.describe_requests()), 2)

    def test_a_local_endpoint_still_describes_them(self):
        self.small_images(2)
        self.configure(endpoints=[self.endpoint(name="local")], default_endpoint="local")
        self.index()
        self.execute("UPDATE images SET hidden = 1")
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped_hidden"]), (2, 0))

    def test_in_a_group_they_only_go_to_its_local_members(self):
        self.small_images(4)
        self.configure([support.library("Family", self.folder, endpoint="mixed")],
                       endpoints=[self.endpoint(name="local", group="mixed"),
                                  self.cloud_endpoint(group="mixed")],
                       default_endpoint="local")
        self.index()
        self.execute("UPDATE images SET hidden = 1 WHERE path IN ('photo0.jpg', 'photo2.jpg')")
        summary = self.describe()
        self.assertEqual((summary["described"], summary["skipped_hidden"]), (4, 0))
        endpoints = {path: json.loads(row["provenance"])["endpoint"]
                     for path, row in self.rows().items()}
        self.assertEqual(endpoints["photo0.jpg"], "local")
        self.assertEqual(endpoints["photo2.jpg"], "local")
        self.assertEqual(len(self.cloud.describe_requests()) + len(
            self.server.describe_requests()), 4)


    def test_a_limit_counts_only_pictures_that_can_be_sent(self):
        self.small_images(4)
        self.configure([support.library("Family", self.folder, endpoint="cloud")],
                       endpoints=[self.endpoint(name="local"), self.cloud_endpoint()],
                       default_endpoint="local")
        self.index()
        # The newest ones (described first) are hidden.
        self.execute("UPDATE images SET hidden = 1 WHERE path IN ('photo2.jpg', 'photo3.jpg')")
        summary = self.describe(limit=2, newest_first=True)
        self.assertEqual((summary["described"], summary["pending"], summary["skipped_hidden"]),
                         (2, 2, 2))
        self.assertEqual({path for path, row in self.rows().items() if row["described"]},
                         {"photo0.jpg", "photo1.jpg"})

    @unittest.skipIf(flask is None, "Flask is not installed")
    def test_the_manage_page_does_not_count_them_as_to_describe(self):
        self.small_images(3)
        self.configure([support.library("Family", self.folder, endpoint="cloud")],
                       endpoints=[self.endpoint(name="local"), self.cloud_endpoint()],
                       default_endpoint="local")
        self.index()
        self.execute("UPDATE images SET hidden = 1 WHERE path = 'photo1.jpg'")
        manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(manager.stop, 30)
        app = serve.build_app(init_db=False, job_manager=manager)
        app.testing = True
        self.addCleanup(app.extensions["search_images"].close)
        state = app.test_client().get(admin.URL_PREFIX + "/state",
                                      headers={"X-Search-Images": "1"}).get_json()
        [library] = state["libraries"]
        self.assertEqual((library["counts"]["pending"], library["counts"]["pending_hidden"]),
                         (2, 1))
        self.assertEqual((library["endpoint_info"]["cloud"], library["endpoint_info"]["local"]),
                         (True, False))
        # A library with a local endpoint describes them, so they are to describe there.
        config = common.load_config()
        config["libraries"][0]["endpoint"] = "local"
        common.save_config(config)
        state = app.test_client().get(admin.URL_PREFIX + "/state",
                                      headers={"X-Search-Images": "1"}).get_json()
        counts = state["libraries"][0]["counts"]
        self.assertEqual(counts["pending"], 3)
        self.assertNotIn("pending_hidden", counts)


class HiddenRefineTest(RefineTestCase):

    def test_hidden_documents_are_not_sent_to_a_cloud_refine_endpoint(self):
        self.small_images(2)
        cloud = fake_llm.FakeLLM().start()
        self.addCleanup(cloud.stop)
        cloud.props = False
        cloud.reply = reply(title="Letter", description="A letter.")
        self.configure([support.library("Family", self.folder, refine={"endpoint": "cloud"})],
                       endpoints=[self.endpoint(name="local"),
                                  self.endpoint(name="cloud", preset="openai",
                                                base_url=cloud.base_url, api_key=CLOUD_KEY,
                                                model="cloud-model")],
                       default_endpoint="local")
        self.index()
        self.set_info("photo0.jpg", DOCUMENT)
        self.set_info("photo1.jpg", DOCUMENT, hidden=1)
        summary = self.refine()
        self.assertEqual((summary["described"], summary["skipped_hidden"]), (1, 1))
        self.assertEqual(len(cloud.describe_requests()), 1)
        self.assertNotIn("tier", json.loads(self.rows()["photo1.jpg"]["provenance"]))


class HiddenCompareTest(DescribeTestCase):

    def test_compare_models_keeps_hidden_pictures_local(self):
        cloud = fake_llm.FakeLLM().start()
        self.addCleanup(cloud.stop)
        cloud.props = False
        self.small_images(3)
        self.configure(endpoints=[
            self.endpoint(name="local"),
            self.endpoint(name="cloud", preset="openai", base_url=cloud.base_url,
                          api_key=CLOUD_KEY, model="cloud-model")], default_endpoint="local")
        self.index()
        self.execute("UPDATE images SET hidden = 1 WHERE path = 'photo1.jpg'")
        hidden_id = str(self.rows()["photo1.jpg"]["id"])
        ctx = jobs.JobContext("compare", log_limit=1000)
        report = compare_models.compare(self.config, "local,cloud", sample=3, ctx=ctx,
                                        sleep=lambda seconds: None)
        local, cloud_entry = report["endpoints"]
        self.assertTrue(local["results"][hidden_id]["ok"])
        self.assertEqual(cloud_entry["results"][hidden_id]["kind"], "not_run")
        self.assertIn("hidden", cloud_entry["results"][hidden_id]["error"])
        self.assertEqual(len(cloud.describe_requests()), 2)
        self.assertEqual(cloud_entry["summary"]["not_run"], 1)
        messages = [entry["message"] for entry in ctx.snapshot(log_tail=1000)["log_tail"]]
        self.assertTrue(any("1 hidden picture(s) were not sent to cloud" in text
                            for text in messages))


if __name__ == "__main__":
    unittest.main()
