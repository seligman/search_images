#!/usr/bin/env python3
"""The refine pass: documents and screenshots described again by a stronger model.

A library may name a refine endpoint (its "refine" setting). Pictures it
has described whose kind (photo_type) or document type is one of the
chosen ones are then described once more with that endpoint, with more
room for the answer (max_output_tokens), so receipts, letters, forms and
screenshots get a fuller transcription. The answer replaces the
description (an answer cut off at the length limit keeps the old values
of the fields it did not reach, and never clears the old privacy flags:
describe.fill_missing), and its provenance says tier "refine". A picture
refined with the current prompt version is not refined again; the regular
describe step never overwrites a refined description (only "Re-describe
out of date" does, for an older prompt, and "Describe again" for one
picture).

This module holds the settings and the choice of pictures (stdlib only,
for the web server's counts); describe.refine_pending() does the work.
"""

import json
import sqlite3

import common
import prompts
from prompts import PROMPT_VER

TIER = "refine"
DEFAULT_PHOTO_TYPES = ("screenshot", "document", "scan")
DEFAULT_DOCUMENT_TYPES = ("receipt", "letter", "form", "card_or_id", "ticket", "chat", "web",
                          "app", "handwriting", "book_page", "slide")
DEFAULT_MAX_OUTPUT_TOKENS = 8000
MAX_OUTPUT_TOKENS_RANGE = (500, 1000000)
# The kinds that can be chosen ("other" and "none" mean nothing in particular).
PHOTO_TYPE_CHOICES = tuple(value for value in prompts.PHOTO_TYPES if value != "other")
DOCUMENT_TYPE_CHOICES = tuple(value for value in prompts.DOCUMENT_TYPES
                              if value not in ("none", "other"))
FIELDS = ("endpoint", "photo_types", "document_types", "max_output_tokens")


def _in_order(items, choices):
    """The items that are among choices, once each, in the order of choices."""
    wanted = set(items)
    return [choice for choice in choices if choice in wanted]

# Columns describe.py needs of a candidate row (as describe._PENDING_SQL).
COLUMNS = ("id, library_id, path, name, content_hash, taken_at, mtime, described, prompt_ver, "
           "describe_attempts, has_preview, projection, hidden, info, provenance")


