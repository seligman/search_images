#!/usr/bin/env python3
"""The Duplicates page: stacks of alike pictures to review, and the best of each.

The search page stacks pictures that look alike (search_index groups them:
identical files, and near-identical shots of about the same shape taken at
about the same time).
The Duplicates page (/duplicates, the admin's) lists those stacks, the
largest first, with what tells the pictures of a stack apart (size, file
size, date, sharpness, format, file name) and a suggestion of the one to
keep (rank_members): the owner can keep it and hide the others, hide one,
or say that the pictures are not duplicates (see not_duplicates.py).
Hiding is the usual hide (nothing is deleted; Unhide undoes it).

review_page() answers GET /api/admin/duplicates (see admin.py). The stacks
of a set of libraries are worked out once and kept until the pictures (or
the pairs kept apart) change.

This module uses only the standard library.
"""

import collections
import itertools
import json
import math
import os
import re
import threading

import common
import not_duplicates
import search_index

KINDS = ("all", "exact", "similar")
PAGE_SIZE = 20
MIN_SIZE_LIMIT = 100
# The most pictures "Not duplicates" keeps apart from each other at once
# (every two of them make a pair; one picture taken out of its stack, with
# "Not a duplicate", makes a pair with each of the others and has no limit).
NOT_DUPLICATES_LIMIT = 100
# A picture with less than this share of the pixels of another in its stack
# is a smaller copy of it (made for a chat, a web page or an e-mail), never
# the best, when it also has the same shape (aspect ratios at most
# SMALLER_COPY_SHAPE apart) and nearly the same look (dHashes at most
# SMALLER_COPY_BITS apart). A smaller picture of another shape (a crop, a
# panorama) or with other content (another shot) is just smaller: it is
# ranked by its other merits, not as a copy.
SMALLER_COPY = 0.5
SMALLER_COPY_SHAPE = 0.03
SMALLER_COPY_BITS = 4
# How many sets of stacks are kept (one per library choice).
MEMO_SIZE = 4

# File names that say a picture is a copy or an edit of another: "photo (1).jpg",
# "photo - Copy.jpg", "photo_edited.jpg", "photo~2.jpg".
_COPY_NAME_RE = re.compile(
    r"(?i)(?:^|[\s_.(-])(?:copy|copie|kopie|copia|edited|bearbeitet)(?=[\s_.)\d-]|$)"
    r"|\(\d+\)\.[a-z0-9]+$|~\d+\.[a-z0-9]+$")
# ... that it was saved from a chat or social app ...
_CHAT_NAME_RE = re.compile(
    r"(?i)^(?:IMG-\d{8}-WA\d+|WhatsApp Image|received_|FB_IMG_|Messenger_|signal-\d{4}-"
    r"|telegram-|Snapchat-|Screenshot_\d{8}-\d{6}_(?:WhatsApp|Facebook|Messenger|Instagram))")
# ... or that it is a screenshot.
_SCREENSHOT_NAME_RE = re.compile(r"(?i)^(?:screenshot|screen ?shot|bildschirmfoto|capture d)")
# File formats by extension, as the page names them.
FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".png": "PNG", ".gif": "GIF",
    ".heic": "HEIC", ".heif": "HEIF", ".hif": "HEIF", ".webp": "WebP", ".tif": "TIFF",
    ".tiff": "TIFF", ".avif": "AVIF", ".bmp": "BMP", ".insp": "Insta360",
    # Videos (they only stack with copies of the same file).
    ".mov": "MOV", ".mp4": "MP4", ".m4v": "M4V", ".3gp": "3GP", ".3g2": "3G2",
    ".avi": "AVI", ".mkv": "MKV", ".webm": "WebM", ".wmv": "WMV", ".mts": "MTS",
    ".m2ts": "M2TS", ".mpg": "MPEG", ".mpeg": "MPEG",
}
# How likely a format is the camera's own, higher first: phones save HEIC
# and convert it to JPEG to share it; editors, chat apps and web pages turn
# JPEGs into PNG or WebP. Formats not named here count 1.
ORIGINAL_FORMATS = {"HEIC": 3, "HEIF": 3, "Insta360": 3, "JPEG": 2, "TIFF": 2}
# Two pictures in different formats are one picture saved twice when their
# file names differ only in the extension, or when the same camera took
# them at the same second and their dHashes are at most this many bits apart.
CONVERSION_BITS = 1
# Where a date taken comes from (images.taken_source), in words.
DATE_SOURCES = {"exif": "camera", "filename": "file name", "mtime": "file date",
                "video": "video"}

