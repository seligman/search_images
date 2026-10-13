#!/usr/bin/env python3
"""Favorites, ratings and albums: the owner's marks on pictures (db.SCHEMA_V6).

Marks (the image_marks table): a picture is a favorite or not, and has a
rating of 0 (none) to 5 stars. A picture without either has no row.

Albums (albums, album_items): a "manual" album holds the pictures put in
it, in an order of the owner's (position); a "smart" album is a saved
search -- what was typed in the search box (query) with the filters, the
sort and the words/meaning mode (options, JSON: see clean_options) --
that the search page (static/search-core.js) works out whenever it is
used, so it always holds the pictures that match now. Album names are
unique, compared without regard to case. Deleting an album never touches
its pictures.

Marks and album places belong to the picture: when a file is moved,
renamed or swapped with another, they go with it exactly as the owner's
edits do (see reassign and scan.py), and they go when the picture's row
is deleted (ON DELETE CASCADE).

The search records (search_index.py) carry fav, rt and al (the album ids
of a picture); the server's /api/index answer also lists the albums
(index_albums). An export publishes none of it unless its "marks" option
says so (export.py). Changing marks or album places does not touch the
pictures' rows: IndexService notices the change by data_state() below.

This module uses only the standard library.
"""

import json
import re
import time
import unicodedata

import image_edits

# Most pictures one request may mark (or put in, or take out of, an album).
MARK_LIMIT = 10000
RATINGS = (0, 1, 2, 3, 4, 5)
KINDS = ("manual", "smart")
# Longest album name, and longest saved search (search-core's MAX_QUERY_LENGTH).
NAME_LENGTH = 100
QUERY_LENGTH = 1000
# Most albums, and most pictures in one manual album.
ALBUM_LIMIT = 10000
ITEMS_LIMIT = 100000
ID_CHUNK = 500              # ids per SQL statement (older SQLite allows 999 parameters)
# What a smart album keeps besides its query (the search page's state, see
# static/site.js searchState): the filter groups of SearchCore.FACETS (a
# test keeps them in step), the sorts, the words/meaning modes.
FILTER_GROUPS = ("library", "type", "people", "setting", "time", "event", "year", "month",
                 "place", "camera", "has", "tags", "subjects", "animals", "food", "vehicles",
                 "colors", "favorite", "rating")
SORTS = ("", "relevance", "newest", "oldest", "name", "quality", "shuffle", "album")
BY = ("", "words", "meaning")
FILTER_VALUES = 50          # values per filter group (site.js MAX_VALUES)
FILTER_VALUE_LENGTH = 200
OPTION_KEYS = ("filters", "sort", "by", "lib", "from", "to")
_DATE_KEY_RE = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")
# album:#<id> names an album by its id only (as search-core's findAlbum reads it).
_ID_TERM_RE = re.compile(r"^#\d{1,18}$")


class AlbumError(ValueError):
    """A change that cannot be made: a message, {field: problem} and an HTTP status."""

    def __init__(self, message, fields=None, status=400):
        ValueError.__init__(self, message)
        self.fields = dict(fields or {})
        self.status = status


def _chunks(ids):
    ids = list(ids)
    for start in range(0, len(ids), ID_CHUNK):
        yield ids[start:start + ID_CHUNK]


def _marks(count):
    return ",".join("?" * count)


# meta keys that count the changes of marks and albums: VERSION_KEY every
# change (the albums too), VERSION_KEY + ":" + a library id those of the
# marks and album places of that library's pictures (see data_state).
VERSION_KEY = "marks_version"


def _bump(conn, ids=()):
    """Count a change of the albums, and of the marks or album places of the
    pictures ids (in their libraries); no commit."""
    libraries = set()
    for part in _chunks(sorted(set(ids))):
        libraries.update(row[0] for row in conn.execute(
            "SELECT DISTINCT library_id FROM images WHERE id IN (%s)" % _marks(len(part)), part))
    for key in [VERSION_KEY] + ["%s:%s" % (VERSION_KEY, library_id)
                                for library_id in sorted(libraries)]:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, '0')", (key,))
        conn.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                     "WHERE key = ?", (key,))


def _storable(text, name, what):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise AlbumError("%s has a character that cannot be stored." % what,
                         {name: "A character that cannot be stored."})
    return text


# ---------------------------------------------------------------------------
# Marks

def _mark(row):
    return {"favorite": bool(row["favorite"]), "rating": int(row["rating"] or 0)}


def load_marks(conn, ids):
    """{image id: {"favorite", "rating"}} of those of ids that have marks."""
    result = {}
    for part in _chunks(ids):
        for row in conn.execute("SELECT image_id, favorite, rating FROM image_marks "
                                "WHERE image_id IN (%s)" % _marks(len(part)), part):
            result[row["image_id"]] = _mark(row)
    return result


