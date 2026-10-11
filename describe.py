#!/usr/bin/env python3
"""Ask LLMs to describe indexed images and store the replies.

For every image that has no description yet (and, when asked, every one
described with an older prompt), the cached preview -- or the original when
there is no preview -- is shrunk with images.prepare_for_llm() and sent as a
JPEG with the prompt from prompts.py to the library's endpoint(s). The JSON
reply is checked and tidied by parse_reply() and stored with the image,
together with its provenance (model, endpoint, time, tokens, prompt hash).

Each library uses its own endpoint (or group of endpoints, which share the
work); see common.endpoints_for_library. Every endpoint gets as many worker
threads as its concurrency setting (for llama.cpp, the server's slot
count), so several requests are in flight at once. Only the calling thread
writes to the database.

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
import llm
import prompts
from prompts import PROMPT_VER, SYSTEM_PROMPT  # noqa: F401  (re-exported)

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

TMP_PREFIX = "describe-"


class ContentError(ValueError):
    """A reply arrived but is not a usable description."""


# ---------------------------------------------------------------------------
# Replies

_WORD_RE = re.compile(r"\w+")
_TOKEN_RE = re.compile(r"\S+")
_SPACES_RE = re.compile(r"\s+")
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
           prompt_ver, describe_attempts, has_preview, projection
    FROM images
    WHERE library_id = ? AND missing_since IS NULL AND (described = 0 OR %s)
    ORDER BY id"""


def pending_rows(conn, libraries, newest_first=None, retry_failed=False,
                 redescribe_stale=False):
    """The images to describe, in order. Returns (rows, skipped_failed).

    Images that have never been described come first; then, only when
    redescribe_stale is set, those described with an older prompt
    (prompt_ver < PROMPT_VER). Within each group, libraries keep their
    config order, and each library is sorted by capture date: newest first
    when newest_first is True, oldest first when False, and by the
    library's own "newest_first" setting when None -- except that images
    which failed in an earlier run come after the others, so a picture an
    endpoint cannot handle never holds up the rest. Missing files are left
    out, and so are images that failed MAX_FAILURES times unless
    retry_failed is set; those are counted in skipped_failed. Hidden images
    are described too.
    """
    fresh, stale = [], []
    skipped_failed = 0
    sql = _PENDING_SQL % ("prompt_ver < ?" if redescribe_stale else "0")
    for library in libraries:
        args = (library["id"], PROMPT_VER) if redescribe_stale else (library["id"],)
        rows = conn.execute(sql, args).fetchall()
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


def stale_count(conn, libraries):
    """How many present images of these libraries were described with an older prompt."""
    ids = [library["id"] for library in libraries]
    if not ids:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM images WHERE missing_since IS NULL AND described = 1 "
        "AND prompt_ver < ? AND library_id IN (%s)" % ",".join("?" * len(ids)),
        [PROMPT_VER] + ids).fetchone()[0]


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


