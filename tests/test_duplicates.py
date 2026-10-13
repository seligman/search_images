"""Tests for the Duplicates page: not_duplicates.py, duplicates.py, their admin routes and page.

The pictures are rows written straight into a temporary index.db (no image
files are needed: the stacks come from the dHashes, content hashes and
dates stored there).
"""

import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr

import support

import admin
import albums
import common
import duplicates
import not_duplicates
import prompts
import search_index
from test_admin import AdminFixture, insert_image, page_config

REMOTE = {"REMOTE_ADDR": "192.0.2.5"}
NODE = shutil.which("node")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "duplicates_test.js")
# 32 of 64 bits set: a dHash with detail (see search_index.LOW_DETAIL_BITS).
HASH = "0f0f0f0f0f0f0f0f"
NEAR = "0f0f0f0f0f0f0f0e"        # one bit away
FOUR = "0f0f0f0f0f0f0f00"        # four bits away
FIVE = "0f0f0f0f0f0f0f10"        # five bits away
OTHER = "f0f0f0f0f0f0f0f0"


def info(title="Two friends on the beach", sharpness=4, aesthetic=4, **more):
    value = {"title": title, "description": title + ".", "photo_type": "photo",
             "sharpness": sharpness, "aesthetic": aesthetic}
    value.update(more)
    return json.dumps(value)


def member(image_id, width=4000, height=3000, size=3000000, sharpness=4, name=None,
           camera="Canon EOS 80D", date_source="camera", taken="2019-07-04T10:15:01",
           content=None, kind="photo", dhash=HASH):
    """The facts of one picture as duplicates.rank_members takes them."""
    name = name or "IMG_%04d.JPG" % image_id
    return {"id": image_id, "library": "lib_family1", "name": name,
            "folder": "", "title": "", "width": width, "height": height, "bytes": size,
            "format": duplicates.file_format(name), "taken": taken, "date_source": date_source,
            "camera": camera, "sharpness": sharpness, "aesthetic": 3, "type": kind,
            "content": content or "hash%d" % image_id, "dhash": dhash}