def clean_rating(value):
    """A rating as sent (0 to 5 stars); raises AlbumError otherwise."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in RATINGS:
        raise AlbumError("A rating is a whole number from 0 (none) to 5.",
                         {"rating": "From 0 to 5."})
    return value


def set_marks(conn, ids, favorite=None, rating=None, now=None):
    """Make pictures favorites (or not) and rate them; no commit.

    favorite (True/False) and rating (0-5) change only when given. Missing
    pictures are skipped; a picture left with neither loses its row.
    Returns {"found", "changed", "marks": {image id: {"favorite",
    "rating"}} of every picture found}.
    """
    if favorite is not None and not isinstance(favorite, bool):
        raise AlbumError("favorite must be true or false.", {"favorite": "True or false."})
    if rating is not None:
        rating = clean_rating(rating)
    now = time.time() if now is None else now
    result = {"found": 0, "changed": 0, "marks": {}}
    changed = []
    for part in _chunks(ids):
        present = [row[0] for row in conn.execute(
            "SELECT id FROM images WHERE missing_since IS NULL AND id IN (%s)"
            % _marks(len(part)), part)]
        current = load_marks(conn, present)
        for image_id in present:
            result["found"] += 1
            before = current.get(image_id, {"favorite": False, "rating": 0})
            after = {"favorite": before["favorite"] if favorite is None else favorite,
                     "rating": before["rating"] if rating is None else rating}
            result["marks"][image_id] = after
            if after == before:
                continue
            result["changed"] += 1
            changed.append(image_id)
            if not after["favorite"] and not after["rating"]:
                conn.execute("DELETE FROM image_marks WHERE image_id = ?", (image_id,))
            else:
                # (Not an upsert: older SQLite versions do not have ON CONFLICT ... DO.)
                conn.execute("INSERT OR REPLACE INTO image_marks (image_id, favorite, rating, "
                             "updated) VALUES (?, ?, ?, ?)",
                             (image_id, int(after["favorite"]), after["rating"], now))
    if changed:
        _bump(conn, changed)
    return result


def for_libraries(conn, library_ids):
    """{image id: (favorite 0/1, rating, [album ids])} of the present pictures of some
    libraries that have marks or album places (two queries)."""
    library_ids = list(library_ids)
    if not library_ids:
        return {}
    where = ("i.missing_since IS NULL AND i.library_id IN (%s)" % _marks(len(library_ids)))
    result = {}
    for row in conn.execute("SELECT m.image_id, m.favorite, m.rating FROM image_marks AS m "
                            "JOIN images AS i ON i.id = m.image_id WHERE " + where,
                            library_ids):
        result[row[0]] = (1 if row[1] else 0, int(row[2] or 0), [])
    for row in conn.execute("SELECT a.image_id, a.album_id FROM album_items AS a "
                            "JOIN images AS i ON i.id = a.image_id WHERE " + where +
                            " ORDER BY a.image_id, a.album_id", library_ids):
        entry = result.get(row[0])
        if entry is None:
            entry = result[row[0]] = (0, 0, [])
        entry[2].append(row[1])
    return result


def picture_marks(conn, image_id):
    """(favorite 0/1, rating, [album ids]) of one picture."""
    row = conn.execute("SELECT favorite, rating FROM image_marks WHERE image_id = ?",
                       (image_id,)).fetchone()
    albums = [item[0] for item in conn.execute(
        "SELECT album_id FROM album_items WHERE image_id = ? ORDER BY album_id", (image_id,))]
    return (1 if row is not None and row[0] else 0, int(row[1] or 0) if row is not None else 0,
            albums)


def marked_ids(conn, library_id):
    """The ids of a library's rows (present or missing) that have marks or album places."""
    ids = {row[0] for row in conn.execute(
        "SELECT m.image_id FROM image_marks AS m JOIN images AS i ON i.id = m.image_id "
        "WHERE i.library_id = ?", (library_id,))}
    ids.update(row[0] for row in conn.execute(
        "SELECT DISTINCT a.image_id FROM album_items AS a JOIN images AS i "
        "ON i.id = a.image_id WHERE i.library_id = ?", (library_id,)))
    return ids


def any_marks(conn):
    """True when any picture has marks or an album place."""
    return (conn.execute("SELECT 1 FROM image_marks LIMIT 1").fetchone() is not None
            or conn.execute("SELECT 1 FROM album_items LIMIT 1").fetchone() is not None)


# ---------------------------------------------------------------------------
# The state the search index depends on

