#!/usr/bin/env python3
"""Shared helpers for the image database tools.

This module uses only the standard library so it can be imported by every
script without pulling in third party dependencies.

Paths are functions, evaluated each time they are called, so tests (and any
long-running process) see the current SETTINGS_BASE rather than the value
that was set when this module was first imported.
"""

import copy
import fnmatch
import importlib.util
import json
import locale
import os
import re
import secrets
import sys
import threading
import time


IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".hif", ".webp",
    ".tif", ".tiff", ".avif", ".bmp",
)
# Formats Pillow can only read through an optional plugin (pillow-heif, or a
# Pillow build with AVIF support), and that browsers may not display.
HEIC_EXTENSIONS = (".heic", ".heif", ".hif", ".avif")

# The folder holding the scripts (and the templates/static folders).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Paths

def settings_base():
    """Folder holding config.json and data/.

    The SETTINGS_BASE environment variable moves both out of the script
    folder. It is read on every call.
    """
    return os.path.abspath(os.environ.get("SETTINGS_BASE") or BASE_DIR)


def config_path():
    return os.path.join(settings_base(), "config.json")


def data_dir():
    return os.path.join(settings_base(), "data")


def db_path():
    """The current database (data/index.db)."""
    return os.path.join(data_dir(), "index.db")


def legacy_db_path():
    """The database written by older versions; only ever read."""
    return os.path.join(data_dir(), "images.db")


def legacy_thumb_dir():
    """The thumbnail shard folder written by older versions; only ever read."""
    return os.path.join(data_dir(), "thumbs")


def thumbs_db_path():
    return os.path.join(data_dir(), "thumbs.db")


def previews_dir():
    return os.path.join(data_dir(), "previews")


def logs_dir():
    return os.path.join(data_dir(), "logs")


def tmp_dir():
    return os.path.join(data_dir(), "tmp")


def exports_dir():
    return os.path.join(data_dir(), "exports")


def reports_dir():
    return os.path.join(data_dir(), "reports")


def tiles_dir():
    return os.path.join(data_dir(), "tiles")


def abort_path():
    """Creating this file asks a running job to stop."""
    return os.path.join(data_dir(), "abort")


def lock_path():
    """Lock file held by whichever process is running jobs."""
    return os.path.join(data_dir(), "worker.lock")


def ensure_dir(path):
    """Create a folder (and its parents) if needed; return the path."""
    os.makedirs(path, exist_ok=True)
    return path


def write_file_atomic(path, data, tmp_path=None):
    """Write bytes (or UTF-8 text) to path so readers never see a partial file.

    The data goes to a temporary file in the same folder first, which then
    replaces the target with os.replace. On Windows the replace can fail for
    a moment while another process has the target open, so it is retried
    briefly before giving up.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if tmp_path is None:
        tmp_path = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Config

DEFAULT_CONFIG = {
    "endpoint": {"kind": "openai", "base_url": "", "api_key": "", "model": ""},
    "helper": "",
    "reader_threads": 6,
    "purge_after_days": 30,
    "libraries": [],
}

# Filled into every library that does not set them. "id" is not here: ids
# are assigned by ensure_library_ids and must then be saved.
LIBRARY_DEFAULTS = {
    "name": "",
    "path": "",
    "ignore": [],
    "default_ignores": True,
    "enabled": True,
    "previews": True,
    "newest_first": True,
    "endpoint": "",
    "metadata": "core",
    "context_notes": "",
    "export": {},
}

LIBRARY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_config_lock = threading.Lock()


class ConfigError(ValueError):
    """config.json exists but could not be read."""


def default_config():
    return copy.deepcopy(DEFAULT_CONFIG)


def _merge_endpoint(stored_endpoint):
    endpoint = dict(DEFAULT_CONFIG["endpoint"])
    if isinstance(stored_endpoint, dict):
        endpoint.update(stored_endpoint)
    return endpoint


def apply_library_defaults(library):
    """Fill missing library settings in place and return the library."""
    for key, value in LIBRARY_DEFAULTS.items():
        if key not in library:
            library[key] = copy.deepcopy(value)
    if not isinstance(library.get("path"), str):
        library["path"] = ""
    if not isinstance(library.get("name"), str) or not library["name"].strip():
        library["name"] = default_library_name(library["path"])
    return library


_warned_encoding = set()


def _read_config_text(path):
    """The text of config.json: UTF-8 (with or without a BOM).

    Older versions read the file in the system's encoding, so a file saved
    that way by hand (for example "ANSI" on Windows) is still accepted, with
    a one-time warning; the next save writes it as UTF-8.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        encoding = locale.getpreferredencoding(False) or "ascii"
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            raise ConfigError("Could not read %s: it is not UTF-8 text (%s)" % (path, error))
        if path not in _warned_encoding:
            _warned_encoding.add(path)
            print("Warning: %s is not UTF-8; it was read as %s. Save it as UTF-8 (the "
                  "next change made by these tools does that)." % (path, encoding),
                  file=sys.stderr)
        return text