class NotDuplicatesStoreTest(support.TempBaseMixin, unittest.TestCase):

    def test_empty(self):
        self.assertEqual(not_duplicates.pairs(), frozenset())
        self.assertEqual(not_duplicates.version(), "")
        self.assertEqual(not_duplicates.path(),
                         os.path.join(common.data_dir(), "not-duplicates.json"))

    def test_keys(self):
        self.assertEqual(not_duplicates.unit_key(5, "abc"), "abc")
        self.assertEqual(not_duplicates.unit_key(5, ""), "#5")
        self.assertEqual(not_duplicates.unit_key(5, None), "#5")

    def test_pairs_among(self):
        self.assertEqual(not_duplicates.pairs_among(["c", "a", "b", "a"]),
                         {("a", "b"), ("a", "c"), ("b", "c")})
        self.assertEqual(not_duplicates.pairs_among(["c", "a", "b"], apart="b"),
                         {("a", "b"), ("b", "c")})
        self.assertEqual(not_duplicates.pairs_among(["a", "a"]), set())

    def test_add_and_remove(self):
        self.assertEqual(not_duplicates.change({("b", "a"), ("a", "c")}), 2)
        self.assertEqual(not_duplicates.pairs(), frozenset({("a", "b"), ("a", "c")}))
        version = not_duplicates.version()
        self.assertTrue(version)
        self.assertEqual(not_duplicates.change({("a", "b")}), 0)      # there already
        self.assertEqual(not_duplicates.version(), version)
        with open(not_duplicates.path(), "rb") as handle:
            data = handle.read()
        self.assertNotIn(b"\r", data)
        self.assertEqual(json.loads(data.decode("utf-8")),
                         {"format": 1, "pairs": [["a", "b"], ["a", "c"]]})
        self.assertEqual(not_duplicates.change({("c", "a"), ("x", "y")}, add=False), 1)
        self.assertEqual(not_duplicates.pairs(), frozenset({("a", "b")}))
        self.assertNotEqual(not_duplicates.version(), version)
        # Nonsense is never stored.
        self.assertEqual(not_duplicates.change({("a", "a"), ("", "b"), ("x" * 200, "b")}), 0)

    def test_carry_over(self):
        not_duplicates.change({("a", "b"), ("a", "c"), ("x", "y")})
        stamp = os.stat(not_duplicates.path()).st_mtime_ns
        # No pair has the old key: nothing is written.
        self.assertEqual(not_duplicates.carry_over({"q": "r"}), 0)
        self.assertEqual(os.stat(not_duplicates.path()).st_mtime_ns, stamp)
        # "a" changed in place into "a2": its pairs are copied; the old ones stay.
        self.assertEqual(not_duplicates.carry_over({"a": "a2"}), 2)
        self.assertEqual(not_duplicates.pairs(), frozenset({
            ("a", "b"), ("a", "c"), ("x", "y"), ("a2", "b"), ("a2", "c")}))
        # A key that became its partner's (now the same file) makes no pair.
        self.assertEqual(not_duplicates.carry_over({"x": "y"}), 0)
        self.assertEqual(not_duplicates.carry_over({}), 0)

    def test_a_broken_file(self):
        common.ensure_dir(common.data_dir())
        with open(not_duplicates.path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(not_duplicates.pairs(), frozenset())
        self.assertIn("cannot be read", errors.getvalue())
        self.assertEqual(not_duplicates.change({("a", "b")}), 1)
        kept = [name for name in os.listdir(common.data_dir())
                if name.startswith("not-duplicates.json.broken-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(not_duplicates.pairs(), frozenset({("a", "b")}))

    def test_odd_entries_are_skipped(self):
        common.ensure_dir(common.data_dir())
        with open(not_duplicates.path(), "w", encoding="utf-8") as handle:
            json.dump({"format": 1, "pairs": [["b", "a"], ["a"], ["a", "a"], [1, 2], "ab",
                                              ["c", "d", "e"]]}, handle)
        self.assertEqual(not_duplicates.pairs(), frozenset({("a", "b")}))


class SplitGroupsTest(unittest.TestCase):

    KEYS = {1: "a", 2: "b", 3: "c", 4: "a", 5: "d", 6: "e"}

    def split(self, groups, pairs):
        return not_duplicates.split_groups(groups, self.KEYS, frozenset(pairs))

    def test_nothing_kept_apart(self):
        groups = [[1, 2, 3]]
        self.assertIs(self.split(groups, []), groups)
        self.assertEqual(self.split(groups, [("d", "e")]), groups)

    def test_a_pair_is_split(self):
        self.assertEqual(self.split([[1, 2, 3]], [("a", "b")]), [[1, 3]])
        self.assertEqual(self.split([[1, 2]], [("a", "b")]), [])
        self.assertEqual(self.split([[1, 2, 3]], [("a", "b"), ("a", "c"), ("b", "c")]), [])

    def test_identical_files_stay_together(self):
        # 1 and 4 are the same file (key "a").
        self.assertEqual(self.split([[1, 2, 4]], [("a", "b")]), [[1, 4]])
        self.assertEqual(self.split([[2, 4, 1, 3]], [("a", "b")]), [[1, 3, 4]])

    def test_groups_stay_sorted(self):
        result = self.split([[1, 2, 3, 5], [6, 10]], [("a", "b")])
        self.assertEqual(result, [[1, 3, 5], [6, 10]])
        result = self.split([[2, 3], [1, 5, 6]], [("a", "d")])
        self.assertEqual(result, [[1, 6], [2, 3]])

    def test_pictures_without_a_key(self):
        self.assertEqual(not_duplicates.split_groups([[7, 8]], {}, frozenset([("#7", "#8")])),
                         [])


class RankTest(unittest.TestCase):

    def best(self, *members):
        ranked, reason = duplicates.rank_members(list(members))
        return ranked[0]["id"], reason

    def test_a_smaller_copy_is_never_the_best(self):
        best, reason = self.best(member(1, sharpness=4),
                                 member(2, width=1200, height=900, sharpness=5))
        self.assertEqual(best, 1)
        self.assertIn("full-size picture (4000 \u00d7 3000)", reason)
        self.assertIn("smaller copies (1200 \u00d7 900 at most)", reason)

    def test_a_smaller_copy_has_the_same_shape_and_look(self):
        ranked, reason = duplicates.rank_members([
            member(1, sharpness=3), member(2, width=1210, height=900, sharpness=5, dhash=FOUR)])
        self.assertEqual([item["id"] for item in ranked], [1, 2])
        self.assertEqual(ranked[1]["copy"], ["it is a smaller copy of IMG_0001.JPG "
                                             "(4000 × 3000)"])
        self.assertEqual(ranked[0]["copy"], [])
        self.assertTrue(reason.startswith("The full-size picture"), reason)
        # Another shape (16:9, or 3.4% wider than 4:3) or another look
        # (5 bits apart): just a smaller picture, ranked by its sharpness.
        for small in (member(2, width=1600, height=900, sharpness=5),
                      member(2, width=1200, height=870, sharpness=5),
                      member(2, width=1200, height=900, sharpness=5, dhash=FIVE),
                      member(2, width=0, height=0, sharpness=5),
                      member(2, width=1200, height=900, sharpness=5, dhash="")):
            ranked, reason = duplicates.rank_members([member(1, sharpness=3), small])
            self.assertEqual(ranked[0]["id"], 2, small)
            self.assertEqual(reason, "The sharpest: sharpness 5 of 5 (the next best: 3).")
            self.assertEqual([item["copy"] for item in ranked], [[], []])
        # With nothing else to tell them apart, the larger one is the best,
        # and the other is called smaller, not a copy.
        best, reason = self.best(member(1), member(2, width=1600, height=900))
        self.assertEqual(best, 1)
        self.assertEqual(reason, "The most pixels: 4000 × 3000 (12.0 MP); the next is a "
                                 "smaller picture (1600 × 900).")

    def test_a_smaller_copy_of_any_larger_picture_is_never_the_best(self):
        # 3 is a smaller copy of 1, though 2 (another shot) is the best.
        ranked, reason = duplicates.rank_members([
            member(1, sharpness=3), member(2, sharpness=4, dhash=OTHER),
            member(3, width=1000, height=750, sharpness=5)])
        self.assertEqual([item["id"] for item in ranked], [2, 1, 3])
        self.assertEqual(reason, "The sharpest: sharpness 4 of 5 (the next best: 3).")
        self.assertEqual(ranked[2]["copy"], ["it is a smaller copy of IMG_0001.JPG "
                                             "(4000 × 3000)"])
        # A copy of a copy is a copy of the largest it matches.
        copies = duplicates._smaller_copies([
            member(1), member(2, width=1600, height=1200), member(3, width=400, height=300)])
        self.assertEqual({key: value["id"] for key, value in copies.items()}, {2: 1, 3: 1})
        # The same file with an out-of-date stored size is not a copy of itself.
        copies = duplicates._smaller_copies([
            member(1, content="same"), member(2, width=1600, height=1200, content="same")])
        self.assertEqual(copies, {})

    def test_the_sharpest(self):
        best, reason = self.best(member(1, sharpness=3), member(2, sharpness=5),
                                 member(3, sharpness=4))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "The sharpest: sharpness 5 of 5 (the next best: 4).")
        # Without a score for every picture, sharpness does not count.
        best, reason = self.best(member(1, sharpness=None, size=4000000), member(2, sharpness=5))
        self.assertEqual(best, 1)
        self.assertIn("The largest file", reason)

    def test_originals_beat_copies(self):
        for name in ("IMG_0001 (1).JPG", "IMG_0001 - Copy.JPG", "IMG_0001_edited.jpg",
                     "IMG_0001~2.JPG", "IMG-20190704-WA0003.jpg", "WhatsApp Image 2019.jpeg",
                     "Screenshot_20190704-101500.png"):
            copy = member(1, name=name, size=9000000)
            best, reason = self.best(copy, member(2))
            self.assertEqual(best, 2, name)
            self.assertTrue(reason.startswith("The original: the next one looks like a copy ("),
                            reason)
        best, reason = self.best(member(1, camera="", size=9000000), member(2))
        self.assertEqual(best, 2)
        self.assertIn("it has no camera data", reason)
        best, _ = self.best(member(1, date_source="file date", size=9000000), member(2))
        self.assertEqual(best, 2)
        best, _ = self.best(member(1, kind="screenshot", size=9000000), member(2))
        self.assertEqual(best, 2)
        # The camera's data only counts against a copy when another has it.
        best, _ = self.best(member(1, camera="", size=9000000), member(2, camera=""))
        self.assertEqual(best, 1)

    def test_pixels_file_size_date_and_order(self):
        best, reason = self.best(member(1), member(2, width=4000, height=3200))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "The most pixels: 4000 \u00d7 3200 (12.8 MP), the next "
                                 "4000 \u00d7 3000.")
        best, reason = self.best(member(1, size=3000000), member(2, size=3100000))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "The largest file (3.0 MB, the next 2.9 MB): usually the "
                                 "most detail.")
        best, reason = self.best(member(1, taken="2019-07-04T10:15:02"), member(2))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "Taken before the next one, which is a later shot or a copy.")
        best, reason = self.best(member(1, taken=""), member(2))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "It has a date taken; the next one has none.")
        best, reason = self.best(member(3), member(2))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "It ties with the next one on everything the index knows; "
                                 "this one was found first.")

    def test_identical_files(self):
        ranked, reason = duplicates.rank_members([
            member(5, content="same", name="IMG_0001 (1).JPG"), member(3, content="same"),
            member(4, content="same")])
        self.assertEqual([item["id"] for item in ranked], [3, 4, 5])
        self.assertTrue(reason.startswith("Identical files. Keeping the one found first."),
                        reason)
        self.assertEqual([item["twin"] for item in ranked], [None, 3, 3])
        self.assertEqual(ranked[2]["copy"], ["its file name says it is a copy or an edit"])
        # Scores of identical files (described one by one) do not count.
        ranked, reason = duplicates.rank_members([member(7, content="same", sharpness=5),
                                                  member(3, content="same", sharpness=3)])
        self.assertEqual(ranked[0]["id"], 3)
        self.assertEqual(reason, "Identical files. Keeping the one found first.")
        _, reason = duplicates.rank_members([member(5, content="same", name="IMG_0001 (1).JPG"),
                                             member(6, content="same")])
        self.assertTrue(reason.startswith("Identical files. This one looks the least like a "
                                          "copy"), reason)

    def test_the_camera_format_beats_a_converted_copy(self):
        # IMG_3762.jpg was made from the phone's IMG_3762.HEIC: larger, and
        # described as sharper, but a copy.
        heic = member(1, name="IMG_3762.HEIC", size=3500000, sharpness=4)
        jpeg = member(2, name="IMG_3762.jpg", size=4700000, sharpness=5)
        best, reason = self.best(jpeg, heic)
        self.assertEqual(best, 1)
        self.assertEqual(reason, "The original: the next one looks like a copy (it seems to be "
                                 "IMG_3762.HEIC saved as JPEG).")
        # A PNG saved from a JPEG (with its EXIF) is much larger.
        best, reason = self.best(member(1, name="IMG_0002.png", size=18000000),
                                 member(2, name="IMG_0002.jpg", size=3000000))
        self.assertEqual(best, 2)
        self.assertIn("IMG_0002.jpg saved as PNG", reason)
        # Other names: the same camera at the same second, one bit apart.
        best, _ = self.best(member(1, name="export.jpg", size=4700000, sharpness=5, dhash=NEAR),
                            member(2, name="IMG_3763.HEIC", sharpness=3))
        self.assertEqual(best, 2)
        # ... but a second later it is another shot, and the sharper one wins.
        best, reason = self.best(member(1, name="later.jpg", sharpness=5, dhash=NEAR,
                                        taken="2019-07-04T10:15:02"),
                                 member(2, name="IMG_3763.HEIC", sharpness=3))
        self.assertEqual(best, 1)
        self.assertEqual(reason, "The sharpest: sharpness 5 of 5 (the next best: 3).")

    def test_one_picture_in_two_formats_counts_once_for_sharpness(self):
        # Shot A in two formats (scored 5 and 3) and shot B (scored 4): A's
        # HEIC is the best, with A's best score.
        ranked, reason = duplicates.rank_members([
            member(1, name="IMG_0001.HEIC", sharpness=3),
            member(2, name="IMG_0001.jpg", sharpness=5),
            member(3, name="IMG_0002.HEIC", sharpness=4, taken="2019-07-04T10:15:05")])
        self.assertEqual([item["id"] for item in ranked], [1, 2, 3])
        self.assertEqual(reason, "The original: the next one looks like a copy (it seems to be "
                                 "IMG_0001.HEIC saved as JPEG).")
        # Without A's JPEG, B (4) is sharper than A's HEIC (3).
        ranked, _ = duplicates.rank_members([
            member(1, name="IMG_0001.HEIC", sharpness=3),
            member(3, name="IMG_0002.HEIC", sharpness=4, taken="2019-07-04T10:15:05")])
        self.assertEqual(ranked[0]["id"], 3)

    def test_formats_before_file_sizes(self):
        best, reason = self.best(member(1, name="a.png", size=9000000, dhash=OTHER),
                                 member(2, name="b.jpg", size=3000000))
        self.assertEqual(best, 2)
        self.assertEqual(reason, "Saved as JPEG, the way cameras save pictures; the next one is "
                                 "PNG, more often a converted copy.")
        # File sizes are compared in one format.
        best, reason = self.best(member(1, name="a.jpg", size=3000000),
                                 member(2, name="b.jpg", size=3100000))
        self.assertEqual(best, 2)
        self.assertTrue(reason.startswith("The largest file"), reason)

    def test_reasons_compare_with_the_next_one(self):
        # A small chat copy scored 5 is not "the others" the best beats.
        ranked, reason = duplicates.rank_members([
            member(1, sharpness=4), member(2, sharpness=3),
            member(3, name="IMG-20190704-WA0003.jpg", width=1200, height=900, sharpness=5)])
        self.assertEqual(ranked[0]["id"], 1)
        self.assertEqual(reason, "The sharpest: sharpness 4 of 5 (the next best: 3).")

    def test_file_sizes_in_words(self):
        self.assertEqual(duplicates._bytes_texts(24900, 24600), ["24.3 KB", "24.0 KB"])
        self.assertEqual(duplicates._bytes_texts(24600, 24590),
                         ["24,600 bytes", "24,590 bytes"])
        self.assertEqual(duplicates._bytes_texts(5000000, 3000000), ["4.8 MB", "2.9 MB"])

    def test_the_picture_the_owner_marked_comes_first(self):
        # A favorite, a rating or an album (albums.py) beats everything else:
        # "Keep the best" must not hide the copy the owner chose.
        favorite = dict(member(2, width=1200, height=900, sharpness=2), favorite=True)
        ranked, reason = duplicates.rank_members([member(1, sharpness=5), favorite])
        self.assertEqual(ranked[0]["id"], 2)
        self.assertEqual(reason, "You marked this one: a favorite (the next one: no marks).")
        ranked, reason = duplicates.rank_members([dict(member(1), rating=3),
                                                  dict(member(2), rating=5),
                                                  dict(member(3), albums=2)])
        self.assertEqual([item["id"] for item in ranked], [2, 1, 3])
        self.assertIn("rated 5 stars (the next one: rated 3 stars)", reason)
        _, reason = duplicates.rank_members([member(5, content="same"),
                                             dict(member(6, content="same"), albums=1)])
        self.assertEqual(reason, "Identical files. You marked this one: in an album (the next "
                                 "one: no marks).")

    def test_file_formats(self):
        self.assertEqual(duplicates.file_format("a.JPG"), "JPEG")
        self.assertEqual(duplicates.file_format("a.heic"), "HEIC")
        self.assertEqual(duplicates.file_format("a.insp"), "Insta360")
        self.assertEqual(duplicates.file_format("a"), "")