def data_state(conn, library_ids):
    """A JSON-friendly summary that changes whenever the marks or albums do.

    {"libraries": {library id: [marks, a sum over them, the sum of their
    times, album places, a sum over those, the sum of their times, and the
    count of the changes of them (_bump)]} of the present pictures of the
    libraries (by library id), "albums": [count, sums over the albums and
    the album places, the count of all changes]}. IndexService keys the
    records of a library by its entry (a mark changes no row of images) and
    the /api/index answer by the whole (it lists the albums). The counts
    of changes make every change count, whatever the sums and the clock
    say (two changes may leave the sums as they were, and a rename made
    within one tick of the clock after the album's last change keeps its
    time).
    """
    library_ids = list(library_ids)
    result = {"libraries": {}, "albums": []}
    versions = {row[0]: row[1] for row in conn.execute(
        "SELECT key, value FROM meta WHERE key = ? OR substr(key, 1, ?) = ?",
        (VERSION_KEY, len(VERSION_KEY) + 1, VERSION_KEY + ":"))}
    if library_ids:
        where = ("i.missing_since IS NULL AND i.library_id IN (%s)"
                 % _marks(len(library_ids)))
        entries = {}
        for row in conn.execute(
                "SELECT i.library_id, COUNT(*), TOTAL(m.image_id * (1 + m.favorite + "
                "2 * m.rating)), TOTAL(m.updated) FROM image_marks AS m "
                "JOIN images AS i ON i.id = m.image_id WHERE " + where +
                " GROUP BY i.library_id", library_ids):
            entries[row[0]] = [row[1], row[2], row[3], 0, 0, 0]
        for row in conn.execute(
                "SELECT i.library_id, COUNT(*), TOTAL(a.image_id * a.album_id), "
                "TOTAL(a.added) FROM album_items AS a JOIN images AS i ON i.id = a.image_id "
                "WHERE " + where + " GROUP BY i.library_id", library_ids):
            entry = entries.setdefault(row[0], [0, 0, 0, 0, 0, 0])
            entry[3], entry[4], entry[5] = row[1], row[2], row[3]
        for library_id in library_ids:
            version = versions.get("%s:%s" % (VERSION_KEY, library_id))
            if version is not None:
                entries.setdefault(library_id, [0, 0, 0, 0, 0, 0]).append(version)
        # (In a fixed order: the ETag is made from this as it is.)
        result["libraries"] = {library_id: entries[library_id] for library_id in sorted(entries)}
    albums = conn.execute("SELECT COUNT(*), TOTAL(id), TOTAL(updated), MAX(updated) "
                          "FROM albums").fetchone()
    items = conn.execute("SELECT COUNT(*), TOTAL(position * image_id), TOTAL(album_id * image_id) "
                         "FROM album_items").fetchone()
    result["albums"] = [value if value is not None else 0 for value in tuple(albums) + tuple(items)]
    result["albums"].append(versions.get(VERSION_KEY, "0"))
    return result


# ---------------------------------------------------------------------------
# Albums: checking what is sent

def clean_name(value):
    """An album name as sent: stripped, 1 to NAME_LENGTH characters; raises AlbumError."""
    if not isinstance(value, str):
        raise AlbumError("Give the album a name.", {"name": "Required."})
    name = " ".join(value.split())
    if not name:
        raise AlbumError("Give the album a name.", {"name": "Required."})
    if len(name) > NAME_LENGTH:
        raise AlbumError("An album name can have at most %d characters." % NAME_LENGTH,
                         {"name": "At most %d characters." % NAME_LENGTH})
    if _CONTROL_RE.search(name):
        raise AlbumError("An album name cannot hold control characters.",
                         {"name": "No control characters."})
    return _storable(name, "name", "The name")


def clean_query(value):
    """A smart album's search text as sent (at most QUERY_LENGTH characters)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AlbumError("The search of a smart album is text.", {"query": "Must be text."})
    query = value.strip()
    if len(query) > QUERY_LENGTH:
        raise AlbumError("The search can have at most %d characters." % QUERY_LENGTH,
                         {"query": "At most %d characters." % QUERY_LENGTH})
    if _CONTROL_RE.search(query.replace("\t", " ")):
        raise AlbumError("The search cannot hold control characters.",
                         {"query": "No control characters."})
    return _storable(query, "query", "The search")


def clean_options(value):
    """A smart album's options as sent: {filters: {group: [values]}, sort, by, lib, from, to}.

    Every key may be left out; the result has only those with a value.
    filters uses the groups of the search page's facets (FILTER_GROUPS);
    sort one of SORTS; by "words", "meaning" or "" (both); lib a library
    id ("" all); from and to the timeline's dates ("YYYY", "YYYY-MM" or
    "YYYY-MM-DD"). Raises AlbumError for anything else.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AlbumError("Send the options of a smart album as an object.",
                         {"options": "Must be an object."})
    unknown = sorted(key for key in value if key not in OPTION_KEYS)
    if unknown:
        raise AlbumError("Unknown option of a smart album: %s." % ", ".join(unknown),
                         {"options": "Unknown: %s." % ", ".join(unknown)})
    result = {}
    filters = value.get("filters")
    if filters not in (None, {}):
        if not isinstance(filters, dict):
            raise AlbumError("The filters are an object of lists.", {"options": "Bad filters."})
        clean = {}
        for group in sorted(filters):
            values = filters[group]
            if group not in FILTER_GROUPS:
                raise AlbumError("Unknown filter: %s." % group, {"options": "Bad filters."})
            if (not isinstance(values, list) or len(values) > FILTER_VALUES
                    or not all(isinstance(item, str) and 0 < len(item) <= FILTER_VALUE_LENGTH
                               for item in values)):
                raise AlbumError("The values of a filter are a list of at most %d words."
                                 % FILTER_VALUES, {"options": "Bad filters."})
            kept = []
            for item in values:
                _storable(item, "options", "A filter")
                if item not in kept:
                    kept.append(item)
            if kept:
                clean[group] = kept
        if clean:
            result["filters"] = clean
    for key, allowed in (("sort", SORTS), ("by", BY)):
        item = value.get(key)
        if item in (None, ""):
            continue
        if item not in allowed:
            raise AlbumError("%s must be one of %s." % (key, ", ".join(a for a in allowed if a)),
                             {"options": "Bad %s." % key})
        result[key] = item
    lib = value.get("lib")
    if lib not in (None, ""):
        if not isinstance(lib, str) or len(lib) > 100 or _CONTROL_RE.search(lib):
            raise AlbumError("lib is a library id.", {"options": "Bad lib."})
        result["lib"] = _storable(lib, "options", "The library")
    for key in ("from", "to"):
        item = value.get(key)
        if item in (None, ""):
            continue
        if not isinstance(item, str) or not _DATE_KEY_RE.match(item):
            raise AlbumError("%s is a date: YYYY, YYYY-MM or YYYY-MM-DD." % key,
                             {"options": "Bad %s." % key})
        result[key] = item
    return result


