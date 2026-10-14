#!/usr/bin/env python3
"""People: the faces People found (people.py), grouped into the people they show.

The faces of one person are grouped by their vectors (SFace's: two
pictures of one face have a cosine of about 0.4 or more, two people about
0): group() puts the faces whose quality (people.quality) is at least
"group_quality" in groups by average linkage -- the two groups whose faces
are the most alike on average become one, while that average is at least
"match", and never two groups with faces of one picture (two faces of one
picture are two people) -- and a group of at least "min_faces" faces, in
pictures of at least two different moments (MIN_MOMENTS: the frames of a
burst, or copies of one shot, are one moment), is a person. Then each
face of lower quality (small, blurred, turned away) joins the person
whose faces it is most like on average, when that average is at least
"match" too (more for a person of only a few faces: SMALL_PERSON), and
that person has no other face in its picture. config.json's "people" can
change the numbers (SETTINGS.md; README, "People", says what to expect
of them).

A person keeps its id from one grouping to the next: a new group takes
the id of the person most of its faces belonged to (each person at most
once), so the names, the hidden people and the covers stay. A person is
shown as its name, or "Person <id>" while it has none.

What the owner says is kept and followed by every later grouping:

    a name             (rename) unique, compared without regard to case
    merge              every face of one person goes to another (both
                       people's faces are then the owner's, but for those
                       of lower quality that joined by themselves: see
                       below)
    move               faces go to another person, or to a new one (a
                       split); each is the owner's there, and "not" the
                       person it came from
    not this person    (reject) the face leaves the person and never goes
                       back to it (nor joins faces that belong to it)
    not a face         (mark_not_face) the face is left out of everything
    hide               the person is not listed (nor offered in searches),
                       its faces stay with it
    cover              the face that stands for the person

A face the owner put in a person (owner 1 in face_people) stays there
whatever the vectors say, and no grouping puts the faces of two people the
owner put faces in together.

    people        id, name ('' while unnamed), hidden, cover (a face id the
                  owner chose, or NULL: the best face), created, updated
    face_people   face_id, person_id, owner (1: the owner's choice), updated
    face_rejects  face_id, person_id, updated ("not this person")
    face_marks    face_id, not_face, updated ("not a face")
    meta          people_version (raised by every change of the people),
                  grouped (the faces' generation when they were last
                  grouped), groups_recipe (the settings they were grouped
                  with), grouped_at

All of it lives in data/faces.db (people.py), with the faces: it is never
exported, never sent anywhere (but for the names of the people of a trip, to
the model writing its story when that runs on this computer or the local
network: trip_stories.py), and Delete face data deletes it. The search
page (person:"Name"), the viewer's pane (the names) and the People page
get it from the server (search_data, picture_view, list_people): the admin
always, others (LAN visitors) only with "visitors": true in config.json's
"people", and then only the named people that are not hidden, in the
pictures they may see. An export carries the names only with its
"people_names" option, as tags (export_names).

This module uses only the standard library (numpy, when this Python has
it, only to compare many faces faster); Pillow cuts out the faces
(crop).
"""

import collections
import datetime
import heapq
import io
import json
import math
import operator
import re
import sqlite3
import struct
import time
import unicodedata

import people

try:
    _sumprod = math.sumprod             # (Python 3.12 and later)
except AttributeError:                  # pragma: no cover - older Pythons
    _mul = operator.mul

    def _sumprod(first, second):
        return sum(map(_mul, first, second))


# Raise when faces are grouped differently: every picture's faces are
# grouped again by the next run. (2: never two faces of one picture in one
# person; moments; SMALL_PERSON.)
GROUPS_VERSION = 2
# Pairs of faces less alike than match minus this are taken as not alike
# at all (0) by the average linkage: few pairs are kept, and the averages
# barely change (two people's faces have a cosine of about 0.1).
PAIR_MARGIN = 0.25
# A new group becomes a person only when the median length of its faces'
# vectors before they were normalised (people.py's norm) is at least this:
# many groups of things that are not faces (wheels, sweets, teddy bears, a
# dog's face) have short vectors (about 6 to 7.5, against 9 to 13 for clear
# faces of people). Not all: statues, toys and prints with faces of their
# own, and portraits on banknotes, have vectors as long as people's, and
# become people ("None are faces" on the People page clears them). A face
# of lower quality joins a person only when its own vector is at least
# REST_NORM long (shorter ones, things and faces seen from the side or half
# hidden, are too unsure).
PERSON_NORM = 8.0
REST_NORM = 7.0
# A new group's faces must show it at MIN_MOMENTS different moments at least
# (or "min_faces", when that is fewer): a burst, or copies of one shot, is
# no person on its own. Two pictures show the same moment (the frames of a
# burst, copies of one shot) when their dHashes (index.db) are at most
# SIGHTING_BITS apart, or both were taken (by a clock to trust) at most
# SIGHTING_SECONDS apart. (Three moments would also leave out a few more
# things -- statues, drawings -- but more people seen on two days, or
# whose clear faces are of two moments.)
SIGHTING_BITS = 10
SIGHTING_SECONDS = 2
MIN_MOMENTS = 2
# (search_index's: a dHash with fewer set bits than this, or more than 64 minus
# this, is of a picture with little detail, and says little about it.)
LOW_DETAIL_BITS = 12
TRUSTED_DATES = ("exif", "filename", "video")
# A face of lower quality joins a person with fewer faces than SMALL_PERSON in
# its group only when it is at least SMALL_MARGIN more alike than "match":
# the average over a few faces is unsure.
SMALL_PERSON = 6
SMALL_MARGIN = 0.1
NAME_LENGTH = 100
FACE_LIMIT = 10000          # faces one request may name
ID_CHUNK = 500
SIMILAR_PEOPLE = 6          # people offered as "may be the same person"
SIMILAR_BELOW = 0.1         # ... down to match minus this (their faces on average)
CROP_EDGE = 160             # pixels of a face's picture (square)
CROP_MARGIN = 0.45          # room around the face's box, as a part of its size
OTHER_FACES_LIMIT = 300     # faces one page of "other faces" lists
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")


class GroupError(ValueError):
    """A change that cannot be made: a message, {field: problem}, an HTTP status and
    more for the answer (extra)."""

    def __init__(self, message, fields=None, status=400, **extra):
        ValueError.__init__(self, message)
        self.fields = dict(fields or {})
        self.status = status
        self.extra = extra


def label(person_id, name):
    """What a person is called: its name, or "Person <id>"."""
    return name or "Person %d" % person_id


def recipe(options):
    """The settings the faces are grouped with (a change groups them all again)."""
    return ("v%d match-%g quality-%g faces-%d margin-%g norm-%g/%g moments-%d/%d/%d "
            "small-%d/%g" % (
                GROUPS_VERSION, options["match"], options["group_quality"],
                options["min_faces"], PAIR_MARGIN, PERSON_NORM, REST_NORM, MIN_MOMENTS,
                SIGHTING_BITS, SIGHTING_SECONDS, SMALL_PERSON, SMALL_MARGIN))


# ---------------------------------------------------------------------------
# Grouping (plain functions: the tests feed them made-up vectors)

