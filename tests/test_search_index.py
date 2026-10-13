"""Tests for search_index.py: compact search records, duplicate groups and /api/index.

Rows are written straight into a temporary database (no scanning); the
duplicate-group tests use made-up dHashes.
"""

import datetime
import gzip
import json
import os
import random
import time
import unittest
from unittest import mock

import support

import common
import db
import image_edits
import imageinfo
import search_index
import serve

try:
    import flask
except ImportError:
    flask = None

NOW = 1700000000.0
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}


quiet = support.quiet


def insert_image(conn, library_id, path, **fields):
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 640, "height": 480,
           "added": NOW, "updated": NOW}
    row.update(fields)
    if isinstance(row.get("info"), dict):
        row["info"] = json.dumps(row["info"])
    columns = sorted(row)
    cursor = conn.execute(
        "INSERT INTO images (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
        [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


def flip(value, *bits):
    """value (an int) with the given bit positions flipped."""
    for bit in bits:
        value ^= 1 << bit
    return value


def hexed(value):
    return "%016x" % value


# A capture date to trust (from EXIF or the file name).
DAY = "2020-06-01T12:00:00"


def dated(items, day=DAY[:10]):
    """(id, dhash, content hash) items with trusted capture times added.

    All on the same day, a minute apart: not a burst, so the hash-only
    tests compare with the 8-bit threshold.
    """
    return [tuple(item) + ("%sT%02d:%02d:00" % (day, 8 + index // 60, index % 60),)
            for index, item in enumerate(items)]


def parse_taken(text):
    """(date, datetime or None for a date without a time) of a date taken, or None."""
    if not text:
        return None
    if "T" not in text:
        return (datetime.date(*map(int, text.split("-"))), None)
    moment = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    return (moment.date(), moment)


def about_the_same_time(first, second):
    """The same calendar day, or at most 12 hours apart (parse_taken values)."""
    if first[0] == second[0]:
        return True
    if first[1] is None or second[1] is None:
        return False
    return abs((first[1] - second[1]).total_seconds()) <= 12 * 3600


def shapes_fit(aspect, others, tolerance=0.15):
    """True if aspect (or None) is within tolerance of every known aspect in others."""
    if aspect is None:
        return True
    return all(max(aspect, other) <= min(aspect, other) * (1 + tolerance) + 1e-9
               for other in others if other is not None)


def brute_force_groups(items, threshold=8, undated_threshold=6, burst_threshold=12,
                       burst_seconds=2, burst_groups=8):
    """duplicate_groups() the slow and obvious way, for comparison."""
    units, order = {}, []
    for item in sorted(items, key=lambda item: item[0]):
        image_id, dhash, content_hash = item[:3]
        taken = item[3] if len(item) > 3 else ""
        aspect = item[4] if len(item) > 4 else None
        key = ("c", content_hash) if content_hash else ("i", image_id)
        if key not in units:
            units[key] = [[], None, None, None]
            order.append(key)
        units[key][0].append(image_id)
        if units[key][1] is None:
            units[key][1] = search_index.parse_dhash(dhash)
        if units[key][2] is None and taken:
            units[key][2] = parse_taken(taken)
        if units[key][3] is None:
            units[key][3] = aspect
    representatives, anchors, groups, alone = [], [], [], []
    shapes = []                     # per group: the aspect ratios of its pictures
    started = {}                    # capture time -> groups started then
    for key in order:
        ids, value, when, aspect = units[key]
        if value is None or search_index._low_detail(value):
            alone.append(ids)
            continue
        best, best_key = None, None
        for index, (other, other_when) in enumerate(representatives):
            distance = bin(value ^ other).count("1")
            anchor = anchors[index]
            if when is not None and anchor is not None and not about_the_same_time(when, anchor):
                continue
            # Every two pictures of a group have about the same shape.
            if not shapes_fit(aspect, shapes[index]):
                continue
            if when is None or other_when is None:
                limit = undated_threshold
            elif (when[1] is not None and other_when[1] is not None
                  and abs((when[1] - other_when[1]).total_seconds()) <= burst_seconds
                  and started[other_when[1]] < burst_groups):
                limit = burst_threshold
            else:
                limit = threshold
            # A dated picture prefers dated representatives; then the
            # nearest; then the oldest.
            rank = 1 if when is not None and other_when is None else 0
            if distance <= limit and (best_key is None or (rank, distance, index) < best_key):
                best, best_key = index, (rank, distance, index)
        if best is None:
            representatives.append((value, when))
            anchors.append(when)
            groups.append(list(ids))
            shapes.append([aspect])
            if when is not None and when[1] is not None:
                started[when[1]] = started.get(when[1], 0) + 1
        else:
            groups[best].extend(ids)
            shapes[best].append(aspect)
            if anchors[best] is None:
                anchors[best] = when
    result = [sorted(ids) for ids in groups + alone if len(ids) > 1]
    return sorted(result, key=lambda ids: ids[0])


V5_INFO = {
    "title": "Golden retriever catching a frisbee", "description": "A dog on a beach.",
    "photo_type": "photo", "document_type": "none", "text": "", "text_language": "",
    "setting": "outdoor", "scene": "beach", "time_of_day": "unknown", "event": "none",
    "people": "none", "people_descriptors": [], "subjects": ["Golden retriever", "frisbee"],
    "animals": ["golden retriever"], "food": [], "vehicles": [],
    "colors": ["Blue", "blue", "sand"], "tags": ["Dog", "dog", " beach ", ""],
    "quality_flags": ["blurry"], "sensitive": "none", "private_info": False,
    "aesthetic": 4, "sharpness": 5, "exposure": 3, "_model_name": "test-model",
}


class IndexFixture(support.TempBaseMixin):

    def setUp(self):
        super().setUp()
        self.family = self.base.library_folder("Family")
        self.other = self.base.library_folder("Other")
        self.base.write_config([
            support.library("Family", self.family, id="lib_family1"),
            support.library("Other", self.other, id="lib_other001"),
            support.library("Off", self.other, id="lib_off00001", enabled=False),
        ])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.config = common.load_config()
        search_index._groups_memo.clear()
        self.addCleanup(search_index._groups_memo.clear)

    def records(self, library_ids=("lib_family1",), **kwargs):
        return {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, list(library_ids), **kwargs)}


class RecordTest(IndexFixture, unittest.TestCase):

    def test_described_picture_gets_short_keys_without_nothing_values(self):
        image_id = insert_image(self.conn, "lib_family1", "2019/Beach/dog.jpg", described=1,
                                prompt_ver=5, info=V5_INFO, taken_at="2019-05-14T14:23:01",
                                camera="Acme One", lat=48.85, lon=2.35,
                                projection="equirectangular", width=4000, height=2000)
        record = self.records()[image_id]
        self.assertEqual(record, {
            "i": image_id, "l": "lib_family1", "n": "dog.jpg", "f": "2019/Beach",
            "t": "Golden retriever catching a frisbee", "d": "A dog on a beach.",
            "sc": "beach", "s": ["Golden retriever", "frisbee"], "a": ["golden retriever"],
            "qf": ["blurry"], "g": ["dog", "beach"], "c": ["blue", "sand"], "st": "outdoor",
            "ty": "photo", "pp": "none", "q": 4, "sh": 5, "dk": "2019-05-14",
            "ts": "2019-05-14T14:23:01", "cm": "Acme One", "gp": 1, "w": 4000, "hgt": 2000,
            "p": "360", "ds": 1})

    def test_flags_scores_and_extended_words(self):
        info = dict(V5_INFO, sensitive="graphic", private_info=True, aesthetic=7,
                    sharpness=True, document_type="receipt", event="birthday",
                    time_of_day="night", people="crowd",
                    objects=[{"name": "chair", "count": 4}, {"count": 2}, "junk"],
                    landmarks=["Eiffel Tower"], activities=["running"], mood="neutral",
                    location_guess="Paris")
        image_id = insert_image(self.conn, "lib_family1", "top.jpg", described=1,
                                prompt_ver=5, info=info)
        record = self.records()[image_id]
        self.assertEqual((record["se"], record["pr"], record["dt"], record["ev"],
                          record["td"], record["pp"]),
                         ("graphic", 1, "receipt", "birthday", "night", "crowd"))
        # Scores outside 1-5 (and booleans) are dropped; mood "neutral" says nothing.
        self.assertNotIn("q", record)
        self.assertNotIn("sh", record)
        self.assertEqual(record["ex"], "chair, Eiffel Tower, running, Paris")
        self.assertNotIn("f", record)                       # top-level file
        info["mood"] = "cheerful"
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (json.dumps(info), image_id))
        self.assertEqual(self.records()[image_id]["ex"],
                         "chair, Eiffel Tower, running, cheerful, Paris")

    def test_older_and_undescribed_pictures(self):
        # Version 4 rated sharpness from 0 to 1: it never becomes a 1-5 score.
        v4 = {"description": "A Sunny Beach", "subjects": ["Dog"], "text": "OPEN",
              "tags": ["Sand", "sea"], "colors": ["Blue"], "sharpness": 0.8,
              "aesthetic_score": 0.9}
        old = insert_image(self.conn, "lib_family1", "old.jpg", described=1, prompt_ver=4,
                           info=v4)
        fresh = insert_image(self.conn, "lib_family1", "new.jpg", width=0, height=0,
                             camera="", taken_at="", lat=None, lon=None)
        broken = insert_image(self.conn, "lib_family1", "broken.jpg", described=1,
                              prompt_ver=5, info="{not json")
        records = self.records()
        self.assertEqual(records[old], {"i": old, "l": "lib_family1", "n": "old.jpg",
                                        "d": "A Sunny Beach", "s": ["Dog"], "o": "OPEN",
                                        "g": ["sand", "sea"], "c": ["blue"], "w": 640,
                                        "hgt": 480, "ds": 1, "v4": 1})
        self.assertEqual(records[fresh], {"i": fresh, "l": "lib_family1", "n": "new.jpg"})
        self.assertEqual(records[broken], {"i": broken, "l": "lib_family1",
                                           "n": "broken.jpg", "w": 640, "hgt": 480, "ds": 1})

    def test_only_present_pictures_of_configured_libraries(self):
        shown = insert_image(self.conn, "lib_family1", "a.jpg")
        hidden = insert_image(self.conn, "lib_family1", "b.jpg", hidden=1)
        insert_image(self.conn, "lib_family1", "gone.jpg", missing_since=NOW)
        insert_image(self.conn, "lib_orphan01", "lost.jpg")
        other = insert_image(self.conn, "lib_other001", "c.jpg")
        records = self.records(("lib_family1", "lib_other001", "lib_orphan01"))
        self.assertEqual(sorted(records), [shown, hidden, other])
        self.assertEqual(records[hidden]["h"], 1)
        records = self.records(("lib_family1",), include_hidden=False)
        self.assertEqual(sorted(records), [shown])
        # Without a config, the caller's library list is taken as it is.
        self.assertEqual(len(search_index.build_records(self.conn, None, ["lib_orphan01"])), 1)
        self.assertEqual(search_index.build_records(self.conn, self.config, []), [])

    def test_export_records_leave_out_private_details(self):
        info = dict(V5_INFO, landmarks=["Eiffel Tower"], location_guess="Paris",
                    activities=["running"])
        image_id = insert_image(self.conn, "lib_family1", "2019/Paris/IMG_1.jpg", described=1,
                                prompt_ver=5, info=info, taken_at="2019-05-14T14:23:01",
                                taken_source="exif", lat=48.85, lon=2.35, camera="Acme One")
        hidden = insert_image(self.conn, "lib_family1", "secret.jpg", hidden=1)
        # A date from the file's time is a file time: never in an export.
        filed = insert_image(self.conn, "lib_family1", "copied.jpg", described=1, prompt_ver=5,
                             info=info, taken_at="2026-10-11T08:00:00", taken_source="mtime")
        self.assertEqual(self.records()[filed]["dk"], "2026-10-11")
        # (On the server it is marked as a file time: no date taken.)
        self.assertEqual(self.records()[filed]["du"], 1)
        self.assertNotIn("du", self.records()[image_id])
        self.assertNotIn("dk", self.records(for_export=True)[filed])
        self.assertNotIn("du", self.records(for_export=True)[filed])
        self.conn.execute("DELETE FROM images WHERE id = ?", (filed,))
        self.conn.commit()
        records = self.records(for_export=True)
        self.assertEqual(sorted(records), [image_id])
        record = records[image_id]
        for key in ("n", "f", "ts", "gp", "h"):
            self.assertNotIn(key, record)
        self.assertEqual(record["ex"], "running")
        self.assertEqual(record["dk"], "2019-05-14")
        self.assertEqual(record["cm"], "Acme One")
        text = json.dumps(records)
        for secret in ("IMG_1", "Paris", "Eiffel", "48.85", "2.35", "14:23", "test-model"):
            self.assertNotIn(secret, text)
        # Each private detail comes back only with its own switch.
        allowed = self.records(for_export=True, export_options={
            "file_names": True, "folders": True, "location": True})
        record = allowed[image_id]
        self.assertEqual((record["n"], record["f"], record["gp"]),
                         ("IMG_1.jpg", "2019/Paris", 1))
        self.assertEqual(record["ex"], "Eiffel Tower, running, Paris")
        self.assertNotIn("ts", record)
        self.assertNotIn(hidden, allowed)
        names_only = self.records(for_export=True, export_options={"file_names": "yes"})
        self.assertNotIn("n", names_only[image_id])        # only true counts

    def test_an_export_without_the_location_has_no_positions_in_the_text(self):
        # A dashcam prints its position over the video, and the model reads it
        # into the text in the picture (and sometimes the description).
        info = dict(V5_INFO, text="2023/08/17 14:20:05 N:12.34567 E:65.43210 45 KM/H",
                    description="A road at 12.34567, -65.43210 in the rain.",
                    title="Road near N 12.34567")
        image_id = insert_image(self.conn, "lib_family1", "clip.jpg", described=1,
                                prompt_ver=5, info=info)
        record = self.records(for_export=True)[image_id]
        self.assertEqual(record["o"], "2023/08/17 14:20:05 45 KM/H")
        self.assertEqual(record["d"], "A road at in the rain.")
        self.assertEqual(record["t"], "Road near")
        # With the location, and on the server, as written.
        record = self.records(for_export=True, export_options={"location": True})[image_id]
        self.assertEqual(record["o"], info["text"])
        self.assertEqual(self.records()[image_id]["d"], info["description"])

    def test_positions_written_out_in_text(self):
        scrub = imageinfo.scrub_positions
        degree = "\u00b0"
        for text, left in (
                ("N:12.34567 E:65.43210", ""),
                ("12.34567N 65.43210E", ""),
                ("12" + degree + " 20' 44.4\" N, 65" + degree + " 25' 55.6\" E", ""),
                ("lat 12.34567 lon -65.43210 here", "here"),
                ("At 12.34567, -65.43210.", "At ."),
                ("SPEED 45\nS:1.2345 W:65.4321", "SPEED 45"),
                # Not positions: prices, versions, short numbers, words.
                ("Total 12.99, 3.49", "Total 12.99, 3.49"),
                ("Version 1.2.3 of SUNSET 1.234", "Version 1.2.3 of SUNSET 1.234"),
                ("Route E 4 north, 12.5 km", "Route E 4 north, 12.5 km"),
                ("", ""), (None, None)):
            self.assertEqual(scrub(text), left, text)

    def test_coordinates_never_appear(self):
        insert_image(self.conn, "lib_family1", "a.jpg", lat=12.3456, lon=-65.4321)
        _status, body, _headers = search_index.IndexService(watch=False).respond(
            self.conn, self.config, {"lib_family1": "Family"}, include_hidden=True)
        text = body.decode("utf-8")
        self.assertNotIn("12.3456", text)
        self.assertNotIn("65.4321", text)


class EditedRecordTest(IndexFixture, unittest.TestCase):
    """The owner's edits (image_edits) are laid over the descriptions."""

    def test_records_use_the_owners_words(self):
        image_id = insert_image(self.conn, "lib_family1", "dog.jpg", described=1,
                                prompt_ver=5, info=V5_INFO)
        plain = insert_image(self.conn, "lib_family1", "plain.jpg", described=1,
                             prompt_ver=5, info=V5_INFO)
        image_edits.save(self.conn, image_id, {"title": "Rex catching his frisbee",
                                               "tags_add": ["Rex"], "tags_remove": ["beach"]})
        self.conn.commit()
        records = self.records()
        record = records[image_id]
        self.assertEqual((record["t"], record["d"], record["g"], record["ed"]),
                         ("Rex catching his frisbee", "A dog on a beach.", ["rex", "dog"], 1))
        self.assertNotIn("ed", records[plain])
        self.assertEqual(records[plain]["g"], ["dog", "beach"])
        # Only the admin's records (those with hidden pictures) tell which.
        public = self.records(include_hidden=False)[image_id]
        self.assertEqual(public, {key: value for key, value in record.items() if key != "ed"})
        # An export publishes the owner's words, but not that they were edited.
        exported = self.records(for_export=True)[image_id]
        self.assertEqual((exported["t"], exported["g"]), ("Rex catching his frisbee",
                                                          ["rex", "dog"]))
        self.assertNotIn("ed", exported)
        self.assertEqual(search_index.picture_record(self.conn, image_id,
                                                     image_edits.load(self.conn, image_id)),
                         dict(record, ed=1))
        self.assertIsNone(search_index.picture_record(self.conn, 999999))

    def test_a_picture_not_described_yet_can_be_edited(self):
        image_id = insert_image(self.conn, "lib_family1", "new.jpg")
        image_edits.save(self.conn, image_id, {"title": "Grandma's garden",
                                               "description": "Taken in June.",
                                               "tags_add": ["garden"]})
        self.conn.commit()
        record = self.records()[image_id]
        self.assertEqual(record, {"i": image_id, "l": "lib_family1", "n": "new.jpg",
                                  "t": "Grandma's garden", "d": "Taken in June.",
                                  "g": ["garden"], "w": 640, "hgt": 480, "ed": 1})

    def test_a_saved_edit_changes_the_index_state(self):
        image_id = insert_image(self.conn, "lib_family1", "dog.jpg", described=1,
                                prompt_ver=5, info=V5_INFO)
        before = search_index.data_state(self.conn, ["lib_family1"])
        image_edits.save(self.conn, image_id, {"tags_add": ["rex"]}, now=NOW + 60)
        self.conn.commit()
        after = search_index.data_state(self.conn, ["lib_family1"])
        self.assertNotEqual(before, after)
        image_edits.save(self.conn, image_id, {"tags_add": []}, now=NOW + 120)
        self.conn.commit()
        self.assertNotEqual(search_index.data_state(self.conn, ["lib_family1"]), after)


class DuplicateGroupTest(unittest.TestCase):

    BASE = 0x0123456789ABCDEF

    def test_pairs_that_differ_in_a_high_bit_are_grouped(self):
        # Sorting the hashes put unrelated pictures between these two, so
        # the old page never compared them (and never grouped them).
        items = [(1, "0123456789abcdef", ""), (2, "8123456789abcdef", ""),
                 (3, "4f00ff00ff00ff00", ""), (4, "70f0f0f0f0f0f0f0", "")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 2]])

    def test_every_pair_within_eight_bits_is_found(self):
        rng = random.Random(7)
        for distance in range(0, 13):
            for _ in range(60):
                other = flip(self.BASE, *rng.sample(range(64), distance))
                pair = [(1, hexed(self.BASE), ""), (2, hexed(other), "")]
                self.assertEqual(search_index.duplicate_groups(dated(pair)),
                                 [[1, 2]] if distance <= 8 else [], distance)
                # Without trusted dates the hashes must be closer.
                self.assertEqual(search_index.duplicate_groups(pair),
                                 [[1, 2]] if distance <= 6 else [], distance)

    def test_two_bits_apart_in_every_band(self):
        # The hardest case for a banded index: no band is within one bit.
        # (Band b holds the bits p with p % 4 == b.)
        other = flip(self.BASE, 0, 4, 1, 5, 2, 6, 3, 7)
        groups = search_index.duplicate_groups(dated([(1, hexed(self.BASE), ""),
                                                      (2, hexed(other), "")]))
        self.assertEqual(groups, [[1, 2]])
        far = flip(other, 8)
        self.assertEqual(search_index.duplicate_groups(dated([(1, hexed(self.BASE), ""),
                                                              (2, hexed(far), "")])), [])

    def test_groups_do_not_chain(self):
        # Each step is 6 bits; single-link grouping would join all five.
        chain = [self.BASE]
        for step in range(4):
            chain.append(flip(chain[-1], *range(6 * step, 6 * step + 6)))
        items = dated([(index + 1, hexed(value), "") for index, value in enumerate(chain)])
        groups = search_index.duplicate_groups(items)
        self.assertEqual(groups, [[1, 2], [3, 4]])
        by_id = dict((item[0], int(item[1], 16)) for item in items)
        for ids in groups:
            for image_id in ids:
                self.assertLessEqual(bin(by_id[ids[0]] ^ by_id[image_id]).count("1"), 8)

    def test_nearest_representative_wins(self):
        a = self.BASE
        b = flip(a, *range(16))                 # 16 bits from a: its own group
        c = flip(a, *range(10))                 # 10 from a, 6 from b
        groups = search_index.duplicate_groups([(1, hexed(a), ""), (2, hexed(b), ""),
                                                (3, hexed(c), "")])
        self.assertEqual(groups, [[2, 3]])

    def test_trusted_dates_must_be_about_the_same_time(self):
        near = hexed(flip(self.BASE, 1, 9, 17, 33, 41))     # 5 bits apart
        base = hexed(self.BASE)

        def pair(first, second, other=near):
            return search_index.duplicate_groups([(1, base, "", first), (2, other, "", second)])
        self.assertEqual(pair("2020-06-01T09:00:00", "2020-06-01T21:30:00"), [[1, 2]])
        # Another day: never, however alike.
        self.assertEqual(pair("2020-06-01T09:00:00", "2020-06-02T09:00:00"), [])
        self.assertEqual(pair("2020-06-01T09:00:00", "2019-06-01T09:00:00", base), [])
        # Across midnight, at most 12 hours apart.
        self.assertEqual(pair("2020-06-01T23:10:00", "2020-06-02T07:45:00"), [[1, 2]])
        self.assertEqual(pair("2020-06-02T07:45:00", "2020-06-01T23:10:00"), [[1, 2]])
        self.assertEqual(pair("2020-06-01T20:00:00", "2020-06-02T08:00:00"), [[1, 2]])
        self.assertEqual(pair("2020-06-01T20:00:00", "2020-06-02T08:00:01"), [])
        self.assertEqual(pair("2020-12-31T22:00:00", "2021-01-01T01:00:00"), [[1, 2]])
        # A date without a time only matches the same day.
        self.assertEqual(pair("2020-06-01", "2020-06-01T23:00:00"), [[1, 2]])
        self.assertEqual(pair("2020-06-01", "2020-06-02T01:00:00"), [])

    def test_pictures_without_a_trusted_date_need_a_closer_match(self):
        base = hexed(self.BASE)
        six = hexed(flip(self.BASE, *range(0, 48, 8)))
        seven = hexed(flip(self.BASE, *range(0, 56, 8)))
        for first, second in (("", ""), (DAY, ""), ("", DAY)):
            self.assertEqual(search_index.duplicate_groups(
                [(1, base, "", first), (2, six, "", second)]), [[1, 2]], (first, second))
            self.assertEqual(search_index.duplicate_groups(
                [(1, base, "", first), (2, seven, "", second)]), [], (first, second))
        self.assertEqual(search_index.duplicate_groups(
            [(1, base, "", DAY), (2, seven, "", DAY)]), [[1, 2]])
        # A date that is not a real one counts as none.
        self.assertEqual(search_index.duplicate_groups(
            [(1, base, "", "2020-02-30T10:00:00"), (2, seven, "", "2020-02-30T10:00:00")]), [])

    def test_trusted_date_sources(self):
        self.assertEqual(search_index.trusted_date(DAY, "exif"), DAY)
        self.assertEqual(search_index.trusted_date(DAY, "filename"), DAY)
        self.assertEqual(search_index.trusted_date(DAY, "mtime"), "")
        self.assertEqual(search_index.trusted_date(DAY, ""), "")
        self.assertEqual(search_index.trusted_date("", "exif"), "")
        self.assertEqual(search_index.trusted_date(None, "exif"), "")
        self.assertEqual(search_index.trusted_date("garbage", "exif"), "")

    def test_dates_do_not_chain_through_a_group(self):
        base = hexed(self.BASE)
        # The first picture's date (a file time) cannot be trusted; the
        # group then keeps the first trusted date it gets.
        items = [(1, base, "", ""),
                 (2, hexed(flip(self.BASE, 1)), "", "2020-06-01T10:00:00"),
                 (3, hexed(flip(self.BASE, 2)), "", "2020-06-01T18:00:00"),
                 (4, hexed(flip(self.BASE, 3)), "", "2020-06-05T10:00:00"),
                 (5, hexed(flip(self.BASE, 4)), "", "")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 2, 3, 5]])
        # A dated first picture: across midnight from it, but not from the
        # picture that joined across midnight.
        items = [(1, base, "", "2020-06-01T23:00:00"),
                 (2, hexed(flip(self.BASE, 1)), "", "2020-06-02T10:00:00"),
                 (3, hexed(flip(self.BASE, 2)), "", "2020-06-02T22:00:00"),
                 (4, hexed(flip(self.BASE, 3)), "", "2020-06-02T22:30:00")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 2], [3, 4]])

    def test_a_dated_picture_skips_a_nearer_group_of_another_day(self):
        base = hexed(self.BASE)
        items = [(1, base, "", "2020-06-01T10:00:00"),
                 (2, hexed(flip(self.BASE, *range(10, 30))), "", "2020-06-03T10:00:00"),
                 (3, hexed(flip(self.BASE, 0)), "", "2020-06-03T11:00:00"),
                 (4, hexed(flip(self.BASE, *range(10, 30), 0, 1, 2, 3, 4, 5, 6)), "",
                  "2020-06-03T11:00:00")]
        # 3 is 1 bit from 1 (another day) and 21 from 2; 4 is 7 bits from 2.
        self.assertEqual(search_index.duplicate_groups(items), [[2, 4]])
        # The same hash as a group of another day starts a group of its own.
        items = [(1, base, "", "2020-06-01T10:00:00"), (2, base, "", "2020-06-03T10:00:00"),
                 (3, base, "", "2020-06-03T12:00:00"), (4, base, "", "2020-06-01T11:00:00")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 4], [2, 3]])

    def test_exact_copies_group_whatever_their_dates(self):
        base = hexed(self.BASE)
        items = [(1, base, "same", "2020-06-01T10:00:00"), (2, base, "same", ""),
                 (3, base, "same", "2001-01-01T00:00:00"),
                 (4, "0000000000000000", "plain", "2020-06-01T10:00:00"),
                 (5, "0000000000000000", "plain", "2021-06-01T10:00:00")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 2, 3], [4, 5]])

    def test_exact_copies_always_group(self):
        items = [(1, hexed(self.BASE), "aaa"), (5, "", "aaa"), (9, "nonsense", "aaa"),
                 (3, hexed(flip(self.BASE, *range(30))), "bbb"),
                 (4, "", "")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 5, 9]])
        # A copy brings its twin along into a near-duplicate group.
        items.append((7, hexed(flip(self.BASE, 1, 2)), ""))
        self.assertEqual(search_index.duplicate_groups(items), [[1, 5, 7, 9]])

    def test_pictures_without_detail_only_group_as_copies(self):
        # Black, white and plain-sky pictures all hash to nearly 0 (or all ones).
        items = [(1, "0000000000000000", "a"), (2, "0000000000000001", "b"),
                 (3, "ffffffffffffffff", "c"), (4, "fffffffffffffffe", "d"),
                 (5, "0000000000000000", "a")]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 5]])

    # Dates near each other (across midnight; within a burst, and just
    # outside one), and none.
    DATES = ("", "", "2020-01-01T10:00:00", "2020-01-01T10:00:02", "2020-01-01T10:00:05",
             "2020-01-01T23:30:00", "2020-01-02T09:00:00", "2020-01-02T20:00:00",
             "2020-01-05T12:00:00", "2020-01-05")
    # Aspect ratios: unknown, 4:3, 3:2, a little wider, 16:9, upright, a panorama.
    ASPECTS = (None, 4 / 3.0, 4 / 3.0, 1.5, 1.6, 16 / 9.0, 0.75, 3.5)

    def test_matches_a_brute_force_reference(self):
        for seed in range(4):
            rng = random.Random(seed)
            hashes, items = [], []
            for image_id in range(1, 801):
                if hashes and rng.random() < 0.4:
                    value = flip(rng.choice(hashes), *rng.sample(range(64), rng.randint(0, 10)))
                else:
                    value = rng.getrandbits(64)
                hashes.append(value)
                dhash = hexed(value) if rng.random() > 0.03 else ""
                content = "c%d" % rng.randrange(200) if rng.random() < 0.15 else ""
                items.append((image_id, dhash, content, rng.choice(self.DATES),
                              rng.choice(self.ASPECTS)))
            if seed == 3:
                items = [item[:3] for item in items]        # no dates at all
            elif seed == 2:
                items = [item[:4] for item in items]        # no shapes
            expected = brute_force_groups(items)
            rng.shuffle(items)
            self.assertEqual(search_index.duplicate_groups(items), expected, seed)

    def test_adding_new_pictures_matches_working_it_out_again(self):
        for seed in range(3):
            rng = random.Random(100 + seed)
            hashes, items, copy_dates = [], [], {}
            for image_id in range(1, 601):
                if hashes and rng.random() < 0.4:
                    value = flip(rng.choice(hashes), *rng.sample(range(64), rng.randint(0, 9)))
                else:
                    value = rng.getrandbits(64)
                hashes.append(value)
                content = "c%d" % rng.randrange(150) if rng.random() < 0.2 else ""
                taken = rng.choice(self.DATES)
                aspect = rng.choice(self.ASPECTS)
                if content:
                    # Exact copies hold the same EXIF data and size, so the
                    # same date and shape.
                    taken, aspect = copy_dates.setdefault(content, (taken, aspect))
                items.append((image_id, hexed(value), content, taken, aspect))
            grouper = search_index._Grouper(8, 6)
            start = 0
            for end in (200, 201, 350, 600):
                self.assertTrue(grouper.add(items[start:end]))
                start = end
                self.assertEqual(grouper.groups(), search_index.duplicate_groups(items[:end]),
                                 (seed, end))
            self.assertEqual(grouper.max_id, 600)
            # Only newer pictures can be added.
            self.assertFalse(grouper.add([(5, hexed(1), "")]))

    def test_a_copy_that_brings_the_first_usable_hash_cannot_be_added(self):
        grouper = search_index._Grouper(8)
        self.assertTrue(grouper.add([(1, "", "same"), (2, hexed(self.BASE), "")]))
        # Worked out again, picture 1 would get the copy's hash at its own
        # (older) place and could pull picture 2 into its group.
        self.assertFalse(grouper.add([(3, hexed(self.BASE), "same")]))
        # A copy without a usable hash just joins its twin.
        other = search_index._Grouper(8)
        other.add([(1, "", "same")])
        self.assertTrue(other.add([(3, "", "same")]))
        self.assertEqual(other.groups(), [[1, 3]])

    def test_a_copy_that_brings_the_first_trusted_date_cannot_be_added(self):
        grouper = search_index._Grouper()
        self.assertTrue(grouper.add([(1, hexed(self.BASE), "same", "")]))
        # Worked out again, the group would have that date (and refuse
        # pictures of other days).
        self.assertFalse(grouper.add([(3, hexed(self.BASE), "same", DAY)]))
        # A copy with the same date, or none, just joins its twin.
        other = search_index._Grouper()
        other.add([(1, hexed(self.BASE), "same", DAY)])
        self.assertTrue(other.add([(2, hexed(self.BASE), "same", ""),
                                   (3, hexed(self.BASE), "same", DAY)]))
        self.assertEqual(other.groups(), [[1, 2, 3]])
        # Dates do not matter for pictures that only group as copies.
        alone = search_index._Grouper()
        alone.add([(1, "", "same", "")])
        self.assertTrue(alone.add([(3, "", "same", DAY)]))
        self.assertEqual(alone.groups(), [[1, 3]])

    def test_alike_low_detail_hashes_take_bounded_work(self):
        # Hashes with about 16 of 64 bits set (pages, flat screenshots)
        # crowd the band buckets; each picture still looks at a bounded
        # number of representatives, so this stays fast: without dates,
        # all on one day (a camera whose clock was reset) and all in the
        # same second (a clock that was never set).
        rng = random.Random(5)
        values = []
        for _ in range(4000):
            value = 0
            for bit in range(64):
                if rng.random() < 0.25:
                    value |= 1 << bit
            values.append(value)
        cases = (("undated", lambda n: "", 6),
                 ("one day", lambda n: "2000-01-01T%02d:%02d:%02d" % (
                     n // 360, n // 6 % 60, n % 6 * 10), 8),
                 ("one second", lambda n: "2000-01-01T00:00:00", 12))
        for label, taken, limit in cases:
            items = [(n, hexed(value), "", taken(n)) for n, value in enumerate(values, 1)]
            grouper = search_index._Grouper()
            started = time.perf_counter()
            grouper.add(items)
            self.assertLess(time.perf_counter() - started, 10.0, label)
            if label != "undated":
                self.assertGreater(grouper.limited, 0, label)
            # Every member is still within the threshold of its group's first picture.
            for ids in grouper.members:
                for image_id in ids:
                    self.assertLessEqual(
                        bin(values[ids[0] - 1] ^ values[image_id - 1]).count("1"), limit, label)

    def test_one_scene_on_many_days(self):
        # A webcam with a fixed view: the same scene with a little noise,
        # every hour for a year. Each day is a group of its own: the many
        # alike groups of other days never crowd out the picture's own day.
        rng = random.Random(11)
        items = []
        first = datetime.date(2020, 1, 1)
        for day in range(366):
            date = (first + datetime.timedelta(days=day)).isoformat()
            for hour in range(24):
                value = flip(self.BASE, *rng.sample(range(64), rng.randint(0, 3)))
                items.append((len(items) + 1, hexed(value), "", "%sT%02d:00:00" % (date, hour)))
        groups = search_index.duplicate_groups(items)
        self.assertEqual(len(groups), 366)
        for ids in groups:
            self.assertEqual(len(ids), 24)
            self.assertEqual(len({items[image_id - 1][3][:10] for image_id in ids}), 1)
        # The very same picture every day takes about linear time too.
        items = [(n + 1, hexed(self.BASE), "", "%sT%02d:00:00" % (
            (first + datetime.timedelta(days=n // 10)).isoformat(), n % 10)) for n in range(20000)]
        started = time.perf_counter()
        groups = search_index.duplicate_groups(items)
        self.assertLess(time.perf_counter() - started, 10.0)
        self.assertEqual(len(groups), 2000)

    def test_a_dated_picture_prefers_dated_groups(self):
        a = self.BASE
        c = flip(a, 0, 1, 2, 3, 4)                  # 5 bits from a
        b = flip(c, *range(10, 16))                 # 6 bits from c, 11 from a
        # 1 has only a file time; 2 and 3 were taken the same minute. 3
        # is nearer to 1, but joins 2: its own burst, not a look-alike.
        items = [(1, hexed(a), "", ""), (2, hexed(b), "", "2004-09-05T02:04:00"),
                 (3, hexed(c), "", "2004-09-05T02:04:21")]
        self.assertEqual(search_index.duplicate_groups(items), [[2, 3]])
        # Even an exact match without a date loses to a dated group.
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(a), "", ""), (2, hexed(flip(a, *range(7))), "", "2004-09-05T02:04:00"),
             (3, hexed(a), "", "2004-09-05T02:04:21")]), [[2, 3]])
        # With no dated group to join, the undated one still takes it.
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(a), "", ""), (3, hexed(c), "", "2004-09-05T02:04:21")]), [[1, 3]])
        # A picture without a trusted date just takes the nearest.
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(a), "", ""), (2, hexed(b), "", "2004-09-05T02:04:00"),
             (3, hexed(c), "", "")]), [[1, 3]])

    def test_bursts_may_differ_more(self):
        base = hexed(self.BASE)
        ten = hexed(flip(self.BASE, *range(0, 40, 4)))          # 10 bits apart
        thirteen = hexed(flip(self.BASE, *range(0, 52, 4)))     # 13 bits apart

        def pair(first, second, other=ten):
            return search_index.duplicate_groups([(1, base, "", first), (2, other, "", second)])
        # Taken at most two seconds apart: the same shot.
        self.assertEqual(pair("2020-06-01T10:00:00", "2020-06-01T10:00:02"), [[1, 2]])
        self.assertEqual(pair("2020-06-01T23:59:59", "2020-06-02T00:00:01"), [[1, 2]])
        self.assertEqual(pair("2020-06-01T10:00:00", "2020-06-01T10:00:03"), [])
        self.assertEqual(pair("2020-06-01T10:00:00", "2020-06-01T10:00:00", thirteen), [])
        # Both need a capture time to trust.
        self.assertEqual(pair("2020-06-01", "2020-06-01T10:00:00"), [])
        self.assertEqual(pair("", "2020-06-01T10:00:00"), [])

    def test_a_second_with_many_groups_is_no_burst(self):
        # A camera whose clock was never set gives every picture the same
        # time. Once BURST_GROUPS groups have started in that second, it no
        # longer counts as a burst.
        rng = random.Random(3)
        moment = "2000-01-01T00:00:00"
        values = [self.BASE]
        while len(values) < search_index.BURST_GROUPS:
            value = rng.getrandbits(64)
            if (not search_index._low_detail(value)
                    and all(bin(value ^ other).count("1") > 24 for other in values)):
                values.append(value)
        items = [(n, hexed(value), "", moment) for n, value in enumerate(values, 1)]
        ten = (len(values) + 1, hexed(flip(self.BASE, *range(0, 40, 4))), "", moment)
        # With one group fewer it is a burst; with them all, it is not.
        self.assertEqual(search_index.duplicate_groups(items[:-1] + [ten]), [[1, ten[0]]])
        self.assertEqual(search_index.duplicate_groups(items + [ten]), [])

    def test_a_file_name_date_without_a_time_is_a_day(self):
        taken, source, _offset = imageinfo.parse_capture_date({}, "IMG-20210625-WA0001.jpg", None)
        self.assertEqual((taken, source), ("2021-06-25T00:00:00", "filename"))
        self.assertEqual(search_index.trusted_date(taken, source), "2021-06-25")
        taken, source, _offset = imageinfo.parse_capture_date({}, "IMG_20210625_101530.jpg", None)
        self.assertEqual(search_index.trusted_date(taken, source), "2021-06-25T10:15:30")
        # Cameras do write midnight as a time.
        self.assertEqual(search_index.trusted_date("2021-06-25T00:00:00", "exif"),
                         "2021-06-25T00:00:00")
        # So such a day does not reach back into the evening before, and
        # pictures named with the same day are no burst.
        day = search_index.trusted_date("2021-06-25T00:00:00", "filename")
        near = hexed(flip(self.BASE, 1, 9, 17, 33, 41))
        ten = hexed(flip(self.BASE, *range(0, 40, 4)))
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(self.BASE), "", day), (2, near, "", "2021-06-24T13:00:00")]), [])
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(self.BASE), "", "2021-06-25T00:00:00"),
             (2, near, "", "2021-06-24T13:00:00")]), [[1, 2]])
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(self.BASE), "", day), (2, ten, "", day)]), [])

    def test_low_detail_limit(self):
        # Pages of text on white have 9 to 20 of their 64 bits set; photos
        # about 20 to 45. Below 12 (or above 52) only copies group.
        self.assertTrue(search_index._low_detail(int("0f0f000000000007", 16)))
        self.assertFalse(search_index._low_detail(int("0f0f0f0000000000", 16)))
        self.assertTrue(search_index._low_detail(~int("0f0f000000000007", 16) & (2 ** 64 - 1)))

    def test_bad_input(self):
        self.assertIsNone(search_index.parse_dhash("xyz"))
        self.assertIsNone(search_index.parse_dhash(None))
        self.assertEqual(search_index.parse_dhash("00000000000000ff"), 255)
        self.assertEqual(search_index.duplicate_groups([]), [])
        with self.assertRaises(ValueError):
            search_index.duplicate_groups([], threshold=9)
        # A shape that is no positive number counts as unknown.
        for bad in (0, -1.5, float("nan"), float("inf"), True, "1.5"):
            self.assertEqual(search_index.duplicate_groups(
                [(1, hexed(self.BASE), "", DAY, 4 / 3.0), (2, hexed(self.BASE), "", DAY, bad)]),
                [[1, 2]], bad)

    def test_only_pictures_of_about_the_same_shape_group(self):
        base, near = hexed(self.BASE), hexed(flip(self.BASE, 3, 40))

        def pair(first, second, copy=False):
            return search_index.duplicate_groups(
                [(1, base, "same" if copy else "", DAY, first),
                 (2, base if copy else near, "same" if copy else "", DAY, second)])
        # 4:3 and 3:2 (12.5% wider) group; 4:3 and 16:9 do not, nor do a
        # night shot and a wide panorama, or a landscape and an upright one.
        self.assertEqual(pair(4 / 3.0, 1.5), [[1, 2]])
        self.assertEqual(pair(1.5, 4 / 3.0), [[1, 2]])
        self.assertEqual(pair(4 / 3.0, 16 / 9.0), [])
        self.assertEqual(pair(4032 / 3024.0, 4000 / 1100.0), [])
        self.assertEqual(pair(4 / 3.0, 3 / 4.0), [])
        # At most 15% apart, either way round.
        self.assertEqual(pair(1.0, 1.15), [[1, 2]])
        self.assertEqual(pair(1.15, 1.0), [[1, 2]])
        self.assertEqual(pair(1.0, 1.151), [])
        # An unknown shape (no size stored) fits any.
        self.assertEqual(pair(None, 3.5), [[1, 2]])
        self.assertEqual(pair(3.5, None), [[1, 2]])
        # Identical files always group.
        self.assertEqual(pair(4 / 3.0, 3.5, copy=True), [[1, 2]])
        # Without trusted dates too.
        self.assertEqual(search_index.duplicate_groups(
            [(1, base, "", "", 4 / 3.0), (2, base, "", "", 16 / 9.0)]), [])
        self.assertEqual(search_index.duplicate_groups(
            [(1, base, "", "", 4 / 3.0), (2, base, "", "", 1.4)]), [[1, 2]])

    def test_every_two_pictures_of_a_group_have_about_the_same_shape(self):
        base = hexed(self.BASE)
        # 2 fits 1 (10% wider) and 3 fits 2 (10% wider), but 3 is 21%
        # wider than 1: it starts a group of its own, which 4 then joins.
        items = [(1, base, "", DAY, 1.0), (2, base, "", DAY, 1.1),
                 (3, base, "", DAY, 1.21), (4, base, "", DAY, 1.25)]
        self.assertEqual(search_index.duplicate_groups(items), [[1, 2], [3, 4]])
        # The same without dates.
        undated = [item[:3] + ("", item[4]) for item in items]
        self.assertEqual(search_index.duplicate_groups(undated), [[1, 2], [3, 4]])

    def test_a_picture_joins_the_nearest_group_of_its_shape(self):
        a = self.BASE
        b = flip(a, *range(10))                 # 10 bits from a: its own group
        c = flip(a, *range(6))                  # 6 from a, 4 from b
        # c is nearer to b, but only a has its shape.
        for first, second, third in (("", "", ""),
                                     ("2020-06-01T10:00:00", "2020-06-01T10:01:00",
                                      "2020-06-01T10:02:00")):
            items = [(1, hexed(a), "", first, 1.5), (2, hexed(b), "", second, 0.75),
                     (3, hexed(c), "", third, 1.5)]
            self.assertEqual(search_index.duplicate_groups(items), [[1, 3]], first)
            self.assertEqual(search_index.duplicate_groups(
                [item[:4] for item in items]), [[2, 3]], first)
        # Two groups of the very same hash: each picture takes the one of
        # its shape.
        d = flip(a, 1, 2)
        self.assertEqual(search_index.duplicate_groups(
            [(1, hexed(a), "", "", 1.5), (2, hexed(a), "", "", 0.75),
             (3, hexed(a), "", "", 0.75), (4, hexed(d), "", "", 1.5),
             (5, hexed(d), "", "", 0.7)]), [[1, 4], [2, 3, 5]])

    def test_a_copy_that_brings_the_first_shape_cannot_be_added(self):
        base = hexed(self.BASE)
        grouper = search_index._Grouper()
        self.assertTrue(grouper.add([(1, base, "same", DAY, None)]))
        # Worked out again, the group would have that shape (and refuse
        # pictures of other shapes).
        self.assertFalse(grouper.add([(3, base, "same", DAY, 1.5)]))
        other = search_index._Grouper()
        other.add([(1, base, "same", DAY, 1.5)])
        self.assertTrue(other.add([(2, base, "same", DAY, None), (3, base, "same", DAY, 1.5),
                                   (4, base, "", DAY, 3.5), (5, base, "", DAY, 1.4)]))
        self.assertEqual(other.groups(), [[1, 2, 3, 5]])
        # Shapes do not matter for pictures that only group as copies.
        alone = search_index._Grouper()
        alone.add([(1, "", "same", DAY, None)])
        self.assertTrue(alone.add([(3, "", "same", DAY, 1.5)]))
        self.assertEqual(alone.groups(), [[1, 3]])

    def test_aspect_ratio(self):
        self.assertEqual(search_index.aspect_ratio(4000, 3000), 4000 / 3000.0)
        self.assertEqual(search_index.aspect_ratio(3000, 4000), 0.75)
        for width, height in ((0, 3000), (4000, 0), (None, 10), (10, None), ("x", 10),
                              (-4, 3), (float("inf"), 3)):
            self.assertIsNone(search_index.aspect_ratio(width, height), (width, height))
        self.assertTrue(search_index.same_shape(1.0, 1.15))
        self.assertFalse(search_index.same_shape(1.0, 1.16))
        self.assertTrue(search_index.same_shape(None, 9.0))


