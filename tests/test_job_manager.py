"""Tests for the web server's job queue: jobs.JobManager, jobs.UIContext and the runners.

Most tests use small stand-in runners, so they are quick; a few run the
real scan and describe runners on synthesized pictures against the fake
LLM server (127.0.0.1 only), inside a temporary SETTINGS_BASE.
"""

import json
import os
import threading
import time
import unittest
from unittest import mock

import support

import common
import db
import jobs

if support.pillow_available():
    import fake_llm
    import imagegen
    import describe
    import llm
else:
    fake_llm = imagegen = describe = llm = None

# Not a real key: a made-up value used to check that keys never leak.
SECRET = "sk-sentinel-job-secret-0123456789"


quiet = support.quiet


wait_for = support.wait_for


class Gate:
    """A runner that steps until released or stopped, honouring pause and cancel."""

    def __init__(self, steps=1000):
        self.steps = steps
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def __call__(self, ctx, conn, config, params, helper=None):
        self.calls.append(dict(params))
        ctx.stage("work", total=self.steps)
        self.started.set()
        for number in range(self.steps):
            ctx.wait_if_paused()
            if ctx.should_stop():
                return {"done": number}
            if self.release.is_set():
                break
            ctx.step(item="item-%d" % number)
            time.sleep(0.005)
        ctx.log("gate released")
        return {"done": self.steps, "nested": {"list": list(range(50)), "nan": float("nan")}}


def quick(ctx, conn, config, params, helper=None):
    ctx.stage("quick", total=2)
    ctx.step(2, item="b.jpg")
    ctx.log("quick job for %s" % params.get("library_id"))
    return {"files": 2}


class ManagerFixture(support.TempBaseMixin):

    def setUp(self):
        super().setUp()
        self.base.write_config([])
        db.init(log=quiet)
        self.gate = Gate()
        self.runners = {"scan": quick, "describe": quick, "process": quick,
                        "previews": quick, "slow": self.gate,
                        "boom": self.boom, "refuse": self.refuse}

    def manager(self, start=True, **options):
        options.setdefault("poll_interval", 0.05)
        options.setdefault("idle_interval", 0.05)
        manager = jobs.JobManager(runners=self.runners, **options)
        if start:
            self.assertTrue(manager.start())
        self.addCleanup(manager.stop, 10)
        return manager

    @staticmethod
    def boom(ctx, conn, config, params, helper=None):
        raise RuntimeError("exploded with %s" % SECRET)

    @staticmethod
    def refuse(ctx, conn, config, params, helper=None):
        raise jobs.JobError("Library lib_x is no longer in the settings.")

    def state_of(self, manager, job_id):
        job = manager.get(job_id)
        return job["state"] if job else None

    def wait_state(self, manager, job_id, states, timeout=10.0):
        if isinstance(states, str):
            states = (states,)
        wait_for(lambda: self.state_of(manager, job_id) in states, timeout)
        self.assertIn(self.state_of(manager, job_id), states)


