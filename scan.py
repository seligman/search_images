#!/usr/bin/env python3
"""Scan image libraries, then ask the LLM to describe new images.

For each enabled library this lists the image files and reads every new or
changed one exactly once. That single read gives the thumbnail (stored in
data/thumbs.db), a 2048 px preview (data/previews/; for a 360-degree photo
also a 4096 px panorama for the 360 view), the cleaned EXIF data, dHash,
content hash, capture date, camera and GPS position, all stored in
data/index.db. Unchanged files are not read again.

Descriptions are never thrown away by accident:
- a file that is touched, moved or renamed keeps its description, and an
  exact copy of a described image gets the same description (matched by
  content hash), also when files swap names or an edited file's original is
  kept under another name;
- a file that disappears is only marked missing (missing_since). It is
  removed after purge_after_days (30 by default) and comes back as it was if
  the file returns first;
- nothing is marked missing when the library folder is offline, when the
  walk stopped early, under a folder that could not be listed or a linked
  folder that is not followed, or when most of a library would vanish at
  once (unless --allow-mass-removal is given). Files that merely moved
  within the library, and files now excluded by ignore patterns, do not
  count towards that limit.
- Libraries that are no longer in config.json are never touched.
- A hidden image stays hidden when it is moved, renamed or copied within its
  library (a new copy is shown when another copy is shown there: the owner
  kept that one), and a shown image stays shown.

Then the describe step (describe.py) runs for images without a description.

Usage:
    python scan.py [--library NAME_OR_ID ...] [--no-scan] [--no-describe]
                   [--newest-first | --oldest-first] [--retry-failed]
                   [--redescribe-stale] [--limit N] [--endpoint NAME]
                   [--allow-mass-removal] [--verbose]
    python scan.py --refine [--library NAME_OR_ID ...] [--limit N] [--retry-failed]
    python scan.py --list-endpoints
    python scan.py --test-endpoint NAME [--image PATH]

--refine only runs the refine pass (describe.refine_pending): documents and
screenshots described again with each library's refine endpoint.

To stop a long run early, create data/abort (or a file named "abort" or
"abort.txt" in the current directory). Work done so far is kept; run again to
continue. Only one scan (or server job) runs at a time.
"""

import argparse
import collections
import concurrent.futures
import json
import os
import sys
import time

import cache
import common
import db
import describe
import image_edits
import images
import jobs
import llm
import not_duplicates
import places

COMMIT_ROWS = 100           # commit after this many written rows...
COMMIT_SECONDS = 3.0        # ...or this many seconds, whichever comes first
# While waiting this long for a read with writes pending, they are committed,
# so a slow share (or a pause) never keeps other writers out of the database.
FLUSH_WAIT = 0.5
MTIME_TOLERANCE = 1.0       # seconds; some file systems round modification times
# Refuse to mark more than this share of a library missing in one scan, once
# more than MASS_REMOVAL_MIN files would be affected.
MASS_REMOVAL_FRACTION = 0.5
MASS_REMOVAL_MIN = 20
DAY = 86400.0
MISSING_LOG_LIMIT = 20      # log at most this many missing paths one by one
FILE_LOG_LIMIT = 20         # per library, "Added ..." (etc.) lines beyond this are only counted
# New content at a path within this many dHash bits of the old content is the
# same picture retouched: the owner's edit stays with it, even when the old
# content turns up under another name.
RETOUCHED_BITS = 12
LIST_LOG_SECONDS = 10.0     # progress line while listing a large library


# ---------------------------------------------------------------------------
# Listing files

def _error_text(error):
    text = str(error) or type(error).__name__
    return text[:300]


class LibraryWalk:
    """The image files below a library root.

    Iterate to get (rel_path, size, mtime, ctime) for each image file that no
    ignore pattern excludes; rel_path is relative to the root and uses "/".
    Ignored folders are not entered at all (see common.should_prune_dir).
    Symlinks and junctions -- to folders or files -- are not followed; they
    are listed in links instead.

    When the helper module has walk_library(root) and it returns an iterator
    (the os.walk contract), that is used together with common.stat_path.
    Otherwise the folder is listed with os.scandir, which needs the root to
    be an existing folder; a helper stat(path) hook, if there is one, still
    supplies sizes and times. After iterating:

        reachable   False if the root could not be listed at all (offline)
        errors      [(rel_path, message)] for folders (or files) that could not
                    be read; rel_path '' means the root itself
        links       [rel_path] of linked folders and files that were skipped
        stopped     True if ctx asked to stop part-way through
        complete    True when every folder was listed
    """

    def __init__(self, root, patterns, helper=None, ctx=None, label=""):
        self.root = root
        self.patterns = list(patterns or [])
        self.helper = helper
        self.ctx = ctx
        self.label = label or root
        self.reachable = True
        self.stopped = False
        self.errors = []
        self.links = []
        self.helper_walk = False
        self.folders = 0
        self.files = 0
        self._last_report = time.monotonic()

    @property
    def complete(self):
        return (self.reachable and not self.stopped
                and not any(rel == "" for rel, _ in self.errors))

    def _error(self, rel, error):
        self.errors.append((rel, _error_text(error)))

    def _stop_requested(self):
        if self.ctx is not None:
            # Pausing here is safe: nothing is waiting to be written while
            # folders are listed.
            self.ctx.wait_if_paused()
            if self.ctx.should_stop():
                self.stopped = True
        return self.stopped

    def _folder_done(self):
        """Count a listed folder; now and then say how far the listing has got."""
        self.folders += 1
        now = time.monotonic()
        if self.ctx is not None and now - self._last_report >= LIST_LOG_SECONDS:
            self._last_report = now
            self.ctx.log("Listing %s: %d folders, %d image files so far"
                         % (self.label, self.folders, self.files))

    def __iter__(self):
        walker = getattr(self.helper, "walk_library", None) if self.helper is not None else None
        try:
            iterator = walker(self.root) if walker is not None else None
        except Exception as error:
            self.helper_walk = True
            self._error("", error)
            return iter(())
        if iterator is None:
            # Also when a helper's walk_library returns None: the normal
            # walk, which checks that the root folder is there.
            return self._scandir_walk()
        self.helper_walk = True
        return self._helper_walk(iterator)

    def _scandir_walk(self):
        if not self.root or not os.path.isdir(self.root):
            self.reachable = False
            return
        helper_stat = getattr(self.helper, "stat", None) if self.helper is not None else None
        stack = [""]
        while stack:
            if self._stop_requested():
                return
            rel_dir = stack.pop()
            folder = os.path.join(self.root, *rel_dir.split("/")) if rel_dir else self.root
            try:
                with os.scandir(folder) as listing:
                    entries = sorted(listing, key=lambda item: item.name)
            except OSError as error:
                self._error(rel_dir, error)
                continue
            subdirs = []
            for entry in entries:
                rel = rel_dir + "/" + entry.name if rel_dir else entry.name
                try:
                    link = common.is_link(entry)
                    # For a link, what it points to decides which patterns apply.
                    is_dir = entry.is_dir(follow_symlinks=not link)
                    if is_dir and common.should_prune_dir(rel, self.patterns):
                        continue
                    if not is_dir and (not common.is_image_file(entry.name)
                                       or common.matches_ignore(rel, self.patterns)):
                        continue
                    if link:
                        self.links.append(rel)
                        continue
                    if is_dir:
                        subdirs.append(rel)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = helper_stat(entry.path) if helper_stat is not None else None
                    if stat is None:
                        stat = entry.stat(follow_symlinks=False)
                except OSError as error:
                    self._error(rel, error)
                    continue
                self.files += 1
                yield rel, stat.st_size, stat.st_mtime, getattr(stat, "st_ctime", 0.0)
            self._folder_done()
            # Depth first, in name order.
            stack.extend(reversed(subdirs))

    def _relative(self, dirpath):
        try:
            rel = os.path.relpath(dirpath, self.root)
        except ValueError:
            return None
        rel = rel.replace(os.sep, "/").replace("\\", "/")
        if rel == ".":
            return ""
        if rel == ".." or rel.startswith("../"):
            return None
        return rel

    def _helper_walk(self, iterator):
        try:
            for dirpath, dirnames, filenames in iterator:
                if self._stop_requested():
                    return
                rel_dir = self._relative(dirpath)
                if rel_dir is None:
                    continue
                prefix = rel_dir + "/" if rel_dir else ""
                dirnames[:] = [name for name in dirnames
                               if not common.should_prune_dir(prefix + name, self.patterns)]
                for filename in sorted(filenames):
                    rel = prefix + filename
                    if not common.is_image_file(filename) or common.matches_ignore(
                            rel, self.patterns):
                        continue
                    stat = common.stat_path(self.helper, os.path.join(dirpath, filename))
                    if stat is None:
                        self._error(rel, "cannot read the file's size and time")
                        continue
                    self.files += 1
                    yield rel, stat.st_size, stat.st_mtime, getattr(stat, "st_ctime", 0.0)
                self._folder_done()
        except Exception as error:
            # The helper's walk broke off: whatever it did not list must not
            # look deleted.
            self._error("", error)


