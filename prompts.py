#!/usr/bin/env python3
"""The prompt sent with every image, its version, and the reply schema.

Each image is sent with SYSTEM_PROMPT (system_prompt(profile) for the
extended profile), the user text from user_text(library) and the JSON
schema from schema(profile), which the LLM's reply must follow. The fields
are listed once (see fields()); describe.parse_reply() uses the same list
to check and tidy replies. search_terms() turns a stored description (this
version's or an older one) into the text the search page matches.

Two profiles exist: "core" (every library) and "extended" (opt-in per
library: counted objects, landmarks, a location guess, mood, activities),
which costs more output tokens.

Kept apart from describe.py so tools that only count or search
descriptions (progress.py, serve.py) can use it without importing Pillow.
This module uses only the standard library.
"""

import collections
import copy
import hashlib
import json
import re

# Raise this whenever the prompt or the schema changes in a way that makes
# older descriptions worth redoing. Re-describing is always something the
# user asks for (scan.py --redescribe-stale); the hash of the prompt and
# schema is recorded with every description, but does not decide staleness.
PROMPT_VER = 5

PROFILES = ("core", "extended")
DEFAULT_PROFILE = "core"

# Longest list item (characters) the schema allows.
ITEM_LENGTH = 60
# llama.cpp turns maxLength and maxItems into grammar repetitions and
# refuses counts of 2000 or more, so a longer limit is only applied when a
# reply is tidied (describe.coerce_info), not sent in the schema.
GRAMMAR_REPEAT_LIMIT = 1999
# Longest context notes (characters) sent with each image.
NOTES_LENGTH = 1000

PHOTO_TYPES = ("photo", "screenshot", "document", "scan", "artwork", "meme", "diagram",
               "other")
DOCUMENT_TYPES = ("none", "receipt", "chat", "web", "app", "whiteboard", "sign", "menu",
                  "letter", "form", "ticket", "card_or_id", "book_page", "handwriting",
                  "slide", "map", "other")
SETTINGS = ("indoor", "outdoor", "vehicle", "unknown")
TIMES_OF_DAY = ("day", "sunrise_sunset", "night", "unknown")
EVENTS = ("none", "birthday", "wedding", "holiday", "travel", "party", "meal", "sports",
          "concert", "graduation", "work", "other")
PEOPLE = ("none", "1", "2", "3-5", "6-10", "crowd")
QUALITY_FLAGS = ("blurry", "dark", "overexposed", "accidental", "screen_photo")
SENSITIVE = ("none", "suggestive", "explicit", "graphic")
MOODS = ("calm", "cheerful", "energetic", "dramatic", "somber", "romantic", "eerie",
         "nostalgic", "neutral")
SCORE_FIELDS = ("aesthetic", "sharpness", "exposure")
SCORE_RANGE = (1, 5)
OBJECT_COUNT_RANGE = (1, 99)

# One reply field. kind is one of:
#   text     a string of at most limit characters
#   enum     one of values; default replaces an unknown value, empty a missing one
#   list     at most limit short strings
#   flags    at most limit items of values
#   bool     true or false
#   score    an integer in SCORE_RANGE
#   objects  at most limit {"name", "count"} objects
# lower: list items are lower-cased when replies are tidied.
Field = collections.namedtuple("Field", "name kind limit values default empty lower")


def _field(name, kind, limit=0, values=(), default="", empty=None, lower=True):
    return Field(name, kind, limit, tuple(values), default,
                 default if empty is None else empty, lower)


# In the order the model writes them: looking (description, title, kind of
# image, text) comes before listing (things, keywords) and judging (flags,
# scores), so the later fields can build on what the model already wrote.
CORE_FIELDS = (
    _field("description", "text", 800),
    _field("title", "text", 120),
    _field("photo_type", "enum", values=PHOTO_TYPES, default="other"),
    _field("document_type", "enum", values=DOCUMENT_TYPES, default="other", empty="none"),
    _field("text", "text", 6000),
    _field("text_language", "text", 8),
    _field("setting", "enum", values=SETTINGS, default="unknown"),
    _field("scene", "text", 40),
    _field("time_of_day", "enum", values=TIMES_OF_DAY, default="unknown"),
    _field("event", "enum", values=EVENTS, default="other", empty="none"),
    _field("people", "enum", values=PEOPLE, default="none"),
    _field("people_descriptors", "list", 6),
    _field("subjects", "list", 10, lower=False),
    _field("animals", "list", 8),
    _field("food", "list", 8),
    _field("vehicles", "list", 8),
)
EXTENDED_FIELDS = (
    _field("objects", "objects", 12),
    _field("activities", "list", 6),
    _field("landmarks", "list", 5, lower=False),
    _field("location_guess", "text", 80),
    _field("mood", "enum", values=MOODS, default="neutral"),
)
CLOSING_FIELDS = (
    _field("colors", "list", 5),
    _field("tags", "list", 15),
    _field("quality_flags", "flags", 5, values=QUALITY_FLAGS),
    _field("sensitive", "enum", values=SENSITIVE, default="none"),
    _field("private_info", "bool", default=False),
    _field("aesthetic", "score", default=None),
    _field("sharpness", "score", default=None),
    _field("exposure", "score", default=None),
)


