#!/usr/bin/env python3
"""Library management shared by the web UI (admin.py) and settings.py.

Checking a library's settings (name, folder, ignore patterns, endpoint,
...), looking at folders without hanging on an offline network share
(every file system call that might block runs in a helper thread with a
time limit), previewing what ignore patterns skip, and moving or deleting
the indexed data of libraries that are no longer in the settings
("orphans").

Folders are only ever read here; nothing in a library folder is written.

This module uses only the standard library.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time

import cache
import common
import db
import embeddings
import jobs
import prompts
import videos

PATH_CHECK_TIMEOUT = 10.0   # seconds before a folder check gives up
SAMPLE_SECONDS = 3.0        # time spent counting pictures in a folder check
LIST_TIMEOUT = 10.0         # seconds before a folder listing gives up
LIST_LIMIT = 5000           # sub-folders listed at most
PREVIEW_SECONDS = 3.0       # time spent walking for an ignore preview
PREVIEW_TIMEOUT = 10.0
EXAMPLE_LIMIT = 10          # example paths in an ignore preview
NOTES_LIMIT = 2000          # characters of context notes
METADATA_PROFILES = ("core", "extended")
LIBRARY_FIELDS = ("name", "path", "enabled", "ignore", "default_ignores", "previews",
                  "newest_first", "endpoint", "metadata", "context_notes", "export", "refine",
                  "videos")
BOOL_FIELDS = ("enabled", "default_ignores", "previews", "newest_first", "videos")
# Folder names the folder picker does not offer (system and NAS folders).
HIDDEN_FOLDERS = {"system volume information", "@eadir", "#recycle", "#snapshot",
                  ".@__thumb", "lost+found"}
FULL_PATH_EXAMPLES = "C:\\Photos\\Family, \\\\server\\share\\Photos or /home/you/Pictures"
DEVICE_PATH_MESSAGE = ("Type the folder the usual way, without a device prefix such as "
                       "\\\\?\\: for example C:\\Photos\\Family or "
                       "\\\\server\\share\\Photos.")


class LibraryError(ValueError):
    """A library setting or operation cannot be used. fields maps setting names to messages."""

    def __init__(self, message, fields=None, status=400):
        ValueError.__init__(self, message)
        self.fields = dict(fields or {})
        self.status = status


class TimedOut(Exception):
    """A file system call did not finish within its time limit."""


def run_with_timeout(func, timeout, *args, **kwargs):
    """Call func in a helper thread and return its result; raise TimedOut after timeout seconds.

    Exceptions raised by func are raised here. A call that times out keeps
    running in its (daemon) thread until the operating system gives up;
    it never holds up the caller or the program's exit.
    """
    box = {}
    finished = threading.Event()

    def target():
        try:
            box["result"] = func(*args, **kwargs)
        except BaseException as error:
            box["error"] = error
        finally:
            finished.set()

    threading.Thread(target=target, name="library-ops", daemon=True).start()
    if not finished.wait(timeout):
        raise TimedOut("no answer within %g seconds" % timeout)
    if "error" in box:
        raise box["error"]
    return box["result"]


# ---------------------------------------------------------------------------
# Paths

def expand_path(path):
    """A folder typed by the user: stripped, unquoted, ~ expanded, separators normalised.

    Quotes around the path (Windows Explorer's "Copy as path" adds them)
    are not part of it: see common.unquote_path.
    """
    text = common.unquote_path(path)
    if not text:
        return ""
    full = os.path.normpath(os.path.expanduser(text))
    if os.name == "nt" and re.match(r"^[A-Za-z]:$", full):
        full += "\\"            # "C:" alone means the current folder on drive C
    return full


def is_device_path(path):
    """True for a Windows device path: two backslashes, then "?" or "." and a third one.

    They name the same folders as ordinary paths in a way the folder rules
    cannot compare, so libraries and the folder picker do not accept them.
    """
    if os.name != "nt" or not isinstance(path, str):
        return False
    text = common.unquote_path(path)
    return text[:2] in ("\\\\", "//") and text[2:3] in ("?", ".") and text[3:4] in ("\\", "/")


def unc_without_share(path):
    """True for a Windows network path that names a server but no share (\\\\server)."""
    if os.name != "nt" or not isinstance(path, str):
        return False
    # Device paths (two separators, then "?" or ".") are not network paths.
    if path[:2] not in ("\\\\", "//") or path[2:3] in ("?", "."):
        return False
    return len([part for part in re.split(r"[\\/]+", path) if part]) < 2


def is_full_path(path):
    """True for an absolute path: a drive or network share on Windows, '/...' elsewhere."""
    if not path:
        return False
    if os.name == "nt":
        if unc_without_share(path):
            return False
        drive, rest = os.path.splitdrive(path)
        if drive.startswith(("\\\\", "//")):
            return True
        return bool(drive) and rest.startswith(("\\", "/"))
    return os.path.isabs(path)


# \\localhost\C$\... (and the same through 127.0.0.1 or ::1) is drive C: itself.
_ADMIN_SHARE_RE = re.compile(r"^[\\/]{2}(localhost|127\.0\.0\.1|\[?::1\]?)[\\/]+([A-Za-z])\$"
                             r"(?=[\\/]|$)", re.I)


def _comparable(path):
    """path as is_within compares it: absolute, case-folded, admin shares as drives."""
    if os.name == "nt":
        path = _ADMIN_SHARE_RE.sub(lambda match: match.group(2) + ":", path)
        if re.match(r"^[A-Za-z]:$", path):
            path += "\\"        # "C:" alone means the current folder on drive C
    return os.path.normcase(os.path.abspath(path))


def is_within(path, folder):
    """True if path is folder or inside it (compared as text; no file system access)."""
    if not path or not folder:
        return False
    path = _comparable(path)
    folder = _comparable(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def path_error(path, require_absolute=True):
    """Why a folder cannot be used for a library, or None if it can.

    The folder must be set and must not be inside the data folder or the
    program folder. With require_absolute (the web UI, whose working
    folder means nothing to the user) it must also be a full path; other
    callers may keep relative paths, which are then not checked further.
    """
    text = common.unquote_path(path)
    if not text:
        return "Choose the folder that holds the pictures."
    if is_device_path(text):
        return DEVICE_PATH_MESSAGE
    full = expand_path(text)
    if unc_without_share(full):
        return "Type the share as well, such as \\\\server\\share."
    if not is_full_path(full):
        if require_absolute:
            return "Use a full path, such as %s." % FULL_PATH_EXAMPLES
        return None
    for folder, what in ((common.data_dir(), "the data folder"),
                         (common.BASE_DIR, "the program folder")):
        if is_within(full, folder):
            return ("This folder is inside %s (%s). Choose the folder where the pictures "
                    "are." % (what, folder))
    return None


def path_warnings(config, path, skip_id=None):
    """Things worth knowing about a library folder that do not stop it being used.

    Overlap with another library's folder (pictures would be indexed
    twice), and the data folder inside it (its previews would be indexed).
    """
    full = expand_path(path)
    if not is_full_path(full):
        return []
    warnings = []
    for library in config.get("libraries", []):
        if skip_id is not None and library.get("id") == skip_id:
            continue
        root = common.library_root(library)
        if root and is_full_path(root) and (is_within(full, root) or is_within(root, full)):
            warnings.append("This folder overlaps the folder of library %s (%s): pictures "
                            "in both are indexed twice." % (common.library_label(library), root))
    if is_within(common.data_dir(), full):
        warnings.append("The data folder %s is inside this folder; add an ignore pattern for "
                        "it so its previews are not indexed as pictures." % common.data_dir())
    return warnings


def parent_folder(path):
    """The folder above path, or '' at a drive, share or file system root."""
    _drive, rest = os.path.splitdrive(path)
    if not rest.strip("\\/"):
        return ""
    return os.path.dirname(path.rstrip("\\/")) or ""


# ---------------------------------------------------------------------------
# Library settings

def names_match(first, second):
    """True when two names are the same, ignoring case and surrounding spaces."""
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    return bool(first.strip()) and first.strip().casefold() == second.strip().casefold()


def library_name_error(config, name, skip=None):
    """Why name cannot be used for a library, or None if it can.

    Names must be non-empty and unique (ignoring case). skip is the library
    being renamed (its dict or its id), which may keep its own name.
    """
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        return "A library needs a name."
    skip_id = skip if isinstance(skip, str) else None
    for library in config.get("libraries", []):
        if library is skip or (skip_id is not None and library.get("id") == skip_id):
            continue
        if names_match(str(library.get("name", "")), name):
            return "Another library is already called %s." % library.get("name")
    return None


def parse_ignore(value):
    """Ignore patterns from a list of strings or text with one per line; None if neither."""
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return common.normalize_ignore(value)


def clean_library_fields(config, fields, library=None, require_absolute=True):
    """Check settings for a new library (library None) or changes to one.

    fields holds any of LIBRARY_FIELDS. A new library needs a path; its
    name defaults to common.default_library_name(path). Returns (changes,
    warnings): the cleaned values to store, and remarks that do not stop
    the change (see path_warnings). Raises LibraryError, with a message per
    field in .fields, when a value cannot be used. The id never changes.
    """
    if not isinstance(fields, dict):
        raise LibraryError("Send the library settings as an object.")
    errors = {}
    changes = {}
    unknown = sorted(set(fields) - set(LIBRARY_FIELDS) - {"id"})
    if unknown:
        raise LibraryError("Unknown library setting(s): %s." % ", ".join(unknown))
    if library is not None and "id" in fields and fields["id"] != library.get("id"):
        errors["id"] = "A library's id never changes."
    if "path" in fields or library is None:
        path = fields.get("path")
        problem = (path_error(path, require_absolute) if isinstance(path, str)
                   else "Choose the folder that holds the pictures.")
        if problem:
            errors["path"] = problem
        else:
            changes["path"] = common.unquote_path(path)
    if "name" in fields or library is None:
        name = fields.get("name")
        if name is not None and not isinstance(name, str):
            errors["name"] = "The name must be text."
        else:
            name = (name or "").strip()
            if not name and library is None:
                name = common.default_library_name(changes.get("path", ""))
            problem = library_name_error(config, name, skip=library)
            if problem:
                errors["name"] = problem
            else:
                changes["name"] = name
    for key in BOOL_FIELDS:
        if key in fields:
            if isinstance(fields[key], bool):
                changes[key] = fields[key]
            else:
                errors[key] = "Must be true or false."
    if "ignore" in fields:
        patterns = parse_ignore(fields["ignore"])
        if patterns is None:
            errors["ignore"] = "Ignore patterns must be text, one pattern per line."
        else:
            changes["ignore"] = patterns
    if "endpoint" in fields:
        wanted = fields["endpoint"]
        if wanted is None:
            wanted = ""
        if not isinstance(wanted, str):
            errors["endpoint"] = "The endpoint must be a name."
        elif wanted.strip() and not common.resolve_endpoints(config, wanted):
            errors["endpoint"] = "No enabled endpoint or group is called %s." % wanted.strip()
        else:
            endpoint = common.get_endpoint(config, wanted) if wanted.strip() else None
            changes["endpoint"] = endpoint["name"] if endpoint is not None else wanted.strip()
    if "metadata" in fields:
        if fields["metadata"] in METADATA_PROFILES:
            changes["metadata"] = fields["metadata"]
        else:
            errors["metadata"] = "Choose core or extended."
    if "context_notes" in fields:
        notes = fields["context_notes"]
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            errors["context_notes"] = "Context notes must be text."
        elif len(notes) > NOTES_LIMIT:
            errors["context_notes"] = "Keep context notes under %d characters." % NOTES_LIMIT
        else:
            changes["context_notes"] = notes.strip()
    if "refine" in fields:
        # The refine pass (see refine.py), replaced as a whole.
        import refine
        stored, problems = refine.clean(config, fields["refine"])
        if problems:
            errors.update(problems)
        else:
            changes["refine"] = stored
    if "export" in fields:
        # The saved export options, replaced as a whole (only the ones that
        # differ from the defaults are stored).
        import export       # imported here: a larger module most callers never need
        wanted = fields["export"] if fields["export"] is not None else {}
        try:
            changes["export"] = export.options_to_save(export.validate_options(wanted))
        except export.OptionsError as error:
            errors["export"] = "Export options that cannot be used: %s" % error
    if errors:
        raise LibraryError("Some settings cannot be used.", errors)
    warnings = []
    if "path" in changes:
        warnings = path_warnings(config, changes["path"],
                                 library.get("id") if library is not None else None)
    return changes, warnings


def reachability_warning(path, timeout=5.0):
    """A warning when a library folder cannot be reached right now, else None."""
    status = folder_status(path, timeout=timeout)
    if status["reachable"]:
        return None
    return ("%s It is saved anyway; scans skip it until it can be reached."
            % status["message"])


# ---------------------------------------------------------------------------
# Looking at folders

class FolderWalk:
    """The image files below a folder, for quick checks (not the scanner's walk).

    Iterate to get (rel_path, pruned_folder) pairs, depth first in name
    order; pruned_folder is the folder (relative path) that the patterns
    prune and that holds the file, or '' when no pattern prunes it. Pruned
    folders are only entered when into_pruned is set. Links are never
    followed. The walk stops at deadline (a time.monotonic() value); then
    truncated is True. readable is False when the folder itself cannot be
    listed.
    """

    def __init__(self, root, patterns=(), deadline=None, into_pruned=False):
        self.root = root
        self.patterns = list(patterns)
        self.deadline = deadline
        self.into_pruned = into_pruned
        self.truncated = False
        self.readable = True

    def __iter__(self):
        stack = [("", "")]
        while stack:
            if self.deadline is not None and time.monotonic() > self.deadline:
                self.truncated = True
                return
            rel_dir, pruned = stack.pop()
            folder = os.path.join(self.root, *rel_dir.split("/")) if rel_dir else self.root
            try:
                with os.scandir(folder) as listing:
                    entries = sorted(listing, key=lambda item: item.name)
            except OSError:
                if not rel_dir:
                    self.readable = False
                continue
            subdirs = []
            for entry in entries:
                rel = rel_dir + "/" + entry.name if rel_dir else entry.name
                try:
                    if common.is_link(entry):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        inner = pruned or (rel if common.should_prune_dir(rel, self.patterns)
                                           else "")
                        if inner and not self.into_pruned:
                            continue
                        subdirs.append((rel, inner))
                    elif (entry.is_file(follow_symlinks=False)
                          and common.is_image_file(entry.name)):
                        yield rel, pruned
                except OSError:
                    continue
            stack.extend(reversed(subdirs))


def missing_folder_message(full):
    """Why a library folder that is not there is not there, as far as can be told.

    On Windows, when its drive or network share answers, the folder itself
    is gone (moved, renamed or a typo); otherwise the drive or share is not
    connected. Elsewhere a share is a folder like any other, so it cannot
    be told apart.
    """
    if os.name == "nt":
        drive = os.path.splitdrive(full)[0]
        if drive and os.path.isdir(drive + "\\"):
            return "The folder does not exist (moved or renamed?)."
    return "The folder cannot be reached (offline?)."


def folder_status(path, timeout=None):
    """{"reachable", "message", "checked_at"} for a library folder. Never raises or hangs.

    Gives up after timeout seconds (default PATH_CHECK_TIMEOUT).
    """
    timeout = PATH_CHECK_TIMEOUT if timeout is None else timeout
    result = {"reachable": False, "message": "", "checked_at": time.time()}
    full = expand_path(path)
    if not full:
        result["message"] = "No folder is set."
        return result

    def look():
        if not os.path.isdir(full):
            return missing_folder_message(full)
        with os.scandir(full) as listing:
            next(listing, None)
        return ""

    try:
        message = run_with_timeout(look, timeout)
    except TimedOut:
        message = "The folder did not answer within %g seconds (an offline share?)." % timeout
    except OSError as error:
        message = "The folder cannot be read: %s" % (error.strerror or error)
    result.update(reachable=not message, message=message, checked_at=time.time())
    return result


def check_path(path, timeout=None, sample_seconds=None):
    """Whether a folder can be used as a library, with a quick count of its pictures.

    Returns {"path" (expanded), "exists", "readable", "is_dir",
    "sample_images" (image files found in at most sample_seconds, NAS
    system folders skipped), "more" (counting stopped early), "timed_out",
    "problem" (path_error, '' if none), "message"}. Never raises; gives up
    after timeout seconds (default PATH_CHECK_TIMEOUT; sample_seconds
    defaults to SAMPLE_SECONDS).
    """
    timeout = PATH_CHECK_TIMEOUT if timeout is None else timeout
    sample_seconds = SAMPLE_SECONDS if sample_seconds is None else sample_seconds
    full = expand_path(path)
    result = {"path": full, "exists": False, "readable": False, "is_dir": False,
              "sample_images": 0, "more": False, "timed_out": False,
              "problem": path_error(path) or "", "message": ""}
    if not full or is_device_path(path) or unc_without_share(full) or not is_full_path(full):
        result["message"] = result["problem"]
        return result

    def look():
        info = {"exists": os.path.exists(full), "is_dir": os.path.isdir(full)}
        if info["is_dir"]:
            walk = FolderWalk(full, common.DEFAULT_IGNORES,
                              deadline=time.monotonic() + sample_seconds)
            info["sample_images"] = sum(1 for rel, _pruned in walk
                                        if not common.matches_ignore(rel, common.DEFAULT_IGNORES))
            info["more"] = walk.truncated
            info["readable"] = walk.readable
        return info

    try:
        result.update(run_with_timeout(look, timeout))
    except TimedOut:
        result["timed_out"] = True
        result["message"] = ("The folder did not answer within %g seconds (an offline "
                             "network share?)." % timeout)
        return result
    except OSError as error:
        result["message"] = "The folder cannot be checked: %s" % (error.strerror or error)
        return result
    if not result["exists"]:
        result["message"] = "The folder does not exist or cannot be reached."
    elif not result["is_dir"]:
        result["message"] = "This is a file, not a folder."
    elif not result["readable"]:
        result["message"] = "The folder exists but cannot be read (permissions?)."
    elif result["more"]:
        result["message"] = ("Readable; at least %d pictures found (counting stopped after "
                             "%g seconds)." % (result["sample_images"], sample_seconds))
    else:
        result["message"] = "Readable; %d picture(s) found." % result["sample_images"]
    if result["problem"]:
        result["message"] = result["problem"] + " " + result["message"]
    return result


def _logical_drives():
    """Drive roots on Windows before Python 3.12 (os.listdrives), without touching them."""
    try:
        import ctypes
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:
        return ["C:" + os.sep]
    return [chr(ord("A") + index) + ":" + os.sep for index in range(26)
            if mask & (1 << index)]


def list_roots():
    """Where the folder picker starts: drive roots (Windows) or '/', plus the home folder."""
    if hasattr(os, "listdrives"):
        try:
            roots = list(os.listdrives())
        except OSError:
            roots = _logical_drives()
    elif os.name == "nt":
        roots = _logical_drives()
    else:
        roots = ["/"]
    return {"path": "", "parent": None, "dirs": roots, "roots": True, "truncated": False,
            "home": os.path.expanduser("~"),
            "hint": "Network shares are not listed; type one, such as \\\\server\\share."}


def _hidden_folder(name):
    return name.startswith((".", "$")) or name.casefold() in HIDDEN_FOLDERS


def list_folder(path, timeout=None, limit=None):
    """The sub-folders of a folder, for the folder picker. No files are listed.

    Returns {"path" (expanded), "parent" ('' at a root, meaning the list of
    roots), "dirs" (names, sorted), "truncated" (more than limit),
    "roots": False}. An empty path gives list_roots(), whose "dirs" are full
    paths. Raises LibraryError for a path that cannot be listed (status 504
    when it did not answer within timeout seconds, default LIST_TIMEOUT;
    limit defaults to LIST_LIMIT).
    """
    timeout = LIST_TIMEOUT if timeout is None else timeout
    limit = LIST_LIMIT if limit is None else limit
    if is_device_path(path):
        raise LibraryError(DEVICE_PATH_MESSAGE)
    full = expand_path(path)
    if not full:
        return list_roots()
    if unc_without_share(full):
        raise LibraryError("Type the share as well, such as \\\\server\\share.")
    if not is_full_path(full):
        raise LibraryError("Use a full path, such as %s." % FULL_PATH_EXAMPLES)

    def listing():
        names = []
        with os.scandir(full) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if _hidden_folder(entry.name):
                    continue
                if len(names) >= limit:
                    return names, True
                names.append(entry.name)
        return names, False

    try:
        names, truncated = run_with_timeout(listing, timeout)
    except TimedOut:
        raise LibraryError("The folder did not answer within %g seconds (an offline network "
                           "share?)." % timeout, status=504)
    except FileNotFoundError:
        raise LibraryError("That folder does not exist.")
    except NotADirectoryError:
        raise LibraryError("That is a file, not a folder.")
    except PermissionError:
        raise LibraryError("That folder cannot be read (permissions).")
    except OSError as error:
        raise LibraryError("That folder cannot be listed: %s" % (error.strerror or error))
    names.sort(key=lambda name: (name.casefold(), name))
    return {"path": full, "parent": parent_folder(full), "dirs": names,
            "truncated": truncated, "roots": False}


def ignore_preview(root, patterns, seconds=None, timeout=None, examples=EXAMPLE_LIMIT):
    """What ignore patterns would skip below root: a quick, time-limited sample.

    patterns is the full list (common.ignore_patterns, or normalize_ignore
    plus DEFAULT_IGNORES). Returns {"skipped" (image files the patterns
    exclude), "scanned" (image files seen), "examples" (up to examples
    relative paths; a pruned folder appears once, as "folder/"),
    "truncated" (the walk stopped after seconds), "readable"}. Raises
    LibraryError when the folder cannot be read or does not answer.
    seconds and timeout default to PREVIEW_SECONDS and PREVIEW_TIMEOUT.
    """
    seconds = PREVIEW_SECONDS if seconds is None else seconds
    timeout = PREVIEW_TIMEOUT if timeout is None else timeout
    full = expand_path(root)
    if not full or not is_full_path(full):
        raise LibraryError("The library has no usable folder.")

    def walk_it():
        walk = FolderWalk(full, patterns, deadline=time.monotonic() + seconds,
                          into_pruned=True)
        result = {"skipped": 0, "scanned": 0, "examples": [], "truncated": False,
                  "readable": True}
        shown = set()
        for rel, pruned in walk:
            result["scanned"] += 1
            if not pruned and not common.matches_ignore(rel, patterns):
                continue
            result["skipped"] += 1
            example = pruned + "/" if pruned else rel
            if example not in shown and len(result["examples"]) < examples:
                shown.add(example)
                result["examples"].append(example)
        result["truncated"] = walk.truncated
        result["readable"] = walk.readable
        return result

    try:
        result = run_with_timeout(walk_it, timeout)
    except TimedOut:
        raise LibraryError("The folder did not answer within %g seconds (an offline network "
                           "share?)." % timeout, status=504)
    if not result["readable"]:
        raise LibraryError("The folder cannot be read right now.")
    return result


class BackgroundChecks:
    """Results of slow checks (is a share online? is a model server up?), made in the background.

    get(key, func, *args) returns the last result for key, or None while
    the first check is still running; a result older than ttl seconds is
    refreshed in a daemon thread (the old one is returned meanwhile). A
    check running for longer than timeout seconds returns timed_out
    instead (when given), even when an older result exists: a share that
    stops answering is not shown as online until the operating system
    gives up. Requests wait for a check only when they ask to: wait
    seconds for the first result of a check that the call itself starts
    (one that is usually quick, such as reading a local file).
    """

    def __init__(self, ttl=60.0, timeout=10.0, max_running=8):
        self.ttl = ttl
        self.timeout = timeout
        self.max_running = max_running
        self._lock = threading.Lock()
        self._results = {}      # key -> (value, time.monotonic() when made)
        self._running = {}      # key -> (time.monotonic() when started, threading.Event)

    def get(self, key, func, *args, timed_out=None, wait=0):
        now = time.monotonic()
        started = False
        with self._lock:
            result = self._results.get(key)
            running = self._running.get(key)
            stale = result is None or now - result[1] > self.ttl
            if stale and running is None and len(self._running) < self.max_running:
                running = self._running[key] = (now, threading.Event())
                started = True
                threading.Thread(target=self._check, args=(key, func, args, running),
                                 name="background-check", daemon=True).start()
        if result is None and started and wait > 0 and running[1].wait(wait):
            with self._lock:
                result = self._results.get(key)
                running = self._running.get(key)
        if (running is not None and timed_out is not None
                and time.monotonic() - running[0] > self.timeout):
            return timed_out
        return result[0] if result is not None else None

    def _check(self, key, func, args, running):
        try:
            value = func(*args)
        except Exception:
            value = None
        with self._lock:
            # A check that forget() let go of may have seen the old state.
            if self._running.get(key) is running:
                del self._running[key]
                if value is not None:
                    self._results[key] = (value, time.monotonic())
        running[1].set()

    def forget(self, key=None):
        """Drop a remembered result (all of them without a key), so the next get checks again."""
        with self._lock:
            if key is None:
                self._results.clear()
                self._running.clear()
            else:
                self._results.pop(key, None)
                self._running.pop(key, None)


# ---------------------------------------------------------------------------
# Indexed data of libraries no longer in the settings

def orphans(conn, config, counts=None):
    """[{id, name, counts}] of library ids with data in the database but not in the settings.

    counts is db.library_counts() when the caller already has it. A library
    removed before anything was indexed leaves only its name behind; it is
    not listed (there is nothing to re-attach or delete).
    """
    names = db.library_names(conn)
    if counts is None:
        counts = db.library_counts(conn, prompts.PROMPT_VER)
    result = []
    for library_id in db.orphan_library_ids(conn, config):
        found = counts.get(library_id, {})
        if not (found.get("total") or found.get("missing")):
            continue
        result.append({"id": library_id, "name": names.get(library_id) or library_id,
                       "counts": found})
    return result


def _move_previews(previews, source_id, target_id):
    """Move a library's preview files to another library id. Returns how many were moved."""
    source = previews.library_dir(source_id)
    target = previews.library_dir(target_id)
    if not os.path.isdir(source):
        return 0
    moved = 0
    for bucket in sorted(os.listdir(source)):
        source_bucket = os.path.join(source, bucket)
        if not os.path.isdir(source_bucket):
            continue
        target_bucket = common.ensure_dir(os.path.join(target, bucket))
        for name in os.listdir(source_bucket):
            os.replace(os.path.join(source_bucket, name), os.path.join(target_bucket, name))
            moved += 1
    shutil.rmtree(source, ignore_errors=True)
    return moved


def reattach_orphan(conn, orphan_id, library_id, config, previews=None):
    """Move the indexed data of an orphan to a library in the settings.

    Refused (LibraryError) when orphan_id is not an orphan, library_id is
    not in the settings, or any of the orphan's files is already indexed
    under that library (same relative path). Descriptions, thumbnails,
    previews and the playable copies of videos move along; the orphan's
    name is forgotten. Run it only while no job is running. Returns
    {"moved" (rows), "previews_moved"}.
    """
    configured = {library.get("id") for library in config.get("libraries", [])}
    if library_id not in configured:
        raise LibraryError("Choose a library from the settings to re-attach to.",
                           {"library_id": "Unknown library."})
    if orphan_id in configured or orphan_id not in db.orphan_library_ids(conn, config):
        raise LibraryError("There is no unattached indexed data with that id.", status=404)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        clashes = conn.execute(
            "SELECT COUNT(*) FROM images AS old JOIN images AS new "
            "ON new.library_id = ? AND new.path = old.path WHERE old.library_id = ?",
            (library_id, orphan_id)).fetchone()[0]
        if clashes:
            raise LibraryError(
                "%d of its files are already indexed in that library, so the two cannot be "
                "merged. Delete one of them first." % clashes, status=409)
        moved = conn.execute("UPDATE images SET library_id = ? WHERE library_id = ?",
                             (library_id, orphan_id)).rowcount
        conn.execute("DELETE FROM read_failures WHERE library_id = ? AND path IN "
                     "(SELECT path FROM read_failures WHERE library_id = ?)",
                     (orphan_id, library_id))
        conn.execute("UPDATE read_failures SET library_id = ? WHERE library_id = ?",
                     (library_id, orphan_id))
        conn.execute("DELETE FROM libraries WHERE id = ?", (orphan_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # After the commit: a preview that did not move is rebuilt by the next
    # scan (the scanner checks that the files exist).
    previews_moved = _move_previews(previews or cache.PreviewStore(), orphan_id, library_id)
    videos.move_library_proxies(orphan_id, library_id)
    return {"moved": moved, "previews_moved": previews_moved}


def delete_library_data(conn, library_id, ctx=None, chunk=500):
    """Delete every row, thumbnail, preview and meaning vector of a library id (and its
    read failures), and the looks (looks.py) no other row needs.

    The playable copies of its videos (videos.py) go too.

    Works in chunks and stops early when ctx asks to (running it again
    finishes the work). Returns {"deleted", "previews_removed", "stopped"}.
    """
    ids = [row[0] for row in conn.execute("SELECT id FROM images WHERE library_id = ?",
                                          (library_id,))]
    if ctx is not None:
        ctx.stage("delete", total=len(ids))
    result = {"deleted": 0, "previews_removed": False, "stopped": False}
    with cache.ThumbStore() as thumbs:
        for start in range(0, len(ids), chunk):
            if ctx is not None:
                ctx.wait_if_paused()
                if ctx.should_stop():
                    result["stopped"] = True
                    return result
            part = ids[start:start + chunk]
            with conn:
                conn.execute("DELETE FROM images WHERE id IN (%s)" % ",".join("?" * len(part)),
                             part)
            thumbs.delete(part)
            embeddings.forget(part)         # (their meaning vectors: search by meaning)
            result["deleted"] += len(part)
            if ctx is not None:
                ctx.step(len(part))
    import looks
    looks.forget_unreferenced(conn)     # (the looks of pictures no row has any more)
    result["previews_removed"] = cache.PreviewStore().delete_library(library_id)
    videos.delete_library_proxies(library_id)
    with conn:
        conn.execute("DELETE FROM read_failures WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
    return result


def delete_library_job(ctx, conn, config, params):
    """The delete_library job: delete an orphan's data after checking the typed name.

    params["confirm_name"] must match the name stored in the database (or
    the id, for data without a stored name), and the library must no
    longer be in the settings.
    """
    library_id = params.get("library_id")
    if not library_id:
        raise jobs.JobError("No library was given.")
    for library in config.get("libraries", []):
        if library.get("id") == library_id:
            raise jobs.JobError("Library %s is in the settings; remove it there first."
                                % common.library_label(library))
    stored = db.library_names(conn).get(library_id) or library_id
    if not names_match(params.get("confirm_name"), stored):
        raise jobs.JobError("The name typed to confirm does not match %s; nothing was deleted."
                            % stored)
    ctx.log("Deleting the indexed data of %s (%s)." % (stored, library_id))
    result = delete_library_data(conn, library_id, ctx)
    if not result["stopped"]:
        ctx.log("Deleted %d image(s) of %s." % (result["deleted"], stored))
    result["name"] = stored
    return result


# ---------------------------------------------------------------------------
# One picture's file

REVEAL_TIMEOUT = 5.0        # seconds to look for a file before showing it


def library_file(root, rel_path):
    """The full path of a picture from its library folder and its stored relative path.

    None when either is empty or the relative path would lead outside the
    folder (an absolute path, a drive, or ".." parts): the result is always
    inside root.
    """
    if not root or not isinstance(rel_path, str) or not rel_path:
        return None
    parts = [part for part in rel_path.replace("\\", "/").split("/") if part not in ("", ".")]
    # (A colon is part of some file names elsewhere, but on Windows it
    # names a drive or a data stream.)
    if not parts or any(part == ".." or (os.name == "nt" and ":" in part) for part in parts):
        return None
    full = os.path.normpath(os.path.join(root, *parts))
    if not is_within(full, root) or _comparable(full) == _comparable(root):
        return None
    return full


def reveal_command(path, platform=None):
    """The command that shows a file in the system's file manager.

    Windows: Explorer with the file selected (a command line, quoted the
    way Explorer reads it). macOS: Finder with the file selected. Others:
    the file's folder, opened with xdg-open.
    """
    platform = platform or sys.platform
    if platform == "win32":
        return 'explorer /select,"%s"' % path
    if platform == "darwin":
        return ["open", "-R", path]
    return ["xdg-open", os.path.dirname(path) or os.sep]


def reveal_file(path, timeout=REVEAL_TIMEOUT):
    """Show a picture's file in the file manager (Explorer, Finder or the folder).

    The file must exist; a folder that does not answer within timeout
    seconds (an offline share) raises TimedOut. Raises LibraryError (404)
    for a missing file and (501) when the file manager cannot be started.
    Returns at once: the file manager is not waited for.
    """
    if not run_with_timeout(os.path.isfile, timeout, path):
        raise LibraryError("The file is not there any more: %s" % path, status=404)
    return _start_file_manager(reveal_command(path))


def open_folder_command(path, platform=None):
    """The command that opens a folder in the system's file manager."""
    platform = platform or sys.platform
    if platform == "win32":
        return ["explorer", path]
    if platform == "darwin":
        return ["open", path]
    return ["xdg-open", path]


def open_folder(path, timeout=REVEAL_TIMEOUT):
    """Open a folder in the file manager (Explorer, Finder, xdg-open); like reveal_file."""
    if not run_with_timeout(os.path.isdir, timeout, path):
        raise LibraryError("The folder is not there any more: %s" % path, status=404)
    return _start_file_manager(open_folder_command(path))


def _start_file_manager(command):
    try:
        process = subprocess.Popen(command, close_fds=True)
    except OSError as error:
        raise LibraryError("The file manager could not be started: %s" % error, status=501)
    # Collect the exit status when it ends, so no zombie or warning is left.
    threading.Thread(target=process.wait, name="reveal-file", daemon=True).start()
    return process