def _options_of(text):
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    try:
        return clean_options(value)
    except AlbumError:
        return {}


def _view(row, count=None):
    album = {"id": row["id"], "name": row["name"], "kind": row["kind"], "cover": row["cover"],
             "created": row["created"], "updated": row["updated"]}
    if row["kind"] == "smart":
        album["query"] = row["query"]
        album["options"] = _options_of(row["options"])
    if count is not None:
        album["count"] = count
    return album


_ALBUM_COLUMNS = "id, name, kind, query, options, cover, created, updated"


def _name_key(name):
    return name.casefold()


def _clash(conn, name, album_id=None):
    """The album other than album_id with the same name (any case), or None."""
    wanted = _name_key(name)
    for row in conn.execute("SELECT id, name FROM albums"):
        if row["id"] != album_id and _name_key(row["name"]) == wanted:
            return row
    return None


# ---------------------------------------------------------------------------
# Albums: reading

def get_album(conn, album_id):
    """One album (see list_albums, without the count), or None."""
    if isinstance(album_id, bool) or not isinstance(album_id, int) or not 0 < album_id < 2 ** 63:
        return None
    row = conn.execute("SELECT %s FROM albums WHERE id = ?" % _ALBUM_COLUMNS,
                       (album_id,)).fetchone()
    return _view(row) if row is not None else None


def find_album(conn, wanted):
    """The album named wanted (any case), or with that id (a number, its digits, or
    "#" and its digits); or None."""
    if isinstance(wanted, int) and not isinstance(wanted, bool):
        return get_album(conn, wanted)
    if not isinstance(wanted, str) or not wanted.strip():
        return None
    if _ID_TERM_RE.match(wanted.strip()):
        return get_album(conn, int(wanted.strip()[1:]))
    name = " ".join(wanted.split())
    clash = _clash(conn, name)
    if clash is not None:
        return get_album(conn, clash["id"])
    if wanted.strip().isdigit():
        return get_album(conn, int(wanted.strip()))
    return None


def list_albums(conn):
    """Every album, by name: [{id, name, kind, cover, created, updated, count, query and
    options (smart albums)}]. count: the present pictures of a manual album."""
    counts = {row[0]: row[1] for row in conn.execute(
        "SELECT a.album_id, COUNT(*) FROM album_items AS a JOIN images AS i "
        "ON i.id = a.image_id WHERE i.missing_since IS NULL GROUP BY a.album_id")}
    rows = conn.execute("SELECT %s FROM albums" % _ALBUM_COLUMNS).fetchall()
    rows.sort(key=lambda row: (_name_key(row["name"]), row["id"]))
    return [_view(row, counts.get(row["id"], 0) if row["kind"] == "manual" else None)
            for row in rows]


def album_items(conn, album_id, library_ids=None, include_hidden=True):
    """The ids of a manual album's present pictures, in the album's order.

    library_ids (None: all) limits them to some libraries; include_hidden
    False leaves hidden pictures out.
    """
    sql = ("SELECT a.image_id FROM album_items AS a JOIN images AS i ON i.id = a.image_id "
           "WHERE a.album_id = ? AND i.missing_since IS NULL")
    params = [album_id]
    if library_ids is not None:
        library_ids = list(library_ids)
        if not library_ids:
            return []
        sql += " AND i.library_id IN (%s)" % _marks(len(library_ids))
        params += library_ids
    if not include_hidden:
        sql += " AND i.hidden = 0"
    sql += " ORDER BY a.position, a.added, a.image_id"
    return [row[0] for row in conn.execute(sql, params)]


def smart_in_libraries(options, library_ids):
    """False when a smart album's saved search (its options) is limited to libraries
    none of which are among library_ids (by its library choice, or its Library filter)."""
    lib = options.get("lib")
    if lib and lib not in library_ids:
        return False
    chosen = (options.get("filters") or {}).get("library") or []
    return not chosen or any(item in library_ids for item in chosen)


def people_searches_shown(config, include_hidden):
    """True when the /api/index answer lists the smart albums that search for a person
    (person:, People): for the admin (include_hidden), and for others only while
    config.json's "people" shows them people ("enabled" and "visitors" both true, as
    people_groups.may_see has it). A person's name is in such a search."""
    if include_hidden:
        return True
    raw = config.get("people") if isinstance(config, dict) else None
    return isinstance(raw, dict) and raw.get("enabled") is True and raw.get("visitors") is True


def searches_person(query):
    """True when a search (a smart album's) has a person: term."""
    return any(term["field"] == "person" for term in query_terms(query))


