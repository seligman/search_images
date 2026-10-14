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
                   frames, updated: the pictures looked at, with the size the
                   helper saw (pixels), how many faces it kept, and of a
                   video looked at in frames how many (0 for a picture)
    faces          id, content_hash, kind, x, y, w, h (the box, as fractions of
                   the picture's width and height, upright as shown),
                   landmarks (JSON [[x, y] x 5], fractions too: the person's
                   right eye, left eye, nose tip, right and left mouth
                   corner), score (the detector's confidence), size (the
                   box's short side in pixels of the picture looked at),
                   sharpness, norm, turn, tilt (see pose()), quality (see
                   quality()), embedding (SFace's vector: 128 little-endian
                   float32, unit length), at and seen (a video's face: the
                   seconds into the video of the frame it is from, and JSON
                   [[seconds, x, y, w, h], ...] of every frame its person
                   was seen in; NULL and '' else: version 4), updated
    face_failures  content_hash, kind, recipe, error, attempts, updated: the
                   pictures that could not be looked at (after MAX_ATTEMPTS
                   tries a picture waits for --retry-failed)
    people, face_people, face_rejects, face_marks
                   the people the faces were grouped into, and what the
                   owner said about them (version 2; see people_groups.py)
    face_crops     face_id, jpeg, updated: a small picture of each face found
                   in a picture looked at in full size (version 3), which the
                   People page shows instead of one cut from the preview

Like the looks (looks.py), faces belong to a picture's content (its
content hash) and the kind of picture looked at (KIND_PHOTO: an ordinary
picture, its preview; KIND_POSTER: a video's poster frame; KIND_360: the
flat, equirectangular preview of a 360-degree photo or video), so copies
share them and a moved or renamed picture keeps them. Of a 360-degree
picture only the faces near its horizon are kept (BAND_360: there the
projection hardly stretches them, and that is where the people holding
the camera are); the viewer draws no boxes on those.

A wide panorama (WIDE_ASPECT times as wide as it is high, or as high as
it is wide: the viewer's wide pictures) or a very large picture
("full_megapixels") hides its faces in a preview of 2048 pixels: the
people in it are a few pixels high there. Such a photo (full_size()) is
looked at in full size instead, read from its original file (at most
FULL_MAX_PIXELS), which the helper looks at in tiles (people_worker.py);
it keeps its kind (KIND_PHOTO), so its faces and what was said about them
stay, but has a recipe of its own (recipes(): RECIPE_FULL), and a small
picture of each of its faces is kept (face_crops), as the preview is too
small to show them.

A video is looked at in several frames ("frames", the most; one
about every "frame_seconds" seconds, spread over it, and its poster
frame), read from its file with ffmpeg on the processor (videos.
frame_source: a 360 video's frames are stitched panoramas, of which only
the faces near the horizon are kept, as of its poster). The faces of one
person in the frames are linked (video_faces: their vectors, TRACK_MATCH,
never two faces of one frame, each alike to the best of them, VIDEO_SAME,
or in the place of one that is, VIDEO_NEAR) and kept as one face: the
best of them, with a vector made of them all, the second it was seen at
("at", where the viewer starts the video) and all the moments that
person was seen at ("seen"), and a picture of it (face_crops: the
preview, the poster, shows another moment); not a face seen once that
nobody could tell (VIDEO_LONE_QUALITY), nor more than VIDEO_MAX_FACES. A
video keeps its kind (KIND_POSTER, or KIND_360), so the faces of its
poster found before keep their ids when found again as the same person
(and what was said about them: the poster frame is looked at again), but
has a recipe of its own (RECIPE_VIDEO). The clips of a dashcam's drives
(drives.py, DASHCAM_SECONDS) are not looked at unless "drives" is true:
their faces (other drivers) are dropped, but those in a person. Without
ffmpeg the videos wait, and so do those whose folder goes away while
they are read, or that take longer than VIDEO_READ_TIMEOUT to read when
it did; a run stopped hears it between frames. "frames": 0 looks at the
poster (the preview) alone, as before.

A picture is looked at again when its recipe changes (other model files
or settings that change what is found: see recipes(); each kind of
picture has its own, so a change that only concerns the pictures looked
at in full size looks at those again, not every picture); its faces are
then replaced, and a face found again in the same place keeps its id
(replace_faces), so what is later said about a face stays with it. Faces
whose content no index row has any more go (purge, and
forget_unreferenced right after rows are deleted), and so do the people
left without a face, names and all; what goes is overwritten in the file
(secure_delete), not only let go.

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
# connect() knows how to bring it up to date (versions 1 to 3: only tables
# and columns were added since).
FACES_VERSION = 4
# Raise when faces are found differently (people_worker.find_faces, how the
# pictures are prepared): every picture is looked at again.
FIND_VERSION = 1
# Raise when the pictures looked at in full size are looked at differently
# (only those are looked at again).
FULL_VERSION = 1
# ... and the videos looked at in frames (only those).
VIDEO_VERSION = 1
KIND_PHOTO = "photo"
KIND_POSTER = "video-poster"
KIND_360 = "360"
# The recipe of a photo looked at in full size (see recipes()); it stays a
# KIND_PHOTO.
RECIPE_FULL = "full"
# ... and of a video looked at in frames (it stays a KIND_POSTER, or a KIND_360).
RECIPE_VIDEO = "video"
# A video's frames: the JPEG the helper gets of each, and how many videos are
# read at the same time (ffmpeg decodes with several threads of its own).
VIDEO_QUALITY = 90
VIDEO_AT_ONCE = 4
# Two faces of a video's frames are one person's when their vectors are this
# alike on average (link_faces: people_groups.average_linkage). Less than the
# grouping's "match": in one video, under the same light, a person's faces
# are small and often blurred, and the vectors of one face in frames a few
# seconds apart are often only this alike; never two faces of one frame. Only
# faces whose vector is at least TRACK_NORM long are linked so (see
# people_groups.REST_NORM: things and faces that cannot be told have short
# vectors, which are alike among themselves); the others join a person only.
TRACK_MATCH = 0.3
TRACK_NORM = 7.0
# ... but a face linked so stays with the face kept of them (the best) only
# when its own vector is at least VIDEO_SAME alike to that one's (the
# grouping's "match" by default), or when it is where a face that stays is in
# another frame (their middles at most VIDEO_NEAR times the larger face's
# width apart across, and its height down), at least TRACK_MATCH alike to
# that one and VIDEO_NEAR_FLOOR to the best: average linkage at TRACK_MATCH
# also takes in another person who looks a little like the ones linked
# already (two women in sunglasses, a crowd), whose moments would then be
# the kept face's, while one person turning away in place stays one. The
# others are linked again among themselves (_people_seen).
VIDEO_SAME = 0.4
VIDEO_NEAR = 1.0
VIDEO_NEAR_FLOOR = 0.2
# A person of a video whose faces' vectors are all shorter than this is left
# out: in a video's many frames the detector finds many things (fire, wheels,
# flowers, sand) and faces turned away, which no person ever takes (their
# quality is at most a quarter: see quality(); people_groups.REST_NORM).
VIDEO_MIN_NORM = 5.0
# ... and so is one seen in a single frame whose face is of a lower quality
# than this (a blurred face in a crowd seen once, a face on a screen in the
# background): nobody could tell who it is, nor would the grouping.
VIDEO_LONE_QUALITY = 0.1
# The most faces kept of one video (at most "max_faces" too): the best of them. A
# video of a crowd (a stadium, a parade) shows hundreds of strangers in its
# frames, few of them more than a blur.
VIDEO_MAX_FACES = 32
# Two moments of a video (seconds) are one when they are this close.
SAME_TIME = 0.05
# A drive (drives.py) whose camera cut its clips at most this long (seconds) is a
# dashcam's: they cut every minute or few (1 to 5), and see other drivers. Action
# cameras cut a long recording into chapters by their size, mostly longer than
# that, of the people with them: those are looked at as any other video.
DASHCAM_SECONDS = 300
# A picture this many times as wide as it is high (or as high as it is wide)
# is a wide panorama (images.WIDE_ASPECT: the viewer's wide pictures).
WIDE_ASPECT = 2.2
# The largest picture looked at in full size, in pixels (a larger one is
# scaled down to this) and on its long edge, and how many are looked at at
# the same time (each takes a few hundred MB, in this program and in a helper).
FULL_MAX_PIXELS = 100000000
FULL_MAX_EDGE = 32768
FULL_AT_ONCE = 2
FULL_QUALITY = 90           # the JPEG the helper gets of it
# The helper looks at it in tiles of at most this many pixels a side, which
# overlap by more than the largest face a size of the pyramid keeps (its
# box's short side people_worker.BAND_HIGH, its long side about 1.4 times
# that).
FULL_TILE = 2048
FULL_OVERLAP = 416
FULL_TIMEOUT = 900.0        # seconds the helper may take for one such picture
# Seconds reading an original's bytes may take (a share that stalls), and reading
# the frames of one video with ffmpeg (each frame has its own limit too).
READ_TIMEOUT = 300.0
VIDEO_READ_TIMEOUT = 600.0
READ_POLL = 0.25            # seconds between looks at a read (stop asked, time up)
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
    # Wide panoramas and very large pictures (of at least this many million
    # pixels) are looked at in full size (full_size()).
    "full_size": True,
    "full_megapixels": 60,
    # Videos: the most frames looked at in one (0: only its poster, the
    # preview), about one every frame_seconds; and the clips of drives
    # (dashcams: other drivers' faces) too, or not.
    "frames": 12,
    "frame_seconds": 4.0,
    "drives": False,
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
    "full_megapixels": (5, 1000),
    "frames": (0, 60),
    "frame_seconds": (0.5, 600),
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


class Stopped(Exception):
    """The run was asked to stop while a picture's file was read (it waits for the next)."""


# What _run's look gives for a picture whose library folder went away during the run.
OFFLINE = object()


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS face_pictures (content_hash TEXT NOT NULL, kind TEXT NOT NULL, "
    "recipe TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, "
    "faces INTEGER NOT NULL, seconds REAL NOT NULL DEFAULT 0, updated REAL NOT NULL, "
    "frames INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (content_hash, kind))",
    "CREATE TABLE IF NOT EXISTS faces (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "content_hash TEXT NOT NULL, kind TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, "
    "w REAL NOT NULL, h REAL NOT NULL, landmarks TEXT NOT NULL, score REAL NOT NULL, "
    "size REAL NOT NULL, sharpness REAL NOT NULL, norm REAL NOT NULL, turn REAL NOT NULL, "
    "tilt REAL NOT NULL, quality REAL NOT NULL, embedding BLOB NOT NULL, "
    "updated REAL NOT NULL, at REAL, seen TEXT NOT NULL DEFAULT '')",
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
) + (
    # The faces' pictures of the pictures looked at in full size: version 3.
    "CREATE TABLE IF NOT EXISTS face_crops (face_id INTEGER PRIMARY KEY "
    "REFERENCES faces (id) ON DELETE CASCADE, jpeg BLOB NOT NULL, updated REAL NOT NULL)",
)
# The columns added to tables of an older file (version 4: the videos' frames).
_COLUMNS = (("faces", "at", "REAL"), ("faces", "seen", "TEXT NOT NULL DEFAULT ''"),
            ("face_pictures", "frames", "INTEGER NOT NULL DEFAULT 0"))
# Every table, in an order they can be emptied in.
TABLES = ("face_crops", "face_marks", "face_rejects", "face_people", "people",
          "face_failures", "faces", "face_pictures", "meta")


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
    result["full_size"] = raw.get("full_size") is not False
    result["drives"] = raw.get("drives") is True
    for key in ("workers", "threads", "max_edge", "min_size", "max_faces", "min_faces",
                "frames"):
        result[key] = _number(raw.get(key), DEFAULTS[key], *LIMITS[key])
    for key in ("min_score", "full_megapixels", "frame_seconds", "match", "group_quality"):
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


def _model_digests(options):
    digests = []
    for key in ("detector", "recognizer"):
        path = model_path(options, key)
        digest = _file_sha256(path)
        if not digest:
            raise PeopleError("The model file %s cannot be read." % path)
        digests.append(digest)
    return digests


def _sha1(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def recipe(options, digests=None):
    """What the faces of the pictures looked at in their preview (at most max_edge
    pixels) are found with: the SHA-1 of the two model files' SHA-256 and the settings
    that change what is found (max_edge, min_score, min_size, max_faces, FIND_VERSION).
    Raises PeopleError when a model file cannot be read."""
    digests = digests or _model_digests(options)
    return _sha1("%s\n%s\nfind-%d edge-%d/%d score-%g size-%d faces-%d" % (
        digests[0], digests[1], FIND_VERSION, options["max_edge"], MIN_EDGE,
        options["min_score"], options["min_size"], options["max_faces"]))


def recipes(options):
    """The recipe of each kind of picture looked at: {KIND_PHOTO, KIND_POSTER, KIND_360:
    recipe(), RECIPE_FULL: that of the photos looked at in full size (the model files,
    min_score, min_size, max_faces, FIND_VERSION and FULL_VERSION, the tiles and the
    faces' pictures; not max_edge, which they do not use), RECIPE_VIDEO: that of the
    videos looked at in frames (recipe()'s settings, VIDEO_VERSION, frames,
    frame_seconds, how the frames are linked and the faces' pictures; recipe() itself
    with "frames" 0: the poster alone)}. Raises PeopleError when a model file
    cannot be read."""
    import people_groups
    digests = _model_digests(options)
    preview = recipe(options, digests)
    full = _sha1("%s\n%s\nfind-%d full-%d tile-%d/%d edge-%d pixels-%d/%d score-%g size-%d "
                 "faces-%d crop-%d/%g quality-%d" % (
                     digests[0], digests[1], FIND_VERSION, FULL_VERSION, FULL_TILE,
                     FULL_OVERLAP, MIN_EDGE, FULL_MAX_PIXELS, FULL_MAX_EDGE,
                     options["min_score"], options["min_size"], options["max_faces"],
                     people_groups.CROP_EDGE, people_groups.CROP_MARGIN, FULL_QUALITY))
    video = preview
    if options["frames"]:
        video = _sha1("%s\nvideo-%d frames-%d every-%g track-%g/%g/%g/%g/%g/%g/%g/%d quality-%d "
                      "crop-%d/%g band-%g/%g" % (
                          preview, VIDEO_VERSION, options["frames"], options["frame_seconds"],
                          TRACK_MATCH, TRACK_NORM, VIDEO_SAME, VIDEO_NEAR, VIDEO_NEAR_FLOOR,
                          VIDEO_MIN_NORM, VIDEO_LONE_QUALITY, VIDEO_MAX_FACES, VIDEO_QUALITY,
                          people_groups.CROP_EDGE, people_groups.CROP_MARGIN, BAND_360[0],
                          BAND_360[1]))
    return {KIND_PHOTO: preview, KIND_POSTER: preview, KIND_360: preview, RECIPE_FULL: full,
            RECIPE_VIDEO: video}


def find_options(options, full=False, crops=False):
    """What a "faces" request asks of the helper (full: for a picture looked at in full
    size, in tiles, with a picture of each face; crops: with a picture of each face, as
    for a video's frames)."""
    request = {"max_edge": options["max_edge"], "min_edge": MIN_EDGE,
               "min_size": options["min_size"], "min_score": options["min_score"],
               "max_faces": options["max_faces"]}
    if full or crops:
        import people_groups
        request.update(crop_edge=people_groups.CROP_EDGE, crop_margin=people_groups.CROP_MARGIN)
    if full:
        request.update(max_edge=people_worker.LARGEST_EDGE, tile=FULL_TILE,
                       overlap=FULL_OVERLAP)
    return request


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


def full_size(row, options):
    """True when an index row's picture is looked at in full size: "full_size" is on,
    it is an ordinary photo (KIND_PHOTO), larger than max_edge (the size it is
    otherwise looked at, at most) on its long edge, and a wide panorama (WIDE_ASPECT
    times as wide as high, or as high as wide) or of at least "full_megapixels" million
    pixels."""
    if not options["full_size"] or kind_of(row) != KIND_PHOTO:
        return False
    try:
        width, height = int(row["width"] or 0), int(row["height"] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        return False
    long_edge, short_edge = max(width, height), min(width, height)
    if short_edge <= 0 or long_edge <= options["max_edge"]:
        return False
    return (long_edge >= WIDE_ASPECT * short_edge - 1e-9
            or width * height >= options["full_megapixels"] * 1e6)


def is_video(row):
    """True for an index row of a video (a 360-degree one too)."""
    keys = row.keys() if hasattr(row, "keys") else row
    return "media" in keys and row["media"] == "video"


def in_frames(row, options):
    """True when an index row's picture is a video looked at in frames ("frames"
    is not 0)."""
    return bool(options["frames"]) and is_video(row)


def recipe_kind(row, options):
    """The kind of recipe (see recipes()) an index row's picture is looked at with:
    RECIPE_FULL for a photo looked at in full size, RECIPE_VIDEO for a video, else its
    kind (kind_of)."""
    if full_size(row, options):
        return RECIPE_FULL
    return RECIPE_VIDEO if is_video(row) else kind_of(row)


def skipped_ids(conn, config, library_ids, options=None):
    """The ids of the videos that are not looked at: the clips of a dashcam's drives
    (drives.library_drives, whose camera cut the clips at most DASHCAM_SECONDS long:
    other drivers' faces), unless "drives" is true; none while the videos are looked at
    in their posters alone ("frames": 0, as before)."""
    options = options or settings(config)
    if options["drives"] or not options["frames"]:
        return set()
    import drives
    found = set()
    for library_id in library_ids:
        for drive in drives.library_drives(conn, library_id, config):
            length = drives.cut_length(drive.clips)
            if length is not None and length <= DASHCAM_SECONDS:
                found.update(drive.ids)
    return found


_ROW_COLUMNS = "id, library_id, path, content_hash, media, projection, has_preview, width, height"


def library_rows(conn, library_ids, skip=()):
    """The present pictures of some libraries that are looked at (a known content hash;
    videos and 360-degree pictures only with a stored preview; not those of skip, ids:
    see skipped_ids), in id order."""
    library_ids = list(library_ids)
    rows = []
    for start in range(0, len(library_ids), ID_CHUNK):
        part = library_ids[start:start + ID_CHUNK]
        for row in conn.execute(
                "SELECT %s FROM images WHERE missing_since IS NULL AND content_hash != '' "
                "AND library_id IN (%s) ORDER BY id" % (_ROW_COLUMNS, ",".join("?" * len(part))),
                part):
            if looked_at(row) is not None and row["id"] not in skip:
                rows.append(row)
    rows.sort(key=lambda row: row["id"])
    return rows


def in_band_360(face):
    """True when a face (face_row) of a 360-degree picture is one that is kept (BAND_360)."""
    middle = face["y"] + face["h"] / 2.0
    return BAND_360[0] < middle < BAND_360[1]


def read_limited(read, seconds, stop=None):
    """read()'s answer, read in a thread of its own, so that a read that hangs (a share
    that stalls) gives up after seconds (images.SourceReadError), or when stop() says
    so (Stopped); the read itself goes on alone until the system ends it."""
    import images
    result = {}

    def run():
        try:
            result["data"] = read()
        except BaseException as error:              # (given to the caller)
            result["error"] = error
    thread = threading.Thread(target=run, daemon=True, name="people-read")
    thread.start()
    deadline = time.monotonic() + seconds
    while True:
        thread.join(READ_POLL)
        if not thread.is_alive():
            break
        if stop is not None and stop():
            raise Stopped()
        if time.monotonic() >= deadline:
            raise images.SourceReadError("cannot read file: it took longer than %g seconds"
                                         % seconds)
    if "error" in result:
        raise result["error"]
    return result["data"]


def picture_bytes(row, options, previews, helper=None, root=None, full=False, stop=None):
    """The bytes the helper is sent for an index row: its stored preview (JPEG, or PNG),
    else (a photo without one) its original made upright and at most max_edge pixels
    on its long edge; with full (see full_size), its original upright in full size (at
    most FULL_MAX_PIXELS and FULL_MAX_EDGE, and small enough for the helper's pipe).
    The original is read within READ_TIMEOUT, until stop() (see read_limited). Raises
    images.ImageReadError (SourceReadError when the file cannot be read; or OSError)
    when it cannot be read, Stopped."""
    data = None
    if row["has_preview"] == 1 and not full:
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
        raise images.ImageReadError("%s and the library's folder is not known" % (
            "the picture is looked at in full size" if full else "there is no preview"))
    path = os.path.join(root, *row["path"].split("/"))
    source = read_limited(lambda: images.read_source_bytes(path, helper), READ_TIMEOUT, stop)
    projection = row["projection"] or ""
    if not full:
        edge = options["max_edge"]
        return images.prepare_for_llm(source, max_edge=edge, min_short=0,
                                      max_pixels=edge * edge, projection=projection)
    try:
        pixels = min(FULL_MAX_PIXELS, int(row["width"]) * int(row["height"])) or FULL_MAX_PIXELS
    except (IndexError, KeyError, TypeError, ValueError):
        pixels = FULL_MAX_PIXELS
    while True:
        data = images.prepare_for_llm(source, max_edge=FULL_MAX_EDGE, min_short=0,
                                      max_pixels=pixels, quality=FULL_QUALITY,
                                      projection=projection)
        # (Its base64 must fit in one line of the helper's input, with room to spare.)
        room = people_worker.MAX_LINE * 0.7 * 3 / 4
        if len(data) <= room or pixels < 1000000:
            return data
        pixels = int(pixels * 0.8 * room / len(data))


def frame_times(length, poster_at, options):
    """The moments (seconds) of a video of length seconds looked at besides its poster
    frame (at poster_at): about one every "frame_seconds", at most "frames" - 1,
    spread evenly over it as the contact sheet's are ((k + 0.5) / n of its length),
    leaving out one that would be the poster frame again."""
    try:
        length = float(length or 0)
    except (TypeError, ValueError):
        length = 0.0
    if not math.isfinite(length) or length <= 0:
        return []
    count = min(options["frames"] - 1,
                max(1, int(math.ceil(length / options["frame_seconds"] - 1e-9))))
    times = [(index + 0.5) / count * length for index in range(max(0, count))]
    return [round(at, 3) for at in times if abs(at - poster_at) > options["frame_seconds"] / 4.0]


def video_frames(row, options, root, tools, level=None, stop=None):
    """(poster_at, [(seconds, JPEG bytes)]) of a video's frames to look at (in_frames):
    its poster frame (the frame its preview was made of: videos.poster_moment) and
    those of frame_times(), at most max_edge pixels, in time order, read from its file
    with ffmpeg (tools; a 360 video's stitched as its poster was, level: its level
    track kept in the index), all within VIDEO_READ_TIMEOUT, until stop() says so
    (asked before each frame). Raises images.ImageReadError (SourceReadError when the
    file cannot be read, or took too long), videos.VideoReadError, or Stopped."""
    import images
    import videos
    if not root:
        raise images.ImageReadError("the video's library folder is not known")
    path = os.path.join(root, *row["path"].split("/"))
    edge = options["max_edge"]
    deadline = time.monotonic() + VIDEO_READ_TIMEOUT
    info, source = videos.frame_source(tools, path, level)

    def grab(seconds, size):
        if stop is not None and stop():
            raise Stopped()
        if time.monotonic() >= deadline:
            raise videos.VideoTimeout("reading the video's frames took longer than %g seconds"
                                      % VIDEO_READ_TIMEOUT)
        return source(seconds, size)
    try:
        poster_at, poster = videos.poster_moment(tools, path, info, edge, grab)
        frames = [(round(poster_at, 3), poster)]
        for seconds in frame_times(info.get("span") or info.get("duration"), poster_at,
                                   options):
            frame = grab(seconds, edge)
            if frame is not None:
                frames.append((seconds, frame))
    except videos.VideoReadError as error:
        # (A file that cannot be read now, or took too long: SourceReadError.)
        raise videos.read_failure(error, path)
    frames.sort(key=lambda item: item[0])
    encoded = []
    for seconds, frame in frames:
        out = io.BytesIO()
        frame.save(out, "JPEG", quality=VIDEO_QUALITY)
        encoded.append((seconds, out.getvalue()))
    return round(poster_at, 3), encoded


def _sightings(at, seen, box, poster_at=None):
    """[(seconds or None, (x, y, w, h))]: where a face was seen. A face of a video kept
    before its frames were looked at (no "at": found in its poster) was seen in the
    poster frame (poster_at); a picture's faces at no time (None)."""
    if seen:
        try:
            found = [(float(item[0]), tuple(float(value) for value in item[1:5]))
                     for item in json.loads(seen)]
            if found:
                return found
        except (TypeError, ValueError, IndexError):
            pass
    return [(at if at is not None else poster_at, tuple(box))]


def _same_time(first, second):
    if first is None or second is None:
        return first is None and second is None
    return abs(first - second) <= SAME_TIME


def link_faces(frames_of, vectors, norms):
    """The faces of a video's frames grouped by the person they show: lists of their
    indexes. frames_of[i] is the frame face i is in, vectors[i] its vector (unit
    length), norms[i] its vector's length before that (people.py's norm). The faces
    whose norm is at least TRACK_NORM are grouped by average linkage (TRACK_MATCH,
    never two faces of one frame); each other face (things, faces turned away or
    hidden, whose vectors say little) then joins the group whose faces it is most like
    on average (at least TRACK_MATCH, one face of a frame), the most alike first, or
    stays alone."""
    import people_groups
    core = [index for index, norm in enumerate(norms) if norm >= TRACK_NORM]
    rest = [index for index, norm in enumerate(norms) if norm < TRACK_NORM]
    core_vectors = [vectors[index] for index in core]
    pairs = people_groups.similar_pairs(core_vectors, TRACK_MATCH - people_groups.PAIR_MARGIN)

    def allowed(first, second):
        seen = {frames_of[core[item]] for item in first}
        return not any(frames_of[core[item]] in seen for item in second)
    groups = [[core[item] for item in group] for group in people_groups.average_linkage(
        len(core), pairs, TRACK_MATCH, allowed=allowed)]
    means = []
    for group in groups:
        total = [0.0] * len(vectors[group[0]])
        for index in group:
            for place, value in enumerate(vectors[index]):
                total[place] += value
        length = math.sqrt(sum(value * value for value in total)) or 1.0
        means.append([value / length for value in total])
    offers = []
    for index in rest:
        for number, mean in enumerate(means):
            value = sum(a * b for a, b in zip(vectors[index], mean))
            if value >= TRACK_MATCH:
                offers.append((-value, index, number))
    offers.sort()
    placed = set()
    for _value, index, number in offers:
        if index in placed or any(frames_of[other] == frames_of[index]
                                  for other in groups[number]):
            continue
        groups[number].append(index)
        placed.add(index)
    groups += [[index] for index in rest if index not in placed]
    return [sorted(group) for group in groups]


def _dot(first, second):
    return sum(a * b for a, b in zip(first, second))


def _near(first, second):
    """True when two faces (face_row dicts) of a video's frames are in about the same
    place (VIDEO_NEAR)."""
    return (abs(first["x"] + first["w"] / 2.0 - second["x"] - second["w"] / 2.0)
            <= VIDEO_NEAR * max(first["w"], second["w"])
            and abs(first["y"] + first["h"] / 2.0 - second["y"] - second["h"] / 2.0)
            <= VIDEO_NEAR * max(first["h"], second["h"]))


def _people_seen(found, vectors):
    """The faces of a video's frames (face_row dicts with "frame", and their vectors)
    grouped by the person they show: link_faces, then each group split so that its
    faces are all at least VIDEO_SAME alike to its best one (by quality, then score),
    or in the place of one that stays and alike to it (see VIDEO_NEAR); the faces split
    off are linked (and split) again among themselves. Lists of indexes."""
    def best_of(group):
        return max(group, key=lambda item: (found[item]["quality"], found[item]["score"],
                                            -item))
    waiting = link_faces([row["frame"] for row in found], vectors,
                         [row["norm"] for row in found])
    groups = []
    while waiting:
        group = waiting.pop(0)
        best = best_of(group)
        kept = [item for item in group
                if item == best or _dot(vectors[item], vectors[best]) >= VIDEO_SAME]
        more = True
        while more:
            more = False
            for item in group:
                if item in kept or _dot(vectors[item], vectors[best]) < VIDEO_NEAR_FLOOR:
                    continue
                if any(_near(found[item], found[other])
                       and _dot(vectors[item], vectors[other]) >= TRACK_MATCH
                       for other in kept):
                    kept.append(item)
                    more = True
        kept.sort()
        groups.append(kept)
        rest = [item for item in group if item not in kept]
        if rest:
            waiting.extend([[rest[index] for index in again] for again in link_faces(
                [found[item]["frame"] for item in rest], [vectors[item] for item in rest],
                [found[item]["norm"] for item in rest])])
    return groups


def video_faces(frames, kind, options):
    """The faces of a video kept from its frames ([(seconds, the helper's answer)], in
    time order): face_row dicts, one per person seen (the faces of the frames linked by
    link_faces, each alike to the best of them: _people_seen), the best of them (by
    quality, then score) with "crop" its picture, "at" its frame's second, "seen" every
    moment and box of that person (JSON), and the vector of them all (each weighted by
    0.1 + its quality, unit length); the best first, at most max_faces (and
    VIDEO_MAX_FACES); not those whose vectors are all shorter than VIDEO_MIN_NORM, nor
    one seen in a single frame of a quality below VIDEO_LONE_QUALITY. Of a 360 video
    (kind KIND_360) only the faces near the horizon count. Returns (faces, width,
    height) (the size of the frames)."""
    found = []
    width = height = 0
    for index, (seconds, reply) in enumerate(frames):
        frame_width, frame_height = int(reply.get("width") or 0), int(reply.get("height") or 0)
        if frame_width and frame_height and not width:
            width, height = frame_width, frame_height
        for face in reply.get("faces") or []:
            row = face_row(face, frame_width, frame_height) if isinstance(face, dict) else None
            if row is None or (kind == KIND_360 and not in_band_360(row)):
                continue
            row.update(crop=_crop_of(face), at=seconds, frame=index)
            found.append(row)
    if not found:
        return [], width, height
    vectors = [struct.unpack("<%df" % EMBEDDING_DIMS, row["embedding"]) for row in found]
    faces = []
    for group in _people_seen(found, vectors):
        if max(found[item]["norm"] for item in group) < VIDEO_MIN_NORM:
            continue
        if len(group) == 1 and found[group[0]]["quality"] < VIDEO_LONE_QUALITY:
            continue
        members = sorted(group, key=lambda item: found[item]["at"])
        best = max(members, key=lambda item: (found[item]["quality"], found[item]["score"],
                                              -item))
        total = [0.0] * EMBEDDING_DIMS
        for item in members:
            weight = 0.1 + found[item]["quality"]
            for place, value in enumerate(vectors[item]):
                total[place] += weight * value
        length = math.sqrt(sum(value * value for value in total)) or 1.0
        face = dict(found[best])
        face.pop("frame", None)
        face["embedding"] = struct.pack("<%df" % EMBEDDING_DIMS,
                                        *[value / length for value in total])
        face["seen"] = json.dumps([[found[item]["at"]] + [round(found[item][name], 5)
                                                          for name in ("x", "y", "w", "h")]
                                   for item in members], separators=(",", ":"))
        faces.append(face)
    faces.sort(key=lambda face: (-face["quality"], -face["score"], face["at"]))
    return faces[:min(options["max_faces"], VIDEO_MAX_FACES)], width, height


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
        "at": None, "seen": "",
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

    A file of an older version that only lacks tables and columns (versions
    1 to 3: version 2 added the people, version 3 the faces' pictures,
    version 4 the moments of the faces of videos) is brought up to date
    (_upgrade), its faces, people, names and all that was said about them
    kept, also without create (the tables and columns are only added). Only
    with create (the runs that write the store) is a new file set up, or
    one of a layout that cannot be brought up to date emptied first;
    StoreError for one of a newer version (or one whose version cannot be
    told), which is left as it is. Without create, such a file gives None,
    and nothing is written. What a connection deletes is overwritten
    (secure_delete): faces, their pictures and names leave no trace.
    """
    path = path or store_path()
    if not create and not os.path.isfile(path):
        return None
    conn = db.connect(path, check_same_thread=False)
    try:
        # (What is deleted is overwritten, not only let go: the faces' vectors and
        # pictures of a picture, a person or a library that went.)
        conn.execute("PRAGMA secure_delete=ON")
        version = _stored_version(conn)
        if version == FACES_VERSION:
            return conn
        upgrade = version is not None and 1 <= version < FACES_VERSION
        if not create and not upgrade:
            conn.close()
            return None
        if version is None or version > FACES_VERSION:
            raise StoreError("%s was written by a newer version of Search Images (or is "
                             "damaged): it is left as it is. Use that version, or delete "
                             "the face data (python people.py --delete-all)." % path)
        if upgrade:
            _upgrade(conn)
            return conn
        with conn:
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


def _upgrade(conn):
    """Bring a file of an older version (1 to 3) up to date: add the tables and columns
    it lacks (nothing else changes: the faces, the people and their names stay), in one
    transaction that waits for any other program doing the same (it then finds the
    file up to date, and changes nothing)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _stored_version(conn) != FACES_VERSION:
            for statement in _SCHEMA:
                conn.execute(statement)
            for table, column, kind in _COLUMNS:
                present = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
                if column not in present:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, kind))
            conn.execute("UPDATE meta SET value = ? WHERE key = 'version'",
                         (str(FACES_VERSION),))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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
                "tilt", "quality", "embedding", "at", "seen")


def _unit(blob):
    try:
        return struct.unpack("<%df" % EMBEDDING_DIMS, bytes(blob))
    except (TypeError, struct.error):
        return None


def replace_faces(store, key, faces, now, poster_at=None, keep_owned=None):
    """Put a picture's faces (face_row dicts) in place of the ones it had: a face found
    again where an earlier one was (SAME_FACE_IOU) keeps that face's id. Of a video
    (poster_at given: where its poster frame is), where means at a moment both were
    seen at (see _sightings), and the earlier face must be the person the new one is:
    the new face's own moment and box (its best face, "at"), or a vector at least
    VIDEO_SAME alike to the new face's. Earlier faces not found again go -- but those
    the owner put in a person stay with keep_owned (by default for a video). No commit.
    Returns the ids, in the order of faces."""
    video = poster_at is not None
    keep_owned = video if keep_owned is None else keep_owned
    free = {}
    vectors = {}
    for row in store.execute("SELECT id, x, y, w, h, at, seen, embedding FROM faces WHERE "
                             "content_hash = ? AND kind = ?", key):
        free[row[0]] = _sightings(row[5], row[6], (row[1], row[2], row[3], row[4]), poster_at)
        vectors[row[0]] = _unit(row[7])
    ids = []
    for face in faces:
        box = (face["x"], face["y"], face["w"], face["h"])
        mine = _sightings(face.get("at"), face.get("seen"), box)
        vector = _unit(face["embedding"]) if video else None
        best, best_iou = None, SAME_FACE_IOU
        for face_id, earlier in free.items():
            value = max([_iou(here, there) for at, here in mine for when, there in earlier
                         if _same_time(at, when)] or [0.0])
            if value < best_iou:
                continue
            if video:
                # (Found where that face was by another of its moments: the same person
                # only when their vectors say so.)
                own = max([_iou(box, there) for when, there in earlier
                           if _same_time(face.get("at"), when)] or [0.0])
                if own < SAME_FACE_IOU and (vector is None or vectors[face_id] is None
                                            or _dot(vector, vectors[face_id]) < VIDEO_SAME):
                    continue
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
    owned = set()
    if keep_owned and free:
        owned = {row[0] for row in store.execute(
            "SELECT face_id FROM face_people WHERE owner = 1 AND face_id IN (%s)"
            % ",".join("?" * len(free)), list(free))}
    for face_id in free:
        if face_id not in owned:
            store.execute("DELETE FROM faces WHERE id = ?", (face_id,))
    return ids


def record_picture(store, key, recipe_value, reply, now=None, keep_owned=False):
    """Store what the helper found in a picture (its answer: width, height, faces,
    seconds; keep_owned: the faces the owner put in a person stay when they are not
    found again, as for a picture looked at again in full size). No commit. Returns
    how many faces were kept."""
    now = time.time() if now is None else now
    width, height = int(reply.get("width") or 0), int(reply.get("height") or 0)
    faces = []
    for face in reply.get("faces") or []:
        row = face_row(face, width, height) if isinstance(face, dict) else None
        if row is not None and (key[1] != KIND_360 or in_band_360(row)):
            row["crop"] = _crop_of(face)
            faces.append(row)
    return _store_faces(store, key, recipe_value, faces, width, height,
                        _seconds_of(reply), 0, now, keep_owned=keep_owned)


def record_video(store, key, recipe_value, frames, options, poster_at=None, now=None):
    """Store what the helper found in a video's frames ([(seconds, its answer)]; see
    video_faces; poster_at: where its poster frame is, whose faces keep the ids of
    those found before in its poster). No commit. Returns how many faces (people seen)
    were kept."""
    now = time.time() if now is None else now
    faces, width, height = video_faces(frames, key[1], options)
    return _store_faces(store, key, recipe_value, faces, width, height,
                        sum(_seconds_of(reply) for _at, reply in frames), len(frames), now,
                        poster_at)


def _seconds_of(reply):
    seconds = reply.get("seconds")
    return float(seconds) if isinstance(seconds, (int, float)) else 0.0


def _store_faces(store, key, recipe_value, faces, width, height, seconds, frames, now,
                 poster_at=None, keep_owned=None):
    ids = replace_faces(store, key, faces, now, poster_at, keep_owned)
    # The faces' pictures (of a picture looked at in full size, or a video's; none
    # else): those of faces found again without one go too.
    for face_id, face in zip(ids, faces):
        crop = face.get("crop")
        if crop:
            store.execute("INSERT OR REPLACE INTO face_crops (face_id, jpeg, updated) VALUES "
                          "(?, ?, ?)", (face_id, sqlite3.Binary(crop), now))
        else:
            store.execute("DELETE FROM face_crops WHERE face_id = ?", (face_id,))
    store.execute("INSERT OR REPLACE INTO face_pictures (content_hash, kind, recipe, width, "
                  "height, faces, seconds, frames, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  list(key) + [recipe_value, width, height, len(faces), round(seconds, 4),
                               frames, now])
    store.execute("DELETE FROM face_failures WHERE content_hash = ? AND kind = ?", key)
    _bump(store)
    return len(faces)


