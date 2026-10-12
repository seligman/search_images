"""Tests for publishing a library from the Manage page.

The export job (jobs.run_export), the admin routes of the Export dialog
(admin.py: preview, saved options, open folder, export jobs) with the same
security rules as every admin route, and the dialog's rules and words
(static/export-review.js, run under Node.js by tests/js/export_review_test.js;
skipped when `node` is not installed). The file manager is never really
started: subprocess.Popen is replaced. The fixture and the request helpers
come from test_admin.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import support

import admin
import common
import export
import jobs
import library_ops
import prompts
import serve
import test_ui_scripts
from test_admin import NOW, AdminFixture, insert_image

if support.pillow_available():
    import imagegen
else:
    imagegen = None

REMOTE = {"REMOTE_ADDR": "192.0.2.5"}
NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "export_review_test.js")


def info(title, photo_type="photo", **fields):
    """A made-up description (JSON text)."""
    value = {"title": title, "description": "A picture of %s." % title.lower(),
             "tags": ["test"], "subjects": ["test"], "photo_type": photo_type,
             "document_type": "none", "sensitive": "none", "private_info": False}
    value.update(fields)
    return json.dumps(value)


def described(title, photo_type="photo", **fields):
    return {"info": info(title, photo_type, **fields), "described": 1,
            "prompt_ver": prompts.PROMPT_VER}


class ExportFixture(AdminFixture):
    """Family: a photo, a screenshot of a chat (private too), a letter, a bank card,
    a suggestive picture, one not described, a hidden one and a missing one."""

    def setUp(self):
        super().setUp()
        self.off = self.base.library_folder("Off")
        self.write_config([support.library("Family", self.family, id="lib_family1"),
                           support.library("Off", self.off, id="lib_off00001",
                                           enabled=False)])

        def add(path, **fields):
            return insert_image(self.conn, "lib_family1", path, **fields)
        self.photo = add("a.jpg", **described("Sunny beach"))
        self.chat = add("shot.png", **described("Phone chat", "screenshot", private_info=True))
        self.letter = add("letter.jpg", **described("Tax letter", "document"))
        self.card = add("card.jpg", **described("Bank card", private_info=True))
        self.racy = add("racy.jpg", **described("Beach outfit", sensitive="suggestive"))
        self.blank = add("new.jpg")
        self.hidden = add("hidden.jpg", hidden=1, **described("Hidden one"))
        self.gone = add("gone.jpg", missing_since=NOW, **described("Gone one"))

    def stub_file_manager(self):
        """The file manager is never started (subprocess.Popen is replaced everywhere)."""
        patcher = mock.patch.object(library_ops.subprocess, "Popen")
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def preview(self, body=None, library_id="lib_family1"):
        return self.post("/libraries/%s/export/preview" % library_id, body or {})

    def export_job(self, params=None, library_id="lib_family1"):
        return self.post("/jobs", {"kind": "export", "library_id": library_id,
                                   "params": params or {}})

    def saved(self):
        return self.library("lib_family1").get("export")

    def refused_from_outside(self, path, body, post_only=True):
        """Every way an admin request can be refused, for one route."""
        self.assertEqual(self.post(path, body, environ_base=REMOTE).status_code, 403)
        response = self.client.post(admin.URL_PREFIX + path, data=json.dumps(body),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)                     # no header
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Origin": "http://attacker.example"}):
            self.assertEqual(self.post(path, body, headers=headers).status_code, 403, headers)
        response = self.client.post(admin.URL_PREFIX + path, data=json.dumps(body),
                                    content_type="text/plain",
                                    headers={"X-Search-Images": "1"})
        self.assertEqual(response.status_code, 415)
        if post_only:
            self.assertEqual(self.get(path).status_code, 405)


class SecurityTest(ExportFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.stub_file_manager()

    def test_only_the_admin_on_this_computer(self):
        out = tempfile.mkdtemp(prefix="search-images-out-")
        self.addCleanup(support.remove_tree, out)
        for path, body in (("/libraries/lib_family1/export/preview", {}),
                           ("/libraries/lib_family1/export/options", {"options": {"zip": True}}),
                           ("/exports/lib_family1/open-folder", {}),
                           ("/jobs", {"kind": "export", "library_id": "lib_family1",
                                      "params": {"out_dir": out}})):
            self.refused_from_outside(path, body, post_only=path != "/jobs")
        # Nothing was saved, queued or opened.
        self.assertEqual(self.saved(), {})
        self.assertEqual(self.ok(self.get("/jobs"))["queue"], [])
        self.popen.assert_not_called()
        self.assertEqual(os.listdir(out), [])

    def test_turned_off_when_listening_beyond_this_computer(self):
        app = serve.build_app(init_db=False, bind_host="0.0.0.0",
                              job_manager=jobs.JobManager())
        self.addCleanup(app.extensions["search_images"].close)
        client = app.test_client()
        for path in ("/api/admin/libraries/lib_family1/export/preview",
                     "/api/admin/exports/lib_family1/open-folder"):
            response = client.post(path, data="{}", content_type="application/json",
                                   headers={"X-Search-Images": "1"})
            self.assertEqual(response.status_code, 403, path)

    def test_open_folder_never_takes_a_path_from_the_request(self):
        # Before the first export there is nothing to open, whatever the body says.
        for body in ({}, {"path": self.family}, {"folder": self.base.path}):
            response = self.post("/exports/lib_family1/open-folder", body)
            self.assertEqual(response.status_code, 404, body)
        self.assertEqual(self.post("/exports/lib_nothere/open-folder").status_code, 404)
        self.popen.assert_not_called()
        # An export folder (made by hand here) is shown; the body is still ignored.
        folder = export.default_output_dir(self.config(), self.library("lib_family1"))
        os.makedirs(folder)
        with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as handle:
            handle.write("<!DOCTYPE html>")
        answer = self.ok(self.post("/exports/lib_family1/open-folder", {"path": self.family}))
        self.assertEqual(answer, {"ok": True, "path": folder})
        command = str(self.popen.call_args[0][0])
        self.assertIn(folder, command)
        self.assertNotIn(self.family, command)


class PreviewTest(ExportFixture, unittest.TestCase):

    def test_counts_and_flagged_pictures(self):
        answer = self.ok(self.preview())
        counts = answer["counts"]
        # The missing picture is not counted; the hidden one is, but never published.
        self.assertEqual((counts["total"], counts["exported"]), (7, 1))
        self.assertEqual(counts["by_reason"], {"private": 2, "sensitive": 1, "document": 1,
                                               "undescribed": 1, "hidden": 1})
        flagged = {item["id"]: item for item in answer["flagged"]}
        self.assertEqual(set(flagged), {self.chat, self.letter, self.card, self.racy,
                                        self.blank})
        # The most serious flag first: the chat counts as private, not as a screenshot.
        self.assertEqual(flagged[self.chat]["reasons"], ["private", "screenshot"])
        self.assertEqual(flagged[self.chat]["title"], "Phone chat")
        self.assertTrue(all(item["excluded"] for item in flagged.values()))
        self.assertEqual(answer["options"], export.default_options())
        self.assertEqual(answer["saved_options"], export.default_options())
        self.assertEqual(answer["default_output"],
                         os.path.join(common.exports_dir(), "family"))
        self.assertIsNone(answer["last"])
        self.assertGreater(answer["size_estimate"], 0)
        # Never a path of the library, a file name or a folder of it.
        text = json.dumps(answer)
        for secret in (self.family, "shot.png", "card.jpg"):
            self.assertNotIn(json.dumps(secret)[1:-1], text)

    def test_options_change_the_answer_but_are_not_saved(self):
        answer = self.ok(self.preview({"options": {
            "include_ids": [self.letter], "exclude_ids": [self.photo], "images": "none",
            "exclude_private": False}}))
        counts = answer["counts"]
        self.assertEqual(counts["exported"], 2)          # the letter and the card
        # The chat is a screenshot too, and those are still left out.
        self.assertEqual(counts["by_reason"], {"excluded": 1, "screenshot": 1, "sensitive": 1,
                                               "undescribed": 1, "hidden": 1})
        flagged = {item["id"]: item for item in answer["flagged"]}
        self.assertFalse(flagged[self.letter]["excluded"])
        self.assertTrue(flagged[self.photo]["excluded"])
        self.assertEqual(answer["size_parts"]["images"], 0)
        self.assertEqual(self.saved(), {})
        quick = self.ok(self.preview({"flagged": False}))
        self.assertNotIn("flagged", quick)
        self.assertEqual(quick["counts"]["exported"], 1)

    def test_free_space_of_the_folder(self):
        out = tempfile.mkdtemp(prefix="search-images-out-")
        self.addCleanup(support.remove_tree, out)
        for folder in (out, os.path.join(out, "not", "made", "yet"), ""):
            answer = self.ok(self.preview({"flagged": False, "out_dir": folder}))
            self.assertIsInstance(answer["free_bytes"], int, folder)
            self.assertGreater(answer["free_bytes"], 0)
        self.assertNotIn("free_bytes", self.ok(self.preview({"flagged": False})))
        response = self.preview({"out_dir": "relative/site"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("out_dir", response.get_json()["fields"])
        self.assertEqual(os.listdir(out), [])

    def test_bad_requests(self):
        response = self.preview({"options": {"images": "huge", "colour": True}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.get_json()["fields"]), {"images", "colour"})
        for body in ({"options": []}, {"options": "web"}):
            self.assertEqual(self.preview(body).status_code, 400, body)
        self.assertEqual(self.post("/libraries/lib_family1/export/preview",
                                   ["not", "an", "object"]).status_code, 400)
        self.assertEqual(self.preview(library_id="lib_nothere").status_code, 404)


class OptionsTest(ExportFixture, unittest.TestCase):

    def test_saved_options_are_checked_and_merged(self):
        answer = self.ok(self.post("/libraries/lib_family1/export/options", {
            "options": {"images": "none", "title": "  Our   pictures "}}))
        self.assertEqual(answer["saved"], {"images": "none", "title": "Our pictures"})
        self.assertEqual(answer["options"]["exclude_types"], ["screenshot", "document"])
        self.assertEqual(self.saved(), {"images": "none", "title": "Our pictures"})
        # The options sent change; the others stay. Defaults are not stored.
        self.ok(self.post("/libraries/lib_family1/export/options",
                          {"options": {"zip": True, "images": "web",
                                       "include_ids": [self.card, self.card]}}))
        self.assertEqual(self.saved(), {"title": "Our pictures", "zip": True,
                                        "include_ids": [self.card]})
        state = self.ok(self.get("/state"))
        info_view = state["libraries"][0]["export_info"]
        self.assertTrue(info_view["options"]["zip"])
        self.assertEqual(info_view["options"]["include_ids"], [self.card])
        self.assertIsNone(info_view["last"])
        # A preview starts from the saved options.
        counts = self.ok(self.preview({"flagged": False}))["counts"]
        self.assertEqual(counts["exported"], 2)

    def test_bad_options_change_nothing(self):
        before = self.saved()
        for options in ({"images": "huge"}, {"zip": "yes"}, {"exclude_types": ["selfie"]},
                        {"include_ids": ["7"]}, {"output": self.family},
                        {"credits": "x" * (export.MAX_CREDITS + 1)}):
            response = self.post("/libraries/lib_family1/export/options",
                                 {"options": options})
            self.assertEqual(response.status_code, 400, options)
            self.assertEqual(set(response.get_json()["fields"]), set(options), options)
        for body in ({}, {"options": None}, {"options": [1]}):
            self.assertEqual(self.post("/libraries/lib_family1/export/options",
                                       body).status_code, 400, body)
        self.assertEqual(self.post("/libraries/lib_nothere/export/options",
                                   {"options": {}}).status_code, 404)
        self.assertEqual(self.saved(), before)

    def test_a_hand_edit_since_the_page_loaded_gives_409(self):
        token = self.ok(self.get("/state"))["config_token"]
        config = self.config()
        config["reader_threads"] = 3
        common.save_config(config)
        response = self.post("/libraries/lib_family1/export/options",
                             {"options": {"zip": True}, "config_token": token})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.saved(), {})


class ExportJobRequestTest(ExportFixture, unittest.TestCase):

    def test_parameters_are_checked(self):
        out = self.base.path                    # holds the data root
        busy = tempfile.mkdtemp(prefix="search-images-busy-")
        self.addCleanup(support.remove_tree, busy)
        with open(os.path.join(busy, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("not an export")
        inside = os.path.join(support.REPO, "exports-test-not-ignored")
        for body, field in (
                ({"kind": "export"}, "library_id"),
                ({"kind": "export", "library_id": None}, "library_id"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"turbo": True}}, None),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"options": {"images": "huge"}}}, "images"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"options": "web"}}, "options"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"save_options": "yes"}}, "save_options"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"out_dir": "exports/family"}}, "out_dir"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"out_dir": os.path.join(self.family, "site")}}, "out_dir"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"out_dir": out}}, "out_dir"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"out_dir": busy}}, "out_dir"),
                ({"kind": "export", "library_id": "lib_family1",
                  "params": {"out_dir": inside}}, "out_dir"),
                ({"kind": "export", "library_id": "lib_off00001"}, None)):
            response = self.post("/jobs", body)
            self.assertEqual(response.status_code, 400, body)
            if field:
                self.assertIn(field, response.get_json()["fields"], body)
        self.assertEqual(self.export_job(library_id="lib_nothere").status_code, 404)
        self.assertEqual(self.ok(self.get("/jobs"))["queue"], [])
        self.assertEqual(os.listdir(busy), ["notes.txt"])
        self.assertFalse(os.path.exists(inside))
        self.assertEqual(self.saved(), {})

    def test_folders_that_cannot_be_used(self):
        # The export of another library (Off) is never replaced.
        theirs = tempfile.mkdtemp(prefix="search-images-theirs-")
        self.addCleanup(support.remove_tree, theirs)
        export_id = "0123456789abcdef"
        with open(os.path.join(theirs, export.MARKER), "wb") as handle:
            handle.write(export.marker_bytes(export_id))
        export.save_state({"format": 1, "export_id": export_id, "secret": "ab" * 32,
                           "files": {}, "library_id": "lib_off00001", "out_dir": theirs})
        response = self.export_job({"out_dir": theirs})
        self.assertEqual(response.status_code, 400)
        message = response.get_json()["fields"]["out_dir"]
        self.assertIn("holds the export of library Off", message)
        # A folder that cannot be written, or read: plain words, never a 500.
        out = tempfile.mkdtemp(prefix="search-images-out-")
        self.addCleanup(support.remove_tree, out)
        with mock.patch.object(export.os, "mkdir", side_effect=PermissionError(13, "Denied")):
            response = self.export_job({"out_dir": os.path.join(out, "site")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("access is denied", response.get_json()["fields"]["out_dir"])
        with mock.patch.object(export.os, "listdir", side_effect=PermissionError(
                13, "Access is denied", out)):
            with open(os.path.join(out, "x.txt"), "w", encoding="utf-8") as handle:
                handle.write("x")
            response = self.export_job({"out_dir": out})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertIn("access is denied", error)
        self.assertNotIn("PermissionError", error)
        self.assertEqual(self.ok(self.get("/jobs"))["queue"], [])

    def test_options_for_one_export_or_saved(self):
        out = tempfile.mkdtemp(prefix="search-images-out-")
        self.addCleanup(support.remove_tree, out)
        default = export.default_output_dir(self.config(), self.library("lib_family1"))
        answer = self.ok(self.export_job({"options": {"images": "none", "zip": False},
                                          "out_dir": default}), 202)
        # The default folder is not stored: it follows the library's name.
        self.assertEqual(answer["job"]["params"], {"options": {"images": "none",
                                                               "zip": False}})
        self.assertNotIn("config_token", answer)
        self.assertEqual(self.saved(), {})
        again = self.ok(self.export_job({"options": {"zip": False, "images": "none"}}), 202)
        self.assertFalse(again["added"])
        # Saved: the job then uses what is saved when it starts.
        answer = self.ok(self.export_job({"options": {"images": "original",
                                                      "include_ids": [self.card]},
                                          "save_options": True, "out_dir": out}), 202)
        self.assertEqual(answer["job"]["params"], {"out_dir": os.path.abspath(out)})
        self.assertIn("config_token", answer)
        self.assertEqual(self.saved(), {"images": "original", "include_ids": [self.card]})
        self.assertEqual(answer["jobs"]["queue"][-1]["library"], "Family")


class ExportJobTest(ExportFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.stub_file_manager()

    def test_the_runner_needs_one_library_and_a_usable_folder(self):
        config = self.config()
        ctx = jobs.JobContext("test")
        for params in ({"library_id": None}, {"library_id": "lib_off00001"},
                       {"library_id": "lib_nothere"},
                       {"library_id": "lib_family1", "out_dir": self.family},
                       {"library_id": "lib_family1", "options": {"images": "huge"}}):
            with self.assertRaises(jobs.JobError, msg=params):
                jobs.run_export(ctx, self.conn, config, params)
        self.assertIn("export", jobs.RUNNERS)
        self.assertEqual(jobs.stage_label("export"), "Exporting pictures")

    def test_errors_reach_the_owner_in_plain_words(self):
        config = self.config()
        ctx = jobs.JobContext("test")
        with mock.patch.object(export, "export_library", side_effect=PermissionError(
                13, "Permission denied", "C:\\Photos\\site\\x.tmp")):
            with self.assertRaises(jobs.JobError) as caught:
                jobs.run_export(ctx, self.conn, config, {"library_id": "lib_family1"})
        message = str(caught.exception)
        self.assertIn("access is denied", message)
        self.assertNotIn("PermissionError", message)
        self.assertNotIn("\\\\", message)
        # No picture can be read (their files are gone): the job fails,
        # and says why, instead of publishing an empty site.
        self.assertTrue(self.manager.start())
        job = self.ok(self.export_job({"options": {"exclude_private": False,
                                                   "exclude_sensitive": False}}), 202)["job"]
        self.assertTrue(self.manager.wait_idle(60))
        finished = self.manager.get(job["id"])
        self.assertEqual(finished["state"], "failed")
        self.assertIn("could be read", finished["error"])
        self.assertEqual(export.last_export("lib_family1")["pictures"], 0)

    def test_open_folder_of_an_export_that_was_moved(self):
        out = tempfile.mkdtemp(prefix="search-images-moved-")
        self.addCleanup(support.remove_tree, out)
        export.save_state({"format": 1, "export_id": "fedcba9876543210", "secret": "cd" * 32,
                           "files": {}, "library_id": "lib_family1",
                           "out_dir": os.path.join(out, "site"), "updated": 5})
        response = self.post("/exports/lib_family1/open-folder")
        self.assertEqual(response.status_code, 404)
        error = response.get_json()["error"]
        self.assertIn("is not there any more", error)
        self.assertIn(os.path.join(out, "site"), error)
        self.popen.assert_not_called()

    def test_one_export_at_a_time(self):
        lock = jobs.WorkerLock(os.path.join(common.data_dir(), export.LOCK_FILE))
        self.assertTrue(lock.acquire("export.py"))
        try:
            with self.assertRaises(export.ExportError) as caught:
                export.export_library(None, self.conn, self.config(), "lib_family1")
            self.assertIn("Another export is running: export.py", str(caught.exception))
            self.assertTrue(self.manager.start())
            job = self.ok(self.export_job(), 202)["job"]
            self.assertTrue(self.manager.wait_idle(30))
            finished = self.manager.get(job["id"])
            self.assertEqual(finished["state"], "failed")
            self.assertIn("Another export is running", finished["error"])
        finally:
            lock.release()

    @unittest.skipIf(imagegen is None, "Pillow is not installed")
    def test_export_from_the_job_queue(self):
        import scan
        # Real pictures this time (the rows above have no files: drop them).
        self.conn.execute("DELETE FROM images")
        self.conn.commit()
        for number, name in enumerate(("beach.jpg", "chat.png", "lake.jpg")):
            image = imagegen.textured_image((96 + 16 * number, 64))
            data = imagegen.jpeg_bytes(image)
            imagegen.write_bytes(self.family, name, data)
        scan.run(self.config(), jobs.JobContext("test"), describe_images=False)
        titles = {"beach.jpg": described("Sunny beach"), "lake.jpg": described("Lake"),
                  "chat.png": described("Phone chat", "screenshot")}
        for name, fields in titles.items():
            self.conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? "
                              "WHERE path = ?", (fields["info"], fields["prompt_ver"], name))
        self.conn.commit()
        self.assertTrue(self.manager.start())
        answer = self.ok(self.export_job({"options": {"title": "Our pictures"},
                                          "save_options": True}), 202)
        self.assertTrue(self.manager.wait_idle(60))
        job = self.manager.get(answer["job"]["id"])
        self.assertEqual(job["state"], "done", job["error"])
        summary = job["summary"]
        folder = os.path.join(common.exports_dir(), "family")
        self.assertEqual((summary["count"], summary["output"], summary["failed"]),
                         (2, folder, 0))
        self.assertEqual(summary["excluded"], {"screenshot": 1})
        self.assertGreater(summary["bytes"], 0)
        self.assertTrue(os.path.isfile(os.path.join(folder, "index.html")))
        with open(os.path.join(folder, "index.html"), encoding="utf-8") as handle:
            self.assertIn("<title>Our pictures</title>", handle.read())
        snap = self.ok(self.get("/jobs"))
        self.assertEqual(snap["recent"][0]["id"], job["id"])
        self.assertEqual(snap["recent"][0]["library"], "Family")
        last = self.ok(self.get("/state"))["libraries"][0]["export_info"]["last"]
        self.assertEqual((last["output"], last["pictures"], last["finished"]),
                         (folder, 2, True))
        self.assertGreater(last["bytes"], 0)
        # Open folder shows the site's start page.
        self.assertEqual(self.ok(self.post("/exports/lib_family1/open-folder"))["path"], folder)
        self.assertIn(folder, str(self.popen.call_args[0][0]))
        # A second export writes nothing new.
        again = self.ok(self.export_job({"options": {"title": "Our pictures"},
                                         "save_options": True}), 202)
        self.assertTrue(self.manager.wait_idle(60))
        summary = self.manager.get(again["job"]["id"])["summary"]
        self.assertEqual((summary["written"], summary["removed"]), (0, 0))


class DialogTest(ExportFixture, unittest.TestCase):

    def test_the_manage_page_loads_the_dialog_script(self):
        html = self.client.get("/manage").get_data(as_text=True)
        scripts = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
        self.assertEqual(scripts, ["/static/site.js", "/static/shell.js",
                                   "/static/export-review.js", "/static/place-names.js",
                                   "/static/manage.js"])
        self.assertEqual(self.client.get("/static/export-review.js").status_code, 200)
        # Links from other sites never open the dialog (the address is dropped).
        response = self.client.get("/manage?dialog=export&id=lib_family1&step=review",
                                   headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("?", response.headers["Location"])

    def test_the_dialog_scripts_are_plain_and_build_no_markup(self):
        # Like the search page's scripts (test_ui_scripts): old-browser
        # syntax, ASCII, and elements made with DOM calls and textContent
        # only (titles and descriptions can never become markup).
        for name in ("export-review.js", "manage.js", "shell.js"):
            text = test_ui_scripts.read(test_ui_scripts.STATIC, name)
            self.assertTrue(all(ord(ch) < 128 for ch in text), name)
            code = test_ui_scripts.code_only(text)
            for pattern in (r"=>", r"\blet\s+[A-Za-z_$]", r"\bconst\s+[A-Za-z_$]", r"`",
                            r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML",
                            r"document\.write", r"\beval\s*\(", r"\bnew\s+Function\b",
                            r"javascript:"):
                self.assertIsNone(re.search(pattern, code), "%s: %s" % (name, pattern))
        text = test_ui_scripts.read(test_ui_scripts.STATIC, "export-review.js")
        self.assertIn("var EXPORT_REVIEW = (function () {", text)
        self.assertIn("module.exports = EXPORT_REVIEW;", text)

    @unittest.skipIf(NODE is None, "Node.js is not installed")
    def test_words_and_rules_under_node(self):
        folder = tempfile.mkdtemp(prefix="export-review-")
        self.addCleanup(support.remove_tree, folder)
        path = os.path.join(folder, "expected.json")
        reasons = list(export.FLAGS) + ["hidden", "excluded"]
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"options": sorted(export.DEFAULT_OPTIONS),
                       "photo_types": list(prompts.PHOTO_TYPES), "reasons": reasons}, handle)
        result = subprocess.run([NODE, NODE_TESTS, path], cwd=support.REPO,
                                capture_output=True, timeout=120)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


class ForeignZipTest(ExportFixture, unittest.TestCase):

    def test_a_zip_someone_else_made_is_refused_before_the_job_is_queued(self):
        folder = tempfile.mkdtemp(prefix="search-images-zip-")
        self.addCleanup(support.remove_tree, folder)
        out = os.path.join(folder, "Vacation")
        with open(out + ".zip", "wb") as handle:
            handle.write(b"my own archive")
        response = self.export_job({"out_dir": out, "options": {"zip": True}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("was not made by an export", response.get_json()["fields"]["out_dir"])
        self.assertEqual(self.ok(self.get("/jobs"))["queue"], [])
        with open(out + ".zip", "rb") as handle:
            self.assertEqual(handle.read(), b"my own archive")


if __name__ == "__main__":
    unittest.main()
