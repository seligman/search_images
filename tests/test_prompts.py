"""Tests for prompts.py (prompt v6, schema, search text) and describe.parse_reply."""

import copy
import json
import sqlite3
import unittest

import support  # noqa: F401  (puts the repository on sys.path)

import fake_llm

import llm
import prompts

try:
    import describe
except (ImportError, SystemExit):       # Pillow is not installed
    describe = None

CORE_FIELDS = [
    "description", "title", "photo_type", "document_type", "text", "text_language",
    "setting", "scene", "time_of_day", "event", "people", "people_descriptors",
    "subjects", "animals", "food", "vehicles", "colors", "tags", "quality_flags",
    "sensitive", "private_info", "aesthetic", "sharpness", "exposure",
]
EXTENDED_ONLY = ["objects", "activities", "landmarks", "location_guess", "mood"]


def every_object(node):
    """Every object schema inside a schema (the schema itself included)."""
    found = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(every_object(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(every_object(item))
    return found


class SchemaTest(unittest.TestCase):

    def test_core_and_extended_fields(self):
        core = prompts.schema("core")
        self.assertEqual(list(core["properties"]), CORE_FIELDS)
        extended = prompts.schema("extended")
        self.assertEqual(sorted(extended["properties"]), sorted(CORE_FIELDS + EXTENDED_ONLY))
        # Unknown profiles fall back to core.
        self.assertEqual(prompts.schema("nonsense"), core)

    def test_strict_mode_rules(self):
        for profile in prompts.PROFILES:
            schema = prompts.schema(profile)
            for node in every_object(schema):
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))

    def test_bounds_and_enums(self):
        properties = prompts.schema("extended")["properties"]
        self.assertEqual(properties["title"]["maxLength"], 120)
        self.assertEqual(properties["description"]["maxLength"], 800)
        self.assertEqual(properties["scene"]["maxLength"], 40)
        # The transcription is only cut when a reply is tidied: llama.cpp
        # refuses grammar repetitions of 2000 or more.
        self.assertNotIn("maxLength", properties["text"])
        for name, bound in (("subjects", 10), ("tags", 15), ("people_descriptors", 6),
                            ("animals", 8), ("food", 8), ("vehicles", 8), ("colors", 5),
                            ("landmarks", 5), ("activities", 6), ("objects", 12)):
            self.assertEqual(properties[name]["maxItems"], bound, name)
        self.assertEqual(properties["tags"]["items"]["maxLength"], 60)
        self.assertEqual(properties["quality_flags"]["items"]["enum"],
                         ["blurry", "dark", "overexposed", "accidental", "screen_photo"])
        self.assertEqual(properties["people"]["enum"],
                         ["none", "1", "2", "3-5", "6-10", "crowd"])
        self.assertIn("card_or_id", properties["document_type"]["enum"])
        for name in ("aesthetic", "sharpness", "exposure"):
            self.assertEqual(properties[name], {"type": "integer", "minimum": 1, "maximum": 5})
        self.assertEqual(properties["private_info"], {"type": "boolean"})
        count = properties["objects"]["items"]["properties"]["count"]
        self.assertEqual((count["minimum"], count["maximum"]), (1, 99))

    def test_every_bound_fits_a_llama_cpp_grammar(self):
        # llama.cpp turns maxLength/maxItems into grammar repetitions and
        # rejects every request whose schema has one of 2000 or more.
        def bounds(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("maxLength", "maxItems", "minLength", "minItems"):
                        yield key, value
                    else:
                        for item in bounds(value):
                            yield item
            elif isinstance(node, list):
                for value in node:
                    for item in bounds(value):
                        yield item
        for profile in prompts.PROFILES:
            found = list(bounds(prompts.schema(profile)))
            self.assertTrue(found, profile)
            for key, value in found:
                self.assertLessEqual(value, prompts.GRAMMAR_REPEAT_LIMIT, (profile, key))

    def test_360_photos_get_a_prompt_note_and_search_words(self):
        note = prompts.projection_note("dual_fisheye")
        self.assertIn("360-degree", note)
        self.assertEqual(prompts.projection_note(""), "")
        self.assertEqual(prompts.projection_note("?"), "")
        self.assertIn(note, prompts.user_text(None, projection="equirectangular"))
        self.assertNotIn("360", prompts.user_text(None))
        info = {"title": "Cabin deck", "description": "A deck."}
        self.assertIn("360 panorama", prompts.search_terms(info, projection="dual_fisheye"))
        self.assertNotIn("360", prompts.search_terms(info))

    def test_each_call_returns_a_fresh_copy(self):
        schema = prompts.schema()
        schema["properties"]["title"]["maxLength"] = 1
        self.assertEqual(prompts.schema()["properties"]["title"]["maxLength"], 120)

    def test_cloud_sanitizing_keeps_a_valid_schema(self):
        claude = llm.sanitize_schema(prompts.schema("extended"), {"kind": "claude"})
        text = json.dumps(claude)
        for keyword in ("minimum", "maximum", "maxLength", "maxItems"):
            self.assertNotIn('"%s"' % keyword, text)
        self.assertEqual(list(claude["properties"]),
                         list(prompts.schema("extended")["properties"]))
        openai = llm.sanitize_schema(prompts.schema(), {"preset": "openai"})
        self.assertNotIn("maxLength", json.dumps(openai))
        self.assertEqual(openai["properties"]["tags"]["maxItems"], 15)

    def test_prompt_hash(self):
        core, extended = prompts.prompt_hash("core"), prompts.prompt_hash("extended")
        self.assertRegex(core, r"^[0-9a-f]{12}$")
        self.assertNotEqual(core, extended)
        self.assertEqual(core, prompts.prompt_hash("core"))


class PromptTest(unittest.TestCase):

    def test_system_prompt_essentials(self):
        text = prompts.SYSTEM_PROMPT
        self.assertEqual(prompts.system_prompt("core"), text)
        lowered = text.lower()
        for phrase in ("searching", "only what is visible", "never identify",
                       "older man in a red jacket", "3 to 8", "transcribed exactly",
                       "never invent", "singular", "synonyms", "unknown", "private_info",
                       "screenshot", "1 (poor) to 5 (excellent)",
                       # Visible names are transcribed, never attached to people.
                       "names written in the picture", "never say that a pictured person",
                       "people descriptors may be a little longer"):
            self.assertIn(phrase, lowered)
        # Every core field is explained, in schema order.
        scores = ("aesthetic", "sharpness", "exposure")
        positions = [lowered.index("- %s:" % name) if name not in scores
                     else lowered.index("- aesthetic, sharpness, exposure:")
                     for name in CORE_FIELDS]
        self.assertEqual(positions, sorted(positions))
        for name in EXTENDED_ONLY:
            self.assertNotIn("- %s:" % name, lowered)
            self.assertIn("- %s:" % name, prompts.system_prompt("extended"))

    def test_user_text(self):
        self.assertEqual(prompts.user_text(None), "Describe this image.")
        self.assertEqual(prompts.user_text({"context_notes": "  "}), "Describe this image.")
        text = prompts.user_text({"context_notes": "Photos from a sailing club."})
        self.assertTrue(text.startswith("Describe this image."))
        self.assertIn("Photos from a sailing club.", text)
        long_notes = prompts.user_text({"context_notes": "x" * 5000})
        self.assertLess(len(long_notes), prompts.NOTES_LENGTH + 200)
        self.assertIn("May 2019", prompts.user_text(None, "2019-05-14T10:00:00"))
        self.assertEqual(prompts.user_text(None, "garbage"), "Describe this image.")

    def test_videos_are_described_as_videos(self):
        # The model is told about the video, never about the sheet of frames
        # it sees ("four sequential video frames ..."), and that a camera's
        # video is a photo even with a time or speed printed on it.
        for frames in (4, 1):
            note = prompts.video_note(frames, 24.4)
            self.assertIn("a video of 24 seconds", note)
            self.assertIn("never mention frames, panels, a grid or a collage", note)
            self.assertIn("photo for a video filmed with a camera", note)
            self.assertIn("screenshot only for a recording of a screen", note)
            self.assertNotIn("not the grid of frames", note)
            self.assertTrue(note.isascii())
        self.assertIn("This picture holds 4 frames, in order", prompts.video_note(4, 24.4))
        self.assertIn("what happens in it", prompts.video_note(4, None))
        self.assertIn("a video.", prompts.video_note(1))
        text = prompts.user_text(None, video=(4, 30.0))
        self.assertTrue(text.startswith("Describe this image.\n\n" + prompts.video_note(4, 30)))
        # A 360 video: the panorama note says it is one, in the same words.
        text = prompts.user_text(None, projection="dual_fisheye", video=(4, 30.0))
        note = prompts.projection_note("dual_fisheye", video=True)
        self.assertTrue(text.endswith(note))
        self.assertIn("It is a 360-degree video", note)
        self.assertIn("what happens in it", note)
        self.assertNotIn("360-degree photo", text)

    def test_versions_and_what_is_out_of_date(self):
        # Version 6 changed only what videos are told: the pictures described
        # by version 5 (and drives) are up to date, videos are not.
        self.assertEqual(prompts.PROMPT_VER, 6)
        self.assertEqual(prompts.CURRENT_SINCE, {"image": 5, "video": 6, "drive": 5})
        self.assertFalse(prompts.is_stale(5, "image"))
        self.assertFalse(prompts.is_stale(5))
        self.assertTrue(prompts.is_stale(4, "image"))
        self.assertTrue(prompts.is_stale(5, "video"))
        self.assertFalse(prompts.is_stale(6, "video"))
        self.assertFalse(prompts.is_stale(5, "drive"))
        self.assertTrue(prompts.is_stale(None, "image"))
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE images (media TEXT, prompt_ver INTEGER)")
        conn.executemany("INSERT INTO images VALUES (?, ?)",
                         [("image", 4), ("image", 5), ("image", 6), ("video", 4),
                          ("video", 5), ("video", 6)])
        stale = conn.execute("SELECT media, prompt_ver FROM images WHERE " +
                             prompts.STALE_SQL + " ORDER BY media, prompt_ver").fetchall()
        conn.close()
        self.assertEqual(stale, [("image", 4), ("video", 4), ("video", 5)])

    def test_no_personal_or_provider_specific_text(self):
        for profile in prompts.PROFILES:
            text = prompts.system_prompt(profile)
            self.assertNotIn("\\", text)
            self.assertTrue(text.isascii())


class SearchTermsTest(unittest.TestCase):

    def test_version_5(self):
        info = describe_reply()
        info.update(title="Birthday cake on a kitchen table", scene="kitchen",
                    setting="indoor", time_of_day="sunrise_sunset", event="birthday",
                    photo_type="photo", document_type="card_or_id",
                    animals=["tabby cat"], food=["chocolate cake"], vehicles=["tricycle"],
                    people_descriptors=["toddler in a party hat"], text="HAPPY BIRTHDAY",
                    landmarks=["Old Town Hall"], activities=["blowing out candles"],
                    objects=[{"name": "candle", "count": 5}], location_guess="Somewhere")
        text = prompts.search_terms(info, "IMG_0001.JPG", "2019/Party Time/IMG_0001.JPG",
                                    "2019-05-14T14:23:01")
        self.assertEqual(text, text.lower())
        for word in ("birthday cake on a kitchen table", "kitchen", "indoor",
                     "sunrise sunset", "birthday", "photo", "card id", "tabby cat",
                     "chocolate cake", "tricycle", "toddler in a party hat", "happy birthday",
                     "old town hall", "blowing out candles", "candle", "img_0001.jpg",
                     "2019/party time/img_0001.jpg", "party time", "2019", "may",
                     "red", "test"):
            self.assertIn(word, text)
        self.assertNotIn("somewhere", text)      # the location guess is only a guess

    def test_unknown_and_none_values_are_left_out(self):
        info = describe_reply()
        info.update(setting="unknown", time_of_day="unknown", event="none",
                    document_type="none")
        text = prompts.search_terms(info)
        for word in ("unknown", "none"):
            self.assertNotIn(word, text)

    def test_version_4_and_missing_descriptions(self):
        text = prompts.search_terms(dict(fake_llm.V4_REPLY, _model_name="secret-model"),
                                    "beach.jpg", "holidays/beach.jpg")
        for word in ("red square", "square", "test", "blue", "beach.jpg", "holidays"):
            self.assertIn(word, text)
        self.assertNotIn("secret-model", text)
        self.assertNotIn("0.4", text)
        self.assertEqual(prompts.search_terms(None, "a.jpg", "a.jpg"), "a.jpg a.jpg")
        self.assertEqual(prompts.search_terms("not a dict"), "")
        self.assertEqual(prompts.search_terms({"tags": [1, None, "ok"]}), "ok")


def varied_text(sentences):
    """Ordinary prose of about eight words per sentence, without loops."""
    adjectives = ["small", "bright", "old", "quiet", "heavy", "green", "narrow", "wooden",
                  "tall", "soft"]
    nouns = ["dog", "lamp", "bridge", "garden", "engine", "letter", "candle", "ocean",
             "pencil", "market", "window", "river"]
    verbs = ["waits", "moves", "rests", "shines", "turns", "falls", "grows", "stays"]
    places = ["town", "hills", "fields", "harbor", "station", "square", "valley"]
    ways = ["slowly", "again", "alone", "softly"]
    return " ".join("The %s %s %s while %s near %s %s today." % (
        adjectives[i % 10], nouns[(i * 5) % 12], verbs[(i * 3) % 8], nouns[(i * 7 + 1) % 12],
        places[i % 7], ways[i % 4]) for i in range(sentences))


def describe_reply(**changes):
    reply = copy.deepcopy(fake_llm.DEFAULT_REPLY)
    reply.update(changes)
    return reply


@unittest.skipIf(describe is None, "Pillow is not installed")
class ParseReplyTest(unittest.TestCase):

    def parse(self, profile="core", **changes):
        return describe.parse_reply(json.dumps(describe_reply(**changes)), profile)

    def test_valid_reply(self):
        info = describe.parse_reply(json.dumps(fake_llm.DEFAULT_REPLY))
        self.assertEqual(info, fake_llm.DEFAULT_REPLY)
        self.assertEqual(list(info), CORE_FIELDS)
        extended = self.parse("extended", objects=[{"name": "chair", "count": 3}],
                              activities=["reading"], landmarks=["Eiffel Tower"],
                              location_guess="Paris, France", mood="calm")
        self.assertEqual(extended["objects"], [{"name": "chair", "count": 3}])
        self.assertEqual(extended["landmarks"], ["Eiffel Tower"])
        self.assertEqual(extended["mood"], "calm")

    def test_fences_and_extra_text(self):
        text = "Sure!\n```json\n%s\n```" % json.dumps(fake_llm.DEFAULT_REPLY)
        self.assertEqual(describe.parse_reply(text)["title"],
                         fake_llm.DEFAULT_REPLY["title"])

    def test_unusable_replies(self):
        for text in ("I cannot do that.", "[1, 2]", "", None,
                     json.dumps(describe_reply(description="   ")),
                     json.dumps({"title": "Only a title"})):
            with self.subTest(text=text):
                with self.assertRaises(describe.ContentError):
                    describe.parse_reply(text)

    def test_scores_are_clamped_integers(self):
        info = self.parse(aesthetic=4.6, sharpness=0, exposure="3/5")
        self.assertEqual((info["aesthetic"], info["sharpness"], info["exposure"]), (5, 1, 3))
        info = self.parse(aesthetic=9, sharpness="very", exposure=None)
        self.assertEqual((info["aesthetic"], info["sharpness"], info["exposure"]),
                         (5, None, None))
        self.assertIsNone(self.parse(aesthetic=True)["aesthetic"])

    def test_invalid_enums(self):
        info = self.parse(photo_type="hologram", document_type="invoice", setting="space",
                          time_of_day="evening", event="", people="several",
                          sensitive="maybe")
        self.assertEqual(info["photo_type"], "other")
        self.assertEqual(info["document_type"], "receipt")     # a known near-miss
        self.assertEqual(info["setting"], "unknown")
        self.assertEqual(info["time_of_day"], "unknown")
        self.assertEqual(info["event"], "none")
        self.assertEqual(info["people"], "3-5")
        self.assertEqual(info["sensitive"], "none")
        info = self.parse(photo_type="Screen Shot", document_type="Card or ID",
                          setting="Outdoors", time_of_day="Sunset", event="Vacation",
                          people=12)
        self.assertEqual((info["photo_type"], info["document_type"], info["setting"],
                          info["time_of_day"], info["event"], info["people"]),
                         ("screenshot", "card_or_id", "outdoor", "sunrise_sunset", "travel",
                          "crowd"))
        for value, expected in ((0, "none"), (1, "1"), ("2", "2"), ("4", "3-5"),
                                ("3–5", "3-5"), ("7", "6-10"), ("none", "none"),
                                ("lots", "crowd"), (None, "none")):
            self.assertEqual(self.parse(people=value)["people"], expected, value)
        self.assertEqual(self.parse("extended", mood="weird")["mood"], "neutral")
        self.assertEqual(self.parse(document_type="unknown")["document_type"], "none")

    def test_lists_are_trimmed_deduplicated_and_bounded(self):
        info = self.parse(tags=["Dog", "dog ", "#Beach", "", "  sunny  day ", 7] +
                          ["tag%d" % number for number in range(30)],
                          colors="Red, blue; GREEN", subjects=["Eiffel Tower", "eiffel tower"],
                          quality_flags=["Blurry", "motion blur", "nonsense", "dark", "dark"],
                          animals=None, food=[{"name": "Pizza"}], vehicles="x" * 100)
        self.assertEqual(info["tags"][:5], ["dog", "beach", "sunny day", "7", "tag0"])
        self.assertEqual(len(info["tags"]), 15)
        self.assertEqual(info["colors"], ["red", "blue", "green"])
        self.assertEqual(info["subjects"], ["Eiffel Tower"])
        self.assertEqual(info["quality_flags"], ["blurry", "dark"])
        self.assertEqual(info["animals"], [])
        self.assertEqual(info["food"], ["pizza"])
        self.assertEqual(len(info["vehicles"][0]), 60)

    def test_title_starts_with_a_capital(self):
        self.assertEqual(self.parse(title="lake at dawn")["title"], "Lake at dawn")

    def test_strings_are_cut_to_their_bounds(self):
        info = self.parse(title='"A very long title. ' + varied_text(4) + '."',
                          description=varied_text(30),
                          text="line one\r\nline two\n\n\n\nline three " + "z" * 3000,
                          scene="City Street.", text_language="English")
        self.assertLessEqual(len(info["title"]), 120)
        self.assertFalse(info["title"].startswith('"'))
        self.assertLessEqual(len(info["description"]), 800)
        self.assertTrue(info["text"].startswith("line one\nline two\n\nline three"))
        self.assertEqual(len(info["text"]), len("line one\nline two\n\nline three ") + 3000)
        self.assertEqual(info["scene"], "city street")
        self.assertEqual(info["text_language"], "en")
        for value, expected in (("de", "de"), ("pt-BR", "pt-br"), ("none", ""),
                                ("Klingon", ""), (5, "")):
            self.assertEqual(self.parse(text_language=value)["text_language"], expected)

    def test_missing_title_comes_from_the_description(self):
        info = self.parse(title="", description="A golden retriever catches a red frisbee "
                          "on a wide sandy beach at noon. Waves roll in.")
        # Eight words at most, not ending on a small word such as "on".
        self.assertEqual(info["title"], "A golden retriever catches a red frisbee")

    def test_types_are_coerced(self):
        info = self.parse(private_info="yes", text=["first line", "second line"],
                          description=12345)
        self.assertIs(info["private_info"], True)
        self.assertEqual(info["text"], "first line\nsecond line")
        self.assertEqual(info["description"], "12345")
        self.assertIs(self.parse(private_info="false")["private_info"], False)
        self.assertIs(self.parse(private_info=None)["private_info"], False)
        objects = self.parse("extended", objects=["3 chairs", {"name": "Table", "count": "200"},
                                                  {"name": "table", "count": 2},
                                                  {"count": 4}, 7])["objects"]
        self.assertEqual(objects, [{"name": "chairs", "count": 3},
                                   {"name": "table", "count": 99}])

    def test_repetition_guard(self):
        looping = "A dog " + "very " * 40 + "big."
        with self.assertRaises(describe.ContentError) as caught:
            self.parse(description=looping)
        self.assertIn("repeats", str(caught.exception))
        with self.assertRaises(describe.ContentError):
            self.parse(title="Sale " * 40)
        # The transcribed text is cut where it starts looping, not rejected.
        phrase = "Buy one get one free today only. " * 12
        self.assertEqual(self.parse(text=phrase)["text"], "Buy one get one free today only.")
        with self.assertRaises(describe.ContentError):
            self.parse(tags=["tag%d" % number for number in range(46)])
        with self.assertRaises(describe.ContentError):
            self.parse("extended", objects=[{"name": "x%d" % n, "count": 1} for n in range(37)])
        # Ordinary text, a long page with many "the", tables of numbers and
        # lists at three times their bound are fine.
        self.assertTrue(self.parse(text=varied_text(45))["text"])
        self.assertTrue(self.parse(text="0.00 " * 300)["text"])
        self.assertEqual(len(self.parse(tags=["t%d" % n for n in range(45)])["tags"]), 15)

    def test_long_transcriptions_that_repeat_are_kept(self):
        # Receipts, statements, file lists, checklists and songs repeat words
        # and lines legitimately; they are transcribed in full.
        items = ["MILK", "BREAD", "EGGS 12CT", "BANANAS", "COFFEE", "APPLES", "RICE",
                 "PASTA", "TOMATOES", "CHEESE", "YOGURT"]
        receipt = "\n".join(
            ["SUPERMARKET", "STORE 0042"]
            + ["%s %d.%02d F" % (items[n % len(items)], 1 + n % 7, n * 37 % 100)
               for n in range(34)]
            + ["SUBTOTAL 112.40", "TAX 4.12", "TOTAL 116.52"])
        statement = "\n".join("2024-01-%02d CARD PURCHASE %d.%02d USD" % (n + 1, 3 + n, n * 11 % 100)
                              for n in range(28))
        listing = "\n".join("IMG_%d.JPG 2.0 MB" % (1000 + n) for n in range(35))
        checklist = "\n".join("Task %d Yes" % n for n in range(1, 41))
        chorus = "Oh la la we sing along\nAll night long we sing along"
        lyrics = "\n".join(["Verse one is here", chorus, "Verse two is there", chorus,
                            "Bridge goes up", chorus, chorus])
        nine = "Start\n" + "\n".join(["Same line"] * (describe.LOOP_REPEATS - 1))
        for text in (receipt, statement, listing, checklist, lyrics, nine):
            with self.subTest(text=text[:20]):
                self.assertLessEqual(len(text), 2000)
                self.assertEqual(self.parse(text=text)["text"], text)

    def test_a_looping_end_of_the_transcription_is_cut(self):
        looped = "Total 5.00\n" * 40
        long_line = "This line is long and has more than twelve words in it, so there"
        cases = [
            ("Receipt\n" + looped, "Receipt\nTotal 5.00"),
            # The last copy was cut short by the length limit.
            ("Receipt\n" + looped + "Tot", "Receipt\nTotal 5.00"),
            ("Receipt\nTotal 5.00 " * 1 + "Total 5.00 " * 30 + "Total 5.", "Receipt\nTotal 5.00"),
            ("Start\n" + (long_line + "\n") * 12, "Start\n" + long_line),
            ("Sale ends today " + "the " * 50, "Sale ends today the"),
            ("ha " * 200, "ha"),
        ]
        for text, expected in cases:
            with self.subTest(text=text[:30]):
                self.assertEqual(self.parse(text=text)["text"], expected)


if __name__ == "__main__":
    unittest.main()
