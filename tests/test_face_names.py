"""Tests for naming a face where it is (the viewer's popover, viewer.js openNaming): the
undo of a change of the people (people_groups.snapshot / restore), the admin routes
that make the changes and undo them (POST /api/admin/people/faces and /people/<id>
with "undo": true, POST /api/admin/people/undo) and who may use them, the pages
seeing the change (/api/people's ETag, the details' people), and the page scripts
(tests/js/face_names_test.js under Node).

The faces are made up: vectors near one axis per made-up person (no photo of anyone).
Every test runs in a temporary SETTINGS_BASE.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from unittest import mock

import support

import common
import people
import people_groups
from test_people_groups import (NOW, StoreMixin, ViewsMixin, face, options, person_vector,
                                store_faces)

try:
    import flask
except ImportError:
    flask = None

JS_TESTS = os.path.join(support.REPO, "tests", "js", "face_names_test.js")


def state_of(store):
    """Everything an undo puts back: the faces' people, rejects and marks, the people."""
    return {
        "face_people": store.execute("SELECT face_id, person_id, owner, updated FROM "
                                     "face_people ORDER BY face_id").fetchall(),
        "face_rejects": store.execute("SELECT face_id, person_id, updated FROM face_rejects "
                                      "ORDER BY face_id, person_id").fetchall(),
        "face_marks": store.execute("SELECT face_id, not_face, updated FROM face_marks ORDER "
                                    "BY face_id").fetchall(),
        "people": store.execute("SELECT id, name, hidden, cover, created, updated FROM people "
                                "ORDER BY id").fetchall(),
    }


