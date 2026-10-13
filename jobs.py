#!/usr/bin/env python3
"""Job plumbing shared by the command line tools and the web server.

Every long-running function (scan, describe, export, ...) takes a context
object: it reports progress and log lines through it and asks it whether to
stop. JobContext is the base used by the server; ConsoleContext prints to the
terminal and also honors abort files. WorkerLock makes sure only one process
runs jobs at a time.

The web server queues jobs with JobManager: one worker thread runs them one
at a time from the jobs table in data/jobs.db (so the queue survives a
restart, and never waits for a scan that is writing index.db), each with a
UIContext that keeps what the browser shows (progress, rate, ETA, the log
tail and recent errors).

This module uses only the standard library. The job runners import the
scanner and the describer (which need Pillow) only when a job runs.
"""

import collections
import contextlib
import json
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime

import common

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None


LOG_LEVELS = ("info", "warn", "error")


class JobStopped(Exception):
    """Raised by JobContext.check() when the job has been asked to stop."""


class JobContext:
    """Progress, log and stop/pause state for one job. Thread-safe.

    Work is split into stages ("scan", "previews", "describe", ...). Each
    stage has a done/total counter; step() advances it. Long loops call
    check() (or should_stop() and wait_if_paused()) between items.
    """

    def __init__(self, name="", log_limit=200):
        self.name = name
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._running = threading.Event()   # cleared while paused
        self._running.set()
        self._log = collections.deque(maxlen=log_limit)
        self._secrets = set()
        self.started = time.time()
        self.stage_name = ""
        self.stage_started = self.started
        self.done = 0
        self.quick = 0          # of done: items that took no real work (see step)
        self.total = 0
        self.item = ""
        self.counts = {}
        self.warnings = 0
        self.errors = 0
        self.interrupted = ""   # why the job was stopped to be resumed later (interrupt)
        self._removed = set()

    def add_secret(self, value):
        """Mask this value (an API key, say) in every later log line."""
        if isinstance(value, str) and len(value.strip()) >= 4:
            with self._lock:
                self._secrets.add(value)

    def log(self, message, level="info"):
        """Record a log line. level is "info", "warn" or "error"."""
        if level not in LOG_LEVELS:
            level = "info"
        with self._lock:
            entry = {"time": time.time(), "level": level,
                     "message": common.redact(message, self._secrets)}
            self._log.append(entry)
            if level == "warn":
                self.warnings += 1
            elif level == "error":
                self.errors += 1
        self._emit(entry)

    def stage(self, name, total=0):
        """Start a new stage; resets the done/total counter."""
        with self._lock:
            self.stage_name = name
            self.stage_started = time.time()
            self.done = 0
            self.quick = 0
            self.total = max(0, int(total or 0))
            self.item = ""
        self._stage_started()

    def set_total(self, total):
        with self._lock:
            self.total = max(0, int(total or 0))

    def step(self, n=1, item=None, quick=False):
        """Count n finished items in the current stage.

        quick marks items that needed no real work (a file a scan finds
        unchanged): they count as done but not towards the rate.
        """
        with self._lock:
            self.done += n
            if quick:
                self.quick += n
            if item is not None:
                self.item = str(item)
        self._progress()

    def count(self, key, n=1):
        """Add n to a named summary counter (added, updated, failed, ...)."""
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + n

    def request_stop(self):
        """Ask the job to stop at the next check. Also ends a pause."""
        self._stop.set()
        self._running.set()

    def should_stop(self):
        return self._stop.is_set()

    def interrupt(self, message):
        """Stop at the next check, as a job to resume later rather than a cancelled one.

        The web server's jobs then end "interrupted" with message as their
        error (the model server's daily stop time uses this).
        """
        with self._lock:
            first = not self.interrupted
            self.interrupted = message or "Interrupted."
        if first:
            self.log(self.interrupted, "warn")
        self.request_stop()

    def remove_library(self, library_id):
        """Say that a library was removed from the settings while the job runs.

        A job that works on several libraries skips the rest of its work on
        that one (see library_removed): nothing more of it is described.
        """
        with self._lock:
            self._removed.add(library_id)

    def library_removed(self, library_id):
        """True when the library was removed from the settings during the job."""
        with self._lock:
            return library_id in self._removed

    def pause(self):
        self._running.clear()

    def resume(self):
        self._running.set()

    @property
    def paused(self):
        return not self._running.is_set()

    def wait_if_paused(self, poll=0.5):
        """Block while the job is paused (returns early if a stop is requested)."""
        while not self._running.is_set() and not self.should_stop():
            self._running.wait(poll)

    def check(self):
        """Wait out a pause, then raise JobStopped if a stop was requested."""
        self.wait_if_paused()
        if self.should_stop():
            raise JobStopped()

    def snapshot(self, log_tail=50):
        """A JSON-friendly copy of the current state."""
        with self._lock:
            return {
                "name": self.name,
                "stage": self.stage_name,
                "done": self.done,
                "total": self.total,
                "item": self.item,
                "counts": dict(self.counts),
                "warnings": self.warnings,
                "errors": self.errors,
                "paused": self.paused,
                "stop_requested": self._stop.is_set(),
                "started": self.started,
                "stage_started": self.stage_started,
                "log_tail": list(self._log)[-log_tail:] if log_tail else [],
            }

    # Hooks for subclasses that display progress.
    def _emit(self, entry):
        pass

    def _stage_started(self):
        pass

    def _progress(self):
        pass


def library_removed(ctx, library_id):
    """ctx.library_removed(library_id) for any context (False for those without it)."""
    check = getattr(ctx, "library_removed", None)
    return bool(check is not None and check(library_id))


