"""Tests for the pages and their scripts: templates, assets and page settings.

The pages are rendered by the Flask test client in a temporary
SETTINGS_BASE. The scripts are checked as text: every element id they
look up exists in their page, they never build HTML from strings, and
(when Node.js is installed) they parse.
"""

import os
import re
import shutil
import subprocess
import unittest

import support

import db
import jobs
import serve

try:
    import flask
except ImportError:
    flask = None

STATIC = os.path.join(support.REPO, "static")
TEMPLATES = os.path.join(support.REPO, "templates")
# Each page: (URL, template, the script that runs it).
PAGES = (("/", "page.html", "app.js"), ("/detail", "detail.html", "detail.js"),
         ("/map", "map.html", "map.js"), ("/manage", "manage.html", "manage.js"),
         ("/settings", "settings.html", "settings.js"),
         ("/duplicates", "duplicates.html", "duplicates.js"))
# Element ids the scripts create themselves (not in a template).
CREATED_IDS = {"shell-header", "shell-toasts", "shell-library", "shell-job",
               "no-endpoint-notice", "no-ffmpeg-notice", "meaning-notice",
               "server-gone"}


quiet = support.quiet


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


page_config = support.page_config


def assets_of(html):
    """The local files a page loads: script src and stylesheet href values
    (not data: addresses, such as the empty icon)."""
    return [url for url in (re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
                            + re.findall(r'<link[^>]*\shref="([^"]+)"', html))
            if not url.startswith("data:")]


def ids_looked_up(script):
    """The element ids a script finds with byId("...") or getElementById("...")."""
    return set(re.findall(r'(?:byId|getElementById)\("([A-Za-z0-9_-]+)"\)', script))


@unittest.skipIf(flask is None, "Flask is not installed")
class PageRenderTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([
            support.library("Family", self.base.library_folder("family"), id="lib_family1"),
            support.library("Trips", self.base.library_folder("trips"), id="lib_trips001"),
            support.library("Old", self.base.library_folder("old"), id="lib_old00001",
                            enabled=False),
        ])
        db.init(log=quiet)
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def test_every_page_renders_with_its_assets(self):
        for url, _template, script in PAGES:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            html = response.get_data(as_text=True)
            assets = assets_of(html)
            for name in ("/static/style.css", "/static/site.js", "/static/shell.js",
                         "/static/" + script):
                self.assertIn(name, assets, url)
            # site.js and shell.js come before the page's own script.
            scripts = [asset for asset in assets if asset.endswith(".js")]
            self.assertEqual(scripts[-1], "/static/" + script, url)
            self.assertLess(scripts.index("/static/site.js"),
                            scripts.index("/static/shell.js"), url)
            for asset in assets:
                served = self.client.get(asset)
                self.assertEqual(served.status_code, 200, asset)
                served.close()

    def test_page_settings_parse_and_name_the_page(self):
        for url, template, _script in PAGES:
            config = page_config(self.client.get(url).get_data(as_text=True))
            self.assertEqual(config["page"], serve.PAGE_NAMES[template])
            self.assertEqual(config["mode"], "server")
            self.assertEqual(config["libraryList"], [
                {"id": "lib_family1", "name": "Family"}, {"id": "lib_trips001", "name": "Trips"}])
            self.assertEqual(config["capabilities"], {"admin": True, "static": False})
            self.assertEqual(config["adminApi"], "/api/admin")

    def test_pages_have_no_inline_code(self):
        # The Content-Security-Policy allows no inline scripts or handlers.
        for url, _template, _script in PAGES:
            html = self.client.get(url).get_data(as_text=True)
            self.assertIsNone(re.search(r"<[^>]+\son[a-z]+\s*=", html, re.I), url)
            for tag in re.findall(r"<script[^>]*>", html):
                self.assertTrue(" src=" in tag or 'type="application/json"' in tag, tag)

    def test_admin_pages_need_this_computer(self):
        remote = {"REMOTE_ADDR": "192.0.2.7"}
        for url in ("/manage", "/settings"):
            self.assertEqual(self.client.get(url, environ_base=remote).status_code, 403)
        config = page_config(self.client.get("/", environ_base=remote).get_data(as_text=True))
        self.assertFalse(config["capabilities"]["admin"])


