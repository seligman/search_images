"""Tests for english_words.py and static/english-words.js (with english-words-more.js): the
words of the language (and the well-known names) that "Did you mean" (static/search-core.js)
never corrects, and that tell a word no picture has from a name.

The filters english_words.py writes must answer the same in Python and in the page's
JavaScript (checked under Node.js when it is installed), never miss a word they were given
(nor a plural the page finds by its singular), rarely pass one they were not, and never
spell out a word in the files. All the words here are made up, everyday English or names
of world capitals.

With ENGLISH_WORDS_LISTS set to a folder that holds 12dicts unpacked (as README.md, Credits,
says), the README's command is run too and must make the shipped files byte for byte.
"""

import hashlib
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
CORE = os.path.join(support.REPO, "static", "search-core.js")
MORE_FILE = os.path.join(os.path.dirname(english_words.WORDS_FILE), english_words.MORE_NAME)
WORDS = ["house", "water", "garden", "window", "bridge", "meadow", "lantern", "kettle",
         "orchard", "harbor", "pebble", "thistle", "saddle", "blanket", "chimney"]
# Words descriptions seldom use: the shipped list has them in its rest, the
# everyday words of its first part lack them.
RARE = ["timepiece", "waterfowl", "funicular", "wombat", "eyewear"]
# Made-up names: no word of the language.
NAMES = ["maplebrook", "thornvale", "quillmoor", "vexbury", "zandrift"]
# Well-known names (world capitals): no words of the language, and no made-up names.
CAPITALS = ["Rome", "Stockholm", "Helsinki", "Athens", "Lisbon"]


def made_up(count, seed=7):
    """Strings of letters that are no words of WORDS (nor, almost surely, of English)."""
    rnd = random.Random(seed)
    found = set()
    while len(found) < count:
        word = "".join(rnd.choice("bcdfghjklmnpqrstvwxz") for _ in range(rnd.randint(5, 9)))
        found.add(word)
    return sorted(found)


def made_up_words(count, seed=9):
    """Made-up words with vowels (so never one of made_up's)."""
    rnd = random.Random(seed)
    found = set()
    while len(found) < count:
        found.add("".join(rnd.choice("bdklmnprst") + rnd.choice("aeiou")
                          for _ in range(rnd.randint(2, 4))))
    return sorted(found)


