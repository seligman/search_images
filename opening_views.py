#!/usr/bin/env python3
"""Where a 360-degree picture opens: its opening view.

The 360 view of a photo or video starts looking at its opening view (yaw:
degrees right of the middle of the panorama, which is where its camera's
front lens looked; pitch: degrees up), and "Reset the view" (0) returns
there. The thumbnail of a 360 picture shows the same view (front_view in
images.py; its dHash stays that of the panorama's middle, so a picture's
place among its look-alikes does not change with its opening view).

Where the view comes from, in this order:

  owner    the owner's own choice: "Open here" in the 360 viewer (POST
           /api/admin/images/<id>/opening-view), with the zoom it had.
  auto     a 360 photo with a title opens where the image embedding model
           (looks.py, the "image_embedding" endpoint) finds what its own
           words say: its title and the first sentence of its description
           are compared with views every images.HEADING_STEP degrees round
           the horizon, and the photo opens at the best one -- only when that
           view beats the front's by MARGIN; otherwise it stays at the
           front. Worked out when looks are made ("opening_views" setting),
           again when its words, its stitch, the model, the prefix its
           words are sent after or METHOD_VERSION change.
  front    yaw 0, pitch 0: everything else (videos too: their poster is
           one moment of a video their words describe as a whole).

Measured on 112 360 photos and videos, every opening view judged by eye:
the front was a good view for 60 and a poor one (sky, a wall, a face up
close) for 2; cheaper choices made from the pixels alone (detail and
contrast around the horizon, skin colour) were no better than the front;
the image model with MARGIN on photos (videos at the front) gave 84 good
views and 2 poor ones (27 better than the front, 4 worse). On videos the
model's view was good less often than the front.

The views are kept by content (the content hash, or "#<image id>" for a
row without one, as gps_check.py's choices), so copies and a moved file
share them, in the opening_views table of index.db (made on first use):

    content_key            the picture's content
    auto_yaw, auto_pitch   the automatic view (NULL: none worked out)
    auto_key               what it was worked out from (see auto_key())
    owner_yaw, owner_pitch, owner_fov   "Open here" (NULL: not set; fov
                           NULL: the viewer's own)
    updated                when a view last changed

A change touches images.updated of the pictures with that content, so the
search index (/api/index) and exports see it. Search records carry the
view as ov ([yaw, pitch] or [yaw, pitch, fov], see annotate); /api/image
as "opening_view" (detail).

This module uses only the standard library; images.py (Pillow) is imported
only to make a thumbnail again (remake_thumbnail).
"""

import hashlib
import json
import math
import re
import sqlite3
import time

import image_edits
import imageinfo

# Raise when the automatic opening views are worked out another way: they
# are all worked out again by the next looks run.
METHOD_VERSION = 1
# The image model's best view must beat the front's (cosine) by this much
# before a photo opens there instead (nearly equal views keep the front).
MARGIN = 0.01
# What the picture's words are sent to the image model after (the query
# prompt of EmbeddingGemma's retrieval task; image_embedding.text_prefix).
DEFAULT_TEXT_PREFIX = "task: search result | query: "
# The viewer's field of view (degrees; viewer.js DEFAULT_FOV): an owner's
# view this near it keeps no fov of its own.
DEFAULT_FOV = 75.0
FOV_RANGE = (30.0, 100.0)               # viewer.js MIN_FOV, MAX_FOV
PITCH_LIMIT = 89.0
# A view this near yaw 0, pitch 0 (degrees) is the front.
FRONT_SLACK = 0.5
ID_CHUNK = 500

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS opening_views (
        content_key TEXT PRIMARY KEY,
        auto_yaw REAL,
        auto_pitch REAL,
        auto_key TEXT NOT NULL DEFAULT '',
        owner_yaw REAL,
        owner_pitch REAL,
        owner_fov REAL,
        updated REAL
    )""",
)

_SENTENCE_RE = re.compile(r"^.*?[.!?](?=\s|$)", re.DOTALL)


class ViewError(ValueError):
    """An opening view that cannot be kept: a message, and {field: problem}."""

    def __init__(self, message, fields=None):
        ValueError.__init__(self, message)
        self.fields = dict(fields or {})


def content_key(content_hash, image_id):
    """What a picture's opening view is kept under: its content hash (or its id)."""
    return content_hash or "#%d" % image_id


def ensure_table(conn):
    """Create the opening_views table when it is not there yet (no commit)."""
    for statement in SCHEMA:
        conn.execute(statement)


def _missing_table(error):
    return "no such table" in str(error)


