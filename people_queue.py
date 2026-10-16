#!/usr/bin/env python3
"""People: the "Who's this?" queue -- the faces and unnamed people the app is
sure about, to confirm one at a time on the People page (people.js,
/people?view=queue; GET /api/admin/people/queue).

What is in it: every face in no person, and every unnamed person that is not
hidden, whose suggestion is marked (people_suggest.py: preselected, the one
Enter takes in the viewer), with that named person, the closeness and its
words. The marks are exactly suggest()'s and suggest_person()'s: worked out
with the same functions, but the faces of nobody all in one go -- their "not
this person" and the people in their pictures read with one query each, and
compared BATCH at a time with numpy (one product of matrices each), PURE_BATCH
without. A face
whose picture is not shown (its library is turned off, the picture went) is
left out: it could not be looked at.

When: the marks are worked out for a state of the face data (the file, its
epoch, generation and people version, the settings: people_suggest.state, and
QUEUE_VERSION) and kept, one state at a time. They are worked out again only
when the queue is asked for after a change: the People page asks when it lists
the people and when the owner starts, not after each answer -- it asks for the
suggestion of each item again right before it shows it (GET
/api/admin/people/suggest, by its ETag) and passes over one that is no longer
marked. In a large library that takes a fraction of a second with numpy and
several seconds without, so a thread of its own works them out
(one at a time, each numbered: a "build"): a request waits QUEUE_WAIT seconds
for them, then is told the number of the build it waits for (202), and asks
again with it -- it then gets the marks of that build or a later one, even when
the face data changed again meanwhile (while faces are being found it changes
every few seconds, sooner than a build without numpy ends). Without numpy the
thread pauses a moment (PAUSE) after each few faces, so that the server's other
requests (the pictures of the faces on the page) are not held up meanwhile.

Order (order()): the faces first, grouped by the named person they are
suggested to be (the person with the most of them first, so the faces to
compare with stay the same), the surest (the widest lead) first; then the
unnamed people, most faces first, those suggested to be the same named person
and seen in the same pictures one after another. Measured on a library's
faces judged by eye (README, People): taking the unnamed people first made
two right answers drop out of the queue before they were asked -- a merge adds
many faces to a named person at once, which made a face after it look less
sure of another person; and an unnamed person seen in two pictures with two
others named so before it was then "in two of its pictures" (people_suggest's
PERSON_SHARED) -- and this order dropped none.

The owner's answers go through the routes the People page and the viewer use
(with Undo); nothing is accepted here. Nothing of the queue is written to
disk, and nothing goes into an export.
"""

import collections
import hashlib
import json
import threading
import time

import people
import people_groups
import people_suggest

# Raise when the queue's items or their order change (the pages ask again: ETag).
QUEUE_VERSION = 1
QUEUE_WAIT = 1.0        # seconds a request waits for the marks being worked out
COMPARE_FACES = 6       # a named person's clearest faces shown to compare with
OWN_FACES = 6           # an unnamed person's clearest faces shown
CANDIDATES = 40         # clearest faces looked at to find those (one of each moment first)
BATCH = 512             # faces of nobody compared at a time (with numpy)
PURE_BATCH = 8          # ... without numpy, each followed by PAUSE
PAUSE = 0.003           # seconds the thread lets other requests in (without numpy)

_lock = threading.Lock()
# The marks of the last build: its key (marks_key), the marks, its number (0: none).
_kept = {"key": None, "marks": None, "build": 0}
# The build running (one at a time): {"number", "config", "key" (read by the thread:
# None until then), "done" (an Event), "error", "stop" (no longer wanted: forget())},
# or None; "started": how many were started; "again": a request waits for the build
# after the one running (it read an older state): it is started when that one ends;
# "config": the settings the last request was made with.
_work = {"build": None, "started": 0, "again": False, "config": None}


class _Stopped(Exception):
    """A build no longer wanted (forget()) stops at its next pause."""


def forget():
    """Drop the marks kept, and those being worked out (Delete face data: the faces and
    names go; the build running stops soon and lets go of the file)."""
    with _lock:
        _kept.update(key=None, marks=None, build=0)
        _work["again"] = False
        if _work["build"] is not None:
            _work["build"]["stop"] = True