class ConsoleContext(JobContext):
    """A JobContext for the command line.

    Prints "[HH:MM:SS] message" lines and at most one progress line per
    progress_interval seconds. should_stop() is also true once an abort file
    exists: data/abort, or "abort" / "abort.txt" in the current directory.
    """

    def __init__(self, name="", stream=None, progress_interval=1.0,
                 check_interval=0.5):
        JobContext.__init__(self, name)
        self.stream = stream
        self.progress_interval = progress_interval
        self.check_interval = check_interval
        self.abort_found = None
        self._print_lock = threading.Lock()
        self._last_progress = 0.0
        self._last_check = None
        self._finished_stage = False

    @staticmethod
    def abort_files():
        return [common.abort_path(), os.path.abspath("abort"),
                os.path.abspath("abort.txt")]

    def abort_file_present(self):
        """The path of an existing abort file, or None."""
        for path in self.abort_files():
            if os.path.exists(path):
                return path
        return None

    def should_stop(self):
        if JobContext.should_stop(self):
            return True
        now = time.monotonic()
        if self._last_check is None or now - self._last_check >= self.check_interval:
            self._last_check = now
            found = self.abort_file_present()
            if found:
                self.abort_found = found
                self.request_stop()
                self.log("Stopping: found " + found)
        return JobContext.should_stop(self)

    @contextlib.contextmanager
    def stop_on_ctrl_c(self):
        """While inside, the first Ctrl+C asks the work to stop, as the abort file does.

        The work then stops at its next safe point and keeps what it has
        done; a second Ctrl+C raises KeyboardInterrupt at once. Outside the
        main thread nothing changes.
        """
        def handler(_signum, _frame):
            if JobContext.should_stop(self):
                raise KeyboardInterrupt
            self.log("Ctrl+C: stopping after the work in progress (press Ctrl+C again to "
                     "stop at once).", "warn")
            self.request_stop()

        try:
            previous = signal.signal(signal.SIGINT, handler)
        except ValueError:
            previous = None         # not the main thread
        try:
            yield self
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _print(self, text):
        stream = self.stream or sys.stdout
        line = "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), text)
        with self._print_lock:
            try:
                stream.write(line)
            except UnicodeEncodeError:
                # A console or pipe that cannot show some file name characters.
                encoding = getattr(stream, "encoding", None) or "ascii"
                stream.write(line.encode(encoding, "backslashreplace").decode(encoding))
            stream.flush()

    def _emit(self, entry):
        prefix = {"warn": "Warning: ", "error": "Error: "}.get(entry["level"], "")
        self._print(prefix + entry["message"])

    def _stage_started(self):
        with self._lock:
            name, total = self.stage_name, self.total
            self._last_progress = time.monotonic()
            self._finished_stage = False
        self._print(name + (" (%d items)" % total if total else ""))

    def _progress(self):
        now = time.monotonic()
        with self._lock:
            finished = bool(self.total) and self.done >= self.total
            if finished and self._finished_stage:
                return
            if not finished and now - self._last_progress < self.progress_interval:
                return
            self._last_progress = now
            self._finished_stage = finished
            if self.total:
                text = "%s: %d/%d (%d%%)" % (self.stage_name, self.done, self.total,
                                             min(100, self.done * 100 // self.total))
            else:
                text = "%s: %d" % (self.stage_name, self.done)
            if self.item:
                text += " " + self.item
        self._print(text)


class WorkerLock:
    """A cross-process lock on data/worker.lock (or another file).

    Held through an open file handle (msvcrt.locking on Windows, fcntl.flock
    elsewhere), so the operating system releases it when the holding process
    exits or crashes; there is no stale-PID guessing. The holder writes its
    label, PID and start time into the file so others can say who is busy.
    """

    # Windows byte-range locks also block reads, so the lock covers one byte
    # well past the label text, leaving the text readable by other processes.
    LOCK_OFFSET = 1 << 20

    def __init__(self, path=None):
        self.path = path or common.lock_path()
        self._fd = None
        self._info = None
        self._thread_lock = threading.Lock()

    @property
    def held(self):
        """True while this object holds the lock."""
        return self._fd is not None

    def _open(self):
        common.ensure_dir(os.path.dirname(os.path.abspath(self.path)))
        return os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)

    @classmethod
    def _try_lock(cls, fd):
        try:
            if msvcrt is not None:
                os.lseek(fd, cls.LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    @classmethod
    def _unlock(cls, fd):
        try:
            if msvcrt is not None:
                os.lseek(fd, cls.LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def acquire(self, owner_label, timeout=0):
        """Take the lock. Returns False if another holder keeps it past timeout seconds."""
        with self._thread_lock:
            if self._fd is not None:
                self._write_info(owner_label)
                return True
            deadline = time.monotonic() + max(0.0, float(timeout or 0))
            # A few quick retries ride out holder() briefly probing the lock.
            quick_tries = 3
            while True:
                fd = self._open()
                if self._try_lock(fd):
                    break
                os.close(fd)
                quick_tries -= 1
                if quick_tries <= 0 and time.monotonic() >= deadline:
                    return False
                time.sleep(0.05 if quick_tries > 0 else 0.2)
            self._fd = fd
            self._write_info(owner_label)
            return True

    def _write_info(self, owner_label):
        self._info = {"owner": str(owner_label), "pid": os.getpid(),
                      "started": time.time()}
        data = json.dumps(self._info, ensure_ascii=False).encode("utf-8")
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, data)
        except OSError:
            pass

    def release(self):
        """Give up the lock (does nothing if this object does not hold it)."""
        with self._thread_lock:
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
            self._info = None
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            self._unlock(fd)
            os.close(fd)

    @staticmethod
    def describe(info):
        """'scan (pid 1234, since 2026-01-02 03:04:05)' from a holder info dict."""
        if not info:
            return ""
        text = str(info.get("owner") or "another process")
        details = []
        if info.get("pid"):
            details.append("pid %s" % info["pid"])
        try:
            details.append("since " + datetime.fromtimestamp(
                float(info["started"])).strftime("%Y-%m-%d %H:%M:%S"))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            pass
        return text + (" (%s)" % ", ".join(details) if details else "")

    def holder_info(self):
        """{owner, pid, started} of the current holder, or None if the lock is free."""
        if self._fd is not None:
            return dict(self._info or {})
        # Probe: if the lock can be taken, nobody holds it and any text left in
        # the file is from a process that has exited.
        fd = self._open()
        try:
            if self._try_lock(fd):
                self._unlock(fd)
                return None
        finally:
            os.close(fd)
        try:
            with open(self.path, "rb") as handle:
                info = json.loads(handle.read(4096).decode("utf-8"))
        except (OSError, ValueError):
            info = None
        return info if isinstance(info, dict) else {"owner": "another process"}

    def holder(self):
        """Who holds the lock, as text ('' when it is free)."""
        return self.describe(self.holder_info())

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.release()


# ---------------------------------------------------------------------------
# Jobs run by the web server

ACTIVE_STATES = ("running", "paused", "cancelling")
FINISHED_STATES = ("done", "failed", "cancelled", "interrupted")
# A job in one of these states makes an identical new one unnecessary.
TWIN_STATES = ("queued", "running", "paused")
# The stage of a job that waits for another process (scan.py) to finish.
WAITING_STAGE = "waiting for another scan to finish"
# How the server's jobs name themselves when they hold the WorkerLock.
WEB_JOB_LABEL = "web server job"
LOG_LINES = 1000            # log lines kept for the browser (all jobs together)
ERROR_LINES = 200           # warnings and errors kept per job
RATE_WINDOW = 120.0         # seconds over which the rate is measured
RECENT_JOBS = 20            # finished jobs listed in a snapshot
SUMMARY_LIST_LIMIT = 20     # list items kept in a stored job summary
PROGRESS_SAVE_SECONDS = 5.0  # how often a running job's progress is written to jobs.db
INTERRUPTED_MESSAGE = "The server stopped while this job was running; resume it to continue."
# Stages as the log names them (the pages use the same words).
STAGE_LABELS = {
    "scan": "Looking for new and changed pictures",
    "previews": "Building previews",
    "describe": "Describing pictures",
    "refine": "Describing documents and screenshots again",
    "embed": "Updating search by meaning",
    "delete": "Deleting indexed data",
    "export": "Exporting pictures",
    "zip": "Writing the zip file",
    WAITING_STAGE: "Waiting for another scan to finish",
    "download": "Downloading place names",
    "build places": "Building the place index",
    "places": "Adding place names",
    "proxies": "Making playable copies of videos",
    "looks": "Making the looks of pictures",
    "drives": "Describing drives",
    "drive_previews": "Making sped-up previews of drives",
    "faces": "Finding faces",
    "people": "Grouping faces into people",
    "delete faces": "Deleting face data",
}
# The job queue lives in data/jobs.db, a database of its own: a running scan
# keeps index.db's write lock almost all the time, and adding, pausing or
# cancelling a job must never wait for it.
JOBS_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
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
)"""


def jobs_db_path():
    """data/jobs.db, the web server's job queue."""
    return os.path.join(common.data_dir(), "jobs.db")


def connect_jobs_db(path=None):
    """Open data/jobs.db (created on first use); rows come back as sqlite3.Row."""
    import db       # imported here: db imports this module
    conn = db.connect(path or jobs_db_path())
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
                        ).fetchone() is None:
            conn.execute(JOBS_SCHEMA)
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def stage_label(name):
    """A stage as the log shows it: "Describing pictures" for "describe"."""
    if name in STAGE_LABELS:
        return STAGE_LABELS[name]
    return name[:1].upper() + name[1:]


class JobError(Exception):
    """A job cannot do what it was asked to; the message is shown to the user."""