def _numpy():
    """numpy when this Python has it (only to compare many faces faster), else None."""
    try:
        import numpy
    except ImportError:
        return None
    return numpy


NUMPY_FROM = 400            # faces from which numpy (when there) compares them
NUMPY_BLOCK = 1024          # rows compared at a time with numpy


def similar_pairs(vectors, floor, use_numpy=None):
    """[(i, j, cosine)] for every pair i < j of vectors (unit length, as tuples of
    floats) whose cosine is at least floor, in order of i, then j. numpy is used
    when there are many and it can be imported (use_numpy True or False forces
    either way); both give the same pairs (but for rounding in the last
    digits)."""
    count = len(vectors)
    numpy = None
    if use_numpy is None:
        use_numpy = count >= NUMPY_FROM
    if use_numpy:
        numpy = _numpy()
    if numpy is not None and count:
        matrix = numpy.asarray(vectors, dtype=numpy.float64)
        pairs = []
        for start in range(0, count, NUMPY_BLOCK):
            block = matrix[start:start + NUMPY_BLOCK] @ matrix.T
            rows, cols = numpy.nonzero(block >= floor)
            for row, col in zip(rows.tolist(), cols.tolist()):
                i = start + row
                if col > i:
                    pairs.append((i, col, float(block[row, col])))
        return pairs
    pairs = []
    for i in range(count):
        first = vectors[i]
        for j in range(i + 1, count):
            value = _sumprod(first, vectors[j])
            if value >= floor:
                pairs.append((i, j, value))
    return pairs


def average_linkage(count, pairs, threshold, start=(), allowed=None):
    """Group the items 0..count-1 by average linkage.

    pairs are [(i, j, similarity)]; a pair not listed counts as 0. The two
    groups whose items are the most similar on average (the sum of the
    similarities of every pair across them, over the number of such pairs)
    become one, again and again, while that average is at least threshold.
    start lists groups the items begin in (lists of items; the rest begin
    alone). allowed(first, second), when given, is asked before two groups
    become one (each a list of items); False keeps them apart. Ties go to
    the groups with the lowest numbers, so the result never depends on
    anything but the input. Returns the groups: lists of items, each in
    order, sorted by their first item.
    """
    members = {}
    owner = [None] * count
    number = 0
    for group in start:
        group = sorted(set(group))
        if not group:
            continue
        members[number] = group
        for item in group:
            owner[item] = number
        number += 1
    for item in range(count):
        if owner[item] is None:
            members[number] = [item]
            owner[item] = number
            number += 1
    links = {key: {} for key in members}
    for i, j, value in pairs:
        a, b = owner[i], owner[j]
        if a == b:
            continue
        links[a][b] = links[a].get(b, 0.0) + value
        links[b][a] = links[b].get(a, 0.0) + value
    heap = []
    for a, others in links.items():
        size = len(members[a])
        for b, total in others.items():
            if a < b:
                average = total / (size * len(members[b]))
                if average >= threshold:
                    heap.append((-average, a, b))
    heapq.heapify(heap)
    while heap:
        _negative, a, b = heapq.heappop(heap)
        if a not in members or b not in members:
            continue                    # (one of them is in another group by now)
        if allowed is not None and not allowed(members[a], members[b]):
            continue
        merged = sorted(members.pop(a) + members.pop(b))
        new = number
        number += 1
        members[new] = merged
        joined = {}
        for old in (a, b):
            for other, total in links.pop(old).items():
                if other in (a, b):
                    continue
                joined[other] = joined.get(other, 0.0) + total
                del links[other][old]
        links[new] = joined
        size = len(merged)
        for other in sorted(joined):
            total = joined[other]
            links[other][new] = total
            average = total / (size * len(members[other]))
            if average >= threshold:
                heapq.heappush(heap, (-average, other, new))
    return sorted(members.values(), key=lambda group: group[0])


def _add(total, vector):
    for place, value in enumerate(vector):
        total[place] += value


def _median(values):
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0


_MOMENT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")


def _seconds(text):
    """Seconds since a fixed day of a date taken "YYYY-MM-DDTHH:MM[:SS]", or None."""
    match = _MOMENT_RE.match(text or "")
    if not match:
        return None
    try:
        day = datetime.date(int(match.group(1)), int(match.group(2)),
                            int(match.group(3))).toordinal()
    except ValueError:
        return None
    hours, minutes, seconds = int(match.group(4)), int(match.group(5)), int(match.group(6) or 0)
    if hours > 23 or minutes > 59 or seconds > 60:
        return None
    return day * 86400 + hours * 3600 + minutes * 60 + seconds


def _dhash_value(text):
    """A dHash (16 hex digits) as a number, or None (none, or of a picture with little
    detail: LOW_DETAIL_BITS)."""
    if not isinstance(text, str) or len(text) != 16:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    bits = bin(value).count("1")
    return value if LOW_DETAIL_BITS <= bits <= 64 - LOW_DETAIL_BITS else None


def picture_moments(conn, keys=None):
    """{(content hash, kind): (dHash as a number or None, seconds taken or None)} of the
    present pictures of index.db (the first row of each content and kind; the time only
    from a clock to trust), which tell sightings apart (sightings()). keys: only those."""
    wanted = None if keys is None else set(keys)
    moments = {}
    sql = ("SELECT content_hash, media, projection, has_preview, dhash, taken_at, taken_source "
           "FROM images WHERE missing_since IS NULL AND content_hash != ''")
    if wanted is None:
        batches = [(sql + " ORDER BY id", [])]
    else:
        hashes = sorted({key[0] for key in wanted})
        batches = []
        for start in range(0, len(hashes), ID_CHUNK):
            part = hashes[start:start + ID_CHUNK]
            batches.append((sql + " AND content_hash IN (%s) ORDER BY id"
                            % ",".join("?" * len(part)), part))
    for statement, values in batches:
        for row in conn.execute(statement, values):
            key = (row["content_hash"], people.looked_at(row))
            if key[1] is None or key in moments or (wanted is not None and key not in wanted):
                continue
            moments[key] = (_dhash_value(row["dhash"]),
                            _seconds(row["taken_at"]) if row["taken_source"] in TRUSTED_DATES
                            else None)
    return moments


def _same_moment(first, second):
    if (first[0] is not None and second[0] is not None
            and bin(first[0] ^ second[0]).count("1") <= SIGHTING_BITS):
        return True
    return (first[1] is not None and second[1] is not None
            and abs(first[1] - second[1]) <= SIGHTING_SECONDS)


