"""Tests for the suggestions of who a face is (people_suggest.py): the named people a
face most likely is, closest first (only those at least "a little like" it are offered),
and the one preselected when it is clearly ahead; what is never offered ("not this
person", "not a face", a face of a named person) and what comes last (a person already
in the picture); hidden people; faces put in a person by hand counted by their quality
only; a face of an unnamed person answered by what that person most likely is ("Name
Person 12 as X?", never for a person the grouping mixed up), the admin's route (GET
/api/admin/people/suggest: ETag, who may ask, nothing kept after Delete face data), and
the merge the viewer makes of it, which can be undone (POST
/api/admin/people/<id>/merge with "undo": true).

The faces are made up: vectors near one axis per made-up person (no photo of anyone).
Every test runs in a temporary SETTINGS_BASE.
"""

import json
import math
import unittest
from unittest import mock

import support

import common
import people
import people_groups
import people_suggest
from test_face_names import state_of
from test_people_groups import (NOW, StoreMixin, ViewsMixin, face, insert_image, options,
                                person_vector, store_faces, unit)

try:
    import flask
except ImportError:
    flask = None

CONFIG = {"people": {"enabled": True}}


def axis(k):
    values = [0.0] * people.EMBEDDING_DIMS
    values[k] = 1.0
    return values


def blend(first, second, weight):
    """first and second (unit vectors) mixed: weight of first, the rest of second."""
    other = math.sqrt(max(0.0, 1.0 - weight * weight))
    return unit([weight * x + other * y for x, y in zip(first, second)])


def mix(weights):
    """A unit vector of so much of each made-up person's axis: {k: weight}."""
    values = [0.0] * people.EMBEDDING_DIMS
    for k, weight in weights.items():
        values[k] = weight
    return unit(values)


class SuggestMixin(StoreMixin):

    def setUp(self):
        super().setUp()
        # Ann (6 pictures), Bob (6), and a third made-up person (4), grouped.
        self.made = self.add_people([6, 6, 4])
        people_groups.group(self.store, options(), now=NOW)
        self.ann = self.person_of(self.made[0][0])
        self.bob = self.person_of(self.made[1][0])
        self.cy = self.person_of(self.made[2][0])
        people_groups.update_person(self.store, self.ann, {"name": "Ann"}, now=NOW)
        people_groups.update_person(self.store, self.bob, {"name": "Bob"}, now=NOW)

    def lone(self, content_hash, vector, sharpness=400.0):
        """A picture with one face (of nobody: not grouped): its face id. A sharpness
        of 6: a face of a quality under group_quality (small, blurred, turned away)."""
        store_faces(self.store, content_hash, [face(100, 100, 200, vector,
                                                    sharpness=sharpness)])
        return self.faces_of(content_hash)[0]

    def quality(self, face_id):
        return self.store.execute("SELECT quality FROM faces WHERE id = ?",
                                  (face_id,)).fetchone()[0]

    def suggest(self, face_id, **options):
        return people_suggest.suggest(self.store, face_id, CONFIG, **options)

    def labels(self, answer):
        return [item["label"] for item in answer["people"]]

    def scores(self, answer):
        return {item["label"]: item["score"] for item in answer["people"]}