def image_for_llm(row, root, previews, helper=None, max_edge=common.DEFAULT_MAX_IMAGE_EDGE):
    """The JPEG bytes to send for an image row.

    Uses the cached preview when there is one, else the original (read
    through the helper's load_image when it has one). Either way the image is
    upright, flattened and shrunk by images.prepare_for_llm() to a long edge
    of max_edge (the endpoint's max_image_edge). Raises
    images.SourceReadError when the original cannot be read and
    images.ImageReadError when it cannot be decoded.
    """
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
                  sleep=time.sleep, max_retries=None, read_helper=None):
    """Describe prepared image bytes with one endpoint. Never raises.

    Sends the profile's system prompt and schema with user_text (default:
    prompts.user_text(None)), retrying as described in the module docstring
    (rate limits and server errors through llm.describe_with_retries, then
    up to CONTENT_RETRIES more requests for unusable replies, with
    retry_extra() for local servers). helper's call_api hook, if any, sees
    every request (image_id and source_path are passed on to it); an image
    it swaps in is read through read_helper (default: helper).

    Returns {"status": "ok" or "error", "kind" (a ProviderError kind, or
    "local" for a failure on this side; None when ok), "error" (redacted
    text), "exception" (the ProviderError, if any), "info" (parse_reply's
    dict when ok), "model" (as the reply names it), "usage" (of the last
    reply), "finish", "seconds" (time spent in requests), "requests" (HTTP
    requests that carried the image, retries included), "tokens_in",
    "tokens_out" and "cost" (totals over every reply; None when unknown),
    "content_errors"}.
    """
    settings = common.endpoint_settings(endpoint)
    key, _source = common.resolve_api_key(settings)
    secrets = [key] if key else []
    profile = prompts.check_profile(profile)
    system = prompts.system_prompt(profile)
    schema = prompts.schema(profile)
    if user_text is None:
        user_text = prompts.user_text(None)
    helper_lock = helper_lock or threading.Lock()
    result = {"status": "error", "kind": None, "error": "", "exception": None, "info": None,
              "model": "", "usage": {"input_tokens": None, "output_tokens": None},
              "finish": "", "seconds": 0.0, "requests": 0, "tokens_in": None,
              "tokens_out": None, "cost": None, "content_errors": 0}
    retries = MAX_RETRIES if max_retries is None else max_retries

    def count_retry(_error, _delay):
        result["requests"] += 1

    def add(key_name, value):
        if value is not None:
            result[key_name] = (result[key_name] or 0) + value

    last_error = None
    for attempt in range(1 + CONTENT_RETRIES):
        if attempt and should_stop is not None and should_stop():
            break
        text, data, mime = user_text, jpeg, "image/jpeg"
        started = time.monotonic()
        try:
            if helper is not None and hasattr(helper, "call_api"):
                text, data, mime = _helper_request(helper, helper_lock, text, jpeg, image_id,
                                                   source_path, read_helper)
            reply = llm.describe_with_retries(
                settings, system, text, data, schema, should_stop=should_stop,
                max_retries=retries, sleep=sleep, mime=mime,
                request_extra=retry_extra(settings) if attempt else None,
                on_retry=count_retry, max_timeout_retries=TIMEOUT_RETRIES)
        except llm.ProviderError as error:
            result["requests"] += 1
            result["seconds"] += time.monotonic() - started
            result.update(kind=error.kind, error=_error_text(error, secrets), exception=error)
            return result
        except Exception as error:
            result.update(kind="local", error=_error_text(error, secrets))
            return result
        result["requests"] += 1
        result["seconds"] += reply["seconds"]
        usage = reply["usage"]
        add("tokens_in", usage.get("input_tokens"))
        add("tokens_out", usage.get("output_tokens"))
        add("cost", llm.estimate_cost(settings, usage))
        result.update(model=reply["model"], usage=usage, finish=reply["finish"])
        if reply["finish"] != "ok":
            message = _FINISH_TEXT.get(reply["finish"], reply["finish"])
            if reply.get("detail"):
                message += ": " + reply["detail"]
            last_error = ContentError(message)
        else:
            try:
                info = parse_reply(reply["text"], profile)
            except ContentError as error:
                last_error = error
            else:
                result.update(status="ok", info=info)
                return result
        result["content_errors"] += 1
        llm.log_error(reply["text"], last_error, secrets)
    result.update(kind="content", error=_error_text(last_error or "stopped", secrets))
    return result


# ---------------------------------------------------------------------------
# Storing results

_STORE_SQL = """
    UPDATE images SET info = ?, described = 1, prompt_ver = ?, provenance = ?,
        describe_attempts = 0, describe_error = '', updated = ?
    WHERE id = ? AND content_hash = ?"""

# Exact copies (same content hash) that still need a description get the
# same one, so each picture is only sent once.
_SHARE_SQL = """
    UPDATE images SET info = ?, described = 1, prompt_ver = ?, provenance = ?,
        describe_attempts = 0, describe_error = '', updated = ?
    WHERE content_hash = ? AND id <> ? AND missing_since IS NULL"""


def store_description(conn, row, info, provenance, library_ids=None, include_stale=False):
    """Save a description for a row and its exact copies. Returns (stored, shared).

    Copies only get it when they have no description yet (or, with
    include_stale, one from an older prompt), and only in library_ids when
    given: libraries that would ask for the same thing (metadata profile
    and context notes). stored is False when the row changed or vanished
    while it was being described (its content hash no longer matches);
    nothing is saved then.
    """
    now = time.time()
    info_text = json.dumps(info, ensure_ascii=False, allow_nan=False)
    provenance_text = json.dumps(provenance, ensure_ascii=False, allow_nan=False)
    stored = conn.execute(_STORE_SQL, (info_text, PROMPT_VER, provenance_text, now,
                                       row["id"], row["content_hash"])).rowcount == 1
    shared = 0
    if stored and row["content_hash"] and (library_ids is None or library_ids):
        sql = _SHARE_SQL
        args = [info_text, PROMPT_VER, provenance_text, now, row["content_hash"], row["id"]]
        if library_ids is not None:
            library_ids = list(library_ids)
            sql += " AND library_id IN (%s)" % ",".join("?" * len(library_ids))
            args += library_ids
        if include_stale:
            sql += " AND (described = 0 OR prompt_ver < ?)"
            args.append(PROMPT_VER)
        else:
            sql += " AND described = 0"
        shared = conn.execute(sql, args).rowcount
    conn.commit()
    return stored, max(0, shared)


