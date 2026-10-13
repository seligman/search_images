#!/usr/bin/env python3
"""Export one library as a static website: searchable, with no server.

The export holds the same search, viewer and detail pages as the server
(the same scripts, see static/site.js), its search records split into
JavaScript files that the pages load with <script> tags, thumbnails and
web-size pictures. It can be copied to any static web host, or opened from
disk by double-clicking index.html.

    index.html  detail.html  [map.html]
    assets/     style.css shell.js site.js search-core.js viewer.js app.js
                detail.js [map.js] [embed-engine.js embed-kernel.js]
    data/       manifest.js  window.SEARCH_DATA_MANIFEST = {...}
                index-NNN.js  SEARCH_DATA.push([records])
                detail-NNN.js SEARCH_DATA.detail({picture id: details})
                [locations.js SEARCH_DATA.locations([{id, lat, lon}])]
                [vectors-NNN.js SEARCH_VECTORS.push({dims, ids, data, model})]
                [looks-NNN.js SEARCH_LOOKS.push({dims, ids, data, model})]
                [meaning/model.js meaning/w-NNN.js meaning/LICENSE.txt]
    thumbs/<id>.jpg   images/<id>.<ext>   [panos/<id>.jpg]   [tiles/z/x/y.jpg]
    [videos/<id>.mp4] (with the videos option "include")
    [robots.txt]      (unless search engines may index the site)
    .search-images-export   {"format": 1, "export_id": "<random>"}

The pages load the scripts and data files with a short hash of their
contents in the address (assets/site.js?v=..., the manifest's "versions"),
so a browser that cached files of an earlier export of the site never
mixes them with this one's.

Privacy is an allow-list (see DEFAULT_OPTIONS): pictures that are hidden,
missing, not checked (not described, or described by the older version 4
prompt), screenshots, documents (also by their document type, see
_reasons), or flagged as private or sensitive are left out unless the
options say otherwise; file names, folders and GPS positions with the map
(and the town, region and country of each position from the place names,
and the extended profile's landmark and place-guess fields) only go out
when switched on. The descriptions themselves go out as written. Paths,
hashes, file times (also as a picture's date), the describing model and
its costs, other libraries and endpoint settings never do.
With the "similar" option (on by default) the meaning vectors of the
exported pictures go along (embeddings.py), so "More like this" and
similar: work in the published site without a model; only vectors made
from the picture's current description are shipped, and a vector holds
nothing the description does not show. The model's name does not go.
With the "meaning" option too (on by default, when it can be used: see
meaning_status) the query model goes along (browser_model.py converts the
GGUF file the embedding server runs; data/meaning/), with the engine that
runs it (assets/embed-engine.js, embed-kernel.js; loaded only by the first
search that needs it), so typed searches find pictures by meaning in the
published site as well, from disk too. The model files hold only public
model data, the two made-up probe sentences with their vectors and the
query prefix; the model's license (embedding.license_file) goes along as
data/meaning/LICENSE.txt, and the foot of the pages links to it with the
owner's credit line (embedding.credit). The vector files then carry the
model's id (an opaque hash).
With the "looks" option (on by default; nothing goes when no exported
picture has a look vector) the look vectors of the exported pictures go
along too (looks.py, data/looks-NNN.js), so "Looks like this" and look:
work in the published site without a model. A look vector is made from
the picture itself, which the export shows anyway; its recipe goes as an
opaque id. Search by an example picture needs the server's image model:
exports do not have it.
Pictures are named by an id made from a secret kept in the data root
(make_xid), so neither the database ids nor their count can be worked out
from the export. Every picture written is cleaned of its metadata (EXIF,
XMP, IPTC, comments, text chunks, data after the image); a colour profile
and, for an original JPEG, the orientation are kept.

An export can be limited to one manual album of the owner's (the album
option, an album id; see albums.py): only the album's pictures of the
library are candidates, and the privacy rules above still apply to them.
The owner's favorites, ratings and album names are not published unless
the "marks" option is on: then the records carry fav, rt and al (album
numbers of the export, 1, 2, ...), and data/manifest.js lists the albums
({id, name, kind, items: the export ids of a manual album's exported
pictures in its order} -- manual albums with at least one exported
picture; smart albums with their saved search, query and options, as
typed but without a library choice, similar:/look: naming the picture by
its export id and album: the album by its number, #2), so album:"Name",
is:favorite and rating: work in the published site too. A smart album
whose search cannot be published is left out: one limited to another
library, or going by what the export leaves out (folder:, file:,
place:, has:gps or the Place filter while the matching option is off;
lib:), or naming a picture or an album that is not published.

A video is published as its poster frame (its thumbnail and web-size
picture), and with the videos option "include" also as a video: the
playable copy (videos.make_proxies) when there is one, else the original
when browsers play it (H.264), up to max_video_mb, copied into an MP4
without its metadata (videos.strip_copy: no tags, data streams, subtitles
or chapters, so no place, date or device). Other videos (HEVC without a
playable copy) keep their poster only; the review step counts them. A
360-degree video's poster is its stitched panorama, and its video is its
LRV (or playable copy): both lenses side by side, which the site's viewer
stitches as it plays (the record's fx). A published video's record names
the codec of what was published (vc: "h264" for a playable copy).

Exports are incremental and deterministic: a file is only written when its
bytes change, a second export with nothing changed writes no files, and
files that an earlier export of the folder created but this one does not
are removed (the list is kept in data/exports/state/<export id>.json in the
data root, never in the export). A folder is only written to when it is
empty or carries the marker file of an earlier export.

Used by the job runner (Manage page), "serve.py package" and its own
command line:

    python export.py --library NAME [--out DIR] [--images web|original|none]
                     [--location] [--file-names] [--folders]
                     [--include-screenshots] [--include-documents]
                     [--include-undescribed] [--include-private]
                     [--include-sensitive] [--zip] [--album NAME] [--marks]
    python export.py --serve FOLDER [--port 8000]

--serve tries an export on this computer (only it can open the site): a
small web server for the folder that, unlike "python -m http.server",
answers the range requests a browser makes to move in a video.

It exits with status 1 when pictures could not be exported, 2 when it was
stopped (an abort file: data/abort).
"""

import argparse
import base64
import collections
import concurrent.futures
import datetime
import errno
import fnmatch
import functools
import hashlib
import hmac
import html
import http.server
import json
import math
import os
import re
import secrets
import shutil
import struct
import sys
import threading
import time
import unicodedata
import zipfile
import zlib

import albums
import browser_model
import cache
import common
import db
import drives
import embeddings
import gps_check
import imageinfo
import jobs
import library_ops
import looks
import places
import prompts
import search_index
import videos
import world_map

# The marker file of an export folder, and its format.
MARKER = ".search-images-export"
# In the data folder: held while an export runs (one at a time, in any
# process), so two exports never write the same folder and state at once.
LOCK_FILE = "export.lock"
MARKER_FORMAT = 1
STATE_FORMAT = 1
# Part of every picture's source signature: raise it when the pictures an
# export writes change, so the next export writes them again.
PICTURE_VERSION = 1
# Data files are about this large (the records are spread over a power of
# two of them, by picture id).
INDEX_SHARD_BYTES = 1500000
DETAIL_SHARD_BYTES = 256 * 1024
# Picture ids in an export: this many base32 characters (60 bits).
XID_LENGTH = 12
XID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
EXPORT_ID_RE = re.compile(r"^[a-z0-9]{8,64}$")
_SECRET_RE = re.compile(r"^[0-9a-f]{32,128}$")
# Positions are rounded to this many decimals (about 11 m).
LOCATION_DECIMALS = 4
IMAGE_MODES = ("web", "original", "none")
VIDEO_MODES = ("skip", "include")
MAX_VIDEO_MB = (1, 100000)
MAX_TITLE = 200
MAX_CREDITS = 2000
# The credit an export with place names carries (CC BY 4.0, see places.py).
PLACES_CREDIT = ('Place names: <a href="%s">GeoNames</a> (<a href="%s" rel="license">'
                 'CC BY 4.0</a>)' % (html.escape(places.ATTRIBUTION_URL),
                                     html.escape(places.LICENSE_URL)))
MAX_ID_LIST = 100000
# Scripts and the style sheet every export carries (map.js only with the map).
ASSETS = ("style.css", "site.js", "shell.js", "search-core.js", "viewer.js", "app.js",
          "detail.js")
MAP_ASSETS = ("map.js",)
# (file in the export, template)
PAGES = (("index.html", "page.html"), ("detail.html", "detail.html"))
MAP_PAGES = (("map.html", "map.html"),)
# Folders directly inside the program folder that git ignores, so an export
# may be written there (a checkout must never get private photos to commit),
# and zip files there that git ignores.
IGNORED_SITE_FOLDERS = ("site", "site_*", "exports")
IGNORED_ZIP_NAMES = ("site.zip", "site_*.zip")
# Default output folder names that cannot be used (the state folder, and
# names Windows reserves).
RESERVED_SLUGS = frozenset(["state", "con", "prn", "aux", "nul"]
                           + ["com%d" % n for n in range(1, 10)]
                           + ["lpt%d" % n for n in range(1, 10)])
# The folders of an export this tool manages (and may clean up).
MANAGED_FOLDERS = ("assets", "data", "thumbs", "images", "panos", "tiles", "videos")
# The extensions of exported originals (see original_picture).
ORIGINAL_EXTENSIONS = ("jpg", "png", "gif", "webp")
# Free space an export leaves on the disk it writes to, at least.
SPACE_MARGIN = 32 * 1024 * 1024
# Zip entries with these extensions are stored, the rest deflated.
STORED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4")
# EXIF details of a picture an export may show (camera settings only: no
# dates and times, places, serial numbers, software, owner or copyright).
DETAIL_EXIF_NUMBERS = ("ExposureTime", "FNumber", "ISOSpeedRatings", "PhotographicSensitivity",
                       "FocalLength", "FocalLengthIn35mmFilm", "ExposureBiasValue")
DETAIL_EXIF_TEXT = ("LensModel",)
# Where an exported date may come from (search_index never exports a date
# from the file's time).
TAKEN_SOURCES = ("exif", "filename", "video")
# Rough sizes for the estimate, when the real file is not there to measure.
ESTIMATE_WEB_BYTES = 450000
ESTIMATE_PANO_BYTES = 2000000
ESTIMATE_RECORD_BYTES = 650
# Photo types that the review step always shows (they often hold text).
REVIEW_TYPES = ("screenshot", "document", "scan")
# A document type that is always private: a card or an ID.
PRIVATE_DOCUMENT_TYPES = ("card_or_id",)
# Document types (the describe step's document_type) that make a picture a
# document or a screenshot for the exclude_types option, whatever its photo
# type says: a photographed receipt is a document, a photo of a chat on a
# phone a screenshot.
DOCUMENT_KINDS = {"receipt": "document", "letter": "document", "form": "document",
                  "ticket": "document", "handwriting": "document",
                  "chat": "screenshot", "web": "screenshot", "app": "screenshot"}
# The flag of a picture with any other document type (a sign, a menu, a book
# page, a whiteboard, ...): it is published, but the review step shows it.
WRITING_FLAG = "writing"
# The flag of a picture described by the older (version 4) prompt, which
# did not ask about private information, sensitive content or the kind of
# picture: it is left out with the pictures not described yet.
UNCHECKED_FLAG = "unchecked"
# The kinds of picture exclude_types can name: the describe step's photo
# types, and "video" (every video, whatever the model saw in its frames).
VIDEO_KIND = "video"
EXPORT_KINDS = prompts.PHOTO_TYPES + (VIDEO_KIND,)
# Every flag a picture can have (see _reasons), most serious first.
FLAGS = (("private", "sensitive") + EXPORT_KINDS
         + (WRITING_FLAG, UNCHECKED_FLAG, "undescribed"))
# Pictures that cannot be read are named in the log up to this many times
# (then only counted), so an unreachable library does not flood the log.
LOGGED_FAILURES = 20
# Written while a file is being replaced (see Writer); left over only when
# an export was killed, and removed by the next one.
_TMP_RE = re.compile(r"\.tmp-\d+-\d+$")
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

DEFAULT_OPTIONS = {
    "title": "",                    # "" = the library's name
    "images": "web",                # "web" (2048 px) | "original" | "none" (thumbnails only)
    "location": False,              # GPS positions, place names, the map, landmark/place-guess
    "file_names": False,
    "folders": False,
    "exclude_types": ["screenshot", "document"],    # photo types left out
    "exclude_private": True,        # pictures showing private information (or an ID card)
    "exclude_sensitive": True,      # suggestive, explicit or graphic pictures
    "exclude_undescribed": True,    # pictures without a description (or an older one)
    "include_ids": [],              # pictures exported despite their flags (see _include_list)
    "exclude_ids": [],              # picture ids never exported
    "credits": "",                  # a line of plain text at the foot of the pages
    "zip": False,                   # also write <folder>.zip next to the folder
    "allow_indexing": False,        # False: the pages ask search engines not to index them
    "videos": "skip",               # "skip" (posters only) | "include" (playable videos too)
    "max_video_mb": 200,            # larger videos keep their poster only
    "similar": True,                # meaning vectors, so "More like this" works (no model needed)
    "meaning": True,                # the query model too: typed searches by meaning (when it
                                    # can be used, see meaning_status)
    "looks": True,                  # look vectors, so "Looks like this" works (no model needed)
    "album": None,                  # an album id: only that manual album's pictures (None: all)
    "marks": False,                 # favorites, ratings and album names (see albums.py)
}
_BOOLEAN_OPTIONS = ("location", "file_names", "folders", "exclude_private", "exclude_sensitive",
                    "exclude_undescribed", "zip", "allow_indexing", "similar", "meaning",
                    "looks", "marks")
# Where the query model goes (in data/), and the scripts that run it (in assets/).
MEANING_FOLDER = "meaning"
MEANING_ASSETS = ("embed-kernel.js", "embed-engine.js")
MEANING_LICENSE = "meaning/LICENSE.txt"
MAX_LICENSE_BYTES = 1024 * 1024


class ExportError(Exception):
    """The export cannot be made (a bad folder, an unknown library, ...)."""


class OptionsError(ExportError):
    """Export options that are not valid. errors: {option name: message}."""

    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join("%s: %s" % item for item in sorted(self.errors.items())))


class ExportWriteError(ExportError):
    """A file of the export could not be written (a full disk, no access, ...): the export stops."""


class StripError(ValueError):
    """A picture's bytes could not be parsed to remove its metadata."""


# Windows error numbers of a full disk (ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL).
_DISK_FULL_WINERRORS = (39, 112)


def os_error_text(error, path=None):
    """Plain words for an OSError (a full disk, no access, a missing drive) about path."""
    path = path or getattr(error, "filename", None) or "the export folder"
    if (getattr(error, "errno", None) == errno.ENOSPC
            or getattr(error, "winerror", None) in _DISK_FULL_WINERRORS):
        return "The disk holding %s is full. Free some space, or choose another folder." % path
    if isinstance(error, PermissionError):
        return ("%s cannot be written or read: access is denied. Choose another folder, or "
                "check its permissions." % path)
    if isinstance(error, FileNotFoundError):
        return "%s is not there (is its drive connected?)." % path
    return "%s cannot be written or read: %s" % (path, error.strerror or error)


def human_bytes(value):
    """1234567 -> "1.2 MB"."""
    value = float(max(0, value))
    unit = "bytes"
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    if unit == "bytes":
        return "%d bytes" % value
    return ("%.1f %s" if value < 10 else "%.0f %s") % (value, unit)


def _images():
    """The images module (it needs Pillow), imported on first use."""
    import images
    return images


# ---------------------------------------------------------------------------
# Options

def default_options():
    """A fresh copy of DEFAULT_OPTIONS."""
    return json.loads(json.dumps(DEFAULT_OPTIONS))


def _id_list(value, name, errors):
    if not isinstance(value, list):
        errors[name] = "must be a list of picture ids"
        return []
    if len(value) > MAX_ID_LIST:
        errors[name] = "may list at most %d pictures" % MAX_ID_LIST
        return []
    ids = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            errors[name] = "must be a list of picture ids (whole numbers)"
            return []
        ids.add(item)
    return sorted(ids)


def _include_list(value, errors):
    """The include_ids option checked: entries sorted by id, one per picture.

    An entry is a picture id, which publishes the picture whatever its
    flags say, or [id, [flags]]: published despite those flags (the ones
    it had when the owner chose to publish it), and left out again when it
    gets another one that leaves it out (see select_records). The Export
    dialog always sends [id, flags].
    """
    name = "include_ids"
    if not isinstance(value, list):
        errors[name] = "must be a list of picture ids"
        return []
    if len(value) > MAX_ID_LIST:
        errors[name] = "may list at most %d pictures" % MAX_ID_LIST
        return []
    entries = {}
    for item in value:
        flags = None
        if isinstance(item, list) and len(item) == 2:
            item, flags = item
            if (not isinstance(flags, list) or len(flags) > len(FLAGS)
                    or not all(isinstance(flag, str) and flag in FLAGS for flag in flags)):
                errors[name] = "must list each picture's flags by name (%s)" % ", ".join(FLAGS)
                return []
            flags = [flag for flag in FLAGS if flag in flags]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            errors[name] = "must be a list of picture ids (whole numbers), or of [id, [flags]]"
            return []
        entries[item] = item if flags is None else [item, flags]
    return [entries[key] for key in sorted(entries)]


def include_choices(options):
    """{picture id: None (publish whatever its flags) or the frozenset of flags it is published despite}."""
    choices = {}
    for entry in options.get("include_ids") or ():
        if isinstance(entry, list):
            choices[entry[0]] = frozenset(entry[1])
        else:
            choices[entry] = None
    return choices


def _plain_text(value, name, limit, errors):
    if not isinstance(value, str):
        errors[name] = "must be text"
        return ""
    text = " ".join(value.split())
    if len(text) > limit:
        errors[name] = "must be at most %d characters" % limit
        return ""
    return text


