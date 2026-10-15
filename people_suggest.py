#!/usr/bin/env python3
"""People: who a face most likely is, among the named people (the viewer's naming box).

How close a face is to a named person: the mean of the NEAREST highest cosines
of its vector with the vectors of that person's clear faces -- those of a
quality (people.quality) of at least "group_quality", the faces the grouping
groups by (people_groups.py), whoever put them in the person; all of its faces
while it has fewer clear ones than NEAREST (and then it is never preselected).
A face is often like a few of a person's faces (the same years, light,
glasses) and unlike the rest, which an average over all of them hides; and a
stranger's small, blurred or turned face is as like the person's poor faces as
the person's own, so those are left out -- also the ones the owner put in the
person (by taking a suggestion, say): counted, they would draw in more poor
faces of strangers, and those more again.

What a face is offered (suggest):
  - a face marked "not a face", or a face of a named person: nothing (it has its
    name; the faces of the named people were measured, and "also looks like"
    would only have been a false alarm);
  - a face in no person: the named people it is closest to, at least SHOWN_FROM
    ("a little like"; none closer: nothing is offered), but those it was said not
    to be (face_rejects: "Not X" is never offered back); a named person with
    another face in the same picture comes last of those and is never
    preselected -- two faces of one picture are two people, but for a mirror,
    the two edges of a 360-degree picture and photos of photos, so it stays;
  - a face of an unnamed person ("Person 12"): what its person most likely is
    (suggest_person: "Name Person 12 as X?", all its faces), which is surer than
    any one of its faces.
Hidden people are listed, marked, and never preselected.

One is preselected (Enter takes it) only when it is clearly ahead: a closeness
of at least FACE_SCORE and FACE_LEAD more than every other named person's
(those it was said not to be and those in the picture too: a face as like a
person it is not is a look-alike), and the named person has at least NEAREST
clear faces. For an unnamed person: its faces' closeness on average, at least
PERSON_SCORE and PERSON_LEAD ahead (a person of fewer than PERSON_SURE faces:
FACE_SCORE and FACE_LEAD, as one face), and every one of its faces measured
-- and the face asked about -- at least PERSON_FACE_FLOOR close to that named
person and PERSON_FACE_LEAD closer to it than to any other (a person the
grouping mixed up is not taken as a whole). These were set by measurement on a
library's faces judged by eye (README, People): the faces the grouping left
out are the unsure ones, and relatives look alike, so a single face is rarely
preselected; an unnamed person more often.

The named people's vectors are read once per state of data/faces.db (its file,
people_groups.state_key: epoch, generation, people version) and the settings,
and kept; so are the answers for unnamed people. Uses only the standard
library (numpy, when there, to compare faces faster).
"""

import copy
import hashlib
import heapq
import json
import math
import operator
import threading

import people
import people_groups

try:
    _sumprod = math.sumprod             # (Python 3.12 and later)
except AttributeError:                  # pragma: no cover - older Pythons
    _mul = operator.mul

    def _sumprod(first, second):
        return sum(map(_mul, first, second))


# Raise when the suggestions change (the admin's pages ask again: ETag).
SUGGEST_VERSION = 3
NEAREST = 5             # a face's closeness to a person: the mean of this many best cosines
SHOWN = 3               # the suggestions offered first ...
SHOWN_FROM = 0.35       # ... those at least this close ("a little like"; "hardly like" was
                        # measured never right)
FACE_SCORE = 0.49       # a face in no person: its best is preselected from this closeness...
FACE_LEAD = 0.175       # ... this much ahead of the next named person (0.17 marked a
                        # turned face 0.174 ahead, judged probably someone else)
PERSON_SCORE = 0.45     # an unnamed person: the same, its faces' closeness on average ...
PERSON_LEAD = 0.16
PERSON_SURE = 3         # ... when it has this many faces or more (fewer: as one face)
PERSON_FACE_FLOOR = 0.35    # ... each of its faces at least this close to that person,
PERSON_FACE_LEAD = 0.05     # ... and this much closer to it than to any other
PERSON_SHARED = 2       # pictures an unnamed person shares with a named one: after the others
PERSON_FACES = 40       # an unnamed person's best faces measured (the rest add little)
# How close, in words (the naming box shows them with the closeness): measured,
# a first suggestion "very like" was nearly always right, "like" mostly, "a
# little like" seldom and "hardly like" never (and it is not shown).
WORDS = ((0.55, "very like"), (0.45, "like"), (0.35, "a little like"))
LAST_WORDS = "hardly like"

# The index of the named people (_index); its "answers" (unnamed people's, as
# suggest_person keeps them) are added to in place -- a single assignment, and an
# index that was replaced meanwhile just drops them.
_cache = {"index": None}
_building = threading.Lock()    # (one request builds an index; others at the same time wait)


