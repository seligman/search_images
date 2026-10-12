"""Tests for the admin routes about single pictures (admin.py) and their helpers.

Hide/unhide, "open folder" (the file manager is never really started:
subprocess.Popen is replaced), full paths, and describe jobs for chosen
pictures. The fixture and the request helpers come from test_admin.
"""

import json
import os
import unittest
from unittest import mock

import support

import admin
import jobs
import library_ops
from test_admin import NOW, AdminFixture, insert_image

REMOTE = {"REMOTE_ADDR": "192.0.2.5"}


class PictureFixture(AdminFixture):

    def setUp(self):
        super().setUp()
        self.other = self.base.library_folder("Other")
        self.write_config([support.library("Family", self.family, id="lib_family1"),
                           support.library("Other", self.other, id="lib_other001"),
                           support.library("Off", self.other, id="lib_off00001",
                                           enabled=False)])
        self.file = self.write_picture("2019/Summer trip/pic one.jpg")
        self.picture = insert_image(self.conn, "lib_family1", "2019/Summer trip/pic one.jpg")
        self.second = insert_image(self.conn, "lib_family1", "b.jpg")
        self.elsewhere = insert_image(self.conn, "lib_other001", "c.jpg")
        self.disabled = insert_image(self.conn, "lib_off00001", "d.jpg")
        self.gone = insert_image(self.conn, "lib_family1", "gone.jpg", missing_since=NOW)
        self.orphan = insert_image(self.conn, "lib_orphan01", "lost.jpg")

    def write_picture(self, rel_path, folder=None):
        path = os.path.join(folder or self.family, *rel_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xe0 not really a picture")
        return path

    def hidden_flags(self):
        return {row["id"]: row["hidden"] for row in self.conn.execute(
            "SELECT id, hidden FROM images")}

    def refused_from_outside(self, path, body):
        """Every way an admin request can be refused, for one route."""
        self.assertEqual(self.post(path, body, environ_base=REMOTE).status_code, 403)
        response = self.client.post(admin.URL_PREFIX + path, data=json.dumps(body),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)                     # no header
        self.assertEqual(self.post(path, body, headers={"Sec-Fetch-Site": "cross-site"})
                         .status_code, 403)
        self.assertEqual(self.post(path, body, headers={"Origin": "http://attacker.example"})
                         .status_code, 403)
        response = self.client.post(admin.URL_PREFIX + path, data=json.dumps(body),
                                    content_type="text/plain",
                                    headers={"X-Search-Images": "1"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.get(path).status_code, 405)


class HideTest(PictureFixture, unittest.TestCase):

    def test_hide_and_show_again(self):
        result = self.ok(self.post("/images/hide", {"ids": [self.picture, self.second,
                                                            self.picture, 999999],
                                                    "hidden": True}))
        self.assertEqual(result, {"hidden": True, "changed": 2, "found": 2, "unknown": 1})
        flags = self.hidden_flags()
        self.assertEqual((flags[self.picture], flags[self.second], flags[self.elsewhere]),
                         (1, 1, 0))
        # Hiding again changes nothing.
        result = self.ok(self.post("/images/hide", {"ids": [self.picture], "hidden": True}))
        self.assertEqual(result["changed"], 0)
        result = self.ok(self.post("/images/hide", {"ids": [self.picture], "hidden": False}))
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.hidden_flags()[self.picture], 0)

    def test_many_pictures_at_once(self):
        ids = [insert_image(self.conn, "lib_family1", "many/%d.jpg" % number)
               for number in range(1200)]
        result = self.ok(self.post("/images/hide", {"ids": ids, "hidden": True}))
        self.assertEqual((result["changed"], result["found"]), (1200, 1200))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM images WHERE hidden = 1").fetchone()[0], 1200)
        too_many = list(range(1, admin.HIDE_LIMIT + 2))
        self.assertEqual(self.post("/images/hide", {"ids": too_many, "hidden": True})
                         .status_code, 400)

    def test_requests_that_make_no_sense(self):
        for body in ({"ids": [self.picture]}, {"ids": [self.picture], "hidden": "yes"},
                     {"ids": [self.picture], "hidden": 1}, {"ids": [], "hidden": True},
                     {"ids": "1,2", "hidden": True}, {"ids": [True], "hidden": True},
                     {"ids": ["1"], "hidden": True}, {"ids": [1.5], "hidden": True},
                     {"ids": [0], "hidden": True}, {"ids": [2 ** 63], "hidden": True},
                     {"hidden": True}):
            response = self.post("/images/hide", body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn("error", response.get_json())
        self.assertEqual(set(self.hidden_flags().values()), {0})

    def test_only_the_admin_on_this_computer(self):
        self.refused_from_outside("/images/hide", {"ids": [self.picture], "hidden": True})
        self.assertEqual(set(self.hidden_flags().values()), {0})

    def test_the_search_index_follows(self):
        etag = self.client.get("/api/index").headers["ETag"]
        self.ok(self.post("/images/hide", {"ids": [self.picture], "hidden": True}))
        response = self.client.get("/api/index", headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 200)
        records = {record["i"]: record for record in response.get_json()["records"]}
        self.assertEqual(records[self.picture]["h"], 1)
        remote = self.client.get("/api/index", environ_base=REMOTE).get_json()
        self.assertNotIn(self.picture, [record["i"] for record in remote["records"]])
        # The admin still sees the hidden picture itself; others do not.
        self.assertEqual(self.client.get("/api/image/%d" % self.picture).status_code, 200)
        self.assertEqual(self.client.get("/api/image/%d" % self.picture,
                                         environ_base=REMOTE).status_code, 404)


class OpenFolderTest(PictureFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(library_ops.subprocess, "Popen")
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_file_is_shown(self):
        # A path in the request is ignored: only the picture's own file is shown.
        result = self.ok(self.post("/images/%d/open-folder" % self.picture,
                                   {"path": self.base.join("config.json")}))
        self.assertEqual(result, {"ok": True, "id": self.picture})
        self.assertEqual(self.popen.call_count, 1)
        command = self.popen.call_args[0][0]
        self.assertEqual(command, library_ops.reveal_command(os.path.normpath(self.file)))
        self.assertNotIn("config.json", json.dumps(command))
        self.assertEqual(self.popen.call_args[1], {"close_fds": True})

    def test_commands_per_system(self):
        path = os.path.join(self.family, "a b", "pic.jpg")
        self.assertEqual(library_ops.reveal_command(path, "win32"),
                         'explorer /select,"%s"' % path)
        self.assertEqual(library_ops.reveal_command(path, "darwin"), ["open", "-R", path])
        self.assertEqual(library_ops.reveal_command(path, "linux"),
                         ["xdg-open", os.path.dirname(path)])

    def test_pictures_that_cannot_be_shown(self):
        os.remove(self.write_picture("b.jpg"))      # in the database, not on disk
        escaping = insert_image(self.conn, "lib_family1", "../outside.jpg")
        with open(os.path.join(os.path.dirname(self.family), "outside.jpg"), "wb") as handle:
            handle.write(b"x")
        for image_id in (999999, self.gone, self.orphan, self.second, escaping, 2 ** 63):
            response = self.post("/images/%d/open-folder" % image_id)
            self.assertEqual(response.status_code, 404, image_id)
            self.assertIn("error", response.get_json())
        self.assertEqual(self.popen.call_count, 0)

    def test_an_offline_share_does_not_hang(self):
        def never(*_args, **_kwargs):
            raise library_ops.TimedOut("no answer")
        with mock.patch.object(library_ops, "run_with_timeout", never):
            response = self.post("/images/%d/open-folder" % self.picture)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.popen.call_count, 0)

    def test_no_file_manager(self):
        self.popen.side_effect = FileNotFoundError("xdg-open")
        response = self.post("/images/%d/open-folder" % self.picture)
        self.assertEqual(response.status_code, 501)

    def test_only_the_admin_on_this_computer(self):
        self.refused_from_outside("/images/%d/open-folder" % self.picture, {})
        self.assertEqual(self.popen.call_count, 0)

    def test_files_stay_inside_the_library(self):
        root = self.family
        self.assertEqual(library_ops.library_file(root, "2019/a.jpg"),
                         os.path.normpath(os.path.join(root, "2019", "a.jpg")))
        self.assertEqual(library_ops.library_file(root, "./2019//a.jpg"),
                         os.path.normpath(os.path.join(root, "2019", "a.jpg")))
        for rel in ("", ".", "..", "../x.jpg", "2019/../../x.jpg", None):
            self.assertIsNone(library_ops.library_file(root, rel), rel)
        self.assertIsNone(library_ops.library_file("", "a.jpg"))
        if os.name == "nt":
            self.assertIsNone(library_ops.library_file(root, "C:/Windows/notepad.exe"))
            self.assertIsNone(library_ops.library_file(root, "a.jpg:stream"))
        else:
            self.assertEqual(library_ops.library_file(root, "/etc/passwd"),
                             os.path.join(root, "etc", "passwd"))
            self.assertIsNotNone(library_ops.library_file(root, "Shot at 14:23:01.png"))


class PathsTest(PictureFixture, unittest.TestCase):

    def test_full_paths_in_the_order_asked(self):
        result = self.ok(self.post("/images/paths", {"ids": [self.elsewhere, self.picture,
                                                             self.orphan, 999999]}))
        self.assertEqual(result["paths"], [
            {"id": self.elsewhere, "path": os.path.normpath(os.path.join(self.other, "c.jpg"))},
            {"id": self.picture, "path": os.path.normpath(self.file)}])
        self.assertEqual(result["unknown"], [self.orphan, 999999])
        self.assertEqual(self.post("/images/paths", {"ids": []}).status_code, 400)
        self.refused_from_outside("/images/paths", {"ids": [self.picture]})


class DescribeChosenTest(PictureFixture, unittest.TestCase):

    def queue(self, params, library_id=None, kind="describe"):
        return self.post("/jobs", {"kind": kind, "library_id": library_id, "params": params})

    def test_describe_chosen_pictures_again(self):
        result = self.ok(self.queue({"ids": [self.second, self.picture, self.second],
                                     "force": True}), 202)
        self.assertTrue(result["added"])
        job = result["job"]
        self.assertEqual((job["kind"], job["library_id"]), ("describe", None))
        self.assertEqual(job["params"], {"ids": sorted([self.picture, self.second]),
                                         "force": True})
        # The same pictures in another order are the same job.
        again = self.ok(self.queue({"ids": [self.picture, self.second], "force": True}), 202)
        self.assertFalse(again["added"])
        self.assertEqual(again["job"]["id"], job["id"])

    def test_pictures_that_cannot_be_described_are_left_out(self):
        result = self.ok(self.queue({"ids": [self.picture, self.gone, self.disabled,
                                             self.orphan, self.elsewhere]}), 202)
        self.assertEqual(result["job"]["params"]["ids"], sorted([self.picture, self.elsewhere]))
        # Within one library, only its pictures.
        result = self.ok(self.queue({"ids": [self.picture, self.elsewhere]},
                                    library_id="lib_other001"), 202)
        self.assertEqual(result["job"]["params"]["ids"], [self.elsewhere])
        for ids in ([self.gone], [self.disabled, self.orphan], [999999]):
            response = self.queue({"ids": ids, "force": True})
            self.assertEqual(response.status_code, 404, ids)

    def test_requests_that_make_no_sense(self):
        too_many = list(range(1, admin.DESCRIBE_IDS_LIMIT + 2))
        for params in ({"ids": []}, {"ids": "1"}, {"ids": [True]}, {"ids": too_many},
                       {"force": True}, {"force": "yes", "ids": [self.picture]}):
            response = self.queue(params)
            self.assertEqual(response.status_code, 400, params)
        for kind in ("scan", "process", "previews"):
            response = self.queue({"ids": [self.picture]}, library_id="lib_family1", kind=kind)
            self.assertEqual(response.status_code, 400, kind)
        self.assertEqual(self.manager.snapshot()["queue"], [])

    def test_the_job_passes_them_on(self):
        try:
            import describe
        except (ImportError, SystemExit):
            self.skipTest("describe.py needs Pillow")
        ctx = jobs.JobContext("describe")
        with mock.patch.object(describe, "describe_pending", return_value={}) as run:
            jobs.run_describe(ctx, self.conn, self.config(),
                              {"library_id": None, "ids": [self.second, self.picture],
                               "force": True})
            jobs.run_describe(ctx, self.conn, self.config(), {"library_id": "lib_family1"})
        first, second = run.call_args_list
        self.assertEqual((first[1]["ids"], first[1]["force"]),
                         ([self.second, self.picture], True))
        self.assertEqual(first[1]["library_ids"], ["lib_family1", "lib_other001"])
        self.assertEqual((second[1]["ids"], second[1]["force"]), (None, False))


MODEL_INFO = {"title": "Dog on a beach", "description": "A brown dog runs on the sand.",
              "photo_type": "photo", "tags": ["dog", "beach", "sand"], "subjects": ["dog"]}


class EditsTest(PictureFixture, unittest.TestCase):
    """The owner's titles, descriptions and tags (image_edits)."""

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = 5 "
                          "WHERE id IN (?, ?)",
                          (json.dumps(MODEL_INFO), self.picture, self.second))
        self.conn.commit()
        self.path = "/images/%d/edits" % self.picture

    def index_records(self, **kwargs):
        return {record["i"]: record
                for record in self.client.get("/api/index", **kwargs).get_json()["records"]}

    def test_read_change_and_clear(self):
        answer = self.ok(self.get(self.path))
        self.assertEqual(answer["edited"], False)
        self.assertEqual(answer["edits"], {"title": None, "description": None, "tags_add": [],
                                           "tags_remove": [], "updated": None})
        self.assertEqual(answer["model_info"], MODEL_INFO)
        self.assertEqual(answer["info"], MODEL_INFO)
        etag = self.client.get("/api/index").headers["ETag"]
        answer = self.ok(self.post(self.path, {"title": "Rex at the beach",
                                               "tags_add": ["Rex"], "tags_remove": ["sand"]}))
        self.assertTrue(answer["edited"])
        self.assertEqual(answer["edits"]["title"], "Rex at the beach")
        self.assertEqual(answer["info"]["title"], "Rex at the beach")
        self.assertEqual(answer["info"]["tags"], ["rex", "dog", "beach"])
        self.assertEqual(answer["model_info"], MODEL_INFO)
        self.assertEqual(answer["record"], {"t": "Rex at the beach",
                                            "d": "A brown dog runs on the sand.",
                                            "g": ["rex", "dog", "beach"], "ed": 1})
        # The search index follows at once.
        response = self.client.get("/api/index", headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 200)
        record = self.index_records()[self.picture]
        self.assertEqual((record["t"], record["g"], record["ed"]),
                         ("Rex at the beach", ["rex", "dog", "beach"], 1))
        # Only the fields sent change.
        answer = self.ok(self.post(self.path, {"description": "Rex, our dog."}))
        self.assertEqual((answer["edits"]["title"], answer["edits"]["description"]),
                         ("Rex at the beach", "Rex, our dog."))
        # null puts the model's text back; an empty edit is no edit.
        answer = self.ok(self.post(self.path, {"title": None, "description": None,
                                               "tags_add": [], "tags_remove": None}))
        self.assertFalse(answer["edited"])
        self.assertEqual(answer["info"], MODEL_INFO)
        self.assertEqual(answer["record"], {"t": "Dog on a beach",
                                            "d": "A brown dog runs on the sand.",
                                            "g": ["dog", "beach", "sand"]})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_edits").fetchone()[0], 0)
        self.assertNotIn("ed", self.index_records()[self.picture])

    def test_the_picture_record_shows_the_owners_words(self):
        self.ok(self.post(self.path, {"title": "Rex", "tags_remove": ["beach"]}))
        detail = self.client.get("/api/image/%d" % self.picture).get_json()
        self.assertEqual(detail["info"]["title"], "Rex")
        self.assertEqual(detail["info"]["tags"], ["dog", "sand"])
        self.assertTrue(detail["edited"])
        self.assertEqual(detail["model_info"], MODEL_INFO)
        self.assertEqual(detail["edits"]["tags_remove"], ["beach"])
        # Others see the words shown, not what the model wrote, the edit,
        # or even that there is one.
        remote = self.client.get("/api/image/%d" % self.picture, environ_base=REMOTE)
        detail = remote.get_json()
        self.assertEqual(detail["info"]["title"], "Rex")
        for key in ("model_info", "edits", "edited"):
            self.assertNotIn(key, detail)
        self.assertNotIn("Dog on a beach", remote.get_data(as_text=True))
        records = self.index_records(environ_base=REMOTE)
        self.assertEqual(records[self.picture]["t"], "Rex")
        self.assertNotIn("ed", records[self.picture])
        self.assertEqual(self.index_records()[self.picture]["ed"], 1)

    def test_requests_that_make_no_sense(self):
        for body in ({"title": 5}, {"tags_add": "rex"}, {"tags_add": [1]},
                     {"color": "red"}, {"title": "x" * 201},
                     {"tags_add": ["x" * 61]},
                     # A lone surrogate (JSON can carry one) cannot be stored.
                     {"title": "a" + chr(0xD800) + "b"}, {"tags_add": [chr(0xDC00)]}):
            response = self.post(self.path, body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn("error", response.get_json())
        self.assertEqual(self.post(self.path, ["title"]).status_code, 400)
        for image_id in (999999, self.gone, 2 ** 63):
            self.assertEqual(self.get("/images/%d/edits" % image_id).status_code, 404)
            self.assertEqual(self.post("/images/%d/edits" % image_id, {"title": "x"})
                             .status_code, 404)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_edits").fetchone()[0], 0)

    def test_only_the_admin_on_this_computer(self):
        self.refused_from_outside("/images/tags", {"ids": [self.picture], "add": ["rex"]})
        body = {"title": "Rex"}
        self.assertEqual(self.post(self.path, body, environ_base=REMOTE).status_code, 403)
        response = self.client.post(admin.URL_PREFIX + self.path, data=json.dumps(body),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)                     # no header
        self.assertEqual(self.post(self.path, body, headers={"Sec-Fetch-Site": "cross-site"})
                         .status_code, 403)
        self.assertEqual(self.post(self.path, body, headers={"Origin": "http://attacker.example"})
                         .status_code, 403)
        response = self.client.post(admin.URL_PREFIX + self.path, data=json.dumps(body),
                                    content_type="text/plain", headers={"X-Search-Images": "1"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.get(self.path, environ_base=REMOTE).status_code, 403)
        response = self.client.get(admin.URL_PREFIX + self.path)
        self.assertEqual(response.status_code, 403)                     # no header
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_edits").fetchone()[0], 0)

    def test_tags_of_many_pictures(self):
        ids = [self.picture, self.second, self.elsewhere, 999999]
        answer = self.ok(self.post("/images/tags", {"ids": ids, "add": ["Summer trip"]}))
        self.assertEqual((answer["changed"], answer["found"], answer["unknown"]), (3, 3, 1))
        self.assertEqual(answer["tags"][str(self.picture)],
                         ["summer trip", "dog", "beach", "sand"])
        self.assertEqual(answer["tags"][str(self.elsewhere)], ["summer trip"])
        self.assertTrue(all(answer["edited"].values()))
        records = self.index_records()
        self.assertEqual(records[self.second]["g"][0], "summer trip")
        answer = self.ok(self.post("/images/tags", {"ids": ids, "remove": ["summer trip",
                                                                           "beach"]}))
        self.assertEqual(answer["changed"], 3)
        self.assertEqual(answer["tags"][str(self.picture)], ["dog", "sand"])
        self.assertEqual(answer["tags"][str(self.elsewhere)], [])
        self.assertEqual(answer["edited"][str(self.elsewhere)], False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_edits").fetchone()[0], 2)
        for body in ({"ids": [self.picture]}, {"ids": [self.picture], "add": [], "remove": []},
                     {"ids": [self.picture], "add": "rex"}, {"ids": [], "add": ["rex"]},
                     {"ids": list(range(1, admin.HIDE_LIMIT + 2)), "add": ["rex"]},
                     {"ids": [self.picture], "remove": ["x" * 61]},
                     {"ids": [self.picture], "add": ["x" + chr(0xDC00)]}):
            response = self.post("/images/tags", body)
            self.assertEqual(response.status_code, 400, str(body)[:60])
            self.assertIn("error", response.get_json())


if __name__ == "__main__":
    unittest.main()
