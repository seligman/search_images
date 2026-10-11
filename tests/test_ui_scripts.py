"""Tests for the scripts of the search, viewer, detail and map pages.

static/site.js (where the pages get their data, the search state in the
address), static/viewer.js (the picture viewer, information pane and 360
degree view), static/app.js (the search page), static/detail.js and
static/map.js.

Their page-free parts are tested under Node.js by tests/js/ui_test.js and
tests/js/map_test.js (the map's projection, pins, wheel and views) (a
development tool only; skipped when `node` is not installed), including a
check that site.js turns the server's full records (/api/image) into the
same compact records /api/index sends. The checks here read the scripts as
text, and render the pages with the Flask test client in a temporary
SETTINGS_BASE.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import support

import db
import jobs
import serve
import test_search_index as fixtures

try:
    import flask
except ImportError:
    flask = None

STATIC = os.path.join(support.REPO, "static")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "ui_test.js")
MAP_TESTS = os.path.join(support.REPO, "tests", "js", "map_test.js")
NODE = shutil.which("node")
SCRIPTS = ("site.js", "viewer.js", "app.js", "detail.js", "map.js")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


code_only = support.code_only


def scripts_of(html):
    return re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)


def run_node(*args, script=NODE_TESTS):
    result = subprocess.run([NODE, script] + list(args), cwd=support.REPO,
                            capture_output=True, timeout=300)
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")


class PageScriptSourceTest(unittest.TestCase):

    def test_ascii_source(self):
        # Characters outside ASCII are written as backslash-u escapes, so a
        # script reads the same whatever encoding a browser assumes for it.
        for name in sorted(os.listdir(STATIC)):
            if name.endswith((".js", ".css")):
                text = read(STATIC, name)
                self.assertTrue(all(ord(ch) < 128 for ch in text), name)

    def test_plain_syntax(self):
        # Like the other page scripts: no arrow functions, let/const,
        # classes, template strings, spread, for...of, modules or regular
        # expression look-behinds (older Safari cannot even load a script
        # that has one).
        for name in SCRIPTS:
            code = code_only(read(STATIC, name))
            for pattern in (r"=>", r"\blet\s+[A-Za-z_$]", r"\bconst\s+[A-Za-z_$]",
                            r"\bclass\s+[A-Za-z_$]", r"`", r"\.\.\.[A-Za-z_$\[]",
                            r"\bfor\s*\([^;)]*\bof\b", r"\bimport\s", r"\bexport\s",
                            r"\(\?<[=!]"):
                self.assertIsNone(re.search(pattern, code), "%s: %s" % (name, pattern))

    def test_nothing_is_built_from_html_text(self):
        # Every element is made with DOM calls and textContent: OCR text,
        # descriptions and file names can never become markup.
        for name in SCRIPTS:
            code = code_only(read(STATIC, name))
            for pattern in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML",
                            r"document\.write", r"\beval\s*\(", r"\bnew\s+Function\b",
                            r"createContextualFragment", r"\bsetTimeout\s*\(\s*\"",
                            r"\.setAttribute\(\s*\"on", r"javascript:"):
                self.assertIsNone(re.search(pattern, code), "%s: %s" % (name, pattern))

    def test_highlights_are_marks_made_from_text(self):
        viewer = read(STATIC, "viewer.js")
        self.assertIn('el("mark", {text: part.text})', viewer)
        self.assertIn("document.createTextNode(part.text)", viewer)

    def test_shared_scripts_also_load_under_node(self):
        for name, global_name in (("site.js", "SITE"), ("viewer.js", "VIEWER"), ("map.js", "MAP")):
            text = read(STATIC, name)
            self.assertIn("var %s = (function () {" % global_name, text)
            self.assertIn("module.exports = %s;" % global_name, text)

    def test_admin_actions_name_pictures_by_id_only(self):
        # Copy path, open folder, hide and describe send picture ids; no
        # path from the page ever goes to the server.
        bodies = []
        for name in ("viewer.js", "app.js"):
            code = read(STATIC, name)
            for url, body in re.findall(r'SHELL\.admin\(("[^"]*"[^,]*),\s*(\{[^}]*\}?)', code):
                bodies.append(body)
                keys = set(re.findall(r"([A-Za-z_]+):", body))
                self.assertLessEqual(keys, {"ids", "hidden", "kind", "library_id", "params"},
                                     "%s: %s %s" % (name, url, body))
        self.assertGreaterEqual(len(bodies), 5)
        viewer = read(STATIC, "viewer.js")
        self.assertIn('SHELL.admin("/images/" + encodeURIComponent(id) + "/open-folder", {})',
                      viewer)

    def test_the_map_view_belongs_to_one_search(self):
        # The search page drops the map's view once its search changes
        # (every change goes through run() and writeUrl()), and links that
        # start a new search leave it out, so the map then fits the new
        # results instead of opening where the old ones were.
        app = read(STATIC, "app.js")
        for name in ("function run(options)", "function writeUrl(options)"):
            body = app[app.index(name):]
            body = body[:body.index("\n    }\n")]
            self.assertIn("dropStaleView();", body, name)
        self.assertEqual(app.count("keepView();"), 2)       # at the start and on Back
        for name, text in (("app.js", app), ("detail.js", read(STATIC, "detail.js")),
                           ("map.js", read(STATIC, "map.js"))):
            chip = text[text.index("chipUrl"):]
            chip = chip[:chip.index("return SITE.searchUrl(target);")]
            self.assertRegex(chip, r'target\.view = "";', name)

    def test_the_map_takes_the_focus_and_leaves_a_way_back(self):
        code = read(STATIC, "map.js")
        # A click or a touch moves the focus to the map (its keys work then).
        for event in ("mousedown", "touchstart"):
            handler = code[code.index('canvas.addEventListener("%s"' % event):]
            handler = handler[:handler.index("\n        }")]
            self.assertIn("takeFocus();", handler, event)
        # "Show these in the grid" tells the search page where it came from
        # (pages opened from disk have no referrer), under the same key.
        key = re.search(r'var FROM_MAP_KEY = "([^"]+)";', code).group(1)
        self.assertIn('var FROM_MAP_KEY = "%s";' % key, read(STATIC, "app.js"))
        self.assertIn("window.sessionStorage.setItem(FROM_MAP_KEY, gridLink.href)", code)
        # The open list is kept in the address, for Back without the
        # browser's page cache.
        self.assertIn("SHELL.setState({pin: formatPoint(", code)
        self.assertIn("SHELL.setState({pin: null});", code)

    def test_the_grid_keeps_only_nearby_thumbnails(self):
        app = read(STATIC, "app.js")
        self.assertIn("new IntersectionObserver(onIntersect", app)
        self.assertRegex(app, r"var BLOCK_TILES = \d+;")
        self.assertIn("clear(block.node);", app)


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PageScriptNodeTest(unittest.TestCase):

    def test_page_free_parts(self):
        code, output = run_node()
        self.assertEqual(code, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")

    def test_map_parts(self):
        # Projection and the repeating world, pins across the date line and
        # while panning, the wheel, fitting the view.
        code, output = run_node(script=MAP_TESTS)
        self.assertEqual(code, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerRecordsTest(fixtures.IndexFixture, unittest.TestCase):
    """The pages work from compact records; site.js makes them from full ones
    where only those exist (older packages, a picture missing from the index).
    Both must agree."""

    def setUp(self):
        super().setUp()
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def add_rows(self):
        conn = self.conn
        extended = dict(fixtures.V5_INFO, objects=[{"name": "ball", "count": 2}],
                        landmarks=["pier"], activities=["running"], mood="cheerful",
                        location_guess="seaside", sensitive="suggestive", private_info=True,
                        people="3-5", people_descriptors=["child in a red coat"],
                        document_type="menu", text="Fish & chips 4.50",
                        food=["fish"], vehicles=["boat"], time_of_day="sunrise_sunset",
                        event="holiday", setting="outdoor")
        prov = json.dumps({"model": "m", "prompt_ver": 5})
        fixtures.insert_image(conn, "lib_family1", "2019/Beach/IMG_0001.jpg",
                              info=fixtures.V5_INFO, described=1, prompt_ver=5, provenance=prov,
                              dhash="0f0f0f0f0f0f0f0f", content_hash="aa",
                              taken_at="2019-05-14T10:00:00", camera="Apple iPhone 12",
                              lat=1.0, lon=2.0)
        fixtures.insert_image(conn, "lib_family1", "2019/Beach/IMG_0002.jpg",
                              info=fixtures.V5_INFO, described=1, prompt_ver=5, provenance=prov,
                              dhash="0f0f0f0f0f0f0f0e", content_hash="bb",
                              taken_at="2019-05-14T10:00:05")
        fixtures.insert_image(conn, "lib_other001", "trip.jpg", info=extended, described=1,
                              prompt_ver=5, provenance=prov, projection="equirectangular",
                              width=4096, height=2048, hidden=1)
        fixtures.insert_image(conn, "lib_other001", "cars/old.jpg", described=1, prompt_ver=4,
                              info={"description": "Old style description of a red car",
                                    "subjects": ["car"], "text": "", "tags": ["Car", "red"],
                                    "colors": ["Red"], "sharpness": 0.8,
                                    "aesthetic_score": 0.9},
                              taken_at="2015-07-01T12:00:00")
        fixtures.insert_image(conn, "lib_family1", "new.jpg")

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_site_js_makes_the_records_the_server_sends(self):
        self.add_rows()
        index = self.client.get("/api/index")
        self.assertEqual(index.status_code, 200)
        records = index.get_json()["records"]
        self.assertEqual(len(records), 5)
        self.assertTrue(any("dg" in rec for rec in records))
        # Version 4 rated sharpness from 0 to 1: it is no 1-5 score.
        (old,) = [rec for rec in records if rec.get("v4")]
        self.assertNotIn("sh", old)
        self.assertNotIn("q", old)
        pairs = []
        for rec in records:
            detail = self.client.get("/api/image/%d" % rec["i"])
            self.assertEqual(detail.status_code, 200)
            pairs.append({"record": rec, "detail": detail.get_json()})
        folder = tempfile.mkdtemp(prefix="ui-records-")
        self.addCleanup(support.remove_tree, folder)
        path = os.path.join(folder, "pairs.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"pairs": pairs}, handle)
        code, output = run_node("--compare", path)
        self.assertEqual(code, 0, "\n" + output)
        self.assertIn("5 of 5 passed", output)


@unittest.skipIf(flask is None, "Flask is not installed")
class SearchPagesRenderTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([
            support.library("Family", self.base.library_folder("family"), id="lib_family1"),
        ])
        db.init(log=fixtures.quiet)
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        self.assertIn("Content-Security-Policy", response.headers)
        return response.get_data(as_text=True)

    def test_search_page_loads_the_engine_and_the_viewer_first(self):
        html = self.page("/?q=beach&type=photo#id=3")
        self.assertEqual(scripts_of(html), ["/static/site.js", "/static/shell.js",
                                            "/static/search-core.js", "/static/viewer.js",
                                            "/static/app.js"])
        for element in ("query", "syntax-help", "sort", "group", "size", "facets",
                        "facet-list", "active-filters", "selection-bar", "index-banner",
                        "grid", "empty", "month-bar", "result-count"):
            self.assertIn('id="%s"' % element, html)
        # The search box is labelled, and the address never reaches the HTML.
        self.assertIn('aria-label="Search pictures"', html)
        self.assertNotIn("beach", html)

    def test_detail_page_uses_the_viewer(self):
        html = self.page("/detail?id=12&q=dog")
        self.assertEqual(scripts_of(html), ["/static/site.js", "/static/shell.js",
                                            "/static/search-core.js", "/static/viewer.js",
                                            "/static/detail.js"])
        for element in ("back", "detail-stage", "detail-tools", "detail-info",
                        "detail-message"):
            self.assertIn('id="%s"' % element, html)
        self.assertNotIn("dog", html)

    def test_map_page_searches_too(self):
        html = self.page("/map?q=beach&view=10,20,3")
        self.assertEqual(scripts_of(html), ["/static/site.js", "/static/shell.js",
                                            "/static/search-core.js", "/static/viewer.js",
                                            "/static/map.js"])
        for element in ("map-search", "map-status", "map-canvas", "map-message", "map-panel",
                        "map-panel-title", "map-panel-grid", "map-panel-list", "map-panel-more",
                        "zoom-in", "zoom-out", "map-panel-close", "map-target"):
            self.assertIn('id="%s"' % element, html)
        self.assertNotIn("beach", html)
        # The buttons with a symbol say in words what they do.
        for button in re.findall(r"<button[^>]*>[^<]*</button>", html):
            if "map-panel-more" not in button:
                self.assertIn("aria-label=", button)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('<canvas id="map-canvas" tabindex="0"', html)
        # Screen readers pass the map's keys on (it is not a still picture)
        # and read out which pin Enter would open.
        self.assertIn('role="application" aria-roledescription="map"', html)
        self.assertNotIn('role="img"', html)
        self.assertRegex(html, r'<span id="map-target" class="visually-hidden" aria-live="polite">')

    def test_pages_name_no_favicon_file(self):
        # An empty data: icon, so browsers do not ask the server for one.
        for url in ("/", "/detail?id=1", "/map", "/manage", "/settings"):
            self.assertIn('<link rel="icon" href="data:,">', self.page(url))

    def test_exported_pages_get_the_new_scripts(self):
        import export
        for name in ("search-core.js", "viewer.js", "shell.js", "site.js"):
            self.assertIn(name, export.ASSETS)


if __name__ == "__main__":
    unittest.main()
