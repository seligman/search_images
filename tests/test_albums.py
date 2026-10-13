"""Tests for albums.py: favorites, ratings and albums (index.db schema 6).

Rows are written straight into a temporary database for the module tests;
the scanner tests (marks following moved files) use real files, and the
routes are tested in test_admin_albums, the export in test_export_albums.
"""

import json
import os
import sqlite3
import unittest

import support

import albums
import common
import db
import image_edits
import search_index
from test_search_index import NOW, insert_image


class AlbumsFixture(support.TempBaseMixin):

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", self.base.library_folder("Family"),
                                                id="lib_family1"),
                                support.library("Other", self.base.library_folder("Other"),
                                                id="lib_other001")])
        db.init(log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.ids = [insert_image(self.conn, "lib_family1", "p%d.jpg" % number)
                    for number in range(6)]
        self.other = insert_image(self.conn, "lib_other001", "o.jpg")
        self.gone = insert_image(self.conn, "lib_family1", "gone.jpg", missing_since=NOW)

    def change(self, function, *args, **kwargs):
        with self.conn:
            return function(self.conn, *args, **kwargs)


class SchemaTest(support.TempBaseMixin, unittest.TestCase):

    def test_a_new_database_has_the_tables(self):
        self.base.write_config([])
        db.init(log=support.quiet)
        conn = db.connect()
        try:
            self.assertEqual(db.user_version(conn), db.SCHEMA_VERSION)
            self.assertGreaterEqual(db.SCHEMA_VERSION, 6)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
            self.assertTrue({"image_marks", "albums", "album_items",
                             "album_items_image"} <= tables)
        finally:
            conn.close()

    def test_version_5_is_upgraded_with_a_backup(self):
        self.base.write_config([support.library("Family", self.base.library_folder("Family"),
                                                id="lib_family1")])
        path = common.db_path()
        common.ensure_dir(os.path.dirname(path))
        conn = sqlite3.connect(path)
        for version, function, _rewrites in db.MIGRATIONS:
            if version <= 5:
                function(conn)
        conn.execute("PRAGMA user_version=5")
        conn.execute("INSERT INTO images (library_id, path, name, size, mtime) "
                     "VALUES ('lib_family1', 'a.jpg', 'a.jpg', 1, 1)")
        conn.commit()
        conn.close()
        messages = []
        report = db.init(log=messages.append)
        self.assertEqual((report["from_version"], report["version"]), (5, db.SCHEMA_VERSION))
        self.assertTrue(os.path.isfile(path + ".bak-v5"))
        self.assertTrue(any("bak-v5" in message for message in messages))
        conn = db.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)
            conn.execute("INSERT INTO image_marks (image_id, favorite, rating, updated) "
                         "VALUES (1, 1, 5, 0)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO image_marks (image_id, rating) VALUES (1, 6)")
        finally:
            conn.close()