class GroupsInRecordsTest(IndexFixture, unittest.TestCase):

    def add(self, path, dhash, library_id="lib_family1", **fields):
        return insert_image(self.conn, library_id, path, dhash=dhash, **fields)

    def test_cover_is_the_best_picture(self):
        dhash = hexed(DuplicateGroupTest.BASE)
        near = hexed(flip(DuplicateGroupTest.BASE, 3, 40))
        plain = self.add("plain.jpg", dhash, taken_at="2020-01-01T10:00:00")
        sharp = self.add("sharp.jpg", near, described=1, prompt_ver=5,
                         info={"aesthetic": 3, "sharpness": 5})
        pretty = self.add("pretty.jpg", dhash, described=1, prompt_ver=5,
                          info={"aesthetic": 5, "sharpness": 1})
        loner = self.add("loner.jpg", hexed(flip(DuplicateGroupTest.BASE, *range(20))))
        records = self.records()
        self.assertEqual({image_id: records[image_id].get("dg") for image_id in records},
                         {plain: pretty, sharp: pretty, pretty: pretty, loner: None})
        # Same scores: the oldest picture wins (a forwarded or re-saved
        # copy is dated by its file, so it is newer).
        older = self.add("older.jpg", "0f0f0f0f0f0f0f0f", taken_at="2018-01-01T00:00:00")
        newer = self.add("newer.jpg", "0f0f0f0f0f0f0f0f", taken_at="2019-01-01T00:00:00")
        records = self.records()
        self.assertEqual((records[older]["dg"], records[newer]["dg"]), (older, older))

    def test_cover_prefers_the_original_over_a_copy(self):
        info = {"aesthetic": 4, "sharpness": 4}
        # A chat-app copy: smaller, no camera, dated by its file (newer).
        copy = self.add("copy.jpg", "0f0f0f0f0f0f0f0f", described=1, prompt_ver=5, info=info,
                        width=900, height=600, taken_at="2023-08-08T10:00:00",
                        content_hash="copy")
        original = self.add("IMG_1.jpg", "0f0f0f0f0f0f0f0f", described=1, prompt_ver=5,
                            info=info, width=1023, height=682, taken_at="2019-06-06T12:00:00",
                            camera="Apple iPhone 12", content_hash="original")
        records = self.records()
        self.assertEqual((records[copy]["dg"], records[original]["dg"]), (original, original))
        # The same size: the one with a camera name.
        self.conn.execute("UPDATE images SET width = 1023, height = 682 WHERE id = ?", (copy,))
        self.conn.commit()
        self.assertEqual(self.records()[copy]["dg"], original)
        # Better scores still come first.
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (json.dumps({"aesthetic": 5, "sharpness": 4}), copy))
        self.conn.commit()
        self.assertEqual(self.records()[copy]["dg"], copy)

    def test_cover_key_order(self):
        key = search_index.cover_key
        self.assertGreater(key({"i": 9, "q": 4, "w": 10, "hgt": 10}),
                           key({"i": 1, "q": 3, "w": 4000, "hgt": 3000, "cm": "x"}))
        self.assertGreater(key({"i": 9, "w": 20, "hgt": 10}), key({"i": 1, "w": 10, "hgt": 10}))
        self.assertGreater(key({"i": 9, "cm": "Canon"}), key({"i": 1}))
        self.assertGreater(key({"i": 9, "dk": "2019-01-01"}), key({"i": 1, "dk": "2020-01-01"}))
        self.assertGreater(key({"i": 9, "ts": "2019-01-01T08:00:00", "dk": "2019-01-01"}),
                           key({"i": 1, "ts": "2019-01-01T09:00:00", "dk": "2019-01-01"}))
        self.assertGreater(key({"i": 9, "dk": "2030-01-01"}), key({"i": 1}))
        self.assertGreater(key({"i": 1}), key({"i": 2}))

    def test_pictures_of_other_shapes_are_not_grouped(self):
        # A night shot and two wide panoramas of the same evening: alike
        # dHashes, taken the same day, but other shapes.
        dhash = hexed(DuplicateGroupTest.BASE)
        near = hexed(flip(DuplicateGroupTest.BASE, 3, 40))

        def add(path, value, width, height, taken, content):
            return self.add(path, value, width=width, height=height, taken_at=taken,
                            taken_source="exif", content_hash=content)
        night = add("night.jpg", dhash, 4032, 3024, "2019-07-04T21:30:00", "n1")
        wide = add("pano1.jpg", near, 4000, 1100, "2019-07-04T18:30:00", "p1")
        wider = add("pano2.jpg", dhash, 4300, 1150, "2019-07-04T18:31:00", "p2")
        # An upright picture (its size as displayed, after EXIF rotation).
        upright = add("upright.jpg", dhash, 3024, 4032, "2019-07-04T21:31:00", "u1")
        # A smaller copy of the night shot, and another copy of its file.
        small = add("night-small.jpg", near, 1008, 756, "2019-07-04T21:30:00", "n2")
        same = add("night (1).jpg", dhash, 4032, 3024, "2019-07-04T21:30:00", "n1")
        records = self.records()
        self.assertEqual({image_id: records[image_id].get("dg") for image_id in records},
                         {night: night, wide: wider, wider: wider, upright: None,
                          small: night, same: night})
        # The groups follow a new size (a picture scanned again).
        self.conn.execute("UPDATE images SET width = 4000, height = 3000 WHERE id = ?",
                          (wider,))
        self.conn.commit()
        records = self.records()
        self.assertEqual((records[wide].get("dg"), records[wider]["dg"]), (None, night))

    def test_documents_and_screenshots_group_only_as_copies(self):
        # Different pages of text have nearly the same dHash.
        page = {"photo_type": "document", "document_type": "letter", "aesthetic": 2}
        first = self.add("page1.png", "0f0f0f0f0f0f0f0f", described=1, prompt_ver=5, info=page,
                         content_hash="p1")
        second = self.add("page2.png", hexed(flip(0x0F0F0F0F0F0F0F0F, 1, 9)), described=1,
                          prompt_ver=5, info=page, content_hash="p2")
        shot = self.add("shot.png", "0f0f0f0f0f0f0f0f", described=1, prompt_ver=5,
                        info={"photo_type": "screenshot", "document_type": "none"},
                        content_hash="s1")
        receipt = self.add("receipt.jpg", "0f0f0f0f0f0f0f0f", described=1, prompt_ver=5,
                           info={"photo_type": "photo", "document_type": "receipt"},
                           content_hash="r1")
        copy = self.add("copy of page1.png", "0f0f0f0f0f0f0f0f", described=1, prompt_ver=5,
                        info=page, content_hash="p1")
        photo = self.add("photo.jpg", hexed(flip(0x0F0F0F0F0F0F0F0F, 2)), content_hash="x1")
        records = self.records()
        self.assertEqual(records[first]["dg"], records[copy]["dg"])
        for image_id in (second, shot, receipt, photo):
            self.assertNotIn("dg", records[image_id], image_id)
        # Photos still group with each other.
        burst = self.add("photo2.jpg", hexed(flip(0x0F0F0F0F0F0F0F0F, 2, 30)),
                         content_hash="x2")
        records = self.records()
        self.assertEqual(records[photo]["dg"], records[burst]["dg"])
        self.assertTrue(search_index.exact_copies_only({"ty": "scan"}))
        self.assertTrue(search_index.exact_copies_only({"ty": "photo", "dt": "menu"}))
        self.assertFalse(search_index.exact_copies_only({"ty": "photo"}))
        self.assertFalse(search_index.exact_copies_only({}))

    def test_only_trusted_capture_dates_count(self):
        base = DuplicateGroupTest.BASE
        seven = hexed(flip(base, *range(0, 56, 8)))     # 7 bits from base

        def groups(source, first, second, other=seven):
            """The dg of two pictures 7 bits apart (or other) with these dates."""
            self.conn.execute("DELETE FROM images")
            self.conn.commit()
            one = self.add("a.jpg", hexed(base), taken_at=first, taken_source=source,
                           content_hash="a")
            two = self.add("b.jpg", other, taken_at=second, taken_source=source,
                           content_hash="b")
            records = self.records()
            found = (records[one].get("dg"), records[two].get("dg"))
            return "grouped" if found == (one, one) else found
        # Taken the same day by the camera's clock (or a date in the file
        # name): 7 bits apart is close enough.
        self.assertEqual(groups("exif", "2019-05-14T10:00:00", "2019-05-14T10:00:05"),
                         "grouped")
        self.assertEqual(groups("filename", "2019-06-14T10:00:00", "2019-06-14T18:00:00"),
                         "grouped")
        # File times are not trusted: then 7 bits is too far ...
        self.assertEqual(groups("mtime", "2019-07-14T10:00:00", "2019-07-14T10:00:05"),
                         (None, None))
        self.assertEqual(groups("", "", ""), (None, None))
        self.assertEqual(groups("mtime", "2019-07-14T10:00:00", "2019-07-24T10:00:05",
                                hexed(flip(base, 3))), "grouped")
        # ... and pictures taken on different days never group.
        self.assertEqual(groups("exif", "2019-08-14T10:00:00", "2019-08-20T10:00:00",
                                hexed(base)), (None, None))

    def test_groups_cached_by_an_older_version_are_not_used(self):
        dhash = "0f0f0f0f0f0f0f0f"
        self.add("a.jpg", dhash, content_hash="h1")
        self.add("b.jpg", dhash, content_hash="h2")
        real = search_index._full_grouper
        with mock.patch.object(search_index, "_full_grouper", wraps=real) as work:
            with mock.patch.object(search_index, "GROUPS_VERSION",
                                   search_index.GROUPS_VERSION - 1):
                self.records()
            self.assertEqual(work.call_count, 1)
            search_index._groups_memo.clear()
            self.records()
            self.assertEqual(work.call_count, 2)
            search_index._groups_memo.clear()
            self.records()
            self.assertEqual(work.call_count, 2)

    def test_hidden_pictures_are_not_grouped(self):
        dhash = "0f0f0f0f0f0f0f0f"
        shown = self.add("a.jpg", dhash)
        hidden = self.add("b.jpg", dhash, hidden=1)
        records = self.records()
        self.assertNotIn("dg", records[shown])
        self.assertNotIn("dg", records[hidden])
        third = self.add("c.jpg", dhash)
        records = self.records()
        self.assertEqual((records[shown]["dg"], records[third]["dg"]), (shown, shown))
        self.assertNotIn("dg", records[hidden])

    def test_groups_stay_within_a_library(self):
        dhash = "0f0f0f0f0f0f0f0f"
        first = self.add("a.jpg", dhash)
        second = self.add("a.jpg", dhash, library_id="lib_other001")
        records = self.records(("lib_family1", "lib_other001"))
        self.assertNotIn("dg", records[first])
        self.assertNotIn("dg", records[second])

    def test_groups_are_worked_out_again_after_leaving_records_out(self):
        dhash = "0f0f0f0f0f0f0f0f"
        ids = [self.add("%d.jpg" % number, dhash, described=1, prompt_ver=5,
                        info={"aesthetic": number + 1}) for number in range(3)]
        records = search_index.build_records(self.conn, self.config, ["lib_family1"])
        self.assertEqual({record["dg"] for record in records}, {ids[2]})
        groups = search_index.library_groups(
            "lib_family1", [(image_id, dhash, "") for image_id in ids])
        kept = [record for record in records if record["i"] != ids[2]]
        self.assertEqual(search_index.apply_groups(kept, groups), 1)
        self.assertEqual({record["dg"] for record in kept}, {ids[1]})
        alone = [record for record in kept if record["i"] == ids[0]]
        self.assertEqual(search_index.apply_groups(alone, groups), 0)
        self.assertNotIn("dg", alone[0])

    def test_groups_are_cached_until_the_hashes_change(self):
        dhash = "0f0f0f0f0f0f0f0f"
        first = self.add("a.jpg", dhash, content_hash="h1")
        second = self.add("b.jpg", dhash, content_hash="h2")
        real = search_index._full_grouper
        with mock.patch.object(search_index, "_full_grouper", wraps=real) as work:
            self.records()
            self.assertEqual(work.call_count, 1)
            path = search_index.groups_cache_path("lib_family1")
            self.assertTrue(path.startswith(common.data_dir()))
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["groups"], [[first, second]])
            # A new description or score does not change the groups.
            self.conn.execute("UPDATE images SET described = 1, prompt_ver = 5, info = ?, "
                              "updated = ? WHERE id = ?",
                              (json.dumps({"aesthetic": 5}), NOW + 5, first))
            self.records()
            self.assertEqual(work.call_count, 1)
            # After a restart the groups come from the file.
            search_index._groups_memo.clear()
            self.records()
            self.assertEqual(work.call_count, 1)
            # A new hash or hiding one works them out again; a new picture
            # is only added to the groups (after the first time, when the
            # groups came from the file).
            self.add("c.jpg", "f0f0f0f0f0f0f0f0")
            self.records()
            self.assertEqual(work.call_count, 2)
            fourth = self.add("d.jpg", "f0f0f0f0f0f0f0f0")
            records = self.records()
            self.assertEqual(work.call_count, 2)
            self.assertEqual(records[fourth]["dg"], records[fourth - 1]["dg"])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["groups"],
                                 [[first, second], [fourth - 1, fourth]])
            self.conn.execute("UPDATE images SET dhash = 'f0f0f0f0f0f0f0f0' WHERE id = ?",
                              (second,))
            records = self.records()
            self.assertEqual(work.call_count, 3)
            self.assertNotIn("dg", records[first])
            self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (first,))
            self.records()
            self.assertEqual(work.call_count, 4)
            # A damaged cache file is simply worked out again.
            search_index._groups_memo.clear()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            self.records()
            self.assertEqual(work.call_count, 5)

    def test_cache_file_names_are_safe(self):
        self.assertTrue(search_index.groups_cache_path("lib_family1").endswith(
            "groups-lib_family1.json"))
        odd = search_index.groups_cache_path("../../etc")
        self.assertEqual(os.path.dirname(odd), os.path.join(common.data_dir(), "cache"))
        self.assertNotIn("..", os.path.basename(odd))