class DuplicatesFixture(AdminFixture):
    """The Family library with stacks: a burst of three, two identical files, a pair."""

    def setUp(self):
        super().setUp()
        self.write_config([support.library("Family", self.family, id="lib_family1"),
                           support.library("Other", self.base.library_folder("Other"),
                                           id="lib_other001")])

        def add(path, dhash, content, taken, library="lib_family1", **fields):
            fields.setdefault("info", info())
            fields.setdefault("described", 1)
            fields.setdefault("prompt_ver", prompts.PROMPT_VER)
            return insert_image(self.conn, library, path, dhash=dhash, content_hash=content,
                                taken_at=taken, taken_source="exif", camera="Canon EOS 80D",
                                width=4000, height=3000, **fields)
        self.burst = [add("beach/1.jpg", HASH, "c1", "2019-07-04T10:15:01", size=300),
                      add("beach/2.jpg", NEAR, "c2", "2019-07-04T10:15:01", size=500,
                          info=info(sharpness=5)),
                      add("beach/3.jpg", HASH, "c3", "2019-07-04T10:15:02", size=400)]
        self.copies = [add("cake.jpg", OTHER, "c4", "2019-09-01T19:30:00",
                           info=info("Cake")),
                       add("backup/cake.jpg", OTHER, "c4", "2019-09-01T19:30:00",
                           info=info("Cake"))]
        self.pair = [add("other/x.jpg", HASH, "c6", "2020-01-01T08:00:00",
                         library="lib_other001", info=info("Dog")),
                     add("other/y.jpg", NEAR, "c7", "2020-01-01T08:00:00",
                         library="lib_other001", info=info("Dog"))]
        self.single = add("single.jpg", "00ff00ff00ff00ff", "c8", "2018-01-01T00:00:01")

    def stacks(self, query=""):
        return self.ok(self.get("/duplicates" + query))


