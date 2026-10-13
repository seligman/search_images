"""Tests for the admin routes of favorites, ratings and albums (admin.py, albums.py),
the Albums page and the marks in /api/image and /api/index (serve.py).

The fixture and the request helpers come from test_admin.
"""

import json
import os
import shutil
import subprocess
import unittest

import support

import admin
import albums
from test_admin import NOW, AdminFixture, insert_image
from test_admin_images import REMOTE, PictureFixture

page_config = support.page_config
NODE = shutil.which("node")


class MarksRouteTest(PictureFixture, unittest.TestCase):

    def test_favorite_and_rate(self):
        result = self.ok(self.post("/marks", {"ids": [self.picture, self.second, self.gone,
                                                      999999], "favorite": True}))
        self.assertEqual((result["changed"], result["found"], result["unknown"]), (2, 2, 2))
        self.assertEqual(result["marks"][str(self.picture)], {"favorite": True, "rating": 0})
        result = self.ok(self.post("/marks", {"ids": [self.picture], "rating": 5}))
        self.assertEqual(result["marks"], {str(self.picture): {"favorite": True, "rating": 5}})
        result = self.ok(self.post("/marks", {"ids": [self.picture], "rating": 0,
                                              "favorite": False}))
        self.assertEqual(result["marks"][str(self.picture)], {"favorite": False, "rating": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_marks").fetchone()[0], 1)

    def test_bad_requests(self):
        for body in ({"ids": [self.picture]}, {"ids": [self.picture], "rating": 6},
                     {"ids": [self.picture], "rating": "5"},
                     {"ids": [self.picture], "favorite": "yes"}, {"ids": [], "rating": 1},
                     {"ids": ["1"], "rating": 1}, {"ids": [True], "rating": 1}):
            self.assertEqual(self.post("/marks", body).status_code, 400, body)
        many = list(range(1, albums.MARK_LIMIT + 2))
        self.assertEqual(self.post("/marks", {"ids": many, "rating": 1}).status_code, 400)
        self.refused_from_outside("/marks", {"ids": [self.picture], "favorite": True})

    def test_ten_thousand_at_once(self):
        ids = [insert_image(self.conn, "lib_family1", "many/%d.jpg" % number)
               for number in range(albums.MARK_LIMIT - 10)]
        result = self.ok(self.post("/marks", {"ids": ids, "favorite": True}))
        self.assertEqual(result["changed"], len(ids))


class AlbumRouteTest(PictureFixture, unittest.TestCase):

    def test_make_change_and_delete_an_album(self):
        reply = self.ok(self.post("/albums", {"name": "Summer trip",
                                              "ids": [self.second, self.picture]}), 201)
        album = reply["album"]
        self.assertEqual((album["name"], album["kind"], album["count"]),
                         ("Summer trip", "manual", 2))
        self.assertEqual(self.post("/albums", {"name": "SUMMER TRIP"}).status_code, 409)
        self.assertEqual(self.post("/albums", {"name": ""}).status_code, 400)
        path = "/albums/%d" % album["id"]
        detail = self.ok(self.get(path))
        self.assertEqual(detail["items"], [self.second, self.picture])
        result = self.ok(self.post(path + "/items", {"add": [self.elsewhere, self.gone],
                                                     "remove": [self.second]}))
        self.assertEqual((result["added"], result["removed"], result["count"]), (1, 1, 2))
        result = self.ok(self.post(path + "/order", {"ids": [self.elsewhere],
                                                     "before": self.picture}))
        self.assertEqual(result["order"], [self.elsewhere, self.picture])
        result = self.ok(self.post(path + "/order", {"order": [self.picture]}))
        self.assertEqual(result["order"], [self.picture, self.elsewhere])
        self.assertEqual(self.post(path + "/order", {"ids": [self.picture],
                                                     "before": self.second}).status_code, 400)
        # Pictures to move: as many as one request may name elsewhere.
        many = list(range(1, albums.MARK_LIMIT + 2))
        self.assertEqual(self.post(path + "/order", {"ids": many}).status_code, 400)
        renamed =self.ok(self.post(path, {"name": "Road trip", "cover": self.elsewhere}))
        self.assertEqual((renamed["album"]["name"], renamed["album"]["cover"]),
                         ("Road trip", self.elsewhere))
        self.assertEqual(self.post(path, {"cover": self.second}).status_code, 400)
        self.assertEqual(self.post(path, {"color": "red"}).status_code, 400)
        listed = self.ok(self.get("/albums"))["albums"]
        self.assertEqual([(item["name"], item["count"]) for item in listed], [("Road trip", 2)])
        result = self.ok(self.post(path + "/delete", {}))
        self.assertEqual(result, {"deleted": album["id"], "pictures": 2})
        self.assertEqual(self.get(path).status_code, 404)
        self.assertEqual(self.post(path + "/delete", {}).status_code, 404)
        self.assertEqual(self.post("/albums/99999999999999999999/delete", {}).status_code, 404)
        # The pictures stay.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images WHERE id IN (?, ?)",
                                           (self.picture, self.elsewhere)).fetchone()[0], 2)

    def test_smart_albums(self):
        options = {"filters": {"favorite": ["yes"]}, "sort": "newest", "by": "words"}
        album = self.ok(self.post("/albums", {"name": "Best beaches", "kind": "smart",
                                              "query": "beach", "options": options}),
                        201)["album"]
        self.assertEqual((album["query"], album["options"]), ("beach", options))
        path = "/albums/%d" % album["id"]
        self.assertEqual(self.post(path + "/items", {"add": [self.picture]}).status_code, 409)
        self.assertEqual(self.post(path + "/order", {"order": [self.picture]}).status_code,
                         409)
        changed = self.ok(self.post(path, {"query": "sunset beach"}))["album"]
        self.assertEqual((changed["query"], changed["options"]), ("sunset beach", options))
        for body in ({"name": "x", "kind": "smart", "query": ""},
                     {"name": "y", "kind": "smart", "query": "a", "options": {"sort": "x"}},
                     {"name": "z", "kind": "smart", "query": "a", "ids": [self.picture]},
                     {"name": "w", "kind": "nope"}):
            self.assertEqual(self.post("/albums", body).status_code, 400, body)

    def test_refused_from_outside(self):
        album = albums.create_album(self.conn, "Trip", ids=[self.picture])
        self.conn.commit()
        path = "/albums/%d" % album["id"]
        for route, body in (("/albums", {"name": "x"}), (path, {"name": "y"}),
                            (path + "/delete", {}), (path + "/items", {"add": [self.second]}),
                            (path + "/order", {"order": [self.picture]})):
            self.assertEqual(self.post(route, body, environ_base=REMOTE).status_code, 403)
            response = self.client.post(admin.URL_PREFIX + route, data=json.dumps(body),
                                        content_type="application/json")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.post(route, body,
                                       headers={"Sec-Fetch-Site": "cross-site"}).status_code,
                             403)
        self.assertEqual(self.get("/albums", environ_base=REMOTE).status_code, 403)
        self.assertEqual(albums.get_album(self.conn, album["id"])["name"], "Trip")