def words(score):
    """How close a closeness is, in words (of the closeness to two places, as shown)."""
    score = round(score, 2)
    for floor, text in WORDS:
        if score >= floor:
            return text
    return LAST_WORDS


def forget():
    """Drop the named people kept in memory (Delete face data: names and vectors go)."""
    _cache["index"] = None


def _store_file(store):
    for row in store.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ""
    return ""


def state(store, config):
    """What the suggestions depend on (for an ETag): the face data's state, the settings
    the faces are measured with and SUGGEST_VERSION."""
    options = people.settings(config)
    return [people_groups.state_key(store), options["group_quality"], SUGGEST_VERSION]


def _index(store, options, use_numpy=None):
    """The named people as the suggestions measure against: {"key", "people": [{"id",
    "name", "label", "hidden", "faces" (how many), "cover", "clear" (how many clear
    faces), "count" (faces measured against), "start", "end" (their rows of
    vectors)}], "vectors" (without numpy) or "matrix" (numpy), "answers" {person id:
    (suggest_person()'s answer, the face ids it measured)}}, read again when the face
    data changed."""
    numpy = people_groups._numpy() if use_numpy is not False else None
    key = (_store_file(store), tuple(people_groups.state_key(store)),
           options["group_quality"], numpy is not None)
    index = _cache["index"]
    if index is not None and index["key"] == key:
        return index
    with _building:
        index = _cache["index"]
        if index is None or index["key"] != key:
            index = _build(store, options, key, numpy)
            _cache["index"] = index
    return index


def _build(store, options, key, numpy):
    marked = {row[0] for row in store.execute("SELECT face_id FROM face_marks WHERE not_face")}
    entries = {}
    for person_id, name, hidden, cover in store.execute(
            "SELECT id, name, hidden, cover FROM people WHERE name != '' ORDER BY id"):
        entries[person_id] = {"id": person_id, "name": name,
                              "label": people_groups.label(person_id, name),
                              "hidden": bool(hidden), "faces": 0, "cover": cover, "best": None,
                              "clear": [], "rest": []}
    for person_id, face_id, quality, blob in store.execute(
            "SELECT fp.person_id, f.id, f.quality, f.embedding FROM face_people fp "
            "JOIN faces f ON f.id = fp.face_id JOIN people p ON p.id = fp.person_id WHERE "
            "p.name != '' ORDER BY fp.person_id, f.quality DESC, f.id"):
        entry = entries.get(person_id)
        if entry is None or face_id in marked:
            continue
        entry["faces"] += 1
        if entry["best"] is None:
            entry["best"] = face_id     # (the best face: its cover when none was chosen)
        vector = people_groups._vector(blob)
        (entry["clear"] if quality >= options["group_quality"] else entry["rest"]).append(vector)
    listed, vectors = [], []
    for person_id in sorted(entries):
        entry = entries[person_id]
        chosen = (entry["clear"] if len(entry["clear"]) >= NEAREST
                  else entry["clear"] + entry["rest"])
        if not chosen:
            continue
        start = len(vectors)
        vectors.extend(chosen)
        listed.append({"id": person_id, "name": entry["name"], "label": entry["label"],
                       "hidden": entry["hidden"], "faces": entry["faces"],
                       "cover": entry["cover"] if entry["cover"] is not None else entry["best"],
                       "clear": len(entry["clear"]), "count": len(chosen), "start": start,
                       "end": len(vectors)})
    matrix = None
    if numpy is not None and vectors:
        matrix = numpy.asarray(vectors, dtype=numpy.float64)
        vectors = None                  # (one copy of them kept)
    return {"key": key, "people": listed, "vectors": vectors, "matrix": matrix, "answers": {}}


def _closeness(index, queries):
    """[{named person id: closeness}] of each of queries (unit vectors)."""
    found = [{} for _query in queries]
    if not queries or not index["people"]:
        return found
    matrix = index["matrix"]
    if matrix is not None:
        numpy = people_groups._numpy()
        values = numpy.asarray(queries, dtype=numpy.float64) @ matrix.T
        for entry in index["people"]:
            part = values[:, entry["start"]:entry["end"]]
            k = min(NEAREST, part.shape[1])
            if part.shape[1] > k:
                part = -numpy.partition(-part, k - 1, axis=1)[:, :k]
            for row, score in zip(found, part.mean(axis=1).tolist()):
                row[entry["id"]] = score
        return found
    vectors = index["vectors"]
    for row, query in zip(found, queries):
        for entry in index["people"]:
            values = [_sumprod(query, vectors[at]) for at in range(entry["start"], entry["end"])]
            best = heapq.nlargest(NEAREST, values)
            row[entry["id"]] = sum(best) / len(best)
    return found


