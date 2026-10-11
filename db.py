#!/usr/bin/env python3
"""The image database (data/index.db): schema, connections and migrations.

init() runs once per process at startup. It creates or migrates the schema,
imports the database written by older versions (data/images.db plus the
data/thumbs/*.dat shard files) the first time, and records the configured
libraries. Request handlers and workers then open cheap connections with
connect(); they never do schema work.

The old files are opened read-only and never changed or deleted, so they stay
behind as a backup that older checkouts can still use.

This module uses only the standard library. Pillow is used, when installed,
only to double-check old thumbnails during the one-time import.
"""

import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from urllib.parse import quote

import common
import imageinfo
import jobs

SCHEMA_VERSION = 2

# The schema for SCHEMA_VERSION 1. Later versions add migrations below rather
# than editing this list.
SCHEMA_V1 = (
    """CREATE TABLE IF NOT EXISTS libraries (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id TEXT NOT NULL,
        path TEXT NOT NULL,
        name TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        ctime REAL NOT NULL DEFAULT 0,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        dhash TEXT NOT NULL DEFAULT '',
        taken_at TEXT NOT NULL DEFAULT '',
        taken_source TEXT NOT NULL DEFAULT '',
        tz_offset TEXT NOT NULL DEFAULT '',
        camera TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        exif TEXT NOT NULL DEFAULT '{}',
        info TEXT NOT NULL DEFAULT '',
        described INTEGER NOT NULL DEFAULT 0,
        prompt_ver INTEGER NOT NULL DEFAULT 0,
        provenance TEXT NOT NULL DEFAULT '',
        describe_attempts INTEGER NOT NULL DEFAULT 0,
        describe_error TEXT NOT NULL DEFAULT '',
        hidden INTEGER NOT NULL DEFAULT 0,
        has_preview INTEGER NOT NULL DEFAULT 0,
        missing_since REAL,
        added REAL NOT NULL DEFAULT 0,
        updated REAL NOT NULL DEFAULT 0,
        UNIQUE (library_id, path)
    )""",
    # Lookups by library use the UNIQUE (library_id, path) index or this one;
    # copies and moves are found by content hash.
    "CREATE INDEX IF NOT EXISTS images_hash ON images(content_hash)",
    "CREATE INDEX IF NOT EXISTS images_taken ON images(library_id, taken_at)",
    """CREATE TABLE IF NOT EXISTS read_failures (
        library_id TEXT NOT NULL,
        path TEXT NOT NULL,
        size INTEGER,
        mtime REAL,
        error TEXT NOT NULL DEFAULT '',
        seen REAL,
        PRIMARY KEY (library_id, path)
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        library_id TEXT,
        params TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT '',
        done INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        started REAL,
        finished REAL
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

# thumbs.db holds one JPEG per image id (see cache.ThumbStore). The extra
# index on the key lets the set of ids be read without touching the BLOBs.
THUMBS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS thumbs (image_id INTEGER PRIMARY KEY, jpeg BLOB NOT NULL)",
    "CREATE INDEX IF NOT EXISTS thumbs_ids ON thumbs(image_id)",
)


def create_thumbs_schema(conn):
    """Create the thumbs table (and its index) if needed; no commit."""
    for statement in THUMBS_SCHEMA:
        conn.execute(statement)

# Thumbnails from the old shard files are kept only if their dHash is within
# this many bits of the dHash stored for the row.
THUMB_DHASH_TOLERANCE = 12

# Filesystems on which SQLite's WAL mode is unsafe (it needs shared memory).
NETWORK_FS_TYPES = {
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "afpfs", "9p", "sshfs",
    "fuse.sshfs", "ceph", "glusterfs", "fuse.glusterfs", "lustre", "davfs",
    "fuse.rclone",
}
DRIVE_REMOTE = 4


class DatabaseError(RuntimeError):
    """The database cannot be used as it is."""


class NewerDatabaseError(DatabaseError):
    """The database was written by a newer version of this program."""


# ---------------------------------------------------------------------------
# Connections

def _windows_drive_type(root):
    """GetDriveTypeW for a drive root such as 'X:\\' (0 if it cannot be determined)."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:
        return 0


def _unescape_mount(text):
    # /proc/self/mounts escapes spaces and a few other characters as \ooo.
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def _posix_fs_type(path):
    """The filesystem type holding path, from /proc/self/mounts ('' if unknown)."""
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""
    real = os.path.realpath(path)
    best, best_type = "", ""
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount = _unescape_mount(fields[1])
        prefix = mount.rstrip("/") + "/"
        if (real == mount or real.startswith(prefix) or mount == "/") and len(mount) >= len(best):
            best, best_type = mount, fields[2]
    return best_type


def is_network_path(path):
    """True if path is on a network drive (UNC path, mapped drive, NFS/SMB mount)."""
    if not path:
        return False
    # Compare with forward slashes: "//server/share", "//?/UNC/server/share"
    # and the local device forms "//?/C:/..." and "//./C:/...".
    slashed = path.replace("\\", "/")
    upper = slashed.upper()
    if upper.startswith("//?/UNC/"):
        return True
    if upper.startswith("//?/") or upper.startswith("//./"):
        path = slashed[4:]
    elif slashed.startswith("//"):
        return True
    if os.name == "nt":
        full = os.path.abspath(path)
        if full.replace("\\", "/").startswith("//"):
            return True
        drive = os.path.splitdrive(full)[0]
        if len(drive) == 2 and drive[1] == ":":
            return _windows_drive_type(drive + os.sep) == DRIVE_REMOTE
        return False
    return _posix_fs_type(os.path.abspath(path)) in NETWORK_FS_TYPES


_warned_network = set()
_warn_lock = threading.Lock()
_network_folders = {}


def _warn_network_once(folder):
    with _warn_lock:
        if folder in _warned_network:
            return
        _warned_network.add(folder)
    print("Warning: the data folder %s is on a network drive. SQLite's faster "
          "WAL mode does not work there, so the database uses the slower "
          "rollback journal. Keep the data folder on a local disk if you can."
          % folder, file=sys.stderr)


def connect(path=None, check_same_thread=True):
    """Open a connection to index.db (or another database file).

    Rows come back as sqlite3.Row. The journal is WAL, except on a network
    drive where WAL is unsafe; there the rollback journal is used and a
    warning is printed once.
    """
    path = path or common.db_path()
    folder = os.path.dirname(os.path.abspath(path))
    common.ensure_dir(folder)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    # Connections are opened per request, so the drive check is remembered.
    network = _network_folders.get(folder)
    if network is None:
        network = _network_folders[folder] = is_network_path(folder)
    wanted = "delete" if network else "wal"
    if network:
        _warn_network_once(folder)
    current = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if current != wanted:
        try:
            conn.execute("PRAGMA journal_mode=%s" % wanted.upper())
        except sqlite3.OperationalError as error:
            # Another connection is busy; the mode is changed on a later open.
            print("Warning: could not set journal mode on %s: %s" % (path, error),
                  file=sys.stderr)
    # NORMAL is durable in WAL mode; the rollback journal needs FULL for that.
    conn.execute("PRAGMA synchronous=%s" % ("NORMAL" if wanted == "wal" else "FULL"))
    return conn


def connect_readonly(path=None):
    """Open index.db (or another database file) for reading only.

    For tools that must never change the database (compare_models.py).
    Rows come back as sqlite3.Row; any write raises sqlite3.Error. Raises
    DatabaseError when the file does not exist or cannot be opened.
    """
    path = path or common.db_path()
    if not os.path.isfile(path):
        raise DatabaseError("%s does not exist; run scan.py first." % path)
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30)
    except sqlite3.Error as error:
        raise DatabaseError("Could not open %s: %s" % (path, error))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as error:
        conn.close()
        raise DatabaseError("Could not open %s: %s" % (path, error))
    return conn