class LogBuffer:
    """The last log lines of every job, each with a sequence number.

    The numbers keep increasing across jobs, so a page that polls with the
    last number it saw gets only the lines it has not shown yet.
    """

    def __init__(self, limit=LOG_LINES):
        self._lock = threading.Lock()
        self._lines = collections.deque(maxlen=limit)
        self._seq = 0

    def append(self, entry):
        """Store a copy of entry with the next sequence number; returns the copy."""
        with self._lock:
            self._seq += 1
            entry = dict(entry, seq=self._seq)
            self._lines.append(entry)
            return entry

    def since(self, seq=0):
        """(lines with a sequence number above seq, the latest number).

        A seq above the latest number (the page saw an earlier server
        process) gives every line still kept.
        """
        try:
            seq = int(seq or 0)
        except (TypeError, ValueError):
            seq = 0
        with self._lock:
            if seq > self._seq:
                seq = 0
            return [dict(entry) for entry in self._lines if entry["seq"] > seq], self._seq

    @property
    def latest(self):
        with self._lock:
            return self._seq


class UIContext(JobContext):
    """A JobContext for the jobs the web server runs (see JobManager).

    Besides the base counters it keeps the rate (items per minute over the
    last RATE_WINDOW seconds; quick steps, such as files a scan finds
    unchanged, are left out so they do not make the rest look fast) and an
    ETA for the current stage; log lines, in a LogBuffer shared by all
    jobs; the last ERROR_LINES warnings and errors, each with the item being
    worked on; and a log file (data/logs/job-<id>.log). Text is redacted
    (common.redact) before it is stored anywhere. saver(stage, done, total),
    when given, is called every save_interval seconds while items are
    counted (the manager writes the progress to jobs.db).
    """

    def __init__(self, job_id=None, name="", buffer=None, log_path=None,
                 log_limit=LOG_LINES, error_limit=ERROR_LINES, rate_window=RATE_WINDOW,
                 saver=None, save_interval=PROGRESS_SAVE_SECONDS):
        JobContext.__init__(self, name, log_limit)
        self.job_id = job_id
        self.buffer = buffer if buffer is not None else LogBuffer(log_limit)
        self.log_path = log_path
        self.rate_window = rate_window
        self.saver = saver
        self.save_interval = save_interval
        self._last_save = time.monotonic()
        self._errors = collections.deque(maxlen=error_limit)
        self._samples = collections.deque([(time.monotonic(), 0)])
        self._file = None
        self._file_failed = False
        self._file_lock = threading.Lock()

    def secrets(self):
        with self._lock:
            return list(self._secrets)

    def redact(self, text):
        """text with API keys (those added with add_secret, and the usual shapes) masked."""
        return common.redact(text, self.secrets())

    def wait_for_stop(self, timeout):
        """Sleep up to timeout seconds; returns True early once a stop is requested."""
        return self._stop.wait(timeout)

    def _emit(self, entry):
        with self._lock:
            stage, item = self.stage_name, self.item
        line = dict(entry, job=self.job_id, stage=stage)
        self.buffer.append(line)
        if entry["level"] in ("warn", "error"):
            with self._lock:
                self._errors.append(dict(line, item=common.redact(item, self._secrets)))
        self._write(entry)

    def _write(self, entry):
        if not self.log_path or self._file_failed:
            return
        prefix = {"warn": "Warning: ", "error": "Error: "}.get(entry["level"], "")
        text = "[%s] %s%s\n" % (datetime.fromtimestamp(entry["time"]).strftime(
            "%Y-%m-%d %H:%M:%S"), prefix, entry["message"])
        with self._file_lock:
            try:
                if self._file is None:
                    common.ensure_dir(os.path.dirname(os.path.abspath(self.log_path)))
                    self._file = open(self.log_path, "a", encoding="utf-8",
                                      errors="backslashreplace", newline="\n")
                self._file.write(text)
                self._file.flush()
            except OSError:
                # The job goes on without its log file.
                self._file_failed = True

    def close(self):
        """Close the log file (the context stays readable)."""
        with self._file_lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None

    def _stage_started(self):
        with self._lock:
            self._samples.clear()
            self._samples.append((time.monotonic(), 0))
            name, total = self.stage_name, self.total
        self.log(stage_label(name) + (" (%d to do)" % total if total else ""))
        self._save(force=True)

    def _progress(self):
        now = time.monotonic()
        with self._lock:
            counted = self.done - self.quick
            # About one sample per second is plenty for a two-minute rate.
            if len(self._samples) >= 2 and now - self._samples[-2][0] < 1.0:
                self._samples[-1] = (now, counted)
            else:
                self._samples.append((now, counted))
            cutoff = now - self.rate_window
            while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
                self._samples.popleft()
        self._save()

    def _save(self, force=False):
        if self.saver is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_save < self.save_interval:
                return
            self._last_save = now
            stage, done, total = self.stage_name, self.done, self.total
        try:
            self.saver(stage, done, total)
        except Exception:
            pass        # the progress in the table is only a convenience

    def rate(self):
        """Items per minute in the current stage over the last rate_window seconds, or None.

        Quick steps (step(quick=True)) are not counted.
        """
        now = time.monotonic()
        with self._lock:
            counted = self.done - self.quick
            start, start_done = self._samples[0]
            for when, done in self._samples:
                if when >= now - self.rate_window:
                    start, start_done = when, done
                    break
            elapsed = now - start
            if elapsed < 1.0 or counted < start_done:
                return None
            return (counted - start_done) * 60.0 / elapsed

    def progress(self):
        """{stage, done, total, item, rate (per minute), eta (seconds), counts, warnings,
        errors, paused, stop_requested}."""
        rate = self.rate()
        with self._lock:
            done, total = self.done, self.total
            data = {"stage": self.stage_name, "done": done, "total": total, "item": self.item,
                    "counts": dict(self.counts), "warnings": self.warnings,
                    "errors": self.errors, "paused": self.paused,
                    "stop_requested": self._stop.is_set()}
        data["rate"] = round(rate, 1) if rate is not None else None
        data["eta"] = (int(round((total - done) * 60.0 / rate))
                       if rate and total > done else None)
        return data

    def error_list(self):
        """The last warnings and errors: [{seq, time, level, message, job, stage, item}]."""
        with self._lock:
            return [dict(entry) for entry in self._errors]


def _json_dict(text):
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def canonical_params(params):
    """Job parameters as sorted JSON text (how identical jobs are recognised)."""
    return json.dumps(params or {}, sort_keys=True, ensure_ascii=True)


def job_record(row):
    """A jobs table row as a JSON-friendly dict (params and summary parsed)."""
    record = {key: row[key] for key in ("id", "kind", "library_id", "state", "stage", "done",
                                        "total", "error", "created", "started", "finished")}
    record["params"] = _json_dict(row["params"])
    record["summary"] = _json_dict(row["summary"])
    return record