def walk_library(root, patterns, helper=None, ctx=None, label=""):
    """Return a LibraryWalk over a library root (see LibraryWalk)."""
    return LibraryWalk(root, patterns, helper, ctx, label)


def excluded_by_ignores(path, patterns):
    """True if a relative path is skipped by the ignore patterns (itself or a folder above it)."""
    if common.matches_ignore(path, patterns):
        return True
    parts = path.split("/")
    return any(common.should_prune_dir("/".join(parts[:depth]), patterns)
               for depth in range(1, len(parts)))


def _looks_alike(first, second):
    """True when two dHashes (16 hex digits) are within RETOUCHED_BITS of each other."""
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1") <= RETOUCHED_BITS
    except (TypeError, ValueError):
        return False


def _under_any(path, prefixes):
    """True if path is one of prefixes, or inside one of them ('' covers everything)."""
    if "" in prefixes or path in prefixes:
        return True
    parts = path.split("/")
    return any("/".join(parts[:depth]) in prefixes for depth in range(1, len(parts)))


# ---------------------------------------------------------------------------
# Reading files in parallel

# Older name; the helper wrapper now lives in common.
thread_safe_helper = common.thread_safe_helper


def reader_threads(config):
    """The number of reader threads to use (config "reader_threads", 1..32)."""
    try:
        count = int(config.get("reader_threads") or 6)
    except (TypeError, ValueError):
        count = 6
    return max(1, min(32, count))


def _parallel_reads(threads, items, read, handle, should_stop, ctx, flush=None):
    """read(item) on a thread pool for each item; handle(item, result) on this thread.

    At most 2 x threads reads are in flight, so memory stays bounded and the
    results are written as they arrive. When should_stop() turns true no
    new reads start; those already running finish and are handled. flush(),
    when given, is called before this thread waits for a while (a slow read,
    a pause), so pending writes are committed rather than holding the
    database's write lock. Returns True if it stopped early.
    """
    in_flight = {}

    def drain():
        done, _ = concurrent.futures.wait(
            in_flight, timeout=FLUSH_WAIT, return_when=concurrent.futures.FIRST_COMPLETED)
        if not done:
            if flush is not None:
                flush()
            done, _ = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
        for future in done:
            handle(in_flight.pop(future), future.result())

    stopped = False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=threads)
    try:
        for item in items:
            if should_stop():
                stopped = True
                break
            if ctx.paused and flush is not None:
                flush()
            ctx.wait_if_paused()
            while len(in_flight) >= 2 * threads:
                drain()
            in_flight[pool.submit(read, item)] = item
        while in_flight:
            drain()
    finally:
        pool.shutdown(wait=True)
    return stopped or should_stop()


def _missing_decoder(error):
    """True when a read failed only because an optional decoder is not installed."""
    text = str(error)
    return images.HEIF_MISSING in text or images.AVIF_MISSING in text


def _read_original(root, rel, helper, want_previews, mtime):
    """Worker thread: images.read_image() for one file. Returns (result, error); never raises.

    With previews, a 360-degree photo also gets its pano (see cache.PreviewStore).
    """
    full = os.path.join(root, *rel.split("/"))
    try:
        return images.read_image(full, helper, want_preview=want_previews, mtime=mtime,
                                 want_pano=want_previews), None
    except Exception as error:
        return None, error


def _failed_before(failures, rel, size, mtime):
    """True if the file could not be read last time and has not changed since."""
    failure = failures.get(rel)
    return (failure is not None and failure["size"] == size
            and abs((failure["mtime"] or 0) - mtime) < MTIME_TOLERANCE)


def _load_failures(conn, library_id):
    return {row["path"]: row for row in conn.execute(
        "SELECT path, size, mtime FROM read_failures WHERE library_id = ?", (library_id,))}


