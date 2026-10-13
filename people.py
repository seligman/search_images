#!/usr/bin/env python3
"""People: the faces in the pictures, found and described on this computer (data/faces.db).

Off by default. With config.json "people": {"enabled": true, ...} (see
SETTINGS.md), a CPU job (Find faces on the Manage page, the step after a
scan, or python people.py) looks at every picture once: a face detector
(YuNet) finds the faces and their five landmarks (eyes, nose tip, mouth
corners), and a face recogniser (SFace) makes a vector of each face, so
that the faces of one person can later be grouped. Both are small ONNX
models from the OpenCV Zoo that run on the processor; they run in a
helper program (people_worker.py) started with the python of a Python
environment of its own that has numpy and OpenCV ("python": that
environment's python, or its folder), never in this program: without
that environment People is simply off, and the rest works as before.
The helper gets a picture's bytes over a pipe and answers with its
faces; nothing leaves this computer.

Faces are biometric data, the most private data this program keeps.
They live in a database of their own, data/faces.db (with the people
they were grouped into and their names, people_groups.py), which nothing
sends anywhere or publishes (an export only the names, with its
people_names option), and which python people.py --delete-all (or Delete
face data on the Manage page) deletes as a whole. Every picture is looked at (hidden ones too: nothing is sent
anywhere).

    meta           key, value: version (FACES_VERSION), epoch (random, new for
                   every file), generation (raised by every change)
    face_pictures  content_hash, kind, recipe, width, height, faces, seconds,
                   updated: the pictures looked at, with the size the helper
                   saw (pixels) and how many faces it kept
    faces          id, content_hash, kind, x, y, w, h (the box, as fractions of
                   the picture's width and height, upright as shown),
                   landmarks (JSON [[x, y] x 5], fractions too: the person's
                   right eye, left eye, nose tip, right and left mouth
                   corner), score (the detector's confidence), size (the
                   box's short side in pixels of the picture looked at),
                   sharpness, norm, turn, tilt (see pose()), quality (see
                   quality()), embedding (SFace's vector: 128 little-endian
                   float32, unit length), updated
    face_failures  content_hash, kind, recipe, error, attempts, updated: the
                   pictures that could not be looked at (after MAX_ATTEMPTS
                   tries a picture waits for --retry-failed)
    people, face_people, face_rejects, face_marks
                   the people the faces were grouped into, and what the
                   owner said about them (version 2; see people_groups.py)

Like the looks (looks.py), faces belong to a picture's content (its
content hash) and the kind of picture looked at (KIND_PHOTO: an ordinary
picture, its preview; KIND_POSTER: a video's poster frame; KIND_360: the
flat, equirectangular preview of a 360-degree photo or video), so copies
share them and a moved or renamed picture keeps them. Of a 360-degree
picture only the faces near its horizon are kept (BAND_360: there the
projection hardly stretches them, and that is where the people holding
the camera are); the viewer draws no boxes on those. A picture is looked
at again when the recipe changes (other model files or settings that
change what is found: see recipe()); its faces are then replaced, and a
face found again in the same place keeps its id (replace_faces), so what
is later said about a face stays with it. Faces whose content no index
row has any more go (purge, and forget_unreferenced right after rows are
deleted), and so do the people left without a face, names and all.

This module uses only the standard library; images.py (Pillow) is
imported when a picture without a preview is read, and the probe face
is drawn with Pillow.
"""

import argparse
import base64
import collections
import concurrent.futures
import io
import json
import math
import os
import queue
import secrets
import sqlite3
import struct
import subprocess
import sys
import threading
import time

import common
import db
import people_worker

# Raise when the layout of faces.db changes: an older file is then emptied by
# the next run that writes it (its faces must be found again), unless
# connect() knows how to bring it up to date (UPGRADES).
FACES_VERSION = 2
# Raise when faces are found differently (people_worker.find_faces, how the
# pictures are prepared): every picture is looked at again.
FIND_VERSION = 1
KIND_PHOTO = "photo"
KIND_POSTER = "video-poster"
KIND_360 = "360"
# The faces of a 360-degree picture that are kept: those whose middle lies
# between these parts of the picture's height (the middle band, around the
# horizon, where the equirectangular projection stretches faces least).
BAND_360 = (0.25, 0.75)
# The model files of the OpenCV Zoo (see README, "People").
DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
DEFAULTS = {
    "enabled": False,
    "python": "",
    "models_dir": "",
    "detector": DETECTOR_FILE,
    "recognizer": RECOGNIZER_FILE,
    "after_scan": True,
    "workers": 4,
    "threads": 1,
    "max_edge": people_worker.DEFAULT_MAX_EDGE,
    "min_score": people_worker.DEFAULT_MIN_SCORE,
    "min_size": people_worker.DEFAULT_MIN_SIZE,
    "max_faces": people_worker.DEFAULT_MAX_FACES,
    # How faces are grouped into people (people_groups.py).
    "match": 0.4,
    "group_quality": 0.25,
    "min_faces": 3,
    "visitors": False,
}
# (minimum, maximum) of the number settings.
LIMITS = {
    "workers": (1, 16),
    "threads": (0, 64),
    "max_edge": (320, 4096),
    "min_score": (0.3, 0.99),
    "min_size": (8, 400),
    "max_faces": (1, people_worker.MAX_FACES),
    "match": (0.2, 0.9),
    "group_quality": (0.0, 1.0),
    "min_faces": (1, 100),
}
# The smallest size of the pyramid (see people_worker.pyramid).
MIN_EDGE = people_worker.DEFAULT_MIN_EDGE
EMBEDDING_DIMS = people_worker.EMBEDDING_DIMS
EMBEDDING_BYTES = 4 * EMBEDDING_DIMS
MAX_ATTEMPTS = 3            # tries before a picture waits for --retry-failed
HELLO_TIMEOUT = 120.0       # seconds the helper may take to start (importing OpenCV)
LOAD_TIMEOUT = 120.0        # ... and to load the models
PICTURE_TIMEOUT = 120.0     # ... and for one picture
QUIT_WAIT = 5.0             # seconds a helper asked to quit may take before it is ended
MAX_RESTARTS = 3            # helpers that stopped (crashed, timed out) before a run gives up
COMMIT_ROWS = 50
COMMIT_SECONDS = 3.0
FAILURE_LOG_LIMIT = 5
ID_CHUNK = 500
STDERR_LINES = 40           # the helper's last lines of standard error kept for messages
FOLDER_CHECK_TIMEOUT = 5.0
DELETE_TRIES = 20           # tries to remove a face data file (0.1 s apart)
# A face found again within this overlap (intersection over union) of an
# earlier face of the same picture keeps the earlier face's id.
SAME_FACE_IOU = 0.5

NOT_ENABLED = ("People is off: set \"people\": {\"enabled\": true, \"python\": ..., "
               "\"models_dir\": ...} in config.json (see SETTINGS.md, \"people\").")


class PeopleError(Exception):
    """Faces cannot be found now (the setup is not complete or does not work)."""


class StoreError(sqlite3.DatabaseError):
    """data/faces.db cannot be written (made by a newer version of this program)."""


class HelperError(Exception):
    """The helper stopped, did not answer in time, or could not start or load its models."""


