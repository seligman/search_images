"""Tests for "Who's this?" (people_queue.py and the People page's queue): it holds exactly
what the suggestions (people_suggest.py) mark -- every face of nobody and every unnamed
person that is not hidden -- with and without numpy, after each kind of change; its order;
the admin's route (GET /api/admin/people/queue: who may ask, the ETag, 202 while it is
worked out and an answer within a build or two however often the faces change meanwhile,
faces whose pictures are not shown left out, Delete face data stops a build); the answers
it sends (POST /api/admin/people/faces with "from": 409 for a face a grouping moved
meanwhile, "Not <name>" for a whole unnamed person, and "seen": 409 for a person that
changed since it was shown) and Undo, one answer after another (POST
/api/admin/people/undo); and the page itself under Node (tests/js/people_queue_test.js).

The faces are made up: vectors near one axis per made-up person (no photo of anyone).
Every test runs in a temporary SETTINGS_BASE.
"""

import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

import support

import admin
import export
import people
import people_groups
import people_queue
import people_suggest
from test_face_names import state_of
from test_people_groups import (NOW, ViewsMixin, face, insert_image, options, person_vector,
                                store_faces)
from test_people_suggest import CONFIG, SuggestMixin, axis, mix

try:
    import flask
except ImportError:
    flask = None

JS_TESTS = os.path.join(support.REPO, "tests", "js", "people_queue_test.js")


def census(store, use_numpy):
    """{(kind, id): named person} of what people_suggest marks now, asked one by one."""
    marks = {}
    for (face_id,) in store.execute(
            "SELECT id FROM faces WHERE id NOT IN (SELECT face_id FROM face_people) AND id NOT "
            "IN (SELECT face_id FROM face_marks WHERE not_face)"):
        found = people_suggest.suggest(store, face_id, CONFIG, use_numpy=use_numpy)["preselect"]
        if found is not None:
            marks[("face", face_id)] = found
    for (person_id,) in store.execute("SELECT id FROM people WHERE name = '' AND hidden = 0 "
                                      "AND id IN (SELECT person_id FROM face_people)"):
        found = people_suggest.suggest_person(store, person_id, CONFIG,
                                              use_numpy=use_numpy)["preselect"]
        if found is not None:
            marks[("person", person_id)] = found
    return marks