def load_config():
    """Read config.json (UTF-8, with or without a BOM) and fill in defaults.

    Never writes. Returns the defaults when the file does not exist yet.
    """
    config = default_config()
    path = config_path()
    if not os.path.exists(path):
        return config
    try:
        stored = json.loads(_read_config_text(path))
    except OSError as error:
        raise ConfigError("Could not read %s: %s" % (path, error))
    except ValueError as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError("Could not read %s: %s" % (path, error))
    if not isinstance(stored, dict):
        raise ConfigError("Could not read %s: the top level is not an object" % path)
    config.update(stored)
    # Endpoint keys are merged separately so older config files keep working.
    # A list of endpoints (round robin) merges the defaults into each item.
    stored_endpoint = stored.get("endpoint", {})
    if isinstance(stored_endpoint, list):
        config["endpoint"] = [_merge_endpoint(item) for item in stored_endpoint]
    else:
        config["endpoint"] = _merge_endpoint(stored_endpoint)
    libraries = config.get("libraries")
    if not isinstance(libraries, list):
        libraries = []
    config["libraries"] = [apply_library_defaults(library)
                           for library in libraries if isinstance(library, dict)]
    return config


def save_config(config):
    """Write config.json atomically (indented JSON).

    Non-ASCII characters are written as \\u escapes, so the file is plain
    ASCII: older versions of these tools, which read it in the system's
    encoding, still see library names and folders correctly. It is valid
    UTF-8 all the same, and hand-typed UTF-8 is read as well.
    """
    path = config_path()
    ensure_dir(os.path.dirname(path))
    text = json.dumps(config, indent=4, ensure_ascii=True) + "\n"
    # The temporary name matches the config.*.json ignore rule, so a crash
    # mid-write never leaves an unignored copy of the API keys behind.
    tmp_path = os.path.join(os.path.dirname(path), "config.tmp-%d.json" % os.getpid())
    with _config_lock:
        write_file_atomic(path, text, tmp_path)


def new_library_id(existing=()):
    """Return a fresh random library id not in existing."""
    while True:
        candidate = "lib_" + secrets.token_hex(4)
        if candidate not in existing:
            return candidate


def ensure_library_ids(config):
    """Give every library a unique, valid id. Returns True if any changed.

    Callers save the config when this returns True: an id that was handed out
    but not saved would be a different id on the next run.
    """
    changed = False
    seen = set()
    for library in config.get("libraries", []):
        library_id = library.get("id")
        if (not isinstance(library_id, str) or not LIBRARY_ID_RE.match(library_id)
                or library_id in seen):
            library["id"] = new_library_id(seen)
            changed = True
        seen.add(library["id"])
    return changed


def _folder_key(library):
    path = library.get("path")
    if not isinstance(path, str) or not path.strip():
        return ""
    return os.path.normcase(os.path.normpath(os.path.expanduser(path.strip())))


def _name_key(library):
    name = library.get("name")
    return name.strip().casefold() if isinstance(name, str) else ""


def copy_library_ids(target, source, only_missing=False, keep=()):
    """Give libraries in target the ids their counterparts in source have.

    Used so that two copies of the settings (say, one loaded before another
    process assigned ids and saved them) never end up with different ids for
    the same library: everything indexed under the other id would become an
    orphan. A target library is matched to a source library with the same
    folder, or else the same name, whose id no other target library uses.

    Only target libraries without a valid id change when only_missing is
    True; otherwise also those whose id is not in source. Libraries whose id
    is in keep never change. Returns True if any id changed.
    """
    source_libraries = [library for library in source.get("libraries", [])
                        if isinstance(library.get("id"), str)
                        and LIBRARY_ID_RE.match(library["id"])]
    source_ids = {library["id"] for library in source_libraries}
    libraries = target.get("libraries", [])
    taken = {library.get("id") for library in libraries}
    changed = False
    for library in libraries:
        library_id = library.get("id")
        valid = isinstance(library_id, str) and LIBRARY_ID_RE.match(library_id)
        if valid and (only_missing or library_id in source_ids or library_id in keep):
            continue
        match = None
        for key in (_folder_key, _name_key):
            value = key(library)
            if value:
                match = next((candidate for candidate in source_libraries
                              if candidate["id"] not in taken and key(candidate) == value),
                             None)
            if match is not None:
                break
        if match is not None:
            library["id"] = match["id"]
            taken.add(match["id"])
            changed = True
    return changed


