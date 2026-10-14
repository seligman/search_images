"""Tests for the faces of wide panoramas and very large pictures, looked at in full size
(people.py: full_size, recipes, the faces' pictures kept in data/faces.db, version 3;
the faces the owner placed kept, originals that hang; who may see the kept pictures,
and that deleting the face data takes them too).

The helper is tests/fake_people_worker.py (see test_people): it "finds"
faces drawn as solid squares, in tiles too. A panorama 6400 x 1600 holds
a large face, which its 2048 x 512 preview shows, and small ones, which
only its full size shows. Every test runs in a temporary SETTINGS_BASE.
"""

import hashlib
import io
import os
import sqlite3
import threading
import time
import unittest
from unittest import mock

import support
import test_people
from test_people import Context, picture

import people
import people_groups

PANO_SIZE = (6400, 1600)
PREVIEW_SIZE = (2048, 512)
# (person, x, y, side) in the panorama's own pixels: a face the preview shows (128
# pixels there) and two it does not (8 and 10 pixels there).
LARGE = (5, 3000, 600, 400)
SMALL = [(6, 1000, 800, 24), (7, 5000, 300, 30)]


def legacy_recipe(options, digests):
    """The recipe of Search Images before pictures were looked at in full size: the
    pictures it looked at must stay up to date."""
    text = "%s\n%s\nfind-%d edge-%d/%d score-%g size-%d faces-%d" % (
        digests[0], digests[1], 1, options["max_edge"], 256, options["min_score"],
        options["min_size"], options["max_faces"])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def row(width, height, media="image", projection=""):
    return {"width": width, "height": height, "media": media, "projection": projection}


class RulesTest(unittest.TestCase):

    def test_which_pictures_are_looked_at_in_full_size(self):
        options = people.settings({})
        self.assertEqual((options["full_size"], options["full_megapixels"]), (True, 60))
        full = people.full_size
        self.assertTrue(full(row(16000, 3800), options))           # a wide panorama
        self.assertTrue(full(row(1600, 5000), options))            # ... upright
        self.assertTrue(full(row(4600, 2090), options))            # (2.2 wide)
        self.assertFalse(full(row(4500, 2090), options))           # (not quite)
        self.assertTrue(full(row(9600, 7200), options))            # 69 million pixels
        self.assertFalse(full(row(8256, 5504), options))           # 45: an ordinary photo
        # A wide picture no larger than its preview is looked at as before.
        self.assertFalse(full(row(2048, 900), options))
        self.assertTrue(full(row(2100, 900), options))
        self.assertFalse(full(row(2100, 900), dict(options, max_edge=3072)))
        # 360-degree pictures and videos are looked at as before (their previews).
        self.assertFalse(full(row(11904, 5952, projection="equirectangular"), options))
        self.assertFalse(full(row(16000, 3800, media="video"), options))
        self.assertFalse(full(row(0, 0), options))
        self.assertFalse(full({"media": "image", "projection": ""}, options))
        # The settings.
        self.assertFalse(full(row(16000, 3800), people.settings({"people": {
            "full_size": False}})))
        options = people.settings({"people": {"full_megapixels": 40}})
        self.assertTrue(full(row(8256, 5504), options))
        self.assertEqual(people.settings({"people": {"full_megapixels": 1}})["full_megapixels"],
                         60)
        self.assertEqual(people.recipe_kind(row(16000, 3800), people.settings({})),
                         people.RECIPE_FULL)
        self.assertEqual(people.recipe_kind(row(4000, 3000), people.settings({})),
                         people.KIND_PHOTO)

    @unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
    def test_wide_is_what_the_viewer_calls_wide(self):
        import images
        self.assertEqual(people.WIDE_ASPECT, images.WIDE_ASPECT)


