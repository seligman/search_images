"""Tests for trip_stories.py: the stories of trips written by a language model from the
trips' facts (text only), kept in index.db, shown on the trip page (/api/trips/<id>/story
and the admin's changes), written by the trip_stories job (python trips.py --stories),
and published by a trip's export only with its "story" option.

Everything here is made up: synthetic rows and pictures in a temporary SETTINGS_BASE, at
made-up positions with made-up place names, made-up people, and a fake model server on
127.0.0.1 (tests/fake_llm.py) that answers with stories the tests write.
"""

import contextlib
import io
import json
import os
import re
import time
import unittest
from unittest import mock

import support

import common
import db
import export
import fake_llm
import jobs
import llm
import people
import tracks
import trip_export
import trip_stories
import trips
from test_admin import AdminFixture
from test_admin_images import REMOTE

if support.pillow_available():
    import cache
    import imagegen
    from test_export import info as described
else:
    imagegen = None

HOME = (40.0, -100.0)
NEAR_HOME = (40.02, -99.97)         # about 3.5 km from HOME
LAKE = (40.0, -98.0)                # about 170 km away
PHONE = "lib_phone009"
LAKES = "lib_lakes009"
HOME_PLACE = ("Hometown", "Region One", "Countryland", "CL")
LAKE_PLACE = ("Lakeside", "Region Two", "Countryland", "CL")
PEAK_PLACE = ("Peakville", "Region Two", "Countryland", "CL")
NOW = 1700000000.0


def quiet(*_args, **_kwargs):
    pass


def info(title, tags=("view",), **fields):
    """A made-up version 5 description."""
    value = {"title": title, "description": "A picture of %s." % title.lower(),
             "tags": list(tags), "subjects": list(tags[:1]), "photo_type": "photo",
             "document_type": "none", "setting": "outdoor", "time_of_day": "day",
             "event": "none", "people": "none", "sensitive": "none", "private_info": False,
             "aesthetic": 3, "sharpness": 4, "text": "", "colors": ["blue"]}
    value.update(fields)
    return value


def add(conn, library_id, path, taken, position=None, place=None, **fields):
    """Insert a described picture row; returns its id."""
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1], "size": 100,
           "mtime": NOW, "ctime": NOW, "width": 640, "height": 480, "added": NOW,
           "updated": NOW, "taken_at": taken, "taken_source": "exif", "described": 1,
           "prompt_ver": 5, "content_hash": "hash-" + path.replace("/", "-")}
    if position is not None:
        row["lat"], row["lon"] = position
    if place is not None:
        row.update(place_city=place[0], place_region=place[1], place_country=place[2],
                   place_cc=place[3], place_km=1.0)
    title = fields.pop("title", "Picture " + path)
    row["info"] = json.dumps(info(title, **fields.pop("words", {})))
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute("INSERT INTO images (%s) VALUES (%s)" % (
        ",".join(columns), ",".join("?" * len(columns))), [row[key] for key in columns])
    conn.commit()
    return cursor.lastrowid


def add_person(store, name, content_hashes, hidden=False):
    """A person (named or not) with a face in each picture of content_hashes."""
    cursor = store.execute("INSERT INTO people (name, hidden, created, updated) VALUES "
                           "(?, ?, 0, 0)", (name, 1 if hidden else 0))
    person_id = cursor.lastrowid
    for content_hash in content_hashes:
        face = store.execute(
            "INSERT INTO faces (content_hash, kind, x, y, w, h, landmarks, score, size, "
            "sharpness, norm, turn, tilt, quality, embedding, updated) VALUES (?, 'photo', 0.4, "
            "0.3, 0.1, 0.1, '[]', 0.9, 50, 100, 10, 0, 0, 0.8, ?, 0)",
            (content_hash, b"\0" * people.EMBEDDING_BYTES)).lastrowid
        store.execute("INSERT INTO face_people (face_id, person_id, owner, updated) VALUES "
                      "(?, ?, 0, 0)", (face, person_id))
    store.commit()
    return person_id


def story_reply(days):
    """A model's answer: {"days": [{"date", "text"}]} as JSON text."""
    return json.dumps({"days": [{"date": date, "text": text} for date, text in days]})


class StoryData(support.TempBaseMixin):
    """A trip library (10-12 May) with pictures of a lake and a peak, people, a picture
    leaving home, a hidden, a private and a sensitive picture; a phone library with home
    days; a made-up home."""

    def setUp(self):
        super().setUp()
        self.folders = {PHONE: self.base.library_folder("Phone"),
                        LAKES: self.base.library_folder("2024-05-12 - Lakes")}
        self.write_config()
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.ids = {}
        conn = self.conn
        for day in range(1, 6):
            add(conn, PHONE, "home/%02d.jpg" % day, "2024-01-%02dT10:00:00" % day,
                position=HOME, place=HOME_PLACE)
        self.ids["leaving"] = add(conn, LAKES, "leaving.jpg", "2024-05-10T06:00:00",
                                  position=NEAR_HOME, place=HOME_PLACE,
                                  title="Our street in Hometown")
        self.ids["lake"] = add(conn, LAKES, "10/lake.jpg", "2024-05-10T12:00:00",
                               position=LAKE, place=LAKE_PLACE, title="Sunset over the lake",
                               words={"tags": ["lake", "sunset"], "aesthetic": 5})
        self.ids["dock"] = add(conn, LAKES, "10/dock.jpg", "2024-05-10T15:00:00",
                               position=LAKE, place=LAKE_PLACE, title="A wooden dock",
                               words={"tags": ["dock"], "aesthetic": 2})
        self.ids["peak"] = add(conn, LAKES, "11/peak.jpg", "2024-05-11T11:00:00",
                               position=(40.1, -98.1), place=PEAK_PLACE,
                               title="Snow on the peak", words={"tags": ["mountain"]})
        self.ids["private"] = add(conn, LAKES, "11/card.jpg", "2024-05-11T12:00:00",
                                  position=LAKE, place=LAKE_PLACE, title="Secret bank card",
                                  words={"private_info": True})
        self.ids["hidden"] = add(conn, LAKES, "11/hidden.jpg", "2024-05-11T13:00:00",
                                 position=LAKE, place=LAKE_PLACE, title="Hidden thing", hidden=1)
        self.ids["sensitive"] = add(conn, LAKES, "11/beach.jpg", "2024-05-11T14:00:00",
                                    position=LAKE, place=LAKE_PLACE, title="Sensitive beach",
                                    words={"sensitive": "suggestive"})
        self.ids["boat"] = add(conn, LAKES, "12/boat.jpg", "2024-05-12T09:00:00",
                               position=LAKE, place=LAKE_PLACE, title="A red boat",
                               words={"tags": ["boat"], "event": "boat_trip"})

    def write_config(self, people_on=True, visitors=False, **extra):
        settings = {"trips": {"home": "%s, %s" % HOME}}
        if people_on:
            settings["people"] = {"enabled": True, "visitors": visitors}
        settings.update(extra)
        self.base.write_config(
            [support.library("Phone", self.folders[PHONE], id=PHONE),
             support.library("Lakes", self.folders[LAKES], id=LAKES)], **settings)
        self.config = common.load_config()

    def add_people(self):
        store = people.connect()
        try:
            add_person(store, "Anna", ["hash-10-lake.jpg", "hash-11-peak.jpg",
                                       "hash-11-hidden.jpg"])
            add_person(store, "Cato", ["hash-10-lake.jpg"], hidden=True)
            add_person(store, "", ["hash-12-boat.jpg"])
            add_person(store, "Zed", ["hash-home-01.jpg"])
        finally:
            store.close()

    def work(self):
        return trip_stories._Work(self.conn, self.config)

    def trip(self, work=None):
        work = work or self.work()
        return work.built.get(LAKES)