def validate_options(options, base=None):
    """Export options checked and completed: a new dict with every option of DEFAULT_OPTIONS.

    options (a dict, or None) overrides base (default: DEFAULT_OPTIONS;
    pass a library's saved options to change only some of them). Unknown
    keys and wrong values raise OptionsError, which names every bad option.
    """
    result = default_options()
    if base:
        result.update(_saved_options(base))
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise OptionsError({"options": "must be an object"})
    errors = {}
    for key, value in options.items():
        if key not in DEFAULT_OPTIONS:
            errors[key] = "is not an export option"
            continue
        if key in _BOOLEAN_OPTIONS:
            if not isinstance(value, bool):
                errors[key] = "must be true or false"
            else:
                result[key] = value
        elif key == "images":
            if value not in IMAGE_MODES:
                errors[key] = "must be one of %s" % ", ".join(IMAGE_MODES)
            else:
                result[key] = value
        elif key == "videos":
            if value not in VIDEO_MODES:
                errors[key] = "must be one of %s" % ", ".join(VIDEO_MODES)
            else:
                result[key] = value
        elif key == "max_video_mb":
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not MAX_VIDEO_MB[0] <= value <= MAX_VIDEO_MB[1]):
                errors[key] = "must be a whole number of megabytes from %d to %d" % MAX_VIDEO_MB
            else:
                result[key] = value
        elif key == "title":
            result[key] = _plain_text(value, key, MAX_TITLE, errors)
        elif key == "credits":
            result[key] = _plain_text(value, key, MAX_CREDITS, errors)
        elif key == "exclude_types":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors[key] = "must be a list of picture types"
            else:
                unknown = sorted(set(value) - set(EXPORT_KINDS))
                if unknown:
                    errors[key] = "unknown picture type(s): %s (known: %s)" % (
                        ", ".join(unknown), ", ".join(EXPORT_KINDS))
                else:
                    result[key] = [kind for kind in EXPORT_KINDS if kind in value]
        elif key == "include_ids":
            result[key] = _include_list(value, errors)
        elif key == "album":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or not 0 < value < 2 ** 63):
                errors[key] = "must be an album id (or null for the whole library)"
            else:
                result[key] = value
        elif key == "exclude_ids":
            result[key] = _id_list(value, key, errors)
    if errors:
        raise OptionsError(errors)
    return result


def _saved_options(saved):
    """The valid options of a saved dict; anything wrong in it is left at its default."""
    if not isinstance(saved, dict):
        return {}
    result = {}
    for key, value in saved.items():
        try:
            result.update({key: validate_options({key: value})[key]})
        except OptionsError:
            pass
    return result


def library_options(library, overrides=None):
    """The export options of a library: its saved ones (config "export"), then overrides."""
    saved = library.get("export") if isinstance(library, dict) else None
    return validate_options(overrides, base=saved)


def options_to_save(options):
    """The options worth keeping in config.json: those that differ from DEFAULT_OPTIONS.

    options must be checked already (validate_options). Options left at
    their defaults are not written, so they follow the defaults.
    """
    return {key: value for key, value in options.items()
            if key in DEFAULT_OPTIONS and DEFAULT_OPTIONS[key] != value}


def options_summary(options):
    """The options an export may publish about itself (no ids, no text)."""
    summary = {"images": options["images"], "location": options["location"],
               "file_names": options["file_names"], "folders": options["folders"]}
    if options["videos"] == "include":
        # (Only then: an export without videos stays as it was.)
        summary["videos"] = options["videos"]
    if options["marks"]:
        summary["marks"] = True
    return summary


# ---------------------------------------------------------------------------
# Folders, the marker and the state

def _normalized(path):
    return os.path.normcase(os.path.abspath(path))


# Folders are compared like the library folders are (no file system access),
# so a drive named through its admin share (\\localhost\C$\...) is the drive.
_is_within = library_ops.is_within


def _first_part(path):
    """The first folder (or file) name of path below the program folder."""
    return os.path.relpath(_normalized(path), _normalized(common.BASE_DIR)).split(os.sep)[0]


def _git_ignored(path, patterns):
    first = _first_part(path)
    return any(fnmatch.fnmatchcase(first, os.path.normcase(pattern)) for pattern in patterns)


def state_dir():
    """data/exports/state: what each export wrote (never inside an export)."""
    return os.path.join(common.exports_dir(), "state")


def library_slug(name):
    """A folder name made from a library name: lower-case ASCII letters, digits and dashes."""
    text = unicodedata.normalize("NFKD", name or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60].strip("-")


def _library_slug(library):
    slug = library_slug(common.library_label(library))
    if not slug:
        slug = library_slug(library.get("id") or "") or "library"
    if slug in RESERVED_SLUGS:
        slug += "-library"
    return slug


def default_output_dir(config, library):
    """data/exports/<slug of the library name>/ (with the library id when two names agree).

    A library keeps the one of those two folders it was last exported to:
    adding (or removing) another library whose name gives the same slug
    never moves the default folder of one that was exported already, so
    exporting it again updates its site in place (same picture ids, same
    links).
    """
    slug = _library_slug(library)
    plain = os.path.join(common.exports_dir(), slug)
    with_id = os.path.join(common.exports_dir(), "%s-%s" % (
        slug, library_slug(library.get("id") or "") or "library"))
    if library.get("id"):
        used = _exported_folders(library["id"])
        known = [(used[_normalized(folder)], folder) for folder in (plain, with_id)
                 if _normalized(folder) in used]
        if known:
            return max(known)[1]
    for other in config.get("libraries", []):
        if other is not library and other.get("id") != library.get("id") \
                and _library_slug(other) == slug:
            return with_id
    return plain


def check_output_dir(out_dir, config, library_id=None):
    """Raise ExportError if an export (of library_id) must not be written to out_dir.

    An export removes the files an earlier export of the folder made, so
    it refuses a folder that overlaps a library, holds the data root, the
    data folder or the program, sits in the data folder outside
    data/exports (or is that folder, or its state folder), is not empty
    and lacks the marker an export leaves, or holds the export of another
    library that is still in the settings. Inside the program folder (a git
    checkout) only folders git ignores are allowed: site/, site_*/,
    exports/ (and data/exports when the data root is there), so private
    photos never end up in a commit. A folder that cannot be read raises
    ExportError too; see check_writable for one that cannot be written.
    """
    out_dir = os.path.abspath(out_dir)
    for library in config.get("libraries", []):
        root = common.library_root(library)
        if root and (_is_within(out_dir, root) or _is_within(root, out_dir)):
            raise ExportError(
                "The output folder %s overlaps the folder of library %s. "
                "Choose a folder outside your libraries."
                % (out_dir, common.library_label(library)))
    for folder, what in ((common.settings_base(), "the data root"),
                         (common.data_dir(), "the data folder"),
                         (common.BASE_DIR, "the program folder")):
        if _is_within(folder, out_dir):
            raise ExportError("The output folder %s contains %s. Choose a different folder."
                              % (out_dir, what))
    exports = common.exports_dir()
    if _is_within(out_dir, common.data_dir()) and (
            not _is_within(out_dir, exports) or _normalized(out_dir) == _normalized(exports)
            or _is_within(out_dir, state_dir())):
        raise ExportError("Inside the data folder, exports can only go to a folder in %s."
                          % exports)
    if (_is_within(out_dir, common.BASE_DIR) and not _is_within(out_dir, exports)
            and not _git_ignored(out_dir, IGNORED_SITE_FOLDERS)):
        raise ExportError(
            "The output folder %s is inside the program folder %s, where its private "
            "photos could end up in a commit. Choose a folder outside it, or one named "
            "site or site_<something> directly inside it." % (out_dir, common.BASE_DIR))
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise ExportError("%s is a file, not a folder." % out_dir)
    if not os.path.isdir(out_dir):
        return
    try:
        names = os.listdir(out_dir)
    except OSError as error:
        raise ExportError(os_error_text(error, out_dir))
    if names and MARKER not in names:
        if _old_package_site(out_dir):
            # Its files are replaced, as with a site an older version marked.
            return
        raise ExportError(
            "%s is not empty and does not hold an export made by this program. Choose an "
            "empty or new folder (an export removes files from the folder it writes to)."
            % out_dir)
    marker = read_marker(out_dir) if names else None
    state = load_state(marker["export_id"]) if marker and marker.get("export_id") else None
    owner = state.get("library_id") if state else None
    if library_id and owner and owner != library_id:
        other = common.get_library(config, owner)
        if other is not None:
            raise ExportError(
                "%s holds the export of library %s. Choose another folder: each library "
                "needs a folder of its own (an export replaces what the folder holds)."
                % (out_dir, common.library_label(other)))


def _old_package_site(out_dir):
    """True for a site made by the "package" command of the first versions (no marker).

    Its index.html, site.js and app.js are at the top, and its
    data/manifest.json lists its data shards (as a marker of a later older
    version does).
    """
    for name in ("index.html", "site.js", "app.js"):
        if not os.path.isfile(os.path.join(out_dir, name)):
            return False
    try:
        with open(os.path.join(out_dir, "data", "manifest.json"), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "shards" in data and "generated" in data


def check_writable(out_dir):
    """Raise ExportError unless files can be made in out_dir.

    When out_dir does not exist yet, the nearest folder above it that does
    must let a folder be made in it. Leaves nothing behind.
    """
    folder = os.path.abspath(out_dir)
    while not os.path.isdir(folder):
        if os.path.exists(folder):
            raise ExportError("%s cannot be made: %s is a file." % (out_dir, folder))
        parent = os.path.dirname(folder)
        if parent == folder:
            raise ExportError("%s cannot be made: its drive or share is not there." % out_dir)
        folder = parent
    probe = os.path.join(folder, "%s.tmp-%d-%d" % (MARKER, os.getpid(), threading.get_ident()))
    try:
        if _normalized(folder) == _normalized(out_dir):
            with open(probe, "wb") as handle:
                handle.write(b"test")
            os.remove(probe)
        else:
            os.mkdir(probe)
            os.rmdir(probe)
    except OSError as error:
        if isinstance(error, PermissionError):
            raise ExportError("Files cannot be written in %s: access is denied. Choose another "
                              "folder." % folder)
        raise ExportError(os_error_text(error, folder))


def zip_path_for(out_dir):
    """The zip file of an export folder: <folder>.zip next to it."""
    return os.path.abspath(out_dir).rstrip("\\/") + ".zip"


def check_zip_path(path, config):
    """Raise ExportError if the zip of an export must not be written to path.

    A file already there is only replaced when an export made it (its
    state records it, and it was not changed since): a zip of the same
    name made by someone else is never overwritten.
    """
    for library in config.get("libraries", []):
        root = common.library_root(library)
        if root and _is_within(path, root):
            raise ExportError("The zip file %s would be inside the folder of library %s."
                              % (path, common.library_label(library)))
    exports = common.exports_dir()
    if _is_within(path, common.data_dir()) and not _is_within(path, exports):
        raise ExportError("Inside the data folder, zip files can only go to %s." % exports)
    if (_is_within(path, common.BASE_DIR) and not _is_within(path, exports)
            and not _git_ignored(path, IGNORED_SITE_FOLDERS + IGNORED_ZIP_NAMES)):
        raise ExportError(
            "The zip file %s would be inside the program folder, where it could end up in "
            "a commit. Export to a folder outside it (or to site/ and without a zip)." % path)
    if os.path.isdir(path):
        raise ExportError("%s is a folder; the zip file cannot be written." % path)
    if os.path.exists(path) and not _zip_made_here(path):
        raise ExportError("%s already exists and was not made by an export of this program "
                          "(or was changed since). Move it, or choose another folder." % path)


def read_marker(out_dir):
    """The marker of an export folder: None, {"export_id": ...} or {"legacy": True}.

    A legacy marker is one written by the "package" command of older
    versions (it lists data shards and has no export id). Raises
    ExportError for a marker that cannot be read.
    """
    path = os.path.join(out_dir, MARKER)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise ExportError("The export marker %s cannot be read (%s). Delete the folder's "
                          "contents to export there again." % (path, error))
    if isinstance(data, dict):
        export_id = data.get("export_id")
        if isinstance(export_id, str) and EXPORT_ID_RE.match(export_id):
            return {"export_id": export_id}
        if "export_id" not in data and ("shards" in data or "generated" in data):
            return {"legacy": True}
    raise ExportError("The export marker %s is not one this program wrote." % path)


def marker_bytes(export_id):
    """The marker file's contents: the format and the export id, nothing else."""
    return (json.dumps({"format": MARKER_FORMAT, "export_id": export_id}, sort_keys=True)
            + "\n").encode("ascii")


def state_path(export_id):
    if not EXPORT_ID_RE.match(export_id or ""):
        raise ValueError("invalid export id: %r" % (export_id,))
    return os.path.join(state_dir(), "%s.json" % export_id)


def load_state(export_id):
    """The saved state of an export (dict), or None."""
    try:
        with open(state_path(export_id), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("export_id") != export_id:
        return None
    if not isinstance(state.get("secret"), str) or not _SECRET_RE.match(state["secret"]):
        return None
    if not isinstance(state.get("files"), dict):
        state["files"] = {}
    return state


def save_state(state):
    common.ensure_dir(state_dir())
    common.write_file_atomic(state_path(state["export_id"]),
                             json.dumps(state, sort_keys=True, indent=1) + "\n")


def _states():
    """Every readable export state in the data root."""
    try:
        names = sorted(os.listdir(state_dir()))
    except OSError:
        return []
    found = []
    for name in names:
        if name.endswith(".json") and EXPORT_ID_RE.match(name[:-5]):
            state = load_state(name[:-5])
            if state is not None:
                found.append(state)
    return found


def _state_for_folder(out_dir, library_id):
    """The newest state of an export of this library into out_dir, or None."""
    wanted = _normalized(out_dir)
    best = None
    for state in _states():
        if (state.get("library_id") == library_id and isinstance(state.get("out_dir"), str)
                and _normalized(state["out_dir"]) == wanted
                and (best is None or state.get("updated", 0) > best.get("updated", 0))):
            best = state
    return best


def last_export_dir(config, library_id):
    """(folder, missing): where a library was last exported to.

    folder is the output folder of the newest export of the library (from
    its state in the data root), and missing is True when that folder is
    not there any more (moved or deleted, or its drive is not connected).
    A library with no export state: its default folder when a site is
    there (made by an older version), else (None, False).
    """
    best = None
    for state in _states():
        if state.get("library_id") == library_id and isinstance(state.get("out_dir"), str) \
                and (best is None or state.get("updated", 0) > best.get("updated", 0)):
            best = state
    if best is not None:
        return best["out_dir"], not os.path.isdir(best["out_dir"])
    library = common.get_library(config, library_id)
    if library is None:
        return None, False
    folder = default_output_dir(config, library)
    if os.path.isfile(os.path.join(folder, "index.html")):
        return folder, False
    return None, False


# State files can be large (one line per file of the export): their short
# summaries are kept while the file is unchanged.
_summaries = {}
_summaries_lock = threading.Lock()


def _state_summary(path):
    """A short summary of one state file (or None), cached while the file is unchanged."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    with _summaries_lock:
        cached = _summaries.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    export_id = os.path.basename(path)[:-len(".json")]
    state = load_state(export_id)
    summary = None
    if state is not None and isinstance(state.get("out_dir"), str):
        sizes = [entry[1] for entry in state["files"].values()
                 if isinstance(entry, list) and len(entry) == 3
                 and isinstance(entry[1], int) and entry[1] > 0]
        zip_info = state.get("zip")
        zip_file = None
        if isinstance(zip_info, dict) and isinstance(zip_info.get("path"), str):
            zip_file = (_normalized(zip_info["path"]), zip_info.get("size"),
                        zip_info.get("mtime"))
        summary = {"library_id": state.get("library_id"), "output": state["out_dir"],
                   "updated": state.get("updated") or 0,
                   "finished": state.get("finished") is not False,
                   "pictures": (state["count"] if isinstance(state.get("count"), int)
                                and not isinstance(state.get("count"), bool) else
                                sum(1 for rel in state["files"] if rel.startswith("thumbs/"))),
                   "bytes": sum(sizes),
                   "zip": zip_info.get("path") if isinstance(zip_info, dict) else None,
                   "zip_file": zip_file}
    with _summaries_lock:
        _summaries[path] = (key, summary)
    return summary


def _state_summaries():
    """The summary (_state_summary) of every readable export state in the data root."""
    try:
        names = os.listdir(state_dir())
    except OSError:
        return []
    found = []
    for name in sorted(names):
        if name.endswith(".json") and EXPORT_ID_RE.match(name[:-len(".json")]):
            summary = _state_summary(os.path.join(state_dir(), name))
            if summary is not None:
                found.append(summary)
    return found


def _exported_folders(library_id):
    """{normalized output folder: time of the newest export into it} of a library."""
    folders = {}
    for summary in _state_summaries():
        if summary["library_id"] == library_id:
            key = _normalized(summary["output"])
            folders[key] = max(folders.get(key, 0), summary["updated"])
    return folders


def _zip_made_here(path):
    """True when the file at path is a zip an export wrote (and nobody changed since)."""
    try:
        stat = os.stat(path)
    except OSError:
        return False
    wanted = (_normalized(path), stat.st_size, stat.st_mtime_ns)
    return any(summary["zip_file"] == wanted for summary in _state_summaries())


def last_export(library_id):
    """What the newest export of a library holds, from its state in the data root, or None.

    {"output" (folder), "updated" (time), "finished" (False: it was stopped
    or failed part-way), "pictures", "bytes" (of the files it wrote), "zip"
    (its zip file or None)}. The output folder itself is not looked at (it
    may be on a share that does not answer).
    """
    best = None
    for summary in _state_summaries():
        if (summary["library_id"] == library_id
                and (best is None or summary["updated"] > best["updated"])):
            best = summary
    if best is None:
        return None
    return {key: value for key, value in best.items() if key not in ("library_id", "zip_file")}


# ---------------------------------------------------------------------------
# Picture ids

def make_xid(secret, export_id, image_id):
    """The id of a picture in an export: XID_LENGTH base32 characters.

    A keyed hash (HMAC-SHA256) of the export id and the database id, with
    a secret that never leaves the data root: the database ids (and how
    many pictures the library has) cannot be found from the export, while
    the same picture keeps its id from one export of the folder to the
    next.
    """
    message = ("%s:%d" % (export_id, int(image_id))).encode("ascii")
    digest = hmac.new(bytes.fromhex(secret), message, hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:XID_LENGTH]


def bucket_of(xid, count):
    """Which of count data files holds a picture (static/site.js does the same sum)."""
    value = 0
    for character in xid[:4]:
        digit = XID_ALPHABET.find(character)
        value = value * 32 + max(digit, 0)
    return value % max(1, count)


def _shard_count(total_bytes, target):
    count = 1
    while total_bytes > count * target and count < 4096:
        count *= 2
    return count


# ---------------------------------------------------------------------------
# Removing metadata from pictures

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


def _tiff_orientation(tiff):
    """The Orientation (1-8) in an EXIF TIFF block's first IFD, or None."""
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    order = "<" if tiff[:2] == b"II" else ">"
    try:
        offset = struct.unpack(order + "I", tiff[4:8])[0]
        count = struct.unpack(order + "H", tiff[offset:offset + 2])[0]
        for index in range(min(count, 512)):
            start = offset + 2 + 12 * index
            tag, kind = struct.unpack(order + "HH", tiff[start:start + 4])
            if tag == 0x0112 and kind == 3:
                value = struct.unpack(order + "H", tiff[start + 8:start + 10])[0]
                return value if 1 <= value <= 8 else None
    except (struct.error, IndexError):
        return None
    return None


# (Moved to imageinfo, which every picture encoder uses; kept here by name.)
scrub_icc = imageinfo.scrub_icc


def _scrub_icc_segments(body):
    """Scrub the ICC profile split over a JPEG's APP2 segments (in body, in place).

    The profile keeps its length, so every segment keeps its size. A
    profile whose parts do not fit together is dropped.
    """
    places = [index for index, part in enumerate(body)
              if part[:2] == b"\xff\xe2" and part[4:16] == b"ICC_PROFILE\x00"]
    if not places:
        return
    parts = []
    for index in places:
        segment = body[index]
        parts.append((segment[16], segment[17], index, segment[18:]))
    total = parts[0][1]
    numbers = sorted(part[0] for part in parts)
    if total != len(parts) or numbers != list(range(1, total + 1)) or any(
            part[1] != total for part in parts):
        for index in reversed(places):
            del body[index]
        return
    parts.sort()
    clean = scrub_icc(b"".join(part[3] for part in parts))
    if clean is None:
        for index in reversed(places):
            del body[index]
        return
    pos = 0
    for number, _total, index, chunk in parts:
        piece = clean[pos:pos + len(chunk)]
        pos += len(chunk)
        payload = b"ICC_PROFILE\x00" + bytes((number, total)) + piece
        body[index] = b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload


def _orientation_segment(orientation):
    """A minimal EXIF APP1 segment holding only an orientation tag."""
    tiff = (b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
            + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0))
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def strip_jpeg(data, keep_orientation=True):
    """JPEG bytes without metadata, losslessly (the image data is not touched).

    Kept: the image itself, a plain JFIF header (without its thumbnail),
    the ICC colour profile (its free text replaced, see scrub_icc), the
    Adobe colour transform and (with keep_orientation) the EXIF
    orientation as a new minimal EXIF block.
    Removed: EXIF (with GPS and maker notes), XMP, IPTC, comments, every
    other application segment (MPF, ...) and anything after the end of
    the image (more pictures, motion-photo video, maker trailers). Raises
    StripError when the bytes are not a JPEG this can parse.
    """
    data = bytes(data)
    if not data.startswith(_JPEG_SOI):
        raise StripError("not a JPEG")
    head, body = [_JPEG_SOI], []
    orientation = None
    pos, size = 2, len(data)
    while pos < size:
        if data[pos] != 0xFF:
            raise StripError("no marker at byte %d" % pos)
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            body.append(bytes((0xFF, marker)))
            continue
        if pos + 2 > size:
            raise StripError("cut off segment")
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > size:
            raise StripError("bad segment length")
        payload = data[pos + 2:pos + length]
        segment = data[pos - 2:pos + length]
        pos += length
        if marker == 0xDA:
            # Start of scan: the compressed data runs to the next marker
            # that is not a stuffed 0xFF00 or a restart marker.
            body.append(segment)
            start = pos
            while True:
                found = data.find(b"\xff", pos)
                if found < 0 or found + 1 >= size:
                    # Cut off: keep what there is and end the image.
                    body.append(data[start:])
                    pos = size
                    break
                following = data[found + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    pos = found + 2
                    continue
                if following == 0xFF:
                    pos = found + 1
                    continue
                body.append(data[start:found])
                pos = found
                break
            continue
        if marker == 0xE0:
            if payload.startswith(b"JFIF\x00") and len(payload) >= 14:
                jfif = payload[:12] + b"\x00\x00"
                head.append(b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + jfif)
            continue
        if marker == 0xE1:
            if payload.startswith(b"Exif\x00\x00") and orientation is None:
                orientation = _tiff_orientation(payload[6:])
            continue
        if marker == 0xE2:
            if payload.startswith(b"ICC_PROFILE\x00"):
                body.append(segment)
            continue
        if marker == 0xEE:
            if payload.startswith(b"Adobe"):
                body.append(segment)
            continue
        if 0xE3 <= marker <= 0xEF or marker == 0xFE:
            continue
        body.append(segment)
    if not any(part[:2] == b"\xff\xda" for part in body):
        raise StripError("no image data")
    _scrub_icc_segments(body)
    if keep_orientation and orientation and orientation != 1:
        head.append(_orientation_segment(orientation))
    return b"".join(head + body + [_JPEG_EOI])


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Ancillary PNG chunks about how to show the picture (colours, transparency,
# animation); every other ancillary chunk (text, EXIF, time, a suggested
# palette with its free-text name, ...) goes. iCCP is rebuilt (_png_iccp).
_PNG_KEEP = frozenset([b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"bKGD", b"pHYs",
                       b"acTL", b"fcTL", b"fdAT", b"cICP", b"mDCv", b"cLLi"])


def _png_chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def _png_iccp(payload):
    """An iCCP chunk with a fixed profile name and the profile scrubbed, or None."""
    name_end = payload.find(b"\x00")
    if name_end < 1 or name_end + 2 > len(payload) or payload[name_end + 1] != 0:
        return None
    try:
        profile = zlib.decompressobj().decompress(payload[name_end + 2:], 4 * 1024 * 1024)
    except zlib.error:
        return None
    clean = scrub_icc(profile)
    if clean is None:
        return None
    return _png_chunk(b"iCCP", b"ICC profile\x00\x00" + zlib.compress(clean, 9))


def strip_png(data):
    """PNG bytes without text, EXIF, time and unknown chunks (losslessly).

    A colour profile stays, named "ICC profile" and with its free text
    replaced (see scrub_icc).
    """
    data = bytes(data)
    if not data.startswith(_PNG_SIGNATURE):
        raise StripError("not a PNG")
    out = [_PNG_SIGNATURE]
    pos = len(_PNG_SIGNATURE)
    while True:
        if pos + 12 > len(data):
            raise StripError("cut off PNG")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            raise StripError("cut off PNG chunk")
        critical = not (kind[0] & 0x20)
        if kind == b"iCCP":
            chunk = _png_iccp(data[pos + 8:end - 4])
            if chunk is not None:
                out.append(chunk)
        elif critical or kind in _PNG_KEEP:
            out.append(data[pos:end])
        pos = end
        if kind == b"IEND":
            break
    return b"".join(out)


def _gif_blocks(data, pos):
    """The end of a run of GIF data sub-blocks starting at pos."""
    while True:
        if pos >= len(data):
            raise StripError("cut off GIF")
        count = data[pos]
        pos += 1
        if count == 0:
            return pos
        pos += count


def strip_gif(data):
    """GIF bytes without comments and application data (the loop setting stays)."""
    data = bytes(data)
    if not data.startswith((b"GIF87a", b"GIF89a")) or len(data) < 13:
        raise StripError("not a GIF")
    pos = 13
    flags = data[10]
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 7) + 1))
    out = [data[:pos]]
    while True:
        if pos >= len(data):
            break
        kind = data[pos]
        if kind == 0x3B:
            break
        if kind == 0x21:
            if pos + 2 > len(data):
                raise StripError("cut off GIF")
            label = data[pos + 1]
            end = _gif_blocks(data, pos + 2)
            keep = label in (0xF9, 0x01)
            if label == 0xFF and pos + 3 + 11 <= len(data):
                keep = data[pos + 3:pos + 14] in (b"NETSCAPE2.0", b"ANIMEXTS1.0")
            if keep:
                out.append(data[pos:end])
            pos = end
        elif kind == 0x2C:
            if pos + 10 > len(data):
                raise StripError("cut off GIF")
            start = pos
            image_flags = data[pos + 9]
            pos += 10
            if image_flags & 0x80:
                pos += 3 * (2 ** ((image_flags & 7) + 1))
            end = _gif_blocks(data, pos + 1)
            out.append(data[start:end])
            pos = end
        else:
            raise StripError("unknown GIF block")
    out.append(b";")
    return b"".join(out)