class PictureError(Exception):
    """The helper could not look at one picture (it goes on with the next)."""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS face_pictures (content_hash TEXT NOT NULL, kind TEXT NOT NULL, "
    "recipe TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, "
    "faces INTEGER NOT NULL, seconds REAL NOT NULL DEFAULT 0, updated REAL NOT NULL, "
    "PRIMARY KEY (content_hash, kind))",
    "CREATE TABLE IF NOT EXISTS faces (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "content_hash TEXT NOT NULL, kind TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, "
    "w REAL NOT NULL, h REAL NOT NULL, landmarks TEXT NOT NULL, score REAL NOT NULL, "
    "size REAL NOT NULL, sharpness REAL NOT NULL, norm REAL NOT NULL, turn REAL NOT NULL, "
    "tilt REAL NOT NULL, quality REAL NOT NULL, embedding BLOB NOT NULL, "
    "updated REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS faces_picture ON faces (content_hash, kind)",
    "CREATE TABLE IF NOT EXISTS face_failures (content_hash TEXT NOT NULL, kind TEXT NOT NULL, "
    "recipe TEXT NOT NULL, error TEXT NOT NULL, attempts INTEGER NOT NULL, "
    "updated REAL NOT NULL, PRIMARY KEY (content_hash, kind))",
) + (
    # The people (people_groups.py): version 2.
    "CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0, cover INTEGER, "
    "created REAL NOT NULL, updated REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS face_people (face_id INTEGER PRIMARY KEY "
    "REFERENCES faces (id) ON DELETE CASCADE, person_id INTEGER NOT NULL "
    "REFERENCES people (id) ON DELETE CASCADE, owner INTEGER NOT NULL DEFAULT 0, "
    "updated REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS face_people_person ON face_people (person_id)",
    "CREATE TABLE IF NOT EXISTS face_rejects (face_id INTEGER NOT NULL "
    "REFERENCES faces (id) ON DELETE CASCADE, person_id INTEGER NOT NULL "
    "REFERENCES people (id) ON DELETE CASCADE, updated REAL NOT NULL, "
    "PRIMARY KEY (face_id, person_id))",
    "CREATE TABLE IF NOT EXISTS face_marks (face_id INTEGER PRIMARY KEY "
    "REFERENCES faces (id) ON DELETE CASCADE, not_face INTEGER NOT NULL DEFAULT 1, "
    "updated REAL NOT NULL)",
)
# The tables of the people (version 2), which an older file gets added.
_PEOPLE_SCHEMA = _SCHEMA[-5:]
# Every table, in an order they can be emptied in.
TABLES = ("face_marks", "face_rejects", "face_people", "people", "face_failures", "faces",
          "face_pictures", "meta")


# ---------------------------------------------------------------------------
# Settings

def _number(value, default, low, high, kind=int):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if kind is int and not isinstance(value, int):
        return default
    if not (low <= value <= high):
        return default
    return kind(value)


def settings(config):
    """config.json's "people" settings, every one filled in and checked (DEFAULTS)."""
    raw = config.get("people") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULTS)
    result["enabled"] = raw.get("enabled") is True
    result["after_scan"] = raw.get("after_scan") is not False
    result["visitors"] = raw.get("visitors") is True
    for key in ("python", "models_dir"):
        result[key] = common.unquote_path(raw.get(key))
    for key in ("detector", "recognizer"):
        value = common.unquote_path(raw.get(key))
        if value:
            result[key] = value
    for key in ("workers", "threads", "max_edge", "min_size", "max_faces", "min_faces"):
        result[key] = _number(raw.get(key), DEFAULTS[key], *LIMITS[key])
    for key in ("min_score", "match", "group_quality"):
        result[key] = _number(raw.get(key), DEFAULTS[key], *LIMITS[key], kind=float)
    return result


def enabled(config):
    """True when config.json turns People on (usable or not)."""
    return settings(config)["enabled"]


def python_path(options):
    """The people environment's python: the "python" setting (relative to the folder of
    config.json), or, for an environment's folder, its Scripts\\python.exe (or
    bin/python); '' when none is set."""
    path = common.resolve_config_relative(options["python"]) if options["python"] else ""
    if path and os.path.isdir(path):
        for parts in (("Scripts", "python.exe"), ("bin", "python3"), ("bin", "python")):
            candidate = os.path.join(path, *parts)
            if os.path.isfile(candidate):
                return candidate
    return path


def model_path(options, key):
    """The full path of the detector or recognizer file: a name in models_dir, or a
    path of its own (relative to the folder of config.json)."""
    name = options[key]
    if os.path.isabs(name) or "/" in name or "\\" in name or not options["models_dir"]:
        return common.resolve_config_relative(name)
    return os.path.join(common.resolve_config_relative(options["models_dir"]), name)


# The helper program (a module attribute, so tests can run a stand-in).
WORKER_SCRIPT = os.path.join(common.BASE_DIR, "people_worker.py")


def setup_problem(options):
    """Why faces cannot be found with these settings ('' when the files are all there;
    whether the helper runs is only known when it starts)."""
    if not options["enabled"]:
        return NOT_ENABLED
    python = python_path(options)
    if not python:
        return ("Set \"python\" in config.json's \"people\" to the python of the people "
                "environment (see SETTINGS.md, \"people\").")
    if not os.path.isfile(python):
        return ("The people environment's python %s is not there (\"python\" in config.json's "
                "\"people\")." % python)
    if not os.path.isfile(WORKER_SCRIPT):
        return "The face helper %s is missing from the program folder." % WORKER_SCRIPT
    for key, what in (("detector", "face detector"), ("recognizer", "face recogniser")):
        path = model_path(options, key)
        if not os.path.isfile(path):
            return ("The %s model %s is not there (\"models_dir\" and \"%s\" in config.json's "
                    "\"people\"; see README, \"People\")." % (what, path, key))
    return ""


def _file_sha256(path):
    import browser_model
    return browser_model.file_sha256(path)


def recipe(options):
    """What the faces are found with: the SHA-1 of the two model files' SHA-256 and the
    settings that change what is found (max_edge, min_score, min_size, max_faces,
    FIND_VERSION). Raises PeopleError when a model file cannot be read."""
    digests = []
    for key in ("detector", "recognizer"):
        path = model_path(options, key)
        digest = _file_sha256(path)
        if not digest:
            raise PeopleError("The model file %s cannot be read." % path)
        digests.append(digest)
    text = "%s\n%s\nfind-%d edge-%d/%d score-%g size-%d faces-%d" % (
        digests[0], digests[1], FIND_VERSION, options["max_edge"], MIN_EDGE,
        options["min_score"], options["min_size"], options["max_faces"])
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def find_options(options):
    """What a "faces" request asks of the helper."""
    return {"max_edge": options["max_edge"], "min_edge": MIN_EDGE,
            "min_size": options["min_size"], "min_score": options["min_score"],
            "max_faces": options["max_faces"]}


# ---------------------------------------------------------------------------
# Pictures

def kind_of(row):
    """The kind of picture looked at for an index row: KIND_360 for a 360-degree photo
    or video (its preview), KIND_POSTER for another video, else KIND_PHOTO."""
    import imageinfo
    keys = row.keys() if hasattr(row, "keys") else row
    if "projection" in keys and row["projection"] in imageinfo.PROJECTIONS_360:
        return KIND_360
    if "media" in keys and row["media"] == "video":
        return KIND_POSTER
    return KIND_PHOTO


def looked_at(row):
    """kind_of(row), or None when the row's picture is not looked at: a video's poster
    and a 360-degree picture only with a stored preview (the row needs has_preview)."""
    kind = kind_of(row)
    if kind != KIND_PHOTO and row["has_preview"] != 1:
        return None
    return kind


_ROW_COLUMNS = "id, library_id, path, content_hash, media, projection, has_preview"


def library_rows(conn, library_ids):
    """The present pictures of some libraries that are looked at (a known content hash;
    videos and 360-degree pictures only with a stored preview), in id order."""
    library_ids = list(library_ids)
    rows = []
    for start in range(0, len(library_ids), ID_CHUNK):
        part = library_ids[start:start + ID_CHUNK]
        for row in conn.execute(
                "SELECT %s FROM images WHERE missing_since IS NULL AND content_hash != '' "
                "AND library_id IN (%s) ORDER BY id" % (_ROW_COLUMNS, ",".join("?" * len(part))),
                part):
            if looked_at(row) is not None:
                rows.append(row)
    rows.sort(key=lambda row: row["id"])
    return rows