_FACT_COLUMNS = "id, size, content_hash, taken_source, dhash"
# What rank_members compares, in this order (the places in its key).
_SIZE, _SHARP, _COPY, _PIXELS, _FORMAT, _BYTES, _TAKEN, _ORDER = range(8)


def file_format(name):
    """The format of a picture as its file name says ("JPEG", "HEIC", ...), or ""."""
    return FORMATS.get(os.path.splitext(name or "")[1].lower(), "")


def _taken_number(text):
    digits = re.sub(r"\D", "", text or "")[:14]
    return int(digits.ljust(14, "0")) if len(digits) >= 8 else None


def _member(record, facts):
    """The facts of one picture the page shows (and ranks by)."""
    name = record.get("n") or ""
    width, height = int(record.get("w") or 0), int(record.get("hgt") or 0)
    member = {
        "id": record["i"],
        "library": record.get("l", ""),
        "name": name,
        "folder": record.get("f", ""),
        "title": record.get("t", ""),
        "width": width,
        "height": height,
        "bytes": int(facts.get("size") or 0),
        "format": file_format(name),
        "taken": record.get("ts") or record.get("dk") or "",
        "date_source": DATE_SOURCES.get(facts.get("taken_source") or "", ""),
        "camera": record.get("cm", ""),
        "sharpness": record.get("sh"),
        "aesthetic": record.get("q"),
        "type": record.get("ty", ""),
        "content": facts.get("content_hash") or "",
        "dhash": facts.get("dhash") or "",
    }
    if record.get("p"):
        member["p"] = record["p"]
    return member


def _format_rank(member):
    return ORIGINAL_FORMATS.get(member["format"], 1)


def _conversions(members):
    """Pairs (a, b) of members that look like one picture saved in two formats.

    Their file names differ only in the extension (IMG_0001.HEIC and
    IMG_0001.jpg), or the same camera took them at the same second and
    their dHashes are at most CONVERSION_BITS apart. Only pictures that
    share a name or a moment are compared.
    """
    buckets = collections.defaultdict(list)
    for member in members:
        if not member["format"]:
            continue
        buckets["name", os.path.splitext(member["name"])[0].lower()].append(member)
        if member["camera"] and member["date_source"] == "camera" and len(member["taken"]) >= 19:
            buckets["moment", member["camera"], member["taken"][:19]].append(member)
    pairs = {}
    for bucket, alike in buckets.items():
        for first, second in itertools.combinations(alike, 2):
            if first["format"] == second["format"]:
                continue
            if bucket[0] == "moment":
                one = search_index.parse_dhash(first["dhash"])
                other = search_index.parse_dhash(second["dhash"])
                if one is None or other is None or bin(one ^ other).count("1") > CONVERSION_BITS:
                    continue
            pairs[min(first["id"], second["id"]), max(first["id"], second["id"])] = (first, second)
    return list(pairs.values())


def _units(members, conversions):
    """{member id: unit id}: identical files, and one picture saved in two
    formats, are one unit (their descriptions describe the same picture)."""
    parent = {member["id"]: member["id"] for member in members}

    def find(image_id):
        while parent[image_id] != image_id:
            parent[image_id] = parent[parent[image_id]]
            image_id = parent[image_id]
        return image_id

    first_of = {}
    for member in members:
        if member["content"]:
            other = first_of.setdefault(member["content"], member["id"])
            parent[find(member["id"])] = find(other)
    for first, second in conversions:
        parent[find(first["id"])] = find(second["id"])
    return {member["id"]: find(member["id"]) for member in members}


def _pixels(member):
    return member["width"] * member["height"]


