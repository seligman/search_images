#!/usr/bin/env python3
"""Job plumbing shared by the command line tools and the web server.

Every long-running function (scan, describe, export, ...) takes a context
object: it reports progress and log lines through it and asks it whether to
stop. JobContext is the base used by the server; ConsoleContext prints to the
terminal and also honors abort files. WorkerLock makes sure only one process
runs jobs at a time.

This module uses only the standard library.
"""

import collections
import json
import os
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
        self.total = 0
        self.item = ""
        self.counts = {}
        self.warnings = 0
        self.errors = 0

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
            self.total = max(0, int(total or 0))
            self.item = ""
        self._stage_started()

    def set_total(self, total):
        with self._lock:
            self.total = max(0, int(total or 0))

    def step(self, n=1, item=None):
        """Count n finished items in the current stage."""
        with self._lock:
            self.done += n
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