def moment_sets(keys, moments):
    """{picture: the number of its sighting} of keys (pictures: (content hash, kind)):
    pictures of one moment (see SIGHTING_BITS; also through others, as the frames of
    a burst are) share a number, numbered 0, 1, ... in the order of keys. Without
    moments (None) every picture is a sighting of its own."""
    keys = list(collections.OrderedDict.fromkeys(keys))
    if moments is None:
        return {key: number for number, key in enumerate(keys)}
    parent = list(range(len(keys)))

    def root(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item
    values = [moments.get(key, (None, None)) for key in keys]
    for i in range(len(keys)):
        if values[i] == (None, None):
            continue
        for j in range(i + 1, len(keys)):
            if _same_moment(values[i], values[j]):
                first, second = root(i), root(j)
                if first != second:
                    parent[max(first, second)] = min(first, second)
    numbers = {}
    return {key: numbers.setdefault(root(index), len(numbers))
            for index, key in enumerate(keys)}


def sightings(keys, moments):
    """How many different moments the pictures keys show (see moment_sets)."""
    return len(set(moment_sets(keys, moments).values()))


def plan(faces, previous, rejected, persons, options, use_numpy=None, moments=None):
    """Who each face is (the grouping, without writing anything).

    faces are [(face id, quality, vector, norm, picture)] of the faces that
    may be grouped (those marked "not a face" left out), picture the (content
    hash, kind) the face is in (a face without one is in a picture of its
    own); previous {face id: (person id, owner)} where they are now; rejected
    {face id: {person ids}} ("not this person"); persons {person id: {"name",
    "hidden"}}; options people's settings (match, group_quality, min_faces);
    moments picture_moments() (None: every picture shows a moment of its
    own). Returns {"assign": {face id: person}, "new": [new person keys],
    "grouped": faces in groups, "groups": groups of min_faces or more}; a
    person is an existing person's id, or a new one's key (a negative number:
    -1 for the largest new group, -2 for the next, ...).
    """
    match = options["match"]
    by_id = {face[0]: (face[1], face[2], face[3]) for face in faces}
    picture_of = {face[0]: face[4] if len(face) > 4 and face[4] is not None else ("", face[0])
                  for face in faces}
    pinned = {face_id: place[0] for face_id, place in previous.items()
              if place[1] and face_id in by_id}
    core = [face[0] for face in faces
            if face[1] >= options["group_quality"] or face[0] in pinned]
    rest = [face[0] for face in faces if face[1] < options["group_quality"]
            and face[0] not in pinned and face[3] >= REST_NORM]
    vectors = [by_id[face_id][1] for face_id in core]
    start = collections.OrderedDict()
    for index, face_id in enumerate(core):
        if face_id in pinned:
            start.setdefault(pinned[face_id], []).append(index)
    pairs = similar_pairs(vectors, match - PAIR_MARGIN, use_numpy)
    # What a face says against joining another: the picture it is in (two faces of
    # one picture are two people), the person the owner put it in, the person it
    # belongs to now, the people it is not.
    picture_at = [picture_of[face_id] for face_id in core]
    pin_of = [pinned.get(face_id) for face_id in core]
    now_of = [previous[face_id][0] if face_id in previous else None for face_id in core]
    not_of = [rejected.get(face_id) or () for face_id in core]

    def allowed(first, second):
        seen = {picture_at[item] for item in first}
        if any(picture_at[item] in seen for item in second):
            return False
        if not (pinned or rejected):
            return True
        for one, two in ((first, second), (second, first)):
            # (Faces the owner put in a person never take in faces another person
            # has now, nor the owner's of another person: a merge does that.)
            pins = {pin_of[item] for item in one} - {None}
            if pins and any((pin_of[item] or now_of[item]) not in pins | {None}
                            for item in two):
                return False
            refused = set()
            for item in one:
                refused.update(not_of[item])
            if refused and any(now_of[item] in refused or pin_of[item] in refused
                               for item in two):
                return False
        return True
    groups = average_linkage(len(core), pairs, match, start=list(start.values()),
                             allowed=allowed)
    groups.sort(key=lambda group: (-len(group), group[0]))
    counted = {}

    def seen_at(number):
        """How many different moments the faces of group number show."""
        if number not in counted:
            counted[number] = sightings([picture_at[item] for item in groups[number]], moments)
        return counted[number]

    def enough(number):
        return (len(groups[number]) >= options["min_faces"]
                and seen_at(number) >= min(options["min_faces"], MIN_MOMENTS))
    # Each group's person: the one the owner put faces of it in, else by the votes
    # of its faces (where they are now), each person given to one group at most
    # that way (a person is its groups with the owner's faces, and that one).
    votes = []
    forced = {}
    for number, group in enumerate(groups):
        voters = collections.Counter()
        for item in group:
            face_id = core[item]
            if face_id in pinned:
                forced[number] = pinned[face_id]
            elif face_id in previous:
                voters[previous[face_id][0]] += 1
        for person_id, count in voters.items():
            votes.append((-count, person_id, number))
    chosen = dict(forced)
    owned = set(forced.values())
    used = set()
    votes.sort()
    for _count, person_id, number in votes:
        if number in chosen or person_id in used or person_id not in persons:
            continue
        info = persons[person_id]
        if not (info.get("name") or info.get("hidden") or person_id in owned or enough(number)):
            continue                    # (a few faces left of an unnamed person: gone)
        chosen[number] = person_id
        used.add(person_id)
    new = []
    for number, group in enumerate(groups):
        if (number not in chosen and len(group) >= options["min_faces"]
                and _median([by_id[core[item]][2] for item in group]) >= PERSON_NORM
                and enough(number)):
            new.append(-(len(new) + 1))
            chosen[number] = new[-1]
    assign = {}
    sums = {}
    for number, person in chosen.items():
        total = [0.0] * len(vectors[0]) if vectors else []
        for item in groups[number]:
            assign[core[item]] = person
            _add(total, vectors[item])
        sums[person] = (total, len(groups[number]))
    grouped = len(assign)
    # The faces of lower quality join the people whose faces they are most like (the
    # most alike first), one face of a picture per person.
    if rest and sums:
        people_list = sorted(sums)
        numpy = _numpy() if use_numpy is not False else None
        if numpy is not None and len(rest) * len(people_list) >= NUMPY_FROM:
            means = numpy.asarray([[value / sums[person][1] for value in sums[person][0]]
                                   for person in people_list], dtype=numpy.float64)
            scores = numpy.asarray([by_id[face_id][1] for face_id in rest],
                                   dtype=numpy.float64) @ means.T
            rows = scores.tolist()
        else:
            means = [[value / sums[person][1] for value in sums[person][0]]
                     for person in people_list]
            rows = [[_sumprod(by_id[face_id][1], mean) for mean in means] for face_id in rest]
        bar = [match + (SMALL_MARGIN if sums[person][1] < SMALL_PERSON else 0.0)
               for person in people_list]
        offers = []
        for face_id, row in zip(rest, rows):
            refused = rejected.get(face_id) or ()
            for place, value in enumerate(row):
                if value >= bar[place] and people_list[place] not in refused:
                    offers.append((-value, face_id, people_list[place]))
        offers.sort()
        held = collections.defaultdict(set)
        for face_id, person in assign.items():
            held[person].add(picture_of[face_id])
        for _value, face_id, person in offers:
            if face_id in assign or picture_of[face_id] in held[person]:
                continue
            assign[face_id] = person
            held[person].add(picture_of[face_id])
    # (Never a person a face is not; a pinned face stays where the owner put it.)
    for face_id in list(assign):
        if face_id in pinned:
            assign[face_id] = pinned[face_id]
        elif assign[face_id] in (rejected.get(face_id) or ()):
            del assign[face_id]
    return {"assign": assign, "new": new, "grouped": grouped,
            "groups": sum(1 for group in groups if len(group) >= options["min_faces"])}


# ---------------------------------------------------------------------------
# The store

def _meta_value(store, key, default=""):
    row = store.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None and row[0] is not None else default


def _meta_number(store, key):
    try:
        return int(_meta_value(store, key, "0") or 0)
    except ValueError:
        return 0


def _set_meta(store, key, value):
    store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def _changed(store):
    """Count a change of the people (no commit)."""
    _set_meta(store, "people_version", _meta_number(store, "people_version") + 1)


def people_version(store):
    """A number raised by every change of the people (their names, faces, ...)."""
    return _meta_number(store, "people_version")


def grouping_due(store, options):
    """True when the faces changed (found, taken out of a person, or put back among the
    faces) or the settings did, since they were last grouped."""
    if store.execute("SELECT 1 FROM faces LIMIT 1").fetchone() is None:
        return False
    return (_meta_value(store, "grouped") != str(_meta_number(store, "generation"))
            or _meta_value(store, "groups_recipe") != recipe(options))


def _vector(blob):
    return struct.unpack("<%df" % people.EMBEDDING_DIMS, bytes(blob))


def _load(store):
    """(faces [(id, quality, vector, norm, (content hash, kind))] that may be grouped,
    previous {face id: (person id, owner)}, rejected {face id: {person ids}}, persons
    {id: {name, hidden}})."""
    marked = {row[0] for row in store.execute("SELECT face_id FROM face_marks WHERE not_face")}
    faces = [(row[0], row[1], _vector(row[2]), row[3], (row[4], row[5])) for row in store.execute(
        "SELECT id, quality, embedding, norm, content_hash, kind FROM faces ORDER BY id")
        if row[0] not in marked]
    previous = {row[0]: (row[1], row[2]) for row in store.execute(
        "SELECT face_id, person_id, owner FROM face_people")}
    rejected = collections.defaultdict(set)
    for face_id, person_id in store.execute("SELECT face_id, person_id FROM face_rejects"):
        rejected[face_id].add(person_id)
    persons = {row[0]: {"name": row[1], "hidden": bool(row[2])} for row in store.execute(
        "SELECT id, name, hidden FROM people")}
    return faces, previous, dict(rejected), persons


def _drop_empty(store, keep=()):
    """Delete the people without a face but those in keep (no commit): named and hidden
    ones too, since a person without a face can never be shown (nor its name taken away)
    again."""
    gone = [row[0] for row in store.execute(
        "SELECT id FROM people WHERE id NOT IN (SELECT DISTINCT person_id FROM face_people)")
        if row[0] not in keep]
    for start in range(0, len(gone), ID_CHUNK):
        part = gone[start:start + ID_CHUNK]
        store.execute("DELETE FROM people WHERE id IN (%s)" % ",".join("?" * len(part)), part)
    # (A cover that left its person stands for it no more.)
    store.execute("UPDATE people SET cover = NULL WHERE cover IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM face_people WHERE face_id = people.cover AND "
                  "person_id = people.id)")
    return len(gone)


GROUP_TRIES = 3


def group(store, options, ctx=None, now=None, use_numpy=None, conn=None):
    """Group the faces into people again (see the module notes) and write it.

    store is data/faces.db (people.connect()); options people.settings();
    conn index.db, whose pictures tell the moments apart (picture_moments;
    without it, every picture is a moment of its own). A change the owner
    makes meanwhile is never lost: the grouping is made again when the
    people changed while it was worked out. Returns {"faces" (that could be
    grouped), "grouped" (in a person), "people", "new", "removed",
    "seconds"}.
    """
    started = time.monotonic()
    now = time.time() if now is None else now
    if ctx is not None:
        ctx.stage("people")
        ctx.log("Grouping the faces into people...")
    for attempt in range(GROUP_TRIES):
        version = people_version(store)
        generation = _meta_number(store, "generation")
        faces, previous, rejected, persons = _load(store)
        moments = (picture_moments(conn, {face[4] for face in faces})
                   if conn is not None else None)
        result = plan(faces, previous, rejected, persons, options, use_numpy=use_numpy,
                      moments=moments)
        store.execute("BEGIN IMMEDIATE")
        try:
            if people_version(store) != version and attempt < GROUP_TRIES - 1:
                store.rollback()        # (the owner changed something: again)
                continue
            new_ids = {}
            for key in result["new"]:
                cursor = store.execute("INSERT INTO people (name, hidden, cover, created, "
                                       "updated) VALUES ('', 0, NULL, ?, ?)", (now, now))
                new_ids[key] = cursor.lastrowid
            store.execute("DELETE FROM face_people WHERE owner = 0")
            present = {face[0] for face in faces}
            rows = []
            for face_id, person in sorted(result["assign"].items()):
                place = previous.get(face_id)
                if place is not None and place[1]:
                    continue            # (the owner's: left as it is)
                if face_id in present:
                    rows.append((face_id, new_ids.get(person, person), now))
            store.executemany("INSERT OR REPLACE INTO face_people (face_id, person_id, owner, "
                              "updated) VALUES (?, ?, 0, ?)", rows)
            removed = _drop_empty(store)
            _set_meta(store, "grouped", generation)
            _set_meta(store, "groups_recipe", recipe(options))
            _set_meta(store, "grouped_at", now)
            _changed(store)
            store.commit()
        except BaseException:
            store.rollback()
            raise
        break
    count = store.execute("SELECT COUNT(*) FROM people WHERE id IN (SELECT person_id FROM "
                          "face_people)").fetchone()[0]
    summary = {"faces": len(faces), "grouped": len(result["assign"]), "people": count,
               "new": len(result["new"]), "removed": removed,
               "seconds": round(time.monotonic() - started, 3)}
    if ctx is not None:
        ctx.log("Grouped %d of %d face(s) into %d people (%d new) in %.1f s." % (
            summary["grouped"], summary["faces"], summary["people"], summary["new"],
            summary["seconds"]))
    return summary


def group_if_due(store, options, ctx=None, force=False, conn=None):
    """group() when the faces changed (or the settings did) since the last grouping, or
    force; else None. conn: index.db (see group)."""
    if not force and not grouping_due(store, options):
        return None
    return group(store, options, ctx, conn=conn)


# ---------------------------------------------------------------------------
# What the owner says

def clean_name(value):
    """A person's name as sent: stripped, at most NAME_LENGTH characters ('' unnames it);
    raises GroupError."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise GroupError("A name is text.", {"name": "Text."})
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if len(name) > NAME_LENGTH:
        raise GroupError("A name can have at most %d characters." % NAME_LENGTH,
                         {"name": "At most %d characters." % NAME_LENGTH})
    if _CONTROL_RE.search(name):
        raise GroupError("A name cannot hold control characters.",
                         {"name": "No control characters."})
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise GroupError("The name has a character that cannot be stored.",
                         {"name": "A character that cannot be stored."})
    if re.match(r"(?i)^person\s+\d+$", name):
        raise GroupError("\"Person <number>\" is how people without a name are shown: choose "
                         "another name.", {"name": "Another name."})
    return name


def name_key(name):
    """A name as names are compared (without regard to case)."""
    return unicodedata.normalize("NFKC", name).casefold()


def _person(store, person_id):
    if isinstance(person_id, bool) or not isinstance(person_id, int):
        raise GroupError("A person is named by its id (a number).", {"person": "A number."})
    row = store.execute("SELECT id, name, hidden, cover FROM people WHERE id = ?",
                        (person_id,)).fetchone()
    if row is None:
        raise GroupError("There is no person %s (any more)." % person_id, status=404)
    return row


def _face_ids(value):
    if (not isinstance(value, list) or not value or len(value) > FACE_LIMIT
            or not all(isinstance(item, int) and not isinstance(item, bool) and item > 0
                       for item in value)):
        raise GroupError("Send the faces as a list of 1 to %d face ids." % FACE_LIMIT,
                         {"faces": "A list of face ids."})
    return sorted(set(value))


def _existing_faces(store, face_ids):
    found = set()
    for start in range(0, len(face_ids), ID_CHUNK):
        part = face_ids[start:start + ID_CHUNK]
        found.update(row[0] for row in store.execute(
            "SELECT id FROM faces WHERE id IN (%s)" % ",".join("?" * len(part)), part))
    return [face_id for face_id in face_ids if face_id in found]


def _clash(store, name, person_id):
    if not name:
        return None
    wanted = name_key(name)
    # (Only people with a face count: one without could be neither seen nor merged.)
    for row in store.execute("SELECT id, name FROM people WHERE name != '' AND id != ? AND "
                             "id IN (SELECT person_id FROM face_people)", (person_id,)):
        if name_key(row[1]) == wanted:
            return row
    return None


def update_person(store, person_id, changes, now=None):
    """{name?, hidden?, cover?}: name a person ('' takes the name away), hide it (or not),
    or choose the face that stands for it (null: the best one). A name another person
    has is refused (409, with "other": its id: merge them instead). Returns
    person_info()."""
    now = time.time() if now is None else now
    if not isinstance(changes, dict) or not set(changes) & {"name", "hidden", "cover"}:
        raise GroupError("Send name, hidden or cover.", {"name": "Send one."})
    with store:
        row = _person(store, person_id)
        sets, values = [], []
        if "name" in changes:
            name = clean_name(changes["name"])
            other = _clash(store, name, person_id)
            if other is not None:
                raise GroupError("%s is the name of another person already: merge the two "
                                 "if they are the same person." % other[1],
                                 {"name": "Taken."}, status=409, other=other[0])
            sets.append("name = ?")
            values.append(name)
        if "hidden" in changes:
            if not isinstance(changes["hidden"], bool):
                raise GroupError("hidden is true or false.", {"hidden": "true or false."})
            sets.append("hidden = ?")
            values.append(1 if changes["hidden"] else 0)
        if "cover" in changes:
            cover = changes["cover"]
            if cover is not None:
                if isinstance(cover, bool) or not isinstance(cover, int) or store.execute(
                        "SELECT 1 FROM face_people WHERE face_id = ? AND person_id = ?",
                        (cover, row["id"])).fetchone() is None:
                    raise GroupError("The cover must be one of the person's faces.",
                                     {"cover": "One of its faces."})
            sets.append("cover = ?")
            values.append(cover)
        store.execute("UPDATE people SET %s, updated = ? WHERE id = ?" % ", ".join(sets),
                      values + [now, row["id"]])
        _changed(store)
    return person_info(store, person_id)


def merge(store, source_id, target_id, now=None, group_quality=None):
    """Every face of person source goes to person target, which keeps its name (or takes
    source's when it has none); source is deleted. The faces of both are the owner's
    from then on (a later grouping keeps them together), but for the faces of a
    quality below group_quality (people's setting; None: none) that joined one of them
    by themselves: a later grouping places those by their vectors again, so a face
    that was wrong there is not kept for good. Returns person_info(target)."""
    now = time.time() if now is None else now
    if source_id == target_id:
        raise GroupError("A person cannot be merged with itself.", {"into": "Another person."})
    with store:
        source = _person(store, source_id)
        target = _person(store, target_id)
        store.execute("UPDATE face_people SET person_id = ?, owner = CASE WHEN owner = 1 OR ? "
                      "IS NULL OR face_id IN (SELECT id FROM faces WHERE quality >= ?) THEN 1 "
                      "ELSE 0 END, updated = ? WHERE person_id IN (?, ?)",
                      (target["id"], group_quality, group_quality, now, source["id"],
                       target["id"]))
        # (Faces that were "not" source are "not" target now; the merged faces are
        # target's whatever they were said to be.)
        store.execute("UPDATE OR IGNORE face_rejects SET person_id = ? WHERE person_id = ?",
                      (target["id"], source["id"]))
        store.execute("DELETE FROM face_rejects WHERE person_id = ? AND face_id IN (SELECT "
                      "face_id FROM face_people WHERE person_id = ?)",
                      (target["id"], target["id"]))
        if not target["name"] and source["name"]:
            store.execute("UPDATE people SET name = ? WHERE id = ?",
                          (source["name"], target["id"]))
        store.execute("DELETE FROM people WHERE id = ?", (source["id"],))
        store.execute("UPDATE people SET updated = ? WHERE id = ?", (now, target["id"]))
        _drop_empty(store)
        _changed(store)
    return person_info(store, target_id)


def move_faces(store, face_ids, target_id=None, name="", now=None):
    """Faces go to person target_id, or (None) to a new person called name ('' : none).
    Each is the owner's there (a later grouping keeps it there), and is "not" the
    person it came from (so the faces left there do not join it again). Returns
    person_info() of the person they went to."""
    now = time.time() if now is None else now
    face_ids = _face_ids(face_ids)
    name = clean_name(name)
    with store:
        face_ids = _existing_faces(store, face_ids)
        if not face_ids:
            raise GroupError("Those faces are not there (any more).", status=404)
        if target_id is None:
            other = _clash(store, name, -1)
            if other is not None:
                raise GroupError("%s is the name of another person already: move the faces "
                                 "to that person." % other[1], {"name": "Taken."},
                                 status=409, other=other[0])
            target_id = store.execute("INSERT INTO people (name, hidden, cover, created, "
                                      "updated) VALUES (?, 0, NULL, ?, ?)",
                                      (name, now, now)).lastrowid
        else:
            target_id = _person(store, target_id)["id"]
        for start in range(0, len(face_ids), ID_CHUNK):
            part = face_ids[start:start + ID_CHUNK]
            marks = ",".join("?" * len(part))
            for face_id, person_id in store.execute(
                    "SELECT face_id, person_id FROM face_people WHERE face_id IN (%s)"
                    % marks, part).fetchall():
                if person_id != target_id:
                    store.execute("INSERT OR REPLACE INTO face_rejects (face_id, person_id, "
                                  "updated) VALUES (?, ?, ?)", (face_id, person_id, now))
            store.execute("DELETE FROM face_rejects WHERE person_id = ? AND face_id IN (%s)"
                          % marks, [target_id] + part)
            store.execute("DELETE FROM face_marks WHERE face_id IN (%s)" % marks, part)
            store.executemany("INSERT OR REPLACE INTO face_people (face_id, person_id, owner, "
                              "updated) VALUES (?, ?, 1, ?)",
                              [(face_id, target_id, now) for face_id in part])
        store.execute("UPDATE people SET updated = ? WHERE id = ?", (now, target_id))
        _drop_empty(store, keep=(target_id,))
        _changed(store)
    return person_info(store, target_id)


def reject_faces(store, face_ids, person_id, now=None):
    """"Not this person": the faces leave person_id and never join it again (the next
    grouping may put them in another person: it is due). Returns {"removed": faces that
    left it, "person": person_info() or None when it is gone}."""
    now = time.time() if now is None else now
    face_ids = _face_ids(face_ids)
    with store:
        row = _person(store, person_id)
        removed = 0
        for start in range(0, len(face_ids), ID_CHUNK):
            part = _existing_faces(store, face_ids[start:start + ID_CHUNK])
            if not part:
                continue
            marks = ",".join("?" * len(part))
            removed += store.execute("DELETE FROM face_people WHERE person_id = ? AND face_id "
                                     "IN (%s)" % marks, [row["id"]] + part).rowcount
            store.executemany("INSERT OR REPLACE INTO face_rejects (face_id, person_id, "
                              "updated) VALUES (?, ?, ?)",
                              [(face_id, row["id"], now) for face_id in part])
        if removed:
            people._bump(store)         # (the faces changed: grouping is due)
        _drop_empty(store)
        _changed(store)
    found = store.execute("SELECT 1 FROM people WHERE id = ?", (row["id"],)).fetchone()
    return {"removed": removed, "person": person_info(store, row["id"]) if found else None}


def mark_not_face(store, face_ids, not_face=True, now=None):
    """"Not a face" (or, not_face False, a face after all: the next grouping places it,
    and is due): marked faces leave their person and are left out of everything. Returns
    how many faces changed."""
    now = time.time() if now is None else now
    face_ids = _face_ids(face_ids)
    changed = 0
    with store:
        for start in range(0, len(face_ids), ID_CHUNK):
            part = _existing_faces(store, face_ids[start:start + ID_CHUNK])
            if not part:
                continue
            marks = ",".join("?" * len(part))
            if not_face:
                store.execute("DELETE FROM face_people WHERE face_id IN (%s)" % marks, part)
                before = store.execute("SELECT COUNT(*) FROM face_marks WHERE face_id IN (%s)"
                                       % marks, part).fetchone()[0]
                store.executemany("INSERT OR REPLACE INTO face_marks (face_id, not_face, "
                                  "updated) VALUES (?, 1, ?)",
                                  [(face_id, now) for face_id in part])
                changed += len(part) - before
            else:
                changed += store.execute("DELETE FROM face_marks WHERE face_id IN (%s)"
                                         % marks, part).rowcount
        if changed and not not_face:
            people._bump(store)         # (faces to group again: grouping is due)
        _drop_empty(store)
        _changed(store)
    return changed


# ---------------------------------------------------------------------------
# Pictures of the faces (index.db)

def _visible_pictures(conn, library_ids, include_hidden, videos=None):
    """{(content hash, kind): [image ids]} (one query: hidden pictures left out unless
    include_hidden). videos, a set, gets the ids of the videos among them."""
    library_ids = list(library_ids)
    found = collections.defaultdict(list)
    for start in range(0, len(library_ids), ID_CHUNK):
        part = library_ids[start:start + ID_CHUNK]
        for row in conn.execute(
                "SELECT id, content_hash, media, projection, has_preview, hidden FROM images "
                "WHERE missing_since IS NULL AND content_hash != '' AND library_id IN (%s)%s "
                "ORDER BY id" % (",".join("?" * len(part)), "" if include_hidden
                                 else " AND hidden = 0"), part):
            kind = people.looked_at(row)
            if kind is not None:
                found[(row["content_hash"], kind)].append(row["id"])
                if videos is not None and people.is_video(row):
                    videos.add(row["id"])
    return found


def _faces_of_people(store, person_ids=None):
    """[(face id, person id, content hash, kind, quality, owner)] of the faces in people."""
    sql = ("SELECT f.id, fp.person_id, f.content_hash, f.kind, f.quality, fp.owner FROM "
           "face_people fp JOIN faces f ON f.id = fp.face_id")
    if person_ids is None:
        return store.execute(sql + " ORDER BY fp.person_id, f.quality DESC, f.id").fetchall()
    rows = []
    person_ids = list(person_ids)
    for start in range(0, len(person_ids), ID_CHUNK):
        part = person_ids[start:start + ID_CHUNK]
        rows.extend(store.execute(sql + " WHERE fp.person_id IN (%s) ORDER BY fp.person_id, "
                                  "f.quality DESC, f.id" % ",".join("?" * len(part)), part))
    return rows


# ---------------------------------------------------------------------------
# What the pages are told

def may_see(config, admin):
    """True when the requester may see people at all: the admin, or LAN visitors while
    config.json's "people" has both "enabled" and "visitors" true."""
    if admin:
        return True
    options = people.settings(config)
    return options["enabled"] and options["visitors"]


def search_data(store, conn, config, library_ids, admin, include_hidden_people=False):
    """The /api/people answer: {"version", "people": [{"id", "name", "label", "cover"
    (a face id), "faces", "ids" (the pictures, in id order), "videos" (how many of
    them are videos), "hidden" (only with include_hidden_people)}]}, by name, then the
    unnamed by how many pictures.

    The admin gets every person (hidden ones too with include_hidden_people) in
    every present picture of the libraries; others only the named people that
    are not hidden, in the pictures that are not hidden. store may be None (no
    face data): no people."""
    answer = {"version": 1, "people": []}
    if store is None:
        return answer
    video_ids = set()
    pictures = _visible_pictures(conn, library_ids, include_hidden=admin, videos=video_ids)
    persons = {row[0]: row for row in store.execute(
        "SELECT id, name, hidden, cover FROM people")}
    found = collections.defaultdict(lambda: {"ids": set(), "faces": 0, "best": None})
    for face_id, person_id, content_hash, kind, _quality, _owner in _faces_of_people(store):
        person = persons.get(person_id)
        if person is None:
            continue
        if not admin and (not person[1] or person[2]):
            continue
        if person[2] and not include_hidden_people:
            continue
        ids = pictures.get((content_hash, kind))
        if not ids:
            continue
        entry = found[person_id]
        entry["ids"].update(ids)
        entry["faces"] += 1
        if entry["best"] is None:
            entry["best"] = face_id     # (by quality: see _faces_of_people)
        if person[3] == face_id:
            entry["cover"] = face_id
    listed = []
    for person_id, entry in found.items():
        person = persons[person_id]
        item = {"id": person_id, "name": person[1], "label": label(person_id, person[1]),
                "cover": entry.get("cover") or entry["best"], "faces": entry["faces"],
                "ids": sorted(entry["ids"]),
                "videos": sum(1 for image_id in entry["ids"] if image_id in video_ids)}
        if include_hidden_people and admin:
            item["hidden"] = bool(person[2])
        listed.append(item)
    listed.sort(key=lambda item: (0 if item["name"] else 1, name_key(item["name"]),
                                  -len(item["ids"]), item["id"]))
    answer["people"] = listed
    return answer


def state_key(store):
    """What search_data depends on in data/faces.db (for an ETag): [epoch, generation,
    people version]; [] without a store."""
    if store is None:
        return []
    return [_meta_value(store, "epoch"), _meta_number(store, "generation"),
            people_version(store)]


def picture_view(store, conn, row, config, admin):
    """The people of one picture for its details (/api/image), or None: {"names": [{"id",
    "label", "named", and of a video "at": the seconds into it each was seen at (the
    frames looked at, see people.video_faces), in order}] (hidden people left out), and
    for the admin "faces": [{"id", "x", "y", "w", "h" (fractions of the picture,
    upright; of a video, of its frame at "at"), "person" (id or null), "label",
    "quality", "hidden" (true for a hidden person's), "at" (a video's: seconds, or
    null for a face of its poster)}]}.
    Others than the admin get only the named people that are not hidden (and only with
    "visitors": true)."""
    if store is None or not may_see(config, admin):
        return None
    kind = people.kind_of(row)
    if kind is None or not row["content_hash"]:
        return None
    faces = store.execute(
        "SELECT f.id, f.x, f.y, f.w, f.h, f.quality, fp.person_id, p.name, p.hidden, f.at, "
        "f.seen FROM faces f LEFT JOIN face_people fp ON fp.face_id = f.id LEFT JOIN people p "
        "ON p.id = fp.person_id WHERE f.content_hash = ? AND f.kind = ? AND f.id NOT IN "
        "(SELECT face_id FROM face_marks WHERE not_face) ORDER BY f.x, f.id",
        (row["content_hash"], kind)).fetchall()
    video = people.is_video(row)
    names = []
    seen = {}
    boxes = []
    for face in faces:
        person_id, name, hidden = face[6], face[7] or "", bool(face[8])
        shown = person_id is not None and not hidden and (admin or name)
        if shown and person_id not in seen:
            seen[person_id] = {"id": person_id, "label": label(person_id, name),
                               "named": bool(name)}
            if video:
                seen[person_id]["at"] = []
            names.append(seen[person_id])
        if shown and video:
            seen[person_id]["at"].extend(moments(face[9], face[10]))
        if admin:
            box = {"id": face[0], "x": round(face[1], 5), "y": round(face[2], 5),
                   "w": round(face[3], 5), "h": round(face[4], 5), "person": person_id,
                   "label": label(person_id, name) if person_id is not None else "",
                   "quality": round(face[5], 3)}
            if hidden:
                box["hidden"] = True
            if video:
                box["at"] = face[9]
            boxes.append(box)
    for item in names:
        if "at" in item:
            item["at"] = sorted(set(item["at"]))
    names.sort(key=lambda item: (0 if item["named"] else 1, name_key(item["label"])))
    view = {"names": names}
    if admin:
        view["faces"] = boxes
    return view


def person_info(store, person_id):
    """{"id", "name", "label", "hidden", "cover", "faces"} of a person (faces: how many)."""
    row = _person(store, person_id)
    count = store.execute("SELECT COUNT(*) FROM face_people WHERE person_id = ?",
                          (row["id"],)).fetchone()[0]
    return {"id": row["id"], "name": row["name"], "label": label(row["id"], row["name"]),
            "hidden": bool(row["hidden"]), "cover": row["cover"], "faces": count}


def _sums(store):
    """{person id: [sum of its faces' vectors, how many]}."""
    sums = {}
    for person_id, blob in store.execute("SELECT fp.person_id, f.embedding FROM face_people fp "
                                         "JOIN faces f ON f.id = fp.face_id"):
        entry = sums.setdefault(person_id, [[0.0] * people.EMBEDDING_DIMS, 0])
        _add(entry[0], _vector(blob))
        entry[1] += 1
    return sums


def person_faces(store, person_id):
    """The ids of a person's faces."""
    return [row[0] for row in store.execute(
        "SELECT face_id FROM face_people WHERE person_id = ? ORDER BY face_id", (person_id,))]


def person_view(store, conn, config, person_id, library_ids):
    """The People page's view of one person: {"person": person_info(), "faces": [{"id",
    "quality", "owner", "image" (a picture it is in, or null when none is shown), and of
    a video's face "at" (the second it was seen at: where the video starts)}] best
    first -- but the best face of every moment (moment_sets) before the second of any,
    so a burst does not fill the top --, "similar": [{"id", "label", "cover", "faces",
    "score"}] (people whose faces look most like its own: maybe the same person)}."""
    info = person_info(store, person_id)
    pictures = _visible_pictures(conn, library_ids, include_hidden=True)
    rows = _faces_of_people(store, [person_id])
    keys = [(row[2], row[3]) for row in rows]
    sets = moment_sets(keys, picture_moments(conn, set(keys)))
    seen = set()
    first, later = [], []
    for row, key in zip(rows, keys):
        (later if sets[key] in seen else first).append(row)
        seen.add(sets[key])
    faces = []
    times = _face_times(store, [row[0] for row in rows])
    for face_id, _person_id, content_hash, kind, quality, owner in first + later:
        ids = pictures.get((content_hash, kind))
        face = {"id": face_id, "quality": round(quality, 3), "owner": bool(owner),
                "image": ids[0] if ids else None}
        if times.get(face_id) is not None:
            face["at"] = times[face_id]
        faces.append(face)
    options = people.settings(config)
    sums = _sums(store)
    similar = []
    mine = sums.get(person_id)
    if mine is not None:
        others = {row[0]: row for row in store.execute(
            "SELECT id, name, hidden FROM people WHERE id != ?", (person_id,))}
        for other_id, (total, count) in sums.items():
            if other_id == person_id or other_id not in others:
                continue
            # (How alike their faces are on average, as the grouping measures it.)
            score = _sumprod(mine[0], total) / float(mine[1] * count)
            if score >= options["match"] - SIMILAR_BELOW:
                row = others[other_id]
                similar.append({"id": other_id, "name": row[1], "label": label(other_id, row[1]),
                                "hidden": bool(row[2]), "faces": count,
                                "score": round(score, 3)})
        similar.sort(key=lambda item: (-item["score"], item["id"]))
        similar = similar[:SIMILAR_PEOPLE]
        for item in similar:
            item["cover"] = _cover_of(store, item["id"])
    return {"person": info, "faces": faces, "similar": similar}


def _cover_of(store, person_id):
    row = store.execute("SELECT cover FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is not None and row[0] is not None:
        return row[0]
    row = store.execute("SELECT f.id FROM face_people fp JOIN faces f ON f.id = fp.face_id "
                        "WHERE fp.person_id = ? ORDER BY f.quality DESC, f.id LIMIT 1",
                        (person_id,)).fetchone()
    return row[0] if row is not None else None


def other_faces(store, conn, library_ids, which="other", offset=0, limit=OTHER_FACES_LIMIT):
    """Faces in no person ("other", best first) or marked "not a face" ("not_face"):
    {"faces": [{"id", "quality", "image", "at" (a video's face: see person_view)}],
    "total"}. Only the faces of pictures shown."""
    pictures = _visible_pictures(conn, library_ids, include_hidden=True)
    if which == "not_face":
        sql = ("SELECT f.id, f.content_hash, f.kind, f.quality FROM faces f JOIN face_marks m "
               "ON m.face_id = f.id WHERE m.not_face ORDER BY m.updated DESC, f.id")
    else:
        sql = ("SELECT f.id, f.content_hash, f.kind, f.quality FROM faces f WHERE f.id NOT IN "
               "(SELECT face_id FROM face_people) AND f.id NOT IN (SELECT face_id FROM "
               "face_marks WHERE not_face) ORDER BY f.quality DESC, f.id")
    listed = []
    for face_id, content_hash, kind, quality in store.execute(sql):
        ids = pictures.get((content_hash, kind))
        if ids:
            listed.append({"id": face_id, "quality": round(quality, 3), "image": ids[0]})
    offset = max(0, int(offset))
    limit = max(1, min(OTHER_FACES_LIMIT, int(limit)))
    page = listed[offset:offset + limit]
    times = _face_times(store, [face["id"] for face in page])
    for face in page:
        if times.get(face["id"]) is not None:
            face["at"] = times[face["id"]]
    return {"faces": page, "total": len(listed), "offset": offset}


def _face_times(store, face_ids):
    """{face id: the second of its video it was seen at (people.py's "at")} of the faces
    of videos among face_ids."""
    found = {}
    face_ids = list(face_ids)
    for start in range(0, len(face_ids), ID_CHUNK):
        part = face_ids[start:start + ID_CHUNK]
        for face_id, at in store.execute("SELECT id, at FROM faces WHERE at IS NOT NULL AND "
                                         "id IN (%s)" % ",".join("?" * len(part)), part):
            found[face_id] = at
    return found


def moments(at, seen):
    """The seconds of a video a face was seen at (its "seen", else its "at"; none for a
    face of a poster found before the frames were looked at)."""
    if seen:
        try:
            return [round(float(item[0]), 3) for item in json.loads(seen)]
        except (TypeError, ValueError, IndexError):
            pass
    return [round(float(at), 3)] if at is not None else []


def status(store, config):
    """People's grouping for the People page: {"faces", "grouped" (faces in a person),
    "people", "named", "hidden", "other" (faces in no person), "not_faces", "due"
    (the faces changed since they were grouped), "grouped_at"}."""
    options = people.settings(config)
    if store is None:
        return {"faces": 0, "grouped": 0, "people": 0, "named": 0, "hidden": 0, "other": 0,
                "not_faces": 0, "due": False, "grouped_at": None}
    faces = store.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    grouped = store.execute("SELECT COUNT(*) FROM face_people").fetchone()[0]
    not_faces = store.execute("SELECT COUNT(*) FROM face_marks WHERE not_face").fetchone()[0]
    total, named, hidden = store.execute(
        "SELECT COUNT(*), TOTAL(name != ''), TOTAL(hidden) FROM people WHERE id IN "
        "(SELECT person_id FROM face_people)").fetchone()
    at = _meta_value(store, "grouped_at")
    try:
        at = float(at) if at else None
    except ValueError:
        at = None
    return {"faces": faces, "grouped": grouped, "people": total, "named": int(named),
            "hidden": int(hidden), "other": faces - grouped - not_faces,
            "not_faces": not_faces, "due": bool(faces) and grouping_due(store, options),
            "grouped_at": at}


# ---------------------------------------------------------------------------
# Faces as pictures

def face_source(store, conn, face_id, library_ids, admin):
    """(face row {id, x, y, w, h, person_id, name, hidden}, image id) of a face whose
    picture the requester may see, or (None, None): others than the admin only see the
    faces of named people that are not hidden, in pictures that are not hidden."""
    face = store.execute(
        "SELECT f.id, f.content_hash, f.kind, f.x, f.y, f.w, f.h, fp.person_id, p.name, "
        "p.hidden FROM faces f LEFT JOIN face_people fp ON fp.face_id = f.id LEFT JOIN "
        "people p ON p.id = fp.person_id WHERE f.id = ?", (face_id,)).fetchone()
    if face is None:
        return None, None
    if not admin and (face["person_id"] is None or not face["name"] or face["hidden"]):
        return None, None
    library_ids = list(library_ids)
    if not library_ids:
        return None, None
    for row in conn.execute(
            "SELECT id, library_id, media, projection, has_preview, hidden FROM images WHERE "
            "content_hash = ? AND missing_since IS NULL AND library_id IN (%s) ORDER BY id"
            % ",".join("?" * len(library_ids)), [face["content_hash"]] + library_ids):
        if people.looked_at(row) != face["kind"] or (row["hidden"] and not admin):
            continue
        return face, row
    return None, None


def crop(data, box, edge=CROP_EDGE, margin=CROP_MARGIN):
    """JPEG bytes of a square around a face (box: x, y, w, h as fractions of the
    picture) cut from a picture's bytes, edge pixels wide. Needs Pillow."""
    from PIL import Image, ImageOps
    image = Image.open(io.BytesIO(data))
    x, y, w, h = box
    width, height = image.size
    side = max(w * width, h * height) * (1 + 2 * margin)
    if image.format == "JPEG":
        # (Decoded at a smaller size when the face is large enough there.)
        scale = max(1, int(side // (edge * 2)))
        if scale > 1:
            image.draft("RGB", (max(1, width // scale), max(1, height // scale)))
    image = image.convert("RGB")
    sx, sy = image.size[0] / float(width), image.size[1] / float(height)
    middle_x, middle_y = (x + w / 2.0) * width * sx, (y + h / 2.0) * height * sy
    half = side * sx / 2.0
    region = (int(round(middle_x - half)), int(round(middle_y - half)),
              int(round(middle_x + half)), int(round(middle_y + half)))
    face = image.crop(region)
    face = ImageOps.fit(face, (edge, edge), method=getattr(Image, "Resampling", Image).LANCZOS)
    out = io.BytesIO()
    face.save(out, "JPEG", quality=85)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Exports

def export_names(conn, library_id):
    """{image id: [names]} of the pictures of a library with named people who are not
    hidden (for an export's "people_names" option: the names become tags). {} when there
    is no face data."""
    try:
        store = people.connect(create=False)
    except sqlite3.Error:
        return {}
    if store is None:
        return {}
    try:
        pictures = _visible_pictures(conn, [library_id], include_hidden=False)
        named = {row[0]: row[1] for row in store.execute(
            "SELECT id, name FROM people WHERE name != '' AND hidden = 0")}
        names = collections.defaultdict(set)
        for _face_id, person_id, content_hash, kind, _quality, _owner in _faces_of_people(store):
            if person_id in named:
                for image_id in pictures.get((content_hash, kind), ()):
                    names[image_id].add(named[person_id])
        return {image_id: sorted(found, key=name_key) for image_id, found in names.items()}
    except sqlite3.Error:
        return {}
    finally:
        store.close()
