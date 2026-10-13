#!/usr/bin/env python3
"""Spot descriptions of videos that describe the contact sheet, not the video.

A video (and a drive) is described from one picture that holds frames from
it in a grid (videos.py, drives.py). The prompt asks for the video itself
(prompts.video_note, drives.drive_note), yet models still write "Four
sequential frames show ...", "The top-left panel shows ...", "A sequence
of dashcam footage ..." or "visible in each frame". find() spots such
"sheet talk" in a title or a description:

- counted frames, panels, stills, shots, views, moments or images ("four
  frames", "4 sequential images", "nine panels", "a four-panel montage",
  "the four images", "four pictures show ...");
- a sequence, series, grid, montage, collage or composite of them ("a
  sequence of four frames", "a series of dashcam shots", "a sequence of
  dashcam footage"), sequential ones ("sequential views"), a "video
  collage", a "contact sheet", a "split-screen", or a sheet "split into"
  / "divided into" panels;
- one frame or panel among the others: by its place ("the top-left
  frame", "the left frame", "the lower panels", "Top right shows ..."), by
  its order ("the third panel", "in the final frame", "the second shot",
  "in the first view", "in the final shot"), "in each frame", "in one
  view", "in one of the frames", "in some frames", "the other views";
- talk across them ("across the frames", "between the panels", "across
  four different moments"), and a picture called "a still frame from a
  video".

Normal video language is left alone: "across the frame", "in the frame",
"out of frame", "the frame of a door", "a picture frame", "frame rate",
"a sequence of events", "the views", "a wide shot", "a 360-degree view",
"two people"; so are what every frame of a video carries ("an overlay at
the bottom of each frame", "the time and speed show in each frame"),
real frames, panels and pictures ("the frames of his glasses", "a
six-panel door", "each panel of the quilt", "a collage of photos hangs on
the wall"), taking pictures and shots in a game ("takes two photos",
"takes a series of shots", "his second shot lands on the green") and the
words of a video with cuts ("The final shot shows ...", "in one
segment", "filmed in one shot": only being "in" the first, final or
another shot or view counts). The rules are English; a description in
another language is never caught.

describe.describe_checked asks the model again once (with reask_note in
the prompt) when a new video or drive description has sheet talk, unless
config.json's "video_sheet_check" is false (enabled). A current video
description with sheet talk that was not asked again ("reasked" in its
provenance) counts as out of date (prompts.stale_sql, through the SQL
function db.connect registers: register), and so does such a drive
description (drives.pending with redescribe_stale): Re-describe out of
date describes them again.

This module uses only the standard library.
"""

import hashlib
import json
import re
import sqlite3

# The config.json switch (true unless set to false).
SETTING = "video_sheet_check"

# What the provenance of a description that was asked again says.
REASKED = "sheet"

# The SQL function (on every db.connect connection): 1 for a description
# (info, provenance) with sheet talk that was not asked again, else 0.
SQL_FUNCTION = "sheet_talk"

# Most matched phrases kept (in provenance and logs).
MAX_PHRASES = 5

_NUMBER_WORDS = r"two|three|four|five|six|seven|eight|nine|ten|twelve|sixteen"
_NUMBER = r"(?:%s|[2-9]|1[0-6])" % _NUMBER_WORDS
_SOME = r"(?:several|multiple)"
_ORDINAL = r"(?:second|third|fourth|fifth|sixth|seventh|eighth|ninth)"
# Words that make a picture one of the sheet's: "four sequential images",
# "four dashcam photos" (but not "two photographs").
_MARK = (r"(?:sequential|consecutive|successive|different|distinct|separate|individual|"
         r"identical|still|video|dashcam|dash|cam|dashboard|camera|dashcam-style|aerial|"
         r"chronological|time-lapse|timelapse|captured|selected|sampled|extracted|frozen)")