def in_band_360(face):
    """True when a face (face_row) of a 360-degree picture is one that is kept (BAND_360)."""
    middle = face["y"] + face["h"] / 2.0
    return BAND_360[0] < middle < BAND_360[1]


def picture_bytes(row, options, previews, helper=None, root=None):
    """The bytes the helper is sent for an index row: its stored preview (JPEG, or PNG),
    else (a photo without one) its original made upright and at most max_edge pixels
    on its long edge. Raises images.ImageReadError (or OSError) when it cannot be read."""
    data = None
    if row["has_preview"] == 1:
        data = previews.read(row["library_id"], row["id"])
    if data is not None:
        return data
    import images
    kind = kind_of(row)
    if kind == KIND_POSTER:
        raise images.ImageReadError("the video's poster frame is missing")
    if kind != KIND_PHOTO:
        raise images.ImageReadError("the 360-degree picture's preview is missing")
    if not root:
        raise images.ImageReadError("there is no preview and the library's folder is not known")
    full = os.path.join(root, *row["path"].split("/"))
    source = images.read_source_bytes(full, helper)
    edge = options["max_edge"]
    return images.prepare_for_llm(source, max_edge=edge, min_short=0,
                                  max_pixels=edge * edge, projection=row["projection"] or "")


def probe_picture(size=320):
    """PNG bytes of the probe face: a face drawn with ellipses (no photo of anyone), which
    the detector finds when the models work. Needs Pillow."""
    from PIL import Image, ImageDraw, ImageFilter
    image = Image.new("RGB", (size, size), (90, 140, 90))
    draw = ImageDraw.Draw(image)
    unit = size / 400.0
    middle = size / 2.0

    def box(x, y, rx, ry):
        return [x - rx * unit, y - ry * unit, x + rx * unit, y + ry * unit]
    draw.pieslice(box(middle, middle - 40 * unit, 120, 130), 180, 360, fill=(60, 40, 30))
    draw.ellipse(box(middle, middle, 100, 130), fill=(220, 180, 150))
    for side in (-40, 40):
        x, y = middle + side * unit, middle - 25 * unit
        draw.ellipse(box(x, y, 20, 11), fill=(245, 245, 245))
        draw.ellipse(box(x, y, 8, 8), fill=(120, 80, 60))
        draw.ellipse(box(x, y, 4, 4), fill=(10, 10, 10))
        draw.line([x - 22 * unit, y - 22 * unit, x + 22 * unit, y - 24 * unit],
                  fill=(70, 50, 40), width=max(1, int(5 * unit)))
    draw.polygon([(middle, middle - 10 * unit), (middle - 14 * unit, middle + 30 * unit),
                  (middle + 14 * unit, middle + 30 * unit)], fill=(195, 150, 120))
    draw.chord(box(middle, middle + 65 * unit, 35, 12), 0, 180, fill=(170, 80, 80))
    image = image.filter(ImageFilter.GaussianBlur(1.5 * unit))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# What a face is like (from what the helper says)

def _clamp(value):
    return max(0.0, min(1.0, value))


def pose(landmarks):
    """(turn, tilt, roll in degrees) of a face from its five landmarks (any units).

    turn: how far the nose tip is from the middle between the eyes, sideways,
    in eye distances (0 for a face looking at the camera; about 0.5 for one
    turned well away); tilt: how far down from the eyes the nose tip is,
    as a part of the way to the mouth (about 0.5 for a face looking
    straight); both measured along the line through the eyes (roll).
    """
    (right_x, right_y), (left_x, left_y), (nose_x, nose_y) = landmarks[0], landmarks[1], \
        landmarks[2]
    mouth_x = (landmarks[3][0] + landmarks[4][0]) / 2.0
    mouth_y = (landmarks[3][1] + landmarks[4][1]) / 2.0
    eyes_x, eyes_y = (right_x + left_x) / 2.0, (right_y + left_y) / 2.0
    dx, dy = left_x - right_x, left_y - right_y
    distance = math.hypot(dx, dy)
    if distance <= 0:
        return 0.0, 0.0, 0.0
    cos, sin = dx / distance, dy / distance

    def along(x, y):
        return ((x - eyes_x) * cos + (y - eyes_y) * sin,
                -(x - eyes_x) * sin + (y - eyes_y) * cos)
    nose_along, nose_down = along(nose_x, nose_y)
    _mouth_along, mouth_down = along(mouth_x, mouth_y)
    turn = nose_along / distance
    tilt = nose_down / mouth_down if mouth_down > 1e-6 else 0.0
    return turn, tilt, math.degrees(math.atan2(dy, dx))


def quality(score, size, sharpness, norm, turn):
    """How good a face is for telling who it is, 0 to 1: the product of how sure the
    detector is (0 at a score of 0.6, 1 from 0.9), its size (0 at 16 pixels, 1 from
    64), its sharpness (0 at 5, 1 from 200: the variance of the Laplacian of the
    112 x 112 face, on a log scale), how much it faces the camera (1 up to a turn of
    0.2, 0 from 0.6) and the length of the recogniser's vector before it was
    normalised (0 at 4, 1 from 8: things that are not faces, or faces that cannot
    be told, get short vectors)."""
    value = (_clamp((score - 0.6) / 0.3) * _clamp((size - 16.0) / 48.0)
             * _clamp((math.log10(max(0.0, sharpness) + 1.0) - 0.7) / 1.3)
             * _clamp(1.0 - (abs(turn) - 0.2) / 0.4) * _clamp((norm - 4.0) / 4.0))
    return round(value, 4)


def face_row(face, width, height):
    """A face of the helper's answer as the faces table keeps it (fractions of the
    picture; see the module notes): a dict, or None when it is not usable."""
    try:
        x, y, w, h = [float(value) for value in face["box"]]
        landmarks = [[float(px), float(py)] for px, py in face["landmarks"]]
        score = float(face["score"])
        sharpness = float(face["sharpness"])
        norm = float(face["norm"])
        embedding = base64.b64decode(face["embedding"], validate=True)
    except (KeyError, TypeError, ValueError):
        return None
    numbers = [x, y, w, h, score, sharpness, norm] + [value for point in landmarks
                                                      for value in point]
    if (len(landmarks) != 5 or len(embedding) != EMBEDDING_BYTES or width <= 0 or height <= 0
            or w <= 0 or h <= 0 or not all(math.isfinite(value) for value in numbers)):
        return None
    vector = struct.unpack("<%df" % EMBEDDING_DIMS, embedding)
    if not all(math.isfinite(value) for value in vector):
        return None
    turn, tilt, _roll = pose(landmarks)
    size = min(w, h)
    return {
        "x": x / width, "y": y / height, "w": w / width, "h": h / height,
        "landmarks": json.dumps([[round(px / width, 5), round(py / height, 5)]
                                 for px, py in landmarks], separators=(",", ":")),
        "score": score, "size": round(size, 2), "sharpness": sharpness, "norm": norm,
        "turn": round(turn, 4), "tilt": round(tilt, 4),
        "quality": quality(score, size, sharpness, norm, turn), "embedding": embedding,
    }


# ---------------------------------------------------------------------------
# The store

def store_path():
    """data/faces.db."""
    return os.path.join(common.data_dir(), "faces.db")