def user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


# ---------------------------------------------------------------------------
# Migrations

def _migrate_to_1(conn):
    for statement in SCHEMA_V1:
        conn.execute(statement)


# Rows that may be 360-degree photos: a 2:1 size (within the tolerance of
# imageinfo.is_panorama_aspect) or a .insp file.
_POSSIBLE_360_SQL = """
    UPDATE images SET projection = ?
    WHERE projection = '' AND ((height > 0 AND width >= height * ? AND width <= height * ?)
        OR lower(name) LIKE '%.insp')"""


def mark_possible_360(conn):
    """Set projection '?' on rows that may be 360 photos; returns the count.

    The scanner's back-fill stage reads those again and stores what they
    really are (see scan.backfill). No commit.
    """
    tolerance = imageinfo.PANORAMA_ASPECT_TOLERANCE
    return conn.execute(_POSSIBLE_360_SQL, (imageinfo.PROJECTION_UNKNOWN,
                                            2.0 * (1.0 - tolerance),
                                            2.0 * (1.0 + tolerance))).rowcount


def _migrate_to_2(conn):
    # 360-degree photos: "" (ordinary), "equirectangular", "dual_fisheye",
    # or "?" (not checked yet).
    conn.execute("ALTER TABLE images ADD COLUMN projection TEXT NOT NULL DEFAULT ''")
    mark_possible_360(conn)