def _smaller_copies(members):
    """{member id: the larger member it is a smaller copy of}.

    A smaller copy has less than SMALLER_COPY of the other's pixels, the
    same shape (search_index.same_shape within SMALLER_COPY_SHAPE) and a
    dHash at most SMALLER_COPY_BITS from the other's; of several such
    larger members, it is a copy of the largest. Without a size or a dHash
    a picture is never taken for a copy, nor for a copy of the same file.
    """
    hashes = {member["id"]: search_index.parse_dhash(member["dhash"]) for member in members}
    largest_first = sorted(members, key=_pixels, reverse=True)
    copies = {}
    for member in members:
        aspect = search_index.aspect_ratio(member["width"], member["height"])
        value = hashes[member["id"]]
        if aspect is None or value is None:
            continue
        for other in largest_first:
            if _pixels(member) >= SMALLER_COPY * _pixels(other):
                break
            if member["content"] and other["content"] == member["content"]:
                continue
            other_value = hashes[other["id"]]
            if (other_value is not None
                    and bin(value ^ other_value).count("1") <= SMALLER_COPY_BITS
                    and search_index.same_shape(
                        aspect, search_index.aspect_ratio(other["width"], other["height"]),
                        SMALLER_COPY_SHAPE)):
                copies[member["id"]] = other
                break
    return copies


def _copy_signs(member, stack, originals):
    """Reasons, in words, why a picture looks like a copy rather than the original.

    originals maps a member's id to the member it seems converted from.
    """
    signs = []
    name = member["name"]
    if member["id"] in originals:
        signs.append("it seems to be %s saved as %s" % (originals[member["id"]]["name"],
                                                         member["format"]))
    if _COPY_NAME_RE.search(name):
        signs.append("its file name says it is a copy or an edit")
    if _CHAT_NAME_RE.search(name):
        signs.append("it was saved from a chat or social app")
    if member["type"] == "screenshot" or _SCREENSHOT_NAME_RE.search(name):
        signs.append("it is a screenshot")
    if not member["camera"] and any(other["camera"] for other in stack):
        signs.append("it has no camera data")
    if (member["date_source"] != "camera"
            and any(other["date_source"] == "camera" for other in stack)):
        signs.append("its date comes from the file, not the camera")
    return signs


def rank_members(members):
    """(members best first, the reason for the best in words).

    The best is, in this order: not a smaller copy (see _smaller_copies: a
    picture with less than SMALLER_COPY of another's pixels, of the same
    shape and nearly the same look; a smaller picture that differs in shape
    or content is not one); the sharpest (when every picture has
    a sharpness score; identical files and one picture saved in two
    formats count as one picture, with the best score of them); the one
    that looks least like a copy (converted from a format cameras save in,
    copy or edit in its file name, saved from a chat app, a screenshot, no
    camera data when others have it, a date from the file when others have
    the camera's); the most pixels; the format cameras save in
    (ORIGINAL_FORMATS); the largest file (of alike pictures in one format,
    the one with the most detail); taken first; found first. The reason
    names the first of these that sets the best apart from the next one.
    Each member gets "copy" (its signs of being a copy, a smaller copy's
    first) and "twin" (the id of an identical file ranked above it, or
    None).
    """
    smaller = _smaller_copies(members)
    conversions = _conversions(members)
    originals = {}
    for first, second in conversions:
        if _format_rank(first) != _format_rank(second):
            copy, original = sorted((first, second), key=_format_rank)
            originals.setdefault(copy["id"], original)
    units = _units(members, conversions)
    # A picture described twice (identical files, or two formats) may be
    # scored differently each time: it counts with its best score.
    scores = {}
    for member in members:
        unit = units[member["id"]]
        if member["sharpness"]:
            scores[unit] = max(scores.get(unit, 0), member["sharpness"])
    unit_count = len(set(units.values()))
    all_sharp = unit_count > 1 and len(scores) == unit_count
    for member in members:
        member["copy"] = _copy_signs(member, members, originals)
        larger = smaller.get(member["id"])
        if larger is not None:
            member["copy"].insert(0, "it is a smaller copy of %s (%s)" % (
                larger["name"] or "a larger picture", _size_text(larger)))

    def key(member):
        pixels = _pixels(member)
        taken = _taken_number(member["taken"])
        return (0 if member["id"] in smaller else 1,
                scores[units[member["id"]]] if all_sharp else 0,
                -len(member["copy"]),
                pixels,
                _format_rank(member),
                member["bytes"],
                -taken if taken is not None else -10 ** 15,
                -member["id"])
    ranked = sorted(members, key=key, reverse=True)
    first_of = {}
    for member in ranked:
        content = member["content"]
        member["twin"] = first_of.get(content) if content else None
        if content:
            first_of.setdefault(content, member["id"])
    return ranked, _reason(ranked, key)


