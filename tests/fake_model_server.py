#!/usr/bin/env python3
"""A stand-in for llama-server, for the model server manager tests.

    python fake_model_server.py --port N [--load-seconds S] [--no-vision]
                                [--ignore-stop] [--exit-after S] [--marker FILE]
                                [--child] [--wrapper] [--wrapper-exit-after S]

Listens on 127.0.0.1:N and answers like fake_llm.FakeLLM (/health, /props,
chat completions with the default description). /health says 503
(loading) for the first S seconds. --no-vision makes /props report no
image support. On Ctrl+Break (Windows) or SIGTERM it writes "graceful
<pid>" to the marker file and exits, unless --ignore-stop is given (then
only ending the process tree stops it). --exit-after makes it exit by
itself with code 3 after S seconds, before it is ready. --child starts a
second, idle process of its own (to check that stopping ends the whole
process tree); its PID is written to the marker file as "child <pid>".
--wrapper makes this process a stand-in for a launch_command wrapper: it
starts the server (this script with the other options) as its child,
writes "child <pid>" to the marker file, and exits at once on Ctrl+Break or
SIGTERM (or by itself after --wrapper-exit-after seconds) without stopping
it, so that only stopping the whole process tree ends the server.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import ThreadingHTTPServer  # noqa: E402

import fake_llm  # noqa: E402


def note(path, text):
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")


def wrap(args):
    """Run the server as a child, and leave it running when this process ends."""
    own = [arg for arg in sys.argv[1:] if arg != "--wrapper"]
    if "--wrapper-exit-after" in own:
        index = own.index("--wrapper-exit-after")
        del own[index:index + 2]
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + own)
    note(args.marker, "child %d" % child.pid)

    def leave(_signum=None, _frame=None):
        note(args.marker, "wrapper-left %d" % os.getpid())
        os._exit(0)

    for name in ("SIGBREAK", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, leave)
    started = time.monotonic()
    while True:
        time.sleep(0.05)
        if (args.wrapper_exit_after is not None
                and time.monotonic() - started >= args.wrapper_exit_after):
            leave()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--load-seconds", type=float, default=0.0)
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--ignore-stop", action="store_true")
    parser.add_argument("--exit-after", type=float)
    parser.add_argument("--marker", default="")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--api-key", default="", help="(accepted and ignored)")
    parser.add_argument("--idle", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wrapper", action="store_true")
    parser.add_argument("--wrapper-exit-after", type=float)
    args = parser.parse_args()

    if args.wrapper:
        wrap(args)
        return

    if args.idle:
        # The child of --child: it only waits to be ended.
        while True:
            time.sleep(0.2)

    def stop_handler(signum, _frame):
        note(args.marker, "graceful %d" % os.getpid())
        os._exit(0)

    for name in ("SIGBREAK", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, signal.SIG_IGN if args.ignore_stop else stop_handler)

    if args.child:
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--port", "0",
                                  "--idle"])
        note(args.marker, "child %d" % child.pid)

    fake = fake_llm.FakeLLM()
    fake.props_data["model_path"] = "/models/model.gguf"
    fake.props_data["total_slots"] = 2
    fake.props_data["modalities"] = {"vision": not args.no_vision}
    if args.load_seconds > 0:
        fake.health_status = 503
        fake.props_status = 503

        def loaded():
            time.sleep(args.load_seconds)
            fake.health_status = 200
            fake.props_status = 200
        threading.Thread(target=loaded, daemon=True).start()

    class Handler(fake_llm._Handler):
        pass
    Handler.fake = fake

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    note(args.marker, "started %d" % os.getpid())
    print("fake model server listening on port %d" % args.port, flush=True)
    if args.exit_after is not None:
        def leave():
            time.sleep(args.exit_after)
            print("giving up (as asked)", flush=True)
            os._exit(3)
        threading.Thread(target=leave, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
