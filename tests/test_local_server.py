"""Tests for local_server.py: the model server manager.

The "model server" is tests/fake_model_server.py (a stand-in for
llama-server on a 127.0.0.1 port), started through launch_command; no
model is ever loaded. Every test runs in a temporary SETTINGS_BASE, and
any process a test leaves behind is ended in its clean-up.
"""

import io
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

import support

import common
import fake_llm
import jobs
import local_server

FAKE_SERVER = os.path.join(support.REPO, "tests", "fake_model_server.py")

wait_for = support.wait_for


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"GGUF")


class Logs:
    """A log(message, level) function that keeps what it is given."""

    def __init__(self):
        self.lines = []

    def __call__(self, message, level="info"):
        self.lines.append((level, message))

    def text(self):
        return "\n".join(message for _level, message in self.lines)


class ServerFixture(support.TempBaseMixin):
    """A config with a llama.cpp endpoint and a local_server that runs the fake server."""

    def setUp(self):
        super().setUp()
        self.models = self.base.make_dir("models")
        for name in ("model.gguf", "model.mmproj.gguf", "sub/your-model.gguf",
                     "sub/mmproj-model.gguf", "notes.txt"):
            touch(os.path.join(self.models, *name.split("/")))
        self.port = free_port()
        self.marker = self.base.join("marker.txt")
        for name, value in (("GRACE_SECONDS", 3.0), ("START_POLL", 0.1), ("KILL_WAIT", 10.0)):
            patcher = mock.patch.object(local_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.end_processes)
        self.configure()

    def configure(self, args=(), endpoint=None, **server):
        entry = {"enabled": True, "endpoint": "local", "models_dir": self.models,
                 "model": "model.gguf", "port": self.port,
                 "launch_command": ["{python}", FAKE_SERVER, "--port", "{port}", "--marker",
                                    self.marker] + list(args)}
        entry.update(server)
        endpoints = [endpoint or {"name": "local", "preset": "llamacpp", "model": "m",
                                  "base_url": "http://127.0.0.1:%d/v1" % self.port}]
        self.base.write_config([], endpoints=endpoints, default_endpoint="local",
                               local_server=entry)
        self.config = common.load_config()
        return self.config

    def marker_lines(self):
        try:
            with open(self.marker, encoding="utf-8") as handle:
                return [line.split() for line in handle.read().splitlines() if line.strip()]
        except OSError:
            return []

    def end_processes(self):
        """End whatever a test left running (servers in the record, their children)."""
        pids = [record.get("pid") for record in local_server.load_state().values()]
        pids += [int(parts[1]) for parts in self.marker_lines()
                 if parts[0] in ("child", "started")]
        for pid in pids:
            if isinstance(pid, int) and local_server.process_identity(pid) is not None:
                local_server.kill_tree(pid)


class SettingsTest(support.TempBaseMixin, unittest.TestCase):

    def test_defaults_and_values_that_cannot_be_used(self):
        settings = local_server.server_settings({"endpoint": " local ", "port": "8091",
                                                 "slots": 0, "ctx_per_slot": 4096.0,
                                                 "auto_stop": "no", "stop_at": "3:30",
                                                 "extra_args": ["-t", 8, None, True],
                                                 "model": "sub\\model.gguf",
                                                 "start_timeout": 5})
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["endpoint"], "local")
        self.assertEqual(settings["port"], 8091)            # a string is not a port
        self.assertEqual(settings["slots"], 4)              # out of range
        self.assertEqual(settings["ctx_per_slot"], 4096)
        self.assertTrue(settings["auto_start"])
        self.assertTrue(settings["auto_stop"])              # not a bool
        self.assertEqual(settings["stop_at"], "03:30")
        self.assertEqual(settings["extra_args"], ["-t", "8"])
        self.assertEqual(settings["model"], "sub/model.gguf")
        self.assertEqual(settings["start_timeout"], 900)
        self.assertEqual(local_server.server_settings({"stop_at": "25:00"})["stop_at"], "")
        self.assertEqual(local_server.parse_stop_at("23:59"), (23, 59))
        self.assertIsNone(local_server.parse_stop_at("noon"))

    def test_one_object_or_a_list(self):
        config = {"local_server": {"endpoint": "Local", "enabled": True}}
        self.assertEqual([item["endpoint"] for item in local_server.server_list(config)],
                         ["Local"])
        self.assertIsNotNone(local_server.find_server(config, "local"))
        self.assertIsNotNone(local_server.managed(config, "LOCAL"))
        config = {"local_server": [{"endpoint": "a", "enabled": True},
                                   {"endpoint": "A", "port": 9000}, {"endpoint": ""},
                                   "junk", {"endpoint": "b"}]}
        self.assertEqual([item["endpoint"] for item in local_server.server_list(config)],
                         ["a", "b"])
        self.assertIsNone(local_server.managed(config, "b"))        # turned off
        self.assertEqual(local_server.server_list({}), [])
        self.assertEqual(local_server.server_list({"local_server": "x"}), [])
        # One object logs to model-server.log; a list to one log per endpoint.
        self.assertTrue(local_server.log_path({"local_server": {}}, "a").endswith(
            os.path.join("logs", "model-server.log")))
        self.assertTrue(local_server.log_path(config, "a b").endswith("model-server-a-b.log"))