# Words that may stand between a count (or "sequence of") and the noun.
_ADJ = r"(?:(?:%s|nearly|almost|similar|static|evenly|spaced|evenly-spaced|snapshot)\s+)" % _MARK
# Nouns that (in a video's description) are the sheet's parts.
_STRONG = r"(?:frames|panels|stills|snapshots|screenshots|quadrants)"
# (Not "still": "one still stands", "each still holding".)
_STRONG_ONE = r"(?:frame|panel|snapshot|screenshot)"
# Nouns that also have other meanings, caught in fewer patterns.
_WEAK = r"(?:shots|views|moments)"
_PICTURES = r"(?:images|pictures|photos|photographs)"
_WEAK_ONE = r"(?:shot|view|image|picture|photo|photograph)"
_CORNER = r"(?:(?:top|bottom|upper|lower|middle)[- ](?:left|right))"
_PLACE = r"(?:%s|top|bottom|upper|lower|left|right)" % _CORNER
_NOT_RATE = r"(?!\s+(?:per|a|an|each|every)\s+(?:second|minute)\b)"

# (rule, pattern, checks): checks are what to rule out around a match
# (see _ruled_out).
_RULES = (
    # Counted: "four frames", "4 sequential images", "nine panels", "a
    # four-panel montage", "several stills", "a 2 x 2 collage".
    ("counted", r"\b%s\s+%s{0,3}(?:%s|%s)\b%s" % (_NUMBER, _ADJ, _STRONG, _WEAK, _NOT_RATE),
     ("physical", "taking")),
    ("counted", r"\b%s\s+%s{0,2}%s\s+%s{0,2}%s\b" % (_NUMBER, _ADJ, _MARK, _ADJ, _PICTURES),
     ("physical", "taking")),
    ("counted", r"\b%s\s+%s{0,3}%s\b%s" % (_SOME, _ADJ, _STRONG, _NOT_RATE), ("physical",)),
    ("counted", r"\b(?:%s|[2-9])-(?:panel|frame|image|still|photo|picture|view|tile|grid|way "
     r"split)s?\b" % _NUMBER_WORDS, ("fixture",)),
    ("counted", r"\b(?:[23]|two|three)\s*(?:x|by|-by-)\s*(?:[23]|two|three)\s+(?:panels?|frames?|"
     r"collage|montage)\b", ()),
    # Counted pictures: "the four images", "Four pictures show ..." (not
    # "takes the two photos", "the two photos on the shelf").
    ("counted", r"\bthe\s+%s\s+%s{0,2}%s\b" % (_NUMBER, _ADJ, _PICTURES), ("taking", "hanging")),
    ("counted", r"\b%s\s+%s{0,2}%s(?=\s+(?:show|capture|depict|display|reveal|feature|"
     r"document|follow|track|illustrate)\b)" % (_NUMBER, _ADJ, _PICTURES), ()),
    # A sequence of them: "a sequence of four frames", "a series of dashcam
    # shots", "a sequence of dashcam footage", "sequential views", "a video
    # collage", "a contact sheet", "a split-screen", "split into four
    # panels" (not "takes a series of shots", "a collage of photos hangs on
    # the wall").
    ("sequence", r"\b(?:sequence|series|succession|grid|montage|collage|compilation|mosaic|"
     r"composite) of (?:%s\s+)?%s{0,3}(?:%s|%s|%s|footage)\b"
     % (_NUMBER, _ADJ, _STRONG, _WEAK, _PICTURES), ("physical", "taking", "hanging")),
    ("sequence", r"\bsplit[- ]?screens?\b", ()),
    ("sequence", r"\b(?:sequential|consecutive|successive)\s+%s{0,2}(?:%s|%s|%s)\b"
     % (_ADJ, _STRONG, _WEAK, _PICTURES), ("physical", "taking")),
    ("sequence", r"\b(?:video|photo|picture|image|frame)\s+(?:collage|montage|mosaic)\b", ()),
    ("sequence", r"\bcontact sheets?\b", ()),
    ("sequence", r"\b(?:split|divided|separated|arranged|broken)\s+(?:up\s+)?into\s+(?:a\s+)?"
     r"(?:%s\s+)?%s{0,2}(?:frames|panels|quadrants|images|stills|views|squares|tiles|cells|"
     r"grid)\b" % (_NUMBER, _ADJ), ()),
    # Some of them: "the first two frames", "in some frames", "the later
    # frames", "the lower panels", "the other views", "in one of the frames"
    # (not "the frames" alone: picture frames on a wall).
    ("some of them", r"\b(?:(?:the\s+)?(?:these|those|all|some|other|later|earlier|subsequent|"
     r"following|remaining|both|many|most|first|last|final|next|previous|%s)\s+"
     r"(?:(?:%s|first|last)\s+)?%s{0,2}|the\s+(?:%s\s+%s{0,2}|%s{1,2}))"
     r"(?:frames|stills|snapshots|screenshots|quadrants)\b%s"
     % (_ORDINAL, _NUMBER, _ADJ, _NUMBER, _ADJ, _ADJ, _NOT_RATE), ("physical", "edge")),
    ("some of them", r"\b(?:all|other|later|earlier|subsequent|following|remaining|both|first|"
     r"last|final|next|previous|%s)\s+(?:%s\s+)?%s{0,2}panels\b" % (_ORDINAL, _NUMBER, _ADJ),
     ("physical",)),
    ("some of them", r"\b(?:other|later|earlier|subsequent|remaining|all)\s+(?:(?:the|%s)\s+)?"
     r"%s{0,2}(?:shots|views|moments|images)\b" % (_NUMBER, _ADJ), ("taking", "scenery")),
    ("some of them", r"\b(?:one|two|three|four|five|six|seven|eight|nine|some|each|all|both|"
     r"most|several|many|none|any|either|half)\s+of\s+the\s+(?:%s\s+)?%s{0,2}"
     r"(?:%s|shots|views|images)\b" % (_NUMBER, _ADJ, _STRONG), ("physical",)),
    # One of them: "in each frame", "each panel", "in one frame", "the third
    # panel", "in the final frame", "the second shot", "in a separate shot",
    # "in the first view", "in the final shot", "in one view", "in the first
    # moment". ("At the bottom of each frame", "overlaid on each frame" and
    # "the time and speed in each frame" -- what every frame of the video
    # carries, such as a dashcam's overlay -- are left alone; so are "his
    # second shot" and "the second shot goes in", and "The final shot
    # shows ...": film words, where only being "in" that shot counts.)
    ("one of them", r"\b(?:each|every)\s+(?:single\s+)?%s{0,2}(?:panel|snapshot|screenshot|"
     r"quadrant)\b" % _ADJ, ("physical",)),
    ("one of them", r"\bin\s+(?:each|every)\s+(?:single\s+)?%s{0,2}(?:frame|%s)\b"
     % (_ADJ, _WEAK_ONE), ("overlay",)),
    ("one of them", r"\b(?:first|last|final|next|previous|later|earlier|subsequent|another|one|"
     r"%s)\s+%s{0,1}%s\b%s" % (_ORDINAL, _ADJ, _STRONG_ONE, _NOT_RATE), ("physical",)),
    ("one of them", r"\b%s\s+(?:shot|view|image|picture|photo)\b" % _ORDINAL,
     ("taking", "possessive", "game")),
    ("one of them", r"\bin\s+(?:the\s+)?(?:first|last|final|next|previous|later|earlier|"
     r"subsequent|%s)\s+(?:(?:two|three|few)\s+)?(?:shots?|views?)\b" % _ORDINAL, ()),
    ("one of them", r"\bin\s+(?:one\s+view|another\s+(?:shot|view|image|picture))\b", ()),
    ("one of them", r"\bin\s+a\s+(?:separate|different)\s+(?:shot|frame|panel|image|view)\b", ()),
    ("one of them", r"\b(?:first|%s)\s+moment\b" % _ORDINAL, ()),
    # By its place: "the top-left frame", "the bottom two panels", "in the
    # top right view", "Top left shows ..." (not "the upper-left quadrant"
    # of a picture, or "the lower frame" of a door).
    ("place", r"\b%s(?:\s+%s)?\s+(?:frames|stills|snapshots|screenshots|panels)\b"
     % (_PLACE, _NUMBER), ("physical",)),
    ("place", r"\b%s\s+(?:frame|panel|snapshot|screenshot|view|image|shot|picture|photo|one)\b"
     % _CORNER, ("physical",)),
    ("place", r"\b(?:top|bottom|upper|lower)\s+(?:%s\s+)?(?:images|pictures|photos|shots|views)\b"
     % _NUMBER, ("taking",)),
    ("place", r"\b%s\s+(?:shows?|captures?|depicts?|features?|displays?|reveals?)\b" % _CORNER,
     ()),
    # "the left frame", "the right panel shows ..." (not "the left frame of
    # the door", "the right frame edge", "the car's left panel").
    ("place", r"\b(?:left|right)(?:-hand)?\s+(?:frame|panel|snapshot|screenshot)\b",
     ("physical", "border", "possessive")),
    # Across them: "across the frames", "between the panels", "across four
    # different moments", "throughout the shots".
    ("across", r"\b(?:across|between|throughout|among|amongst)\s+(?:(?:the|these|those|all|"
     r"all the|each of the)\s+)?(?:%s\s+)?%s{0,2}%s\b" % (_NUMBER, _ADJ, _STRONG), ("physical",)),
    ("across", r"\b(?:across|between|throughout)\s+(?:the|these|those|all|"
     r"all the|each of the|%s)\s+(?:%s\s+)?%s{0,2}(?:shots|views|images|moments)\b"
     % (_NUMBER, _NUMBER, _ADJ), ("scenery",)),
    # The picture as a frame of the video: "a still frame from a video", "a
    # video frame shows", "frames from a dashcam video".
    ("still frame", r"\b(?:a|the|this|one)\s+(?:single\s+|still\s+|static\s+|video\s+){0,2}"
     r"(?:frame|still)\s+(?:from|of)\s+(?:a|an|the|this)\s+(?:\w+\s+)?(?:video|clip|recording|"
     r"footage|film)\b", ()),
    ("still frame", r"\b(?:a|this)\s+(?:single\s+|still\s+)?video\s+(?:frame|still)\b", ()),
    ("still frame", r"\bstill frames?\b", ()),
    ("still frame", r"\b(?:frames|stills|snapshots|panels|screenshots)\s+(?:taken\s+)?(?:from|of)"
     r"\s+(?:a|an|the|this)\s+(?:[\w-]+\s+){0,2}(?:video|clip|recording|footage|film|drive)\b",
     ()),
)

