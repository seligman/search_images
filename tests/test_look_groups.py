"""Tests for duplicates by look: look vectors in the duplicate groups (search_index) and
"Same picture, any date" (search_index.look_copies, duplicates.build_same_stacks, and
what the Duplicates page's buttons do there).

The look vectors are made up here (random unit vectors, and others at a
chosen cosine from them); the rows are written straight into a temporary
index.db and the vectors into its data/image_vectors.db.
"""

import math
import os
import random
import struct
import unittest
from unittest import mock

import support

import albums
import common
import db
import duplicates
import looks
import not_duplicates
import prompts
import search_index
from test_admin import AdminFixture

DIMS = 64
NOW = 1700000000.0
DAY = "2019-07-04T10:15:00"
# dHashes with detail (about half their bits set), far apart from each other.
HASH_A = "0f0f0f0f0f0f0f0f"
HASH_A1 = "0f0f0f0f0f0f0f0e"       # one bit from HASH_A
HASH_A2 = "0f0f0f0f0f0f0f0d"       # one bit from HASH_A, two from HASH_A1
HASH_B = "33cc33cc55aa55aa"
HASH_C = "6996699669966996"
# A dHash of a picture with little detail (search_index.LOW_DETAIL_BITS).
FLAT = "0000000000010003"


def unit(rng):
    values = [rng.gauss(0.0, 1.0) for _ in range(DIMS)]
    size = math.sqrt(sum(value * value for value in values))
    return [value / size for value in values]


def turned(base, rng, cosine):
    """A unit vector at the given cosine from base (a unit vector)."""
    other = unit(rng)
    along = sum(a * b for a, b in zip(other, base))
    other = [a - along * b for a, b in zip(other, base)]
    size = math.sqrt(sum(value * value for value in other))
    sine = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return [cosine * b + sine * a / size for a, b in zip(other, base)]


def packed(vector, scale=127):
    """A unit vector as looks.py keeps it: signed bytes, times 127."""
    return struct.pack("%db" % len(vector),
                       *[max(-127, min(127, int(round(value * scale)))) for value in vector])


def cosine(first, second):
    return search_index._look_cosine(search_index._look(first), search_index._look(second))


def near(base, rng, low, high):
    """Packed look of a unit vector whose cosine to base (packed) is from low to high."""
    for _ in range(500):
        look = packed(turned(base, rng, (low + high) / 2.0))
        if low <= cosine(packed(base), look) <= high:
            return look
    raise AssertionError("no vector at %s to %s" % (low, high))


def background(rng, count=24, start_id=1000):
    """Pictures of other days with unrelated looks."""
    items = []
    for index in range(count):
        items.append((start_id + index, "%016x" % rng.getrandbits(64), "bg%d" % index,
                      "2010-01-%02dT12:00:00" % (index + 1), 4 / 3, packed(unit(rng))))
    return items


class CosineTest(unittest.TestCase):

    def test_divided_by_the_vectors_own_lengths(self):
        rng = random.Random(4)
        base = unit(rng)
        for wanted in (0.2, 0.9, 0.99, 0.999):
            self.assertAlmostEqual(cosine(packed(base), packed(turned(base, rng, wanted))),
                                   wanted, delta=0.004)
        # Rounded to whole numbers, a look's length is not exactly 127: with
        # itself (or the same look at another length) the cosine is still 1.
        for _ in range(20):
            look = packed(unit(rng))
            self.assertAlmostEqual(cosine(look, look), 1.0, places=12)
        self.assertAlmostEqual(cosine(packed(base), packed(base, scale=60)), 1.0, delta=0.001)
        self.assertEqual(search_index._look_cosine(search_index._look(b"\x01\x02"),
                                                   search_index._look(b"\x01")), -2.0)
        self.assertIsNone(search_index._look(bytes(DIMS)))
        self.assertIsNone(search_index._look(b""))