def index_albums(conn, library_ids, include_hidden=True, person_searches=True):
    """The albums as the /api/index answer lists them, by name.

    [{id, name, kind, cover, items (manual: the ids of its present pictures
    in the libraries, in order), query and options (smart)}]. cover is the
    chosen picture when it is among those the answer holds, else null (the
    page shows the first one). Hidden pictures only with include_hidden.
    Without include_hidden (anyone but the admin) an album is listed only
    when it can show something there: a manual album with a picture in the
    answer, a smart album not limited to other (say disabled) libraries.
    Without person_searches (see people_searches_shown) the smart albums
    whose search names a person (person:) are left out.
    """
    library_ids = list(library_ids)
    rows = conn.execute("SELECT %s FROM albums" % _ALBUM_COLUMNS).fetchall()
    rows.sort(key=lambda row: (_name_key(row["name"]), row["id"]))
    items = {}
    if library_ids:
        sql = ("SELECT a.album_id, a.image_id FROM album_items AS a JOIN images AS i "
               "ON i.id = a.image_id WHERE i.missing_since IS NULL AND i.library_id IN (%s)%s "
               "ORDER BY a.album_id, a.position, a.added, a.image_id"
               % (_marks(len(library_ids)), "" if include_hidden else " AND i.hidden = 0"))
        for row in conn.execute(sql, library_ids):
            items.setdefault(row[0], []).append(row[1])
    shown = set()
    for ids in items.values():
        shown.update(ids)
    cover_ok = {}
    covers = [row["cover"] for row in rows if row["cover"] is not None]
    for part in _chunks(sorted(set(covers))):
        for row in conn.execute(
                "SELECT id FROM images WHERE missing_since IS NULL AND id IN (%s)%s%s"
                % (_marks(len(part)), " AND library_id IN (%s)" % _marks(len(library_ids))
                   if library_ids else "", "" if include_hidden else " AND hidden = 0"),
                part + library_ids):
            cover_ok[row[0]] = True
    result = []
    for row in rows:
        album = {"id": row["id"], "name": row["name"], "kind": row["kind"],
                 "cover": row["cover"] if cover_ok.get(row["cover"]) else None}
        if row["kind"] == "manual":
            album["items"] = items.get(row["id"], [])
            if not include_hidden and not album["items"]:
                continue        # (only hidden pictures, or those of other libraries)
        else:
            album["query"] = row["query"]
            album["options"] = _options_of(row["options"])
            if not include_hidden and not smart_in_libraries(album["options"], library_ids):
                continue
            if not person_searches and searches_person(row["query"]):
                continue        # (a person's name: People is not shown here)
        result.append(album)
    return result


# ---------------------------------------------------------------------------
# Albums: changing (no commit; the caller commits)

def _present_ids(conn, ids):
    """Of ids (in order, without repeats), those of present pictures."""
    seen = []
    known = set()
    for image_id in ids:
        if image_id not in known:
            known.add(image_id)
            seen.append(image_id)
    present = set()
    for part in _chunks(seen):
        present.update(row[0] for row in conn.execute(
            "SELECT id FROM images WHERE missing_since IS NULL AND id IN (%s)"
            % _marks(len(part)), part))
    return [image_id for image_id in seen if image_id in present]


def _touch(conn, album_id, now):
    conn.execute("UPDATE albums SET updated = ? WHERE id = ?", (now, album_id))


def _manual(conn, album_id):
    album = get_album(conn, album_id)
    if album is None:
        raise AlbumError("There is no album %s." % album_id, status=404)
    if album["kind"] != "manual":
        raise AlbumError("%s is a smart album: it shows the pictures its search finds, so "
                         "pictures cannot be put in it or taken out." % album["name"],
                         {"album": "A smart album."}, status=409)
    return album


def create_album(conn, name, kind="manual", query="", options=None, ids=(), now=None):
    """Make an album; returns it (see list_albums). ids: pictures to put in a manual one."""
    name = clean_name(name)
    if kind not in KINDS:
        raise AlbumError("kind must be manual or smart.", {"kind": "manual or smart."})
    query = clean_query(query) if kind == "smart" else ""
    options = clean_options(options) if kind == "smart" else {}
    if kind == "smart" and not query and not options.get("filters") and not (
            options.get("from") or options.get("to") or options.get("lib")):
        raise AlbumError("A smart album needs a search: type words or choose filters first.",
                         {"query": "Required."})
    if kind == "smart" and ids:
        raise AlbumError("A smart album holds the pictures its search finds; it takes no "
                         "pictures of its own.", {"ids": "Not for a smart album."})
    if _clash(conn, name) is not None:
        raise AlbumError("There is an album called %s already." % name,
                         {"name": "Taken."}, status=409)
    if conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] >= ALBUM_LIMIT:
        raise AlbumError("There can be at most %d albums." % ALBUM_LIMIT, status=409)
    now = time.time() if now is None else now
    album_id = conn.execute(
        "INSERT INTO albums (name, kind, query, options, cover, created, updated) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
        (name, kind, query, json.dumps(options, ensure_ascii=False, sort_keys=True),
         now, now)).lastrowid
    _bump(conn)
    if ids:
        add_items(conn, album_id, ids, now)
    album = get_album(conn, album_id)
    if kind == "manual":
        album["count"] = len(album_items(conn, album_id))
    return album