def fields(profile=DEFAULT_PROFILE):
    """The reply fields of a profile ("core" or "extended"), in schema order."""
    if profile == "extended":
        return CORE_FIELDS + EXTENDED_FIELDS + CLOSING_FIELDS
    return CORE_FIELDS + CLOSING_FIELDS


def check_profile(profile):
    """profile if it is a known profile, else DEFAULT_PROFILE."""
    return profile if profile in PROFILES else DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# The prompt

_SYSTEM_TEMPLATE = """\
You catalog the pictures in a personal photo library. People will find each picture \
later only by searching for words you write, so describe it the way someone would \
search for it. Reply with one JSON object that follows the given schema, using exactly \
the field names below, and nothing else.

Rules:
- Describe only what is visible. Do not invent details, and do not guess at things \
the picture cannot show, such as names, exact places, or what happened before or after.
- Never identify a person or say who someone is, even if they look famous or a name \
appears nearby. Describe people generically instead: "toddler", "older man in a red \
jacket", "bride", "two teenagers on bikes". Do not guess ethnicity, religion, \
nationality, health or other sensitive traits. Names written in the picture (on a \
cake, a sign, a jersey, a document) are still transcribed in the text field; just \
never say that a pictured person is that name.
- When you are unsure, prefer "unknown", "none", an empty string or an empty list to \
a guess.
- Write in English unless the library notes ask for another language. Text you \
transcribe stays in its original language.
- List items are short phrases, usually one to four words (people descriptors may \
be a little longer), most important first, with no duplicates.
- The user message may include notes about the library from its owner. Use them as \
background only where they fit what you see; they never change these rules.

Fields:
- description: two or three sentences about the main subject, the setting and \
notable details (actions, objects, colors, weather, signs). Start with the content \
itself, not with "This image shows". For a screenshot or document, say what kind it \
is and what it is about.
- title: 3 to 8 specific words, like a photo caption: "Golden retriever catching a \
frisbee on a beach", not "Dog photo". No period at the end.
- photo_type: photo (a camera picture of people, places or things), screenshot \
(captured directly from a phone or computer screen), document (a picture taken mainly \
to record something to read, such as a receipt, letter, form, page, ticket, card, \
whiteboard or notice), scan (a scanned print, slide or paper), artwork (painting, \
drawing, illustration, cartoon or 3D render), meme (a picture with a joke caption), \
diagram (chart, graph, map, plan or schematic) or other.
- document_type: the kind of readable content that is the main subject: receipt, \
chat (messages), web (a web page), app (another app screen), whiteboard, sign, menu, \
letter, form, ticket, card_or_id (ID, passport, payment, membership or business \
card), book_page, handwriting, slide, map or other. Use none when the picture is not \
mainly about text, as for most photos.
- text: all legible text, transcribed exactly as written (same spelling, capitals, \
numbers and language), with separate lines or blocks on separate lines. Do not \
translate, correct or complete it, never invent text you cannot read, and do not \
add labels, headings or notes of your own (such as "[Table]"). Use "" when there is \
none. For long documents, transcribe the main text and skip repeated boilerplate.
- text_language: the ISO 639-1 code of the main language of that text, such as "en" \
or "de"; "" when there is no text.
- setting: indoor, outdoor, vehicle (inside a car, bus, train, plane or boat) or \
unknown (screenshots, documents, close-ups without context).
- scene: 1 to 3 words naming the kind of place, such as "beach", "kitchen", "city \
street", "mountain trail" or "office"; "" when there is no real place.
- time_of_day: day, sunrise_sunset (low golden sun or a twilight sky), night, or \
unknown (indoors, screenshots, documents, or when you cannot tell).
- event: the occasion, only when the picture clearly shows it: birthday (cake with \
candles, party hats), wedding, holiday (holiday decorations or costumes), travel \
(sightseeing, luggage, famous sights), party, meal (people eating together, a laid \
table), sports (playing or watching), concert, graduation or work. Use other for a \
clear occasion not in this list, and none for everyday scenes.
- people: how many people are visible: none, 1, 2, 3-5, 6-10 or crowd (more than ten).
- people_descriptors: up to 6 generic descriptions, one per visible person (never \
two for the same person), by apparent age group, clothing, role or action: "toddler in a yellow raincoat", "woman holding a \
baby", "man playing guitar". Never names.
- subjects: up to 10 main things in the picture, most important first: "golden \
retriever", "frisbee", "sandy beach".
- animals: kinds of real, living animals, as specific as you can tell (toys, \
statues, stickers and pictures of animals go in subjects instead): "golden retriever", "tabby \
cat", "seagull".
- food: dishes, drinks and ingredients: "pepperoni pizza", "latte", "strawberries".
- vehicles: kinds of vehicles: "red pickup truck", "sailboat", "bicycle", "steam \
train".
{extended}- colors: up to 5 dominant colors as simple names: "red", "navy blue", "white".
- tags: up to 15 lowercase keywords someone might type to find this picture: objects, \
the kind of place, activities, the occasion, the season or weather when obvious, and \
the kind of picture. Use singular words ("dog", not "dogs") and add common synonyms \
and broader words ("puppy", "pet", "animal"). Tags may repeat words from other fields.
- quality_flags: any that apply of blurry (out of focus or motion blur), dark \
(underexposed), overexposed (washed out), accidental (pocket shot, finger over the \
lens, no clear subject) and screen_photo (a camera photo of a screen); [] for a good \
picture.
- sensitive: none, suggestive (sexualized pose, lingerie, partial nudity), explicit \
(nudity or sexual activity) or graphic (blood, injury, gore or violence). Ordinary \
swimwear is none.
- private_info: true when the picture shows details someone would not want shared: \
ID documents, passports, payment cards, bank or account numbers, passwords or access \
codes, home addresses, medical or financial documents, letters, statements or \
forms that show a person's name together with an address or an account, policy or \
ID number, or private messages; otherwise false.
- aesthetic, sharpness, exposure: whole numbers from 1 (poor) to 5 (excellent). \
aesthetic is composition, light and appeal as a photo (an ordinary snapshot is 3), \
sharpness is focus and detail on the main subject, exposure is brightness and \
contrast. For screenshots and documents, rate how clean and legible they are."""

