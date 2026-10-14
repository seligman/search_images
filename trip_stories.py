#!/usr/bin/env python3
"""Trip stories: a short written story of each trip (trips.py), a paragraph a day.

A language model writes it from the facts of the trip, which are all it is
given: text only, never a picture. The facts (facts_of, prompt_text):

  the trip's title and dates as the pictures in the facts tell them
  (trips.told_by: no hidden picture names the trip), its places (in the
  order visited); for each day
  its date, its places in order (trips.trip_places), the named people seen
  that day, how many pictures and videos, its drives (from and to, about
  how far and how long, the drive's own title and description) and its best
  pictures (best_items: at most the "pictures" setting, ranked as the trip
  page ranks them -- favorites, rating, the description's scores -- one of
  each stack of near-duplicates, spread over the day), each with a number,
  its time, kind, town, title, description (shortened), tags, event and
  the named people in it.

Never in the facts: hidden pictures; pictures that show private information
or are sensitive (search_index's pr, se), ID cards; people who have no name
or are hidden on the People page, and every name while People is off
(people.py); file names, folders, cameras, positions and database ids
(pictures go by their number in the facts); and home: no picture taken in
the home area -- trip_export.HomeArea of the trip export's home_km, never
less than its default -- or in a town with pictures there, and no drive
end there (its town is left out).

The model answers SCHEMA: {"days": [{"date", "text"}]}, a paragraph for
each day asked for. A picture's number in square brackets after the words
about it ("[4]") becomes "[#<image id>]" in the text kept (parse_reply),
which the trip page turns into a link to the picture; a number that day did
not give is dropped. So is any sentence that names a person the facts did
not name (People's other names, hidden people's too; a name as a name is
written: without_sentences), an unnamed one by People's label ("Person 12":
LABEL_RE) or a town of home.
The model is asked to name the people of a day in a sentence of their own:
where names may not be shown, those sentences are left out (without_names).

A long trip is written a few days at a time (plan_requests): each request
holds the facts of its days (and one line for each other day) within the
"prompt_tokens" setting, and asks for no more answer than the endpoint has
room for (a model server of local_server.py: its ctx_per_slot).

Stories are kept in the side table trip_stories of index.db (made on first
use), one row per trip by its key (trips.Trips.key: an away trip whose
first day moved finds its story by the dates it had, find_row), with what
it was written from (basis: the trip's dates, pictures and named people), the
model and what it cost (provenance), and whether the owner edited it or hid
it. state() tells whether a story is current, or out of date because the
trip changed much since (days added or gone, a quarter of its pictures
changed, other people named, a picture it cites hidden or gone): the job
writes those again unless the owner edited them (they are kept until the
owner asks for a new one).

Who sees a story:
  the admin    the trip page (/api/trips/<id>/story, story_view): as kept,
               with its state; edits it, hides it, writes it again.
  visitors     only a story not hidden, of a trip they may see; without the
               sentences that cite a picture hidden or gone since, and
               without the sentences that name people unless People is shown
               to them ("visitors" of config.json's "people") -- and then
               only the story's people who are still named and not hidden
               on the People page (_unwanted_names: not a name since
               changed, cleared or deleted with the face data).
  an export    of a trip (trip_export.py) only with its "story" option (off
               by default) and its location option (the story names the
               trip's places), never when hidden (export_days): the same
               sentences out, names only with "people_names" and only of
               people the export publishes (export.published_names: a
               person none of whose pictures it publishes is not named),
               also the sentences naming a town of the export's home area
               or citing a picture it does not publish or publishes as at
               home; the links go to the export's picture ids.

The job "trip_stories" (jobs.py; python trips.py --stories [--trip ID]
[--redo]; the Trips page) writes the stories that are missing or out of
date (not edited) of every trip that is not hidden, or of one trip; with
redo also the current ones (one trip: even when edited; every trip: all
but the edited ones). A story the owner edits while the job runs is
kept (_keep). A trip some of whose answers cannot be used keeps the
paragraphs of the others; the job fails only when no story could be
written. Each trip goes to its endpoint (endpoint_for: the
"endpoint" setting, else the refine endpoint of the trip's libraries, else
their describe endpoint), through local_server.py's lease of model servers
(it starts them, and stops the job before their stop_at time: the job can
be resumed). A cloud endpoint gets the facts as text (estimate: about how
many tokens and what they cost, logged first and shown by the pages before
a job starts), without the names of people: names go only to an endpoint on
this computer or the local network (common.is_local_endpoint; People's
names are never sent anywhere else), so a story a cloud endpoint writes
names nobody; hidden pictures are never in the facts, so never sent.

Settings (config.json "trip_stories"; see SETTINGS.md): "endpoint",
"pictures" (best pictures a day in the facts), "prompt_tokens" (the most
a request's prompt may hold, estimated) and "notes" (the owner's words
added to the prompt: who the people are, the language, the voice).

This module uses only the standard library.
"""

import argparse
import collections
import concurrent.futures
import datetime
import hashlib
import json
import math
import re
import sqlite3
import sys
import threading
import time

import common
import jobs
import llm
import search_index
import trips

FORMAT = 1
DEFAULTS = {"endpoint": "", "pictures": 6, "prompt_tokens": 6000, "notes": ""}
# (low, high) of the number settings.
RANGES = {"pictures": (1, 24), "prompt_tokens": (1000, 200000)}
NOTES_LENGTH = 1000
# Characters of a picture's title and description in the facts.
TITLE_CHARS = 100
DESCRIPTION_CHARS = 260
TAGS_PER_PICTURE = 8
# Characters of a day's paragraph as kept (a model's, or the owner's).
TEXT_CHARS = 3000
# Characters per token of the facts and prompt, for estimates (measured with the
# tokenizer of a local model on real trips' prompts: 3.2 to 3.8, most near 3.7; a little
# less, so that a request rather holds less than the budget than more).
CHARS_PER_TOKEN = 3.5
# Tokens of an answer per day (a paragraph of up to four sentences, in JSON), for
# estimates and for how many days one request asks for.
ANSWER_TOKENS_PER_DAY = 220
ANSWER_TOKENS_BASE = 60
# The most answer tokens a request asks for, and the least room a request needs.
MAX_ANSWER_TOKENS = 4000
MIN_ANSWER_TOKENS = 1000
# Days written in one request at most.
MAX_DAYS_PER_REQUEST = 10
# Requests to one endpoint at once at most (each a whole trip, its requests in order).
MAX_WORKERS = 4
MAX_RETRIES = 4
# Seconds to wait for a local server that is still loading its model.
STARTUP_WAIT = 180.0
# A story is out of date when this share of the pictures it was written from changed.
CHANGED_SHARE = 0.25
# Kinds of pictures that are never among the best (as static/trips.js), documents
# that are private, and quality flags that put a picture after the others.
NOT_BEST_TYPES = ("screenshot", "document", "scan", "diagram", "meme")
PRIVATE_DOCUMENTS = ("card_or_id",)
DEMOTING_FLAGS = ("accidental", "screen_photo")
FLAW_COST = {"blurry": 15, "dark": 6, "overexposed": 6}
PIXELS_CAP = 12000000
WIDE_ASPECT = 2.2
ID_CHUNK = 500

TABLE_SQL = """CREATE TABLE IF NOT EXISTS trip_stories (
    trip_key TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    span_start TEXT NOT NULL DEFAULT '',
    span_end TEXT NOT NULL DEFAULT '',
    days TEXT NOT NULL DEFAULT '[]',
    basis TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT '{}',
    edited INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    made REAL NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0
)"""

SYSTEM_PROMPT = """You write the story of a trip for a family photo album, from facts about it.

Rules:
- Use only the facts given. Never add anything they do not say: no other places, people, \
names, events, weather, food, feelings or reasons.
- Write one paragraph for each day you are asked for: two to four sentences (one or two \
when the day has few facts), in the past tense, in plain words.
- Say where the day went (its places, in order), what happened, and its highlights. A long \
drive is worth a mention.
- Name people only as the facts name them, and only on the days the facts list them. Name \
them in a short sentence of their own, such as "<name> and <name> were there.", never in \
the sentences about places and highlights.
- After the words about a picture, write its number in square brackets, like [4]. Use \
only the numbers of that day, one or two per sentence at most.
- Never mention the numbers otherwise, nor files, cameras, the facts or these rules, nor \
that something is a photo or a video. No title, no headings, no lists.
- Vary how the paragraphs begin.

Answer with JSON only: {"days": [{"date": "YYYY-MM-DD", "text": "the paragraph"}]}, one \
entry for each day asked for, in order."""

SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"date": {"type": "string"}, "text": {"type": "string"}},
                "required": ["date", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["days"],
    "additionalProperties": False,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A picture's number in a reply ("[4]", "[4, 7]", "[#4]").
_REF_RE = re.compile(r"\[\s*(?:#|(?:pictures?|photos?|images?|no\.?)\s*)?\s*"
                     r"(\d{1,6}(?:\s*(?:,|and|&)\s*#?\s*\d{1,6})*)\s*\]", re.IGNORECASE)
# A picture's link in a kept text: [#<image id>] (an export's: [#<export id>]).
MARKER_RE = re.compile(r"\[#([0-9A-Za-z]{1,40})\]")
# The end of a sentence: its mark, closing quotes or brackets, and the links after it.
_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'\u201d\u2019)]*(?:\s*\[#[0-9A-Za-z]{1,40}\])*"
                              r"(?=\s|$)")
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
# A month or weekday after these is no person ("in June", "on Monday", "10 June").
_CALENDAR_BEFORE_RE = re.compile(
    r"(?:\d|\b(?:in|on|of|by|during|early|late|mid|until|till|since|last|next|this|that|"
    r"every|each|from|to))[\s,.\-]*$", re.IGNORECASE)
