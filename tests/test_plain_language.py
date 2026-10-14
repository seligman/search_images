"""Tests for plain-language search on the Python side: its switch (config.json's
"plain_language", the pages' capabilities.plain, an export's), the place of its chips
on the search page, and the names an export publishes for it (manifest.names, with
the "people_names" option).

How a query is understood is tested in tests/js/search_core_test.js (interpret,
Per-term coverage), the chips in tests/js/app_plain_test.js.
"""

import os
import shutil
import subprocess
import unittest

import support

import db
import jobs
import search_index
import serve

try:
    import flask
except ImportError:
    flask = None

NODE = shutil.which("node")


class SettingTest(unittest.TestCase):

    def test_on_unless_turned_off(self):
        for config in ({}, None, {"plain_language": True}, {"plain_language": {}},
                       {"plain_language": {"enabled": True}}, {"plain_language": "no"}):
            self.assertTrue(search_index.plain_language(config), config)
        for config in ({"plain_language": False}, {"plain_language": {"enabled": False}}):
            self.assertFalse(search_index.plain_language(config), config)


class TextsTest(unittest.TestCase):

    def test_the_most_texts_embedded_at_once_agree(self):
        # search-core.js meaningTexts and site.js send at most what /api/embed takes.
        import re
        import embeddings
        for name in ("search-core.js", "site.js"):
            with open(os.path.join(support.REPO, "static", name), encoding="utf-8") as handle:
                found = re.findall(r"var MEANING_TEXTS = (\d+);", handle.read())
            self.assertEqual([int(value) for value in found], [embeddings.QUERY_TEXTS], name)


@unittest.skipIf(flask is None, "Flask is not installed")
class PageTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.libraries = [support.library("Family", self.base.library_folder("family"),
                                          id="lib_family1")]
        self.base.write_config(self.libraries)
        db.init(log=support.quiet)
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def capabilities(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return support.page_config(response.get_data(as_text=True))["capabilities"]

    def test_the_pages_hear_the_switch(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("plain", support.page_config(html)["capabilities"])
        # The chips go above the other filters of the results.
        self.assertLess(html.index('id="plain-chips"'), html.index('id="active-filters"'))
        self.base.write_config(self.libraries, plain_language=False)
        for url in ("/", "/map", "/albums"):
            self.assertIs(self.capabilities(url)["plain"], False, url)
        self.base.write_config(self.libraries, plain_language={"enabled": True})
        self.assertNotIn("plain", self.capabilities("/"))


@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class ExportTest(unittest.TestCase):
    """An export made while plain-language search is off is off too; one with the
    people_names option lists the names it publishes (see also test_people_groups)."""

    def test_an_export_hears_the_switch(self):
        from test_people_groups import ExportTest as PeopleExport
        case = PeopleExport("test_names_go_only_with_the_option_and_only_as_tags")
        case.setUp()
        try:
            from test_export import read_manifest
            out = case.export(people_names=True)
            manifest = read_manifest(out)
            self.assertEqual(manifest["names"], ["Zelda Quorn"])
            self.assertNotIn("plain", manifest["capabilities"])
            # Without the option: no names.
            self.assertNotIn("names", read_manifest(case.export()))
            # Off when it was made: the pages say so.
            case.config["plain_language"] = False
            out = case.export()
            self.assertIs(read_manifest(out)["capabilities"]["plain"], False)
            with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
                self.assertIs(support.page_config(handle.read())["capabilities"]["plain"],
                              False)
        finally:
            case.doCleanups()
            case.tearDown()


@unittest.skipIf(NODE is None, "Node.js is not installed")
class SearchPageScriptTest(unittest.TestCase):

    def test_the_chips_of_the_search_page(self):
        # tests/js/app_plain_test.js: the real page scripts on a made-up DOM.
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "app_plain_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