class LookGroupTest(unittest.TestCase):
    """duplicate_groups with looks, on made-up items."""

    def setUp(self):
        self.rng = random.Random(7)
        self.base = unit(self.rng)
        self.a = packed(self.base)

    def groups(self, items, **options):
        everything = list(items) + background(random.Random(99))
        found = search_index.duplicate_groups(everything, **options)
        return [ids for ids in found if ids[0] < 1000]

    def look(self, low, high):
        return near(self.base, self.rng, low, high)

    def test_alike_looks_stack_within_a_minute(self):
        alike = self.look(0.986, 0.989)
        items = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                 (2, HASH_B, "b", "2019-07-04T10:15:50", 4 / 3, alike)]
        self.assertEqual(self.groups(items), [[1, 2]])
        # Without looks (or with the look rule off) their dHashes keep them apart.
        self.assertEqual(self.groups([item[:5] for item in items]), [])
        self.assertEqual(self.groups(items, look_threshold=None), [])
        # Taken a minute and a half apart, that is not close enough.
        later = [items[0], (2, HASH_B, "b", "2019-07-04T10:16:30", 4 / 3, alike)]
        self.assertEqual(self.groups(later), [])

    def test_the_same_look_stacks_within_ten_minutes(self):
        same = self.look(0.996, 0.999)
        items = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                 (2, HASH_B, "b", "2019-07-04T10:20:00", 4 / 3, same)]
        self.assertEqual(self.groups(items), [[1, 2]])
        later = [items[0], (2, HASH_B, "b", "2019-07-04T10:35:00", 4 / 3, same)]
        self.assertEqual(self.groups(later), [])
        # Across midnight, by the clock.
        night = [(1, HASH_A, "a", "2019-07-04T23:58:00", 4 / 3, self.a),
                 (2, HASH_B, "b", "2019-07-05T00:02:00", 4 / 3, same)]
        self.assertEqual(self.groups(night), [[1, 2]])

    def test_not_without_a_time(self):
        same = self.look(0.997, 1.0)
        for taken in ("2019-07-07T10:15:00", "", "2019-07-04"):
            items = [(1, HASH_A, "a", DAY, 4 / 3, self.a), (2, HASH_B, "b", taken, 4 / 3, same)]
            self.assertEqual(self.groups(items), [], taken)
        # Two pictures dated by the day alone (a file name without a time).
        days = [(1, HASH_A, "a", "2019-07-04", 4 / 3, self.a),
                (2, HASH_B, "b", "2019-07-04", 4 / 3, same)]
        self.assertEqual(self.groups(days), [])

    def test_a_second_many_pictures_share_is_no_moment(self):
        same = self.look(0.997, 1.0)
        pair = [(1, HASH_A, "a", DAY, 4 / 3, self.a), (2, HASH_B, "b", DAY, 4 / 3, same)]
        self.assertEqual(self.groups(pair), [[1, 2]])
        # A camera whose clock was never set: BURST_GROUPS pictures of one second.
        rng = random.Random(3)
        crowd = [(10 + index, "%016x" % rng.getrandbits(64), "x%d" % index, DAY, 4 / 3,
                  packed(unit(rng))) for index in range(search_index.BURST_GROUPS)]
        self.assertEqual([ids for ids in self.groups(pair + crowd) if 1 in ids], [])

    def test_shapes_and_how_close(self):
        same = self.look(0.997, 1.0)
        wide = [(1, HASH_A, "a", DAY, 4 / 3, self.a), (2, HASH_B, "b", DAY, 16 / 9, same)]
        self.assertEqual(self.groups(wide), [])
        far = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
               (2, HASH_B, "b", DAY, 4 / 3, self.look(0.94, 0.96))]
        self.assertEqual(self.groups(far), [])
        # An explicit threshold.
        self.assertEqual(self.groups(far, look_threshold=0.9), [[1, 2]])

    def test_groups_join_whole_and_nothing_chains(self):
        # A dHash pair (the second's look is unlike) and a picture whose look
        # is the pair's first: one stack.
        pair = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                (2, HASH_A1, "b", "2019-07-04T10:15:01", 4 / 3, self.look(0.93, 0.95)),
                (3, HASH_C, "c", "2019-07-04T10:15:30", 4 / 3, self.look(0.996, 0.999))]
        self.assertEqual(self.groups(pair), [[1, 2, 3]])
        # A ~ B and B ~ C, but A and C are further apart: C is not stacked
        # with A (looks are compared with the leader of a stack only).
        rng = random.Random(11)
        other = turned(self.base, rng, 0.0)
        step = math.acos(0.9925)

        def at(angle):
            return packed([math.cos(angle) * a + math.sin(angle) * b
                           for a, b in zip(self.base, other)])
        self.assertLess(cosine(at(0), at(2 * step)), search_index.LOOK_STACK_COSINE)
        # (Seconds apart: not one burst, where C would join through B.)
        chain = [(1, HASH_A, "a", DAY, 4 / 3, at(0)),
                 (2, HASH_B, "b", "2019-07-04T10:15:10", 4 / 3, at(step)),
                 (3, HASH_C, "c", "2019-07-04T10:15:20", 4 / 3, at(2 * step))]
        self.assertEqual(self.groups(chain), [[1, 2]])

    def test_a_single_picture_joins_through_the_burst_frame_it_matches(self):
        frame = self.look(0.94, 0.95)
        rng = random.Random(12)
        unpacked = list(struct.unpack("%db" % DIMS, frame))
        size = math.sqrt(sum(value * value for value in unpacked))
        next_frame = near([value / size for value in unpacked], rng, 0.995, 0.999)
        self.assertLess(cosine(self.a, next_frame), search_index.LOOK_STACK_COSINE)
        burst = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                 (2, HASH_A1, "b", "2019-07-04T10:15:05", 4 / 3, frame)]
        single = (3, HASH_C, "c", "2019-07-04T10:15:06", 4 / 3, next_frame)
        self.assertEqual(self.groups(burst), [[1, 2]])
        self.assertEqual(self.groups(burst + [single]), [[1, 2, 3]])
        # Not through the stack's first picture alone, nor seconds later.
        self.assertEqual(self.groups(burst[:1] + [single]), [])
        later = (3, HASH_C, "c", "2019-07-04T10:15:09", 4 / 3, next_frame)
        self.assertEqual(self.groups(burst + [later]), [[1, 2]])

    def test_pictures_with_little_detail_stack_by_their_looks(self):
        items = [(1, FLAT, "a", DAY, 4 / 3, self.a),
                 (2, FLAT[:-1] + "7", "b", "2019-07-04T10:15:05", 4 / 3,
                  self.look(0.996, 0.999))]
        self.assertEqual(self.groups(items), [[1, 2]])
        self.assertEqual(self.groups([item[:5] for item in items]), [])

    def test_looks_that_differ_keep_close_dhashes_apart(self):
        # Two coins on the same cloth: dHashes a bit apart, looks unrelated.
        items = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                 (2, HASH_A1, "b", "2019-07-04T10:15:20", 4 / 3, self.look(0.05, 0.15))]
        self.assertEqual(self.groups(items), [])
        self.assertEqual(self.groups(items, look_apart=None), [[1, 2]])
        self.assertEqual(self.groups([item[:5] for item in items]), [[1, 2]])
        # Identical files always stay together.
        copies = [(1, HASH_A, "same", DAY, 4 / 3, self.a),
                  (2, HASH_A, "same", DAY, 4 / 3, self.a)]
        self.assertEqual(self.groups(copies), [[1, 2]])

    def test_every_picture_of_a_group_counts_for_keeping_apart(self):
        # b and c are each 0.9 from a (the stack's first), but 0.62 from each other.
        rng = random.Random(13)
        other = turned(self.base, rng, 0.0)
        sine = math.sqrt(1 - 0.9 * 0.9)
        b = packed([0.9 * x + sine * y for x, y in zip(self.base, other)])
        c = packed([0.9 * x - sine * y for x, y in zip(self.base, other)])
        self.assertGreater(cosine(self.a, c), search_index.LOOK_APART)
        self.assertLess(cosine(b, c), search_index.LOOK_APART)
        items = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                 (2, HASH_A1, "b", "2019-07-04T10:15:10", 4 / 3, b),
                 (3, HASH_A2, "c", "2019-07-04T10:15:20", 4 / 3, c)]
        self.assertEqual(self.groups(items), [[1, 2]])

    def test_alike_things_minutes_apart_are_kept_apart(self):
        # The other side of a mug, photographed again: close dHashes, looks 0.9.
        alike = self.look(0.89, 0.91)
        soon = [(1, HASH_A, "a", DAY, 4 / 3, self.a),
                (2, HASH_A1, "b", "2019-07-04T10:15:40", 4 / 3, alike)]
        self.assertEqual(self.groups(soon), [[1, 2]])
        later = [soon[0], (2, HASH_A1, "b", "2019-07-04T10:20:00", 4 / 3, alike)]
        self.assertEqual(self.groups(later), [])
        # ... but the same view taken again stacks by its dHash.
        again = [soon[0], (2, HASH_A1, "b", "2019-07-04T10:20:00", 4 / 3,
                           self.look(0.95, 0.97))]
        self.assertEqual(self.groups(again), [[1, 2]])
        # Without times to compare, only the plain cut-off counts.
        undated = [soon[0], (2, HASH_A1, "b", "", 4 / 3, alike)]
        self.assertEqual(self.groups(undated), [[1, 2]])

    def test_no_group_holds_two_pictures_that_look_too_different(self):
        rng = random.Random(17)
        bases = [unit(rng) for _ in range(4)]
        items = []
        for index in range(160):
            base = bases[index % 4]
            look = packed(turned(base, rng, rng.uniform(0.6, 1.0)))
            dhash = "0f0f0f0f0f0f0f%02x" % (0x0f ^ (1 << rng.randrange(8)))
            items.append((index + 1, dhash, "p%d" % index,
                          "2019-07-04T10:%02d:%02d" % (index // 8, rng.randrange(60)), 4 / 3,
                          look))
        found = search_index.duplicate_groups(items)
        self.assertTrue(any(len(ids) > 2 for ids in found))
        by_id = {item[0]: item for item in items}
        for ids in found:
            for first in ids:
                for second in ids:
                    if first < second:
                        one, other = by_id[first], by_id[second]
                        self.assertFalse(search_index._any_apart(
                            [(search_index._moment(one[3]), search_index._look(one[5]))],
                            [(search_index._moment(other[3]), search_index._look(other[5]))],
                            search_index.LOOK_APART), (first, second))

    def test_new_pictures_and_new_looks(self):
        rng = random.Random(5)
        items = background(rng, 30, start_id=1)
        for index in range(31, 61):
            moment = "2019-08-%02dT10:00:%02d" % (1 + index % 20, index % 7)
            items.append((index, "%016x" % rng.getrandbits(64), "n%d" % index, moment, 4 / 3,
                          packed(turned(self.base, rng, 0.999 if index % 3 else 0.5))))
        expected = search_index.duplicate_groups(items)
        self.assertTrue(any(len(ids) > 1 for ids in expected))
        with support.TempBase():
            first = search_index.library_groups("lib_test0001", items[:45])
            self.assertEqual(first, search_index.duplicate_groups(items[:45]))
            grouper = search_index._groups_memo["lib_test0001"][2]
            # Only newer pictures: added to the groups in memory, as from scratch.
            self.assertEqual(search_index.library_groups("lib_test0001", items), expected)
            self.assertIs(search_index._groups_memo["lib_test0001"][2], grouper)
            # A picture that got its look later: worked out again.
            changed = [item[:5] + (None,) if item[0] == 31 else item for item in items]
            self.assertEqual(search_index.library_groups("lib_test0001", changed),
                             search_index.duplicate_groups(changed))
            self.assertEqual(search_index.library_groups("lib_test0001", items), expected)
            # From the groups file too (nothing in memory).
            search_index._groups_memo.clear()
            self.assertEqual(search_index.library_groups("lib_test0001", items), expected)

    def test_hiding_a_picture_leaves_the_other_stacks_alone(self):
        # The cut-offs do not depend on the rest of the library.
        rng = random.Random(8)
        items = background(rng, 60, start_id=1)
        for index in range(61, 121):
            moment = "2019-08-%02dT10:00:%02d" % (1 + index % 20, index % 7)
            items.append((index, "%016x" % rng.getrandbits(64), "n%d" % index, moment, 4 / 3,
                          packed(turned(self.base, rng, rng.choice((0.987, 0.995, 0.6))))))
        expected = search_index.duplicate_groups(items)
        for gone in (5, 17, 33):
            left = [item for item in items if item[0] != gone]
            self.assertEqual(search_index.duplicate_groups(left), expected, gone)


class LookCopiesTest(unittest.TestCase):

    def test_planted_copies_found_with_or_without_bands(self):
        rng = random.Random(21)
        items = []
        planted = []
        for index in range(400):
            items.append((index + 1, "h%d" % index, 4 / 3, packed(unit(rng))))
        # Copies at 0.996 to 0.999 of some of them, and an identical file.
        for number, source in enumerate((5, 60, 61, 200, 333)):
            base = list(struct.unpack("%db" % DIMS, items[source - 1][3]))
            size = math.sqrt(sum(value * value for value in base))
            copy = packed(turned([value / size for value in base], rng, 0.996 + 0.0007 * number))
            items.append((1000 + number, "copy%d" % number, 4 / 3, copy))
            planted.append([source, 1000 + number])
        items.append((2000, "h16", 4 / 3, items[16][3]))           # the same file as 17
        found = search_index.look_copies(items)
        self.assertEqual(found, search_index.look_copies(items, banded=False))
        for pair in planted:
            self.assertIn(pair, found)
        self.assertIn([17, 2000], found)
        self.assertEqual(len(found), len(planted) + 1)
        # A stricter threshold leaves the furthest copy out.
        strict = search_index.look_copies(items, 0.9985)
        self.assertNotIn(planted[0], strict)
        self.assertEqual(strict, search_index.look_copies(items, 0.9985, banded=False))

    def test_a_large_set(self):
        # More pictures than are compared one by one (LOOK_BAND_MIN).
        rng = random.Random(23)
        items = [(index, "x%d" % index, 1.5, packed(unit(rng))) for index in range(1, 700)]
        base = list(struct.unpack("%db" % DIMS, items[100][3]))
        size = math.sqrt(sum(value * value for value in base))
        items.append((900, "y", 1.5, packed(turned([value / size for value in base], rng, 0.998))))
        found = search_index.look_copies(items)
        self.assertEqual(found, [[101, 900]])
        self.assertEqual(found, search_index.look_copies(items, banded=False))

    def test_shapes_and_missing_looks(self):
        rng = random.Random(22)
        base = unit(rng)
        items = [(1, "a", 4 / 3, packed(base)),
                 (2, "b", 16 / 9, packed(turned(base, rng, 0.999))),
                 (3, "c", None, packed(turned(base, rng, 0.999))),
                 (4, "d", 4 / 3, None),
                 (5, "e", 4 / 3, b"\x01\x02"),                  # another length: left out
                 (6, "f", 4 / 3, bytes(DIMS))]                  # no length: left out
        self.assertEqual(search_index.look_copies(items), [[1, 3]])
        self.assertEqual(search_index.look_copies([]), [])
        self.assertEqual(search_index.look_copies(items[:1]), [])


class LibraryFixture(support.TempBaseMixin):
    """Libraries whose rows are written straight into index.db, and their looks."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Family")
        self.base.write_config([support.library("Family", self.folder, id="lib_family1"),
                                support.library("Trips", self.base.library_folder("Trips"),
                                                id="lib_trips01")])
        db.init(log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.config = common.load_config()
        self.rng = random.Random(31)
        self.vectors = []                       # (content hash, kind, bytes)

    def add(self, path, dhash, taken, look=None, kind=looks.KIND_PHOTO, **fields):
        content = fields.pop("content_hash", "c-" + path)
        row = {"library_id": "lib_family1", "path": path, "name": path.split("/")[-1],
               "size": 100, "mtime": NOW, "ctime": NOW, "width": 4000, "height": 3000,
               "added": NOW, "updated": NOW, "dhash": dhash, "content_hash": content,
               "taken_at": taken, "taken_source": "exif" if taken else "",
               "described": 1, "prompt_ver": prompts.PROMPT_VER,
               "info": '{"title": "A picture", "photo_type": "photo"}'}
        row.update(fields)
        columns = sorted(row)
        cursor = self.conn.execute("INSERT INTO images (%s) VALUES (%s)" % (
            ",".join(columns), ",".join("?" * len(columns))), [row[column] for column in columns])
        self.conn.commit()
        if look is not None:
            self.vectors.append((content, kind, look))
        return cursor.lastrowid

    def add_background(self, count=20):
        for index in range(count):
            self.add("bg/%02d.jpg" % index, "%016x" % self.rng.getrandbits(64),
                     "2010-02-%02dT09:00:00" % (index + 1), packed(unit(self.rng)))

    def store(self):
        store = looks.connect()
        try:
            if not looks.meta(store)["dims"]:
                looks.reset(store, "test-recipe", DIMS)
            looks.write(store, self.vectors)
        finally:
            store.close()
        self.vectors = []

    def records(self, **options):
        return {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, ["lib_family1"], **options)}


class BuildRecordsTest(LibraryFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        base = unit(self.rng)
        self.base = base
        self.add_background()
        self.one = self.add("a/1.jpg", HASH_A, DAY, packed(base))
        self.two = self.add("a/2.jpg", HASH_B, "2019-07-04T10:15:20",
                            packed(turned(base, self.rng, 0.998)))
        # Alike looks of pictures that never stack by them.
        self.video = self.add("a/clip.mp4", "", "2019-07-04T10:15:30",
                              packed(turned(base, self.rng, 0.999)), kind=looks.KIND_POSTER,
                              media="video", duration=12.0)
        # The next clip of a drive: its poster looks the same.
        self.clip = self.add("a/clip2.mp4", "", "2019-07-04T10:15:40",
                             packed(turned(base, self.rng, 0.999)), kind=looks.KIND_POSTER,
                             media="video", duration=60.0)
        self.pano = self.add("a/pano.jpg", HASH_C, "2019-07-04T10:15:10",
                             packed(turned(base, self.rng, 0.999)), kind=looks.KIND_PANO,
                             projection="equirectangular", width=6000, height=3000)
        self.page = self.add("a/page.jpg", "3c3c3c3c3c3c3c3c", "2019-07-04T10:15:25",
                             packed(turned(base, self.rng, 0.999)),
                             info='{"title": "A form", "photo_type": "document"}')
        self.hidden = self.add("a/hidden.jpg", "5a5a5a5a5a5a5a5a", "2019-07-04T10:15:15",
                               packed(turned(base, self.rng, 0.999)), hidden=1)

    def test_records_and_exports_stack_by_look(self):
        records = self.records()
        self.assertEqual(records[self.one].get("dg"), records[self.two].get("dg"))
        self.assertIsNone(records[self.one].get("dg"))       # no looks yet
        self.store()
        records = self.records()
        self.assertIsNotNone(records[self.one].get("dg"))
        self.assertEqual(records[self.one]["dg"], records[self.two]["dg"])
        for other in (self.video, self.clip, self.pano, self.page, self.hidden):
            self.assertNotIn("dg", records[other], other)
        exported = self.records(for_export=True)
        self.assertEqual(exported[self.one]["dg"], exported[self.two]["dg"])
        self.assertNotIn(self.hidden, exported)

    def test_a_favorite_is_the_cover(self):
        self.store()
        records = self.records()
        cover = records[self.one]["dg"]
        other = self.two if cover == self.one else self.one
        albums.set_marks(self.conn, [other], favorite=True)
        self.conn.commit()
        self.assertEqual(self.records()[self.one]["dg"], other)
        # An export without its "marks" option knows nothing of favorites.
        self.assertEqual(self.records(for_export=True)[self.one]["dg"], cover)

    def test_the_index_answer_follows_new_looks(self):
        service = search_index.IndexService(watch=False)
        self.addCleanup(service.close)
        libraries = {"lib_family1": "Family"}
        status, body, headers = service.respond(self.conn, self.config, libraries, True)
        self.assertEqual(status, 200)
        first = headers["ETag"]
        before = looks.store_key()
        self.store()
        self.assertNotEqual(looks.store_key(), before)
        status, body, headers = service.respond(
            self.conn, self.config, libraries, True,
            etag_matches=lambda tag: '"%s"' % tag == first)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["ETag"], first)
        records = {record["i"]: record for record in support.strict_json(
            body.decode("utf-8"))["records"]}
        self.assertEqual(records[self.one]["dg"], records[self.two]["dg"])
        # Unchanged: 304.
        again = headers["ETag"]
        status, _body, _headers = service.respond(
            self.conn, self.config, libraries, True,
            etag_matches=lambda tag: '"%s"' % tag == again)
        self.assertEqual(status, 304)


class StoreKeyTest(support.TempBaseMixin, unittest.TestCase):

    def test_changes_with_the_kept_looks(self):
        self.assertIsNone(looks.store_key())
        store = looks.connect()
        self.addCleanup(store.close)
        looks.reset(store, "test-recipe", DIMS)
        first = looks.store_key()
        self.assertIsNotNone(first)
        self.assertEqual(looks.store_key(), first)
        looks.write(store, [("c1", looks.KIND_PHOTO, packed(unit(random.Random(1))))])
        second = looks.store_key()
        self.assertNotEqual(second, first)
        self.assertEqual(second[2], str(DIMS))


class SamePictureTest(LibraryFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        base = unit(self.rng)
        self.base = base
        self.add_background()
        # A burst stacked by its dHashes, the first of which was saved again
        # years later (another file, a dHash further off).
        self.first = self.add("2019/1.jpg", HASH_A, DAY, packed(base))
        self.second = self.add("2019/2.jpg", HASH_A1, "2019-07-04T10:15:01",
                               packed(turned(base, self.rng, 0.95)))
        self.again = self.add("2023/1-copy.jpg", HASH_C, "2023-01-02T08:00:00",
                              packed(turned(base, self.rng, 0.999)))
        # Without a date to go by.
        other = unit(self.rng)
        self.sent = self.add("chat/a.jpg", HASH_B, "", packed(other), taken_source="mtime")
        self.saved = self.add("chat/b.jpg", "5a5a5a5a5a5a5a5a", "",
                              packed(turned(other, self.rng, 0.998)), taken_source="mtime")
        self.store()

    def page(self, libraries=("lib_family1",), **options):
        duplicates._memo.clear()
        return duplicates.review_page(self.conn, self.config, list(libraries), **options)

    def test_pictures_that_look_the_same_whatever_their_dates(self):
        result = self.page(kind="same")
        self.assertEqual(result["kind"], "same")
        self.assertEqual(result["counts"]["same"], 2)
        self.assertEqual(result["looked"], 25)
        stacks = {tuple(sorted(member["id"] for member in stack["members"])): stack
                  for stack in result["stacks"]}
        self.assertEqual(set(stacks), {(self.first, self.again), (self.sent, self.saved)})
        stack = stacks[(self.first, self.again)]
        self.assertEqual(stack["kind"], "same")
        self.assertNotIn("libraries", stack)
        members = {member["id"]: member for member in stack["members"]}
        # Its stack on the search page (as dg), and how many pictures that holds.
        dg = self.records()[self.first]["dg"]
        self.assertEqual((members[self.first]["stack"], members[self.first]["stacked"]), (dg, 2))
        self.assertNotIn("stacked", members[self.again])
        self.assertNotIn("library", members[self.again])
        self.assertTrue(stack["reason"])
        # The search page's own stacks are as before.
        self.assertEqual(self.page()["counts"]["all"], 1)
        # One stack by a picture in it.
        one = self.page(kind="same", stack_id=self.again)
        self.assertEqual([stack["id"] for stack in one["stacks"]], [stack["id"]])
        self.assertEqual(self.page(kind="same", stack_id=self.second)["stacks"], [])

    def test_already_stacked_hidden_and_kept_apart(self):
        # Pictures the search page stacks together are not listed again.
        self.conn.execute("UPDATE images SET taken_at = ?, taken_source = 'exif' WHERE id IN "
                          "(?, ?)", (DAY, self.sent, self.saved))
        self.conn.commit()
        self.assertEqual(self.page(kind="same")["counts"]["same"], 1)
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.again,))
        self.conn.commit()
        self.assertEqual(self.page(kind="same")["counts"]["same"], 0)
        self.conn.execute("UPDATE images SET hidden = 0 WHERE id = ?", (self.again,))
        self.conn.commit()
        not_duplicates.change(not_duplicates.pairs_among(["c-2019/1.jpg", "c-2023/1-copy.jpg"]))
        self.assertEqual(self.page(kind="same")["counts"]["same"], 0)

    def test_worked_out_only_when_asked_and_kept_while_pictures_are_hidden(self):
        result = self.page()
        self.assertNotIn("same", result["counts"])
        self.assertIsNone(result["looked"])
        calls = []
        real = search_index.look_copies

        def counted(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        with mock.patch.object(search_index, "look_copies", counted):
            self.assertEqual(self.page(kind="same")["counts"]["same"], 2)
            self.assertEqual(len(calls), 1)
            # Known now: every kind shows how many there are.
            self.assertEqual(self.page(kind="exact")["counts"]["same"], 2)
            # Hiding a picture does not compare the looks again.
            self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.saved,))
            self.conn.commit()
            self.assertEqual(self.page()["counts"]["same"], 1)
            self.assertEqual(len(calls), 1)
            # New looks do.
            self.vectors.append(("c-2019/2.jpg", looks.KIND_PHOTO, packed(unit(self.rng))))
            self.store()
            self.assertNotIn("same", self.page()["counts"])
            self.assertEqual(self.page(kind="same")["counts"]["same"], 1)
            self.assertEqual(len(calls), 2)

    def test_copies_in_other_libraries(self):
        trip = self.add("trip/1.jpg", "1e1e1e1e1e1e1e1e", "2023-05-01T09:00:00",
                        packed(turned(self.base, self.rng, 0.999)), library_id="lib_trips01")
        self.store()
        both = self.page(("lib_family1", "lib_trips01"), kind="same")
        stack = next(stack for stack in both["stacks"]
                     if any(member["id"] == trip for member in stack["members"]))
        self.assertEqual(sorted(member["id"] for member in stack["members"]),
                         sorted([self.first, self.again, trip]))
        self.assertEqual(stack["libraries"], ["lib_family1", "lib_trips01"])
        self.assertEqual({member["library"] for member in stack["members"]},
                         {"lib_family1", "lib_trips01"})
        # One library at a time: only its own pictures.
        alone = self.page(kind="same")
        self.assertFalse(any(member["id"] == trip for stack in alone["stacks"]
                             for member in stack["members"]))
        self.assertEqual(self.page(("lib_trips01",), kind="same")["total"], 0)
        # Pictures "Not duplicates" can keep apart: all of them here.
        self.assertNotIn("can_apart", stack)
        self.assertFalse(any("can_apart" in member for member in stack["members"]))

    def test_identical_files_in_other_libraries(self):
        # The burst copied to another library, file for file: the same
        # picture in two libraries, but nothing "Not duplicates" can keep
        # apart (identical files, or pictures of one stack of the search page).
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.again,))
        self.conn.commit()
        copies = [self.add("trip/%d.jpg" % number, dhash, taken, library_id="lib_trips01",
                           content_hash=content)
                  for number, (dhash, taken, content) in enumerate((
                      (HASH_A, DAY, "c-2019/1.jpg"),
                      (HASH_A1, "2019-07-04T10:15:01", "c-2019/2.jpg")))]
        result = self.page(("lib_family1", "lib_trips01"), kind="same")
        stacks = {tuple(sorted(member["id"] for member in stack["members"])): stack
                  for stack in result["stacks"]}
        pairs = [tuple(sorted(pair)) for pair in ((self.first, copies[0]),
                                                  (self.second, copies[1]))]
        self.assertEqual(set(stacks), set(pairs) | {(self.sent, self.saved)})
        for pair in pairs:
            self.assertIs(stacks[pair]["can_apart"], False)
            self.assertTrue(all(member["can_apart"] is False
                                for member in stacks[pair]["members"]))
        self.assertNotIn("can_apart", stacks[(self.sent, self.saved)])

    def test_without_looks(self):
        os.remove(looks.store_path())
        result = self.page(kind="same")
        self.assertEqual((result["counts"]["same"], result["looked"], result["stacks"]),
                         (0, 0, []))


class SamePictureActionsTest(AdminFixture, unittest.TestCase):
    """What the Duplicates page's buttons send for a "Same picture, any date" stack."""

    def setUp(self):
        super().setUp()
        rng = random.Random(41)
        base = unit(rng)
        vectors = []

        def add(path, dhash, taken, look):
            content = "c-" + path
            vectors.append((content, looks.KIND_PHOTO, look))
            row = {"library_id": "lib_family1", "path": path, "name": path.split("/")[-1],
                   "size": 100, "mtime": NOW, "ctime": NOW, "width": 4000, "height": 3000,
                   "added": NOW, "updated": NOW, "dhash": dhash, "content_hash": content,
                   "taken_at": taken, "taken_source": "exif", "described": 1,
                   "prompt_ver": prompts.PROMPT_VER,
                   "info": '{"title": "A picture", "photo_type": "photo"}'}
            columns = sorted(row)
            cursor = self.conn.execute("INSERT INTO images (%s) VALUES (%s)" % (
                ",".join(columns), ",".join("?" * len(columns))), [row[name] for name in columns])
            self.conn.commit()
            return cursor.lastrowid
        # A burst of two that look the same (stacked on the search page) and
        # a copy of its first saved years later.
        self.first = add("2019/1.jpg", HASH_A, DAY, packed(base))
        self.next = add("2019/2.jpg", HASH_A1, "2019-07-04T10:15:01",
                        packed(turned(base, rng, 0.998)))
        self.copy = add("2023/1.jpg", HASH_C, "2023-01-02T08:00:00",
                        packed(turned(base, rng, 0.998)))
        store = looks.connect()
        try:
            looks.reset(store, "test-recipe", DIMS)
            looks.write(store, vectors)
        finally:
            store.close()

    def same(self):
        return self.ok(self.get("/duplicates?kind=same"))

    def dg(self, image_id):
        records = self.client.get("/api/index").get_json()["records"]
        return {record["i"]: record.get("dg") for record in records}[image_id]

    def test_not_duplicates_keeps_the_burst_stacked(self):
        stack = self.same()["stacks"][0]
        ids = [member["id"] for member in stack["members"]]
        self.assertEqual(sorted(ids), sorted([self.first, self.next, self.copy]))
        burst = self.dg(self.first)
        self.assertIsNotNone(burst)
        self.assertEqual(self.dg(self.next), burst)
        together = [[member["id"] for member in stack["members"] if member.get("stack") == burst]]
        self.assertEqual(sorted(together[0]), sorted([self.first, self.next]))
        # As the page sends it: every picture but those of one search stack.
        result = self.ok(self.post("/duplicates/not-duplicates",
                                   {"ids": ids, "together": together}))
        self.assertEqual((result["pairs"], result["changed"]), (2, 2))
        self.assertEqual(self.dg(self.first), burst)
        self.assertEqual(self.dg(self.next), burst)
        self.assertEqual(self.same()["total"], 0)
        undone = self.ok(self.post("/duplicates/not-duplicates",
                                   {"ids": ids, "together": together, "undo": True}))
        self.assertEqual(undone["changed"], 2)
        self.assertEqual(self.same()["total"], 1)
        # One picture: only the pairs with pictures of other stacks.
        result = self.ok(self.post("/duplicates/not-duplicates",
                                   {"ids": ids, "apart": self.first, "together": together}))
        self.assertEqual(result["pairs"], 1)
        self.assertEqual(self.dg(self.next), burst)

    def test_bad_together(self):
        ids = [self.first, self.next, self.copy]
        for together in ("x", [self.first], [[self.first, 999999]], [[]], [["a"]],
                         [[self.first]] * 4):
            response = self.post("/duplicates/not-duplicates", {"ids": ids, "together": together})
            self.assertEqual(response.status_code, 400, together)
        response = self.post("/duplicates/not-duplicates", {"ids": ids, "together": [ids]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("nothing to keep apart", response.get_json()["error"])
        self.assertEqual(not_duplicates.pairs(), frozenset())


if __name__ == "__main__":
    unittest.main()
