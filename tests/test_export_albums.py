"""Tests for an export limited to an album, and the "marks" option (export.py).

A temporary data root gets a library of synthesized pictures (and a second
library), a real scan, made-up descriptions, favorites, ratings and albums.
An export limited to an album publishes only its pictures; the owner's
favorites, ratings and album names go out only with the marks option.
tests/js/export_test.js (Node.js; skipped when it is missing) loads the
exports the way the pages do and searches them.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import support

import albums
import common
import db
import export
import jobs
import scan
from test_export import read_manifest, read_records

NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "export_test.js")
ALBUM = "Trip to publish"
OTHER_ALBUM = "Secret album name"
SMART_ALBUM = "Smart secret album"
SMART_QUERY = "secretsmartquery"


def info(title, **fields):
    value = {"title": title, "description": "A picture of %s." % title.lower(),
             "tags": ["tree"], "subjects": ["tree"], "photo_type": "photo",
             "document_type": "none", "setting": "outdoor", "time_of_day": "day",
             "event": "none", "people": "none", "sensitive": "none", "private_info": False,
             "aesthetic": 3, "sharpness": 4, "text": "", "colors": ["green"]}
    value.update(fields)
    return value


@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class AlbumExportTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        from test_scan import write_jpeg
        import describe
        self.family = self.base.library_folder("Family")
        self.other_folder = self.base.library_folder("Other")
        for number in range(30):
            write_jpeg(os.path.join(self.family, "p%02d.jpg" % number), seed=number + 1)
        for number in range(2):
            write_jpeg(os.path.join(self.other_folder, "o%d.jpg" % number), seed=100 + number)
        self.base.write_config([support.library("Family", self.family),
                                support.library("Other", self.other_folder)])
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        scan.run(self.config, jobs.JobContext("test"), describe_images=False)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.library_id = self.config["libraries"][0]["id"]
        self.ids = {}
        for row in self.conn.execute("SELECT id, path FROM images"):
            self.ids[row["path"]] = row["id"]
            title = "Picture " + row["path"].split(".")[0]
            fields = {"private_info": True} if row["path"] == "p05.jpg" else {}
            self.conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? "
                              "WHERE id = ?", (json.dumps(info(title, **fields)),
                                               describe.PROMPT_VER, row["id"]))
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.ids["p04.jpg"],))
        # The album: 20 pictures of the library (one hidden, one private) and
        # two of the other library, in an order of the owner's.
        chosen = ["p%02d.jpg" % number for number in range(19, -1, -1)] + ["o0.jpg", "o1.jpg"]
        self.album = albums.create_album(self.conn, ALBUM, ids=[self.ids[path]
                                                                for path in chosen])
        self.other_album = albums.create_album(self.conn, OTHER_ALBUM,
                                               ids=[self.ids["p01.jpg"], self.ids["p25.jpg"]])
        albums.create_album(self.conn, SMART_ALBUM, kind="smart", query=SMART_QUERY,
                            options={"lib": self.library_id,
                                     "filters": {"library": [self.library_id],
                                                 "favorite": ["yes"]}})
        albums.set_marks(self.conn, [self.ids["p01.jpg"], self.ids["p02.jpg"],
                                     self.ids["p25.jpg"]], favorite=True)
        albums.set_marks(self.conn, [self.ids["p03.jpg"]], rating=4)
        self.conn.commit()
        self.out = tempfile.mkdtemp(prefix="album-export-")
        self.addCleanup(support.remove_tree, self.out)

    def export(self, **options):
        target = os.path.join(self.out, "site-%d" % len(os.listdir(self.out)))
        summary = export.export_library(jobs.JobContext("test"), self.conn, self.config,
                                        self.library_id, target, options)
        return summary, target

    def all_text(self, folder):
        texts = []
        for root, _dirs, files in os.walk(folder):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    texts.append(handle.read())
        return b"\n".join(texts)

    def test_an_album_without_marks_publishes_only_its_pictures(self):
        summary, out = self.export(album=self.album["id"])
        self.assertEqual(summary["count"], 18)
        self.assertEqual(summary["total"], 20)
        self.assertEqual(summary["excluded"], {"hidden": 1, "private": 1})
        records = read_records(out)
        manifest = read_manifest(out)
        self.assertEqual(len(records), 18)
        titles = sorted(record["t"] for record in records)
        self.assertEqual(titles, sorted("Picture p%02d" % number for number in range(20)
                                        if number not in (4, 5)))
        for record in records:
            for key in ("fav", "rt", "al"):
                self.assertNotIn(key, record)
        self.assertNotIn("albums", manifest)
        self.assertNotIn("marks", manifest["options"])
        self.assertNotIn("marks", manifest["capabilities"])
        text = self.all_text(out)
        for secret in (ALBUM, OTHER_ALBUM, SMART_ALBUM, SMART_QUERY, '"fav"', '"rt"', '"al"'):
            self.assertNotIn(secret.encode("utf-8"), text, secret)
        self.assertEqual(summary["album"], ALBUM)
        self.assertEqual(summary["albums"], 0)

    def test_marks_publish_favorites_ratings_and_album_names(self):
        summary, out = self.export(album=self.album["id"], marks=True)
        records = {record["t"]: record for record in read_records(out)}
        manifest = read_manifest(out)
        self.assertEqual(manifest["options"].get("marks"), True)
        self.assertTrue(manifest["capabilities"]["marks"])
        self.assertEqual((records["Picture p01"]["fav"], records["Picture p03"]["rt"]), (1, 4))
        self.assertNotIn("fav", records["Picture p00"])
        published = {album["name"]: album for album in manifest["albums"]}
        self.assertEqual(set(published), {ALBUM, OTHER_ALBUM, SMART_ALBUM})
        trip = published[ALBUM]
        # The album's exported pictures, in its order, by their export ids.
        order = ["Picture p%02d" % number for number in range(19, -1, -1)
                 if number not in (4, 5)]
        by_id = {record["i"]: title for title, record in records.items()}
        self.assertEqual([by_id[xid] for xid in trip["items"]], order)
        self.assertEqual([album["id"] for album in manifest["albums"]], [1, 2, 3])
        self.assertEqual(records["Picture p01"]["al"],
                         sorted([trip["id"], published[OTHER_ALBUM]["id"]]))
        # Only the exported picture of the other album; no library choice.
        self.assertEqual(len(published[OTHER_ALBUM]["items"]), 1)
        smart = published[SMART_ALBUM]
        self.assertEqual((smart["kind"], smart["query"]), ("smart", SMART_QUERY))
        self.assertEqual(smart["options"], {"filters": {"favorite": ["yes"]}})
        self.assertEqual(summary["albums"], 3)
        if NODE:
            expectations = {"count": 18, "albums": [ALBUM, OTHER_ALBUM, SMART_ALBUM],
                            "queries": [
                                {"query": "album:\"%s\"" % ALBUM, "with_albums": True,
                                 "ordered": order},
                                {"query": "is:favorite", "titles": ["Picture p01",
                                                                    "Picture p02"]},
                                {"query": "rating:>=3", "titles": ["Picture p03"]},
                                {"query": "album:\"%s\"" % OTHER_ALBUM, "with_albums": True,
                                 "titles": ["Picture p01"]}]}
            path = os.path.join(self.out, "expected.json")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(expectations, handle)
            result = subprocess.run([NODE, NODE_TESTS, out, path], cwd=support.REPO,
                                    capture_output=True, timeout=300)
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, "\n" + output)

    def test_the_whole_library_with_marks(self):
        summary, out = self.export(marks=True)
        manifest = read_manifest(out)
        self.assertEqual(summary["count"], 28)
        published = {album["name"]: album for album in manifest["albums"]}
        self.assertEqual(len(published[ALBUM]["items"]), 18)
        self.assertEqual(len(published[OTHER_ALBUM]["items"]), 2)
        self.assertIsNone(summary["album"])

    def test_an_album_that_cannot_be_used(self):
        smart = albums.find_album(self.conn, SMART_ALBUM)
        with self.assertRaises(export.ExportError) as caught:
            self.export(album=smart["id"])
        self.assertIn("smart album", str(caught.exception))
        with self.assertRaises(export.ExportError) as caught:
            self.export(album=987654)
        self.assertIn("no longer exists", str(caught.exception))
        for bad in ("Trip", True, 0, -3, 1.5):
            with self.assertRaises(export.OptionsError):
                export.validate_options({"album": bad})
        self.assertEqual(export.validate_options({"album": None, "marks": True})["marks"], True)
        # The review step counts the album's pictures only.
        preview = export.preview_export(self.conn, self.config, self.library_id,
                                        {"album": self.album["id"]}, reachable=True)
        self.assertEqual(preview["counts"]["album"], {"id": self.album["id"], "name": ALBUM,
                                                      "pictures": 20, "elsewhere": 2})
        self.assertEqual((preview["counts"]["total"], preview["counts"]["exported"]), (20, 18))

    def test_a_deleted_album_never_becomes_another_one(self):
        # The export dialog saves the album's id: once that album is deleted,
        # an album made later must not take its id (and be published).
        doomed = albums.create_album(self.conn, "To publish", ids=[self.ids["p10.jpg"]])
        albums.delete_album(self.conn, doomed["id"])
        private = albums.create_album(self.conn, "Private", ids=[self.ids["p11.jpg"]])
        self.conn.commit()
        self.assertNotEqual(private["id"], doomed["id"])
        with self.assertRaises(export.ExportError) as caught:
            self.export(album=doomed["id"])
        self.assertIn("no longer exists", str(caught.exception))

    def test_smart_albums_go_out_only_when_their_search_may(self):
        other = self.config["libraries"][1]["id"]
        hidden = self.ids["p04.jpg"]

        def smart(name, query="", **options):
            albums.create_album(self.conn, name, kind="smart", query=query, options=options)

        smart("Smart elsewhere", "tree", lib=other)
        smart("Smart filter elsewhere", "tree", filters={"library": [other]})
        smart("Smart folders", 'folder:"Private/Medical" tree')
        smart("Smart files", "-file:IMG_1234")
        smart("Smart places", "place:Springfield")
        smart("Smart place filter", "tree", filters={"place": ["Springfield"]})
        smart("Smart gps", "has:location")
        smart("Smart lib", "lib:Other")
        smart("Like seven", "similar:%d" % self.ids["p07.jpg"])
        smart("Like hidden", "look:%d" % hidden)
        smart("Of albums", 'album:"%s" OR album:#%d' % (OTHER_ALBUM, self.album["id"]))
        smart("Of nothing", 'album:"Nothing here"')
        smart("Of folders", 'album:"Smart folders"')
        self.conn.commit()
        summary, out = self.export(marks=True)
        manifest = read_manifest(out)
        published = {album["name"]: album for album in manifest["albums"]}
        self.assertEqual(set(published), {ALBUM, OTHER_ALBUM, SMART_ALBUM, "Like seven",
                                          "Of albums"})
        self.assertEqual(summary["albums"], 5)
        # The picture by its export id; the albums by their numbers there.
        xid = [record["i"] for record in read_records(out) if record["t"] == "Picture p07"][0]
        self.assertEqual(published["Like seven"]["query"], "similar:" + xid)
        self.assertEqual(published["Of albums"]["query"], 'album:"#%d" OR album:#%d' % (
            published[OTHER_ALBUM]["id"], published[ALBUM]["id"]))
        text = self.all_text(out)
        for secret in ("Private/Medical", "IMG_1234", "Springfield", "lib:Other",
                       "Nothing here", "Smart elsewhere", "Smart folders", "has:location"):
            self.assertNotIn(secret.encode("utf-8"), text, secret)
        # (No database id in a published search; the pages' own help has
        # examples such as look:1234, so only the albums are looked at.)
        queries = " ".join(album.get("query", "") for album in manifest["albums"])
        for secret in ("similar:%d" % self.ids["p07.jpg"], "look:%d" % hidden):
            self.assertNotRegex(queries, secret + r"\b")
        if NODE:
            # The site finds the pictures of both albums the smart one names.
            titles = sorted(set("Picture p%02d" % number for number in range(20)
                                if number not in (4, 5)) | {"Picture p25"})
            path = os.path.join(self.out, "expected-smart.json")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"count": 28, "queries": [{"query": 'album:"Of albums"',
                                                     "with_albums": True,
                                                     "titles": titles}]}, handle)
            result = subprocess.run([NODE, NODE_TESTS, out, path], cwd=support.REPO,
                                    capture_output=True, timeout=300)
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, "\n" + output)
        # With the folders, file names and location published, so are those
        # searches; never one of another library, by library name, or like
        # a picture that is not published.
        _summary, out = self.export(marks=True, folders=True, file_names=True, location=True)
        names = {album["name"] for album in read_manifest(out)["albums"]}
        self.assertTrue({"Smart folders", "Smart files", "Smart places", "Smart place filter",
                         "Smart gps", "Of folders"} <= names, names)
        self.assertFalse({"Smart elsewhere", "Smart filter elsewhere", "Smart lib",
                          "Like hidden", "Of nothing"} & names, names)

    def test_the_command_line_takes_an_album_name(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            with self.assertRaises(SystemExit) as caught:
                export.main(["--library", "Family", "--album", "no such album",
                             "--out", os.path.join(self.out, "cli")])
            self.assertIn("no album", str(caught.exception))
            summary = export.main(["--library", "Family", "--album", ALBUM.upper(), "--marks",
                                   "--out", os.path.join(self.out, "cli")])
        self.assertIn("Limited to the album " + ALBUM, printed.getvalue())
        self.assertEqual((summary["count"], summary["album"], summary["albums"]),
                         (18, ALBUM, 3))


if __name__ == "__main__":
    unittest.main()