def update_album(conn, album_id, changes, now=None):
    """Change an album's name, cover, or (smart) query and options; returns it.

    changes: {name?, cover? (a picture id, or null for the first one),
    query?, options?}; only the keys sent change.
    """
    if not isinstance(changes, dict):
        raise AlbumError("Send the changes as an object.")
    unknown = sorted(key for key in changes if key not in ("name", "cover", "query", "options"))
    if unknown:
        raise AlbumError("Unknown field: %s." % ", ".join(unknown),
                         {key: "Not a field of an album." for key in unknown})
    album = get_album(conn, album_id)
    if album is None:
        raise AlbumError("There is no album %s." % album_id, status=404)
    now = time.time() if now is None else now
    sets, params = [], []
    if "name" in changes:
        name = clean_name(changes["name"])
        if _clash(conn, name, album_id) is not None:
            raise AlbumError("There is an album called %s already." % name,
                             {"name": "Taken."}, status=409)
        sets.append("name = ?")
        params.append(name)
    if "cover" in changes:
        cover = changes["cover"]
        if cover is not None:
            if isinstance(cover, bool) or not isinstance(cover, int) or cover < 1:
                raise AlbumError("cover is a picture id (or null).", {"cover": "A picture id."})
            if not _present_ids(conn, [cover]):
                raise AlbumError("There is no picture %d." % cover, {"cover": "Unknown."},
                                 status=404)
            if album["kind"] == "manual" and conn.execute(
                    "SELECT 1 FROM album_items WHERE album_id = ? AND image_id = ?",
                    (album_id, cover)).fetchone() is None:
                raise AlbumError("The cover must be one of the album's pictures.",
                                 {"cover": "Not in the album."})
        sets.append("cover = ?")
        params.append(cover)
    if "query" in changes or "options" in changes:
        if album["kind"] != "smart":
            raise AlbumError("Only a smart album has a search.", {"query": "A manual album."})
        query = clean_query(changes["query"]) if "query" in changes else album["query"]
        options = (clean_options(changes["options"]) if "options" in changes
                   else album["options"])
        if not query and not options.get("filters") and not (
                options.get("from") or options.get("to") or options.get("lib")):
            raise AlbumError("A smart album needs a search: words or filters.",
                             {"query": "Required."})
        sets += ["query = ?", "options = ?"]
        params += [query, json.dumps(options, ensure_ascii=False, sort_keys=True)]
    if sets:
        conn.execute("UPDATE albums SET %s, updated = ? WHERE id = ?" % ", ".join(sets),
                     params + [now, album_id])
        _bump(conn)
    return get_album(conn, album_id)


def delete_album(conn, album_id):
    """Delete an album (its pictures stay); returns how many pictures it held."""
    album = get_album(conn, album_id)
    if album is None:
        raise AlbumError("There is no album %s." % album_id, status=404)
    held = [row[0] for row in conn.execute("SELECT image_id FROM album_items WHERE album_id = ?",
                                           (album_id,))]
    conn.execute("DELETE FROM album_items WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    _bump(conn, held)
    return len(held)


def add_items(conn, album_id, ids, now=None):
    """Put pictures at the end of a manual album, in the order given; returns
    {"added", "found"} (pictures already in it keep their place)."""
    _manual(conn, album_id)
    now = time.time() if now is None else now
    present = _present_ids(conn, ids)
    have = set()
    for part in _chunks(present):
        have.update(row[0] for row in conn.execute(
            "SELECT image_id FROM album_items WHERE album_id = ? AND image_id IN (%s)"
            % _marks(len(part)), [album_id] + part))
    new = [image_id for image_id in present if image_id not in have]
    count = conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id = ?",
                         (album_id,)).fetchone()[0]
    if count + len(new) > ITEMS_LIMIT:
        raise AlbumError("An album can hold at most %d pictures." % ITEMS_LIMIT, status=409)
    last = conn.execute("SELECT MAX(position) FROM album_items WHERE album_id = ?",
                        (album_id,)).fetchone()[0] or 0
    conn.executemany("INSERT INTO album_items (album_id, image_id, position, added) "
                     "VALUES (?, ?, ?, ?)",
                     [(album_id, image_id, last + number, now)
                      for number, image_id in enumerate(new, 1)])
    if new:
        _touch(conn, album_id, now)
        _bump(conn, new)
    return {"added": len(new), "found": len(present)}


def remove_items(conn, album_id, ids, now=None):
    """Take pictures out of a manual album; returns how many were in it."""
    album = _manual(conn, album_id)
    now = time.time() if now is None else now
    removed = 0
    ids = list(ids)
    for part in _chunks(ids):
        removed += conn.execute("DELETE FROM album_items WHERE album_id = ? AND image_id IN (%s)"
                                % _marks(len(part)), [album_id] + part).rowcount
    if removed:
        if album["cover"] in ids:
            conn.execute("UPDATE albums SET cover = NULL WHERE id = ?", (album_id,))
        _touch(conn, album_id, now)
        _bump(conn, ids)
    return removed


def _order(conn, album_id):
    return [row[0] for row in conn.execute(
        "SELECT image_id FROM album_items WHERE album_id = ? ORDER BY position, added, image_id",
        (album_id,))]