def wrap_yaw(yaw):
    """yaw in degrees, brought into -180..180."""
    yaw = math.fmod(float(yaw) + 180.0, 360.0)
    return (yaw + 360.0 if yaw < 0 else yaw) - 180.0


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def clean_view(yaw, pitch, fov=None):
    """(yaw, pitch, fov) checked and tidied: yaw in -180..180, pitch within
    PITCH_LIMIT, fov within FOV_RANGE or None (the viewer's own: also when it
    is the viewer's default). Raises ViewError."""
    yaw, pitch = _number(yaw), _number(pitch)
    problems = {}
    if yaw is None:
        problems["yaw"] = "Send a number of degrees."
    if pitch is None or abs(pitch) > PITCH_LIMIT:
        problems["pitch"] = "Send a number of degrees from -%g to %g." % (PITCH_LIMIT,
                                                                         PITCH_LIMIT)
    if fov is not None:
        fov = _number(fov)
        if fov is None or not FOV_RANGE[0] - 0.5 <= fov <= FOV_RANGE[1] + 0.5:
            problems["fov"] = "Send a number of degrees from %g to %g." % FOV_RANGE
    if problems:
        raise ViewError("That is not a view a 360 picture can open at.", problems)
    if fov is not None:
        fov = min(FOV_RANGE[1], max(FOV_RANGE[0], fov))
        if abs(fov - DEFAULT_FOV) < 0.5:
            fov = None
    return round(wrap_yaw(yaw), 2), round(pitch, 2), None if fov is None else round(fov, 2)


def _view_of(row):
    """{"yaw", "pitch", "fov", "owner"} of an opening_views row: the owner's, else
    the automatic one; None when neither is set."""
    if row["owner_yaw"] is not None and row["owner_pitch"] is not None:
        return {"yaw": row["owner_yaw"], "pitch": row["owner_pitch"],
                "fov": row["owner_fov"], "owner": True}
    if row["auto_yaw"] is not None and row["auto_pitch"] is not None:
        return {"yaw": row["auto_yaw"], "pitch": row["auto_pitch"], "fov": None,
                "owner": False}
    return None


def is_front(view):
    """True for no view, or one at the front at the viewer's own zoom."""
    return (view is None or (abs(view["yaw"]) < FRONT_SLACK and abs(view["pitch"]) < FRONT_SLACK
                             and view.get("fov") is None))


def record_value(view):
    """A view as a search record's ov: [yaw, pitch] or [yaw, pitch, fov] (degrees, one
    decimal), or None for the front."""
    if is_front(view):
        return None
    value = [round(view["yaw"], 1), round(view["pitch"], 1)]
    if view.get("fov") is not None:
        value.append(round(view["fov"], 1))
    return value


def _load_keys(conn, keys):
    """{content key: row} of the opening_views rows of some keys ({} without the table)."""
    keys = list(dict.fromkeys(keys))
    found = {}
    try:
        for start in range(0, len(keys), ID_CHUNK):
            chunk = keys[start:start + ID_CHUNK]
            for row in conn.execute("SELECT * FROM opening_views WHERE content_key IN (%s)"
                                    % ",".join("?" * len(chunk)), chunk):
                found[row["content_key"]] = row
    except sqlite3.OperationalError as error:
        if not _missing_table(error):
            raise
        return {}
    return found


def views_for(conn, ids):
    """{image id: view} (see _view_of) of the pictures among ids that have one."""
    ids = [int(image_id) for image_id in ids]
    keys = {}
    for start in range(0, len(ids), ID_CHUNK):
        chunk = ids[start:start + ID_CHUNK]
        for row in conn.execute("SELECT id, content_hash FROM images WHERE id IN (%s)"
                                % ",".join("?" * len(chunk)), chunk):
            keys[row[0]] = content_key(row[1], row[0])
    rows = _load_keys(conn, keys.values())
    views = {}
    for image_id, key in keys.items():
        view = _view_of(rows[key]) if key in rows else None
        if view is not None:
            views[image_id] = view
    return views