class FaceTest(SuggestMixin, unittest.TestCase):

    def test_a_face_like_one_named_person_is_preselected(self):
        face_id = self.lone("x", person_vector(1, seed=50))
        answer = self.suggest(face_id)
        self.assertEqual((answer["face"], answer["basis"], answer["preselect"]),
                         (face_id, "face", self.ann))
        # Only the named people at least "a little like" it (Bob is not), closest first.
        self.assertEqual(self.labels(answer), ["Ann"])
        first = answer["people"][0]
        self.assertEqual((first["id"], first["name"], first["faces"], first["hidden"],
                          first["in_picture"], first["words"]),
                         (self.ann, "Ann", 6, False, False, "very like"))
        self.assertGreater(first["score"], 0.7)
        self.assertIn(first["cover"], people_groups.person_faces(self.store, self.ann))
        # (limit 0: every named person, however far: to measure them.)
        every = self.suggest(face_id, limit=0)
        self.assertEqual(self.labels(every), ["Ann", "Bob"])
        self.assertEqual(every["people"][1]["words"], "hardly like")
        self.assertLess(every["people"][1]["score"], people_suggest.SHOWN_FROM)

    def test_nothing_is_offered_for_a_face_like_nobody(self):
        face_id = self.lone("x", axis(60))
        self.assertEqual(self.suggest(face_id), {"face": face_id, "basis": "face",
                                                 "people": [], "preselect": None})
        self.assertEqual(self.labels(self.suggest(face_id, limit=0)), ["Ann", "Bob"])

    def test_only_one_clearly_ahead_is_preselected(self):
        ann_face, bob_face = person_vector(1, seed=50), person_vector(2, seed=50)
        cases = {
            # As like Bob as Ann: both listed, neither preselected.
            "both": (blend(ann_face, bob_face, math.sqrt(0.5)), None),
            # Like Ann, but not enough (the closeness is under FACE_SCORE).
            "faint": (blend(ann_face, axis(60), 0.6), None),
            "clear": (blend(ann_face, axis(60), 0.7), self.ann),
        }
        for name, (vector, wanted) in cases.items():
            answer = self.suggest(self.lone(name, vector), limit=0)
            self.assertEqual(answer["preselect"], wanted, name)
            first, second = answer["people"][0]["score"], answer["people"][1]["score"]
            ahead = (first >= people_suggest.FACE_SCORE and
                     first - second >= people_suggest.FACE_LEAD)
            self.assertEqual(ahead, wanted is not None, name)
        both = self.suggest(self.faces_of("both")[0])
        self.assertEqual(sorted(self.labels(both)), ["Ann", "Bob"])

    def test_not_this_person_is_never_offered_back(self):
        face_id = self.lone("x", person_vector(1, seed=50))
        people_groups.reject_faces(self.store, [face_id], self.ann, now=NOW)
        answer = self.suggest(face_id, limit=0)
        self.assertEqual(self.labels(answer), ["Bob"])
        self.assertIsNone(answer["preselect"])

    def test_the_lead_counts_the_people_it_was_said_not_to_be(self):
        # Like Ann, and Bob nearly as much: a look-alike. "Not Ann" does not make it Bob.
        face_id = self.lone("x", blend(person_vector(1, seed=50), person_vector(2, seed=50),
                                       0.72))
        before = self.suggest(face_id, limit=0)
        self.assertEqual(self.labels(before), ["Ann", "Bob"])
        self.assertGreaterEqual(self.scores(before)["Bob"], people_suggest.FACE_SCORE)
        self.assertIsNone(before["preselect"])
        people_groups.reject_faces(self.store, [face_id], self.ann, now=NOW)
        after = self.suggest(face_id)
        self.assertEqual(self.labels(after), ["Bob"])
        self.assertIsNone(after["preselect"])

    def test_nothing_for_not_a_face_a_named_face_or_none(self):
        face_id = self.lone("x", person_vector(1, seed=50))
        people_groups.mark_not_face(self.store, [face_id], now=NOW)
        self.assertEqual(self.suggest(face_id), {"face": face_id, "basis": "not_face",
                                                 "people": [], "preselect": None})
        named = self.faces_of(self.made[0][0])[0]
        self.assertEqual(self.suggest(named), {"face": named, "basis": "named", "people": [],
                                               "preselect": None})
        self.assertEqual(self.suggest(999999)["basis"], "missing")

    def test_hidden_people_are_listed_never_preselected(self):
        people_groups.update_person(self.store, self.ann, {"hidden": True}, now=NOW)
        answer = self.suggest(self.lone("x", person_vector(1, seed=50)))
        self.assertEqual(self.labels(answer), ["Ann"])
        self.assertIs(answer["people"][0]["hidden"], True)
        self.assertIsNone(answer["preselect"])

    def test_a_person_already_in_the_picture_comes_after_the_others(self):
        # Two faces like Ann in one picture (a mirror): one joins Ann, the other cannot.
        store_faces(self.store, "mirror", [face(100, 100, 200, person_vector(1, seed=60)),
                                           face(500, 100, 200, person_vector(1, seed=61))])
        people_groups.group(self.store, options(), now=NOW)
        ids = self.faces_of("mirror")
        outside = [face_id for face_id in ids
                   if people_groups.face_person(self.store, face_id) is None]
        self.assertEqual(len(outside), 1)
        answer = self.suggest(outside[0], limit=0)
        self.assertEqual(self.labels(answer), ["Bob", "Ann"])
        self.assertEqual([item["in_picture"] for item in answer["people"]], [False, True])
        self.assertGreater(answer["people"][1]["score"], answer["people"][0]["score"])
        self.assertIsNone(answer["preselect"])
        # (Shown: Ann alone, the only one like it -- in the picture, never marked.)
        shown = self.suggest(outside[0])
        self.assertEqual((self.labels(shown), shown["preselect"]), (["Ann"], None))

    def test_a_close_person_in_the_picture_keeps_its_place_among_those_shown(self):
        # Four named people; the face is most like Ann, who is in its picture too.
        self.add_people([0, 0, 0, 4], seed=7, prefix="dee")
        people_groups.group(self.store, options(), now=NOW)
        dee = self.person_of("dee-3-0-7")
        people_groups.update_person(self.store, self.cy, {"name": "Cy"}, now=NOW)
        people_groups.update_person(self.store, dee, {"name": "Dee"}, now=NOW)
        store_faces(self.store, "mirror", [face(100, 100, 200, person_vector(1, seed=60)),
                                           face(500, 100, 200, mix({1: 0.8, 2: 0.6,
                                                                    3: 0.55, 4: 0.3}))])
        ann_face, face_id = self.faces_of("mirror")
        people_groups.move_faces(self.store, [ann_face], target_id=self.ann, now=NOW)
        every = self.suggest(face_id, limit=0)
        self.assertEqual(self.labels(every), ["Bob", "Cy", "Dee", "Ann"])
        scores = self.scores(every)
        self.assertGreater(scores["Ann"], scores["Bob"])
        self.assertGreater(scores["Cy"], max(scores["Dee"], people_suggest.SHOWN_FROM))
        # The three closest are shown (not Dee), Ann last of them and not marked.
        shown = self.suggest(face_id)
        self.assertEqual(self.labels(shown), ["Bob", "Cy", "Ann"])
        self.assertEqual([item["in_picture"] for item in shown["people"]],
                         [False, False, True])
        self.assertIsNone(shown["preselect"])

    def test_a_named_person_of_few_clear_faces_is_listed_not_preselected(self):
        people_groups.update_person(self.store, self.cy, {"name": "Cy"}, now=NOW)
        answer = self.suggest(self.lone("x", person_vector(3, seed=50)))
        self.assertEqual(self.labels(answer), ["Cy"])
        self.assertEqual(answer["people"][0]["words"], "very like")
        self.assertLess(answer["people"][0]["faces"], people_suggest.NEAREST)
        self.assertIsNone(answer["preselect"])
        # (At most three are shown first; limit 0: all, 1: one.)
        face_id = self.faces_of("x")[0]
        self.assertEqual(len(self.suggest(face_id, limit=0)["people"]), 3)
        self.assertEqual(len(self.suggest(face_id, limit=1)["people"]), 1)

    def test_faces_put_in_a_person_by_hand_count_by_their_quality_only(self):
        # Poor faces (blurred, turned away...) are like one another more than like anyone.
        # Taken into Ann by hand (as Enter on a suggestion does), they do not make her
        # the likeliest for the next poor face of a stranger: Ann is measured by her clear
        # faces only.
        poor = [self.lone("poor-%d" % n, person_vector(70, seed=n), sharpness=6.0)
                for n in range(6)]
        self.assertLess(self.quality(poor[0]), people.settings(CONFIG)["group_quality"])
        people_groups.move_faces(self.store, poor, target_id=self.ann, now=NOW)
        stranger = self.lone("stranger", person_vector(70, seed=99), sharpness=6.0)
        answer = self.suggest(stranger, limit=0)
        self.assertIsNone(answer["preselect"])
        self.assertLess(self.scores(answer)["Ann"], people_suggest.SHOWN_FROM)
        self.assertEqual(self.suggest(stranger)["people"], [])
        # Ann's own faces are still hers.
        self.assertEqual(self.suggest(self.lone("x", person_vector(1, seed=50)))["preselect"],
                         self.ann)
        # A named person of poor faces only is measured by them all: listed, never marked.
        dee = people_groups.move_faces(self.store, poor[:5], None, "Dee", now=NOW)["id"]
        answer = self.suggest(stranger)
        self.assertEqual(self.labels(answer), ["Dee"])
        self.assertEqual(answer["people"][0]["words"], "very like")
        self.assertIsNone(answer["preselect"])
        self.assertEqual(people_groups.person_info(self.store, dee)["faces"], 5)

    def test_words(self):
        self.assertEqual([people_suggest.words(value) for value in (0.9, 0.55, 0.5, 0.45, 0.4,
                                                                    0.35, 0.2, -0.1)],
                         ["very like", "very like", "like", "like", "a little like",
                          "a little like", "hardly like", "hardly like"])
        # (Of the closeness as shown, to two places.)
        self.assertEqual([people_suggest.words(value) for value in (0.547, 0.449, 0.3449)],
                         ["very like", "like", "hardly like"])

    def test_the_answers_follow_every_change(self):
        face_id = self.lone("x", person_vector(2, seed=50))
        options_now = people.settings(CONFIG)
        index = people_suggest._index(self.store, options_now)
        self.assertIs(people_suggest._index(self.store, options_now), index)
        tag = people_suggest.etag(self.store, CONFIG, face_id)
        self.assertEqual(people_suggest.etag(self.store, CONFIG, face_id), tag)
        self.assertNotEqual(people_suggest.etag(self.store, CONFIG, face_id + 1), tag)
        self.assertEqual(self.suggest(face_id)["people"][0]["label"], "Bob")
        people_groups.update_person(self.store, self.bob, {"name": "Bo"}, now=NOW)
        self.assertIsNot(people_suggest._index(self.store, options_now), index)
        self.assertEqual(self.suggest(face_id)["people"][0]["label"], "Bo")
        self.assertNotEqual(people_suggest.etag(self.store, CONFIG, face_id), tag)
        # (The faces measured against follow "group_quality": another ETag.)
        other = {"people": {"enabled": True, "group_quality": 0.6}}
        self.assertNotEqual(people_suggest.etag(self.store, other, face_id),
                            people_suggest.etag(self.store, CONFIG, face_id))
        # Forgotten (Delete face data): read again.
        people_suggest.forget()
        self.assertIsNone(people_suggest._cache["index"])
        self.assertEqual(self.suggest(face_id)["people"][0]["label"], "Bo")

    def test_numpy_and_plain_python_agree(self):
        if people_groups._numpy() is None:
            self.skipTest("numpy is not installed")
        ids = [self.lone("x%d" % n, blend(person_vector(1 + n % 2, seed=70 + n), axis(60),
                                          0.5 + 0.1 * n)) for n in range(4)]
        for face_id in ids:
            fast = self.suggest(face_id, limit=0, use_numpy=True)
            slow = self.suggest(face_id, limit=0, use_numpy=False)
            self.assertEqual(self.labels(fast), self.labels(slow))
            self.assertEqual(fast["preselect"], slow["preselect"])
            for one, two in zip(fast["people"], slow["people"]):
                self.assertAlmostEqual(one["score"], two["score"], places=3)
        person = people_groups.move_faces(self.store, ids[:3], None, "", now=NOW)["id"]
        fast = people_suggest.suggest_person(self.store, person, CONFIG, limit=0,
                                             use_numpy=True)
        slow = people_suggest.suggest_person(self.store, person, CONFIG, limit=0,
                                             use_numpy=False)
        self.assertEqual((self.labels(fast), fast["preselect"]),
                         (self.labels(slow), slow["preselect"]))