def _write_order(conn, album_id, order, now):
    conn.executemany("UPDATE album_items SET position = ? WHERE album_id = ? AND image_id = ?",
                     [(number, album_id, image_id) for number, image_id in enumerate(order, 1)])
    _touch(conn, album_id, now)
    _bump(conn)


def move_items(conn, album_id, ids, before=None, now=None):
    """Move pictures of a manual album (in the order given) to just before picture
    before (None: to the end); returns the album's new order (all its pictures)."""
    _manual(conn, album_id)
    now = time.time() if now is None else now
    order = _order(conn, album_id)
    inside = set(order)
    moving = []
    chosen = set()
    for image_id in ids:
        if image_id in inside and image_id not in chosen:
            chosen.add(image_id)
            moving.append(image_id)
    if before is not None and before not in inside:
        raise AlbumError("before must be a picture of the album (or null for the end).",
                         {"before": "Not in the album."})
    if before in chosen:
        raise AlbumError("A picture cannot move before itself.", {"before": "Moved too."})
    rest = [image_id for image_id in order if image_id not in chosen]
    at = rest.index(before) if before is not None else len(rest)
    new = rest[:at] + moving + rest[at:]
    if new != order:
        _write_order(conn, album_id, new, now)
    return new


def set_order(conn, album_id, ids, now=None):
    """Put a manual album's pictures in the order of ids; those it leaves out
    follow in their old order. Returns the new order."""
    _manual(conn, album_id)
    now = time.time() if now is None else now
    order = _order(conn, album_id)
    inside = set(order)
    first = []
    chosen = set()
    for image_id in ids:
        if image_id in inside and image_id not in chosen:
            chosen.add(image_id)
            first.append(image_id)
    new = first + [image_id for image_id in order if image_id not in chosen]
    if new != order:
        _write_order(conn, album_id, new, now)
    return new


# ---------------------------------------------------------------------------
# A smart album's search, read as static/search-core.js reads it (parseText,
# findAlbum), for an export that publishes it (export.py)

# search-core's FIELD_NAMES: the field names (and their other spellings)
# a "name:value" term may start with.
FIELD_NAMES = {
    "tag": "tag", "tags": "tag", "subject": "subject", "subjects": "subject",
    "title": "title", "text": "text", "ocr": "text", "description": "description",
    "desc": "description", "scene": "scene", "setting": "setting", "event": "event",
    "animal": "animal", "animals": "animal", "food": "food", "vehicle": "vehicle",
    "vehicles": "vehicle", "color": "color", "colour": "color", "colors": "color",
    "colours": "color", "camera": "camera", "folder": "folder", "file": "file",
    "filename": "file", "type": "type", "people": "people", "year": "year",
    "before": "before", "after": "after", "lib": "lib", "library": "lib", "has": "has",
    "is": "is", "place": "place", "places": "place", "on": "on", "similar": "similar",
    "like": "similar", "look": "look", "looks": "look", "rating": "rating",
    "rated": "rating", "stars": "rating", "album": "album", "albums": "album",
    "person": "person",
}
QUOTE_CHARS = "\"\u201c\u201d\u201e"
_FIELD_TERM_RE = re.compile(r"([A-Za-z]{1,12}):")
_SPACE_RE = re.compile(r"\s")
_APOSTROPHE_RE = re.compile("['`\u00b4\u2018\u2019\u201b\u02bc\u2032]")
_SPECIAL_LETTERS = {"\u00df": "ss", "\u00e6": "ae", "\u0153": "oe", "\u00f8": "o",
                    "\u0111": "d", "\u00f0": "d", "\u0142": "l", "\u00fe": "th",
                    "\u0131": "i", "\u0127": "h"}


def query_terms(text):
    """The field terms of a search ("name:value") as search-core reads them.

    [{field (the field's own name, see FIELD_NAMES), value, neg, quoted,
    start, end (of the whole term), value_start, value_stop (of the value
    in text)}], in order. Plain words are left out. Unlike search-core,
    terms after its 40th are read too (an export checks every one).
    """
    text = text if isinstance(text, str) else ""
    terms = []
    length = len(text)
    index = 0
    while index < length:
        if _SPACE_RE.match(text, index):
            index += 1
            continue
        start = index
        neg = False
        if text[index] == "-":
            after = index
            while after < length and text[after] == "-":
                after += 1
            if after >= length or _SPACE_RE.match(text, after):
                index = after                   # (a lone minus)
                continue
            neg = True
            index = after
        match = _FIELD_TERM_RE.match(text, index, min(length, index + 14))
        field = FIELD_NAMES.get(match.group(1).lower()) if match else None
        if field:
            index = match.end()
        quoted = index < length and text[index] in QUOTE_CHARS
        if quoted:
            stop = index + 1
            while stop < length and text[stop] not in QUOTE_CHARS:
                stop += 1
            value_start, value_stop = index + 1, stop
            index = stop + 1 if stop < length else stop
        else:
            stop = index
            while stop < length and not _SPACE_RE.match(text, stop):
                stop += 1
            value_start, value_stop = index, stop
            index = stop
        value = text[value_start:value_stop]
        if field and value.strip():
            terms.append({"field": field, "value": value, "neg": neg, "quoted": quoted,
                          "start": start, "end": index, "value_start": value_start,
                          "value_stop": value_stop})
    return terms