def any_views(conn):
    """True when the opening_views table has a row (False without the table)."""
    try:
        return conn.execute("SELECT 1 FROM opening_views LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError as error:
        if not _missing_table(error):
            raise
        return False


def annotate(conn, records):
    """Give the search records of 360 pictures (p "360") their opening view (ov, see
    record_value) when it is not the front. records keep their database ids (i)."""
    if not any_views(conn):
        return
    wanted = {record["i"]: record for record in records
              if record.get("p") == "360" and isinstance(record.get("i"), int)}
    if not wanted:
        return
    for image_id, view in views_for(conn, wanted).items():
        value = record_value(view)
        if value is not None:
            wanted[image_id]["ov"] = value


def detail(conn, image_id, content_hash, owner=False):
    """What /api/image tells of a 360 picture's opening view: {"yaw", "pitch", "fov"
    (None: the viewer's own)} -- the front when none is set -- and for the owner also
    "owner" (True: "Open here" set it) and "auto" ([yaw, pitch] worked out by the
    image model, or None)."""
    rows = _load_keys(conn, [content_key(content_hash, image_id)])
    row = next(iter(rows.values()), None)
    view = _view_of(row) if row is not None else None
    result = {"yaw": view["yaw"] if view else 0.0, "pitch": view["pitch"] if view else 0.0,
              "fov": view["fov"] if view else None}
    if owner:
        result["owner"] = bool(view and view["owner"])
        result["auto"] = ([row["auto_yaw"], row["auto_pitch"]]
                          if row is not None and row["auto_yaw"] is not None else None)
    return result


def touch(conn, key, now=None):
    """Mark the present pictures with content key as changed (images.updated), so the
    search index and exports notice a new opening view."""
    stamp = time.time() if now is None else now
    if key.startswith("#"):
        conn.execute("UPDATE images SET updated = ? WHERE id = ?", (stamp, int(key[1:])))
    else:
        conn.execute("UPDATE images SET updated = ? WHERE content_hash = ?", (stamp, key))


def _picture(conn, image_id):
    """The present 360 picture's row (id, library_id, content_hash, has_preview, media),
    or None."""
    row = conn.execute("SELECT id, library_id, content_hash, has_preview, media, projection "
                       "FROM images WHERE id = ? AND missing_since IS NULL",
                       (image_id,)).fetchone()
    if row is None or row["projection"] not in imageinfo.PROJECTIONS_360:
        return None
    return row


def set_owner(conn, image_id, yaw, pitch, fov=None, now=None):
    """The owner's opening view of a 360 picture (and its copies): yaw, pitch, fov
    (degrees; see clean_view). Returns the picture's row, or None when image_id is no
    present 360 picture. Raises ViewError. No commit."""
    yaw, pitch, fov = clean_view(yaw, pitch, fov)
    row = _picture(conn, image_id)
    if row is None:
        return None
    key = content_key(row["content_hash"], image_id)
    stamp = time.time() if now is None else now
    ensure_table(conn)
    # (Not an upsert: older SQLite versions do not have ON CONFLICT ... DO.)
    conn.execute("INSERT OR IGNORE INTO opening_views (content_key) VALUES (?)", (key,))
    conn.execute("UPDATE opening_views SET owner_yaw = ?, owner_pitch = ?, owner_fov = ?, "
                 "updated = ? WHERE content_key = ?", (yaw, pitch, fov, stamp, key))
    touch(conn, key, stamp)
    return row


def clear_owner(conn, image_id, now=None):
    """Forget the owner's opening view of a 360 picture (it opens at the automatic
    view, or the front, again). Returns its row, or None when image_id is no present
    360 picture. No commit."""
    row = _picture(conn, image_id)
    if row is None:
        return None
    key = content_key(row["content_hash"], image_id)
    stamp = time.time() if now is None else now
    try:
        conn.execute("UPDATE opening_views SET owner_yaw = NULL, owner_pitch = NULL, "
                     "owner_fov = NULL, updated = ? WHERE content_key = ?", (stamp, key))
    except sqlite3.OperationalError as error:
        if not _missing_table(error):
            raise
        return row
    touch(conn, key, stamp)
    return row


# ---------------------------------------------------------------------------
# The automatic view (worked out by looks.py)

def query_text(info, edit=None):
    """The words a 360 photo's views are compared with: its title and the first
    sentence of its description (the owner's edit laid over the model's), or '' when
    it has no title."""
    shown = image_edits.apply(info if isinstance(info, dict) else None, edit)
    shown = shown if isinstance(shown, dict) else {}
    title = shown.get("title") if isinstance(shown.get("title"), str) else ""
    title = " ".join(title.split())
    if not title:
        return ""
    description = shown.get("description")
    description = " ".join(description.split()) if isinstance(description, str) else ""
    match = _SENTENCE_RE.match(description)
    first = (match.group(0) if match else description)[:300].strip()
    if not first:
        return title
    return "%s%s %s" % (title, "" if title[-1:] in ".!?" else ".", first)


def auto_key(stitch, text, made_with=""):
    """What an automatic view is worked out from: METHOD_VERSION, the stitch of its
    panorama (images.stitch_key() for an unstitched original, '' for one stitched by
    its camera), its words (query_text) and made_with (the model and the prefix its
    words are sent after, see looks.views_pending). Another key: worked out again."""
    digest = hashlib.sha1((made_with + "|" + text).encode("utf-8")).hexdigest()[:16]
    return "%d|%s|%s" % (METHOD_VERSION, stitch, digest)


_PENDING_SQL = """
    SELECT i.id, i.library_id, i.content_hash, i.projection, i.info, i.described
    FROM images AS i
    WHERE i.missing_since IS NULL AND i.media = 'image' AND i.has_preview = 1
        AND i.projection IN (%s) AND i.library_id IN (%s)
    ORDER BY i.id DESC"""


def pending(conn, library_ids, stitch, made_with=""):
    """The 360 photos of some libraries whose automatic view is to be worked out:
    [(row, words, key)], newest first, one per content (the newest copy with
    words). Only photos with a stored preview (the views are cut from it) and a
    title (query_text); stitch is images.stitch_key() (the key of an unstitched
    original's panorama), made_with what else the views depend on (auto_key)."""
    library_ids = list(library_ids)
    if not library_ids:
        return []
    rows = conn.execute(_PENDING_SQL % (",".join("?" * len(imageinfo.PROJECTIONS_360)),
                                        ",".join("?" * len(library_ids))),
                        list(imageinfo.PROJECTIONS_360) + library_ids).fetchall()
    if not rows:
        return []
    edits = image_edits.load_many(conn, [row["id"] for row in rows])
    known = _load_keys(conn, [content_key(row["content_hash"], row["id"]) for row in rows])
    result, seen = [], set()
    for row in rows:
        key_of = content_key(row["content_hash"], row["id"])
        if key_of in seen:
            continue
        info = None
        if row["described"]:
            info = _parse_info(row["info"])
        text = query_text(info, edits.get(row["id"]))
        if not text:
            continue                # (a copy with words may come later)
        seen.add(key_of)
        wanted = auto_key(stitch if row["projection"] == imageinfo.PROJECTION_DUAL_FISHEYE
                          else "", text, made_with)
        have = known.get(key_of)
        if have is not None and have["auto_key"] == wanted:
            continue
        result.append((row, text, wanted))
    return result


def _parse_info(text):
    try:
        info = json.loads(text or "null", parse_constant=lambda _name: None)
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def choose(scores, yaws, margin=MARGIN):
    """The yaw (degrees) a 360 photo opens at, from how well its words match its views
    (scores: cosines of the views at yaws, evenly spaced round the horizon, one of them
    0): the best view, nudged towards the better of its neighbours (the top of a
    parabola through the three), when it beats the front by more than margin; else 0."""
    if not scores or len(scores) != len(yaws) or 0 not in yaws:
        return 0.0
    front = scores[yaws.index(0)]
    best = max(range(len(scores)), key=lambda index: scores[index])
    if scores[best] - front <= margin:
        return 0.0
    count = len(scores)
    before, here, after = scores[best - 1], scores[best], scores[(best + 1) % count]
    curve = before - 2.0 * here + after
    shift = (before - after) / (2.0 * curve) if curve < 0 else 0.0
    step = 360.0 / count
    return round(wrap_yaw(yaws[best] + max(-0.5, min(0.5, shift)) * step), 1)


def store_auto(conn, key, yaw, pitch, made_from, now=None):
    """Keep the automatic view (yaw, pitch) of a content key, worked out from made_from
    (auto_key()). Returns True when the view the picture opens at changed (no owner's
    view hides it). No commit."""
    stamp = time.time() if now is None else now
    ensure_table(conn)
    before = _load_keys(conn, [key]).get(key)
    conn.execute("INSERT OR IGNORE INTO opening_views (content_key) VALUES (?)", (key,))
    conn.execute("UPDATE opening_views SET auto_yaw = ?, auto_pitch = ?, auto_key = ?, "
                 "updated = ? WHERE content_key = ?", (yaw, pitch, made_from, stamp, key))
    owner = before is not None and before["owner_yaw"] is not None
    old = (before["auto_yaw"], before["auto_pitch"]) if before is not None else (None, None)
    changed = not owner and (old[0] is None or abs(old[0] - yaw) > 0.05
                             or abs(old[1] - pitch) > 0.05)
    if changed:
        touch(conn, key, stamp)
    return changed


def forget_unreferenced(conn):
    """Drop the views of contents no row of index.db has any more (rows only missing
    for now keep theirs, as looks.purge does). Returns how many went. No commit."""
    try:
        return conn.execute(
            "DELETE FROM opening_views WHERE content_key NOT IN (SELECT content_hash FROM "
            "images WHERE content_hash != '') AND NOT (content_key LIKE '#%' AND "
            "CAST(substr(content_key, 2) AS INTEGER) IN (SELECT id FROM images))").rowcount
    except sqlite3.OperationalError as error:
        if not _missing_table(error):
            raise
        return 0


def drop_auto(conn, now=None):
    """Forget every automatic view (the "opening_views" setting is off): pictures open
    at the owner's view or the front. Returns the ids of the present pictures whose
    view changed (their thumbnails are to be made again). No commit."""
    try:
        keys = [row[0] for row in conn.execute(
            "SELECT content_key FROM opening_views WHERE auto_yaw IS NOT NULL OR "
            "auto_key != ''")]
    except sqlite3.OperationalError as error:
        if not _missing_table(error):
            raise
        return []
    if not keys:
        return []
    rows = _load_keys(conn, keys)
    stamp = time.time() if now is None else now
    changed = []
    for key in keys:
        row = rows.get(key)
        turned = (row is not None and row["owner_yaw"] is None and row["auto_yaw"] is not None
                  and (abs(row["auto_yaw"]) >= FRONT_SLACK
                       or abs(row["auto_pitch"] or 0.0) >= FRONT_SLACK))
        conn.execute("UPDATE opening_views SET auto_yaw = NULL, auto_pitch = NULL, "
                     "auto_key = '', updated = ? WHERE content_key = ?", (stamp, key))
        if turned:
            touch(conn, key, stamp)
            changed.extend(_ids_of(conn, key))
    conn.execute("DELETE FROM opening_views WHERE auto_yaw IS NULL AND owner_yaw IS NULL")
    return changed


def _ids_of(conn, key):
    if key.startswith("#"):
        return [int(key[1:])]
    return [row[0] for row in conn.execute(
        "SELECT id FROM images WHERE content_hash = ? AND missing_since IS NULL", (key,))]


# ---------------------------------------------------------------------------
# Thumbnails

def thumbnail_view(conn, image_id, content_hash):
    """(yaw, pitch) the thumbnail of a 360 picture shows (its opening view), or None
    for the front."""
    rows = _load_keys(conn, [content_key(content_hash, image_id)])
    row = next(iter(rows.values()), None)
    view = _view_of(row) if row is not None else None
    if view is None or (abs(view["yaw"]) < FRONT_SLACK and abs(view["pitch"]) < FRONT_SLACK):
        return None
    return view["yaw"], view["pitch"]


def scanned_thumbnail(conn, image_id, content_hash, result):
    """The thumbnail to store for a 360 picture just read (result: images.read_image's
    or videos.read_video's): the result's own (the front), or -- when the picture has
    an opening view (the owner's or one worked out before) -- one cut from its preview
    at that view."""
    thumb = result["thumb"]
    if result.get("projection") not in imageinfo.PROJECTIONS_360 or not result.get("preview"):
        return thumb
    view = thumbnail_view(conn, image_id, content_hash)
    if view is None:
        return thumb
    import images
    try:
        return images.view_thumbnail(result["preview"], *view)
    except images.ImageReadError:
        return thumb


def remake_thumbnail(conn, image_id, previews, thumbs, commit=True):
    """Make a 360 picture's thumbnail again from its stored preview, at its opening view
    (previews: a cache.PreviewStore, thumbs: a cache.ThumbStore). Returns True when it
    was made; False when the picture has no stored preview or it cannot be read (the
    thumbnail stays)."""
    row = conn.execute("SELECT id, library_id, content_hash, has_preview FROM images "
                       "WHERE id = ?", (image_id,)).fetchone()
    if row is None or row["has_preview"] != 1:
        return False
    try:
        data = previews.read(row["library_id"], image_id)
    except (OSError, ValueError):
        data = None
    if not data:
        return False
    import images
    view = thumbnail_view(conn, image_id, row["content_hash"]) or (0.0, 0.0)
    try:
        thumb = images.view_thumbnail(data, *view)
    except images.ImageReadError:
        return False
    thumbs.put(image_id, thumb, commit=commit)
    return True


def copies(conn, image_id):
    """The ids of the present pictures that share image_id's content (itself first)."""
    row = conn.execute("SELECT content_hash FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return []
    if not row[0]:
        return [image_id]
    others = [other[0] for other in conn.execute(
        "SELECT id FROM images WHERE content_hash = ? AND id <> ? AND missing_since IS NULL "
        "ORDER BY id", (row[0], image_id))]
    return [image_id] + others