_EXTENDED_GUIDE = """\
- objects: up to 12 countable things with how many are visible, such as {"name": \
"chair", "count": 4}. Count carefully; for large numbers give your best estimate.
- activities: up to 6 things people or animals are doing: "hiking", "blowing out \
candles", "swimming".
- landmarks: famous named places or structures you recognize with confidence, such \
as "Eiffel Tower" or "Golden Gate Bridge"; [] when unsure.
- location_guess: the city, region or country, only when landmarks, signs or \
distinctive scenery make it clear; "" otherwise.
- mood: the overall feel: calm, cheerful, energetic, dramatic, somber, romantic, \
eerie, nostalgic or neutral.
"""

_SYSTEM_PROMPTS = {
    "core": _SYSTEM_TEMPLATE.replace("{extended}", ""),
    "extended": _SYSTEM_TEMPLATE.replace("{extended}", _EXTENDED_GUIDE),
}
SYSTEM_PROMPT = _SYSTEM_PROMPTS["core"]

USER_TEXT = "Describe this image."

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def system_prompt(profile=DEFAULT_PROFILE):
    """The system prompt for a profile (SYSTEM_PROMPT for "core")."""
    return _SYSTEM_PROMPTS[check_profile(profile)]


def user_text(library, taken_at=None, projection=""):
    """The text sent after the image: "Describe this image." plus the library's notes.

    library is a library dict (or None); its optional "context_notes" (at
    most NOTES_LENGTH characters) are added as background, so they are sent
    to the provider with every image of that library. taken_at
    ('YYYY-MM-DD...') adds the month and year the picture was taken; the
    describe step does not pass it (no EXIF data leaves the machine), but
    callers that want to may. projection ("equirectangular" or
    "dual_fisheye") adds projection_note() for a 360-degree photo.
    """
    parts = [USER_TEXT]
    note = projection_note(projection)
    if note:
        parts.append(note)
    notes = library.get("context_notes") if isinstance(library, dict) else None
    if isinstance(notes, str) and notes.strip():
        notes = notes.strip()[:NOTES_LENGTH]
        parts.append("Notes about this photo library from its owner:\n" + notes)
    match = re.match(r"^(\d{4})-(\d{2})", taken_at or "")
    if match and 1 <= int(match.group(2)) <= 12:
        parts.append("It was taken in %s %s." % (_MONTHS[int(match.group(2)) - 1],
                                                 match.group(1)))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The schema

def _string_list(field):
    items = {"type": "string", "maxLength": ITEM_LENGTH}
    return {"type": "array", "maxItems": field.limit, "items": items}