def _stored_version(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
                    ).fetchone() is None:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def connect(path=None, create=True):
    """Open data/faces.db (made when missing, unless create is False: then None).

    Only with create (the runs that write the store) is a new file set up,
    or one of an older layout (FACES_VERSION) emptied first; StoreError for
    one of a newer version (or one whose version cannot be told), which is
    left as it is. Without create, such a file gives None, and nothing is
    written.
    """
    path = path or store_path()
    if not create and not os.path.isfile(path):
        return None
    conn = db.connect(path, check_same_thread=False)
    try:
        version = _stored_version(conn)
        if version == FACES_VERSION:
            return conn
        if not create:
            conn.close()
            return None
        if version is None or version > FACES_VERSION:
            raise StoreError("%s was written by a newer version of Search Images (or is "
                             "damaged): it is left as it is. Use that version, or delete "
                             "the face data (python people.py --delete-all)." % path)
        with conn:
            if version == 1:
                # (Version 2 only added the people: the faces stay.)
                for statement in _PEOPLE_SCHEMA:
                    conn.execute(statement)
                conn.execute("UPDATE meta SET value = ? WHERE key = 'version'",
                             (str(FACES_VERSION),))
            else:
                for name in TABLES:
                    conn.execute("DROP TABLE IF EXISTS %s" % name)
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", (
                    ("version", str(FACES_VERSION)), ("epoch", secrets.token_hex(8)),
                    ("generation", "0")))
    except BaseException:
        conn.close()
        raise
    return conn


def _bump(conn):
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")


def meta(conn):
    """{version, epoch, generation, pictures (looked at), faces, people (with a face),
    named (of those), runtime (what the helper of the last run said it runs on: "OpenCV
    4.14.0, numpy 2.5.4", or '')} of an open store."""
    rows = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}

    def number(key):
        try:
            return int(rows.get(key) or 0)
        except ValueError:
            return 0
    people_count, named = conn.execute(
        "SELECT COUNT(*), TOTAL(name != '') FROM people WHERE id IN (SELECT person_id FROM "
        "face_people)").fetchone()
    return {"version": number("version"), "epoch": rows.get("epoch") or "",
            "generation": number("generation"),
            "pictures": conn.execute("SELECT COUNT(*) FROM face_pictures").fetchone()[0],
            "faces": conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0],
            "people": people_count, "named": int(named), "runtime": rows.get("runtime") or ""}


def read_meta():
    """meta() of data/faces.db, or None when there is none (or it cannot be read)."""
    try:
        store = connect(create=False)
    except sqlite3.Error:
        return None
    if store is None:
        return None
    try:
        return meta(store)
    except sqlite3.Error:
        return None
    finally:
        store.close()


def _iou(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    union = first[2] * first[3] + second[2] * second[3] - inter
    return inter / union if union > 0 else 0.0


_FACE_FIELDS = ("x", "y", "w", "h", "landmarks", "score", "size", "sharpness", "norm", "turn",
                "tilt", "quality", "embedding")


def replace_faces(store, key, faces, now):
    """Put a picture's faces (face_row dicts) in place of the ones it had: a face found
    again where an earlier one was (SAME_FACE_IOU) keeps that face's id; earlier
    faces not found again go. No commit. Returns the ids, in the order of faces."""
    old = [(row[0], (row[1], row[2], row[3], row[4])) for row in store.execute(
        "SELECT id, x, y, w, h FROM faces WHERE content_hash = ? AND kind = ?", key)]
    free = dict(old)
    ids = []
    for face in faces:
        box = (face["x"], face["y"], face["w"], face["h"])
        best, best_iou = None, SAME_FACE_IOU
        for face_id, earlier in free.items():
            value = _iou(box, earlier)
            if value >= best_iou:
                best, best_iou = face_id, value
        values = [sqlite3.Binary(face[name]) if name == "embedding" else face[name]
                  for name in _FACE_FIELDS]
        if best is not None:
            del free[best]
            store.execute("UPDATE faces SET %s, updated = ? WHERE id = ?"
                          % ", ".join("%s = ?" % name for name in _FACE_FIELDS),
                          values + [now, best])
            ids.append(best)
        else:
            cursor = store.execute(
                "INSERT INTO faces (content_hash, kind, %s, updated) VALUES (?, ?, %s, ?)"
                % (", ".join(_FACE_FIELDS), ",".join("?" * len(_FACE_FIELDS))),
                list(key) + values + [now])
            ids.append(cursor.lastrowid)
    for face_id in free:
        store.execute("DELETE FROM faces WHERE id = ?", (face_id,))
    return ids


def record_picture(store, key, recipe_value, reply, now=None):
    """Store what the helper found in a picture (its answer: width, height, faces,
    seconds). No commit. Returns how many faces were kept."""
    now = time.time() if now is None else now
    width, height = int(reply.get("width") or 0), int(reply.get("height") or 0)
    faces = []
    for face in reply.get("faces") or []:
        row = face_row(face, width, height) if isinstance(face, dict) else None
        if row is not None and (key[1] != KIND_360 or in_band_360(row)):
            faces.append(row)
    replace_faces(store, key, faces, now)
    seconds = reply.get("seconds")
    store.execute("INSERT OR REPLACE INTO face_pictures (content_hash, kind, recipe, width, "
                  "height, faces, seconds, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  list(key) + [recipe_value, width, height, len(faces),
                               float(seconds) if isinstance(seconds, (int, float)) else 0.0,
                               now])
    store.execute("DELETE FROM face_failures WHERE content_hash = ? AND kind = ?", key)
    _bump(store)
    return len(faces)


def record_failure(store, key, recipe_value, error, now=None):
    """Count a failed try at a picture (attempts start again for another recipe). The
    faces it had stay. No commit. Returns its attempts now."""
    now = time.time() if now is None else now
    row = store.execute("SELECT recipe, attempts FROM face_failures WHERE content_hash = ? "
                        "AND kind = ?", key).fetchone()
    attempts = (row[1] + 1) if row is not None and row[0] == recipe_value else 1
    store.execute("INSERT OR REPLACE INTO face_failures (content_hash, kind, recipe, error, "
                  "attempts, updated) VALUES (?, ?, ?, ?, ?, ?)",
                  list(key) + [recipe_value, str(error)[:500], attempts, now])
    _bump(store)
    return attempts


def picture_states(store):
    """({(content hash, kind): recipe} of the pictures looked at, {(content hash, kind):
    (recipe, attempts)} of the failed ones)."""
    done = {(row[0], row[1]): row[2] for row in store.execute(
        "SELECT content_hash, kind, recipe FROM face_pictures")}
    failed = {(row[0], row[1]): (row[2], row[3]) for row in store.execute(
        "SELECT content_hash, kind, recipe, attempts FROM face_failures")}
    return done, failed


def _known_keys(conn):
    known = set()
    for row in conn.execute("SELECT DISTINCT content_hash, media, projection FROM images "
                            "WHERE content_hash != ''"):
        kind = kind_of(row)
        if kind is not None:
            known.add((row[0], kind))
    return known


def drop_faceless_people(store):
    """Delete the people that have no face left (named and hidden ones too: a name
    without a face could never be seen, nor taken away, again). No commit. Returns how
    many went."""
    return store.execute("DELETE FROM people WHERE id NOT IN (SELECT person_id FROM "
                         "face_people)").rowcount


def purge(store, conn):
    """Drop the faces (and records) of contents no row of index.db has any more (as that
    kind of picture), and the people left without a face, with their names. Rows that
    are only missing for now, or of a library that is offline, stay in the index, so
    their faces stay. Returns how many pictures' data went."""
    known = _known_keys(conn)
    stored = set()
    for table in ("face_pictures", "face_failures"):
        stored.update((row[0], row[1]) for row in store.execute(
            "SELECT content_hash, kind FROM %s" % table))
    stored.update((row[0], row[1]) for row in store.execute(
        "SELECT DISTINCT content_hash, kind FROM faces"))
    gone = sorted(key for key in stored if key not in known)
    if not gone:
        return 0
    with store:
        for start in range(0, len(gone), ID_CHUNK):
            part = gone[start:start + ID_CHUNK]
            for table in ("faces", "face_pictures", "face_failures"):
                store.executemany("DELETE FROM %s WHERE content_hash = ? AND kind = ?" % table,
                                  part)
        if drop_faceless_people(store):
            # (The people changed: see people_groups.people_version.)
            store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('people_version', "
                          "COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key = "
                          "'people_version'), 0) + 1)")
        _bump(store)
    return len(gone)


