#!/usr/bin/env python3
"""Ask LLMs to describe indexed images and store the replies.

For every image that has no description yet (and, when asked, every one
whose description is out of date: from an older prompt, or a video's that
talks about its frames), the cached preview -- or the original when
there is no preview -- is shrunk with images.prepare_for_llm() and sent as a
JPEG with the prompt from prompts.py to the library's endpoint(s). The JSON
reply is checked and tidied by parse_reply() and stored with the image,
together with its provenance (model, endpoint, time, tokens, prompt hash).

Each library uses its own endpoint (or group of endpoints, which share the
work); see common.endpoints_for_library. Every endpoint gets as many worker
threads as its concurrency setting (for llama.cpp, the server's slot
count), so several requests are in flight at once. Only the calling thread
writes to the database. Hidden pictures only go to endpoints on this
computer or the local network (others skip them). A model server that
config.json's local_server runs for an endpoint is started when a run
needs it (local_server.Lease).

refine_pending() is the refine pass: documents and screenshots that are
described already are described again by the library's refine endpoint
(see refine.py), and their provenance says tier "refine".

A video is described from its contact sheet. When the title or the
description of a video's reply talks about the sheet instead of the video
("four frames show ...", see sheet_talk), the same endpoint is asked once
more with the same picture and one more sentence (describe_checked); the
provenance then says "reasked": "sheet". config.json's "video_sheet_check"
(true by default) turns this off.

A reply cut off at the length limit is used when it got as far as the
title and the description (repair; the provenance says "truncated"),
unless its transcribed text ended in a loop: the image is then asked for
again, as it is for broken JSON, and the repaired reply is only used when
the retries do no better. A reply cut off before the title is asked for
again with twice the endpoint's max_output_tokens (once; when the
endpoint refuses that many, the remaining tries use its own setting).
The fields a cut-off reply did not reach are filled in by fill_missing:
the refine pass keeps the old description's values for them (and its
privacy flags), and otherwise a private_info that was not reached is
True.

What happens when something goes wrong (see llm.ProviderError for kinds):
- retryable (rate limits, busy or restarting servers, time-outs) -- asked
  again with a growing pause (llm.describe_with_retries). If that is not
  enough, the image goes back in the queue once, behind the other waiting
  images; an endpoint that fails this way MAX_ENDPOINT_FAILURES times in a
  row is not used for the rest of the run. On an endpoint that works, an
  image that times out (or makes the server fail) twice is charged.
- fatal (rejected key, no credit) -- the whole run stops with a message.
- endpoint (unknown model, settings the service rejects) -- that endpoint is
  not used for the rest of the run; its libraries' remaining images wait for
  the next run.
- image (the service rejected this image, or its content policy blocked
  it) -- the image fails at once.
- content (cut off, refused, or not a usable description) -- asked again up
  to CONTENT_RETRIES more times, then the image fails.
Until an endpoint has described an image in the run, image and content
failures are blamed on its settings or model instead: the image is not
charged (it is tried once more after the other images), and after
MAX_ENDPOINT_FAILURES of them in a row the endpoint is not used.
A failed image gets describe_attempts + 1 and its (redacted) error; images
that failed MAX_FAILURES runs are skipped until a run asks to retry them,
and images that failed before are described after the others.
Problems with the endpoint itself are not charged to images -- except that
when an endpoint that has worked in the run stops on an image (an error
that ends the run or the endpoint, or time-outs), that image is charged
once, so the same picture cannot stop every run.

The command line entry point is scan.py, which indexes and then describes.
"""

import collections
import concurrent.futures
import copy
import inspect
import json
import math
import os
import random
import re
import threading
import time

import cache
import common
import imageinfo
import images
import jobs
import llm
import prompts
import sheet_talk
import videos
from prompts import PROMPT_VER

# Unusable replies (cut off, refused, not valid JSON, no description) are
# asked for this many more times in one run before the image counts as
# failed.
CONTENT_RETRIES = 2
# Images that failed this many runs are skipped unless retry_failed is set.
MAX_FAILURES = common.MAX_DESCRIBE_FAILURES
# An endpoint whose requests fail this many times in a row without it being
# the image's fault (server down, overloaded, refusing every request before
# it has described anything) is not used for the rest of the run.
MAX_ENDPOINT_FAILURES = 3
# Retries of one request after rate limits and server errors
# (llm.describe_with_retries), and of a request that ran out of time: such
# an image is put back at the end of the queue instead, since asking again
# at once would most likely take as long.
MAX_RETRIES = 6
TIMEOUT_RETRIES = 0
# How often a progress line is logged during a run (seconds).
PROGRESS_SECONDS = 30.0
# How long to wait for a local server that does not answer at all before
# the run, and for one that is starting up (llama.cpp loading its model, or
# a server the helper's before_launch just started).
UNREACHABLE_WAIT = 3.0
STARTUP_WAIT = 180.0
# How long to wait for a library folder (a network share, say) to answer
# before images that must be read from it are left for another run.
FOLDER_CHECK_TIMEOUT = 10.0
# Repetition guard: a short text field (title, description, ...) in which
# one word appears more than this many times (and makes up a quarter of the
# words) is a looping reply, and so is a list more than LIST_OVERFLOW times
# longer than its bound.
REPEAT_LIMIT = 30
LIST_OVERFLOW = 3
# The transcribed text is never rejected for repeating itself: receipts,
# tables, lists and lyrics repeat legitimately. Only a tail in which the same
# line, or the same phrase of up to LOOP_PHRASE_WORDS words, comes
# LOOP_REPEATS or more times in a row (a model stuck in a loop until its
# length limit) is cut off, keeping one copy.
LOOP_REPEATS = 10
LOOP_PHRASE_WORDS = 12
# Sampling changes for asking a local model again after an unusable reply.
RETRY_REPEAT_PENALTY = 1.1
# A reply cut off at the length limit is asked for again with twice the
# endpoint's max_output_tokens, but never more than this.
MAX_OUTPUT_TOKENS = 1000000

TMP_PREFIX = "describe-"


class ContentError(ValueError):
    """A reply arrived but is not a usable description."""


# ---------------------------------------------------------------------------
# Replies

_WORD_RE = re.compile(r"\w+")
_TOKEN_RE = re.compile(r"\S+")
_SPACES_RE = re.compile(r"\s+")
# Half of a UTF-16 surrogate pair on its own (an escaped emoji whose other
# half is missing): it cannot be stored as UTF-8, so it is dropped.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_ENUM_SEPARATORS_RE = re.compile(r"[\s\-/]+")
_NOTHING = ("none", "null", "nil", "n_a", "na", "nothing", "no", "unknown", "")

# Common near-misses for enum values (from servers that do not enforce the
# schema), mapped to the real value.
_ENUM_ALIASES = {
    "photo_type": {
        "photograph": "photo", "picture": "photo", "image": "photo", "selfie": "photo",
        "screen_shot": "screenshot", "screen_capture": "screenshot", "screengrab": "screenshot",
        "scanned": "scan", "scanned_document": "scan", "illustration": "artwork",
        "drawing": "artwork", "painting": "artwork", "art": "artwork", "cartoon": "artwork",
        "render": "artwork", "graphic": "artwork", "chart": "diagram", "graph": "diagram",
        "infographic": "diagram", "map": "diagram",
    },
    "document_type": {
        "chat_screenshot": "chat", "messages": "chat", "message": "chat",
        "text_message": "chat", "conversation": "chat", "web_screenshot": "web",
        "webpage": "web", "web_page": "web", "website": "web", "app_screenshot": "app",
        "application": "app", "id": "card_or_id", "id_card": "card_or_id",
        "passport": "card_or_id", "credit_card": "card_or_id", "card": "card_or_id",
        "business_card": "card_or_id", "book": "book_page", "page": "book_page",
        "note": "handwriting", "handwritten": "handwriting", "handwritten_note": "handwriting",
        "presentation": "slide", "invoice": "receipt", "bill": "receipt",
        "signage": "sign", "blackboard": "whiteboard", "chalkboard": "whiteboard",
    },
    "setting": {
        "inside": "indoor", "indoors": "indoor", "interior": "indoor",
        "outside": "outdoor", "outdoors": "outdoor", "exterior": "outdoor",
        "car": "vehicle", "in_vehicle": "vehicle", "inside_vehicle": "vehicle",
    },
    "time_of_day": {
        "daytime": "day", "morning": "day", "afternoon": "day", "midday": "day",
        "noon": "day", "sunrise": "sunrise_sunset", "sunset": "sunrise_sunset",
        "dawn": "sunrise_sunset", "dusk": "sunrise_sunset", "golden_hour": "sunrise_sunset",
        "twilight": "sunrise_sunset", "sunrise_or_sunset": "sunrise_sunset",
        "nighttime": "night", "night_time": "night",
    },
    "event": {
        "vacation": "travel", "trip": "travel", "sightseeing": "travel",
        "christmas": "holiday", "halloween": "holiday", "thanksgiving": "holiday",
        "easter": "holiday", "dinner": "meal", "lunch": "meal", "breakfast": "meal",
        "game": "sports", "sport": "sports", "match": "sports", "show": "concert",
        "business": "work", "meeting": "work", "conference": "work",
        "celebration": "party",
    },
    "sensitive": {
        "safe": "none", "false": "none", "nudity": "explicit", "sexual": "explicit",
        "nsfw": "explicit", "violence": "graphic", "violent": "graphic", "gore": "graphic",
        "blood": "graphic", "racy": "suggestive",
    },
    "mood": {
        "happy": "cheerful", "joyful": "cheerful", "peaceful": "calm", "serene": "calm",
        "relaxed": "calm", "exciting": "energetic", "sad": "somber", "sombre": "somber",
        "melancholy": "somber", "spooky": "eerie", "creepy": "eerie",
    },
    "quality_flags": {
        "blur": "blurry", "blurred": "blurry", "out_of_focus": "blurry",
        "motion_blur": "blurry", "underexposed": "dark", "too_dark": "dark",
        "washed_out": "overexposed", "too_bright": "overexposed",
        "pocket_shot": "accidental", "photo_of_screen": "screen_photo",
        "screen": "screen_photo", "moire": "screen_photo",
    },
}

_PEOPLE_WORDS = {
    "zero": 0, "nobody": 0, "no_one": 0, "noone": 0, "one": 1, "single": 1, "solo": 1,
    "person": 1, "two": 2, "couple": 2, "pair": 2, "three": 3, "four": 4, "five": 5,
    "few": 3, "a_few": 3, "several": 4, "group": 4, "small_group": 4, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "many": 8, "large_group": 11,
    "lots": 11, "crowd": 11,
}

_LANGUAGE_NAMES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "dutch": "nl", "russian": "ru", "chinese": "zh", "japanese": "ja",
    "korean": "ko", "arabic": "ar", "hebrew": "he", "hindi": "hi", "greek": "el",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi", "polish": "pl",
    "turkish": "tr", "czech": "cs", "ukrainian": "uk", "vietnamese": "vi", "thai": "th",
}
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")


def _repeats(text):
    """True when a short text field looks like a model stuck in a loop.

    One word (with a letter in it) used more than REPEAT_LIMIT times and
    making up at least a quarter of the words, or a long text made of a few
    phrases said over and over (under 30 % distinct four-word sequences).
    Numbers are not counted, so tables of figures are not mistaken for loops.
    """
    words = [word.lower() for word in _WORD_RE.findall(text)
             if any(char.isalpha() for char in word)]
    if len(words) <= REPEAT_LIMIT:
        return False
    _word, count = collections.Counter(words).most_common(1)[0]
    if count > REPEAT_LIMIT and count * 4 >= len(words):
        return True
    if len(words) >= 40:
        grams = [tuple(words[index:index + 4]) for index in range(len(words) - 3)]
        if len(set(grams)) < 0.3 * len(grams):
            return True
    return False