def compact_summary(value, secrets=(), depth=0):
    """A small JSON-friendly copy of a job summary, for the jobs table.

    Numbers, booleans, None and (redacted) strings are kept; tuples become
    lists; lists are cut to SUMMARY_LIST_LIMIT items; deeper levels than
    three are dropped; non-finite numbers become None.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return common.redact(value, secrets)[:1000]
    if depth >= 3:
        return None
    if isinstance(value, dict):
        return {str(key): compact_summary(item, secrets, depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [compact_summary(item, secrets, depth + 1)
                for item in list(value)[:SUMMARY_LIST_LIMIT]]
    return common.redact(str(value), secrets)[:1000]


def lock_kind(info):
    """Who holds the WorkerLock, from its holder info: "server", "cli", "other" or ""."""
    if not info:
        return ""
    owner = str(info.get("owner") or "")
    if owner.startswith(WEB_JOB_LABEL):
        return "server"
    if owner.startswith("scan.py"):
        return "cli"
    return "other"


def _connect_index_db():
    import db       # imported here: db imports this module
    return db.connect()


def _load_helper(config):
    return common.load_helper(config.get("helper", ""))


class JobManager:
    """Runs queued jobs one at a time on a worker thread.

    Jobs are rows in the jobs table of data/jobs.db. enqueue() adds one (an
    identical job that is already queued or running is not added again);
    the worker runs them oldest first with the runner for their kind, a
    plain function run(ctx, conn, config, params, helper=None) whose conn is
    an index.db connection and whose params include "library_id" (None:
    every enabled library). Each job holds the WorkerLock while it runs;
    while another process (a scan.py run) holds it, the next job waits in
    state "queued" with stage WAITING_STAGE. pause(), resume() and cancel()
    act at the points where jobs call ctx.should_stop() and
    ctx.wait_if_paused() (between files and images; a request already sent
    to an LLM finishes first).

    start() marks jobs that an earlier server process left running as
    "interrupted" (resume() queues them again: every kind of job can safely
    run twice) and starts the worker. Only one JobManager per data folder
    runs at a time (data/jobs.lock); another one stays stopped and says why
    in its message, and shows the job the other one runs as "remote" (it
    can neither pause nor cancel it). Tests construct and start managers
    themselves.

    Two locks: _lock guards what is kept in memory (the current job) and is
    never held while waiting for a file or the database, so snapshot() is
    always quick; _queue_lock keeps changes to the jobs table in order, and
    is taken before _lock when both are needed.
    """

    def __init__(self, runners=None, poll_interval=2.0, idle_interval=5.0,
                 config_loader=None, helper_loader=None, connect=None):
        self.runners = dict(RUNNERS if runners is None else runners)
        self.poll_interval = poll_interval
        self.idle_interval = idle_interval
        self.config_loader = config_loader or common.load_config
        self.helper_loader = helper_loader or _load_helper
        self._connect = connect or connect_jobs_db
        self.buffer = LogBuffer(LOG_LINES)
        self.message = ""
        self._lock = threading.RLock()
        self._queue_lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._thread = None
        self._current = None
        self._last_ctx = None
        self._abort_created = False
        self._worker_lock = WorkerLock()
        self._manager_lock = WorkerLock(os.path.join(common.data_dir(), "jobs.lock"))

    # -- starting and stopping ------------------------------------------------

    @property
    def running(self):
        """True while the worker thread runs."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self):
        """Start the worker thread. Returns False when another process already runs jobs."""
        with self._queue_lock:
            if self.running:
                return True
            if not self._manager_lock.acquire("web server jobs"):
                self.message = ("Another server (%s) runs the jobs of this data folder; jobs "
                                "queued here are run by it."
                                % (self._manager_lock.holder() or "another process"))
                return False
            self.message = ""
            self._shutdown.clear()
            self._mark_interrupted()
            thread = threading.Thread(target=self._loop, name="job-manager", daemon=True)
            with self._lock:
                self._thread = thread
            thread.start()
            return True

    def stop(self, timeout=30.0):
        """Stop the worker; a running job stops at its next safe point, "interrupted"."""
        with self._lock:
            thread = self._thread
            self._shutdown.set()
            current = self._current
        if current is not None:
            current["ctx"].request_stop()
        self._wake.set()
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return False
        with self._lock:
            self._thread = None
        self._manager_lock.release()
        return True

    def _mark_interrupted(self):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET state = 'interrupted', finished = ?, "
                    "error = CASE WHEN error = '' THEN ? ELSE error END "
                    "WHERE state IN ('running', 'paused', 'cancelling')",
                    (time.time(), INTERRUPTED_MESSAGE))
        finally:
            conn.close()

    # -- the queue ------------------------------------------------------------

    def _job(self, conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return job_record(row) if row is not None else None

    def _twin(self, conn, kind, library_id, key, skip=None):
        """A queued, running or paused job with the same kind, library and params, or None."""
        for row in conn.execute("SELECT * FROM jobs WHERE state IN (%s) AND kind = ? "
                                "AND library_id IS ? ORDER BY id"
                                % ", ".join("?" * len(TWIN_STATES)),
                                TWIN_STATES + (kind, library_id)):
            if row["id"] != skip and canonical_params(_json_dict(row["params"])) == key:
                return job_record(row)
        return None

    def enqueue(self, kind, library_id=None, params=None):
        """Queue a job. Returns (job dict, added).

        added is False when an identical job (same kind, library and
        params) is already queued, running or paused; that job is returned
        instead (its "state" says which).
        """
        if kind not in self.runners:
            raise ValueError("Unknown kind of job: %s" % kind)
        key = canonical_params(params)
        with self._queue_lock:
            conn = self._connect()
            try:
                twin = self._twin(conn, kind, library_id, key)
                if twin is not None:
                    return twin, False
                with conn:
                    cursor = conn.execute(
                        "INSERT INTO jobs (kind, library_id, params, state, created) "
                        "VALUES (?, ?, ?, 'queued', ?)", (kind, library_id, key, time.time()))
                job = self._job(conn, cursor.lastrowid)
            finally:
                conn.close()
        self._wake.set()
        return job, True

    def get(self, job_id):
        """The job with this id (a dict), or None."""
        conn = self._connect()
        try:
            return self._job(conn, job_id)
        finally:
            conn.close()

    def owns(self, job_id):
        """True when this manager is running the job with this id (or waiting to)."""
        with self._lock:
            return self._current is not None and self._current["job"]["id"] == job_id

    def latest_by_library(self):
        """{library id or None: its latest job} (None: jobs for every library); deletions (of
        a library's data, or of the face data) left out, and so are the exports of trips (of
        no one library, see trip_export.py)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE id IN (SELECT MAX(id) FROM jobs WHERE kind NOT IN "
                "('delete_library', 'delete_faces') AND NOT (kind = 'export' AND "
                "library_id IS NULL) GROUP BY library_id)").fetchall()
        finally:
            conn.close()
        return {row["library_id"]: job_record(row) for row in rows}

    def _update(self, job_id, **values):
        columns = sorted(values)
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE jobs SET %s WHERE id = ?"
                             % ", ".join("%s = ?" % column for column in columns),
                             [values[column] for column in columns] + [job_id])
        finally:
            conn.close()

    def _set_active_state(self, job_id, state):
        """Record running / paused / cancelling, unless the job has finished meanwhile."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE jobs SET state = ? WHERE id = ? AND state NOT IN "
                             "('done', 'failed', 'cancelled', 'interrupted')", (state, job_id))
        finally:
            conn.close()

    def _mark_started(self, job_id, state, started):
        """Record that a job starts. False when it is no longer queued (cancelled elsewhere)."""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute("UPDATE jobs SET state = ?, started = ?, stage = '' "
                                      "WHERE id = ? AND state = 'queued'",
                                      (state, started, job_id))
            return cursor.rowcount > 0
        finally:
            conn.close()

    def _save_progress(self, job_id, stage, done, total):
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE jobs SET stage = ?, done = ?, total = ? WHERE id = ? "
                             "AND state IN ('queued', 'running', 'paused', 'cancelling')",
                             (stage, done, total, job_id))
        finally:
            conn.close()

    def pause(self, job_id):
        """Pause the running job with this id. Returns True if it was running."""
        with self._queue_lock:
            with self._lock:
                current = self._current
                if (current is None or current["job"]["id"] != job_id
                        or current["state"] != "running" or current["waiting"]):
                    return False
                current["state"] = "paused"
                ctx = current["ctx"]
            ctx.pause()
            self._set_active_state(job_id, "paused")
        ctx.log("Paused; the job waits at the next safe point.")
        return True

    def resume(self, job_id):
        """Resume a paused job, or queue an interrupted one again. Returns the job or None."""
        with self._queue_lock:
            with self._lock:
                current = self._current
                mine = current is not None and current["job"]["id"] == job_id
                if mine:
                    if current["state"] != "paused":
                        return None
                    current["state"] = "running"
                    ctx = current["ctx"]
                    job = dict(current["job"], state="running")
            if mine:
                # Recorded before the job carries on.
                self._set_active_state(job_id, "running")
                ctx.resume()
                ctx.log("Resumed.")
                return job
            conn = self._connect()
            try:
                job = self._job(conn, job_id)
                if job is None or job["state"] != "interrupted":
                    return None
                twin = self._twin(conn, job["kind"], job["library_id"],
                                  canonical_params(job["params"]), skip=job_id)
                with conn:
                    if twin is not None:
                        conn.execute("UPDATE jobs SET state = 'cancelled', error = ? "
                                     "WHERE id = ?", ("The same job is already queued or "
                                                      "running (job %d)." % twin["id"], job_id))
                    else:
                        conn.execute(
                            "UPDATE jobs SET state = 'queued', stage = '', done = 0, "
                            "total = 0, summary = '{}', error = '', created = ?, "
                            "started = NULL, finished = NULL WHERE id = ?",
                            (time.time(), job_id))
                job = twin or self._job(conn, job_id)
            finally:
                conn.close()
        self._wake.set()
        return job

    def cancel(self, job_id):
        """Cancel a job: a queued one at once, a running one at its next safe point.

        An interrupted job is marked cancelled (it is no longer offered for
        resuming). Returns True if the job could be cancelled.
        """
        with self._queue_lock:
            with self._lock:
                current = self._current
                mine = current is not None and current["job"]["id"] == job_id
                if mine:
                    if current["state"] == "cancelling":
                        return True
                    waiting = current["waiting"] or current["state"] == "queued"
                    current["state"] = "cancelling"
                    ctx = current["ctx"]
            if mine:
                ctx.request_stop()
                if not waiting:
                    self._set_active_state(job_id, "cancelling")
                ctx.log("Cancelling at the next safe point...")
                return True
            conn = self._connect()
            try:
                with conn:
                    cursor = conn.execute(
                        "UPDATE jobs SET state = 'cancelled', finished = ? "
                        "WHERE id = ? AND state IN ('queued', 'interrupted')",
                        (time.time(), job_id))
                return cursor.rowcount > 0
            finally:
                conn.close()

    def cancel_library_jobs(self, library_id):
        """Stop the work on a library that was removed from the settings.

        Its queued jobs are cancelled (returns how many). Its running job
        is cancelled too, at its next safe point; a running job of every
        library skips the rest of this one's pictures (JobContext.
        remove_library), so nothing more of it is described or paid for.
        """
        with self._queue_lock:
            with self._lock:
                current = self._current
                skip = current["job"]["id"] if current is not None else -1
                running = current["job"] if current is not None else None
                ctx = current["ctx"] if current is not None else None
            if running is not None and running["library_id"] == library_id:
                self.cancel(running["id"])
            elif running is not None and running["library_id"] is None and ctx is not None:
                ctx.remove_library(library_id)
                ctx.log("Library %s was removed from the settings; this job skips the rest "
                        "of its pictures." % library_id, "warn")
            conn = self._connect()
            try:
                with conn:
                    cursor = conn.execute(
                        "UPDATE jobs SET state = 'cancelled', finished = ?, error = ? "
                        "WHERE library_id = ? AND state = 'queued' AND id <> ?",
                        (time.time(), "The library was removed.", library_id, skip))
                return cursor.rowcount
            finally:
                conn.close()

    # -- the worker -----------------------------------------------------------

    def _system_log(self, message, level="error"):
        self.buffer.append({"time": time.time(), "level": level, "job": None, "stage": "",
                            "message": common.redact(message)})

    def _loop(self):
        while not self._shutdown.is_set():
            self._cleanup_abort()
            try:
                job = self._claim_next()
            except Exception as error:
                self._system_log("Could not read the job queue: %s" % error)
                job = None
            if job is None:
                self._wake.wait(self.idle_interval)
                self._wake.clear()
                continue
            try:
                self._run(job)
            except Exception as error:
                # The worker must survive anything a job does.
                self._system_log("Job %d stopped unexpectedly: %s" % (job["id"], error))
                with self._lock:
                    self._current = None

    def _claim_next(self):
        with self._queue_lock:
            if self._shutdown.is_set():
                return None
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM jobs WHERE state = 'queued' "
                                   "ORDER BY created, id LIMIT 1").fetchone()
            finally:
                conn.close()
            if row is None:
                return None
            job = job_record(row)
            job_id = job["id"]
            log_path = os.path.join(common.logs_dir(), "job-%d.log" % job_id)

            def saver(stage, done, total):
                self._save_progress(job_id, stage, done, total)
            ctx = UIContext(job_id, job["kind"], self.buffer, log_path, saver=saver)
            with self._lock:
                self._current = {"job": job, "ctx": ctx, "state": "queued", "waiting": False}
            return job

    def _wait_for_lock(self, current, label):
        """Take the WorkerLock, waiting while another process holds it. False if stopped first."""
        ctx = current["ctx"]
        announced = False
        try:
            while True:
                if ctx.should_stop() or self._shutdown.is_set():
                    return False
                if self._worker_lock.acquire(label):
                    return True
                if not announced:
                    announced = True
                    with self._lock:
                        current["waiting"] = True
                    ctx.stage(WAITING_STAGE)
                    ctx.log("Another job is running: %s. This one starts when it has finished."
                            % (self._worker_lock.holder() or "another process"), "warn")
                    self._update(current["job"]["id"], stage=WAITING_STAGE)
                self._cleanup_abort()
                ctx.wait_for_stop(self.poll_interval)
        finally:
            with self._lock:
                current["waiting"] = False

    def _run(self, job):
        with self._lock:
            current = self._current
        ctx = current["ctx"]
        label = "%s %d (%s)" % (WEB_JOB_LABEL, job["id"], job["kind"])
        state, error, summary = None, "", {}
        acquired = False
        try:
            acquired = self._wait_for_lock(current, label)
            if not acquired:
                if current["state"] == "cancelling":
                    state = "cancelled"
                else:
                    # The server is stopping: the job stays queued for next time.
                    self._release_current(ctx)
                return
            with self._queue_lock:
                with self._lock:
                    if current["state"] == "queued":
                        current["state"] = "running"
                    current["job"]["started"] = time.time()
                    new_state, started = current["state"], current["job"]["started"]
                if not self._mark_started(job["id"], new_state, started):
                    # Cancelled meanwhile, on the page of another server.
                    ctx.request_stop()
                    state = "cancelled"
                    return
            ctx.log("Started job %d: %s" % (job["id"], job["kind"]))
            config = self.config_loader()
            for endpoint in common.endpoint_list(config):
                ctx.add_secret(common.resolve_api_key(endpoint)[0])
            helper = self.helper_loader(config)
            params = dict(job["params"], library_id=job["library_id"])
            conn = _connect_index_db()
            try:
                summary = self.runners[job["kind"]](ctx, conn, config, params, helper=helper)
            finally:
                conn.close()
            summary = summary if isinstance(summary, dict) else {}
            error = summary.get("error") or ""
            if ctx.should_stop():
                state = self._stopped_state(ctx)
                if state == "interrupted" and ctx.interrupted:
                    error = ctx.interrupted
            else:
                state = "failed" if error else "done"
        except JobStopped:
            state = self._stopped_state(ctx)
            if state == "interrupted":
                error = ctx.interrupted
        except JobError as exc:
            state, error = "failed", str(exc)
        except BaseException as exc:
            # Also SystemExit, which loading a missing helper module raises.
            state, error = "failed", "%s: %s" % (type(exc).__name__, exc)
        finally:
            if acquired:
                self._worker_lock.release()
            if state is not None:
                self._finish(job, ctx, state, error, summary)

    def _stopped_state(self, ctx):
        """How a job that stopped early ends: "interrupted" (to be resumed) or "cancelled".

        Interrupted when the server is stopping, or when the job was
        interrupted (ctx.interrupt) rather than cancelled by someone.
        """
        if self._shutdown.is_set():
            return "interrupted"
        with self._lock:
            current = self._current
            cancelling = current is not None and current["state"] == "cancelling"
        return "interrupted" if ctx.interrupted and not cancelling else "cancelled"

    def _finish(self, job, ctx, state, error, summary):
        error = ctx.redact(error or "")[:1000]
        if state == "failed":
            ctx.log("Job failed: %s" % error, "error")
        elif state == "cancelled":
            ctx.log("Job cancelled.")
        elif state == "interrupted":
            ctx.log("Job interrupted; resume it to continue.", "warn")
        else:
            ctx.log("Job finished.")
        if state == "interrupted" and not error:
            error = INTERRUPTED_MESSAGE
        progress = ctx.progress()
        record = compact_summary(summary, ctx.secrets())
        try:
            self._update(job["id"], state=state, stage=progress["stage"], done=progress["done"],
                         total=progress["total"], error=error, finished=time.time(),
                         summary=json.dumps(record, allow_nan=False))
        finally:
            self._release_current(ctx)

    def _release_current(self, ctx):
        with self._lock:
            self._current = None
            self._last_ctx = ctx
        ctx.close()

    # -- the other process holding the lock -----------------------------------

    def lock_status(self):
        """{"busy", "holder" (text), "other" (held by another process or object), "kind"}.

        kind says who holds the lock: "server" (a job of a web server),
        "cli" (scan.py), "other", or "" when it is free.
        """
        if self._worker_lock.held:
            return {"busy": True, "holder": self._worker_lock.holder(), "other": False,
                    "kind": "server"}
        info = self._worker_lock.holder_info()
        if info is None:
            return {"busy": False, "holder": "", "other": False, "kind": ""}
        return {"busy": True, "holder": WorkerLock.describe(info), "other": True,
                "kind": lock_kind(info)}

    def abort_other(self):
        """Ask a scan.py run that holds the WorkerLock to stop, by creating data/abort.

        The file is removed again once that process has let go of the lock
        (scan.py refuses to start while it exists). A job of another web
        server is not asked (that server does not look for the file; its
        own Manage page can pause or cancel it). Returns {"requested",
        "message", "holder"}.
        """
        status = self.lock_status()
        if not status["other"]:
            message = ("The running job belongs to this server; cancel it instead."
                       if status["busy"] else "No other scan is running.")
            return {"requested": False, "message": message, "holder": status["holder"]}
        if status["kind"] == "server":
            return {"requested": False, "holder": status["holder"],
                    "message": "The running job belongs to another web server that uses this "
                               "data folder (%s); pause or cancel it on that server's Manage "
                               "page." % status["holder"]}
        path = common.abort_path()
        common.ensure_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("Stop requested from the web page at %s\n"
                         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with self._lock:
            self._abort_created = True
        self._system_log("Asked %s to stop." % status["holder"], "warn")
        return {"requested": True, "holder": status["holder"],
                "message": "Asked %s to stop; it stops after the file it is working on."
                           % status["holder"]}

    def _cleanup_abort(self):
        with self._lock:
            if not self._abort_created:
                return
        if self.lock_status()["other"]:
            return
        try:
            os.remove(common.abort_path())
        except FileNotFoundError:
            pass
        except OSError:
            return
        with self._lock:
            self._abort_created = False

    # -- what the browser shows -----------------------------------------------

    def _remote_job(self, conn):
        """The job another server runs on this data folder, as a read-only current job."""
        if self.running:
            return None
        row = conn.execute("SELECT * FROM jobs WHERE state IN ('running', 'paused', "
                           "'cancelling') ORDER BY started DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        info = self._manager_lock.holder_info()
        if info is None:
            return None         # left over from a server that has stopped
        view = job_record(row)
        view.update(remote=True, holder=WorkerLock.describe(info), waiting=False,
                    paused=view["state"] == "paused", item="", rate=None, eta=None,
                    warnings=0, errors=0)
        return view

    def snapshot(self, since_seq=0, names=None):
        """The state of the job system for the browser.

        {"current": the running (or waiting) job with its progress, or None,
        "queue": queued jobs in order, "recent": the last RECENT_JOBS
        finished jobs, "log": log lines after since_seq, "seq": the latest
        log line number, "errors": warnings and errors of the current (or
        last) job, "lock": lock_status(), "manager": {"running", "message"}}.
        A job that another server runs is shown as current with "remote":
        True (and only the progress it saves now and then). names ({library
        id: name}) fills in each job's "library".
        """
        names = names or {}
        self._cleanup_abort()
        with self._lock:
            current = self._current
            if current is not None:
                view = dict(current["job"], state=current["state"],
                            waiting=current["waiting"])
                ctx = current["ctx"]
            else:
                view, ctx = None, self._last_ctx
        conn = self._connect()
        try:
            queue = [job_record(row) for row in conn.execute(
                "SELECT * FROM jobs WHERE state = 'queued' ORDER BY created, id")]
            recent = [job_record(row) for row in conn.execute(
                "SELECT * FROM jobs WHERE state IN ('done', 'failed', 'cancelled', "
                "'interrupted') ORDER BY COALESCE(finished, created) DESC, id DESC LIMIT ?",
                (RECENT_JOBS,))]
            if view is None:
                view = self._remote_job(conn)
        finally:
            conn.close()
        if view is not None:
            queue = [job for job in queue if job["id"] != view["id"]]
            if current is not None:
                state = view["state"]
                view.update(current["ctx"].progress())
                view["state"] = state
        for job in [view] + queue + recent:
            if job is not None:
                library_id = job["library_id"]
                job["library"] = names.get(library_id, library_id) if library_id else ""
                if job.get("started") and job.get("finished"):
                    job["duration"] = max(0.0, job["finished"] - job["started"])
        lines, seq = self.buffer.since(since_seq)
        return {"current": view, "queue": queue, "recent": recent, "log": lines, "seq": seq,
                "errors": ctx.error_list() if ctx is not None else [],
                "lock": self.lock_status(),
                "manager": {"running": self.running, "message": self.message}}

    def wait_idle(self, timeout=30.0):
        """Wait until no job runs or is queued. Returns True if that happened in time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Under the queue lock: a job is either still queued or already current.
            with self._queue_lock:
                with self._lock:
                    idle = self._current is None
                if idle:
                    conn = self._connect()
                    try:
                        queued = conn.execute(
                            "SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0]
                    finally:
                        conn.close()
                    if not queued:
                        return True
            time.sleep(0.02)
        return False


# ---------------------------------------------------------------------------
# Job runners: run(ctx, conn, config, params, helper=None) -> summary dict.
# A summary with an "error" marks the job as failed.

def job_libraries(config, params):
    """The libraries a job works on: its library, or every enabled library.

    Raises JobError when the job's library is gone from the settings or
    disabled.
    """
    library_id = params.get("library_id")
    if not library_id:
        return [library for library in common.enabled_libraries(config) if library.get("id")]
    for library in config.get("libraries", []):
        if library.get("id") == library_id:
            if not library.get("enabled", True):
                raise JobError("Library %s is disabled; enable it first."
                               % common.library_label(library))
            return [library]
    raise JobError("Library %s is no longer in the settings." % library_id)


# Scan summary fields worth keeping with a finished job.
_SCAN_FIELDS = ("library_id", "name", "files", "added", "updated", "changed", "moved",
                "copied", "unchanged", "restored", "failed", "unsupported", "missing",
                "excluded", "guarded", "removed_moved", "purged", "offline", "aborted",
                "complete", "videos_skipped")


def _scan_summary(result, params, offline_fails):
    libraries = []
    for item in result.get("libraries", []):
        entry = {key: item[key] for key in _SCAN_FIELDS if key in item}
        entry["errors"] = len(item.get("errors") or ())
        if isinstance(item.get("backfill"), dict):
            entry["previews"] = {key: item["backfill"].get(key)
                                 for key in ("needed", "filled", "failed", "panoramas",
                                             "panos")}
        libraries.append(entry)
    summary = {"libraries": libraries, "stopped": bool(result.get("stopped"))}
    offline = [item.get("name", "") for item in libraries if item.get("offline")]
    if offline:
        summary["offline"] = offline
        if offline_fails and params.get("library_id") and len(libraries) == 1:
            summary["error"] = ("The folder of %s is not available (offline?); nothing was "
                                "changed." % offline[0])
    described = result.get("describe")
    if isinstance(described, dict):
        summary["describe"] = described
        if described.get("error"):
            summary["error"] = described["error"]
    if isinstance(result.get("gps_check"), dict):
        summary["gps_check"] = result["gps_check"]
    if isinstance(result.get("embed"), dict):
        summary["embed"] = result["embed"]
    if isinstance(result.get("faces"), dict):
        summary["faces"] = result["faces"]
    if isinstance(result.get("looks_pending"), int):
        summary["looks_pending"] = result["looks_pending"]
    if isinstance(result.get("views_pending"), int):
        summary["views_pending"] = result["views_pending"]
    return summary


def _describe_options(params):
    limit = params.get("limit")
    return {"retry_failed": bool(params.get("retry_failed")),
            "redescribe_stale": bool(params.get("redescribe_stale")),
            "limit": limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
            "endpoint_name": params.get("endpoint") or None}


def run_scan(ctx, conn, config, params, helper=None):
    """Scan a library (or every enabled one) and fill in previews; no describing."""
    import scan
    libraries = job_libraries(config, params)
    result = scan.run(config, ctx, library_ids=[library["id"] for library in libraries],
                      describe_images=False,
                      allow_mass_removal=bool(params.get("allow_mass_removal")),
                      helper=helper, conn=conn)
    return _scan_summary(result, params, offline_fails=True)


def run_describe(ctx, conn, config, params, helper=None):
    """Describe the images of a library (or every enabled one) that need it.

    params "ids" (image ids) limits the job to those pictures, and "force"
    then describes them again even when they have a description.
    """
    import describe
    libraries = job_libraries(config, params)
    ids = params.get("ids")
    ids = [image_id for image_id in ids if isinstance(image_id, int)] if isinstance(
        ids, list) else None
    library_ids = [library["id"] for library in libraries]
    summary = describe.describe_pending(conn, config, ctx, helper=helper,
                                        library_ids=library_ids,
                                        ids=ids, force=bool(params.get("force")),
                                        **_describe_options(params))
    return _with_embed(summary, conn, config, ctx, library_ids)


def _with_embed(summary, conn, config, ctx, library_ids):
    """A describe or refine summary with the embed step that follows it (search by
    meaning, when it is set up and the run was not stopped) as its "embed"."""
    import embeddings
    if not summary.get("stopped"):
        embedded = embeddings.after_run(conn, config, ctx, library_ids=library_ids)
        if embedded is not None:
            summary["embed"] = embedded
    return summary


def run_refine(ctx, conn, config, params, helper=None):
    """Describe the documents and screenshots of a library (or every enabled one) again
    with its refine endpoint (describe.refine_pending)."""
    import describe
    libraries = job_libraries(config, params)
    limit = params.get("limit")
    library_ids = [library["id"] for library in libraries]
    summary = describe.refine_pending(
        conn, config, ctx, helper=helper, library_ids=library_ids,
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        retry_failed=bool(params.get("retry_failed")),
        only_library=bool(params.get("library_id")))
    return _with_embed(summary, conn, config, ctx, library_ids)


def run_embed(ctx, conn, config, params, helper=None):
    """Update search by meaning for a library (or every enabled one): embed the pictures
    whose vector is missing or stale (embeddings.update). params "limit": at most
    that many. Raises JobError when search by meaning is not set up."""
    import embeddings
    libraries = job_libraries(config, params)
    if not embeddings.configured(config):
        raise JobError(embeddings.NOT_CONFIGURED)
    limit = params.get("limit")
    summary = embeddings.update(
        conn, config, ctx, library_ids=[library["id"] for library in libraries],
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None)
    if not summary["error"] and not summary["pending"] and not summary["stopped"]:
        ctx.log("Search by meaning is up to date (%d picture(s))." % summary["up_to_date"])
    return summary


def run_image_embed(ctx, conn, config, params, helper=None):
    """Make the looks (image vectors) of a library's pictures (or every enabled
    library's) that have none yet (looks.update). params "limit": at most that many;
    "rebuild": first throw away the kept ones made with other settings (or, after a
    failed probe check, all of them and the reference); run again, it goes on where
    it was. Raises JobError when looks are not set up."""
    import looks
    libraries = job_libraries(config, params)
    if not looks.configured(config):
        raise JobError(looks.NOT_CONFIGURED)
    limit = params.get("limit")
    summary = looks.update(
        conn, config, ctx, library_ids=[library["id"] for library in libraries],
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        rebuild=bool(params.get("rebuild")), helper=helper)
    if not summary["error"] and not summary["pending"] and not summary["stopped"]:
        ctx.log("Looks are up to date (%d picture(s))." % summary["up_to_date"])
    return summary


def run_process(ctx, conn, config, params, helper=None):
    """Scan, then describe: one library, or every enabled one ("Process all")."""
    import scan
    libraries = job_libraries(config, params)
    options = _describe_options(params)
    result = scan.run(config, ctx, library_ids=[library["id"] for library in libraries],
                      allow_mass_removal=bool(params.get("allow_mass_removal")),
                      helper=helper, conn=conn, **options)
    return _scan_summary(result, params, offline_fails=False)


def run_previews(ctx, conn, config, params, helper=None):
    """Build missing previews (and other details older rows lack); no scan, no describing.

    The GPS check (gps_check.run) follows, as after a scan (also when
    stopped): a picture read again may have a position it had not. Then
    the local times (local_times.run_safely).
    """
    import cache
    import scan
    import videos
    libraries = job_libraries(config, params)
    threads = scan.reader_threads(config)
    video = videos.setup(config)
    walker = getattr(helper, "walk_library", None) if helper is not None else None
    results = []
    with cache.ThumbStore() as thumbs:
        previews = cache.PreviewStore()
        for library in libraries:
            if ctx.should_stop():
                break
            label = common.library_label(library)
            root = common.library_root(library)
            if walker is None and not (root and os.path.isdir(root)):
                ctx.log("Skipping library %s: the folder %s is not available (offline?)."
                        % (label, root or "(none)"), "warn")
                results.append({"library_id": library["id"], "name": label, "offline": True})
                continue
            result = scan.backfill(conn, library, ctx, helper, thumbs, previews, threads,
                                   video)
            result.update(library_id=library["id"], name=label)
            results.append(result)
            if result.get("aborted"):
                break
    summary = {"libraries": results, "stopped": ctx.should_stop()}
    # (Pictures read again may have positions back: check them again, also
    # when stopped.)
    import gps_check
    checked = gps_check.run_safely(conn, config, ctx)
    if checked is not None:
        summary["gps_check"] = checked
    # (Tracks read, and dates read again, may change local times.)
    import local_times
    localised = local_times.run_safely(conn, config, ctx)
    if localised is not None:
        summary["local_times"] = localised
    if params.get("library_id") and len(results) == 1 and results[0].get("offline"):
        summary["error"] = "The folder of %s is not available (offline?)." % results[0]["name"]
    return summary


def run_delete_library(ctx, conn, config, params, helper=None):
    """Delete the indexed data of a library that is no longer in the settings."""
    import library_ops
    return library_ops.delete_library_job(ctx, conn, config, params)


def run_export(ctx, conn, config, params, helper=None):
    """Export one library as a static website (export.export_library).

    params: "out_dir" (default: data/exports/<library name>/) and
    "options" (they override the library's saved export options for this
    export only). Raises JobError when the export cannot be made (an
    unknown or disabled library, a folder it must not write to, bad
    options); one picture that cannot be read is only counted. With
    "trip" (a trip's id; no library) the trip is exported instead
    (trip_export.export_trip).
    """
    import export
    if params.get("trip"):
        import trip_export
        options = params.get("options")
        try:
            return trip_export.export_trip(ctx, conn, config, params["trip"],
                                           params.get("out_dir") or None,
                                           options if isinstance(options, dict) else None,
                                           helper=helper)
        except export.ExportError as error:
            raise JobError(str(error))
        except OSError as error:
            raise JobError(export.os_error_text(error))
    if not params.get("library_id"):
        raise JobError("An export is made of one library; choose which.")
    library = job_libraries(config, params)[0]
    options = params.get("options")
    try:
        return export.export_library(ctx, conn, config, library["id"],
                                     params.get("out_dir") or None,
                                     options if isinstance(options, dict) else None,
                                     helper=helper)
    except export.ExportError as error:
        raise JobError(str(error))
    except OSError as error:
        # (export.py words its own; this is for anything it did not expect.)
        raise JobError(export.os_error_text(error))


def run_places(ctx, conn, config, params, helper=None):
    """Add place names to the pictures with a GPS position (places.py).

    params "download": first download the GeoNames files again and build
    the place index from them (the only network use), then look up every
    picture again; "redo": look up every picture again. Without either,
    only pictures not looked up yet. A place index made by another version
    of this program is built again from the downloaded files first (then
    every picture is looked up again). The pictures of every library are
    looked up, enabled or not: it only touches the database, and the
    Manage page counts them all (places.status). Raises JobError when
    there are no place names (or the download fails). The GPS check
    (gps_check.run) follows, and the local times (local_times.py: the
    place index holds the time zones).
    """
    import places
    summary = {}
    try:
        if params.get("download"):
            summary["index"] = places.update(ctx)
            if ctx.should_stop():
                return summary
        else:
            rebuilt = places.rebuild_if_needed(ctx)
            if rebuilt is not None:
                summary["index"] = rebuilt
        if places.shared() is None:
            raise JobError("There are no place names yet: download them first.")
        summary["places"] = places.label_pictures(
            conn, ctx, None, redo=bool(params.get("download") or params.get("redo")
                                       or summary.get("index")))
        import gps_check
        checked = gps_check.run_safely(conn, config, ctx)
        if checked is not None:
            summary["gps_check"] = checked
        import local_times
        localised = local_times.run_safely(conn, config, ctx)
        if localised is not None:
            summary["local_times"] = localised
    except places.PlacesError as error:
        if ctx.should_stop():
            return summary          # (cancelled: no error)
        raise JobError(str(error))
    finally:
        places.release()
    return summary


def run_proxies(ctx, conn, config, params, helper=None):
    """Make playable (H.264) copies of the videos browsers may not play (videos.make_proxies).

    Only when "video_proxies" is enabled in config.json; raises JobError
    otherwise, and when ffmpeg is missing.
    """
    import videos
    if not videos.proxy_settings(config)["enabled"]:
        raise JobError("Playable copies of videos are turned off: set \"video_proxies\": "
                       "{\"enabled\": true} in config.json first.")
    libraries = job_libraries(config, params)
    try:
        return videos.make_proxies(conn, config, ctx, [library["id"] for library in libraries])
    except videos.ToolsMissing as error:
        raise JobError(str(error))


def run_drive_previews(ctx, conn, config, params, helper=None):
    """Make sped-up previews of the drives (drives.make_previews).

    Only when "drive_previews" is enabled in config.json; raises JobError
    otherwise, and when ffmpeg is missing.
    """
    import drives
    import videos
    if not drives.preview_settings(config)["enabled"]:
        raise JobError("Sped-up previews of drives are turned off: set \"drive_previews\": "
                       "{\"enabled\": true} in config.json first.")
    libraries = job_libraries(config, params)
    try:
        return drives.make_previews(conn, config, ctx, [library["id"] for library in libraries])
    except videos.ToolsMissing as error:
        raise JobError(str(error))


def run_faces(ctx, conn, config, params, helper=None):
    """Find the faces in a library's pictures (or every enabled library's) that were not
    looked at yet (people.update). params "limit": at most that many; "retry_failed":
    also the pictures that failed 3 times. Raises JobError when People is off or its
    setup is not complete."""
    import people
    libraries = job_libraries(config, params)
    options = people.settings(config)
    problem = people.setup_problem(options)
    if problem:
        raise JobError(problem)
    limit = params.get("limit")
    summary = people.update(
        conn, config, ctx, library_ids=[library["id"] for library in libraries],
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        retry_failed=bool(params.get("retry_failed")), helper=helper)
    if not summary["error"] and not summary["pending"] and not summary["stopped"]:
        ctx.log("Faces are up to date (%d picture(s))." % summary["up_to_date"])
    return summary


def run_group_faces(ctx, conn, config, params, helper=None):
    """Group the faces found into people again (people_groups.group), for every library:
    after "match", "group_quality" or "min_faces" changed, or to see what a grouping
    makes of the faces now. What the owner said about the people is kept."""
    import sqlite3
    import people
    import people_groups
    options = people.settings(config)
    if not options["enabled"]:
        raise JobError(people.NOT_ENABLED)
    if not os.path.isfile(people.store_path()):
        raise JobError("There are no faces to group yet: find faces first.")
    try:
        store = people.connect()        # (an older file is brought up to date)
    except sqlite3.Error as error:
        raise JobError("The face data cannot be read: %s" % error)
    try:
        return people_groups.group(store, options, ctx, conn=conn)
    finally:
        store.close()


def run_delete_faces(ctx, conn, config, params, helper=None):
    """Delete all face data (people.delete_all): data/faces.db goes as a whole, with the
    people and their names. Raises JobError when some of it could not be deleted."""
    import people
    ctx.stage("delete faces")
    result = people.delete_all()
    if result["error"]:
        raise JobError("The face data was NOT deleted (not all of it): %s. Close what has "
                       "data/faces.db open and delete it again." % result["error"])
    ctx.log("Deleted the face data: %d face(s) of %d picture(s) and %d people (%d named)%s."
            % (result["faces"], result["pictures"], result["people"], result["named"],
               "" if result["removed"] else
               " (data/faces.db was open elsewhere: it was emptied and wiped)"))
    return result


RUNNERS = {
    "scan": run_scan,
    "describe": run_describe,
    "refine": run_refine,
    "embed": run_embed,
    "image_embed": run_image_embed,
    "process": run_process,
    "previews": run_previews,
    "delete_library": run_delete_library,
    "export": run_export,
    "places": run_places,
    "proxies": run_proxies,
    "drive_previews": run_drive_previews,
    "faces": run_faces,
    "group_faces": run_group_faces,
    "delete_faces": run_delete_faces,
}