def forget_unreferenced(conn):
    """purge() right after rows were deleted from index.db (Delete indexed data, the
    scanner's purge of rows missing too long), when data/faces.db exists, so no face
    outlives its picture. conn is the index.db connection that deleted them. Best
    effort; returns how many pictures' data went."""
    try:
        store = connect(create=False)
    except sqlite3.Error:
        return 0
    if store is None:
        return 0
    try:
        return purge(store, conn)
    except sqlite3.Error:
        return 0
    finally:
        store.close()


WIPE_TIMEOUT = 30.0         # seconds a wipe waits for another program's write to end


def _wipe(path):
    """Empty every table of the face data file at path (but meta) and overwrite what
    they held (secure_delete, a checkpoint, VACUUM). Returns '' when it is empty and
    wiped now, else why not."""
    try:
        conn = sqlite3.connect(path, timeout=WIPE_TIMEOUT)
    except sqlite3.Error as error:
        return str(error)
    try:
        conn.execute("PRAGMA secure_delete=ON")
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        tables = [name for name in TABLES[:-1] if name in present]
        with conn:
            for name in tables:
                conn.execute("DELETE FROM %s" % name)
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("VACUUM")
        left = sum(conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
                   for name in tables)
        if left:
            return "%d row(s) are still there" % left
        if busy is not None and busy[0]:
            return ("the tables are empty, but another program reading the file kept its "
                    "old pages from being overwritten")
    except sqlite3.Error as error:
        return str(error)
    finally:
        conn.close()
    return ""


def delete_all():
    """Delete all face data: data/faces.db and its journal files (the faces, the people
    and their names). Where a file cannot be removed (open elsewhere), its tables are
    emptied and the file wiped instead. Returns {"pictures", "faces", "people",
    "named" (how many there were), "removed": True when the files are gone, "error":
    '' or why the face data could NOT all be deleted (then some of it is still
    there)}."""
    path = store_path()
    result = {"pictures": 0, "faces": 0, "people": 0, "named": 0, "removed": True,
              "error": ""}
    current = read_meta()
    if current is not None:
        result.update(pictures=current["pictures"], faces=current["faces"],
                      people=current["people"], named=current["named"])
    leftover = []
    for name in (path, path + "-wal", path + "-shm", path + "-journal"):
        for attempt in range(DELETE_TRIES):
            try:
                os.remove(name)
                break
            except FileNotFoundError:
                break
            except OSError:
                # (Windows keeps a file that a reader has open for a moment.)
                if attempt == DELETE_TRIES - 1:
                    leftover.append(name)
                else:
                    time.sleep(0.1)
    if not leftover:
        return result
    result["removed"] = False
    if os.path.isfile(path):
        problem = _wipe(path)
        if problem:
            result["error"] = "%s could not be emptied: %s" % (path, common.redact(problem))
    else:
        # (The file went, but a journal of it is still there, open elsewhere.)
        result["error"] = "%s could not be removed (open elsewhere?)" % ", ".join(leftover)
    return result


# ---------------------------------------------------------------------------
# Counts and status (the Manage page)

def counts(conn, config, library_ids, store=None):
    """{library id: {"pictures": pictures looked at by People, "done": looked at with
    the current settings, "faces": faces found in those, "with_faces": pictures of
    them with a face, "pending": to look at, "failed": waiting for a retry,
    "changed": looked at with other settings (their faces stay until they are looked
    at again)}}. When the recipe cannot be told now, pictures looked at count as done."""
    library_ids = list(library_ids)
    result = {library_id: {"pictures": 0, "done": 0, "faces": 0, "with_faces": 0,
                           "pending": 0, "failed": 0, "changed": 0}
              for library_id in library_ids}
    own = store is None
    if own:
        try:
            store = connect(create=False)
        except sqlite3.Error:
            store = None
    try:
        if store is not None:
            done, failed = picture_states(store)
            found = {(row[0], row[1]): row[2] for row in store.execute(
                "SELECT content_hash, kind, faces FROM face_pictures")}
        else:
            done, failed, found = {}, {}, {}
    finally:
        if own and store is not None:
            store.close()
    options = settings(config)
    current = None
    if options["enabled"] and not setup_problem(options):
        try:
            current = recipe(options)
        except PeopleError:
            current = None
    for row in library_rows(conn, library_ids):
        entry = result[row["library_id"]]
        key = (row["content_hash"], kind_of(row))
        entry["pictures"] += 1
        made_with = done.get(key)
        if made_with is not None and (current is None or made_with == current):
            entry["done"] += 1
            entry["faces"] += found.get(key, 0)
            entry["with_faces"] += 1 if found.get(key) else 0
            continue
        if made_with is not None:
            entry["changed"] += 1
        failure = failed.get(key)
        if failure is not None and failure[0] == current and failure[1] >= MAX_ATTEMPTS:
            entry["failed"] += 1
        else:
            entry["pending"] += 1
    return result


def status(config):
    """People for the Manage page: {"enabled", "ready" (faces can be found now: the
    files are there), "problem", "after_scan", "pictures", "faces", "people", "named"
    and "runtime" (kept in data/faces.db, see meta), "stored" (data/faces.db exists)}."""
    options = settings(config)
    problem = setup_problem(options)
    current = read_meta()
    return {"enabled": options["enabled"], "ready": options["enabled"] and not problem,
            "problem": problem if options["enabled"] else "",
            "after_scan": options["after_scan"], "stored": current is not None,
            "pictures": current["pictures"] if current else 0,
            "faces": current["faces"] if current else 0,
            "people": current["people"] if current else 0,
            "named": current["named"] if current else 0,
            "runtime": current["runtime"] if current else ""}


# ---------------------------------------------------------------------------
# The helper

def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def worker_command(options):
    """The command line that starts the helper: the environment's python, isolated from
    this program's environment variables and folders (-I), writing no bytecode (-B)."""
    return [python_path(options), "-I", "-B", WORKER_SCRIPT]


# The environment variables the helper gets of this program's (the rest, API keys
# among them, it never sees): what Windows and a Python need to run, and the
# settings of the libraries it uses (OpenCV, OpenMP, OpenBLAS, MKL).
HELPER_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP",
              "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
              "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
              "PROCESSOR_IDENTIFIER", "OS", "LANG", "LANGUAGE")