def _cut_loop(text):
    """text without a looping tail (see LOOP_REPEATS); one copy of what repeats is kept.

    A last line or phrase cut short (by the length limit) does not hide the
    loop before it.
    """
    lines = text.split("\n")
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if end >= 2:
        last, before = lines[end - 1].strip(), lines[end - 2].strip()
        if last != before and before.startswith(last):
            end -= 1            # an unfinished copy of the line before
    if end:
        last = lines[end - 1].strip()
        first = end - 1
        while first and lines[first - 1].strip() == last:
            first -= 1
        if last and end - first >= LOOP_REPEATS:
            text = "\n".join(lines[:first + 1]).rstrip()
    tokens = [(match.group(0), match.end()) for match in _TOKEN_RE.finditer(text)]
    words = [token for token, _end in tokens]
    for size in range(1, LOOP_PHRASE_WORDS + 1):
        for skip in range(size):
            # skip: words of an unfinished last copy.
            stop = len(words) - skip
            if stop < size * LOOP_REPEATS:
                continue
            # The earliest word from which the end repeats with this period.
            start = stop - size
            while start > 0 and words[start - 1] == words[start - 1 + size]:
                start -= 1
            if (stop - start) // size >= LOOP_REPEATS:
                return text[:tokens[start + size - 1][1]].rstrip()
    return text


def _as_text(value, multiline=False):
    """A reply value as tidy text ('' for anything that is not text or a number)."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value) if math.isfinite(value) else ""
    if isinstance(value, (list, tuple)):
        parts = [_as_text(item, multiline) for item in value]
        value = ("\n" if multiline else " ").join(part for part in parts if part)
    if not isinstance(value, str):
        return ""
    value = _SURROGATE_RE.sub("", value)
    if not multiline:
        return _SPACES_RE.sub(" ", value).strip()
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n")
             .split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _truncate(text, limit, at_word=True):
    """text cut to at most limit characters (at a word break when one is near)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if at_word:
        space = max(cut.rfind(" "), cut.rfind("\n"))
        if space >= limit * 0.7:
            cut = cut[:space]
    return cut.rstrip(" ,;:-\n")


def _enum_key(value):
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if isinstance(item, str)), "")
    if isinstance(value, bool) or value is None:
        return ""
    return _ENUM_SEPARATORS_RE.sub("_", str(value).strip().lower()).strip("_")


def _people_bucket(count):
    if count <= 0:
        return "none"
    if count <= 2:
        return str(count)
    if count <= 5:
        return "3-5"
    if count <= 10:
        return "6-10"
    return "crowd"


def _people(value):
    """The people enum from a count, a value of the enum, or common words."""
    if isinstance(value, bool):
        return "none"
    if isinstance(value, (int, float)):
        return _people_bucket(int(round(value))) if math.isfinite(value) else "none"
    text = _as_text(value).lower().replace("–", "-").replace("—", "-")
    text = re.sub(r"\s*-\s*", "-", text)
    if text in prompts.PEOPLE:
        return text
    match = re.match(r"^(\d+)(?:-(\d+))?\+?(?:\s+(?:people|persons?))?$", text)
    if match:
        return _people_bucket(int(match.group(2) or match.group(1)))
    key = _enum_key(text)
    if key in _PEOPLE_WORDS:
        return _people_bucket(_PEOPLE_WORDS[key])
    return "none"


def _enum(field, value):
    if field.name == "people":
        return _people(value)
    key = _enum_key(value)
    if key in field.values:
        return key
    alias = _ENUM_ALIASES.get(field.name, {}).get(key)
    if alias:
        return alias
    if key in _NOTHING:
        return field.empty
    return field.default


def _items(value):
    """A reply list as a Python list (a string becomes its comma-separated parts)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in re.split(r"[,;\n]+", value) if part.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _check_overflow(field, items):
    if len(items) > LIST_OVERFLOW * field.limit:
        raise ContentError("the reply repeats itself (%s has %d items)"
                           % (field.name, len(items)))


def _string_list(field, value):
    items = _items(value)
    _check_overflow(field, items)
    result, seen = [], set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("value") or item.get("text") or ""
        text = _as_text(item)
        if field.name == "tags":
            text = text.lstrip("#")
        text = text.strip(" .,;:\"'")
        if field.lower:
            text = text.lower()
        text = _truncate(text, prompts.ITEM_LENGTH)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= field.limit:
            break
    return result


def _flags(field, value):
    items = _items(value)
    _check_overflow(field, items)
    aliases = _ENUM_ALIASES.get(field.name, {})
    result = []
    for item in items:
        key = _enum_key(item)
        key = aliases.get(key, key)
        if key in field.values and key not in result:
            result.append(key)
    return result[:field.limit]


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return math.isfinite(value) and value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1")
    return False


def _number(value):
    """A finite number from a number or a string starting with one, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.match(r"^\s*(-?\d+(?:\.\d+)?)", value)
        value = float(match.group(1)) if match else None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _score(value):
    """An integer score clamped to 1..5 (rounded), or None when there is none."""
    number = _number(value)
    if number is None:
        return None
    low, high = prompts.SCORE_RANGE
    return int(min(high, max(low, math.floor(number + 0.5))))


def _objects(field, value):
    items = _items(value)
    _check_overflow(field, items)
    low, high = prompts.OBJECT_COUNT_RANGE
    result, seen = [], set()
    for item in items:
        if isinstance(item, dict):
            name, count = item.get("name") or item.get("object") or "", item.get("count")
        elif isinstance(item, str):
            match = re.match(r"^\s*(\d+)\s*[x×]?\s+(.+)$", item)
            name, count = (match.group(2), match.group(1)) if match else (item, 1)
        else:
            continue
        name = _truncate(_as_text(name).lower().strip(" .,;:\"'"), prompts.ITEM_LENGTH)
        number = _number(count)
        count = int(min(high, max(low, math.floor(number + 0.5)))) if number is not None else 1
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "count": count})
        if len(result) >= field.limit:
            break
    return result


def _language(value):
    """An ISO 639 code ('en', 'pt-br') from a code or a common language name, else ''."""
    text = _as_text(value).lower()
    if not text:
        return ""
    text = re.split(r"[\s,;/]+", text)[0].replace("_", "-")
    text = _LANGUAGE_NAMES.get(text, text)
    return text if _LANGUAGE_RE.match(text) else ""


def _text_field(field, value):
    if field.name == "text_language":
        return _language(value)
    multiline = field.name == "text"
    text = _as_text(value, multiline)
    if multiline:
        text = _cut_loop(text)
    elif _repeats(text):
        raise ContentError("the reply repeats itself (the %s field loops)" % field.name)
    if field.name == "title":
        text = text.strip("\"'").rstrip(".")
        text = text[:1].upper() + text[1:]
    elif field.name == "scene":
        text = text.lower().rstrip(".")
    return _truncate(text, field.limit, at_word=not multiline)


# Words a title made from a description should not end on.
_DANGLING_WORDS = ("a", "an", "the", "and", "or", "of", "on", "in", "at", "to", "with",
                   "by", "for", "from", "near", "under", "over", "while", "its", "their")


def _title_from(description):
    """A short title (the first sentence, at most 8 words) for a reply without one."""
    sentence = re.split(r"(?<=[.!?])\s", description, 1)[0]
    words = sentence.rstrip(".!?").split()[:8]
    while len(words) > 1 and words[-1].lower().strip(",;:") in _DANGLING_WORDS:
        words.pop()
    return _truncate(" ".join(words).rstrip(",;:"), 120)


def coerce_info(data, profile="core"):
    """A description dict with every field of the profile, checked and tidied.

    data is a parsed reply (dict). Values of the wrong type are converted
    where that is safe, enum values outside the schema become the field's
    "unknown"/"other"/"none" value, scores become integers 1-5 (None when
    missing), lists and strings are cut to their bounds, list items are
    trimmed and de-duplicated (tags lower-cased). A looping tail of the
    transcribed text is cut off (_cut_loop). Raises ContentError for another
    looping field (see _repeats, LIST_OVERFLOW).
    """
    info = {}
    for field in prompts.fields(prompts.check_profile(profile)):
        value = data.get(field.name)
        if field.kind == "text":
            info[field.name] = _text_field(field, value)
        elif field.kind == "enum":
            info[field.name] = _enum(field, value)
        elif field.kind == "list":
            info[field.name] = _string_list(field, value)
        elif field.kind == "flags":
            info[field.name] = _flags(field, value)
        elif field.kind == "bool":
            info[field.name] = _bool(value)
        elif field.kind == "score":
            info[field.name] = _score(value)
        elif field.kind == "objects":
            info[field.name] = _objects(field, value)
    if not info.get("title") and info.get("description"):
        info["title"] = _title_from(info["description"])
    return info


def parse_reply(text, profile="core"):
    """Turn an LLM reply into a description dict (see coerce_info).

    Tolerant: the JSON object is found even inside Markdown fences or other
    text, and values are tidied rather than rejected. Raises ContentError
    when there is no JSON object, when the reply has no description, or
    when it repeats itself.
    """
    try:
        data = llm.extract_json(text or "")
    except ValueError as error:
        raise ContentError(str(error))
    if not isinstance(data, dict):
        raise ContentError("the reply is not a JSON object")
    info = coerce_info(data, profile)
    if not info.get("description"):
        raise ContentError("the reply has no description")
    return info


Repaired = collections.namedtuple("Repaired", "info missing looped")