# Ordered (version, function, rewrites_data). A migration that rewrites
# existing data gets a backup copy (index.db.bak-v<old version>) first.
MIGRATIONS = (
    (1, _migrate_to_1, False),
    (2, _migrate_to_2, True),
)


def _newer_error(version):
    return NewerDatabaseError(
        "The database was created by a newer version of this program (schema "
        "%d; this version understands up to %d). Update the code, or point "
        "SETTINGS_BASE at a different folder." % (version, SCHEMA_VERSION))


def _backup(conn, path, version):
    target = "%s.bak-v%d" % (path, version)
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def migrate(conn, path=None, log=None):
    """Bring the schema up to SCHEMA_VERSION. Returns the starting version.

    Each step runs in its own BEGIN IMMEDIATE transaction, so a crash leaves
    the database at the last completed version.
    """
    log = log or print
    path = path or common.db_path()
    start = user_version(conn)
    if start > SCHEMA_VERSION:
        raise _newer_error(start)
    for target, func, rewrites_data in MIGRATIONS:
        version = user_version(conn)
        if target <= version:
            continue
        # A database created by this call has nothing worth a backup.
        if rewrites_data and start > 0:
            log("Backed up the database to " + _backup(conn, path, version))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have migrated while we waited for the lock.
            if user_version(conn) < target:
                func(conn)
                conn.execute("PRAGMA user_version=%d" % target)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return start


# ---------------------------------------------------------------------------
# Startup

def register_libraries(conn, config):
    """Record each configured library id in the libraries table (name kept current).

    Writes only when a library is new or renamed, so a start-up with nothing
    to change never waits for (or blocks) a writer such as a running scan.
    """
    known = {row[0]: row[1] for row in conn.execute("SELECT id, name FROM libraries")}
    changes = []
    for library in config.get("libraries", []):
        library_id = library.get("id")
        if not library_id:
            continue
        name = common.library_label(library)
        if known.get(library_id) != name:
            changes.append((library_id, name))
            known[library_id] = name
    if not changes:
        return
    now = time.time()
    with conn:
        for library_id, name in changes:
            conn.execute("INSERT OR IGNORE INTO libraries (id, name, created) "
                         "VALUES (?, ?, ?)", (library_id, name, now))
            conn.execute("UPDATE libraries SET name = ? WHERE id = ? AND name <> ?",
                         (name, library_id, name))


def _settle_library_ids(config, log):
    """Make sure every library has an id, saved in config.json, that config uses too.

    Called with init.lock held. config.json is read again here: config may
    be a copy loaded before another process gave the libraries their ids.
    Ids already saved always win, so the same library never gets two ids.
    """
    path = common.config_path()
    saved = common.load_config() if os.path.exists(path) else None
    if saved is None:
        if common.ensure_library_ids(config) and config.get("libraries"):
            common.save_config(config)
            log("Assigned ids to new libraries in " + path)
        return
    # Saved libraries without an id take the one the caller already gave them.
    changed = common.copy_library_ids(saved, config, only_missing=True)
    if common.ensure_library_ids(saved) or changed:
        common.save_config(saved)
        log("Assigned ids to new libraries in " + path)
    common.copy_library_ids(config, saved)
    # Libraries that are only in the caller's copy keep (or get) their own.
    common.ensure_library_ids(config)


