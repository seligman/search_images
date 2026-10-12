"""Replies cut off at the length limit: llm.repair_json and how describing uses it."""

import json
import unittest

import support

import llm

if support.pillow_available():
    import fake_llm
    from test_describe import DescribeTestCase, reply
    import describe
else:
    DescribeTestCase = object

FULL = json.dumps({"description": "A receipt from a cafe.", "title": "Cafe receipt",
                   "photo_type": "document", "text": "Coffee 3.50\nTotal é \\ 3.50",
                   "tags": ["receipt", "cafe"], "aesthetic": 3})


class RepairJsonTest(unittest.TestCase):

    def test_whole_and_cut_off_replies(self):
        self.assertEqual(llm.repair_json(FULL), json.loads(FULL))
        self.assertEqual(llm.repair_json("Sure!\n```json\n" + FULL + "\n```"), json.loads(FULL))
        cut = FULL[:FULL.index("Total")]
        data = llm.repair_json(cut)
        self.assertEqual(data["title"], "Cafe receipt")
        self.assertEqual(data["text"], "Coffee 3.50\n")
        self.assertNotIn("tags", data)
        # Cut inside a key, after a colon, inside a word or a list.
        for marker, keys in (('"photo_t', ["description", "title"]),
                             ('"photo_type": ', ["description", "title"]),
                             ('"tags": ["receipt", "ca', ["description", "title",
                                                         "photo_type", "text", "tags"])):
            data = llm.repair_json(FULL[:FULL.index(marker) + len(marker)])
            self.assertEqual(list(data), keys, marker)
        self.assertEqual(llm.repair_json(FULL[:FULL.index('"ca') + 3])["tags"],
                         ["receipt", "ca"])
        # An unfinished escape is dropped; a finished one is kept.
        self.assertEqual(llm.repair_json('{"a": "b\\u00')["a"], "b")
        self.assertEqual(llm.repair_json('{"a": "b\\')["a"], "b")
        self.assertEqual(llm.repair_json('{"a": "b\\\\')["a"], "b\\")
        self.assertEqual(llm.repair_json('{"a": [1, 2, {"x": tr'), {"a": [1, 2]})
        for text in ("", "no json here", '{"descr', "[1, 2]", '{"a": 1]'):
            with self.assertRaises(ValueError):
                llm.repair_json(text)

    def test_a_cut_number_is_not_used_but_a_whole_word_is(self):
        # 12 cut after the 1 must not become 1.
        self.assertEqual(llm.repair_json('{"a": "x", "count": 1'), {"a": "x"})
        self.assertEqual(llm.repair_json('{"a": [1, 2'), {"a": [1]})
        self.assertEqual(llm.repair_json('{"a": "x", "count": -0.5e'), {"a": "x"})
        self.assertEqual(llm.repair_json('{"a": "x", "private_info": true'),
                         {"a": "x", "private_info": True})
        self.assertEqual(llm.repair_json('{"a": null'), {"a": None})
        self.assertEqual(llm.repair_json('{"a": [1, 2]'), {"a": [1, 2]})

    def test_half_a_surrogate_pair_is_dropped(self):
        # An emoji written as two \u escapes, cut between them (or in the second).
        backslash = chr(92)
        whole = '{"a": "x ' + backslash + "ud83d" + backslash + "ude00"
        self.assertEqual(llm.repair_json(whole)["a"], "x \U0001F600")
        for cut in (len(whole) - 6, len(whole) - 4, len(whole) - 1):
            self.assertEqual(llm.repair_json(whole[:cut])["a"], "x ", cut)
        # A real backslash before "ud83d" is not an escape.
        self.assertEqual(llm.repair_json('{"a": "x \\\\ud83d')["a"], "x \\ud83d")


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class TruncatedDescribeTest(DescribeTestCase, unittest.TestCase):

    def test_a_reply_cut_after_the_title_is_used_and_flagged(self):
        self.small_images(1)
        self.configure()
        self.index()
        text = reply(title="Shopping list", description="A handwritten shopping list.",
                     text="eggs\nmilk\n" * 50)
        self.server.queue(fake_llm.Reply(text[:text.index("milk") + 30], finish="length"))
        summary = self.describe()
        self.assertEqual((summary["described"], summary["failed"]), (1, 0))
        self.assertEqual(len(self.server.describe_requests()), 1)
        row = self.rows()["photo0.jpg"]
        info = json.loads(row["info"])
        self.assertEqual(info["title"], "Shopping list")
        self.assertTrue(info["text"].startswith("eggs\nmilk"))
        self.assertEqual(info["tags"], [])          # cut before the tags
        self.assertTrue(json.loads(row["provenance"])["truncated"])
        self.assertIn("cut off at the length limit", self.log_text())

    def test_a_reply_cut_before_the_title_is_asked_again_with_more_room(self):
        self.small_images(1)
        self.configure()
        self.index()
        self.server.queue(fake_llm.Reply(reply()[:40], finish="length"), reply())
        summary = self.describe()
        self.assertEqual(summary["described"], 1)
        first, second = self.server.describe_requests()
        self.assertEqual(first["payload"]["max_tokens"], 4000)
        self.assertEqual(second["payload"]["max_tokens"], 8000)
        provenance = json.loads(self.rows()["photo0.jpg"]["provenance"])
        self.assertNotIn("truncated", provenance)
        self.assertEqual(provenance["requests"], 2)

    def test_more_room_is_asked_for_only_once(self):
        self.small_images(1)
        self.configure(endpoint=self.endpoint(max_output_tokens=1000))
        self.index()
        self.server.reply = fake_llm.Reply(reply()[:40], finish="length")
        summary = self.describe()
        self.assertEqual(summary["described"], 0)
        tokens = [request["payload"]["max_tokens"] for request in self.server.describe_requests()]
        self.assertEqual(tokens, [1000, 2000, 2000])
        self.assertIn("cut off", self.log_text())

    def test_broken_json_is_asked_again_and_repaired_only_when_that_fails(self):
        self.small_images(2)
        self.configure()
        self.index()
        text = reply(title="A dog", description="A dog on a beach.")
        broken = text[:text.index('"tags"')]
        self.server.queue(broken, reply(title="A whole reply"))
        self.assertEqual(self.describe(limit=1)["described"], 1)
        self.assertEqual(len(self.server.describe_requests()), 2)
        described = [row for row in self.rows().values() if row["described"]]
        self.assertEqual(json.loads(described[0]["info"])["title"], "A whole reply")
        self.assertNotIn("repaired", json.loads(described[0]["provenance"]))
        # Every try broken: the repaired reply is used, and said to be repaired.
        self.server.reply = broken
        self.assertEqual(self.describe()["described"], 1)
        self.assertEqual(len(self.server.describe_requests()), 5)
        row = next(row for row in self.rows().values()
                   if json.loads(row["info"] or "{}").get("title") == "A dog")
        provenance = json.loads(row["provenance"])
        self.assertTrue(provenance["repaired"])
        self.assertNotIn("truncated", provenance)
        self.assertIn("from a reply whose JSON broke off", self.log_text())
        self.assertNotIn("cut off at the length limit", self.log_text())

    def test_a_reply_that_loops_until_it_is_cut_off_is_asked_again(self):
        self.small_images(1)
        self.configure()
        self.index()
        text = reply(title="Receipt", description="A receipt.", text="Total 3.50\n" * 2000)
        self.server.queue(fake_llm.Reply(text[:6000], finish="length"),
                          reply(title="Receipt", text="Total 3.50"))
        self.assertEqual(self.describe()["described"], 1)
        requests = self.server.describe_requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["payload"]["max_tokens"],
                         requests[1]["payload"]["max_tokens"])      # no more room for a loop
        row = self.rows()["photo0.jpg"]
        self.assertEqual(json.loads(row["info"])["tags"], fake_llm.DEFAULT_REPLY["tags"])
        self.assertNotIn("truncated", json.loads(row["provenance"]))

    def test_a_looping_reply_is_used_when_the_retries_do_no_better(self):
        self.small_images(1)
        self.configure()
        self.index()
        text = reply(title="Receipt", description="A receipt.", text="Total 3.50\n" * 2000)
        self.server.reply = fake_llm.Reply(text[:6000], finish="length")
        self.assertEqual(self.describe()["described"], 1)
        self.assertEqual(len(self.server.describe_requests()), 3)
        row = self.rows()["photo0.jpg"]
        self.assertEqual(json.loads(row["info"])["text"], "Total 3.50")     # loop trimmed
        self.assertTrue(json.loads(row["provenance"])["truncated"])

    def test_privacy_fields_a_cut_off_reply_did_not_reach_count_as_private(self):
        self.small_images(1)
        self.configure()
        self.index()
        text = reply(title="Letter", description="A letter.", text="Line one.\nLine two.",
                     private_info=False)
        self.server.queue(fake_llm.Reply(text[:text.index('"setting"')], finish="length"))
        self.assertEqual(self.describe()["described"], 1)
        row = self.rows()["photo0.jpg"]
        info = json.loads(row["info"])
        self.assertIs(info["private_info"], True)
        self.assertEqual(info["tags"], [])
        provenance = json.loads(row["provenance"])
        self.assertIn("private_info", provenance["missing_fields"])
        self.assertIn("sensitive", provenance["missing_fields"])
        self.assertNotIn("text", provenance["missing_fields"])
        # A reply that got as far as private_info keeps its own answer.
        text = json.dumps({"description": "A dog.", "title": "Dog", "private_info": False,
                           "aesthetic": 4})
        repaired = describe.repair(text[:text.index('"aesthetic"')])
        self.assertIn("aesthetic", repaired.missing)
        self.assertNotIn("private_info", repaired.missing)
        self.assertIs(describe.fill_missing(repaired.info, repaired.missing)["private_info"],
                      False)

    def test_an_enum_value_the_cut_came_in_counts_as_missing(self):
        text = reply(title="Dog", description="A dog.", sensitive="suggestive")
        repaired = describe.repair(text[:text.index('"suggestive"') + 5])
        self.assertIn("sensitive", repaired.missing)
        self.assertEqual(describe.fill_missing(
            repaired.info, repaired.missing, {"sensitive": "suggestive"})["sensitive"],
            "suggestive")

    def test_an_endpoint_that_refuses_more_room_is_still_used(self):
        self.small_images(3)
        self.configure(endpoint=self.endpoint(max_output_tokens=8192))
        self.index()
        body = {"error": {"type": "invalid_request_error", "message":
                          "max_tokens is too large: 16384. This model supports at most 8192 "
                          "completion tokens."}}
        self.server.queue(fake_llm.Reply(reply()[:40], finish="length"),
                          fake_llm.HttpError(400, body))
        summary = self.describe()
        self.assertEqual((summary["described"], summary["retired"], summary["error"]),
                         (3, [], ""))
        tokens = [request["payload"]["max_tokens"] for request in self.server.describe_requests()]
        self.assertEqual(tokens[:3], [8192, 16384, 8192])

    def test_a_cut_between_the_halves_of_an_emoji_is_stored(self):
        self.small_images(1)
        self.configure()
        self.index()
        text = json.dumps(dict(fake_llm.DEFAULT_REPLY, title="Chat", description="A chat.",
                               text="hello \U0001F600 world"))     # sent as two \u escapes
        self.server.queue(fake_llm.Reply(text[:text.index("\\ude00") + 3], finish="length"))
        self.assertEqual(self.describe()["described"], 1)
        self.assertEqual(json.loads(self.rows()["photo0.jpg"]["info"])["text"], "hello")
        # Half a pair that comes some other way is dropped when the text is tidied.
        self.assertEqual(describe._as_text("ok \ud83d"), "ok")
        self.assertEqual(describe.coerce_info({"description": "A \udc00dog."})["description"],
                         "A dog.")

    def test_repair_reply_needs_a_description_and_a_title(self):
        self.assertIsNone(describe.repair_reply('{"description": "A dog", "tit'))
        self.assertIsNone(describe.repair_reply('{"title": "Dog", "descr'))
        self.assertIsNone(describe.repair_reply("nothing"))
        info = describe.repair_reply('{"description": "A dog.", "title": "Dog", "photo_ty')
        self.assertEqual((info["description"], info["title"], info["photo_type"]),
                         ("A dog.", "Dog", "other"))
        # A looping field is not repaired into a description.
        self.assertIsNone(describe.repair_reply(json.dumps(
            {"description": "nice " * 200, "title": "Loop"})[:-5]))


if __name__ == "__main__":
    unittest.main()
