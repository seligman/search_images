"""Tests for the people the faces show (people_groups.py): grouping faces into people,
stable ids, what the owner says (names, merges, moves, "not this person", "not a
face", hidden people), what the pages are told (/api/people, the details, the People
page, face pictures), who may see it, the faces job's grouping step, and the export
option that publishes the names (and nothing else).

The faces are made up: vectors near one axis per made-up person (no photo of anyone),
stored with people.record_picture as the face helper's answers would be. Every test
runs in a temporary SETTINGS_BASE.
"""

import base64
import io
import json
import math
import os
import random
import sqlite3
import struct
import tempfile
import unittest

import support

import common
import db
import jobs
import people
import people_groups
import test_people

try:
    import flask
except ImportError:
    flask = None

NOW = 1700000000.0
DIMS = people.EMBEDDING_DIMS


def unit(values):
    length = math.sqrt(sum(value * value for value in values))
    return [value / length for value in values]


def person_vector(k, noise=0.05, seed=0):
    """A vector of made-up person k: along axis k, with a little noise."""
    rnd = random.Random(seed * 1000 + k)
    values = [rnd.gauss(0, noise) for _ in range(DIMS)]
    values[k] += 1.0
    return unit(values)


def face(x, y, side, vector, score=0.95, norm=11.0, sharpness=400.0):
    """A face of the helper's answer (people_worker): box, landmarks, vector..."""
    marks = [[x + 0.3 * side, y + 0.4 * side], [x + 0.7 * side, y + 0.4 * side],
             [x + 0.5 * side, y + 0.6 * side], [x + 0.35 * side, y + 0.8 * side],
             [x + 0.65 * side, y + 0.8 * side]]
    return {"box": [x, y, side, side], "landmarks": marks, "score": score, "norm": norm,
            "sharpness": sharpness,
            "embedding": base64.b64encode(struct.pack("<%df" % DIMS, *vector)).decode("ascii")}


def store_faces(store, content_hash, faces, kind=people.KIND_PHOTO, width=1000, height=800):
    """Record a picture's faces as a run of the helper would."""
    people.record_picture(store, (content_hash, kind), "recipe",
                          {"width": width, "height": height, "faces": faces, "seconds": 0.1},
                          now=NOW)
    store.commit()


def options(**changes):
    value = people.settings({"people": {"enabled": True}})
    value.update(changes)
    return value


# ---------------------------------------------------------------------------
# Grouping (plain functions)

class SimilarPairsTest(unittest.TestCase):

    def test_python_and_numpy_find_the_same_pairs(self):
        vectors = [tuple(person_vector(k % 4, noise=0.3, seed=k)) for k in range(30)]
        plain = people_groups.similar_pairs(vectors, 0.2, use_numpy=False)
        self.assertTrue(plain)
        self.assertTrue(all(i < j and value >= 0.2 for i, j, value in plain))
        if people_groups._numpy() is None:
            self.skipTest("numpy is not installed")
        fast = people_groups.similar_pairs(vectors, 0.2, use_numpy=True)
        self.assertEqual([(i, j) for i, j, _value in fast], [(i, j) for i, j, _value in plain])
        for (_i, _j, first), (_k, _l, second) in zip(plain, fast):
            self.assertAlmostEqual(first, second, places=6)


class AverageLinkageTest(unittest.TestCase):

    def test_groups_by_the_average_of_every_pair(self):
        # 0-1-2 alike; 3-4 alike; 2-3 alike, but 3 and 4 are not like 0, 1.
        pairs = [(0, 1, 0.9), (0, 2, 0.8), (1, 2, 0.85), (3, 4, 0.9), (2, 3, 0.6)]
        self.assertEqual(people_groups.average_linkage(6, pairs, 0.4),
                         [[0, 1, 2], [3, 4], [5]])
        # (Single linkage would join everything through 2-3: the average does not.)
        self.assertEqual(people_groups.average_linkage(6, pairs, 0.09),
                         [[0, 1, 2, 3, 4], [5]])
        self.assertEqual(people_groups.average_linkage(3, [], 0.4), [[0], [1], [2]])

    def test_start_groups_and_what_is_not_allowed(self):
        pairs = [(0, 1, 0.9), (1, 2, 0.9), (0, 2, 0.9)]
        # (3 starts with 0: the group {0, 3} is like 1 and 2 at 0.45 on average.)
        self.assertEqual(people_groups.average_linkage(4, pairs, 0.4, start=[[0, 3]]),
                         [[0, 1, 2, 3]])
        self.assertEqual(people_groups.average_linkage(4, pairs, 0.5, start=[[0, 3]]),
                         [[0, 3], [1, 2]])
        apart = people_groups.average_linkage(
            3, pairs, 0.4, allowed=lambda first, second: not (0 in first + second and
                                                              2 in first + second))
        self.assertEqual(len(apart), 2)
        self.assertTrue(all(not (0 in group and 2 in group) for group in apart))

    def test_the_same_input_gives_the_same_groups(self):
        vectors = [tuple(person_vector(k % 5, noise=0.05, seed=k)) for k in range(60)]
        pairs = people_groups.similar_pairs(vectors, 0.1, use_numpy=False)
        first = people_groups.average_linkage(len(vectors), pairs, 0.4)
        self.assertEqual(first, people_groups.average_linkage(len(vectors), list(pairs), 0.4))
        self.assertEqual(len(first), 5)
        for group in first:
            self.assertEqual({item % 5 for item in group}, {group[0] % 5})