def library_root(library):
    """The folder of a library: its path with ~ expanded ('' if none is set).

    Relative paths stay relative to the current directory, as before.
    """
    path = library.get("path") if isinstance(library, dict) else None
    if not isinstance(path, str) or not path.strip():
        return ""
    return os.path.normpath(os.path.expanduser(path.strip()))


# A leading date: a year (1900-2099), optionally with month and day, using one
# consistent separator (or none) between the parts.
_DATE_PREFIX_RE = re.compile(
    r"^(?:19|20)\d\d"
    r"(?:(?P<sep>[-_. ]?)(?P<month>0[1-9]|1[0-2])"
    r"(?:(?P=sep)(?:0[1-9]|[12]\d|3[01]))?)?"
    r"(?!\d)(?P<rest>.*)$", re.S)
_DASHES = chr(0x2013) + chr(0x2014)     # en and em dash
_NAME_SEPARATORS_RE = re.compile(r"[\s\-_.," + _DASHES + "]*")


def _strip_date_prefix(name):
    match = _DATE_PREFIX_RE.match(name)
    if not match:
        return name
    rest = match.group("rest")
    separator = _NAME_SEPARATORS_RE.match(rest).group(0)
    if rest and not separator:
        # "1999ers" is a name, not a date.
        return name
    if match.group("month") is None and not re.search("[-_." + _DASHES + "]", separator):
        # A bare year needs an explicit separator ("2014 - Trip"), so names
        # such as "2001 Space Odyssey" are left alone.
        return name
    return rest[len(separator):]


def default_library_name(folder):
    """Suggest a library name from a folder path.

    Uses the last path component with a leading date removed, for example
    "2014-10-24 - Garden", "2014-10 Garden" and "20141024_Garden" all become
    "Garden". Falls back to the plain folder name if nothing would be left.
    """
    parts = [part for part in re.split(r"[\\/]+", (folder or "").strip()) if part]
    if not parts:
        return ""
    base = parts[-1].strip()
    name = base
    for _ in range(3):
        stripped = _strip_date_prefix(name).strip()
        if stripped == name:
            break
        name = stripped
    return name or base


def get_library(config, id_or_name):
    """Find a library by id, or else by name (case-insensitive). None if absent."""
    if not id_or_name:
        return None
    libraries = config.get("libraries", [])
    for library in libraries:
        if library.get("id") == id_or_name:
            return library
    wanted = str(id_or_name).casefold()
    for library in libraries:
        if str(library.get("name", "")).casefold() == wanted:
            return library
    return None


def enabled_libraries(config):
    """The libraries that are not disabled, in config order."""
    return [library for library in config.get("libraries", [])
            if library.get("enabled", True)]


def library_label(library):
    """A short human-readable name for a library, for messages."""
    return (library.get("name") or default_library_name(library.get("path", ""))
            or library.get("id") or "(unnamed)")


# ---------------------------------------------------------------------------
# File types and ignore patterns

def is_image_file(name):
    return name.lower().endswith(IMAGE_EXTENSIONS)


def is_heic_like(name):
    """True for HEIC/HEIF/AVIF names, which need a plugin and browser conversion."""
    return name.lower().endswith(HEIC_EXTENSIONS)


# Junk that NAS boxes and operating systems leave in photo folders. Each
# entry is used both at the library root and at any depth.
_DEFAULT_IGNORE_NAMES = (
    "@eaDir", ".@__thumb", "#recycle", "$RECYCLE.BIN", ".Trashes",
    ".thumbnails", "._*", "Thumbs.db",
)
DEFAULT_IGNORES = tuple(
    pattern for name in _DEFAULT_IGNORE_NAMES for pattern in (name, "*/" + name))


def normalize_ignore(patterns):
    """Return a clean list of ignore patterns with forward-slash separators.

    Either separator (/ or backslash) is accepted in the config so the same
    library entry works on every platform. Leading "./" or "/" and trailing
    "/" are dropped, since patterns are matched against relative paths.
    """
    result = []
    if isinstance(patterns, str):
        patterns = [patterns]
    for pattern in patterns or []:
        if not isinstance(pattern, str):
            continue
        pattern = re.sub("/+", "/", pattern.strip().replace("\\", "/"))
        while pattern.startswith("./"):
            pattern = pattern[2:]
        pattern = pattern.strip("/")
        if pattern:
            result.append(pattern)
    return result


def ignore_patterns(library):
    """All ignore patterns for a library: its own plus the default ones."""
    patterns = normalize_ignore(library.get("ignore"))
    if library.get("default_ignores", True):
        patterns.extend(DEFAULT_IGNORES)
    return patterns


