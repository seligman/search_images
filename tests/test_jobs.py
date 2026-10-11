"""Tests for jobs.py: job contexts and the cross-process worker lock."""

import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import support

import common
import jobs

# Run in a child process: takes the lock, reports, then waits for a line on
# stdin and exits WITHOUT releasing (like a crash), so the operating system
# has to release the lock.
CHILD = """
import os, sys
sys.path.insert(0, sys.argv[1])
import jobs
lock = jobs.WorkerLock(sys.argv[2])
print("acquired" if lock.acquire("child job") else "busy:" + lock.holder(), flush=True)
sys.stdin.readline()
os._exit(0)
"""


class JobContextTest(unittest.TestCase):

    def test_stages_steps_and_snapshot(self):
        ctx = jobs.JobContext("scan")
        ctx.stage("scan", total=3)
        ctx.step(item="a.jpg")
        ctx.step(2, item="b.jpg")
        ctx.count("added")
        ctx.count("added", 2)
        ctx.log("hello")
        ctx.log("careful", level="warn")
        ctx.log("broken", level="error")
        ctx.log("odd level", level="debug")
        snap = ctx.snapshot()
        self.assertEqual((snap["name"], snap["stage"], snap["done"], snap["total"], snap["item"]),
                         ("scan", "scan", 3, 3, "b.jpg"))
        self.assertEqual(snap["counts"], {"added": 3})
        self.assertEqual((snap["warnings"], snap["errors"]), (1, 1))
        self.assertEqual([entry["level"] for entry in snap["log_tail"]],
                         ["info", "warn", "error", "info"])
        self.assertFalse(snap["paused"])
        self.assertFalse(snap["stop_requested"])
        ctx.stage("previews")
        ctx.set_total(10)
        snap = ctx.snapshot(log_tail=2)
        self.assertEqual((snap["stage"], snap["done"], snap["total"]), ("previews", 0, 10))
        self.assertEqual(len(snap["log_tail"]), 2)

    def test_log_is_bounded_and_redacted(self):
        ctx = jobs.JobContext(log_limit=5)
        # Not real secrets: made-up values used only by this test.
        ctx.add_secret("super-secret-value")
        for n in range(10):
            ctx.log("line %d" % n)
        ctx.log("failed with super-secret-value and Bearer abcdef123456")
        tail = ctx.snapshot()["log_tail"]
        self.assertEqual(len(tail), 5)
        self.assertNotIn("super-secret-value", tail[-1]["message"])
        self.assertNotIn("abcdef123456", tail[-1]["message"])

    def test_stop_and_check(self):
        ctx = jobs.JobContext()
        ctx.check()
        self.assertFalse(ctx.should_stop())
        ctx.request_stop()
        self.assertTrue(ctx.should_stop())
        with self.assertRaises(jobs.JobStopped):
            ctx.check()

    def test_pause_blocks_until_resumed_or_stopped(self):
        ctx = jobs.JobContext()
        ctx.pause()
        self.assertTrue(ctx.paused)
        finished = threading.Event()

        def worker():
            ctx.wait_if_paused(poll=0.01)
            finished.set()
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertFalse(finished.wait(0.2))
        ctx.resume()
        self.assertTrue(finished.wait(5))
        thread.join()
        ctx.pause()
        finished.clear()
        thread = threading.Thread(target=worker)
        thread.start()
        ctx.request_stop()
        self.assertTrue(finished.wait(5))
        thread.join()
        self.assertFalse(ctx.paused)


class ConsoleContextTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        # Abort files are also looked for in the current directory.
        self.cwd = os.getcwd()
        self.work = tempfile.mkdtemp(prefix="search-images-cwd-")
        os.chdir(self.work)
        self.addCleanup(support.remove_tree, self.work)
        self.addCleanup(os.chdir, self.cwd)

    def test_output_format_and_throttling(self):
        out = io.StringIO()
        ctx = jobs.ConsoleContext("scan", stream=out, progress_interval=60)
        ctx.log("Scanning Family")
        ctx.log("slow share", level="warn")
        ctx.stage("scan", total=500)
        for n in range(500):
            ctx.step(item="f%d.jpg" % n)
        lines = out.getvalue().splitlines()
        for line in lines:
            self.assertRegex(line, r"^\[[0-9]{2}:[0-9]{2}:[0-9]{2}\] ")
        self.assertEqual(lines[0][11:], "Scanning Family")
        self.assertEqual(lines[1][11:], "Warning: slow share")
        self.assertEqual(lines[2][11:], "scan (500 items)")
        # Progress is throttled: only the final line of the stage is printed.
        self.assertEqual(lines[3:], [lines[3]])
        self.assertIn("scan: 500/500 (100%) f499.jpg", lines[3])

    def test_unprintable_names_do_not_crash(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        ctx = jobs.ConsoleContext(stream=out)
        message = "Added \u65e5\u672c/\u00e9t\u00e9.jpg"
        ctx.log(message)
        out.flush()
        expected = message.encode("cp1252", "backslashreplace")
        self.assertIn(expected, raw.getvalue())

    def test_abort_files(self):
        for location in (common.abort_path(), os.path.join(self.work, "abort"),
                         os.path.join(self.work, "abort.txt")):
            ctx = jobs.ConsoleContext(stream=io.StringIO(), check_interval=0)
            self.assertFalse(ctx.should_stop())
            common.ensure_dir(os.path.dirname(location))
            open(location, "w").close()
            try:
                self.assertTrue(ctx.should_stop())
                self.assertEqual(os.path.abspath(ctx.abort_found), os.path.abspath(location))
                with self.assertRaises(jobs.JobStopped):
                    ctx.check()
            finally:
                os.remove(location)


class WorkerLockTest(support.TempBaseMixin, unittest.TestCase):

    def test_same_process(self):
        first = jobs.WorkerLock()
        second = jobs.WorkerLock()
        self.assertEqual(first.path, common.lock_path())
        self.assertEqual(second.holder(), "")
        self.assertTrue(first.acquire("scan Family"))
        self.addCleanup(first.release)
        self.assertTrue(first.held)
        self.assertTrue(first.acquire("scan Family"))      # already held: still True
        self.assertFalse(second.acquire("server job"))
        holder = second.holder()
        self.assertIn("scan Family", holder)
        self.assertIn("pid %d" % os.getpid(), holder)
        self.assertRegex(holder, r"since [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
        self.assertIn("scan Family", first.holder())
        first.release()
        self.assertFalse(first.held)
        self.assertEqual(second.holder(), "")
        self.assertTrue(second.acquire("server job"))
        second.release()
        first.release()             # releasing twice is harmless

    def test_leftover_text_without_a_holder_is_ignored(self):
        common.ensure_dir(common.data_dir())
        with open(common.lock_path(), "w") as handle:
            handle.write('{"owner": "crashed job", "pid": 1, "started": 0}')
        lock = jobs.WorkerLock()
        self.assertEqual(lock.holder(), "")
        self.assertTrue(lock.acquire("new job"))
        lock.release()

    def test_wait_for_lock(self):
        first = jobs.WorkerLock()
        self.assertTrue(first.acquire("short job"))
        timer = threading.Timer(0.3, first.release)
        timer.start()
        second = jobs.WorkerLock()
        start = time.monotonic()
        self.assertTrue(second.acquire("waiting job", timeout=10))
        self.assertLess(time.monotonic() - start, 9)
        timer.join()
        second.release()

    def start_child(self, path):
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, support.REPO, path], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=support.child_env(),
            text=True)
        self.addCleanup(self.stop_child, child)
        # A child that never answers must not hang the test run.
        watchdog = threading.Timer(60, child.terminate)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        return child

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            try:
                child.stdin.close()
            except OSError:
                pass
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(10)
        for stream in (child.stdout, child.stderr, child.stdin):
            if stream and not stream.closed:
                stream.close()

    def test_lock_across_processes(self):
        path = common.lock_path()
        child = self.start_child(path)
        self.assertEqual(child.stdout.readline().strip(), "acquired")
        lock = jobs.WorkerLock(path)
        self.assertFalse(lock.acquire("parent job"))
        holder = lock.holder()
        self.assertIn("child job", holder)
        self.assertIn("pid %d" % child.pid, holder)
        # The child exits without releasing; the OS frees the lock.
        child.stdin.write("exit\n")
        child.stdin.flush()
        self.assertEqual(child.wait(30), 0)
        self.assertTrue(lock.acquire("parent job", timeout=10))
        try:
            other = self.start_child(path)
            line = other.stdout.readline().strip()
            self.assertTrue(line.startswith("busy:"), line)
            self.assertIn("parent job", line)
            self.assertIn("pid %d" % os.getpid(), line)
        finally:
            lock.release()

    def test_never_uses_os_kill(self):
        with open(os.path.join(support.REPO, "jobs.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIsNone(re.search(r"\bos\.kill\b|\bkill\(", source))


class CtrlCTest(unittest.TestCase):
    """The first Ctrl+C asks a command line job to stop; a second one stops at once."""

    def test_first_asks_second_interrupts(self):
        import signal
        before = signal.getsignal(signal.SIGINT)
        ctx = jobs.ConsoleContext("test", stream=io.StringIO())
        with ctx.stop_on_ctrl_c():
            handler = signal.getsignal(signal.SIGINT)
            self.assertIsNot(handler, before)
            handler(signal.SIGINT, None)
            self.assertTrue(ctx.should_stop())
            self.assertIn("Ctrl+C", ctx.stream.getvalue())
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_other_contexts_know_of_no_removed_library(self):
        ctx = jobs.JobContext("test")
        self.assertFalse(jobs.library_removed(ctx, "lib_a"))
        ctx.remove_library("lib_a")
        self.assertTrue(jobs.library_removed(ctx, "lib_a"))
        self.assertFalse(jobs.library_removed(object(), "lib_a"))


if __name__ == "__main__":
    unittest.main()