def _field_schema(field):
    if field.kind == "text":
        if field.limit > GRAMMAR_REPEAT_LIMIT:
            return {"type": "string"}
        return {"type": "string", "maxLength": field.limit}
    if field.kind == "enum":
        return {"type": "string", "enum": list(field.values)}
    if field.kind == "list":
        return _string_list(field)
    if field.kind == "flags":
        return {"type": "array", "maxItems": field.limit,
                "items": {"type": "string", "enum": list(field.values)}}
    if field.kind == "bool":
        return {"type": "boolean"}
    if field.kind == "score":
        return {"type": "integer", "minimum": SCORE_RANGE[0], "maximum": SCORE_RANGE[1]}
    if field.kind == "objects":
        return {"type": "array", "maxItems": field.limit, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "maxLength": ITEM_LENGTH},
                "count": {"type": "integer", "minimum": OBJECT_COUNT_RANGE[0],
                          "maximum": OBJECT_COUNT_RANGE[1]},
            },
            "required": ["name", "count"]}}
    raise ValueError("unknown field kind: %s" % field.kind)


def schema(profile=DEFAULT_PROFILE):
    """The JSON schema replies must follow (a fresh copy each call).

    Every field is required and every object closed (additionalProperties
    false), as strict structured-output modes need. Strings and lists carry
    maxLength / maxItems bounds for local servers' grammars; llm.sanitize_schema
    drops what a cloud provider does not accept, and parse_reply enforces
    the bounds again.
    """
    properties = {}
    for field in fields(check_profile(profile)):
        properties[field.name] = _field_schema(field)
    return copy.deepcopy({"type": "object", "additionalProperties": False,
                          "properties": properties, "required": list(properties)})


def prompt_hash(profile=DEFAULT_PROFILE):
    """A short fingerprint (sha1, 12 hex digits) of a profile's system prompt and schema.

    Recorded with every description, so descriptions made with a different
    prompt can be told apart even within one PROMPT_VER.
    """
    profile = check_profile(profile)
    text = system_prompt(profile) + "\n" + json.dumps(schema(profile), sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Search text

# Free-text fields whose words are searchable, for this version's
# descriptions and older ones (v4 had description, subjects, text, tags and
# colors).
_SEARCH_FIELDS = ("title", "description", "subjects", "tags", "text", "animals", "food",
                  "vehicles", "people_descriptors", "scene", "colors", "landmarks",
                  "activities")
# Enum fields added to the search text unless they hold this "nothing" value.
_SEARCH_ENUMS = (("setting", "unknown"), ("time_of_day", "unknown"), ("event", "none"),
                 ("photo_type", ""), ("document_type", "none"))
_PATH_SEPARATORS = re.compile(r"[\\/_\-.,+()\[\]]+")


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _enum_words(value):
    """'card_or_id' -> 'card id', 'sunrise_sunset' -> 'sunrise sunset'."""
    return value.replace("_or_", " ").replace("_", " ")


def search_terms(info, name="", path="", taken_at="", projection=""):
    """The lower-cased text a search is matched against, for one image.

    It holds the description's words (title, description, subjects, tags,
    visible text, animals, food, vehicles, people descriptors, scene,
    colors, and for the extended profile landmarks, activities and object
    names), the meaningful enum values (setting, time of day, event, photo
    and document type, but not "unknown" or "none"), the file name, the
    relative path and its folder words, and the capture year and month name.
    info is a description dict (this version's or an older one) or None.
    A 360-degree photo (projection) also matches "360" and "panorama".
    """
    parts = []
    if projection_note(projection):
        parts.append("360 panorama 360-degree")
    if isinstance(info, dict):
        for key in _SEARCH_FIELDS:
            parts.extend(_strings(info.get(key)))
        objects = info.get("objects")
        if isinstance(objects, list):
            parts.extend(item["name"] for item in objects
                         if isinstance(item, dict) and isinstance(item.get("name"), str))
        for key, nothing in _SEARCH_ENUMS:
            value = info.get(key)
            if isinstance(value, str) and value and value != nothing:
                parts.append(_enum_words(value))
    if isinstance(name, str) and name:
        parts.append(name)
    if isinstance(path, str) and path:
        parts.append(path)
        folder = path.replace("\\", "/").rpartition("/")[0]
        words = _PATH_SEPARATORS.sub(" ", folder).strip()
        if words:
            parts.append(words)
    match = re.match(r"^(\d{4})-(\d{2})", taken_at or "")
    if match:
        parts.append(match.group(1))
        month = int(match.group(2))
        if 1 <= month <= 12:
            parts.append(_MONTHS[month - 1])
    return " ".join(part.strip() for part in parts if part and part.strip()).lower()


def projection_note(projection):
    """A sentence to add to the prompt for a 360-degree photo ("" otherwise).

    360 photos are sent as equirectangular panoramas (see images.py), whose
    bent lines and stretched top and bottom are not part of the scene.
    """
    if projection in ("equirectangular", "dual_fisheye"):
        return ("This is a 360-degree photo shown as an equirectangular panorama: "
                "describe the scene, not the projection or the distortion.")
    return ""