_COMPILED = tuple((name, re.compile(pattern, re.IGNORECASE), checks)
                  for name, pattern, checks in _RULES)

# "physical": "the frames of his glasses", "frames on the wall", "one panel
# of the fence" are real frames and panels.
_PHYSICAL_AFTER = re.compile(
    r"\s+(?:of|on|around)\s+(?:(?:his|her|their|its|the|a|an|some|those|these|my|your|our)"
    r"\s+)?(?:[\w-]+\s+)?(?:glasses|eyeglasses|sunglasses|spectacles|bikes?|bicycles?|beds?|"
    r"bunk|doors?|doorways?|windows?|paintings?|pictures?|photos?|photographs?|art|artwork|"
    r"mirrors?|houses?|buildings?|chairs?|tents?|hives?|beehives?|structures?|walls?|shelf|"
    r"shelves|racks?|swings?|fences?|gates?|tables?|looms?|machines?|solar|roofs?|ceilings?|"
    r"glass|wood|metal|steel|fabric|paper|plastic|concrete|stone|quilts?|drywall|comics?|"
    r"comic\s+strips?|cabinets?|closets?)\b", re.IGNORECASE)
_FRAMES_OR_PANELS = re.compile(r"(?:frames?|panels?)$", re.IGNORECASE)
# "scenery": "across the views of the valley", "the other views from the
# deck" are views of a place.
_OF_AFTER = re.compile(r"\s+(?:of|from|over|across|toward|towards)\b", re.IGNORECASE)
# "fixture": "a six-panel door", "four-panel closet doors".
_FIXTURE_AFTER = re.compile(
    r"\s+(?:[\w-]+\s+){0,2}(?:doors?|closets?|windows?|fences?|gates?|shutters?|cabinets?|"
    r"radiators?|screens?|curtains?|walls?|ceilings?)\b", re.IGNORECASE)