# ---------------------------------------------------------------------------

class TextTest(unittest.TestCase):

    def test_sentences_and_what_is_left_out(self):
        text = ("They walked to the dock [#3]. Anna and Cato came along. The sunset in "
                "Hometown was red! [#4] At Lakeside they rested.")
        self.assertEqual(trip_stories.sentences(text), [
            "They walked to the dock [#3].", "Anna and Cato came along.",
            "The sunset in Hometown was red! [#4]", "At Lakeside they rested."])
        self.assertEqual(trip_stories.without_sentences(text, names=["anna smith"]),
                         "They walked to the dock [#3]. The sunset in Hometown was red! [#4] "
                         "At Lakeside they rested.")
        self.assertEqual(trip_stories.without_sentences(text, places=["Hometown"], ids=[3]),
                         "Anna and Cato came along. At Lakeside they rested.")
        # Places as written; names in any case written as a name, by the whole name or
        # its first word.
        self.assertEqual(trip_stories.without_sentences(text, places=["hometown"]), text)
        self.assertEqual(trip_stories.without_sentences("CATO swam.", names=["Cato Jones"]), "")
        self.assertEqual(trip_stories.name_words(["Anna Lee-Smith", "Al", "  Bo  Day "]),
                         {"Anna Lee-Smith", "Anna", "Al", "Bo Day", "Bo"})

    def test_everyday_words_are_no_names(self):
        names = ["Anna Hill", "Rose", "June", "May Brook"]
        text = ("They climbed the hill [#3]. The rose garden bloomed [#4]. Snow fell in "
                "June. On 10 June they swam. June 12 was hot. It may rain by the brook. "
                "Hill Road was steep.")
        self.assertEqual(trip_stories.without_sentences(text, names=names), text)
        # Written as names, they are names.
        self.assertEqual(trip_stories.without_sentences(
            "Rose and Anna swam. June smiled. May Brook came. ANNA sang. The hill was steep.",
            names=names), "The hill was steep.")
        # A name of a script without small letters counts as written.
        name = "\u738b\u5c0f\u660e"
        self.assertEqual(trip_stories.without_sentences("With %s. By the sea." % name,
                                                        names=[name]), "By the sea.")
        # Names that may be named: their words stay ("Anna" of Anna Jones), the others'
        # go.
        self.assertEqual(trip_stories.without_sentences(
            "Anna swam. Anna Smith sat.", names=["Anna Smith"], keep_names=["Anna Jones"]),
            "Anna swam.")

    def test_an_unnamed_person_by_peoples_label(self):
        # (people_groups.label: "Person 12" is someone People has no name for.)
        text = ("Person 3 was also seen. They swam [#4]. Persons 12 waited. A Person 7b. "
                "Then person 5 left.")
        self.assertEqual(trip_stories.without_sentences(text),
                         "They swam [#4]. Persons 12 waited. A Person 7b. Then person 5 left.")

    def test_links(self):
        text = "A dock [#3], a boat [#9][#3] and more [#77]."
        self.assertEqual(trip_stories.links(text), [3, 9, 77])
        self.assertEqual(trip_stories.map_links(text, {3: "abc", 9: "xyz"}),
                         "A dock [#abc], a boat [#xyz][#abc] and more.")
        self.assertEqual(trip_stories.clean_text("**Day one**\n- a  list\x07 item"),
                         "Day one a list item")
        long_text = "One sentence here. " * 300
        cut = trip_stories.clean_text(long_text)
        self.assertLessEqual(len(cut), trip_stories.TEXT_CHARS)
        self.assertTrue(cut.endswith("here."))

    def test_settings(self):
        self.assertEqual(trip_stories.settings({}), trip_stories.DEFAULTS)
        options = trip_stories.settings({"trip_stories": {
            "endpoint": " big ", "pictures": 99, "prompt_tokens": 10, "notes": "We are\nfour.",
            "unknown": 1}})
        self.assertEqual(options, {"endpoint": "big", "pictures": 24, "prompt_tokens": 1000,
                                   "notes": "We are four."})

    def test_best_items(self):
        records = [
            {"i": 1, "ts": "2024-05-10T10:00:00", "q": 3},
            {"i": 2, "ts": "2024-05-10T10:00:05", "q": 5},           # the same moment as 1
            {"i": 3, "ts": "2024-05-10T11:00:00", "q": 2, "fav": 1},
            {"i": 4, "ts": "2024-05-10T12:00:00", "q": 4, "dg": 9},
            {"i": 5, "ts": "2024-05-10T13:00:00", "q": 5, "dg": 9},  # the same stack as 4
            {"i": 6, "ts": "2024-05-10T14:00:00", "q": 5, "ty": "screenshot"},
            {"i": 7, "ts": "2024-05-10T15:00:00", "q": 5, "pr": 1},
            {"i": 8, "ts": "2024-05-10T16:00:00", "q": 5, "dv": 8},  # a drive's clip
            {"i": 10, "ts": "2024-05-10T10:01:00", "q": 1},          # within two minutes
        ]
        best = trip_stories.best_items(records, 3)
        self.assertEqual([record["i"] for record in best], [2, 3, 5])
        best = trip_stories.best_items(records, 5)
        self.assertEqual([record["i"] for record in best], [2, 10, 3, 5])