class FullSizeFixture(test_people.PeopleFixture):

    def setUp(self):
        super().setUp()
        from PIL import Image
        original = Image.open(io.BytesIO(picture([LARGE] + SMALL, size=PANO_SIZE)))
        preview = io.BytesIO()
        original.resize(PREVIEW_SIZE, Image.NEAREST).save(preview, "PNG")
        with open(os.path.join(self.family, "wide.png"), "wb") as handle:
            handle.write(picture([LARGE] + SMALL, size=PANO_SIZE))
        self.wide = self.add("lib_family1", "wide.png", preview.getvalue(),
                             width=PANO_SIZE[0], height=PANO_SIZE[1])
        self.key = ("hash-wide.png", people.KIND_PHOTO)

    def faces_of_wide(self):
        """{person (the square's colour): (face id, x, w)} of the panorama's faces."""
        return {face[4]: (face[5], face[0], face[2])
                for face in self.stored().get(self.key, [])}

    def crops(self):
        store = people.connect(create=False)
        try:
            return {face_id: bytes(jpeg) for face_id, jpeg in store.execute(
                "SELECT face_id, jpeg FROM face_crops")}
        finally:
            store.close()


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class FullSizeTest(FullSizeFixture, unittest.TestCase):

    def test_the_recipes_of_other_pictures_do_not_change(self):
        options = people.settings(self.config)
        digests = people._model_digests(options)
        made = people.recipes(options)
        self.assertEqual(made[people.KIND_PHOTO], legacy_recipe(options, digests))
        self.assertEqual(made[people.KIND_PHOTO], people.recipe(options))
        self.assertEqual({made[people.KIND_POSTER], made[people.KIND_360]},
                         {made[people.KIND_PHOTO]})
        self.assertNotEqual(made[people.RECIPE_FULL], made[people.KIND_PHOTO])
        # Another way of looking at full size changes that recipe only; max_edge (which
        # the full size does not use) only the others.
        with mock.patch.object(people, "FULL_VERSION", 2):
            other = people.recipes(options)
        self.assertEqual(other[people.KIND_PHOTO], made[people.KIND_PHOTO])
        self.assertNotEqual(other[people.RECIPE_FULL], made[people.RECIPE_FULL])
        other = people.recipes(dict(options, max_edge=1024))
        self.assertEqual(other[people.RECIPE_FULL], made[people.RECIPE_FULL])
        self.assertNotEqual(other[people.KIND_PHOTO], made[people.KIND_PHOTO])
        other = people.recipes(dict(options, min_size=30))
        self.assertNotEqual(other[people.RECIPE_FULL], made[people.RECIPE_FULL])

    def test_only_the_wide_picture_is_looked_at_again(self):
        # Before: every picture looked at in its preview (as this program used to).
        self.configure({"full_size": False})
        summary = self.update()
        self.assertEqual((summary["looked"], summary["full"]), (6, 0))
        before = self.faces_of_wide()
        self.assertEqual(sorted(before), [5])               # (the small faces are missed)
        store = people.connect()
        try:
            people_groups.move_faces(store, [before[5][0]], name="Pat")
        finally:
            store.close()
        others = {key: value for key, value in self.stored().items() if key != self.key}
        # Now: only the panorama waits, to be looked at in full size.
        self.configure()
        counted = people.counts(self.conn, self.config, ["lib_family1", "lib_trips01"])
        self.assertEqual((counted["lib_family1"]["pending"], counted["lib_family1"]["changed"],
                          counted["lib_family1"]["full_pending"], counted["lib_family1"]["full"]),
                         (1, 1, 1, 0))
        self.assertEqual(counted["lib_trips01"]["pending"], 0)
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["pending"], summary["looked"], summary["full"],
                          summary["up_to_date"], summary["failed"]), (1, 1, 1, 6, 0))
        self.assertIn("1 of them (wide panoramas and very large pictures) are looked at in "
                      "full size", ctx.text())
        self.assertIn("Looked for faces in 1 picture(s) (1 in full size)", ctx.text())
        after = self.faces_of_wide()
        self.assertEqual(sorted(after), [5, 6, 7])
        # The face found before keeps its id, and so its person and name.
        self.assertEqual(after[5][0], before[5][0])
        self.assertAlmostEqual(after[5][1], LARGE[1] / float(PANO_SIZE[0]), places=2)
        self.assertTrue(18 <= after[6][2] * PANO_SIZE[0] <= 32, after[6])
        store = people.connect(create=False)
        try:
            named = store.execute("SELECT p.name, fp.owner FROM face_people fp JOIN people p "
                                  "ON p.id = fp.person_id WHERE fp.face_id = ?",
                                  (after[5][0],)).fetchone()
        finally:
            store.close()
        self.assertEqual(tuple(named), ("Pat", 1))
        # The other pictures were not looked at again: their faces are as they were.
        self.assertEqual({key: value for key, value in self.stored().items()
                          if key != self.key}, others)
        # A picture of each of its faces is kept (the preview is too small for them).
        crops = self.crops()
        self.assertEqual(sorted(crops), sorted(face_id for face_id, _x, _w in after.values()))
        self.assertTrue(all(data.startswith(b"\xff\xd8\xff") for data in crops.values()))
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(crops[after[6][0]])).size,
                         (people_groups.CROP_EDGE, people_groups.CROP_EDGE))
        counted = people.counts(self.conn, self.config, ["lib_family1"])["lib_family1"]
        self.assertEqual((counted["pending"], counted["full"], counted["full_pending"]),
                         (0, 1, 0))
        self.assertEqual(self.update()["pending"], 0)

    def test_without_full_size_the_preview_is_looked_at_again(self):
        self.update()
        self.assertEqual(sorted(self.faces_of_wide()), [5, 6, 7])
        self.assertEqual(len(self.crops()), 3)
        self.configure({"full_size": False})
        summary = self.update()
        self.assertEqual((summary["looked"], summary["full"]), (1, 0))
        self.assertEqual(sorted(self.faces_of_wide()), [5])
        # (The faces found again in the preview have no picture of their own any more.)
        self.assertEqual(self.crops(), {})

    def test_an_offline_library_keeps_the_faces_found_before(self):
        self.configure({"full_size": False})
        self.update()
        before = self.faces_of_wide()
        self.configure(libraries=[support.library("Family", self.base.join("gone"),
                                                  id="lib_family1"),
                                  support.library("Trips", self.trips, id="lib_trips01")])
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["offline"], summary["looked"], summary["failed"]), (1, 0, 0))
        self.assertIn("looked at in full size, or videos) wait for a later run", ctx.text())
        self.assertEqual(self.faces_of_wide(), before)

    def test_an_original_that_cannot_be_read(self):
        self.configure({"full_size": False})
        self.update()
        before = self.faces_of_wide()
        self.configure()
        os.remove(os.path.join(self.family, "wide.png"))
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["looked"], summary["failed"]), (0, 1))
        self.assertIn("Picture %d: no faces looked for" % self.wide, ctx.text())
        # (Its faces stay until it can be looked at.)
        self.assertEqual(self.faces_of_wide(), before)

    def test_a_face_the_owner_placed_stays_when_the_full_size_does_not_show_it(self):
        # Two faces found in the preview: the owner puts one in a person.
        from PIL import Image
        two = (3, 4000, 700, 400)
        preview = io.BytesIO()
        Image.open(io.BytesIO(picture([LARGE, two], size=PANO_SIZE))).resize(
            PREVIEW_SIZE, Image.NEAREST).save(preview, "PNG")
        self.previews.write("lib_family1", self.wide, preview.getvalue())
        self.configure({"full_size": False})
        self.update()
        before = self.faces_of_wide()
        self.assertEqual(sorted(before), [3, 5])
        store = people.connect()
        try:
            people_groups.move_faces(store, [before[5][0]], name="Pat")
        finally:
            store.close()
        # The original in full size shows neither (a picture edited since, say).
        with open(os.path.join(self.family, "wide.png"), "wb") as handle:
            handle.write(picture(SMALL, size=PANO_SIZE))
        self.configure()
        self.assertEqual(self.update()["full"], 1)
        after = self.faces_of_wide()
        self.assertEqual(after[5][0], before[5][0])         # (the owner's stays)
        self.assertEqual(sorted(after), [5, 6, 7])          # (the other went)

    def test_reading_an_original_that_hangs_gives_up(self):
        import images
        release = threading.Event()
        self.addCleanup(release.set)
        started = time.monotonic()
        with self.assertRaises(images.SourceReadError):
            people.read_limited(lambda: release.wait(20) and b"late", 0.3)
        with self.assertRaises(people.Stopped):
            people.read_limited(lambda: release.wait(20), 30, stop=lambda: True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(people.read_limited(lambda: b"bytes", 5), b"bytes")

        def broken():
            raise OSError("gone")
        with self.assertRaises(OSError):
            people.read_limited(broken, 5)
        # In a run: the panorama's original does not come while its folder goes away. It
        # waits for its folder (no failed try), keeping its faces.
        self.configure({"full_size": False})
        self.update()
        before = self.faces_of_wide()
        self.configure()
        import library_ops
        answers = iter([{"reachable": True}])

        def folder_status(root, timeout=None):
            return next(answers, {"reachable": False})

        def hangs(path, helper=None):
            release.wait(20)
            return b""
        ctx = Context()
        with mock.patch.object(people, "READ_TIMEOUT", 0.3), \
                mock.patch.object(library_ops, "folder_status", folder_status), \
                mock.patch.object(images, "read_source_bytes", hangs):
            summary = self.update(ctx)
        self.assertEqual((summary["offline"], summary["failed"], summary["looked"]), (1, 0, 0))
        self.assertIn("cannot be reached any more", ctx.text())
        self.assertEqual(self.faces_of_wide(), before)

    def test_a_large_picture_is_made_small_enough_for_the_helper(self):
        options = people.settings(self.config)
        row = self.conn.execute("SELECT * FROM images WHERE id = ?", (self.wide,)).fetchone()
        import cache
        data = people.picture_bytes(row, options, cache.PreviewStore(), root=self.family,
                                    full=True)
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(data)).size, PANO_SIZE)
        self.assertEqual(Image.open(io.BytesIO(people.picture_bytes(
            row, options, cache.PreviewStore(), root=self.family))).size, PREVIEW_SIZE)
        with mock.patch.object(people, "FULL_MAX_PIXELS", 1600000):
            data = people.picture_bytes(row, options, cache.PreviewStore(), root=self.family,
                                        full=True)
        self.assertLessEqual(Image.open(io.BytesIO(data)).size[0] *
                             Image.open(io.BytesIO(data)).size[1], 1600000)
        # (Its base64 must fit in a line of the helper's input.)
        data = people.picture_bytes(row, options, cache.PreviewStore(), root=self.family,
                                    full=True)
        with mock.patch.object(people.people_worker, "MAX_LINE", len(data)):
            smaller = people.picture_bytes(row, options, cache.PreviewStore(),
                                           root=self.family, full=True)
        self.assertLessEqual(len(smaller), len(data) * 0.7 * 3 / 4)
        self.assertLess(Image.open(io.BytesIO(smaller)).size[0], PANO_SIZE[0])

    def test_the_command_line_says_so(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(people.main(["--status"], out=out), 0)
        self.assertIn("Family: 0 of 4 picture(s) looked at, 0 face(s) in 0; 4 to look at (1 "
                      "in full size)", out.getvalue())
        # In all, as a run looks at them: copy.png of Trips is two.png of Family (the
        # same content), looked at once.
        totals = {}
        listed = people.counts(self.conn, self.config, ["lib_family1", "lib_trips01"],
                               totals=totals)
        self.assertEqual(totals["pending"],
                         sum(entry["pending"] for entry in listed.values()) - 1)
        self.assertIn("  In all: %d picture(s) to look at (1 in full size" % totals["pending"],
                      out.getvalue())
        summary = self.update()
        self.assertEqual(summary["looked"] + summary["no_tools"], totals["pending"])
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(people.main(["--status"], out=out), 0)
        self.assertIn("Family: 4 of 4 picture(s) looked at (1 in full size)", out.getvalue())
        self.assertNotIn("In all", out.getvalue())


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class UpgradeTest(FullSizeFixture, unittest.TestCase):
    """A data/faces.db of version 2 (before the faces' pictures) is brought up to date,
    keeping the faces and all that was said about the people."""

    def test_names_merges_moves_and_marks_stay(self):
        self.configure({"full_size": False, "min_faces": 1})
        for index in range(3):
            self.add("lib_trips01", "pat%d.png" % index,
                     picture([(5, 30 + 40 * index, 40, 80), (2, 250, 100, 70)]))
        self.update()
        store = people.connect()
        try:
            faces = {row[0]: row[1] for row in store.execute(
                "SELECT f.id, fp.person_id FROM faces f JOIN face_people fp ON fp.face_id = "
                "f.id WHERE f.content_hash = 'hash-wide.png'")}
            large = self.faces_of_wide()[5][0]
            pat = faces[large]
            people_groups.update_person(store, pat, {"name": "Pat"})
            # (Another person merged into Pat, a face moved, one "not this person", one
            # "not a face".)
            two = store.execute("SELECT person_id FROM face_people fp JOIN faces f ON f.id = "
                                "fp.face_id WHERE f.content_hash = 'hash-pat0.png' AND "
                                "person_id != ?", (pat,)).fetchone()[0]
            people_groups.update_person(store, two, {"name": "Lee", "hidden": True})
            one = store.execute("SELECT person_id FROM face_people fp JOIN faces f ON f.id = "
                                "fp.face_id WHERE f.content_hash = 'hash-clip.mp4'").fetchone()[0]
            people_groups.merge(store, one, two)
            moved = store.execute("SELECT id FROM faces WHERE content_hash = 'hash-two.png' "
                                  "ORDER BY id").fetchone()[0]
            people_groups.move_faces(store, [moved], pat)
            rejected = store.execute("SELECT f.id FROM faces f JOIN face_people fp ON "
                                     "fp.face_id = f.id WHERE fp.person_id = ? AND "
                                     "f.content_hash = 'hash-pat2.png'", (pat,)).fetchone()[0]
            people_groups.reject_faces(store, [rejected], pat)
            marked = store.execute("SELECT id FROM faces WHERE content_hash = 'hash-hidden.png'"
                                   ).fetchone()[0]
            people_groups.mark_not_face(store, [marked])
            lee = store.execute("SELECT COUNT(*) FROM face_people WHERE person_id = ?",
                                (two,)).fetchone()[0]
            snapshot = {table: sorted(tuple(row) for row in store.execute(
                "SELECT * FROM %s" % table)) for table in ("faces", "people", "face_people",
                                                          "face_rejects", "face_marks")}
        finally:
            store.close()
        # As Milestone 6 left it: version 2, without the faces' pictures.
        conn = sqlite3.connect(people.store_path())
        conn.execute("DROP TABLE face_crops")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'version'")
        conn.commit()
        conn.close()
        # Reading it brings it up to date, keeping it all.
        store = people.connect(create=False)
        try:
            self.assertEqual(people.meta(store)["version"], people.FACES_VERSION)
            self.assertEqual({table: sorted(tuple(row) for row in store.execute(
                "SELECT * FROM %s" % table)) for table in snapshot}, snapshot)
            self.assertEqual(store.execute("SELECT COUNT(*) FROM face_crops").fetchone()[0], 0)
        finally:
            store.close()
        # Then the panorama is looked at in full size: what was said stays.
        self.configure({"min_faces": 1})
        summary = self.update()
        self.assertEqual((summary["looked"], summary["full"]), (1, 1))
        store = people.connect(create=False)
        try:
            names = {row[0]: (row[1], row[2]) for row in store.execute(
                "SELECT name, hidden, (SELECT COUNT(*) FROM face_people WHERE person_id = "
                "people.id) FROM people WHERE name != ''")}
            self.assertEqual(names["Lee"], (1, lee))
            self.assertEqual(store.execute("SELECT p.name FROM face_people fp JOIN people p ON "
                                           "p.id = fp.person_id WHERE fp.face_id = ?",
                                           (large,)).fetchone()[0], "Pat")
            self.assertEqual(store.execute("SELECT person_id, owner FROM face_people WHERE "
                                           "face_id = ?", (moved,)).fetchone()[:], (pat, 1))
            self.assertIsNotNone(store.execute("SELECT 1 FROM face_rejects WHERE face_id = ? "
                                               "AND person_id = ?", (rejected, pat)).fetchone())
            self.assertIsNotNone(store.execute("SELECT 1 FROM face_marks WHERE face_id = ?",
                                               (marked,)).fetchone())
        finally:
            store.close()


@unittest.skipUnless(support.pillow_available() and test_people.flask is not None,
                     "Pillow and Flask are needed")
class FacePictureTest(FullSizeFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import serve
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def test_a_face_found_in_full_size_shows_its_own_picture(self):
        self.update()
        small = self.faces_of_wide()[6][0]
        kept = self.crops()[small]
        response = self.client.get("/face/%d" % small)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, kept)
        self.assertIn("no-cache", response.headers["Cache-Control"])
        again = self.client.get("/face/%d" % small,
                                headers={"If-None-Match": response.headers["ETag"]})
        self.assertEqual(again.status_code, 304)
        # A face of another picture is still cut from its preview.
        other = self.stored()[("hash-two.png", "photo")][0][5]
        self.assertNotIn(other, self.crops())
        response = self.client.get("/face/%d" % other)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\xff\xd8\xff"))
        # Looked at again (another recipe): a new picture, a new ETag.
        first = self.client.get("/face/%d" % small).headers["ETag"]
        store = people.connect()
        try:
            store.execute("UPDATE face_crops SET updated = updated + 1")
            store.commit()
        finally:
            store.close()
        self.assertNotEqual(self.client.get("/face/%d" % small).headers["ETag"], first)

    def test_visitors_see_the_kept_pictures_of_named_people_only(self):
        self.update()
        faces = self.faces_of_wide()
        named, unnamed = faces[6][0], faces[7][0]
        crops = self.crops()
        store = people.connect()
        try:
            person = people_groups.move_faces(store, [named], name="Ada")["id"]
        finally:
            store.close()
        visitor = self.app.test_client()
        visitor.environ_base["REMOTE_ADDR"] = "192.0.2.20"
        # (Without "visitors": none.)
        self.assertEqual(visitor.get("/face/%d" % named).status_code, 404)
        self.configure({"visitors": True})
        response = visitor.get("/face/%d" % named)
        self.assertEqual((response.status_code, response.data), (200, crops[named]))
        self.assertIn(unnamed, crops)
        self.assertEqual(visitor.get("/face/%d" % unnamed).status_code, 404)
        self.assertEqual(self.client.get("/face/%d" % unnamed).status_code, 200)
        # A hidden picture, or a hidden person: none.
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.wide,))
        self.conn.commit()
        self.assertEqual(visitor.get("/face/%d" % named).status_code, 404)
        self.conn.execute("UPDATE images SET hidden = 0 WHERE id = ?", (self.wide,))
        self.conn.commit()
        self.assertEqual(visitor.get("/face/%d" % named).status_code, 200)
        store = people.connect()
        try:
            people_groups.update_person(store, person, {"hidden": True})
        finally:
            store.close()
        self.assertEqual(visitor.get("/face/%d" % named).status_code, 404)
        self.assertEqual(self.client.get("/face/%d" % named).status_code, 200)

    def test_deleting_all_face_data_leaves_no_face_pictures(self):
        self.update()
        crops = list(self.crops().values())
        self.assertTrue(crops)
        path = people.store_path()
        # (Even while another program reads the file: it is emptied and wiped.)
        reader = sqlite3.connect(path)
        try:
            reader.execute("SELECT COUNT(*) FROM face_crops").fetchone()
            result = people.delete_all()
        finally:
            reader.close()
        self.assertEqual(result["error"], "")
        for name in (path, path + "-wal", path + "-journal"):
            if os.path.isfile(name):
                with open(name, "rb") as handle:
                    data = handle.read()
                self.assertFalse(any(crop[40:120] in data for crop in crops), name)
        if os.path.isfile(path):
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_crops").fetchone()[0],
                                 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