class MarksTest(AlbumsFixture, unittest.TestCase):

    def test_set_marks_and_clear_them(self):
        result = self.change(albums.set_marks, self.ids[:2] + [self.gone, 999], favorite=True)
        self.assertEqual((result["found"], result["changed"]), (2, 2))
        self.assertEqual(result["marks"][self.ids[0]], {"favorite": True, "rating": 0})
        result = self.change(albums.set_marks, [self.ids[0]], rating=4)
        self.assertEqual(result["marks"][self.ids[0]], {"favorite": True, "rating": 4})
        # The same again changes nothing.
        self.assertEqual(self.change(albums.set_marks, [self.ids[0]], rating=4)["changed"], 0)
        self.assertEqual(albums.load_marks(self.conn, self.ids),
                         {self.ids[0]: {"favorite": True, "rating": 4},
                          self.ids[1]: {"favorite": True, "rating": 0}})
        # A picture left with neither loses its row.
        self.change(albums.set_marks, self.ids[:2], favorite=False)
        self.assertEqual(albums.load_marks(self.conn, self.ids),
                         {self.ids[0]: {"favorite": False, "rating": 4}})
        self.change(albums.set_marks, [self.ids[0]], rating=0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_marks").fetchone()[0], 0)
        for bad in ({"rating": 6}, {"rating": -1}, {"rating": True}, {"rating": 2.5},
                    {"rating": "4"}, {"favorite": 1}):
            with self.assertRaises(albums.AlbumError):
                albums.set_marks(self.conn, self.ids, **bad)

    def test_marks_do_not_touch_the_rows_but_change_the_state(self):
        before_rows = [tuple(row) for row in self.conn.execute(
            "SELECT id, updated FROM images ORDER BY id")]
        before = albums.data_state(self.conn, ["lib_family1", "lib_other001"])
        self.change(albums.set_marks, [self.ids[0]], favorite=True)
        after = albums.data_state(self.conn, ["lib_family1", "lib_other001"])
        self.assertNotEqual(before, after)
        self.assertIn("lib_family1", after["libraries"])
        self.assertNotIn("lib_other001", after["libraries"])
        self.change(albums.set_marks, [self.ids[0]], rating=2)
        self.assertNotEqual(after, albums.data_state(self.conn, ["lib_family1"]))
        self.assertEqual(before_rows, [tuple(row) for row in self.conn.execute(
            "SELECT id, updated FROM images ORDER BY id")])

    def test_marks_and_places_go_with_the_row(self):
        self.change(albums.set_marks, [self.ids[0]], favorite=True, rating=3)
        album = self.change(albums.create_album, "Trip", ids=[self.ids[0], self.ids[1]])
        self.change(albums.update_album, album["id"], {"cover": self.ids[0]})
        with self.conn:
            self.conn.execute("DELETE FROM images WHERE id = ?", (self.ids[0],))
        self.assertEqual(albums.load_marks(self.conn, [self.ids[0]]), {})
        self.assertEqual(albums.album_items(self.conn, album["id"]), [self.ids[1]])
        self.assertIsNone(albums.get_album(self.conn, album["id"])["cover"])

    def test_for_libraries(self):
        self.change(albums.set_marks, [self.ids[0], self.other], favorite=True)
        self.change(albums.set_marks, [self.ids[1]], rating=5)
        first = self.change(albums.create_album, "A", ids=[self.ids[1], self.ids[2]])
        second = self.change(albums.create_album, "B", ids=[self.ids[2]])
        marks = albums.for_libraries(self.conn, ["lib_family1"])
        self.assertEqual(marks[self.ids[0]], (1, 0, []))
        self.assertEqual(marks[self.ids[1]], (0, 5, [first["id"]]))
        self.assertEqual(marks[self.ids[2]], (0, 0, [first["id"], second["id"]]))
        self.assertNotIn(self.other, marks)
        self.assertEqual(albums.picture_marks(self.conn, self.ids[2]),
                         (0, 0, [first["id"], second["id"]]))


class AlbumTest(AlbumsFixture, unittest.TestCase):

    def test_names_are_unique_in_any_case(self):
        album = self.change(albums.create_album, "  Summer   trip ")
        self.assertEqual(album["name"], "Summer trip")
        self.assertEqual((album["kind"], album["count"]), ("manual", 0))
        for name in ("summer TRIP", "Summer trip"):
            with self.assertRaises(albums.AlbumError) as caught:
                self.change(albums.create_album, name)
            self.assertEqual(caught.exception.status, 409)
        # Any case also beyond ASCII.
        self.change(albums.create_album, "\u00dcber")
        with self.assertRaises(albums.AlbumError):
            self.change(albums.create_album, "\u00fcber")
        for bad in ("", "   ", None, 5, "x" * (albums.NAME_LENGTH + 1), "a\x07b",
                    "a" + chr(0xD800)):
            with self.assertRaises(albums.AlbumError):
                albums.clean_name(bad)
        self.assertEqual(albums.find_album(self.conn, "SUMMER TRIP")["id"], album["id"])
        self.assertEqual(albums.find_album(self.conn, album["id"])["id"], album["id"])
        self.assertEqual(albums.find_album(self.conn, str(album["id"]))["id"], album["id"])
        self.assertIsNone(albums.find_album(self.conn, "nothing"))

    def test_items_order_cover_and_delete(self):
        album = self.change(albums.create_album, "Trip",
                            ids=[self.ids[2], self.ids[0], self.gone, self.ids[2]])
        self.assertEqual(album["count"], 2)
        self.assertEqual(albums.album_items(self.conn, album["id"]), [self.ids[2], self.ids[0]])
        added = self.change(albums.add_items, album["id"], [self.ids[1], self.ids[0], 999])
        self.assertEqual(added, {"added": 1, "found": 2})
        self.assertEqual(albums.album_items(self.conn, album["id"]),
                         [self.ids[2], self.ids[0], self.ids[1]])
        order = self.change(albums.move_items, album["id"], [self.ids[1]], self.ids[2])
        self.assertEqual(order, [self.ids[1], self.ids[2], self.ids[0]])
        order = self.change(albums.move_items, album["id"], [self.ids[1]], None)
        self.assertEqual(order, [self.ids[2], self.ids[0], self.ids[1]])
        order = self.change(albums.set_order, album["id"], [self.ids[0], 999])
        self.assertEqual(order, [self.ids[0], self.ids[2], self.ids[1]])
        with self.assertRaises(albums.AlbumError):
            self.change(albums.move_items, album["id"], [self.ids[0]], self.ids[5])
        # The cover is one of its pictures; taking it out clears it.
        with self.assertRaises(albums.AlbumError):
            self.change(albums.update_album, album["id"], {"cover": self.ids[5]})
        self.change(albums.update_album, album["id"], {"cover": self.ids[2]})
        self.assertEqual(albums.get_album(self.conn, album["id"])["cover"], self.ids[2])
        self.assertEqual(self.change(albums.remove_items, album["id"], [self.ids[2], 999]), 1)
        self.assertIsNone(albums.get_album(self.conn, album["id"])["cover"])
        renamed = self.change(albums.update_album, album["id"], {"name": "Road trip"})
        self.assertEqual(renamed["name"], "Road trip")
        self.assertEqual(self.change(albums.delete_album, album["id"]), 2)
        self.assertIsNone(albums.get_album(self.conn, album["id"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM album_items").fetchone()[0], 0)
        # The pictures stay.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 8)

    def test_smart_albums(self):
        options = {"filters": {"type": ["photo"], "rating": ["5", "4"], "favorite": ["yes"]},
                   "sort": "newest", "by": "words", "lib": "lib_family1", "from": "2019",
                   "to": "2020-06"}
        album = self.change(albums.create_album, "Best", kind="smart", query=" sunset beach ",
                            options=options)
        self.assertEqual(album["query"], "sunset beach")
        self.assertEqual(album["options"], options)
        self.assertNotIn("count", album)
        # A smart album takes no pictures of its own.
        for function, args in ((albums.add_items, ([self.ids[0]],)),
                               (albums.remove_items, ([self.ids[0]],)),
                               (albums.move_items, ([self.ids[0]], None))):
            with self.assertRaises(albums.AlbumError) as caught:
                self.change(function, album["id"], *args)
            self.assertEqual(caught.exception.status, 409)
        changed = self.change(albums.update_album, album["id"], {"query": "beach",
                                                                  "options": {}})
        self.assertEqual((changed["query"], changed["options"]), ("beach", {}))
        for bad in ({"filters": {"nope": ["x"]}}, {"filters": {"type": "photo"}},
                    {"filters": {"type": [1]}}, {"sort": "random"}, {"by": "magic"},
                    {"from": "2019-13"}, {"to": "yesterday"}, {"lib": 5}, {"extra": 1}, []):
            with self.assertRaises(albums.AlbumError):
                albums.clean_options(bad)
        with self.assertRaises(albums.AlbumError):
            self.change(albums.create_album, "Empty search", kind="smart", query="  ")
        with self.assertRaises(albums.AlbumError):
            self.change(albums.create_album, "With ids", kind="smart", query="x",
                        ids=[self.ids[0]])
        with self.assertRaises(albums.AlbumError):
            self.change(albums.create_album, "Odd", kind="other")
        with self.assertRaises(albums.AlbumError):
            self.change(albums.update_album, self.change(albums.create_album, "M")["id"],
                        {"query": "x"})

    def test_index_albums_and_list(self):
        hidden = insert_image(self.conn, "lib_family1", "hidden.jpg", hidden=1)
        trip = self.change(albums.create_album, "trip", ids=[self.ids[1], hidden, self.other,
                                                             self.ids[0]])
        smart = self.change(albums.create_album, "Beach", kind="smart", query="beach")
        self.change(albums.update_album, trip["id"], {"cover": hidden})
        listed = albums.index_albums(self.conn, ["lib_family1"], include_hidden=True)
        self.assertEqual([album["name"] for album in listed], ["Beach", "trip"])
        self.assertEqual(listed[1]["items"], [self.ids[1], hidden, self.ids[0]])
        self.assertEqual(listed[1]["cover"], hidden)
        self.assertEqual((listed[0]["kind"], listed[0]["query"], listed[0]["options"]),
                         ("smart", "beach", {}))
        self.assertNotIn("items", listed[0])
        others = albums.index_albums(self.conn, ["lib_family1"], include_hidden=False)
        self.assertEqual(others[1]["items"], [self.ids[1], self.ids[0]])
        self.assertIsNone(others[1]["cover"])
        everything = {album["id"]: album for album in albums.list_albums(self.conn)}
        self.assertEqual(everything[trip["id"]]["count"], 4)
        self.assertNotIn("count", everything[smart["id"]])
        state = albums.data_state(self.conn, ["lib_family1"])
        self.change(albums.move_items, trip["id"], [self.ids[0]], self.ids[1])
        self.assertNotEqual(state["albums"], albums.data_state(self.conn,
                                                               ["lib_family1"])["albums"])


class FollowTest(AlbumsFixture, unittest.TestCase):
    """reassign and give: marks follow content like edits; album places merge."""

    def marks(self):
        return albums.load_marks(self.conn, self.ids)

    def test_a_move_takes_marks_and_places(self):
        a, b = self.ids[0], self.ids[1]
        self.change(albums.set_marks, [a], favorite=True, rating=4)
        album = self.change(albums.create_album, "Trip", ids=[self.ids[3], a, self.ids[4]])
        self.change(albums.update_album, album["id"], {"cover": a})
        self.assertEqual(self.change(albums.give, a, [b]), b)
        self.assertEqual(self.marks(), {b: {"favorite": True, "rating": 4}})
        # (b took a's place in the album, and its cover.)
        self.assertEqual(albums.album_items(self.conn, album["id"]),
                         [self.ids[3], b, self.ids[4]])
        self.assertEqual(albums.get_album(self.conn, album["id"])["cover"], b)

    def test_a_copy_keeps_them_too_and_own_marks_stay(self):
        a, b, c = self.ids[0], self.ids[1], self.ids[2]
        self.change(albums.set_marks, [a], favorite=True)
        self.change(albums.set_marks, [b], rating=2)
        first = self.change(albums.create_album, "One", ids=[a])
        second = self.change(albums.create_album, "Two", ids=[b])
        # b has marks of its own: they stay, and a's go to c with its album places.
        self.assertEqual(self.change(albums.give, a, [b, c], copy=True), c)
        self.assertEqual(self.marks()[b], {"favorite": False, "rating": 2})
        self.assertEqual(self.marks()[a], {"favorite": True, "rating": 0})
        self.assertEqual(self.marks()[c], {"favorite": True, "rating": 0})
        self.assertEqual(albums.picture_marks(self.conn, b)[2], [second["id"]])
        self.assertEqual(albums.picture_marks(self.conn, c)[2], [first["id"]])
        self.assertEqual(albums.picture_marks(self.conn, a)[2], [first["id"]])
        # Album places alone go to the first picture, and merge with its own.
        self.change(albums.set_marks, [self.ids[3]], favorite=False)
        third = self.change(albums.create_album, "Three", ids=[self.ids[3]])
        self.assertEqual(self.change(albums.give, self.ids[3], [b]), b)
        self.assertEqual(albums.picture_marks(self.conn, b)[2], [second["id"], third["id"]])
        self.assertEqual(albums.picture_marks(self.conn, self.ids[3])[2], [])

    def test_swapped_files_swap_their_marks(self):
        a, b = self.ids[0], self.ids[1]
        self.change(albums.set_marks, [a], favorite=True)
        self.change(albums.set_marks, [b], rating=5)
        first = self.change(albums.create_album, "One", ids=[a])
        second = self.change(albums.create_album, "Two", ids=[b])
        handed = self.change(albums.reassign, {a: ([b], False), b: ([a], False)})
        self.assertEqual(handed, {a: b, b: a})
        self.assertEqual(self.marks(), {b: {"favorite": True, "rating": 0},
                                        a: {"favorite": False, "rating": 5}})
        self.assertEqual(albums.picture_marks(self.conn, a)[2], [second["id"]])
        self.assertEqual(albums.picture_marks(self.conn, b)[2], [first["id"]])

    def test_edits_choose_heirs_the_same_way(self):
        plan = {1: ([2, 3], False), 2: ([4], True)}
        self.assertEqual(image_edits.choose_heirs(plan, {1}), {1: 2})
        self.assertEqual(image_edits.choose_heirs(plan, {1, 2}), {1: 3, 2: 4})
        self.assertEqual(image_edits.choose_heirs({1: ([2], False)}, {1, 2}), {})


class RecordsTest(AlbumsFixture, unittest.TestCase):

    def test_records_carry_the_marks(self):
        self.change(albums.set_marks, [self.ids[0]], favorite=True, rating=5)
        album = self.change(albums.create_album, "Trip", ids=[self.ids[0], self.ids[1]])
        config = common.load_config()
        records = {record["i"]: record for record in search_index.build_records(
            self.conn, config, ["lib_family1"])}
        self.assertEqual((records[self.ids[0]]["fav"], records[self.ids[0]]["rt"],
                          records[self.ids[0]]["al"]), (1, 5, [album["id"]]))
        self.assertEqual(records[self.ids[1]]["al"], [album["id"]])
        for key in ("fav", "rt", "al"):
            self.assertNotIn(key, records[self.ids[2]])
        # An export: none of it unless its marks option says so.
        exported = {record["i"]: record for record in search_index.build_records(
            self.conn, config, ["lib_family1"], for_export=True, export_options={})}
        for key in ("fav", "rt", "al"):
            self.assertNotIn(key, exported[self.ids[0]])
        exported = {record["i"]: record for record in search_index.build_records(
            self.conn, config, ["lib_family1"], for_export=True,
            export_options={"marks": True})}
        self.assertEqual(exported[self.ids[0]]["fav"], 1)
        self.assertEqual(search_index.picture_record(self.conn, self.ids[0])["rt"], 5)

    def test_the_index_answer_follows_marks_and_albums(self):
        config = common.load_config()
        service = search_index.IndexService(watch=False)
        libraries = {"lib_family1": "Family"}

        def answer():
            status, body, headers = service.respond(self.conn, config, libraries)
            self.assertEqual(status, 200)
            return json.loads(body.decode("utf-8")), headers["ETag"]

        first, tag = answer()
        self.assertEqual(first["albums"], [])
        builds = service.library_builds
        self.change(albums.set_marks, [self.ids[0]], favorite=True)
        second, tag2 = answer()
        self.assertNotEqual(tag, tag2)
        self.assertEqual([record.get("fav") for record in second["records"]
                          if record["i"] == self.ids[0]], [1])
        self.assertEqual(service.library_builds, builds + 1)
        album = self.change(albums.create_album, "Trip", ids=[self.ids[2], self.ids[1]])
        third, tag3 = answer()
        self.assertNotEqual(tag2, tag3)
        self.assertEqual(third["albums"], [{"id": album["id"], "name": "Trip", "kind": "manual",
                                            "cover": None,
                                            "items": [self.ids[2], self.ids[1]]}])
        # A rename changes the answer, but no records are built again.
        builds = service.library_builds
        self.change(albums.update_album, album["id"], {"name": "Road trip"})
        fourth, tag4 = answer()
        self.assertNotEqual(tag3, tag4)
        self.assertEqual(fourth["albums"][0]["name"], "Road trip")
        self.assertEqual(service.library_builds, builds)
        # Nothing changed: the same tag (304 for a browser that has it).
        status, _body, _headers = service.respond(self.conn, config, libraries,
                                                  etag_matches=lambda value: value in tag4)
        self.assertEqual(status, 304)


class SafetyTest(AlbumsFixture, unittest.TestCase):
    """Album ids, the index's state, what others see, long orders, album: terms."""

    def test_an_album_id_is_never_used_again(self):
        # (An export's saved album, or an album:#<id> search, must never come
        # to mean another album: ids are not taken again after a delete.)
        self.change(albums.create_album, "First")
        second = self.change(albums.create_album, "Second", ids=[self.ids[0]])
        self.change(albums.delete_album, second["id"])
        third = self.change(albums.create_album, "Third", ids=[self.ids[1]])
        self.assertGreater(third["id"], second["id"])
        self.assertIsNone(albums.get_album(self.conn, second["id"]))
        self.assertIsNone(albums.find_album(self.conn, "#%d" % second["id"]))
        self.assertEqual(albums.find_album(self.conn, "#%d" % third["id"])["name"], "Third")

    def test_a_change_that_keeps_the_sums_is_seen(self):
        # Take picture p out of album a and put picture q in album b with
        # p * a == q * b: the per-library state must still change, or the
        # index would keep serving the old album places.
        config = common.load_config()
        service = search_index.IndexService(watch=False)
        libraries = {"lib_family1": "Family"}

        def places():
            _status, body, headers = service.respond(self.conn, config, libraries)
            records = json.loads(body.decode("utf-8"))["records"]
            return {record["i"]: record.get("al") for record in records}, headers["ETag"]

        one = self.change(albums.create_album, "One")
        two = self.change(albums.create_album, "Two")
        pairs = [(p, q) for p in self.ids for q in self.ids
                 if p != q and p * one["id"] == q * two["id"]]
        self.assertTrue(pairs, "no pictures with p * a == q * b")
        p, q = pairs[0]
        # (All at one time: a clock that ticks slowly gives changes made
        # close together the same time.)
        self.change(albums.add_items, one["id"], [p], now=NOW)
        before, tag = places()
        self.assertEqual((before[p], before[q]), ([one["id"]], None))
        state = albums.data_state(self.conn, ["lib_family1"])
        self.change(albums.remove_items, one["id"], [p], now=NOW)
        self.change(albums.add_items, two["id"], [q], now=NOW)
        self.assertNotEqual(state["libraries"], albums.data_state(self.conn,
                                                                  ["lib_family1"])["libraries"])
        after, tag2 = places()
        self.assertNotEqual(tag, tag2)
        self.assertEqual((after[p], after[q]), (None, [two["id"]]))
        fresh = {record["i"]: record.get("al") for record in search_index.build_records(
            self.conn, config, ["lib_family1"])}
        self.assertEqual(after, fresh)

    def test_every_change_counts_whatever_the_clock_says(self):
        album = self.change(albums.create_album, "Trip", ids=[self.ids[0]], now=NOW)
        states = [albums.data_state(self.conn, ["lib_family1"])]
        changes = [
            (albums.update_album, album["id"], {"name": "Road trip"}),
            (albums.update_album, album["id"], {"cover": self.ids[0]}),
            (albums.add_items, album["id"], [self.ids[1]]),
            (albums.move_items, album["id"], [self.ids[1]], self.ids[0]),
            (albums.set_marks, [self.ids[2]], True),
            (albums.set_marks, [self.ids[2]], False, 2),
        ]
        for change in changes:
            self.change(change[0], *change[1:], now=NOW)
            states.append(albums.data_state(self.conn, ["lib_family1"]))
        self.assertEqual(len({json.dumps(state, sort_keys=True) for state in states}),
                         len(states))
        # The records change only with marks and album places: a rename or a
        # move within the album keeps those of the library.
        self.assertEqual(states[0]["libraries"], states[2]["libraries"])
        self.assertEqual(states[3]["libraries"], states[4]["libraries"])
        self.assertNotEqual(states[4]["libraries"], states[5]["libraries"])
        # Nothing changed, nothing counted.
        self.change(albums.set_marks, [self.ids[2]], False, 2, now=NOW)
        self.assertEqual(albums.data_state(self.conn, ["lib_family1"]), states[-1])
        # A library's count does not move for another library's pictures.
        other = albums.data_state(self.conn, ["lib_other001"])
        self.change(albums.set_marks, [self.ids[3]], True)
        self.assertEqual(albums.data_state(self.conn, ["lib_other001"])["libraries"],
                         other["libraries"])

    def test_marks_are_keyword_only_where_others_add_arguments(self):
        state = search_index.data_state(self.conn, {"lib_family1": "Family"})
        marks = albums.data_state(self.conn, ["lib_family1"])
        with self.assertRaises(TypeError):
            search_index.index_etag(self.conn, {"lib_family1": "Family"}, True, state, marks)
        self.assertEqual(
            search_index.index_etag(self.conn, {"lib_family1": "Family"}, True, state,
                                    marks=marks),
            search_index.index_etag(self.conn, {"lib_family1": "Family"}, True))

    def test_others_see_only_albums_that_show_them_something(self):
        hidden = insert_image(self.conn, "lib_family1", "hidden.jpg", hidden=1)
        self.change(albums.create_album, "Shown", ids=[self.ids[0], hidden])
        self.change(albums.create_album, "Hidden only", ids=[hidden])
        self.change(albums.create_album, "Elsewhere", ids=[self.other])
        self.change(albums.create_album, "Smart here", kind="smart", query="x",
                    options={"lib": "lib_family1"})
        self.change(albums.create_album, "Smart anywhere", kind="smart", query="x")
        self.change(albums.create_album, "Smart elsewhere", kind="smart",
                    query='folder:"Private/Medical"', options={"lib": "lib_other001"})
        self.change(albums.create_album, "Smart filter elsewhere", kind="smart", query="x",
                    options={"filters": {"library": ["lib_other001"]}})
        names = [album["name"] for album in albums.index_albums(
            self.conn, ["lib_family1"], include_hidden=False)]
        self.assertEqual(names, ["Shown", "Smart anywhere", "Smart here"])
        # The admin sees every album.
        self.assertEqual(len(albums.index_albums(self.conn, ["lib_family1"])), 7)

    def test_long_orders_stay_quick(self):
        ids = [insert_image(self.conn, "lib_family1", "many/%d.jpg" % number)
               for number in range(albums.MARK_LIMIT)]
        album = self.change(albums.create_album, "Many", ids=ids)
        import time
        start = time.time()
        new = self.change(albums.move_items, album["id"], ids[1:] + ids[:1], None)
        self.assertEqual(new, ids[1:] + ids[:1])
        new = self.change(albums.set_order, album["id"], list(reversed(ids)) + ids[:5])
        self.assertEqual(new, list(reversed(ids)))
        # (Both took minutes with this many before: they were quadratic.)
        self.assertLess(time.time() - start, 20)
        with self.assertRaises(albums.AlbumError):
            self.change(albums.move_items, album["id"], [ids[0], ids[1]], ids[1])

    def test_query_terms_read_like_search_core(self):
        terms = albums.query_terms('beach -folder:"Private/Medical" FILE:IMG_1.jpg '
                                   'album:\u201cSummer trip\u201d similar:42 nothing:here '
                                   'place: tag:x"y')
        self.assertEqual([(term["field"], term["value"], term["neg"]) for term in terms],
                         [("folder", "Private/Medical", True), ("file", "IMG_1.jpg", False),
                          ("album", "Summer trip", False), ("similar", "42", False),
                          ("tag", 'x"y', False)])
        text = 'album:"Summer trip" OR album:dogs'
        first, second = albums.query_terms(text)
        self.assertEqual(text[first["value_start"]:first["value_stop"]], "Summer trip")
        self.assertEqual(text[second["value_start"]:second["value_stop"]], "dogs")
        self.assertEqual(albums.query_terms(""), [])
        self.assertEqual(albums.query_terms(None), [])

    def test_find_listed_finds_albums_as_search_core_does(self):
        listed = [{"id": 3, "name": 'Say "hi"'}, {"id": 7, "name": "3"},
                  {"id": 8, "name": "Caf\u00e9"}, {"id": 9, "name": "Cars"}]
        self.assertEqual(albums.find_listed(listed, "#3")["id"], 3)
        self.assertEqual(albums.find_listed(listed, "3")["id"], 7)       # the name first
        self.assertEqual(albums.find_listed(listed, "say hi")["id"], 3)
        self.assertEqual(albums.find_listed(listed, "CAFE")["id"], 8)
        self.assertIsNone(albums.find_listed(listed, "Ca"))               # two start so
        self.assertEqual(albums.find_listed(listed, "car")["id"], 9)
        self.assertIsNone(albums.find_listed(listed, "#99"))
        self.assertIsNone(albums.find_listed(listed, "  "))


@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class ScanFollowTest(support.TempBaseMixin, unittest.TestCase):
    """Marks and album places follow moved, renamed and swapped files (scan.py)."""

    def setUp(self):
        super().setUp()
        from test_scan import write_jpeg
        import jobs
        import scan
        self.write_jpeg = write_jpeg
        self.jobs = jobs
        self.scan_module = scan
        self.folder = self.base.library_folder("Family")

    def configure(self, libraries=None, **extra):
        self.base.write_config(libraries or [support.library("Family", self.folder)], **extra)
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)

    def scan(self, **kwargs):
        self.ctx = self.jobs.JobContext("test")
        self.scan_module.run(self.config, self.ctx, describe_images=False, **kwargs)

    def with_conn(self, function, *args, **kwargs):
        conn = db.connect()
        try:
            with conn:
                return function(conn, *args, **kwargs)
        finally:
            conn.close()

    def id_of(self, path):
        return self.with_conn(lambda conn: conn.execute(
            "SELECT id FROM images WHERE path = ? AND missing_since IS NULL",
            (path,)).fetchone()[0])

    def marks_by_path(self):
        def read(conn):
            result = {}
            for row in conn.execute("SELECT id, path FROM images WHERE missing_since IS NULL"):
                favorite, rating, album_ids = albums.picture_marks(conn, row["id"])
                if favorite or rating or album_ids:
                    result[row["path"]] = (favorite, rating, album_ids)
            return result
        return self.with_conn(read)

    def test_a_moved_and_renamed_file_keeps_its_marks(self):
        path = self.write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        for index in range(3):
            self.write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
        self.configure()
        self.scan()
        picture = self.id_of("a.jpg")
        self.with_conn(albums.set_marks, [picture], favorite=True, rating=4)
        album = self.with_conn(albums.create_album, "Trip",
                               ids=[self.id_of("k0.jpg"), picture, self.id_of("k1.jpg")])
        self.with_conn(albums.update_album, album["id"], {"cover": picture})
        os.makedirs(os.path.join(self.folder, "Moved"))
        os.rename(path, os.path.join(self.folder, "Moved", "renamed.jpg"))
        self.scan()
        moved = self.id_of("Moved/renamed.jpg")
        self.assertEqual(self.marks_by_path(), {
            "Moved/renamed.jpg": (1, 4, [album["id"]]), "k0.jpg": (0, 0, [album["id"]]),
            "k1.jpg": (0, 0, [album["id"]])})
        # Its place in the album and the cover went with it.
        self.assertEqual(self.with_conn(albums.album_items, album["id"])[1], moved)
        self.assertEqual(self.with_conn(albums.get_album, album["id"])["cover"], moved)

    def test_swapped_names_swap_the_marks(self):
        a = self.write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        b = self.write_jpeg(os.path.join(self.folder, "b.jpg"), seed=4)
        self.write_jpeg(os.path.join(self.folder, "c.jpg"), seed=5)
        self.configure(reader_threads=1)
        self.scan()
        self.with_conn(albums.set_marks, [self.id_of("a.jpg")], favorite=True)
        self.with_conn(albums.set_marks, [self.id_of("b.jpg")], rating=2)
        os.replace(a, a + ".tmp")
        os.replace(b, a)
        os.replace(a + ".tmp", b)
        import time
        later = time.time() + 10
        os.utime(a, (later, later))
        os.utime(b, (later, later))
        self.scan()
        self.assertEqual(self.marks_by_path(), {"a.jpg": (0, 2, []), "b.jpg": (1, 0, [])})

    def test_a_purged_file_takes_its_marks_along_unless_a_copy_is_left(self):
        import time
        self.write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        self.write_jpeg(os.path.join(self.folder, "b.jpg"), seed=6)
        self.write_jpeg(os.path.join(self.folder, "copy-of-b.jpg"), seed=6)
        for index in range(3):
            self.write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
        self.configure(purge_after_days=1)
        self.scan()
        self.with_conn(albums.set_marks, [self.id_of("a.jpg"), self.id_of("b.jpg")],
                       favorite=True)
        album = self.with_conn(albums.create_album, "Trip",
                               ids=[self.id_of("a.jpg"), self.id_of("b.jpg")])
        os.remove(os.path.join(self.folder, "a.jpg"))
        os.remove(os.path.join(self.folder, "b.jpg"))
        start = time.time()
        self.scan(now=start)
        self.scan(now=start + 2 * 86400)
        self.assertEqual(self.marks_by_path(), {"copy-of-b.jpg": (1, 0, [album["id"]])})
        self.assertEqual(self.with_conn(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM image_marks").fetchone()[0]), 1)

    def test_a_move_to_another_library_keeps_the_marks(self):
        import time
        other = self.base.library_folder("Other")
        path = self.write_jpeg(os.path.join(self.folder, "a.jpg"), seed=3)
        for index in range(3):
            self.write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other)], purge_after_days=1)
        self.scan()
        self.with_conn(albums.set_marks, [self.id_of("a.jpg")], rating=5)
        album = self.with_conn(albums.create_album, "Trip", ids=[self.id_of("a.jpg")])
        os.rename(path, os.path.join(other, "a.jpg"))
        start = time.time()
        self.scan(now=start)
        self.scan(now=start + 2 * 86400)
        self.assertEqual(self.marks_by_path(), {"a.jpg": (0, 5, [album["id"]])})
        self.assertEqual(self.with_conn(lambda conn: conn.execute(
            "SELECT library_id FROM images WHERE path = 'a.jpg' AND missing_since IS NULL"
        ).fetchone()[0]), self.config["libraries"][1]["id"])

    def test_edits_and_marks_come_from_their_own_missing_copies(self):
        # Two copies of a picture: the older has the owner's edit, the newer
        # a favorite. Both go missing; the picture turns up in another
        # library: it gets the edit and the favorite.
        other = self.base.library_folder("Other")
        x1 = self.write_jpeg(os.path.join(self.folder, "x1.jpg"), seed=3)
        x2 = self.write_jpeg(os.path.join(self.folder, "x2.jpg"), seed=3)
        for index in range(10):
            self.write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
        self.configure([support.library("Family", self.folder),
                        support.library("Other", other)])
        self.scan()
        first, second = sorted((self.id_of("x1.jpg"), self.id_of("x2.jpg")))
        self.with_conn(image_edits.save, first, {"title": "Grandma's birthday"})
        self.with_conn(albums.set_marks, [second], favorite=True)
        os.remove(x1)
        os.rename(x2, os.path.join(other, "y.jpg"))
        self.scan()
        found = self.id_of("y.jpg")
        self.assertEqual(self.with_conn(image_edits.load, found)["title"], "Grandma's birthday")
        self.assertEqual(self.with_conn(albums.picture_marks, found), (1, 0, []))

    def test_a_rating_no_copy_can_take_is_noted(self):
        import time
        self.write_jpeg(os.path.join(self.folder, "a.jpg"), seed=6)
        self.write_jpeg(os.path.join(self.folder, "copy-of-a.jpg"), seed=6)
        for index in range(3):
            self.write_jpeg(os.path.join(self.folder, "k%d.jpg" % index), seed=10 + index)
        self.configure(purge_after_days=1)
        self.scan()
        self.with_conn(albums.set_marks, [self.id_of("a.jpg")], rating=5)
        self.with_conn(albums.set_marks, [self.id_of("copy-of-a.jpg")], rating=2)
        os.remove(os.path.join(self.folder, "a.jpg"))
        start = time.time()
        messages = []
        for now in (start, start + 2 * 86400):
            self.scan(now=now)
            messages += [entry["message"]
                         for entry in self.ctx.snapshot(log_tail=1000)["log_tail"]]
        self.assertEqual(self.marks_by_path(), {"copy-of-a.jpg": (0, 2, [])})
        self.assertEqual(self.with_conn(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM images WHERE path = 'a.jpg'").fetchone()[0]), 0)
        self.assertEqual(sum("Your rating of a.jpg went with it" in message
                             for message in messages), 1, messages)


if __name__ == "__main__":
    unittest.main()