@unittest.skipIf(flask is None, "Flask is not installed")
class IndexRouteTest(IndexFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.first = insert_image(self.conn, "lib_family1", "a.jpg", dhash="0f0f0f0f0f0f0f0f",
                                  described=1, prompt_ver=5, info=V5_INFO)
        self.second = insert_image(self.conn, "lib_other001", "b.jpg")
        self.hidden = insert_image(self.conn, "lib_family1", "h.jpg", hidden=1)
        insert_image(self.conn, "lib_off00001", "off.jpg")
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.state = self.app.extensions["search_images"]
        self.addCleanup(self.state.close)
        self.client = self.app.test_client()

    def get(self, url="/api/index", etag=None, gzip_ok=False, **kwargs):
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if gzip_ok:
            headers["Accept-Encoding"] = "gzip, deflate"
        return self.client.get(url, headers=headers, **kwargs)

    def payload(self, response):
        data = response.data
        if response.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))

    def write(self, sql, args=()):
        """A change made by another connection (a scan, a job, another process)."""
        conn = db.connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def test_answer_has_libraries_and_records(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        payload = self.payload(response)
        self.assertEqual(set(payload), {"version", "generated", "libraries", "albums",
                                        "records"})
        self.assertEqual(payload["version"], search_index.INDEX_VERSION)
        self.assertEqual(payload["libraries"], [{"id": "lib_family1", "name": "Family"},
                                                {"id": "lib_other001", "name": "Other"}])
        self.assertEqual([record["i"] for record in payload["records"]],
                         [self.first, self.second, self.hidden])
        self.assertEqual(response.headers["Cache-Control"], "private, no-cache")
        self.assertIn("Accept-Encoding", response.headers["Vary"])

    def test_etag_and_not_modified(self):
        response = self.get()
        etag = response.headers["ETag"]
        self.assertTrue(etag.startswith('"') and etag.endswith('"'), etag)
        again = self.get(etag=etag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.data, b"")
        self.assertEqual(again.headers["ETag"], etag)
        self.assertEqual(self.get(etag='"something-else"').status_code, 200)
        self.assertEqual(self.get(etag="*").status_code, 304)
        # A change gives a new ETag and a full answer.
        self.write("UPDATE images SET updated = ?, info = ? WHERE id = ?",
                   (NOW + 60, json.dumps(dict(V5_INFO, title="Changed")), self.first))
        changed = self.get(etag=etag)
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers["ETag"], etag)
        self.assertEqual(self.payload(changed)["records"][0]["t"], "Changed")

    def test_changes_that_leave_updated_alone_still_change_the_etag(self):
        changes = (
            ("UPDATE images SET hidden = 1 WHERE id = ?", (self.first,)),
            ("UPDATE images SET hidden = 0 WHERE id = ?", (self.first,)),
            ("UPDATE images SET projection = 'equirectangular' WHERE id = ?", (self.first,)),
            ("UPDATE images SET described = 1, prompt_ver = 4, info = '{}' WHERE id = ?",
             (self.second,)),
            ("UPDATE images SET missing_since = 1 WHERE id = ?", (self.second,)),
            ("UPDATE images SET library_id = 'lib_other001' WHERE id = ?", (self.hidden,)),
            ("DELETE FROM images WHERE id = ?", (self.hidden,)),
        )
        etag = self.get().headers["ETag"]
        for sql, args in changes:
            self.write(sql, args)
            response = self.get(etag=etag)
            self.assertEqual(response.status_code, 200, sql)
            etag = response.headers["ETag"]
        # Renaming a library changes the names in the answer, so the ETag too.
        config = common.load_config()
        common.get_library(config, "lib_other001")["name"] = "Renamed"
        common.save_config(config)
        response = self.get(etag=etag)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payload(response)["libraries"][1]["name"], "Renamed")

    def test_gzip_when_the_browser_takes_it(self):
        plain = self.get()
        packed = self.get(gzip_ok=True)
        self.assertEqual(packed.headers["Content-Encoding"], "gzip")
        self.assertNotIn("Content-Encoding", plain.headers)
        self.assertEqual(self.payload(packed)["records"], self.payload(plain)["records"])
        self.assertNotEqual(packed.headers["ETag"], plain.headers["ETag"])
        # Either ETag means the same records.
        for etag in (plain.headers["ETag"], packed.headers["ETag"]):
            self.assertEqual(self.get(etag=etag, gzip_ok=True).status_code, 304)
            self.assertEqual(self.get(etag=etag).status_code, 304)
        refused = self.client.get("/api/index", headers={"Accept-Encoding": "gzip;q=0"})
        self.assertNotIn("Content-Encoding", refused.headers)

    def test_answers_are_built_once_per_change(self):
        for _ in range(3):
            self.get()
            self.get(gzip_ok=True)
        self.assertEqual(self.state.index.builds, 1)
        self.write("UPDATE images SET hidden = 1 WHERE id = ?", (self.second,))
        self.get()
        self.assertEqual(self.state.index.builds, 2)

    def test_a_lone_surrogate_in_stored_text_does_not_break_the_index(self):
        # Older releases stored info with ASCII escapes, so half of an
        # emoji (cut off) could be stored; it cannot be written as UTF-8.
        broken = '{"title": "Party \\ud83d", "description": "Cake \\udc36 time"}'
        self.write("UPDATE images SET info = ?, updated = ? WHERE id = ?",
                   (broken, NOW + 5, self.first))
        for gzip_ok in (False, True):
            response = self.get(gzip_ok=gzip_ok)
            self.assertEqual(response.status_code, 200)
            record = [record for record in self.payload(response)["records"]
                      if record["i"] == self.first][0]
            self.assertEqual(record["t"], "Party \ufffd")
            self.assertEqual(record["d"], "Cake \ufffd time")
        self.assertEqual(search_index.utf8("a\ud83db"), "a\ufffdb".encode("utf-8"))
        self.assertEqual(search_index.utf8("\U0001f436"), "\U0001f436".encode("utf-8"))

    def test_only_changed_libraries_are_built_again(self):
        self.get()
        built = self.state.index.library_builds
        self.assertEqual(built, 2)
        self.write("UPDATE images SET hidden = 1 WHERE id = ?", (self.second,))
        payload = self.payload(self.get())
        self.assertEqual(self.state.index.library_builds, built + 1)
        self.assertEqual([record["i"] for record in payload["records"]],
                         [self.first, self.second, self.hidden])
        self.assertEqual([record.get("h") for record in payload["records"]], [None, 1, 1])
        # Another visitor (no hidden pictures) has lists of its own.
        self.get(environ_base=REMOTE)
        self.assertEqual(self.state.index.library_builds, built + 3)
        self.get(environ_base=REMOTE)
        self.assertEqual(self.state.index.library_builds, built + 3)

    def test_one_library(self):
        payload = self.payload(self.get("/api/index?lib=lib_other001"))
        self.assertEqual(payload["libraries"], [{"id": "lib_other001", "name": "Other"}])
        self.assertEqual([record["i"] for record in payload["records"]], [self.second])
        self.assertEqual(len(self.payload(self.get("/api/index?lib=all"))["records"]), 3)
        for wanted in ("lib_off00001", "lib_orphan01", "nonsense"):
            response = self.get("/api/index?lib=" + wanted)
            self.assertEqual(response.status_code, 404, wanted)
            self.assertIn("error", response.get_json())

    def test_hidden_pictures_only_for_the_admin(self):
        admin = self.get()
        remote = self.get(environ_base=REMOTE)
        self.assertIn(self.hidden, [record["i"] for record in self.payload(admin)["records"]])
        self.assertNotIn(self.hidden,
                         [record["i"] for record in self.payload(remote)["records"]])
        self.assertNotEqual(admin.headers["ETag"], remote.headers["ETag"])
        self.assertEqual(self.get(etag=admin.headers["ETag"], environ_base=REMOTE).status_code,
                         200)

    def test_without_the_watching_connection(self):
        # The ETag is right also when SQLite's change counter cannot be read.
        self.state.index.close()
        with mock.patch.object(self.state.index, "_watching", False):
            etag = self.get().headers["ETag"]
            self.write("UPDATE images SET hidden = 1 WHERE id = ?", (self.second,))
            self.assertEqual(self.get(etag=etag).status_code, 200)

    def test_unknown_host_is_refused(self):
        self.assertEqual(self.client.get("/api/index",
                                         headers={"Host": "attacker.example"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