def _digest(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def person_seen(store, person_id):
    """A person as Who's this? shows it, in short: its name, whether it is hidden and
    the ids of its faces (a digest), or None when it is gone. The page sends it back
    with its answer about the person ("seen"): refused (409) when it changed
    meanwhile -- a grouping gave it other faces, another tab named or hid it."""
    row = store.execute("SELECT name, hidden FROM people WHERE id = ?",
                        (person_id,)).fetchone()
    if row is None:
        return None
    return _digest([row[0], bool(row[1]), people_groups.person_faces(store, person_id)])[:16]


def marks_key(store, config):
    """What the marks depend on: the face data's file and state, the settings,
    SUGGEST_VERSION (people_suggest.state), QUEUE_VERSION, and numpy (its last bits)."""
    return _digest([QUEUE_VERSION, people_suggest._store_file(store),
                    people_suggest.state(store, config), people_groups._numpy() is not None])


def empty():
    """The queue without face data."""
    return {"version": QUEUE_VERSION, "data": "", "items": [], "people": {},
            "counts": {"faces": 0, "persons": 0, "person_faces": 0}}


def get(store, conn, config, library_ids, wait=None, after=None):
    """(The queue for the pictures shown now, its ETag, None) -- see finish() -- or
    (None, None, the number of the build to ask again with: after) while its marks are
    being worked out. store: data/faces.db, conn: index.db, library_ids: the libraries
    shown. wait: seconds to wait for marks being worked out (None: QUEUE_WAIT). The
    marks are those of the face data as it is now, or of a build started after the
    request that was told its number (after) -- so a request asked again is answered
    within a build or two, however often the face data changes meanwhile. One build
    runs at a time: a request that comes while one runs that read an older state waits
    for the next one (several requests, one next build)."""
    key = marks_key(store, config)
    deadline = time.monotonic() + max(0.0, QUEUE_WAIT if wait is None else wait)
    found = None
    with _lock:
        _work["config"] = config
        # (A number of another run of the server, or of none yet: as if none.)
        need = after if after is not None and 0 < after <= _work["started"] + 1 else None
    while True:
        with _lock:
            if _kept["key"] == key or (need is not None and _kept["build"] >= need):
                found = (_kept["key"], _kept["marks"])
                break
            build = _work["build"]
            if need is None:
                if build is None:
                    need = _work["started"] + 1
                elif build["key"] == key:
                    need = build["number"]
                else:
                    # (It read an older state, or is about to read one: the one after it.)
                    need = build["number"] + 1
                    _work["again"] = True
            if build is None:
                build = _start()
        if not build["done"].wait(max(0.0, deadline - time.monotonic())):
            return None, None, need
        if build["error"] is not None and build["number"] >= need:
            raise build["error"]
    key, marks = found
    if marks is None:
        return empty(), None, None
    queue, tag = finish(marks, key, conn, library_ids)
    return queue, tag, None


def _start():
    """Start the next build (with _lock held)."""
    _work["started"] += 1
    build = {"number": _work["started"], "config": _work["config"], "key": None,
             "done": threading.Event(), "error": None, "stop": False}
    _work["build"] = build
    threading.Thread(target=_run, args=(build,), name="people-queue", daemon=True).start()
    return build


def _run(build):
    """The thread that works the marks out (with a connection of its own)."""
    try:
        store = people.connect(create=False)
        key, marks = None, None
        if store is not None:
            try:
                # (One read: the state and everything read with it agree, whatever
                # another connection writes meanwhile.)
                store.execute("BEGIN")
                try:
                    key = marks_key(store, build["config"])
                    with _lock:
                        build["key"] = key
                    marks = build_marks(store, build["config"],
                                        stop=lambda: build["stop"])
                finally:
                    store.rollback()
            finally:
                store.close()
        with _lock:
            if not build["stop"]:
                _kept.update(key=key, marks=marks, build=build["number"])
    except _Stopped:
        pass                        # (forgotten: those who wait ask for the next build)
    except Exception as error:      # (told to the requests that wait)
        build["error"] = error
    finally:
        with _lock:
            _work["build"] = None
            if _work["again"]:
                _work["again"] = False
                _start()
        build["done"].set()


def build_marks(store, config, use_numpy=None, stop=None):
    """What the queue is made of, for the state of the face data now: {"faces",
    "persons" (the marked items, in order: order()), "named" {id: a named person
    suggested}, "theirs" {named person id: its clearest faces}, "own" {unnamed person id:
    its clearest faces}, "where" {face id: (content hash, kind)}, "times" {face id: the
    second of its video}, "data" (which face data: a digest of its epoch), "seconds"
    (how long it took)}. stop(): True when the marks are no longer wanted (asked
    between batches: _Stopped)."""
    started = time.perf_counter()
    options = people.settings(config)
    index = people_suggest._index(store, options, use_numpy)
    pace, batch = _pace(index, stop)
    faces = _marked_faces(store, index, pace, batch)
    persons = _marked_persons(store, index, pace)
    wanted = sorted({item["to"] for item in faces + persons})
    own = _clearest(store, [item["person"] for item in persons])
    theirs = _clearest(store, wanted)
    ids = ([item["face"] for item in faces + persons]
           + [face_id for rows in list(own.values()) + list(theirs.values())
              for face_id in rows])
    where = {}
    for start in range(0, len(ids), people_groups.ID_CHUNK):
        part = ids[start:start + people_groups.ID_CHUNK]
        for face_id, content_hash, kind in store.execute(
                "SELECT id, content_hash, kind FROM faces WHERE id IN (%s)"
                % ",".join("?" * len(part)), part):
            where[face_id] = (content_hash, kind)
    named = {entry["id"]: {"id": entry["id"], "name": entry["name"], "label": entry["label"],
                           "faces": entry["faces"]}
             for entry in index["people"] if entry["id"] in theirs}
    ordered = order(faces, persons)
    for item in ordered:
        item.pop("pictures", None)      # (only to order them)
    return {"faces": [item for item in ordered if item["kind"] == "face"],
            "persons": [item for item in ordered if item["kind"] == "person"],
            "named": named, "theirs": theirs, "own": own, "where": where,
            "times": people_groups._face_times(store, ids),
            "data": _digest(people_groups._meta_value(store, "epoch"))[:12],
            "seconds": round(time.perf_counter() - started, 3)}


def _pace(index, stop):
    """(pace, the faces of nobody compared at a time): pace() is called after each batch
    of them and each unnamed person -- _Stopped when stop() says the marks are no longer
    wanted; without numpy (a loop that holds Python's lock all along) it pauses a
    moment too (PAUSE), for the server's other requests."""
    pure = bool(index["vectors"])       # (named people's vectors, and no numpy)

    def pace():
        if stop is not None and stop():
            raise _Stopped()
        if pure:
            time.sleep(PAUSE)
    return pace, (PURE_BATCH if pure else BATCH)


def finish(marks, key, conn, library_ids):
    """(The queue for the pictures shown now, its ETag) of marks: {"version", "data"
    (which face data: the page's skips are its), "items" in order, "people" {named
    person id (text): {"id", "name", "label", "faces" (how many), "show": its clearest
    faces to compare with}}, "counts" {"faces", "persons", "person_faces" (theirs)}}.
    An item: {"kind": "face", "face", "to" (the named person), "score", "lead" (over the
    next named person), "words", "image" (a picture it is in), "at" (a video's: the
    second it was seen at)?} or {"kind": "person", "person", "label", "face" (its
    clearest face: the one its suggestion is asked again by), "faces" (how many),
    "seen" (person_seen(): sent back with the answer, refused when it changed), "show"
    (its clearest faces), "to", "score", "lead", "words"}. The ETag: the marks' key,
    the libraries and the pictures shown."""
    where, times = marks["where"], marks["times"]
    keys = set(where.values())
    pictures = _pictures(conn, keys, library_ids)
    moments = people_groups.picture_moments(conn, keys)

    def picture(face_id):
        ids = pictures.get(where.get(face_id))
        return ids[0] if ids else None

    def varied(face_ids, limit):
        """At most limit of face_ids (clearest first) whose pictures are shown, one of
        each moment first (people_groups.moment_sets: a burst, a video's frames)."""
        shown = [face_id for face_id in face_ids if picture(face_id) is not None]
        sets = people_groups.moment_sets([where[face_id] for face_id in shown], moments)
        seen, first, later = set(), [], []
        for face_id in shown:
            number = sets[where[face_id]]
            (later if number in seen else first).append(face_id)
            seen.add(number)
        return (first + later)[:limit]

    items = []
    for item in marks["faces"]:
        image = picture(item["face"])
        if image is None:
            continue
        item = dict(item, image=image)
        if times.get(item["face"]) is not None:
            item["at"] = times[item["face"]]
        items.append(item)
    face_count = len(items)
    for item in marks["persons"]:
        show = varied(marks["own"].get(item["person"], []), OWN_FACES)
        if show:
            items.append(dict(item, show=show))
    listed = {str(person_id): dict(entry, show=varied(marks["theirs"].get(person_id, []),
                                                      COMPARE_FACES))
              for person_id, entry in marks["named"].items()}
    persons = items[face_count:]
    queue = {"version": QUEUE_VERSION, "data": marks.get("data", ""), "items": items,
             "people": listed,
             "counts": {"faces": face_count, "persons": len(persons),
                        "person_faces": sum(item["faces"] for item in persons)}}
    shown = sorted([face_id, picture(face_id)] for face_id in where)
    return queue, _digest([key, sorted(library_ids), shown])


def order(faces, persons):
    """The items in the order they are asked about (see the module notes): the faces,
    grouped by the person suggested (most such faces first), the widest lead first; then
    the unnamed people, most faces first, but those suggested to be the same person and
    seen in the same picture (the frames of one video, a 360-degree picture: "linked")
    one after another, each after one it is linked to, from the one with the most links:
    a person already in two of an unnamed person's pictures is never preselected for it,
    so in a chain or a star of them each would have at most one named before it."""
    by_id = {item["person"]: item for item in persons}
    links = {item["person"]: set() for item in persons}
    for item in persons:
        for other in persons:
            if (other["person"] != item["person"] and other["to"] == item["to"]
                    and set(item["pictures"]) & set(other["pictures"])):
                links[item["person"]].add(other["person"])

    def rank(person_id):
        item = by_id[person_id]
        return (-item["faces"], -item["score"], person_id)

    groups, seen = [], set()
    for person_id in sorted(by_id, key=rank):
        if person_id in seen:
            continue
        members, stack = set(), [person_id]
        while stack:
            current = stack.pop()
            if current not in members:
                members.add(current)
                stack.extend(links[current] - members)
        first = min(members, key=lambda member: (-len(links[member]),) + rank(member))
        walked, waiting = [first], collections.deque([first])
        seen.add(first)
        while waiting:
            current = waiting.popleft()
            for other in sorted(links[current] - seen, key=rank):
                seen.add(other)
                walked.append(other)
                waiting.append(other)
        groups.append(walked)
    groups.sort(key=lambda walked: (-sum(by_id[member]["faces"] for member in walked),
                                    rank(walked[0])))
    count = collections.Counter(item["to"] for item in faces)
    faces = sorted(faces, key=lambda item: (-count[item["to"]], item["to"], -item["lead"],
                                            item["face"]))
    return faces + [by_id[member] for walked in groups for member in walked]


def _lead(scores, person_id):
    others = [value for other, value in scores.items() if other != person_id]
    return scores[person_id] - max(others or [0.0])


def _marked_faces(store, index, pace=None, batch=BATCH):
    """The faces in no person whose suggestion is marked (as people_suggest.suggest),
    compared batch at a time (pace() after each)."""
    rows = store.execute(
        "SELECT id, embedding, content_hash, kind FROM faces WHERE id NOT IN (SELECT "
        "face_id FROM face_people) AND id NOT IN (SELECT face_id FROM face_marks WHERE "
        "not_face) ORDER BY id").fetchall()
    refused = collections.defaultdict(set)
    for face_id, person_id in store.execute(
            "SELECT face_id, person_id FROM face_rejects WHERE face_id NOT IN (SELECT face_id "
            "FROM face_people)"):
        refused[face_id].add(person_id)
    # (The people of the other faces of their pictures: a face of nobody is in no
    # person itself, so every row here is another face's.)
    inside = collections.defaultdict(set)
    for content_hash, kind, person_id in store.execute(
            "SELECT f.content_hash, f.kind, fp.person_id FROM faces f JOIN face_people fp ON "
            "fp.face_id = f.id WHERE f.content_hash IN (SELECT content_hash FROM faces WHERE "
            "id NOT IN (SELECT face_id FROM face_people))"):
        inside[(content_hash, kind)].add(person_id)
    found = []
    for start in range(0, len(rows), batch):
        if pace is not None and start:
            pace()
        part = rows[start:start + batch]
        scores = people_suggest._closeness(index, [people_groups._vector(row[1])
                                                   for row in part])
        for row, closeness in zip(part, scores):
            _items, preselect = people_suggest._ranked(
                index, closeness, refused.get(row[0], ()), inside.get((row[2], row[3]), ()),
                people_suggest.FACE_SCORE, people_suggest.FACE_LEAD, people_suggest.SHOWN)
            if preselect is None:
                continue
            score = closeness[preselect]
            found.append({"kind": "face", "face": row[0], "to": preselect,
                          "score": round(score, 3),
                          "lead": round(_lead(closeness, preselect), 3),
                          "words": people_suggest.words(score)})
    return found


def _marked_persons(store, index, pace=None):
    """The unnamed people (not hidden) whose suggestion is marked (as
    people_suggest.suggest_person, whose answers the index keeps: the suggest route has
    them ready when the page asks again); pace() after each."""
    found = []
    for (person_id,) in store.execute(
            "SELECT id FROM people WHERE name = '' AND hidden = 0 AND id IN (SELECT "
            "person_id FROM face_people) ORDER BY id").fetchall():
        if pace is not None:
            pace()
        answer, _measured = people_suggest._person(store, index, person_id)
        target = answer["preselect"]
        if target is None:
            continue
        scores = {item["id"]: item["score"] for item in answer["people"]}
        best = store.execute(
            "SELECT f.id FROM face_people fp JOIN faces f ON f.id = fp.face_id WHERE "
            "fp.person_id = ? AND f.id NOT IN (SELECT face_id FROM face_marks WHERE not_face) "
            "ORDER BY f.quality DESC, f.id LIMIT 1", (person_id,)).fetchone()
        pictures = sorted("%s/%s" % tuple(row) for row in store.execute(
            "SELECT DISTINCT f.content_hash, f.kind FROM face_people fp JOIN faces f ON "
            "f.id = fp.face_id WHERE fp.person_id = ?", (person_id,)))
        found.append({"kind": "person", "person": person_id,
                      "label": answer["person"]["label"], "face": best[0],
                      "faces": answer["person"]["faces"],
                      "seen": person_seen(store, person_id), "to": target,
                      "score": scores[target], "lead": round(_lead(scores, target), 3),
                      "words": people_suggest.words(scores[target]), "pictures": pictures})
    return found


def _clearest(store, person_ids):
    """{person id: its clearest faces (at most CANDIDATES), clearest first}."""
    found = {}
    for person_id in person_ids:
        found[person_id] = [row[0] for row in store.execute(
            "SELECT f.id FROM face_people fp JOIN faces f ON f.id = fp.face_id WHERE "
            "fp.person_id = ? AND f.id NOT IN (SELECT face_id FROM face_marks WHERE not_face) "
            "ORDER BY f.quality DESC, f.id LIMIT ?", (person_id, CANDIDATES))]
    return found


def _pictures(conn, keys, library_ids):
    """{(content hash, kind): [ids of the pictures shown]} of keys (hidden pictures too:
    the admin's)."""
    found = collections.defaultdict(list)
    library_ids = set(library_ids)
    hashes = sorted({content_hash for content_hash, _kind in keys if content_hash})
    if not library_ids or not hashes:
        return found
    for start in range(0, len(hashes), people_groups.ID_CHUNK):
        part = hashes[start:start + people_groups.ID_CHUNK]
        # (By the pictures' hashes only: with the libraries in the query too, SQLite
        # reads every picture of those libraries.)
        for row in conn.execute(
                "SELECT id, library_id, content_hash, media, projection, has_preview, hidden "
                "FROM images WHERE content_hash IN (%s) AND missing_since IS NULL ORDER BY id"
                % ",".join("?" * len(part)), part):
            if row["library_id"] not in library_ids:
                continue
            kind = people.looked_at(row)
            if kind is not None and (row["content_hash"], kind) in keys:
                found[(row["content_hash"], kind)].append(row["id"])
    return found