def album_key(text):
    """An album name as album: compares it (search-core's albumKey): lower case,
    without accents, apostrophes and quotes, single spaces."""
    value = _APOSTROPHE_RE.sub("", str(text).lower())
    value = "".join(char for char in unicodedata.normalize("NFKD", value)
                    if not unicodedata.combining(char))
    value = "".join(_SPECIAL_LETTERS.get(char, char) for char in value.lower())
    value = _APOSTROPHE_RE.sub("", value)
    for quote in QUOTE_CHARS:
        value = value.replace(quote, "")
    return " ".join(value.split())


def find_listed(albums, value):
    """The album of albums ([{id, name, ...}]) that an album: value names, as
    search-core's findAlbum finds it: #<id> by its id only; else by its name
    (album_key), then by its id, then the only one whose name starts so. None
    when there is none (or more than one starts so)."""
    value = str(value).strip()
    if _ID_TERM_RE.match(value):
        return next((album for album in albums if str(album["id"]) == value[1:]), None)
    wanted = album_key(value)
    if not wanted:
        return None
    for album in albums:
        if album_key(album["name"]) == wanted:
            return album
    for album in albums:
        if str(album["id"]) == value:
            return album
    starts = [album for album in albums if album_key(album["name"]).startswith(wanted)]
    return starts[0] if len(starts) == 1 else None


# ---------------------------------------------------------------------------
# Marks and album places follow their pictures (moves, renames, swaps)

def reassign(conn, plan, now=None):
    """Hand marks and album places on to the pictures their content went to; no commit.

    plan is image_edits.reassign's: {from id: (to ids, best first; copy)}.
    Marks (favorite, rating) go by the rules of edits (image_edits.choose_heirs):
    to the first of the to ids without marks of its own (or whose own move
    on too) that no other picture's take; with copy the from id keeps them
    too. Album places go to the picture that took the marks, or (when
    there were none to take) to the first of the to ids: it joins each
    album of the from id it is not in yet, at the from id's place; without
    copy the from id leaves them. Returns {from id: to id} of the pictures
    whose marks or album places were handed on.
    """
    now = time.time() if now is None else now
    ids = set(plan)
    for targets, _copy in plan.values():
        ids.update(targets)
    ids = sorted(ids)
    marks = load_marks(conn, ids)
    chosen = image_edits.choose_heirs(plan, marks)
    for source in chosen:
        if not plan[source][1]:
            conn.execute("DELETE FROM image_marks WHERE image_id = ?", (source,))
    for source, target in sorted(chosen.items()):
        mark = marks[source]
        conn.execute("INSERT OR REPLACE INTO image_marks (image_id, favorite, rating, updated) "
                     "VALUES (?, ?, ?, ?)",
                     (target, int(mark["favorite"]), mark["rating"], now))
    handed = dict(chosen)
    if chosen:
        _bump(conn, ids)
    # Album places: worked out from what every picture of the plan holds
    # now, so two files that swapped names swap their albums.
    places = {}
    for part in _chunks(ids):
        for row in conn.execute("SELECT album_id, image_id, position, added FROM album_items "
                                "WHERE image_id IN (%s)" % _marks(len(part)), part):
            places.setdefault(row["image_id"], {})[row["album_id"]] = (row["position"],
                                                                       row["added"])
    heirs = {}
    for source in sorted(plan):
        targets, _copy = plan[source]
        if not places.get(source):
            continue
        target = chosen.get(source)
        if target is None:
            target = next((item for item in targets if item != source), None)
        if target is not None:
            heirs[source] = target
    if not heirs:
        return handed
    final = {}
    for image_id in ids:
        leaving = image_id in heirs and not plan[image_id][1]
        final[image_id] = {} if leaving else dict(places.get(image_id, {}))
    for source, target in sorted(heirs.items()):
        for album_id, place in places[source].items():
            final[target].setdefault(album_id, place)
        handed.setdefault(source, target)
    touched_albums = set()
    for image_id in ids:
        before = places.get(image_id, {})
        after = final[image_id]
        if before == after:
            continue
        for album_id in set(before) | set(after):
            touched_albums.add(album_id)
        conn.execute("DELETE FROM album_items WHERE image_id = ?", (image_id,))
        conn.executemany("INSERT INTO album_items (album_id, image_id, position, added) "
                         "VALUES (?, ?, ?, ?)",
                         [(album_id, image_id, place[0], place[1])
                          for album_id, place in sorted(after.items())])
    if touched_albums:
        _bump(conn, ids)
    for album_id in sorted(touched_albums):
        # (A cover that moved on is the picture that took its place.)
        row = conn.execute("SELECT cover FROM albums WHERE id = ?", (album_id,)).fetchone()
        if row is not None and row["cover"] in heirs and not plan[row["cover"]][1]:
            conn.execute("UPDATE albums SET cover = ? WHERE id = ?",
                         (heirs[row["cover"]], album_id))
        _touch(conn, album_id, now)
    return handed


def give(conn, from_id, to_ids, copy=False, now=None):
    """Hand picture from_id's marks and album places to the first of to_ids that can
    take them (see reassign); no commit. Returns the id that took them, or None."""
    return reassign(conn, {from_id: (list(to_ids), copy)}, now).get(from_id)