def init(config=None, log=None):
    """Prepare the database for this process. Call once at startup.

    Assigns (and saves) ids for libraries that lack one -- config gets the
    ids saved in config.json, even when it was loaded before they were
    assigned --, imports the old database on first run, creates or migrates
    the schema, refuses a database written by a newer version, and registers
    the configured libraries. config defaults to config.json.
    Returns a report dict: {"created", "from_version", "version", "legacy"}
    where "legacy" holds the import counts when an import happened.
    """
    log = log or print
    common.ensure_dir(common.data_dir())
    path = common.db_path()
    report = {"created": False, "from_version": None, "version": SCHEMA_VERSION,
              "legacy": None}
    # Startup work is serialized across processes, so a server and a scan
    # started together cannot both assign library ids, import or migrate.
    lock = jobs.WorkerLock(os.path.join(common.data_dir(), "init.lock"))
    if not lock.acquire("database setup"):
        log("Waiting for another process to finish setting up the database (%s)"
            % (lock.holder() or "unknown"))
        if not lock.acquire("database setup", timeout=900):
            raise DatabaseError("Another process is still setting up the database: "
                                + (lock.holder() or "unknown"))
    try:
        if config is None:
            config = common.load_config()
        _settle_library_ids(config, log)
        if not os.path.exists(path) and os.path.exists(common.legacy_db_path()):
            report["legacy"] = import_legacy(config, log=log)
            report["created"] = True
        conn = connect(path)
        try:
            version = user_version(conn)
            report["from_version"] = version
            if version > SCHEMA_VERSION:
                raise _newer_error(version)
            if version < SCHEMA_VERSION:
                migrate(conn, path, log)
                if version == 0:
                    report["created"] = True
                else:
                    log("Upgraded the database from version %d to %d"
                        % (version, SCHEMA_VERSION))
            register_libraries(conn, config)
        finally:
            conn.close()
        thumbs = connect(common.thumbs_db_path())
        try:
            create_thumbs_schema(thumbs)
            thumbs.commit()
        finally:
            thumbs.close()
    finally:
        lock.release()
    return report


# ---------------------------------------------------------------------------
# Queries shared by the scanner, the server and the tools

def library_counts(conn, prompt_ver=0, max_failures=common.MAX_DESCRIBE_FAILURES):
    """Per-library counts: {library_id: {total, described, stale, failed, gave_up,
    missing, hidden}}.

    total, described, stale, failed, gave_up and hidden count present files
    only (missing_since IS NULL); missing counts files that have
    disappeared. stale = described with a prompt_ver older than prompt_ver;
    failed = not described after at least one failed attempt; gave_up = not
    described after max_failures or more (skipped until a run retries
    them). Registered libraries with no images are included with zeros.
    """
    keys = ("total", "described", "stale", "failed", "gave_up", "missing", "hidden")
    result = {}
    for row in conn.execute("SELECT id FROM libraries"):
        result[row[0]] = dict.fromkeys(keys, 0)
    sql = """
        SELECT library_id,
            SUM(missing_since IS NULL),
            SUM(missing_since IS NULL AND described = 1),
            SUM(missing_since IS NULL AND described = 1 AND prompt_ver < ?),
            SUM(missing_since IS NULL AND described = 0 AND describe_attempts > 0),
            SUM(missing_since IS NULL AND described = 0 AND describe_attempts >= ?),
            SUM(missing_since IS NOT NULL),
            SUM(missing_since IS NULL AND hidden = 1)
        FROM images GROUP BY library_id"""
    for row in conn.execute(sql, (prompt_ver, max_failures)):
        result[row[0]] = {key: int(value or 0) for key, value in zip(keys, row[1:])}
    return result


def library_names(conn):
    """{library_id: name} for every library recorded in the database."""
    return {row[0]: row[1] for row in conn.execute("SELECT id, name FROM libraries")}


def orphan_library_ids(conn, config):
    """Library ids found in the database but not in the config, sorted.

    Orphans are never deleted automatically; the UI offers to re-attach them
    to a configured library or delete them.
    """
    configured = {library.get("id") for library in config.get("libraries", [])}
    found = {row[0] for row in conn.execute("SELECT id FROM libraries")}
    found.update(row[0] for row in conn.execute("SELECT DISTINCT library_id FROM images"))
    return sorted(found - configured)


# ---------------------------------------------------------------------------
# One-time import of the old database