def node_answers(script, *args):
    """What a Node.js script prints (JSON), given search-core.js and args."""
    result = subprocess.run([NODE, "-e", script, CORE] + list(args), capture_output=True,
                            timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return json.loads(result.stdout.decode("ascii"))


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

    def test_short_and_odd_words_are_left_out_and_accents_folded(self):
        found = english_words.build(["an", "ox", "cat", "Dog", "dog", "x-ray", " lamp ", "99",
                                     "caf\u00e9", "Entr\u00e9e", "\u00e6ther", "don't",
                                     "o\u2019clock"])
        self.assertEqual(found["count"], 8)
        # (Apostrophes go, as the page takes them out of a search: "don't" is "dont".)
        for word in ("cat", "dog", "lamp", "cafe", "entree", "aether", "dont", "oclock"):
            self.assertTrue(english_words.contains(found, word), word)
        self.assertEqual([english_words.fold(word) for word in
                          ("Don't", "ma`am", "o\u2019clock", "rock\u02bcn\u2032roll", "y\u00b4all")],
                         ["dont", "maam", "oclock", "rocknroll", "yall"])
        self.assertEqual(english_words.clean_words(["b", "Bee", "bee", "b4"]), ["bee"])
        # 12dicts' marks after a word go ("$": in fewer sources, "+", "!"...).
        self.assertEqual(english_words.clean_words(["abaci$", "abductee+", "emoji!", "Mon:"]),
                         ["abaci", "abductee", "emoji", "mon"])
        self.assertEqual(english_words.fold("Stra\u00dfe \u00d8re"), "strasse ore")

    def test_the_file_spells_no_word(self):
        found = english_words.build(WORDS)
        text = english_words.words_js(found, "a made-up list")
        self.assertTrue(all(ord(ch) < 128 for ch in text))
        self.assertNotIn("\r", text)
        data = english_words.read_js(text)
        self.assertIsNone(data.pop("more"))
        self.assertIsNone(data.pop("names"))
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


class PluralTest(unittest.TestCase):

    def test_page_forms_are_the_pages(self):
        words = ["cats", "glasses", "boxes", "beaches", "shoes", "tomatoes", "horses", "buses",
                 "waves", "knives", "leaves", "berries", "ties", "cookies", "menus", "kiwis",
                 "cactus", "tennis", "glass", "1990s", "toes", "goes", "men", "children",
                 "geese", "feet", "bus", "news", "series", "species", "oases", "wolves",
                 "dishes", "quizzes", "potatoes", "lens", "mess", "abyss", "dies", "pies"]
        self.assertEqual(english_words.page_forms("cats"), ["cats", "cat"])
        self.assertEqual(english_words.page_forms("knives"), ["knives", "knive", "knif", "knife"])
        self.assertEqual(english_words.page_forms("geese"), ["geese", "goose"])
        self.assertEqual(english_words.page_forms("glass"), ["glass"])
        if not NODE:
            self.skipTest("node is not installed")
        script = ("var SearchCore = require(process.argv[1]);"
                  "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(function (w) {"
                  " return SearchCore.forms(w); })));")
        answers = node_answers(script, json.dumps(words))
        for word, forms in zip(words, answers):
            self.assertEqual(english_words.page_forms(word), forms, word)

    def test_plurals_the_page_finds_anyway_are_left_out(self):
        words = ["cat", "cats", "glass", "glasses", "box", "boxes", "berry", "berries",
                 "news", "new", "knife", "knives", "pants", "goose", "geese", "bus", "buses",
                 "dies", "die", "cookies"]
        kept = english_words.drop_plurals(words)
        self.assertEqual(kept, ["berry", "box", "bus", "cat", "cookies", "die", "glass", "goose",
                                "knife", "new", "pants"])
        # The page still knows every word given: it or one of its singulars is kept.
        for word in words:
            self.assertTrue(any(form in kept for form in english_words.page_forms(word)), word)
        # A word the other part holds is enough.
        self.assertEqual(english_words.drop_plurals(["cats", "dogs", "dog"], kept=["cat"]),
                         ["dog"])

    def test_a_list_in_two_parts(self):
        words = ["house", "houses", "kettle", "kettles", "cutlery", "feline", "felines",
                 "rodent", "rodents", "meadow"]
        first, rest = english_words.split_words(words, ["house", "kettle", "kettles", "Meadow",
                                                        "absent"])
        self.assertEqual(first, ["house", "kettle", "meadow"])
        self.assertEqual(rest, ["cutlery", "feline", "rodent"])
        self.assertEqual(english_words.split_words(words), (english_words.drop_plurals(
            english_words.clean_words(words)), None))


class TwoFilesTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.out = os.path.join(self.folder, "english-words.js")

    def write(self, words, first):
        return english_words.write_files(words, "a made-up list", self.out, first)

    def test_the_first_part_names_the_rest_with_its_version(self):
        everyday = WORDS + [word + "s" for word in WORDS]
        written = self.write(everyday + RARE + [word + "s" for word in RARE], everyday)
        self.assertEqual([os.path.basename(path) for path, _ in written],
                         ["english-words.js", english_words.MORE_NAME])
        with open(self.out, encoding="ascii") as handle:
            first = english_words.read_js(handle.read())
        with open(os.path.join(self.folder, english_words.MORE_NAME), "rb") as handle:
            rest_bytes = handle.read()
        rest = english_words.read_js(rest_bytes.decode("ascii"))
        self.assertEqual(first["count"], len(WORDS))
        self.assertEqual(rest["count"], len(RARE))
        self.assertEqual(first["more"], {"file": english_words.MORE_NAME,
                                         "v": hashlib.sha1(rest_bytes).hexdigest()[:10],
                                         "count": len(RARE)})
        self.assertIsNone(rest["more"])
        self.assertIn(b"var ENGLISH_WORDS_MORE = {", rest_bytes)
        self.assertIn(b"module.exports = ENGLISH_WORDS_MORE;", rest_bytes)
        filters = english_words.read_files(self.out)
        for word in WORDS + RARE + [word + "s" for word in WORDS + RARE] + ["Kettles"]:
            self.assertTrue(english_words.is_word(filters, word), word)
        for word in NAMES:
            self.assertFalse(english_words.is_word(filters, word), word)
        # In one part (no --first), as before.
        written = self.write(WORDS, None)
        self.assertEqual(len(written), 1)
        with open(self.out, encoding="ascii") as handle:
            self.assertIsNone(english_words.read_js(handle.read())["more"])

    def test_two_parts_pass_as_few_others_as_one(self):
        # Each part at half the rate: the two together pass about as many others
        # as one filter of the whole list would.
        words = made_up_words(4000)
        others = made_up(20000, seed=5)
        self.write(words, words[:2000])
        two = english_words.read_files(self.out)
        self.assertEqual([part["count"] for part in two], [2000, 2000])
        self.write(words, None)
        one = english_words.read_files(self.out)
        passing = [sum(1 for word in others if english_words.is_word(filters, word))
                   for filters in (two, one)]
        self.assertLess(passing[0], 2 * len(others) / english_words.RATE_DIVISOR + 5)
        self.assertLess(passing[1], 2 * len(others) / english_words.RATE_DIVISOR + 5)

    def test_the_same_answers_in_the_page(self):
        if not NODE:
            self.skipTest("node is not installed")
        everyday = WORDS + ["meadows", "kettles"]
        self.write(everyday + RARE + ["rodents", "felines"], everyday)
        asked = (WORDS + RARE + [word + "s" for word in WORDS + RARE] + NAMES +
                 made_up(3000, seed=11))
        asked_path = os.path.join(self.folder, "asked.json")
        with open(asked_path, "w", encoding="ascii", newline="\n") as handle:
            json.dump(asked, handle)
        # The page with both parts (as once the rest is in), then with the first alone.
        script = ("var SearchCore = require(process.argv[1]);"
                  "var first = require(process.argv[2]), rest = require(process.argv[3]);"
                  "var asked = JSON.parse(require('fs').readFileSync(process.argv[4], 'ascii'));"
                  "SearchCore.setWords([first, rest]);"
                  "var both = asked.map(function (w) { return SearchCore.isWord(w); });"
                  "SearchCore.setWords(first);"
                  "var alone = asked.map(function (w) { return SearchCore.isWord(w); });"
                  "console.log(JSON.stringify([both, alone, SearchCore.wordsState()]));")
        both, alone, state = node_answers(script, self.out,
                                          os.path.join(self.folder, english_words.MORE_NAME),
                                          asked_path)
        filters = english_words.read_files(self.out)
        for word, answer, first_only in zip(asked, both, alone):
            self.assertEqual(answer, english_words.is_word(filters, word), word)
            self.assertEqual(first_only, english_words.is_word(filters[:1], word), word)
        # (With no page to load it, the rest was wanted: a word outside the first
        # part is then no word, as without it.)
        self.assertEqual(state, "wanted")


class NamesTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.out = os.path.join(self.folder, "english-words.js")

    def test_names_are_the_capitalized_words_that_are_no_words(self):
        # (Of a list of names, only the words with a capital first letter; one the
        # list of words has, or a singular of, is a word anyway.)
        names = english_words.name_words(CAPITALS + ["Window", "Kettles", "lantern",
                                                     "St. Ives", "NATO"],
                                         WORDS + ["kettle"])
        self.assertEqual(names, ["athens", "helsinki", "lisbon", "nato", "rome", "stockholm"])

    def test_the_names_go_with_the_rest(self):
        everyday = WORDS + [word + "s" for word in WORDS]
        written = english_words.write_files(everyday + RARE, "a made-up list", self.out,
                                             everyday, CAPITALS + ["Garden"])
        self.assertEqual(len(written), 2)
        with open(self.out, encoding="ascii") as handle:
            first = english_words.read_js(handle.read())
        with open(os.path.join(self.folder, english_words.MORE_NAME), encoding="ascii") as handle:
            rest_text = handle.read()
        rest = english_words.read_js(rest_text)
        self.assertIsNone(first["names"])
        self.assertEqual(rest["names"]["count"], len(CAPITALS))
        self.assertEqual(len(rest["names"]["data"]) * 5, rest["names"]["size"])
        self.assertIn("5 well-known names", " ".join(rest_text.split()))
        # (The words read as before: the names are a filter of their own.)
        self.assertEqual(rest["count"], len(RARE))
        filters = english_words.read_files(self.out)
        for word in CAPITALS + ["ROME"]:
            self.assertTrue(english_words.is_name(filters, word), word)
            self.assertFalse(english_words.is_word(filters, word), word)
        for word in WORDS + RARE + NAMES + ["garden", "romes"]:
            self.assertFalse(english_words.is_name(filters, word), word)
        # In one part, the names go with it.
        english_words.write_files(WORDS, "a made-up list", self.out, None, CAPITALS)
        filters = english_words.read_files(self.out)
        self.assertEqual(filters[0]["names"]["count"], len(CAPITALS))
        self.assertTrue(english_words.is_name(filters, "lisbon"))
        with self.assertRaises(ValueError):
            english_words.words_js(filters[0], "a made-up list", more={"file": "x.js", "v": "1",
                                                                       "count": 1},
                                   names=filters[0]["names"])

    def test_the_same_names_in_the_page(self):
        if not NODE:
            self.skipTest("node is not installed")
        everyday = WORDS + ["meadows"]
        english_words.write_files(everyday + RARE, "a made-up list", self.out, everyday,
                                  CAPITALS)
        asked = ([word.lower() for word in CAPITALS] + WORDS + RARE + NAMES + ["romes"] +
                 made_up(3000, seed=13))
        asked_path = os.path.join(self.folder, "asked.json")
        with open(asked_path, "w", encoding="ascii", newline="\n") as handle:
            json.dump(asked, handle)
        script = ("var SearchCore = require(process.argv[1]);"
                  "var first = require(process.argv[2]), rest = require(process.argv[3]);"
                  "var asked = JSON.parse(require('fs').readFileSync(process.argv[4], 'ascii'));"
                  "SearchCore.setWords(first);"
                  "var before = asked.map(function (w) { return SearchCore.isName(w); });"
                  "SearchCore.setWords([first, rest]);"
                  "console.log(JSON.stringify([before, asked.map(function (w) {"
                  " return SearchCore.isName(w); })]));")
        before, after = node_answers(script, self.out,
                                     os.path.join(self.folder, english_words.MORE_NAME),
                                     asked_path)
        filters = english_words.read_files(self.out)
        # (No name is told until the rest of the list is in.)
        self.assertEqual(before, [False] * len(asked))
        for word, answer in zip(asked, after):
            self.assertEqual(answer, english_words.is_name(filters, word), word)
        self.assertEqual(after[:len(CAPITALS)], [True] * len(CAPITALS))


class ShippedFilesTest(unittest.TestCase):

    def setUp(self):
        self.filters = english_words.read_files(english_words.WORDS_FILE)

    def test_the_shipped_list(self):
        first, rest = self.filters
        for found in (first, rest):
            self.assertEqual(found["format"], english_words.FORMAT)
            self.assertEqual(len(found["data"]) * 5, found["size"])
            self.assertTrue(set(found["data"]) <= set(english_words.ALPHABET))
        with open(MORE_FILE, "rb") as handle:
            rest_bytes = handle.read()
        self.assertEqual(first["more"], {"file": english_words.MORE_NAME,
                                         "v": english_words.version(rest_bytes),
                                         "count": rest["count"]})
        # Everyday words in the first part, rarer ones in the rest, plurals by
        # their singulars; made-up names in neither.
        self.assertGreater(first["count"], 15000)
        self.assertGreater(first["count"] + rest["count"], 40000)
        for word in ("house", "water", "garden", "window", "bridge", "cold", "words", "the",
                     "kitchen", "sheep", "italian", "christmas", "monday", "cafe", "running",
                     "o'clock", "oclock", "don't", "ma'am", "isn't", "y'all"):
            self.assertTrue(english_words.is_word(self.filters[:1], word), word)
        for word in RARE + ["timepieces", "wombats", "funiculars"]:
            self.assertTrue(english_words.is_word(self.filters, word), word)
            self.assertFalse(english_words.is_word(self.filters[:1], word), word)
        for word in NAMES:
            self.assertFalse(english_words.is_word(self.filters, word), word)
            self.assertFalse(english_words.is_name(self.filters, word), word)
        passing = [word for word in made_up(4000, seed=3)
                   if english_words.is_word(self.filters, word)]
        self.assertLess(len(passing), 5 * 4000 / english_words.RATE_DIVISOR + 3)
        # Well-known names, with the rest: "did you mean" leaves them alone.
        self.assertIsNone(first["names"])
        self.assertGreater(rest["names"]["count"], 5000)
        for word in CAPITALS + ["Norway", "Greece", "Nepal", "Vienna", "Kenya", "Mozart"]:
            self.assertTrue(english_words.is_name(self.filters, word), word)
            self.assertFalse(english_words.is_word(self.filters, word), word)
        passing = [word for word in made_up(4000, seed=4)
                   if english_words.is_name(self.filters, word)]
        self.assertLess(len(passing), 5 * 4000 / english_words.NAMES_RATE_DIVISOR + 3)

    def test_small_where_it_counts(self):
        # The first part goes with every search page: no larger than the list it
        # replaced (about 67 KB); the rest only when a search first needs it.
        with open(english_words.WORDS_FILE, encoding="ascii") as handle:
            text = handle.read()
        self.assertLess(len(text), 70 * 1024)
        self.assertLess(os.path.getsize(MORE_FILE), 130 * 1024)
        self.assertIn("var ENGLISH_WORDS = {", text)
        self.assertIn("module.exports = ENGLISH_WORDS;", text)
        for path in (english_words.WORDS_FILE, MORE_FILE):
            with open(path, "rb") as handle:
                data = handle.read()
            self.assertTrue(all(byte < 128 for byte in data))
            self.assertNotIn(b"\r", data)
            self.assertTrue(all(len(line) < 100 for line in data.splitlines()))

    def readme_command(self):
        """The README's command that makes the shipped files (Credits): its lists and
        its --source."""
        readme = os.path.join(support.REPO, "README.md")
        with open(readme, encoding="utf-8") as handle:
            found = re.search(r"python english_words\.py --list (\S+) --list (\S+) "
                              r"--first (\S+) --names (\S+) --source \"([^\"]*)\"",
                              handle.read())
        self.assertIsNotNone(found)
        return list(found.groups()[:4]), found.group(5)

    def test_the_readmes_command_makes_the_shipped_files(self):
        # (README's Credits give the command that makes the files as shipped from the
        # downloaded lists: its --source is in both files' notes, the lists' licence
        # named -- by name only: no page asset names another host, not even in a
        # comment. The command itself is run below when the lists are at hand.)
        lists, source = self.readme_command()
        self.assertEqual([os.path.basename(path) for path in lists],
                         ["3of6game.txt", "3esl.txt", "3esl.txt", "5d+2a.txt"])
        first, rest = self.filters
        with open(english_words.WORDS_FILE, encoding="ascii") as handle:
            first_text = handle.read()
        with open(MORE_FILE, encoding="ascii") as handle:
            rest_text = handle.read()
        more = first.pop("more")
        rest.pop("more")
        names = rest.pop("names")
        first.pop("names")
        self.assertEqual(english_words.words_js(first, source, more), first_text)
        self.assertEqual(english_words.words_js(rest, source, rest=True, names=names), rest_text)
        for text in (first_text, rest_text):
            self.assertIn("public domain", " ".join(text.split()))
            self.assertNotIn("://", text)

    def test_the_module_gives_the_readmes_command(self):
        # (english_words.py's own notes give the command that makes the shipped files,
        # the README's, and the zip's SHA-256 as README.md, Credits, has it.)
        lists, source = self.readme_command()
        notes = " ".join(english_words.__doc__.split())
        self.assertIn("python english_words.py --list %s --list %s --first %s --names %s "
                      "--source \"%s\"" % tuple(lists + [source]), notes)
        with open(os.path.join(support.REPO, "README.md"), encoding="utf-8") as handle:
            readme = " ".join(handle.read().split())
        digest = re.search(r"SHA-256 ([0-9a-f]{64})", readme)
        self.assertIsNotNone(digest)
        self.assertIn("12dicts-6.0.2.zip, SHA-256 %s" % digest.group(1), notes)

    @unittest.skipUnless(os.environ.get("ENGLISH_WORDS_LISTS"),
                         "set ENGLISH_WORDS_LISTS to the folder 12dicts is unpacked in")
    def test_the_readmes_command_run(self):
        # (The lists cannot ship: with ENGLISH_WORDS_LISTS, the folder that holds the
        # unpacked 12dicts zip, the README's command runs from it into a temporary
        # folder and must write the shipped files byte for byte.)
        lists, source = self.readme_command()
        folder = os.environ["ENGLISH_WORDS_LISTS"]
        paths = [os.path.join(folder, *path.split("/")[1:]) for path in lists]
        for path in paths:
            self.assertTrue(os.path.isfile(path), path)
        out_folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out_folder, True)
        out = os.path.join(out_folder, "english-words.js")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(english_words._main(["--list", paths[0], "--list", paths[1],
                                                  "--first", paths[2], "--names", paths[3],
                                                  "--source", source, "--out", out]), 0)
        for made, shipped in ((out, english_words.WORDS_FILE),
                              (os.path.join(out_folder, english_words.MORE_NAME), MORE_FILE)):
            with open(made, "rb") as handle:
                made_bytes = handle.read()
            with open(shipped, "rb") as handle:
                self.assertEqual(made_bytes, handle.read(), os.path.basename(shipped))


