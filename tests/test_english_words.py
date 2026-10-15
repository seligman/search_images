"""Tests for english_words.py and static/english-words.js: the words of the language
that "Did you mean" (static/search-core.js) never corrects.

The filter english_words.py writes must answer the same in Python and in the
page's JavaScript (checked under Node.js when it is installed), never miss a
word it was given, rarely pass one it was not, and never spell out a word in
the file. All the words here are made up or everyday English.
"""

import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

import support

import english_words

NODE = shutil.which("node")
WORDS = ["house", "water", "garden", "window", "bridge", "meadow", "lantern", "kettle",
         "orchard", "harbor", "pebble", "thistle", "saddle", "blanket", "chimney"]


def made_up(count, seed=7):
    """Strings of letters that are no words of WORDS (nor, almost surely, of English)."""
    rnd = random.Random(seed)
    found = set()
    while len(found) < count:
        word = "".join(rnd.choice("bcdfghjklmnpqrstvwxz") for _ in range(rnd.randint(5, 9)))
        found.add(word)
    return sorted(found)


class FilterTest(unittest.TestCase):

    def test_every_word_given_passes_and_few_others(self):
        many = ["%s%s" % (word, suffix) for word in WORDS for suffix in ("", "s", "ing", "ed")]
        found = english_words.build(many)
        self.assertEqual(found["count"], len(set(many)))
        for word in many:
            self.assertTrue(english_words.contains(found, word), word)
        others = made_up(5000)
        passing = [word for word in others if english_words.contains(found, word)]
        # (About one in RATE_DIVISOR; five times that is far off.)
        self.assertLess(len(passing), 5 * len(others) / english_words.RATE_DIVISOR + 3)
        # Upper case is the same word; anything but letters a to z never passes.
        self.assertTrue(english_words.contains(found, "House"))
        for word in ("", "h0use", "caf\u00e9", "two words"):
            self.assertFalse(english_words.contains(found, word), word)

    def test_short_and_odd_words_are_left_out(self):
        found = english_words.build(["an", "ox", "cat", "Dog", "dog", "x-ray", "caf\u00e9",
                                     " lamp ", "99"])
        self.assertEqual(found["count"], 3)
        for word in ("cat", "dog", "lamp"):
            self.assertTrue(english_words.contains(found, word), word)
        self.assertEqual(english_words.clean_words(["b", "Bee", "bee", "b4"]), ["bee"])

    def test_the_file_spells_no_word(self):
        found = english_words.build(WORDS)
        text = english_words.words_js(found, "a made-up list")
        self.assertTrue(all(ord(ch) < 128 for ch in text))
        self.assertNotIn("\r", text)
        data = english_words.read_js(text)
        self.assertEqual(data, found)
        # Five bits a character, from an alphabet without vowels: no word of
        # the list can appear in it (a string of consonants or digits can, by
        # chance: tests/test_repo_hygiene.py checks the file for private words).
        self.assertTrue(set(found["data"]) <= set(english_words.ALPHABET))
        self.assertFalse(set("aeiouy") & set(english_words.ALPHABET))
        self.assertEqual(len(found["data"]) * 5, found["size"])
        body = text.split("var ENGLISH_WORDS", 1)[1]
        for word in WORDS:
            self.assertNotIn(word, body)
        self.assertTrue(all(len(line) < 100 for line in text.splitlines()))

    def test_the_same_answers_in_the_page(self):
        if not NODE:
            self.skipTest("node is not installed")
        found = english_words.build(WORDS)
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "words.js")
        with open(path, "w", encoding="ascii", newline="\n") as handle:
            handle.write(english_words.words_js(found, "a made-up list"))
        asked = WORDS + [word + "s" for word in WORDS] + made_up(3000, seed=11)
        asked_path = os.path.join(folder, "asked.json")
        with open(asked_path, "w", encoding="ascii", newline="\n") as handle:
            json.dump(asked, handle)
        script = ("var SearchCore = require(process.argv[1]);"
                  "SearchCore.setWords(require(process.argv[2]));"
                  "var asked = JSON.parse(require('fs').readFileSync(process.argv[3], 'ascii'));"
                  "console.log(JSON.stringify(asked.map(function (w) {"
                  " return SearchCore.isWord(w); })));")
        result = subprocess.run([NODE, "-e", script,
                                 os.path.join(support.REPO, "static", "search-core.js"), path,
                                 asked_path], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        answers = json.loads(result.stdout.decode("ascii"))
        for word, answer in zip(asked, answers):
            # (The page asks for a word's singular too: houses is a house.)
            expected = english_words.contains(found, word) or (
                word.endswith("s") and english_words.contains(found, word[:-1]))
            self.assertEqual(answer, expected, word)


class ShippedFileTest(unittest.TestCase):

    def test_the_shipped_list(self):
        with open(english_words.WORDS_FILE, encoding="ascii") as handle:
            text = handle.read()
        found = english_words.read_js(text)
        self.assertIsNotNone(found)
        self.assertEqual(found["format"], english_words.FORMAT)
        self.assertGreater(found["count"], 20000)
        self.assertEqual(len(found["data"]) * 5, found["size"])
        self.assertTrue(set(found["data"]) <= set(english_words.ALPHABET))
        for word in ("house", "water", "garden", "window", "bridge", "cold", "words", "the",
                     "kitchen", "sheep"):
            self.assertTrue(english_words.contains(found, word), word)
        passing = [word for word in made_up(4000, seed=3)
                   if english_words.contains(found, word)]
        self.assertLess(len(passing), 5 * 4000 / english_words.RATE_DIVISOR + 3)
        # Small next to the search engine: its budget, as visitors download it.
        self.assertLess(len(text), 80 * 1024)
        self.assertIn("var ENGLISH_WORDS = {", text)
        self.assertIn("module.exports = ENGLISH_WORDS;", text)

    def test_the_readmes_command_makes_the_shipped_notes(self):
        # (The README's Credits give the command that makes the file as shipped: its
        # --source is the file's notes, the words' licence named, as exports carry it --
        # by name only: no page asset names another host, not even in a comment.)
        with open(english_words.WORDS_FILE, encoding="ascii") as handle:
            text = handle.read()
        readme = os.path.join(os.path.dirname(english_words.WORDS_FILE), "..", "README.md")
        with open(readme, encoding="utf-8") as handle:
            found = re.search(r'english_words\.py --model MODEL\.gguf --source "([^"]*)"',
                              handle.read())
        self.assertIsNotNone(found)
        self.assertEqual(english_words.words_js(english_words.read_js(text), found.group(1)),
                         text)
        self.assertIn("under the Apache License 2.0", text)
        self.assertNotIn("://", text)

    def test_command_line(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        listed = os.path.join(folder, "words.txt")
        with open(listed, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("house water\ngarden\n")
        out = os.path.join(folder, "english-words.js")
        with redirect_stdout(io.StringIO()) as said:
            self.assertEqual(english_words._main(["--list", listed, "--out", out]), 0)
        self.assertIn("3 words", said.getvalue())
        with redirect_stdout(io.StringIO()) as said:
            self.assertEqual(english_words._main(["--out", out, "--check", "garden", "gardn"]), 0)
        self.assertEqual(said.getvalue().splitlines(), ["garden: a word", "gardn: not a word"])
        with open(out, encoding="ascii") as handle:
            self.assertIn("3 words of 3 letters or more: a list of words.", handle.read())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(english_words._main([]), 2)
            self.assertEqual(english_words._main(["--list", listed, "--out", out,
                                                  "--source", "caf\u00e9"]), 2)
        self.assertTrue(re.search(r"\(see english_words\.py", open(out, encoding="ascii").read()))


if __name__ == "__main__":
    unittest.main()