def repair(text, profile="core"):
    """A description from a reply that was cut off (llm.repair_json), or None.

    Only a reply that got as far as both a description and a title is
    used. Returns Repaired(info, missing, looped): info as coerce_info
    makes it (the fields after the cut get their empty values), missing
    the names of the fields the reply did not reach (an enum field the cut
    came in or right after counts as missing: its value may be cut short),
    looped True when the transcribed text ended in a loop (_cut_loop
    trimmed it), which is likely why the reply ran out of room.
    """
    try:
        data = llm.repair_json(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or not _as_text(data.get("description")) \
            or not _as_text(data.get("title")):
        return None
    try:
        info = coerce_info(data, profile)
    except ContentError:
        return None
    if not info.get("description") or not info.get("title"):
        return None
    fields = prompts.fields(prompts.check_profile(profile))
    missing = [field.name for field in fields if field.name not in data]
    last = next(iter(reversed(list(data))), None)
    if any(field.name == last and field.kind == "enum" for field in fields):
        missing.append(last)
    raw = _as_text(data.get("text"), multiline=True)
    return Repaired(info, missing, bool(raw) and _cut_loop(raw) != raw)


def repair_reply(text, profile="core"):
    """The description of repair() (or None): a cut-off reply's fields up to the cut."""
    repaired = repair(text, profile)
    return repaired.info if repaired is not None else None


# The privacy fields, and how strong each "sensitive" value is.
_SENSITIVE_ORDER = {value: index for index, value in enumerate(prompts.SENSITIVE)}


def fill_missing(info, missing, previous=None):
    """A cut-off reply's description made safe to store (a new dict).

    missing names the fields the reply did not reach (repair()). With
    previous (the description it replaces, as in the refine pass), those
    fields keep their previous values, and the privacy fields can only get
    stronger: private_info stays True when either says so, and the more
    serious "sensitive" value is kept. Without one, a private_info the
    reply did not reach is True (unknown is treated as possibly private, so
    exports leave the picture out unless private pictures are included).
    """
    info = dict(info)
    missing = set(missing or ())
    if isinstance(previous, dict):
        for name in missing:
            if name in previous:
                info[name] = copy.deepcopy(previous[name])
        info["private_info"] = bool(info.get("private_info")) or \
            previous.get("private_info") is True
        before = previous.get("sensitive")
        if _SENSITIVE_ORDER.get(before, -1) > _SENSITIVE_ORDER.get(info.get("sensitive"), -1):
            info["sensitive"] = before
    elif "private_info" in missing:
        info["private_info"] = True
    return info


# ---------------------------------------------------------------------------
# What to describe, and in which order

def selected_libraries(config, library_ids=None):
    """Enabled libraries, limited to library_ids (ids) when given."""
    libraries = common.enabled_libraries(config)
    if library_ids is not None:
        wanted = set(library_ids)
        libraries = [library for library in libraries if library.get("id") in wanted]
    return [library for library in libraries if library.get("id")]


def library_profile(library):
    """The metadata profile a library asks for ("core" unless "extended")."""
    value = library.get("metadata") if isinstance(library, dict) else None
    return prompts.check_profile(value)


def capture_sort_key(row):
    """Sort key for describe order: the capture date as text, then the id.

    Comparing 'YYYY-MM-DDTHH:MM:SS' strings never converts to epoch seconds,
    so dates before 1970 work everywhere.
    """
    return (row["taken_at"] or imageinfo.local_iso(row["mtime"]), row["id"])


_PENDING_SQL = """
    SELECT id, library_id, path, name, content_hash, taken_at, mtime, described,
           prompt_ver, describe_attempts, has_preview, projection, hidden, media, duration,
           CASE WHEN media = 'video' THEN exif ELSE '' END AS video_exif
    FROM images
    WHERE library_id = ? AND missing_since IS NULL AND (described = 0 OR %s)
    ORDER BY id"""


def pending_rows(conn, libraries, newest_first=None, retry_failed=False,
                 redescribe_stale=False, ids=None, force=False, stale_sql=None):
    """The images to describe, in order. Returns (rows, skipped_failed).

    Images that have never been described come first; then, only when
    redescribe_stale is set, those whose description is out of date
    (stale_sql, default prompts.stale_sql(): prompt_ver before
    prompts.CURRENT_SINCE for their media, or a video's description that
    talks about its frames). Within each group, libraries keep their
    config order, and each library is sorted by capture date: newest first
    when newest_first is True, oldest first when False, and by the
    library's own "newest_first" setting when None -- except that images
    which failed in an earlier run come after the others, so a picture an
    endpoint cannot handle never holds up the rest. Missing files are left
    out, and so are images that failed MAX_FAILURES times unless
    retry_failed is set; those are counted in skipped_failed. Hidden images
    are described too.

    ids (image ids) limits the images to those. With ids, force also
    takes the ones that already have a description, and the ones that
    failed too often (someone asked for exactly these pictures); force
    without ids changes nothing.
    """
    fresh, stale = [], []
    skipped_failed = 0
    wanted = set(ids) if ids is not None else None
    force = bool(force) and wanted is not None
    if force:
        retry_failed = True
    if force:
        condition = "1"
    elif redescribe_stale:
        condition = stale_sql or prompts.stale_sql()
    else:
        condition = "0"
    sql = _PENDING_SQL % condition
    for library in libraries:
        rows = conn.execute(sql, (library["id"],)).fetchall()
        if wanted is not None:
            # Filtered here, not in SQL: up to 1000 ids would pass the
            # parameter limit of older SQLite versions.
            rows = [row for row in rows if row["id"] in wanted]
        if not retry_failed:
            kept = [row for row in rows if row["describe_attempts"] < MAX_FAILURES]
            skipped_failed += len(rows) - len(kept)
            rows = kept
        order = library.get("newest_first", True) if newest_first is None else newest_first
        rows.sort(key=capture_sort_key, reverse=bool(order))
        rows.sort(key=lambda row: row["describe_attempts"] > 0)     # stable
        fresh.extend(row for row in rows if not row["described"])
        stale.extend(row for row in rows if row["described"])
    return fresh + stale, skipped_failed


def stale_count(conn, libraries, stale_sql=None):
    """How many present images of these libraries have an out-of-date description
    (stale_sql, default prompts.stale_sql())."""
    ids = [library["id"] for library in libraries]
    if not ids:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM images WHERE missing_since IS NULL AND described = 1 "
        "AND %s AND library_id IN (%s)" % (stale_sql or prompts.stale_sql(),
                                           ",".join("?" * len(ids))),
        ids).fetchone()[0]


# ---------------------------------------------------------------------------
# One image

def _error_text(error, secrets=()):
    """An error as text for logs and the database: keys masked first, then cut to 500 characters."""
    return common.redact(str(error) or type(error).__name__, secrets)[:500]


def _tmp_path(image_id):
    name = "%s%d-%d-%d.jpg" % (TMP_PREFIX, image_id, os.getpid(), threading.get_ident())
    return os.path.join(common.ensure_dir(common.tmp_dir()), name)


