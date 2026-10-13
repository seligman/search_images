#!/usr/bin/env python3
"""The owner's own titles, descriptions and tags for pictures (the image_edits table).

What the describing model wrote stays in images.info, untouched; an edit
is kept beside it and laid over it wherever a description is shown,
searched or exported (apply). Describing a picture again never reads or
changes its edit, so the owner's words survive a new model or prompt.

One row per edited picture (db.SCHEMA_V3):

    title, description   the owner's text, or NULL: the model's is used
    tags_add             tags the owner added (JSON list, in the owner's
                         order); they come first and are searched like
                         any other tag -- the way to teach the search a
                         name, such as a pet's
    tags_remove          the model's tags the owner took away (JSON list)
    updated              when the edit was last saved

A row whose edit is empty is deleted, and a picture's row takes its edit
along when it goes (ON DELETE CASCADE). Saving an edit also touches the
picture's images.updated, so the search index notices the change.

Tags are compared without regard to case or repeated spaces, and kept in
lower case, as the search records keep them. This module uses only the
standard library.
"""

import json
import time

import prompts

# Longest title and description (characters) the owner may write; the
# model's own limits are lower (prompts.CORE_FIELDS).
TITLE_LENGTH = 200
DESCRIPTION_LENGTH = 4000
# Longest tag (as the model's list items), and most tags in each list.
TAG_LENGTH = prompts.ITEM_LENGTH
TAGS_LIMIT = 50
# The keys of an edit, as the API sends and receives them.
FIELDS = ("title", "description", "tags_add", "tags_remove")
ID_CHUNK = 500              # ids per SQL statement (older SQLite allows 999 parameters)


class EditError(ValueError):
    """An edit that cannot be saved: a message, and {field: problem}."""

    def __init__(self, message, fields=None):
        ValueError.__init__(self, message)
        self.fields = dict(fields or {})


def empty():
    """An edit that changes nothing."""
    return {"title": None, "description": None, "tags_add": [], "tags_remove": []}


def tag_key(tag):
    """What two tags are compared by: lower case, single spaces."""
    return " ".join(str(tag).split()).casefold()


def _storable(text, name, what):
    """text, or EditError when it holds what cannot be stored (a lone UTF-16 surrogate)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise EditError("%s has a character that cannot be stored." % what,
                        {name: "A character that cannot be stored."})
    return text


def _clean_text(value, limit, name):
    """A title or description as sent: text (stripped), or None for "" / null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise EditError("%s must be text (or null to use the model's)." % name.capitalize(),
                        {name: "Must be text."})
    text = _storable(value.strip(), name, "The " + name)
    if len(text) > limit:
        raise EditError("The %s is too long (at most %d characters)." % (name, limit),
                        {name: "At most %d characters." % limit})
    return text or None


