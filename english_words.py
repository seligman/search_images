#!/usr/bin/env python3
"""The words of the English language that "Did you mean" leaves alone.

When a word of a search matches no picture, the search page offers a
spelling that has matches ("did you mean", static/search-core.js). A word
that is itself a word of the language ("cold", "words") is meant as typed:
offering another one that pictures have ("old", "woods") only gets in the
way. So the page knows the words of the language: static/english-words.js,
which this module writes.

It holds no words as text, only a Bloom filter of them: a bit array, each
word setting HASHES bits picked by two FNV-1a hashes of it (bit i of a word
is (h1 + i * h2) mod SIZE), so a word is known when all of its bits are set.
A word that is not in the list passes too, about once in RATE_DIVISOR
tries (a misspelling then gets no suggestion), never the other way. The
bits are written five to a character from an alphabet without vowels
(ALPHABET), so the file spells out no word that has a vowel; a string of
consonants, digits, "-" and "_" can still turn up by chance, so
tests/test_repo_hygiene.py checks it for private words like any other
file. It is less than half the size of the words as text (about 67 KB,
about 42 KB where the web host compresses it), and works the same in the
server's pages and in a published site, offline.

The words are the whole words of a WordPiece vocabulary of three letters
or more (letters a to z only): the one of mdbr-leaf-ir, the model that
search by meaning recommends, which is the uncased vocabulary of BERT
(about 21,000 words: everyday English and many common names, places
included). Any other list works too (one or more words per line):

    python english_words.py --model MODEL.gguf   (a BERT-family GGUF model)
    python english_words.py --list WORDS.txt
    python english_words.py --check WORD ...     (is it in the file?)

--source sets where the file's notes say the words are from (by default
the model's name, or "a list of words"); README.md (Credits) has the
command that made the file as shipped, credits included.

This module uses only the standard library.
"""

import argparse
import math
import os
import re
import sys
import textwrap

WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static",
                          "english-words.js")
FORMAT = 1
# Words shorter than this are never corrected anyway (see suggest() in
# static/search-core.js), so they are left out.
MIN_LETTERS = 3
# About one word in this many that is not in the list passes as one.
RATE_DIVISOR = 1000
# Five bits a character; no vowels (nor y), so no word can be spelled.
ALPHABET = "0123456789bcdfghjklmnpqrstvwxz-_"
SEED_1 = 2166136261             # FNV-1a's offset basis
SEED_2 = 1540483477             # another start, for the second hash
FNV_PRIME = 16777619
LINE = 92                       # characters of data per line of the file
_WORD = re.compile(r"^[a-z]+$")


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


def clean_words(words):
    """The words worth keeping, lower case, sorted, each once."""
    kept = set()
    for word in words:
        word = str(word).strip().lower()
        if len(word) >= MIN_LETTERS and _WORD.match(word):
            kept.add(word)
    return sorted(kept)


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


def words_js(words_filter, source):
    """The text of static/english-words.js for a filter; source says where its words
    are from (one line of plain ASCII)."""
    data = words_filter["data"]
    chunks = [data[start:start + LINE] for start in range(0, len(data), LINE)]
    about = textwrap.wrap("%d words of %d letters or more: %s. As a Bloom filter: %d bits, "
                          "%d of them per word, five to a character (see english_words.py, "
                          "which made this file)." % (words_filter["count"], MIN_LETTERS,
                                                      source, words_filter["size"],
                                                      words_filter["hashes"]), 76)
    lines = ["/* The words of the English language, for \"Did you mean\" in",
             "   static/search-core.js: a word that is one is never corrected to another."]
    lines.extend("   " + line for line in about)
    lines[-1] += " */"
    lines.append("var ENGLISH_WORDS = {format: %d, count: %d, size: %d, hashes: %d, data: ["
                 % (words_filter["format"], words_filter["count"], words_filter["size"],
                    words_filter["hashes"]))
    lines.append(",\n".join('    "%s"' % chunk for chunk in chunks))
    lines.append("].join(\"\")};")
    lines.append("")
    lines.append('if (typeof module === "object" && module && module.exports) {')
    lines.append("    module.exports = ENGLISH_WORDS;")
    lines.append("}")
    return "\n".join(lines) + "\n"


def read_js(text):
    """The filter in the text of an english-words.js file (words_js), or None."""
    head = re.search(r"format: (\d+), count: (\d+), size: (\d+), hashes: (\d+)", text)
    if not head:
        return None
    return {"format": int(head.group(1)), "count": int(head.group(2)),
            "size": int(head.group(3)), "hashes": int(head.group(4)),
            "data": "".join(re.findall(r'^    "([^"]*)"', text, re.M))}


def model_words(path):
    """The whole words of a GGUF model's WordPiece vocabulary, and the model's name."""
    import browser_model
    gguf = browser_model.read_gguf(path)
    pieces = browser_model.vocabulary(gguf)
    name = gguf["meta"].get("general.name")
    return ([piece for piece in pieces if not piece.startswith("##")],
            name if isinstance(name, str) and name else "the model")


def list_words(path):
    """The words of a text file (one or more a line)."""
    with open(path, encoding="utf-8-sig") as handle:
        return handle.read().split()


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Write static/english-words.js, the words "
                                                 "\"Did you mean\" never corrects.")
    parser.add_argument("--model", metavar="MODEL.gguf",
                        help="take the whole words of this model's WordPiece vocabulary")
    parser.add_argument("--list", metavar="WORDS.txt", help="take the words of a text file")
    parser.add_argument("--source", default="",
                        help="where the words are from, for the file's notes")
    parser.add_argument("--out", default=WORDS_FILE, help="the file to write")
    parser.add_argument("--check", nargs="+", metavar="WORD",
                        help="say whether words are in the file (--out)")
    args = parser.parse_args(argv)
    if args.check:
        with open(args.out, encoding="ascii") as handle:
            found = read_js(handle.read())
        if found is None:
            print("%s is not a word list of this module." % args.out)
            return 2
        for word in args.check:
            print("%s: %s" % (word, "a word" if contains(found, word) else "not a word"))
        return 0
    if bool(args.model) == bool(args.list):
        parser.print_help()
        return 2
    if args.model:
        words, label = model_words(args.model)
        source = args.source or "the whole words of the vocabulary of %s" % label
    else:
        words = list_words(args.list)
        source = args.source or "a list of words"
    if not all(32 <= ord(ch) < 127 for ch in source) or "*/" in source:
        print("--source must be plain ASCII (and no */).")
        return 2
    words_filter = build(words)
    with open(args.out, "w", encoding="ascii", newline="\n") as handle:
        handle.write(words_js(words_filter, source))
    print("Wrote %s: %d words, %d bits (%d bytes)." % (args.out, words_filter["count"],
                                                       words_filter["size"],
                                                       os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