def _record_read_failure(conn, library_id, rel, size, mtime, error):
    """Remember a file that could not be read, so it is skipped until it changes."""
    conn.execute(
        "INSERT OR REPLACE INTO read_failures (library_id, path, size, mtime, error, seen) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (library_id, rel, size, mtime, _error_text(error), time.time()))


def _update_row(conn, image_id, values, extra=""):
    """UPDATE one images row with a dict of column values (extra: more SQL assignments)."""
    columns = sorted(values)
    assignments = ", ".join("%s = ?" % column for column in columns)
    if extra:
        assignments += ", " + extra
    conn.execute("UPDATE images SET %s WHERE id = ?" % assignments,
                 [values[column] for column in columns] + [image_id])


def _delete_preview(previews, library_id, image_id, ctx):
    """Remove a preview file and its pano (best effort; a failure is only logged)."""
    try:
        previews.delete_both(library_id, image_id)
    except OSError as error:
        ctx.log("Could not delete the preview of image %d: %s"
                % (image_id, _error_text(error)), "warn")


def _store_pano(previews, library_id, image_id, pano, ctx, rel):
    """After a new preview was written: write its pano, or remove an older one.

    pano is the read_image() result's pano (None when the picture is not a
    360 photo). Returns True if a pano was written. A pano that cannot be
    written is removed rather than left showing older content; the server
    makes it again when it is asked for.
    """
    try:
        if pano:
            previews.write_pano(library_id, image_id, pano)
            return True
        previews.delete_pano(library_id, image_id)
    except OSError as error:
        try:
            previews.delete_pano(library_id, image_id)
        except OSError:
            pass
        ctx.log("Could not save the 360-degree panorama of %s: %s"
                % (rel, _error_text(error)), "warn")
    return False


class _CommitTimer:
    """Commits thumbs.db and index.db every COMMIT_ROWS writes or COMMIT_SECONDS."""

    def __init__(self, conn, thumbs):
        self.conn = conn
        self.thumbs = thumbs
        self.pending = 0
        self.last = time.monotonic()

    def wrote(self, count=1):
        self.pending += count
        if self.pending >= COMMIT_ROWS or time.monotonic() - self.last >= COMMIT_SECONDS:
            self.commit()

    def commit(self):
        # Thumbnails first: a committed row then always has its thumbnail, or
        # is read again on the next scan.
        self.thumbs.commit()
        self.conn.commit()
        self.pending = 0
        self.last = time.monotonic()

    def flush(self):
        """Commit now if anything is waiting (before this thread blocks)."""
        if self.pending:
            self.commit()


def _metadata(result):
    """Column values for the image facts returned by images.read_image().

    With place names (places.py) they include the place of its GPS
    position; without, the place is left to be looked up later.
    """
    try:
        exif = json.dumps(result["exif"], ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        exif = "{}"
    values = {
        "width": int(result["width"]), "height": int(result["height"]),
        "content_hash": result["content_hash"], "dhash": result["dhash"],
        "taken_at": result["taken_at"], "taken_source": result["taken_source"],
        "tz_offset": result["tz_offset"], "camera": result["camera"],
        "lat": result["lat"], "lon": result["lon"], "exif": exif,
        "projection": result.get("projection", ""),
    }
    values.update(places.place_columns(result["lat"], result["lon"]))
    return values


_DESCRIPTION_COLUMNS = ("info", "described", "prompt_ver", "provenance", "hidden",
                        "describe_attempts", "describe_error")
_EMPTY_DESCRIPTION = {"info": "", "described": 0, "prompt_ver": 0, "provenance": "",
                      "describe_attempts": 0, "describe_error": ""}

_ROW_SQL = """
    SELECT id, path, size, mtime, content_hash, dhash, width, height, described,
           hidden, has_preview, missing_since
    FROM images WHERE library_id = ?"""

_SOURCE_COLUMNS = "id, library_id, path, " + ", ".join(_DESCRIPTION_COLUMNS) + ", missing_since"
# Rows with the same content: the images_hash index makes this one lookup.
_SOURCE_BY_HASH_SQL = ("SELECT " + _SOURCE_COLUMNS
                       + " FROM images WHERE content_hash = ? AND id <> ?")
# Whether a library shows a content: 0 when a present row with it is shown,
# else 1 when a row with it is hidden, else NULL (see LibraryScan._insert).
_SHOWN_OR_HIDDEN_SQL = ("SELECT MIN(CASE WHEN hidden = 0 AND missing_since IS NULL THEN 0 "
                        "WHEN hidden = 1 THEN 1 END) FROM images "
                        "WHERE content_hash = ? AND library_id = ?")


# ---------------------------------------------------------------------------
# Scanning one library

def _path_likeness(first, second):
    """How many trailing parts (file name, folders) two relative paths share."""
    first, second = first.lower().split("/"), second.lower().split("/")
    count = 0
    while count < min(len(first), len(second)) and first[-1 - count] == second[-1 - count]:
        count += 1
    return count


def _moved_to(missing, twins_of, new_ids):
    """{missing row id: the id of the row added in this walk it most likely moved to}.

    missing are rows whose files are gone, twins_of maps each one's id to
    the rows listed in this walk with its content, new_ids are the rows
    given that content in this walk. Each new row is the destination of at
    most one missing row: the pairs with the most alike paths go first
    (a folder moved or renamed keeps the folders below it and the file
    names), then the newest rows.
    """
    pairs = sorted(((_path_likeness(row["path"], twin["path"]), twin["id"], row["id"])
                    for row in missing for twin in twins_of.get(row["id"], ())
                    if twin["id"] in new_ids), reverse=True)
    result, taken = {}, set()
    for _likeness, twin_id, row_id in pairs:
        if row_id not in result and twin_id not in taken:
            result[row_id] = twin_id
            taken.add(twin_id)
    return result


def _edit_words(edit):
    """An edit without its time, to compare two."""
    words = image_edits.view(edit)
    words.pop("updated")
    return words


class LibraryScan:
    """The state of one library scan. Use scan_library() rather than this directly."""

    def __init__(self, conn, library, ctx, helper=None, thumbs=None, previews=None,
                 threads=6, allow_mass_removal=False, purge_after_days=30, now=None,
                 verbose=False):
        self.conn = conn
        self.library = library
        self.library_id = library["id"]
        self.label = common.library_label(library)
        self.root = common.library_root(library)
        self.patterns = common.ignore_patterns(library)
        self.ctx = ctx
        self.helper = helper
        self.read_helper = thread_safe_helper(helper)
        self.profile = describe.library_profile(library)
        self.thumbs = thumbs
        self.previews = previews
        self.threads = max(1, int(threads or 1))
        self.want_previews = bool(library.get("previews", True))
        self.allow_mass_removal = allow_mass_removal
        self.purge_after_days = purge_after_days
        self.now = time.time() if now is None else now
        self.verbose = verbose
        self.rows = {}
        self.failures = {}
        self.thumb_ids = set()
        self.walked = set()
        self.cleared = set()        # ids whose missing_since this scan already cleared
        self.read_errors = []
        self.went_offline = False
        self.helper_walk = False
        # (size, dhash) -> ids of rows imported from the old database, which
        # have no content hash yet.
        self.legacy_index = collections.defaultdict(list)
        # Descriptions of rows whose content changed in this scan, by the old
        # content (hash, or (size, dhash) for imported rows), so a file that
        # still holds the old content elsewhere can keep them.
        self.replaced = {}
        # The owner's edits (image_edits): ids of this library's rows that
        # have one, the rows given new content in this scan (added or
        # changed), and {id: (old row, new dHash)} of edited rows whose
        # content changed -- the edit follows its content to another path.
        self.edited = set()
        self.any_edits = False
        self.new_content = set()
        self.edit_left = {}
        # {old key: new key} of rows whose not_duplicates key changed (new
        # content in place, or a content hash found for an imported row).
        self.rekeyed = {}
        self.unsupported_logged = set()
        self.logged = collections.Counter()
        self.timer = None
        self.summary = {
            "library_id": self.library_id, "name": self.label, "offline": False,
            "aborted": False, "complete": False, "files": 0, "added": 0, "updated": 0,
            "changed": 0, "moved": 0, "copied": 0, "unchanged": 0, "restored": 0,
            "failed": 0, "skipped_failed": 0, "unsupported": 0, "read_errors": 0,
            "missing": 0, "protected": 0, "links": 0, "excluded": 0, "guarded": 0,
            "removed_moved": 0, "purged": 0, "errors": [],
        }

    # -- bookkeeping --------------------------------------------------------

    def _wrote(self, count=1):
        self.timer.wrote(count)

    def commit(self):
        self.timer.commit()

    def flush(self):
        self.timer.flush()

    def _note(self, kind, message):
        """Log a per-file line; past FILE_LOG_LIMIT of a kind they are only counted."""
        self.logged[kind] += 1
        count = self.logged[kind]
        if self.verbose or count <= FILE_LOG_LIMIT:
            self.ctx.log(message)
        elif count == FILE_LOG_LIMIT + 1:
            self.ctx.log("(More %s files are counted but not listed; use --verbose to list "
                         "every file.)" % kind)

    def _load(self):
        for row in self.conn.execute(_ROW_SQL, (self.library_id,)):
            self.rows[row["path"]] = row
            if not row["content_hash"] and row["dhash"]:
                self.legacy_index[(row["size"], row["dhash"])].append(row["id"])
        self.failures = _load_failures(self.conn, self.library_id)
        self.thumb_ids = self.thumbs.ids()
        self.edited = {row[0] for row in self.conn.execute(
            "SELECT e.image_id FROM image_edits AS e JOIN images AS i ON i.id = e.image_id "
            "WHERE i.library_id = ?", (self.library_id,))}
        self.any_edits = bool(self.edited) or self.conn.execute(
            "SELECT 1 FROM image_edits LIMIT 1").fetchone() is not None

    def delete_rows(self, ids):
        """Delete image rows with their thumbnails and previews (no commit)."""
        ids = list(ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            self.conn.execute("DELETE FROM images WHERE id IN (%s)"
                              % ",".join("?" * len(chunk)), chunk)
        self.thumbs.delete(ids, commit=False)
        for image_id in ids:
            _delete_preview(self.previews, self.library_id, image_id, self.ctx)

    def _set_missing(self, ids, value):
        ids = list(ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            self.conn.execute(
                "UPDATE images SET missing_since = ? WHERE id IN (%s)"
                % ",".join("?" * len(chunk)), [value] + chunk)

    # -- the scan -----------------------------------------------------------

    def run(self):
        ctx = self.ctx
        ctx.log("Scanning library %s (%s)" % (self.label, self.root or "no folder set"))
        self.timer = _CommitTimer(self.conn, self.thumbs)
        self._load()
        walk = walk_library(self.root, self.patterns, self.helper, ctx, self.label)
        ctx.stage("scan")
        entries = list(walk)
        self.walked = {entry[0] for entry in entries}
        self.helper_walk = walk.helper_walk
        self.summary["files"] = len(entries)
        self.summary["links"] = len(walk.links)
        if not walk.reachable:
            self.summary["offline"] = True
            ctx.log("Skipping library %s: the folder %s is not available (offline?). "
                    "Nothing was changed." % (self.label, self.root or "(none)"), "warn")
            return self.summary
        for rel, message in walk.errors:
            ctx.log("Could not read %s: %s" % (rel or self.root, message), "warn")
        if walk.links:
            ctx.log("%d linked folder(s) or file(s) were not followed: %s%s. Files indexed "
                    "under them earlier are kept as they are; add a link's target as a "
                    "library of its own to index it."
                    % (len(walk.links), ", ".join(walk.links[:5]),
                       ", ..." if len(walk.links) > 5 else ""), "warn")
        ctx.set_total(len(entries))
        try:
            stopped = walk.stopped or _parallel_reads(
                self.threads, self._to_read(entries), self._read, self._handle,
                self._should_stop, ctx, self.flush)
            self.commit()
            if stopped or self.went_offline:
                self.summary["aborted"] = not self.went_offline
                self.summary["offline"] = self.went_offline
                ctx.log("The scan of %s did not finish, so no files were marked missing."
                        % self.label, "warn" if self.went_offline else "info")
            elif not walk.complete:
                ctx.log("The folder %s could not be listed, so no files were marked "
                        "missing." % (self.root or self.label), "warn")
            else:
                self._finish(walk)
            self.commit()
        finally:
            self.summary["errors"] = walk.errors + self.read_errors
            self._carry_over_pairs()
        ctx.log(self._summary_line())
        return self.summary

    def _carry_over_pairs(self):
        """Pictures the owner said are not duplicates stay apart when a file
        changes in place (rotated, or tags written into it): their pairs
        (not_duplicates.py, kept by content) are copied to the new content."""
        if not self.rekeyed:
            return
        try:
            count = not_duplicates.carry_over(self.rekeyed)
        except OSError as error:
            self.ctx.log("Could not update %s: %s" % (not_duplicates.FILE_NAME,
                                                      _error_text(error)), "warn")
            return
        if count:
            self.ctx.log("Kept %d pair(s) of pictures apart as not duplicates after their "
                         "files changed." % count)

    def _summary_line(self):
        summary = self.summary
        parts = ["%d files" % summary["files"]]
        for key in ("added", "changed", "updated", "moved", "copied", "restored", "failed",
                    "unsupported", "missing", "excluded", "guarded", "removed_moved",
                    "purged"):
            if summary[key]:
                parts.append("%d %s" % (summary[key], key.replace("_", " ")))
        return "Library %s: %s." % (self.label, ", ".join(parts))

    def _should_stop(self):
        return self.went_offline or self.ctx.should_stop()

    def _to_read(self, entries):
        """Yield the entries that need reading; deal with the rest on the way."""
        for entry in entries:
            rel, size, mtime, _ctime = entry
            row = self.rows.get(rel)
            if (row is not None and row["size"] == size
                    and abs(row["mtime"] - mtime) < MTIME_TOLERANCE
                    and row["id"] in self.thumb_ids):
                self.summary["unchanged"] += 1
                if row["missing_since"] is not None:
                    self.conn.execute("UPDATE images SET missing_since = NULL WHERE id = ?",
                                      (row["id"],))
                    self.cleared.add(row["id"])
                    self.summary["restored"] += 1
                    self._note("found again", "Found again: " + rel)
                    self._wrote()
                self.ctx.step(item=rel, quick=True)
                continue
            if _failed_before(self.failures, rel, size, mtime):
                self.summary["skipped_failed"] += 1
                self.ctx.step(item=rel, quick=True)
                continue
            yield entry

    def _read(self, entry):
        """Worker thread: read one file. Returns (result, error); never raises."""
        rel, _size, mtime, _ctime = entry
        return _read_original(self.root, rel, self.read_helper, self.want_previews, mtime)

    def _handle(self, entry, outcome):
        rel, size, mtime, ctime = entry
        result, error = outcome
        if error is None:
            self._store(rel, size, mtime, ctime, result)
        elif isinstance(error, images.SourceReadError):
            # Possibly temporary (a share that went away): not recorded as a
            # bad file, and never a reason to mark anything missing.
            self.read_errors.append((rel, _error_text(error)))
            self.summary["read_errors"] += 1
            self.ctx.log("Could not read %s: %s" % (rel, _error_text(error)), "warn")
            if not self.helper_walk and not os.path.isdir(self.root):
                self.went_offline = True
                self.ctx.log("The folder %s is no longer available; stopping this "
                             "library." % self.root, "warn")
        elif _missing_decoder(error):
            # Not a bad file: it is read once the decoder is installed.
            self.summary["unsupported"] += 1
            if str(error) not in self.unsupported_logged:
                self.unsupported_logged.add(str(error))
                self.ctx.log("Skipping %s: %s" % (rel, error), "warn")
        else:
            _record_read_failure(self.conn, self.library_id, rel, size, mtime, error)
            self.summary["failed"] += 1
            self.ctx.log("Could not read %s: %s" % (rel, _error_text(error)), "warn")
            self._wrote()
        self.ctx.step(item=rel)

    def _is_move_source(self, row):
        """True for a missing row of this library whose file is not back at its path."""
        return (row["library_id"] == self.library_id and row["missing_since"] is not None
                and row["path"] not in self.walked)

    def _find_source(self, metadata, size, exclude_id):
        """(row, moved): a row whose description this content can reuse, or (None, False).

        Prefers a missing row of this library with the same content (the
        file was moved or renamed: moved is True), then any described row
        with the same content hash. Rows imported from the old database have
        no content hash yet; for those, the same size and dHash in this
        library count. Last come descriptions this scan took off files whose
        content changed (the old content may live on under another name).
        """
        rows = self.conn.execute(_SOURCE_BY_HASH_SQL,
                                 (metadata["content_hash"], exclude_id)).fetchall()
        if not rows and metadata["dhash"]:
            ids = [image_id for image_id in self.legacy_index.get((size, metadata["dhash"]), ())
                   if image_id != exclude_id]
            if ids:
                rows = self.conn.execute(
                    "SELECT " + _SOURCE_COLUMNS + " FROM images WHERE id IN (%s) "
                    "AND content_hash = ''" % ",".join("?" * len(ids)), ids).fetchall()

        def best(candidates):
            return min(candidates, key=lambda row: (-row["described"], -row["prompt_ver"],
                                                    row["library_id"] != self.library_id,
                                                    row["id"]))

        moves = [row for row in rows if self._is_move_source(row)]
        if moves:
            return best(moves), True
        described = [row for row in rows if row["described"] and self._fits_profile(row)]
        if described:
            return best(described), False
        replaced = (self.replaced.get(metadata["content_hash"])
                    or self.replaced.get((size, metadata["dhash"])))
        return replaced, False

    def _fits_profile(self, row):
        """True when a described row's description suits this library.

        A current description from another library is only reused when it
        has the fields this library asks for: an "extended" library does
        not take a "core" description (it would never be asked for the
        rest). Descriptions from an older prompt stay stale wherever they go.
        """
        if row["library_id"] == self.library_id or row["prompt_ver"] < describe.PROMPT_VER:
            return True
        try:
            provenance = json.loads(row["provenance"] or "{}")
        except ValueError:
            provenance = {}
        profile = provenance.get("profile") if isinstance(provenance, dict) else None
        return self.profile == "core" or profile == self.profile

    def _remember_description(self, row):
        """Keep the description of a row whose content is about to change."""
        if not row["described"]:
            return
        source = self.conn.execute("SELECT " + _SOURCE_COLUMNS + " FROM images WHERE id = ?",
                                   (row["id"],)).fetchone()
        if source is None:
            return
        source = dict(source)
        source["missing_since"] = None
        key = row["content_hash"] or ((row["size"], row["dhash"]) if row["dhash"] else None)
        if key and key not in self.replaced:
            self.replaced[key] = source

    @staticmethod
    def _same_content(row, metadata, size, mtime):
        if row["content_hash"]:
            return row["content_hash"] == metadata["content_hash"]
        # Imported from the old database, so no content hash yet.
        if row["size"] == size and abs(row["mtime"] - mtime) < MTIME_TOLERANCE:
            return True
        return bool(row["dhash"]) and row["dhash"] == metadata["dhash"] and row["size"] == size

    def _store(self, rel, size, mtime, ctime, result):
        metadata = _metadata(result)
        now = time.time()
        row = self.rows.get(rel)
        preview = result["preview"] if self.want_previews else None
        if row is None:
            image_id = self._insert(rel, size, mtime, ctime, metadata, preview is not None, now)
        else:
            image_id = row["id"]
            values = dict(metadata, size=size, mtime=mtime, ctime=ctime, updated=now)
            old_key = not_duplicates.unit_key(image_id, row["content_hash"])
            new_key = not_duplicates.unit_key(image_id, metadata["content_hash"])
            if old_key != new_key:
                self.rekeyed[old_key] = new_key
            if self._same_content(row, metadata, size, mtime):
                self.summary["updated"] += 1
                self._note("updated", "Updated " + rel
                           + (" (description kept)" if row["described"] else ""))
            else:
                self._remember_description(row)
                self.new_content.add(image_id)
                if image_id in self.edited:
                    self.edit_left[image_id] = (row, metadata["dhash"])
                source, _moved = self._find_source(metadata, size, image_id)
                if source is not None:
                    values.update({key: source[key] for key in _DESCRIPTION_COLUMNS
                                   if key != "hidden"})
                    self.summary["copied"] += 1
                    self._note("changed", "Changed %s (description copied from %s)"
                               % (rel, source["path"]))
                else:
                    values.update(_EMPTY_DESCRIPTION)
                    self._note("changed", "Changed " + rel)
                self.summary["changed"] += 1
                if preview is None and row["has_preview"]:
                    _delete_preview(self.previews, self.library_id, image_id, self.ctx)
                    values["has_preview"] = 0
            if preview is not None:
                values["has_preview"] = 1
            _update_row(self.conn, image_id, values, "missing_since = NULL")
            self.cleared.add(image_id)
        if rel in self.failures:
            self.conn.execute("DELETE FROM read_failures WHERE library_id = ? AND path = ?",
                              (self.library_id, rel))
        self.thumbs.put(image_id, result["thumb"], commit=False)
        self.thumb_ids.add(image_id)
        if preview is not None:
            try:
                self.previews.write(self.library_id, image_id, preview)
            except OSError as error:
                # Never leave the previous picture's preview behind: it would
                # be shown, and sent to the LLM, for the new content.
                _delete_preview(self.previews, self.library_id, image_id, self.ctx)
                self.conn.execute("UPDATE images SET has_preview = 0 WHERE id = ?", (image_id,))
                self.ctx.log("Could not save the preview of %s: %s"
                             % (rel, _error_text(error)), "warn")
            else:
                _store_pano(self.previews, self.library_id, image_id, result.get("pano"),
                            self.ctx, rel)
        self._wrote()

    def _insert(self, rel, size, mtime, ctime, metadata, has_preview, now):
        values = dict(metadata)
        values.update(library_id=self.library_id, path=rel, name=rel.rsplit("/", 1)[-1],
                      size=size, mtime=mtime, ctime=ctime, has_preview=1 if has_preview else 0,
                      added=now, updated=now)
        values.update(_EMPTY_DESCRIPTION)
        values["hidden"] = 0
        source, moved = self._find_source(metadata, size, -1)
        if source is not None:
            values.update({key: source[key] for key in _DESCRIPTION_COLUMNS})
        if not moved:
            # A hidden picture stays hidden as a copy (and under a new name,
            # see _remove_moved), unless a copy of it is shown here: the owner
            # kept that one in view and hid the others ("Keep best"). A
            # moved file keeps its own flag.
            shown = self.conn.execute(_SHOWN_OR_HIDDEN_SQL,
                                      (metadata["content_hash"], self.library_id)).fetchone()[0]
            if shown is not None:
                values["hidden"] = shown
        columns = sorted(values)
        image_id = self.conn.execute(
            "INSERT INTO images (%s) VALUES (%s)" % (", ".join(columns),
                                                     ", ".join("?" * len(columns))),
            [values[column] for column in columns]).lastrowid
        self.summary["added"] += 1
        self.new_content.add(image_id)
        if moved:
            # The old row was the same file at its previous path; the
            # owner's edit (image_edits) moves along with it.
            if source["id"] in self.edited:
                image_edits.give(self.conn, source["id"], [image_id])
            self.delete_rows([source["id"]])
            self.summary["moved"] += 1
            self._note("moved", "Moved %s -> %s" % (source["path"], rel))
        elif source is not None:
            self.summary["copied"] += 1
            self._note("added", "Added %s (description copied from %s)" % (rel, source["path"]))
        else:
            self._note("added", "Added " + rel)
        if not moved and self.any_edits:
            self._adopt_edit(image_id, rel, metadata["content_hash"])
        return image_id

    # -- the owner's edits ---------------------------------------------------

    def _adopt_edit(self, image_id, rel, content_hash):
        """A new file whose content was last seen as a missing file gets that file's edit.

        The missing file is most likely the same one, moved here from
        another library; it keeps its edit too (copied), in case it comes
        back.
        """
        if not content_hash:
            return
        for row in self.conn.execute(
                "SELECT i.id, i.library_id, i.path FROM image_edits AS e "
                "JOIN images AS i ON i.id = e.image_id WHERE i.content_hash = ? "
                "AND i.missing_since IS NOT NULL AND i.id <> ? "
                "ORDER BY i.missing_since DESC, i.id DESC", (content_hash, image_id)).fetchall():
            if row["library_id"] == self.library_id and row["path"] in self.walked:
                continue        # back at its own path in this walk
            if image_edits.give(self.conn, row["id"], [image_id], copy=True) is not None:
                self._note("edits", "Kept your edit of %s for %s" % (row["path"], rel))
            return

    def _follow_content(self, present):
        """Edits of files whose content changed go where the old content is now.

        A file moved away and a new one saved under its name (cameras reuse
        names), or two files that swapped names: the owner's edit belongs to
        the picture, so it goes to the file added or changed in this scan
        that now holds the content it was made for. When the new content at
        the old path looks like the same picture (retouched, with the
        original kept under another name), both keep the edit. A file whose
        old content is nowhere else keeps its edit.
        """
        plan = {}
        for image_id, (row, new_dhash) in self.edit_left.items():
            twins = sorted((twin["id"] for twin in self._twins(row, present)
                            if twin["id"] in self.new_content), reverse=True)
            if twins:
                plan[image_id] = (twins, _looks_alike(row["dhash"], new_dhash))
        if not plan:
            return
        paths = {twin["id"]: twin["path"] for group in present for rows in group.values()
                 for twin in rows}
        for source, target in sorted(image_edits.reassign(self.conn, plan).items()):
            self._note("edits", "Your edit of %s went with its picture to %s"
                       % (self.edit_left[source][0]["path"], paths.get(target, target)))

    def _share_edit(self, row):
        """Copy a file's edit, as it goes missing, to the same picture in another library.

        That file most likely moved there (the other library was scanned
        first); this one keeps its edit too, in case it comes back.
        """
        if not row["content_hash"]:
            return
        heirs = [other["id"] for other in self.conn.execute(
            "SELECT id FROM images WHERE content_hash = ? AND library_id <> ? "
            "AND missing_since IS NULL ORDER BY id DESC", (row["content_hash"], self.library_id))]
        if heirs and image_edits.give(self.conn, row["id"], heirs, copy=True) is not None:
            self._note("edits", "Kept your edit of %s for its copy in another library"
                       % row["path"])

    def _hand_on_edit(self, row, preferred=()):
        """Before a missing file's row is deleted, give its edit to a present copy.

        preferred (ids) come first, then present rows with the same content
        in any library, newest first. An edit that no copy can take (each
        has an edit of its own, and none the same) is logged as it goes.
        """
        heirs = list(preferred)
        if row["content_hash"]:
            heirs += [other["id"] for other in self.conn.execute(
                "SELECT id FROM images WHERE content_hash = ? AND missing_since IS NULL "
                "AND id <> ? ORDER BY id DESC", (row["content_hash"], row["id"]))
                      if other["id"] not in heirs]
        if not heirs or image_edits.give(self.conn, row["id"], heirs) is not None:
            return
        edit = image_edits.load(self.conn, row["id"])
        if edit is None:
            return
        words = _edit_words(edit)
        if any(_edit_words(other) == words
               for other in image_edits.load_many(self.conn, heirs).values()):
            return              # a copy has the same edit already
        self._note("edits lost", "Your edit of %s (%s) was removed with it: every copy of the "
                   "picture has an edit of its own" % (row["path"], words["title"] or ", ".join(
                       words["tags_add"]) or "a description"))

    # -- after a complete walk ----------------------------------------------

    def _present_walked(self):
        """Present rows of this library whose files were listed in this walk.

        Returns (by_hash, by_size_dhash): {content_hash: [row]} and
        {(size, dhash): [row]}.
        """
        by_hash = collections.defaultdict(list)
        by_size_dhash = collections.defaultdict(list)
        for row in self.conn.execute(
                "SELECT id, path, content_hash, size, dhash, described FROM images "
                "WHERE library_id = ? AND missing_since IS NULL", (self.library_id,)):
            if row["path"] not in self.walked:
                continue
            if row["content_hash"]:
                by_hash[row["content_hash"]].append(row)
            if row["dhash"]:
                by_size_dhash[(row["size"], row["dhash"])].append(row)
        return by_hash, by_size_dhash

    @staticmethod
    def _twins(row, present):
        """Rows listed in this walk that hold the same content as row."""
        by_hash, by_size_dhash = present
        if row["content_hash"]:
            twins = by_hash.get(row["content_hash"], [])
        elif row["dhash"]:
            twins = by_size_dhash.get((row["size"], row["dhash"]), [])
        else:
            twins = []
        return [twin for twin in twins if twin["id"] != row["id"]]

    def _finish(self, walk):
        ctx = self.ctx
        error_paths = {rel for rel, _ in walk.errors + self.read_errors}
        links = set(walk.links)

        # Every file listed in this walk exists, even if it could not be read.
        back = [row["id"] for path, row in self.rows.items()
                if path in self.walked and row["missing_since"] is not None
                and row["id"] not in self.cleared]
        if back:
            self._set_missing(back, None)
            self.summary["restored"] += len(back)

        present = sum(1 for row in self.rows.values() if row["missing_since"] is None)
        candidates = [row for path, row in self.rows.items()
                      if path not in self.walked and row["missing_since"] is None]
        to_mark = [row for row in candidates
                   if not _under_any(row["path"], error_paths)
                   and not _under_any(row["path"], links)]
        self.summary["protected"] = len(candidates) - len(to_mark)
        if self.summary["protected"]:
            ctx.log("%d file(s) in folders that could not be read (or linked folders) were "
                    "left as they are." % self.summary["protected"], "warn")
        if not self.walked:
            # Nothing at all was listed: an emptied or unmounted folder is more
            # likely than a deliberate deletion of everything.
            if to_mark:
                self._guard(to_mark, present)
            self.summary["complete"] = True
            return
        present_walked = (self._present_walked()
                          if (to_mark or self._has_missing() or self.edit_left) else None)

        # Files now excluded by ignore patterns were not deleted, but they
        # are no longer part of the library.
        excluded = [row for row in to_mark if excluded_by_ignores(row["path"], self.patterns)]
        # Files whose content was listed at another path in this walk moved.
        excluded_ids = {row["id"] for row in excluded}
        moved = [row for row in to_mark if row["id"] not in excluded_ids
                 and self._twins(row, present_walked)]
        moved_ids = {row["id"] for row in moved}
        rest = [row for row in to_mark if row["id"] not in excluded_ids
                and row["id"] not in moved_ids]
        if excluded:
            self._set_missing([row["id"] for row in excluded], self.now)
            self.summary["excluded"] = len(excluded)
            ctx.log("%d file(s) in %s are now excluded by ignore patterns; they were marked "
                    "missing." % (len(excluded), self.label))
            for row in excluded:
                if row["id"] in self.edited:
                    self._share_edit(row)
        if moved:
            self._set_missing([row["id"] for row in moved], self.now)
        trusted = True
        if rest:
            if self._is_mass_removal(rest, present) and not self.allow_mass_removal:
                self._guard(rest, present)
                trusted = False
            else:
                self._set_missing([row["id"] for row in rest], self.now)
                self.summary["missing"] = len(rest)
                for row in rest:
                    if row["id"] in self.edited:
                        self._share_edit(row)
                for row in rest[:MISSING_LOG_LIMIT]:
                    ctx.log("Missing: " + row["path"])
                if len(rest) > MISSING_LOG_LIMIT:
                    ctx.log("... and %d more missing file(s)" % (len(rest) - MISSING_LOG_LIMIT))
        self.summary["complete"] = True
        if self.edit_left:
            self._follow_content(present_walked)
        if present_walked is not None:
            self._remove_moved(present_walked)
        if not trusted:
            # The walk did not look trustworthy: keep everything else as it is.
            return
        self._purge(error_paths | links)
        stale = [path for path in self.failures
                 if path not in self.walked and not _under_any(path, error_paths)]
        for path in stale:
            self.conn.execute("DELETE FROM read_failures WHERE library_id = ? AND path = ?",
                              (self.library_id, path))

    @staticmethod
    def _is_mass_removal(rows, present):
        return len(rows) > MASS_REMOVAL_MIN and len(rows) > present * MASS_REMOVAL_FRACTION

    def _guard(self, rows, present):
        self.summary["guarded"] = len(rows)
        self.ctx.log("%d of the %d files in %s seem to be gone. That is too many to be "
                     "likely, so none were marked missing. If they really were deleted or "
                     "moved, scan again with --allow-mass-removal."
                     % (len(rows), present, self.label), "warn")

    def _has_missing(self):
        return any(row["missing_since"] is not None for row in self.rows.values())

    def _remove_moved(self, present_walked):
        """Delete missing rows whose content was listed at another path in this walk.

        The new rows get the old one's description when they have none yet.
        The file the old one moved to (the row added in this walk with its
        content and the most alike path, see _moved_to) stays hidden if the
        old one was hidden, and the owner's edit moves to it. Copies that
        were there before keep their own flag: a hidden copy deleted from
        the folder never hides the copy the owner kept. A shown row whose
        copies here are all hidden, and that moved to no new path, is kept
        (missing): it moved away, and is shown again when it comes back.
        """
        missing = [row for row in self.conn.execute(
            "SELECT " + _SOURCE_COLUMNS + ", content_hash, size, dhash FROM images "
            "WHERE library_id = ? AND missing_since IS NOT NULL", (self.library_id,))
                   if row["path"] not in self.walked]
        twins_of = {row["id"]: self._twins(row, present_walked) for row in missing}
        moved_to = _moved_to(missing, twins_of, self.new_content)
        gone = []
        for row in missing:
            twins = twins_of[row["id"]]
            if not twins:
                continue
            ids = [twin["id"] for twin in twins]
            marks = ",".join("?" * len(ids))
            target = moved_to.get(row["id"])
            if target is None and not row["hidden"] and self.conn.execute(
                    "SELECT 1 FROM images WHERE hidden = 0 AND id IN (%s) LIMIT 1" % marks,
                    ids).fetchone() is None:
                # Shown, while every copy left is hidden: the owner kept
                # this one, which went elsewhere. It stays a missing row,
                # so the file is shown again when a later scan finds it.
                continue
            if row["described"]:
                self.conn.execute(
                    "UPDATE images SET info = ?, described = 1, prompt_ver = ?, "
                    "provenance = ?, describe_attempts = 0, describe_error = '' "
                    "WHERE described = 0 AND id IN (%s)" % marks,
                    [row["info"], row["prompt_ver"], row["provenance"]] + ids)
            if row["hidden"] and target is not None:
                self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (target,))
            if row["id"] in self.edited:
                # The owner's edit goes to the file at the new path: the one
                # it moved to, else one added in this walk, else the newest
                # copy without an edit of its own.
                self._hand_on_edit(row, sorted(ids, key=lambda image_id: (
                    image_id != target, image_id not in self.new_content, -image_id)))
            gone.append(row)
        if gone:
            self.delete_rows([row["id"] for row in gone])
            self.summary["removed_moved"] = len(gone)
            for row in gone[:MISSING_LOG_LIMIT]:
                self.ctx.log("Moved away: %s (its file is at another path now)" % row["path"])

    def _purge(self, protected_paths):
        days = self.purge_after_days
        if days is None:
            return
        try:
            days = float(days)
        except (TypeError, ValueError):
            return
        if days < 0:
            return
        cutoff = self.now - days * DAY
        rows = [row for row in self.conn.execute(
            "SELECT id, path, content_hash FROM images WHERE library_id = ? "
            "AND missing_since IS NOT NULL AND missing_since < ?", (self.library_id, cutoff))
                if row["path"] not in self.walked
                and not _under_any(row["path"], protected_paths)]
        for row in rows:
            if row["id"] in self.edited:
                # A copy of the picture (in any library) keeps the owner's words.
                self._hand_on_edit(row)
        if rows:
            self.delete_rows([row["id"] for row in rows])
            self.summary["purged"] = len(rows)
            self.ctx.log("Removed %d file(s) that have been missing for more than %g days."
                         % (len(rows), days))


def scan_library(conn, library, ctx, helper=None, thumbs=None, previews=None,
                 threads=6, allow_mass_removal=False, purge_after_days=30, now=None,
                 verbose=False):
    """Bring the index of one library up to date with its folder.

    conn is an index.db connection, ctx a jobs.JobContext; thumbs and
    previews default to cache.ThumbStore() and cache.PreviewStore(). now
    (seconds) is the time used for missing_since and purging. verbose logs
    every added or changed file instead of the first FILE_LOG_LIMIT. Returns
    a summary dict with counts (files, added, updated, changed, moved,
    copied, unchanged, restored, failed, skipped_failed, unsupported,
    read_errors, missing, protected, links, excluded, guarded,
    removed_moved, purged), flags (offline, aborted, complete) and errors
    [(rel_path, message)].
    """
    own_thumbs = thumbs is None
    thumbs = thumbs or cache.ThumbStore()
    try:
        start_stitch_state(conn, library["id"])
        scan = LibraryScan(conn, library, ctx, helper, thumbs, previews or cache.PreviewStore(),
                           threads, allow_mass_removal, purge_after_days, now, verbose)
        return scan.run()
    finally:
        if own_thumbs:
            thumbs.close()


# ---------------------------------------------------------------------------
# Filling in what older rows lack

# The meta row (this prefix + library id) that says which of a library's
# unstitched 360 photos still need stitching again, as JSON:
# {"key": images.stitch_key() they are being stitched for, "pending": [ids]}.
STITCH_META = "stitch:"
# The back-fill saves what is still pending after every this many of them.
STITCH_SAVE_EVERY = 25


def stitch_state(conn, library_id):
    """(stitch key, [pending image ids]) recorded for a library, or (None, [])."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?",
                       (STITCH_META + library_id,)).fetchone()
    try:
        value = json.loads(row[0]) if row is not None else None
    except ValueError:
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("key"), str):
        return None, []
    pending = value.get("pending")
    if not isinstance(pending, list):
        pending = []
    return value["key"], [item for item in pending
                          if isinstance(item, int) and not isinstance(item, bool)]


def save_stitch_state(conn, library_id, key, pending):
    """Record a library's stitch key and pending ids (no commit)."""
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (STITCH_META + library_id,
                  json.dumps({"key": key, "pending": sorted(pending)}, separators=(",", ":"))))


def start_stitch_state(conn, library_id):
    """Note that a library without any rows yet stitches its 360 photos the current way.

    Called before a scan: a library scanned for the first time gets the
    current images.stitch_key() with nothing pending, so the back-fill does
    not stitch its photos twice. A library with rows but no record was
    stitched by an older version (see _restitch_ids).
    """
    if stitch_state(conn, library_id)[0] is not None:
        return
    if conn.execute("SELECT 1 FROM images WHERE library_id = ? LIMIT 1",
                    (library_id,)).fetchone() is None:
        save_stitch_state(conn, library_id, images.stitch_key(), [])
        conn.commit()


def _restitch_ids(conn, library_id):
    """(stitch key, ids of a library's dual-fisheye photos to stitch again as a set).

    When the recorded stitch key is not images.stitch_key() (a new version
    of the stitching, or other pole fill settings), every dual-fisheye row
    is pending, also those of files missing now (they are stitched again
    once they are back unchanged); the back-fill reads them again and takes
    them off the list as it goes, so a stopped run carries on later. Rows
    that are gone, or no longer dual-fisheye frames, drop off the list. No
    commit.
    """
    key = images.stitch_key()
    stored, pending = stitch_state(conn, library_id)
    frames = {row[0] for row in conn.execute(
        "SELECT id FROM images WHERE library_id = ? AND projection = ?",
        (library_id, images.PROJECTION_DUAL_FISHEYE))}
    wanted = frames if stored != key else frames.intersection(pending)
    if stored != key or len(wanted) != len(pending):
        save_stitch_state(conn, library_id, key, wanted)
    return key, wanted


_BACKFILL_SQL = """
    SELECT id, path, size, mtime, content_hash, dhash, width, height, has_preview,
           projection, described
    FROM images WHERE library_id = ? AND missing_since IS NULL ORDER BY id"""


def backfill(conn, library, ctx, helper=None, thumbs=None, previews=None, threads=6):
    """Read rows that lack a content hash, dHash, size or (wanted) preview.

    Rows imported from the old database have no content hash or preview;
    this reads each such original once and fills them in, with fresh EXIF
    facts and thumbnail. Rows whose projection is "?" (stored before 360
    photos were recognised) are read again the same way, which gives 360
    photos their panorama preview, front-view thumbnail and dHash; so are
    360 photos with a preview but no pano (stored before panos were made;
    only those larger than their preview get one, see images.has_pano),
    and unstitched 360 photos stitched by an older version or with other
    pole fill settings (see _restitch_ids; their descriptions are kept).
    Rows of files the ignore patterns now exclude are left alone. It runs
    as stage "previews", can be stopped at any point and continues where
    it left off. Returns a summary dict: {needed, filled, failed, changed,
    unsupported, aborted, panoramas, panos, restitched}.
    """
    own_thumbs = thumbs is None
    thumbs = thumbs or cache.ThumbStore()
    previews = previews or cache.PreviewStore()
    try:
        return _backfill(conn, library, ctx, helper, thumbs, previews, threads)
    finally:
        if own_thumbs:
            thumbs.close()


def _backfill(conn, library, ctx, helper, thumbs, previews, threads):
    library_id = library["id"]
    root = common.library_root(library)
    patterns = common.ignore_patterns(library)
    want_previews = bool(library.get("previews", True))
    summary = {"needed": 0, "filled": 0, "failed": 0, "changed": 0, "unsupported": 0,
               "aborted": False, "panoramas": 0, "panos": 0, "restitched": 0}
    redescribe = []
    on_disk, panos_on_disk = previews.stored_ids(library_id)
    failures = _load_failures(conn, library_id)
    stitch_key, restitch = _restitch_ids(conn, library_id)
    pending_before = len(restitch)
    needed = []
    for row in conn.execute(_BACKFILL_SQL, (library_id,)).fetchall():
        has_preview = bool(row["has_preview"])
        if has_preview and row["id"] not in on_disk:
            # The file was deleted: build it again. (A file without the flag
            # is never trusted; it may hold an older picture.)
            conn.execute("UPDATE images SET has_preview = 0 WHERE id = ?", (row["id"],))
            has_preview = False
        if (row["content_hash"] and row["dhash"] and row["width"] > 0 and row["height"] > 0
                and (has_preview or not want_previews)
                and row["projection"] != images.PROJECTION_UNKNOWN
                and row["id"] not in restitch
                and not (want_previews and has_preview
                         and row["projection"] in images.PROJECTIONS_360
                         and images.has_pano(row["width"], row["height"])
                         and row["id"] not in panos_on_disk)):
            continue
        if (excluded_by_ignores(row["path"], patterns)
                or _failed_before(failures, row["path"], row["size"], row["mtime"])):
            continue
        needed.append(row)
    # (Pending rows left alone here -- missing, ignored now, or unreadable
    # before -- stay pending until they can be read.)
    conn.commit()
    summary["needed"] = len(needed)
    if not needed:
        return summary
    stitching = sum(1 for row in needed if row["id"] in restitch)
    if stitching:
        ctx.log("Stitching %d 360-degree photo(s) in %s again (their descriptions are kept)"
                % (stitching, common.library_label(library)))
    ctx.stage("previews", total=len(needed))
    ctx.log("Filling in previews and details for %d image(s) in %s"
            % (len(needed), common.library_label(library)))
    read_helper = thread_safe_helper(helper)
    timer = _CommitTimer(conn, thumbs)
    unsupported_logged = set()
    rekeyed = {}                # not_duplicates keys that change (see LibraryScan)

    def read(row):
        return _read_original(root, row["path"], read_helper, want_previews, row["mtime"])

    def handle(row, outcome):
        result, error = outcome
        # A file that could not be read for now (a share that went away)
        # stays pending; one read, changed since, or bad for good is done.
        if row["id"] in restitch and not isinstance(error, images.SourceReadError):
            restitch.discard(row["id"])
            if error is None and result.get("projection") == images.PROJECTION_DUAL_FISHEYE:
                summary["restitched"] += 1
            if not len(restitch) % STITCH_SAVE_EVERY:
                save_stitch_state(conn, library_id, stitch_key, restitch)
        if error is not None and _missing_decoder(error):
            summary["unsupported"] += 1
            if str(error) not in unsupported_logged:
                unsupported_logged.add(str(error))
                ctx.log("Skipping %s: %s" % (row["path"], error), "warn")
        elif error is not None:
            if not isinstance(error, images.SourceReadError):
                _record_read_failure(conn, library_id, row["path"], row["size"],
                                     row["mtime"], error)
            summary["failed"] += 1
            ctx.log("Could not read %s: %s" % (row["path"], _error_text(error)), "warn")
        elif row["content_hash"] and row["content_hash"] != result["content_hash"]:
            # Changed since it was scanned; the next scan picks it up.
            summary["changed"] += 1
        else:
            if not row["content_hash"] and result["content_hash"]:
                rekeyed[not_duplicates.unit_key(row["id"], "")] = result["content_hash"]
            values = _metadata(result)
            values["updated"] = time.time()
            preview = result["preview"] if want_previews else None
            if preview is not None:
                try:
                    previews.write(library_id, row["id"], preview)
                    values["has_preview"] = 1
                except OSError as error:
                    _delete_preview(previews, library_id, row["id"], ctx)
                    values["has_preview"] = 0
                    ctx.log("Could not save the preview of %s: %s"
                            % (row["path"], _error_text(error)), "warn")
                else:
                    if _store_pano(previews, library_id, row["id"], result.get("pano"), ctx,
                                   row["path"]):
                        summary["panos"] += 1
            _update_row(conn, row["id"], values)
            thumbs.put(row["id"], result["thumb"], commit=False)
            summary["filled"] += 1
            if values["projection"] in images.PROJECTIONS_360:
                summary["panoramas"] += 1
                if (values["projection"] == images.PROJECTION_DUAL_FISHEYE
                        and row["described"] and row["projection"] != values["projection"]):
                    redescribe.append(row["path"])
        timer.wrote()
        ctx.step(item=row["path"])

    try:
        summary["aborted"] = _parallel_reads(threads, needed, read, handle, ctx.should_stop,
                                             ctx, timer.flush)
    finally:
        if pending_before:
            save_stitch_state(conn, library_id, stitch_key, restitch)
        timer.commit()
        if rekeyed:
            # Imported rows found their content hash: their pairs follow.
            try:
                not_duplicates.carry_over(rekeyed)
            except OSError as error:
                ctx.log("Could not update %s: %s" % (not_duplicates.FILE_NAME,
                                                     _error_text(error)), "warn")
    if summary["panoramas"]:
        made = (" (%d with a large panorama for the 360 view)" % summary["panos"]
                if want_previews else "")
        ctx.log("%d 360-degree photo(s) recognised in %s%s."
                % (summary["panoramas"], common.library_label(library), made))
    if redescribe:
        # Their descriptions were made from the two raw fisheye circles.
        ctx.log("%d described image(s) turned out to be unstitched 360-degree photos (%s%s); "
                "their descriptions were made from the unstitched picture, so describe them "
                "again to get better ones."
                % (len(redescribe), ", ".join(redescribe[:3]),
                   ", ..." if len(redescribe) > 3 else ""), "warn")
    return summary


# ---------------------------------------------------------------------------
# Whole runs and the command line

def run(config, ctx, library_ids=None, scan_images=True, describe_images=True,
        newest_first=None, retry_failed=False, allow_mass_removal=False, helper=None,
        now=None, verbose=False, redescribe_stale=False, limit=None, endpoint_name=None,
        conn=None):
    """Scan (and back-fill) the selected libraries, then describe what needs it.

    library_ids limits the run to those library ids (default: every enabled
    library). db.init() must have run in this process. redescribe_stale,
    limit and endpoint_name are passed on to describe.describe_pending.
    conn is an index.db connection to use (default: one opened and closed
    here). Returns {"libraries": [scan summaries], "describe": describe
    summary or None, "stopped": bool}.
    """
    libraries = describe.selected_libraries(config, library_ids)
    summary = {"libraries": [], "describe": None, "stopped": False}
    threads = reader_threads(config)
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        previews = cache.PreviewStore()
        if scan_images:
            with cache.ThumbStore() as thumbs:
                for library in libraries:
                    if ctx.should_stop():
                        summary["stopped"] = True
                        break
                    if jobs.library_removed(ctx, library["id"]):
                        # Removed from the settings while this job runs.
                        continue
                    result = scan_library(conn, library, ctx, helper, thumbs, previews, threads,
                                          allow_mass_removal,
                                          config.get("purge_after_days", 30), now, verbose)
                    summary["libraries"].append(result)
                    if result["aborted"]:
                        summary["stopped"] = True
                        break
                    if result["offline"]:
                        continue
                    result["backfill"] = backfill(conn, library, ctx, helper, thumbs,
                                                  previews, threads)
                    if result["backfill"]["aborted"]:
                        summary["stopped"] = True
                        break
                    # Place names for pictures that have a position but no
                    # place yet (when place names were downloaded).
                    labelled = places.label_pictures(conn, ctx, [library["id"]])
                    if labelled is not None:
                        result["places"] = labelled
                        if labelled["aborted"]:
                            summary["stopped"] = True
                            break
        if describe_images and not summary["stopped"] and not ctx.should_stop():
            summary["describe"] = describe.describe_pending(
                conn, config, ctx, helper=helper,
                library_ids=[library["id"] for library in libraries
                             if not jobs.library_removed(ctx, library["id"])],
                newest_first=newest_first, retry_failed=retry_failed,
                redescribe_stale=redescribe_stale, limit=limit, endpoint_name=endpoint_name,
                previews=previews)
            summary["stopped"] = summary["describe"]["stopped"]
        elif ctx.should_stop():
            summary["stopped"] = True
    finally:
        if own_conn:
            conn.close()
        # The towns take memory; a later run loads them again.
        places.release()
    return summary


def run_refine(config, ctx, library_ids, helper, args):
    """scan.py --refine: the refine pass only (describe.refine_pending); returns run()'s shape."""
    conn = db.connect()
    try:
        result = describe.refine_pending(
            conn, config, ctx, helper=helper, library_ids=library_ids, limit=args.limit,
            retry_failed=args.retry_failed, newest_first=args.newest_first,
            only_library=bool(library_ids) and len(library_ids) == 1)
    finally:
        conn.close()
    return {"libraries": [], "describe": result, "stopped": result["stopped"]}


def _safe_output():
    """Never crash on file names the console cannot show."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


DEFAULT_TEST_IMAGE = os.path.join(common.BASE_DIR, "sample", "images", "18-chalkboard-menu.jpg")


def endpoint_rows(config):
    """[(name, kind, preset, model, key source, enabled, group)] for --list-endpoints.

    The key source says where a key would come from ("env NAME", "file",
    "config", "none", ...); keys themselves are never included.
    """
    rows = []
    for endpoint in common.endpoint_list(config):
        rows.append((endpoint["name"], endpoint["kind"], endpoint["preset"],
                     endpoint["model"] or "(none)", common.resolve_api_key(endpoint)[1],
                     "yes" if endpoint["enabled"] else "no", endpoint["group"] or "-"))
    return rows


def list_endpoints(config, out=None):
    """Print the configured endpoints (never their keys). Returns the exit code."""
    out = out or sys.stdout
    rows = endpoint_rows(config)
    if not rows:
        print("No LLM endpoints are configured. Run settings.py.", file=out)
        return 0
    headers = ("name", "kind", "preset", "model", "key", "enabled", "group")
    widths = [max(len(str(row[index])) for row in rows + [headers])
              for index in range(len(headers))]
    for row in [headers] + rows:
        print("  ".join(str(value).ljust(width)
                        for value, width in zip(row, widths)).rstrip(), file=out)
    default = common.default_endpoint_name(config)
    print("Default endpoint: %s" % (default or "(none)"), file=out)
    for library in config.get("libraries", []):
        if library.get("endpoint"):
            print("Library %s uses %s." % (common.library_label(library), library["endpoint"]),
                  file=out)
    return 0


def test_endpoint(config, name, image_path=None, out=None):
    """Describe one image with an endpoint and print the result (never the key).

    The image defaults to a sample picture. Returns the exit code: 0 when
    the endpoint produced a usable description, 1 when it did not, 2 for an
    unknown endpoint or an unreadable image.
    """
    out = out or sys.stdout
    endpoint = common.get_endpoint(config, name)
    if endpoint is None:
        names = ", ".join(item["name"] for item in common.endpoint_list(config)) or "none"
        print("Unknown endpoint: %s (configured: %s)" % (name, names), file=out)
        return 2
    path = image_path or DEFAULT_TEST_IMAGE
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        jpeg = images.prepare_for_llm(data, max_edge=endpoint["max_image_edge"])
    except (OSError, images.ImageReadError) as error:
        print("Cannot read the test image %s: %s" % (path, error), file=out)
        return 2
    print("Testing endpoint %s (%s, model %s) with %s..."
          % (endpoint["name"], endpoint["preset"], endpoint["model"] or "(none)",
             os.path.basename(path)), file=out)
    if common.insecure_endpoint(endpoint):
        print("Warning: %s is not encrypted (http://): %s the network unprotected. Use "
              "https:// if the service has it."
              % (common.redact(endpoint["base_url"]),
                 "the picture and the API key cross" if common.resolve_api_key(endpoint)[0]
                 else "the picture crosses"), file=out)
    result = llm.test_endpoint(endpoint, jpeg,
                               parse=lambda text: describe.parse_reply(text, "core"))
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    if not result["ok"]:
        print("The test failed: %s" % (result["error"] or result["error_kind"]), file=out)
    return 0 if result["ok"] else 1


def main(argv=None):
    """Command line entry point. Returns the process exit code."""
    _safe_output()
    parser = argparse.ArgumentParser(description="Scan the image libraries and describe "
                                                 "new images.")
    parser.add_argument("--library", action="append", metavar="NAME_OR_ID",
                        help="Only this library (by name or id). Can be repeated.")
    parser.add_argument("--no-scan", action="store_true",
                        help="Skip the index image step, and only run LLM descriptions.")
    parser.add_argument("--no-describe", action="store_true",
                        help="Index images without requesting LLM descriptions.")
    order = parser.add_mutually_exclusive_group()
    order.add_argument("--newest-first", dest="newest_first", action="store_const",
                       const=True, default=None,
                       help="Describe the most recently taken images first (default: "
                            "each library's newest_first setting).")
    order.add_argument("--oldest-first", dest="newest_first", action="store_const",
                       const=False, help="Describe the oldest images first.")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Also retry images that failed to describe %d times."
                             % describe.MAX_FAILURES)
    parser.add_argument("--redescribe-stale", action="store_true",
                        help="Also describe again images described with an older prompt "
                             "(after the new ones).")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Describe at most N images in this run.")
    parser.add_argument("--refine", action="store_true",
                        help="Only run the refine pass: describe the documents and screenshots "
                             "again with each library's refine endpoint (no scan, no regular "
                             "describing).")
    parser.add_argument("--endpoint", metavar="NAME",
                        help="Describe with this endpoint (or group) instead of each "
                             "library's own.")
    parser.add_argument("--list-endpoints", action="store_true",
                        help="List the configured LLM endpoints and exit.")
    parser.add_argument("--test-endpoint", metavar="NAME",
                        help="Describe one image with this endpoint, print the result "
                             "and exit.")
    parser.add_argument("--image", metavar="PATH",
                        help="The image for --test-endpoint (default: a sample picture).")
    parser.add_argument("--allow-mass-removal", action="store_true",
                        help="Mark files missing even when most of a library seems to "
                             "have vanished.")
    parser.add_argument("--verbose", action="store_true",
                        help="List every added or changed file (by default only the "
                             "first %d of each kind per library)." % FILE_LOG_LIMIT)
    args = parser.parse_args(argv)

    if args.list_endpoints or args.test_endpoint:
        # These only read config.json (and talk to the endpoint).
        try:
            config = common.load_config()
        except common.ConfigError as error:
            print("Error: %s" % error)
            return 2
        if args.list_endpoints:
            return list_endpoints(config)
        return test_endpoint(config, args.test_endpoint, args.image)
    ctx = jobs.ConsoleContext("scan")
    found = ctx.abort_file_present()
    if found:
        print("Abort file present (%s); delete it first." % found)
        return 1
    try:
        config = common.load_config()
    except common.ConfigError as error:
        print("Error: %s" % error)
        return 2
    if args.limit is not None and args.limit < 0:
        print("--limit must be 0 or more.")
        return 2
    if args.refine and (args.endpoint or args.redescribe_stale):
        print("--refine uses each library's refine endpoint (see SETTINGS.md); --endpoint and "
              "--redescribe-stale do not go with it.")
        return 2
    if args.endpoint and not args.no_describe and not common.resolve_endpoints(
            config, args.endpoint):
        names = ", ".join(item["name"] for item in common.endpoint_list(config)) or "none"
        print("No enabled endpoint or group is named %s (configured: %s). See "
              "--list-endpoints." % (args.endpoint, names))
        return 2
    if not config.get("libraries"):
        print("No libraries configured. Run settings.py first.")
        return 0
    lock = jobs.WorkerLock()
    if not lock.acquire("scan.py"):
        print("Another job is running: %s. Try again when it has finished."
              % (lock.holder() or "another process"))
        return 1
    try:
        db.init(config, log=ctx.log)
        library_ids = None
        if args.library:
            library_ids = []
            for wanted in args.library:
                library = common.get_library(config, wanted)
                if library is None:
                    print("Unknown library: %s" % wanted)
                    return 2
                if not library.get("enabled", True):
                    ctx.log("Library %s is disabled; skipping it."
                            % common.library_label(library), "warn")
                    continue
                library_ids.append(library["id"])
        helper = common.load_helper(config.get("helper", ""))
        with ctx.stop_on_ctrl_c():
            if args.refine:
                summary = run_refine(config, ctx, library_ids, helper, args)
            else:
                summary = run(config, ctx, library_ids=library_ids, scan_images=not args.no_scan,
                              describe_images=not args.no_describe,
                              newest_first=args.newest_first, retry_failed=args.retry_failed,
                              allow_mass_removal=args.allow_mass_removal, helper=helper,
                              verbose=args.verbose, redescribe_stale=args.redescribe_stale,
                              limit=args.limit, endpoint_name=args.endpoint)
    except (db.DatabaseError, common.ConfigError) as error:
        print("Error: %s" % error)
        return 2
    except KeyboardInterrupt:
        # A second Ctrl+C: what was committed is kept.
        print("\nStopped (Ctrl+C). Most progress is saved; run scan.py again to continue.")
        return 130
    finally:
        lock.release()
    described = summary.get("describe") or {}
    if summary["stopped"]:
        ctx.log("Stopped as asked. Progress saved; run scan.py again to continue.")
    elif described.get("fatal") or described.get("error"):
        # Unattended runs (a scheduled task) can tell this from success.
        ctx.log("Stopped early: %s" % described["error"], "error")
        return 1
    else:
        ctx.log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