class JobManagerTest(ManagerFixture, unittest.TestCase):

    def test_queue_run_done(self):
        manager = self.manager()
        job, added = manager.enqueue("scan", "lib_a", {"allow_mass_removal": True})
        self.assertTrue(added)
        self.assertEqual(job["state"], "queued")
        self.assertTrue(manager.wait_idle(10))
        job = manager.get(job["id"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["summary"], {"files": 2})
        self.assertEqual((job["stage"], job["done"], job["total"]), ("quick", 2, 2))
        self.assertIsNotNone(job["started"])
        self.assertIsNotNone(job["finished"])
        snap = manager.snapshot(names={"lib_a": "Family"})
        self.assertIsNone(snap["current"])
        self.assertEqual(snap["queue"], [])
        self.assertEqual(snap["recent"][0]["id"], job["id"])
        self.assertEqual(snap["recent"][0]["library"], "Family")
        self.assertIn("duration", snap["recent"][0])
        messages = [line["message"] for line in snap["log"]]
        self.assertIn("quick job for lib_a", messages)
        seqs = [line["seq"] for line in snap["log"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(snap["seq"], seqs[-1])
        # Only newer lines are sent once the page has seen some.
        self.assertEqual(manager.snapshot(since_seq=snap["seq"])["log"], [])
        self.assertEqual(manager.snapshot(since_seq=seqs[-2])["log"][0]["seq"], seqs[-1])
        # A page that saw a later number (an earlier server) gets everything.
        self.assertEqual(len(manager.snapshot(since_seq=10 ** 6)["log"]), len(seqs))
        # The job has its own log file.
        with open(os.path.join(common.logs_dir(), "job-%d.log" % job["id"]),
                  encoding="utf-8") as handle:
            self.assertIn("quick job for lib_a", handle.read())
        self.assertFalse(snap["lock"]["busy"])
        self.assertTrue(snap["manager"]["running"])

    def test_duplicates_are_dropped_and_order_is_fifo(self):
        manager = self.manager(start=False)
        first, added = manager.enqueue("scan", "lib_a", {"allow_mass_removal": True})
        self.assertTrue(added)
        twin, added = manager.enqueue("scan", "lib_a", {"allow_mass_removal": True})
        self.assertFalse(added)
        self.assertEqual(twin["id"], first["id"])
        other, added = manager.enqueue("scan", "lib_a", {})
        self.assertTrue(added)
        everyone, added = manager.enqueue("scan", None, {})
        self.assertTrue(added)
        again, added = manager.enqueue("scan", None, {})
        self.assertFalse(added)
        self.assertEqual(again["id"], everyone["id"])
        queue = manager.snapshot()["queue"]
        self.assertEqual([job["id"] for job in queue], [first["id"], other["id"], everyone["id"]])
        with self.assertRaises(ValueError):
            manager.enqueue("export", None, {})

    def test_pause_resume_and_cancel_a_running_job(self):
        manager = self.manager()
        job, _added = manager.enqueue("slow", "lib_a", {})
        self.assertTrue(self.gate.started.wait(10))
        self.assertTrue(manager.pause(job["id"]))
        self.assertEqual(self.state_of(manager, job["id"]), "paused")
        self.assertFalse(manager.pause(job["id"]))          # already paused
        time.sleep(0.1)
        done = manager.snapshot()["current"]["done"]
        time.sleep(0.2)
        current = manager.snapshot()["current"]
        self.assertEqual(current["done"], done)              # no progress while paused
        self.assertTrue(current["paused"])
        self.assertEqual(current["state"], "paused")
        self.assertIsNotNone(manager.resume(job["id"]))
        self.assertEqual(self.state_of(manager, job["id"]), "running")
        wait_for(lambda: manager.snapshot()["current"]["done"] > done)
        self.assertTrue(manager.cancel(job["id"]))
        self.wait_state(manager, job["id"], "cancelled")
        self.assertTrue(manager.wait_idle(10))
        self.assertFalse(manager.cancel(job["id"]))          # finished already
        self.assertIsNone(manager.resume(job["id"]))

    def test_cancel_a_queued_job(self):
        manager = self.manager()
        running, _added = manager.enqueue("slow", None, {})
        self.assertTrue(self.gate.started.wait(10))
        queued, _added = manager.enqueue("scan", "lib_b", {})
        self.assertTrue(manager.cancel(queued["id"]))
        self.assertEqual(self.state_of(manager, queued["id"]), "cancelled")
        self.gate.release.set()
        self.assertTrue(manager.wait_idle(10))
        self.assertEqual(self.state_of(manager, running["id"]), "done")
        self.assertEqual(self.state_of(manager, queued["id"]), "cancelled")
        # A finished job keeps a compact summary: lists cut, NaN dropped.
        summary = manager.get(running["id"])["summary"]
        self.assertEqual(len(summary["nested"]["list"]), jobs.SUMMARY_LIST_LIMIT)
        self.assertIsNone(summary["nested"]["nan"])

    def test_cancel_library_jobs(self):
        manager = self.manager(start=False)
        one, _added = manager.enqueue("scan", "lib_a", {})
        two, _added = manager.enqueue("describe", "lib_a", {})
        other, _added = manager.enqueue("scan", "lib_b", {})
        self.assertEqual(manager.cancel_library_jobs("lib_a"), 2)
        self.assertEqual([self.state_of(manager, job["id"]) for job in (one, two, other)],
                         ["cancelled", "cancelled", "queued"])

    def test_failures_are_recorded_and_redacted(self):
        manager = self.manager()
        boom, _added = manager.enqueue("boom", None, {})
        refuse, _added = manager.enqueue("refuse", "lib_x", {})
        self.assertTrue(manager.wait_idle(10))
        job = manager.get(boom["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("RuntimeError: exploded", job["error"])
        self.assertNotIn(SECRET, job["error"])
        job = manager.get(refuse["id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"], "Library lib_x is no longer in the settings.")
        snap = manager.snapshot()
        self.assertNotIn(SECRET, json.dumps(snap))
        self.assertEqual(snap["errors"][-1]["level"], "error")

    def test_keys_of_configured_endpoints_are_masked_in_logs(self):
        self.base.write_config([], endpoints=[{"name": "cloud", "preset": "openai",
                                               "model": "m", "api_key": SECRET}])

        def leaky(ctx, conn, config, params, helper=None):
            ctx.log("the server said: bad key %s" % SECRET, "warn")
            return {"error": "rejected %s" % SECRET}
        self.runners["leaky"] = leaky
        manager = self.manager()
        job, _added = manager.enqueue("leaky", None, {})
        self.assertTrue(manager.wait_idle(10))
        job = manager.get(job["id"])
        self.assertEqual(job["state"], "failed")
        text = json.dumps([job, manager.snapshot()])
        self.assertNotIn(SECRET, text)
        with open(os.path.join(common.logs_dir(), "job-%d.log" % job["id"]),
                  encoding="utf-8") as handle:
            self.assertNotIn(SECRET, handle.read())

    def test_interrupted_jobs_after_a_restart_can_be_resumed(self):
        conn = jobs.connect_jobs_db()
        self.addCleanup(conn.close)
        with conn:
            for state in ("running", "paused", "cancelling"):
                conn.execute("INSERT INTO jobs (kind, library_id, params, state, created, "
                             "started) VALUES ('scan', 'lib_a', '{}', ?, ?, ?)",
                             (state, time.time(), time.time()))
        ids = [row[0] for row in conn.execute("SELECT id FROM jobs ORDER BY id")]
        manager = self.manager()
        for job_id in ids:
            self.assertEqual(self.state_of(manager, job_id), "interrupted")
            self.assertEqual(manager.get(job_id)["error"], jobs.INTERRUPTED_MESSAGE)
        resumed = manager.resume(ids[0])
        self.assertEqual(resumed["id"], ids[0])
        self.assertTrue(manager.wait_idle(10))
        self.assertEqual(self.state_of(manager, ids[0]), "done")
        self.assertEqual(manager.get(ids[0])["error"], "")
        # A cancelled interrupted job is no longer offered for resuming.
        self.assertTrue(manager.cancel(ids[1]))
        self.assertEqual(self.state_of(manager, ids[1]), "cancelled")
        self.assertIsNone(manager.resume(ids[1]))

    def test_stop_interrupts_the_running_job(self):
        manager = self.manager()
        job, _added = manager.enqueue("slow", None, {})
        self.assertTrue(self.gate.started.wait(10))
        self.assertTrue(manager.stop(10))
        self.assertEqual(self.state_of(manager, job["id"]), "interrupted")
        self.assertFalse(manager.running)

    def test_only_one_manager_runs_per_data_folder(self):
        first = self.manager()
        second = self.manager(start=False)
        self.assertFalse(second.start())
        self.assertIn("Another server", second.message)
        self.assertFalse(second.snapshot()["manager"]["running"])
        first.stop(10)
        self.assertTrue(second.start())

    def test_waits_while_another_process_holds_the_lock(self):
        other = jobs.WorkerLock()
        self.assertTrue(other.acquire("scan.py"))
        self.addCleanup(other.release)
        manager = self.manager()
        job, _added = manager.enqueue("scan", "lib_a", {})
        current = wait_for(lambda: (manager.snapshot()["current"] or {}).get("waiting"))
        self.assertTrue(current)
        snap = manager.snapshot()
        self.assertEqual(snap["current"]["state"], "queued")
        self.assertEqual(snap["current"]["stage"], jobs.WAITING_STAGE)
        self.assertEqual(self.state_of(manager, job["id"]), "queued")
        self.assertTrue(snap["lock"]["busy"])
        self.assertTrue(snap["lock"]["other"])
        self.assertIn("scan.py", snap["lock"]["holder"])
        # "Ask it to stop" leaves an abort file for the other process...
        answer = manager.abort_other()
        self.assertTrue(answer["requested"])
        self.assertTrue(os.path.exists(common.abort_path()))
        # ...which is removed once that process has let go.
        other.release()
        self.assertTrue(manager.wait_idle(10))
        self.assertEqual(self.state_of(manager, job["id"]), "done")
        self.assertTrue(wait_for(lambda: not os.path.exists(common.abort_path())))
        self.assertFalse(manager.abort_other()["requested"])

    def test_the_queue_never_waits_for_a_scan_writing_index_db(self):
        # A running scan holds index.db's write lock nearly all the time; the
        # queue lives in jobs.db, so adding, listing and cancelling jobs go on.
        manager = self.manager(start=False)
        writer = db.connect()
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            job, added = manager.enqueue("scan", "lib_a", {})
            self.assertTrue(added)
            self.assertEqual(manager.snapshot()["queue"][0]["id"], job["id"])
            self.assertTrue(manager.cancel(job["id"]))
            self.assertEqual(manager.latest_by_library()["lib_a"]["state"], "cancelled")
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            writer.rollback()
        self.assertTrue(os.path.isfile(jobs.jobs_db_path()))

    def test_a_running_job_is_not_queued_again(self):
        manager = self.manager()
        job, _added = manager.enqueue("slow", "lib_a", {})
        self.assertTrue(self.gate.started.wait(10))
        self.wait_state(manager, job["id"], "running")
        twin, added = manager.enqueue("slow", "lib_a", {})
        self.assertFalse(added)
        self.assertEqual((twin["id"], twin["state"]), (job["id"], "running"))
        self.assertTrue(manager.pause(job["id"]))
        twin, added = manager.enqueue("slow", "lib_a", {})
        self.assertFalse(added)
        self.assertEqual(twin["state"], "paused")
        # Other parameters are another job.
        other, added = manager.enqueue("slow", "lib_a", {"limit": 1})
        self.assertTrue(added)
        self.assertTrue(manager.cancel(other["id"]))
        self.assertIsNotNone(manager.resume(job["id"]))
        self.gate.release.set()
        self.assertTrue(manager.wait_idle(10))

    def test_a_second_server_shows_the_job_the_first_one_runs(self):
        first = self.manager()
        second = self.manager(start=False)
        self.assertFalse(second.start())
        # Queued through the second server, run by the first.
        job, _added = second.enqueue("slow", "lib_a", {})
        self.assertTrue(self.gate.started.wait(10))
        snap = wait_for(lambda: second.snapshot() if (second.snapshot()["current"] or {}).get(
            "remote") else None)
        self.assertTrue(snap, second.snapshot())
        current = snap["current"]
        self.assertEqual((current["id"], current["state"], current["stage"]),
                         (job["id"], "running", "work"))
        self.assertIn("web server jobs", current["holder"])
        self.assertEqual(snap["queue"], [])
        self.assertEqual((snap["lock"]["other"], snap["lock"]["kind"]), (True, "server"))
        self.assertFalse(second.owns(job["id"]))
        # It cannot pause or cancel the other server's job, nor ask it to stop.
        self.assertFalse(second.pause(job["id"]))
        self.assertFalse(second.cancel(job["id"]))
        answer = second.abort_other()
        self.assertFalse(answer["requested"])
        self.assertIn("another web server", answer["message"])
        self.assertFalse(os.path.exists(common.abort_path()))
        # The first server still can.
        self.assertTrue(first.owns(job["id"]))
        self.assertTrue(first.pause(job["id"]))
        self.assertEqual(wait_for(lambda: second.snapshot()["current"]["state"] == "paused"),
                         True)
        self.assertTrue(first.cancel(job["id"]))
        self.wait_state(first, job["id"], "cancelled")
        self.assertIsNone(second.snapshot()["current"])

    def test_a_job_cancelled_elsewhere_while_waiting_does_not_run(self):
        other = jobs.WorkerLock()
        self.assertTrue(other.acquire("scan.py"))
        self.addCleanup(other.release)
        manager = self.manager()
        job, _added = manager.enqueue("slow", "lib_a", {})
        wait_for(lambda: (manager.snapshot()["current"] or {}).get("waiting"))
        # Another server's page cancels it in jobs.db (it is still "queued" there).
        conn = jobs.connect_jobs_db()
        try:
            with conn:
                conn.execute("UPDATE jobs SET state = 'cancelled' WHERE id = ?", (job["id"],))
        finally:
            conn.close()
        other.release()
        self.assertTrue(manager.wait_idle(10))
        self.assertEqual(self.state_of(manager, job["id"]), "cancelled")
        self.assertEqual(self.gate.calls, [])

    def test_a_waiting_job_can_be_cancelled(self):
        other = jobs.WorkerLock()
        self.assertTrue(other.acquire("scan.py"))
        self.addCleanup(other.release)
        manager = self.manager()
        job, _added = manager.enqueue("scan", "lib_a", {})
        wait_for(lambda: (manager.snapshot()["current"] or {}).get("waiting"))
        self.assertFalse(manager.pause(job["id"]))
        self.assertTrue(manager.cancel(job["id"]))
        self.wait_state(manager, job["id"], "cancelled")


class UIContextTest(support.TempBaseMixin, unittest.TestCase):

    def test_rate_eta_errors_and_log_file(self):
        path = os.path.join(common.logs_dir(), "job-7.log")
        ctx = jobs.UIContext(7, "describe", log_path=path)
        ctx.add_secret(SECRET)
        ctx.stage("describe", total=100)
        self.assertIsNone(ctx.progress()["rate"])
        start = time.monotonic()
        with mock.patch.object(jobs.time, "monotonic", return_value=start + 30):
            ctx.step(10, item="a.jpg")
        with mock.patch.object(jobs.time, "monotonic", return_value=start + 60):
            ctx.step(10, item="b.jpg")
            progress = ctx.progress()
        self.assertAlmostEqual(progress["rate"], 20.0, delta=0.5)      # per minute
        self.assertAlmostEqual(progress["eta"], 240, delta=10)          # 80 left at 20/min
        self.assertEqual(progress["item"], "b.jpg")
        # The rate only looks at the last RATE_WINDOW seconds.
        with mock.patch.object(jobs.time, "monotonic", return_value=start + 600):
            ctx.step(10, item="c.jpg")
        with mock.patch.object(jobs.time, "monotonic", return_value=start + 660):
            ctx.step(30, item="d.jpg")
            self.assertAlmostEqual(ctx.rate(), 30.0, delta=1)
        ctx.log("could not read d.jpg: key %s" % SECRET, "warn")
        ctx.log("fine")
        errors = ctx.error_list()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["item"], "d.jpg")
        self.assertEqual(errors[0]["stage"], "describe")
        self.assertNotIn(SECRET, errors[0]["message"])
        lines, seq = ctx.buffer.since(0)
        self.assertEqual(seq, lines[-1]["seq"])
        self.assertTrue(all(line["job"] == 7 for line in lines))
        ctx.close()
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Warning: could not read d.jpg", text)
        self.assertNotIn(SECRET, text)

    def test_quick_steps_do_not_count_towards_the_rate(self):
        saved = []
        ctx = jobs.UIContext(8, "scan", saver=lambda *values: saved.append(values),
                             save_interval=1000)
        ctx.stage("scan", total=10000)
        start = time.monotonic()
        # A re-scan: 9000 unchanged files in a second, then slow new ones.
        with mock.patch.object(jobs.time, "monotonic", return_value=start + 1):
            ctx.step(9000, quick=True)
        for number in range(1, 31):
            with mock.patch.object(jobs.time, "monotonic", return_value=start + 1 + 2 * number):
                ctx.step(item="new-%d.jpg" % number)
        with mock.patch.object(jobs.time, "monotonic", return_value=start + 61):
            progress = ctx.progress()
        self.assertAlmostEqual(progress["rate"], 30.0, delta=1.5)        # per minute
        self.assertGreater(progress["eta"], 30 * 60)                     # 970 left at 30/min
        self.assertEqual((progress["done"], ctx.quick), (9030, 9000))
        # The stage start was saved at once; the rest waits for save_interval.
        self.assertEqual(saved, [("scan", 0, 10000)])
        messages = [line["message"] for line in ctx.buffer.since(0)[0]]
        self.assertIn("Looking for new and changed pictures (10000 to do)", messages)

    def test_buffers_are_bounded(self):
        buffer = jobs.LogBuffer(limit=5)
        ctx = jobs.UIContext(1, buffer=buffer, error_limit=3)
        for number in range(10):
            ctx.log("problem %d" % number, "error")
        lines, seq = buffer.since(0)
        self.assertEqual(len(lines), 5)
        self.assertEqual(seq, 10)
        self.assertEqual([entry["message"] for entry in ctx.error_list()],
                         ["problem 7", "problem 8", "problem 9"])


@unittest.skipIf(fake_llm is None, "Pillow is not installed")
class RealRunnerTest(support.TempBaseMixin, unittest.TestCase):
    """The real scan / describe / process / previews runners, end to end."""

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(describe, "UNREACHABLE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        llm._no_stream_options.clear()
        self.addCleanup(llm._no_stream_options.clear)
        self.family = self.base.library_folder("Family")
        self.trips = self.base.library_folder("Trips")
        # Different sizes: identical pictures would share one description.
        for width, folder, name in ((64, self.family, "a.jpg"), (72, self.family, "b.jpg"),
                                    (80, self.trips, "c.jpg")):
            image = imagegen.marker_image((width, 48))
            imagegen.write_bytes(folder, name, imagegen.jpeg_bytes(image))
        endpoint = {"name": "local", "kind": "openai", "base_url": self.server.base_url,
                    "model": "fake-model"}
        self.base.write_config([
            support.library("Family", self.family, id="lib_family1"),
            support.library("Trips", self.trips, id="lib_trips01"),
            support.library("Offline", self.base.join("libraries", "missing"),
                            id="lib_offline"),
        ], endpoints=[endpoint], default_endpoint="local")
        db.init(log=quiet)
        self.manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.assertTrue(self.manager.start())
        self.addCleanup(self.manager.stop, 30)

    def rows(self):
        conn = db.connect()
        try:
            return conn.execute("SELECT library_id, path, described, has_preview FROM images "
                                "ORDER BY path").fetchall()
        finally:
            conn.close()

    def test_scan_then_describe_one_library(self):
        job, _added = self.manager.enqueue("scan", "lib_family1", {})
        self.assertTrue(self.manager.wait_idle(60))
        job = self.manager.get(job["id"])
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(job["summary"]["libraries"][0]["added"], 2)
        rows = self.rows()
        self.assertEqual([(row["path"], row["described"], row["has_preview"]) for row in rows],
                         [("a.jpg", 0, 1), ("b.jpg", 0, 1)])
        job, _added = self.manager.enqueue("describe", "lib_family1", {"limit": 1})
        self.assertTrue(self.manager.wait_idle(60))
        job = self.manager.get(job["id"])
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(job["summary"]["described"], 1)
        self.assertEqual(sum(row["described"] for row in self.rows()), 1)

    def test_process_all_skips_offline_libraries(self):
        job, _added = self.manager.enqueue("process", None, {})
        self.assertTrue(self.manager.wait_idle(60))
        job = self.manager.get(job["id"])
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(job["summary"]["offline"], ["Offline"])
        self.assertEqual(job["summary"]["describe"]["described"], 3)
        rows = self.rows()
        self.assertEqual(sorted({row["library_id"] for row in rows}),
                         ["lib_family1", "lib_trips01"])
        self.assertTrue(all(row["described"] for row in rows))
        warnings = [line["message"] for line in self.manager.snapshot()["log"]
                    if line["level"] == "warn"]
        self.assertTrue(any("Offline" in message for message in warnings), warnings)

    def test_scanning_an_offline_library_fails_and_previews_job_runs(self):
        job, _added = self.manager.enqueue("scan", "lib_offline", {})
        previews, _added = self.manager.enqueue("previews", "lib_trips01", {})
        self.assertTrue(self.manager.wait_idle(60))
        job = self.manager.get(job["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("not available", job["error"])
        self.assertEqual(self.manager.get(previews["id"])["state"], "done")

    def test_a_disabled_or_removed_library_fails_cleanly(self):
        config = common.load_config()
        config["libraries"][0]["enabled"] = False
        common.save_config(config)
        disabled, _added = self.manager.enqueue("scan", "lib_family1", {})
        gone, _added = self.manager.enqueue("describe", "lib_gone0001", {})
        self.assertTrue(self.manager.wait_idle(60))
        self.assertIn("disabled", self.manager.get(disabled["id"])["error"])
        self.assertIn("no longer in the settings", self.manager.get(gone["id"])["error"])


class RemovedLibraryTest(ManagerFixture, unittest.TestCase):
    """Removing a library stops the work on it, also the job that is running."""

    def test_the_running_job_of_a_removed_library_is_cancelled(self):
        manager = self.manager()
        running, _added = manager.enqueue("slow", "lib_a", {})
        self.assertTrue(self.gate.started.wait(10))
        queued, _added = manager.enqueue("scan", "lib_a", {})
        self.assertEqual(manager.cancel_library_jobs("lib_a"), 1)
        self.wait_state(manager, running["id"], "cancelled")
        self.assertEqual(self.state_of(manager, queued["id"]), "cancelled")
        self.assertLess(manager.get(running["id"])["done"], self.gate.steps)

    def test_a_job_of_every_library_skips_a_removed_one(self):
        manager = self.manager()
        running, _added = manager.enqueue("slow", None, {})
        self.assertTrue(self.gate.started.wait(10))
        other, _added = manager.enqueue("scan", "lib_b", {})
        self.assertEqual(manager.cancel_library_jobs("lib_a"), 0)
        ctx = manager._current["ctx"]
        self.assertTrue(ctx.library_removed("lib_a"))
        self.assertFalse(ctx.library_removed("lib_b"))
        self.assertTrue(jobs.library_removed(ctx, "lib_a"))
        # The job itself goes on, and so do the other libraries' jobs.
        self.assertEqual(self.state_of(manager, running["id"]), "running")
        self.gate.release.set()
        self.assertTrue(manager.wait_idle(10))
        self.assertEqual(self.state_of(manager, running["id"]), "done")
        self.assertEqual(self.state_of(manager, other["id"]), "done")


if __name__ == "__main__":
    unittest.main()