def _size_text(member):
    return "%d \u00d7 %d" % (member["width"], member["height"])


def _bytes_texts(larger, smaller):
    """Two file sizes in words, as precise as it takes to tell them apart."""
    for unit, size, formats in (("MB", 1024.0 * 1024.0, ("%.1f", "%.2f")),
                                ("KB", 1024.0, ("%.0f", "%.1f"))):
        if larger >= size or unit == "KB":
            for number in formats:
                texts = [(number + " " + unit) % (count / size) for count in (larger, smaller)]
                if texts[0] != texts[1]:
                    return texts
    return ["{:,} bytes".format(larger), "{:,} bytes".format(smaller)]


def _reason(ranked, key):
    """Why ranked[0] is the best, in words (see rank_members): what sets it
    apart from the next one, ranked[1]."""
    best = ranked[0]
    if len(ranked) < 2:
        return "The only picture left in this stack."
    runner = ranked[1]
    best_key, runner_key = key(best), key(runner)
    step = next((index for index in range(len(best_key)) if best_key[index] != runner_key[index]),
                len(best_key))
    if best["content"] and all(member["content"] == best["content"] for member in ranked):
        if step == _COPY:
            return "Identical files. This one looks the least like a copy (the next: %s)." % (
                "; ".join(runner["copy"]))
        if step == _TAKEN:
            return "Identical files. This one has the earliest date; the next looks like a copy."
        return "Identical files. Keeping the one found first."
    if step == _SIZE:
        # (Every picture ranked below a smaller copy is one too.)
        smaller = max(ranked[1:], key=_pixels)
        return ("The full-size picture (%s); the others are smaller copies (%s at most)."
                % (_size_text(best), _size_text(smaller)))
    if step == _SHARP:
        return ("The sharpest: sharpness %d of 5 (the next best: %d)."
                % (best_key[_SHARP], runner_key[_SHARP]))
    if step == _COPY:
        if best["copy"]:
            return "Looks the least like a copy (the next: %s)." % "; ".join(runner["copy"])
        return "The original: the next one looks like a copy (%s)." % "; ".join(runner["copy"])
    if step == _PIXELS:
        if _pixels(runner) < SMALLER_COPY * _pixels(best):
            # Much smaller, but of another shape or look: not called a copy.
            return "The most pixels: %s (%.1f MP); the next is a smaller picture (%s)." % (
                _size_text(best), _pixels(best) / 1e6, _size_text(runner))
        return "The most pixels: %s (%.1f MP), the next %s." % (
            _size_text(best), _pixels(best) / 1e6, _size_text(runner))
    if step == _FORMAT:
        return ("Saved as %s, the way cameras save pictures; the next one is %s, more often a "
                "converted copy." % (best["format"], runner["format"] or "in another format"))
    if step == _BYTES:
        return "The largest file (%s, the next %s): usually the most detail." % tuple(
            _bytes_texts(best["bytes"], runner["bytes"]))
    if step == _TAKEN:
        if _taken_number(runner["taken"]) is None:
            return "It has a date taken; the next one has none."
        return "Taken before the next one, which is a later shot or a copy."
    return ("It ties with the next one on everything the index knows; this one was "
            "found first.")


def _stack(members):
    """A stack as the page gets it, from its members' facts."""
    ranked, reason = rank_members(members)
    contents = {member["content"] for member in ranked}
    exact = len(contents) == 1 and "" not in contents
    return {
        "id": min(member["id"] for member in ranked),
        "library": ranked[0]["library"],
        "kind": "exact" if exact else "similar",
        "size": len(ranked),
        "best": ranked[0]["id"],
        "reason": reason,
        "members": ranked,
        # Kinds of content: how many parts "Not duplicates" makes at most.
        "contents": len(contents),
    }