class PersonTest(SuggestMixin, unittest.TestCase):

    def unnamed(self, shared=0, vectors=None):
        """A person without a name of 4 faces like Ann (the owner's: kept apart from
        her), shared of them in pictures that show Ann too (or of faces of vectors,
        [(vector, sharpness)], best first). Returns its id."""
        for n in range(shared):
            store_faces(self.store, "both-%d" % n,
                        [face(100, 100, 200, person_vector(1, seed=200 + n)),
                         face(500, 100, 200, person_vector(1, seed=300 + n))])
        if shared:
            people_groups.group(self.store, options(), now=NOW)
        free = []
        for n in range(shared):
            ids = self.faces_of("both-%d" % n)
            # (One face of the picture joined Ann; the other could not.)
            self.assertEqual(sorted(str(people_groups.face_person(self.store, face_id))
                                    for face_id in ids), sorted([str(self.ann), "None"]))
            free.extend(face_id for face_id in ids
                        if people_groups.face_person(self.store, face_id) is None)
        if vectors is None:
            vectors = [(person_vector(1, seed=90 + n), 400.0) for n in range(shared, 4)]
        for n, (vector, sharpness) in enumerate(vectors):
            free.append(self.lone("own-%d" % n, vector, sharpness))
        return people_groups.move_faces(self.store, free, None, "", now=NOW)["id"]

    def test_an_unnamed_person_is_offered_as_a_whole(self):
        person = self.unnamed()
        answer = people_suggest.suggest_person(self.store, person, CONFIG)
        self.assertEqual(answer["basis"], "person")
        self.assertEqual(answer["person"], {"id": person, "label": "Person %d" % person,
                                            "faces": 4})
        self.assertEqual(self.labels(answer), ["Ann"])
        self.assertEqual(self.labels(people_suggest.suggest_person(
            self.store, person, CONFIG, limit=0)), ["Ann", "Bob"])
        self.assertEqual(answer["preselect"], self.ann)
        # A face of it: what its person most likely is (the whole person).
        face_id = people_groups.person_faces(self.store, person)[0]
        by_face = self.suggest(face_id)
        self.assertEqual((by_face["basis"], by_face["person"], by_face["preselect"]),
                         ("person", answer["person"], self.ann))
        self.assertEqual(by_face["people"], answer["people"])
        # A named person: nothing; none: nothing.
        self.assertEqual(people_suggest.suggest_person(self.store, self.ann, CONFIG)["basis"],
                         "named")
        self.assertEqual(people_suggest.suggest_person(self.store, 99999, CONFIG)["basis"],
                         "missing")
        # (Kept until the people change; each answer a copy of its own.)
        answer["people"].clear()
        self.assertEqual(len(people_suggest.suggest_person(self.store, person, CONFIG)
                             ["people"]), 1)

    def test_a_person_the_grouping_mixed_up_is_not_taken_whole(self):
        # Three faces like Ann and one like Bob: on average much like Ann, but "Name
        # Person N as Ann" would put Bob's face in her for good.
        person = self.unnamed(vectors=[(person_vector(1, seed=90 + n), 400.0)
                                       for n in range(3)] +
                              [(person_vector(2, seed=95), 400.0)])
        answer = people_suggest.suggest_person(self.store, person, CONFIG, limit=0)
        self.assertEqual(self.labels(answer)[0], "Ann")
        self.assertGreaterEqual(answer["people"][0]["score"], people_suggest.PERSON_SCORE)
        self.assertGreaterEqual(answer["people"][0]["score"] - answer["people"][1]["score"],
                                people_suggest.PERSON_LEAD)
        self.assertIsNone(answer["preselect"])
        for face_id in people_groups.person_faces(self.store, person):
            by_face = self.suggest(face_id)
            self.assertEqual((by_face["basis"], self.labels(by_face), by_face["preselect"]),
                             ("person", ["Ann"], None))

    def test_the_odd_face_of_a_person_does_not_take_it_whole(self):
        # Of a large person only its best faces are measured; one of the others, like
        # Bob, asks: the person is listed for it, not marked.
        person = self.unnamed(vectors=[(person_vector(1, seed=90 + n), 400.0)
                                       for n in range(3)] +
                              [(person_vector(2, seed=95), 30.0)])
        odd = self.faces_of("own-3")[0]
        self.assertLess(self.quality(odd), self.quality(self.faces_of("own-0")[0]))
        people_suggest.forget()
        with mock.patch.object(people_suggest, "PERSON_FACES", 3):
            self.assertEqual(people_suggest.suggest_person(self.store, person, CONFIG)
                             ["preselect"], self.ann)
            self.assertEqual(self.suggest(self.faces_of("own-0")[0])["preselect"], self.ann)
            by_odd = self.suggest(odd)
            self.assertEqual((by_odd["basis"], self.labels(by_odd), by_odd["preselect"]),
                             ("person", ["Ann"], None))
        people_suggest.forget()

    def test_a_person_of_a_face_or_two_is_held_to_a_face_s_bar(self):
        # Faces as like Ann as an unnamed person needs, but not a single face.
        vectors = [blend(person_vector(1, seed=50 + n), axis(60 + n), 0.58)
                   for n in range(3)]
        one = self.unnamed(vectors=[(vectors[0], 400.0)])
        answer = people_suggest.suggest_person(self.store, one, CONFIG)
        score = answer["people"][0]["score"]
        self.assertTrue(people_suggest.PERSON_SCORE <= score < people_suggest.FACE_SCORE,
                        score)
        self.assertEqual((answer["person"]["faces"], self.labels(answer), answer["preselect"]),
                         (1, ["Ann"], None))
        # Three of them: a person's bar.
        for face_id in people_groups.person_faces(self.store, one):
            people_groups.mark_not_face(self.store, [face_id], now=NOW)
        three = self.unnamed(vectors=[(vector, 400.0) for vector in vectors])
        answer = people_suggest.suggest_person(self.store, three, CONFIG)
        self.assertEqual((answer["person"]["faces"], answer["preselect"]), (3, self.ann))
        self.assertLess(answer["people"][0]["score"], people_suggest.FACE_SCORE)

    def test_what_was_said_keeps_a_named_person_from_being_offered(self):
        person = self.unnamed()
        faces = people_groups.person_faces(self.store, person)
        # A face of it said not to be Ann: never "Name Person N as Ann".
        people_groups.reject_faces(self.store, [faces[0]], self.ann, now=NOW)
        answer = people_suggest.suggest_person(self.store, person, CONFIG, limit=0)
        self.assertEqual(self.labels(answer), ["Bob"])
        self.assertIsNone(answer["preselect"])
        self.store.execute("DELETE FROM face_rejects")
        self.store.commit()
        people_groups._changed(self.store)
        self.store.commit()
        self.assertEqual(people_suggest.suggest_person(self.store, person, CONFIG)["preselect"],
                         self.ann)
        # A face of Ann said not to be this person: the same.
        ann_face = people_groups.person_faces(self.store, self.ann)[0]
        people_groups.reject_faces(self.store, [ann_face], person, now=NOW)
        self.assertEqual(self.labels(people_suggest.suggest_person(self.store, person, CONFIG,
                                                                   limit=0)), ["Bob"])

    def test_a_named_person_in_two_of_its_pictures_comes_after_the_others(self):
        person = self.unnamed(shared=2)
        answer = people_suggest.suggest_person(self.store, person, CONFIG, limit=0)
        self.assertEqual(self.labels(answer), ["Bob", "Ann"])
        self.assertEqual([(item["in_picture"], item["shared"]) for item in answer["people"]],
                         [(False, 0), (True, 2)])
        self.assertIsNone(answer["preselect"])
        shown = people_suggest.suggest_person(self.store, person, CONFIG)
        self.assertEqual([(item["label"], item["shared"]) for item in shown["people"]],
                         [("Ann", 2)])

    def test_one_picture_shared_is_not_enough_to_put_her_last(self):
        # (A mirror, a 360 picture's seam or a photo of a photo shows one person twice.)
        answer = people_suggest.suggest_person(self.store, self.unnamed(shared=1), CONFIG,
                                               limit=0)
        self.assertEqual(self.labels(answer), ["Ann", "Bob"])
        self.assertEqual([(item["in_picture"], item["shared"]) for item in answer["people"]],
                         [(False, 1), (False, 0)])
        self.assertEqual(answer["preselect"], self.ann)