class ScriptTest(unittest.TestCase):

    def scripts(self):
        return sorted(name for name in os.listdir(STATIC) if name.endswith(".js"))

    def test_scripts_find_only_elements_their_page_has(self):
        shell_ids = ids_looked_up(read(STATIC, "shell.js"))
        for _url, template, script in PAGES:
            html = read(TEMPLATES, template)
            present = set(re.findall(r'\sid="([^"]+)"', html))
            wanted = ids_looked_up(read(STATIC, script)) | shell_ids
            missing = wanted - present - CREATED_IDS
            self.assertEqual(missing, set(), "%s looks up ids missing from %s"
                             % (script, template))

    def test_scripts_never_build_html_from_text(self):
        for name in self.scripts():
            text = read(STATIC, name)
            for match in re.finditer(r"\.(innerHTML|outerHTML)\s*(\+?=)\s*([^;\n]*)", text):
                # Emptying an element is the only use allowed.
                self.assertEqual((match.group(2), match.group(3).strip()), ("=", '""'),
                                 "%s: %s" % (name, match.group(0)))
            for pattern in (r"insertAdjacentHTML", r"document\.write", r"\beval\(",
                            r"new Function\(", r"createContextualFragment"):
                self.assertIsNone(re.search(pattern, text), "%s uses %s" % (name, pattern))

    def test_new_scripts_use_only_text_for_data(self):
        # The admin pages build every element with SHELL.el / textContent.
        for name in ("shell.js", "manage.js", "settings.js", "duplicates.js"):
            self.assertIsNone(re.search(r"\.(innerHTML|outerHTML)\b", read(STATIC, name)),
                              name)

    def test_the_address_never_names_a_folder_or_starts_work(self):
        # ?dialog=... may open a dialog, but nothing taken from the address
        # is sent to the server: no folder to list, no endpoint test without
        # asking first.
        manage = read(STATIC, "manage.js")
        self.assertNotIn("getState().path", manage)
        self.assertIn('folderPicker("", chooseFolder)', manage)
        for name in ("manage.js", "settings.js"):
            text = read(STATIC, name)
            used = set(re.findall(r"\bwanted\.([A-Za-z_]+)", text))
            self.assertLessEqual(used, {"dialog", "id", "name", "step"}, name)
        # The Export dialog's step only picks what it shows (the review, or
        # the last result); an export starts with its button alone.
        self.assertIn("openDialog(wanted.dialog, wanted.id, wanted.step)", manage)
        self.assertEqual(re.findall(r'options\.step === "([a-z]+)"', manage),
                         ["done", "done", "review"])
        self.assertEqual(manage.count('{kind: "export"'), 1)
        self.assertIn('var go = box.button("Export", "primary", startExport);', manage)
        settings = read(STATIC, "settings.js")
        self.assertIn("openDialog(wanted.dialog, wanted.name, true)", settings)
        self.assertIn("testEndpoint(endpoint, {ask: fromAddress})", settings)

    @unittest.skipIf(shutil.which("node") is None, "Node.js is not installed")
    def test_scripts_parse(self):
        for name in self.scripts():
            result = subprocess.run(["node", "--check", os.path.join(STATIC, name)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, "%s: %s" % (name, result.stderr))


@unittest.skipIf(flask is None, "Flask is not installed")
class ExportPageTest(support.TempBaseMixin, unittest.TestCase):

    def test_exported_pages_load_only_files_that_were_copied(self):
        self.base.write_config([support.library("Family", self.base.library_folder("family"),
                                                id="lib_family1")])
        db.init(log=quiet)
        output = self.base.join("data", "exports", "site")
        serve.package(output, log=quiet)
        for page in ("index.html", "detail.html"):
            html = read(output, page)
            for asset in assets_of(html):
                self.assertFalse(asset.startswith("/"), asset)
                # Asked for with the hash of the file's contents (?v=...).
                asset, _sep, version = asset.partition("?v=")
                self.assertRegex(version, r"^[0-9a-f]{10}$", asset)
                self.assertTrue(os.path.isfile(os.path.join(output, asset)), asset)
            config = page_config(html)
            self.assertIs(config["capabilities"]["admin"], False)
            self.assertIs(config["capabilities"]["static"], True)
            self.assertNotIn("adminApi", config)
        # The admin pages and their scripts are not part of an export (nor,
        # without location, the map).
        for name in ("manage.html", "settings.html", "manage.js", "settings.js",
                     "assets/manage.js", "assets/settings.js", "map.html", "assets/map.js",
                     "duplicates.html", "assets/duplicates.js", "assets/duplicates.css"):
            self.assertFalse(os.path.exists(os.path.join(output, name)), name)


if __name__ == "__main__":
    unittest.main()