class CommandLineTest(unittest.TestCase):

    def test_command_line(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        listed = os.path.join(folder, "words.txt")
        # (One word a line; a phrase is left out; Latin-1 as older lists are.)
        with open(listed, "wb") as handle:
            handle.write("house\r\nwater$\ngarden\nice cream\ncaf\u00e9\n".encode("latin-1"))
        rarer = os.path.join(folder, "rarer.txt")
        with open(rarer, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("cutlery\nfeline\nfelines\nhouses\n")
        out = os.path.join(folder, "english-words.js")
        with redirect_stdout(io.StringIO()) as said:
            self.assertEqual(english_words._main(["--list", listed, "--out", out]), 0)
        self.assertIn("4 words", said.getvalue())
        with open(out, encoding="ascii") as handle:
            text = handle.read()
        self.assertIn("4 words of 3 letters or more", text)
        self.assertIn(": a list of words.", text)
        self.assertTrue(re.search(r"\(see\s+english_words\.py", text))
        named = os.path.join(folder, "names.txt")
        with open(named, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("Rome\nLisbon\nGarden\nhouse\n")
        with redirect_stdout(io.StringIO()) as said:
            self.assertEqual(english_words._main(["--list", listed, "--list", rarer, "--first",
                                                  listed, "--names", named, "--source",
                                                  "two lists", "--out", out]), 0)
        self.assertEqual(len(said.getvalue().splitlines()), 2)
        self.assertTrue(os.path.exists(os.path.join(folder, english_words.MORE_NAME)))
        with redirect_stdout(io.StringIO()) as said:
            self.assertEqual(english_words._main(["--out", out, "--check", "garden", "gardn",
                                                  "Felines", "Houses", "ice", "Rome",
                                                  "lisbon"]), 0)
        self.assertEqual(said.getvalue().splitlines(),
                         ["garden: a word", "gardn: not a word", "Felines: a word",
                          "Houses: a word", "ice: not a word", "Rome: a name",
                          "lisbon: a name"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(english_words._main([]), 2)
            self.assertEqual(english_words._main(["--list", listed, "--out", out,
                                                  "--source", "caf\u00e9"]), 2)
            self.assertEqual(english_words._main(["--out", os.path.join(folder, "none.js"),
                                                  "--check", "house"]), 2)


if __name__ == "__main__":
    unittest.main()