def clean_tmp():
    """Delete temporary describe files left behind by an earlier run.

    Only call this while holding the worker lock, when no other describe
    run can be using them.
    """
    folder = common.tmp_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(TMP_PREFIX):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def _source_path_style(call_api):
    """How a call_api hook takes the original's path: "keyword", "positional" or None.

    Hooks written as call_api(prompt, image_path) only get the temporary
    JPEG. A hook with a source_path parameter (or **kwargs) gets it as
    source_path=...; one with a third positional parameter gets it there.
    """
    try:
        parameters = list(inspect.signature(call_api).parameters.values())
    except (TypeError, ValueError):
        return None
    kinds = inspect.Parameter
    if any(parameter.kind == kinds.VAR_KEYWORD or (
            parameter.name == "source_path"
            and parameter.kind in (kinds.POSITIONAL_OR_KEYWORD, kinds.KEYWORD_ONLY))
           for parameter in parameters):
        return "keyword"
    positional = [parameter for parameter in parameters
                  if parameter.kind in (kinds.POSITIONAL_ONLY, kinds.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3 or any(parameter.kind == kinds.VAR_POSITIONAL
                                   for parameter in parameters):
        return "positional"
    return None


def _helper_request(helper, helper_lock, prompt, jpeg, image_id, source_path="",
                    read_helper=None):
    """Run the helper's call_api hook. Returns (prompt, image bytes, mime).

    The hook takes (prompt, image_path); prompt is the user text sent after
    the image (the system prompt is sent as it is). The prepared JPEG is
    written to a temporary file under data/tmp for it. Hooks that accept a
    third argument (see _source_path_style) also get the original file's
    path. The hook may return None (send as is), or a new (prompt,
    image_path) pair; a different path is read through the helper's
    load_image like any original (through read_helper when given: see
    common.thread_safe_helper).
    """
    tmp = _tmp_path(image_id)
    try:
        with open(tmp, "wb") as handle:
            handle.write(jpeg)
        style = _source_path_style(helper.call_api)
        with helper_lock:
            if style == "keyword":
                changed = helper.call_api(prompt, tmp, source_path=source_path)
            elif style == "positional":
                changed = helper.call_api(prompt, tmp, source_path)
            else:
                changed = helper.call_api(prompt, tmp)
        if changed is None:
            return prompt, jpeg, "image/jpeg"
        prompt, path = changed
        if os.path.abspath(path) == os.path.abspath(tmp):
            # The hook may have edited the file in place.
            with open(tmp, "rb") as handle:
                data = handle.read()
        else:
            data = common.load_image_bytes(read_helper if read_helper is not None else helper,
                                           path)
        return prompt, data, llm.image_mime(data, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _projection(row):
    """The row's 360-degree projection ("" for other rows and older databases)."""
    return row["projection"] if "projection" in row.keys() else ""


def _is_video(row):
    """True for a video's row (see videos.py; False for older databases)."""
    return "media" in row.keys() and row["media"] == videos.MEDIA_VIDEO


def _stored_frames(row):
    """The frames on a video row's stored contact sheet (recorded when it was made), or None."""
    if row["has_preview"] != 1 or "video_exif" not in row.keys():
        return None
    return videos.sheet_frames(row["video_exif"])[0]


def _video(row, frames):
    """(frames on its contact sheet, seconds) of a video row, for prompts.user_text; else None.

    The stored sheet's own count when it is known (a short video's sheet
    can hold fewer frames than asked for), else frames (the setting).
    """
    if not _is_video(row):
        return None
    duration = row["duration"] if "duration" in row.keys() else None
    if not duration:
        return (1, duration)
    return (_stored_frames(row) or frames, duration)


def video_for_llm(row, root, previews, max_edge=common.DEFAULT_MAX_IMAGE_EDGE,
                  frames=videos.DEFAULT_FRAMES):
    """(JPEG bytes, frames on it or None when not known) of a video row's contact sheet.

    The sheet kept with the previews, else one made now with frames
    frames (see image_for_llm).
    """
    data = (previews.read_sheet(row["library_id"], row["id"])
            if row["has_preview"] == 1 else None)
    count = _stored_frames(row) if data is not None else None
    if data is None:
        full = os.path.join(root, *row["path"].split("/"))
        data, count = videos.sheet_jpeg(videos.find_tools(), full, edge=max_edge,
                                        frames=frames)
    return images.prepare_for_llm(data, max_edge=max_edge), count


def video_text(user_text, row, planned_frames, count):
    """user_text with the video note saying count frames instead of planned_frames' count.

    planned_frames is what _video(row, ...) was given when the text was
    made; count what the sheet sent really holds (None: as planned).
    """
    planned = _video(row, planned_frames)
    if planned is None or not count or count == planned[0]:
        return user_text
    return user_text.replace(prompts.video_note(*planned), prompts.video_note(count, planned[1]))


def image_for_llm(row, root, previews, helper=None, max_edge=common.DEFAULT_MAX_IMAGE_EDGE,
                  frames=videos.DEFAULT_FRAMES):
    """The JPEG bytes to send for an image row.

    Uses the cached preview when there is one, else the original (read
    through the helper's load_image when it has one). Either way the image is
    upright, flattened and shrunk by images.prepare_for_llm() to a long edge
    of max_edge (the endpoint's max_image_edge). A video sends its contact
    sheet (kept with the previews, or made now with frames frames). Raises
    images.SourceReadError when the original cannot be read and
    images.ImageReadError (videos.VideoReadError for a video;
    videos.ToolsMissing without ffmpeg) when it cannot be decoded.
    """
    if _is_video(row):
        return video_for_llm(row, root, previews, max_edge, frames)[0]
    if row["has_preview"] == 1:
        data = previews.read(row["library_id"], row["id"])
        if data is not None:
            try:
                return images.prepare_for_llm(data, max_edge=max_edge)
            except images.ImageReadError:
                pass        # a damaged preview: fall back to the original
    # An unstitched 360 original is stitched first, as its preview was.
    projection = row["projection"] if "projection" in row.keys() else None
    full = os.path.join(root, *row["path"].split("/"))
    return images.prepare_for_llm(images.read_source_bytes(full, helper),
                                  max_edge=max_edge, projection=projection)


def retry_extra(endpoint):
    """Request fields for asking a local model again after an unusable reply.

    A small repeat penalty and a new random seed, in the form the server
    understands; None for cloud services (their sampling already varies).
    """
    settings = common.endpoint_settings(endpoint)
    seed = random.randint(1, 2 ** 31 - 1)
    if settings["kind"] == "ollama":
        return {"options": {"repeat_penalty": RETRY_REPEAT_PENALTY, "seed": seed}}
    preset = settings["preset"]
    if preset == "vllm":
        return {"repetition_penalty": RETRY_REPEAT_PENALTY, "seed": seed}
    if preset == "ollama-openai":
        return {"seed": seed}
    if preset in common.LOCAL_PRESETS:
        return {"repeat_penalty": RETRY_REPEAT_PENALTY, "seed": seed}
    return None


_FINISH_TEXT = {"length": "the reply was cut off (max_output_tokens reached)",
                "refusal": "the model refused to describe the image",
                "filtered": "the service's content filter blocked the reply"}


def describe_jpeg(endpoint, jpeg, profile="core", user_text=None, helper=None,
                  helper_lock=None, image_id=0, source_path="", should_stop=None,
                  sleep=time.sleep, max_retries=None, read_helper=None, content_retries=None):
    """Describe prepared image bytes with one endpoint. Never raises.

    Sends the profile's system prompt and schema with user_text (default:
    prompts.user_text(None)), retrying as described in the module docstring
    (rate limits and server errors through llm.describe_with_retries, then
    up to CONTENT_RETRIES more requests for unusable replies, with
    retry_extra() for local servers). helper's call_api hook, if any, sees
    every request (image_id and source_path are passed on to it); an image
    it swaps in is read through read_helper (default: helper).
    max_retries and content_retries (default MAX_RETRIES, CONTENT_RETRIES)
    limit the two kinds of retries; 0 and 0 send a single request.

    A reply cut off at the length limit is closed (repair) and used when
    it got as far as the description and the title ("truncated" is then
    True), unless its transcribed text ended in a loop: then, as for a
    reply whose JSON breaks off, the image is asked for again, and the
    repaired reply is only used when the retries do no better ("truncated"
    or "repaired"; "missing" names the fields it did not reach). A reply
    cut off before the title is asked for again (once) with twice the
    endpoint's max_output_tokens; when the endpoint refuses that many, the
    image's remaining tries use its own setting.

    Returns {"status": "ok" or "error", "kind" (a ProviderError kind, or
    "local" for a failure on this side; None when ok), "error" (redacted
    text), "exception" (the ProviderError, if any), "info" (parse_reply's
    dict when ok), "truncated", "repaired", "missing", "model" (as the
    reply names it), "usage" (of the last reply), "finish", "seconds" (time
    spent in requests), "requests" (HTTP requests that carried the image,
    retries included), "tokens_in", "tokens_out" and "cost" (totals over
    every reply; None when unknown), "content_errors"}.
    """
    settings = common.endpoint_settings(endpoint)
    request_settings = settings
    key, _source = common.resolve_api_key(settings)
    secrets = [key] if key else []
    profile = prompts.check_profile(profile)
    system = prompts.system_prompt(profile)
    schema = prompts.schema(profile)
    if user_text is None:
        user_text = prompts.user_text(None)
    helper_lock = helper_lock or threading.Lock()
    result = {"status": "error", "kind": None, "error": "", "exception": None, "info": None,
              "truncated": False, "repaired": False, "missing": [],
              "model": "", "usage": {"input_tokens": None, "output_tokens": None},
              "finish": "", "seconds": 0.0, "requests": 0, "tokens_in": None,
              "tokens_out": None, "cost": None, "content_errors": 0}
    retries = MAX_RETRIES if max_retries is None else max_retries

    def count_retry(_error, _delay):
        result["requests"] += 1

    def add(key_name, value):
        if value is not None:
            result[key_name] = (result[key_name] or 0) + value

    def use(repaired, cut):
        """The result for a repaired reply (cut: it stopped at the length limit)."""
        result.update(status="ok", info=repaired.info, missing=list(repaired.missing),
                      truncated=cut, repaired=not cut, kind=None, error="", exception=None)
        return result

    last_error = None
    fallback = None         # (Repaired, cut): the best repaired reply so far
    doubled = False         # more room was asked for already
    for attempt in range(1 + (CONTENT_RETRIES if content_retries is None else content_retries)):
        if attempt and should_stop is not None and should_stop():
            break
        text, data, mime = user_text, jpeg, "image/jpeg"
        started = time.monotonic()
        try:
            if helper is not None and hasattr(helper, "call_api"):
                text, data, mime = _helper_request(helper, helper_lock, text, jpeg, image_id,
                                                   source_path, read_helper)
            reply = llm.describe_with_retries(
                request_settings, system, text, data, schema, should_stop=should_stop,
                max_retries=retries, sleep=sleep, mime=mime,
                request_extra=retry_extra(settings) if attempt else None,
                on_retry=count_retry, max_timeout_retries=TIMEOUT_RETRIES)
        except llm.ProviderError as error:
            result["requests"] += 1
            result["seconds"] += time.monotonic() - started
            if (request_settings is not settings and error.kind in ("endpoint", "image")
                    and error.status in (400, 413, 422)):
                # The endpoint refused room for a longer answer (more output
                # tokens than its model allows): this image's answer does not
                # fit, which is not the endpoint's fault. The image's other
                # tries use the endpoint's own setting.
                request_settings = settings
                last_error = ContentError("%s, and the endpoint refused room for a longer one "
                                          "(%s)" % (_FINISH_TEXT["length"],
                                                    _error_text(error, secrets)))
                result["content_errors"] += 1
                continue
            if fallback is not None and error.kind != "fatal":
                return use(*fallback)
            result.update(kind=error.kind, error=_error_text(error, secrets), exception=error)
            return result
        except Exception as error:
            if fallback is not None:
                return use(*fallback)
            result.update(kind="local", error=_error_text(error, secrets))
            return result
        result["requests"] += 1
        result["seconds"] += reply["seconds"]
        usage = reply["usage"]
        add("tokens_in", usage.get("input_tokens"))
        add("tokens_out", usage.get("output_tokens"))
        add("cost", llm.estimate_cost(settings, usage))
        result.update(model=reply["model"], usage=usage, finish=reply["finish"])
        cut = reply["finish"] == "length"
        if reply["finish"] not in ("ok", "length"):
            message = _FINISH_TEXT.get(reply["finish"], reply["finish"])
            if reply.get("detail"):
                message += ": " + reply["detail"]
            last_error = ContentError(message)
        else:
            try:
                if cut:
                    raise ContentError(_FINISH_TEXT["length"])
                info = parse_reply(reply["text"], profile)
            except ContentError as error:
                last_error = error
                repaired = repair(reply["text"], profile)
                if repaired is not None:
                    if cut and not repaired.looped:
                        # The answer was simply longer than the room it had.
                        return use(repaired, cut)
                    if fallback is None or len(repaired.missing) < len(fallback[0].missing):
                        fallback = (repaired, cut)
                    if repaired.looped:
                        last_error = ContentError("the reply was cut off while its text "
                                                  "repeated itself")
                elif cut and not doubled:
                    # Asked again (once) with room for twice as long an answer.
                    doubled = True
                    request_settings = dict(settings, max_output_tokens=min(
                        settings["max_output_tokens"] * 2, MAX_OUTPUT_TOKENS))
            else:
                result.update(status="ok", info=info)
                return result
        result["content_errors"] += 1
        llm.log_error(reply["text"], last_error, secrets)
    if fallback is not None:
        return use(*fallback)
    result.update(kind="content", error=_error_text(last_error or "stopped", secrets))
    return result


def describe_checked(endpoint, jpeg, kind="video", user_text=None, **options):
    """describe_jpeg for a contact sheet: a video's (kind "video") or a drive's ("drive").

    When the reply's title or description talks about the sheet instead
    of the footage (sheet_talk.in_info), the same endpoint is asked once
    more with the same picture and user_text plus sheet_talk.reask_note(kind).
    That second request waits out rate limits and server errors like any
    other (options' max_retries, default MAX_RETRIES), but an unusable
    reply is not asked for again (no content retries): never more than one
    extra answer. The second reply is kept when it is better: complete
    (not cut off or repaired) where the first was not, or as complete and
    with fewer phrases of sheet talk (none: fixed). Otherwise the first is
    kept: on a tie (the second did no better, and it answered the usual
    prompt), when the second is incomplete and the first is not (its
    missing fields would be guessed), and when the second request gets no
    usable answer. options are passed on to describe_jpeg (the endpoint's
    hidden-picture rules are the caller's, as for any request).

    Returns describe_jpeg's dict for the reply kept, with the requests,
    seconds, tokens, cost and content errors of both requests, plus
    "reasked": None when the first reply was kept as it is, else {"phrases":
    the first reply's sheet talk, "kept": "first" or "second", "fixed": the
    second reply was kept and has none, "left": the sheet talk of the reply
    kept, "partial": the second reply was cut off or repaired, "settled":
    the model answered the second request (the description then counts as
    asked again, even when it still has sheet talk), "error": why the
    second request got no usable answer, or ""}.
    """
    first = describe_jpeg(endpoint, jpeg, user_text=user_text, **options)
    first["reasked"] = None
    phrases = sheet_talk.in_info(first["info"]) if first["status"] == "ok" else []
    should_stop = options.get("should_stop")
    if not phrases or (should_stop is not None and should_stop()):
        return first
    text = user_text if user_text is not None else prompts.user_text(None)
    options = dict(options, content_retries=0)
    second = describe_jpeg(endpoint, jpeg, user_text=text + "\n\n" + sheet_talk.reask_note(kind),
                           **options)
    later = sheet_talk.in_info(second["info"]) if second["status"] == "ok" else None
    if later is not None and (_partial(second), len(later)) < (_partial(first), len(phrases)):
        keep = second           # complete, with no sheet talk or less of it
    else:
        keep = first            # no answer, or no better (a tie keeps the first)
    result = dict(keep)
    for key in ("requests", "seconds", "content_errors"):
        result[key] = first.get(key, 0) + second.get(key, 0)
    for key in ("tokens_in", "tokens_out", "cost"):
        values = [item[key] for item in (first, second) if item.get(key) is not None]
        result[key] = sum(values) if values else None
    result["reasked"] = {
        "phrases": phrases, "kept": "second" if keep is second else "first",
        "fixed": keep is second and later == [], "left": later if keep is second else phrases,
        "partial": later is not None and _partial(second),
        "settled": second["status"] == "ok" or second.get("kind") in ("content", "image"),
        "error": "" if second["status"] == "ok" else second.get("error") or "no answer"}
    return result


def _partial(result):
    """True when a describe_jpeg result's reply was cut off or repaired (fields guessed)."""
    return bool(result.get("truncated") or result.get("repaired") or result.get("missing"))


def reask_note(reasked):
    """What a log line says about a describe_checked result's "reasked" ("" for None)."""
    if not reasked:
        return ""
    what = "it described the frames (%s), asked again: " % ", ".join(
        '"%s"' % phrase for phrase in reasked["phrases"][:sheet_talk.MAX_PHRASES])
    if reasked["fixed"]:
        return what + "fixed"
    if not reasked["settled"]:
        return what + ("the second request failed (%s); kept the first answer, still out of "
                       "date" % reasked["error"])
    if reasked["error"]:
        return what + ("the second answer could not be used (%s); kept the first answer"
                       % reasked["error"])
    if reasked["kept"] == "first" and reasked["partial"]:
        return what + "the second answer was incomplete; kept the first answer"
    return what + "still about the frames; kept the %s answer" % reasked["kept"]


def reask_provenance(provenance, reasked):
    """Record a describe_checked result's "reasked" in a provenance dict (when settled)."""
    if reasked and reasked["settled"]:
        provenance["reasked"] = sheet_talk.REASKED
        provenance["reask_kept"] = reasked["kept"]
        if reasked["left"]:
            provenance["sheet_talk"] = reasked["left"][:sheet_talk.MAX_PHRASES]
    return provenance


def reask_count(summary, reasked):
    """Count a describe_checked result's "reasked" in a run's summary: reasked (the
    model answered the second request), reask_fixed and reask_failed (it did not)."""
    if not reasked:
        return
    if reasked["settled"]:
        summary["reasked"] += 1
        summary["reask_fixed"] += 1 if reasked["fixed"] else 0
    else:
        summary["reask_failed"] += 1


def reask_summary(what, summary):
    """The run's line about descriptions asked again (summary's reasked, reask_fixed and
    reask_failed; "" when there were none)."""
    parts = []
    if summary.get("reasked"):
        parts.append("%d %s description(s) asked again because they described the frames; "
                     "%d fixed." % (summary["reasked"], what, summary.get("reask_fixed", 0)))
    if summary.get("reask_failed"):
        parts.append("%d %s description(s) described the frames but could not be asked again "
                     "(the second request failed): they stay out of date."
                     % (summary["reask_failed"], what))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Storing results

_STORE_SQL = """
    UPDATE images SET info = ?, described = 1, prompt_ver = ?, provenance = ?,
        describe_attempts = 0, describe_error = '', updated = ?
    WHERE id = ? AND content_hash = ?"""

# Exact copies (same content hash) that still need a description get the
# same one, so each picture is only sent once (the WHERE clause is added).
_SHARE_SQL = """
    UPDATE images SET info = ?, described = 1, prompt_ver = ?, provenance = ?,
        describe_attempts = 0, describe_error = '', updated = ?
    WHERE """


def store_description(conn, row, info, provenance, library_ids=None, include_stale=False,
                      also_ids=None, stale_sql=None):
    """Save a description for a row and its exact copies. Returns (stored, shared).

    Copies only get it when they have no description yet (or, with
    include_stale, one that is out of date: stale_sql, default
    prompts.stale_sql(); or when their id is in
    also_ids: pictures someone asked to describe again), and only in
    library_ids when given: libraries that would ask for the same thing
    (metadata profile and context notes). stored is False when the row
    changed or vanished while it was being described (its content hash no
    longer matches); nothing is saved then.
    """
    now = time.time()
    info_text = json.dumps(info, ensure_ascii=False, allow_nan=False)
    provenance_text = json.dumps(provenance, ensure_ascii=False, allow_nan=False)
    stored = conn.execute(_STORE_SQL, (info_text, PROMPT_VER, provenance_text, now,
                                       row["id"], row["content_hash"])).rowcount == 1
    shared = 0
    stale_sql = stale_sql or prompts.stale_sql()
    if stored and row["content_hash"] and (library_ids is None or library_ids):
        values = [info_text, PROMPT_VER, provenance_text, now]
        where = "content_hash = ? AND id <> ? AND missing_since IS NULL"
        where_args = [row["content_hash"], row["id"]]
        if library_ids is not None:
            library_ids = list(library_ids)
            where += " AND library_id IN (%s)" % ",".join("?" * len(library_ids))
            where_args += library_ids
        forced = []
        if also_ids:
            # Copies the update below leaves alone (their description is
            # current) that were asked to be described again.
            current = " AND described = 1" + (" AND NOT " + stale_sql
                                              if include_stale else "")
            forced = [found[0] for found in conn.execute(
                "SELECT id FROM images WHERE " + where + current, where_args)
                if found[0] in also_ids]
        if include_stale:
            where += " AND (described = 0 OR %s)" % stale_sql
        else:
            where += " AND described = 0"
        shared = conn.execute(_SHARE_SQL + where, values + where_args).rowcount
        for image_id in forced:
            shared += conn.execute(_SHARE_SQL + "id = ?", values + [image_id]).rowcount
    conn.commit()
    return stored, max(0, shared)


def record_failure(conn, row, error):
    """Count a failed describe attempt for a row and keep its last error.

    A description the row already has (from an older prompt) is kept.
    """
    conn.execute("UPDATE images SET describe_attempts = describe_attempts + 1, "
                 "describe_error = ? WHERE id = ?", (error[:500], row["id"]))
    conn.commit()


def _json_dict(text):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _who(settings):
    """'The OpenAI endpoint 'cloud'', for messages."""
    return "The %s endpoint '%s'" % (common.ENDPOINT_PRESETS[settings["preset"]]["label"],
                                     settings["name"] or settings["model"])


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _duration(minutes):
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return "%d min" % round(minutes)
    return "%d h %02d min" % divmod(int(round(minutes)), 60)


# ---------------------------------------------------------------------------
# The describe run

Task = collections.namedtuple("Task", "position row route root profile user_text")


class _Endpoint:
    """One endpoint's state during a run."""

    def __init__(self, settings):
        self.settings = settings
        self.name = settings["name"]
        self.concurrency = 1
        self.served_model = None    # what the server says it runs (llm.probe)
        self.in_flight = 0
        self.failures = 0           # failures in a row that were not the images' fault
        self.suspects = []          # (task, error) of those that may be the image's
        self.succeeded = False      # described an image in this run
        self.retired = False
        key, _source = common.resolve_api_key(settings)
        self.secrets = [key] if key else []

    def label(self):
        preset = common.ENDPOINT_PRESETS[self.settings["preset"]]["label"]
        return "%s (%s)" % (self.name, preset)

    def model_name(self, reply_model=""):
        """The model to record: what the server reports, else the configured name.

        llama.cpp answers with whatever model name the request gave, so its
        /props (probed before the run) is trusted first.
        """
        if self.settings["preset"] in common.LLAMACPP_PRESETS and self.served_model:
            return self.served_model
        return reply_model or self.served_model or self.settings["model"]


def _new_summary():
    return {"described": 0, "shared": 0, "failed": 0, "skipped": 0, "skipped_failed": 0,
            "skipped_offline": 0, "skipped_hidden": 0, "pending": 0, "stale": 0,
            "stopped": False, "error": "", "fatal": False, "retired": [], "requests": 0,
            "tokens_in": 0, "tokens_out": 0, "cost": None, "reasked": 0, "reask_fixed": 0,
            "reask_failed": 0}


def describe_pending(conn, config, ctx, helper=None, library_ids=None, newest_first=None,
                     retry_failed=False, redescribe_stale=False, limit=None,
                     endpoint_name=None, previews=None, sleep=time.sleep, ids=None,
                     force=False, servers=None):
    """Describe the images that need it. Returns a summary dict.

    conn is an index.db connection; ctx a jobs.JobContext. Only enabled
    libraries are described (limited to library_ids when given): images
    without a description, plus those whose description is out of date (an
    older prompt, or a video's description that talks about its frames) when
    redescribe_stale is set (see pending_rows), at most limit of them.
    ids (image ids) limits the run to those pictures; with ids, force
    describes them again even when they have a description (the
    "Re-describe this picture" button), and exact copies among them get
    the new description too. Each library uses its own endpoint(s)
    unless endpoint_name names an endpoint
    or group for all of them. Stops early, keeping every saved description,
    when ctx asks to stop, on a fatal error, or when no usable endpoint is
    left. The helper's before_launch and after_launch hooks bracket the run;
    after_launch runs even when the run fails. sleep is used for retry
    pauses (tests pass a stub).

    Images without a preview are read from their library's folder; when
    that folder cannot be reached (an offline share) they are left for a
    later run (skipped_offline) instead of failing one by one. Hidden
    pictures are only sent to endpoints on this computer or the local
    network; those that would go elsewhere are skipped (skipped_hidden).
    A model server that config.json's local_server runs for an endpoint is
    started first when needed (see local_server.Lease; servers is the
    local_server.ServerManager to use, by default the process's own).

    Summary keys: described, shared (copies that got the same description),
    failed, skipped, skipped_failed, skipped_offline, skipped_hidden,
    pending, stale (out of date and not included: see pending_rows),
    stopped, error, fatal, retired (endpoint names), requests, tokens_in,
    tokens_out, cost (None when unknown), reasked, reask_fixed and
    reask_failed (videos asked again because their description talked
    about the frames, how many of those the second answer fixed, and how
    many could not be asked again because the second request failed: see
    describe_checked).

    A run over whole libraries (no ids, no limit) then describes the drives
    that need it (runs of video clips, see drives.describe_drives), with a
    model server lease held over both, so a server started for the pictures
    is not stopped and started again for the drives; their summary is in
    summary["drives"], and their requests, tokens and cost count in the totals.
    """
    options = dict(helper=helper, library_ids=library_ids, newest_first=newest_first,
                   retry_failed=retry_failed, redescribe_stale=redescribe_stale, limit=limit,
                   endpoint_name=endpoint_name, previews=previews, sleep=sleep, ids=ids,
                   force=force, servers=servers)
    if ids is not None or limit is not None:
        return _describe_pictures(conn, config, ctx, **options)
    import drives
    import local_server     # stdlib only; imported here to keep this module's imports short
    work, offline = drives.reachable(drives.pending(
        conn, config, selected_libraries(config, library_ids), redescribe_stale, retry_failed),
        ctx, FOLDER_CHECK_TIMEOUT)
    if not work:
        return _describe_pictures(conn, config, ctx, **options)
    lease = local_server.lease_for(ctx, config,
                                   drives.endpoint_names(config, work, endpoint_name),
                                   manager=servers)
    try:
        lease.acquire()     # (each pass finds and reports its own problems)
        summary = _describe_pictures(conn, config, ctx, **options)
        if not summary["stopped"] and not summary["fatal"] and not ctx.should_stop():
            summary["drives"] = drives.describe_drives(
                conn, config, ctx, selected_libraries(config, library_ids),
                endpoint_name=endpoint_name, redescribe_stale=redescribe_stale,
                retry_failed=retry_failed, servers=servers, sleep=sleep, work=work)
            summary["drives"]["skipped"] += offline
            summary["stopped"] = summary["drives"]["stopped"]
            # (What the drives used counts in the run's totals.)
            used = summary["drives"]
            for key in ("requests", "tokens_in", "tokens_out"):
                summary[key] += used[key]
            if used["cost"] is not None:
                summary["cost"] = (summary["cost"] or 0.0) + used["cost"]
            if used["tokens_in"] or used["tokens_out"]:
                ctx.log("In all (pictures and drives):" + drives.usage_text(summary))
    finally:
        lease.release()
    return summary


def _describe_pictures(conn, config, ctx, helper=None, library_ids=None, newest_first=None,
                       retry_failed=False, redescribe_stale=False, limit=None,
                       endpoint_name=None, previews=None, sleep=time.sleep, ids=None,
                       force=False, servers=None):
    """describe_pending for the pictures (the drives come after them)."""
    summary = _new_summary()
    libraries = selected_libraries(config, library_ids)
    if ids is not None:
        ids = {image_id for image_id in ids
               if isinstance(image_id, int) and not isinstance(image_id, bool)}
    force = bool(force) and ids is not None
    stale = prompts.stale_sql(config)
    rows, summary["skipped_failed"] = pending_rows(conn, libraries, newest_first,
                                                   retry_failed, redescribe_stale,
                                                   ids=ids, force=force, stale_sql=stale)
    rows, summary["skipped_offline"] = _drop_unreachable(rows, libraries, ctx, helper)
    if not redescribe_stale and ids is None:
        # (Not for a run over chosen pictures: the hint is about the library.)
        summary["stale"] = stale_count(conn, libraries, stale)
    if summary["skipped_failed"]:
        ctx.log("Skipping %d image(s) that failed %d times; Retry failed on the Manage page "
                "(or scan.py --retry-failed) tries them again."
                % (summary["skipped_failed"], MAX_FAILURES))
    if summary["stale"]:
        ctx.log("%d image(s) have out-of-date descriptions (from an older prompt, or of a "
                "video and about its frames); Re-describe out of date on the Manage page (or "
                "scan.py --redescribe-stale) updates them." % summary["stale"])
    summary["pending"] = len(rows) if limit is None else min(len(rows), max(0, limit))
    if not rows:
        if ids is not None and not summary["skipped_offline"]:
            ctx.log("Nothing to describe: the chosen pictures already have descriptions, "
                    "are gone, or belong to a disabled library.")
        elif not summary["skipped_offline"]:
            ctx.log("All images already have descriptions.")
        return summary

    by_library = {library["id"]: library for library in libraries}
    chosen = None
    if endpoint_name:
        chosen = common.resolve_endpoints(config, endpoint_name)
        if not chosen:
            summary["error"] = "No enabled endpoint or group is named %r." % endpoint_name
            ctx.log(summary["error"] + " See scan.py --list-endpoints.", "error")
            return summary
    routes = {}
    waiting = {row["library_id"] for row in rows}
    for library in libraries:
        if library["id"] not in waiting:
            continue
        found = chosen if chosen is not None else common.endpoints_for_library(config, library)
        routes[library["id"]] = [endpoint for endpoint in found if endpoint["model"]]
    if not any(endpoint["model"] and endpoint["enabled"]
               for endpoint in common.endpoint_list(config)):
        ctx.log("No LLM model configured; skipping descriptions. Add an endpoint on the "
                "Settings page or with settings.py.")
        return summary

    def unrouted(library, count):
        wanted = library.get("endpoint") or common.default_endpoint_name(config)
        if wanted:
            ctx.log("Library %s: no enabled endpoint with a model is named %r; skipping its "
                    "%d image(s)." % (common.library_label(library), wanted, count), "warn")
        else:
            ctx.log("Library %s names no endpoint and no default endpoint is set; skipping "
                    "its %d image(s). Choose one with settings.py." % (
                        common.library_label(library), count), "warn")

    # (The limit counts pictures that can be sent: hidden ones that may not
    # go to the library's endpoints would otherwise fill it on every run.)
    assigned = _limited(_assign_routes(rows, routes, by_library, ctx, summary, unrouted),
                        limit, ctx, "Describing %d of %d image(s) (limit).")
    summary["pending"] = len(assigned)
    if not assigned:
        if limit == 0 and rows:
            ctx.log("Nothing to describe (limit 0).")
        return summary
    return _execute(conn, config, ctx, summary, assigned, by_library, helper, previews, sleep,
                    servers, redescribe_stale=redescribe_stale,
                    force_ids=ids if force else None)


def _limited(assigned, limit, ctx, message):
    """The first limit of the assigned pictures (all of them without a limit)."""
    if limit is not None and len(assigned) > max(0, limit):
        if limit > 0:
            ctx.log(message % (limit, len(assigned)))
        assigned = assigned[:max(0, limit)]
    return assigned


def _is_hidden(row):
    return "hidden" in row.keys() and bool(row["hidden"])


def _assign_routes(rows, routes, by_library, ctx, summary, unrouted):
    """[(row, endpoints)]: the endpoints each row may be sent to, in order.

    routes maps library ids to their endpoints. A row whose library has
    none is skipped (unrouted(library, count) says so, once per library).
    A hidden picture only goes to the endpoints on this computer or the
    local network; when its library has none, it is skipped and counted
    in summary["skipped_hidden"] (one line says where they were not sent).
    """
    assigned = []
    missing = collections.Counter()
    held = collections.Counter()
    for row in rows:
        route = routes.get(row["library_id"]) or []
        if not route:
            missing[row["library_id"]] += 1
            continue
        if _is_hidden(row):
            local = [endpoint for endpoint in route if common.is_local_endpoint(endpoint)]
            if not local:
                held[", ".join(sorted({common.url_host(endpoint["base_url"]) or endpoint["name"]
                                       for endpoint in route}))] += 1
                continue
            route = local
        assigned.append((row, route))
    for library_id, count in missing.items():
        unrouted(by_library[library_id], count)
        summary["skipped"] += count
    for hosts, count in sorted(held.items()):
        ctx.log("%d hidden picture(s) were not sent to %s: hidden pictures are only described "
                "by endpoints on this computer or the local network." % (count, hosts), "warn")
        summary["skipped"] += count
        summary["skipped_hidden"] += count
    return assigned


def _execute(conn, config, ctx, summary, assigned, by_library, helper, previews, sleep,
             servers, redescribe_stale=False, force_ids=None, tier="", token_limits=None,
             stage="describe", verb="Describing"):
    """Describe the assigned (row, endpoints) pairs; returns summary, filled in.

    tier ("refine" for the refine pass) is recorded in the provenance, which
    then also keeps the model and endpoint of the description it replaces;
    the refine pass always asks for the core profile. token_limits ({endpoint
    name: max_output_tokens}) overrides the endpoints' own setting.
    """
    run = _DescribeRun(conn, ctx, summary, helper, previews or cache.PreviewStore(), sleep)
    run.redescribe_stale = redescribe_stale
    run.force_ids = force_ids
    run.tier = tier
    run.video_frames = videos.frame_count(config)
    run.sheet_check = sheet_talk.enabled(config)
    run.stale_sql = prompts.stale_sql(config)

    def profile_of(library):
        return "core" if tier == "refine" else library_profile(library)

    # Exact copies only share a description within libraries that ask for
    # the same thing (metadata profile and context notes). This is the
    # library's own prompt: a picture's adds notes that follow from its
    # content (360-degree projection, video frames), the same for its copies.
    for library in config.get("libraries", []):
        if isinstance(library, dict) and library.get("id"):
            signature = (profile_of(library), prompts.user_text(library))
            run.share_ids.setdefault(signature, []).append(library["id"])
            run.share_key[library["id"]] = signature
    used = collections.OrderedDict()
    for _row, route in assigned:
        for endpoint in route:
            used.setdefault(endpoint["name"], endpoint)
    for name, endpoint in used.items():
        if token_limits and token_limits.get(name):
            endpoint = dict(endpoint, max_output_tokens=token_limits[name])
        run.add_endpoint(endpoint)
    roots = {library_id: common.library_root(library)
             for library_id, library in by_library.items()}
    for position, (row, route) in enumerate(assigned):
        library = by_library[row["library_id"]]
        run.add_task(Task(position, row, tuple(endpoint["name"] for endpoint in route),
                          roots.get(row["library_id"], ""), profile_of(library),
                          prompts.user_text(library, projection=_projection(row),
                                            video=_video(row, run.video_frames))))
    clean_tmp()
    ctx.stage(stage, total=len(assigned))
    ctx.log("%s %d image(s)..." % (verb, len(assigned)))
    import local_server     # stdlib only; imported here to keep this module's imports short
    lease = local_server.lease_for(ctx, config, list(used), manager=servers)
    try:
        for name, problem in lease.acquire().items():
            run.server_problem = run.server_problem or problem
            run.retire(run.by_name[name], problem)
        if helper is not None and hasattr(helper, "before_launch"):
            helper.before_launch()
        run.prepare_endpoints(started_by_helper=helper is not None
                              and hasattr(helper, "before_launch"))
        run.run()
    finally:
        try:
            if helper is not None and hasattr(helper, "after_launch"):
                helper.after_launch()
        finally:
            lease.release()
    run.finish()
    return summary


def refine_pending(conn, config, ctx, helper=None, library_ids=None, limit=None,
                   retry_failed=False, newest_first=None, previews=None, sleep=time.sleep,
                   servers=None, only_library=False):
    """The refine pass: describe documents and screenshots again with a stronger model.

    For each enabled library (limited to library_ids) that names a refine
    endpoint (refine.settings), its described pictures of the chosen kinds
    that are not refined yet with the current prompt (refine.candidate_rows)
    are described again with that endpoint, with the core profile, the
    usual prompt and the refine setting's max_output_tokens. The new
    description replaces the old one; its provenance has tier "refine" and
    the model and endpoint of the one before ("previous"). Pictures that
    failed MAX_FAILURES times are skipped unless retry_failed is set; at
    most limit pictures are done. only_library: the run was asked for one
    library, so that library having no refine endpoint is an error.
    Hidden pictures, offline folders and model servers are handled as in
    describe_pending, whose summary keys this returns too.
    """
    import refine
    summary = _new_summary()
    libraries = selected_libraries(config, library_ids)
    chosen = [library for library in libraries if refine.enabled(library)]
    if not chosen:
        if only_library and len(libraries) == 1:
            summary["error"] = ("Library %s has no refine endpoint: choose one in its settings "
                                "on the Manage page." % common.library_label(libraries[0]))
            ctx.log(summary["error"], "error")
        else:
            ctx.log("No library names a refine endpoint; nothing to refine.")
        return summary
    rows = []
    for library in chosen:
        found = refine.candidate_rows(conn, library)
        if not retry_failed:
            kept = [row for row in found if row["describe_attempts"] < MAX_FAILURES]
            summary["skipped_failed"] += len(found) - len(kept)
            found = kept
        order = library.get("newest_first", True) if newest_first is None else newest_first
        found.sort(key=capture_sort_key, reverse=bool(order))
        found.sort(key=lambda row: row["describe_attempts"] > 0)     # stable
        rows.extend(found)
    if summary["skipped_failed"]:
        ctx.log("Skipping %d picture(s) that failed %d times; Retry failed (or scan.py "
                "--refine --retry-failed) tries them again."
                % (summary["skipped_failed"], MAX_FAILURES))
    rows, summary["skipped_offline"] = _drop_unreachable(rows, chosen, ctx, helper)
    summary["pending"] = len(rows) if limit is None else min(len(rows), max(0, limit))
    if not rows:
        if not summary["skipped_offline"]:
            ctx.log("Nothing to refine: every document and screenshot is refined already.")
        return summary
    by_library = {library["id"]: library for library in chosen}
    routes, token_limits = {}, {}
    for library in chosen:
        wanted = refine.settings(library)
        routes[library["id"]] = [endpoint for endpoint in
                                 common.resolve_endpoints(config, wanted["endpoint"])
                                 if endpoint["model"]]
        for endpoint in routes[library["id"]]:
            # More room than the endpoint's own setting, never less (a
            # thinking model may have been given more).
            own = common.endpoint_settings(endpoint)["max_output_tokens"]
            token_limits[endpoint["name"]] = max(token_limits.get(endpoint["name"], 0),
                                                 wanted["max_output_tokens"], own)

    def unrouted(library, count):
        ctx.log("Library %s: no enabled endpoint with a model is named %r (its refine "
                "endpoint); skipping its %d picture(s)." % (
                    common.library_label(library), refine.settings(library)["endpoint"], count),
                "warn")

    assigned = _limited(_assign_routes(rows, routes, by_library, ctx, summary, unrouted),
                        limit, ctx, "Refining %d of %d picture(s) (limit).")
    summary["pending"] = len(assigned)
    if not assigned:
        if only_library and not summary["skipped_hidden"] and limit != 0:
            summary["error"] = "The refine endpoint of this library cannot be used."
        return summary
    return _execute(conn, config, ctx, summary, assigned, by_library, helper, previews, sleep,
                    servers, force_ids={row["id"] for row, _route in assigned}, tier="refine",
                    token_limits=token_limits, stage="refine",
                    verb="Describing again (refine pass):")


def _drop_unreachable(rows, libraries, ctx, helper=None, timeout=None):
    """(rows, count): rows without the images that would have to be read from a folder
    that cannot be reached right now.

    Images with a preview are kept (the preview is in the data folder), and
    so is everything when the helper reads the originals (load_image). One
    warning per library says how many images wait for its folder.
    """
    if helper is not None and getattr(helper, "load_image", None) is not None:
        return rows, 0
    needed = {row["library_id"] for row in rows if row["has_preview"] != 1}
    if not needed:
        return rows, 0
    import library_ops      # only needed here; it imports nothing that needs Pillow
    timeout = FOLDER_CHECK_TIMEOUT if timeout is None else timeout
    offline = {}
    for library in libraries:
        if library["id"] in needed:
            root = common.library_root(library)
            if not library_ops.folder_status(root, timeout=timeout)["reachable"]:
                offline[library["id"]] = (common.library_label(library), root)
    if not offline:
        return rows, 0
    kept = []
    waiting = collections.Counter()
    for row in rows:
        if row["library_id"] in offline and row["has_preview"] != 1:
            waiting[row["library_id"]] += 1
        else:
            kept.append(row)
    for library_id, count in waiting.items():
        label, root = offline[library_id]
        ctx.log("Library %s: %d image(s) without a preview are left for later: the folder "
                "%s cannot be reached (offline?)." % (label, count, root or "(none)"), "warn")
    return kept, sum(waiting.values())


class _DescribeRun:
    """The state of one describe_pending() run (used on the calling thread only)."""

    def __init__(self, conn, ctx, summary, helper, previews, sleep):
        self.conn = conn
        self.ctx = ctx
        self.summary = summary
        self.helper = helper
        self.helper_lock = threading.Lock()
        # Worker threads read originals at the same time: the helper's
        # load_image runs one at a time unless it says it is thread safe.
        self.read_helper = common.thread_safe_helper(helper)
        self.previews = previews
        self.sleep = sleep
        self.redescribe_stale = False
        self.force_ids = None                      # ids described again on request
        self.tier = ""                             # "refine" for the refine pass
        self.server_problem = ""                   # why a model server could not be used
        self.share_ids = {}                        # (profile, user text) -> library ids
        self.share_key = {}                        # library id -> its key in share_ids
        self.endpoints = []
        self.by_name = {}
        self.queues = collections.OrderedDict()    # route -> deque of Tasks
        self.last_position = 0
        self.requeued = collections.Counter()      # image id -> times put back
        self.done_hashes = set()                   # (content hash, profile, user text)
        self.removed_logged = set()                # removed libraries already logged
        # (Images skipped before this run started are not in its counts.)
        self.skipped_before = summary["skipped"]
        self.in_flight = {}                        # future -> (task, endpoint)
        self.stop_event = threading.Event()
        self.stopping = False
        self.handled = 0
        self.total = 0
        self.started = time.monotonic()
        self.last_progress = self.started
        self.hashes = {}
        self.video_frames = videos.DEFAULT_FRAMES   # frames on a contact sheet made now
        self.sheet_check = True                     # ask videos again (describe_checked)
        self.stale_sql = None                       # prompts.stale_sql(config)

    # -- setting up -------------------------------------------------------------

    def add_endpoint(self, settings):
        state = _Endpoint(settings)
        self.endpoints.append(state)
        self.by_name[state.name] = state
        for secret in state.secrets:
            self.ctx.add_secret(secret)
        self.ctx.add_secret(settings.get("api_key"))

    def add_task(self, task):
        self.queues.setdefault(task.route, collections.deque()).append(task)
        self.last_position = max(self.last_position, task.position)
        self.total += 1

    def should_stop(self):
        return self.stop_event.is_set() or self.ctx.should_stop()

    def prepare_endpoints(self, started_by_helper=False):
        """Ask each endpoint's server about itself (model, slots), waiting for it to start.

        A local server that is still loading its model (or that the helper's
        before_launch has just started) is waited for, up to STARTUP_WAIT
        seconds; one that does not answer at all is waited for
        UNREACHABLE_WAIT seconds and then not used. Cloud services are not
        contacted.
        """
        for state in self.endpoints:
            settings = state.settings
            if state.retired:
                continue        # (its model server could not be started)
            if settings["preset"] in common.CLOUD_PRESETS and not state.secrets:
                # Never send pictures to a cloud service without a key.
                self.retire(state, "no API key is set (%s), so no picture was sent. Set the "
                            "key on the Settings page or with settings.py."
                            % common.resolve_api_key(settings)[1])
                continue
            info = llm.probe(settings)
            if info["loading"] or info["reachable"] is False:
                wait = (STARTUP_WAIT if info["loading"] or started_by_helper
                        else UNREACHABLE_WAIT)
                if wait > 0:
                    if info["loading"] or wait >= 10:
                        self.ctx.log("Waiting for endpoint %s to be ready..." % state.label())
                    llm.wait_until_ready(settings, wait, should_stop=self.should_stop,
                                         sleep=self.sleep)
                    info = llm.probe(settings)
            state.served_model = info["model"]
            state.concurrency = common.endpoint_concurrency(settings, info["slots"])
            if info["reachable"] is False:
                self.retire(state, "%s cannot be reached at %s. Is the server running?"
                            % (_who(settings),
                               common.redact(settings["base_url"], state.secrets)))
                continue
            if info["vision"] is False:
                self.ctx.log("Endpoint %s reports no image support (llama.cpp needs its "
                             "--mmproj file)." % state.label(), "warn")
            if common.insecure_endpoint(settings):
                self.ctx.log("Endpoint %s sends pictures%s unencrypted across the network "
                             "(%s). Use https:// if the service has it."
                             % (state.label(), " and its API key" if state.secrets else "",
                                common.redact(settings["base_url"], state.secrets)), "warn")
            where = ("" if common.is_local_endpoint(settings) else
                     "; pictures are sent to %s" % common.url_host(settings["base_url"]))
            self.ctx.log("Using endpoint %s: model %s, %d request(s) at a time%s."
                         % (state.label(), state.model_name(), state.concurrency, where))
        self.drop_unserved()

    # -- the queue --------------------------------------------------------------

    def take(self, state):
        """The next task this endpoint may work on (lowest position first), or None."""
        best = None
        for route, queue in self.queues.items():
            if state.name in route and queue and (best is None
                                                  or queue[0].position < best[0].position):
                best = queue
        return best.popleft() if best is not None else None

    def put_back(self, task, later=False):
        """Return a task to its queue: first in line, or (later) last, so other images go first."""
        if later:
            self.last_position += 1
            self.queues[task.route].append(task._replace(position=self.last_position))
        else:
            self.queues[task.route].appendleft(task)

    def more_work(self, state):
        """True while this endpoint has other images in flight or waiting."""
        return state.in_flight > 0 or any(
            queue for route, queue in self.queues.items() if state.name in route)

    def clear_queues(self):
        for queue in self.queues.values():
            queue.clear()

    def step(self, task, count=1):
        self.handled += count
        self.ctx.step(count, item=task.row["path"] if task is not None else None)

    def drop_unserved(self):
        """Skip the queued images whose endpoints have all been retired."""
        for route, queue in self.queues.items():
            if queue and all(self.by_name[name].retired for name in route):
                count = len(queue)
                self.summary["skipped"] += count
                self.ctx.log("Skipped %d image(s): no usable endpoint is left for them "
                             "(%s)." % (count, ", ".join(route)), "warn")
                last = queue[-1]
                queue.clear()
                self.step(last, count)
        if not self.stopping and all(state.retired for state in self.endpoints):
            if not self.summary["error"] and self.server_problem:
                self.summary["error"] = self.server_problem
            elif not self.summary["error"]:
                self.summary["error"] = "No usable LLM endpoint is left."
                self.ctx.log(self.summary["error"] + " Check that the model server is "
                             "running and the endpoint settings are right.", "error")
            self.stopping = True
            self.stop_event.set()

    def retire(self, state, message):
        """Stop using an endpoint for the rest of the run. True if it was in use."""
        if state.retired:
            return False
        state.retired = True
        self.summary["retired"].append(state.name)
        self.ctx.log("Stopped using endpoint %s for this run: %s" % (state.label(), message),
                     "error")
        return True

    def stop_all(self):
        self.stopping = True
        self.stop_event.set()
        self.clear_queues()

    # -- running ----------------------------------------------------------------

    def skip_removed(self, task):
        """Skip an image of a library removed from the settings while the job runs."""
        library_id = task.row["library_id"]
        if library_id not in self.removed_logged:
            self.removed_logged.add(library_id)
            self.ctx.log("Library %s was removed from the settings: its other pictures are "
                         "not described." % library_id, "warn")
        self.summary["skipped"] += 1
        self.step(task)

    def fill(self, pool):
        """Give every endpoint with a free slot its next image."""
        while not self.stopping:
            submitted = False
            for state in self.endpoints:
                if state.retired or state.in_flight >= state.concurrency:
                    continue
                task = self.take(state)
                if task is None:
                    continue
                submitted = True
                if jobs.library_removed(self.ctx, task.row["library_id"]):
                    self.skip_removed(task)
                    continue
                content_hash = task.row["content_hash"]
                if content_hash and (content_hash, task.profile,
                                     task.user_text) in self.done_hashes:
                    # An exact copy was just described; it got the description too.
                    self.step(task)
                    continue
                future = pool.submit(self.work, state, task)
                self.in_flight[future] = (task, state)
                state.in_flight += 1
            if not submitted:
                return

    def work(self, state, task):
        """Worker thread: prepare and describe one image. Never raises."""
        row = task.row
        source_path = os.path.join(task.root, *row["path"].split("/"))
        user_text = task.user_text
        try:
            if _is_video(row):
                # (The note says how many frames the sheet really holds.)
                jpeg, count = video_for_llm(row, task.root, self.previews,
                                            max_edge=state.settings["max_image_edge"],
                                            frames=self.video_frames)
                user_text = video_text(user_text, row, self.video_frames, count)
            else:
                jpeg = image_for_llm(row, task.root, self.previews, self.read_helper,
                                     max_edge=state.settings["max_image_edge"],
                                     frames=self.video_frames)
        except images.SourceReadError as error:
            return {"status": "skipped", "error": _error_text(error, state.secrets)}
        except Exception as error:
            text = _error_text(error, state.secrets)
            if (images.HEIF_MISSING in text or images.AVIF_MISSING in text
                    or isinstance(error, videos.ToolsMissing)):
                # Not the image's fault: it works once the decoder is installed.
                return {"status": "skipped", "error": text}
            return {"status": "error", "kind": "local", "error": text, "exception": None}
        options = dict(profile=task.profile, user_text=user_text, helper=self.helper,
                       helper_lock=self.helper_lock, image_id=row["id"],
                       source_path=source_path, should_stop=self.should_stop,
                       sleep=self.sleep, read_helper=self.read_helper)
        if _is_video(row) and self.sheet_check:
            return describe_checked(state.settings, jpeg, "video", **options)
        return describe_jpeg(state.settings, jpeg, **options)

    def run(self):
        workers = sum(state.concurrency for state in self.endpoints if not state.retired)
        if not workers or self.stopping:
            return
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            if self.ctx.should_stop():
                self.summary["stopped"] = True
                self.stop_all()
            self.fill(pool)
            while self.in_flight:
                done, _pending = concurrent.futures.wait(
                    self.in_flight, timeout=PROGRESS_SECONDS,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    task, state = self.in_flight.pop(future)
                    state.in_flight -= 1
                    self.handle(task, state, future.result())
                self.drop_unserved()
                self.progress()
                if not self.stopping and self.ctx.should_stop():
                    self.summary["stopped"] = True
                    self.stop_all()
                if not self.stopping:
                    self.ctx.wait_if_paused()
                    self.fill(pool)
        finally:
            # Workers still retrying give up at their next check.
            self.stop_event.set()
            pool.shutdown(wait=True)

    def progress(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress < PROGRESS_SECONDS:
            return
        self.last_progress = now
        elapsed = max(now - self.started, 1e-6)
        # Skipped images took no work: they do not count towards the rate.
        skipped = self.summary["skipped"] - self.skipped_before
        worked = self.handled - skipped
        rate = worked / elapsed * 60.0
        text = "Progress: %d/%d image(s)" % (self.handled, self.total)
        if skipped:
            text += ", %d skipped" % skipped
        if worked > 0:
            text += ", %.1f images/min" % rate
            remaining = self.total - self.handled
            if remaining > 0 and rate > 0:
                minutes = remaining / rate
                text += ", %s%s left" % ("" if minutes < 1 else "about ", _duration(minutes))
        text += ", %d failed" % self.summary["failed"]
        self.ctx.log(text + ".")

    # -- results ----------------------------------------------------------------

    def count_usage(self, result):
        summary = self.summary
        summary["requests"] += result.get("requests", 0)
        for key in ("tokens_in", "tokens_out"):
            if result.get(key) is not None:
                summary[key] += result[key]
        if result.get("cost") is not None:
            summary["cost"] = (summary["cost"] or 0.0) + result["cost"]

    def handle(self, task, state, result):
        self.count_usage(result)
        reask_count(self.summary, result.get("reasked"))
        status = result["status"]
        row = task.row
        if status == "ok":
            state.succeeded = True
            state.failures = 0
            state.suspects = []
            self.store(task, state, result)
            self.step(task)
            return
        if status == "skipped":
            self.summary["skipped"] += 1
            self.ctx.log("Skipped %s: %s" % (row["path"], result["error"]), "warn")
            self.step(task)
            return
        kind = result.get("kind")
        error = result.get("exception")
        settings = state.settings
        if kind in ("retryable", "content") and self.should_stop():
            # Interrupted by a stop: neither the image's nor the endpoint's fault.
            return
        # The service answered this very request with an error, after it had
        # described other images in this run: this image may be the cause.
        answered = state.succeeded and error is not None and bool(error.status)
        if kind == "fatal":
            if not self.summary["fatal"]:
                message = llm.explain_error(error, settings)
                self.summary.update(fatal=True, error=message)
                self.ctx.log("%s Stopping (while describing %s)." % (message, row["path"]),
                             "error")
                self.retire(state, "the run was stopped")
                if answered:
                    # Charged once, so that a picture the service refuses
                    # cannot stop every run from now on.
                    self.charge(task, state, result["error"])
                self.stop_all()
            return
        if kind == "endpoint":
            if self.retire(state, llm.explain_error(error, settings)) and answered:
                self.charge(task, state, result["error"])
            self.put_back(task)
            return
        if kind == "retryable":
            self.handle_retryable(task, state, result, error)
            return
        if not state.succeeded and (kind == "content" or (
                kind == "image" and error is not None and error.status)):
            # This endpoint has not described a single image in this run:
            # a refusal or an unusable reply is most likely its settings or
            # model, not this image. The image is not charged, and after a
            # few such failures the endpoint is not used (which also caps
            # what a misconfigured cloud endpoint can cost). While other
            # images are waiting, it is asked again once after them: if the
            # endpoint has worked by then, a second failure is the image's.
            state.failures += 1
            what = ("refused every request" if kind == "image"
                    else "gave only unusable replies")
            if state.failures >= MAX_ENDPOINT_FAILURES:
                self.summary["skipped"] += 1
                self.retire(state, "it %s so far (%s). Check the endpoint settings and "
                            "model." % (what, result["error"]))
                self.step(task)
            elif not self.requeued[row["id"]] and self.more_work(state):
                self.requeued[row["id"]] += 1
                self.put_back(task, later=True)
                self.ctx.log("The endpoint %s failed on %s (%s); trying it again later."
                             % (state.label(), row["path"], result["error"]), "warn")
            else:
                self.summary["skipped"] += 1
                self.ctx.log("The endpoint %s failed on %s: %s"
                             % (state.label(), row["path"], result["error"]), "warn")
                self.step(task)
            return
        if kind in ("image", "content"):
            # The endpoint answers: this failure is the image's.
            state.failures = 0
            state.suspects = []
        self.fail(task, state, result["error"])

    def handle_retryable(self, task, state, result, error):
        """A request that kept failing in a way worth retrying (see handle)."""
        row = task.row
        settings = state.settings
        if (error is not None and error.retry_after is not None
                and error.retry_after > llm.MAX_RETRY_AFTER):
            self.retire(state, "%s (it asks to wait %d minutes)" % (
                llm.explain_error(error, settings), round(error.retry_after / 60.0)))
            self.put_back(task)
            return
        # Running out of time and server errors, on an endpoint that works,
        # may be this image's fault (a picture that takes too long, or that
        # the server chokes on); dropped connections and rate limits are not.
        suspect = state.succeeded and error is not None and (
            error.timeout or (error.status or 0) >= 500)
        if suspect and self.requeued[row["id"]] >= 1:
            # It failed this way twice: the image's problem, so its first
            # failure no longer counts against the endpoint.
            state.failures = max(0, state.failures - 1)
            state.suspects = [item for item in state.suspects
                              if item[0].row["id"] != row["id"]]
            self.fail(task, state, result["error"])
            return
        state.failures += 1
        if suspect:
            state.suspects.append((task, result["error"]))
        if state.failures >= MAX_ENDPOINT_FAILURES:
            if self.retire(state, llm.explain_error(error, settings)):
                # Images that ran out of time (or broke the server) on an
                # endpoint that worked are charged once, so the same few
                # pictures cannot end every run.
                for item, message in state.suspects:
                    self.charge(item, state, message)
                state.suspects = []
            self.put_back(task)
            return
        # Asked again after the other waiting images, not at once.
        self.requeued[row["id"]] += 1
        self.put_back(task, later=True)
        self.ctx.log("The endpoint %s failed on %s (%s); trying it again later."
                     % (state.label(), row["path"], result["error"]), "warn")

    def charge(self, task, state, message):
        """Count a failed attempt for an image without settling it in this run."""
        error = common.redact(message, state.secrets)
        record_failure(self.conn, task.row, error)
        self.ctx.log("Counted a failed attempt for %s: %s" % (task.row["path"], error), "warn")

    def fail(self, task, state, message):
        error = common.redact(message, state.secrets)
        record_failure(self.conn, task.row, error)
        self.summary["failed"] += 1
        self.ctx.log("Failed to describe %s: %s" % (task.row["path"], error), "warn")
        self.step(task)

    def prompt_hash(self, profile):
        if profile not in self.hashes:
            self.hashes[profile] = prompts.prompt_hash(profile)
        return self.hashes[profile]

    def store(self, task, state, result):
        row = task.row
        model = state.model_name(result.get("model") or "")
        info = dict(result["info"])
        partial = result.get("truncated") or result.get("repaired")
        if partial:
            # The fields the reply did not reach: the refine pass keeps the
            # old description's (and never weakens its privacy flags);
            # otherwise an unknown private_info counts as private.
            previous = None
            if self.tier and "info" in row.keys():
                previous = _json_dict(row["info"]) or None
            info = fill_missing(info, result.get("missing"), previous)
        info["_model_name"] = model
        # Tokens, cost, time and requests of every request this image took
        # (content retries included), as the run's summary counts them.
        provenance = {"model": model, "endpoint": state.name,
                      "seconds": round(result["seconds"], 2),
                      "tokens_in": result.get("tokens_in"),
                      "tokens_out": result.get("tokens_out"),
                      "prompt_ver": PROMPT_VER, "prompt_hash": self.prompt_hash(task.profile),
                      "profile": task.profile, "requests": result["requests"],
                      "at": _utc_now()}
        if result.get("cost") is not None:
            provenance["cost"] = round(result["cost"], 8)
        if result.get("truncated"):
            provenance["truncated"] = True
        elif result.get("repaired"):
            provenance["repaired"] = True
        if partial and result.get("missing"):
            provenance["missing_fields"] = list(result["missing"])
        reask_provenance(provenance, result.get("reasked"))
        if self.tier:
            provenance["tier"] = self.tier
            before = _json_dict(row["provenance"] if "provenance" in row.keys() else "")
            previous = {key: before[key] for key in ("model", "endpoint", "at", "tier")
                        if isinstance(before.get(key), str) and before[key]}
            if previous:
                provenance["previous"] = previous
        signature = (task.profile, task.user_text)
        stored, shared = store_description(self.conn, row, info, provenance,
                                           library_ids=self.share_ids.get(
                                               self.share_key.get(row["library_id"]), []),
                                           include_stale=self.redescribe_stale,
                                           also_ids=self.force_ids, stale_sql=self.stale_sql)
        if stored:
            self.summary["described"] += 1
            self.summary["shared"] += shared
            if row["content_hash"]:
                self.done_hashes.add((row["content_hash"],) + signature)
            note = ""
            if result.get("truncated"):
                note = ", cut off at the length limit"
            elif result.get("repaired"):
                note = ", from a reply whose JSON broke off"
            if result.get("reasked"):
                note += "; " + reask_note(result["reasked"])
            self.ctx.log("%s %s (%.1f s)%s" % ("Refined" if self.tier else "Described",
                                                 row["path"], result["seconds"], note))
        else:
            self.summary["skipped"] += 1
            self.ctx.log("Not saved: %s changed while it was being described." % row["path"],
                         "warn")

    def finish(self):
        summary = self.summary
        if self.total and self.handled:
            self.progress(force=True)
        parts = ["%s %d image(s)" % ("Refined" if self.tier else "Described",
                                     summary["described"])]
        if summary["shared"]:
            parts.append("%d exact copies shared a description" % summary["shared"])
        if summary["failed"]:
            parts.append("%d failed" % summary["failed"])
        if summary["skipped"]:
            parts.append("%d skipped" % summary["skipped"])
        text = ", ".join(parts) + "."
        if summary["tokens_in"] or summary["tokens_out"]:
            text += " Tokens: %d in, %d out" % (summary["tokens_in"], summary["tokens_out"])
            if summary["cost"] is not None:
                text += " (about $%.4f)" % summary["cost"]
            text += "."
        self.ctx.log(text)
        line = reask_summary("video", summary)
        if line:
            self.ctx.log(line)
        if summary["stopped"]:
            self.ctx.log("Stopped. Descriptions so far are saved; run again to continue.")