def record_failure(conn, row, error):
    """Count a failed describe attempt for a row and keep its last error.

    A description the row already has (from an older prompt) is kept.
    """
    conn.execute("UPDATE images SET describe_attempts = describe_attempts + 1, "
                 "describe_error = ? WHERE id = ?", (error[:500], row["id"]))
    conn.commit()


def _who(settings):
    """'The OpenAI endpoint 'cloud'', for messages."""
    return "The %s endpoint '%s'" % (common.ENDPOINT_PRESETS[settings["preset"]]["label"],
                                     settings["name"] or settings["model"])


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _duration(minutes):
    if minutes < 1:
        return "under a minute"
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


def describe_pending(conn, config, ctx, helper=None, library_ids=None, newest_first=None,
                     retry_failed=False, redescribe_stale=False, limit=None,
                     endpoint_name=None, previews=None, sleep=time.sleep):
    """Describe the images that need it. Returns a summary dict.

    conn is an index.db connection; ctx a jobs.JobContext. Only enabled
    libraries are described (limited to library_ids when given): images
    without a description, plus those described with an older prompt when
    redescribe_stale is set (see pending_rows), at most limit of them. Each
    library uses its own endpoint(s) unless endpoint_name names an endpoint
    or group for all of them. Stops early, keeping every saved description,
    when ctx asks to stop, on a fatal error, or when no usable endpoint is
    left. The helper's before_launch and after_launch hooks bracket the run;
    after_launch runs even when the run fails. sleep is used for retry
    pauses (tests pass a stub).

    Summary keys: described, shared (copies that got the same description),
    failed, skipped, skipped_failed, pending, stale (described with an older
    prompt and not included), stopped, error, fatal, retired (endpoint
    names), requests, tokens_in, tokens_out, cost (None when unknown).
    """
    summary = {"described": 0, "shared": 0, "failed": 0, "skipped": 0,
               "skipped_failed": 0, "pending": 0, "stale": 0, "stopped": False, "error": "",
               "fatal": False, "retired": [], "requests": 0, "tokens_in": 0,
               "tokens_out": 0, "cost": None}
    libraries = selected_libraries(config, library_ids)
    rows, summary["skipped_failed"] = pending_rows(conn, libraries, newest_first,
                                                   retry_failed, redescribe_stale)
    if not redescribe_stale:
        summary["stale"] = stale_count(conn, libraries)
    if summary["skipped_failed"]:
        ctx.log("Skipping %d image(s) that failed %d times; retry them with "
                "--retry-failed." % (summary["skipped_failed"], MAX_FAILURES))
    if summary["stale"]:
        ctx.log("%d image(s) have descriptions from an older prompt; describe them again "
                "with --redescribe-stale." % summary["stale"])
    if limit is not None and len(rows) > max(0, limit):
        ctx.log("Describing %d of %d image(s) (limit)." % (max(0, limit), len(rows)))
        rows = rows[:max(0, limit)]
    summary["pending"] = len(rows)
    if not rows:
        ctx.log("Nothing to describe (limit 0)." if limit == 0
                else "All images already have descriptions.")
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
    used = collections.OrderedDict()
    waiting = {row["library_id"] for row in rows}
    for library in libraries:
        if library["id"] not in waiting:
            continue
        found = chosen if chosen is not None else common.endpoints_for_library(config, library)
        routes[library["id"]] = [endpoint for endpoint in found if endpoint["model"]]
        for endpoint in routes[library["id"]]:
            used.setdefault(endpoint["name"], endpoint)
    if not any(endpoint["model"] and endpoint["enabled"]
               for endpoint in common.endpoint_list(config)):
        ctx.log("No LLM model configured; skipping descriptions. Run settings.py.")
        return summary
    kept = []
    unrouted = collections.Counter()
    for row in rows:
        if routes.get(row["library_id"]):
            kept.append(row)
        else:
            unrouted[row["library_id"]] += 1
    for library_id, count in unrouted.items():
        library = by_library[library_id]
        wanted = library.get("endpoint") or common.default_endpoint_name(config)
        if wanted:
            ctx.log("Library %s: no enabled endpoint with a model is named %r; skipping its "
                    "%d image(s)." % (common.library_label(library), wanted, count), "warn")
        else:
            ctx.log("Library %s names no endpoint and no default endpoint is set; skipping "
                    "its %d image(s). Choose one with settings.py." % (
                        common.library_label(library), count), "warn")
        summary["skipped"] += count
    rows = kept
    summary["pending"] = len(rows)
    if not rows:
        return summary

    run = _DescribeRun(conn, ctx, summary, helper, previews or cache.PreviewStore(), sleep)
    run.redescribe_stale = redescribe_stale
    # Exact copies only share a description within libraries that ask for
    # the same thing (metadata profile and context notes).
    for library in config.get("libraries", []):
        if isinstance(library, dict) and library.get("id"):
            signature = (library_profile(library), prompts.user_text(library))
            run.share_ids.setdefault(signature, []).append(library["id"])
    for endpoint in used.values():
        run.add_endpoint(endpoint)
    roots = {library["id"]: common.library_root(library) for library in libraries}
    for position, row in enumerate(rows):
        library = by_library[row["library_id"]]
        route = tuple(endpoint["name"] for endpoint in routes[row["library_id"]])
        run.add_task(Task(position, row, route, roots.get(row["library_id"], ""),
                          library_profile(library),
                          prompts.user_text(library, projection=_projection(row))))
    clean_tmp()
    ctx.stage("describe", total=len(rows))
    ctx.log("Describing %d image(s)..." % len(rows))
    try:
        if helper is not None and hasattr(helper, "before_launch"):
            helper.before_launch()
        run.prepare_endpoints(started_by_helper=helper is not None
                              and hasattr(helper, "before_launch"))
        run.run()
    finally:
        if helper is not None and hasattr(helper, "after_launch"):
            helper.after_launch()
    run.finish()
    return summary


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
        self.share_ids = {}                        # (profile, user text) -> library ids
        self.endpoints = []
        self.by_name = {}
        self.queues = collections.OrderedDict()    # route -> deque of Tasks
        self.last_position = 0
        self.requeued = collections.Counter()      # image id -> times put back
        self.done_hashes = set()                   # (content hash, profile, user text)
        self.in_flight = {}                        # future -> (task, endpoint)
        self.stop_event = threading.Event()
        self.stopping = False
        self.handled = 0
        self.total = 0
        self.started = time.monotonic()
        self.last_progress = self.started
        self.hashes = {}

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
            if not self.summary["error"]:
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
        try:
            jpeg = image_for_llm(row, task.root, self.previews, self.read_helper,
                                 max_edge=state.settings["max_image_edge"])
        except images.SourceReadError as error:
            return {"status": "skipped", "error": _error_text(error, state.secrets)}
        except Exception as error:
            text = _error_text(error, state.secrets)
            if images.HEIF_MISSING in text or images.AVIF_MISSING in text:
                # Not the image's fault: it works once the decoder is installed.
                return {"status": "skipped", "error": text}
            return {"status": "error", "kind": "local", "error": text, "exception": None}
        return describe_jpeg(state.settings, jpeg, profile=task.profile,
                             user_text=task.user_text, helper=self.helper,
                             helper_lock=self.helper_lock, image_id=row["id"],
                             source_path=source_path, should_stop=self.should_stop,
                             sleep=self.sleep, read_helper=self.read_helper)

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
        rate = self.handled / elapsed * 60.0
        text = "Progress: %d/%d image(s)" % (self.handled, self.total)
        if self.handled:
            text += ", %.1f images/min" % rate
            remaining = self.total - self.handled
            if remaining > 0 and rate > 0:
                text += ", about %s left" % _duration(remaining / rate)
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
        signature = (task.profile, task.user_text)
        stored, shared = store_description(self.conn, row, info, provenance,
                                           library_ids=self.share_ids.get(signature, []),
                                           include_stale=self.redescribe_stale)
        if stored:
            self.summary["described"] += 1
            self.summary["shared"] += shared
            if row["content_hash"]:
                self.done_hashes.add((row["content_hash"],) + signature)
            self.ctx.log("Described %s (%.1f s)" % (row["path"], result["seconds"]))
        else:
            self.summary["skipped"] += 1
            self.ctx.log("Not saved: %s changed while it was being described." % row["path"],
                         "warn")

    def finish(self):
        summary = self.summary
        if self.total and self.handled:
            self.progress(force=True)
        parts = ["Described %d image(s)" % summary["described"]]
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
        if summary["stopped"]:
            self.ctx.log("Stopped. Descriptions so far are saved; run again to continue.")
