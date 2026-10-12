"""Tests for image_edits.py: the owner's own titles, descriptions and tags.

Rows are written straight into a temporary database. The routes that
change edits are tested in test_admin_images, the search records in
test_search_index.
"""

import json
import sqlite3
import unittest

import support

import common
import db
import image_edits
from test_search_index import NOW, insert_image

INFO = {"title": "Dog on a beach", "description": "A brown dog runs on the sand.",
        "tags": ["Dog", "beach", "sand"], "subjects": ["dog"]}


class HelpersTest(unittest.TestCase):

    def test_clean_tags(self):
        self.assertEqual(image_edits.clean_tags([" Rex ", "rex", "Grandma's  garden", "",
                                                 "REX", "beach"]),
                         ["rex", "grandma's garden", "beach"])
        self.assertEqual(image_edits.clean_tags(None), [])
        for bad in ("rex", [1], [None], ["x" * (image_edits.TAG_LENGTH + 1)],
                    ["t%d" % number for number in range(image_edits.TAGS_LIMIT + 1)],
                    ["x" + chr(0xDC00)]):
            with self.assertRaises(image_edits.EditError):
                image_edits.clean_tags(bad)

    def test_effective_tags_put_the_owners_first(self):
        edit = {"tags_add": ["rex", "beach"], "tags_remove": ["sand", "DOG"]}
        self.assertEqual(image_edits.effective_tags(INFO["tags"], edit), ["rex", "beach"])
        self.assertEqual(image_edits.effective_tags(INFO["tags"], {"tags_add": ["sea"]}),
                         ["sea", "Dog", "beach", "sand"])
        self.assertEqual(image_edits.effective_tags(None, {"tags_add": ["rex"]}), ["rex"])
        self.assertEqual(image_edits.effective_tags(["a", 3, " "], None), ["a"])

    def test_apply_lays_the_edit_over_the_description(self):
        edit = {"title": "Rex at the beach", "description": None, "tags_add": ["rex"],
                "tags_remove": ["sand"]}
        shown = image_edits.apply(INFO, edit)
        self.assertEqual(shown["title"], "Rex at the beach")
        self.assertEqual(shown["description"], INFO["description"])
        self.assertEqual(shown["tags"], ["rex", "Dog", "beach"])
        self.assertEqual(shown["subjects"], ["dog"])
        # The model's description itself is not changed.
        self.assertEqual(INFO["title"], "Dog on a beach")
        self.assertIs(image_edits.apply(INFO, None), INFO)
        self.assertIs(image_edits.apply(INFO, image_edits.empty()), INFO)
        # A picture not described yet can still have the owner's words.
        self.assertEqual(image_edits.apply(None, {"title": "Rex", "tags_add": ["rex"]}),
                         {"title": "Rex", "tags": ["rex"]})

    def test_merge_changes_only_what_is_sent(self):
        current = {"title": "Rex", "description": "Our dog.", "tags_add": ["rex"],
                   "tags_remove": ["sand"], "updated": NOW}
        merged = image_edits.merge(current, {"description": "  Rex, asleep.  "})
        self.assertEqual(merged, {"title": "Rex", "description": "Rex, asleep.",
                                  "tags_add": ["rex"], "tags_remove": ["sand"]})
        # null (or "") puts the model's text back.
        merged = image_edits.merge(current, {"title": None, "description": ""})
        self.assertEqual((merged["title"], merged["description"]), (None, None))
        # A tag both added and removed is added.
        merged = image_edits.merge(None, {"tags_add": ["Sand"], "tags_remove": ["sand", "x"]})
        self.assertEqual((merged["tags_add"], merged["tags_remove"]), (["sand"], ["x"]))
        self.assertTrue(image_edits.is_empty(image_edits.merge(current, {
            "title": None, "description": None, "tags_add": None, "tags_remove": []})))
        for bad in ({"title": 5}, {"description": ["x"]}, {"color": "red"},
                    {"title": "x" * (image_edits.TITLE_LENGTH + 1)},
                    {"description": "x" * (image_edits.DESCRIPTION_LENGTH + 1)},
                    {"tags_add": "rex"},
                    # A lone surrogate (JSON can carry one) cannot be stored.
                    {"title": "a" + chr(0xD800) + "b"}, {"tags_remove": [chr(0xDFFF)]}):
            with self.assertRaises(image_edits.EditError) as caught:
                image_edits.merge(current, bad)
            self.assertTrue(caught.exception.fields, bad)
        with self.assertRaises(image_edits.EditError):
            image_edits.merge(current, ["title"])

    def test_view_has_every_field(self):
        self.assertEqual(image_edits.view(None), {"title": None, "description": None,
                                                  "tags_add": [], "tags_remove": [],
                                                  "updated": None})
        self.assertEqual(image_edits.view({"title": "Rex", "tags_add": ["rex"],
                                           "updated": NOW})["tags_add"], ["rex"])


class StorageTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", self.base.library_folder("F"),
                                                id="lib_family1"),
                                support.library("Other", self.base.library_folder("O"),
                                                id="lib_other001")])
        db.init(log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.dog = insert_image(self.conn, "lib_family1", "dog.jpg", described=1,
                                prompt_ver=5, info=INFO)
        self.plain = insert_image(self.conn, "lib_family1", "plain.jpg", described=1,
                                  prompt_ver=5, info={"title": "Plain", "tags": ["wall"]})
        self.fresh = insert_image(self.conn, "lib_family1", "new.jpg")
        self.gone = insert_image(self.conn, "lib_family1", "gone.jpg", missing_since=NOW,
                                 described=1, prompt_ver=5, info=INFO)
        self.other = insert_image(self.conn, "lib_other001", "cat.jpg")

    def updated(self, image_id):
        return self.conn.execute("SELECT updated FROM images WHERE id = ?",
                                 (image_id,)).fetchone()[0]

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM image_edits").fetchone()[0]

    def test_save_load_and_an_empty_edit_is_deleted(self):
        saved = image_edits.save(self.conn, self.dog, {"title": "Rex", "tags_add": ["Rex"]},
                                 now=NOW + 5)
        self.conn.commit()
        self.assertEqual(saved, {"title": "Rex", "description": None, "tags_add": ["rex"],
                                 "tags_remove": [], "updated": NOW + 5})
        self.assertEqual(image_edits.load(self.conn, self.dog), saved)
        # The picture's row is touched, so the search index notices.
        self.assertEqual(self.updated(self.dog), NOW + 5)
        self.assertIsNone(image_edits.load(self.conn, self.plain))
        row = self.conn.execute("SELECT * FROM image_edits").fetchone()
        self.assertEqual((row["tags_add"], row["tags_remove"]), ('["rex"]', "[]"))
        saved = image_edits.save(self.conn, self.dog, {"description": "Our dog."}, now=NOW + 6)
        self.assertEqual((saved["title"], saved["description"]), ("Rex", "Our dog."))
        self.assertIsNone(image_edits.save(self.conn, self.dog, {
            "title": None, "description": "", "tags_add": []}, now=NOW + 7))
        self.conn.commit()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.updated(self.dog), NOW + 7)

    def test_an_edit_goes_with_its_picture(self):
        image_edits.save(self.conn, self.dog, {"title": "Rex"})
        image_edits.save(self.conn, self.plain, {"title": "Wall"})
        self.conn.commit()
        self.conn.execute("DELETE FROM images WHERE id = ?", (self.dog,))
        self.conn.commit()
        self.assertEqual(list(image_edits.load_many(self.conn, [self.dog, self.plain])),
                         [self.plain])
        # Only pictures that exist can have one.
        with self.assertRaises(sqlite3.IntegrityError):
            image_edits.save(self.conn, 999999, {"title": "Nobody"})
        self.conn.rollback()

    def test_edits_of_the_present_pictures_of_some_libraries(self):
        for image_id in (self.dog, self.gone, self.other):
            image_edits.save(self.conn, image_id, {"tags_add": ["x"]})
        self.conn.commit()
        self.assertEqual(sorted(image_edits.for_libraries(self.conn, ["lib_family1"])),
                         [self.dog])
        self.assertEqual(sorted(image_edits.for_libraries(
            self.conn, ["lib_family1", "lib_other001"])), [self.dog, self.other])
        self.assertEqual(image_edits.for_libraries(self.conn, []), {})

    def test_change_tags_on_many_pictures(self):
        ids = [self.dog, self.plain, self.fresh, self.gone, 999999]
        result = image_edits.change_tags(self.conn, ids, add=["Summer Trip"], now=NOW + 1)
        self.conn.commit()
        self.assertEqual((result["found"], result["changed"]), (3, 3))
        self.assertEqual(result["tags"], {self.dog: ["summer trip", "Dog", "beach", "sand"],
                                          self.plain: ["summer trip", "wall"],
                                          self.fresh: ["summer trip"]})
        self.assertEqual(set(result["edited"].values()), {True})
        # Again: nothing to change.
        self.assertEqual(image_edits.change_tags(self.conn, ids, add=["summer trip"])["changed"],
                         0)
        # Removing: from the owner's tags, and the model's where it has them.
        result = image_edits.change_tags(self.conn, [self.dog, self.plain],
                                         remove=["summer trip", "beach"], now=NOW + 2)
        self.conn.commit()
        self.assertEqual(result["tags"], {self.dog: ["Dog", "sand"], self.plain: ["wall"]})
        self.assertEqual(result["edited"], {self.dog: True, self.plain: False})
        self.assertEqual(image_edits.load(self.conn, self.dog)["tags_remove"], ["beach"])
        # The plain picture never had "beach": its edit is empty, so gone.
        self.assertIsNone(image_edits.load(self.conn, self.plain))
        # Adding a tag the owner removed brings it back -- as the model's.
        result = image_edits.change_tags(self.conn, [self.dog], add=["beach"])
        self.assertEqual(result["tags"][self.dog], ["Dog", "beach", "sand"])
        self.assertIsNone(image_edits.load(self.conn, self.dog))
        # A tag the model gave already changes nothing.
        result = image_edits.change_tags(self.conn, [self.dog, self.plain], add=["dog"])
        self.assertEqual((result["changed"], list(result["tags"])), (1, [self.plain]))
        self.assertIsNone(image_edits.load(self.conn, self.dog))

    def test_saving_a_tag_the_model_gave_keeps_no_copy(self):
        saved = image_edits.save(self.conn, self.dog, {"tags_add": ["rex", "DOG"],
                                                       "tags_remove": ["sand"]})
        self.assertEqual((saved["tags_add"], saved["tags_remove"]), (["rex"], ["sand"]))
        # Typing a removed one again only puts it back.
        saved = image_edits.save(self.conn, self.dog, {"tags_add": ["rex", "sand"]})
        self.assertEqual((saved["tags_add"], saved["tags_remove"]), (["rex"], []))
        self.assertIsNone(image_edits.save(self.conn, self.dog, {"tags_add": ["beach"]}))

    def test_too_many_tags_on_one_picture(self):
        image_edits.save(self.conn, self.dog, {"tags_add": [
            "t%d" % number for number in range(image_edits.TAGS_LIMIT)]})
        self.conn.commit()
        with self.assertRaises(image_edits.EditError):
            image_edits.change_tags(self.conn, [self.dog], add=["one more"])
        self.conn.rollback()
        self.assertEqual(len(image_edits.load(self.conn, self.dog)["tags_add"]),
                         image_edits.TAGS_LIMIT)

    def title(self, image_id):
        edit = image_edits.load(self.conn, image_id)
        return edit["title"] if edit else None

    def test_give_to_the_new_row_of_a_moved_file(self):
        image_edits.save(self.conn, self.gone, {"title": "Moved"}, now=NOW + 1)
        image_edits.save(self.conn, self.plain, {"title": "Own"})
        # The first of the rows without an edit of its own takes it.
        self.assertEqual(image_edits.give(self.conn, self.gone, [self.plain, self.fresh],
                                          now=NOW + 9), self.fresh)
        self.assertEqual(image_edits.load(self.conn, self.fresh)["updated"], NOW + 1)
        self.assertIsNone(image_edits.load(self.conn, self.gone))
        self.assertEqual((self.updated(self.fresh), self.updated(self.gone)), (NOW + 9, NOW + 9))
        # A picture with an edit of its own keeps it; nothing changes.
        self.assertIsNone(image_edits.give(self.conn, self.fresh, [self.plain]))
        self.assertEqual((self.title(self.fresh), self.title(self.plain)), ("Moved", "Own"))
        self.assertIsNone(image_edits.give(self.conn, self.dog, [self.other]))
        # A copy: both have it.
        self.assertEqual(image_edits.give(self.conn, self.fresh, [self.other], copy=True),
                         self.other)
        self.assertEqual((self.title(self.fresh), self.title(self.other)), ("Moved", "Moved"))

    def test_reassign_swaps_and_never_overwrites(self):
        image_edits.save(self.conn, self.dog, {"title": "Dog"})
        image_edits.save(self.conn, self.plain, {"title": "Plain"})
        image_edits.save(self.conn, self.other, {"title": "Other"})
        # Two files that swapped names swap their edits.
        self.assertEqual(image_edits.reassign(self.conn, {self.dog: ([self.plain], False),
                                                          self.plain: ([self.dog], False)}),
                         {self.dog: self.plain, self.plain: self.dog})
        self.assertEqual((self.title(self.dog), self.title(self.plain)), ("Plain", "Dog"))
        # An edit whose target keeps its own (it has nowhere to go, or is
        # only copied) stays where it is -- and so does one meant for it.
        plan = {self.dog: ([self.plain], False), self.plain: ([self.other], False)}
        self.assertEqual(image_edits.reassign(self.conn, plan), {})
        plan = {self.dog: ([self.plain], False), self.plain: ([self.fresh], True)}
        self.assertEqual(image_edits.reassign(self.conn, plan), {self.plain: self.fresh})
        self.assertEqual([self.title(image_id) for image_id in
                          (self.dog, self.plain, self.fresh, self.other)],
                         ["Plain", "Dog", "Dog", "Other"])
        # Two edits never go to the same picture.
        image_edits.save(self.conn, self.fresh, {"title": None})
        plan = {self.dog: ([self.fresh], False), self.plain: ([self.fresh, self.gone], False)}
        self.assertEqual(image_edits.reassign(self.conn, plan),
                         {self.dog: self.fresh, self.plain: self.gone})
        self.assertEqual(self.count(), 3)