class FilesTest(ServerFixture, unittest.TestCase):

    def test_model_choices(self):
        # A model split in two parts (only the first is offered).
        parts = ["model-%05d-of-00002%s" % (number, local_server.MODEL_EXTENSION)
                 for number in (1, 2)]
        for part in parts:
            touch(os.path.join(self.models, part))
        touch(os.path.join(self.models, ".hidden", "model.gguf"))
        touch(os.path.join(self.models, "sub", "deeper", "model.gguf"))
        choices = local_server.model_choices(local_server.find_server(self.config, "local"))
        self.assertEqual(choices["error"], "")
        self.assertEqual(choices["models"], [parts[0], "model.gguf", "sub/your-model.gguf"])
        self.assertEqual(choices["mmprojs"], ["model.mmproj.gguf", "sub/mmproj-model.gguf"])
        missing = local_server.server_settings({"models_dir": self.base.join("nowhere")})
        self.assertIn("does not exist", local_server.model_choices(missing)["error"])
        self.assertIn("not set", local_server.model_choices(
            local_server.server_settings({}))["error"])

    def test_the_projector_is_found_next_to_the_model(self):
        settings = local_server.find_server(self.config, "local")
        self.assertEqual(local_server.auto_mmproj(settings), "model.mmproj.gguf")
        settings["model"] = "sub/your-model.gguf"
        self.assertEqual(local_server.auto_mmproj(settings), "sub/mmproj-model.gguf")
        self.assertEqual(local_server.mmproj_path(settings),
                         os.path.join(self.models, "sub", "mmproj-model.gguf"))
        os.remove(os.path.join(self.models, "model.mmproj.gguf"))
        settings["model"] = "model.gguf"
        self.assertEqual(local_server.auto_mmproj(settings), "")
        self.assertEqual(local_server.mmproj_path(settings), "")
        settings["mmproj"] = "sub/mmproj-model.gguf"
        self.assertTrue(local_server.mmproj_path(settings).endswith("mmproj-model.gguf"))

    def test_only_files_inside_the_models_folder(self):
        settings = local_server.find_server(self.config, "local")
        for model in ("../model.gguf", "sub/../../model.gguf"):
            settings["model"] = model
            with self.assertRaises(local_server.ServerError):
                local_server.model_path(settings)
        settings["model"] = "your-model.gguf"         # (only in sub/)
        with self.assertRaisesRegex(local_server.ServerError, "does not exist"):
            local_server.model_path(settings)
        settings["model"] = ""
        with self.assertRaisesRegex(local_server.ServerError, "No model"):
            local_server.model_path(settings)
        settings["model"] = "model.gguf"
        settings["mmproj"] = "../model.mmproj.gguf"
        with self.assertRaises(local_server.ServerError):
            local_server.mmproj_path(settings)

    def test_the_default_command(self):
        binary = self.base.join("bin", "llama-server")
        settings = local_server.server_settings({
            "binary": binary, "models_dir": self.models, "model": "model.gguf", "port": 8095,
            "slots": 3, "ctx_per_slot": 4096, "extra_args": ["--threads", "8"]})
        self.assertEqual(local_server.build_command(settings), [
            binary, "-m", os.path.join(self.models, "model.gguf"),
            "--mmproj", os.path.join(self.models, "model.mmproj.gguf"),
            "--host", "127.0.0.1", "--port", "8095", "-np", "3", "-c", "12288",
            "-fa", "on", "--reasoning", "off", "--no-webui", "--threads", "8"])
        settings["binary"] = ""
        with self.assertRaisesRegex(local_server.ServerError, "binary"):
            local_server.build_command(settings)

    def test_a_launch_command_replaces_it(self):
        settings = local_server.server_settings({
            "models_dir": self.models, "model": "sub/your-model.gguf", "port": 8092,
            "slots": 2, "ctx_per_slot": 1024, "extra_args": ["--ignored"],
            "launch_command": ["{python}", "wrapper.py", "--model-path", "{model_path}",
                               "--mmproj-path={mmproj_path}", "--port", "{port}",
                               "--slots", "{slots}", "-c", "{ctx}", "{ctx_per_slot}"]})
        self.assertEqual(local_server.build_command(settings), [
            sys.executable, "wrapper.py", "--model-path",
            os.path.join(self.models, "sub", "your-model.gguf"),
            "--mmproj-path=" + os.path.join(self.models, "sub", "mmproj-model.gguf"),
            "--port", "8092", "--slots", "2", "-c", "2048", "1024"])

    def test_secret_arguments_are_masked(self):
        # Not real keys: made-up values used only by this test.
        command = ["llama-server", "--api-key", "hunter2-password", "--api-key-file=k.txt",
                   "--alias", "sk-test-0123456789abcdefghij"]
        text = local_server.masked_command(command)
        for secret in ("hunter2-password", "k.txt", "sk-test-0123456789abcdefghij"):
            self.assertNotIn(secret, text)
        self.assertIn("--api-key ***", text)
        self.assertIn("--api-key-file=***", text)

    def test_what_stops_a_start(self):
        settings = local_server.find_server(self.config, "local")
        self.assertEqual(local_server.setup_problem(self.config, settings), "")
        config = self.configure(endpoint={"name": "local", "preset": "llamacpp", "model": "m",
                                          "base_url": "http://127.0.0.1:8080/v1"})
        problem = local_server.setup_problem(config, local_server.find_server(config, "local"))
        self.assertIn("Set the endpoint's base URL to http://127.0.0.1:%d/v1" % self.port,
                      problem)
        config = self.configure(endpoint={"name": "other", "preset": "llamacpp", "model": "m"})
        self.assertIn("There is no endpoint called local", local_server.setup_problem(
            config, local_server.find_server(config, "local")))
        config = self.configure(enabled=False)
        self.assertIn("turned off", local_server.setup_problem(
            config, local_server.find_server(config, "local")))
        config = self.configure(model="your-model.gguf")
        self.assertIn("does not exist", local_server.setup_problem(
            config, local_server.find_server(config, "local")))
        config = self.configure(launch_command=[], binary=self.base.join("no", "server.exe"))
        self.assertIn("does not exist", local_server.setup_problem(
            config, local_server.find_server(config, "local")))