_JPEG_START = b"\xff\xd8\xff"
MAX_CROP_BYTES = 256 * 1024


def _crop_of(face):
    """The JPEG bytes of a face's picture in the helper's answer ("crop"), or None."""
    value = face.get("crop")
    if not isinstance(value, str) or not value:
        return None
    try:
        data = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        return None
    if not data.startswith(_JPEG_START) or len(data) > MAX_CROP_BYTES:
        return None
    return data


def stored_crop(store, face_id):
    """(JPEG bytes, when it was made) of the picture kept of a face (one found in a
    picture looked at in full size), or None."""
    row = store.execute("SELECT jpeg, updated FROM face_crops WHERE face_id = ?",
                        (face_id,)).fetchone()
    return (bytes(row[0]), row[1]) if row is not None else None


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


def forget_skipped(store, conn, skip):
    """Drop the faces found before in the videos that are not looked at (skip: their
    ids, see skipped_ids), unless another row that is looked at shares their content:
    the clips of a dashcam's drives, with other drivers' faces. The faces in a person
    (the owner's or the grouping's: someone seen elsewhere too) stay. Returns how many
    faces went."""
    if not skip:
        return 0
    skipped, kept = set(), set()
    for row in conn.execute("SELECT id, content_hash, media, projection, has_preview FROM "
                            "images WHERE content_hash != ''"):
        kind = looked_at(row)
        if kind is not None:
            (skipped if row["id"] in skip else kept).add((row["content_hash"], kind))
    gone = sorted(skipped - kept)
    if not gone:
        return 0
    removed = changed = 0
    with store:
        for key in gone:
            removed += store.execute(
                "DELETE FROM faces WHERE content_hash = ? AND kind = ? AND id NOT IN (SELECT "
                "face_id FROM face_people)", key).rowcount
            for table in ("face_pictures", "face_failures"):
                changed += store.execute("DELETE FROM %s WHERE content_hash = ? AND kind = ?"
                                         % table, key).rowcount
        if removed or changed:
            if drop_faceless_people(store):
                store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                              "('people_version', COALESCE((SELECT CAST(value AS INTEGER) "
                              "FROM meta WHERE key = 'people_version'), 0) + 1)")
            _bump(store)
    return removed


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