class ReviewPageTest(DuplicatesFixture, unittest.TestCase):

    def test_stacks_largest_first(self):
        result = self.stacks()
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["counts"], {"all": 3, "exact": 1, "similar": 2})
        self.assertEqual((result["pictures"], result["extra"]), (7, 4))
        self.assertEqual((result["page"], result["pages"]), (1, 1))
        first = result["stacks"][0]
        self.assertEqual((first["size"], first["kind"], first["library"]),
                         (3, "similar", "lib_family1"))
        self.assertEqual(first["best"], self.burst[1])
        self.assertIn("sharpest", first["reason"])
        best = first["members"][0]
        self.assertEqual(best["id"], self.burst[1])
        for key in ("name", "folder", "width", "height", "bytes", "format", "taken",
                    "date_source", "camera", "sharpness", "aesthetic", "copy", "twin"):
            self.assertIn(key, best)
        self.assertEqual((best["name"], best["folder"], best["bytes"], best["date_source"]),
                         ("2.jpg", "beach", 500, "camera"))
        self.assertNotIn("content", best)               # content hashes stay on the server
        exact = [stack for stack in result["stacks"] if stack["kind"] == "exact"][0]
        self.assertEqual(exact["contents"], 1)
        self.assertEqual(exact["members"][1]["twin"], exact["best"])

    def test_filters_pages_and_one_stack(self):
        self.assertEqual(self.stacks("?kind=exact")["total"], 1)
        self.assertEqual(self.stacks("?kind=similar")["total"], 2)
        self.assertEqual(self.stacks("?min=3")["total"], 1)
        self.assertEqual(self.stacks("?lib=lib_other001")["total"], 1)
        result = duplicates.review_page(self.conn, common.load_config(),
                                        ["lib_family1", "lib_other001"], page=2, page_size=2)
        self.assertEqual((result["page"], result["pages"], len(result["stacks"])), (2, 2, 1))
        result = duplicates.review_page(self.conn, common.load_config(),
                                        ["lib_family1", "lib_other001"], page=9, page_size=2)
        self.assertEqual(result["page"], 2)
        one = self.stacks("?stack=%d" % self.burst[2])
        self.assertEqual([stack["best"] for stack in one["stacks"]], [self.burst[1]])
        self.assertEqual(self.stacks("?stack=%d" % self.single)["stacks"], [])
        # "Same picture, any date" needs looks (there are none here).
        same = self.stacks("?kind=same")
        self.assertEqual((same["kind"], same["total"], same["looked"]), ("same", 0, 0))
        self.assertEqual(self.stacks("?kind=same&stack=%d" % self.burst[2])["stacks"], [])

    def test_bad_requests(self):
        for query in ("?kind=odd", "?min=1", "?min=x", "?min=101", "?page=0", "?stack=-1",
                      "?stack=abc"):
            response = self.get("/duplicates" + query)
            self.assertEqual(response.status_code, 400, query)
            self.assertIn("error", response.get_json())
        self.assertEqual(self.get("/duplicates?lib=lib_nothere1").status_code, 404)

    def test_hidden_pictures_leave_their_stacks(self):
        self.ok(self.post("/images/hide", {"ids": [self.burst[0], self.burst[2]],
                                           "hidden": True}))
        result = self.stacks()
        self.assertEqual(result["counts"]["similar"], 1)
        self.ok(self.post("/images/hide", {"ids": [self.burst[0], self.burst[2]],
                                           "hidden": False}))
        self.assertEqual(self.stacks()["counts"]["similar"], 2)

    def test_only_the_admin_on_this_computer(self):
        self.assertEqual(self.get("/duplicates", environ_base=REMOTE).status_code, 403)
        response = self.client.get(admin.URL_PREFIX + "/duplicates")
        self.assertEqual(response.status_code, 403)                     # no header
        self.assertEqual(self.get("/duplicates", headers={"Sec-Fetch-Site": "cross-site"})
                         .status_code, 403)