def orphan_library_id(name):
    """The stable id given to an old library name that is not in the config."""
    return "lib_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def _readonly_uri(path):
    """A SQLite URI that opens path read-only (also for UNC paths)."""
    full = os.path.abspath(path).replace("\\", "/")
    if full.startswith("//"):
        prefix = "file://"          # file:////server/share/... keeps the UNC host
    elif full.startswith("/"):
        prefix = "file://"
    else:
        prefix = "file:///"         # file:///C:/...
    return prefix + quote(full, safe="/:") + "?mode=ro"


def _remove_tree(path):
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _open_legacy(path, log):
    """Open the old images.db for reading. Returns (connection, copy folder or None).

    Normally the file is opened read-only. When a rollback journal or WAL
    file lies next to it (the old scanner was stopped in the middle of a
    transaction), SQLite has to recover it, which needs write access; then
    the files are copied to data/tmp and the copy is opened instead, so the
    originals are never changed.
    """
    side_files = [suffix for suffix in ("-journal", "-wal") if os.path.exists(path + suffix)]
    if not side_files:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except BaseException:
            conn.close()
            raise
        return conn, None
    log("The old database was not closed cleanly (%s found); importing from a "
        "recovered copy." % ", ".join(os.path.basename(path) + s for s in side_files))
    folder = os.path.join(common.ensure_dir(common.tmp_dir()),
                          "legacy-import-%d" % os.getpid())
    _remove_tree(folder)
    os.makedirs(folder)
    copy = os.path.join(folder, os.path.basename(path))
    conn = None
    try:
        for suffix in [""] + side_files:
            shutil.copyfile(path + suffix, copy + suffix)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        # Reading recovers the copy: the journal is rolled back (or the WAL
        # replayed) there.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        if conn is not None:
            conn.close()
        _remove_tree(folder)
        raise
    return conn, folder


def _remove_db_files(path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def _fsync_file(path):
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def _finite(value, fixed):
    """Copy of a JSON value with NaN/Infinity replaced by None."""
    if isinstance(value, float) and not math.isfinite(value):
        fixed.append(True)
        return None
    if isinstance(value, dict):
        return {key: _finite(item, fixed) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item, fixed) for item in value]
    return value


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _valid_dhash(text):
    return isinstance(text, str) and re.match(r"^[0-9a-fA-F]{16}$", text) is not None


def _hamming(first, second):
    return bin(int(first, 16) ^ int(second, 16)).count("1")


def _load_pillow():
    """Pillow's Image module, or None when Pillow is not installed."""
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def _thumb_dhash(image_module, data):
    """dHash of JPEG bytes (images.compute_dhash, as the scanner uses); raises if not a JPEG."""
    import images       # Pillow is installed when an image module is given
    with image_module.open(io.BytesIO(data)) as image:
        if image.format != "JPEG":
            raise ValueError("not a JPEG")
        image.load()
        return images.compute_dhash(image)


def check_legacy_thumb(data, dhash, image_module=None):
    """True if old thumbnail bytes look like the right picture.

    The bytes must start with the JPEG marker. With Pillow available they
    must also decode, and when the row has a dHash, the thumbnail's dHash must
    be within THUMB_DHASH_TOLERANCE bits of it (a thumbnail that points at
    the wrong image is far off).
    """
    if not data or not data.startswith(b"\xff\xd8"):
        return False
    if image_module is None:
        return True
    try:
        computed = _thumb_dhash(image_module, data)
    except Exception:
        return False
    if _valid_dhash(dhash):
        return _hamming(computed, dhash) <= THUMB_DHASH_TOLERANCE
    return True


class _ShardReader:
    """Reads slices of the old thumb-NNNN.dat files, keeping them open."""

    def __init__(self, directory):
        self.directory = directory
        self.handles = {}

    def read(self, index, offset, length):
        if not all(isinstance(value, int) for value in (index, offset, length)):
            return None
        if index < 0 or offset < 0 or not 0 < length <= 8 * 1024 * 1024:
            return None
        handle = self.handles.get(index)
        if handle is None:
            path = os.path.join(self.directory, "thumb-%04d.dat" % index)
            try:
                handle = open(path, "rb")
            except OSError:
                return None
            self.handles[index] = handle
        handle.seek(offset)
        data = handle.read(length)
        return data if len(data) == length else None

    def close(self):
        for handle in self.handles.values():
            handle.close()
        self.handles = {}


def _table_columns(conn, table):
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