_WEBP_KEEP = frozenset([b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"])


def strip_webp(data):
    """WebP bytes without EXIF, XMP and unknown chunks (losslessly)."""
    data = bytes(data)
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise StripError("not a WebP")
    out = []
    pos = 12
    end = min(len(data), 8 + struct.unpack("<I", data[4:8])[0])
    profile_kept = False
    while pos + 8 <= end:
        kind = data[pos:pos + 4]
        length = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        stop = pos + 8 + length + (length & 1)
        if pos + 8 + length > len(data):
            raise StripError("cut off WebP chunk")
        chunk = data[pos:min(stop, len(data))]
        if kind == b"VP8X" and length >= 10:
            # Say that there is no EXIF (0x08) and no XMP (0x04) any more.
            chunk = chunk[:8] + bytes((chunk[8] & ~0x0C & 0xFF,)) + chunk[9:]
        elif kind == b"ICCP":
            clean = scrub_icc(chunk[8:8 + length])
            if clean is None:
                pos = stop
                continue
            chunk = chunk[:8] + clean + chunk[8 + length:]
            profile_kept = True
        if kind in _WEBP_KEEP:
            if len(chunk) & 1:
                chunk += b"\x00"
            out.append(chunk)
        pos = stop
    if not out:
        raise StripError("empty WebP")
    if out[0][:4] == b"VP8X" and len(out[0]) >= 9 and not profile_kept:
        # No colour profile (any more): say so (0x20).
        out[0] = out[0][:8] + bytes((out[0][8] & ~0x20 & 0xFF,)) + out[0][9:]
    payload = b"WEBP" + b"".join(out)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def web_jpeg(data):
    """A screen-sized picture (a stored preview) as clean JPEG bytes.

    A JPEG loses its metadata losslessly; a PNG preview (see images.py) is
    turned into a JPEG.
    """
    if imageinfo.sniff_mime(data) == "image/jpeg":
        try:
            return strip_jpeg(data, keep_orientation=False)
        except StripError:
            pass
    images = _images()
    return images.jpeg_from_bytes(data, images.PREVIEW_QUALITY)


def original_picture(data, orientation=None):
    """(clean bytes, extension) of an original, its metadata removed.

    JPEG, PNG, GIF and WebP keep their image data as they are; a BMP
    becomes a PNG (the same pixels: a BMP can name a colour profile file
    and keep bytes after its pixels). Other formats (HEIC, HEIF, AVIF,
    TIFF, ...), and a PNG, GIF or WebP that needs turning upright
    (orientation 2-8), become full-size JPEGs.
    """
    mime = imageinfo.sniff_mime(data)
    upright = orientation in (None, 1)
    try:
        if mime == "image/jpeg":
            return strip_jpeg(data), "jpg"
        if mime == "image/png" and upright:
            return strip_png(data), "png"
        if mime == "image/gif" and upright:
            return strip_gif(data), "gif"
        if mime == "image/webp" and upright:
            return strip_webp(data), "webp"
    except StripError:
        pass
    images = _images()
    if mime == "image/bmp" and upright:
        return images.png_from_bytes(data), "png"
    return images.jpeg_from_bytes(data, images.BROWSER_QUALITY), "jpg"


def clean_jpeg(data):
    """Thumbnail or panorama JPEG bytes without metadata (re-encoded only if unreadable)."""
    try:
        return strip_jpeg(data, keep_orientation=False)
    except StripError:
        images = _images()
        return images.jpeg_from_bytes(data, images.PREVIEW_QUALITY)


# ---------------------------------------------------------------------------
# What goes out

_ROW_COLUMNS = ("id, path, name, size, mtime, width, height, content_hash, has_preview, "
                "projection, exif, taken_at, taken_source, lat, lon, media, duration")


def _is_video(row):
    return row["media"] == videos.MEDIA_VIDEO


def video_plan(row, options, library_id):
    """What an export does with a video row: (kind, source path or None, size, reason).

    kind is "skip" (the videos option is "skip", or the row is no video),
    "video" (the file at source, size bytes, is copied: its playable copy,
    or the original when browsers play it) or "poster" (only the poster is
    published; reason "not_playable" -- HEVC and others, without a
    playable copy -- or "too_large", over max_video_mb). source None with
    kind "video": the original, read from the library's folder -- for a
    360 video its LRV there (both lenses side by side, which the site's
    player stitches; size: its media data, as the scan found it), or its
    original only when that holds both lenses in one stream browsers play
    (videos.playable_360); never lens streams.
    """
    if options.get("videos") != "include" or not _is_video(row):
        return "skip", None, 0, ""
    proxy = videos.existing_proxy(library_id, row["id"], row["content_hash"])
    size = _file_size(proxy) if proxy else None
    if proxy and size is not None:
        source = proxy
    elif row["projection"] == videos.PROJECTION_360:
        facts = videos.video_facts(row["exif"])
        lrv = videos.lrv_facts(row["exif"])
        if videos.playable(lrv) == "yes" and isinstance(lrv.get("size"), int):
            # (What a copy without metadata keeps: not the camera's trailer.)
            media = lrv.get("media_size")
            source, size = None, media if isinstance(media, int) and media > 0 else lrv["size"]
        elif facts.get("lenses") == "frame" and videos.playable(facts) == "yes":
            source, size = None, row["size"] or 0
        else:
            return "poster", None, 0, "not_playable"
    else:
        try:
            exif = json.loads(row["exif"] or "{}")
        except (TypeError, ValueError):
            exif = {}
        facts = exif.get("video") if isinstance(exif, dict) else None
        if videos.playable(facts) != "yes":
            return "poster", None, 0, "not_playable"
        source, size = None, row["size"] or 0
    if size > options["max_video_mb"] * 1024 * 1024:
        return "poster", None, 0, "too_large"
    return "video", source, size, ""


def published_codec(row, source):
    """The codec of the video an export publishes for a row ("" when not known).

    source is video_plan's: a playable copy (H.264), or None for the file in
    the library -- a 360 video's LRV (its codec as the scan found it), else
    the original.
    """
    if source is not None:
        return "h264"
    facts = videos.video_facts(row["exif"])
    if row["projection"] == videos.PROJECTION_360:
        lrv = videos.lrv_facts(row["exif"])
        if lrv.get("codec"):
            return str(lrv["codec"])
    return str(facts.get("codec") or "")


def _load_rows(conn, library_id):
    """{id: row} of the present, visible pictures of a library."""
    rows = conn.execute(
        "SELECT %s FROM images WHERE library_id = ? AND missing_since IS NULL AND hidden = 0"
        % _ROW_COLUMNS, (library_id,))
    return {row["id"]: row for row in rows}


def _hidden_count(conn, library_id, among=None):
    if among is not None:
        ids = sorted(among)
        count = 0
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            count += conn.execute(
                "SELECT COUNT(*) FROM images WHERE library_id = ? AND hidden = 1 AND "
                "missing_since IS NULL AND id IN (%s)" % ",".join("?" * len(part)),
                [library_id] + part).fetchone()[0]
        return count
    return conn.execute("SELECT COUNT(*) FROM images WHERE library_id = ? AND hidden = 1 "
                        "AND missing_since IS NULL", (library_id,)).fetchone()[0]


# has: words search-core reads as "has a location" (HAS_WORDS), and their starts.
_LOCATION_WORDS = ("gps", "location", "geo", "position", "coordinate", "place")


def _rewrite(text, changes):
    """text with the spans of changes ({(start, stop): new text}, not overlapping)
    replaced."""
    for (start, stop), value in sorted(changes.items(), reverse=True):
        text = text[:start] + value + text[stop:]
    return text


def album_choice(conn, library_id, options):
    """The album an export is limited to: {id, name, ids (its pictures of the library,
    hidden ones too), elsewhere (how many of its pictures are in other libraries)},
    or None for the whole library. Raises ExportError when the album is gone or is
    a smart one."""
    album_id = options.get("album")
    if album_id is None:
        return None
    album = albums.get_album(conn, album_id)
    if album is None:
        raise ExportError("The album this export is limited to (id %d) no longer exists. "
                          "Choose another album, or the whole library, in the export "
                          "options." % album_id)
    if album["kind"] != "manual":
        raise ExportError("%s is a smart album (a saved search). An export can be limited to "
                          "a manual album only: on the Albums page, \"Copy to a new album\" "
                          "makes one with its pictures." % album["name"])
    ids = albums.album_items(conn, album_id, [library_id])
    everywhere = albums.album_items(conn, album_id)
    return {"id": album_id, "name": album["name"], "ids": set(ids),
            "elsewhere": len(everywhere) - len(ids)}


def picture_kinds(record):
    """The kinds of a picture the exclude_types option looks at, in FLAGS order.

    Its photo type, and the kind its document type stands for (see
    DOCUMENT_KINDS): a receipt is a document even when the AI model typed
    the picture a photo or a scan. A video is "video", and also the kind
    the model saw in its frames (search_index's vt: a screen recording is
    a screenshot).
    """
    kinds = set()
    if record.get("ty"):
        kinds.add(record["ty"])
    if record.get("vt"):
        kinds.add(record["vt"])
    if record.get("mv"):
        kinds.add(VIDEO_KIND)
    if record.get("dt") in DOCUMENT_KINDS:
        kinds.add(DOCUMENT_KINDS[record["dt"]])
    return [kind for kind in EXPORT_KINDS if kind in kinds]


def _reasons(record, options):
    """(every flag of a record, the flags that leave it out under options).

    The most serious first, in FLAGS order (private, sensitive, the kinds
    of picture, other writing, an older description, not described): a
    picture is counted under the first flag that leaves it out, and the
    review step groups it under its first flag. A description by the
    older version 4 prompt says nothing about private or sensitive content
    or the kind of picture, so it is "unchecked", left out like a picture
    not described yet.
    """
    flags = []
    active = []

    def flag(name, leaves_out):
        flags.append(name)
        if leaves_out:
            active.append(name)

    document_type = record.get("dt")
    if record.get("pr") or document_type in PRIVATE_DOCUMENT_TYPES:
        flag("private", options["exclude_private"])
    if record.get("se"):
        flag("sensitive", options["exclude_sensitive"])
    for kind in picture_kinds(record):
        if kind in REVIEW_TYPES or kind in options["exclude_types"]:
            flag(kind, kind in options["exclude_types"])
    if (document_type and document_type not in DOCUMENT_KINDS
            and document_type not in PRIVATE_DOCUMENT_TYPES
            and not any(name in EXPORT_KINDS for name in flags)):
        flag(WRITING_FLAG, False)
    if not record.get("ds"):
        flag("undescribed", options["exclude_undescribed"])
    elif record.get("v4"):
        flag(UNCHECKED_FLAG, options["exclude_undescribed"])
    return flags, active


def select_records(conn, config, library, options):
    """Pick the records of a library's export.

    Returns (records to export, every record {id: record}, flagged list,
    counts) where records are search records (search_index, export
    variant) with database ids still, and flagged lists every picture the
    review step should show: {id, reason, reasons, title, excluded, and
    new_flags when the owner chose to publish it before it got these
    flags, which leave it out again}.
    """
    library_id = library["id"]
    records = search_index.build_records(conn, config, [library_id], for_export=True,
                                         export_options=options)
    album = album_choice(conn, library_id, options)
    if album is not None:
        records = [record for record in records if record["i"] in album["ids"]]
    include = include_choices(options)
    exclude = set(options["exclude_ids"])
    chosen, excluded, flagged = [], {}, []
    for record in records:
        image_id = record["i"]
        flags, active = _reasons(record, options)
        new_flags = None
        if image_id in exclude:
            reason = "excluded"
        elif not active:
            reason = None
        elif image_id in include and (include[image_id] is None
                                      or include[image_id].issuperset(active)):
            reason = None
        else:
            reason = active[0]
            if image_id in include:
                new_flags = [name for name in active if name not in include[image_id]]
        if reason is None:
            chosen.append(record)
        else:
            excluded[image_id] = reason
        if flags or image_id in exclude or image_id in include:
            item = {"id": image_id, "reason": (flags or [reason or "included"])[0],
                    "reasons": flags, "title": record.get("t") or "",
                    "excluded": reason is not None}
            if new_flags:
                item["new_flags"] = new_flags
            flagged.append(item)
    by_reason = collections.Counter(excluded.values())
    hidden = _hidden_count(conn, library_id, album["ids"] if album is not None else None)
    if hidden:
        by_reason["hidden"] = hidden
    counts = {"total": len(records) + hidden, "exported": len(chosen),
              "by_reason": dict(sorted(by_reason.items()))}
    if album is not None:
        # (Only the album's pictures were counted; the rest of the library is not mentioned.)
        counts["album"] = {"id": album["id"], "name": album["name"],
                           "pictures": len(album["ids"]), "elsewhere": album["elsewhere"]}
    return chosen, {record["i"]: record for record in records}, flagged, counts


def _thumb_sizes(ids):
    """{image id: thumbnail size in bytes} from thumbs.db."""
    sizes = {}
    path = common.thumbs_db_path()
    if not ids or not os.path.isfile(path):
        return sizes
    conn = db.connect(path)
    try:
        ids = list(ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in conn.execute("SELECT image_id, length(jpeg) FROM thumbs WHERE image_id "
                                    "IN (%s)" % ",".join("?" * len(chunk)), chunk):
                sizes[row[0]] = row[1] or 0
    finally:
        conn.close()
    return sizes


def _file_size(path):
    try:
        return os.stat(path).st_size
    except (OSError, ValueError):
        return None


def folder_reachable(library, helper=None):
    """True when a library's pictures can be read: its folder is there (or a helper loads them)."""
    if helper is not None and getattr(helper, "load_image", None) is not None:
        return True
    root = common.library_root(library)
    return bool(root) and os.path.isdir(root)


class _Sizes:
    """The sizes of the files an export writes for each picture (estimated when not known)."""

    def __init__(self, library_id, options, ids):
        self.library_id = library_id
        self.options = options
        self.previews = cache.PreviewStore()
        self.thumbs = _thumb_sizes(ids)
        self.images = None

    def stored_preview(self, row):
        """The size of a row's stored preview, or None when it has none."""
        if row["has_preview"] != 1:
            return None
        try:
            return _file_size(self.previews.path_for(self.library_id, row["id"]))
        except ValueError:
            return None

    def needs_original(self, row):
        """True when the picture's files cannot be made without reading its original."""
        mode = self.options["images"]
        if row["id"] not in self.thumbs:
            return True
        if video_plan(row, self.options, self.library_id)[:2] == ("video", None):
            return True             # the original video is copied
        if (mode == "original" and row["projection"] != imageinfo.PROJECTION_DUAL_FISHEYE
                and not _is_video(row)):
            return True
        return mode != "none" and self.stored_preview(row) is None

    def parts(self, record, row):
        """{thumbs, images, panos, data} bytes of one picture."""
        parts = {"thumbs": self.thumbs.get(row["id"], 0), "images": 0, "panos": 0,
                 "data": ESTIMATE_RECORD_BYTES}
        plan = video_plan(row, self.options, self.library_id)
        if plan[0] == "video":
            parts["videos"] = plan[2]
        mode = self.options["images"]
        if mode == "none":
            return parts
        if (mode == "original" and row["projection"] != imageinfo.PROJECTION_DUAL_FISHEYE
                and not _is_video(row)):
            parts["images"] = row["size"] or 0
        else:
            size = self.stored_preview(row)
            parts["images"] = size if size is not None else min(row["size"] or 0,
                                                                ESTIMATE_WEB_BYTES)
        if record.get("p") == "360" and not _is_video(row):
            if self.images is None:
                self.images = _images()
            if self.images.has_pano(row["width"], row["height"]):
                try:
                    size = _file_size(self.previews.pano_path_for(self.library_id, row["id"]))
                except ValueError:
                    size = None
                parts["panos"] = size if size is not None else ESTIMATE_PANO_BYTES
        return parts


def vector_bytes(dims):
    """About how many bytes one picture's meaning vector adds to an export's vector files."""
    return 4 * ((dims + 2) // 3) + XID_LENGTH + 3


def _look_sizes(conn, library_id, options, ids):
    """{image id: bytes its look vector adds to the export} for the pictures of ids
    that have one ({} when the looks option is off)."""
    if not options.get("looks"):
        return {}
    found = looks.fresh_looks(conn, library_id, ids)
    size = vector_bytes(found["dims"]) if found["dims"] else 0
    return {image_id: size for image_id in found["vectors"]}


def _carried(pairs):
    """{image id: the drive words (dvt, dvd, dvg) its record carries} of the pictures
    ((image id, record) pairs) whose records carry some (embeddings.fresh_vectors)."""
    carried = {}
    for image_id, record in pairs:
        fields = {key: record[key] for key in ("dvt", "dvd", "dvg") if key in record}
        if fields:
            carried[image_id] = fields
    return carried


def _vector_sizes(conn, library_id, options, ids, carried=None):
    """{image id: bytes its up-to-date meaning vector adds to the export} for the
    pictures of ids that have one ({} when the similar option is off). carried: see
    embeddings.fresh_vectors."""
    if not options.get("similar"):
        return {}
    found = embeddings.fresh_vectors(conn, library_id, ids, carried=carried)
    size = vector_bytes(found["dims"]) if found["dims"] else 0
    return {image_id: size for image_id in found["vectors"]}


def meaning_status(config, options, vectors=None):
    """Whether an export with these options can ship search by meaning in the browser
    (the "meaning" option; the export review shows the reason when not).

    vectors is how many of the pictures to publish have an up-to-date meaning
    vector (None: not counted). It can when the similar option is on and
    vectors go, search by meaning is set up with a model file that can run in
    a browser (browser_model.inspect, with its Dense head if it has one), a
    readable license file, and vectors.db holds vectors made with that model
    (its recipe) and a probe. Returns {"available", "reason" ('' when
    available), "model_file", "dense_file", "license_file", "dims", "query_prefix",
    "credit", "probe"}.
    """
    result = {"available": False, "reason": "", "model_file": "", "dense_file": "",
              "license_file": "", "dims": 0, "query_prefix": "", "credit": "", "probe": []}
    later = "Update meaning search on the Manage page first"
    if not options.get("similar"):
        result["reason"] = ("It needs More like this by meaning (the similar option): it "
                            "searches the same vectors.")
        return result
    if vectors == 0:
        result["reason"] = ("No picture to publish has an up-to-date meaning vector: %s."
                            % later)
        return result
    settings = embeddings.settings(config)
    endpoint, problem = embeddings.endpoint_for(config, settings)
    if endpoint is None:
        result["reason"] = problem
        return result
    model_path, problem = embeddings.model_source(config, settings)
    if problem:
        result["reason"] = problem
        return result
    if not model_path:
        result["reason"] = ("Set embedding.model_file in config.json to the GGUF file the "
                            "embedding server runs (see SETTINGS.md).")
        return result
    dense_path = embeddings.file_setting(settings["dense_file"])
    current = embeddings.read_meta()
    dims = embeddings.vector_size(current) if current else settings["dims"]
    checked = browser_model.inspect(model_path, dense_path or None,
                                    settings["dims"])
    if not checked["supported"]:
        result["reason"] = checked["why"]
        return result
    license_path = embeddings.file_setting(settings["license_file"])
    if not license_path:
        result["reason"] = ("Set embedding.license_file in config.json to the model's license "
                            "text: it is published with the model.")
        return result
    try:
        size = os.path.getsize(license_path)
    except OSError:
        size = -1
    if not 0 < size <= MAX_LICENSE_BYTES:
        result["reason"] = ("The license file %s (embedding.license_file) cannot be read%s."
                            % (license_path, " or is empty" if size == 0 else
                               " (larger than 1 MB)" if size > 0 else ""))
        return result
    try:
        made_with = embeddings.recipe(config, settings)
    except browser_model.ModelError as error:
        result["reason"] = str(error)
        return result
    if (current is None or current["model"] != embeddings.model_key(endpoint, settings)
            or current["dims"] != settings["dims"] or current["changed"]
            or current["recipe"] != made_with):
        result["reason"] = ("The meaning vectors were made with another model or other "
                            "settings: %s." % later)
        return result
    if not current["probe"]:
        result["reason"] = "%s (it checks the model)." % later
        return result
    result.update(available=True, model_file=model_path, dense_file=dense_path,
                  license_file=license_path, dims=dims, query_prefix=settings["query_prefix"],
                  credit=settings["credit"], probe=current["probe"])
    return result


_meaning_sizes = {}
_meaning_lock = threading.Lock()


def _meaning_key(status):
    return json.dumps([browser_model.file_sha256(status["model_file"]),
                       browser_model.file_sha256(status["dense_file"])
                       if status["dense_file"] else "",
                       status["dims"], status["query_prefix"], status["probe"],
                       browser_model.FORMAT, browser_model.PART_BYTES], sort_keys=True)


def _static_bytes(name):
    with open(os.path.join(_static_dir(), name), "rb") as handle:
        return handle.read()


def meaning_files(status):
    """The files of search by meaning in the browser for an available meaning_status():
    {"files": {path in the export: bytes}, "assets": {asset name: bytes}, "id", "dims",
    "parts", "bytes", "gzip" (all of them, as GitHub Pages and most web servers send them:
    the size visitors download)}. Raises browser_model.ModelError."""
    converted = browser_model.convert(status["model_file"], status["dense_file"] or None,
                                      status["dims"], status["query_prefix"], status["probe"])
    with open(status["license_file"], "rb") as handle:
        license_text = handle.read()
    files = {"data/" + rel: text.encode("ascii") for rel, text in converted["files"].items()}
    files["data/" + MEANING_LICENSE] = license_text
    assets = {name: _static_bytes(name) for name in MEANING_ASSETS}
    extra = [license_text] + list(assets.values())
    result = {"files": files, "assets": assets, "id": converted["id"],
              "dims": converted["dims"], "parts": converted["parts"],
              "bytes": converted["bytes"] + sum(len(data) for data in extra),
              "gzip": converted["gzip"] + sum(len(_gzip_bytes(data)) for data in extra)}
    with _meaning_lock:
        _meaning_sizes.clear()
        _meaning_sizes[_meaning_key(status)] = {key: result[key] for key in (
            "id", "dims", "parts", "bytes", "gzip")}
    return result


def _gzip_bytes(data):
    """data compressed as web servers send it."""
    import gzip as gzip_module
    return gzip_module.compress(data, browser_model.GZIP_LEVEL, mtime=0)


def meaning_size(status):
    """{"id", "dims", "parts", "bytes", "gzip"} of meaning_files(status), remembered (the
    first time converts the model: a second or two). None when not available."""
    if not status.get("available"):
        return None
    key = _meaning_key(status)
    with _meaning_lock:
        found = _meaning_sizes.get(key)
    if found is None:
        try:
            meaning_files(status)
        except (browser_model.ModelError, OSError):
            return None
        with _meaning_lock:
            found = _meaning_sizes.get(key)
    return dict(found) if found else None


def meaning_text(info):
    """The review step's and the log's line about search by meaning in the browser
    (info: {"bytes", "gzip"} of meaning_size)."""
    return ("Search by meaning in the browser: %s of model files; visitors download about %s "
            "once, the first time they search (works offline and from disk)."
            % (human_bytes(info["bytes"]), human_bytes(info["gzip"])))


def _asset_bytes(options):
    return sum(_file_size(os.path.join(_static_dir(), name)) or 0
               for name in ASSETS + (MAP_ASSETS if options["location"] else ()))


def preview_export(conn, config, library_id, options=None, reachable=None):
    """What an export of a library would hold, for the review step before exporting.

    options are checked like export_library's (default: the library's
    saved options). Returns {"counts": {"total", "exported", "by_reason":
    {reason: count}, "unavailable": how many of the pictures to export
    need the library's folder (originals, pictures without a stored
    preview) while it cannot be reached, "needs_folder": how many need it
    at all, "similar": with the similar option, {"vectors": how many of
    the pictures to export have an up-to-date meaning vector, "stale":
    an out-of-date one, "missing": none} (of the pictures to export),
    "meaning": search by meaning in the browser {"available", "reason",
    "bytes", "gzip" (when available)}, see meaning_status, "looks": with
    the looks option, {"vectors": how many of the pictures to export have
    a look vector, "missing": how many do not}, "drives": the drives among the
    pictures to publish, see drives.export_texts: {"drives", "described" (those
    that keep their own description), "private", "sensitive" (those whose
    description the options leave out)}},
    "flagged": [{id, reason, reasons, title, excluded, bytes
    (what the picture adds to the export), parts (the same by kind, as in
    size_parts), needs_folder, new_flags (see select_records), and for a
    video, video: how it would be published, as counted in counts.videos
    ("included", "not_playable" or "too_large", see video_plan; "poster"
    when videos are not included)}],
    "size_estimate": bytes, "size_parts": {thumbs, images, panos, data, videos,
    vectors, meaning, looks}, "options": the options used}. Reasons: hidden,
    excluded (by id), undescribed, unchecked, a picture type (screenshot,
    document, ...), private, sensitive. reachable says whether the
    library's folder can be read (None: look now; it may take long for a
    share that does not answer).
    """
    library = common.get_library(config, library_id)
    if library is None or not library.get("id"):
        raise ExportError("There is no library %r in the settings." % (library_id,))
    library_id = library["id"]
    options = library_options(library, options)
    chosen, by_id, flagged, counts = select_records(conn, config, library, options)
    rows = _load_rows(conn, library_id)
    flagged_ids = {item["id"] for item in flagged}
    sizes = _Sizes(library_id, options, sorted({record["i"] for record in chosen}
                                               | flagged_ids))
    # The drives as the export would publish them (on copies of the records).
    published = [dict(record) for record in chosen]
    counts["drives"] = drives.export_texts(published, options,
                                           drives.settings(config)["min_clips"],
                                           drives.clip_texts(conn, library_id, config),
                                           drives.clip_places(conn, library_id, config))
    vectors = _vector_sizes(conn, library_id, options,
                            {record["i"] for record in chosen} | flagged_ids,
                            carried=_carried((record["i"], record) for record in published))
    look_sizes = _look_sizes(conn, library_id, options,
                             {record["i"] for record in chosen} | flagged_ids)
    parts = {"thumbs": 0, "images": 0, "panos": 0, "data": 0, "videos": 0,
             "vectors": 0, "meaning": 0, "looks": 0}
    looked = {"vectors": 0, "missing": 0} if options["looks"] else None
    needs_folder = 0
    video_counts = {"total": 0, "included": 0, "not_playable": 0, "too_large": 0}
    similar = {"vectors": 0, "stale": 0, "missing": 0} if options["similar"] else None
    for record in chosen:
        row = rows.get(record["i"])
        if row is None:
            continue
        for key, value in sizes.parts(record, row).items():
            parts[key] += value
        if record["i"] in vectors:
            parts["vectors"] += vectors[record["i"]]
        if similar is not None:
            similar["vectors" if record["i"] in vectors else "missing"] += 1
        if record["i"] in look_sizes:
            parts["looks"] += look_sizes[record["i"]]
        if looked is not None:
            looked["vectors" if record["i"] in look_sizes else "missing"] += 1
        if sizes.needs_original(row):
            needs_folder += 1
        if _is_video(row):
            video_counts["total"] += 1
            kind, _source, _size, reason = video_plan(row, options, library_id)
            if kind == "video":
                video_counts["included"] += 1
            elif reason:
                video_counts[reason] += 1
    if similar is not None and similar["missing"]:
        # (Of those without a usable vector, how many have an older one.)
        stale_ids = _stale_ids(conn, library_id, {record["i"] for record in chosen}, vectors)
        similar["stale"] = len(stale_ids)
        similar["missing"] -= similar["stale"]
    for item in flagged:
        row, record = rows.get(item["id"]), by_id.get(item["id"])
        if row is None or record is None:
            item.update(bytes=0, parts={}, needs_folder=False)
            continue
        item["parts"] = sizes.parts(record, row)
        if item["id"] in vectors:
            item["parts"]["vectors"] = vectors[item["id"]]
        if item["id"] in look_sizes:
            item["parts"]["looks"] = look_sizes[item["id"]]
        item["bytes"] = sum(item["parts"].values())
        item["needs_folder"] = sizes.needs_original(row)
        if _is_video(row):
            kind, _source, _size, reason = video_plan(row, options, library_id)
            item["video"] = "included" if kind == "video" else reason or "poster"
    if reachable is None:
        reachable = folder_reachable(library)
    counts["needs_folder"] = needs_folder
    counts["unavailable"] = 0 if reachable else needs_folder
    # Videos among the pictures to publish: how many go out as videos, and
    # how many keep their poster only (and why).
    counts["videos"] = video_counts
    if similar is not None:
        counts["similar"] = similar
    if looked is not None:
        counts["looks"] = looked
    status = meaning_status(config, options, similar["vectors"] if similar else 0)
    counts["meaning"] = {"available": status["available"], "reason": status["reason"]}
    # (The size takes converting the model once: only when the option is on.)
    size = meaning_size(status) if options["meaning"] else None
    if size is None and status["available"] and options["meaning"]:
        counts["meaning"].update(available=False, reason="The model file could not be "
                                                         "converted for a browser.")
    elif size is not None:
        counts["meaning"].update(bytes=size["bytes"], gzip=size["gzip"])
        if options["meaning"]:
            parts["meaning"] = size["bytes"]
    parts["data"] += _asset_bytes(options)
    return {"counts": counts, "flagged": flagged,
            "size_estimate": sum(parts.values()), "size_parts": parts, "options": options}


def _stale_ids(conn, library_id, ids, fresh):
    """Of ids, those without a fresh vector (fresh: {id: ...}) that have an older one."""
    store = embeddings.connect(create=False)
    if store is None:
        return set()
    try:
        have = embeddings.stored(store, [library_id])
    finally:
        store.close()
    return {image_id for image_id in ids if image_id not in fresh and image_id in have}


# How the log names what an export left out: reason -> (one, many).
REASON_WORDS = {
    "hidden": ("hidden picture", "hidden pictures"),
    "excluded": ("picture left out by choice", "pictures left out by choice"),
    "undescribed": ("picture not described yet", "pictures not described yet"),
    UNCHECKED_FLAG: ("picture described by an older version (not checked)",
                     "pictures described by an older version (not checked)"),
    "private": ("picture with private information", "pictures with private information"),
    "sensitive": ("sensitive picture", "sensitive pictures"),
    "failed": ("picture that could not be read", "pictures that could not be read"),
    "other": ("other picture", "other pictures"),
}


def left_out_text(by_reason):
    """{reason: count} -> "1,204 pictures not described yet, 3 screenshots" (most first)."""
    parts = []
    for reason, count in sorted(by_reason.items(), key=lambda item: (-item[1], item[0])):
        if count <= 0:
            continue
        one, many = REASON_WORDS.get(reason, (reason, reason + "s"))
        parts.append("{:,} {}".format(count, one if count == 1 else many))
    return ", ".join(parts)


def detail_entry(row, record):
    """The details of one picture an export adds to its record (or None).

    The date taken (the day only) and how it was found (the camera or the
    file name; a date from the file's time is never exported), and the
    camera settings (exposure, aperture, ISO, focal length, lens model):
    no times, places, serial numbers, software, owner or copyright.
    """
    entry = {}
    if record.get("dk"):
        entry["taken_at"] = record["dk"]
        if row["taken_source"] in TAKEN_SOURCES:
            entry["taken_source"] = row["taken_source"]
    exif = None
    try:
        exif = json.loads(row["exif"] or "{}", parse_constant=lambda _name: None)
    except (TypeError, ValueError):
        exif = None
    details = {}
    if isinstance(exif, dict):
        for key in DETAIL_EXIF_NUMBERS:
            value = exif.get(key)
            if isinstance(value, list) and value:
                value = value[0]
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)):
                details[key] = value
        for key in DETAIL_EXIF_TEXT:
            value = exif.get(key)
            if isinstance(value, str) and value.strip():
                details[key] = " ".join(value.split())[:100]
    if details:
        entry["exif"] = details
    # Only when it adds something: the camera settings, or a note that the
    # date comes from the file name (the viewer says so).
    if details or entry.get("taken_source") == "filename":
        return entry
    return None


def _location(row):
    """(lat, lon) of a row, rounded, or None: the same rule as the server's map."""
    lat, lon = row["lat"], row["lon"]
    if not world_map.valid_position(lat, lon):
        return None
    return round(lat, LOCATION_DECIMALS), round(lon, LOCATION_DECIMALS)


# ---------------------------------------------------------------------------
# Writing files

def _js_json(value):
    """JSON text for a data script: ASCII only (every other character escaped)."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _write_replacing(path, data):
    """Write bytes so readers see the old file or the new one, never a partial file.

    Not flushed to the disk first: the next export checks what it wrote.
    """
    common.write_file_atomic(path, data, fsync=False)


class Writer:
    """Writes the files of an export: only when their bytes change. Thread safe.

    previous is the state of the last export of the folder: {relative
    path: [source signature, size, mtime_ns]}. A file written from a
    source with the same signature, still the same size and time on disk,
    is not even read again (current()). Every file of this export is
    listed in files (the next state); stale() lists the earlier ones that
    were not.
    """

    def __init__(self, out_dir, previous=None):
        self.out_dir = out_dir
        self.previous = dict(previous or {})
        self.files = {}
        self.written = 0
        self.unchanged = 0
        self._lock = threading.Lock()

    def path(self, rel):
        return os.path.join(self.out_dir, *rel.split("/"))

    def _record(self, rel, sig, stat, wrote):
        with self._lock:
            self.files[rel] = [sig or "", stat.st_size, stat.st_mtime_ns]
            if wrote:
                self.written += 1
            else:
                self.unchanged += 1

    def current(self, rel, sig):
        """True (and the file is kept) if rel was written from the same source and is unchanged."""
        if not sig:
            return False
        before = self.previous.get(rel)
        if not before or len(before) != 3 or before[0] != sig:
            return False
        try:
            stat = os.stat(self.path(rel))
        except OSError:
            return False
        if stat.st_size != before[1] or stat.st_mtime_ns != before[2]:
            return False
        self._record(rel, sig, stat, False)
        return True

    def write(self, rel, data, sig=None):
        """Write data to rel unless the file already holds exactly that; True if written.

        Raises ExportWriteError when the file cannot be written (or read):
        the export cannot go on.
        """
        path = self.path(rel)
        try:
            try:
                stat = os.stat(path)
                if stat.st_size == len(data):
                    with open(path, "rb") as handle:
                        if handle.read() == data:
                            self._record(rel, sig, stat, False)
                            return False
            except FileNotFoundError:
                pass
            common.ensure_dir(os.path.dirname(path))
            _write_replacing(path, data)
            stat = os.stat(path)
        except OSError as error:
            raise ExportWriteError(os_error_text(error, path))
        self._record(rel, sig, stat, True)
        return True

    def forget(self, rel):
        """Take back a file this export wrote or kept: it is not part of the export after all.

        A file no earlier export made is removed now; one of the earlier
        export's is removed with the other stale files (see stale()).
        """
        with self._lock:
            entry = self.files.pop(rel, None)
        if entry is not None and rel not in self.previous:
            try:
                os.remove(self.path(rel))
            except OSError:
                pass

    def intact(self, rel):
        """The stat of a file of the earlier export that is still there unchanged, or None."""
        before = self.previous.get(rel)
        if not before or len(before) != 3:
            return None
        try:
            stat = os.stat(self.path(rel))
        except OSError:
            return None
        if stat.st_size != before[1] or stat.st_mtime_ns != before[2]:
            return None
        return stat

    def write_file(self, rel, make, sig):
        """Make rel with make(path) unless current(rel, sig); True if it was made.

        make writes the whole file at path, replacing it in one step (for
        files too large to hold in memory, such as videos). An OSError
        while making it stops the export (ExportWriteError); other errors
        are the source's and are raised as they are.
        """
        if self.current(rel, sig):
            return False
        path = self.path(rel)
        try:
            common.ensure_dir(os.path.dirname(path))
        except OSError as error:
            raise ExportWriteError(os_error_text(error, path))
        try:
            make(path)
            stat = os.stat(path)
        except OSError as error:
            raise ExportWriteError(os_error_text(error, path))
        self._record(rel, sig, stat, True)
        return True

    def keep(self, rel):
        """Keep a file of the earlier export as it is (True), if it is still there unchanged."""
        stat = self.intact(rel)
        if stat is None:
            return False
        self._record(rel, self.previous[rel][0], stat, False)
        return True

    def stale(self):
        """Files the earlier export wrote that this one did not."""
        return sorted(rel for rel in self.previous if rel not in self.files)


def _safe_rel(rel):
    """True for a relative path (with / separators) that stays inside the export."""
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\\" in rel:
        return False
    parts = rel.split("/")
    return all(part not in ("", ".", "..") and ":" not in part for part in parts)


def _guess_own_files(out_dir):
    """The files an export probably made in out_dir, when its state is gone.

    Only names this program uses, in the folders it manages (and, for a
    folder made by the "package" command of older versions, that layout).
    """
    found = []
    patterns = (
        re.compile(r"^(index|detail|map)\.html$|^robots\.txt$"),
        re.compile(r"^(style\.css|site\.js|shell\.js|search-core\.js|viewer\.js|app\.js|"
                   r"detail\.js|map\.js)$"),
        re.compile(r"^assets/[a-z-]+\.(js|css)$"),
        re.compile(r"^data/(manifest\.js|manifest\.json|locations\.js|locations\.json|"
                   r"(index|detail|vectors|looks)-\d+\.js|data-\d+\.json)$"),
        re.compile(r"^data/meaning/(model\.js|w-\d+\.js|LICENSE\.txt)$"),
        re.compile(r"^(thumbs|panos)/[a-z2-7]{%d}\.jpg$" % XID_LENGTH),
        re.compile(r"^videos/[a-z2-7]{%d}\.mp4$" % XID_LENGTH),
        re.compile(r"^images/([a-z2-7]{%d}|\d+)\.[a-z0-9]{2,5}$" % XID_LENGTH),
        re.compile(r"^tiles/\d+/\d+/\d+\.jpg$"),
    )
    for folder, _dirs, files in os.walk(out_dir):
        rel_folder = os.path.relpath(folder, out_dir).replace(os.sep, "/")
        if rel_folder != "." and rel_folder.split("/")[0] not in MANAGED_FOLDERS:
            continue
        for name in files:
            rel = name if rel_folder == "." else rel_folder + "/" + name
            if any(pattern.match(rel) for pattern in patterns):
                found.append(rel)
    return found


def _remove_empty_folders(out_dir):
    for name in MANAGED_FOLDERS:
        top = os.path.join(out_dir, name)
        if not os.path.isdir(top):
            continue
        for folder, _dirs, _files in sorted(os.walk(top), key=lambda item: -len(item[0])):
            try:
                if not os.listdir(folder):
                    os.rmdir(folder)
            except OSError:
                pass


def _remove_temp_files(out_dir):
    """Remove half-written files an interrupted export left in the folders it manages."""
    for name in MANAGED_FOLDERS + (".",):
        top = os.path.join(out_dir, name)
        if not os.path.isdir(top):
            continue
        walker = os.walk(top) if name != "." else [(top, [], os.listdir(top))]
        for folder, _dirs, files in walker:
            for file_name in files:
                if _TMP_RE.search(file_name):
                    try:
                        os.remove(os.path.join(folder, file_name))
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Pages

def _static_dir():
    return os.path.join(common.BASE_DIR, "static")


def _version(data):
    """A short hash of a file's contents, for its address (see the module notes)."""
    return hashlib.sha1(data).hexdigest()[:10]


def render_page(template, title, page_title, capabilities, export_id, options,
                tiles_max=None, versions=None, data_version=None, meaning=None):
    """One page of the export: the template in static mode, as text.

    The page settings (JSON block) name this export's one library, its
    capabilities and title, and data_version (the hash of data/manifest.js);
    the page asks search engines not to index it (or its pictures) unless
    options allow it, and shows the credits line at its foot -- with the
    GeoNames credit when the export holds place names (capabilities
    "places"; the map page, which has no foot, shows only that credit, in
    a corner of the map). versions ({asset name: hash}) go into the addresses of the
    scripts and the style sheet. meaning ({"credit"}, when the export ships the
    query model of search by meaning) adds "Search by meaning: <credit> ·
    license" to the foot of the search and detail pages.
    """
    import serve
    location = options["location"]
    extra = {"title": title}
    if data_version:
        extra["dataVersion"] = data_version
    text = serve.render_template_file(
        template, "static", "assets/", "data/", tiles_max=tiles_max if location else None,
        tile_prefix="tiles/" if location and tiles_max is not None else None,
        libraries={export_id: title}, capabilities=capabilities, extra=extra)
    if versions:
        text = re.sub(r'((?:src|href)="assets/)([A-Za-z0-9_.-]+)"',
                      lambda match: '%s%s%s"' % (
                          match.group(1), match.group(2),
                          "?v=" + versions[match.group(2)] if match.group(2) in versions
                          else ""), text)
    text, count = re.subn(r"<title>[^<]*</title>",
                          lambda _match: "<title>%s</title>" % html.escape(page_title),
                          text, count=1)
    if count != 1:
        raise ExportError("The page template %s has no <title>." % template)
    if not options["allow_indexing"]:
        anchor = '<meta charset="utf-8">'
        if anchor not in text:
            raise ExportError("The page template %s has no charset line." % template)
        text = text.replace(anchor, anchor + '\n<meta name="robots" content="noindex, '
                                              'noimageindex">', 1)
    credits = [html.escape(options["credits"])] if options["credits"] else []
    if capabilities.get("places"):
        credits.append(PLACES_CREDIT)
    if meaning is not None:
        # (Not on the map page, which has no foot and does not use the model.)
        credits.append('Search by meaning: %s<a href="data/%s" rel="license">license</a>' % (
            html.escape(meaning["credit"]) + " &middot; " if meaning.get("credit") else "",
            MEANING_LICENSE))
    if credits and template != "map.html":
        anchor = '<script type="application/json" id="app-config">'
        if anchor not in text:
            raise ExportError("The page template %s has no page settings." % template)
        footer = '<footer class="export-credits">%s</footer>\n' % "<br>".join(credits)
        text = text.replace(anchor, footer + anchor, 1)
    elif template == "map.html" and capabilities.get("places"):
        anchor = '<div id="map-message"'
        if anchor not in text:
            raise ExportError("The page template %s has no map message." % template)
        text = text.replace(anchor, '<p id="map-credit" class="export-credits map-credit">'
                                    '%s</p>\n%s' % (PLACES_CREDIT, anchor), 1)
    return text


# ---------------------------------------------------------------------------
# The export

class LogContext(jobs.JobContext):
    """A JobContext that passes each log line to a function (print by default)."""

    def __init__(self, log=print, name="export"):
        super().__init__(name)
        self._log_function = log

    def _emit(self, entry):
        level = entry.get("level")
        prefix = "Warning: " if level == "warn" else "Error: " if level == "error" else ""
        self._log_function(prefix + entry["message"])


class _Picture:
    """One picture to export: its record, row and id in the export."""

    __slots__ = ("record", "row", "xid", "ok", "ext", "pano", "video", "error", "fatal")

    def __init__(self, record, row, xid):
        self.record = record
        self.row = row
        self.xid = xid
        self.ok = False
        self.ext = "jpg"
        self.pano = False
        self.video = False      # videos/<xid>.mp4 was written (or kept)
        self.error = ""
        self.fatal = None       # an ExportWriteError: the export stops

    def files(self):
        """Every file of the export this picture may have."""
        return (["thumbs/%s.jpg" % self.xid, "panos/%s.jpg" % self.xid,
                 "videos/%s.mp4" % self.xid]
                + ["images/%s.%s" % (self.xid, ext) for ext in ORIGINAL_EXTENSIONS])


class _Export:
    """The work of export_library() (see there)."""

    def __init__(self, ctx, conn, config, library, out_dir, options, helper):
        self.ctx = ctx
        self.conn = conn
        self.config = config
        self.library = library
        self.library_id = library["id"]
        self.out_dir = out_dir
        self.options = options
        self.title = options["title"] or common.library_label(library)
        self.helper = common.thread_safe_helper(helper)
        self.root = common.library_root(library)
        self.previews = cache.PreviewStore()
        self.thumbs = None
        self.writer = None
        self.state = None
        self.export_id = None
        self.secret = None
        self.failed = 0
        self.pano_failed = 0
        self.video_failed = 0
        self.video_tools = None
        self.kept = 0
        self.logged = 0
        self.count = None       # pictures published, once the export has finished
        self.marked = False     # the folder has been written to (see mark)
        # Meaning vectors shipped (the similar option, see vector_files).
        self.vector_stats = {"vectors": 0, "stale": 0, "missing": 0, "bytes": 0}
        self.vector_parts = []      # [(name, payload)] of the vector files
        # Look vectors shipped (the looks option, see look_files).
        self.look_stats = {"vectors": 0, "missing": 0, "bytes": 0}
        # Search by meaning in the browser (the meaning option, see meaning).
        self.meaning = None
        self.meaning_status = None
        # The albums the manifest lists (the marks option, see album_numbers).
        self.published_albums = []

    # -- set up ---------------------------------------------------------------

    def prepare(self):
        """Check the folder, and settle the export id, secret and earlier files (nothing is written)."""
        check_output_dir(self.out_dir, self.config, self.library_id)
        check_writable(self.out_dir)
        if self.options["zip"]:
            check_zip_path(zip_path_for(self.out_dir), self.config)
        marker = read_marker(self.out_dir) if os.path.isdir(self.out_dir) else None
        state = None
        guessed = None
        if marker and marker.get("export_id"):
            self.export_id = marker["export_id"]
            state = load_state(self.export_id)
            if state is None:
                # The folder is an export, but what it wrote is not known
                # here: count the files of this program's layout as its own.
                guessed = _guess_own_files(self.out_dir)
                self.ctx.log("No record of the earlier export into %s was found; the files "
                             "it made are recognised by their names." % self.out_dir, "warn")
        elif (marker and marker.get("legacy")) or (
                marker is None and os.path.isdir(self.out_dir)
                and _old_package_site(self.out_dir)):
            guessed = _guess_own_files(self.out_dir)
            self.ctx.log("%s holds a site made by an older version; its files are replaced."
                         % self.out_dir)
        else:
            # A new or empty folder: keep the ids of an earlier export of
            # this library into the same folder, if it was emptied since.
            state = _state_for_folder(self.out_dir, self.library_id)
            if state is not None:
                self.export_id = state["export_id"]
        if state is None:
            self.export_id = self.export_id or secrets.token_hex(8)
            state = {"format": STATE_FORMAT, "export_id": self.export_id,
                     "secret": secrets.token_hex(32), "files": {}}
            if guessed:
                state["files"] = {rel: ["", -1, -1] for rel in guessed}
        if state.get("library_id") not in (None, self.library_id):
            # (check_output_dir refuses the export of a library that is
            # still in the settings.)
            self.ctx.log("%s held an export of a library that is no longer in the settings; "
                         "it is replaced." % self.out_dir, "warn")
            state.pop("count", None)
        state["library_id"] = self.library_id
        state["out_dir"] = self.out_dir
        self.state = state
        self.secret = state["secret"]
        self.writer = Writer(self.out_dir, {
            rel: entry for rel, entry in state["files"].items()
            if _safe_rel(rel) and isinstance(entry, list)})

    def mark(self):
        """Make the folder and mark it: the first write of an export."""
        try:
            common.ensure_dir(self.out_dir)
            _remove_temp_files(self.out_dir)
        except OSError as error:
            raise ExportWriteError(os_error_text(error, self.out_dir))
        # Marked first, so an interrupted export can simply be run again.
        self.writer.write(MARKER, marker_bytes(self.export_id))
        self.marked = True

    def save_state(self, finished):
        """Save what the folder holds now (on a stop or failure: also every earlier file).

        A finished export also records how many pictures it published
        (last_export); a stopped one leaves the earlier count, as the site's
        data files are still the earlier export's.
        """
        files = dict(self.writer.files)
        if not finished:
            for rel, entry in self.writer.previous.items():
                if rel not in files and os.path.isfile(self.writer.path(rel)):
                    files[rel] = entry
        files.pop(MARKER, None)
        self.state["files"] = files
        self.state["updated"] = time.time()
        self.state["finished"] = bool(finished)
        if finished and self.count is not None:
            self.state["count"] = self.count
        try:
            save_state(self.state)
        except OSError as error:
            raise ExportError("The record of this export could not be saved in the data "
                              "folder: %s" % os_error_text(error, state_dir()))

    # -- pictures -------------------------------------------------------------

    def original_path(self, row):
        if not self.root:
            return None
        return os.path.join(self.root, *row["path"].split("/"))

    def tools(self):
        """ffmpeg and ffprobe (videos.Tools), or None."""
        if self.video_tools is None:
            self.video_tools = videos.find_tools(self.config) or False
        return self.video_tools or None

    def read_original(self, row, **kwargs):
        """images.read_image of a row's original (ImageReadError when it cannot be read).

        For a video, videos.read_video (its poster is the preview).
        """
        images = _images()
        path = self.original_path(row)
        if path is None:
            raise images.ImageReadError("the library has no folder")
        if _is_video(row):
            return videos.read_video(path, self.tools(), mtime=row["mtime"],
                                     want_preview=kwargs.get("want_preview", True),
                                     want_sheet=False)
        return images.read_image(path, self.helper, mtime=row["mtime"], **kwargs)

    def _source_sig(self, kind, row, path=None):
        """A signature of what a picture file is made from (see Writer.current)."""
        if path is not None:
            try:
                stat = os.stat(path)
            except OSError:
                return None
            return "%s:%d:%s:%d:%d" % (kind, PICTURE_VERSION, row["content_hash"],
                                       stat.st_size, stat.st_mtime_ns)
        sig = "%s:%d:%s:%d:%r" % (kind, PICTURE_VERSION, row["content_hash"],
                                  row["size"] or 0, row["mtime"])
        if kind != "original" and row["projection"] == imageinfo.PROJECTION_DUAL_FISHEYE:
            # Made by stitching the original: a new way of stitching (or
            # other pole fills) makes a new picture.
            sig += ":" + _images().stitch_key()
        return sig

    def _thumb(self, picture):
        row = picture.row
        rel = "thumbs/%s.jpg" % picture.xid
        data = self.thumbs.get(row["id"])
        if data is None:
            data = self.read_original(row, want_preview=False)["thumb"]
            sig = self._source_sig("thumb-original", row)
        else:
            sig = "thumb:%d:%s" % (PICTURE_VERSION, hashlib.sha1(data).hexdigest())
        if not self.writer.current(rel, sig):
            self.writer.write(rel, clean_jpeg(data), sig)

    def _stored_preview(self, row):
        if row["has_preview"] != 1:
            return None
        try:
            path = self.previews.path_for(self.library_id, row["id"])
        except ValueError:
            return None
        return path if os.path.isfile(path) else None

    def _web_image(self, picture):
        row = picture.row
        rel = "images/%s.jpg" % picture.xid
        path = self._stored_preview(row)
        if path is not None:
            sig = self._source_sig("web", row, path)
            if sig and self.writer.current(rel, sig):
                return
            with open(path, "rb") as handle:
                data = handle.read()
        else:
            sig = self._source_sig("web-original", row)
            if self.writer.current(rel, sig):
                return
            data = self.read_original(row)["preview"]
            if not data:
                raise ValueError("no preview could be made")
        self.writer.write(rel, web_jpeg(data), sig)

    def _original(self, picture):
        row = picture.row
        if _is_video(row):
            # A video's picture is its poster (the video itself goes to
            # videos/, see _video).
            self._web_image(picture)
            picture.ext = "jpg"
            return
        if row["projection"] == imageinfo.PROJECTION_DUAL_FISHEYE:
            # The original is two unstitched fisheye circles, which no page
            # can show as the 360-degree picture: the site gets the
            # stitched panorama (its web-size picture) instead.
            self._web_image(picture)
            picture.ext = "jpg"
            return
        sig = self._source_sig("original", row)
        for ext in ORIGINAL_EXTENSIONS:
            if self.writer.current("images/%s.%s" % (picture.xid, ext), sig):
                picture.ext = ext
                return
        path = self.original_path(row)
        if path is None:
            raise ValueError("the library has no folder")
        data = common.load_image_bytes(self.helper, path)
        orientation = None
        try:
            exif = json.loads(row["exif"] or "{}", parse_constant=lambda _name: None)
            orientation = exif.get("Orientation") if isinstance(exif, dict) else None
        except (TypeError, ValueError):
            pass
        clean, picture.ext = original_picture(data, orientation)
        self.writer.write("images/%s.%s" % (picture.xid, picture.ext), clean, sig)

    def _pano(self, picture):
        row = picture.row
        rel = "panos/%s.jpg" % picture.xid
        path = None
        if row["has_preview"] == 1:
            try:
                path = self.previews.pano_path_for(self.library_id, row["id"])
            except ValueError:
                path = None
        if path and os.path.isfile(path):
            sig = self._source_sig("pano", row, path)
            if not (sig and self.writer.current(rel, sig)):
                with open(path, "rb") as handle:
                    self.writer.write(rel, clean_jpeg(handle.read()), sig)
            picture.pano = True
            return
        sig = self._source_sig("pano-original", row)
        if self.writer.current(rel, sig):
            picture.pano = True
            return
        # Not made yet (a row scanned before panoramas existed): make it
        # now, and keep it with the previews, as the server does.
        result = self.read_original(row, want_preview=False, want_pano=True)
        data = result.get("pano")
        if not data:
            raise ValueError("no panorama could be made")
        if (path and self.library.get("previews", True)
                and result.get("content_hash") == row["content_hash"]):
            try:
                self.previews.write_pano(self.library_id, row["id"], data)
            except OSError as error:
                self.ctx.log("Could not keep the 360-degree panorama of %s: %s"
                             % (row["path"], error), "warn")
        self.writer.write(rel, clean_jpeg(data), sig)
        picture.pano = True

    def _video(self, picture):
        """Write videos/<xid>.mp4 of a video (see video_plan); True when it is published."""
        row = picture.row
        kind, source, size, _reason = video_plan(row, self.options, self.library_id)
        if kind != "video":
            return False
        rel = "videos/%s.mp4" % picture.xid
        if source is None and row["projection"] == videos.PROJECTION_360:
            # (Its LRV, as the server's player gets it.)
            source, kind = videos.playable_360(row, self.original_path(row))
            if source is None:
                raise ValueError("its low-resolution copy (.lrv) is not there")
            sig = self._source_sig(kind, row, source)
        elif source is None:
            source = self.original_path(row)
            if source is None:
                raise ValueError("the library has no folder")
            sig = self._source_sig("video", row)
        else:
            sig = self._source_sig("video-proxy", row, source)
        tools = self.tools()
        if tools is None and not self.writer.current(rel, sig):
            raise videos.ToolsMissing()
        self.writer.write_file(rel, lambda path: videos.strip_copy(tools, source, path), sig)
        return True

    def keep_previous(self, picture):
        """Keep the files an earlier export wrote for a picture that cannot be read now.

        Only when its thumbnail and its picture (for the images option) are
        still there as written: a library that is offline for a moment
        then leaves the site as it was. True when they were kept.
        """
        xid = picture.xid
        wanted = ["thumbs/%s.jpg" % xid]
        mode = self.options["images"]
        if mode != "none":
            found = [rel for rel in self.writer.previous
                     if rel.startswith("images/%s." % xid)]
            if len(found) != 1 or (mode == "web" and not found[0].endswith(".jpg")):
                return False
            wanted.append(found[0])
            picture.ext = found[0].rsplit(".", 1)[1]
        if not all(self.writer.intact(rel) for rel in wanted):
            return False
        for rel in wanted:
            self.writer.keep(rel)
        if mode != "none" and self.writer.keep("panos/%s.jpg" % xid):
            picture.pano = True
        # The video itself only while the options still publish it (the
        # owner may have turned videos off since, or the copy is gone).
        if (_is_video(picture.row)
                and video_plan(picture.row, self.options, self.library_id)[0] == "video"
                and self.writer.keep("videos/%s.mp4" % xid)):
            picture.video = True
        return True

    def note_failure(self, text):
        """Log a picture that went wrong (the first LOGGED_FAILURES of them)."""
        self.logged += 1
        if self.logged <= LOGGED_FAILURES:
            self.ctx.log(text, "warn")
        elif self.logged == LOGGED_FAILURES + 1:
            self.ctx.log("More pictures could not be read; they are counted, not listed.",
                         "warn")

    def picture(self, picture):
        """Write one picture's files (run in a worker thread).

        Sets picture.ok, or .error when the picture cannot be read (its
        files are then taken back, see forget_picture), or .fatal when a
        file cannot be written. The picture is written before its
        thumbnail, so a picture that cannot be read leaves no thumbnail.
        """
        mode = self.options["images"]
        try:
            if mode == "web":
                self._web_image(picture)
            elif mode == "original":
                self._original(picture)
            self._thumb(picture)
        except ExportWriteError as error:
            picture.fatal = error
            return picture
        except Exception as error:
            picture.error = "%s" % (error,) or type(error).__name__
            return picture
        picture.ok = True
        if (mode != "none" and picture.record.get("p") == "360" and not _is_video(picture.row)
                and _images().has_pano(picture.row["width"], picture.row["height"])):
            try:
                self._pano(picture)
            except ExportWriteError as error:
                picture.fatal = error
            except Exception as error:
                picture.error = "no 360-degree panorama: %s" % (error,)
        if _is_video(picture.row) and self.options["videos"] == "include":
            # A video that cannot be copied keeps its poster.
            try:
                picture.video = self._video(picture)
            except ExportWriteError as error:
                picture.fatal = error
            except Exception as error:
                picture.error = "the video itself could not be copied: %s" % (error,)
        return picture

    def forget_picture(self, picture):
        """Take back the files written for a picture that is not exported after all."""
        for rel in picture.files():
            self.writer.forget(rel)

    def pictures(self, chosen, rows):
        """Write the pictures of the chosen records; returns those that worked (in order)."""
        ctx = self.ctx
        items = []
        seen = {}
        for record in chosen:
            row = rows.get(record["i"])
            if row is None:
                continue
            xid = make_xid(self.secret, self.export_id, record["i"])
            if xid in seen:
                # 60 bits: practically never; but two pictures must not share files.
                ctx.log("Skipped %s: its id in the export is taken." % row["path"], "error")
                self.failed += 1
                continue
            seen[xid] = True
            items.append(_Picture(record, row, xid))
        ctx.stage("export", total=len(items))
        threads = max(1, min(16, int(self.config.get("reader_threads") or 4)))
        done = []
        stopped = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
            queue = collections.deque()
            source = iter(items)

            def fill():
                while len(queue) < threads * 2:
                    item = next(source, None)
                    if item is None:
                        return
                    queue.append(pool.submit(self.picture, item))

            fill()
            while queue:
                picture = queue.popleft().result()
                if picture.fatal is not None:
                    # A file cannot be written (a full disk, ...): stop here.
                    for future in queue:
                        future.cancel()
                    raise picture.fatal
                if picture.ok:
                    done.append(picture)
                    if picture.error:
                        if _is_video(picture.row):
                            self.video_failed += 1
                        else:
                            self.pano_failed += 1
                        self.note_failure("%s: %s" % (picture.row["path"], picture.error))
                elif self.keep_previous(picture):
                    done.append(picture)
                    self.kept += 1
                    self.note_failure("%s cannot be read (%s); the earlier export's files "
                                      "are kept." % (picture.row["path"], picture.error))
                else:
                    self.forget_picture(picture)
                    self.failed += 1
                    self.note_failure("Skipped %s: %s" % (picture.row["path"], picture.error))
                ctx.step(item=picture.row["path"])
                ctx.wait_if_paused()
                if ctx.should_stop():
                    stopped = True
                    for future in queue:
                        future.cancel()
                    break
                fill()
        return done, stopped

    # -- data files -----------------------------------------------------------

    def records(self, pictures):
        """The final search records (export ids everywhere), sorted by id.

        Besides the keys of search_index, a record may have "ix" (the
        extension of its picture when not jpg), "pn": 1 (it has a large
        360-degree panorama) and "tn": its place in capture order among the
        exported pictures of its day (1, 2, ...; only on days with more
        than one), so pictures of one day sort as they were taken while
        their times stay out of the export.
        """
        records = [picture.record for picture in pictures]
        # Duplicate groups over the exported pictures only: a group's
        # cover may have been left out (or be hidden).
        groups = collections.defaultdict(list)
        for record in records:
            if record.get("dg") is not None:
                groups[record["dg"]].append(record["i"])
        search_index.apply_groups(records, list(groups.values()))
        # Drives over the exported clips only, with the drives' own words the
        # options allow (drives.export_texts), with export ids below.
        drives.export_texts(records, self.options, drives.settings(self.config)["min_clips"],
                            drives.clip_texts(self.conn, self.library_id, self.config),
                            drives.clip_places(self.conn, self.library_id, self.config))
        days = collections.defaultdict(list)
        for picture in pictures:
            day = picture.record.get("dk")
            if day:
                days[day].append(((picture.row["taken_at"] or ""), picture.row["id"],
                                  picture.record))
        for entries in days.values():
            if len(entries) > 1:
                entries.sort(key=lambda entry: entry[:2])
                for number, entry in enumerate(entries, 1):
                    entry[2]["tn"] = number
        xids = {picture.record["i"]: picture.xid for picture in pictures}
        numbers = self.album_numbers(pictures)
        for picture in pictures:
            record = picture.record
            record["i"] = picture.xid
            record["l"] = self.export_id
            if record.get("dg") is not None:
                record["dg"] = xids[record["dg"]]
            if "al" in record:
                # (The database's album ids stay here: the export numbers them.)
                kept = sorted(numbers[album_id] for album_id in record["al"]
                              if album_id in numbers)
                if kept:
                    record["al"] = kept
                else:
                    del record["al"]
            if record.get("dv") is not None:
                record["dv"] = xids[record["dv"]]
            if picture.ext != "jpg":
                record["ix"] = picture.ext
            if picture.pano:
                record["pn"] = 1
            if picture.video:
                record["vf"] = 1
                codec = published_codec(picture.row, video_plan(picture.row, self.options,
                                                                self.library_id)[1])
                if codec:
                    # (What the site's player gets: a playable copy is H.264.)
                    record["vc"] = codec[:20]
        records.sort(key=lambda record: record["i"])
        return records

    def album_numbers(self, pictures):
        """{album id: its number in the export} of the albums the export publishes (the
        marks option; else none), and their manifest entries in self.published_albums.

        Manual albums with at least one exported picture (their items: the
        export ids of those pictures, in the album's order) and the smart
        albums whose search can be published (see smart_search; one that
        names an album that is not published is left out too), numbered 1,
        2, ... in the order of their ids.
        """
        self.published_albums = []
        if not self.options["marks"] or not pictures:
            return {}
        xids = {picture.row["id"]: picture.xid for picture in pictures}
        every = sorted(albums.list_albums(self.conn), key=lambda item: item["id"])
        manual = {}
        for album in every:
            if album["kind"] == "manual":
                items = [xids[image_id] for image_id in albums.album_items(
                    self.conn, album["id"], [self.library_id], include_hidden=False)
                         if image_id in xids]
                if items:
                    manual[album["id"]] = items
        smart = {}
        for album in every:
            if album["kind"] == "smart":
                search = self.smart_search(album, xids)
                if search is not None:
                    smart[album["id"]] = search
        # (A smart album may name another album: it goes when that one is
        # not published, and so on.)
        named = {}
        changed = True
        while changed:
            changed = False
            for album_id in sorted(smart):
                targets = [albums.find_listed(every, term["value"])
                           for term in smart[album_id]["albums"]]
                if any(target is None or (target["id"] not in manual
                                          and target["id"] not in smart)
                       for target in targets):
                    del smart[album_id]
                    changed = True
                else:
                    named[album_id] = [target["id"] for target in targets]
        numbers = {}
        for album in every:
            if album["id"] in manual or album["id"] in smart:
                numbers[album["id"]] = len(numbers) + 1
        for album in every:
            entry = {"name": album["name"], "kind": album["kind"]}
            if album["id"] in manual:
                entry["items"] = manual[album["id"]]
            elif album["id"] in smart:
                search = smart[album["id"]]
                # album: terms name the published album by its number there.
                changes = dict(search["changes"])
                for term, target in zip(search["albums"], named[album["id"]]):
                    changes[(term["value_start"], term["value_stop"])] = "#%d" % numbers[target]
                entry["query"] = _rewrite(album.get("query") or "", changes)
                entry["options"] = search["options"]
            else:
                continue
            entry["id"] = numbers[album["id"]]
            self.published_albums.append(entry)
        return numbers

    def smart_search(self, album, xids):
        """What of a smart album's saved search can be published, or None when it cannot.

        Not when it is limited to another library (its library choice or
        Library filter; this library's choice is dropped); nor when its
        search (query terms or filters) goes by what the export leaves out:
        folder: (the folders option), file: (file_names), place:, has:gps
        and the Place filter (location), lib: (the libraries' own names);
        nor when it looks for pictures like one that is not published
        (similar:, look:). Returns {"options", "changes": {(start, stop):
        new value} (similar: and look: name the picture by its export id),
        "albums": the album: terms (album_numbers checks them)}.
        """
        options = dict(album.get("options") or {})
        if not albums.smart_in_libraries(options, [self.library_id]):
            return None
        options.pop("lib", None)
        filters = dict(options.get("filters") or {})
        filters.pop("library", None)
        if not self.options["location"] and (filters.get("place") or "gps" in (
                filters.get("has") or [])):
            return None
        if filters:
            options["filters"] = filters
        else:
            options.pop("filters", None)
        needs = {"folder": "folders", "file": "file_names", "place": "location"}
        changes = {}
        named = []
        for term in albums.query_terms(album.get("query") or ""):
            field = term["field"]
            value = term["value"].strip()
            if field == "lib" or (field in needs and not self.options[needs[field]]):
                return None
            if (field == "has" and not self.options["location"]
                    and value.lower().startswith(_LOCATION_WORDS)):
                return None
            if field in ("similar", "look"):
                xid = xids.get(int(value)) if re.match(r"^[0-9]{1,18}$", value) else None
                if xid is None:
                    return None
                changes[(term["value_start"], term["value_stop"])] = xid
            elif field == "album":
                named.append(term)
        return {"options": options, "changes": changes, "albums": named}

    def data_files(self, pictures, records, capabilities):
        """{relative path: text} of the data scripts, and the manifest (without its date)."""
        files = {}
        texts = [(record["i"], _js_json(record)) for record in records]
        count = _shard_count(sum(len(text) + 2 for _xid, text in texts), INDEX_SHARD_BYTES)
        shards = [[] for _ in range(count)]
        for xid, text in texts:
            shards[bucket_of(xid, count)].append(text)
        index_names = []
        for number, lines in enumerate(shards):
            name = "index-%03d.js" % number
            index_names.append(name)
            files["data/" + name] = "SEARCH_DATA.push([\n%s\n]);\n" % ",\n".join(lines)

        details = []
        for picture in pictures:
            entry = detail_entry(picture.row, picture.record)
            if entry is not None:
                details.append((picture.xid, _js_json(entry)))
        details.sort()
        count = _shard_count(sum(len(text) + 20 for _xid, text in details), DETAIL_SHARD_BYTES)
        buckets = [[] for _ in range(count)]
        for xid, text in details:
            buckets[bucket_of(xid, count)].append('"%s":%s' % (xid, text))
        detail_names = []
        for number, lines in enumerate(buckets):
            name = "detail-%03d.js" % number
            detail_names.append(name)
            files["data/" + name] = "SEARCH_DATA.detail({\n%s\n});\n" % ",\n".join(lines)

        vector_names = self.vector_files(pictures, files)
        look_names = self.look_files(pictures, files)
        meaning = self.meaning_data(files) if vector_names else None
        manifest = {
            "format": MARKER_FORMAT, "version": search_index.INDEX_VERSION,
            "title": self.title,
            "library": {"id": self.export_id, "name": self.title},
            "libraries": [{"id": self.export_id, "name": self.title}],
            "count": len(records), "shards": index_names, "details": detail_names,
            "capabilities": capabilities, "options": options_summary(self.options),
        }
        if self.options["credits"]:
            manifest["credits"] = self.options["credits"]
        if vector_names:
            manifest["vectors"] = vector_names
        if look_names:
            manifest["looks"] = look_names
        if meaning is not None:
            manifest["meaning"] = meaning
        if self.published_albums:
            manifest["albums"] = self.published_albums
        if self.options["location"]:
            points = []
            for picture in pictures:
                where = _location(picture.row)
                if where is not None:
                    points.append({"id": picture.xid, "lat": where[0], "lon": where[1]})
            points.sort(key=lambda point: point["id"])
            files["data/locations.js"] = "SEARCH_DATA.locations([\n%s\n]);\n" % ",\n".join(
                _js_json(point) for point in points)
            manifest["locations"] = "locations.js"
        manifest["versions"] = {rel[len("data/"):]: _version(text.encode("ascii"))
                                for rel, text in sorted(files.items())}
        if meaning is not None:
            manifest["versions"].update((rel[len("data/"):], _version(data))
                                        for rel, data in sorted(self.meaning["files"].items()))
        return files, manifest

    def vector_files(self, pictures, files):
        """Add data/vectors-NNN.js (the similar option) to files; returns their names.

        Each holds SEARCH_VECTORS.push({"dims", "ids": [export ids], "data":
        base64 of the vectors one after the other, "model": the query model's
        id with the meaning option}) for the pictures whose ids fall in it
        (bucket_of, like the index shards). Only up-to-date vectors go
        (embeddings.fresh_vectors); the model's name stays here.
        """
        self.vector_stats = {"vectors": 0, "stale": 0, "missing": 0, "bytes": 0}
        if not self.options["similar"] or not pictures:
            return []
        found = embeddings.fresh_vectors(self.conn, self.library_id,
                                         [picture.row["id"] for picture in pictures],
                                         carried=_carried((picture.row["id"], picture.record)
                                                          for picture in pictures))
        items = sorted((picture.xid, found["vectors"][picture.row["id"]])
                       for picture in pictures if picture.row["id"] in found["vectors"])
        self.vector_stats.update(vectors=len(items), stale=found["stale"],
                                 missing=found["missing"])
        if not items:
            return []
        dims = found["dims"]
        count = _shard_count(len(items) * vector_bytes(dims), INDEX_SHARD_BYTES)
        buckets = [[] for _ in range(count)]
        for xid, vec in items:
            buckets[bucket_of(xid, count)].append((xid, vec))
        names = []
        self.vector_parts = []
        for number, entries in enumerate(buckets):
            name = "vectors-%03d.js" % number
            names.append(name)
            payload = {"dims": dims, "ids": [xid for xid, _vec in entries],
                       "data": base64.b64encode(b"".join(vec for _xid, vec in entries))
                       .decode("ascii")}
            self.vector_parts.append((name, payload))
        self.write_vector_parts(files)
        return names

    def look_files(self, pictures, files):
        """Add data/looks-NNN.js (the looks option) to files; returns their names.

        Each holds SEARCH_LOOKS.push({"dims", "ids": [export ids], "data":
        base64 of the look vectors one after the other, "model": the
        recipe's opaque id (looks.look_id)}) for the pictures whose ids fall
        in it (bucket_of, like the index shards).
        """
        self.look_stats = {"vectors": 0, "missing": 0, "bytes": 0}
        if not self.options["looks"] or not pictures:
            return []
        found = looks.fresh_looks(self.conn, self.library_id,
                                  [picture.row["id"] for picture in pictures])
        items = sorted((picture.xid, found["vectors"][picture.row["id"]])
                       for picture in pictures if picture.row["id"] in found["vectors"])
        self.look_stats.update(vectors=len(items), missing=len(pictures) - len(items))
        if not items:
            return []
        dims = found["dims"]
        count = _shard_count(len(items) * vector_bytes(dims), INDEX_SHARD_BYTES)
        buckets = [[] for _ in range(count)]
        for xid, vec in items:
            buckets[bucket_of(xid, count)].append((xid, vec))
        names = []
        for number, entries in enumerate(buckets):
            name = "looks-%03d.js" % number
            names.append(name)
            payload = {"dims": dims, "ids": [xid for xid, _vec in entries],
                       "data": base64.b64encode(b"".join(vec for _xid, vec in entries))
                       .decode("ascii"), "model": found["model"]}
            text = "SEARCH_LOOKS.push(%s);\n" % _js_json(payload)
            files["data/" + name] = text
            self.look_stats["bytes"] += len(text)
        return names

    def write_vector_parts(self, files, model=None):
        """The texts of the vector files into files (with the query model's id, if any)."""
        self.vector_stats["bytes"] = 0
        for name, payload in self.vector_parts:
            if model:
                payload = dict(payload, model=model)
            text = "SEARCH_VECTORS.push(%s);\n" % _js_json(payload)
            files["data/" + name] = text
            self.vector_stats["bytes"] += len(text)

    def meaning_data(self, files):
        """Search by meaning in the browser (the meaning option, see meaning_status): its
        files (kept in self.meaning, written with the others) and manifest entry, or None
        when the option is off or it cannot be used (the reason is logged)."""
        self.meaning = None
        self.meaning_status = None
        if not self.options["meaning"]:
            return None
        status = meaning_status(self.config, self.options, self.vector_stats["vectors"])
        self.meaning_status = status
        if not status["available"]:
            return None
        try:
            made = meaning_files(status)
        except (browser_model.ModelError, OSError) as error:
            status.update(available=False, reason="The model file could not be converted for "
                                                  "a browser: %s" % common.redact(str(error)))
            return None
        self.meaning = made
        self.write_vector_parts(files, made["id"])
        names = sorted(rel[len("data/"):] for rel in made["files"])
        return {"id": made["id"], "dims": made["dims"], "model": MEANING_FOLDER + "/model.js",
                "parts": [name for name in names if name.startswith(MEANING_FOLDER + "/w-")],
                "bytes": made["bytes"], "gzip": made["gzip"], "license": MEANING_LICENSE,
                "credit": status["credit"],
                "assets": {name: _version(data) for name, data in made["assets"].items()}}

    def manifest_text(self, manifest, files):
        """data/manifest.js, dated the day the export's contents last changed."""
        digest = hashlib.sha1(_js_json(manifest).encode("ascii"))
        for rel in sorted(files):
            digest.update(rel.encode("ascii") + b"\n" + files[rel].encode("ascii"))
        for rel in sorted(self.writer.files):
            if rel.startswith(("thumbs/", "images/", "panos/", "videos/")):
                digest.update(("%s %s\n" % (rel, self.writer.files[rel][0])).encode("utf-8"))
        content = digest.hexdigest()
        generated = self.state.get("generated")
        if self.state.get("content") != content or not isinstance(generated, str):
            generated = datetime.date.today().isoformat()
        self.state["content"] = content
        self.state["generated"] = generated
        manifest = dict(manifest, generated=generated)
        return "window.SEARCH_DATA_MANIFEST = %s;\n" % _js_json(manifest)

    def tiles(self):
        """Write the map tiles (with location only); returns the highest zoom, or None.

        Incremental like every file of the export (Writer): a tile whose
        source is unchanged since the last export (same size and time of
        the file in data/tiles, or the same bundled basemap) is neither
        read nor written again, so a second export of a pyramid of 11,000
        tiles costs a stat per tile. A tile of the pyramid that is cut
        short (an interrupted make_tiles.py) is left out -- the map shows
        a coarser tile in its place -- with a warning.
        """
        import serve
        max_zoom = serve.tiles_max_zoom()
        if max_zoom is not None:
            source = common.tiles_dir()
            damaged = 0
            for folder, _dirs, names in os.walk(source):
                for name in sorted(names):
                    if not name.endswith(".jpg"):
                        continue
                    path = os.path.join(folder, name)
                    rel = "tiles/" + os.path.relpath(path, source).replace(os.sep, "/")
                    if not re.match(r"^tiles/\d+/\d+/\d+\.jpg$", rel):
                        continue
                    stat = os.stat(path)
                    sig = "tile:%d:%d" % (stat.st_size, stat.st_mtime_ns)
                    if self.writer.current(rel, sig):
                        continue
                    with open(path, "rb") as handle:
                        data = handle.read()
                    if not imageinfo.jpeg_complete(data):
                        damaged += 1
                        continue
                    self.writer.write(rel, data, sig)
            if damaged:
                self.ctx.log("%d map tile(s) in %s are damaged (cut short) and were left out; "
                             "run make_tiles.py again to repair them." % (damaged, source),
                             "warn")
            return max_zoom
        # No pyramid: the tiles the server cuts from the bundled small map.
        try:
            stat = os.stat(serve.FALLBACK_BASEMAP)
        except OSError:
            return None
        sig = "basemap:%d:%d:%d:%d" % (stat.st_size, stat.st_mtime_ns, serve.TILE_SIZE,
                                       serve.FALLBACK_TILE_VERSION)
        for zoom in range(serve.FALLBACK_MAX_ZOOM + 1):
            for x in range(2 ** (zoom + 1)):
                for y in range(2 ** zoom):
                    rel = "tiles/%d/%d/%d.jpg" % (zoom, x, y)
                    if self.writer.current(rel, sig):
                        continue
                    data = serve._fallback_tile_bytes(zoom, x, y)
                    if data is None:
                        return None
                    self.writer.write(rel, data, sig)
        return serve.FALLBACK_MAX_ZOOM

    # -- the zip --------------------------------------------------------------

    def write_zip(self):
        """<folder>.zip of the export (rebuilt only when a file changed); its path."""
        path = zip_path_for(self.out_dir)
        # Everything but the marker (an unpacked zip is a site, not an export folder).
        files = sorted(rel for rel in self.writer.files if rel != MARKER)
        digest = hashlib.sha1()
        for rel in files:
            entry = self.writer.files[rel]
            digest.update(("%s %d %d\n" % (rel, entry[1], entry[2])).encode("utf-8"))
        sig = digest.hexdigest()
        before = self.state.get("zip")
        if isinstance(before, dict) and before.get("sig") == sig and before.get("path") == path:
            try:
                stat = os.stat(path)
                if stat.st_size == before.get("size") and stat.st_mtime_ns == before.get("mtime"):
                    return path
            except OSError:
                pass
        self.ctx.stage("zip", total=len(files))
        temp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
        try:
            self._zip_files(temp, files)
            os.replace(temp, path)
            stat = os.stat(path)
        except OSError as error:
            raise ExportWriteError("The zip file could not be written: %s"
                                   % os_error_text(error, path))
        finally:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
        self.state["zip"] = {"path": path, "sig": sig, "size": stat.st_size,
                             "mtime": stat.st_mtime_ns}
        return path

    def _zip_files(self, temp, files):
        with zipfile.ZipFile(temp, "w", allowZip64=True) as archive:
            for rel in files:
                source = self.writer.path(rel)
                info = zipfile.ZipInfo(rel, date_time=_ZIP_DATE)
                info.external_attr = 0o644 << 16
                info.create_system = 0
                if rel.lower().endswith(STORED_EXTENSIONS):
                    info.compress_type = zipfile.ZIP_STORED
                else:
                    info.compress_type = zipfile.ZIP_DEFLATED
                size = os.path.getsize(source)
                with open(source, "rb") as reader, \
                        archive.open(info, "w", force_zip64=size >= 2 ** 31) as entry:
                    shutil.copyfileobj(reader, entry, 1024 * 1024)
                self.ctx.step(quick=True)

    # -- everything -----------------------------------------------------------

    def check_sources(self, chosen, rows, sizes):
        """Refuse an export whose pictures cannot be read at all (the library is offline).

        Pictures need the library's folder for originals, and when they
        have no stored preview (web size) or thumbnail. When it cannot be
        reached and every picture needs it, with no earlier export of the
        folder whose files could be kept, nothing could be exported: an
        ExportError says what to do. When only some need it, a warning.
        """
        if folder_reachable(self.library, self.helper):
            return
        needing = sum(1 for record in chosen if record["i"] in rows
                      and sizes.needs_original(rows[record["i"]]))
        if not needing:
            return
        where = "The folder of %s (%s) is not available (offline?)" % (
            common.library_label(self.library), self.root or "none")
        # (keep_previous keeps a picture's earlier files: its thumbnail, and
        # its picture unless only thumbnails are exported.)
        kept = "thumbs/" if self.options["images"] == "none" else "images/"
        earlier = any(rel.startswith(kept) for rel in self.writer.previous)
        if needing >= len(chosen) and not earlier:
            if self.options["images"] == "original":
                hint = ("Connect it and export again, or choose Web size: it uses the "
                        "pictures kept in the data folder.")
            else:
                hint = ("Connect it and export again (with Build previews on, a scan keeps "
                        "web-size pictures in the data folder for the next time).")
            raise ExportError("%s, and every picture to export needs it. %s" % (where, hint))
        self.ctx.log("%s: %d of the %d pictures need it. Where an earlier export's files are "
                     "still there they are kept; the others are left out." % (
                         where, needing, len(chosen)), "warn")

    def check_space(self, chosen, rows, sizes):
        """Raise ExportError when the disk of the folder has too little room for the export."""
        estimate = _asset_bytes(self.options)
        for record in chosen:
            row = rows.get(record["i"])
            if row is not None:
                estimate += sum(sizes.parts(record, row).values())
        present = sum(entry[1] for entry in self.writer.previous.values()
                      if isinstance(entry, list) and len(entry) == 3
                      and isinstance(entry[1], int) and entry[1] > 0)
        needed = max(0, estimate - present)
        if self.options["zip"]:
            needed += estimate
        folder = self.out_dir
        while not os.path.isdir(folder) and os.path.dirname(folder) != folder:
            folder = os.path.dirname(folder)
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            return
        if free < needed + SPACE_MARGIN:
            raise ExportError(
                "There is not enough free space on the disk holding %s: the export needs "
                "about %s more, and %s is free. Free some space, choose another folder, or "
                "export smaller pictures." % (self.out_dir, human_bytes(needed + SPACE_MARGIN),
                                              human_bytes(free)))

    def run(self):
        ctx = self.ctx
        self.prepare()
        finished = False
        try:
            chosen, _records, _flagged, counts = select_records(
                self.conn, self.config, self.library, self.options)
            rows = _load_rows(self.conn, self.library_id)
            sizes = _Sizes(self.library_id, self.options, [record["i"] for record in chosen])
            # Refused before anything is written: the folder and the record
            # of the last export stay as they were.
            self.check_sources(chosen, rows, sizes)
            self.check_space(chosen, rows, sizes)
            self.mark()
            with cache.ThumbStore() as thumbs:
                self.thumbs = thumbs
                pictures, stopped = self.pictures(chosen, rows)
            if stopped:
                ctx.log("Export stopped; run it again to finish it.", "warn")
                return self.summary(counts, pictures, stopped=True)
            if not pictures and self.failed:
                # Writing an empty site would also remove an earlier one.
                raise ExportError(
                    "None of the %d pictures to export could be read (the log names them), "
                    "so the folder is left as it was." % self.failed)
            if not pictures:
                ctx.log("No picture is published: %s. Change the export options, or describe "
                        "the pictures first." % (left_out_text(counts["by_reason"])
                                                 or "the library has none"), "warn")
            records = self.records(pictures)
            self.count = len(records)
            location = self.options["location"]
            capabilities = {"admin": False, "static": True, "map": location,
                            "images": self.options["images"] != "none",
                            "originals": self.options["images"] == "original",
                            "panos": any(picture.pano for picture in pictures),
                            "file_names": self.options["file_names"],
                            "folders": self.options["folders"]}
            if any(picture.video for picture in pictures):
                capabilities["videos"] = True
            if self.options["marks"]:
                # (Favorites, ratings and album names were published.)
                capabilities["marks"] = True
            if location and any(record.get("co") for record in records):
                # Place names (and with them the GeoNames credit, render_page).
                capabilities["places"] = True
            files, manifest = self.data_files(pictures, records, capabilities)
            for rel in sorted(files):
                self.writer.write(rel, files[rel].encode("ascii"))
            if self.meaning is not None:
                # (Not in files: their hashes are in the manifest already, and the
                # page versions of data files are of their text.)
                for rel, data in sorted(self.meaning["files"].items()):
                    self.writer.write(rel, data)
                for name, data in sorted(self.meaning["assets"].items()):
                    self.writer.write("assets/" + name, data)
            manifest_bytes = self.manifest_text(manifest, files).encode("ascii")
            self.writer.write("data/manifest.js", manifest_bytes)
            versions = {}
            for name in ASSETS + (MAP_ASSETS if location else ()):
                with open(os.path.join(_static_dir(), name), "rb") as handle:
                    data = handle.read()
                versions[name] = _version(data)
                self.writer.write("assets/" + name, data)
            tiles_max = self.tiles() if location else None
            pages = PAGES + (MAP_PAGES if location else ())
            for name, template in pages:
                page_title = self.title if name != "map.html" else "Map - %s" % self.title
                text = render_page(template, self.title, page_title, capabilities,
                                   self.export_id, self.options, tiles_max, versions,
                                   _version(manifest_bytes),
                                   {"credit": self.meaning_status["credit"]}
                                   if self.meaning is not None else None)
                self.writer.write(name, text.encode("utf-8"))
            if not self.options["allow_indexing"]:
                # Only heard at the root of a host; the pages say it too.
                self.writer.write("robots.txt", b"User-agent: *\nDisallow: /\n")
            removed = self.remove_stale()
            self.save_state(True)
            finished = True
            zip_file = self.write_zip() if self.options["zip"] else self.remove_zip()
            self.save_state(True)
            summary = self.summary(counts, pictures, removed=removed, zip_file=zip_file)
            ctx.log("Exported %d picture(s) of %s to %s: %d file(s) written, %d unchanged, "
                    "%d removed." % (summary["count"], self.title, self.out_dir,
                                     summary["written"], summary["unchanged"], removed))
            if counts.get("album"):
                ctx.log("Limited to the album %s: %d of its picture(s) are in this library%s."
                        % (counts["album"]["name"], counts["album"]["pictures"],
                           " (%d more in other libraries were left out)"
                           % counts["album"]["elsewhere"] if counts["album"]["elsewhere"]
                           else ""))
            if self.options["marks"]:
                ctx.log("Favorites, ratings and %d album(s) were published (the marks option)."
                        % len(self.published_albums))
            if self.failed:
                ctx.log("%d picture(s) could not be exported (see the warnings above)."
                        % self.failed, "warn")
            if self.kept:
                ctx.log("%d picture(s) could not be read; the files of the earlier export "
                        "were kept." % self.kept, "warn")
            stats = self.vector_stats
            if self.options["similar"] and (stats["vectors"] or stats["stale"]
                                            or stats["missing"]):
                ctx.log("More like this: %d picture(s) have a meaning vector (%s)." % (
                    stats["vectors"], human_bytes(stats["bytes"])))
                if stats["stale"] or stats["missing"]:
                    ctx.log("%d picture(s) have no up-to-date meaning vector, so More like this "
                            "finds them by their tags instead: Update meaning search on the "
                            "Manage page, then export again."
                            % (stats["stale"] + stats["missing"]), "warn")
            looked = self.look_stats
            if self.options["looks"] and looked["vectors"]:
                ctx.log("Looks like this: %d picture(s) have a look vector (%s)%s." % (
                    looked["vectors"], human_bytes(looked["bytes"]),
                    "; %d have none yet (Update looks on the Manage page, then export "
                    "again)" % looked["missing"] if looked["missing"] else ""))
            if self.meaning is not None:
                ctx.log(meaning_text(self.meaning))
            elif self.options["meaning"] and self.meaning_status is not None and \
                    self.meaning_status["reason"] and embeddings.configured(self.config):
                ctx.log("Search by meaning in the browser is not included: %s"
                        % self.meaning_status["reason"])
            return summary
        finally:
            if not finished and self.marked:
                try:
                    self.save_state(False)
                except ExportError as error:
                    # Do not hide the error that stopped the export.
                    ctx.log(str(error), "error")

    def remove_zip(self):
        """Remove the zip an earlier export wrote (zip is off now: it may hold
        pictures that are left out now). Returns None."""
        before = self.state.pop("zip", None)
        if not isinstance(before, dict) or not isinstance(before.get("path"), str):
            return None
        path = before["path"]
        try:
            stat = os.stat(path)
            if stat.st_size == before.get("size") and stat.st_mtime_ns == before.get("mtime"):
                os.remove(path)
                self.ctx.log("Removed the zip file of the earlier export: %s" % path)
            else:
                self.ctx.log("The zip file %s was changed since the export wrote it; it is "
                             "left as it is." % path, "warn")
        except OSError:
            pass
        return None

    def remove_stale(self):
        """Remove the files an earlier export made that this one did not; returns the count."""
        removed = 0
        for rel in self.writer.stale():
            if rel == MARKER or not _safe_rel(rel):
                continue
            path = self.writer.path(rel)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
            except OSError as error:
                self.ctx.log("Could not remove %s: %s" % (path, error), "warn")
        _remove_empty_folders(self.out_dir)
        return removed

    def summary(self, counts, pictures, removed=0, zip_file=None, stopped=False):
        by_reason = dict(counts["by_reason"])
        if self.failed:
            by_reason["failed"] = self.failed
        total_bytes = sum(entry[1] for entry in self.writer.files.values() if entry[1] > 0)
        return {"library_id": self.library_id, "export_id": self.export_id,
                "output": self.out_dir, "zip": zip_file, "count": len(pictures),
                "total": counts["total"], "excluded": by_reason, "failed": self.failed,
                "pano_failed": self.pano_failed, "kept": self.kept,
                "videos": sum(1 for picture in pictures if picture.video),
                "video_failed": self.video_failed,
                "written": self.writer.written,
                "unchanged": self.writer.unchanged, "removed": removed, "bytes": total_bytes,
                "images": self.options["images"], "stopped": stopped,
                "vectors": self.vector_stats["vectors"],
                "looks": self.look_stats["vectors"],
                "meaning": {"id": self.meaning["id"], "bytes": self.meaning["bytes"],
                            "gzip": self.meaning["gzip"]} if self.meaning is not None else None,
                "album": counts.get("album", {}).get("name"),
                "albums": len(self.published_albums),
                "serve": serve_command(self.out_dir)}



def export_library(ctx, conn, config, library_id, out_dir=None, options=None, helper=None):
    """Export one library as a static website; returns a summary dict.

    ctx is a jobs.JobContext (progress: stage "export", pictures done of
    total; None: a quiet one). conn is an index.db connection (db.init
    must have run). out_dir defaults to data/exports/<library name>/ in
    the data root (see default_output_dir; check_output_dir lists the
    folders that are refused). options override the library's saved
    export options (config "export"; see DEFAULT_OPTIONS and
    validate_options). helper is the configured helper module, if any.
    With the location option the GPS check (gps_check.run) runs first.

    The summary: {library_id, export_id, output, zip, count (pictures
    exported), total, excluded {reason: count}, failed, pano_failed, kept
    (pictures that could not be read, whose files from the last export
    were kept), written, unchanged, removed (files), bytes, images,
    stopped}.
    Raises ExportError (OptionsError) when the export cannot be made (also
    while another export runs, here or in another process); one picture
    that cannot be read is skipped and counted, never fatal.
    """
    if ctx is None:
        ctx = jobs.JobContext("export")
    library = common.get_library(config, library_id)
    if library is None:
        raise ExportError("There is no library %r in the settings." % (library_id,))
    if not library.get("id"):
        raise ExportError("The library %s has no id yet." % common.library_label(library))
    options = library_options(library, options)
    out_dir = os.path.abspath(out_dir) if out_dir else default_output_dir(config, library)
    job = _Export(ctx, conn, config, library, out_dir, options, helper)
    lock = jobs.WorkerLock(os.path.join(common.data_dir(), LOCK_FILE))
    if not lock.acquire("export of %s" % common.library_label(library)):
        raise ExportError("Another export is running: %s. Try again when it has finished."
                          % (lock.holder() or "another process"))
    try:
        if options["location"]:
            # Positions read since the last GPS check (a stopped scan's) are
            # checked first: one it sets aside is never published.
            gps_check.run_safely(conn, config, ctx)
        return job.run()
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Command line

def _cli_options(args, library):
    """The options the command line switches change (the saved ones are the base)."""
    options = {}
    if args.images:
        options["images"] = args.images
    for name in ("location", "file_names", "folders", "zip", "marks"):
        if getattr(args, name, False):
            options[name] = True
    if args.videos:
        options["videos"] = args.videos
    for name in ("undescribed", "private", "sensitive"):
        if getattr(args, "include_" + name):
            options["exclude_" + name] = False
    current = library_options(library)
    excluded = [kind for kind in current["exclude_types"]
                if not (kind == "screenshot" and args.include_screenshots)
                and not (kind == "document" and args.include_documents)]
    if excluded != current["exclude_types"]:
        options["exclude_types"] = excluded
    return options


def serve_command(folder):
    """The command that tries an export on this computer (only this computer can open it):
    this program's own small web server (serve_folder)."""
    return 'python "%s" --serve "%s"' % (os.path.abspath(__file__), folder)


SERVE_PORT = 8000
# Bytes sent at a time for a range of a file.
_RANGE_CHUNK = 256 * 1024
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """The files of a folder, answering a request for one byte range of a file (206):
    browsers ask for those to move in a video (python -m http.server sends the whole
    file instead, so seeking fails)."""

    _range_left = None

    def send_head(self):
        self._range_left = None
        wanted = self.headers.get("Range")
        match = _RANGE_RE.match(wanted) if wanted else None
        path = self.translate_path(self.path)
        if (match is None or not (match.group(1) or match.group(2)) or os.path.isdir(path)
                or path.endswith("/")):
            return super().send_head()
        try:
            handle = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            size = os.fstat(handle.fileno()).st_size
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:
                start, end = max(0, size - int(match.group(2))), size - 1
            if start >= size or end < start:
                handle.close()
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified",
                             self.date_time_string(os.fstat(handle.fileno()).st_mtime))
            self.end_headers()
            handle.seek(start)
            self._range_left = end - start + 1
            return handle
        except Exception:
            handle.close()
            raise

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def copyfile(self, source, outputfile):
        if self._range_left is None:
            super().copyfile(source, outputfile)
            return
        left = self._range_left
        while left > 0:
            chunk = source.read(min(_RANGE_CHUNK, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)

    def log_message(self, format, *args):
        pass                    # (quiet: the console shows how to stop it)


def make_server(folder, port=SERVE_PORT, bind="127.0.0.1"):
    """A web server (not started) for the files of folder, on bind:port (port 0: any
    free one), with RangeRequestHandler."""
    handler = functools.partial(RangeRequestHandler, directory=os.path.abspath(folder))
    return http.server.ThreadingHTTPServer((bind, port), handler)


def serve_folder(folder, port=SERVE_PORT, log=print):
    """Serve an export's folder at http://localhost:port/ for this computer only, until
    Ctrl+C."""
    if not os.path.isfile(os.path.join(folder, "index.html")):
        raise ExportError("%s holds no exported site (no index.html)." % folder)
    server = make_server(folder, port)
    log("Serving %s at http://localhost:%d/ (only this computer can open it). Press "
        "Ctrl+C to stop." % (os.path.abspath(folder), server.server_address[1]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")
    finally:
        server.server_close()


def report(summary, log=print):
    """Print what an export did for the command line; returns the exit status (0: all well).

    What was left out and why, and how to open the site when it holds
    pictures. 1 when pictures could not be exported, 2 when it was
    stopped.
    """
    left_out = left_out_text(summary.get("excluded") or {})
    if left_out:
        log("Left out: %s." % left_out)
    if summary.get("stopped"):
        log("The export was stopped before it finished; run it again to finish it.")
        return 2
    if summary.get("count"):
        log("Open %s in a browser, or try it with: %s"
            % (os.path.join(summary["output"], "index.html"), serve_command(summary["output"])))
        log("(then open http://localhost:%d/)" % SERVE_PORT)
    else:
        log("The site holds no pictures. Change the export options (the Export dialog on the "
            "Manage page, or the switches of export.py), or describe the pictures first.")
    if summary.get("zip"):
        log("Zip file: %s" % summary["zip"])
    return 1 if summary.get("failed") else 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Export one library as a static website (see README: Publishing a "
                    "library). The library's saved export options are used, changed by "
                    "the options given here.")
    parser.add_argument("--library", help="the library's name or id (needed for an "
                                           "export)")
    parser.add_argument("--out", help="output folder (default: data/exports/<library name> "
                                      "in the data folder); it must be empty or an earlier "
                                      "export")
    parser.add_argument("--images", choices=IMAGE_MODES,
                        help="web: 2048 px pictures (default); original: the originals, "
                             "metadata removed; none: thumbnails only")
    parser.add_argument("--location", action="store_true",
                        help="include GPS positions, the town, region and country names "
                             "(when place names were downloaded; with a GeoNames credit), "
                             "the map and the AI model's separate landmark and place-guess "
                             "fields (titles and descriptions are published as written "
                             "either way, except that without it GPS positions written "
                             "out in them or in the text in the picture, as a dashcam "
                             "prints them, are taken out)")
    parser.add_argument("--file-names", action="store_true", help="include file names")
    parser.add_argument("--folders", action="store_true", help="include folder names")
    parser.add_argument("--include-screenshots", action="store_true",
                        help="include screenshots")
    parser.add_argument("--include-documents", action="store_true",
                        help="include documents")
    parser.add_argument("--include-undescribed", action="store_true",
                        help="include pictures not described yet (or described by an older "
                             "version), which were not checked for private or sensitive "
                             "content")
    parser.add_argument("--include-private", action="store_true",
                        help="include pictures flagged as showing private information")
    parser.add_argument("--include-sensitive", action="store_true",
                        help="include pictures flagged as suggestive, explicit or graphic")
    parser.add_argument("--zip", action="store_true",
                        help="also write <output folder>.zip next to the folder")
    parser.add_argument("--album", metavar="NAME",
                        help="only the pictures of this manual album (its name, or its id); "
                             "the privacy options still apply to them")
    parser.add_argument("--marks", action="store_true",
                        help="also publish favorites, ratings and album names (so "
                             "is:favorite, rating: and album: work in the site)")
    parser.add_argument("--videos", choices=VIDEO_MODES,
                        help="skip: videos are published as their poster frame (default); "
                             "include: also the videos browsers can play (H.264, or a "
                             "playable copy), up to max_video_mb, without their metadata")
    parser.add_argument("--serve", metavar="FOLDER",
                        help="instead of exporting: try an exported site on this computer "
                             "(only it can open it) at http://localhost:%d/, with seeking in "
                             "videos (python -m http.server cannot do that); Ctrl+C stops it"
                        % SERVE_PORT)
    parser.add_argument("--port", type=int, default=SERVE_PORT,
                        help="the port for --serve (default %d)" % SERVE_PORT)
    args = parser.parse_args(argv)
    if args.serve:
        try:
            serve_folder(args.serve, args.port)
        except (ExportError, OSError) as error:
            raise SystemExit("Error: %s" % error)
        return None
    if not args.library:
        parser.error("--library is needed (or --serve FOLDER)")
    try:
        db.init()
        config = common.load_config()
        library = common.get_library(config, args.library)
        if library is None:
            raise ExportError("There is no library %r in the settings." % args.library)
        options = _cli_options(args, library)
        ctx = jobs.ConsoleContext("export")
        conn = db.connect()
        try:
            if args.album:
                album = albums.find_album(conn, args.album)
                if album is None:
                    raise ExportError("There is no album %r (see the Albums page)." % args.album)
                options["album"] = album["id"]
            with ctx.stop_on_ctrl_c():
                summary = export_library(ctx, conn, config, library["id"], args.out, options,
                                         helper=common.load_helper(config.get("helper", "")))
        finally:
            conn.close()
    except KeyboardInterrupt:
        # A second Ctrl+C: the next run finishes the export.
        raise SystemExit("\nStopped (Ctrl+C). Run the same command again to finish the export.")
    except (ExportError, common.ConfigError, db.DatabaseError) as error:
        raise SystemExit("Error: %s" % error)
    except OSError as error:
        raise SystemExit("Error: %s" % os_error_text(error))
    status = report(summary)
    if status:
        raise SystemExit(status)
    return summary


if __name__ == "__main__":
    main()
