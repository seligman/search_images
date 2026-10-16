#!/usr/bin/env python3
"""The words of the English language: what "Did you mean" leaves alone, and
what tells a word from a name.

When a word of a search matches no picture, the search page offers a
spelling that has matches ("did you mean", static/search-core.js). A word
that is itself a word of the language ("cold", "words", "cutlery") is meant
as typed: offering another one that pictures have ("old", "woods") only
gets in the way, and so is a well-known name ("rome" is not "rose"). And a
word that no picture has and that is no word of the language
("Maplebrook", "Thornvale", "Rome" too) is taken for a name: in a small set
it gets none of the pictures closest to it in meaning, which are mostly
unrelated ones (see MEANING_FEW_Z there). So the page knows the words of
the language, and well-known names: static/english-words.js and
static/english-words-more.js, which this module writes.

They hold no words as text, only Bloom filters of them: a bit array, each
word setting HASHES bits picked by two FNV-1a hashes of it (bit i of a word
is (h1 + i * h2) mod SIZE), so a word is known when all of its bits are set.
A word that is not in the list passes too, about once in RATE_DIVISOR
tries (a misspelling then gets no suggestion), never the other way. The
bits are written five to a character from an alphabet without vowels
(ALPHABET), so the files spell out no word that has a vowel; a string of
consonants, digits, "-" and "_" can still turn up by chance, so
tests/test_repo_hygiene.py checks them for private words like any other
file. That is about 14 bits a word in one filter, about 16 in each of a
list in two parts (below): about a quarter of the words as text, the
plurals left out below counted. It works the same in the server's pages
and in a published site, offline.

The words are those of two public domain lists of 12dicts (Alan Beale):
3of6game, about 65,000 words of English as six learner's dictionaries
have it, with their inflected forms (plurals, -ing, -ed, -er, -est), and
3esl, about 22,000 everyday words, for the capitalized ones 3of6game
leaves out ("italian", "christmas", "monday"). Only words of three letters
or more are kept (letters a to z, apostrophes and accents taken off as the
page takes them off: "oclock" for "o'clock", "cafe" for "caf\u00e9"), and a
plural is left out when the page finds it by its singular anyway
(page_forms: the same rule as wordForms in static/search-core.js).

The well-known names (--names) are the capitalized words of 12dicts' 5d+2a
list (places, people, peoples: "Rome", "Norway", "Mozart"), but for those
that are words of the list anyway. They are a filter of their own, in the
rest of the list (below), at a lower error rate (NAMES_RATE_DIVISOR: they
are few): "did you mean" leaves them alone, and a search still takes one
that no picture has for a name.

The list comes in two parts (--first FIRST.txt): the words of FIRST.txt
(3esl's: the everyday ones) go with the page, in english-words.js; the
others go to english-words-more.js (MORE_NAME), which the page loads only
when a search first has a word that no picture has and that is not in the
first part (wantWords in static/search-core.js). Each part is a filter at
half the error rate (PART_RATE_DIVISOR), so that the two together pass
about as many words that are not in the list as one filter would. The first
part names the second with its version (the hash of its bytes, as an
export's addresses carry it), so a published site's copy of it is never a
stale one. Any other lists work too:

    python english_words.py --list WORDS.txt [--list MORE.txt ...] [--first FIRST.txt]
                            [--names NAMES.txt]
    python english_words.py --model MODEL.gguf   (the whole words of a BERT-family
                                                  GGUF model's vocabulary, with or
                                                  without lists)
    python english_words.py --check WORD ...     (a word, or a name, as the page
                                                  asks?)

A list has a word a line (UTF-8, or Latin-1 as older lists are); a line of
several words, a phrase, is left out, and so are the marks some lists put
after a word (12dicts' "$", "+", "!"...); of a list of names, only the
words with a capital first letter count. --source sets where the files'
notes say the words are from.

The files as shipped are made from 12dicts 6.0.2 (12dicts-6.0.2.zip, SHA-256
64ac1d35acb66b550c7ebc56e080b62e0bad8f5984d72059dc2e05ac48780e52; where
to download it: README.md, Credits), unpacked into a folder 12dicts in the
program folder, by this command run there (one line; it writes both files,
the credits in their notes, byte for byte as shipped):

    python english_words.py --list 12dicts/International/3of6game.txt
        --list 12dicts/American/3esl.txt --first 12dicts/American/3esl.txt
        --names 12dicts/International/5d+2a.txt
        --source "the 3of6game, 3esl and 5d+2a lists of 12dicts 6.0.2 by Alan Beale (public domain)"

This module uses only the standard library.
"""