HELPER_ENV_PREFIXES = ("OPENCV_", "OMP_", "OPENBLAS_", "MKL_", "LC_")
# ... and these, unless set: OpenBLAS (under numpy) would otherwise set aside a
# buffer for every processor, close to a gigabyte per helper that does no
# matrix work to speak of; OpenCV's own threads are "threads".
HELPER_ENV_DEFAULTS = {"OPENCV_LOG_LEVEL": "ERROR", "OPENBLAS_NUM_THREADS": "1",
                       "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def helper_environment(environ=None):
    """The environment variables the helper is started with (see HELPER_ENV)."""
    environ = os.environ if environ is None else environ
    env = {}
    for name, value in environ.items():
        upper = name.upper()
        if upper in HELPER_ENV or upper.startswith(HELPER_ENV_PREFIXES):
            env[name] = value
    present = {name.upper() for name in env}
    for name, value in HELPER_ENV_DEFAULTS.items():
        if name not in present:
            env[name] = value
    return env


class Helper:
    """One running helper program (people_worker.py), asked one thing at a time."""

    def __init__(self, options):
        self.options = options
        self.proc = None
        self.info = {}
        self._answers = queue.Queue()
        self._stderr = collections.deque(maxlen=STDERR_LINES)
        self._next_id = 0
        self._lock = threading.Lock()
        self._error_reader = None

    def start(self):
        """Start the helper and load the models. Raises HelperError."""
        command = worker_command(self.options)
        env = helper_environment()
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=common.ensure_dir(
                                             common.data_dir()), env=env,
                                         creationflags=_no_window())
        except OSError as error:
            raise HelperError("The face helper cannot be started with %s: %s"
                              % (command[0], error.strerror or error))
        threading.Thread(target=self._read_answers, args=(self.proc.stdout,), daemon=True,
                         name="people-helper-out").start()
        self._error_reader = threading.Thread(target=self._read_errors,
                                              args=(self.proc.stderr,), daemon=True,
                                              name="people-helper-err")
        self._error_reader.start()
        hello = self.request({"op": "hello"}, HELLO_TIMEOUT)
        if hello.get("worker") != people_worker.WORKER_VERSION:
            self.stop()
            raise HelperError("The face helper is of another version (%s, not %s): use the "
                              "people_worker.py of this program." % (
                                  hello.get("worker"), people_worker.WORKER_VERSION))
        if hello.get("problem"):
            self.stop()
            raise HelperError(hello["problem"])
        self.info = hello
        try:
            self.request({"op": "load", "detector": model_path(self.options, "detector"),
                          "recognizer": model_path(self.options, "recognizer"),
                          "threads": self.options["threads"]}, LOAD_TIMEOUT)
        except PictureError as error:
            self.stop()
            raise HelperError(str(error))
        return self

    def _read_answers(self, stream):
        try:
            for line in iter(stream.readline, b""):
                self._answers.put(line)
        except (OSError, ValueError):
            pass
        self._answers.put(None)

    def _read_errors(self, stream):
        try:
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            pass

    def stderr_tail(self, lines=5):
        """The helper's last lines of standard error ('' when it wrote none)."""
        return " | ".join(list(self._stderr)[-lines:])

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _gone(self, why, ended=False):
        """The HelperError for a helper that stopped (ended: its output ended) or is
        stopped now."""
        code = None
        if self.proc is not None and ended:
            # (Its exit code and last words, when it is ending.)
            try:
                code = self.proc.wait(QUIT_WAIT)
            except subprocess.TimeoutExpired:
                code = None
            if self._error_reader is not None:
                self._error_reader.join(QUIT_WAIT)
        elif self.proc is not None:
            code = self.proc.poll()
        tail = self.stderr_tail()
        self.stop()
        return HelperError("The face helper %s%s%s" % (
            why, " (exit code %s)" % code if code is not None else "",
            ": " + common.redact(tail) if tail else "."))

    def request(self, payload, timeout):
        """Send one request; its answer (a dict). Raises PictureError when the helper
        says it cannot do it, HelperError when the helper stopped or did not answer in
        time (it is then stopped)."""
        with self._lock:
            if not self.alive():
                raise self._gone("is not running", ended=True)
            self._next_id += 1
            request_id = self._next_id
            message = dict(payload, id=request_id)
            line = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise self._gone("stopped", ended=True)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise self._gone("did not answer within %g seconds" % timeout)
                try:
                    answer = self._answers.get(timeout=left)
                except queue.Empty:
                    continue
                if answer is None:
                    raise self._gone("stopped", ended=True)
                try:
                    reply = json.loads(answer.decode("utf-8"))
                except ValueError:
                    continue            # (not an answer: never written by the helper)
                if not isinstance(reply, dict) or reply.get("id") != request_id:
                    continue
                if not reply.get("ok"):
                    raise PictureError(str(reply.get("error") or "the helper gave no reason"))
                return reply

    def faces(self, data, options):
        """The helper's answer for one picture (bytes)."""
        request = dict(find_options(options), op="faces",
                       image=base64.b64encode(data).decode("ascii"))
        return self.request(request, PICTURE_TIMEOUT)

    def stop(self):
        """Ask the helper to quit, and end it when it does not."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.stdin.write(b'{"op":"quit"}\n')
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(QUIT_WAIT)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(QUIT_WAIT)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass


def check_probe(helper, options):
    """Ask a started helper to find the probe face. Returns how long it took (seconds);
    raises HelperError when it finds none (the models do not work as they should)."""
    started = time.monotonic()
    try:
        reply = helper.faces(probe_picture(), dict(options, min_size=8,
                                                   min_score=min(options["min_score"], 0.7)))
    except PictureError as error:
        raise HelperError("The face helper cannot look at the test face: %s" % error)
    if not reply.get("faces"):
        raise HelperError("The face helper finds no face in the test face (a face drawn with "
                          "ellipses that the YuNet detector finds): are \"detector\" and "
                          "\"recognizer\" the right model files?")
    return time.monotonic() - started


def check(config):
    """Start the helper, load the models and find the probe face: {"ok", "problem",
    "python", "opencv", "numpy", "worker", "seconds"} (for people.py --check and the
    Manage page)."""
    options = settings(config)
    result = {"ok": False, "problem": "", "python": "", "opencv": "", "numpy": "",
              "worker": 0, "seconds": 0.0}
    problem = setup_problem(options)
    if problem:
        result["problem"] = problem
        return result
    started = time.monotonic()
    helper = Helper(options)
    try:
        helper.start()
        check_probe(helper, options)
        result.update(ok=True, python=str(helper.info.get("python") or ""),
                      opencv=str(helper.info.get("opencv") or ""),
                      numpy=str(helper.info.get("numpy") or ""),
                      worker=helper.info.get("worker") or 0)
    except HelperError as error:
        result["problem"] = str(error)
    finally:
        helper.stop()
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


# ---------------------------------------------------------------------------
# The run

def _new_summary():
    return {"configured": False, "looked": 0, "faces": 0, "with_faces": 0, "failed": 0,
            "gave_up": 0, "up_to_date": 0, "pending": 0, "removed": 0, "offline": 0,
            "restarts": 0, "workers": 0, "seconds": 0.0, "stopped": False, "error": "",
            "people": None}


def _offline_libraries(rows, libraries, ctx, helper):
    """{library id} of the libraries whose folder cannot be reached now, among those with
    rows that have no stored preview (their pictures wait for a later run)."""
    if helper is not None and getattr(helper, "load_image", None) is not None:
        return set()
    needed = {row["library_id"] for row in rows if row["has_preview"] != 1}
    if not needed:
        return set()
    import library_ops
    offline = set()
    for library in libraries:
        if library["id"] in needed:
            root = common.library_root(library)
            found = library_ops.folder_status(root, timeout=FOLDER_CHECK_TIMEOUT)
            if not found["reachable"]:
                offline.add(library["id"])
                ctx.log("People: the pictures of %s without a preview wait for a later run: "
                        "the folder %s cannot be reached (offline?)."
                        % (common.library_label(library), root or "(none)"), "warn")
    return offline


def pending_rows(conn, store, library_ids, recipe_value, retry_failed=False):
    """(the rows to look at, one per content and kind -- a copy with a stored preview
    rather than one without --, newest first; how many are up to date; how many wait
    for a retry)."""
    done, failed = picture_states(store)
    chosen = {}
    up_to_date = waiting = 0
    for row in library_rows(conn, library_ids):
        key = (row["content_hash"], kind_of(row))
        if done.get(key) == recipe_value:
            up_to_date += 1
            continue
        if key in chosen:
            if row["has_preview"] == 1 and chosen[key]["has_preview"] != 1:
                chosen[key] = row
            continue
        failure = failed.get(key)
        if (not retry_failed and failure is not None and failure[0] == recipe_value
                and failure[1] >= MAX_ATTEMPTS):
            waiting += 1
            continue
        chosen[key] = row
    todo = sorted(chosen.values(), key=lambda row: -row["id"])
    return todo, up_to_date, waiting


def update(conn, config, ctx, library_ids=None, limit=None, retry_failed=False, helper=None):
    """Find the faces of the pictures not looked at yet (with the current recipe).

    conn is an index.db connection; ctx a jobs.JobContext. Only enabled
    libraries (limited to library_ids) are looked at; at most limit
    pictures (newest first), one per content and kind (copies share
    their faces). Nothing is started when every picture is up to date.
    "workers" helpers run side by side (each with "threads" threads); the
    first finds the probe face before any picture is sent (a helper that
    finds none stops the run). A picture the helper cannot read is
    counted (failed) and tried again by later runs, up to MAX_ATTEMPTS
    times (retry_failed: again); a helper that stops or does not answer
    in time is started again, up to MAX_RESTARTS times. A picture without
    a stored preview whose library folder cannot be reached waits for a
    later run (offline). Results are committed as they come (a stopped
    run keeps what it did). helper is the helper module (load_image) of
    config.json, for originals.

    Then, unless the run stopped, the faces are grouped into people again
    when they changed (people_groups.group_if_due).

    Summary keys: configured, looked, faces, with_faces, failed, gave_up
    (pictures that now wait for a retry), up_to_date, pending, removed,
    offline, restarts, workers, seconds, stopped, error, people (the
    grouping's summary, or None when it did not run).
    """
    summary = _new_summary()
    options = settings(config)
    if not options["enabled"]:
        return summary
    summary["configured"] = True
    problem = setup_problem(options)
    if problem:
        summary["error"] = problem
        ctx.log(problem, "error")
        return summary
    wanted = None if library_ids is None else set(library_ids)
    libraries = [library for library in common.enabled_libraries(config)
                 if library.get("id") and (wanted is None or library["id"] in wanted)]
    try:
        recipe_value = recipe(options)
    except PeopleError as error:
        summary["error"] = str(error)
        ctx.log(summary["error"], "error")
        return summary
    try:
        store = connect()
    except sqlite3.Error as error:
        summary["error"] = "The faces cannot be stored: %s" % error
        ctx.log(summary["error"], "error")
        return summary
    try:
        summary["removed"] = purge(store, conn)
        todo, summary["up_to_date"], waiting = pending_rows(
            conn, store, [library["id"] for library in libraries], recipe_value, retry_failed)
        if waiting:
            ctx.log("People: %d picture(s) failed %d times and wait (Retry failed, or people.py "
                    "--retry-failed)." % (waiting, MAX_ATTEMPTS))
        offline = _offline_libraries(todo, libraries, ctx, helper) if todo else set()
        if offline:
            summary["offline"] = sum(1 for row in todo if row["library_id"] in offline
                                     and row["has_preview"] != 1)
            todo = [row for row in todo if not (row["library_id"] in offline
                                                and row["has_preview"] != 1)]
        if limit is not None and len(todo) > max(0, limit):
            todo = todo[:max(0, limit)]
        summary["pending"] = len(todo)
        if todo:
            roots = {library["id"]: common.library_root(library) for library in libraries}
            _run(store, options, recipe_value, todo, ctx, summary, roots, helper)
        if not summary["error"] and not summary["stopped"] and not ctx.should_stop():
            # The faces found, grouped into people (people_groups.py), when they changed.
            import people_groups
            summary["people"] = people_groups.group_if_due(store, options, ctx, conn=conn)
    finally:
        store.close()
    return summary


def _start_helpers(options, count, ctx, summary):
    """Start count helpers side by side; the first checks the probe face. Returns the
    started ones (at least one), or [] with summary["error"] set."""
    def start(index):
        helper = Helper(options)
        try:
            helper.start()
            if index == 0:
                check_probe(helper, options)
        except HelperError as error:
            helper.stop()
            return error
        return helper
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        started = list(pool.map(start, range(count)))
    helpers = [item for item in started if isinstance(item, Helper)]
    problems = [item for item in started if not isinstance(item, Helper)]
    if problems and (not helpers or not isinstance(started[0], Helper)):
        for helper in helpers:
            helper.stop()
        summary["error"] = str(problems[0])
        ctx.log(summary["error"], "error")
        return []
    if problems:
        ctx.log("People: %d of %d helper(s) could not start: %s" % (
            len(problems), count, problems[0]), "warn")
    return helpers


def _major_minor(runtime):
    """"4.14" of "OpenCV 4.14.0, numpy 2.5.4" ('' when it says no OpenCV version)."""
    words = runtime.split(",")[0].split()
    parts = words[1].split(".") if len(words) > 1 else []
    return ".".join(parts[:2])


def note_runtime(store, info, ctx):
    """Keep what the helper (its hello: info) runs on in the store's meta ("runtime"),
    and warn when OpenCV is another version than the one that found the faces kept:
    their vectors may not match those of the faces found now. No commit."""
    runtime = "OpenCV %s, numpy %s" % (info.get("opencv") or "?", info.get("numpy") or "?")
    row = store.execute("SELECT value FROM meta WHERE key = 'runtime'").fetchone()
    before = row[0] if row is not None and row[0] else ""
    if (before and _major_minor(before) != _major_minor(runtime)
            and store.execute("SELECT 1 FROM faces LIMIT 1").fetchone() is not None):
        ctx.log("People: the faces kept were found with %s; this run uses %s. Faces found "
                "by different OpenCV versions may not be grouped well together: delete the "
                "face data and find the faces again if people get mixed up." % (before, runtime),
                "warn")
    store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('runtime', ?)", (runtime,))
    return runtime


def _run(store, options, recipe_value, todo, ctx, summary, roots, loader):
    """Look at todo (index rows) with the helpers (see update)."""
    import cache
    import images
    started = time.monotonic()
    ctx.stage("faces", total=len(todo))
    count = max(1, min(options["workers"], len(todo)))
    ctx.log("Finding faces in %d picture(s) with %d helper(s)..." % (len(todo), count))
    helpers = _start_helpers(options, count, ctx, summary)
    if not helpers:
        summary["seconds"] = round(time.monotonic() - started, 3)
        return
    summary["workers"] = len(helpers)
    note_runtime(store, helpers[0].info, ctx)
    idle = queue.Queue()
    for helper in helpers:
        idle.put(helper)
    previews = cache.PreviewStore()
    restarts = {"count": 0}
    restart_lock = threading.Lock()
    fatal = []
    failures = []

    def look(row):
        """(row, the helper's answer or an Exception) for one picture."""
        if fatal or ctx.should_stop():
            return row, None
        try:
            data = picture_bytes(row, options, previews, loader, roots.get(row["library_id"]))
        except (images.ImageReadError, OSError) as error:
            return row, PictureError(common.redact(str(error)))
        helper = idle.get()
        try:
            if helper is None:
                return row, None
            return row, helper.faces(data, options)
        except PictureError as error:
            return row, error
        except HelperError as error:
            # (This picture may be what stopped it: it counts as a failed try.)
            with restart_lock:
                restarts["count"] += 1
                again = restarts["count"] <= MAX_RESTARTS and not fatal
            if again:
                replacement = Helper(options)
                try:
                    replacement.start()
                    helper = replacement
                    ctx.log("People: the helper was started again after: %s" % error, "warn")
                except HelperError as other:
                    replacement.stop()
                    fatal.append("%s; starting it again failed: %s" % (error, other))
                    helper = None
            else:
                fatal.append("%s (it stopped %d times in this run)" % (error,
                                                                       restarts["count"]))
                helper = None
            return row, PictureError(str(error))
        finally:
            if helper is not None:
                idle.put(helper)
            elif fatal:
                idle.put(None)      # (wakes up the others: there is no helper to give)

    pending_writes = 0
    last_commit = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(helpers))
    try:
        rows = iter(todo)
        running = set()

        def fill():
            while len(running) < 2 * len(helpers) and not fatal and not ctx.should_stop():
                row = next(rows, None)
                if row is None:
                    return
                running.add(pool.submit(look, row))

        fill()
        while running:
            finished, _ = concurrent.futures.wait(
                running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in finished:
                running.discard(future)
                row, outcome = future.result()
                if outcome is None:
                    continue
                key = (row["content_hash"], kind_of(row))
                if isinstance(outcome, Exception):
                    summary["failed"] += 1
                    failures.append((row["id"], str(outcome)))
                    if record_failure(store, key, recipe_value, outcome) >= MAX_ATTEMPTS:
                        summary["gave_up"] += 1
                else:
                    kept = record_picture(store, key, recipe_value, outcome)
                    summary["looked"] += 1
                    summary["faces"] += kept
                    summary["with_faces"] += 1 if kept else 0
                pending_writes += 1
                ctx.step(1)
            if pending_writes and (pending_writes >= COMMIT_ROWS
                                   or time.monotonic() - last_commit >= COMMIT_SECONDS):
                store.commit()
                pending_writes, last_commit = 0, time.monotonic()
            ctx.wait_if_paused()
            fill()
        store.commit()
    finally:
        pool.shutdown(wait=True)
        try:
            store.commit()
        except sqlite3.Error:
            pass
        stopped = []
        while not idle.empty():
            helper = idle.get_nowait()
            if helper is not None:
                stopped.append(helper)
        for helper in stopped:
            helper.stop()
        summary["restarts"] = restarts["count"]
        summary["seconds"] = round(time.monotonic() - started, 3)
    if fatal:
        summary["error"] = fatal[0]
        ctx.log(summary["error"], "error")
    summary["stopped"] = ctx.should_stop() and not fatal
    _log_done(ctx, summary, failures)


def _log_done(ctx, summary, failures):
    for image_id, error in failures[:FAILURE_LOG_LIMIT]:
        ctx.log("Picture %d: no faces looked for: %s" % (image_id, error), "warn")
    if len(failures) > FAILURE_LOG_LIMIT:
        ctx.log("... and %d more." % (len(failures) - FAILURE_LOG_LIMIT), "warn")
    if summary["looked"] or summary["failed"]:
        each = summary["seconds"] / summary["looked"] if summary["looked"] else 0.0
        ctx.log("Looked for faces in %d picture(s) in %.1f s (%.2f s a picture): %d face(s) in "
                "%d of them%s." % (summary["looked"], summary["seconds"], each, summary["faces"],
                                   summary["with_faces"], "; %d failed" % summary["failed"]
                                   if summary["failed"] else ""))


def after_run(conn, config, ctx, library_ids=None, helper=None):
    """The faces step at the end of a scan (when People is on, "after_scan" is not
    false, and the run was not stopped): update() for the scanned libraries. Returns
    its summary, or None. A setup that does not work is said once in the log (the
    scan itself is not failed by it)."""
    options = settings(config)
    if not options["enabled"] or not options["after_scan"] or ctx.should_stop():
        return None
    problem = setup_problem(options)
    if problem:
        ctx.log("Faces were not looked for: %s" % problem, "warn")
        return None
    try:
        summary = update(conn, config, ctx, library_ids=library_ids, helper=helper)
    except (sqlite3.Error, OSError) as error:
        ctx.log("Faces could not be looked for: %s" % common.redact(str(error)), "warn")
        return None
    if summary["error"]:
        # (Said in the log already; the scan's own result stands.)
        summary["warning"], summary["error"] = summary["error"], ""
    return summary


# ---------------------------------------------------------------------------
# Command line

def _print_status(config, out):
    conn = db.connect()
    try:
        ids = [library["id"] for library in config.get("libraries", []) if library.get("id")]
        counted = counts(conn, config, ids)
    finally:
        conn.close()
    info = status(config)
    print("People is %s.%s" % ("on" if info["enabled"] else "off", (" " + info["problem"])
                               if info["problem"] else ""), file=out)
    print("data/faces.db: %s" % ("%d face(s) in %d picture(s) looked at%s" % (
        info["faces"], info["pictures"], " (last run: %s)" % info["runtime"]
        if info["runtime"] else "") if info["stored"] else "none"), file=out)
    for library in config.get("libraries", []):
        entry = counted.get(library.get("id"))
        if entry and entry["pictures"]:
            print("  %s: %d of %d picture(s) looked at, %d face(s) in %d; %d to look at%s%s" % (
                common.library_label(library), entry["done"], entry["pictures"], entry["faces"],
                entry["with_faces"], entry["pending"],
                ", %d failed" % entry["failed"] if entry["failed"] else "",
                ", %d looked at with other settings" % entry["changed"]
                if entry["changed"] else ""), file=out)
    return 0


def main(argv=None, out=None):
    """Command line entry point. Returns the process exit code."""
    import jobs
    out = out or sys.stdout
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(description="Find the faces in the pictures (People; see "
                                                 "\"people\" in SETTINGS.md).")
    parser.add_argument("--library", action="append", metavar="NAME_OR_ID",
                        help="Only this library (by name or id). Can be repeated.")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Look at most at N pictures in this run.")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Also look again at pictures that failed %d times." % MAX_ATTEMPTS)
    parser.add_argument("--check", action="store_true",
                        help="Start the face helper, load the models, find a test face, and "
                             "say how that went.")
    parser.add_argument("--status", action="store_true",
                        help="Say how many pictures were looked at and how many faces were "
                             "found.")
    parser.add_argument("--delete-all", action="store_true",
                        help="Delete all face data (data/faces.db).")
    parser.add_argument("--yes", action="store_true", help="With --delete-all: do not ask.")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        print("--limit must be 0 or more.", file=out)
        return 2
    if sum(bool(value) for value in (args.check, args.status, args.delete_all)) > 1:
        print("--check, --status and --delete-all go one at a time.", file=out)
        return 2
    try:
        config = common.load_config()
    except common.ConfigError as error:
        print("Error: %s" % error, file=out)
        return 2
    if args.check:
        result = check(config)
        if result["ok"]:
            print("The face helper works: Python %s, OpenCV %s, numpy %s (%.1f s)."
                  % (result["python"], result["opencv"], result["numpy"], result["seconds"]),
                  file=out)
            return 0
        print("The face helper does not work: %s" % result["problem"], file=out)
        return 1
    lock = jobs.WorkerLock()
    if not lock.acquire("people.py"):
        print("Another job is running: %s. Try again when it has finished."
              % (lock.holder() or "another process"), file=out)
        return 1
    try:
        if args.delete_all:
            if not args.yes:
                answer = input("Delete all face data (data/faces.db)? Type yes: ")
                if answer.strip().lower() != "yes":
                    print("Nothing was deleted.", file=out)
                    return 1
            result = delete_all()
            if result["error"]:
                print("The face data was NOT deleted (not all of it): %s. Close what has "
                      "data/faces.db open and run --delete-all again." % result["error"],
                      file=out)
                return 1
            print("Deleted the face data: %d face(s) of %d picture(s)%s." % (
                result["faces"], result["pictures"], "" if result["removed"] else
                " (data/faces.db is open elsewhere: it was emptied and wiped, and goes "
                "with the next --delete-all)"), file=out)
            return 0
        db.init(config)
        if args.status:
            return _print_status(config, out)
        library_ids = None
        if args.library:
            library_ids = []
            for wanted in args.library:
                library = common.get_library(config, wanted)
                if library is None:
                    print("Unknown library: %s" % wanted, file=out)
                    return 2
                library_ids.append(library["id"])
        if not enabled(config):
            print(NOT_ENABLED, file=out)
            return 2
        ctx = jobs.ConsoleContext("people")
        helper = common.load_helper(config.get("helper", ""))
        conn = db.connect()
        try:
            with ctx.stop_on_ctrl_c():
                summary = update(conn, config, ctx, library_ids=library_ids, limit=args.limit,
                                 retry_failed=args.retry_failed, helper=helper)
        finally:
            conn.close()
    except (db.DatabaseError, sqlite3.Error) as error:
        print("Error: %s" % error, file=out)
        return 2
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C). What was found is kept; run people.py again to continue.",
              file=out)
        return 130
    finally:
        lock.release()
    if summary["error"]:
        return 1
    if summary["stopped"]:
        ctx.log("Stopped as asked. What was found is kept; run people.py again to continue.")
    elif not summary["pending"]:
        ctx.log("Faces are up to date (%d picture(s))." % summary["up_to_date"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