# "edge": "an overlay at the bottom of the frames" -- what every frame of
# the video shows, as "at the bottom of each frame".
_ALL_FRAMES = re.compile(r"(?:the|these|all|all the|both)\s+frames$", re.IGNORECASE)
_EDGE_BEFORE = re.compile(r"\b(?:bottom|top|edges?|corners?|sides?|base)\s+of\s+$",
                          re.IGNORECASE)
# "taking": "takes two photos", "poses for several pictures", "makes three
# shots", "takes a series of shots", "takes his second shot".
_TAKING_BEFORE = re.compile(
    r"\b(?:take|takes|taking|took|taken|snap|snaps|snapping|snapped|pose|poses|posing|posed|"
    r"hold|holds|holding|held|print|prints|printed|fire|fires|firing|"
    r"fired|hit|hits|hitting|make|makes|making|made|pour|pours|pouring|poured|drink|drinks|"
    r"drinking|drank|order|orders|ordered|attempt|attempts|attempting|"
    r"miss|misses|missed|missing|block|blocks|blocked|sink|sinks|sank|shoot|shoots|"
    r"shooting|shot|score|scores|scored|scoring|practice|practices|practicing|"
    r"practising)\s+(?:(?:for|up|off|out|of|at)\s+)?"
    r"(?:(?:the|a|an|another|his|her|their|its|one)\s+)?$", re.IGNORECASE)