class PagesTest(PictureFixture, unittest.TestCase):

    def test_the_albums_page_and_the_marks_of_a_picture(self):
        album = albums.create_album(self.conn, "Summer", ids=[self.picture])
        albums.set_marks(self.conn, [self.picture], favorite=True, rating=3)
        self.conn.commit()
        response = self.client.get("/albums")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("albums.js", html)
        config = page_config(html)
        self.assertEqual(config["page"], "albums")
        self.assertTrue(config["capabilities"]["admin"])
        # Others may look (read only): the page, but no admin API.
        response = self.client.get("/albums", environ_base=REMOTE)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(page_config(response.get_data(as_text=True))["capabilities"]["admin"])
        detail = self.client.get("/api/image/%d" % self.picture).get_json()
        self.assertEqual(detail["marks"], {"favorite": True, "rating": 3,
                                           "albums": [{"id": album["id"], "name": "Summer"}]})
        index = self.client.get("/api/index").get_json()
        self.assertEqual([item["name"] for item in index["albums"]], ["Summer"])
        record = [item for item in index["records"] if item["i"] == self.picture][0]
        self.assertEqual((record["fav"], record["rt"], record["al"]), (1, 3, [album["id"]]))

    def test_others_are_not_told_of_albums_they_cannot_open(self):
        hidden = insert_image(self.conn, "lib_family1", "secret.jpg", hidden=1)
        albums.create_album(self.conn, "Shown", ids=[self.picture])
        albums.create_album(self.conn, "Hidden only", ids=[hidden])
        albums.create_album(self.conn, "Smart elsewhere", kind="smart", query="x",
                            options={"lib": "lib_gone0001"})
        self.conn.commit()
        mine = self.client.get("/api/index").get_json()
        self.assertEqual(sorted(item["name"] for item in mine["albums"]),
                         ["Hidden only", "Shown", "Smart elsewhere"])
        theirs = self.client.get("/api/index", environ_base=REMOTE).get_json()
        self.assertEqual([item["name"] for item in theirs["albums"]], ["Shown"])



@unittest.skipIf(NODE is None, "Node.js is not installed")
class SearchPageScriptTest(unittest.TestCase):

    def test_the_album_picker_marks_and_save_as_album(self):
        # tests/js/app_albums_test.js: the real page scripts on a made-up DOM.
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "app_albums_test.js")],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