_INSERT_IMAGE_SQL = """
    INSERT OR IGNORE INTO images (
        id, library_id, path, name, size, mtime, ctime, width, height,
        content_hash, dhash, taken_at, taken_source, tz_offset, camera, lat, lon,
        exif, info, described, prompt_ver, provenance, hidden, has_preview,
        missing_since, added, updated)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            0, 0, NULL, ?, ?)"""


def _convert_legacy_row(row, library_id, now, counts):
    """Values for _INSERT_IMAGE_SQL from one old images row (a dict).

    The capture date, camera and position come from the stored EXIF through
    imageinfo, the same rules the scanner uses for new files.
    """
    fixed = []
    try:
        exif = json.loads(row.get("exif") or "{}")
    except ValueError:
        exif = {}
    if not isinstance(exif, dict):
        exif = {}
    exif = _finite(exif, fixed)
    if fixed:
        counts["exif_fixed"] += 1
    info = row.get("info") or ""
    provenance = ""
    if info:
        try:
            parsed = json.loads(info)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            info_fixed = []
            parsed = _finite(parsed, info_fixed)
            if info_fixed:
                info = json.dumps(parsed, ensure_ascii=False)
            model = parsed.get("_model_name")
            if isinstance(model, str) and model:
                provenance = json.dumps({"model": model})
    width = int(row.get("width") or 0)
    height = int(row.get("height") or 0)
    # The old scanner stored the size before EXIF rotation.
    if exif.get("Orientation") in (5, 6, 7, 8):
        width, height = height, width
    path = str(row["path"]).replace("\\", "/")
    name = row.get("name") or path.rsplit("/", 1)[-1]
    mtime = row.get("mtime") if _is_number(row.get("mtime")) else None
    meta = imageinfo.exif_metadata(exif, name, mtime)
    dhash = row.get("dhash") or ""
    return (
        row["id"], library_id, path, name,
        int(row.get("size") or 0), float(row.get("mtime") or 0),
        float(row.get("ctime") or 0), width, height,
        dhash.lower() if _valid_dhash(dhash) else "",
        meta["taken_at"], meta["taken_source"], meta["tz_offset"], meta["camera"],
        meta["lat"], meta["lon"], json.dumps(exif, ensure_ascii=False, allow_nan=False), info,
        1 if row.get("described") else 0, int(row.get("prompt_ver") or 0),
        provenance, now, now,
    )