import argparse
import hashlib
import math
import os
import re
import sys
import textwrap
import unicodedata

WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static",
                          "english-words.js")
# The second part of a list in two parts (--first), next to the first.
MORE_NAME = "english-words-more.js"
FORMAT = 1
# Words shorter than this are never corrected anyway (see suggest() in
# static/search-core.js), so they are left out.
MIN_LETTERS = 3
# About one word in this many that is not in the list passes as one.
RATE_DIVISOR = 1000
# Each part of a list in two parts: a word that is in neither is asked of
# both, so each passes it half as often.
PART_RATE_DIVISOR = 2 * RATE_DIVISOR
# The well-known names (--names): a word passes as one by chance (and then gets no
# suggestion) about once in this many tries. They are few, so a lower rate is cheap.
NAMES_RATE_DIVISOR = 10 * RATE_DIVISOR
# Five bits a character; no vowels (nor y), so no word can be spelled.
ALPHABET = "0123456789bcdfghjklmnpqrstvwxz-_"
SEED_1 = 2166136261             # FNV-1a's offset basis
SEED_2 = 1540483477             # another start, for the second hash
FNV_PRIME = 16777619
LINE = 92                       # characters of data per line of the file
_WORD = re.compile(r"^[a-z]+$")
# Marks a list puts after a word (12dicts: "$" fewer sources, "+" a
# signature word, "!" a new word, ":" an abbreviation...).
_MARKS = re.compile(r"[%!#$^=&+<>~@*:]+$")
# Letters the page spells out (SPECIAL_LETTERS in static/search-core.js).
SPECIAL_LETTERS = {"\u00df": "ss", "\u00e6": "ae", "\u0153": "oe", "\u00f8": "o",
                   "\u0111": "d", "\u00f0": "d", "\u0142": "l", "\u00fe": "th",
                   "\u0131": "i", "\u0127": "h"}
# The apostrophes the page takes out of a word ("don't" is "dont": APOSTROPHE_CHARS in
# static/search-core.js).
APOSTROPHES = "'`\u00b4\u2018\u2019\u201b\u02bc\u2032"
# The page's IRREGULAR_PLURALS (page_forms is its wordForms).
IRREGULAR_PLURALS = {"men": "man", "women": "woman", "children": "child", "people": "person",
                     "mice": "mouse", "geese": "goose", "feet": "foot", "teeth": "tooth",
                     "oxen": "ox", "cacti": "cactus", "fungi": "fungus"}