class StopTimeTest(unittest.TestCase):

    def window(self, stop_at, now):
        return local_server.stop_window({"stop_at": stop_at}, now)

    def test_the_window_runs_from_ten_minutes_before_to_an_hour_after(self):
        day = datetime(2026, 5, 4)
        self.assertIsNone(self.window("03:30", day.replace(hour=3, minute=19)))
        self.assertEqual(self.window("03:30", day.replace(hour=3, minute=20)),
                         day.replace(hour=3, minute=30))
        self.assertIsNotNone(self.window("03:30", day.replace(hour=4, minute=29)))
        self.assertIsNone(self.window("03:30", day.replace(hour=4, minute=30)))
        self.assertIsNone(self.window("", day))
        # Across midnight.
        self.assertEqual(self.window("00:05", datetime(2026, 5, 3, 23, 58)),
                         datetime(2026, 5, 4, 0, 5))
        self.assertEqual(self.window("23:50", datetime(2026, 5, 4, 0, 40)),
                         datetime(2026, 5, 3, 23, 50))

    def test_the_next_time_jobs_must_stop(self):
        settings = {"stop_at": "03:30"}
        self.assertEqual(local_server.next_pre_stop(settings, datetime(2026, 5, 4, 1, 0)),
                         datetime(2026, 5, 4, 3, 20))
        self.assertEqual(local_server.next_pre_stop(settings, datetime(2026, 5, 4, 3, 25)),
                         datetime(2026, 5, 5, 3, 20))
        self.assertIsNone(local_server.next_pre_stop({"stop_at": ""}, datetime(2026, 5, 4)))
        message = local_server.window_message({"endpoint": "local", "stop_at": "03:30"},
                                              datetime(2026, 5, 4, 3, 30))
        self.assertIn("from 03:20 until 04:30", message)


class ProcessTest(unittest.TestCase):

    def test_identity_tells_processes_apart(self):
        mine = local_server.process_identity(os.getpid())
        self.assertIsNotNone(mine)
        self.assertEqual(local_server.process_identity(os.getpid()), mine)
        self.assertTrue(local_server.is_running(os.getpid(), mine))
        self.assertFalse(local_server.is_running(os.getpid(), mine + "0"))
        self.assertFalse(local_server.is_running(os.getpid(), None))
        for pid in (0, -1, True, "12", None):
            self.assertIsNone(local_server.process_identity(pid))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(local_server.process_identity(child.pid))