class KeepApartTest(DuplicatesFixture, unittest.TestCase):

    def test_not_duplicates_and_undo(self):
        etag = self.client.get("/api/index").headers["ETag"]
        result = self.ok(self.post("/duplicates/not-duplicates", {"ids": self.burst}))
        self.assertEqual(result, {"pairs": 3, "changed": 3, "undo": False, "unknown": 0})
        self.assertEqual(not_duplicates.pairs(),
                         frozenset({("c1", "c2"), ("c1", "c3"), ("c2", "c3")}))
        self.assertEqual(self.stacks()["counts"], {"all": 2, "exact": 1, "similar": 1})
        # The search page's stacks follow (a new ETag, records without dg).
        response = self.client.get("/api/index", headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 200)
        records = {record["i"]: record for record in response.get_json()["records"]}
        self.assertNotIn("dg", records[self.burst[0]])
        self.assertIn("dg", records[self.copies[0]])
        undone = self.ok(self.post("/duplicates/not-duplicates", {"ids": self.burst,
                                                                  "undo": True}))
        self.assertEqual(undone["changed"], 3)
        self.assertEqual(not_duplicates.pairs(), frozenset())
        self.assertEqual(self.stacks()["counts"]["similar"], 2)

    def test_one_picture_apart(self):
        result = self.ok(self.post("/duplicates/not-duplicates",
                                   {"ids": self.burst, "apart": self.burst[2]}))
        self.assertEqual(result["pairs"], 2)
        stack = self.stacks("?stack=%d" % self.burst[0])["stacks"][0]
        self.assertEqual(sorted(item["id"] for item in stack["members"]),
                         sorted(self.burst[:2]))
        self.assertEqual(self.stacks("?stack=%d" % self.burst[2])["stacks"], [])

    def test_large_stacks(self):
        # A webcam series of one day: 120 pictures in one stack.
        ids = [insert_image(self.conn, "lib_family1", "cam/%03d.jpg" % index, dhash=HASH,
                            content_hash="cam%d" % index, taken_at="2021-03-01T%02d:%02d:00"
                            % (index // 60, index % 60), taken_source="exif",
                            camera="Webcam", width=1280, height=720)
               for index in range(120)]
        stack = self.stacks("?stack=%d" % ids[0])
        self.assertEqual(stack["stacks"][0]["size"], 120)
        self.assertEqual(stack["not_duplicates_limit"], duplicates.NOT_DUPLICATES_LIMIT)
        # Too many to keep every two apart (every pair would be stored) ...
        response = self.post("/duplicates/not-duplicates", {"ids": ids})
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most %d pictures" % duplicates.NOT_DUPLICATES_LIMIT,
                      response.get_json()["error"].lower())
        self.assertNotIn("in parts", response.get_json()["error"])
        # ... but one picture can leave the stack.
        result = self.ok(self.post("/duplicates/not-duplicates", {"ids": ids, "apart": ids[5]}))
        self.assertEqual(result["pairs"], 119)
        self.assertEqual(self.stacks("?stack=%d" % ids[5])["stacks"], [])
        self.assertEqual(self.stacks("?stack=%d" % ids[0])["stacks"][0]["size"], 119)

    def test_identical_files_cannot_be_kept_apart(self):
        response = self.post("/duplicates/not-duplicates", {"ids": self.copies})
        self.assertEqual(response.status_code, 400)
        self.assertIn("identical files", response.get_json()["error"])
        self.assertEqual(not_duplicates.pairs(), frozenset())

    def test_bad_requests(self):
        for body in ({"ids": self.burst, "apart": 999999}, {"ids": self.burst, "apart": True},
                     {"ids": self.burst, "undo": "yes"}, {"ids": []}, {"ids": "1,2"},
                     {"ids": list(range(1, duplicates.NOT_DUPLICATES_LIMIT + 2))},
                     {"ids": list(range(1, admin.HIDE_LIMIT + 2)), "apart": 1}):
            self.assertEqual(self.post("/duplicates/not-duplicates", body).status_code, 400,
                             body)
        self.assertEqual(not_duplicates.pairs(), frozenset())

    def test_only_the_admin_on_this_computer(self):
        body = {"ids": self.burst}
        self.assertEqual(self.post("/duplicates/not-duplicates", body,
                                   environ_base=REMOTE).status_code, 403)
        self.assertEqual(self.post("/duplicates/not-duplicates", body,
                                   headers={"Origin": "http://attacker.example"}).status_code,
                         403)
        response = self.client.post(admin.URL_PREFIX + "/duplicates/not-duplicates",
                                    data=json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.get("/duplicates/not-duplicates").status_code, 405)
        self.assertEqual(not_duplicates.pairs(), frozenset())

    def test_exports_follow(self):
        not_duplicates.change(not_duplicates.pairs_among(["c1", "c2", "c3"]))
        records = search_index.build_records(self.conn, common.load_config(), ["lib_family1"],
                                             for_export=True)
        grouped = {record["i"] for record in records if "dg" in record}
        self.assertEqual(grouped, set(self.copies))


