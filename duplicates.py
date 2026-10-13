#!/usr/bin/env python3
"""The Duplicates page: stacks of alike pictures to review, and the best of each.

The search page stacks pictures that look alike (search_index groups them:
identical files, and near-identical shots of about the same shape taken at
about the same time, by their dHashes or their looks).
The Duplicates page (/duplicates, the admin's) lists those stacks, the
largest first, with what tells the pictures of a stack apart (size, file
size, date, sharpness, format, file name) and a suggestion of the one to
keep (rank_members): the owner can keep it and hide the others, hide one,
or say that the pictures are not duplicates (see not_duplicates.py).
Hiding is the usual hide (nothing is deleted; Unhide undoes it).

"Same picture, any date" (kind "same") lists other stacks: pictures that
look the same (search_index.look_copies: their looks at least
search_index.SAME_PICTURE_COSINE close), whatever their dates and in any
of the libraries shown, that the search page does not stack together: a
copy saved again years later or kept in another library, a picture
without a date to go by, or a near-identical shot the search page put in
another stack. It needs the looks (looks.py); the same best and actions
apply ("Not duplicates" there keeps apart only pictures of different
stacks of the search page: see admin.py's together).

review_page() answers GET /api/admin/duplicates (see admin.py). The stacks
of a set of libraries are worked out once and kept until the pictures
(their looks, the owner's marks or the pairs kept apart) change; the
"Same picture, any date" groups only when they are asked for, and kept
until the pictures with looks or the looks change (hiding a picture does
not count).

This module uses only the standard library.
"""

import collections
import hashlib
import itertools
import json
import math
import os
import re
import threading

import albums
import common
import looks
import not_duplicates
import search_index

# "same": the "Same picture, any date" stacks (build_same_stacks); the others
# are the search page's stacks.
KINDS = ("all", "exact", "similar", "same")
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
_MARKED, _SIZE, _SHARP, _COPY, _PIXELS, _FORMAT, _BYTES, _TAKEN, _ORDER = range(9)


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
    # The owner's marks (albums.py): a favorite, its rating, how many albums.
    if record.get("fav"):
        member["favorite"] = True
    if record.get("rt"):
        member["rating"] = record["rt"]
    if record.get("al"):
        member["albums"] = len(record["al"])
    return member


def _marks_key(member):
    """How much the owner marked a picture: (favorite, rating, in an album)."""
    return (1 if member.get("favorite") else 0, member.get("rating") or 0,
            1 if member.get("albums") else 0)