class FactsTest(StoryData, unittest.TestCase):

    def test_the_facts_leave_out_what_must_not_go(self):
        self.add_people()
        work = self.work()
        trip = self.trip(work)
        facts = work.facts_for(trip)
        self.assertEqual(facts.dates, ["2024-05-10", "2024-05-11", "2024-05-12"])
        day1, day2, day3 = facts.days
        self.assertEqual([item["id"] for item in day1["items"]],
                         [self.ids["lake"], self.ids["dock"]])
        self.assertEqual([item["id"] for item in day2["items"]], [self.ids["peak"]])
        self.assertEqual(day1["places"], ["Lakeside"])
        self.assertEqual(day2["places"], ["Peakville"])
        # Named people who are not hidden only.
        self.assertEqual(day1["people"], ["Anna"])
        self.assertEqual(day3["people"], [])
        self.assertEqual(facts.people, [("Anna", 2)])
        self.assertEqual(facts.refs, {1: self.ids["lake"], 2: self.ids["dock"],
                                      3: self.ids["peak"], 4: self.ids["boat"]})
        self.assertEqual(facts.home_towns, ["Hometown"])
        prompt = trip_stories.prompt_text(facts, facts.dates)
        for secret in ("Hometown", "Our street", "Secret bank card", "Hidden thing",
                       "Sensitive beach", "Cato", "Zed", "lake.jpg", "10/", LAKES, "40.0",
                       "-98.0", str(self.ids["lake"]) + "]"):
            self.assertNotIn(secret, prompt)
        for wanted in ("Trip: Lakes", "Day 1, Friday 10 May 2024 (date 2024-05-10)",
                       '[1] 12:00, photo, Lakeside: "Sunset over the lake". A picture of '
                       "sunset over the lake. Tags: lake, sunset. With Anna.",
                       "People: Anna", "Event: boat trip.",
                       "Write the paragraphs of these days: 2024-05-10, 2024-05-11, "
                       "2024-05-12."):
            self.assertIn(wanted, prompt)
        # The basis: the trip's pictures that are not hidden nor at home.
        self.assertEqual(facts.basis["dates"], facts.dates)
        self.assertNotIn(self.ids["hidden"], facts.basis["ids"])
        self.assertNotIn(self.ids["leaving"], facts.basis["ids"])
        self.assertIn(self.ids["private"], facts.basis["ids"])
        self.assertEqual(facts.basis["names"], ["Anna"])

    def test_hidden_pictures_name_nothing(self):
        # An away trip of 3-4 March: four pictures at the lake each day, and many hidden
        # ones the same days in another town.
        secret_town = ("Secretville", "Region Two", "Countryland", "CL")
        for day in (3, 4):
            for number in range(4):
                add(self.conn, PHONE, "mar/%d-%d.jpg" % (day, number),
                    "2024-03-%02dT1%d:00:00" % (day, number), position=LAKE,
                    place=LAKE_PLACE, title="Lake view %d" % number)
            for number in range(12):
                add(self.conn, PHONE, "mar/h%d-%d.jpg" % (day, number),
                    "2024-03-%02dT0%d:30:00" % (day, number % 9), position=(40.0, -97.9),
                    place=secret_town, hidden=1, title="Hidden %d" % number)
        # (A hidden picture the day before: the owner sees a trip of 2-4 March.)
        add(self.conn, PHONE, "mar/h-early.jpg", "2024-03-02T10:00:00",
            position=(40.0, -97.9), place=secret_town, hidden=1)
        work = self.work()
        trip = [item for item in work.built.trips if item.kind == "away"][0]
        self.assertIn("Secretville", trip.title)
        self.assertEqual(trip.start, "2024-03-02")
        facts = work.facts_for(trip)
        prompt = trip_stories.prompt_text(facts, facts.dates)
        self.assertNotIn("Secretville", prompt)
        self.assertNotIn("Hidden", prompt)
        self.assertEqual(prompt.splitlines()[:2], ["Trip: Lakeside, Region Two",
                                                   "Dates: 3-4 March 2024 (2 days)"])
        self.assertIn("Day 1, Sunday 3 March 2024 (date 2024-03-03)", prompt)
        # The owner's title stays.
        trips.set_trip(self.conn, self.config, [PHONE, LAKES], trip.id, title="Spring")
        work = self.work()
        facts = work.facts_for(work.built.get(trip.id))
        self.assertEqual(facts.title, "Spring")

    def test_no_names_while_people_is_off(self):
        self.add_people()
        self.write_config(people_on=False)
        facts = self.work().facts_for(self.trip())
        self.assertEqual(facts.people, [])
        self.assertNotIn("Anna", trip_stories.prompt_text(facts, facts.dates))

    def test_a_trip_export_home_km_widens_home(self):
        # With the home area of a trip export's 160 km squares, the peak is at home: its
        # day is not told (the lake, just outside the area, is).
        trips.record_export(self.conn, self.config, [PHONE, LAKES], LAKES, {"home_km": 160})
        area = trip_export.HomeArea(HOME, 160)
        self.assertTrue(area.contains(40.1, -98.1))
        self.assertFalse(area.contains(*LAKE))
        facts = self.work().facts_for(self.trip())
        self.assertEqual(facts.dates, ["2024-05-10", "2024-05-12"])
        self.assertEqual(trip_stories.home_km(self.work().built, self.trip()), 160)
        trips.record_export(self.conn, self.config, [PHONE, LAKES], LAKES, {"home_km": 0})
        self.assertEqual(trip_stories.home_km(self.work().built, self.trip()),
                         trip_export.HOME_KM)

    def add_drive(self, first_position):
        """Three dashcam clips one after another on 11 May (with GPS tracks), from
        first_position to the peak, and the drive's description; their ids."""
        import drives
        ids = []
        positions = [first_position, LAKE, (40.1, -98.1)]
        towns = [HOME_PLACE if first_position == NEAR_HOME else LAKE_PLACE, LAKE_PLACE,
                 PEAK_PLACE]
        for number, (position, town) in enumerate(zip(positions, towns)):
            image_id = add(self.conn, LAKES, "dash/20240511_12%02d00_%04d.MP4" % (number, number),
                           "2024-05-11T12:%02d:00" % number, position=position, place=town,
                           media="video", duration=60.0, title="Clip %d" % number)
            fixes = [tracks.Fix(float(second), 1715428800.0 + 60 * number + second,
                                position[0], position[1] + second * 0.001, 25.0, 90.0)
                     for second in range(0, 61, 5)]
            tracks.store(self.conn, tracks.content_key(
                {"content_hash": "hash-dash-20240511_12%02d00_%04d.MP4" % (number, number),
                 "id": image_id}), tracks.Track(fixes, "freegps"), 60.0)
            ids.append(image_id)
        self.conn.commit()
        found = drives.library_drives(self.conn, LAKES, self.config)
        self.assertEqual([drive.ids for drive in found], [ids])
        drives.save_description(LAKES, found[0], info("Along the shore road",
                                                      description="A road along a lake."), {})
        return ids

    def test_a_drive(self):
        ids = self.add_drive(LAKE)
        facts = self.work().facts_for(self.trip())
        drive = facts.day("2024-05-11")["drives"][0]
        self.assertEqual((drive["id"], drive["from"], drive["to"], drive["minutes"]),
                         (ids[0], "Lakeside", "Peakville", 3.0))
        self.assertGreater(drive["km"], 5)
        self.assertEqual(drive["title"], "Along the shore road")
        self.assertIn('a drive from Lakeside to Peakville (about %d km, 3 min): "Along the '
                      'shore road". A road along a lake.' % drive["km"],
                      trip_stories.day_text(facts.day("2024-05-11")))
        # The clips are no pictures of the best.
        self.assertFalse(set(ids) & {item["id"] for item in facts.day("2024-05-11")["items"]})

    def test_a_drive_from_home(self):
        ids = self.add_drive(NEAR_HOME)
        facts = self.work().facts_for(self.trip())
        drive = facts.day("2024-05-11")["drives"][0]
        # Neither the town of home nor the drive's words (filmed there too); its link is
        # its first clip away from home.
        self.assertEqual((drive["id"], drive["from"], drive["to"], drive["title"],
                          drive["text"]), (ids[1], "", "Peakville", "", ""))
        self.assertNotIn("Hometown", trip_stories.prompt_text(facts, facts.dates))
        # How far and how long: only the two clips away from home.
        lengths = trip_stories._drive_lengths(self.conn, set(ids))
        self.assertEqual(drive["minutes"], 2.0)
        self.assertEqual(drive["km"], round(lengths[ids[1]] + lengths[ids[2]]))
        self.assertIn("[%d] a drive to Peakville (about %d km, 2 min):" % (
            drive["ref"], drive["km"]), trip_stories.day_text(facts.day("2024-05-11")))

    def test_drive_words(self):
        line = trip_stories._drive_line
        drive = {"ref": 4, "from": "", "to": "", "km": 0, "minutes": 0, "title": "",
                 "text": ""}
        self.assertEqual(line(dict(drive, to="Peakville")), "[4] a drive to Peakville:")
        self.assertEqual(line(dict(drive, **{"from": "Lakeside"})),
                         "[4] a drive from Lakeside:")
        self.assertEqual(line(dict(drive, to="Peakville", **{"from": "Peakville"})),
                         "[4] a drive around Peakville:")
        self.assertEqual(line(dict(drive, to="Peakville", **{"from": "Lakeside"})),
                         "[4] a drive from Lakeside to Peakville:")
        self.assertEqual(line(drive), "[4] a drive:")

    def test_requests_of_a_few_days(self):
        facts = self.work().facts_for(self.trip())
        whole = trip_stories.plan_requests(facts, 6000, 4000)
        self.assertEqual(whole, [facts.dates])
        one_each = trip_stories.plan_requests(facts, 10, 4000)
        self.assertEqual(one_each, [[date] for date in facts.dates])
        two = trip_stories.plan_requests(facts, 6000, trip_stories.ANSWER_TOKENS_BASE
                                         + 2 * trip_stories.ANSWER_TOKENS_PER_DAY)
        self.assertEqual(two, [facts.dates[:2], facts.dates[2:]])
        prompt = trip_stories.prompt_text(facts, facts.dates[2:])
        self.assertIn("The other days of the trip (already written; do not write them):\n"
                      "Day 1 (2024-05-10): Lakeside\nDay 2 (2024-05-11): Peakville", prompt)
        self.assertNotIn("Sunset over the lake", prompt)

    def test_parse_reply(self):
        self.add_people()
        work = self.work()
        facts = work.facts_for(self.trip())
        reply = story_reply([
            ("2024-05-10", "**The lake** glowed at sunset [1], and the dock waited [2, 4]. "
                           "Zed was there. In Hometown it rained."),
            ("2024-05-11", "Snow lay on the peak [Picture 3]."),
            ("2024-05-13", "A day that was not asked for."),
            ("2024-05-12", "")])
        days, problems = trip_stories.parse_reply(reply, facts, facts.dates, ["Zed"])
        self.assertEqual(days, {
            "2024-05-10": "The lake glowed at sunset [#%d], and the dock waited [#%d]."
                          % (self.ids["lake"], self.ids["dock"]),
            "2024-05-11": "Snow lay on the peak [#%d]." % self.ids["peak"]})
        self.assertEqual(len(problems), 3, problems)
        # Cut off at the length limit: the days that are whole are kept.
        cut = story_reply([("2024-05-10", "One."), ("2024-05-11", "Two.")])[:-12]
        days, _problems = trip_stories.parse_reply(cut, facts, facts.dates)
        self.assertEqual(list(days), ["2024-05-10"])
        with self.assertRaises(ValueError):
            trip_stories.parse_reply("no JSON at all", facts, facts.dates)
        # A month is no name, nor "sunny" a Sunny.
        days, problems = trip_stories.parse_reply(story_reply([
            ("2024-05-10", "On 10 May they climbed the hill [1]. It was sunny.")]), facts,
            ["2024-05-10"], {"May Park", "Sunny", "Anna Hill"})
        self.assertEqual(days, {"2024-05-10": "On 10 May they climbed the hill [#%d]. It was "
                                              "sunny." % self.ids["lake"]})
        self.assertEqual(problems, [])
        # An unnamed person by People's label (from the owner's notes, say): left out.
        days, problems = trip_stories.parse_reply(story_reply([
            ("2024-05-10", "The lake glowed [1]. Person 3 was also seen.")]), facts,
            ["2024-05-10"])
        self.assertEqual(days, {"2024-05-10": "The lake glowed [#%d]." % self.ids["lake"]})
        self.assertEqual(len(problems), 1, problems)