@unittest.skipIf(flask is None, "Flask is not installed")
@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class RouteTest(ViewsMixin, unittest.TestCase):
    """GET /api/admin/people/suggest and the viewer's merge (with undo)."""

    def setUp(self):
        super().setUp()
        import serve
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.visitor = self.app.test_client()
        self.visitor.environ_base["REMOTE_ADDR"] = "192.0.2.20"
        self.addCleanup(self.app.extensions["search_images"].close)
        # Ann in three more pictures (enough faces to be preselected), and a face of
        # nobody like her, in a picture of its own.
        for n in range(3):
            store_faces(self.store, "more-%d" % n, [face(100, 100, 200,
                                                         person_vector(1, seed=40 + n))])
        people_groups.group(self.store, options(), now=NOW)
        self.assertEqual(people_groups.person_info(self.store, self.ann)["faces"], 6)
        store_faces(self.store, "lone", [face(100, 100, 200, person_vector(1, seed=50))])
        insert_image(self.conn, self.library_id, "lone.jpg", "lone")
        self.lone = self.faces_of("lone")[0]

    def admin(self, path, body=None, headers=None):
        sent = {"X-Search-Images": "1"}
        sent.update(headers or {})
        if body is None:
            return self.client.get("/api/admin" + path, headers=sent)
        return self.client.post("/api/admin" + path, headers=sent, json=body)

    def test_the_admin_gets_them_by_etag(self):
        reply = self.admin("/people/suggest?face=%d" % self.lone)
        self.assertEqual(reply.status_code, 200)
        answer = reply.get_json()
        self.assertEqual((answer["basis"], answer["preselect"]), ("face", self.ann))
        # (Only the named people: Bob has no name yet.)
        self.assertEqual([item["label"] for item in answer["people"]], ["Ann"])
        # (Kept by the browser, asked again by its ETag: 304 while nothing changed.)
        self.assertEqual(reply.headers["Cache-Control"], "private, no-cache")
        tag = reply.headers["ETag"]
        again = self.admin("/people/suggest?face=%d" % self.lone, headers={"If-None-Match": tag})
        self.assertEqual((again.status_code, again.get_data()), (304, b""))
        self.assertEqual(again.headers["ETag"], tag)
        # Ann named again: another answer.
        self.admin("/people/%d" % self.ann, {"name": "Ada"})
        fresh = self.admin("/people/suggest?face=%d" % self.lone, headers={"If-None-Match": tag})
        self.assertEqual(fresh.status_code, 200)
        self.assertNotEqual(fresh.headers["ETag"], tag)
        self.assertEqual([item["label"] for item in fresh.get_json()["people"]], ["Ada"])
        # The other admin answers are never kept.
        self.assertEqual(self.admin("/people").headers["Cache-Control"], "no-store")
        # A face of an unnamed person: what that person most likely is.
        bob_face = self.faces_of(self.made[1][0])[0]
        answer = self.admin("/people/suggest?face=%d" % bob_face).get_json()
        self.assertEqual((answer["basis"], answer["person"]["id"], answer["person"]["faces"]),
                         ("person", self.bob, 3))

    def test_only_the_admin_on_this_computer(self):
        path = "/api/admin/people/suggest?face=%d" % self.lone
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get(path, headers={"X-Search-Images": "1",
                                                        "Sec-Fetch-Site": "cross-site"})
                         .status_code, 403)
        self.config["people"] = {"enabled": True, "visitors": True}
        with open(common.config_path(), "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        self.assertEqual(self.visitor.get(path, headers={"X-Search-Images": "1"}).status_code,
                         403)
        for bad in ("", "?face=", "?face=x", "?face=0", "?face=-4", "?face=1.5"):
            self.assertEqual(self.admin("/people/suggest" + bad).status_code, 400, bad)
        self.assertEqual(self.admin("/people/suggest?face=999999").get_json()["basis"],
                         "missing")

    def test_no_face_data(self):
        self.assertEqual(self.admin("/people/suggest?face=%d" % self.lone).status_code, 200)
        self.assertIsNotNone(people_suggest._cache["index"])
        self.store.close()
        people.delete_all()
        answer = self.admin("/people/suggest?face=%d" % self.lone)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.get_json(), {"face": self.lone, "basis": "missing",
                                             "people": [], "preselect": None})
        # (The named people's names and vectors are not kept in memory either.)
        self.assertIsNone(people_suggest._cache["index"])
        self.store = people.connect()

    def test_the_delete_face_data_job_takes_what_is_kept_away(self):
        self.assertEqual(self.admin("/people/suggest?face=%d" % self.lone).status_code, 200)
        self.assertIsNotNone(people_suggest._cache["index"])
        queued = self.admin("/jobs", {"kind": "delete_faces", "params": {"confirm": True}})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        self.assertIsNone(people_suggest._cache["index"])

    def test_name_an_unnamed_person_as_a_named_one_and_undo(self):
        # (A face said not to be Bob: a merge makes it "not Ann"; the undo puts it back.)
        people_groups.reject_faces(self.store, [self.lone], self.bob, now=NOW)
        bob_face = self.faces_of(self.made[1][0])[0]
        ann_face = self.faces_of(self.made[0][0])[0]
        before = state_of(self.store)
        stale = self.admin("/people/%d/merge" % self.bob, {"into": self.ann, "face": ann_face,
                                                          "undo": True})
        self.assertEqual(stale.status_code, 409)
        self.assertIn("not in this person any more", stale.get_json()["error"])
        self.assertEqual(state_of(self.store), before)
        for bad in ("x", True, 0, -3, 1.5):
            self.assertEqual(self.admin("/people/%d/merge" % self.bob,
                                        {"into": self.ann, "face": bad}).status_code, 400, bad)
        merged = self.admin("/people/%d/merge" % self.bob, {"into": self.ann, "face": bob_face,
                                                           "undo": True})
        self.assertEqual(merged.status_code, 200)
        answer = merged.get_json()
        self.assertEqual((answer["person"]["id"], answer["person"]["faces"]), (self.ann, 9))
        self.assertIsInstance(answer["undo"], str)
        self.assertIsNone(self.store.execute("SELECT 1 FROM people WHERE id = ?",
                                             (self.bob,)).fetchone())
        self.assertEqual([tuple(row) for row in self.store.execute(
            "SELECT person_id FROM face_rejects WHERE face_id = ?", (self.lone,))],
                         [(self.ann,)])
        undone = self.admin("/people/undo", {"undo": answer["undo"]})
        self.assertEqual(undone.status_code, 200)
        self.assertEqual(state_of(self.store), before)
        # Without "undo": as before (no token).
        plain = self.admin("/people/%d/merge" % self.bob, {"into": self.ann}).get_json()
        self.assertNotIn("undo", plain)
        self.assertEqual(plain["person"]["faces"], 9)

    def test_a_merge_too_large_to_keep_cannot_be_undone(self):
        with mock.patch.object(people_groups, "UNDO_MERGE_FACES", 4):
            reply = self.admin("/people/%d/merge" % self.bob, {"into": self.ann, "undo": True})
        self.assertEqual(reply.status_code, 200)
        self.assertIsNone(reply.get_json()["undo"])
        self.assertEqual(reply.get_json()["person"]["faces"], 9)


if __name__ == "__main__":
    unittest.main()