_TAKEN_NOUNS = re.compile(r"(?:shots?|photos?|photographs?|pictures?|images?|snapshots?)$",
                          re.IGNORECASE)
# "hanging": "a collage of photos hangs on the wall", "the two pictures
# above the desk" are real pictures.
_HANGING_AFTER = re.compile(
    r"\s+(?:hangs?|hanging|hung|(?:is|are)\s+(?:hung|pinned|taped|mounted|framed|displayed|"
    r"stuck)|(?:sits?|rests?|leans?|stands?)\s+on|(?:on|above|over|across|along)\s+(?:the|a|"
    r"an|his|her|their)\s+(?:[\w-]+\s+){0,2}(?:walls?|fridge|refrigerator|board|door|shelf|"
    r"shelves|desk|table|easel|mantel|mantelpiece|dresser))\b", re.IGNORECASE)
# "possessive": "his second shot", "the golfer's second shot", "the car's
# left panel" (but "the video's second shot" is the sheet's).
_POSSESSIVE_BEFORE = re.compile(r"(?:\b(?:his|her|their|my|your|our|its)|\b(\w+)'s|s')\s+$",
                                re.IGNORECASE)
_SHEET_OWNERS = ("video", "clip", "footage", "recording", "sheet", "film", "dashcam", "camera")
# "game": "the second shot goes in", "the third shot lands on the green".
_GAME_AFTER = re.compile(
    r"\s+(?:goes|went|lands|landed|misses|missed|hits|bounces|bounced|sails|sailed|rolls|"
    r"rolled|drops|dropped|sinks|sank|scores|scored|rattles|clanks|curls|hooks|slices|"
    r"is\s+(?:good|blocked|saved|wide|short|long|in|out)|at\s+(?:the\s+)?(?:hoop|basket|"
    r"goal|net|pin|hole|green|target|rim|cup))\b", re.IGNORECASE)