class UndoTest(StoreMixin, unittest.TestCase):
    """people_groups.snapshot / settle_snapshot / restore."""

    def setUp(self):
        super().setUp()
        self.made = self.add_people([4, 3])
        # A face of nobody: one picture of a third made-up person.
        store_faces(self.store, "lone", [face(100, 100, 200, person_vector(9))])
        people_groups.group(self.store, options(), now=NOW)
        self.ann = self.person_of(self.made[0][0])
        self.bob = self.person_of(self.made[1][0])
        people_groups.update_person(self.store, self.ann, {"name": "Ann"}, now=NOW)
        self.lone = self.faces_of("lone")[0]
        self.assertIsNone(self.person_of("lone"))

    def change(self, work, face_ids=(), person_ids=(), made=None):
        """work() made so that it can be undone (as the routes do): returns (snap,
        result)."""
        result, snap = people_groups.undoable(self.store, face_ids, person_ids, work)
        made_ids = made(result) if made else ()
        self.assertTrue(people_groups.settle_snapshot(self.store, snap, made_ids))
        return snap, result

    def other(self, wait=None):
        """Another connection to the face data (another tab, a job), which waits wait
        seconds for the file to be free to write, when given."""
        conn = people.connect()
        self.addCleanup(conn.close)
        if wait is not None:
            conn.execute("PRAGMA busy_timeout=%d" % int(wait * 1000))
        return conn

    def test_a_new_person_from_a_face_of_nobody_goes_again(self):
        before = state_of(self.store)
        version = people_groups.people_version(self.store)
        snap, moved = self.change(
            lambda: people_groups.move_faces(self.store, [self.lone], None, "Nell", now=NOW + 5),
            [self.lone], made=lambda result: [result["id"]])
        self.assertEqual(moved["name"], "Nell")
        self.assertEqual(snap["made"], [moved["id"]])
        people_groups.restore(self.store, snap)
        self.assertEqual(state_of(self.store), before)
        self.assertIsNone(self.store.execute("SELECT 1 FROM people WHERE id = ?",
                                             (moved["id"],)).fetchone())
        # The people changed twice (the change, its undo): pages ask again (ETags).
        self.assertEqual(people_groups.people_version(self.store), version + 2)

    def test_naming_an_unnamed_person_is_undone(self):
        before = state_of(self.store)
        snap, _info = self.change(lambda: people_groups.update_person(
            self.store, self.bob, {"name": "Bo"}, now=NOW + 5), (), [self.bob])
        self.assertEqual(people_groups.person_info(self.store, self.bob)["name"], "Bo")
        people_groups.restore(self.store, snap)
        self.assertEqual(state_of(self.store), before)
        self.assertEqual(people_groups.person_info(self.store, self.bob)["label"],
                         "Person %d" % self.bob)

    def test_a_person_deleted_with_its_last_face_comes_back(self):
        # Bob with one face left, its cover, and another face "not Bob".
        faces = people_groups.person_faces(self.store, self.bob)
        people_groups.move_faces(self.store, faces[1:], self.ann, now=NOW + 1)
        people_groups.update_person(self.store, self.bob, {"name": "Bob", "cover": faces[0],
                                                           "hidden": True}, now=NOW + 2)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM face_rejects WHERE "
                                            "person_id = ?", (self.bob,)).fetchone()[0], 2)
        before = state_of(self.store)
        snap, _info = self.change(lambda: people_groups.move_faces(
            self.store, faces[:1], self.ann, now=NOW + 5), faces[:1], [self.ann])
        self.assertIsNone(self.store.execute("SELECT 1 FROM people WHERE id = ?",
                                             (self.bob,)).fetchone())
        # (Deleting Bob deleted the "not Bob" of the other faces too.)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM face_rejects WHERE "
                                            "person_id = ?", (self.bob,)).fetchone()[0], 0)
        people_groups.restore(self.store, snap)
        self.assertEqual(state_of(self.store), before)
        info = people_groups.person_info(self.store, self.bob)
        self.assertEqual((info["name"], info["hidden"], info["cover"], info["faces"]),
                         ("Bob", True, faces[0], 1))

    def test_not_this_person_and_not_a_face_are_undone(self):
        face_id = people_groups.person_faces(self.store, self.ann)[0]
        before = state_of(self.store)
        self.assertFalse(people_groups.grouping_due(self.store, options()))
        snap, _reply = self.change(lambda: people_groups.reject_faces(
            self.store, [face_id], self.ann, now=NOW + 5), [face_id], [self.ann])
        # (Taken out of a person: the faces wait for a grouping.)
        self.assertTrue(people_groups.grouping_due(self.store, options()))
        people_groups.restore(self.store, snap)
        self.assertEqual(state_of(self.store), before)
        self.assertFalse(people_groups.grouping_due(self.store, options()))
        snap, _changed = self.change(lambda: people_groups.mark_not_face(
            self.store, [face_id], now=NOW + 6), [face_id], [self.ann])
        self.assertIsNone(self.person_of(self.made[0][0]))
        people_groups.restore(self.store, snap)
        self.assertEqual(state_of(self.store), before)

    def test_a_face_the_grouping_placed_is_its_again(self):
        # (owner 0: the grouping's; a move makes it the owner's, the undo the grouping's.)
        face_id = people_groups.person_faces(self.store, self.bob)[0]
        self.assertEqual(self.store.execute("SELECT owner FROM face_people WHERE face_id = ?",
                                            (face_id,)).fetchone()[0], 0)
        before = state_of(self.store)
        snap, _info = self.change(lambda: people_groups.move_faces(
            self.store, [face_id], self.ann, now=NOW + 5), [face_id], [self.ann])
        people_groups.restore(self.store, snap)
        self.assertEqual(state_of(self.store), before)

    def test_nothing_is_undone_once_the_people_changed_again(self):
        snap, _info = self.change(lambda: people_groups.update_person(
            self.store, self.bob, {"name": "Bo"}), (), [self.bob])
        people_groups.update_person(self.store, self.ann, {"hidden": True})
        with self.assertRaises(people_groups.GroupError) as caught:
            people_groups.restore(self.store, snap)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(people_groups.person_info(self.store, self.bob)["name"], "Bo")
        # Nor after a grouping.
        snap, _info = self.change(lambda: people_groups.update_person(
            self.store, self.bob, {"name": "Bob"}), (), [self.bob])
        people_groups.group(self.store, options())
        with self.assertRaises(people_groups.GroupError):
            people_groups.restore(self.store, snap)

    def test_a_change_in_between_takes_the_undo_away(self):
        snap = people_groups.snapshot(self.store, [self.lone], ())
        people_groups.update_person(self.store, self.bob, {"name": "Bo"})
        people_groups.move_faces(self.store, [self.lone], self.ann)
        self.assertFalse(people_groups.settle_snapshot(self.store, snap))
        self.assertFalse(people_groups.settle_snapshot(self.store, None))

    def test_nothing_comes_between_the_snapshot_and_the_change(self):
        # Another connection's write while the change is made waits (and here gives
        # up): the snapshot is of the state the change changed.
        other = self.other(wait=0.1)
        tried = []

        def change():
            try:
                people_groups.update_person(other, self.bob, {"name": "Robert"})
                tried.append("written")
            except sqlite3.OperationalError:
                tried.append("waited")
            return people_groups.move_faces(self.store, [self.lone], None, "Nell", now=NOW + 5)
        moved, snap = people_groups.undoable(self.store, [self.lone], (), change)
        self.assertEqual(tried, ["waited"])
        self.assertTrue(people_groups.settle_snapshot(self.store, snap, [moved["id"]]))
        self.assertEqual(people_groups.person_info(self.store, self.bob)["name"], "")
        # A change that fails leaves nothing behind (and the file free).
        with self.assertRaises(people_groups.GroupError):
            people_groups.undoable(self.store, [self.lone], (), lambda: people_groups.move_faces(
                self.store, [self.lone], None, "Ann"))
        self.assertFalse(self.store.in_transaction)
        people_groups.update_person(other, self.bob, {"name": "Robert"})

    def test_a_write_from_elsewhere_right_after_the_change_takes_the_undo_away(self):
        moved, snap = people_groups.undoable(
            self.store, [self.lone], (),
            lambda: people_groups.move_faces(self.store, [self.lone], None, "Nell"))
        # Faces found by a job, between the change and its settling (the people's
        # version stays: only the faces' generation is raised).
        version = people_groups.people_version(self.store)
        store_faces(self.other(), "found", [face(100, 100, 200, person_vector(9, seed=5))])
        self.assertEqual(people_groups.people_version(self.store), version)
        self.assertFalse(people_groups.settle_snapshot(self.store, snap, [moved["id"]]))

    def test_the_undo_checks_and_writes_in_one_go(self):
        snap, _moved = self.change(
            lambda: people_groups.move_faces(self.store, [self.lone], None, "Nell"),
            [self.lone], made=lambda result: [result["id"]])
        # Another tab's change right after the undo read the people's version: it waits
        # (and here gives up) rather than being overwritten by the undo.
        other = self.other(wait=0.1)
        real = people_groups.people_version
        tried = []

        def version(store):
            value = real(store)
            if store is self.store and not tried:
                try:
                    people_groups.move_faces(other, [self.lone], self.bob)
                    tried.append("written")
                except sqlite3.OperationalError:
                    tried.append("waited")
            return value
        with mock.patch.object(people_groups, "people_version", version):
            people_groups.restore(self.store, snap)
        self.assertEqual(tried, ["waited"])
        self.assertIsNone(self.person_of("lone"))
        # (A refused undo leaves the file free.)
        with self.assertRaises(people_groups.GroupError):
            people_groups.restore(self.store, snap)
        self.assertFalse(self.store.in_transaction)

    def test_a_grouping_keeps_what_the_owner_said_while_it_was_worked_out(self):
        # The viewer's changes (another connection) while each try of a grouping is worked
        # out: the last try is written whatever changed, and keeps them.
        other = self.other()
        bob_face = people_groups.person_faces(self.store, self.bob)[0]
        ann_faces = people_groups.person_faces(self.store, self.ann)
        changes = [
            lambda: people_groups.update_person(other, self.bob, {"name": "Bo"}),
            lambda: people_groups.update_person(other, self.bob, {"name": "Bob"}),
            lambda: (people_groups.move_faces(other, [bob_face], None, "Nell"),
                     people_groups.reject_faces(other, [ann_faces[0]], self.ann),
                     people_groups.mark_not_face(other, [ann_faces[1]])),
        ]
        real = people_groups.plan

        def plan(*args, **kwargs):
            result = real(*args, **kwargs)
            if changes:
                changes.pop(0)()
            return result
        with mock.patch.object(people_groups, "plan", plan):
            people_groups.group(self.store, options())
        self.assertEqual(changes, [])
        nell = people_groups.face_person(self.store, bob_face)
        self.assertEqual(people_groups.person_info(self.store, nell)["name"], "Nell")
        self.assertEqual(self.store.execute("SELECT owner FROM face_people WHERE face_id = ?",
                                            (bob_face,)).fetchone()[0], 1)
        self.assertNotEqual(people_groups.face_person(self.store, ann_faces[0]), self.ann)
        self.assertIsNone(people_groups.face_person(self.store, ann_faces[1]))
        self.assertEqual(people_groups.person_info(self.store, self.bob)["name"], "Bob")

    def test_a_face_that_went_while_grouping_is_left_out(self):
        # (A picture's faces deleted meanwhile -- the picture went -- without a change of
        # the people: the grouping writes the others.)
        gone = people_groups.person_faces(self.store, self.bob)[0]
        other = self.other()
        real = people_groups.plan

        def plan(*args, **kwargs):
            result = real(*args, **kwargs)
            other.execute("DELETE FROM faces WHERE id = ?", (gone,))
            other.commit()
            return result
        with mock.patch.object(people_groups, "plan", plan):
            people_groups.group(self.store, options())
        self.assertEqual(len(people_groups.person_faces(self.store, self.bob)), 2)
        self.assertEqual(len(people_groups.person_faces(self.store, self.ann)), 4)

    def test_snapshots_of_too_many_faces_or_bad_ids(self):
        self.assertIsNone(people_groups.snapshot(
            self.store, list(range(1, people_groups.UNDO_FACES + 2)), ()))
        self.assertIsNone(people_groups.snapshot(self.store, ["x"], ()))
        snap = people_groups.snapshot(self.store, [self.lone, self.lone], [True, "x", self.ann])
        self.assertEqual(sorted(snap["faces"]), [self.lone])
        self.assertEqual(sorted(snap["people"]), [self.ann])

    def test_faces_found_again_since_are_left_as_they_are(self):
        # A run that found the picture's faces again (other ids) in between: the undo
        # puts back what it can (the people) and leaves the new faces alone.
        snap, moved = self.change(
            lambda: people_groups.move_faces(self.store, [self.lone], None, "Nell"),
            [self.lone], made=lambda result: [result["id"]])
        self.store.execute("DELETE FROM faces WHERE id = ?", (self.lone,))
        self.store.commit()
        result = people_groups.restore(self.store, snap)
        self.assertEqual(result["faces"], 0)
        self.assertIsNone(self.store.execute("SELECT 1 FROM people WHERE id = ?",
                                             (moved["id"],)).fetchone())