def matches_ignore(rel_path, patterns):
    """True if a relative path ('/' separators) matches any pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def should_prune_dir(rel_dir, patterns):
    """True if the walk should not descend into a directory at all.

    A directory is pruned only when a pattern names it (its relative path
    matches the pattern) or a pattern ending in "/*" covers everything inside
    it (the part before "/*" matches it). Patterns that merely could match
    some file inside, such as "*_*", do not prune; files are still matched
    one by one with matches_ignore.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(rel_dir, pattern):
            return True
        if pattern.endswith("/*") and fnmatch.fnmatch(rel_dir, pattern[:-2]):
            return True
    return False


# ---------------------------------------------------------------------------
# Helper module and file access

def resolve_config_relative(path):
    """Resolve a path from config.json: relative paths are relative to its folder."""
    if not path:
        return path
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(config_path()), path)
    return os.path.normpath(path)


_warned_helper = set()


def find_helper(path):
    """The file a helper path from config.json refers to, as (full path, tried).

    A relative path is looked up next to config.json first. Older versions
    took it relative to the current directory, so that, and then the program
    folder, are tried next (with a one-time warning). full is None when no
    candidate exists; tried lists every path that was checked.
    """
    first = resolve_config_relative(path)
    tried = [first]
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        for folder in (os.getcwd(), BASE_DIR):
            candidate = os.path.normpath(os.path.join(folder, expanded))
            if candidate not in tried:
                tried.append(candidate)
    for candidate in tried:
        if os.path.isfile(candidate):
            if candidate != first and path not in _warned_helper:
                _warned_helper.add(path)
                print("Warning: the helper %r is not next to config.json (%s); using %s. "
                      "Give an absolute path, or one relative to the folder holding "
                      "config.json." % (path, os.path.dirname(config_path()), candidate),
                      file=sys.stderr)
            return candidate, tried
    return None, tried


def load_helper(path):
    """Load an optional helper module from a file path, or return None.

    A relative path is resolved against the folder holding config.json (see
    find_helper for the older locations that are also accepted). Exits with
    a message listing the paths tried when the file cannot be found.
    """
    if not path:
        return None
    full, tried = find_helper(path)
    if full is None:
        raise SystemExit("Helper module not found: %s (looked for %s)"
                         % (path, ", ".join(tried)))
    spec = importlib.util.spec_from_file_location("image_db_helper", full)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_image_bytes(helper, path):
    """Return the raw bytes for a source image file.

    The helper module can supply a `load_image(path)` function that returns
    the bytes for a source image. If the helper is absent, has no such
    function, or that function returns None, the file is read from disk.
    """
    if helper is not None:
        func = getattr(helper, "load_image", None)
        if func is not None:
            data = func(path)
            if data is not None:
                return data
    with open(path, "rb") as handle:
        return handle.read()


def stat_path(helper, path):
    """Return an os.stat_result-like object, or None if the path is missing.

    The helper module can supply a `stat(path)` function returning an object
    with at least st_size, st_mtime, and st_ctime attributes. If the helper
    is absent, has no such function, or that function returns None, os.stat
    is used; a missing file is reported as None rather than raising.
    """
    if helper is not None:
        func = getattr(helper, "stat", None)
        if func is not None:
            result = func(path)
            if result is not None:
                return result
    try:
        return os.stat(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Keeping secrets out of logs and error messages

REDACTED = "***"

_REDACT_PATTERNS = (
    # Bearer <token>, wherever it appears
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"),
    # Authorization: <credentials> (including "Basic <credentials>")
    re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?(?:basic\s+)?)"
               r"(?!bearer\b)[^\s\"',;&}]+"),
    # x-api-key: <key>, "api_key": "<key>", api-key=<key>, apikey=<key>
    re.compile(r"(?i)((?:x-)?api[-_]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"),
    # ...?key=<key> or &access_token=<key> in URLs
    re.compile(r"(?i)([?&](?:key|token|access_token|api[-_]?key)=)[^&#\s\"']+"),
    # Common provider key shapes (sk-..., sk-ant-...) wherever they appear
    re.compile(r"(\b)sk-[A-Za-z0-9_-]{16,}"),
    # user:password@ in a URL
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@:\s\"']+:[^/@\s\"']+(?=@)"),
)


def redact(text, secrets_list=()):
    """Return text with API keys and similar secrets masked.

    Every value in secrets_list (short values under 4 characters are not
    treated as secrets) is replaced, and so are the usual credential shapes:
    "Bearer xxx", "x-api-key: xxx", "api_key": "xxx", "?key=xxx",
    "api-key=xxx" and the user:password part of a URL.
    """
    if text is None:
        return ""
    text = str(text)
    if isinstance(secrets_list, str):
        secrets_list = [secrets_list]
    values = sorted({value for value in (secrets_list or ())
                     if isinstance(value, str) and len(value.strip()) >= 4},
                    key=len, reverse=True)
    for value in values:
        text = text.replace(value, REDACTED)
        stripped = value.strip()
        if stripped != value:
            text = text.replace(stripped, REDACTED)
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
    return text