# "border": "the right frame edge", "the left frame rail".
_BORDER_AFTER = re.compile(r"\s+(?:edges?|borders?|rails?|lines?)\b", re.IGNORECASE)
# "overlay": "the time and speed show in each frame" -- a dashcam's (or a
# camera's) overlay is in every frame of the video.
_OVERLAY_WORDS = re.compile(
    r"\b(?:overlays?|overlaid|timestamps?|time\s*stamps?|date\s*stamps?|watermarks?|"
    r"captions?|subtitles?|readouts?|speedometer|GPS|coordinates|mph|km/h)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_LEADING_THE = re.compile(r"^the\s+", re.IGNORECASE)


def _sentence(text, match):
    """The sentence of text around a match."""
    start = 0
    for end in _SENTENCE_END.finditer(text, 0, match.start()):
        start = end.end()
    after = _SENTENCE_END.search(text, match.end())
    return text[start:after.end() if after else len(text)]


def _possessive(text, match):
    """True when a possessive stands before a match ("his", "the golfer's"), but not the
    video's own ("the video's second shot")."""
    found = _POSSESSIVE_BEFORE.search(text[max(0, match.start() - 30):match.start()])
    if not found:
        return False
    owner = found.group(1)
    return not (owner and owner.lower() in _SHEET_OWNERS)


def _ruled_out(text, match, checks):
    """True when a match is a real thing ("physical", "fixture", "scenery", "hanging",
    "border"), an act ("taking", "game", "possessive") or what every frame of the video
    shows ("edge", "overlay"), not the sheet."""
    if "physical" in checks and _FRAMES_OR_PANELS.search(match.group(0)) and \
            _PHYSICAL_AFTER.match(text, match.end()):
        return True
    if "scenery" in checks and _OF_AFTER.match(text, match.end()):
        return True
    if "fixture" in checks and _FIXTURE_AFTER.match(text, match.end()):
        return True
    if "edge" in checks and _ALL_FRAMES.match(match.group(0)) and \
            _EDGE_BEFORE.search(text[max(0, match.start() - 30):match.start()]):
        return True
    if "taking" in checks and _TAKEN_NOUNS.search(match.group(0)):
        before = text[max(0, match.start() - 40):match.start()]
        if _TAKING_BEFORE.search(before):
            return True
    if "hanging" in checks and _HANGING_AFTER.match(text, match.end()):
        return True
    if "border" in checks and _BORDER_AFTER.match(text, match.end()):
        return True
    if "game" in checks and _GAME_AFTER.match(text, match.end()):
        return True
    if "possessive" in checks and _possessive(text, match):
        return True
    if "overlay" in checks and _OVERLAY_WORDS.search(_sentence(text, match)):
        return True
    return False


def find(text):
    """The phrases of text (a title or a description) that talk about the contact sheet.

    Returns [phrase] in the order they appear (overlapping matches merged),
    [] when there are none (or text is not a string).
    """
    if not isinstance(text, str) or not text:
        return []
    spans = []
    for _name, pattern, checks in _COMPILED:
        for match in pattern.finditer(text):
            if not _ruled_out(text, match, checks):
                spans.append((match.start(), match.end()))
    if not spans:
        return []
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    # (Without a leading "the": "the first two frames" is "first two frames".)
    return [_LEADING_THE.sub("", " ".join(text[start:end].split())) for start, end in merged]


def in_info(info):
    """find() over a description dict's title and description (other fields are left alone)."""
    if not isinstance(info, dict):
        return []
    found = []
    for key in ("title", "description"):
        found.extend(find(info.get(key)))
    return found


def was_reasked(provenance):
    """True when a description's provenance (a dict) says it was asked again for sheet talk."""
    return isinstance(provenance, dict) and provenance.get("reasked") == REASKED


def unsettled(info, provenance):
    """True when a description (info and provenance dicts) has sheet talk and was not asked
    again: it counts as out of date (see the module docstring)."""
    return not was_reasked(provenance) and bool(in_info(info))


# Whether a description (by a digest of its info column) has sheet talk:
# the Manage page counts out-of-date descriptions often, and the rules take
# a while over thousands of videos. Emptied when it holds MEMO_SIZE.
MEMO_SIZE = 200000
_memo = {}


def _sql_unsettled(info_text, provenance_text):
    """unsettled() for the SQL function: the info and provenance columns as JSON text."""
    if isinstance(provenance_text, str) and '"reasked"' in provenance_text:
        try:
            if was_reasked(json.loads(provenance_text)):
                return 0
        except ValueError:
            pass
    if not isinstance(info_text, str):
        return 0
    key = hashlib.blake2b(info_text.encode("utf-8", "surrogatepass"), digest_size=16).digest()
    found = _memo.get(key)
    if found is None:
        try:
            found = 1 if in_info(json.loads(info_text)) else 0
        except ValueError:
            found = 0
        if len(_memo) >= MEMO_SIZE:
            _memo.clear()
        _memo[key] = found
    return found


def register(conn):
    """Make SQL_FUNCTION(info, provenance) known on an sqlite3 connection."""
    try:
        conn.create_function(SQL_FUNCTION, 2, _sql_unsettled, deterministic=True)
    except sqlite3.NotSupportedError:
        # (deterministic needs SQLite 3.8.3 or later.)
        conn.create_function(SQL_FUNCTION, 2, _sql_unsettled)


def enabled(config):
    """True unless config.json turns the check off ("video_sheet_check": false)."""
    return not (isinstance(config, dict) and config.get(SETTING) is False)


def reask_note(kind="video"):
    """The sentence added to the user text when a video ("video") or a drive ("drive") is
    asked again because its first description talked about the frames."""
    what = "drive" if kind == "drive" else "video"
    scene = "one continuous trip" if kind == "drive" else "one continuous scene"
    return ("Your first answer described the frames or panels of this picture instead of the "
            "%s: describe the footage itself, as %s, and never mention frames, panels, shots, "
            "moments, a grid, a sequence of images or their positions." % (what, scene))