class StoreTest(StoryData, unittest.TestCase):

    def test_kept_found_and_out_of_date(self):
        work = self.work()
        trip = self.trip(work)
        facts = work.facts_for(trip)
        self.assertEqual(work.state(trip), ("missing", []))
        days = [{"date": "2024-05-10", "text": "At the lake [#%d]." % self.ids["lake"]}]
        trip_stories.save(self.conn, work.built, trip, days, facts.basis, {"model": "m"})
        work = self.work()
        story = work.story(self.trip(work))
        self.assertEqual((story["days"], story["edited"], story["hidden"]), (days, False, False))
        self.assertEqual(work.state(self.trip(work)), ("current", []))
        # The trip changes much: two more days of pictures.
        for day in (13, 14):
            add(self.conn, LAKES, "%d/more.jpg" % day, "2024-05-%dT10:00:00" % day,
                position=LAKE, place=LAKE_PLACE)
        work = self.work()
        state, reasons = work.state(self.trip(work))
        self.assertEqual(state, "stale")
        self.assertIn("its days changed (2 days added)", reasons)
        # A picture it links is hidden.
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.ids["lake"],))
        self.conn.commit()
        work = self.work()
        self.assertIn("a picture it links is hidden or gone", work.state(self.trip(work))[1])

    def test_an_away_trip_keeps_its_story_when_its_first_day_moves(self):
        for day in (3, 4):
            for number in range(6):
                add(self.conn, PHONE, "mar/%d-%d.jpg" % (day, number),
                    "2024-03-%02dT1%d:00:00" % (day, number), position=LAKE,
                    place=LAKE_PLACE)
        work = self.work()
        trip = work.built.get("away-20240303")
        trip_stories.save(self.conn, work.built, trip, [{"date": "2024-03-03", "text": "Hi."}],
                          work.basis(trip), {})
        add(self.conn, PHONE, "mar/early.jpg", "2024-03-02T10:00:00", position=LAKE,
            place=LAKE_PLACE)
        work = self.work()
        moved = work.built.get("away-20240302")
        self.assertEqual(work.story(moved)["days"], [{"date": "2024-03-03", "text": "Hi."}])
        # Written again, it is kept by its new key (the old row goes).
        trip_stories.save(self.conn, work.built, moved, [], work.basis(moved), {})
        self.assertEqual(sorted(trip_stories.stored(self.conn)), ["away-20240302"])

    def test_the_owners_changes(self):
        library_ids = [PHONE, LAKES]
        view = trip_stories.change_story(self.conn, self.config, library_ids, LAKES, days=[
            {"date": "2024-05-11", "text": "  My own words [#%d] [#%d] [#999999].  "
                                           % (self.ids["peak"], self.ids["hidden"])},
            {"date": "2024-05-10", "text": ""}])
        # The owner's links stay when they are the trip's (a hidden one too: the visitors
        # do not see it), others go; an empty day has no paragraph.
        self.assertEqual(view["days"], [{"date": "2024-05-11", "text": "My own words [#%d] "
                                         "[#%d]." % (self.ids["peak"], self.ids["hidden"])}])
        self.assertTrue(view["edited"])
        for wrong in ([{"date": "2024-06-01", "text": "x"}], "text", [{"text": "x"}]):
            with self.assertRaises(trip_stories.StoryError):
                trip_stories.change_story(self.conn, self.config, library_ids, LAKES, days=wrong)
        view = trip_stories.change_story(self.conn, self.config, library_ids, LAKES, hidden=True)
        self.assertTrue(view["hidden"])
        self.assertEqual(trip_stories.story_view(self.conn, self.config, library_ids, LAKES),
                         {"trip": LAKES, "days": []})
        view = trip_stories.change_story(self.conn, self.config, library_ids, LAKES,
                                         forget_it=True)
        self.assertEqual(view["state"], "missing")
        with self.assertRaises(trips.TripError):
            trip_stories.change_story(self.conn, self.config, library_ids, "away-19990101",
                                      hidden=True)

    def test_what_visitors_see(self):
        self.add_people()
        work = self.work()
        trip = self.trip(work)
        text = ("The lake glowed [#%d]. Anna was there. Something else [#%d]. Cato swam."
                % (self.ids["lake"], self.ids["hidden"]))
        trip_stories.save(self.conn, work.built, trip, [{"date": "2024-05-10", "text": text}],
                          work.basis(trip), {"model": "m"})
        library_ids = [PHONE, LAKES]
        admin = trip_stories.story_view(self.conn, self.config, library_ids, LAKES, admin=True)
        self.assertEqual(admin["days"][0]["text"], text)
        self.assertEqual(admin["state"], "stale")
        visitor = trip_stories.story_view(self.conn, self.config, library_ids, LAKES)
        self.assertEqual(visitor, {"trip": LAKES, "days": [
            {"date": "2024-05-10", "text": "The lake glowed [#%d]." % self.ids["lake"]}]})
        # People shown to visitors: the names, but never a hidden person's (Cato).
        self.write_config(visitors=True)
        visitor = trip_stories.story_view(self.conn, self.config, library_ids, LAKES)
        self.assertEqual(visitor["days"][0]["text"], "The lake glowed [#%d]. Anna was there."
                         % self.ids["lake"])
        trips.set_trip(self.conn, self.config, library_ids, LAKES, hidden=True)
        with self.assertRaises(trips.TripError):
            trip_stories.story_view(self.conn, self.config, library_ids, LAKES)

    def test_names_changed_on_the_people_page_since(self):
        # People shown to visitors: a story's names show while they are People's names.
        self.add_people()
        self.write_config(visitors=True)
        work = self.work()
        trip = self.trip(work)
        text = ("The lake glowed [#%d]. Anna was there. Person 2 waved."
                % self.ids["lake"])
        trip_stories.save(self.conn, work.built, trip, [{"date": "2024-05-10", "text": text}],
                          work.basis(trip), {"model": "m"})
        library_ids = [PHONE, LAKES]

        def seen():
            return trip_stories.story_view(self.conn, self.config, library_ids,
                                           LAKES)["days"][0]["text"]
        # (Never "Person 2": an unnamed person.)
        self.assertEqual(seen(), "The lake glowed [#%d]. Anna was there." % self.ids["lake"])
        only_lake = "The lake glowed [#%d]." % self.ids["lake"]
        for change in ("UPDATE people SET name = 'Annabel' WHERE name = 'Anna'",
                       "UPDATE people SET name = '' WHERE name = 'Annabel'"):
            store = people.connect()
            try:
                store.execute(change)
                store.commit()
            finally:
                store.close()
            # Renamed, then left without a name: the story's "Anna" is no one now.
            self.assertEqual(seen(), only_lake, change)
        # Deleted with all the face data (Delete face data).
        self.assertEqual(people.delete_all()["error"], "")
        self.assertEqual(seen(), only_lake)
        # The admin's story is as kept.
        self.assertEqual(trip_stories.story_view(self.conn, self.config, library_ids, LAKES,
                                                 admin=True)["days"][0]["text"], text)

    def test_no_story_of_a_trip_visitors_are_not_shown(self):
        # An away trip found by the owner's hidden pictures (2 visible, 10 hidden): it is
        # no trip to visitors (trips.view), so its story is not theirs either.
        for number in range(12):
            add(self.conn, PHONE, "mar/%d.jpg" % number, "2024-03-03T1%d:00:00" % (number % 10),
                position=LAKE, place=LAKE_PLACE, hidden=1 if number >= 2 else 0)
        library_ids = [PHONE, LAKES]
        work = self.work()
        trip = work.built.get("away-20240303")
        trip_stories.save(self.conn, work.built, trip, [{"date": "2024-03-03",
                                                         "text": "A day at the lake."}],
                          work.basis(trip), {"model": "m"})
        shown = [item["id"] for item in trips.view(self.conn, self.config, library_ids)["trips"]]
        self.assertEqual(shown, [LAKES])
        with self.assertRaises(trips.TripError) as raised:
            trip_stories.story_view(self.conn, self.config, library_ids, "away-20240303")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(trip_stories.story_view(self.conn, self.config, library_ids,
                                                 "away-20240303", admin=True)["days"],
                         [{"date": "2024-03-03", "text": "A day at the lake."}])
        # Trips turned off: no story for visitors at all.
        self.write_config(trips={"enabled": False, "home": "%s, %s" % HOME})
        with self.assertRaises(trips.TripError):
            trip_stories.story_view(self.conn, self.config, library_ids, LAKES)