def clean_tags(value, name="tags"):
    """A list of tags as sent: lower case, single spaces, no repeats, in order.

    None is an empty list. Raises EditError for anything but a list of
    text, a tag longer than TAG_LENGTH, or more than TAGS_LIMIT tags.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise EditError("Send %s as a list of words." % name, {name: "A list of words."})
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise EditError("Tags are text.", {name: "Tags are text."})
        tag = _storable(" ".join(item.split()).lower(), name, "A tag")
        if not tag:
            continue
        if len(tag) > TAG_LENGTH:
            raise EditError("A tag can have at most %d characters." % TAG_LENGTH,
                            {name: "At most %d characters per tag." % TAG_LENGTH})
        key = tag_key(tag)
        if key not in seen:
            seen.add(key)
            result.append(tag)
    if len(result) > TAGS_LIMIT:
        raise EditError("At most %d tags at a time." % TAGS_LIMIT,
                        {name: "At most %d tags." % TAGS_LIMIT})
    return result


def _without(tags, others):
    """tags without those that are in others (compared by tag_key)."""
    drop = {tag_key(tag) for tag in others}
    return [tag for tag in tags if tag_key(tag) not in drop]


def normalize(edit):
    """A tidy copy of an edit: removed tags that were also added are not removed."""
    edit = edit or {}
    result = empty()
    for name in ("title", "description"):
        value = edit.get(name)
        result[name] = (value.strip() or None) if isinstance(value, str) else None
    result["tags_add"] = clean_tags(edit.get("tags_add") or [], "tags_add")
    result["tags_remove"] = _without(clean_tags(edit.get("tags_remove") or [], "tags_remove"),
                                     result["tags_add"])
    return result


def is_empty(edit):
    """True when an edit changes nothing."""
    return not edit or not (edit.get("title") or edit.get("description")
                            or edit.get("tags_add") or edit.get("tags_remove"))


def _tag_list(text):
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()] \
        if isinstance(value, list) else []


def _from_row(row):
    return {"title": row["title"] or None, "description": row["description"] or None,
            "tags_add": _tag_list(row["tags_add"]), "tags_remove": _tag_list(row["tags_remove"]),
            "updated": row["updated"]}


_COLUMNS = "image_id, title, description, tags_add, tags_remove, updated"


def load(conn, image_id):
    """The edit of one picture (with its "updated" time), or None when it has none."""
    row = conn.execute("SELECT %s FROM image_edits WHERE image_id = ?" % _COLUMNS,
                       (image_id,)).fetchone()
    return _from_row(row) if row is not None else None


def load_many(conn, ids):
    """{image id: edit} of those of ids that have one."""
    ids = list(ids)
    result = {}
    for start in range(0, len(ids), ID_CHUNK):
        part = ids[start:start + ID_CHUNK]
        for row in conn.execute("SELECT %s FROM image_edits WHERE image_id IN (%s)"
                                % (_COLUMNS, ",".join("?" * len(part))), part):
            result[row["image_id"]] = _from_row(row)
    return result


def for_libraries(conn, library_ids):
    """{image id: edit} of the present pictures of some libraries (one query)."""
    library_ids = list(library_ids)
    if not library_ids:
        return {}
    sql = ("SELECT e.image_id, e.title, e.description, e.tags_add, e.tags_remove, e.updated "
           "FROM image_edits AS e JOIN images AS i ON i.id = e.image_id "
           "WHERE i.missing_since IS NULL AND i.library_id IN (%s)"
           % ",".join("?" * len(library_ids)))
    return {row["image_id"]: _from_row(row) for row in conn.execute(sql, library_ids)}


def effective_tags(model_tags, edit):
    """The tags shown and searched: the owner's first, then the model's that were not removed."""
    added = list((edit or {}).get("tags_add") or [])
    removed = (edit or {}).get("tags_remove") or []
    model = [tag for tag in (model_tags if isinstance(model_tags, list) else [])
             if isinstance(tag, str) and tag.strip()]
    result = []
    seen = set()
    drop = {tag_key(tag) for tag in removed}
    for tag in added + model:
        key = tag_key(tag)
        if key in seen or (key in drop and tag not in added):
            continue
        seen.add(key)
        result.append(tag)
    return result


def apply(info, edit):
    """The description to show and search: info (the model's, a dict or None) with edit laid over it.

    The title and description are replaced when the edit has them; the
    tags follow effective_tags. info itself is not changed; without an
    edit it is returned as it is.
    """
    if is_empty(edit):
        return info
    result = dict(info) if isinstance(info, dict) else {}
    for name in ("title", "description"):
        if edit.get(name):
            result[name] = edit[name]
    if edit.get("tags_add") or edit.get("tags_remove"):
        result["tags"] = effective_tags(result.get("tags"), edit)
    return result


def view(edit):
    """An edit as the API sends it: every key of FIELDS, plus "updated" (or None)."""
    result = empty()
    if edit:
        for name in FIELDS:
            if edit.get(name):
                result[name] = list(edit[name]) if name.startswith("tags") else edit[name]
    result["updated"] = (edit or {}).get("updated")
    return result


def merge(current, changes):
    """current (an edit or None) changed by a request's changes; raises EditError.

    Only the keys present in changes change; null clears that part ("" too,
    for the title and description).
    """
    if not isinstance(changes, dict):
        raise EditError("Send the changes as an object.")
    unknown = sorted(key for key in changes if key not in FIELDS)
    if unknown:
        raise EditError("Unknown field: %s." % ", ".join(unknown),
                        {key: "Not a field of an edit." for key in unknown})
    result = normalize(current)
    if "title" in changes:
        result["title"] = _clean_text(changes["title"], TITLE_LENGTH, "title")
    if "description" in changes:
        result["description"] = _clean_text(changes["description"], DESCRIPTION_LENGTH,
                                            "description")
    for name in ("tags_add", "tags_remove"):
        if name in changes:
            result[name] = clean_tags(changes[name], name)
    result["tags_remove"] = _without(result["tags_remove"], result["tags_add"])
    return result


def _write(conn, image_id, edit, now):
    """Store (or delete, when empty) one picture's edit and touch its row; no commit."""
    if is_empty(edit):
        conn.execute("DELETE FROM image_edits WHERE image_id = ?", (image_id,))
    else:
        # (Not an upsert: older SQLite versions do not have ON CONFLICT ... DO.)
        conn.execute(
            "INSERT OR REPLACE INTO image_edits (image_id, title, description, tags_add, "
            "tags_remove, updated) VALUES (?, ?, ?, ?, ?, ?)",
            (image_id, edit["title"], edit["description"],
             json.dumps(edit["tags_add"], ensure_ascii=False),
             json.dumps(edit["tags_remove"], ensure_ascii=False), now))
    # The search index notices changes by the rows' "updated" sums.
    conn.execute("UPDATE images SET updated = ? WHERE id = ?", (now, image_id))


def save(conn, image_id, changes, now=None):
    """Change one picture's edit (see merge) and store it; no commit.

    A tag added that the model's tags already have is not kept as the
    owner's (it only stops being removed). Returns the stored edit
    (view()), or None when it is now empty and was deleted.
    """
    now = time.time() if now is None else now
    edit = merge(load(conn, image_id), changes)
    row = conn.execute("SELECT info FROM images WHERE id = ?", (image_id,)).fetchone()
    model_keys = {tag_key(tag) for tag in _model_tags(row["info"] if row else "")}
    edit["tags_add"] = [tag for tag in edit["tags_add"] if tag_key(tag) not in model_keys]
    _write(conn, image_id, edit, now)
    if is_empty(edit):
        return None
    edit["updated"] = now
    return edit


def _model_tags(info_text):
    try:
        info = json.loads(info_text or "{}", parse_constant=lambda _name: None)
    except (TypeError, ValueError):
        return []
    tags = info.get("tags") if isinstance(info, dict) else None
    return [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []


def change_tags(conn, ids, add=(), remove=(), now=None):
    """Add and remove tags on many pictures at once; no commit.

    add: tags each picture gets (as the owner's; one the owner had removed
    comes back, and one the model's tags have is only no longer removed).
    remove: tags each picture loses -- taken from the owner's added tags,
    and noted as removed only where the model's tags have it (so pictures
    that never had it are left alone). A tag in both lists is added.
    Missing pictures are skipped.
    Returns {"found", "changed", "tags": {image id: effective tags} of the
    changed pictures, "edited": {image id: True/False} of the same}.
    """
    now = time.time() if now is None else now
    add = clean_tags(list(add), "add")
    remove = _without(clean_tags(list(remove), "remove"), add)
    ids = list(ids)
    result = {"found": 0, "changed": 0, "tags": {}, "edited": {}}
    for start in range(0, len(ids), ID_CHUNK):
        part = ids[start:start + ID_CHUNK]
        rows = conn.execute("SELECT id, info FROM images WHERE missing_since IS NULL AND "
                            "id IN (%s)" % ",".join("?" * len(part)), part).fetchall()
        current = load_many(conn, [row["id"] for row in rows])
        for row in rows:
            result["found"] += 1
            before = normalize(current.get(row["id"]))
            model = _model_tags(row["info"])
            model_keys = {tag_key(tag) for tag in model}
            edit = normalize(before)
            edit["tags_add"] = _without(edit["tags_add"], remove)
            for tag in remove:
                if tag_key(tag) in model_keys and tag_key(tag) not in {
                        tag_key(item) for item in edit["tags_remove"]}:
                    edit["tags_remove"].append(tag)
            edit["tags_remove"] = _without(edit["tags_remove"], add)
            added_keys = {tag_key(item) for item in edit["tags_add"]}
            for tag in add:
                if tag_key(tag) not in added_keys and tag_key(tag) not in model_keys:
                    edit["tags_add"].append(tag)
                    added_keys.add(tag_key(tag))
            if len(edit["tags_add"]) > TAGS_LIMIT or len(edit["tags_remove"]) > TAGS_LIMIT:
                raise EditError("A picture can have at most %d added (or removed) tags."
                                % TAGS_LIMIT)
            if edit == before:
                continue
            _write(conn, row["id"], edit, now)
            result["changed"] += 1
            result["tags"][row["id"]] = effective_tags(model, edit)
            result["edited"][row["id"]] = not is_empty(edit)
    return result


def choose_heirs(plan, owners):
    """{from id: to id}: where the data of plan's pictures goes (see reassign).

    plan is reassign's; owners holds the ids (of its pictures) that have
    data of their own. Each source that has data goes to the first of its
    to ids that has none -- or whose own moves on as well -- and that no
    other source takes. A target whose own data finds nowhere to go keeps
    it; the data meant for it stays where it was (albums.py hands marks on
    by the same rules).
    """
    chosen = {}
    taken = set()
    for source in sorted(plan):
        targets, _copy = plan[source]
        if source not in owners:
            continue
        for target in targets:
            if target == source or target in taken:
                continue
            if target in owners and (target not in plan or plan[target][1]):
                continue        # its own data stays: never overwritten
            chosen[source] = target
            taken.add(target)
            break

    def leaves(image_id):
        return image_id in chosen and not plan[image_id][1]

    # (And so on, until nothing changes.)
    changed = True
    while changed:
        changed = False
        for source, target in list(chosen.items()):
            if target in owners and not leaves(target):
                del chosen[source]
                changed = True
    return chosen


def reassign(conn, plan, now=None):
    """Hand edits on to the pictures their content went to (moves, swaps); no commit.

    plan: {from id: (to ids, best first; copy)}. Each edit goes to the
    first of its to ids that has no edit of its own -- or whose own edit
    moves on as well (two files that swapped names swap their edits) --
    and that no other edit of the plan takes. It moves there, or with copy
    it is copied (from id keeps it too). An edit no picture can take
    stays where it is. Both pictures' rows are touched, so the search
    index notices. Returns {from id: to id} of the edits handed on.
    """
    now = time.time() if now is None else now
    ids = set(plan)
    for targets, _copy in plan.values():
        ids.update(targets)
    edits = load_many(conn, ids)
    chosen = choose_heirs(plan, edits)
    for source in chosen:
        if not plan[source][1]:
            conn.execute("DELETE FROM image_edits WHERE image_id = ?", (source,))
    for source, target in sorted(chosen.items()):
        edit = edits[source]
        conn.execute(
            "INSERT OR REPLACE INTO image_edits (image_id, title, description, tags_add, "
            "tags_remove, updated) VALUES (?, ?, ?, ?, ?, ?)",
            (target, edit["title"], edit["description"],
             json.dumps(edit["tags_add"], ensure_ascii=False),
             json.dumps(edit["tags_remove"], ensure_ascii=False), edit["updated"]))
    touched = sorted(set(chosen) | set(chosen.values()))
    for start in range(0, len(touched), ID_CHUNK):
        part = touched[start:start + ID_CHUNK]
        conn.execute("UPDATE images SET updated = ? WHERE id IN (%s)"
                     % ",".join("?" * len(part)), [now] + part)
    return chosen


def give(conn, from_id, to_ids, copy=False, now=None):
    """Give picture from_id's edit to the first of to_ids without one of its own; no commit.

    For a file whose content now lives at another path: the edit moves
    there (with copy, from_id keeps it too). Returns the id that took it,
    or None when from_id has no edit or every one of to_ids has its own.
    """
    return reassign(conn, {from_id: (list(to_ids), copy)}, now).get(from_id)