class MarksTest(SuggestMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        people_queue.forget()
        people_suggest.forget()
        self.addCleanup(people_queue.forget)
        self.addCleanup(people_suggest.forget)

    def marks(self, use_numpy):
        built = people_queue.build_marks(self.store, CONFIG, use_numpy=use_numpy)
        return {(item["kind"], item[item["kind"]]): item["to"]
                for item in built["faces"] + built["persons"]}

    def same(self, use_numpy):
        marks = self.marks(use_numpy)
        self.assertEqual(marks, census(self.store, use_numpy))
        return marks

    def unnamed(self, prefix, k, count, seed):
        faces = [self.lone("%s%d" % (prefix, n), person_vector(k, seed=seed + n))
                 for n in range(count)]
        return people_groups.move_faces(self.store, faces, None, "", now=NOW)["id"]

    def follow_changes(self, use_numpy):
        # Like Ann, but in a picture with Ann (the grouping put the other face there): two
        # faces of one picture are two people.
        store_faces(self.store, "with-ann", [face(100, 100, 200, person_vector(1, seed=80)),
                                             face(500, 100, 200, person_vector(1, seed=81))])
        people_groups.group(self.store, options(), now=NOW)
        beside = [face_id for face_id in self.faces_of("with-ann")
                  if people_groups.face_person(self.store, face_id) is None]
        self.assertEqual(len(beside), 1)
        like_ann = [self.lone("a%d" % n, person_vector(1, seed=50 + n)) for n in range(3)]
        like_bob = self.lone("b0", person_vector(2, seed=60))
        self.lone("z0", axis(60))
        # (Like Ann, but turned away: under group_quality.)
        self.lone("blur", person_vector(1, seed=70), sharpness=6.0)
        # Like Ann and Bob at once: neither is clearly ahead.
        self.lone("both", mix({1: 1.0, 2: 1.0}))
        unnamed = self.unnamed("u", 1, 4, 90)
        hidden = self.unnamed("h", 1, 4, 120)
        people_groups.update_person(self.store, hidden, {"hidden": True}, now=NOW)
        marks = self.same(use_numpy)
        self.assertEqual(marks.get(("face", like_ann[0])), self.ann)
        self.assertEqual(marks.get(("face", like_bob)), self.bob)
        self.assertEqual(marks.get(("person", unnamed)), self.ann)
        self.assertNotIn(("face", beside[0]), marks)
        # A hidden unnamed person (a stranger) is not asked about.
        self.assertNotIn(("person", hidden), marks)
        self.assertTrue(people_suggest.suggest_person(self.store, hidden, CONFIG)["preselect"])
        for step in (
                lambda: people_groups.move_faces(self.store, [like_ann[0]], self.ann, now=NOW),
                lambda: people_groups.reject_faces(self.store, [like_ann[1]], self.ann, now=NOW),
                lambda: people_groups.merge(self.store, unnamed, self.ann, now=NOW,
                                            group_quality=options()["group_quality"]),
                lambda: people_groups.mark_not_face(self.store, [like_bob], now=NOW),
                lambda: people_groups.mark_not_face(self.store, [like_bob], False, now=NOW),
                lambda: people_groups.update_person(self.store, self.cy, {"name": "Cy"},
                                                    now=NOW),
                lambda: people_groups.update_person(self.store, self.bob, {"hidden": True},
                                                    now=NOW),
                lambda: people_groups.group(self.store, options(), now=NOW)):
            step()
            self.same(use_numpy)
        self.assertNotIn(("face", like_ann[1]), self.same(use_numpy))

    def test_the_queue_holds_what_the_suggestions_mark_without_numpy(self):
        self.follow_changes(False)

    def test_the_queue_holds_what_the_suggestions_mark_with_numpy(self):
        if people_groups._numpy() is None:
            self.skipTest("numpy is not installed")
        self.follow_changes(True)

    def test_more_faces_than_a_batch(self):
        for n in range(7):
            self.lone("a%d" % n, person_vector(1, seed=50 + n))
            self.lone("z%d" % n, axis(60 + n))
        with mock.patch.object(people_queue, "BATCH", 3), \
                mock.patch.object(people_queue, "PURE_BATCH", 3):
            marks = self.same(False)
            if people_groups._numpy() is not None:
                self.assertEqual(self.same(True), marks)
        self.assertEqual(len(marks), 7)

    def test_without_numpy_other_requests_are_let_in(self):
        # (A loop in Python holds the interpreter's lock: without numpy the build pauses
        # after each few faces and each unnamed person, so the server's other requests
        # -- the faces' pictures -- are not held up for seconds.)
        for n in range(7):
            self.lone("a%d" % n, person_vector(1, seed=50 + n))
        self.unnamed("u", 1, 4, 90)
        pauses = []
        real = time.sleep

        def sleep(seconds):
            pauses.append(seconds)
            real(0)
        with mock.patch.object(people_queue, "PURE_BATCH", 2), \
                mock.patch.object(time, "sleep", sleep):
            built = people_queue.build_marks(self.store, CONFIG, use_numpy=False)
            pure = len(pauses)
            if people_groups._numpy() is not None:
                people_queue.build_marks(self.store, CONFIG, use_numpy=True)
        self.assertTrue(built["faces"] and built["persons"])
        # Between the batches of faces of nobody (2 at a time), and before each unnamed
        # person; none with numpy (one product of matrices).
        self.assertGreaterEqual(pure, 4)
        self.assertEqual(set(pauses), {people_queue.PAUSE})
        self.assertEqual(len(pauses), pure)

    def test_a_build_no_longer_wanted_stops(self):
        for n in range(4):
            self.lone("a%d" % n, person_vector(1, seed=50 + n))
        compared = []
        real = people_suggest._closeness

        def closeness(index, queries):
            compared.append(len(queries))
            return real(index, queries)
        wanted = [True]
        with mock.patch.object(people_queue, "PURE_BATCH", 1), \
                mock.patch.object(people_queue, "BATCH", 1), \
                mock.patch.object(people_suggest, "_closeness", closeness):
            compared_before = len(compared)
            people_queue.build_marks(self.store, CONFIG, use_numpy=False,
                                     stop=lambda: not wanted[0])
            whole = len(compared) - compared_before
            wanted[0] = False
            del compared[:]
            with self.assertRaises(people_queue._Stopped):
                people_queue.build_marks(self.store, CONFIG, use_numpy=False,
                                         stop=lambda: not wanted[0])
        self.assertLess(len(compared), whole)

    def test_the_order(self):
        def f(face_id, to, lead):
            return {"kind": "face", "face": face_id, "to": to, "lead": lead, "score": 0.6}

        def p(person_id, to, faces, pictures):
            return {"kind": "person", "person": person_id, "to": to, "faces": faces,
                    "score": 0.6, "lead": 0.3, "pictures": pictures}
        faces = [f(1, 7, 0.2), f(2, 8, 0.3), f(3, 7, 0.3), f(4, 7, 0.2)]
        # Persons 10, 11 and 12 are suggested to be 7, and 12 is in a picture with each
        # of the others: 12 first (once 10 and 11 are 7, 7 would be in two of its
        # pictures: never preselected), then the others, most faces first; 13 (8's) in
        # one of the same pictures is no link.
        persons = [p(10, 7, 3, ["x/photo"]), p(11, 7, 9, ["y/photo"]),
                   p(12, 7, 5, ["x/photo", "y/photo"]), p(13, 8, 7, ["x/photo"]),
                   p(14, 8, 30, ["w/photo"])]
        ordered = people_queue.order(faces, persons)
        # The faces first, grouped by the person suggested (most first), the surest first.
        self.assertEqual([(item["kind"][0], item.get("face") or item["person"])
                          for item in ordered],
                         [("f", 3), ("f", 1), ("f", 4), ("f", 2),
                          ("p", 14), ("p", 12), ("p", 11), ("p", 10), ("p", 13)])


@unittest.skipIf(flask is None, "Flask is not installed")
@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class RouteTest(ViewsMixin, unittest.TestCase):
    """GET /api/admin/people/queue, and the answers Who's this? sends."""

    def setUp(self):
        super().setUp()
        import serve
        people_queue.forget()
        people_suggest.forget()
        self.addCleanup(people_queue.forget)
        self.addCleanup(people_suggest.forget)
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.visitor = self.app.test_client()
        self.visitor.environ_base["REMOTE_ADDR"] = "192.0.2.20"
        self.addCleanup(self.app.extensions["search_images"].close)
        # Ann in three more pictures (enough clear faces to be preselected), a face of
        # nobody like her, and an unnamed person of three faces like her.
        for n in range(3):
            self.picture("more-%d" % n, person_vector(1, seed=40 + n))
        people_groups.group(self.store, options())
        self.lone = self.picture("lone", person_vector(1, seed=50))
        self.lone_image = self.image["lone"]
        self.unnamed_faces = [self.picture("own-%d" % n, person_vector(1, seed=90 + n))
                              for n in range(3)]
        self.unnamed = people_groups.move_faces(self.store, self.unnamed_faces, None,
                                                "")["id"]

    def picture(self, content_hash, vector, shown=True):
        store_faces(self.store, content_hash, [face(100, 100, 200, vector)])
        if shown:
            self.image[content_hash] = insert_image(self.conn, self.library_id,
                                                    content_hash + ".jpg", content_hash)
        return self.faces_of(content_hash)[0]

    def admin(self, path, body=None, headers=None):
        sent = {"X-Search-Images": "1"}
        sent.update(headers or {})
        if body is None:
            return self.client.get("/api/admin" + path, headers=sent)
        return self.client.post("/api/admin" + path, headers=sent, json=body)

    def queue(self, headers=None):
        reply = self.admin("/people/queue", headers=headers)
        for _try in range(200):
            if reply.status_code != 202:
                break
            time.sleep(0.05)
            reply = self.admin("/people/queue", headers=headers)
        return reply

    def items(self, answer):
        return [(item["kind"], item[item["kind"]], item["to"]) for item in answer["items"]]

    def test_the_admin_gets_the_queue_by_etag(self):
        reply = self.queue()
        self.assertEqual(reply.status_code, 200)
        answer = reply.get_json()
        # The face first, then the unnamed person.
        self.assertEqual(self.items(answer), [("face", self.lone, self.ann),
                                              ("person", self.unnamed, self.ann)])
        lone, person = answer["items"]
        self.assertEqual(lone["image"], self.lone_image)
        self.assertEqual(lone["words"], people_suggest.words(lone["score"]))
        self.assertGreater(lone["lead"], 0.1)
        self.assertEqual((person["label"], person["faces"]),
                         ("Person %d" % self.unnamed, 3))
        # (Its clearest face: asked about again by it, and the merge checks it.)
        self.assertIn(person["face"], self.unnamed_faces)
        self.assertEqual(sorted(person["show"]), sorted(self.unnamed_faces))
        ann = answer["people"][str(self.ann)]
        self.assertEqual(ann["label"], "Ann")
        # (Her clearest faces to compare with: only those whose pictures are shown.)
        self.assertTrue(ann["show"])
        self.assertTrue(set(ann["show"]) <= set(people_groups.person_faces(self.store,
                                                                            self.ann)))
        self.assertEqual(answer["counts"], {"faces": 1, "persons": 1, "person_faces": 3})
        self.assertEqual(reply.headers["Cache-Control"], "private, no-cache")
        tag = reply.headers["ETag"]
        again = self.admin("/people/queue", headers={"If-None-Match": tag})
        self.assertEqual((again.status_code, again.get_data()), (304, b""))
        # The face named: the queue is another.
        self.assertEqual(self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                                      "from": None, "to": self.ann}
                                    ).status_code, 200)
        fresh = self.queue({"If-None-Match": tag})
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(self.items(fresh.get_json()), [("person", self.unnamed, self.ann)])

    def test_only_the_admin_on_this_computer(self):
        self.assertEqual(self.client.get("/api/admin/people/queue").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/people/queue", headers={
            "X-Search-Images": "1", "Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.visitor.get("/api/admin/people/queue",
                                          headers={"X-Search-Images": "1"}).status_code, 403)
        import serve
        lan = serve.build_app(init_db=False, bind_host="0.0.0.0")
        lan.testing = True
        self.addCleanup(lan.extensions["search_images"].close)
        self.assertEqual(lan.test_client().get("/api/admin/people/queue", headers={
            "X-Search-Images": "1"}).status_code, 403)

    def test_another_site_another_computer_or_lan_mode_get_nothing(self):
        # Each way in but the admin's own page on this computer is refused before the
        # queue is worked out or looked at, with nothing of it in the answer: another
        # site's page (by Sec-Fetch-Site, or by its Origin), another computer, a server
        # that listens on the network (LAN mode, even asked from this computer).
        import serve
        asked = []
        real = people_queue.get

        def counted(*args, **kwargs):
            asked.append(1)
            return real(*args, **kwargs)
        lan = serve.build_app(init_db=False, bind_host="0.0.0.0")
        lan.testing = True
        self.addCleanup(lan.extensions["search_images"].close)
        own = {"X-Search-Images": "1", "Sec-Fetch-Site": "same-origin",
               "Origin": "http://localhost"}
        refused = [
            ("cross-site", self.client, dict(own, **{"Sec-Fetch-Site": "cross-site"})),
            ("same-site", self.client, dict(own, **{"Sec-Fetch-Site": "same-site"})),
            ("foreign origin", self.client, dict(own, Origin="http://attacker.example")),
            ("foreign origin, no Sec-Fetch-Site", self.client,
             {"X-Search-Images": "1", "Origin": "http://attacker.example"}),
            ("another port", self.client, dict(own, Origin="http://localhost:8081")),
            ("opaque origin", self.client, dict(own, Origin="null")),
            ("another computer", self.visitor, own),
            ("LAN mode", lan.test_client(), own),
        ]
        with mock.patch.object(people_queue, "get", side_effect=counted):
            for name, client, headers in refused:
                reply = client.get("/api/admin/people/queue", headers=headers)
                self.assertEqual(reply.status_code, 403, name)
                self.assertNotIn("items", reply.get_json(), name)
                self.assertNotIn("ETag", reply.headers, name)
            self.assertEqual(asked, [])
            # (The admin's own page is let in.)
            for _try in range(200):
                reply = self.client.get("/api/admin/people/queue", headers=own)
                if reply.status_code != 202:
                    break
                time.sleep(0.05)
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(asked)
        self.assertEqual(self.items(reply.get_json()), [("face", self.lone, self.ann),
                                                        ("person", self.unnamed, self.ann)])
        # Nor are the answers and Undo taken from another site's page.
        before = state_of(self.store)
        answer = {"action": "move", "faces": [self.lone], "from": None, "to": self.ann,
                  "undo": True}
        for headers in (dict(own, **{"Sec-Fetch-Site": "cross-site"}),
                        dict(own, Origin="http://attacker.example")):
            self.assertEqual(self.client.post("/api/admin/people/faces", headers=headers,
                                              json=answer).status_code, 403, headers)
            self.assertEqual(self.client.post("/api/admin/people/undo", headers=headers,
                                              json={"undo": "0" * 24}).status_code, 403,
                             headers)
        self.assertEqual(self.visitor.post("/api/admin/people/faces", headers=own,
                                           json=answer).status_code, 403)
        self.assertEqual(state_of(self.store), before)

    def test_while_it_is_worked_out_the_page_is_told_to_ask_again(self):
        release = threading.Event()
        real = people_queue.build_marks

        builds = []

        def slow(store, config, use_numpy=None, stop=None):
            builds.append(1)
            release.wait(10)
            return real(store, config, use_numpy, stop)
        with mock.patch.object(people_queue, "build_marks", slow), \
                mock.patch.object(people_queue, "QUEUE_WAIT", 0.05):
            waiting = self.admin("/people/queue")
            self.assertEqual(waiting.status_code, 202)
            number = waiting.get_json()["build"]
            self.assertIsInstance(number, int)
            self.assertEqual(waiting.get_json(), {"building": True, "build": number,
                                                  "items": []})
            self.assertEqual(waiting.headers["Retry-After"], "1")
            self.assertNotIn("ETag", waiting.headers)
            # (One at a time: asked again, the same is waited for.)
            again = self.admin("/people/queue?build=%d" % number)
            self.assertEqual((again.status_code, again.get_json()["build"]), (202, number))
            self.assertEqual(self.admin("/people/queue").get_json()["build"], number)
            release.set()
            reply = self.queue()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(reply.get_json()["items"]), 2)
        self.assertEqual(len(builds), 1)
        for bad in ("x", "-1", "1.5", "9" * 19):
            self.assertEqual(self.admin("/people/queue?build=" + bad).status_code, 400, bad)
        # (A number of another run of the server: as if none.)
        self.assertEqual(self.admin("/people/queue?build=999999").status_code, 200)

    def test_the_queue_comes_while_the_faces_keep_changing(self):
        # While faces are being found the face data changes every few seconds -- sooner
        # than a build without numpy ends. The page asks again with the number of the
        # build it was told: it gets that build's queue (or a later one's).
        real = people_queue.build_marks
        builds = []

        def slow(store, config, use_numpy=None, stop=None):
            builds.append(1)
            time.sleep(0.4)
            return real(store, config, use_numpy, stop)
        stop = threading.Event()

        def finder():
            store = people.connect()
            try:
                while not stop.wait(0.1):
                    people._bump(store)
                    store.commit()
            finally:
                store.close()
        writer = threading.Thread(target=finder, daemon=True)
        with mock.patch.object(people_queue, "build_marks", slow), \
                mock.patch.object(people_queue, "QUEUE_WAIT", 0.1):
            writer.start()
            try:
                reply, number, polls = None, 0, 0
                while polls < 40:
                    polls += 1
                    reply = self.admin("/people/queue" + ("?build=%d" % number if number
                                                          else ""))
                    if reply.status_code != 202:
                        break
                    number = reply.get_json()["build"]
                    time.sleep(0.1)
            finally:
                stop.set()
                writer.join()
        self.assertEqual(reply.status_code, 200, polls)
        self.assertEqual(len(reply.get_json()["items"]), 2)
        # (Within a build or two, not one after another for as long as it changes.)
        self.assertLessEqual(len(builds), 3)

    def test_a_face_whose_picture_is_not_shown_is_left_out(self):
        hidden_face = self.picture("elsewhere", person_vector(1, seed=55), shown=False)
        marks = people_queue.build_marks(self.store, self.config)
        self.assertIn(hidden_face, [item["face"] for item in marks["faces"]])
        self.assertNotIn(hidden_face, [item[item["kind"]] for item in
                                       self.queue().get_json()["items"]])

    def test_no_face_data(self):
        self.store.close()
        people.delete_all()
        reply = self.admin("/people/queue")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json()["items"], [])
        self.assertNotIn("ETag", reply.headers)
        self.store = people.connect()

    def test_a_face_a_grouping_moved_meanwhile_is_not_answered(self):
        before = state_of(self.store)
        # The queue showed the face of nobody; meanwhile it went into an unnamed person.
        people_groups.move_faces(self.store, [self.lone], self.unnamed)
        moved = state_of(self.store)
        for body in ({"action": "move", "faces": [self.lone], "to": self.ann},
                     {"action": "reject", "faces": [self.lone], "person": self.ann},
                     {"action": "not_face", "faces": [self.lone]}):
            for undo in (True, False):
                sent = dict(body, undo=undo)
                sent["from"] = None
                reply = self.admin("/people/faces", sent)
                self.assertEqual(reply.status_code, 409, sent)
                self.assertIn("not where it was", reply.get_json()["error"])
                # (Nothing said: not "not Person 12", nor Ann's.)
                self.assertEqual(state_of(self.store), moved)
                self.assertFalse(self.store.in_transaction)
        self.assertNotEqual(moved, before)
        # Where it is now, it is answered.
        reply = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "from": self.unnamed, "to": self.ann})
        self.assertEqual(reply.status_code, 200)
        # A face marked "not a face" meanwhile is no face of nobody either.
        other = self.picture("marked", person_vector(1, seed=56))
        people_groups.mark_not_face(self.store, [other])
        self.assertEqual(self.admin("/people/faces", {"action": "move", "faces": [other],
                                                      "from": None, "to": self.ann}
                                    ).status_code, 409)
        for bad in ("x", True, 0, -1, 1.5):
            self.assertEqual(self.admin("/people/faces", {"action": "move", "faces": [other],
                                                          "from": bad, "to": self.ann}
                                        ).status_code, 400, bad)

    def test_not_ann_for_a_whole_unnamed_person(self):
        before = state_of(self.store)
        reply = self.admin("/people/faces", {"action": "reject", "faces": "all",
                                             "from": self.unnamed, "person": self.ann,
                                             "undo": True})
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        # Its faces stay its own, and are never offered as Ann's again.
        self.assertEqual(people_groups.person_faces(self.store, self.unnamed),
                         sorted(self.unnamed_faces))
        self.assertEqual(sorted(row[0] for row in self.store.execute(
            "SELECT face_id FROM face_rejects WHERE person_id = ?", (self.ann,))),
            sorted(self.unnamed_faces))
        self.assertIsNone(people_suggest.suggest_person(self.store, self.unnamed,
                                                        CONFIG)["preselect"])
        self.assertEqual(self.items(self.queue().get_json()), [("face", self.lone, self.ann)])
        undone = self.admin("/people/undo", {"undo": reply.get_json()["undo"]})
        self.assertEqual(undone.status_code, 200)
        self.assertEqual(state_of(self.store), before)
        # A person that went meanwhile (merged elsewhere): nothing to answer.
        people_groups.merge(self.store, self.unnamed, self.bob)
        gone = self.admin("/people/faces", {"action": "reject", "faces": "all",
                                            "from": self.unnamed, "person": self.ann})
        self.assertEqual(gone.status_code, 404)

    def test_a_person_a_grouping_changed_meanwhile_is_not_merged(self):
        # The queue asked "Is Person 12 Ann?" by its clearest face; meanwhile that face
        # went to another person: the merge is refused.
        best = self.queue().get_json()["items"][1]["face"]
        people_groups.move_faces(self.store, [best], self.bob)
        refused = self.admin("/people/%d/merge" % self.unnamed, {"into": self.ann,
                                                                  "face": best, "undo": True})
        self.assertEqual(refused.status_code, 409)

    def test_an_answer_about_a_person_that_changed_since_it_was_shown_is_refused(self):
        item = self.queue().get_json()["items"][1]
        self.assertEqual((item["kind"], item["seen"]),
                         ("person", people_queue.person_seen(self.store, self.unnamed)))
        start = state_of(self.store)
        not_ann = {"action": "reject", "faces": "all", "from": item["person"],
                   "person": item["to"], "seen": item["seen"], "undo": True}
        yes_ann = {"into": item["to"], "face": item["face"], "seen": item["seen"],
                   "undo": True}
        # Meanwhile (a grouping, another tab) other faces joined it and those shown left.
        extra = [self.picture("extra-%d" % n, person_vector(1, seed=120 + n))
                 for n in range(2)]
        people_groups.move_faces(self.store, extra, self.unnamed)
        people_groups.move_faces(self.store, self.unnamed_faces[1:], self.bob)
        changed = state_of(self.store)
        self.assertNotEqual(changed, start)
        for path, sent in (("/people/faces", not_ann),
                           ("/people/%d/merge" % self.unnamed, yes_ann),
                           ("/people/%d/merge" % self.unnamed, dict(yes_ann, undo=False))):
            reply = self.admin(path, sent)
            self.assertEqual(reply.status_code, 409, (path, sent))
            self.assertIn("changed since it was shown", reply.get_json()["error"])
            # (Nothing said: not "not Ann" of faces never shown, no merge.)
            self.assertEqual(state_of(self.store), changed)
            self.assertFalse(self.store.in_transaction)
        # Named or hidden meanwhile: refused too.
        for change in ({"name": "Carl"}, {"hidden": True}):
            fresh = self.queue().get_json()["items"]
            self.assertEqual(fresh[-1]["person"], self.unnamed)
            seen = fresh[-1]["seen"]
            people_groups.update_person(self.store, self.unnamed, change)
            after = state_of(self.store)
            for path, sent in (("/people/faces", dict(not_ann, seen=seen)),
                               ("/people/%d/merge" % self.unnamed, dict(yes_ann, seen=seen))):
                self.assertEqual(self.admin(path, sent).status_code, 409, (change, path))
                self.assertEqual(state_of(self.store), after)
            people_groups.update_person(self.store, self.unnamed,
                                        {"name": "", "hidden": False})
        # As it was shown, it is answered.
        seen = people_queue.person_seen(self.store, self.unnamed)
        reply = self.admin("/people/%d/merge" % self.unnamed, dict(yes_ann, seen=seen))
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        for bad in (1, True, "", "x" * 65):
            self.assertEqual(self.admin("/people/faces", dict(not_ann, seen=bad)).status_code,
                             400, bad)
        # (seen is said of the person in from.)
        self.assertEqual(self.admin("/people/faces", {"action": "reject", "faces": [self.lone],
                                                      "person": self.ann, "from": None,
                                                      "seen": seen}).status_code, 400)

    def test_the_suggestion_says_when_an_unnamed_person_is_hidden(self):
        item = self.queue().get_json()["items"][1]
        asked = self.admin("/people/suggest?face=%d" % item["face"]).get_json()
        self.assertEqual((asked["person"]["id"], asked["person"]["hidden"]),
                         (self.unnamed, False))
        people_groups.update_person(self.store, self.unnamed, {"hidden": True})
        asked = self.admin("/people/suggest?face=%d" % item["face"]).get_json()
        # (The page passes it over: a stranger the owner hid.)
        self.assertTrue(asked["person"]["hidden"])
        self.assertEqual(self.items(self.queue().get_json()), [("face", self.lone, self.ann)])

    def test_not_ann_for_a_large_unnamed_person_can_be_undone(self):
        many = [self.picture("big-%d" % n, person_vector(1, seed=300 + n))
                for n in range(people_groups.UNDO_FACES + 5)]
        people_groups.move_faces(self.store, many, self.unnamed)
        before = state_of(self.store)
        reply = self.admin("/people/faces", {
            "action": "reject", "faces": "all", "from": self.unnamed, "person": self.ann,
            "seen": people_queue.person_seen(self.store, self.unnamed), "undo": True})
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        token = reply.get_json()["undo"]
        self.assertIsInstance(token, str)
        self.assertEqual(len(people_groups.person_faces(self.store, self.unnamed)),
                         people_groups.UNDO_FACES + 8)
        self.assertEqual(self.admin("/people/undo", {"undo": token}).status_code, 200)
        self.assertEqual(state_of(self.store), before)

    def test_an_undo_after_a_later_change_keeps_the_earlier_ones(self):
        first = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "from": None, "to": self.ann, "undo": True})
        # (Another tab, or the viewer, then makes a change of its own with undo.)
        later = self.admin("/people/%d" % self.bob, {"hidden": True, "undo": True})
        tokens = [first.get_json()["undo"], later.get_json()["undo"]]
        refused = self.admin("/people/undo", {"undo": tokens[0]})
        self.assertEqual(refused.status_code, 409)
        # (Kept: the page keeps its answers, they can be undone once the later one is.)
        self.assertIs(refused.get_json().get("kept"), True)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[1]}).status_code, 200)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[0]}).status_code, 200)
        self.assertIsNone(people_groups.face_person(self.store, self.lone))
        # A token gone for good: no "kept".
        gone = self.admin("/people/undo", {"undo": tokens[0]})
        self.assertEqual(gone.status_code, 409)
        self.assertNotIn("kept", gone.get_json())

    def test_a_later_change_that_can_no_longer_be_undone_lets_the_answers_go(self):
        answer = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                              "from": None, "to": self.ann, "undo": True})
        token = answer.get_json()["undo"]
        # (Then the viewer names Person 12 as Ann: undo, but no "from" or "seen".)
        viewer = self.admin("/people/%d/merge" % self.unnamed,
                            {"into": self.ann, "face": self.unnamed_faces[0], "undo": True})
        self.assertIsInstance(viewer.get_json()["undo"], str)
        # While the viewer offers its Undo, the answer is kept.
        refused = self.admin("/people/undo", {"undo": token})
        self.assertEqual(refused.status_code, 409)
        self.assertIs(refused.get_json().get("kept"), True)
        # A grouping then: neither can be undone any more, and the page is told so.
        people_groups.group(self.store, options())
        refused = self.admin("/people/undo", {"undo": token})
        self.assertEqual(refused.status_code, 409)
        self.assertNotIn("kept", refused.get_json())
        self.assertIn("changed since then", refused.get_json()["error"])
        gone = self.admin("/people/undo", {"undo": viewer.get_json()["undo"]})
        self.assertEqual(gone.status_code, 409)
        self.assertNotIn("kept", gone.get_json())

    def test_an_answer_below_a_viewer_change_whose_undo_has_gone_is_let_go(self):
        answer = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                              "from": None, "to": self.ann, "undo": True})
        # (The viewer's change, its message with Undo gone already.)
        with mock.patch.object(admin, "UNDO_VIEWER_SECONDS", -1):
            viewer = self.admin("/people/%d" % self.bob, {"hidden": True, "undo": True})
        self.assertIsInstance(viewer.get_json()["undo"], str)
        refused = self.admin("/people/undo", {"undo": answer.get_json()["undo"]})
        self.assertEqual(refused.status_code, 409)
        self.assertNotIn("kept", refused.get_json())
        self.assertIn("viewer", refused.get_json()["error"])
        self.assertEqual(people_groups.face_person(self.store, self.lone), self.ann)
        # The next answer can be undone again.
        unnamed = people_queue.person_seen(self.store, self.unnamed)
        merged = self.admin("/people/%d/merge" % self.unnamed,
                            {"into": self.ann, "seen": unnamed, "undo": True})
        self.assertEqual(self.admin("/people/undo", {"undo": merged.get_json()["undo"]})
                         .status_code, 200)

    def test_the_later_change_undone_while_an_undo_is_judged_keeps_the_answer(self):
        answer = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                              "from": None, "to": self.ann, "undo": True})
        with mock.patch.object(admin, "UNDO_VIEWER_SECONDS", -1):
            viewer = self.admin("/people/%d" % self.bob, {"hidden": True, "undo": True})
        real = people_groups.restorable
        undone = []

        def meanwhile(store, snap):
            if not undone:
                # (The viewer's change is undone just then, from another tab.)
                undone.append(None)
                undone[0] = self.admin("/people/undo",
                                       {"undo": viewer.get_json()["undo"]}).status_code
            return real(store, snap)
        with mock.patch.object(people_groups, "restorable", side_effect=meanwhile):
            refused = self.admin("/people/undo", {"undo": answer.get_json()["undo"]})
        self.assertEqual(undone, [200])
        self.assertEqual(refused.status_code, 409)
        # (Kept: asked again, it is the last change now.)
        self.assertIs(refused.get_json().get("kept"), True)
        self.assertEqual(self.admin("/people/undo", {"undo": answer.get_json()["undo"]})
                         .status_code, 200)
        self.assertIsNone(people_groups.face_person(self.store, self.lone))

    def test_answers_another_tab_gave_later_keep_the_earlier_ones(self):
        # (However long ago: that tab keeps its answers to undo.)
        with mock.patch.object(admin, "UNDO_VIEWER_SECONDS", -1):
            first = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                                 "from": None, "to": self.ann, "undo": True})
            other = self.admin("/people/faces", {
                "action": "reject", "faces": "all", "from": self.unnamed, "person": self.ann,
                "seen": people_queue.person_seen(self.store, self.unnamed), "undo": True})
        tokens = [first.get_json()["undo"], other.get_json()["undo"]]
        refused = self.admin("/people/undo", {"undo": tokens[0]})
        self.assertEqual(refused.status_code, 409)
        self.assertIs(refused.get_json().get("kept"), True)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[1]}).status_code, 200)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[0]}).status_code, 200)
        self.assertIsNone(people_groups.face_person(self.store, self.lone))

    def test_answers_are_undone_one_after_another(self):
        before = state_of(self.store)
        named = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "from": None, "to": self.ann, "undo": True})
        after_first = state_of(self.store)
        merged = self.admin("/people/%d/merge" % self.unnamed,
                            {"into": self.ann, "face": self.unnamed_faces[0], "undo": True})
        after_second = state_of(self.store)
        not_face = self.picture("thing", person_vector(1, seed=57))
        marked = self.admin("/people/faces", {"action": "not_face", "faces": [not_face],
                                              "from": None, "undo": True})
        tokens = [reply.get_json()["undo"] for reply in (named, merged, marked)]
        self.assertTrue(all(isinstance(token, str) for token in tokens))
        # Only the last can be undone (an earlier one is refused, and nothing is lost).
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[0]}).status_code, 409)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[2]}).status_code, 200)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[2]}).status_code, 409)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[1]}).status_code, 200)
        self.assertEqual(state_of(self.store), after_first)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[0]}).status_code, 200)
        self.assertEqual(state_of(self.store), before)
        self.assertNotEqual(after_second, after_first)

    def test_a_change_in_between_takes_the_earlier_answers_away(self):
        first = self.admin("/people/faces", {"action": "move", "faces": [self.lone],
                                             "from": None, "to": self.ann, "undo": True})
        second = self.admin("/people/%d/merge" % self.unnamed, {"into": self.ann,
                                                                 "undo": True})
        # (Another tab hides Bob: a change made without undo.)
        self.admin("/people/%d" % self.bob, {"hidden": True})
        refused = self.admin("/people/undo", {"undo": second.get_json()["undo"]})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("changed since then", refused.get_json()["error"])
        self.assertEqual(self.admin("/people/undo", {"undo": first.get_json()["undo"]})
                         .status_code, 409)
        self.assertEqual(people_groups.face_person(self.store, self.lone), self.ann)
        # A grouping in between too: then a new answer can be undone again.
        third = self.admin("/people/faces", {"action": "not_face", "faces": [self.lone],
                                             "undo": True}).get_json()["undo"]
        people_groups.group(self.store, options())
        self.assertEqual(self.admin("/people/undo", {"undo": third}).status_code, 409)

    def test_undo_keeps_the_last_steps(self):
        faces = [self.picture("step-%d" % n, person_vector(1, seed=60 + n)) for n in range(3)]
        with mock.patch.object(admin, "UNDO_STEPS", 2):
            tokens = [self.admin("/people/faces", {"action": "not_face", "faces": [face_id],
                                                   "from": None, "undo": True}
                                 ).get_json()["undo"] for face_id in faces]
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[2]}).status_code, 200)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[1]}).status_code, 200)
        self.assertEqual(self.admin("/people/undo", {"undo": tokens[0]}).status_code, 409)
        self.assertEqual([people_groups.faces_in(self.store, [face_id], None)
                          for face_id in faces], [False, True, True])

    def test_delete_face_data_forgets_the_queue(self):
        self.assertEqual(len(self.queue().get_json()["items"]), 2)
        self.assertIsNotNone(people_queue._kept["marks"])
        queued = self.admin("/jobs", {"kind": "delete_faces", "params": {"confirm": True}})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        self.assertIsNone(people_queue._kept["marks"])

    def test_delete_face_data_stops_a_build_on_its_way(self):
        # A build running when Delete face data is asked for does not keep the names it
        # read (it stops at its next pause, and lets go of the file).
        self.lone_too = self.picture("lone-too", person_vector(1, seed=51))
        started, release = threading.Event(), threading.Event()
        compared = []
        real = people_suggest._closeness

        def closeness(index, queries):
            compared.append(len(queries))
            if len(compared) == 1:
                started.set()
                release.wait(10)
            return real(index, queries)
        with mock.patch.object(people_suggest, "_closeness", closeness), \
                mock.patch.object(people_queue, "BATCH", 1), \
                mock.patch.object(people_queue, "PURE_BATCH", 1), \
                mock.patch.object(people_queue, "QUEUE_WAIT", 0.05):
            self.assertEqual(self.admin("/people/queue").status_code, 202)
            self.assertTrue(started.wait(10))
            queued = self.admin("/jobs", {"kind": "delete_faces",
                                          "params": {"confirm": True}})
            self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
            release.set()
            for _try in range(200):
                if people_queue._work["build"] is None:
                    break
                time.sleep(0.02)
        self.assertIsNone(people_queue._work["build"])
        self.assertEqual((people_queue._kept["key"], people_queue._kept["marks"]),
                         (None, None))
        # (Stopped after the batch it was in: the other face of nobody was not compared.)
        self.assertEqual(len(compared), 1)

    def test_nothing_of_the_queue_goes_into_an_export(self):
        names = set(export.ASSETS) | set(getattr(export, "MAP_ASSETS", ()))
        self.assertNotIn("people.js", names)
        self.assertNotIn("people.css", names)
        for name in ("export.py", "trip_export.py"):
            with open(os.path.join(support.REPO, name), encoding="utf-8") as handle:
                self.assertNotIn("people_queue", handle.read(), name)


@unittest.skipIf(shutil.which("node") is None, "Node.js is not installed")
class NodeTest(unittest.TestCase):

    def test_the_page_walks_the_queue(self):
        # (The real page scripts on the made-up DOM of tests/js/app_meaning_test.js.)
        result = subprocess.run([shutil.which("node"), JS_TESTS], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)


if __name__ == "__main__":
    unittest.main()