_CALENDAR_AFTER_RE = re.compile(r"[\s,.\-]*\d")
_MARKDOWN_RE = re.compile(r"(\*\*|__|^#+\s*|^\s*[-*]\s+)", re.MULTILINE)
# People's label of a person without a name (people_groups.label): never in a story.
LABEL_RE = re.compile(r"(?<!\w)Person \d+(?!\w)")
_SPACES_RE = re.compile(r"[ \t\r\n]+")


class StoryError(ValueError):
    """A change the owner asked for that cannot be made: status 400 or 404."""

    def __init__(self, message, status=400):
        ValueError.__init__(self, message)
        self.status = status


# ---------------------------------------------------------------------------
# Settings

def settings(config):
    """config.json "trip_stories" checked, with the defaults (DEFAULTS; numbers held to
    RANGES): {"endpoint", "pictures", "prompt_tokens", "notes"}."""
    stored = config.get("trip_stories") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    result = dict(DEFAULTS)
    if isinstance(stored.get("endpoint"), str):
        result["endpoint"] = stored["endpoint"].strip()
    for key, (low, high) in RANGES.items():
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(low, min(high, value))
    notes = stored.get("notes")
    if isinstance(notes, str):
        result["notes"] = _SPACES_RE.sub(" ", _CONTROL_RE.sub(" ", notes)).strip()[:NOTES_LENGTH]
    return result


def _with_model(endpoints):
    return [endpoint for endpoint in endpoints if endpoint.get("model")]


def endpoint_for(config, trip):
    """(endpoint, how it was chosen) that writes a trip's story, or (None, why there is
    none): the "endpoint" setting, else the refine endpoint of the trip's libraries
    (its own first), else their describe endpoint; the first enabled endpoint with a
    model of a group."""
    wanted = settings(config)["endpoint"]
    if wanted:
        found = _with_model(common.resolve_endpoints(config, wanted))
        if found:
            return found[0], "the trip_stories endpoint"
        return None, ("No enabled endpoint with a model is called %s (\"trip_stories\": "
                      "\"endpoint\" in config.json)." % wanted)
    import refine
    counts = collections.Counter(picture.library for picture in trip.pictures)
    order = list(trip.libraries) + [library_id for library_id, _count in counts.most_common()
                                    if library_id not in trip.libraries]
    libraries = [library for library in (common.get_library(config, library_id)
                                         for library_id in order)
                 if library is not None and library.get("enabled", True)]
    for library in libraries:
        name = refine.settings(library)["endpoint"]
        found = _with_model(common.resolve_endpoints(config, name)) if name else []
        if found:
            return found[0], "the refine endpoint of %s" % common.library_label(library)
    for library in libraries:
        found = _with_model(common.endpoints_for_library(config, library))
        if found:
            return found[0], "the endpoint of %s" % common.library_label(library)
    return None, ("No endpoint can write the story: choose a refine endpoint for the trip's "
                  "library on the Manage page, or set \"trip_stories\": {\"endpoint\": ...} "
                  "in config.json.")