@unittest.skipIf(flask is None, "Flask is not installed")
@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class RoutesTest(ViewsMixin, unittest.TestCase):
    """The admin's routes with "undo": true, and /api/admin/people/undo."""

    def setUp(self):
        super().setUp()
        import serve
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.visitor = self.app.test_client()
        self.visitor.environ_base["REMOTE_ADDR"] = "192.0.2.20"
        self.addCleanup(self.app.extensions["search_images"].close)
        # A face of nobody in a picture of its own.
        import test_people_groups
        store_faces(self.store, "lone", [face(100, 100, 200, person_vector(9))])
        self.lone_image = test_people_groups.insert_image(self.conn, self.library_id,
                                                          "lone.jpg", "lone")
        self.lone = self.faces_of("lone")[0]

    def admin(self, path, body=None):
        headers = {"X-Search-Images": "1"}
        if body is None:
            return self.client.get("/api/admin" + path, headers=headers)
        return self.client.post("/api/admin" + path, headers=headers, json=body)

    def picture_people(self, image_id):
        return self.client.get("/api/image/%d" % image_id).get_json()["people"]

    def test_a_face_named_in_the_viewer_and_undone(self):
        before = state_of(self.store)
        listed = self.client.get("/api/people")
        etag = listed.headers["ETag"]
        self.assertEqual(self.client.get("/api/people", headers={"If-None-Match": etag})
                         .status_code, 304)
        self.assertEqual(self.picture_people(self.lone_image)["faces"][0]["person"], None)
        reply = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "to": None, "name": "Nell", "undo": True})
        self.assertEqual(reply.status_code, 200)
        answer = reply.get_json()
        self.assertEqual(answer["person"]["name"], "Nell")
        self.assertIsInstance(answer["undo"], str)
        # The details, /api/people and person: see it at once (a new ETag).
        view = self.picture_people(self.lone_image)
        self.assertEqual([item["label"] for item in view["names"]], ["Nell"])
        self.assertEqual(view["faces"][0]["label"], "Nell")
        listed = self.client.get("/api/people", headers={"If-None-Match": etag})
        self.assertEqual(listed.status_code, 200)
        self.assertIn("Nell", [item["name"] for item in listed.get_json()["people"]])
        # Undo: as it was (the person made goes).
        undone = self.admin("/people/undo", {"undo": answer["undo"]})
        self.assertEqual((undone.status_code, undone.get_json()), (200, {"undone": True}))
        self.assertEqual(state_of(self.store), before)
        self.assertEqual(self.picture_people(self.lone_image)["names"], [])
        self.assertNotIn("Nell", [item["name"] for item in
                                  self.client.get("/api/people").get_json()["people"]])
        # Once.
        again = self.admin("/people/undo", {"undo": answer["undo"]})
        self.assertEqual(again.status_code, 409)

    def test_naming_a_person_and_moving_faces(self):
        before = state_of(self.store)
        named = self.admin("/people/%d" % self.bob, {"name": "Bo", "undo": True}).get_json()
        self.assertEqual(named["person"]["label"], "Bo")
        undone = self.admin("/people/undo", {"undo": named["undo"]})
        self.assertEqual(undone.status_code, 200)
        self.assertEqual(state_of(self.store), before)
        face_id = self.faces_of(self.made[1][0])[0]
        moved = self.admin("/people/faces", {"action": "move", "faces": [face_id],
                                             "to": self.ann, "person": self.bob,
                                             "undo": True}).get_json()
        self.assertEqual(moved["person"]["id"], self.ann)
        self.assertEqual(moved["source"]["faces"], 2)
        self.assertEqual(self.admin("/people/undo", {"undo": moved["undo"]}).status_code, 200)
        self.assertEqual(state_of(self.store), before)
        rejected = self.admin("/people/faces", {"action": "reject", "faces": [face_id],
                                                "person": self.bob, "undo": True}).get_json()
        self.assertEqual(rejected["removed"], 1)
        self.assertEqual(self.admin("/people/undo", {"undo": rejected["undo"]}).status_code,
                         200)
        gone = self.admin("/people/faces", {"action": "not_face", "faces": [face_id],
                                            "person": self.bob, "undo": True}).get_json()
        self.assertEqual(gone["changed"], 1)
        self.assertEqual(self.admin("/people/undo", {"undo": gone["undo"]}).status_code, 200)
        self.assertEqual(state_of(self.store), before)

    def test_only_the_last_change_and_only_while_nothing_else_changed(self):
        first = self.admin("/people/%d" % self.bob, {"name": "Bo", "undo": True}).get_json()
        second = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                              "to": self.ann, "undo": True}).get_json()
        self.assertEqual(self.admin("/people/undo", {"undo": first["undo"]}).status_code, 409)
        # A change from elsewhere (the People page, without undo) in between.
        self.admin("/people/%d" % self.ann, {"hidden": True})
        refused = self.admin("/people/undo", {"undo": second["undo"]})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("People page", refused.get_json()["error"])
        self.assertIn("changed since then", refused.get_json()["error"])
        self.assertEqual(self.person_of("lone"), self.ann)
        # Refused once, it is forgotten (its snapshot let go).
        again = self.admin("/people/undo", {"undo": second["undo"]})
        self.assertEqual(again.status_code, 409)
        self.assertIn("started again", again.get_json()["error"])
        for bad in ({}, {"undo": 5}, {"undo": ""}, {"undo": "nope"}):
            self.assertEqual(self.admin("/people/undo", bad).status_code, 409, bad)
        self.assertEqual(self.client.post("/api/admin/people/undo", headers={
            "X-Search-Images": "1"}, data="x", content_type="application/json").status_code,
            400)

    def test_without_undo_the_answers_are_as_before(self):
        reply = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "to": self.ann}).get_json()
        self.assertNotIn("undo", reply)
        self.assertNotIn("undo", self.admin("/people/%d" % self.bob, {"name": "Bo"}).get_json())
        # (All of a person's faces, while they are few: undone too.)
        many = self.admin("/people/faces", {"action": "move", "faces": "all",
                                            "person": self.ann, "to": self.bob,
                                            "undo": True}).get_json()
        self.assertIn("undo", many)
        bad = self.admin("/people/faces", {"action": "move", "faces": ["x"], "to": self.ann,
                                           "undo": True})
        self.assertEqual(bad.status_code, 400)

    def test_only_the_admin_on_this_computer(self):
        reply = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "to": None, "name": "Nell", "undo": True})
        token = reply.get_json()["undo"]
        # Without the admin's header (a page of another site): refused.
        self.assertEqual(self.client.post("/api/admin/people/undo",
                                          json={"undo": token}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/people/undo", json={"undo": token},
                                          headers={"X-Search-Images": "1",
                                                   "Sec-Fetch-Site": "cross-site"}
                                          ).status_code, 403)
        # Another computer (a LAN visitor), even with "visitors": true.
        self.config["people"] = {"enabled": True, "visitors": True}
        with open(common.config_path(), "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        for path, body in (("/people/undo", {"undo": token}),
                           ("/people/faces", {"action": "move", "faces": [self.lone],
                                              "to": self.ann, "undo": True}),
                           ("/people/%d" % self.bob, {"name": "X", "undo": True})):
            self.assertEqual(self.visitor.post("/api/admin" + path, json=body,
                                               headers={"X-Search-Images": "1"}).status_code,
                             403, path)
        # Visitors never get the boxes (nor the faces of nobody).
        self.assertNotIn("faces", self.visitor.get("/api/image/%d" % self.lone_image)
                         .get_json().get("people", {}))
        self.assertEqual(self.admin("/people/undo", {"undo": token}).status_code, 200)

    def test_renaming_a_person_by_one_of_its_faces(self):
        # The viewer names a person by the face it was asked about: refused when a
        # grouping or another tab moved that face to another person meanwhile.
        bob_face = self.faces_of(self.made[1][0])[0]
        ann_face = self.faces_of(self.made[0][0])[0]
        stale = self.admin("/people/%d" % self.bob, {"name": "Bo", "face": ann_face,
                                                    "undo": True})
        self.assertEqual(stale.status_code, 409)
        self.assertIn("not in this person any more", stale.get_json()["error"])
        self.assertEqual(people_groups.person_info(self.store, self.bob)["name"], "")
        self.assertFalse(self.store.in_transaction)
        for bad in ("x", True, 0, -3, 1.5):
            self.assertEqual(self.admin("/people/%d" % self.bob, {"name": "Bo", "face": bad})
                             .status_code, 400, bad)
        named = self.admin("/people/%d" % self.bob, {"name": "Bo", "face": bob_face,
                                                    "undo": True})
        self.assertEqual(named.status_code, 200)
        self.assertEqual(named.get_json()["person"]["name"], "Bo")
        self.assertIsInstance(named.get_json()["undo"], str)

    def test_delete_face_data_takes_the_undo_away(self):
        token = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "to": None, "name": "Nell", "undo": True}
                           ).get_json()["undo"]
        etag = self.client.get("/api/people").headers["ETag"]
        # The file is open elsewhere (it cannot be removed): emptied and wiped instead.
        path = people.store_path()
        real = os.remove

        def remove(name):
            if os.path.abspath(name).startswith(os.path.abspath(path)):
                raise PermissionError("open elsewhere")
            return real(name)
        with mock.patch.object(people.os, "remove", remove):
            result = people.delete_all()
        self.assertFalse(result["removed"])
        self.assertEqual(result["error"], "")
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)
        # Another file now: the pages' copies are old (ETag), and nothing comes back.
        self.assertEqual(self.client.get("/api/people", headers={"If-None-Match": etag})
                         .status_code, 200)
        refused = self.admin("/people/undo", {"undo": token})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)

    def test_the_delete_face_data_job_takes_the_undo_away(self):
        token = self.admin("/people/%d" % self.bob, {"name": "Bo", "undo": True}
                           ).get_json()["undo"]
        queued = self.admin("/jobs", {"kind": "delete_faces", "params": {"confirm": True}})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        refused = self.admin("/people/undo", {"undo": token})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(people_groups.person_info(self.store, self.bob)["name"], "Bo")

    def test_no_face_data(self):
        self.store.close()
        people.delete_all()
        self.assertEqual(self.admin("/people/undo", {"undo": "x"}).status_code, 409)
        self.assertEqual(self.admin("/people/faces", {"action": "move", "faces": [1],
                                                      "to": None, "undo": True}).status_code,
                         404)
        self.store = people.connect()


@unittest.skipIf(shutil.which("node") is None, "Node.js is not installed")
class NodeTest(unittest.TestCase):

    def test_the_viewer_names_faces(self):
        # (The real page scripts on the made-up DOM of tests/js/app_meaning_test.js.)
        result = subprocess.run([shutil.which("node"), JS_TESTS], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)


if __name__ == "__main__":
    unittest.main()