def _fnv(word, seed):
    value = seed
    for byte in word.encode("ascii"):
        value = ((value ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return value


def bit_positions(word, size, hashes):
    """The bits of a word (lower case a to z) in a filter of size bits."""
    first = _fnv(word, SEED_1)
    step = _fnv(word, SEED_2) | 1
    return [(first + number * step) % size for number in range(hashes)]


def fold(word):
    """A word as the page searches it: lower case, apostrophes and accents taken off
    ("Don't" is "dont", "Caf\u00e9" is "cafe"), a few letters spelled out (normalize in
    static/search-core.js)."""
    word = str(word).strip().lower()
    word = "".join(SPECIAL_LETTERS.get(ch, ch) for ch in word if ch not in APOSTROPHES)
    return "".join(ch for ch in unicodedata.normalize("NFKD", word)
                   if not unicodedata.combining(ch)).lower()


def clean_words(words):
    """The words worth keeping (marks after them dropped, folded), sorted, each once."""
    kept = set()
    for word in words:
        word = fold(_MARKS.sub("", str(word).strip()))
        if len(word) >= MIN_LETTERS and _WORD.match(word):
            kept.add(word)
    return sorted(kept)


def page_forms(word):
    """The forms the page looks a folded word up by: the word, then for an English
    plural its possible singulars (computeForms in static/search-core.js)."""
    forms = [word]
    if word in IRREGULAR_PLURALS:
        return forms + [IRREGULAR_PLURALS[word]]
    length = len(word)
    if length <= 3 or not word.endswith("s"):
        return forms
    before = word[-2]
    if not "a" <= before <= "z" or before == "s":
        return forms                                    # 1990s, glass
    stem = word[:-1]
    if before in "ui":
        forms.append(stem)                              # menus, kiwis
    elif word.endswith("ies"):
        if length > 4:
            forms.append(word[:-3] + "y")               # berries
        forms.append(stem)
    elif re.search(r"(?:ss|x|z|ch|sh)es$", word):
        forms.extend([word[:-2], stem])                 # glasses, boxes
    elif re.search(r"[os]es$", word):
        forms.append(stem)                              # shoes, horses
        if length - 2 >= 3:
            forms.append(word[:-2])                     # tomatoes, buses
    elif word.endswith("ves"):
        forms.extend([stem, word[:-3] + "f", word[:-3] + "fe"])
    else:
        forms.append(stem)                              # cats
    return forms


def drop_plurals(words, kept=()):
    """The words without those the page finds by another form anyway: a word goes when
    one of its other forms (page_forms) is in kept (words a filter the page always
    has holds) or is a word that stays (one with no other form in words or kept).
    The page still knows every word given."""
    given = set(words)
    kept = set(kept)
    stays = set(word for word in given
                if not any(form in given or form in kept for form in page_forms(word)[1:]))
    return sorted(word for word in given
                  if word in stays or not any(form in stays or form in kept
                                              for form in page_forms(word)[1:]))


def name_words(names, words=()):
    """The well-known names of a list of names (--names): its words with a capital first
    letter, as clean_words keeps them, but for those the page takes for words anyway
    (words: the list's words; a name goes when it or one of its singulars is one)."""
    known = set(clean_words(words))
    capitals = [word for word in names if str(word).strip()[:1].isupper()]
    return [word for word in clean_words(capitals)
            if not any(form in known for form in page_forms(word))]


def split_words(words, first=None):
    """(first part, rest) of a list, plurals the page finds anyway left out (drop_plurals):
    the words of first that are in the list, then the others; rest is None without first."""
    words = clean_words(words)
    if first is None:
        return drop_plurals(words), None
    listed = set(words)
    head = set(word for word in clean_words(first) if word in listed)
    entries = drop_plurals(head)
    return entries, drop_plurals([word for word in words if word not in head], kept=entries)


def build(words, rate_divisor=RATE_DIVISOR):
    """{format, count, size, hashes, data} of a list of words (see the module notes)."""
    words = clean_words(words)
    count = max(1, len(words))
    size = int(math.ceil(count * math.log(rate_divisor) / (math.log(2) ** 2)))
    size = max(5, size + (-size) % 5)
    hashes = max(1, int(round(size / count * math.log(2))))
    bits = bytearray(size)
    for word in words:
        for position in bit_positions(word, size, hashes):
            bits[position] = 1
    chars = []
    for start in range(0, size, 5):
        value = 0
        for offset in range(5):
            value |= bits[start + offset] << offset
        chars.append(ALPHABET[value])
    return {"format": FORMAT, "count": len(words), "size": size, "hashes": hashes,
            "data": "".join(chars)}


def contains(words_filter, word):
    """True when a lower case word passes the filter (build's answer)."""
    word = str(word).lower()
    if not _WORD.match(word):
        return False
    data = words_filter["data"]
    for position in bit_positions(word, words_filter["size"], words_filter["hashes"]):
        if not (ALPHABET.index(data[position // 5]) >> (position % 5)) & 1:
            return False
    return True


def is_word(filters, word):
    """True when the page takes a word for one of the language: it or one of its
    singulars (page_forms) passes one of the filters (both parts of a list)."""
    return any(contains(words_filter, form) for form in page_forms(fold(word))
               for words_filter in filters if words_filter)


def is_name(filters, word):
    """True when the page takes a word that is no word of the language for a well-known
    name: it passes the names filter of one of the files (as typed, no other form)."""
    return not is_word(filters, word) and any(
        contains(found["names"], fold(word)) for found in filters
        if found and found.get("names"))


def version(data):
    """A short hash of a file's bytes, for its address (export._version's)."""
    return hashlib.sha1(data).hexdigest()[:10]


def _chunks(data):
    return ",\n".join('    "%s"' % data[start:start + LINE] for start in range(0, len(data), LINE))


def words_js(words_filter, source, more=None, rest=False, names=None):
    """The text of static/english-words.js for a filter; source says where its words
    are from (one line of plain ASCII). more ({file, v, count}): the second part of
    the list, which the page loads when it first needs it; rest: this is that second
    part (english-words-more.js, the global ENGLISH_WORDS_MORE); names: the filter of
    the well-known names, which goes with the last part (never with more)."""
    if more and names:
        raise ValueError("the names go with the rest of the list")
    if rest:
        what = ("The rest of the words of the English language (see english-words.js), which "
                "the search page loads when it first needs them.")
    else:
        what = ("The words of the English language, for static/search-core.js: \"Did you "
                "mean\" never corrects a word that is one, and a word that no picture has "
                "and that is none is taken for a name.")
        if more:
            what += (" These are the words most searches meet; the others are in %s, which "
                     "the page loads when it first needs them." % more["file"])
    what += (" %d words of %d letters or more (a plural the page finds by its singular left "
             "out): %s. As a Bloom filter: %d bits, %d of them per word, five to a character "
             "(see english_words.py, which made this file)."
             % (words_filter["count"], MIN_LETTERS, source, words_filter["size"],
                words_filter["hashes"]))
    if names:
        what += (" And %d well-known names (places, people), which \"did you mean\" leaves "
                 "alone though a search takes them for names: a filter of %d bits, %d per name."
                 % (names["count"], names["size"], names["hashes"]))
    lines = textwrap.wrap(what, 76, initial_indent="/* ", subsequent_indent="   ",
                          break_long_words=False, break_on_hyphens=False)
    lines[-1] += " */"
    name = "ENGLISH_WORDS_MORE" if rest else "ENGLISH_WORDS"
    lines.append("var %s = {format: %d, count: %d, size: %d, hashes: %d, data: ["
                 % (name, words_filter["format"], words_filter["count"], words_filter["size"],
                    words_filter["hashes"]))
    lines.append(_chunks(words_filter["data"]))
    if more:
        lines.append('].join(""), more: {file: "%s", v: "%s", count: %d}};'
                     % (more["file"], more["v"], more["count"]))
    elif names:
        lines.append('].join(""), names: {count: %d, size: %d, hashes: %d, data: ['
                     % (names["count"], names["size"], names["hashes"]))
        lines.append(_chunks(names["data"]))
        lines.append('].join("")}};')
    else:
        lines.append("].join(\"\")};")
    lines.append("")
    lines.append('if (typeof module === "object" && module && module.exports) {')
    lines.append("    module.exports = %s;" % name)
    lines.append("}")
    return "\n".join(lines) + "\n"


def read_js(text):
    """The filter in the text of an english-words.js file (words_js), or None; its "more"
    is {file, v, count} of the second part when it names one, its "names" the filter of
    the well-known names ({count, size, hashes, data}) when it has them, else None."""
    text, _, named = text.partition('].join(""), names: {')
    head = re.search(r"format: (\d+), count: (\d+), size: (\d+), hashes: (\d+)", text)
    if not head:
        return None
    more = re.search(r'more: \{file: "([a-z][a-z0-9-]*\.js)", v: "([0-9a-f]+)", count: (\d+)\}',
                     text)
    names = re.match(r"count: (\d+), size: (\d+), hashes: (\d+)", named)
    return {"format": int(head.group(1)), "count": int(head.group(2)),
            "size": int(head.group(3)), "hashes": int(head.group(4)),
            "data": "".join(re.findall(r'^    "([^"]*)"', text, re.M)),
            "more": {"file": more.group(1), "v": more.group(2), "count": int(more.group(3))}
            if more else None,
            "names": {"count": int(names.group(1)), "size": int(names.group(2)),
                      "hashes": int(names.group(3)),
                      "data": "".join(re.findall(r'^    "([^"]*)"', named, re.M))}
            if names else None}


def read_files(path):
    """[first part, second part or None] of the files at path (english-words.js)."""
    with open(path, encoding="ascii") as handle:
        found = read_js(handle.read())
    if found is None:
        return None
    rest = None
    if found["more"]:
        more_path = os.path.join(os.path.dirname(os.path.abspath(path)), found["more"]["file"])
        with open(more_path, encoding="ascii") as handle:
            rest = read_js(handle.read())
    return [found, rest]


def write_files(words, source, out=WORDS_FILE, first=None, names=None):
    """Write the list (words; first: the words that go with the page; names: a list of
    names, see the module notes) to out and, in two parts, MORE_NAME next to it, the
    names with the last part. Returns [(path, filter)]."""
    entries, rest = split_words(words, first)
    rate = PART_RATE_DIVISOR if rest is not None else RATE_DIVISOR
    names_filter = build(name_words(names, words), NAMES_RATE_DIVISOR) if names else None
    written = []
    more = None
    if rest is not None:
        rest_filter = build(rest, PART_RATE_DIVISOR)
        rest_text = words_js(rest_filter, source, rest=True, names=names_filter).encode("ascii")
        more_path = os.path.join(os.path.dirname(os.path.abspath(out)), MORE_NAME)
        with open(more_path, "wb") as handle:
            handle.write(rest_text)
        more = {"file": MORE_NAME, "v": version(rest_text), "count": rest_filter["count"]}
        written.append((more_path, rest_filter))
    words_filter = build(entries, rate)
    with open(out, "wb") as handle:
        handle.write(words_js(words_filter, source, more,
                              names=None if more else names_filter).encode("ascii"))
    written.insert(0, (out, words_filter))
    return written


def model_words(path):
    """The whole words of a GGUF model's WordPiece vocabulary, and the model's name."""
    import browser_model
    gguf = browser_model.read_gguf(path)
    pieces = browser_model.vocabulary(gguf)
    name = gguf["meta"].get("general.name")
    return ([piece for piece in pieces if not piece.startswith("##")],
            name if isinstance(name, str) and name else "the model")


def list_words(path):
    """The words of a text file: one a line (UTF-8, else Latin-1); a phrase (a line of
    several words) is left out."""
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return [line.strip() for line in text.splitlines()
            if line.strip() and len(line.split()) == 1]


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Write static/english-words.js (and "
                                                 "english-words-more.js), the words of the "
                                                 "English language for the search page.")
    parser.add_argument("--list", metavar="WORDS.txt", action="append", default=[],
                        help="take the words of a text file (again for more files)")
    parser.add_argument("--model", metavar="MODEL.gguf",
                        help="take the whole words of this model's WordPiece vocabulary too")
    parser.add_argument("--first", metavar="FIRST.txt",
                        help="write the list in two parts: the words of FIRST.txt that are in "
                             "it go with the page, the others to %s" % MORE_NAME)
    parser.add_argument("--names", metavar="NAMES.txt", action="append", default=[],
                        help="take the words with a capital first letter of a text file for "
                             "well-known names (\"did you mean\" leaves them alone)")
    parser.add_argument("--source", default="",
                        help="where the words are from, for the files' notes")
    parser.add_argument("--out", default=WORDS_FILE, help="the file to write")
    parser.add_argument("--check", nargs="+", metavar="WORD",
                        help="say whether words are words of the files (--out)")
    args = parser.parse_args(argv)
    if args.check:
        try:
            filters = read_files(args.out)
        except OSError as error:
            print("Cannot read the word list: %s" % error)
            return 2
        if filters is None:
            print("%s is not a word list of this module." % args.out)
            return 2
        for word in args.check:
            print("%s: %s" % (word, "a word" if is_word(filters, word)
                              else "a name" if is_name(filters, word) else "not a word"))
        return 0
    if not args.model and not args.list:
        parser.print_help()
        return 2
    words = []
    sources = []
    if args.model:
        found, label = model_words(args.model)
        words.extend(found)
        sources.append("the whole words of the vocabulary of %s" % label)
    for path in args.list:
        words.extend(list_words(path))
    if args.list:
        sources.append("a list of words" if len(args.list) == 1 else "lists of words")
    source = args.source or " and ".join(sources)
    if not all(32 <= ord(ch) < 127 for ch in source) or "*/" in source:
        print("--source must be plain ASCII (and no */).")
        return 2
    first = list_words(args.first) if args.first else None
    names = [word for path in args.names for word in list_words(path)]
    for path, words_filter in write_files(words, source, args.out, first, names):
        print("Wrote %s: %d words, %d bits (%d bytes)." % (path, words_filter["count"],
                                                           words_filter["size"],
                                                           os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