def _ranked(index, scores, refused, apart, sure, lead, limit, shared=None):
    """(The suggestions -- refused left out --, the id of the one preselected or None.)
    limit: the closest that are at least SHOWN_FROM (0: every named person, to measure
    them), closest first but those apart last. shared {person id: pictures} (an
    unnamed person's): said of each as "shared"."""
    everyone = [entry for entry in index["people"] if entry["id"] in scores]
    everyone.sort(key=lambda entry: (-scores[entry["id"]], entry["id"]))
    preselect = None
    if everyone:
        first = everyone[0]
        score = scores[first["id"]]
        # (The lead over every other named person: those apart and those refused too.)
        second = scores[everyone[1]["id"]] if len(everyone) > 1 else 0.0
        if (first["id"] not in refused and first["id"] not in apart and not first["hidden"]
                and first["clear"] >= NEAREST and score >= sure and score - second >= lead):
            preselect = first["id"]
    listed = [entry for entry in everyone if entry["id"] not in refused]
    if limit:
        # (The closest first, then those apart: a close one in the picture keeps its
        # place among those shown, last.)
        listed = [entry for entry in listed
                  if round(scores[entry["id"]], 3) >= SHOWN_FROM][:limit]
    listed.sort(key=lambda entry: entry["id"] in apart)     # (stable)
    items = []
    for entry in listed:
        score = scores[entry["id"]]
        item = {"id": entry["id"], "name": entry["name"], "label": entry["label"],
                "faces": entry["faces"], "cover": entry["cover"], "hidden": entry["hidden"],
                "score": round(score, 3), "words": words(score),
                "in_picture": entry["id"] in apart}
        if shared is not None:
            item["shared"] = shared.get(entry["id"], 0)
        items.append(item)
    return items, preselect


def _fits(scores, person_id):
    """Whether a face of these closenesses (every named person's) is at least
    PERSON_FACE_FLOOR close to person_id and PERSON_FACE_LEAD closer to it than to any
    other named person."""
    score = scores.get(person_id)
    others = [value for other, value in scores.items() if other != person_id]
    return (score is not None and score >= PERSON_FACE_FLOOR
            and score - max(others or [0.0]) >= PERSON_FACE_LEAD)


def suggest(store, face_id, config, limit=SHOWN, use_numpy=None):
    """The named people a face most likely is: {"face", "basis", "people": [{"id",
    "name", "label", "faces", "cover" (a face id), "hidden", "score" (the closeness),
    "words" (it, in words), "in_picture" (a face of that person is in this picture)}]
    -- the closest limit of them that are at least SHOWN_FROM close, those in the
    picture last (0: every named person, closest first, to measure them) --,
    "preselect": the id Enter takes, or None}. basis: "face" (a face in no person),
    "person" (a face of an unnamed person: what that person most likely is, as
    suggest_person, with "person": {"id", "label", "faces"}; preselected only when
    this face is like that named person too), "named" (a face of a named person),
    "not_face" or "missing": nothing. config: config.json (people's settings)."""
    answer = {"face": face_id, "basis": "missing", "people": [], "preselect": None}
    row = store.execute(
        "SELECT f.embedding, f.content_hash, f.kind, fp.person_id, p.name, (SELECT 1 FROM "
        "face_marks m WHERE m.face_id = f.id AND m.not_face) FROM faces f LEFT JOIN "
        "face_people fp ON fp.face_id = f.id LEFT JOIN people p ON p.id = fp.person_id "
        "WHERE f.id = ?", (face_id,)).fetchone()
    if row is None:
        return answer
    if row[5]:
        answer["basis"] = "not_face"
        return answer
    if row[3] is not None:
        if row[4]:
            answer["basis"] = "named"
            return answer
        index = _index(store, people.settings(config), use_numpy)
        found, measured = _person(store, index, row[3])
        preselect = found["preselect"]
        if preselect is not None and face_id not in measured and not _fits(
                _closeness(index, [people_groups._vector(row[0])])[0], preselect):
            preselect = None        # (the odd one out of its person: not the whole person)
        answer.update({"basis": "person", "person": found.get("person"),
                       "people": _shown(found["people"], limit), "preselect": preselect})
        return answer
    index = _index(store, people.settings(config), use_numpy)
    refused = {item[0] for item in store.execute(
        "SELECT person_id FROM face_rejects WHERE face_id = ?", (face_id,))}
    apart = {item[0] for item in store.execute(
        "SELECT DISTINCT fp.person_id FROM faces f JOIN face_people fp ON fp.face_id = f.id "
        "WHERE f.content_hash = ? AND f.kind = ? AND f.id != ?", (row[1], row[2], face_id))}
    scores = _closeness(index, [people_groups._vector(row[0])])[0]
    answer["basis"] = "face"
    answer["people"], answer["preselect"] = _ranked(index, scores, refused, apart, FACE_SCORE,
                                                    FACE_LEAD, limit)
    return answer