class MigrationToV3Test(support.TempBaseMixin, unittest.TestCase):
    """Schema 3 adds the image_edits table (with a backup of version 2)."""

    def test_version_2_database_is_migrated(self):
        self.base.write_config([support.library("Family", "C:\\Photos\\Family",
                                                id="lib_aaaa0001")])
        common.ensure_dir(common.data_dir())
        conn = sqlite3.connect(common.db_path())
        db._migrate_to_1(conn)
        db._migrate_to_2(conn)
        conn.execute("PRAGMA user_version=2")
        conn.execute("INSERT INTO images (id, library_id, path, name, size, mtime) "
                     "VALUES (7, 'lib_aaaa0001', 'a.jpg', 'a.jpg', 1, 1)")
        conn.commit()
        conn.close()
        log = []
        report = db.init(common.load_config(), log=log.append)
        self.assertEqual(report["from_version"], 2)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertGreaterEqual(db.user_version(conn), 3)
        self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(image_edits)")],
                         ["image_id", "title", "description", "tags_add", "tags_remove",
                          "updated"])
        image_edits.save(conn, 7, {"title": "Kept"})
        conn.commit()
        self.assertEqual(image_edits.load(conn, 7)["title"], "Kept")
        backup = sqlite3.connect(common.db_path() + ".bak-v2")
        try:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(backup.execute(
                "SELECT name FROM sqlite_master WHERE name = 'image_edits'").fetchone())
        finally:
            backup.close()
        self.assertTrue(any("Backed up" in line for line in log))


if __name__ == "__main__":
    unittest.main()