def import_legacy(config, log=None):
    """Copy the old images.db and thumbnail shards into index.db and thumbs.db.

    Builds both files under temporary names and moves them into place only
    when the import has finished, so an interrupted import simply runs again
    next time. The old files are opened read-only and never changed (a copy
    is read when they need recovery, see _open_legacy). Image
    ids are kept, so links stay valid. Old library names map to configured
    library ids by exact name; other names become orphan libraries.

    Returns counts: rows, skipped, thumbs_ok, thumbs_rejected, thumbs_missing,
    exif_fixed, libraries ({library_id: rows}) and orphans ([{id, name, rows}]).
    Raises DatabaseError when the old database cannot be read.
    """
    log = log or print
    legacy_path = common.legacy_db_path()
    target = common.db_path()
    thumbs_target = common.thumbs_db_path()
    tmp = target + ".tmp"
    thumbs_tmp = thumbs_target + ".tmp"
    for path in (tmp, thumbs_tmp):
        _remove_db_files(path)
    counts = {"rows": 0, "skipped": 0, "thumbs_ok": 0, "thumbs_rejected": 0,
              "thumbs_missing": 0, "exif_fixed": 0, "libraries": {}, "orphans": []}
    image_module = _load_pillow()
    if image_module is None:
        log("Pillow is not installed, so old thumbnails are only checked for a "
            "JPEG header.")
    name_to_id = {}
    for library in config.get("libraries", []):
        if library.get("id") and isinstance(library.get("name"), str):
            name_to_id.setdefault(library["name"], library["id"])
    orphans = {}
    now = time.time()
    log("Importing the old database " + legacy_path)
    source = copy_dir = None
    shards = _ShardReader(common.legacy_thumb_dir())
    dest = sqlite3.connect(tmp)
    thumbs = sqlite3.connect(thumbs_tmp)
    try:
        source, copy_dir = _open_legacy(legacy_path, log)
        for conn in (dest, thumbs):
            # Temporary files: speed over crash safety (a crash restarts the import).
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
        _migrate_to_1(dest)
        create_thumbs_schema(thumbs)
        for library in config.get("libraries", []):
            if library.get("id"):
                dest.execute("INSERT OR IGNORE INTO libraries (id, name, created) "
                             "VALUES (?, ?, ?)",
                             (library["id"], common.library_label(library), now))
        columns = _table_columns(source, "images")
        cursor = source.execute("SELECT * FROM images ORDER BY id") if columns else None
        while cursor is not None:
            batch = cursor.fetchmany(500)
            if not batch:
                break
            for record in batch:
                row = dict(zip(record.keys(), record))
                name = row.get("library")
                if not isinstance(name, str) or not row.get("path") or row.get("id") is None:
                    counts["skipped"] += 1
                    continue
                library_id = name_to_id.get(name)
                if library_id is None:
                    library_id = orphans.get(name)
                    if library_id is None:
                        library_id = orphan_library_id(name)
                        orphans[name] = library_id
                        dest.execute("INSERT OR IGNORE INTO libraries (id, name, created) "
                                     "VALUES (?, ?, ?)", (library_id, name or "(unnamed)", now))
                try:
                    values = _convert_legacy_row(row, library_id, now, counts)
                except (TypeError, ValueError):
                    # A row with unusable values (hand-edited, say) is left out.
                    counts["skipped"] += 1
                    continue
                if dest.execute(_INSERT_IMAGE_SQL, values).rowcount != 1:
                    counts["skipped"] += 1
                    continue
                counts["rows"] += 1
                counts["libraries"][library_id] = counts["libraries"].get(library_id, 0) + 1
                data = shards.read(row.get("thumb_shard"), row.get("thumb_offset"),
                                   row.get("thumb_length"))
                if data is None:
                    counts["thumbs_missing"] += 1
                elif check_legacy_thumb(data, row.get("dhash"), image_module):
                    thumbs.execute("INSERT OR REPLACE INTO thumbs (image_id, jpeg) "
                                   "VALUES (?, ?)", (row["id"], data))
                    counts["thumbs_ok"] += 1
                else:
                    counts["thumbs_rejected"] += 1
            if counts["rows"] and counts["rows"] % 5000 < len(batch):
                log("  %d images imported" % counts["rows"])
        # The rows are in the version 1 layout; bring them up to date.
        for version, func, _rewrites_data in MIGRATIONS:
            if version > 1:
                func(dest)
        dest.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        dest.commit()
        thumbs.commit()
    except BaseException as error:
        dest.close()
        thumbs.close()
        if source is not None:
            source.close()
        shards.close()
        _remove_tree(copy_dir)
        for path in (tmp, thumbs_tmp):
            try:
                _remove_db_files(path)
            except OSError:
                pass
        if isinstance(error, sqlite3.Error):
            raise DatabaseError(
                "Could not import the old database %s: %s. It was left as it is; "
                "restore it from a backup (or move it away to start with an empty "
                "index) and run again." % (legacy_path, error)) from error
        raise
    dest.close()
    thumbs.close()
    source.close()
    shards.close()
    _remove_tree(copy_dir)
    counts["orphans"] = [{"id": library_id, "name": name,
                          "rows": counts["libraries"].get(library_id, 0)}
                         for name, library_id in sorted(orphans.items())]
    for path in (tmp, thumbs_tmp):
        _fsync_file(path)
    # Any thumbs.db or WAL files left from an earlier attempt belong to no
    # index.db, so they are replaced. index.db goes last: its presence marks
    # a finished import.
    for path in (thumbs_target, target):
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    os.replace(thumbs_tmp, thumbs_target)
    os.replace(tmp, target)
    log("Imported %d images (%d skipped). Thumbnails: %d kept, %d rejected "
        "(rebuilt on the next scan), %d missing." % (
            counts["rows"], counts["skipped"], counts["thumbs_ok"],
            counts["thumbs_rejected"], counts["thumbs_missing"]))
    if counts["exif_fixed"]:
        log("Fixed invalid EXIF numbers in %d images." % counts["exif_fixed"])
    for orphan in counts["orphans"]:
        log("Library %r (%d images) is not in config.json; it is kept as %s "
            "until you re-attach or delete it." % (orphan["name"], orphan["rows"],
                                                   orphan["id"]))
    return counts