class PlanTest(unittest.TestCase):

    def faces(self, people_count=3, each=5, quality=0.9, norm=11.0, start=1, first=0):
        found = []
        face_id = start
        for k in range(first, first + people_count):
            for n in range(each):
                found.append((face_id, quality, tuple(person_vector(k + 1, seed=n)), norm))
                face_id += 1
        return found

    def test_new_people_and_faces_of_lower_quality(self):
        faces = self.faces(each=6)
        # A blurred face of person 1, one of somebody else, a short vector of person 2.
        faces.append((100, 0.1, tuple(person_vector(1, noise=0.15, seed=9)), 9.0))
        faces.append((101, 0.1, tuple(person_vector(40, seed=9)), 9.0))
        faces.append((102, 0.1, tuple(person_vector(2, seed=8)), 5.0))
        result = people_groups.plan(faces, {}, {}, {}, options())
        self.assertEqual(sorted(set(result["assign"].values())), [-3, -2, -1])
        self.assertEqual(result["groups"], 3)
        persons = {}
        for face_id, person in result["assign"].items():
            persons.setdefault(person, []).append(face_id)
        self.assertEqual(sorted(sorted(ids) for ids in persons.values()),
                         [[1, 2, 3, 4, 5, 6, 100], [7, 8, 9, 10, 11, 12],
                          [13, 14, 15, 16, 17, 18]])
        self.assertNotIn(101, result["assign"])
        self.assertNotIn(102, result["assign"])            # (REST_NORM)

    def test_too_few_faces_and_things_are_nobody(self):
        faces = self.faces(people_count=1, each=2)
        faces += self.faces(people_count=1, each=4, norm=6.0, start=50, first=5)
        result = people_groups.plan(faces, {}, {}, {}, options())
        self.assertEqual(result["assign"], {})              # (min_faces; PERSON_NORM)
        result = people_groups.plan(faces, {}, {}, {}, options(min_faces=2))
        self.assertEqual(sorted(result["assign"]), [1, 2])

    def test_people_keep_their_ids(self):
        faces = self.faces()
        first = people_groups.plan(faces, {}, {}, {}, options())["assign"]
        # The people as stored: 7, 8, 9.
        ids = {-1: 7, -2: 8, -3: 9}
        previous = {face_id: (ids[person], 0) for face_id, person in first.items()}
        persons = {7: {"name": "", "hidden": False}, 8: {"name": "Ann", "hidden": False},
                   9: {"name": "", "hidden": True}}
        faces += [(200 + n, 0.9, tuple(person_vector(2, seed=50 + n)), 11.0) for n in range(3)]
        again = people_groups.plan(faces, previous, {}, persons, options())
        self.assertEqual(again["new"], [])
        for face_id, person in first.items():
            self.assertEqual(again["assign"][face_id], ids[person])
        self.assertEqual({again["assign"][200 + n] for n in range(3)}, {ids[first[6]]})

    def test_what_the_owner_said_is_kept(self):
        faces = self.faces(people_count=2)
        first = people_groups.plan(faces, {}, {}, {}, options())["assign"]
        ids = {first[1]: 1, first[6]: 2}
        previous = {face_id: (ids[person], 0) for face_id, person in first.items()}
        persons = {1: {"name": "", "hidden": False}, 2: {"name": "", "hidden": False}}
        # Face 3 is not person 1; face 7 (like person 2) was put in person 1 by the
        # owner: it stays there, and takes no face of person 2 with it.
        previous[7] = (1, 1)
        result = people_groups.plan(faces, previous, {3: {1}}, persons, options())
        self.assertNotEqual(result["assign"].get(3), 1)
        self.assertEqual(result["assign"][7], 1)
        self.assertEqual([result["assign"][n] for n in (1, 2, 4, 5)], [1, 1, 1, 1])
        self.assertEqual([result["assign"][n] for n in (6, 8, 9, 10)], [2, 2, 2, 2])
        self.assertEqual(result["new"], [])
        # Faces the owner put in two people are never joined (a split stays split).
        previous = {face_id: (1, 1) for face_id in range(1, 4)}
        previous.update({face_id: (2, 1) for face_id in range(4, 6)})
        result = people_groups.plan(faces[:5], previous, {}, persons, options())
        self.assertEqual([result["assign"][n] for n in range(1, 6)], [1, 1, 1, 2, 2])

    def test_two_faces_of_one_picture_are_two_people(self):
        faces = [(n + 1, 0.9, tuple(person_vector(1, seed=n)), 11.0, ("a%d" % n, "photo"))
                 for n in range(5)]
        # Two faces of one picture much like made-up person 1 (a mirror, twins), and two
        # blurred ones of another picture: one of each pair at most is person 1.
        faces += [(10, 0.9, tuple(person_vector(1, seed=20)), 11.0, ("two", "photo")),
                  (11, 0.9, tuple(person_vector(1, seed=21)), 11.0, ("two", "photo")),
                  (20, 0.1, tuple(person_vector(1, seed=30)), 9.0, ("rest", "photo")),
                  (21, 0.1, tuple(person_vector(1, seed=31)), 9.0, ("rest", "photo"))]
        result = people_groups.plan(faces, {}, {}, {}, options())
        person = result["assign"][1]
        self.assertEqual(sum(result["assign"].get(n) == person for n in (10, 11)), 1)
        self.assertEqual(sum(result["assign"].get(n) == person for n in (20, 21)), 1)
        # (A face without its picture is a picture of its own.)
        loose = people_groups.plan([item[:4] for item in faces], {}, {}, {}, options())
        self.assertEqual({loose["assign"].get(n) for n in (1, 10, 11, 20, 21)},
                         {loose["assign"][1]})

    def test_a_burst_or_copies_of_one_shot_are_nobody(self):
        base = 0x0F0F0F0F0F0F0F0F
        faces = [(n + 1, 0.9, tuple(person_vector(1, seed=n)), 11.0, ("b%d" % n, "photo"))
                 for n in range(4)]
        burst = {("b%d" % n, "photo"): (base ^ n, None) for n in range(4)}
        self.assertEqual(people_groups.plan(faces, {}, {}, {}, options(),
                                            moments=burst)["assign"], {})
        # Taken within two seconds (by a clock to trust): one moment too.
        timed = {key: (None, 1000 + n // 2) for n, key in enumerate(sorted(burst))}
        timed[("b3", "photo")] = (None, 1002)
        self.assertEqual(people_groups.plan(faces, {}, {}, {}, options(),
                                            moments=timed)["assign"], {})
        # Two moments make a person; so do pictures whose moments are not known.
        two = dict(burst)
        two[("b3", "photo")] = (base ^ 0xFFFF000000, None)
        self.assertEqual(len(set(people_groups.plan(faces, {}, {}, {}, options(),
                                                    moments=two)["assign"].values())), 1)
        self.assertEqual(len(people_groups.plan(faces, {}, {}, {}, options())["assign"]), 4)
        # A person the owner named keeps the faces of a burst; an unnamed one does not.
        previous = {n + 1: (7, 0) for n in range(4)}
        named = people_groups.plan(faces, previous, {}, {7: {"name": "Bo", "hidden": False}},
                                   options(), moments=burst)
        self.assertEqual(set(named["assign"].values()), {7})
        unnamed = people_groups.plan(faces, previous, {}, {7: {"name": "", "hidden": False}},
                                     options(), moments=burst)
        self.assertEqual(unnamed["assign"], {})

    def test_moments(self):
        base = 0x0F0F0F0F0F0F0F0F
        step = base ^ 0xFF                  # (8 bits from base, and 8 from the next)
        moments = {"a": (base, None), "b": (step, None), "c": (step ^ 0xFF00, None),
                   "d": (None, 50), "e": (None, 52), "f": (None, None)}
        self.assertEqual(people_groups.moment_sets(list("abcdef"), moments),
                         {"a": 0, "b": 0, "c": 0, "d": 1, "e": 1, "f": 2})
        self.assertEqual(people_groups.sightings(list("abcdef"), moments), 3)
        self.assertEqual(people_groups.sightings(list("abcdef"), None), 6)
        self.assertEqual(people_groups._dhash_value("0f0f0f0f0f0f0f0f"), base)
        for little in ("0000000000000000", "ffffffffffffff00", "", "nothing", None):
            self.assertIsNone(people_groups._dhash_value(little), little)
        self.assertEqual(people_groups._seconds("2020-01-02T00:00:01") -
                         people_groups._seconds("2020-01-01T23:59:59"), 2)
        for undated in ("2020-01-01", "", "2020-13-01T00:00:00", "2020-01-01T25:00"):
            self.assertIsNone(people_groups._seconds(undated), undated)

    def test_a_few_faces_take_in_only_faces_much_alike(self):
        def axis(face_id, picture, quality=0.9, alike=1.0):
            values = [0.0] * DIMS
            values[1], values[2] = alike, math.sqrt(1.0 - alike * alike)
            return (face_id, quality, tuple(values), 11.0, (picture, "photo"))
        # A blurred face 0.45 alike (match: 0.4): not enough for a person of three faces,
        # enough for one of six; 0.55 is enough for both.
        few = [axis(n + 1, "c%d" % n) for n in range(3)]
        many = [axis(n + 1, "c%d" % n) for n in range(6)]
        for core, alike, joins in ((few, 0.45, False), (many, 0.45, True), (few, 0.55, True)):
            result = people_groups.plan(core + [axis(50, "r", 0.1, alike)], {}, {}, {},
                                        options())
            self.assertEqual(50 in result["assign"], joins, (len(core), alike))

    def test_a_named_person_with_few_faces_left_stays(self):
        faces = self.faces(people_count=1, each=2)
        previous = {1: (5, 0), 2: (5, 0)}
        named = people_groups.plan(faces, previous, {}, {5: {"name": "Bo", "hidden": False}},
                                   options())
        self.assertEqual(named["assign"], {1: 5, 2: 5})
        unnamed = people_groups.plan(faces, previous, {}, {5: {"name": "", "hidden": False}},
                                     options())
        self.assertEqual(unnamed["assign"], {})


# ---------------------------------------------------------------------------
# The store: grouping and what the owner says

class StoreMixin(support.TempBaseMixin):

    def setUp(self):
        super().setUp()
        self.base.write_config([], people={"enabled": True})
        self.store = people.connect()
        self.addCleanup(self.store.close)

    def add_people(self, counts, seed=0, prefix="pic"):
        """Pictures with one face each: counts[k] pictures of made-up person k+1.
        Returns {k: [content hashes]}."""
        made = {}
        for k, count in enumerate(counts):
            for n in range(count):
                content_hash = "%s-%d-%d-%d" % (prefix, k, n, seed)
                store_faces(self.store, content_hash,
                            [face(100, 100, 200, person_vector(k + 1, seed=seed * 100 + n))])
                made.setdefault(k, []).append(content_hash)
        return made

    def person_of(self, content_hash):
        row = self.store.execute("SELECT fp.person_id FROM faces f JOIN face_people fp ON "
                                 "fp.face_id = f.id WHERE f.content_hash = ?",
                                 (content_hash,)).fetchone()
        return row[0] if row else None

    def faces_of(self, content_hash):
        return [row[0] for row in self.store.execute(
            "SELECT id FROM faces WHERE content_hash = ? ORDER BY id", (content_hash,))]


class GroupTest(StoreMixin, unittest.TestCase):

    def test_grouping_and_stable_ids(self):
        made = self.add_people([4, 3, 2])
        self.assertTrue(people_groups.grouping_due(self.store, options()))
        summary = people_groups.group(self.store, options(), now=NOW)
        self.assertEqual((summary["faces"], summary["grouped"], summary["people"],
                          summary["new"]), (9, 7, 2, 2))
        self.assertFalse(people_groups.grouping_due(self.store, options()))
        self.assertTrue(people_groups.grouping_due(self.store, options(match=0.5)))
        first = self.person_of(made[0][0])
        second = self.person_of(made[1][0])
        self.assertEqual(first, 1)                          # (the largest group first)
        self.assertIsNone(self.person_of(made[2][0]))       # (2 faces: nobody yet)
        people_groups.update_person(self.store, first, {"name": "Ann"})
        version = people_groups.people_version(self.store)
        # More pictures: person 3 now has enough; the others keep their ids and names.
        made.update({2: made[2] + self.add_people([0, 0, 2], seed=1)[2]})
        self.assertTrue(people_groups.grouping_due(self.store, options()))
        summary = people_groups.group(self.store, options())
        self.assertEqual(summary["new"], 1)
        self.assertGreater(people_groups.people_version(self.store), version)
        self.assertEqual(self.person_of(made[0][0]), first)
        self.assertEqual(self.person_of(made[1][0]), second)
        self.assertEqual(people_groups.person_info(self.store, first)["name"], "Ann")
        third = self.person_of(made[2][0])
        self.assertNotIn(third, (first, second))
        self.assertEqual(people_groups.person_info(self.store, third)["label"],
                         "Person %d" % third)

    def test_a_change_made_while_grouping_is_not_lost(self):
        made = self.add_people([3, 3])
        people_groups.group(self.store, options())
        person = self.person_of(made[0][0])
        real_plan = people_groups.plan
        calls = []

        def plan(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                # (The owner names the person while the grouping works.)
                other = people.connect()
                try:
                    people_groups.update_person(other, person, {"name": "Cy"})
                finally:
                    other.close()
            return real_plan(*args, **kwargs)
        people_groups.plan = plan
        try:
            people_groups.group(self.store, options(match=0.5))
        finally:
            people_groups.plan = real_plan
        self.assertEqual(len(calls), 2)
        self.assertEqual(people_groups.person_info(self.store, person)["name"], "Cy")

    def test_names(self):
        made = self.add_people([3, 3])
        people_groups.group(self.store, options())
        first, second = self.person_of(made[0][0]), self.person_of(made[1][0])
        info = people_groups.update_person(self.store, first, {"name": "  Ann   Lee "})
        self.assertEqual((info["name"], info["label"]), ("Ann Lee", "Ann Lee"))
        with self.assertRaises(people_groups.GroupError) as caught:
            people_groups.update_person(self.store, second, {"name": "ann lee"})
        self.assertEqual((caught.exception.status, caught.exception.extra["other"]),
                         (409, first))
        for bad in ("Person 12", "a\x01b", "x" * 101, 12):
            with self.assertRaises(people_groups.GroupError):
                people_groups.update_person(self.store, second, {"name": bad})
        with self.assertRaises(people_groups.GroupError) as caught:
            people_groups.update_person(self.store, 999, {"name": "Bo"})
        self.assertEqual(caught.exception.status, 404)
        info = people_groups.update_person(self.store, first, {"name": ""})
        self.assertEqual(info["label"], "Person %d" % first)
        # Hidden, and the cover (one of its own faces only).
        self.assertTrue(people_groups.update_person(self.store, first,
                                                    {"hidden": True})["hidden"])
        own = self.faces_of(made[0][1])[0]
        other = self.faces_of(made[1][0])[0]
        self.assertEqual(people_groups.update_person(self.store, first,
                                                     {"cover": own})["cover"], own)
        with self.assertRaises(people_groups.GroupError):
            people_groups.update_person(self.store, first, {"cover": other})
        with self.assertRaises(people_groups.GroupError):
            people_groups.update_person(self.store, first, {"hidden": "yes"})

    def test_merge(self):
        made = self.add_people([3, 3])
        people_groups.group(self.store, options())
        first, second = self.person_of(made[0][0]), self.person_of(made[1][0])
        people_groups.update_person(self.store, second, {"name": "Bo"})
        info = people_groups.merge(self.store, second, first)
        self.assertEqual((info["id"], info["name"], info["faces"]), (first, "Bo", 6))
        with self.assertRaises(people_groups.GroupError):
            people_groups.person_info(self.store, second)
        owners = {row[0] for row in self.store.execute("SELECT owner FROM face_people")}
        self.assertEqual(owners, {1})
        # A later grouping keeps them together.
        people_groups.group(self.store, options(match=0.5))
        self.assertEqual({self.person_of(content_hash) for content_hash in made[0] + made[1]},
                         {first})
        with self.assertRaises(people_groups.GroupError):
            people_groups.merge(self.store, first, first)

    def test_a_merge_pins_the_clear_faces_only(self):
        made = self.add_people([3, 3])
        # A blurred face of made-up person 1 (it joins by itself).
        store_faces(self.store, "blurred", [face(100, 100, 200, person_vector(1, seed=40),
                                                 sharpness=6.0)])
        people_groups.group(self.store, options())
        first, second = self.person_of(made[0][0]), self.person_of(made[1][0])
        self.assertEqual(self.person_of("blurred"), first)
        people_groups.merge(self.store, second, first,
                            group_quality=options()["group_quality"])
        owners = dict(self.store.execute("SELECT f.content_hash, fp.owner FROM face_people fp "
                                          "JOIN faces f ON f.id = fp.face_id"))
        self.assertEqual(owners.pop("blurred"), 0)
        self.assertEqual(set(owners.values()), {1})
        self.assertEqual(self.person_of("blurred"), first)

    def test_changes_that_leave_faces_without_a_person_make_grouping_due(self):
        made = self.add_people([4])
        people_groups.group(self.store, options())
        self.assertFalse(people_groups.grouping_due(self.store, options()))
        person = self.person_of(made[0][0])
        thing = self.faces_of(made[0][0])[0]
        people_groups.mark_not_face(self.store, [thing])
        self.assertFalse(people_groups.grouping_due(self.store, options()))
        people_groups.mark_not_face(self.store, [thing], False)       # (a face after all)
        self.assertTrue(people_groups.grouping_due(self.store, options()))
        people_groups.group(self.store, options())
        self.assertEqual(self.person_of(made[0][0]), person)
        people_groups.reject_faces(self.store, [self.faces_of(made[0][1])[0]], person)
        self.assertTrue(people_groups.grouping_due(self.store, options()))

    def test_a_split_stays_split(self):
        made = self.add_people([6])
        people_groups.group(self.store, options())
        person = self.person_of(made[0][0])
        moved = [self.faces_of(content_hash)[0] for content_hash in made[0][:3]]
        info = people_groups.move_faces(self.store, moved, None, "New one")
        self.assertEqual((info["name"], info["faces"]), ("New one", 3))
        self.assertEqual(people_groups.person_info(self.store, person)["faces"], 3)
        # (They all look alike, but the owner said they are two people.)
        people_groups.group(self.store, options(match=0.3))
        self.assertEqual({self.person_of(content_hash) for content_hash in made[0][:3]},
                         {info["id"]})
        self.assertEqual({self.person_of(content_hash) for content_hash in made[0][3:]},
                         {person})
        # Moving faces to a taken name is refused; to an existing person works.
        with self.assertRaises(people_groups.GroupError) as caught:
            people_groups.move_faces(self.store, moved[:1], None, "new ONE")
        self.assertEqual(caught.exception.status, 409)
        back = people_groups.move_faces(self.store, moved[:1], person)
        self.assertEqual(back["id"], person)
        with self.assertRaises(people_groups.GroupError):
            people_groups.move_faces(self.store, [], person)
        with self.assertRaises(people_groups.GroupError):
            people_groups.move_faces(self.store, [99999], person)

    def test_not_this_person_and_not_a_face(self):
        made = self.add_people([5])
        people_groups.group(self.store, options())
        person = self.person_of(made[0][0])
        rejected = self.faces_of(made[0][0])[0]
        result = people_groups.reject_faces(self.store, [rejected], person)
        self.assertEqual((result["removed"], result["person"]["faces"]), (1, 4))
        people_groups.group(self.store, options(match=0.3))
        self.assertIsNone(self.person_of(made[0][0]))
        self.assertEqual(self.person_of(made[0][1]), person)
        # Not a face: left out of every grouping, until the mark is taken back.
        thing = self.faces_of(made[0][1])[0]
        self.assertEqual(people_groups.mark_not_face(self.store, [thing]), 1)
        self.assertEqual(people_groups.mark_not_face(self.store, [thing]), 0)
        people_groups.group(self.store, options(match=0.35))
        self.assertIsNone(self.person_of(made[0][1]))
        status = people_groups.status(self.store, {"people": {"enabled": True}})
        self.assertEqual((status["not_faces"], status["other"], status["grouped"]), (1, 1, 3))
        self.assertEqual(people_groups.mark_not_face(self.store, [thing], False), 1)
        # A person whose last faces leave goes, with its name: nothing could show it.
        people_groups.update_person(self.store, person, {"name": "Di"})
        result = people_groups.reject_faces(self.store, [self.faces_of(content_hash)[0]
                                                         for content_hash in made[0][1:]],
                                            person)
        self.assertIsNone(result["person"])
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM people WHERE name = 'Di'")
                         .fetchone()[0], 0)

    def test_faces_found_again_keep_their_people(self):
        made = self.add_people([3])
        people_groups.group(self.store, options())
        person = self.person_of(made[0][0])
        # The same face found again (a new recipe): it keeps its id, so its person.
        store_faces(self.store, made[0][0], [face(102, 101, 199, person_vector(1, seed=0))])
        self.assertEqual(self.person_of(made[0][0]), person)
        # A face no longer found takes its place in the person with it.
        store_faces(self.store, made[0][1], [])
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM face_people").fetchone()[0], 2)

    def test_an_older_store_is_brought_up_to_date(self):
        path = self.base.join("data", "old-faces.db")
        conn = sqlite3.connect(path)
        # (Version 1: without the people, and the faces' pictures of version 3.)
        for statement in people._SCHEMA[:-6]:
            conn.execute(statement)
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                         (("version", "1"), ("epoch", "e"), ("generation", "3")))
        conn.execute("INSERT INTO faces (content_hash, kind, x, y, w, h, landmarks, score, size, "
                     "sharpness, norm, turn, tilt, quality, embedding, updated) VALUES ('h', "
                     "'photo', 0, 0, 1, 1, '[]', 0.9, 50, 100, 10, 0, 0.5, 1, ?, 0)",
                     (b"\0" * 512,))
        conn.commit()
        conn.close()
        # (Reading it brings it up to date too: only tables are added.)
        upgraded = people.connect(path, create=False)
        try:
            self.assertEqual(people.meta(upgraded)["version"], people.FACES_VERSION)
            self.assertEqual(people.meta(upgraded)["faces"], 1)
            self.assertEqual(people.meta(upgraded)["people"], 0)
        finally:
            upgraded.close()

    def test_delete_all_takes_the_people_and_names(self):
        made = self.add_people([3])
        people_groups.group(self.store, options())
        people_groups.update_person(self.store, self.person_of(made[0][0]), {"name": "Di"})
        self.store.close()
        result = people.delete_all()
        self.assertEqual((result["faces"], result["people"], result["named"], result["removed"]),
                         (3, 1, 1, True))
        self.assertFalse(os.path.exists(people.store_path()))
        self.store = people.connect()
        self.addCleanup(self.store.close)


# ---------------------------------------------------------------------------
# What the pages are told (index rows for the faces' pictures)

def insert_image(conn, library_id, path, content_hash, **fields):
    row = {"library_id": library_id, "path": path, "name": path, "size": 100, "mtime": NOW,
           "ctime": NOW, "width": 1000, "height": 800, "added": NOW, "updated": NOW,
           "content_hash": content_hash, "has_preview": 1}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute("INSERT INTO images (%s) VALUES (%s)" % (
        ",".join(columns), ",".join("?" * len(columns))), [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


class ViewsMixin(StoreMixin):

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Family")
        self.config = self.base.write_config([support.library("Family", self.folder)],
                                             people={"enabled": True})
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.library_id = self.config["libraries"][0]["id"]
        self.made = self.add_people([3, 3])
        self.image = {}
        for k, hashes in self.made.items():
            for n, content_hash in enumerate(hashes):
                self.image[content_hash] = insert_image(self.conn, self.library_id,
                                                        "p%d%d.jpg" % (k, n), content_hash)
        people_groups.group(self.store, options())
        self.ann = self.person_of(self.made[0][0])
        self.bob = self.person_of(self.made[1][0])
        people_groups.update_person(self.store, self.ann, {"name": "Ann"})


class ViewsTest(ViewsMixin, unittest.TestCase):

    def test_search_data_for_the_admin_and_for_others(self):
        hidden_picture = self.image[self.made[0][2]]
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (hidden_picture,))
        self.conn.commit()
        answer = people_groups.search_data(self.store, self.conn, self.config,
                                           [self.library_id], admin=True)
        self.assertEqual([item["label"] for item in answer["people"]],
                         ["Ann", "Person %d" % self.bob])
        self.assertEqual(answer["people"][0]["ids"],
                         sorted(self.image[content_hash] for content_hash in self.made[0]))
        self.assertNotIn("hidden", answer["people"][0])
        others = people_groups.search_data(self.store, self.conn, self.config,
                                           [self.library_id], admin=False)
        self.assertEqual([item["label"] for item in others["people"]], ["Ann"])
        self.assertNotIn(hidden_picture, others["people"][0]["ids"])
        people_groups.update_person(self.store, self.ann, {"hidden": True})
        self.assertEqual(people_groups.search_data(self.store, self.conn, self.config,
                                                   [self.library_id], admin=False)["people"],
                         [])
        admin = people_groups.search_data(self.store, self.conn, self.config,
                                          [self.library_id], admin=True)
        self.assertNotIn("Ann", [item["label"] for item in admin["people"]])
        listed = people_groups.search_data(self.store, self.conn, self.config,
                                           [self.library_id], admin=True,
                                           include_hidden_people=True)
        self.assertEqual([item["hidden"] for item in listed["people"]], [True, False])
        self.assertEqual(people_groups.search_data(None, self.conn, self.config,
                                                   [self.library_id], True)["people"], [])

    def test_the_people_of_a_picture(self):
        row = self.conn.execute("SELECT * FROM images WHERE id = ?",
                                (self.image[self.made[0][0]],)).fetchone()
        self.assertIsNone(people_groups.picture_view(self.store, self.conn, row, self.config,
                                                     admin=False))
        view = people_groups.picture_view(self.store, self.conn, row, self.config, admin=True)
        self.assertEqual(view["names"], [{"id": self.ann, "label": "Ann", "named": True}])
        self.assertEqual(len(view["faces"]), 1)
        box = view["faces"][0]
        self.assertEqual((box["x"], box["y"], box["w"], box["h"], box["person"]),
                         (0.1, 0.125, 0.2, 0.25, self.ann))
        visitors = dict(self.config, people={"enabled": True, "visitors": True})
        view = people_groups.picture_view(self.store, self.conn, row, visitors, admin=False)
        self.assertEqual(view, {"names": [{"id": self.ann, "label": "Ann", "named": True}]})
        other = self.conn.execute("SELECT * FROM images WHERE id = ?",
                                  (self.image[self.made[1][0]],)).fetchone()
        self.assertEqual(people_groups.picture_view(self.store, self.conn, other, visitors,
                                                    admin=False), {"names": []})

    def test_a_person_and_the_other_faces(self):
        view = people_groups.person_view(self.store, self.conn, self.config, self.ann,
                                         [self.library_id])
        self.assertEqual(view["person"]["label"], "Ann")
        self.assertEqual(sorted(item["image"] for item in view["faces"]),
                         sorted(self.image[content_hash] for content_hash in self.made[0]))
        self.assertEqual(view["similar"], [])
        # Faces much like the faces of Ann, which the owner put in a person of their
        # own: maybe the same person.
        twins = []
        for n in range(3):
            content_hash = "twin-%d" % n
            vector = unit([a + b for a, b in zip(person_vector(1, seed=70 + n),
                                                 person_vector(30, seed=n))])
            store_faces(self.store, content_hash, [face(100, 100, 200, vector)])
            insert_image(self.conn, self.library_id, "t%d.jpg" % n, content_hash)
            twins.extend(self.faces_of(content_hash))
        people_groups.move_faces(self.store, twins, None)
        view = people_groups.person_view(self.store, self.conn, self.config, self.ann,
                                         [self.library_id])
        self.assertEqual(len(view["similar"]), 1)
        self.assertGreater(view["similar"][0]["score"], 0.4)
        twin = view["similar"][0]["id"]
        people_groups.reject_faces(self.store, people_groups.person_faces(self.store, twin),
                                   twin)
        other = people_groups.other_faces(self.store, self.conn, [self.library_id])
        self.assertEqual(other["total"], 3)
        self.assertEqual(people_groups.other_faces(self.store, self.conn, [self.library_id],
                                                   "not_face")["total"], 0)

    def test_one_face_of_each_moment_first(self):
        base = 0x0F0F0F0F0F0F0F0F
        # Ann's first two pictures are one moment (a burst), the third another.
        for n, dhash in enumerate((base, base ^ 1, base ^ 0xFFFF0000)):
            self.conn.execute("UPDATE images SET dhash = ? WHERE id = ?",
                              ("%016x" % dhash, self.image[self.made[0][n]]))
        self.conn.commit()
        view = people_groups.person_view(self.store, self.conn, self.config, self.ann,
                                         [self.library_id])
        self.assertEqual([item["image"] for item in view["faces"]],
                         [self.image[self.made[0][n]] for n in (0, 2, 1)])

    def test_the_moments_of_the_pictures(self):
        first, second = self.made[0][0], self.made[0][1]
        self.conn.execute("UPDATE images SET dhash = '0f0f0f0f0f0f0f0f', taken_at = "
                          "'2020-05-01T10:00:00', taken_source = 'exif' WHERE id = ?",
                          (self.image[first],))
        # (A time from the file, which copying changes, says nothing.)
        self.conn.execute("UPDATE images SET dhash = '0000000000000000', taken_at = "
                          "'2020-05-01T10:00:01', taken_source = 'mtime' WHERE id = ?",
                          (self.image[second],))
        self.conn.commit()
        moments = people_groups.picture_moments(self.conn, {(first, "photo"),
                                                            (second, "photo")})
        self.assertEqual(moments, {(first, "photo"): (0x0F0F0F0F0F0F0F0F,
                                                      people_groups._seconds(
                                                          "2020-05-01T10:00:00")),
                                   (second, "photo"): (None, None)})
        self.assertEqual(len(people_groups.picture_moments(self.conn)), 6)

    def test_deleting_a_library_takes_its_people_and_names(self):
        import library_ops
        library_ops.delete_library_data(self.conn, self.library_id)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)
        made = self.add_people([0, 0, 3], prefix="new")
        people_groups.group(self.store, options())
        newcomer = self.person_of(made[2][0])
        self.assertEqual(people_groups.update_person(self.store, newcomer,
                                                     {"name": "Ann"})["name"], "Ann")

    def test_export_names(self):
        names = people_groups.export_names(self.conn, self.library_id)
        self.assertEqual(names, {self.image[content_hash]: ["Ann"]
                                 for content_hash in self.made[0]})
        people_groups.update_person(self.store, self.ann, {"hidden": True})
        self.assertEqual(people_groups.export_names(self.conn, self.library_id), {})

    def test_face_pictures(self):
        from PIL import Image
        picture = Image.new("RGB", (1000, 800), (20, 120, 40))
        out = io.BytesIO()
        picture.save(out, "JPEG")
        data = people_groups.crop(out.getvalue(), (0.1, 0.125, 0.2, 0.25))
        self.assertEqual(Image.open(io.BytesIO(data)).size, (people_groups.CROP_EDGE,) * 2)
        face_id = self.faces_of(self.made[1][0])[0]
        found, row = people_groups.face_source(self.store, self.conn, face_id,
                                               [self.library_id], admin=True)
        self.assertEqual(row["id"], self.image[self.made[1][0]])
        # Others: only the faces of named people.
        self.assertEqual(people_groups.face_source(self.store, self.conn, face_id,
                                                   [self.library_id], admin=False),
                         (None, None))
        face_id = self.faces_of(self.made[0][0])[0]
        self.assertIsNotNone(people_groups.face_source(self.store, self.conn, face_id,
                                                       [self.library_id], admin=False)[0])
        self.assertEqual(people_groups.face_source(self.store, self.conn, face_id, [],
                                                   admin=True), (None, None))


# ---------------------------------------------------------------------------
# The server

@unittest.skipIf(flask is None, "Flask is not installed")
@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class ServerTest(ViewsMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import serve
        from PIL import Image
        import cache
        previews = cache.PreviewStore()
        for content_hash, image_id in self.image.items():
            picture = Image.new("RGB", (1000, 800), (30, 60, 90))
            out = io.BytesIO()
            picture.save(out, "JPEG")
            previews.write(self.library_id, image_id, out.getvalue())
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.visitor = self.app.test_client()
        self.visitor.environ_base["REMOTE_ADDR"] = "192.0.2.20"
        self.addCleanup(self.app.extensions["search_images"].close)

    def admin(self, path, body=None):
        headers = {"X-Search-Images": "1"}
        if body is None:
            return self.client.get("/api/admin" + path, headers=headers)
        return self.client.post("/api/admin" + path, headers=headers, json=body)

    def visitors(self, allowed):
        self.config["people"] = {"enabled": True, "visitors": allowed}
        with open(common.config_path(), "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)

    def test_api_people_and_who_gets_it(self):
        answer = self.client.get("/api/people").get_json()
        self.assertEqual([item["label"] for item in answer["people"]],
                         ["Ann", "Person %d" % self.bob])
        again = self.client.get("/api/people", headers={
            "If-None-Match": self.client.get("/api/people").headers["ETag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(self.visitor.get("/api/people").status_code, 404)
        self.visitors(True)
        answer = self.visitor.get("/api/people?hidden=1").get_json()
        self.assertEqual([item["label"] for item in answer["people"]], ["Ann"])
        self.assertNotIn("hidden", answer["people"][0])

    def test_pages_details_and_faces(self):
        page = self.client.get("/people")
        self.assertEqual(page.status_code, 200)
        self.assertIs(support.page_config(page.get_data(as_text=True))["capabilities"]["people"],
                      True)
        self.assertIn("people.js", page.get_data(as_text=True))
        self.assertEqual(self.visitor.get("/people").status_code, 404)
        home = self.visitor.get("/").get_data(as_text=True)
        self.assertNotIn("people", support.page_config(home)["capabilities"])
        image_id = self.image[self.made[0][0]]
        detail = self.client.get("/api/image/%d" % image_id).get_json()
        self.assertEqual(detail["people"]["names"][0]["label"], "Ann")
        self.assertEqual(len(detail["people"]["faces"]), 1)
        self.assertNotIn("people", self.visitor.get("/api/image/%d" % image_id).get_json())
        face_id = self.faces_of(self.made[1][0])[0]
        picture = self.client.get("/face/%d" % face_id)
        self.assertEqual((picture.status_code, picture.mimetype), (200, "image/jpeg"))
        self.assertEqual(self.client.get("/face/%d" % face_id, headers={
            "If-None-Match": picture.headers["ETag"]}).status_code, 304)
        self.assertEqual(self.visitor.get("/face/%d" % face_id).status_code, 404)
        self.assertEqual(self.client.get("/face/999999").status_code, 404)
        self.visitors(True)
        self.assertEqual(self.visitor.get("/people").status_code, 200)
        detail = self.visitor.get("/api/image/%d" % image_id).get_json()
        self.assertEqual(detail["people"], {"names": [{"id": self.ann, "label": "Ann",
                                                       "named": True}]})
        self.assertEqual(self.visitor.get("/face/%d" % face_id).status_code, 404)
        named = self.faces_of(self.made[0][0])[0]
        self.assertEqual(self.visitor.get("/face/%d" % named).status_code, 200)

    def test_face_pictures_are_asked_for_again(self):
        face_id = self.faces_of(self.made[0][0])[0]
        first = self.client.get("/face/%d" % face_id)
        cache = first.headers["Cache-Control"]
        self.assertIn("no-cache", cache)
        self.assertIn("private", cache)
        self.assertNotIn("max-age", cache)
        again = self.client.get("/face/%d" % face_id,
                                headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(again.status_code, 304)
        self.assertIn("no-cache", again.headers["Cache-Control"])
        # A new store (face ids start again) never answers with the old crop's tag.
        self.store.close()
        people.delete_all()
        self.store = people.connect()
        self.addCleanup(self.store.close)
        self.made = self.add_people([3, 3])
        for k, hashes in self.made.items():
            for n, content_hash in enumerate(hashes):
                insert_image(self.conn, self.library_id, "q%d%d.jpg" % (k, n), content_hash)
        self.assertEqual(self.faces_of(self.made[0][0]), [face_id])
        from PIL import Image
        import cache as cache_module
        picture = Image.new("RGB", (1000, 800), (30, 60, 90))
        out = io.BytesIO()
        picture.save(out, "JPEG")
        row = self.conn.execute("SELECT id FROM images WHERE content_hash = ? ORDER BY id DESC",
                                (self.made[0][0],)).fetchone()
        cache_module.PreviewStore().write(self.library_id, row[0], out.getvalue())
        fresh = self.client.get("/face/%d" % face_id,
                                headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(fresh.status_code, 200)
        self.assertNotEqual(fresh.headers["ETag"], first.headers["ETag"])

    def test_smart_albums_that_name_a_person_are_for_who_sees_people(self):
        import albums
        albums.create_album(self.conn, "With Ann", kind="smart", query='person:"Ann" beach')
        albums.create_album(self.conn, "Beaches", kind="smart", query="beach")
        self.conn.commit()

        def listed(client):
            return sorted(album["name"] for album in client.get("/api/index").get_json()
                          ["albums"])
        self.assertEqual(listed(self.client), ["Beaches", "With Ann"])
        self.assertEqual(listed(self.visitor), ["Beaches"])
        self.visitors(True)
        self.assertEqual(listed(self.visitor), ["Beaches", "With Ann"])
        # People off: visitors see nothing of it, whatever "visitors" says.
        self.config["people"] = {"enabled": False, "visitors": True}
        with open(common.config_path(), "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        self.assertEqual(listed(self.visitor), ["Beaches"])
        self.assertEqual(self.visitor.get("/api/people").status_code, 404)
        self.assertEqual(self.visitor.get("/people").status_code, 404)
        self.assertEqual(self.client.get("/api/people").status_code, 200)
        self.assertTrue(albums.people_searches_shown(None, True))
        self.assertFalse(albums.people_searches_shown({"people": {"visitors": True}}, False))

    def test_a_plain_language_album_shows_its_words_to_visitors(self):
        # (Saved from a plain-language search: the person is an alternative to the
        # words typed. Visitors who cannot see People get the words.)
        import albums
        albums.create_album(self.conn, "At the beach", kind="smart",
                            query='person:"Ann" OR ann beach')
        albums.create_album(self.conn, "With Ann", kind="smart", query='person:"Ann" beach')
        self.conn.commit()

        def listed(client):
            return {album["name"]: album["query"] for album in
                    client.get("/api/index").get_json()["albums"]}
        self.assertEqual(listed(self.client), {"At the beach": 'person:"Ann" OR ann beach',
                                               "With Ann": 'person:"Ann" beach'})
        self.assertEqual(listed(self.visitor), {"At the beach": "ann beach"})
        self.visitors(True)
        self.assertEqual(listed(self.visitor)["At the beach"], 'person:"Ann" OR ann beach')

    def test_the_faces_route_says_when_the_person_went(self):
        gone = self.admin("/people/faces", {"action": "not_face", "faces": "all",
                                            "person": self.bob}).get_json()
        self.assertEqual((gone["changed"], gone["source"]), (3, None))
        faces = people_groups.person_faces(self.store, self.ann)
        moved = self.admin("/people/faces", {"action": "move", "faces": faces[:1], "to": None,
                                             "name": "", "person": self.ann}).get_json()
        self.assertEqual(moved["source"]["faces"], 2)
        moved = self.admin("/people/faces", {"action": "move", "faces": faces[1:], "to":
                                             moved["person"]["id"], "person": self.ann}
                           ).get_json()
        self.assertIsNone(moved["source"])

    def test_the_request_log_leaves_out_names(self):
        import logging
        import serve
        fields = ("127.0.0.1", "11/Oct/2026 19:03:45")

        def logged(line, code=200):
            record = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                                       '%s - - [%s] "%s" %s %s', fields + (line, code, "-"),
                                       None)
            self.assertTrue(serve.QueryLogFilter().filter(record))
            return record.getMessage()
        # Werkzeug logs the address decoded (uri_to_iri): a quote is a quote, an
        # accented letter itself, a space stays %20.
        for line, kept in (
                ("GET /?q=person%3A%22Zelda+Quorn%22 HTTP/1.1", "/?q=... HTTP/1.1"),
                ('GET /?q=person:"Zelda%20Quorn" HTTP/1.1', "/?q=... HTTP/1.1"),
                ('GET /?q=beach+person:"Zelda%20Quorn"&sort=oldest HTTP/1.1',
                 "/?q=...&sort=oldest HTTP/1.1"),
                ("GET /map?lib=a&q=person:Zelda HTTP/1.1", "/map?lib=a&q=... HTTP/1.1"),
                ("GET /api/embed?q=zelda HTTP/1.1", "/api/embed?q=... HTTP/1.1"),
                ('GET /api/embed?q="zelda%20quorn"%20at%20the%20beach HTTP/1.1',
                 "/api/embed?q=... HTTP/1.1"),
                # A trip page's Open in search: the trip's title.
                ("GET /?sort=oldest&ids=t.5.2-1.1-1&pick=Trip:+Zeldaville,+day+2 HTTP/1.1",
                 "/?sort=oldest&ids=...&pick=... HTTP/1.1"),
                # A place, a tag, a camera chosen (the facets), the map's centre.
                ("GET /?place=Countryland|Region|Zeldaville HTTP/1.1", "/?place=... HTTP/1.1"),
                ("GET /?place=Germany|Bavaria|M\u00fcnchen%20Zelda HTTP/1.1",
                 "/?place=... HTTP/1.1"),
                ("GET /?tags=zelda+quorn&type=photo HTTP/1.1", "/?tags=...&type=... HTTP/1.1"),
                ("GET /detail?id=39&place=Countryland|Region|Zeldaville HTTP/1.1",
                 "/detail?id=39&place=... HTTP/1.1"),
                ("GET /map?lib=all&view=48.85,2.35,12&q=zelda HTTP/1.1",
                 "/map?lib=all&view=...&q=... HTTP/1.1"),
                # A town looked for (the viewer's Place dialog), a person opened.
                ("GET /api/admin/places/towns?q=Zeldaville HTTP/1.1",
                 "/api/admin/places/towns?q=... HTTP/1.1"),
                ("GET /people?person=12 HTTP/1.1", "/people?person=12 HTTP/1.1"),
                # Odd addresses: no name or value goes either.
                ("GET /?Zelda&=Quorn&zelda%20quorn=1 HTTP/1.1", "/?...&...&... HTTP/1.1")):
            text = logged(line)
            self.assertNotIn("elda", text, line)
            self.assertIn(kept, text, line)
        # With the colours Werkzeug may add around the line (a 404).
        text = logged("\x1b[33mGET /?q=zelda HTTP/1.1\x1b[0m", 404)
        self.assertNotIn("elda", text)
        self.assertIn("/?q=... HTTP/1.1\x1b[0m", text)
        # Ids only: as they are.
        for line in ("GET /face/12 HTTP/1.1", "GET /thumb/7?v=1712345678 HTTP/1.1",
                     "GET /api/index?lib=all HTTP/1.1", "GET /static/app.js HTTP/1.1"):
            self.assertIn(line, logged(line))

    def test_admin_routes(self):
        state = self.admin("/people").get_json()
        self.assertEqual((state["groups"]["people"], state["groups"]["named"]), (2, 1))
        self.assertEqual(state["settings"]["match"], people.DEFAULTS["match"])
        view = self.admin("/people/%d" % self.ann).get_json()
        self.assertEqual(view["person"]["name"], "Ann")
        clash = self.admin("/people/%d" % self.bob, {"name": "ann"})
        self.assertEqual((clash.status_code, clash.get_json()["other"]), (409, self.ann))
        self.assertEqual(self.admin("/people/%d" % self.bob, {"name": "Bob"}).get_json()
                         ["person"]["label"], "Bob")
        faces = view["faces"]
        moved = self.admin("/people/faces", {"action": "move", "faces": [faces[0]["id"]],
                                             "to": None, "name": "Cal"}).get_json()
        self.assertEqual(moved["person"]["name"], "Cal")
        rejected = self.admin("/people/faces", {"action": "reject", "faces": [faces[1]["id"]],
                                                "person": self.ann}).get_json()
        self.assertEqual(rejected["removed"], 1)
        self.assertEqual(self.admin("/people/faces", {"action": "not_face",
                                                      "faces": [faces[1]["id"]]}).get_json(),
                         {"changed": 1})
        other = self.admin("/people/other?which=not_face").get_json()
        self.assertEqual([item["id"] for item in other["faces"]], [faces[1]["id"]])
        merged = self.admin("/people/%d/merge" % moved["person"]["id"],
                            {"into": self.ann}).get_json()
        self.assertEqual(merged["person"]["faces"], 2)
        everything = self.admin("/people/faces", {"action": "not_face", "faces": "all",
                                                  "person": self.bob}).get_json()
        self.assertEqual(everything["changed"], 3)
        for bad in ({"action": "nope", "faces": [1]}, {"action": "reject", "faces": [1]},
                    {"action": "move", "faces": "all"}, {"action": "move", "faces": [1],
                                                         "to": "x"}):
            self.assertEqual(self.admin("/people/faces", bad).status_code, 400, bad)
        self.assertEqual(self.admin("/people/999").status_code, 404)
        self.assertEqual(self.admin("/people/other?which=x").status_code, 400)
        # Grouping again is a job of every library.
        self.assertEqual(self.admin("/jobs", {"kind": "group_faces",
                                              "library_id": self.library_id}).status_code, 400)
        self.assertEqual(self.admin("/jobs", {"kind": "group_faces"}).status_code, 202)
        # Without the admin's header (another site's page): refused.
        self.assertEqual(self.client.post("/api/admin/people/%d" % self.ann,
                                          json={"name": "X"}).status_code, 403)
        self.assertEqual(self.visitor.get("/api/admin/people").status_code, 403)


# ---------------------------------------------------------------------------
# The jobs

class JobTest(StoreMixin, unittest.TestCase):

    def test_the_group_faces_job(self):
        self.add_people([3, 3])
        self.store.close()
        summary = jobs.run_group_faces(jobs.JobContext("test"), None,
                                       {"people": {"enabled": True}}, {})
        self.assertEqual(summary["people"], 2)
        with self.assertRaises(jobs.JobError):
            jobs.run_group_faces(jobs.JobContext("test"), None, {}, {})
        people.delete_all()
        with self.assertRaises(jobs.JobError):
            jobs.run_group_faces(jobs.JobContext("test"), None, {"people": {"enabled": True}},
                                 {})
        self.store = people.connect()
        self.addCleanup(self.store.close)


@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class FacesRunTest(test_people.PeopleFixture, unittest.TestCase):
    """The faces job groups the faces it found (the stand-in helper of test_people)."""

    def test_a_run_groups_what_it_found_and_only_then(self):
        self.configure(people_settings={"min_faces": 1})
        summary = self.update()
        self.assertEqual(summary["looked"], 5)
        # (Squares 1, 2 and 3 are three made-up people; 4 is far from a 360 photo's
        # horizon: not kept.)
        self.assertEqual((summary["people"]["people"], summary["people"]["new"]), (3, 3))
        self.assertIsNone(self.update()["people"])
        self.configure(people_settings={"min_faces": 2})
        self.assertEqual(self.update()["people"]["people"], 1)
        store = people.connect(create=False)
        try:
            answer = people_groups.search_data(store, self.conn, self.config,
                                               ["lib_family1", "lib_trips01"], True)
        finally:
            store.close()
        self.assertEqual(answer["people"][0]["ids"], sorted([self.two, self.copy, self.video]))


class PageScriptTest(unittest.TestCase):

    def read(self, *parts):
        with open(os.path.join(support.REPO, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_people_page_finds_its_elements(self):
        import re
        html = self.read("templates", "people.html")
        script = self.read("static", "people.js")
        present = set(re.findall(r'\sid="([^"]+)"', html))
        wanted = set(re.findall(r'(?:byId|getElementById)\("([A-Za-z0-9_-]+)"\)', script))
        # (The grid of other faces and its "Show more" are made by the script.)
        self.assertEqual(wanted - present, {"people-faces", "people-more"})
        self.assertIn("people.js", html)
        self.assertIn("people.css", html)

    def test_smart_album_field_names_are_search_cores(self):
        import re
        import albums
        script = self.read("static", "search-core.js")
        block = re.search(r"var FIELD_NAMES = dict\(\{(.*?)\}\);", script, re.S).group(1)
        names = dict(re.findall(r"([a-z]+): \"([a-z]+)\"", block))
        self.assertEqual(names, albums.FIELD_NAMES)

    def test_exports_never_hold_the_people_page(self):
        import export
        for name in ("people.js", "people.css"):
            self.assertNotIn(name, export.ASSETS + export.MAP_ASSETS + export.MEANING_ASSETS)
        self.assertIs(export.DEFAULT_OPTIONS["people_names"], False)


# ---------------------------------------------------------------------------
# Exports: only the names, only with people_names

@unittest.skipIf(not support.pillow_available(), "Pillow is not installed")
class ExportTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import describe
        import scan
        from test_scan import write_jpeg
        from test_export_albums import info
        folder = self.base.library_folder("Family")
        for number in range(6):
            write_jpeg(os.path.join(folder, "p%d.jpg" % number), seed=number + 1)
        self.base.write_config([support.library("Family", folder)])
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        scan.run(self.config, jobs.JobContext("test"), describe_images=False)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.library_id = self.config["libraries"][0]["id"]
        rows = self.conn.execute("SELECT id, path, content_hash FROM images ORDER BY path").fetchall()
        for row in rows:
            self.conn.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ? WHERE "
                              "id = ?", (json.dumps(info("Picture " + row["path"])),
                                         describe.PROMPT_VER, row["id"]))
        self.conn.commit()
        self.ids = {row["path"]: row["id"] for row in rows}
        store = people.connect()
        try:
            for n, row in enumerate(rows):
                k = 1 if n < 3 else 2
                store_faces(store, row["content_hash"],
                            [face(10, 10, 30, person_vector(k, seed=n))], width=64, height=48)
            people_groups.group(store, options())
            persons = {}
            for person_id, content_hash in store.execute(
                    "SELECT fp.person_id, f.content_hash FROM face_people fp JOIN faces f ON "
                    "f.id = fp.face_id"):
                persons.setdefault(person_id, set()).add(content_hash)
            self.assertEqual(len(persons), 2)
            first = [person for person, hashes in persons.items() if rows[0]["content_hash"]
                     in hashes][0]
            people_groups.update_person(store, first, {"name": "Zelda Quorn"})
            self.unnamed = [people_groups.label(person, "") for person in persons
                            if person != first][0]
        finally:
            store.close()
        self.out = tempfile.mkdtemp(prefix="people-export-")
        self.addCleanup(support.remove_tree, self.out)

    def export(self, **options):
        import export
        target = os.path.join(self.out, "site-%d" % len(os.listdir(self.out)))
        export.export_library(jobs.JobContext("test"), self.conn, self.config, self.library_id,
                              target, options)
        return target

    def text_of(self, folder):
        found = []
        for root, _dirs, names in os.walk(folder):
            for name in names:
                with open(os.path.join(root, name), "rb") as handle:
                    found.append(handle.read())
        return b"\n".join(found)

    def test_smart_albums_that_name_a_person_are_not_published(self):
        import albums
        albums.create_album(self.conn, "Zelda's album", kind="smart",
                            query='person:"Zelda Quorn" beach')
        albums.create_album(self.conn, "Not her", kind="smart", query="-person:Zelda")
        albums.create_album(self.conn, "Pictures", kind="smart", query="picture")
        self.conn.commit()
        self.assertEqual([term["field"] for term in albums.query_terms(
            'person:"Zelda Quorn" beach')], ["person"])
        for people_names in (False, True):
            out = self.export(marks=True, people_names=people_names)
            with open(os.path.join(out, "data", "manifest.js"), encoding="utf-8") as handle:
                manifest = handle.read()
            self.assertIn("Pictures", manifest)
            self.assertNotIn("person:", manifest)
            self.assertNotIn("Not her", manifest)
            if not people_names:
                self.assertNotIn(b"zelda", self.text_of(out).lower())

    def test_a_plain_language_album_goes_as_its_words_with_names(self):
        import albums
        albums.create_album(self.conn, "At the beach", kind="smart",
                            query='person:"Zelda Quorn" OR zelda picture')
        self.conn.commit()
        out = self.export(marks=True)
        with open(os.path.join(out, "data", "manifest.js"), encoding="utf-8") as handle:
            self.assertNotIn("At the beach", handle.read())
        self.assertNotIn(b"zelda", self.text_of(out).lower())
        out = self.export(marks=True, people_names=True)
        from test_export import read_manifest
        published = {album["name"]: album for album in read_manifest(out)["albums"]}
        self.assertEqual(published["At the beach"]["query"], "zelda picture")

    def test_names_go_only_with_the_option_and_only_as_tags(self):
        from test_export import read_records
        out = self.export()
        text = self.text_of(out).lower()
        for secret in (b"zelda", b"quorn", self.unnamed.lower().encode("ascii")):
            self.assertNotIn(secret, text, secret)
        out = self.export(people_names=True)
        records = {record["t"]: record for record in read_records(out)}
        for number in range(6):
            tags = records["Picture p%d.jpg" % number].get("g") or []
            self.assertEqual("zelda quorn" in tags, number < 3, (number, tags))
        self.assertNotIn(self.unnamed.lower().encode("ascii"), self.text_of(out).lower())


if __name__ == "__main__":
    unittest.main()