def _marks_text(member):
    """The owner's marks of a picture in words ("a favorite, rated 4 stars"), or ""."""
    parts = []
    if member.get("favorite"):
        parts.append("a favorite")
    if member.get("rating"):
        parts.append("rated %d star%s" % (member["rating"], "" if member["rating"] == 1 else "s"))
    if member.get("albums"):
        parts.append("in an album" if member["albums"] == 1
                     else "in %d albums" % member["albums"])
    return ", ".join(parts)


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

    The best is, in this order: the one the owner marked (a favorite, then
    the higher rating, then one in an album: albums.py); not a smaller copy
    (see _smaller_copies: a picture with less than SMALLER_COPY of
    another's pixels, of the same shape and nearly the same look; a smaller
    picture that differs in shape or content is not one); the sharpest
    (when every picture has a sharpness score; identical files and one
    picture saved in two formats count as one picture, with the best score
    of them); the one that looks least like a copy (converted from a format
    cameras save in, copy or edit in its file name, saved from a chat app,
    a screenshot, no camera data when others have it, a date from the file
    when others have the camera's); the most pixels; the format cameras save in
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
        return (_marks_key(member),
                0 if member["id"] in smaller else 1,
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
    if step == _MARKED:
        mine = _marks_text(best)
        other = _marks_text(runner)
        return ("%sYou marked this one: %s (the next one: %s)." % (
            "Identical files. " if best["content"] and all(
                member["content"] == best["content"] for member in ranked) else "",
            mine, other or "no marks"))
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


def _sorted(stacks):
    """stacks largest first; then the most recently added."""
    stacks.sort(key=lambda stack: (-stack["size"],
                                   -max(member["id"] for member in stack["members"])))
    return stacks


def build_stacks(conn, config, library_ids, records=None):
    """Every stack of the libraries' visible pictures, largest first.

    Each is a dict: id (its first picture's id), library, kind ("exact":
    identical files only, or "similar"), size, best (the id to keep),
    reason (why, in words), members (best first; see rank_members and
    _member) and contents (how many different files it holds). records
    are the libraries' search records without hidden pictures, when the
    caller has them.
    """
    if records is None:
        records = search_index.build_records(conn, config, library_ids, include_hidden=False)
    groups = collections.defaultdict(list)
    for record in records:
        if "dg" in record:
            groups[record["dg"]].append(record)
    facts = _facts(conn, [record["i"] for members in groups.values() for record in members])
    return _sorted([_stack([_member(record, facts.get(record["i"], {})) for record in members])
                    for members in groups.values() if len(members) > 1])


_SAME_COLUMNS = "id, library_id, content_hash, media, projection, width, height, hidden"
# The keys of a search record that _member reads.
_MEMBER_KEYS = ("i", "l", "n", "f", "t", "w", "hgt", "ts", "dk", "cm", "sh", "q", "ty", "p",
                "fav", "rt", "al")


def _look_items(conn, library_ids, records):
    """The pictures of the libraries that "Same picture, any date" compares, hidden ones
    too: [(image id, content hash, aspect ratio, look key (search_index.look_key))]
    in id order, and the set of the hidden ones among them. records are the
    libraries' search records with the hidden pictures (build_records)."""
    by_id = {record["i"]: record for record in records}
    items = []
    hidden = set()
    for start in range(0, len(library_ids), 500):
        part = library_ids[start:start + 500]
        sql = ("SELECT %s FROM images WHERE missing_since IS NULL AND library_id IN (%s)"
               % (_SAME_COLUMNS, ",".join("?" * len(part))))
        for row in conn.execute(sql, part):
            record = by_id.get(row["id"])
            if record is None:
                continue
            key = search_index.look_key(row, record)
            if key is None:
                continue
            items.append((row["id"], row["content_hash"],
                          search_index.aspect_ratio(row["width"], row["height"]), key))
            if row["hidden"]:
                hidden.add(row["id"])
    items.sort()
    return items, hidden


_copies_lock = threading.Lock()
_copies_memo = collections.OrderedDict()   # key -> (groups, ids with a look)


def _copies_key(items):
    """What look_copy_groups() gives depends on: the kept looks and the items."""
    digest = hashlib.sha1(json.dumps([common.db_path(), looks.store_key(),
                                      search_index.SAME_PICTURE_COSINE]).encode("utf-8"))
    for image_id, content_hash, aspect, key in items:
        digest.update(("%d %s %r %s\n" % (image_id, content_hash, aspect, key[1])
                       ).encode("utf-8", "replace"))
    return digest.hexdigest()


def look_copy_groups(items, compute=True):
    """(search_index.look_copies of items, the ids of those with a look), worked out
    only when the items or the kept looks changed; None when compute is False
    and they are not known yet.

    items are _look_items() (hidden pictures too: hiding or showing one does
    not make the groups be worked out again; the stacks leave them out).
    """
    key = _copies_key(items)
    with _copies_lock:
        found = _copies_memo.get(key)
        if found is not None:
            _copies_memo.move_to_end(key)
            return found
    if not compute:
        return None
    vectors = search_index.group_looks({image_id: look_key
                                        for image_id, _content, _aspect, look_key in items})
    groups = search_index.look_copies([(image_id, content_hash, aspect, vectors[image_id])
                                       for image_id, content_hash, aspect, _key in items
                                       if image_id in vectors])
    found = (groups, frozenset(vectors))
    with _copies_lock:
        _copies_memo[key] = found
        while len(_copies_memo) > MEMO_SIZE:
            _copies_memo.popitem(last=False)
    return found


def _separable(view, ids):
    """The ids of a "Same picture, any date" stack that "Not duplicates" can keep apart
    from another of them: pairs go by content (identical files are one), and
    those within one stack of the search page are left out (see admin.py's
    together)."""
    content = {image_id: view["content"].get(image_id) for image_id in ids}
    shared = collections.defaultdict(set)       # search page stack -> its contents here
    for image_id in ids:
        if image_id in view["dg"]:
            shared[view["dg"][image_id]].add(content[image_id])
    found = set()
    for one, other in itertools.combinations(ids, 2):
        first, second = content[one], content[other]
        if first != second and not any(first in kinds and second in kinds
                                       for kinds in shared.values()):
            found.update((one, other))
    return found


def build_same_stacks(conn, view, compute=True):
    """The "Same picture, any date" stacks of a _view(), largest first, and how many of
    its visible pictures have a look: (stacks, looked); None when compute is
    False and the groups of look_copy_groups() are not known yet.

    A stack is a group of search_index.look_copies (pictures that look the
    same, whatever their dates, in any of the view's libraries) without
    its hidden pictures, split where the owner said pictures are not
    duplicates, that the search page does not show as one stack (its
    pictures are in two stacks or more, or alone). The stacks are as
    build_stacks() makes them, with kind "same"; each member in a stack on
    the search page also has "stack" (that stack's id, as the search page's
    dg) and "stacked" (how many pictures it holds), a member (and a stack)
    that "Not duplicates" cannot keep apart from any other (_separable) has
    can_apart False, and a stack whose pictures are in more than one
    library has "libraries".
    """
    found = view.get("same")
    if found is not None:
        return found
    copies = look_copy_groups(view["items"], compute)
    if copies is None:
        return None
    groups, with_look = copies
    hidden, slim, dg = view["hidden"], view["records"], view["dg"]
    groups = [[image_id for image_id in ids if image_id not in hidden] for ids in groups]
    groups = [ids for ids in groups if len(ids) > 1]
    keys = {image_id: not_duplicates.unit_key(image_id, view["content"].get(image_id))
            for ids in groups for image_id in ids}
    groups = not_duplicates.split_groups(groups, keys)
    # Only what the search page does not show as one stack already.
    groups = [ids for ids in groups if len({dg.get(image_id, ("alone", image_id))
                                             for image_id in ids}) > 1]
    facts = _facts(conn, [image_id for ids in groups for image_id in ids])
    stacks = []
    for ids in groups:
        members = []
        for image_id in ids:
            member = _member(slim[image_id], facts.get(image_id, {}))
            if image_id in dg:
                member["stack"] = dg[image_id]
                member["stacked"] = view["sizes"][dg[image_id]]
            members.append(member)
        stack = _stack(members)
        stack["kind"] = "same"
        # "Not duplicates" keeps apart pictures of different files and stacks
        # (see admin.py's together): say where there is nothing to keep apart.
        separable = _separable(view, ids)
        for member in members:
            if member["id"] not in separable:
                member["can_apart"] = False
        if not separable:
            stack["can_apart"] = False
        libraries = sorted({member["library"] for member in members})
        if len(libraries) > 1:
            stack["libraries"] = libraries
        stacks.append(stack)
    found = (_sorted(stacks), sum(1 for image_id in slim if image_id in with_look))
    view["same"] = found
    return found


_memo_lock = threading.Lock()
_memo = collections.OrderedDict()     # key -> _view()


def _view(conn, config, library_ids):
    """What the page needs of the libraries, kept until their pictures (or their looks,
    the owner's marks, the pairs kept apart) change: a dict of stacks
    (build_stacks()), dg ({image id: its search page stack}), sizes ({stack:
    pictures}), items and hidden (_look_items()), content ({image id: content
    hash} of those), records ({image id: what _member reads} of the visible
    ones) and same (build_same_stacks(), once worked out).
    """
    library_ids = list(library_ids)
    key = json.dumps([common.db_path(), library_ids, search_index.data_state(conn, library_ids),
                      not_duplicates.version(), search_index.GROUPS_VERSION,
                      albums.data_state(conn, library_ids), looks.store_key()],
                     separators=(",", ":"))
    with _memo_lock:
        found = _memo.get(key)
        if found is not None:
            _memo.move_to_end(key)
            return found
    records = search_index.build_records(conn, config, library_ids, include_hidden=True)
    visible = [record for record in records if not record.get("h")]
    items, hidden = _look_items(conn, library_ids, records)
    eligible = {item[0] for item in items}
    found = {
        "stacks": build_stacks(conn, config, library_ids, visible),
        "dg": {record["i"]: record["dg"] for record in visible if "dg" in record},
        "items": items,
        "hidden": hidden,
        "content": {item[0]: item[1] for item in items},
        "records": {record["i"]: {name: record[name] for name in _MEMBER_KEYS if name in record}
                    for record in visible if record["i"] in eligible},
        "same": None,
    }
    found["sizes"] = collections.Counter(found["dg"].values())
    with _memo_lock:
        _memo[key] = found
        while len(_memo) > MEMO_SIZE:
            _memo.popitem(last=False)
    return found


def stacks_for(conn, config, library_ids):
    """build_stacks(), kept until the libraries' pictures (or their looks, the owner's
    marks, the pairs kept apart) change."""
    return _view(conn, config, library_ids)["stacks"]


def same_stacks_for(conn, config, library_ids):
    """The stacks of build_same_stacks(), kept likewise."""
    return build_same_stacks(conn, _view(conn, config, library_ids))[0]


def _public(stack):
    """A stack without what only the server needs."""
    members = []
    for member in stack["members"]:
        item = dict(member)
        item.pop("content", None)
        item.pop("dhash", None)
        if not stack.get("libraries"):
            item.pop("library", None)
        members.append(item)
    result = dict(stack)
    result["members"] = members
    return result


def review_page(conn, config, library_ids, kind="all", min_size=2, page=1, stack_id=None,
                page_size=PAGE_SIZE):
    """What GET /api/admin/duplicates answers.

    The stacks of the libraries with at least min_size pictures, of a kind
    (KINDS: "exact" are identical files only, "similar" the others, "all"
    both; "same" the "Same picture, any date" stacks, see
    build_same_stacks), one page of page_size. stack_id (an image id) asks
    for the one stack of the kind ("same", or else the search page's) that
    holds that picture instead. Answers {stacks, total (stacks), pictures
    (in them), extra (pictures the best of each would leave: what "keep
    the best" hides), page, pages, page_size, kind, min, counts ({kind:
    stacks} with min_size), stack (the stack_id asked for, or None),
    not_duplicates_limit (the largest stack "Not duplicates" can split),
    looked (how many of the visible pictures have a look: "same" needs
    them)}. The "same" stacks are only worked out when they are asked for:
    until then (and after the looks changed) counts has no "same" and
    looked is None.
    """
    view = _view(conn, config, library_ids)
    stacks = view["stacks"]
    sized = [stack for stack in stacks if stack["size"] >= min_size]
    counts = {"all": len(sized),
              "exact": sum(1 for stack in sized if stack["kind"] == "exact")}
    counts["similar"] = counts["all"] - counts["exact"]
    same, looked = build_same_stacks(conn, view, compute=kind == "same") or ([], None)
    same_sized = [stack for stack in same if stack["size"] >= min_size]
    if looked is not None:
        counts["same"] = len(same_sized)
    if stack_id is not None:
        chosen = [stack for stack in (same if kind == "same" else stacks)
                  if any(member["id"] == stack_id for member in stack["members"])]
    elif kind == "same":
        chosen = same_sized
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
        "looked": looked,
    }