def settings(library):
    """A library's refine settings with the defaults filled in.

    {"endpoint": '' (off) or an endpoint or group name, "photo_types",
    "document_types" (in the order of the *_CHOICES), "max_output_tokens"}.
    """
    stored = library.get("refine") if isinstance(library, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    endpoint = stored.get("endpoint")
    result = {"endpoint": endpoint.strip() if isinstance(endpoint, str) else "",
              "photo_types": _in_order(DEFAULT_PHOTO_TYPES, PHOTO_TYPE_CHOICES),
              "document_types": _in_order(DEFAULT_DOCUMENT_TYPES, DOCUMENT_TYPE_CHOICES),
              "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS}
    for key, choices in (("photo_types", PHOTO_TYPE_CHOICES),
                         ("document_types", DOCUMENT_TYPE_CHOICES)):
        value = stored.get(key)
        if isinstance(value, list):
            result[key] = _in_order([item for item in value if isinstance(item, str)],
                                    choices)
    tokens = stored.get("max_output_tokens")
    low, high = MAX_OUTPUT_TOKENS_RANGE
    if isinstance(tokens, int) and not isinstance(tokens, bool) and low <= tokens <= high:
        result["max_output_tokens"] = tokens
    return result


def enabled(library):
    """True when a library names a refine endpoint."""
    return bool(settings(library)["endpoint"])


def clean(config, value):
    """Check a refine setting sent by the browser. Returns (value to store, errors).

    value is a dict with any of FIELDS (None or {} turns refining off).
    Only what differs from the defaults is stored, and nothing at all when
    no endpoint is chosen. errors maps "refine" to a message.
    """
    if value is None or value == {}:
        return {}, {}
    if not isinstance(value, dict):
        return None, {"refine": "Send the refine settings as an object."}
    unknown = sorted(set(value) - set(FIELDS))
    if unknown:
        return None, {"refine": "Unknown refine setting(s): %s." % ", ".join(unknown)}
    endpoint = value.get("endpoint") or ""
    if not isinstance(endpoint, str):
        return None, {"refine": "The refine endpoint must be a name."}
    endpoint = endpoint.strip()
    if not endpoint:
        return {}, {}
    if not common.resolve_endpoints(config, endpoint):
        return None, {"refine": "No enabled endpoint or group is called %s." % endpoint}
    found = common.get_endpoint(config, endpoint)
    stored = {"endpoint": found["name"] if found is not None else endpoint}
    for key, choices, default in (("photo_types", PHOTO_TYPE_CHOICES, DEFAULT_PHOTO_TYPES),
                                  ("document_types", DOCUMENT_TYPE_CHOICES,
                                   DEFAULT_DOCUMENT_TYPES)):
        if key not in value or value[key] is None:
            continue
        items = value[key]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            return None, {"refine": "%s must be a list of names." % key}
        wrong = [item for item in items if item not in choices]
        if wrong:
            return None, {"refine": "Unknown %s: %s." % (key.replace("_", " "),
                                                         ", ".join(sorted(set(wrong))))}
        items = _in_order(items, choices)
        if items != _in_order(default, choices):
            stored[key] = items
    if not stored.get("photo_types", True) and not stored.get("document_types", True):
        return None, {"refine": "Choose at least one kind of picture to refine."}
    tokens = value.get("max_output_tokens")
    if tokens is not None and tokens != "":
        if isinstance(tokens, str):
            try:
                tokens = int(tokens.strip())
            except ValueError:
                tokens = None
        low, high = MAX_OUTPUT_TOKENS_RANGE
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not low <= tokens <= high:
            return None, {"refine": "The longest answer must be a whole number from %d to %d "
                                    "tokens." % (low, high)}
        if tokens != DEFAULT_MAX_OUTPUT_TOKENS:
            stored["max_output_tokens"] = tokens
    return stored, {}


def matches(info, wanted):
    """True when a description's photo_type or document_type is one of those wanted."""
    if not isinstance(info, dict):
        return False
    return (info.get("photo_type") in wanted["photo_types"]
            or info.get("document_type") in wanted["document_types"])


def is_refined(provenance, prompt_ver):
    """True for a description refined with the current prompt version."""
    return (isinstance(provenance, dict) and provenance.get("tier") == TIER
            and isinstance(prompt_ver, int) and prompt_ver >= PROMPT_VER)


_json1 = []


def has_json1():
    """True when SQLite has its JSON functions (as the Python builds of recent years do)."""
    if not _json1:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("SELECT json_extract('{\"a\": 1}', '$.a')").fetchone()
            _json1.append(True)
        except sqlite3.Error:
            _json1.append(False)
        finally:
            conn.close()
    return _json1[0]


def _where(wanted):
    """The WHERE clause (and its arguments) of the candidates of one library."""
    photo, document = wanted["photo_types"] or [""], wanted["document_types"] or [""]
    sql = ("library_id = ? AND missing_since IS NULL AND described = 1 AND ("
           "(CASE WHEN json_valid(info) THEN json_extract(info, '$.photo_type') END) IN (%s) OR "
           "(CASE WHEN json_valid(info) THEN json_extract(info, '$.document_type') END) IN (%s))"
           " AND NOT (prompt_ver >= ? AND IFNULL(CASE WHEN json_valid(provenance) THEN "
           "json_extract(provenance, '$.tier') END, '') = ?)"
           % (",".join("?" * len(photo)), ",".join("?" * len(document))))
    return sql, list(photo) + list(document) + [PROMPT_VER, TIER]


def _python_filter(rows, wanted):
    """The candidates among rows, judged in Python (SQLite without JSON functions)."""
    found = []
    for row in rows:
        try:
            info = json.loads(row["info"] or "")
        except ValueError:
            continue
        try:
            provenance = json.loads(row["provenance"] or "")
        except ValueError:
            provenance = None
        if matches(info, wanted) and not is_refined(provenance, row["prompt_ver"]):
            found.append(row)
    return found


def candidate_rows(conn, library, max_failures=None):
    """The described pictures of a library that the refine pass would describe again.

    Present pictures whose description's kind or document type is chosen
    in the library's refine settings, without those already refined with
    the current prompt version. With max_failures, pictures that failed
    that many times are left out. Rows hold COLUMNS, ordered by id.
    """
    wanted = settings(library)
    if not wanted["endpoint"]:
        return []
    if has_json1():
        sql, args = _where(wanted)
        rows = conn.execute("SELECT %s FROM images WHERE %s ORDER BY id" % (COLUMNS, sql),
                            [library["id"]] + args).fetchall()
    else:
        rows = _python_filter(conn.execute(
            "SELECT %s FROM images WHERE library_id = ? AND missing_since IS NULL AND "
            "described = 1 ORDER BY id" % COLUMNS, (library["id"],)).fetchall(), wanted)
    if max_failures is not None:
        rows = [row for row in rows if row["describe_attempts"] < max_failures]
    return rows


def counts(conn, library, max_failures=common.MAX_DESCRIBE_FAILURES, include_hidden=True):
    """{"pending", "hidden", "failed"}: a library's pictures for the refine pass.

    pending: those the pass would describe; failed: those it skips because
    they failed max_failures times (Retry failed tries them again). Without
    include_hidden (the refine endpoints are all outside this computer and
    network, so hidden pictures are never sent to them), hidden pictures
    are left out of both and counted in hidden instead. All 0 when the
    library has no refine endpoint.
    """
    result = {"pending": 0, "hidden": 0, "failed": 0}
    wanted = settings(library)
    if not wanted["endpoint"]:
        return result
    if not has_json1():
        for row in candidate_rows(conn, library):
            if row["hidden"] and not include_hidden:
                result["hidden"] += 1
            elif row["describe_attempts"] >= max_failures:
                result["failed"] += 1
            else:
                result["pending"] += 1
        return result
    sql, args = _where(wanted)
    found = conn.execute(
        "SELECT SUM(describe_attempts < ? AND (hidden = 0 OR ?)), "
        "SUM(hidden <> 0 AND NOT ?), SUM(describe_attempts >= ? AND (hidden = 0 OR ?)) "
        "FROM images WHERE %s" % sql,
        [max_failures, int(include_hidden), int(include_hidden), max_failures,
         int(include_hidden), library["id"]] + args).fetchone()
    return {"pending": int(found[0] or 0), "hidden": int(found[1] or 0),
            "failed": int(found[2] or 0)}


def pending_count(conn, library, max_failures=common.MAX_DESCRIBE_FAILURES,
                  include_hidden=True):
    """How many pictures of a library the refine pass would describe (0 when it is off)."""
    return counts(conn, library, max_failures, include_hidden)["pending"]