def answer_room(config, endpoint):
    """(answer tokens a request asks for, the most prompt tokens it may hold) for an
    endpoint: MAX_ANSWER_TOKENS and the "prompt_tokens" setting, less when the model
    server of local_server.py that serves it has a smaller context a request."""
    import local_server
    budget = settings(config)["prompt_tokens"]
    answer = max(MAX_ANSWER_TOKENS, common.endpoint_settings(endpoint)["max_output_tokens"])
    answer = min(answer, MAX_ANSWER_TOKENS * 2)
    server = local_server.managed(config, endpoint.get("name", ""))
    if server is not None and server.get("kind", "describe") == "describe":
        context = server["ctx_per_slot"]
        answer = max(MIN_ANSWER_TOKENS, min(answer, context // 3))
        budget = max(MIN_ANSWER_TOKENS, min(budget, context - answer - 256))
    return answer, budget


# ---------------------------------------------------------------------------
# Text

def estimate_tokens(text):
    """About how many tokens a text is (CHARS_PER_TOKEN)."""
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


def sentences(text):
    """The sentences of a paragraph (each with its end mark, closing quotes and the picture
    links after it)."""
    parts, start = [], 0
    for match in _SENTENCE_END_RE.finditer(text):
        parts.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _word_pattern(words, ignore_case):
    words = sorted({word for word in words if word}, key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(word) for word in words),
                      re.IGNORECASE if ignore_case else 0)


def name_words(names):
    """The words that may name these people: each name as given, and its first word (a
    given name: "Anna Smith" is also "Anna") when that has two letters or more; not its
    other words (a surname such as Hill or Park is an everyday word too)."""
    words = set()
    for name in names or ():
        name = _SPACES_RE.sub(" ", name or "").strip()
        if not name:
            continue
        words.add(name)
        first = name.split(" ")[0].strip(".,;:")
        if len(first) >= 2:
            words.add(first)
    return words


def _calendar_word(word):
    """True for the name of a month or a day of the week (some are people's names too:
    April, May, June)."""
    return word.casefold() in {item.casefold() for item in trips.MONTHS + WEEKDAYS}


def _names_someone(sentence, name_re):
    """True when a sentence names someone name_re finds: a match written as a name is
    (its first letter not a small one: "the hill" names no Anna Hill), but for a month or
    day of the week next to a number or after "in", "on" ... ("10 June", "in May")."""
    for match in name_re.finditer(sentence):
        word = match.group(0)
        if word[:1].islower():
            continue
        if _calendar_word(word) and (_CALENDAR_BEFORE_RE.search(sentence[:match.start()])
                                     or _CALENDAR_AFTER_RE.match(sentence[match.end():])):
            continue
        return True
    return False


def without_sentences(text, names=(), places=(), ids=(), keep_names=()):
    """text without its sentences that name one of names (name_words; in any case, but
    only as a name is written: _names_someone) or an unnamed person by People's label
    ("Person 12": LABEL_RE), one of places (as written) or link one of ids (picture links
    [#id]). keep_names: names that may be named (their words are not looked for: "Anna"
    of an Anna Smith who may be named stays when an Anna Jones may not)."""
    if not text:
        return ""
    name_re = _word_pattern(name_words(names) - name_words(keep_names), True)
    place_re = _word_pattern(places, False)
    unwanted = {str(item) for item in ids}
    kept = []
    for sentence in sentences(text):
        if name_re is not None and _names_someone(sentence, name_re):
            continue
        if LABEL_RE.search(sentence):
            continue
        if place_re is not None and place_re.search(MARKER_RE.sub(" ", sentence)):
            continue
        if unwanted and any(link in unwanted for link in MARKER_RE.findall(sentence)):
            continue
        kept.append(sentence)
    return " ".join(kept)


def map_links(text, mapping):
    """text with each picture link [#id] changed to [#mapping[id]] (mapping's keys: image
    ids), or taken out when mapping has none for it."""
    def change(match):
        key = match.group(1)
        target = mapping.get(int(key)) if key.isdigit() else None
        if target is None:
            target = mapping.get(key)
        return "[#%s]" % target if target is not None else ""
    return _tidy(MARKER_RE.sub(change, text or ""))


def _tidy(text):
    """text with runs of spaces made one, and none before a mark that ends a clause."""
    return re.sub(r"\s+([.,;:!?])", r"\1", _SPACES_RE.sub(" ", text)).strip()


def links(text):
    """The ids of the picture links [#id] of a text, in order (numbers as int)."""
    found = []
    for value in MARKER_RE.findall(text or ""):
        value = int(value) if value.isdigit() else value
        if value not in found:
            found.append(value)
    return found


def clean_text(value, limit=TEXT_CHARS):
    """A paragraph as kept: one line, no Markdown, control characters or runs of spaces,
    at most limit characters (cut at the end of a sentence, else a word)."""
    text = value if isinstance(value, str) else ""
    text = _MARKDOWN_RE.sub("", _CONTROL_RE.sub(" ", text))
    text = _SPACES_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [match.end() for match in _SENTENCE_END_RE.finditer(cut)]
    if ends and ends[-1] > limit // 2:
        return cut[:ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"


def _shorten(text, limit):
    """text cut to at most limit characters at a sentence end, else a word (with an
    ellipsis)."""
    text = _SPACES_RE.sub(" ", _CONTROL_RE.sub(" ", text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[:end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"


# ---------------------------------------------------------------------------
# The best pictures of a day (as static/trips.js ranks them)

def usable(record):
    """True when a picture's words may be in the facts: not hidden, private or
    sensitive, nor an ID card."""
    return (isinstance(record, dict) and not record.get("h") and not record.get("pr")
            and record.get("se") in (None, "", "none")
            and record.get("dt") not in PRIVATE_DOCUMENTS)


def eligible(record):
    """True when a picture may be among a day's best (usable, and no screenshot,
    document, scan, diagram or meme)."""
    return (usable(record) and record.get("ty") not in NOT_BEST_TYPES
            and record.get("vt") not in NOT_BEST_TYPES)


def _demoted(record):
    if record.get("fav") or record.get("rt"):
        return False
    return (bool(record.get("dt")) and record.get("dt") != "none") or any(
        flag in DEMOTING_FLAGS for flag in record.get("qf") or ())


def _quality(record):
    value = (record.get("q") or 0) * 10 + (record.get("sh") or 0)
    return value - sum(FLAW_COST.get(flag, 0) for flag in record.get("qf") or ())


def rank_key(record):
    """The order of the best (static/trips.js compareRank): favorites, rating, not
    demoted, the description's scores, in an album, pixels (up to PIXELS_CAP), id."""
    pixels = min(PIXELS_CAP, (record.get("w") or 0) * (record.get("hgt") or 0))
    return (0 if record.get("fav") else 1, -(record.get("rt") or 0),
            1 if _demoted(record) else 0, -_quality(record),
            0 if record.get("al") else 1, -pixels, record["i"])


def _moment(record):
    try:
        return datetime.datetime.strptime((record.get("ts") or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def best_items(records, count):
    """The best count of a day's records (eligible ones; clips of drives are not among
    them), in the order taken: by rank_key, one of each stack of near-duplicates (dg),
    and spread over the day (as static/trips.js: one taken within SAME_MOMENT_SECONDS of
    one chosen is left out, one within SPREAD_SECONDS waits until the others had their
    turn)."""
    ranked = sorted((record for record in records
                     if eligible(record) and record.get("dv") is None), key=rank_key)
    chosen, waiting, stacks = [], [], set()

    def gap(record):
        at = _moment(record)
        if at is None:
            return None
        gaps = [abs((at - _moment(other)).total_seconds()) for other in chosen
                if _moment(other) is not None]
        return min(gaps) if gaps else None

    for record in ranked:
        if len(chosen) >= count:
            break
        stack = record.get("dg")
        if stack is not None and stack in stacks:
            continue
        seconds = gap(record)
        if seconds is not None and seconds < trips.SAME_MOMENT_SECONDS:
            continue
        if seconds is not None and seconds < trips.SPREAD_SECONDS:
            waiting.append(record)
            continue
        chosen.append(record)
        if stack is not None:
            stacks.add(stack)
    for record in waiting:
        if len(chosen) >= count:
            break
        stack = record.get("dg")
        seconds = gap(record)
        if (stack is not None and stack in stacks) or (
                seconds is not None and seconds < trips.SAME_MOMENT_SECONDS):
            continue
        chosen.append(record)
        if stack is not None:
            stacks.add(stack)
    chosen.sort(key=lambda record: (record.get("ts") or "", record["i"]))
    return chosen


# ---------------------------------------------------------------------------
# People, home, drives

def all_names(hidden_only=False):
    """Every name given to a person on the People page (hidden people's too; hidden_only:
    only theirs); [] without face data."""
    import people
    try:
        store = people.connect(create=False)
    except Exception:
        return []
    if store is None:
        return []
    try:
        return [row[0] for row in store.execute(
            "SELECT name FROM people WHERE name != ''%s" % (" AND hidden = 1" if hidden_only
                                                            else ""))]
    except sqlite3.Error:
        return []
    finally:
        store.close()


def _unwanted_names(story, names_allowed):
    """(names whose sentences go, names that may stay). Unless names_allowed every name
    goes (the story's people's and People's); with it (True, or the names that may be
    named: an export's published names) the story's people may stay who are still named
    so and not hidden on the People page now -- a person hidden, renamed or left
    without a name since, or deleted with the face data (people.py --delete-all), is
    not named -- and, given names, are among them; every other name goes."""
    basis = set(story["basis"].get("names") or ())
    named = set(all_names())
    keep = set()
    if names_allowed:
        keep = (basis & named) - set(all_names(hidden_only=True))
        if not isinstance(names_allowed, bool):
            keep &= set(names_allowed)
    return (basis | named) - keep, keep


def named_people(conn, config, ids):
    """{image id: [names]} of the named people who are not hidden in some pictures, while
    People is on (people.py); {} otherwise."""
    import people
    if not ids or not people.settings(config)["enabled"]:
        return {}
    try:
        store = people.connect(create=False)
    except Exception:
        return {}
    if store is None:
        return {}
    by_key = collections.defaultdict(list)
    try:
        ids = sorted(ids)
        for start in range(0, len(ids), ID_CHUNK):
            part = ids[start:start + ID_CHUNK]
            for row in conn.execute(
                    "SELECT id, content_hash, media, projection, has_preview FROM images "
                    "WHERE id IN (%s) AND content_hash != ''" % ",".join("?" * len(part)),
                    part):
                kind = people.looked_at(row)
                if kind is not None:
                    by_key[(row["content_hash"], kind)].append(row["id"])
        hashes = sorted({key[0] for key in by_key})
        found = collections.defaultdict(set)
        for start in range(0, len(hashes), ID_CHUNK):
            part = hashes[start:start + ID_CHUNK]
            for content_hash, kind, name in store.execute(
                    "SELECT f.content_hash, f.kind, p.name FROM faces f JOIN face_people fp ON "
                    "fp.face_id = f.id JOIN people p ON p.id = fp.person_id WHERE p.name != '' "
                    "AND p.hidden = 0 AND f.content_hash IN (%s) AND f.id NOT IN (SELECT "
                    "face_id FROM face_marks WHERE not_face)" % ",".join("?" * len(part)),
                    part):
                for image_id in by_key.get((content_hash, kind), ()):
                    found[image_id].add(name)
        return {image_id: sorted(names, key=str.casefold) for image_id, names in found.items()}
    except sqlite3.Error:
        return {}
    finally:
        store.close()


class Home:
    """The home of the trips as a story keeps it out: the area (trip_export.HomeArea of
    km) and the towns with pictures there."""

    def __init__(self, conn, home, km, towns=None):
        import trip_export
        self.area = trip_export.HomeArea(home, km) if home is not None and km else None
        self.towns = trip_export.home_towns(conn, self.area) if towns is None else set(towns)
        self.town_names = sorted({town[0] for town in self.towns if town[0]})

    def holds(self, picture):
        """True for a trip's picture (trips._Picture) taken at home (see
        trip_export.near_home)."""
        import trip_export
        return trip_export.near_home(picture, self.area, self.towns)


def home_km(built, trip):
    """The size of the home area a trip's story keeps out: its export's home_km, never
    less than trip_export.HOME_KM."""
    import trip_export
    saved = built.export_options(trip).get("home_km")
    km = saved if isinstance(saved, int) and not isinstance(saved, bool) else 0
    return max(trip_export.HOME_KM, km if km in trip_export.HOME_KM_STEPS else 0)


class _Homes:
    """Home objects by km, made once each."""

    def __init__(self, conn, built):
        self.conn = conn
        self.built = built
        self._made = {}

    def of(self, trip):
        km = home_km(self.built, trip)
        if km not in self._made:
            self._made[km] = Home(self.conn, self.built.home, km)
        return self._made[km]


def _drive_lengths(conn, clip_ids):
    """{clip id: km driven} from the clips' GPS tracks (tracks.py)."""
    import places
    import tracks
    if not clip_ids:
        return {}
    rows = []
    ids = sorted(clip_ids)
    for start in range(0, len(ids), ID_CHUNK):
        part = ids[start:start + ID_CHUNK]
        rows.extend(conn.execute("SELECT id, content_hash, media FROM images WHERE id IN (%s)"
                                 % ",".join("?" * len(part)), part).fetchall())
    result = {}
    for image_id, facts in tracks.for_rows(conn, rows).items():
        points = facts.points
        result[image_id] = sum(places.distance_km(points[index][1], points[index][2],
                                                  points[index + 1][1], points[index + 1][2])
                               for index in range(len(points) - 1))
    return result


# ---------------------------------------------------------------------------
# The facts of a trip

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _weekday_words(date):
    day = trips._date(date)
    return "%s %d %s %d" % (WEEKDAYS[day.weekday()], day.day, trips.MONTHS[day.month - 1],
                            day.year)


def _kind_words(record):
    seconds = record.get("dr") or 0
    length = ""
    if record.get("mv") and seconds:
        length = (" (%d min)" % round(seconds / 60.0) if seconds >= 90
                  else " (%d s)" % round(seconds))
    if record.get("p") == "360":
        return ("360-degree video" if record.get("mv") else "360-degree photo") + length
    if record.get("mv"):
        return "video" + length
    width, height = record.get("w") or 0, record.get("hgt") or 0
    if width and height and width / float(height) > WIDE_ASPECT:
        return "panorama"
    return "photo"


def _minutes_words(minutes):
    minutes = int(round(minutes))
    if minutes < 60:
        return "%d min" % max(1, minutes)
    return "%d h %02d min" % (minutes // 60, minutes % 60)


class Facts:
    """What a trip's story is written from (facts_of): .days [{date, number, words,
    places, people, pictures, videos, items, drives}], .refs {number: image id},
    .basis (see basis_of), and the trip's title, dates, places and people (facts_of: as
    the pictures in the facts tell them, trips.told_by)."""

    def __init__(self, trip):
        self.trip_id = trip.id
        self.title = trip.title
        self.start = trip.start or ""
        self.end = trip.end or ""
        self.days = []
        self.refs = {}
        self.places = []
        self.people = []
        self.basis = {}
        self.home_towns = []         # (towns of home: never named in a paragraph)

    def day(self, date):
        for day in self.days:
            if day["date"] == date:
                return day
        return None

    @property
    def dates(self):
        return [day["date"] for day in self.days]


def basis_of(trip, home, names_by_id):
    """What a story is written from, to tell later whether the trip changed much: {"dates"
    (the days with a picture that is not hidden nor at home), "ids" (those pictures),
    "names" (the named people in them)}."""
    kept = [picture for picture in trip.pictures
            if picture.day and not picture.hidden and not home.holds(picture)]
    names = set()
    for picture in kept:
        names.update(names_by_id.get(picture.id, ()))
    return {"dates": sorted({picture.day for picture in kept}),
            "ids": sorted(picture.id for picture in kept),
            "names": sorted(names, key=str.casefold)}


def facts_of(conn, config, trip, records, names_by_id, home, drive_map=None, lengths=None,
             per_day=None):
    """The Facts of a trip: from records ({image id: search record}, without hidden
    pictures), names_by_id (named_people), home (Home), drive_map ({clip id: its
    drives.Drive}) and lengths (_drive_lengths). per_day: best pictures a day (default:
    the "pictures" setting)."""
    per_day = per_day or settings(config)["pictures"]
    drive_map = drive_map or {}
    lengths = lengths or {}
    facts = Facts(trip)
    facts.basis = basis_of(trip, home, names_by_id)
    facts.home_towns = list(home.town_names)
    pictured = {item.id: item for item in trip.pictures}
    by_day = collections.OrderedDict()
    for picture in trip.pictures:
        if not picture.day or picture.hidden or home.holds(picture):
            continue
        record = records.get(picture.id)
        if not usable(record):
            continue
        by_day.setdefault(picture.day, []).append((picture, record))
    # The trip's dates and title as the pictures in the facts tell them (trips.told_by):
    # its title made from the places of all its pictures could name the place of a
    # hidden one, or home (the owner's or the library's title stays).
    facts.start, facts.end, facts.title = trips.told_by(
        trip, [picture for members in by_day.values() for picture, _record in members])
    trip_people = collections.Counter()
    placed = []
    ref = 0
    for date, members in by_day.items():
        pictures = [picture for picture, _record in members]
        day_records = [record for _picture, record in members]
        located = [picture for picture in pictures if not picture.flying]
        placed.extend(located)
        people_here = collections.Counter()
        for picture in pictures:
            for name in names_by_id.get(picture.id, ()):
                people_here[name] += 1
        trip_people.update(people_here)
        day = {"date": date, "number": trips._days_between(facts.start, date) + 1
               if facts.start else 1,
               "words": _weekday_words(date),
               "places": [place["name"] for place in trips.trip_places(located, limit=6)],
               "people": sorted(people_here, key=lambda name: (-people_here[name],
                                                                name.casefold())),
               "pictures": len(members),
               "videos": sum(1 for record in day_records if record.get("mv")),
               "items": [], "drives": []}
        for record in best_items(day_records, per_day):
            ref += 1
            facts.refs[ref] = record["i"]
            place = record.get("ci") or record.get("rg") or ""
            day["items"].append({
                "ref": ref, "id": record["i"],
                "time": (record.get("ts") or "")[11:16],
                "kind": _kind_words(record), "place": place,
                "title": _shorten(record.get("t") or "", TITLE_CHARS),
                "text": _shorten(record.get("d") or "", DESCRIPTION_CHARS),
                "tags": [tag for tag in (record.get("g") or [])][:TAGS_PER_PICTURE],
                "event": record.get("ev") or "",
                "people": list(names_by_id.get(record["i"], ()))})
        # The drives of the day: one entry each (the clips of a drive are no picture of
        # the best), from and to (not at home), about how far and how long.
        seen = set()
        clips_of_day = {picture.id for picture in pictures}
        for picture, record in members:
            drive = drive_map.get(picture.id)
            if drive is None or drive.key in seen:
                continue
            seen.add(drive.key)
            if drive.hidden:
                continue
            clips = [clip for clip in drive.clips if clip.id in records]
            if not clips:
                continue
            towns = [records[clip.id].get("ci") for clip in clips
                     if records[clip.id].get("ci")]
            ends, at_home = [], set()
            for clip in clips:
                item = pictured.get(clip.id)
                if (item is not None and home.holds(item)) or (
                        records[clip.id].get("ci") in home.town_names):
                    at_home.add(clip.id)
            # (How far and how long: not what was filmed at home, which would tell how far
            # from home the drive went.)
            away = [clip for clip in clips if clip.id not in at_home]
            for clip, town in ((clips[0], towns[0] if towns else ""),
                               (clips[-1], towns[-1] if towns else "")):
                item = pictured.get(clip.id)
                if item is not None and home.holds(item):
                    town = ""
                if town and town in home.town_names:
                    town = ""
                ends.append(town)
            # The drive's own words: not when it was filmed at home too (they may tell of
            # it), nor when private or sensitive.
            text_record = next((records[clip.id] for clip in clips
                                if records[clip.id].get("dvt") or records[clip.id].get("dvd")),
                               {})
            if at_home or text_record.get("dvp") or text_record.get("dvs") not in (
                    None, "", "none"):
                text_record = {}
            # (Its link: its first clip of the day that may be told of.)
            linked = next(clip for clip in clips if clip.id in clips_of_day)
            ref += 1
            facts.refs[ref] = linked.id
            day["drives"].append({
                "ref": ref, "id": linked.id, "from": ends[0], "to": ends[1],
                "km": round(sum(lengths.get(clip.id, 0.0) for clip in away)),
                "minutes": sum(clip.duration or 0 for clip in away) / 60.0,
                "title": _shorten(text_record.get("dvt") or "", TITLE_CHARS),
                "text": _shorten(text_record.get("dvd") or "", DESCRIPTION_CHARS)})
        facts.days.append(day)
    facts.places = [place["name"] for place in trips.trip_places(placed)]
    facts.people = [(name, count) for name, count in sorted(
        trip_people.items(), key=lambda item: (-item[1], item[0].casefold()))]
    return facts


def _item_line(item):
    bits = ["[%d]" % item["ref"]]
    head = ", ".join(part for part in (item["time"], item["kind"], item["place"]) if part)
    line = bits[0] + " " + head + ":"
    if item["title"]:
        line += ' "%s".' % item["title"].rstrip(".")
    if item["text"]:
        line += " " + item["text"]
    if item["event"] and item["event"] not in ("none", "other"):
        line += " Event: %s." % item["event"].replace("_", " ")
    if item["tags"]:
        line += " Tags: %s." % ", ".join(item["tags"])
    if item["people"]:
        line += " With %s." % trips._join(item["people"])
    return line


def _drive_line(drive):
    route = ""
    if drive["from"] and drive["to"]:
        route = ("around %s" % drive["to"] if drive["from"] == drive["to"]
                 else "from %s to %s" % (drive["from"], drive["to"]))
    elif drive["to"]:
        route = "to %s" % drive["to"]
    elif drive["from"]:
        route = "from %s" % drive["from"]
    sizes = []
    if drive["km"] >= 1:
        sizes.append("about %d km" % drive["km"])
    if drive["minutes"] > 0:
        sizes.append(_minutes_words(drive["minutes"]))
    line = "[%d] a drive" % drive["ref"]
    if route:
        line += " " + route
    if sizes:
        line += " (%s)" % ", ".join(sizes)
    line += ":"
    if drive["title"]:
        line += ' "%s".' % drive["title"].rstrip(".")
    if drive["text"]:
        line += " " + drive["text"]
    return line


def day_text(day):
    """The facts of one day, as the prompt gives them."""
    lines = ["Day %d, %s (date %s)" % (day["number"], day["words"], day["date"])]
    if day["places"]:
        lines.append("Places, in order: %s" % ", ".join(day["places"]))
    if day["people"]:
        lines.append("People: %s" % ", ".join(day["people"]))
    count = "Pictures: %d" % day["pictures"]
    if day["videos"]:
        count += " (%d of them videos)" % day["videos"]
    lines.append(count)
    for drive in day["drives"]:
        lines.append(_drive_line(drive))
    for item in day["items"]:
        lines.append(_item_line(item))
    return "\n".join(lines)


def prompt_text(facts, dates, notes=""):
    """The user text of a request that asks for the paragraphs of dates (some of the
    facts' days): the trip, one line for each other day, the facts of those days."""
    wanted = [day for day in facts.days if day["date"] in dates]
    lines = ["Trip: %s" % facts.title]
    if facts.start:
        count = trips._days_between(facts.start, facts.end) + 1
        lines.append("Dates: %s (%d day%s)" % (trips.span_words(facts.start, facts.end), count,
                                                "" if count == 1 else "s"))
    if facts.places:
        lines.append("Places of the trip, in order: %s" % ", ".join(facts.places))
    if facts.people:
        lines.append("People named in the trip: %s" % ", ".join(name for name, _ in
                                                                facts.people))
    if notes:
        lines.append("Notes from the album's owner: %s" % notes)
    others = [day for day in facts.days if day["date"] not in dates]
    if others:
        lines.append("")
        lines.append("The other days of the trip (already written; do not write them):")
        for day in others:
            lines.append("Day %d (%s): %s" % (day["number"], day["date"],
                                              ", ".join(day["places"]) or "no place known"))
    for day in wanted:
        lines.append("")
        lines.append(day_text(day))
    lines.append("")
    lines.append("Write the paragraphs of these days: %s." % ", ".join(
        day["date"] for day in wanted))
    return "\n".join(lines)


def plan_requests(facts, budget, answer_tokens, notes=""):
    """The days of each request (lists of dates, in order) so that each prompt holds at
    most about budget tokens and its answer fits answer_tokens."""
    most = max(1, min(MAX_DAYS_PER_REQUEST,
                      (answer_tokens - ANSWER_TOKENS_BASE) // ANSWER_TOKENS_PER_DAY))
    base = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(prompt_text(facts, [], notes))
    requests, current, used = [], [], base
    for day in facts.days:
        size = estimate_tokens(day_text(day)) + 4
        if current and (used + size > budget or len(current) >= most):
            requests.append(current)
            current, used = [], base
        current.append(day["date"])
        used += size
    if current:
        requests.append(current)
    return requests


def estimate(facts, config, endpoint):
    """{"requests", "tokens_in", "tokens_out", "cost" (None without prices)} of writing a
    story from these facts with an endpoint (estimated)."""
    answer, budget = answer_room(config, endpoint)
    notes = settings(config)["notes"]
    plan = plan_requests(facts, budget, answer, notes)
    tokens_in = sum(estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(prompt_text(facts, dates,
                                                                                 notes))
                    for dates in plan)
    tokens_out = sum(ANSWER_TOKENS_BASE + ANSWER_TOKENS_PER_DAY * len(dates) for dates in plan)
    return {"requests": len(plan), "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost": llm.estimate_cost(endpoint, {"input_tokens": tokens_in,
                                                 "output_tokens": tokens_out})}


# ---------------------------------------------------------------------------
# The answer

def parse_reply(text, facts, dates, other_names=()):
    """({date: paragraph}, [problems]) of a reply asking for dates: each paragraph
    cleaned (clean_text), its picture numbers made links ([#<image id>]: only those of
    its day; others dropped), without the sentences naming other_names (people the
    facts did not name), an unnamed person ("Person 12") or a town of home. Raises
    ValueError when the reply holds no JSON object."""
    try:
        data = llm.extract_json(text)
    except ValueError:
        data = llm.repair_json(text)
    entries = data.get("days") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("the reply has no days")
    problems = []
    result = {}
    wanted = set(dates)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date = entry.get("date")
        date = date.strip() if isinstance(date, str) else ""
        if date not in wanted:
            if date:
                problems.append("a paragraph for %s, which was not asked for" % date)
            continue
        if date in result:
            continue
        day = facts.day(date)
        allowed = {item["ref"]: item["id"] for item in day["items"] + day["drives"]}

        def link(match, allowed=allowed):
            numbers = [int(number) for number in re.findall(r"\d+", match.group(1))]
            found = []
            for number in numbers:
                if number in allowed:
                    found.append("[#%d]" % allowed[number])
                else:
                    problems.append("picture number %d on %s, which that day has not"
                                    % (number, date))
            return "".join(found)
        paragraph = _REF_RE.sub(link, entry.get("text") if isinstance(entry.get("text"), str)
                                else "")
        paragraph = _tidy(clean_text(paragraph))
        before = paragraph
        paragraph = without_sentences(paragraph, names=other_names, places=facts.home_towns,
                                      keep_names=[name for name, _count in facts.people])
        if paragraph != before:
            problems.append("sentences on %s that named someone or somewhere not in the facts "
                            "were left out" % date)
        if paragraph:
            result[date] = paragraph
    return result, problems


# ---------------------------------------------------------------------------
# The store (index.db's trip_stories)

def ensure_table(conn):
    """Create the trip_stories table when it is not there yet (no commit)."""
    conn.execute(TABLE_SQL)


def _loads(text, default):
    try:
        value = json.loads(text or "")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _row(row):
    return {"key": row["trip_key"], "trip_id": row["trip_id"],
            "span": [row["span_start"], row["span_end"]],
            "days": [entry for entry in _loads(row["days"], [])
                     if isinstance(entry, dict) and isinstance(entry.get("date"), str)
                     and isinstance(entry.get("text"), str)],
            "basis": _loads(row["basis"], {}), "provenance": _loads(row["provenance"], {}),
            "edited": bool(row["edited"]), "hidden": bool(row["hidden"]),
            "made": row["made"], "updated": row["updated"]}


def stored(conn):
    """{trip key: story} of every kept story ({} before the first): {"key", "trip_id",
    "span", "days" [{date, text}], "basis", "provenance", "edited", "hidden", "made",
    "updated"}."""
    try:
        rows = conn.execute("SELECT * FROM trip_stories").fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return {}
        raise
    return {row["trip_key"]: _row(row) for row in rows}


def version(conn):
    """A short text that changes whenever a story does."""
    try:
        row = conn.execute("SELECT COUNT(*), TOTAL(updated) FROM trip_stories").fetchone()
    except sqlite3.OperationalError:
        return "0"
    return "%d:%r" % (row[0], row[1])


def _overlap(span, start, end):
    if not span or not span[0] or not start:
        return 0
    first, last = max(span[0], start), min(span[1], end)
    return trips._days_between(first, last) + 1 if first <= last else 0


def find_row(built, trip, rows):
    """The story of a trip among rows (stored): the one kept by its key, else -- for an
    away trip whose first day moved since -- the one of an away trip no longer found
    whose dates overlap most with the trip's."""
    key = built.key(trip)
    if key in rows:
        return rows[key]
    if trip.kind != "away" or "." in trip.id:
        return None
    present = {built.key(item) for item in built.trips}
    best = None
    for row in rows.values():
        if row["key"] in present or not row["key"].startswith("away-") or "." in row["key"]:
            continue
        days = _overlap(row["span"], trip.start, trip.end)
        if days and (best is None or days > best[0]):
            best = (days, row)
    return best[1] if best else None


def save(conn, built, trip, days, basis, provenance, edited=False, hidden=None, made=None,
         now=None):
    """Keep a trip's story (days: [{date, text}]) under its key, replacing the one it had
    (find_row); hidden None keeps the old one's; made (when the model wrote it) is now
    unless given. Commits. Returns the story."""
    now = time.time() if now is None else now
    ensure_table(conn)
    old = find_row(built, trip, stored(conn))
    if hidden is None:
        hidden = bool(old and old["hidden"])
    with conn:
        if old is not None:
            conn.execute("DELETE FROM trip_stories WHERE trip_key = ?", (old["key"],))
        conn.execute(
            "INSERT OR REPLACE INTO trip_stories (trip_key, trip_id, span_start, span_end, days, "
            "basis, provenance, edited, hidden, made, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (built.key(trip), trip.id, trip.start or "", trip.end or "",
             json.dumps(days, ensure_ascii=False), json.dumps(basis, ensure_ascii=False),
             json.dumps(provenance, ensure_ascii=False), 1 if edited else 0,
             1 if hidden else 0, now if made is None else made, now))
    return find_row(built, trip, stored(conn))


def forget(conn, built, trip):
    """Delete a trip's story; True when it had one."""
    old = find_row(built, trip, stored(conn))
    if old is None:
        return False
    with conn:
        conn.execute("DELETE FROM trip_stories WHERE trip_key = ?", (old["key"],))
    return True


# ---------------------------------------------------------------------------
# Out of date?

def _visible(conn, ids):
    """The ids among ids of present pictures that are not hidden."""
    ids = sorted({image_id for image_id in ids if isinstance(image_id, int)})
    found = set()
    for start in range(0, len(ids), ID_CHUNK):
        part = ids[start:start + ID_CHUNK]
        found.update(row[0] for row in conn.execute(
            "SELECT id FROM images WHERE id IN (%s) AND hidden = 0 AND missing_since IS NULL"
            % ",".join("?" * len(part)), part))
    return found


def state(story, basis, visible=None):
    """(state, [reasons]) of a kept story against the trip's basis now (basis_of):
    "missing" (no story), "empty" (nothing to write from), "stale" (the trip changed
    much since: the reasons say how) or "current". visible: the ids among the story's
    links that are present and not hidden (None: not checked)."""
    if story is None:
        return ("empty" if not basis.get("dates") else "missing"), []
    reasons = []
    before = story["basis"] or {}
    old_dates, new_dates = set(before.get("dates") or ()), set(basis.get("dates") or ())
    if old_dates != new_dates:
        added, gone = len(new_dates - old_dates), len(old_dates - new_dates)
        bits = []
        if added:
            bits.append("%d day%s added" % (added, "" if added == 1 else "s"))
        if gone:
            bits.append("%d day%s gone" % (gone, "" if gone == 1 else "s"))
        reasons.append("its days changed (%s)" % ", ".join(bits))
    old_ids, new_ids = set(before.get("ids") or ()), set(basis.get("ids") or ())
    union = old_ids | new_ids
    if union and len(old_ids ^ new_ids) / float(len(union)) > CHANGED_SHARE:
        reasons.append("%d%% of its pictures changed" % round(
            100.0 * len(old_ids ^ new_ids) / len(union)))
    old_names = set(before.get("names") or ())
    new_names = set(basis.get("names") or ())
    if old_names != new_names:
        reasons.append("other people are named in it now")
    if visible is not None:
        cited = {image_id for entry in story["days"] for image_id in links(entry["text"])}
        if cited - set(visible):
            reasons.append("a picture it links is hidden or gone")
    return ("stale" if reasons else "current"), reasons


# ---------------------------------------------------------------------------
# What is shown

def visible_days(conn, config, story, admin, names_allowed=None):
    """A story's days as a requester may see them (see the module notes): the admin's as
    kept; others' without the sentences that link a picture hidden or gone since, or
    name an unnamed person ("Person 12"), nor (unless names_allowed, default: People
    shown to visitors) those naming people (_unwanted_names: only the story's people
    still named so, never a hidden person)."""
    if admin:
        return [dict(entry) for entry in story["days"]]
    import people_groups
    if names_allowed is None:
        names_allowed = people_groups.may_see(config, False)
    cited = {image_id for entry in story["days"] for image_id in links(entry["text"])}
    shown = _visible(conn, cited)
    gone = {image_id for image_id in cited if image_id not in shown}
    names, keep = _unwanted_names(story, names_allowed)
    result = []
    for entry in story["days"]:
        text = without_sentences(entry["text"], names=names, ids=gone, keep_names=keep)
        if text:
            result.append({"date": entry["date"], "text": text})
    return result


def _trip_and_story(conn, config, library_ids, trip_id):
    built = trips.build(conn, config, library_ids)
    trip = built.get(trip_id)
    return built, trip, find_row(built, trip, stored(conn))


def story_view(conn, config, library_ids, trip_id, admin=False):
    """/api/trips/<id>/story: {"trip", "days": [{date, text}]} as the requester may see it
    (visible_days; a visitor gets no days of a hidden story, nor -- 404 -- of a trip
    trips.view does not show them: trips.visitors_see);
    the admin also gets "state" and "reasons" (state()), "hidden", "edited", "made",
    "updated", "model", "endpoint", "local" and "usage" ({tokens_in, tokens_out, cost,
    requests, seconds}). Raises trips.TripError (404)."""
    built, trip, story = _trip_and_story(conn, config, library_ids, trip_id)
    if not admin and not trips.visitors_see(trip, trips.settings(config),
                                            trips.visible_by_base(built)):
        # (As /api/trips: no hidden trip, none found by the owner's hidden pictures.)
        raise trips.TripError("There is no trip %s." % trip_id, 404)
    result = {"trip": trip.id, "days": []}
    if story is not None and (admin or not story["hidden"]):
        result["days"] = visible_days(conn, config, story, admin)
    if not admin:
        return result
    homes = _Homes(conn, built)
    basis = basis_of(trip, homes.of(trip), named_people(
        conn, config, [picture.id for picture in trip.pictures]))
    cited = ({image_id for entry in story["days"] for image_id in links(entry["text"])}
             if story else set())
    result["state"], result["reasons"] = state(story, basis, _visible(conn, cited))
    if story is not None:
        provenance = story["provenance"]
        result.update(hidden=story["hidden"], edited=story["edited"], made=story["made"],
                      updated=story["updated"], model=provenance.get("model", ""),
                      endpoint=provenance.get("endpoint", ""),
                      local=bool(provenance.get("local")),
                      usage={key: provenance.get(key) for key in (
                          "tokens_in", "tokens_out", "cost", "requests", "seconds")})
    return result


def change_story(conn, config, library_ids, trip_id, days=None, hidden=None, forget_it=False):
    """The owner's change of a trip's story: days ([{date, text}]: the text as written,
    a day left empty has no paragraph; links [#id] only to the trip's pictures),
    hidden (True/False) or forget_it (delete it). Returns story_view (the admin's).
    Raises StoryError, trips.TripError (404)."""
    built, trip, story = _trip_and_story(conn, config, library_ids, trip_id)
    if forget_it:
        forget(conn, built, trip)
        return story_view(conn, config, library_ids, trip.id, admin=True)
    if hidden is not None and not isinstance(hidden, bool):
        raise StoryError("hidden is true or false.")
    if days is None and hidden is None:
        raise StoryError("Send days, hidden or forget.")
    if days is None:
        if story is None:
            raise StoryError("The trip has no story to hide or show.", 404)
        kept = story["days"]
        edited = story["edited"]
    else:
        kept = clean_days(trip, days)
        edited = True
    if story is not None:
        basis, provenance, made = story["basis"], dict(story["provenance"]), story["made"]
        if days is not None:
            provenance["edited_at"] = time.time()
    else:
        homes = _Homes(conn, built)
        basis = basis_of(trip, homes.of(trip), named_people(
            conn, config, [picture.id for picture in trip.pictures]))
        provenance, made = {"edited_at": time.time()}, 0
    save(conn, built, trip, kept, basis, provenance, edited=edited, hidden=hidden, made=made)
    return story_view(conn, config, library_ids, trip.id, admin=True)


def clean_days(trip, days):
    """The owner's days of a story checked: [{date, text}] of the trip's days in order
    (an empty text: no paragraph), links only to the trip's pictures. Raises
    StoryError."""
    if not isinstance(days, list):
        raise StoryError("Send the days as a list of {date, text}.")
    allowed = {picture.id for picture in trip.pictures + trip.other}
    result = {}
    for entry in days:
        if not isinstance(entry, dict) or not isinstance(entry.get("date"), str) \
                or not isinstance(entry.get("text", ""), str):
            raise StoryError("Each day is {date, text}.")
        date = entry["date"]
        if not _DATE_RE.match(date) or not trip.start or not trip.start <= date <= trip.end:
            raise StoryError("%s is not a day of the trip." % date)
        text = clean_text(entry.get("text") or "")
        text = _tidy(MARKER_RE.sub(lambda match: match.group(0) if match.group(1).isdigit()
                                   and int(match.group(1)) in allowed else "", text))
        if text:
            result[date] = text
    return [{"date": date, "text": result[date]} for date in sorted(result)]


def export_days(conn, config, built, trip, published, names_allowed, home=None,
                at_home=()):
    """The days of a trip's story for its export: [{date, text}] with the links made the
    export's ids (published: {image id: export id}), without the sentences that name
    people (but for those of names_allowed: the names the export publishes,
    export.published_names, a list; or True: the story's people; _unwanted_names: a
    hidden person's never), a town of home (the export's Home), or link a picture not
    published or at home (at_home: ids); [] when there is no story or it is hidden."""
    story = find_row(built, trip, stored(conn))
    if story is None or story["hidden"]:
        return []
    names, keep = _unwanted_names(story, names_allowed)
    cited = {image_id for entry in story["days"] for image_id in links(entry["text"])}
    unwanted = {image_id for image_id in cited if image_id not in published} | set(at_home)
    places = home.town_names if home is not None else ()
    result = []
    for entry in story["days"]:
        text = without_sentences(entry["text"], names=names, places=places, ids=unwanted,
                                 keep_names=keep)
        text = map_links(text, {image_id: xid for image_id, xid in published.items()})
        if text:
            result.append({"date": entry["date"], "text": text})
    return result


def export_status(conn, config, built, trip):
    """{"available" (a story an export can publish: there is one and it is not hidden),
    "hidden", "days"} of a trip's story, for the export dialog."""
    story = find_row(built, trip, stored(conn))
    if story is None:
        return {"available": False, "hidden": False, "days": 0}
    return {"available": not story["hidden"] and bool(story["days"]),
            "hidden": story["hidden"], "days": len(story["days"])}


# ---------------------------------------------------------------------------
# Writing them (the job)

def _library_ids(config):
    return [library["id"] for library in common.enabled_libraries(config) if library.get("id")]


class _Work:
    """What the job knows of the trips: built (trips.build), records, names, drives,
    homes; facts_for(trip) makes a trip's Facts."""

    def __init__(self, conn, config, library_ids=None):
        import drives
        self.conn = conn
        self.config = config
        self.library_ids = library_ids if library_ids is not None else _library_ids(config)
        self.built = trips.build(conn, config, self.library_ids)
        self.homes = _Homes(conn, self.built)
        self.rows = stored(conn)
        # {trip id: when its story was last changed (None: none)} as the job chose the
        # trips (write_stories; see _keep).
        self.seen = {}
        self.other_names = set(all_names())
        self._records = None
        self._names = None
        self._facts = {}
        self.drive_map = {}
        for library_id in self.library_ids:
            for drive in drives.library_drives(conn, library_id, config):
                for clip in drive.clips:
                    self.drive_map[clip.id] = drive
        self._lengths = None

    @property
    def records(self):
        if self._records is None:
            self._records = {record["i"]: record for record in search_index.build_records(
                self.conn, self.config, self.library_ids, include_hidden=False)}
        return self._records

    def names(self, trip):
        if self._names is None:
            self._names = named_people(self.conn, self.config, [
                picture.id for item in self.built.trips for picture in item.pictures])
        return self._names

    def lengths(self):
        if self._lengths is None:
            self._lengths = _drive_lengths(self.conn, set(self.drive_map))
        return self._lengths

    def basis(self, trip):
        return basis_of(trip, self.homes.of(trip), self.names(trip))

    def facts_for(self, trip, names=True):
        """The trip's Facts; names False: without the names of people (for an endpoint
        that is not local: see the module notes), but with the same basis (the trip's
        named people), so the story is not out of date for that."""
        key = (trip.id, bool(names))
        if key not in self._facts:
            home = self.homes.of(trip)
            facts = facts_of(self.conn, self.config, trip, self.records,
                             self.names(trip) if names else {}, home, self.drive_map,
                             self.lengths())
            if not names:
                facts.basis = self.basis(trip)
            self._facts[key] = facts
        return self._facts[key]

    def story(self, trip):
        return find_row(self.built, trip, self.rows)

    def state(self, trip):
        story = self.story(trip)
        cited = ({image_id for entry in story["days"] for image_id in links(entry["text"])}
                 if story else set())
        return state(story, self.basis(trip), _visible(self.conn, cited))


def choose(work, trip_id=None, redo=False):
    """[(trip, why)] of the trips whose stories to write, and [(trip, why not)] of the
    others: one trip (trip_id; even hidden) or every trip that is not hidden; those
    without a story or with one out of date, but not one the owner edited (unless redo
    for one trip); with redo, also those that are current. Raises trips.TripError (404)
    for an unknown trip_id."""
    chosen, skipped = [], []
    wanted = [work.built.get(trip_id)] if trip_id else [
        trip for trip in work.built.trips if not trip.hidden]
    for trip in wanted:
        current, reasons = work.state(trip)
        story = work.story(trip)
        if story is not None and story["edited"] and not (redo and trip_id):
            skipped.append((trip, "kept: you edited it" + (
                " (out of date: %s)" % "; ".join(reasons) if current == "stale" else "")))
        elif current == "current" and not redo:
            skipped.append((trip, "up to date"))
        elif current == "empty" or not work.facts_for(trip).days:
            skipped.append((trip, "nothing to write about: no picture that may be told of "
                                  "(hidden, private, sensitive or at home)"))
        else:
            chosen.append((trip, current if current != "stale" else "out of date: " +
                           "; ".join(reasons)))
    return chosen, skipped


def plan(conn, config, trip_id=None, redo=False):
    """What a job would do, for the pages to show before it starts: {"trips": [{id,
    title, why}], "skipped": {"current", "edited", "empty"}, "endpoints": [{name,
    model, local (on this computer or the local network: it gets the names of people
    too), host, trips, requests, tokens_in, tokens_out, cost}], "problems":
    [{id, title, problem}], "cost" (None when unknown or nothing goes to a paid
    service)}. Raises trips.TripError."""
    work = _Work(conn, config)
    chosen, skipped = choose(work, trip_id, redo)
    result = {"trips": [], "skipped": {"current": 0, "edited": 0, "empty": 0},
              "endpoints": [], "problems": [], "cost": None}
    for _trip, why in skipped:
        key = ("edited" if why.startswith("kept") else "empty" if why.startswith("nothing")
               else "current")
        result["skipped"][key] += 1
    by_name = collections.OrderedDict()
    for trip, why in chosen:
        result["trips"].append({"id": trip.id, "title": trip.title, "why": why})
        endpoint, how = endpoint_for(config, trip)
        if endpoint is None:
            result["problems"].append({"id": trip.id, "title": trip.title, "problem": how})
            continue
        entry = by_name.get(endpoint["name"])
        if entry is None:
            entry = by_name[endpoint["name"]] = {
                "name": endpoint["name"], "model": endpoint.get("model", ""),
                "local": common.is_local_endpoint(endpoint),
                "host": common.url_host(common.endpoint_settings(endpoint)["base_url"]),
                "trips": 0, "requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": None,
                "_endpoint": endpoint}
        guess = estimate(work.facts_for(trip, names=entry["local"]), config, endpoint)
        entry["trips"] += 1
        for key in ("requests", "tokens_in", "tokens_out"):
            entry[key] += guess[key]
    total = None
    for entry in by_name.values():
        endpoint = entry.pop("_endpoint")
        entry["cost"] = llm.estimate_cost(endpoint, {"input_tokens": entry["tokens_in"],
                                                     "output_tokens": entry["tokens_out"]})
        if entry["cost"] is not None:
            total = (total or 0.0) + entry["cost"]
        result["endpoints"].append(entry)
    result["cost"] = round(total, 6) if total is not None else None
    return result


def _usage_words(entry):
    words = "about %s tokens in, %s out" % (format(entry["tokens_in"], ","),
                                            format(entry["tokens_out"], ","))
    if entry.get("cost") is not None:
        words += ", about $%.4f" % entry["cost"]
    return words


class _Stopped(Exception):
    pass


class _EndpointGone(Exception):
    """The endpoint cannot be used any more (fatal or endpoint error)."""


class _NoStory(ValueError):
    """No usable paragraph came of a trip's requests; .usage: what they used."""

    def __init__(self, message, usage):
        ValueError.__init__(self, message)
        self.usage = usage


def _request(endpoint, facts, dates, notes, ctx, sleep):
    """One request for the paragraphs of dates: the model's reply. Raises _Stopped,
    _EndpointGone, llm.ProviderError."""
    if ctx.should_stop():
        raise _Stopped()
    ctx.wait_if_paused()
    text = prompt_text(facts, dates, notes)
    try:
        reply = llm.describe_with_retries(endpoint, SYSTEM_PROMPT, text, None, SCHEMA,
                                          should_stop=ctx.should_stop, sleep=sleep,
                                          max_retries=MAX_RETRIES)
    except llm.ProviderError as error:
        if ctx.should_stop():
            raise _Stopped()
        if error.kind in ("fatal", "endpoint"):
            raise _EndpointGone(llm.explain_error(error, endpoint))
        raise
    return reply


def _answer(reply, facts, dates, other_names):
    """({date: paragraph}, problems) of a reply (parse_reply). Raises ValueError when the
    model would not answer, or the reply holds no JSON object."""
    if reply["finish"] in ("refusal", "filtered"):
        raise ValueError("the model would not answer (%s)" % (reply.get("detail")
                                                              or reply["finish"]))
    return parse_reply(reply["text"], facts, dates, other_names)


def write_trip(endpoint, facts, config, ctx, other_names=(), sleep=time.sleep):
    """Write a trip's story from its facts with an endpoint: {"days": [{date, text}],
    "usage": {tokens_in, tokens_out, requests, seconds}, "model", "problems"}. A day
    left out of an answer (or of one that cannot be used) is asked for once more; the
    days still missing then are problems, the others are kept. Raises _Stopped,
    _EndpointGone, llm.ProviderError, _NoStory (a ValueError: no usable paragraph at
    all; its usage counts the requests made)."""
    answer, budget = answer_room(config, endpoint)
    notes = settings(config)["notes"]
    endpoint = dict(endpoint, max_output_tokens=answer)
    written = {}
    usage = {"tokens_in": 0, "tokens_out": 0, "requests": 0, "seconds": 0.0}
    model = ""
    problems = []
    for dates in plan_requests(facts, budget, answer, notes):
        left = list(dates)
        for _attempt in range(2):
            reply = _request(endpoint, facts, left, notes, ctx, sleep)
            usage["requests"] += 1
            usage["tokens_in"] += reply["usage"].get("input_tokens") or 0
            usage["tokens_out"] += reply["usage"].get("output_tokens") or 0
            usage["seconds"] += reply.get("seconds") or 0.0
            model = model or reply.get("model") or ""
            try:
                days, found = _answer(reply, facts, left, other_names)
            except ValueError as error:
                problems.append(str(error))
                continue
            problems.extend(found)
            written.update(days)
            left = [date for date in left if date not in written]
            if not left:
                break
        if left:
            problems.append("no paragraph for %s" % ", ".join(left))
    usage["seconds"] = round(usage["seconds"], 1)
    if not written:
        raise _NoStory("no usable paragraph in the answers%s" % (
            ": " + problems[0] if problems else ""), usage)
    return {"days": [{"date": date, "text": written[date]} for date in facts.dates
                     if date in written],
            "usage": usage, "model": model, "problems": problems}


def _new_summary():
    return {"trips": 0, "written": 0, "current": 0, "edited": 0, "empty": 0, "failed": 0,
            "tokens_in": 0, "tokens_out": 0, "cost": None, "requests": 0, "stopped": False,
            "error": ""}


def _ready(settings_, ctx, sleep):
    """(slots or None, problem or "") of an endpoint about to be used: a cloud service
    needs its key; a local server must answer (one still loading is waited for)."""
    key = common.resolve_api_key(settings_)[0]
    if settings_["preset"] in common.CLOUD_PRESETS and not key:
        return None, ("no API key is set (%s), so nothing was sent"
                      % common.resolve_api_key(settings_)[1])
    if not common.is_local_endpoint(settings_):
        return None, ""
    info = llm.probe(settings_)
    if info["loading"] or info["reachable"] is False:
        if info["loading"]:
            ctx.log("Waiting for endpoint %s to be ready..." % settings_["label"])
        llm.wait_until_ready(settings_, STARTUP_WAIT if info["loading"] else 3.0,
                             should_stop=ctx.should_stop, sleep=sleep)
        info = llm.probe(settings_)
    if info["reachable"] is False:
        return None, "it cannot be reached at %s (is the server running?)" % common.redact(
            settings_["base_url"], [key] if key else [])
    return info["slots"], ""


def write_stories(ctx, conn, config, trip_id=None, redo=False, servers=None,
                  sleep=time.sleep):
    """The job: write the stories of the trips choose() picks (see the module notes).
    Returns a summary: {"trips" (looked at), "written", "current", "edited" (kept),
    "empty", "failed", "tokens_in", "tokens_out", "cost", "requests", "stopped",
    "error" (when none could be written: why)}. Raises trips.TripError (404) for an
    unknown trip_id."""
    import local_server
    summary = _new_summary()
    ctx.stage("stories")
    work = _Work(conn, config)
    chosen, skipped = choose(work, trip_id, redo)
    summary["trips"] = len(chosen) + len(skipped)
    for trip, why in skipped:
        key = ("edited" if why.startswith("kept") else "empty" if why.startswith("nothing")
               else "current")
        summary[key] += 1
        if trip_id or key != "current":
            ctx.log("%s: %s." % (trip.title, why))
    if summary["current"] and not trip_id:
        ctx.log("%d trip(s) have a story that is up to date." % summary["current"])
    if not chosen:
        ctx.log("No story to write.")
        return summary
    # (Whether a story the owner edits while the job runs is theirs: see _keep.)
    work.seen = {trip.id: (work.story(trip) or {}).get("updated") for trip, _why in chosen}
    groups = collections.OrderedDict()
    for trip, why in chosen:
        endpoint, how = endpoint_for(config, trip)
        if endpoint is None:
            ctx.log("%s: %s" % (trip.title, how), "error")
            summary["failed"] += 1
            summary.setdefault("_problem", how)
            continue
        groups.setdefault(endpoint["name"], (endpoint, how, []))[2].append((trip, why))
    ctx.stage("stories", total=sum(len(items) for _e, _h, items in groups.values()))
    lease = local_server.lease_for(ctx, config, list(groups), manager=servers)
    try:
        problems = lease.acquire()
        for name, (endpoint, how, items) in groups.items():
            if ctx.should_stop():
                break
            _write_group(ctx, conn, config, work, endpoint, how, items, problems.get(name),
                         summary, sleep)
    finally:
        lease.release()
    # (Some stories failed, others were written: the job is done, its summary counts the
    # failed ones; it fails only when none could be written.)
    problem = summary.pop("_problem", "")
    if ctx.should_stop():
        summary["stopped"] = True
    elif summary["failed"] and not summary["written"]:
        summary["error"] = problem or "No story could be written (see the log)."
    if summary["cost"] is not None:
        summary["cost"] = round(summary["cost"], 6)
    return summary


def _write_group(ctx, conn, config, work, endpoint, how, items, problem, summary, sleep):
    """Write the stories of items ([(trip, why)]) with one endpoint (chosen as how says)."""
    settings_ = common.endpoint_settings(endpoint)
    label = settings_["label"]
    slots, trouble = (None, problem) if problem else _ready(settings_, ctx, sleep)
    if trouble:
        ctx.log("Endpoint %s cannot write stories: %s." % (label, trouble), "error")
        summary["failed"] += len(items)
        summary.setdefault("_problem", "Endpoint %s: %s." % (label, trouble))
        for _item in items:
            ctx.step()
        return
    local = common.is_local_endpoint(settings_)
    facts = {}
    for trip, _why in items:
        # (People's names go to no endpoint but one on this computer or the network.)
        facts[trip.id] = work.facts_for(trip, names=local)
    total = {"tokens_in": 0, "tokens_out": 0, "cost": None}
    for trip, _why in items:
        guess = estimate(facts[trip.id], config, endpoint)
        total["tokens_in"] += guess["tokens_in"]
        total["tokens_out"] += guess["tokens_out"]
    total["cost"] = llm.estimate_cost(endpoint, {"input_tokens": total["tokens_in"],
                                                 "output_tokens": total["tokens_out"]})
    workers = max(1, min(MAX_WORKERS, len(items), common.endpoint_concurrency(settings_, slots)))
    ctx.log("Writing %d stor%s with %s (%s; model %s%s): %s. Only text is sent (the trips' "
            "facts%s), no picture." % (
                len(items), "y" if len(items) == 1 else "ies", label, how,
                settings_["model"], ", on this computer" if local else
                ", sent to %s" % common.url_host(settings_["base_url"]), _usage_words(total),
                "" if local else ", without the names of people"))
    gone = {}
    lock = threading.Lock()

    def run(trip):
        with lock:
            if gone:
                raise _EndpointGone(gone["why"])
        try:
            return write_trip(endpoint, facts[trip.id], config, ctx, work.other_names - set(
                name for name, _count in facts[trip.id].people), sleep)
        except _EndpointGone as error:
            with lock:
                gone.setdefault("why", str(error))
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = collections.OrderedDict(
            (pool.submit(run, trip), (trip, why)) for trip, why in items)
        try:
            for future in concurrent.futures.as_completed(futures):
                trip, why = futures[future]
                try:
                    result = future.result()
                except _Stopped:
                    continue
                except _EndpointGone as error:
                    if "logged" not in gone:
                        gone["logged"] = True
                        ctx.log("Endpoint %s cannot be used: %s" % (label, error), "error")
                        summary.setdefault("_problem", "Endpoint %s: %s" % (label, error))
                    summary["failed"] += 1
                    ctx.step(item=trip.title)
                    continue
                except (llm.ProviderError, ValueError) as error:
                    if isinstance(error, _NoStory):
                        _add_usage(summary, endpoint, error.usage)
                    ctx.log("%s: the story could not be written: %s" % (
                        trip.title, common.redact(error)), "warn")
                    summary["failed"] += 1
                    ctx.step(item=trip.title)
                    continue
                _keep(ctx, conn, work, trip, facts[trip.id], result, endpoint, settings_, local,
                      summary)
                ctx.step(item=trip.title)
                if ctx.should_stop():
                    break
        finally:
            for future in futures:
                future.cancel()


def _add_usage(summary, endpoint, usage):
    """Add what requests used to the job's summary; returns their cost (None: unknown)."""
    cost = llm.estimate_cost(endpoint, {"input_tokens": usage["tokens_in"],
                                        "output_tokens": usage["tokens_out"]})
    summary["requests"] += usage["requests"]
    summary["tokens_in"] += usage["tokens_in"]
    summary["tokens_out"] += usage["tokens_out"]
    if cost is not None:
        summary["cost"] = (summary["cost"] or 0.0) + cost
    return cost


def _keep(ctx, conn, work, trip, facts, result, endpoint, settings_, local, summary):
    """Keep the story the model wrote, unless the owner edited the trip's story while the
    job ran (its row changed since the job chose the trip, work.seen, and is edited
    now): theirs stays."""
    usage = result["usage"]
    cost = _add_usage(summary, endpoint, usage)
    now = find_row(work.built, trip, stored(conn))
    if (now is not None and now["edited"]
            and now["updated"] != work.seen.get(trip.id)):
        summary["edited"] += 1
        ctx.log("%s: kept: you edited the story while the job ran." % trip.title)
        return
    provenance = {"model": result["model"] or settings_["model"], "endpoint": settings_["name"],
                  "local": local, "tokens_in": usage["tokens_in"],
                  "tokens_out": usage["tokens_out"], "cost": cost,
                  "requests": usage["requests"], "seconds": usage["seconds"],
                  "format": FORMAT}
    save(conn, work.built, trip, result["days"], facts.basis, provenance, edited=False)
    work.rows = stored(conn)
    summary["written"] += 1
    cited = sum(len(links(entry["text"])) for entry in result["days"])
    ctx.log("Wrote the story of %s: %d of %d day(s), %d picture link(s)%s." % (
        trip.title, len(result["days"]), len(facts.days), cited,
        "" if not result["problems"] else " (%s)" % "; ".join(result["problems"][:3])))


# ---------------------------------------------------------------------------
# Command line (python trips.py)

def _list(conn, config, out):
    work = _Work(conn, config)
    for trip in work.built.trips:
        current, _reasons = work.state(trip)
        story = work.story(trip)
        flags = [current]
        if story is not None and story["edited"]:
            flags.append("edited")
        if story is not None and story["hidden"]:
            flags.append("story hidden")
        if trip.hidden:
            flags.append("trip hidden")
        print("%-24s %s  %s (%d pictures; story: %s)" % (
            trip.id, trips.span_words(trip.start, trip.end) if trip.start else "undated",
            trip.title, len(trip.pictures) + len(trip.other), ", ".join(flags)), file=out)


def main_cli(argv=None, out=None):
    """python trips.py: --list the trips (ids, dates, titles, their stories' states), or
    --stories [--trip ID] [--redo]: write the trips' stories as the job does. Returns the
    exit code."""
    out = out or sys.stdout
    if hasattr(out, "reconfigure"):
        try:
            out.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="trips.py",
        description="The trips found in the pictures (see README: Trips): list them, or write "
                    "their stories with a language model (see README: Trip stories).")
    parser.add_argument("--list", action="store_true",
                        help="list the trips: id, dates, title, the state of their story")
    parser.add_argument("--stories", action="store_true",
                        help="write the stories of the trips that have none or an out-of-date "
                             "one (not those you edited), as the Trips page's Write stories "
                             "does")
    parser.add_argument("--trip", metavar="ID",
                        help="with --stories: only this trip (its id: see --list, or the "
                             "address of its page, /trip/ID)")
    parser.add_argument("--redo", action="store_true",
                        help="with --stories: also write again the stories that are up to "
                             "date (with --trip: even one you edited)")
    jobs.add_settings_base_argument(parser)
    args = parser.parse_args(argv)
    jobs.use_settings_base(parser, args)
    if args.list == args.stories:
        parser.error("choose --list or --stories")
    if (args.trip or args.redo) and not args.stories:
        parser.error("--trip and --redo go with --stories")
    import db
    if args.list and jobs.missing_settings():
        print(jobs.missing_settings(), file=out)
        return 2
    if args.list:
        try:
            db.init()
            config = common.load_config()
            conn = db.connect()
        except (db.DatabaseError, common.ConfigError) as error:
            print("Error: %s" % error, file=out)
            return 2
        try:
            _list(conn, config, out)
        finally:
            conn.close()
        return 0
    ctx = jobs.ConsoleContext("stories", stream=out)
    jobs.log_tool_command(ctx, "trips.py", argv)
    if jobs.missing_settings():
        # (A scheduled run without its settings folder must not look like one that worked.)
        ctx.log(jobs.missing_settings(), "error")
        return 2
    lock = jobs.WorkerLock()
    if not lock.acquire("trips.py --stories"):
        ctx.log("Another job is running: %s. Try again when it has finished."
                % (lock.holder() or "another process"), "error")
        return 1
    try:
        db.init(log=ctx.log)
        config = common.load_config()
        conn = db.connect()
        try:
            with ctx.stop_on_ctrl_c():
                summary = jobs.RUNNERS["trip_stories"](
                    ctx, conn, config, {"library_id": None, "trip": args.trip,
                                        "redo": args.redo}, helper=None)
        finally:
            conn.close()
    except jobs.JobError as error:
        ctx.log(str(error), "error")
        return 1
    except (db.DatabaseError, common.ConfigError) as error:
        ctx.log("Error: %s" % error, "error")
        return 2
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C). The stories written are kept; run the same command again "
              "to write the others.", file=out)
        return 130
    finally:
        lock.release()
    if summary.get("stopped"):
        ctx.log("Stopped as asked. The stories written are kept; run the same command again "
                "to write the others.")
        return 0
    if summary.get("error"):
        ctx.log(summary["error"], "error")
        return 1
    ctx.log("Done: %d stor%s written, %d up to date, %d kept (edited), %d failed." % (
        summary["written"], "y" if summary["written"] == 1 else "ies", summary["current"],
        summary["edited"], summary["failed"]))
    return 0