class ManagerTest(ServerFixture, unittest.TestCase):

    def test_start_waits_until_ready_and_stop_is_graceful(self):
        self.configure(args=["--load-seconds", "0.5"])
        manager = local_server.ServerManager()
        logs = Logs()
        ok, message = manager.start(self.config, "local", wait=True, log=logs)
        self.assertTrue(ok, message)
        self.assertIn("is ready (model.gguf)", message)
        self.assertIn("Waiting for the model server to load its model", logs.text())
        status = manager.status(self.config, "local")
        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["owned"])
        self.assertEqual(status["started_by"], "you")
        self.assertEqual(status["served"], {"model": "model.gguf", "slots": 2, "vision": True})
        self.assertEqual(status["endpoint_url"], "http://127.0.0.1:%d/v1" % self.port)
        self.assertIn("fake model server listening", "\n".join(status["log_tail"]))
        record = local_server.load_state()["local"]
        self.assertEqual(record["pid"], status["pid"])
        self.assertEqual(record["identity"], local_server.process_identity(status["pid"]))
        # Starting again does not start a second one.
        self.assertEqual(manager.start(self.config, "local"),
                         (True, "The model server is already running."))
        ok, message = manager.stop(self.config, "local", reason="a test asked")
        self.assertTrue(ok, message)
        self.assertEqual(message, "Stopped the model server for local because a test asked.")
        self.assertIn(["graceful", str(record["pid"])], self.marker_lines())
        self.assertEqual(local_server.load_state(), {})
        status = manager.status(self.config, "local")
        self.assertEqual((status["state"], status["owned"]), ("stopped", False))
        with open(local_server.log_path(self.config, "local"), encoding="utf-8") as handle:
            log = handle.read()
        self.assertIn("starting the model server for local (by you)", log)
        self.assertIn("Stopped the model server for local", log)
        self.assertEqual(manager.stop(self.config, "local")[0], False)

    def test_start_in_the_background(self):
        self.configure(args=["--load-seconds", "1"])
        manager = local_server.ServerManager()
        ok, message = manager.start(self.config, "local")
        self.assertTrue(ok)
        self.assertEqual(message, "Starting the model server...")
        seen = set()

        def ready():
            seen.add(manager.status(self.config, "local")["state"])
            return "ready" in seen
        self.assertTrue(wait_for(ready, 20, 0.05))
        self.assertIn("loading", seen)
        self.assertTrue(manager.stop(self.config, "local")[0])

    def test_a_restarted_manager_still_stops_the_server(self):
        first = local_server.ServerManager()
        self.assertTrue(first.start(self.config, "local", wait=True)[0])
        pid = local_server.load_state()["local"]["pid"]
        second = local_server.ServerManager()
        self.assertTrue(second.owned("local"))
        status = second.status(self.config, "local")
        self.assertEqual((status["state"], status["pid"]), ("ready", pid))
        self.assertTrue(second.stop(self.config, "local")[0])
        self.assertIn(["graceful", str(pid)], self.marker_lines())
        self.assertIsNone(local_server.process_identity(pid))

    def test_a_pid_given_to_another_program_is_never_touched(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(other.kill)
        local_server.save_state({"local": {"pid": other.pid, "identity": "w1",
                                           "started": time.time(), "by": "you"}})
        manager = local_server.ServerManager()
        self.assertFalse(manager.owned("local"))
        ok, message = manager.stop(self.config, "local")
        self.assertFalse(ok)
        self.assertIn("No model server started here", message)
        self.assertIsNone(other.poll())
        self.assertEqual(local_server.load_state(), {})

    def test_a_port_that_already_answers_is_left_alone(self):
        with fake_llm.FakeLLM() as other:
            port = int(other.root_url.rsplit(":", 1)[1])
            config = self.configure(port=port, endpoint={
                "name": "local", "preset": "llamacpp", "model": "m",
                "base_url": "http://127.0.0.1:%d/v1" % port})
            manager = local_server.ServerManager()
            ok, message = manager.start(config, "local", wait=True)
            self.assertFalse(ok)
            self.assertIn("Port %d is already in use (llama.cpp with model.gguf)" % port,
                          message)
            status = manager.status(config, "local")
            self.assertEqual(status["state"], "external")
            self.assertFalse(status["owned"])
            self.assertFalse(manager.stop(config, "local")[0])
            self.assertEqual(self.marker_lines(), [])

    def test_a_server_that_ignores_the_stop_is_ended_with_its_children(self):
        self.configure(args=["--ignore-stop", "--child"])
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        child = int(next(parts[1] for parts in self.marker_lines() if parts[0] == "child"))
        self.assertIsNotNone(local_server.process_identity(child))
        ok, message = manager.stop(self.config, "local")
        self.assertTrue(ok, message)
        self.assertIn("so it was ended", message)
        self.assertFalse(any(parts[0] == "graceful" for parts in self.marker_lines()))
        self.assertTrue(wait_for(lambda: local_server.process_identity(child) is None, 10))

    def test_a_server_that_cannot_see_images_is_stopped_again(self):
        self.configure(args=["--no-vision"])
        manager = local_server.ServerManager()
        ok, message = manager.start(self.config, "local", wait=True)
        self.assertFalse(ok)
        self.assertIn("cannot see images", message)
        self.assertEqual(local_server.load_state(), {})
        self.assertFalse(local_server.port_in_use(self.port))
        status = manager.status(self.config, "local")
        self.assertEqual(status["state"], "error")
        self.assertIn("cannot see images", status["message"])
        # Without a projector nothing is expected of it.
        os.remove(os.path.join(self.models, "model.mmproj.gguf"))
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        self.assertTrue(manager.stop(self.config, "local")[0])

    def test_a_server_that_exits_while_starting(self):
        self.configure(args=["--load-seconds", "30", "--exit-after", "0.5"])
        manager = local_server.ServerManager()
        ok, message = manager.start(self.config, "local", wait=True)
        self.assertFalse(ok)
        self.assertIn("stopped while starting (exit code 3)", message)
        self.assertIn("giving up (as asked)", message)
        self.assertEqual(manager.status(self.config, "local")["state"], "error")
        self.assertEqual(local_server.load_state(), {})

    def test_a_server_that_takes_too_long_is_stopped(self):
        self.configure(args=["--load-seconds", "60"])
        manager = local_server.ServerManager()
        ok, message = manager.start(self.config, "local", wait=True, timeout=1)
        self.assertFalse(ok)
        self.assertIn("did not become ready within 1 seconds", message)
        self.assertEqual(local_server.load_state(), {})
        self.assertTrue(any(parts[0] == "graceful" for parts in self.marker_lines()))

    def test_stopping_while_it_starts(self):
        self.configure(args=["--load-seconds", "60"])
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local")[0])
        self.assertTrue(wait_for(lambda: manager.status(self.config, "local")["state"]
                                 == "loading", 20, 0.05))
        ok, message = manager.stop(self.config, "local")
        self.assertTrue(ok, message)
        status = manager.status(self.config, "local")
        self.assertEqual(status["state"], "stopped")
        self.assertFalse(local_server.port_in_use(self.port))

    def test_problems_are_reported_without_starting_anything(self):
        manager = local_server.ServerManager()
        config = self.configure(model="your-model.gguf")
        ok, message = manager.start(config, "local")
        self.assertFalse(ok)
        self.assertIn("does not exist", message)
        self.assertEqual(manager.start(config, "nothing")[0], False)
        status = manager.status(config, "nothing")
        self.assertEqual(status["state"], "not configured")
        config = self.configure(enabled=False)
        self.assertEqual(manager.status(config, "local")["state"], "off")
        self.assertEqual(self.marker_lines(), [])


class LeaseTest(ServerFixture, unittest.TestCase):

    def test_a_run_starts_the_server_and_stops_it_afterwards(self):
        manager = local_server.ServerManager()
        ctx = jobs.JobContext("describe")
        ctx.job_id = 7
        lease = manager.lease(ctx, self.config, ["local", "cloud"])
        self.assertEqual(lease.acquire(), {})
        self.assertEqual(lease.started, ["local"])
        status = manager.status(self.config, "local")
        self.assertEqual((status["state"], status["started_by"]), ("ready", "job 7"))
        self.assertEqual(status["in_use_by"], ["job 7"])
        lease.release()
        self.assertFalse(manager.owned("local"))
        self.assertEqual(manager.status(self.config, "local")["in_use_by"], [])
        messages = [entry["message"] for entry in ctx.snapshot(log_tail=100)["log_tail"]]
        self.assertTrue(any("Starting the model server for local" in text for text in messages))
        self.assertTrue(any("because the job that started it has finished" in text
                            for text in messages))

    def test_a_server_someone_started_keeps_running(self):
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        with manager.lease(jobs.JobContext("describe"), self.config, ["local"]) as lease:
            self.assertEqual(lease.acquire(), {})
            self.assertEqual(lease.started, [])
        self.assertTrue(manager.owned("local"))
        self.assertTrue(manager.stop(self.config, "local")[0])
        # Nor does one a run started when auto_stop is off.
        config = self.configure(auto_stop=False)
        with manager.lease(jobs.JobContext("describe"), config, ["local"]) as lease:
            self.assertEqual(lease.acquire(), {})
            self.assertEqual(lease.started, ["local"])
        self.assertTrue(manager.owned("local"))
        self.assertTrue(manager.stop(config, "local")[0])

    def test_without_auto_start_a_stopped_server_is_a_problem(self):
        config = self.configure(auto_start=False)
        manager = local_server.ServerManager()
        with manager.lease(jobs.JobContext("describe"), config, ["local"]) as lease:
            problems = lease.acquire()
        self.assertIn("auto start is off", problems["local"])
        self.assertEqual(self.marker_lines(), [])

    def test_a_server_started_elsewhere_is_used_as_it_is(self):
        with fake_llm.FakeLLM() as other:
            port = int(other.root_url.rsplit(":", 1)[1])
            config = self.configure(port=port, endpoint={
                "name": "local", "preset": "llamacpp", "model": "m",
                "base_url": "http://127.0.0.1:%d/v1" % port})
            manager = local_server.ServerManager()
            ctx = jobs.JobContext("describe")
            with manager.lease(ctx, config, ["local"]) as lease:
                self.assertEqual(lease.acquire(), {})
                self.assertEqual(lease.started, [])
            self.assertIn("already running on port %d (not started here)" % port,
                          ctx.snapshot()["log_tail"][-1]["message"])
            self.assertEqual(self.marker_lines(), [])

    def test_no_run_uses_it_around_its_stop_time(self):
        config = self.configure(stop_at="03:30")
        manager = local_server.ServerManager(clock=lambda: datetime(2026, 5, 4, 3, 45))
        with manager.lease(jobs.JobContext("describe"), config, ["local"]) as lease:
            problems = lease.acquire()
        self.assertIn("stops at 03:30 every day", problems["local"])
        self.assertIn("until 04:30", problems["local"])
        self.assertEqual(self.marker_lines(), [])

    def test_a_run_is_interrupted_before_the_stop_time(self):
        config = self.configure(stop_at="03:30")
        now = [datetime(2026, 5, 4, 3, 19, 59)]
        manager = local_server.ServerManager(clock=lambda: now[0])
        ctx = jobs.JobContext("describe")
        lease = manager.lease(ctx, config, ["local"])
        self.assertEqual(lease.acquire(), {})
        now[0] = datetime(2026, 5, 4, 3, 20, 1)
        self.assertTrue(wait_for(ctx.should_stop, 10))
        self.assertIn("daily stop time (03:30)", ctx.interrupted)
        self.assertIn("resumed after 04:30", ctx.interrupted)
        lease.release()
        self.assertFalse(manager.owned("local"))

    def test_the_stop_time_stops_a_server_started_here_once(self):
        config = self.configure(stop_at="03:30")
        now = [datetime(2026, 5, 4, 2, 0)]
        manager = local_server.ServerManager(clock=lambda: now[0])
        self.assertTrue(manager.start(config, "local", wait=True)[0])
        self.assertEqual(manager.check_schedule(config), [])
        now[0] = datetime(2026, 5, 4, 3, 25)        # jobs stop, the server keeps running
        self.assertEqual(manager.check_schedule(config), [])
        self.assertTrue(manager.owned("local"))
        now[0] = datetime(2026, 5, 4, 3, 31)
        logs = Logs()
        self.assertEqual(manager.check_schedule(config, log=logs), ["local"])
        self.assertIn("because its stop time (03:30) came", logs.text())
        self.assertFalse(manager.owned("local"))
        # Started by hand afterwards: it keeps running.
        self.assertTrue(manager.start(config, "local", wait=True)[0])
        now[0] = datetime(2026, 5, 4, 3, 40)
        self.assertEqual(manager.check_schedule(config), [])
        self.assertTrue(manager.owned("local"))
        self.assertTrue(manager.stop(config, "local")[0])

    def test_turned_off_servers_are_not_managed(self):
        config = self.configure(enabled=False)
        manager = local_server.ServerManager()
        with manager.lease(jobs.JobContext("describe"), config, ["local"]) as lease:
            self.assertEqual(lease.acquire(), {})
            self.assertEqual(lease.started, [])
        self.assertEqual(self.marker_lines(), [])

    def test_shutdown_stops_what_runs_started(self):
        manager = local_server.ServerManager()
        lease = manager.lease(jobs.JobContext("describe"), self.config, ["local"])
        self.assertEqual(lease.acquire(), {})
        manager.shutdown(grace=3)
        self.assertFalse(manager.owned("local"))


class InterruptedJobTest(support.TempBaseMixin, unittest.TestCase):
    """A job stopped for the stop time ends "interrupted" (resumable), not cancelled."""

    def setUp(self):
        super().setUp()
        import db
        self.base.write_config([])
        db.init(log=support.quiet)
        self.calls = []

        def runner(ctx, conn, config, params, helper=None):
            self.calls.append(params)
            ctx.stage("describe", total=1)
            if len(self.calls) == 1:
                ctx.interrupt("Stopping for the model server's daily stop time (03:30).")
            return {"described": 0, "stopped": ctx.should_stop()}

        def cancelled(ctx, conn, config, params, helper=None):
            ctx.stage("describe", total=1)
            started.set()
            support.wait_for(ctx.should_stop, 10)
            ctx.interrupt("Late interrupt.")
            return {}

        started = threading.Event()
        self.started = started
        self.manager = jobs.JobManager(runners={"describe": runner, "slow": cancelled},
                                       poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(self.manager.stop, 10)
        self.assertTrue(self.manager.start())

    def test_interrupted_then_resumed(self):
        job, _added = self.manager.enqueue("describe", None, {})
        self.assertTrue(self.manager.wait_idle(10))
        job = self.manager.get(job["id"])
        self.assertEqual(job["state"], "interrupted")
        self.assertIn("daily stop time", job["error"])
        self.assertIsNotNone(self.manager.resume(job["id"]))
        self.assertTrue(self.manager.wait_idle(10))
        self.assertEqual(self.manager.get(job["id"])["state"], "done")

    def test_a_cancel_stays_a_cancel(self):
        job, _added = self.manager.enqueue("slow", None, {})
        self.assertTrue(self.started.wait(10))
        self.assertTrue(self.manager.cancel(job["id"]))
        self.assertTrue(self.manager.wait_idle(10))
        job = self.manager.get(job["id"])
        self.assertEqual((job["state"], job["error"]), ("cancelled", ""))

    def test_the_console_context_stops_too(self):
        ctx = jobs.ConsoleContext("scan", stream=io.StringIO())
        ctx.interrupt("Stop for the stop time.")
        ctx.interrupt("Again.")
        self.assertTrue(ctx.should_stop())
        self.assertEqual(ctx.interrupted, "Again.")
        self.assertEqual(ctx.stream.getvalue().count("Stop for the stop time."), 1)


class DefaultManagerTest(support.TempBaseMixin, unittest.TestCase):

    def test_one_manager_per_data_folder(self):
        first = local_server.default_manager()
        self.assertIs(local_server.default_manager(), first)
        with support.TempBase():
            self.assertIsNot(local_server.default_manager(), first)


class ProcessTreeTest(ServerFixture, unittest.TestCase):
    """What a server started (a wrapper's child) belongs to it, even after the wrapper ends."""

    def child_pid(self):
        self.assertTrue(wait_for(lambda: any(parts[0] == "child"
                                             for parts in self.marker_lines()), 10))
        return int(next(parts[1] for parts in self.marker_lines() if parts[0] == "child"))

    def test_a_wrapper_that_ends_first_does_not_leave_its_server_running(self):
        # The wrapper leaves on Ctrl+Break at once; the server it started ignores it.
        self.configure(args=["--wrapper", "--ignore-stop"])
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        child = self.child_pid()
        ok, message = manager.stop(self.config, "local")
        self.assertTrue(ok, message)
        self.assertIn("so it was ended", message)
        self.assertTrue(wait_for(lambda: local_server.process_identity(child) is None, 10))
        self.assertFalse(local_server.port_in_use(self.port))
        self.assertEqual(local_server.load_state(), {})

    def test_a_server_whose_wrapper_has_ended_is_still_ours(self):
        self.configure(args=["--wrapper", "--wrapper-exit-after", "1"])
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        record = local_server.load_state()["local"]
        child = self.child_pid()
        self.assertTrue(wait_for(lambda: local_server.process_identity(record["pid"]) is None,
                                 10))
        self.assertIsNotNone(local_server.process_identity(child))
        # It is still the server started here (not "external"), also after a restart.
        self.assertTrue(manager.owned("local"))
        restarted = local_server.ServerManager()
        status = restarted.status(self.config, "local")
        self.assertEqual((status["state"], status["owned"]), ("ready", True))
        ok, message = restarted.stop(self.config, "local")
        self.assertTrue(ok, message)
        self.assertIn(["graceful", str(child)], self.marker_lines())
        self.assertFalse(local_server.port_in_use(self.port))
        self.assertEqual(local_server.load_state(), {})

    def test_a_wrapper_that_ends_at_once_still_hands_over_its_server(self):
        # (It ends before the server it started has even begun to listen.)
        self.configure(args=["--wrapper", "--wrapper-exit-after", "0", "--load-seconds",
                             "0.5"])
        manager = local_server.ServerManager()
        ok, message = manager.start(self.config, "local", wait=True)
        self.assertTrue(ok, message)
        child = self.child_pid()
        self.assertIn([child, local_server.process_identity(child)],
                      local_server.load_state()["local"]["members"])
        self.assertTrue(manager.stop(self.config, "local")[0])
        self.assertIn(["graceful", str(child)], self.marker_lines())

    def test_a_wrapper_that_ends_while_its_server_starts_is_not_a_failure(self):
        self.configure(args=["--wrapper", "--wrapper-exit-after", "0.2", "--load-seconds",
                             "1.5"])
        manager = local_server.ServerManager()
        ok, message = manager.start(self.config, "local", wait=True)
        self.assertTrue(ok, message)
        self.assertTrue(manager.stop(self.config, "local")[0])

    def test_a_port_that_still_answers_after_the_stop_is_reported(self):
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        with mock.patch.object(local_server, "port_in_use", return_value=True), \
                mock.patch.object(local_server, "PORT_RELEASE_WAIT", 0.2):
            ok, message = manager.stop(self.config, "local")
        self.assertFalse(ok)
        self.assertIn("port %d still answers" % self.port, message)
        self.assertEqual(local_server.load_state(), {})


class SharedRecordTest(ServerFixture, unittest.TestCase):
    """data/model-server.json is shared by several programs (the web server, scan.py)."""

    def test_two_programs_starting_servers_at_once_keep_both_records(self):
        other_port = free_port()
        entries = []
        for name, port in (("one", self.port), ("two", other_port)):
            entries.append({"enabled": True, "endpoint": name, "models_dir": self.models,
                            "model": "model.gguf", "port": port,
                            "launch_command": ["{python}", FAKE_SERVER, "--port", "{port}",
                                               "--marker", self.marker]})
        self.base.write_config([], endpoints=[
            {"name": name, "preset": "llamacpp", "model": "m",
             "base_url": "http://127.0.0.1:%d/v1" % port}
            for name, port in (("one", self.port), ("two", other_port))],
            default_endpoint="one", local_server=entries)
        config = common.load_config()
        for _round in range(3):
            first, second = local_server.ServerManager(), local_server.ServerManager()
            barrier = threading.Barrier(2)

            def start(manager, name):
                barrier.wait()
                manager.start(config, name)
            threads = [threading.Thread(target=start, args=(first, "one")),
                       threading.Thread(target=start, args=(second, "two"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(sorted(local_server.load_state()), ["one", "two"])
            self.assertTrue(wait_for(lambda: first.status(config, "one")["state"] == "ready"
                                     and second.status(config, "two")["state"] == "ready",
                                     20, 0.1))
            self.assertTrue(first.stop(config, "one")[0])
            self.assertTrue(second.stop(config, "two")[0])
            self.assertEqual(local_server.load_state(), {})

    def test_a_lost_record_does_not_orphan_a_server_this_program_started(self):
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local", wait=True)[0])
        pid = local_server.load_state()["local"]["pid"]
        local_server.save_state({})             # (another program saved over it)
        self.assertTrue(manager.owned("local"))
        self.assertEqual(local_server.load_state()["local"]["pid"], pid)   # written again
        ok, message = manager.stop(self.config, "local")
        self.assertTrue(ok, message)
        self.assertIsNone(local_server.process_identity(pid))

    def test_the_lock_keeps_others_out_until_it_is_released(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with local_server.state_lock() as locked:
                self.assertTrue(locked)
                with local_server.state_lock() as again:        # re-entrant
                    self.assertTrue(again)
                held.set()
                release.wait(10)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(10))
        started = time.monotonic()
        with local_server.state_lock(timeout=0.3) as locked:
            self.assertFalse(locked)            # gave up waiting (and went on)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        release.set()
        thread.join(10)
        with local_server.state_lock(timeout=1) as locked:
            self.assertTrue(locked)


class StartAndStopTogetherTest(ServerFixture, unittest.TestCase):

    def test_a_stop_during_a_start_tells_those_waiting_for_it(self):
        self.configure(args=["--load-seconds", "30"])
        manager = local_server.ServerManager()
        self.assertTrue(manager.start(self.config, "local")[0])       # the Start button
        self.assertTrue(wait_for(lambda: manager.status(self.config, "local")["state"]
                                 == "loading", 20, 0.05))
        ctx = jobs.JobContext("describe")
        lease = manager.lease(ctx, self.config, ["local"])
        found = {}
        thread = threading.Thread(target=lambda: found.update(problems=lease.acquire()))
        thread.start()
        time.sleep(0.5)
        self.assertTrue(manager.stop(self.config, "local", wait=False)[0])    # the Stop button
        thread.join(30)
        self.assertIn("stopped before it was ready", found["problems"]["local"])
        self.assertTrue(wait_for(lambda: manager.status(self.config, "local")["state"]
                                 == "stopped", 20, 0.1))
        lease.release()
        self.assertFalse(local_server.port_in_use(self.port))


class LogTest(ServerFixture, unittest.TestCase):

    def test_secret_arguments_are_masked_in_the_log(self):
        path = self.base.join("server.log")
        # Not real keys: made-up values used only by this test.
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("$ llama-server -m model.gguf --api-key lsk-made-up-1234567890 "
                         "--port 8091\n--hf-token hf-made-up-value\n--api-key-file=k.txt\n"
                         "-hft 'quoted made-up'\nmy--api-key stays\n")
        lines = local_server.log_tail(path)
        text = "\n".join(lines)
        for secret in ("lsk-made-up-1234567890", "hf-made-up-value", "k.txt", "quoted made-up"):
            self.assertNotIn(secret, text)
        self.assertIn("--api-key *** --port 8091", text)
        self.assertIn("my--api-key stays", text)
        # Not real keys: made-up values used only by this test.
        settings = local_server.server_settings({
            "launch_command": ["x", "--api-key", "made-up-1", "--hf-token=made-up-2"],
            "extra_args": ["-hft", "made-up-3", "--api-key"]})
        self.assertEqual(local_server.command_secrets(settings),
                         ["made-up-1", "made-up-2", "made-up-3"])

    def test_a_failed_start_quotes_only_its_own_log_lines(self):
        self.configure(args=["--load-seconds", "30", "--exit-after", "0.3"])
        manager = local_server.ServerManager()
        self.assertFalse(manager.start(self.config, "local", wait=True)[0])
        ok, message = manager.start(self.config, "local", wait=True)
        self.assertFalse(ok)
        self.assertEqual(message.count("giving up (as asked)"), 1, message)
        self.assertNotIn("=====", message)
        self.assertNotIn("$ ", message)
        self.assertIn("fake model server listening", message)

    def test_the_stop_time_placeholder(self):
        settings = local_server.server_settings({
            "models_dir": self.models, "model": "model.gguf", "stop_at": "3:30",
            "launch_command": ["{python}", "launcher.py", "--stop-at", "{stop_at}"]})
        self.assertEqual(local_server.build_command(settings)[-2:], ["--stop-at", "03:30"])
        settings["stop_at"] = ""
        self.assertEqual(local_server.build_command(settings)[-2:], ["--stop-at", ""])


class FileFactsTest(ServerFixture, unittest.TestCase):

    def test_the_status_can_use_facts_worked_out_elsewhere(self):
        manager = local_server.ServerManager()
        with mock.patch.object(local_server, "file_facts",
                               side_effect=AssertionError("looked at the files")):
            status = manager.status(self.config, "local", facts={
                "checking": True, "problem": "", "command": None, "mmproj_auto": ""})
        self.assertTrue(status["checking"])
        self.assertEqual((status["problem"], status["command"]), ("", ""))
        facts = local_server.file_facts(local_server.find_server(self.config, "local"))
        self.assertEqual(facts["problem"], "")
        self.assertEqual(facts["mmproj_auto"], "model.mmproj.gguf")
        self.assertIn(FAKE_SERVER, facts["command"])
        status = manager.status(self.config, "local", facts=facts)
        self.assertFalse(status["checking"])
        self.assertIn("fake_model_server.py", status["command"])
        config = self.configure(model="your-model.gguf")
        facts = local_server.file_facts(local_server.find_server(config, "local"))
        self.assertIn("does not exist", facts["problem"])
        self.assertIsNone(facts["command"])


class MoreLeaseTest(ServerFixture, unittest.TestCase):

    def test_a_server_started_by_hand_that_still_loads_is_used(self):
        other = subprocess.Popen([sys.executable, FAKE_SERVER, "--port", str(self.port),
                                  "--load-seconds", "30"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.assertTrue(wait_for(lambda: local_server.health(self.port) == 503, 20, 0.1))
        for auto_start in (True, False):
            config = self.configure(auto_start=auto_start)
            manager = local_server.ServerManager()
            ctx = jobs.JobContext("describe")
            with manager.lease(ctx, config, ["local"]) as lease:
                self.assertEqual(lease.acquire(), {})
                self.assertEqual(lease.started, [])
            self.assertIn("still loading its model", ctx.snapshot()["log_tail"][-1]["message"])
        self.assertIsNone(other.poll())

    def test_a_run_that_cannot_use_the_server_does_not_hold_it(self):
        config = self.configure(auto_start=False, stop_at="03:30")
        manager = local_server.ServerManager(clock=lambda: datetime(2026, 5, 4, 2, 0))
        ctx = jobs.JobContext("describe")
        lease = manager.lease(ctx, config, ["local", "cloud"])
        self.assertIn("auto start is off", lease.acquire()["local"])
        self.assertEqual(manager.leases("local"), [])
        self.assertEqual(lease._timers, {})
        manager.check_schedule(config, now=datetime(2026, 5, 4, 3, 21))
        self.assertFalse(ctx.interrupted)
        self.assertFalse(ctx.should_stop())
        lease.release()


class ScheduleTest(ServerFixture, unittest.TestCase):

    def test_a_restart_does_not_stop_a_server_started_after_the_stop_time(self):
        config = self.configure(stop_at="03:30")
        first = local_server.ServerManager(clock=lambda: datetime(2026, 5, 4, 3, 40))
        self.assertTrue(first.start(config, "local", wait=True)[0])     # by hand, at 03:40
        restarted = local_server.ServerManager(clock=lambda: datetime(2026, 5, 4, 3, 45))
        self.assertEqual(restarted.check_schedule(config), [])
        self.assertTrue(restarted.owned("local"))
        # One started before the stop time is stopped, also by a restarted program.
        self.assertTrue(restarted.stop(config, "local")[0])
        early = local_server.ServerManager(clock=lambda: datetime(2026, 5, 4, 3, 0))
        self.assertTrue(early.start(config, "local", wait=True)[0])
        self.assertEqual(restarted.check_schedule(config), ["local"])
        self.assertFalse(restarted.owned("local"))

    def test_a_stop_that_fails_is_tried_again(self):
        config = self.configure(stop_at="03:30")
        now = [datetime(2026, 5, 4, 3, 0)]
        manager = local_server.ServerManager(clock=lambda: now[0])
        self.assertTrue(manager.start(config, "local", wait=True)[0])
        now[0] = datetime(2026, 5, 4, 3, 31)
        with mock.patch.object(manager, "stop", return_value=(False, "did not stop")) as stop:
            self.assertEqual(manager.check_schedule(config), [])
            now[0] = datetime(2026, 5, 4, 3, 32)
            self.assertEqual(manager.check_schedule(config), [])
            self.assertEqual(stop.call_count, 2)
        self.assertEqual(manager.check_schedule(config), ["local"])
        self.assertFalse(manager.owned("local"))


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class DescribeWithManagedServerTest(ServerFixture, unittest.TestCase):
    """A describe run starts the model server it needs, and stops it afterwards."""

    def test_describe_starts_and_stops_the_server(self):
        import db
        import describe
        import imagegen
        folder = self.base.library_folder("Family")
        imagegen.jpeg_with_exif(folder, "a.jpg", image=imagegen.blocks_image(8))
        self.base.write_config(
            [support.library("Family", folder)],
            endpoints=[{"name": "local", "preset": "llamacpp", "model": "m",
                        "base_url": "http://127.0.0.1:%d/v1" % self.port}],
            default_endpoint="local", local_server=self.config["local_server"])
        config = common.load_config()
        db.init(config, log=support.quiet)
        config = common.load_config()
        import scan
        scan.run(config, jobs.JobContext("scan"), describe_images=False)
        manager = local_server.ServerManager()
        ctx = jobs.JobContext("describe", log_limit=1000)
        conn = db.connect()
        try:
            summary = describe.describe_pending(conn, config, ctx, servers=manager,
                                                sleep=lambda seconds: None)
        finally:
            conn.close()
        self.assertEqual(summary["described"], 1, summary)
        self.assertFalse(manager.owned("local"))
        self.assertTrue(any(parts[0] == "graceful" for parts in self.marker_lines()))
        log = "\n".join(entry["message"] for entry in ctx.snapshot(log_tail=1000)["log_tail"])
        self.assertIn("Starting the model server for local", log)
        self.assertIn("model.gguf", log)

    def test_describe_fails_with_the_reason_the_server_cannot_start(self):
        import db
        import describe
        import imagegen
        import scan
        folder = self.base.library_folder("Family")
        imagegen.jpeg_with_exif(folder, "a.jpg", image=imagegen.blocks_image(8))
        entry = dict(self.config["local_server"], auto_start=False)
        self.base.write_config(
            [support.library("Family", folder)],
            endpoints=[{"name": "local", "preset": "llamacpp", "model": "m",
                        "base_url": "http://127.0.0.1:%d/v1" % self.port}],
            default_endpoint="local", local_server=entry)
        config = common.load_config()
        db.init(config, log=support.quiet)
        config = common.load_config()
        scan.run(config, jobs.JobContext("scan"), describe_images=False)
        conn = db.connect()
        try:
            summary = describe.describe_pending(conn, config, jobs.JobContext("describe"),
                                                servers=local_server.ServerManager(),
                                                sleep=lambda seconds: None)
        finally:
            conn.close()
        self.assertEqual(summary["described"], 0)
        self.assertIn("auto start is off", summary["error"])
        self.assertEqual(self.marker_lines(), [])


if __name__ == "__main__":
    unittest.main()