def counts(conn, config, library_ids, store=None, totals=None):
    """{library id: {"pictures": pictures looked at by People, "done": looked at with
    the current settings, "faces": faces found in those, "with_faces": pictures of
    them with a face, "pending": to look at, "failed": waiting for a retry,
    "changed": looked at with other settings (their faces stay until they are looked
    at again), "full": of the pictures done, those looked at in full size (see
    full_size), "full_pending": of those pending, those to look at in full size,
    "videos" and "video_pending": the same for the videos looked at in frames (see
    in_frames), "skipped": videos not looked at (the clips of drives: skipped_ids)}}.
    When the recipes cannot be told now, pictures looked at count as done. totals: a
    dict that gets "pending", "full_pending" and "video_pending" of the enabled
    libraries as a run looks at them (update: a picture in several libraries -- the
    same content -- once)."""
    library_ids = list(library_ids)
    if totals is not None:
        totals.update(pending=0, full_pending=0, video_pending=0)
        enabled = {library.get("id") for library in common.enabled_libraries(config)}
        counted = set()
    result = {library_id: {"pictures": 0, "done": 0, "faces": 0, "with_faces": 0,
                           "pending": 0, "failed": 0, "changed": 0, "full": 0,
                           "full_pending": 0, "videos": 0, "video_pending": 0, "skipped": 0}
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
    wanted = None
    if options["enabled"] and not setup_problem(options):
        try:
            wanted = recipes(options)
        except PeopleError:
            wanted = None
    skip = skipped_ids(conn, config, library_ids, options)
    for row in library_rows(conn, library_ids):
        entry = result[row["library_id"]]
        if row["id"] in skip:
            entry["skipped"] += 1
            continue
        key = (row["content_hash"], kind_of(row))
        full = full_size(row, options)
        frames = in_frames(row, options)
        current = wanted[recipe_kind(row, options)] if wanted is not None else None
        entry["pictures"] += 1
        made_with = done.get(key)
        if made_with is not None and (current is None or made_with == current):
            entry["done"] += 1
            entry["faces"] += found.get(key, 0)
            entry["with_faces"] += 1 if found.get(key) else 0
            entry["full"] += 1 if full and current is not None else 0
            entry["videos"] += 1 if frames and current is not None else 0
            continue
        if made_with is not None:
            entry["changed"] += 1
        failure = failed.get(key)
        if failure is not None and failure[0] == current and failure[1] >= MAX_ATTEMPTS:
            entry["failed"] += 1
        else:
            entry["pending"] += 1
            entry["full_pending"] += 1 if full else 0
            entry["video_pending"] += 1 if frames else 0
            if totals is not None and row["library_id"] in enabled and key not in counted:
                counted.add(key)
                totals["pending"] += 1
                totals["full_pending"] += 1 if full else 0
                totals["video_pending"] += 1 if frames else 0
    return result


def _among(full, videos):
    """" (3 in full size, 2 videos in frames)", or ''."""
    parts = (["%d in full size" % full] if full else []) + (
        ["%d video(s) in frames" % videos] if videos else [])
    return " (%s)" % ", ".join(parts) if parts else ""


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

    def faces(self, data, options, full=False, crops=False):
        """The helper's answer for one picture (bytes; full: one looked at in full size;
        crops: with a picture of each face, as for a video's frame)."""
        request = dict(find_options(options, full, crops), op="faces",
                       image=base64.b64encode(data).decode("ascii"))
        return self.request(request, FULL_TIMEOUT if full else PICTURE_TIMEOUT)

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
            "gave_up": 0, "up_to_date": 0, "pending": 0, "full": 0, "videos": 0, "frames": 0,
            "removed": 0, "skipped": 0, "dropped": 0, "offline": 0, "no_tools": 0,
            "restarts": 0, "workers": 0, "seconds": 0.0, "stopped": False, "error": "",
            "people": None}