class TextOnlyRequestTest(unittest.TestCase):

    def test_no_picture_is_sent(self):
        with fake_llm.FakeLLM(reply='{"days": []}') as server:
            claude = {"kind": "claude", "base_url": server.root_url, "model": "m"}
            # Not a real key: a made-up value used only by this test.
            claude["api_key"] = "not-a-real-key"
            for endpoint in ({"kind": "openai", "base_url": server.base_url, "model": "m"},
                             claude,
                             {"kind": "ollama", "base_url": server.root_url, "model": "m"}):
                reply = llm.describe(endpoint, "system words", "user words", None, None)
                self.assertEqual(reply["text"], '{"days": []}')
            for entry in server.describe_requests():
                self.assertEqual(entry["image"], b"")
                self.assertEqual(entry["prompt"], "user words")
                self.assertNotIn("image", json.dumps(entry["payload"]))
            self.assertEqual([entry["parts"] for entry in server.describe_requests()],
                             [["text"], ["text"], ["text"]])


class JobTest(StoryData, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.add_people()

    def use_endpoint(self, **endpoint):
        settings = {"name": "big", "preset": "llamacpp", "base_url": self.server.base_url,
                    "model": "big-model"}
        settings.update(endpoint)
        libraries = [support.library("Phone", self.folders[PHONE], id=PHONE,
                                     refine={"endpoint": settings["name"]}),
                     support.library("Lakes", self.folders[LAKES], id=LAKES,
                                     refine={"endpoint": settings["name"]})]
        self.base.write_config(libraries, endpoints=[
            {"name": "small", "preset": "llamacpp", "base_url": "http://127.0.0.1:9/v1",
             "model": "small-model"}, settings], default_endpoint="small",
            trips={"home": "%s, %s" % HOME}, people={"enabled": True})
        self.config = common.load_config()

    def reply_for_trip(self, mention="Anna was there."):
        return story_reply([
            ("2024-05-10", "The lake glowed at sunset [1]. %s" % mention),
            ("2024-05-11", "Snow lay on the peak [3]."),
            ("2024-05-12", "A red boat waited [4].")])

    def run_job(self, **params):
        ctx = jobs.JobContext("trip_stories")
        summary = jobs.RUNNERS["trip_stories"](ctx, self.conn, self.config,
                                               dict({"library_id": None}, **params))
        return summary, [entry["message"] for entry in ctx.snapshot(500)["log_tail"]]

    def test_a_story_written_and_kept(self):
        self.use_endpoint()
        self.server.reply = self.reply_for_trip()
        summary, log = self.run_job()
        self.assertEqual((summary["written"], summary["failed"], summary["error"]), (1, 0, ""),
                         log)
        requests = self.server.describe_requests()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        # Text only, to the library's refine endpoint, with room for the answer.
        self.assertEqual(request["parts"], ["text"])
        self.assertEqual(request["payload"]["model"], "big-model")
        self.assertEqual(request["payload"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(request["payload"]["max_tokens"], trip_stories.MAX_ANSWER_TOKENS)
        self.assertEqual(request["system"], trip_stories.SYSTEM_PROMPT)
        self.assertIn("Write the paragraphs of these days", request["prompt"])
        for secret in ("Hometown", "Hidden thing", "Secret bank card", "Cato", LAKES):
            self.assertNotIn(secret, json.dumps(request["payload"]))
        # (A model on this computer gets the names of the people.)
        self.assertIn("With Anna.", request["prompt"])
        self.assertTrue(any("Writing 1 story with big (the refine endpoint of Lakes; model "
                            "big-model, on this computer)" in line for line in log), log)
        story = trip_stories.story_view(self.conn, self.config, [PHONE, LAKES], LAKES,
                                        admin=True)
        self.assertEqual(story["days"], [
            {"date": "2024-05-10", "text": "The lake glowed at sunset [#%d]. Anna was there."
                                           % self.ids["lake"]},
            {"date": "2024-05-11", "text": "Snow lay on the peak [#%d]." % self.ids["peak"]},
            {"date": "2024-05-12", "text": "A red boat waited [#%d]." % self.ids["boat"]}])
        self.assertEqual((story["state"], story["model"], story["endpoint"], story["local"]),
                         ("current", "big-model", "big", True))
        # Again: up to date, nothing asked.
        summary, _log = self.run_job()
        self.assertEqual((summary["written"], summary["current"]), (0, 1))
        self.assertEqual(len(self.server.describe_requests()), 1)
        # With redo: written again.
        summary, _log = self.run_job(redo=True)
        self.assertEqual(summary["written"], 1)
        # Edited by the owner: kept, even out of date, unless asked for this trip.
        trip_stories.change_story(self.conn, self.config, [PHONE, LAKES], LAKES,
                                  days=[{"date": "2024-05-10", "text": "Mine."}])
        summary, log = self.run_job(redo=True)
        self.assertEqual((summary["written"], summary["edited"]), (0, 1))
        summary, log = self.run_job(redo=True, trip=LAKES)
        self.assertEqual(summary["written"], 1)
        self.assertFalse(trip_stories.story_view(self.conn, self.config, [PHONE, LAKES],
                                                 LAKES, admin=True)["edited"])

    def test_an_edit_made_while_the_job_runs_is_kept(self):
        self.use_endpoint()
        self.server.reply = self.reply_for_trip()
        real = trip_stories.write_trip

        def owner_edits_meanwhile(*args, **kwargs):
            result = real(*args, **kwargs)
            conn = db.connect()
            try:
                trip_stories.change_story(conn, self.config, [PHONE, LAKES], LAKES,
                                          days=[{"date": "2024-05-10", "text": "My words."}])
            finally:
                conn.close()
            return result
        with mock.patch.object(trip_stories, "write_trip", owner_edits_meanwhile):
            summary, log = self.run_job(trip=LAKES)
        self.assertEqual((summary["written"], summary["edited"]), (0, 1), log)
        self.assertTrue(any("you edited the story while the job ran" in line for line in log))
        self.assertGreater(summary["tokens_in"], 0)
        story = trip_stories.story_view(self.conn, self.config, [PHONE, LAKES], LAKES,
                                        admin=True)
        self.assertEqual((story["days"], story["edited"]),
                         ([{"date": "2024-05-10", "text": "My words."}], True))
        # Asked to write it again (this trip, redo): the edit made before is replaced.
        summary, log = self.run_job(trip=LAKES, redo=True)
        self.assertEqual(summary["written"], 1, log)

    def test_a_reply_that_cannot_be_used_twice(self):
        # One day a request: the second day's answers cannot be read; the other days stay,
        # and every request counts.
        self.use_endpoint()
        self.server.queue(story_reply([("2024-05-10", "The lake [1].")]),
                          "no JSON here", "nor here",
                          story_reply([("2024-05-12", "A boat [4].")]))
        with mock.patch.object(trip_stories, "MAX_DAYS_PER_REQUEST", 1):
            summary, log = self.run_job(trip=LAKES)
        self.assertEqual((summary["written"], summary["failed"], summary["requests"]),
                         (1, 0, 4), log)
        story = trip_stories.story_view(self.conn, self.config, [PHONE, LAKES], LAKES,
                                        admin=True)
        self.assertEqual([entry["date"] for entry in story["days"]],
                         ["2024-05-10", "2024-05-12"])
        self.assertTrue(any("no paragraph for 2024-05-11" in line for line in log), log)
        # Nothing usable at all: failed, the requests counted still.
        self.server.queue("no JSON", "none", "at", "all", "here", "either")
        with mock.patch.object(trip_stories, "MAX_DAYS_PER_REQUEST", 1):
            summary, log = self.run_job(trip=LAKES, redo=True)
        self.assertEqual((summary["written"], summary["failed"], summary["requests"]),
                         (0, 1, 6), log)
        self.assertTrue(summary["error"])

    def test_some_stories_failed_others_were_written(self):
        self.use_endpoint()
        for day in (3, 4):
            for number in range(6):
                add(self.conn, PHONE, "mar/%d-%d.jpg" % (day, number),
                    "2024-03-%02dT1%d:00:00" % (day, number), position=LAKE,
                    place=LAKE_PLACE)
        self.server.reply = self.reply_for_trip()
        real = trip_stories.endpoint_for

        def none_for_the_away_trip(config, trip):
            if trip.kind == "away":
                return None, "No endpoint here."
            return real(config, trip)
        with mock.patch.object(trip_stories, "endpoint_for", none_for_the_away_trip):
            summary, log = self.run_job()
        # The job is done (not failed): its summary counts the one that failed.
        self.assertEqual((summary["written"], summary["failed"], summary["error"]),
                         (1, 1, ""), log)
        self.assertTrue(any("No endpoint here." in line for line in log), log)
        with mock.patch.object(trip_stories, "endpoint_for", none_for_the_away_trip):
            summary, log = self.run_job(trip="away-20240303")
        self.assertEqual(summary["error"], "No endpoint here.")

    def test_a_name_not_in_the_facts_and_missing_days(self):
        self.use_endpoint()
        # The first answer names a hidden person and leaves out a day; the second gives it.
        self.server.queue(story_reply([
            ("2024-05-10", "The lake glowed [1]. Cato swam."),
            ("2024-05-12", "A red boat [4].")]),
            story_reply([("2024-05-11", "Snow [3].")]))
        summary, log = self.run_job()
        self.assertEqual(summary["written"], 1, log)
        self.assertEqual(len(self.server.describe_requests()), 2)
        self.assertIn("Write the paragraphs of these days: 2024-05-11.",
                      self.server.describe_requests()[1]["prompt"])
        story = trip_stories.story_view(self.conn, self.config, [PHONE, LAKES], LAKES,
                                        admin=True)
        self.assertEqual([entry["text"] for entry in story["days"]],
                         ["The lake glowed [#%d]." % self.ids["lake"],
                          "Snow [#%d]." % self.ids["peak"],
                          "A red boat [#%d]." % self.ids["boat"]])

    def test_a_cloud_endpoint_gets_text_and_an_estimate(self):
        # Not a real key: a made-up value used only by this test.
        self.use_endpoint(preset="openai", base_url=self.server.base_url, api_key="sk-fake-key",
                          price_in=2.0, price_out=10.0)
        self.server.reply = self.reply_for_trip()
        plan = trip_stories.plan(self.conn, self.config)
        self.assertEqual([entry["id"] for entry in plan["trips"]], [LAKES])
        endpoint = plan["endpoints"][0]
        self.assertEqual((endpoint["name"], endpoint["local"], endpoint["trips"],
                          endpoint["requests"]), ("big", False, 1, 1))
        self.assertGreater(endpoint["tokens_in"], 300)
        self.assertAlmostEqual(plan["cost"], (endpoint["tokens_in"] * 2.0
                                              + endpoint["tokens_out"] * 10.0) / 1e6)
        summary, log = self.run_job()
        self.assertEqual(summary["written"], 1, log)
        self.assertTrue(any("sent to 127.0.0.1" in line and "about $" in line for line in log),
                        log)
        payload = json.dumps(self.server.describe_requests()[0]["payload"])
        self.assertNotIn("Hidden thing", payload)
        self.assertNotIn("image_url", payload)
        # No name of a person goes to a cloud endpoint: the story names nobody (the
        # model's "Anna was there." is no name the facts gave), and is up to date.
        self.assertNotIn("Anna", payload)
        self.assertTrue(any("without the names of people" in line for line in log), log)
        story = trip_stories.story_view(self.conn, self.config, [PHONE, LAKES], LAKES,
                                        admin=True)
        self.assertEqual(story["days"][0]["text"],
                         "The lake glowed at sunset [#%d]." % self.ids["lake"])
        self.assertEqual(story["state"], "current")
        # Without a key nothing is sent.
        self.use_endpoint(preset="openai", base_url=self.server.base_url, api_key="")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            summary, log = self.run_job(redo=True)
        self.assertIn("no API key is set", summary["error"])
        self.assertEqual(len(self.server.describe_requests()), 1)

    def test_the_model_servers_are_leased(self):
        self.use_endpoint()
        self.server.reply = self.reply_for_trip()
        leased = []

        class Lease:
            def __init__(self, ctx, config, names, manager=None):
                leased.append(list(names))

            def acquire(self):
                return {}

            def release(self):
                leased.append("released")
        import local_server
        with mock.patch.object(local_server, "lease_for", Lease):
            summary, _log = self.run_job()
        self.assertEqual(summary["written"], 1)
        self.assertEqual(leased, [["big"], "released"])
        # A server that cannot be used (its daily stop time): nothing is sent.

        class Closed(Lease):
            def acquire(self):
                return {"big": "inside its stop time"}
        with mock.patch.object(local_server, "lease_for", Closed):
            summary, _log = self.run_job(redo=True)
        self.assertEqual(summary["written"], 0)
        self.assertIn("inside its stop time", summary["error"])
        self.assertEqual(len(self.server.describe_requests()), 1)

    def test_stopped_and_resumed(self):
        self.use_endpoint()
        for day in (3, 4):
            for number in range(6):
                add(self.conn, PHONE, "mar/%d-%d.jpg" % (day, number),
                    "2024-03-%02dT1%d:00:00" % (day, number), position=LAKE,
                    place=LAKE_PLACE)
        self.server.reply = story_reply([("2024-03-03", "Away [1]."), ("2024-03-04", "More."),
                                         ("2024-05-10", "Lake [1]."),
                                         ("2024-05-11", "Peak."), ("2024-05-12", "Boat.")])
        ctx = jobs.JobContext("trip_stories")
        real = trip_stories._keep

        def keep_then_stop(*args, **kwargs):
            real(*args, **kwargs)
            ctx.interrupt("Stopping for the model server's daily stop time.")
        with mock.patch.object(trip_stories, "_keep", keep_then_stop), \
                mock.patch.object(trip_stories, "MAX_WORKERS", 1):
            summary = trip_stories.write_stories(ctx, self.conn, self.config)
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["written"], 1)
        summary, _log = self.run_job()
        self.assertEqual((summary["written"], summary["current"]), (1, 1))

    def test_nothing_to_tell(self):
        self.use_endpoint()
        info_rows = self.conn.execute("SELECT id, info FROM images WHERE library_id = ?",
                                      (LAKES,)).fetchall()
        for row in info_rows:
            value = json.loads(row["info"])
            value["private_info"] = True
            self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                              (json.dumps(value), row["id"]))
        self.conn.commit()
        summary, log = self.run_job(trip=LAKES)
        self.assertEqual((summary["written"], summary["empty"], summary["error"]), (0, 1, ""))
        self.assertTrue(any("nothing to write about" in line for line in log), log)
        self.assertEqual(self.server.describe_requests(), [])

    def test_unknown_trip_and_no_endpoint(self):
        self.use_endpoint()
        with self.assertRaises(jobs.JobError):
            self.run_job(trip="away-19990101")
        self.base.write_config([support.library("Lakes", self.folders[LAKES], id=LAKES)],
                               trips={"home": "%s, %s" % HOME})
        self.config = common.load_config()
        summary, _log = self.run_job()
        self.assertIn("No endpoint can write the story", summary["error"])


class CommandLineTest(StoryData, unittest.TestCase):

    def test_list_and_stories(self):
        server = fake_llm.FakeLLM(reply=story_reply([("2024-05-10", "Lake [1].")]))
        server.start()
        self.addCleanup(server.stop)
        self.base.write_config(
            [support.library("Phone", self.folders[PHONE], id=PHONE),
             support.library("Lakes", self.folders[LAKES], id=LAKES)],
            endpoints=[{"name": "big", "preset": "llamacpp", "base_url": server.base_url,
                        "model": "big-model"}],
            trips={"home": "%s, %s" % HOME}, trip_stories={"endpoint": "big"})
        out = io.StringIO()
        self.assertEqual(trip_stories.main_cli(["--list"], out=out), 0)
        self.assertIn("lib_lakes009", out.getvalue())
        self.assertIn("Lakes (", out.getvalue())
        self.assertIn("story: missing", out.getvalue())
        out = io.StringIO()
        self.assertEqual(trip_stories.main_cli(["--stories", "--trip", LAKES], out=out), 0,
                         out.getvalue())
        self.assertIn("] Command line: python trips.py --stories --trip lib_lakes009",
                      out.getvalue())
        self.assertIn("Done: 1 story written", out.getvalue())
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                trip_stories.main_cli(["--redo"], out=out)


@unittest.skipUnless(imagegen is not None, "Pillow is not installed")
class ExportTest(StoryData, unittest.TestCase):
    """The story in a trip's export (data/trips.js)."""

    def setUp(self):
        super().setUp()
        self.add_people()
        # Pictures the export can read: a small JPEG for each row of the trip library.
        thumb = imagegen.jpeg_bytes(imagegen.textured_image((160, 120)))
        store = cache.PreviewStore()
        with cache.ThumbStore() as thumbs:
            for row in self.conn.execute("SELECT id, library_id, path FROM images"):
                folder = self.folders[row["library_id"]]
                path = os.path.join(folder, *row["path"].split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(thumb)
                thumbs.put(row["id"], thumb)
                store.write(row["library_id"], row["id"], thumb)
        self.conn.execute("UPDATE images SET has_preview = 1")
        self.conn.commit()
        work = self.work()
        trip = self.trip(work)
        self.text = ("The lake glowed [#%d]. Anna was there. In Hometown it rained [#%d]. "
                     "The peak [#%d]." % (self.ids["lake"], self.ids["dock"], self.ids["peak"]))
        trip_stories.save(self.conn, work.built, trip,
                          [{"date": "2024-05-10", "text": self.text},
                           {"date": "2024-05-12", "text": "A boat [#%d]." % self.ids["boat"]}],
                          work.basis(trip), {"model": "m"})

    def export(self, options):
        out = os.path.join(common.exports_dir(), "trip")
        summary = trip_export.export_trip(jobs.JobContext("test"), self.conn,
                                          common.load_config(), LAKES, out, options)
        with open(os.path.join(out, "data", "trips.js"), encoding="ascii") as handle:
            text = handle.read()
        data = json.loads(text[text.index("(") + 1:text.rindex(")")])
        return summary, data["trips"][0], text

    def test_only_with_the_option(self):
        summary, trip, text = self.export({"location": True})
        self.assertNotIn("story", json.dumps(trip))
        self.assertNotIn("story_days", summary)
        summary, trip, text = self.export({"story": True, "location": True})
        self.assertEqual(summary["story_days"], 2)
        stories = {day["date"]: day.get("story") for day in trip["days"]}
        xids = re.findall(r"\[#([a-z0-9]+)\]", json.dumps(stories))
        self.assertEqual(len(xids), 3)
        # No names without people_names, no town of home, links to export ids only.
        self.assertTrue(stories["2024-05-10"].startswith("The lake glowed [#"))
        self.assertTrue(stories["2024-05-10"].endswith("The peak [#%s]." % xids[1]))
        for secret in ("Anna", "Hometown", "[#%d]" % self.ids["lake"]):
            self.assertNotIn(secret, text)
        self.assertIsNone(stories["2024-05-11"])
        summary, trip, text = self.export({"story": True, "people_names": True,
                                           "location": True})
        self.assertIn("Anna was there.", text)
        # A hidden person is never named.
        trip_stories.change_story(self.conn, self.config, [PHONE, LAKES], LAKES, days=[
            {"date": "2024-05-10", "text": "Cato swam [#%d]. Anna sat [#%d]."
             % (self.ids["lake"], self.ids["dock"])}])
        summary, trip, text = self.export({"story": True, "people_names": True,
                                           "location": True})
        self.assertNotIn("Cato", text)
        self.assertIn("Anna sat [#", text)

    def test_names_only_of_the_people_it_publishes(self):
        options = {"story": True, "people_names": True, "location": True}
        # Anna's pictures of the trip are the lake and the peak: one published, she is.
        summary, trip, text = self.export(dict(options, exclude_ids=[self.ids["lake"]]))
        self.assertIn("Anna was there.", text)
        # Neither: the export does not name her (nor list her name), though the story
        # was written from them.
        summary, trip, text = self.export(dict(options, exclude_ids=[self.ids["lake"],
                                                                     self.ids["peak"]]))
        self.assertNotIn("Anna", text)
        # (Nothing else of that day could go: its links are left out, or at home.)
        stories = {day["date"]: day.get("story") for day in trip["days"]}
        self.assertIsNone(stories.get("2024-05-10"))
        self.assertTrue(stories["2024-05-12"].startswith("A boat [#"))
        # After Delete face data no name goes, whatever the story was written from.
        self.assertEqual(people.delete_all()["error"], "")
        summary, trip, text = self.export(options)
        self.assertNotIn("Anna", text)
        self.assertIn("The lake glowed [#", text)

    def test_not_without_the_location(self):
        # The story names the trip's places, which go only with the location option.
        trip_stories.change_story(self.conn, self.config, [PHONE, LAKES], LAKES, days=[
            {"date": "2024-05-10", "text": "They reached Lakeside [#%d]." % self.ids["lake"]},
            {"date": "2024-05-11", "text": "Then on to Peakville [#%d]." % self.ids["peak"]}])
        summary, trip, text = self.export({"story": True})
        self.assertEqual(summary["story_days"], 0)
        self.assertNotIn('"story"', text)
        for town in ("Lakeside", "Peakville"):
            self.assertNotIn(town, text)
        summary, trip, text = self.export({"story": True, "location": True})
        self.assertEqual(summary["story_days"], 2)

    def test_hidden_story_and_unpublished_pictures(self):
        summary, trip, text = self.export({"story": True, "location": True,
                                           "exclude_ids": [self.ids["lake"]]})
        stories = {day["date"]: day.get("story") for day in trip["days"]}
        self.assertTrue(stories["2024-05-10"].startswith("The peak [#"), stories)
        trip_stories.change_story(self.conn, self.config, [PHONE, LAKES], LAKES, hidden=True)
        summary, trip, text = self.export({"story": True, "location": True})
        self.assertEqual(summary["story_days"], 0)
        self.assertNotIn('"story"', text)

    def test_the_export_dialog_knows(self):
        found = trip_export.export_info(self.conn, self.config, LAKES)
        self.assertEqual(found["story"], {"available": True, "hidden": False, "days": 2})
        self.assertFalse(found["options"]["story"])
        trip_export.save_options(self.conn, self.config, LAKES, {"story": True})
        self.assertTrue(trip_export.export_info(self.conn, self.config, LAKES)["options"]["story"])
        with self.assertRaises(export.OptionsError):
            trip_export.trip_options({}, {"story": "yes"})


class RoutesTest(AdminFixture, unittest.TestCase):
    """/api/trips/<id>/story and the admin's routes."""

    def setUp(self):
        super().setUp()
        self.lakes = self.base.library_folder("2024-05-12 - Lakes")
        self.write_config(libraries=[support.library("Lakes", self.lakes, id=LAKES)],
                          trips={"home": "%s, %s" % HOME})
        self.lake = add(self.conn, LAKES, "10/lake.jpg", "2024-05-10T12:00:00", position=LAKE,
                        place=LAKE_PLACE, title="Sunset over the lake")
        self.hidden = add(self.conn, LAKES, "10/hidden.jpg", "2024-05-10T13:00:00",
                          position=LAKE, place=LAKE_PLACE, title="Hidden", hidden=1)

    def test_routes(self):
        found = self.ok(self.client.get("/api/trips/%s/story" % LAKES,
                                         headers={"X-Search-Images": "1"}))
        self.assertEqual((found["days"], found["state"]), ([], "missing"))
        text = "Sunset [#%d]. Something [#%d]." % (self.lake, self.hidden)
        answer = self.ok(self.post("/trips/%s/story" % LAKES,
                                   {"days": [{"date": "2024-05-10", "text": text}]}))
        self.assertEqual(answer["story"]["days"][0]["text"], text)
        self.assertTrue(answer["story"]["edited"])
        visitor = self.ok(self.client.get("/api/trips/%s/story" % LAKES,
                                          environ_base=REMOTE))
        self.assertEqual(visitor, {"trip": LAKES, "days": [
            {"date": "2024-05-10", "text": "Sunset [#%d]." % self.lake}]})
        self.ok(self.post("/trips/%s/story" % LAKES, {"hidden": True}))
        visitor = self.ok(self.client.get("/api/trips/%s/story" % LAKES,
                                          environ_base=REMOTE))
        self.assertEqual(visitor["days"], [])
        self.assertEqual(self.client.get("/api/trips/away-19990101/story").status_code, 404)
        self.assertEqual(self.post("/trips/%s/story" % LAKES, {}).status_code, 400)
        self.assertEqual(self.post("/trips/%s/story" % LAKES, {"days": [
            {"date": "1999-01-01", "text": "x"}]}).status_code, 400)
        # The plan of a job, and the job (no library; the trip's title for the list).
        plan = self.ok(self.get("/trips/stories/plan?trip=%s&redo=1" % LAKES))
        self.assertEqual([entry["id"] for entry in plan["trips"]], [LAKES])
        self.assertEqual(plan["endpoints"][0]["name"], "local")
        self.assertEqual(self.post("/jobs", {"kind": "trip_stories", "library_id": LAKES,
                                             "params": {}}).status_code, 400)
        answer = self.ok(self.post("/jobs", {"kind": "trip_stories", "library_id": None,
                                             "params": {"trip": LAKES, "redo": True}}), 202)
        self.assertEqual(answer["job"]["params"], {"trip": LAKES, "trip_title": "Lakes",
                                                   "redo": True})
        self.assertEqual(self.post("/jobs", {"kind": "trip_stories", "params": {
            "trip": "away-19990101"}}).status_code, 404)
        forgot = self.ok(self.post("/trips/%s/story" % LAKES, {"forget": True}))
        self.assertEqual(forgot["story"]["state"], "missing")


if __name__ == "__main__":
    unittest.main()