class DuplicatesPageTest(DuplicatesFixture, unittest.TestCase):

    def test_the_page(self):
        response = self.client.get("/duplicates")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("duplicates.js", html)
        self.assertIn("duplicates.css", html)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        config = page_config(html)
        self.assertEqual(config["page"], "duplicates")
        self.assertTrue(config["capabilities"]["admin"])

    def test_not_for_other_computers(self):
        response = self.client.get("/duplicates", environ_base=REMOTE)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Duplicates only work on this computer", response.get_data(as_text=True))

    def test_stacks_are_kept_until_something_changes(self):
        config = common.load_config()
        first = duplicates.stacks_for(self.conn, config, ["lib_family1"])
        self.assertIs(duplicates.stacks_for(self.conn, config, ["lib_family1"]), first)
        self.conn.execute("UPDATE images SET updated = updated + 1 WHERE id = ?",
                          (self.single,))
        self.conn.commit()
        second = duplicates.stacks_for(self.conn, config, ["lib_family1"])
        self.assertIsNot(second, first)
        not_duplicates.change({("c1", "c2")})
        self.assertIsNot(duplicates.stacks_for(self.conn, config, ["lib_family1"]), second)

    def test_a_marked_picture_is_the_best_and_shows_its_marks(self):
        first = self.stacks()["stacks"][0]
        self.assertEqual(first["best"], self.burst[1])
        albums.set_marks(self.conn, [self.burst[0]], favorite=True, rating=4)
        albums.create_album(self.conn, "Beach", ids=[self.burst[0], self.burst[2]])
        self.conn.commit()
        first = self.stacks()["stacks"][0]
        self.assertEqual(first["best"], self.burst[0])
        self.assertIn("You marked this one: a favorite, rated 4 stars, in an album",
                      first["reason"])
        marks = {item["id"]: (item.get("favorite"), item.get("rating"), item.get("albums"))
                 for item in first["members"]}
        self.assertEqual(marks, {self.burst[0]: (True, 4, 1), self.burst[1]: (None, None, None),
                                 self.burst[2]: (None, None, 1)})


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PageScriptTest(unittest.TestCase):

    def test_page_free_parts(self):
        result = subprocess.run([NODE, NODE_TESTS], cwd=support.REPO, capture_output=True,
                                timeout=120)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("ok - dates with and without a time", output)


if __name__ == "__main__":
    unittest.main()