def _facts(conn, ids):
    """{image id: row facts} for ids (size, content hash, date source)."""
    facts = {}
    ids = list(ids)
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        for row in conn.execute("SELECT %s FROM images WHERE id IN (%s)"
                                % (_FACT_COLUMNS, ",".join("?" * len(part))), part):
            facts[row["id"]] = dict(row)
    return facts


def build_stacks(conn, config, library_ids):
    """Every stack of the libraries' visible pictures, largest first.

    Each is a dict: id (its first picture's id), library, kind ("exact":
    identical files only, or "similar"), size, best (the id to keep),
    reason (why, in words), members (best first; see rank_members and
    _member) and contents (how many different files it holds).
    """
    records = search_index.build_records(conn, config, library_ids, include_hidden=False)
    groups = collections.defaultdict(list)
    for record in records:
        if "dg" in record:
            groups[record["dg"]].append(record)
    facts = _facts(conn, [record["i"] for members in groups.values() for record in members])
    stacks = [_stack([_member(record, facts.get(record["i"], {})) for record in members])
              for members in groups.values() if len(members) > 1]
    # Largest first; then the most recently added.
    stacks.sort(key=lambda stack: (-stack["size"],
                                   -max(member["id"] for member in stack["members"])))
    return stacks


_memo_lock = threading.Lock()
_memo = collections.OrderedDict()     # key -> stacks


def stacks_for(conn, config, library_ids):
    """build_stacks(), kept until the libraries' pictures or the pairs kept apart change."""
    library_ids = list(library_ids)
    key = json.dumps([common.db_path(), library_ids, search_index.data_state(conn, library_ids),
                      not_duplicates.version(), search_index.GROUPS_VERSION],
                     separators=(",", ":"))
    with _memo_lock:
        found = _memo.get(key)
        if found is not None:
            _memo.move_to_end(key)
            return found
    stacks = build_stacks(conn, config, library_ids)
    with _memo_lock:
        _memo[key] = stacks
        while len(_memo) > MEMO_SIZE:
            _memo.popitem(last=False)
    return stacks


def _public(stack):
    """A stack without what only the server needs."""
    members = []
    for member in stack["members"]:
        item = dict(member)
        item.pop("content", None)
        item.pop("dhash", None)
        item.pop("library", None)
        members.append(item)
    result = dict(stack)
    result["members"] = members
    return result


def review_page(conn, config, library_ids, kind="all", min_size=2, page=1, stack_id=None,
                page_size=PAGE_SIZE):
    """What GET /api/admin/duplicates answers.

    The stacks of the libraries with at least min_size pictures, of a kind
    (KINDS: "exact" are identical files only, "similar" the others), one
    page of page_size. stack_id (an image id) asks for the one stack that
    holds that picture instead. Answers {stacks, total (stacks), pictures
    (in them), extra (pictures the best of each would leave: what "keep
    the best" hides), page, pages, page_size, kind, min, counts ({kind:
    stacks} with min_size), stack (the stack_id asked for, or None),
    not_duplicates_limit (the largest stack "Not duplicates" can split)}.
    """
    stacks = stacks_for(conn, config, library_ids)
    sized = [stack for stack in stacks if stack["size"] >= min_size]
    counts = {"all": len(sized),
              "exact": sum(1 for stack in sized if stack["kind"] == "exact")}
    counts["similar"] = counts["all"] - counts["exact"]
    if stack_id is not None:
        chosen = [stack for stack in stacks
                  if any(member["id"] == stack_id for member in stack["members"])]
    else:
        chosen = [stack for stack in sized if kind == "all" or stack["kind"] == kind]
    pages = max(1, int(math.ceil(len(chosen) / float(page_size))))
    page = max(1, min(int(page), pages))
    start = (page - 1) * page_size
    return {
        "stacks": [_public(stack) for stack in chosen[start:start + page_size]],
        "total": len(chosen),
        "pictures": sum(stack["size"] for stack in chosen),
        "extra": sum(stack["size"] - 1 for stack in chosen),
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "kind": kind,
        "min": min_size,
        "counts": counts,
        "stack": stack_id,
        "not_duplicates_limit": NOT_DUPLICATES_LIMIT,
    }