def _offline_libraries(rows, libraries, ctx, helper, needs_original=None, by_path=None):
    """{library id} of the libraries whose folder cannot be reached now, among those with
    rows whose original is read (needs_original(row); by default those without a stored
    preview; unless helper reads them, see config.json's "helper") or whose file is read
    from its folder whatever the helper (by_path(row): videos): their pictures wait for a
    later run."""
    loads = helper is not None and getattr(helper, "load_image", None) is not None
    if needs_original is None:
        def needs_original(row):
            return row["has_preview"] != 1
    needed = {row["library_id"] for row in rows
              if (not loads and needs_original(row)) or (by_path is not None and by_path(row))}
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
                ctx.log("People: the pictures of %s read from their files (without a preview, "
                        "looked at in full size, or videos) wait for a later run: the folder "
                        "%s cannot be reached (offline?)."
                        % (common.library_label(library), root or "(none)"), "warn")
    return offline


def pending_rows(conn, store, library_ids, recipe_values, retry_failed=False, options=None,
                 skip=()):
    """(the rows to look at, one per content and kind -- a copy with a stored preview
    rather than one without --, newest first; how many are up to date; how many wait
    for a retry). recipe_values is recipes() (or one recipe, of every picture); options
    the settings, which tell the pictures looked at in full size and the videos looked
    at in frames (none without); skip the ids of rows not looked at (skipped_ids)."""
    if not isinstance(recipe_values, dict):
        recipe_values = dict.fromkeys((KIND_PHOTO, KIND_POSTER, KIND_360, RECIPE_FULL,
                                       RECIPE_VIDEO), recipe_values)
    done, failed = picture_states(store)
    chosen = {}
    up_to_date = waiting = 0
    for row in library_rows(conn, library_ids, skip):
        key = (row["content_hash"], kind_of(row))
        recipe_value = recipe_values[recipe_kind(row, options) if options else key[1]]
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
    a stored preview, or looked at in full size (see full_size: it is read
    from its original file; at most FULL_AT_ONCE such pictures at a time),
    or a video looked at in frames (in_frames: read with ffmpeg, at most
    VIDEO_AT_ONCE at a time; without ffmpeg the videos wait), whose library
    folder cannot be reached waits for a later run (offline; the faces it
    had stay), also when the folder goes away while the run reads it (a
    file that cannot be read, or not in time: READ_TIMEOUT,
    VIDEO_READ_TIMEOUT). The clips of a dashcam's drives are skipped
    (skipped_ids; the faces found in them before go: forget_skipped).
    Results are committed as they come (a stopped run keeps what it did;
    Stop is heard while an original or a video's frames are read). helper
    is the helper module (load_image) of config.json, for originals.

    Then, unless the run stopped, the faces are grouped into people again
    when they changed (people_groups.group_if_due).

    Summary keys: configured, looked, faces, with_faces, failed, gave_up
    (pictures that now wait for a retry), up_to_date, pending, full (of
    the pictures looked at, those in full size), videos and frames (of
    them, the videos looked at in frames, and their frames), removed,
    skipped (clips of drives not looked at), dropped (faces of those that
    went), offline, no_tools (videos that wait for ffmpeg), restarts,
    workers, seconds, stopped, error, people (the grouping's summary, or
    None when it did not run).
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
        recipe_values = recipes(options)
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
        library_ids = [library["id"] for library in libraries]
        skip = skipped_ids(conn, config, library_ids, options)
        summary["skipped"] = len(skip)
        summary["dropped"] = forget_skipped(store, conn, skip)
        if summary["dropped"]:
            ctx.log("People: %d face(s) found before in clips of dashcam drives (other "
                    "drivers) went; those clips are not looked at (\"drives\" in config.json's "
                    "\"people\")." % summary["dropped"])
        todo, summary["up_to_date"], waiting = pending_rows(
            conn, store, library_ids, recipe_values, retry_failed, options, skip)
        if waiting:
            ctx.log("People: %d picture(s) failed %d times and wait (Retry failed, or people.py "
                    "--retry-failed)." % (waiting, MAX_ATTEMPTS))
        tools = None
        if any(in_frames(row, options) for row in todo):
            import videos
            tools = videos.find_tools(config)
            if tools is None:
                summary["no_tools"] = sum(1 for row in todo if in_frames(row, options))
                ctx.log("People: %d video(s) wait: %s" % (summary["no_tools"],
                                                         videos.TOOLS_MISSING), "warn")
                todo = [row for row in todo if not in_frames(row, options)]

        def needs_original(row):
            return row["has_preview"] != 1 or full_size(row, options)

        def by_path(row):
            return in_frames(row, options)

        def offline_row(row):
            return row["library_id"] in offline and (needs_original(row) or by_path(row))
        offline = (_offline_libraries(todo, libraries, ctx, helper, needs_original, by_path)
                   if todo else set())
        if offline:
            summary["offline"] = sum(1 for row in todo if offline_row(row))
            todo = [row for row in todo if not offline_row(row)]
        if limit is not None and len(todo) > max(0, limit):
            todo = todo[:max(0, limit)]
        summary["pending"] = len(todo)
        if todo:
            roots = {library["id"]: common.library_root(library) for library in libraries}
            levels = _level_tracks(conn, [row["id"] for row in todo if in_frames(row, options)
                                          and kind_of(row) == KIND_360])
            _run(store, options, recipe_values, todo, ctx, summary, roots, helper, tools,
                 levels)
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


def _level_tracks(conn, image_ids):
    """{image id: the level track kept in the index (videos.stored_level), or None} of
    some 360 videos: their frames are turned level as their posters were."""
    if not image_ids:
        return {}
    import videos
    found = {}
    for start in range(0, len(image_ids), ID_CHUNK):
        part = list(image_ids[start:start + ID_CHUNK])
        for row in conn.execute("SELECT id, exif FROM images WHERE id IN (%s)"
                                % ",".join("?" * len(part)), part):
            found[row[0]] = videos.stored_level(row[1])
    return found


def _run(store, options, recipe_values, todo, ctx, summary, roots, loader, tools=None,
         levels=None):
    """Look at todo (index rows) with the helpers (see update); tools: ffmpeg, for the
    videos looked at in frames (levels: their level tracks, see _level_tracks)."""
    import cache
    import images
    import videos
    levels = levels or {}
    started = time.monotonic()
    ctx.stage("faces", total=len(todo))
    count = max(1, min(options["workers"], len(todo)))
    ctx.log("Finding faces in %d picture(s) with %d helper(s)..." % (len(todo), count))
    in_full = sum(1 for row in todo if full_size(row, options))
    if in_full:
        ctx.log("People: %d of them (wide panoramas and very large pictures) are looked at in "
                "full size, read from their files (at most %d at a time)." % (in_full,
                                                                             FULL_AT_ONCE))
    in_video = sum(1 for row in todo if in_frames(row, options))
    if in_video:
        ctx.log("People: %d of them are videos, looked at in up to %d frames each (about one "
                "every %g s), read from their files with ffmpeg (at most %d at a time)." % (
                    in_video, options["frames"], options["frame_seconds"],
                    VIDEO_AT_ONCE))
    full_slots = threading.BoundedSemaphore(FULL_AT_ONCE)
    video_slots = threading.BoundedSemaphore(VIDEO_AT_ONCE)
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

    # The libraries whose folder went away during the run: their pictures read from
    # their files wait for a later run (offline), as those of a folder that could not
    # be reached at its start do.
    gone = set()
    gone_lock = threading.Lock()

    def stop():
        return bool(fatal) or ctx.should_stop()

    def unreadable(row, error):
        """(row, OFFLINE when its library's folder cannot be reached now, else a
        PictureError) for a picture whose file could not be read (SourceReadError)."""
        library_id = row["library_id"]
        with gone_lock:
            known = library_id in gone
        if not known:
            import library_ops
            root = roots.get(library_id)
            if not (root and library_ops.folder_status(
                    root, timeout=FOLDER_CHECK_TIMEOUT)["reachable"]):
                with gone_lock:
                    first = library_id not in gone
                    gone.add(library_id)
                if first:
                    ctx.log("People: the folder %s cannot be reached any more (offline?): its "
                            "pictures read from their files wait for a later run."
                            % (root or "(none)"), "warn")
                known = True
        if known:
            return row, OFFLINE
        return row, PictureError(common.redact(str(error) or type(error).__name__))

    def look(row):
        """(row, the helper's answer, an Exception, OFFLINE, or None when stopped) for
        one picture."""
        if in_frames(row, options):
            return look_at_video(row)
        if not full_size(row, options):
            return look_at(row, False)
        # (A picture in full size takes much memory: only a few at a time.)
        with full_slots:
            return look_at(row, True)

    def look_at(row, full):
        if stop():
            return row, None
        if (full or row["has_preview"] != 1) and row["library_id"] in gone:
            return row, OFFLINE
        try:
            data = picture_bytes(row, options, previews, loader, roots.get(row["library_id"]),
                                 full, stop)
        except Stopped:
            return row, None
        except images.SourceReadError as error:
            return unreadable(row, error)
        except (images.ImageReadError, OSError, MemoryError) as error:
            return row, PictureError(common.redact(str(error) or type(error).__name__))
        return with_helper(row, lambda helper: helper.faces(data, options, full))

    def look_at_video(row):
        if stop():
            return row, None
        if row["library_id"] in gone:
            return row, OFFLINE
        try:
            # (Only a few videos are read at a time: ffmpeg takes several threads.)
            with video_slots:
                poster_at, frames = video_frames(row, options, roots.get(row["library_id"]),
                                                 tools, levels.get(row["id"]), stop)
        except Stopped:
            return row, None
        except images.SourceReadError as error:
            return unreadable(row, error)
        except (images.ImageReadError, videos.VideoReadError, OSError, MemoryError) as error:
            return row, PictureError(common.redact(str(error) or type(error).__name__))

        def ask(helper):
            answers = []
            for seconds, data in frames:
                if fatal or ctx.should_stop():
                    return None             # (stopped: the video waits for the next run)
                answers.append((seconds, helper.faces(data, options, crops=True)))
            return {"frames": answers, "poster_at": poster_at}
        return with_helper(row, ask)

    def with_helper(row, ask):
        """(row, ask(a helper)'s answer, or an Exception) with an idle helper."""
        helper = idle.get()
        try:
            if helper is None:
                return row, None
            return row, ask(helper)
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
                if outcome is OFFLINE:
                    summary["offline"] += 1
                    ctx.step(1)
                    continue
                key = (row["content_hash"], kind_of(row))
                full = full_size(row, options)
                recipe_value = recipe_values[recipe_kind(row, options)]
                if isinstance(outcome, Exception):
                    summary["failed"] += 1
                    failures.append((row["id"], str(outcome)))
                    if record_failure(store, key, recipe_value, outcome) >= MAX_ATTEMPTS:
                        summary["gave_up"] += 1
                else:
                    if "frames" in outcome:
                        kept = record_video(store, key, recipe_value, outcome["frames"],
                                            options, outcome["poster_at"])
                        summary["videos"] += 1
                        summary["frames"] += len(outcome["frames"])
                    else:
                        # (One looked at again in full size keeps the faces the
                        # owner placed, as a video does.)
                        kept = record_picture(store, key, recipe_value, outcome,
                                              keep_owned=full)
                    summary["looked"] += 1
                    summary["full"] += 1 if full else 0
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
        parts = []
        if summary["full"]:
            parts.append("%d in full size" % summary["full"])
        if summary["videos"]:
            parts.append("%d video(s) in %d frame(s)" % (summary["videos"], summary["frames"]))
        ctx.log("Looked for faces in %d picture(s)%s in %.1f s (%.2f s a picture): %d face(s) "
                "in %d of them%s." % (summary["looked"], " (%s)" % ", ".join(parts)
                                      if parts else "", summary["seconds"], each,
                                      summary["faces"], summary["with_faces"],
                                      "; %d failed" % summary["failed"]
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
        totals = {}
        counted = counts(conn, config, ids, totals=totals)
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
        if entry and (entry["pictures"] or entry["skipped"]):
            print("  %s: %d of %d picture(s) looked at%s, %d face(s) in %d; %d to look at%s%s%s%s"
                  % (common.library_label(library), entry["done"], entry["pictures"],
                     _among(entry["full"], entry["videos"]), entry["faces"],
                     entry["with_faces"], entry["pending"],
                     _among(entry["full_pending"], entry["video_pending"]),
                     ", %d failed" % entry["failed"] if entry["failed"] else "",
                     ", %d looked at with other settings" % entry["changed"]
                     if entry["changed"] else "",
                     ", %d clip(s) of dashcam drives skipped" % entry["skipped"]
                     if entry["skipped"] else ""), file=out)
    if totals["pending"]:
        # (What the next run looks at: a picture in several libraries once.)
        print("  In all: %d picture(s) to look at%s; a picture in more than one library is "
              "looked at once." % (totals["pending"], _among(totals["full_pending"],
                                                             totals["video_pending"])),
              file=out)
    return 0


def _group_again(config, ctx):
    """people.py --group: the group_faces job, with its runner (jobs.RUNNERS). Returns the
    exit code."""
    import jobs
    conn = db.connect()
    try:
        with ctx.stop_on_ctrl_c():
            jobs.RUNNERS["group_faces"](ctx, conn, config, {"library_id": None})
    except jobs.JobError as error:
        ctx.log(str(error), "error")
        return 1
    finally:
        conn.close()
    ctx.log("Stopped as asked; run people.py --group again." if ctx.should_stop()
            else "Done.")
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
    parser.add_argument("--group", action="store_true",
                        help="Group the faces found into people again, as Group faces again "
                             "on the People page does (every library's faces together).")
    jobs.add_settings_base_argument(parser)
    args = parser.parse_args(argv)
    jobs.use_settings_base(parser, args)
    if args.limit is not None and args.limit < 0:
        print("--limit must be 0 or more.", file=out)
        return 2
    if sum(bool(value) for value in (args.check, args.status, args.delete_all,
                                      args.group)) > 1:
        print("--check, --status, --delete-all and --group go one at a time.", file=out)
        return 2
    if args.group and (args.library or args.limit is not None or args.retry_failed):
        print("--group groups the faces of every library: --library, --limit and "
              "--retry-failed do not go with it.", file=out)
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
    if not args.status:
        jobs.log_tool_command(jobs.ConsoleContext("people", stream=out), "people.py", argv)
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
        if args.group:
            return _group_again(config, jobs.ConsoleContext("people", stream=out))
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