def _shown(items, limit):
    """Of an unnamed person's suggestions (every named person, those apart last):
    those shown (as _ranked's limit)."""
    if not limit:
        return items
    shown = sorted((item for item in items if item["score"] >= SHOWN_FROM),
                   key=lambda item: (-item["score"], item["id"]))[:limit]
    return sorted(shown, key=lambda item: item["in_picture"])


def suggest_person(store, person_id, config, limit=SHOWN, use_numpy=None):
    """The named people an unnamed person most likely is ("Name Person 12 as X?"):
    {"basis": "person", "person": {"id", "label", "faces"}, "people": [...] as
    suggest()'s ("in_picture": that person is in PERSON_SHARED or more of its
    pictures, "shared": in how many), "preselect"}; basis "named" (nothing) for a
    named person, "missing" for none. Never a named person a face of this one was
    said not to be, nor one with a face said not to be this one (a merge would
    forget that "not"; the grouping keeps them apart too). Kept until the face data
    changes."""
    index = _index(store, people.settings(config), use_numpy)
    answer = _person(store, index, person_id)[0]
    answer["people"] = _shown(answer["people"], limit)
    return answer


def _person(store, index, person_id):
    """(A copy of the kept answer for person_id -- every named person --, the ids of the
    faces it measured.)"""
    known = index["answers"].get(person_id)
    if known is None:
        known = _person_answer(store, index, person_id)
        index["answers"][person_id] = known
    return copy.deepcopy(known[0]), known[1]


def _person_answer(store, index, person_id):
    answer = {"basis": "missing", "people": [], "preselect": None}
    row = store.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        return answer, frozenset()
    if row[0]:
        answer["basis"] = "named"
        return answer, frozenset()
    faces = [item for item in store.execute(
        "SELECT f.id, f.embedding FROM face_people fp JOIN faces f ON f.id = fp.face_id WHERE "
        "fp.person_id = ? AND f.id NOT IN (SELECT face_id FROM face_marks WHERE not_face) "
        "ORDER BY f.quality DESC, f.id", (person_id,))]
    answer["basis"] = "person"
    answer["person"] = {"id": person_id, "label": people_groups.label(person_id, ""),
                        "faces": len(faces)}
    if not faces:
        return answer, frozenset()
    refused = {item[0] for item in store.execute(
        "SELECT DISTINCT r.person_id FROM face_rejects r JOIN face_people fp ON "
        "fp.face_id = r.face_id WHERE fp.person_id = ?", (person_id,))}
    refused.update(item[0] for item in store.execute(
        "SELECT DISTINCT fp.person_id FROM face_rejects r JOIN face_people fp ON "
        "fp.face_id = r.face_id WHERE r.person_id = ?", (person_id,)))
    shared = {item[0]: item[1] for item in store.execute(
        "SELECT fp2.person_id, COUNT(DISTINCT f.content_hash || '/' || f.kind) FROM "
        "face_people fp JOIN faces f ON f.id = fp.face_id JOIN faces f2 ON f2.content_hash "
        "= f.content_hash AND f2.kind = f.kind AND f2.id != f.id JOIN face_people fp2 ON "
        "fp2.face_id = f2.id WHERE fp.person_id = ? AND fp2.person_id != ? GROUP BY "
        "fp2.person_id", (person_id, person_id))}
    apart = {other for other, count in shared.items() if count >= PERSON_SHARED}
    measured = faces[:PERSON_FACES]
    rows = _closeness(index, [people_groups._vector(item[1]) for item in measured])
    scores = {entry["id"]: sum(found[entry["id"]] for found in rows) / float(len(rows))
              for entry in index["people"]}
    # (A person of a face or two is as sure as one face: its bar.)
    sure, lead = ((PERSON_SCORE, PERSON_LEAD) if len(measured) >= PERSON_SURE
                  else (FACE_SCORE, FACE_LEAD))
    answer["people"], answer["preselect"] = _ranked(index, scores, refused, apart, sure, lead,
                                                    0, shared)
    if answer["preselect"] is not None and not all(_fits(found, answer["preselect"])
                                                   for found in rows):
        answer["preselect"] = None      # (a face of it like another, or like nobody much)
    return answer, frozenset(item[0] for item in measured)


def etag(store, config, face_id):
    """The ETag of suggest()'s answer for face_id (it changes with state())."""
    text = json.dumps([state(store, config), face_id])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:32]
