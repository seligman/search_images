"""Tests for the command lines of jobs (jobs.py: job_command, log_command): every kind of
job logs as its first line the command that does the same, and that command, run, does
the same.

The round trip: for each kind of job, the job's runner is run as the Manage page's job
manager runs it, with the work it hands on to (scan.run, describe.describe_pending,
export.export_library, ...) replaced by recorders; then the command line is built from
the job's params, split as Windows splits a command line, and given to the tool's own
main() (its argparse) with the same recorders; both must hand on the same work."""

import base64
import contextlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import support

import admin
import albums
import common
import db
import describe
import embeddings
import export
import gps_check
import jobs
import local_times
import looks
import make_places
import people
import places
import scan
import named_places
import street_maps
import trip_export
import trip_stories
import trips

B = chr(92)     # a backslash


def windows_split(line):
    """The arguments of a command line as a Windows program (the C runtime) reads them."""
    args, arg, index, quoted, started = [], [], 0, False, False
    while index < len(line):
        char = line[index]
        if char in " \t" and not quoted:
            if started:
                args.append("".join(arg))
                arg, started = [], False
            index += 1
            continue
        started = True
        if char == B:
            end = index
            while end < len(line) and line[end] == B:
                end += 1
            count = end - index
            if end < len(line) and line[end] == '"':
                arg.append(B * (count // 2))
                if count % 2:
                    arg.append('"')
                    end += 1
            else:
                arg.append(B * count)
            index = end
            continue
        if char == '"':
            if quoted and line[index + 1:index + 2] == '"':
                arg.append('"')
                index += 2
                continue
            quoted = not quoted
            index += 1
            continue
        arg.append(char)
        index += 1
    if started:
        args.append("".join(arg))
    return args


# Arguments that must reach a program as they are, whichever shell the line is typed in.
TRICKY = ["plain", "with space", "a,b,c", "#12", "x&y", "(paren)", "it's", "semi;colon",
          "{brace}", "@at", "tilde~", "!bang", "^caret", "<lt>", "|pipe", "a=b", "plus+sign",
          "café été", "--title=", "--credits=-me", "--out=" + "D:" + B + "a b",
          "C:" + B + "My Pictures" + B + "Trip 2019", "D:" + B,
          B + B + "server" + B + "share" + B + "Photos",
          "C:" + B + "Web Site" + B, "C:" + B + "Web Site" + B + B, "3:private+writing,5"]


def same_arguments(test, back, sent):
    """Assert that the arguments a program got are those sent: exactly, but for the folders
    that end in a backslash (PowerShell 7 may hand those on with one more)."""
    test.assertEqual(len(back), len(sent), back)
    for got, wanted in zip(back, sent):
        if wanted.endswith(B):
            test.assertEqual(os.path.normpath(got), os.path.normpath(wanted))
        else:
            test.assertEqual(got, wanted)


class QuotingTest(unittest.TestCase):

    def test_plain_arguments_stay_and_others_are_quoted(self):
        self.assertEqual(jobs.quote_argument("--library"), "--library")
        self.assertEqual(jobs.quote_argument("D:" + B + "Photos" + B + "x.jpg"),
                         "D:" + B + "Photos" + B + "x.jpg")
        self.assertEqual(jobs.quote_argument("My Pictures"), '"My Pictures"')
        self.assertEqual(jobs.quote_argument("1,2"), '"1,2"')
        self.assertEqual(jobs.quote_argument("#12"), '"#12"')
        # A backslash that ends quoted text is doubled, or it would escape the quote.
        self.assertEqual(jobs.quote_argument("C:" + B + "a b" + B),
                         '"C:' + B + "a b" + B + B + '"')

    def test_what_the_shells_read_differently_has_no_command_line(self):
        # (PowerShell takes the curly double quotes for straight ones.)
        for text in ("", "50% off", "$HOME", "a`b", 'say "hi"', "two" + chr(10) + "lines",
                     "a,b" + B, "The \u201cBest\u201d Pics", "low \u201equote"):
            with self.subTest(text=text):
                with self.assertRaises(jobs.NoCommand):
                    jobs.quote_argument(text)

    def test_the_c_runtime_reads_back_every_argument(self):
        line = jobs.command_text("scan.py", TRICKY)
        self.assertTrue(line.startswith("python scan.py "))
        # (The C runtime reads them back exactly.)
        self.assertEqual(windows_split(line)[2:], TRICKY)

    def test_option_values_that_look_like_options_or_are_empty(self):
        self.assertEqual(jobs.option_tokens("--title", ""), ["--title="])
        self.assertEqual(jobs.option_tokens("--credits", "-me"), ["--credits=-me"])
        self.assertEqual(jobs.option_tokens("--limit", 0), ["--limit", "0"])

    def test_a_line_too_long_for_cmd_has_none(self):
        with self.assertRaises(jobs.NoCommand) as caught:
            jobs.command_text("scan.py", ["--ids", ",".join(str(n) for n in range(5000))])
        self.assertIn("characters long", str(caught.exception))


@unittest.skipUnless(os.name == "nt", "cmd and PowerShell are Windows'")
class ShellTest(unittest.TestCase):
    """The quoted arguments, typed in cmd and in PowerShell, reach a Python program as they
    are."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="search-images-shell-")
        self.addCleanup(support.remove_tree, self.folder)
        self.script = os.path.join(self.folder, "echo_args.py")
        with open(self.script, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("import json, sys\n"
                         "sys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode('ascii'))\n")
        self.line = " ".join([jobs.quote_argument(sys.executable),
                              jobs.quote_argument(self.script)]
                             + [jobs.quote_argument(arg) for arg in TRICKY])

    def check(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        back = json.loads(result.stdout.decode("ascii").strip().splitlines()[-1])
        same_arguments(self, back, TRICKY)

    def test_cmd(self):
        self.check(subprocess.run('cmd /d /s /c "%s"' % self.line, capture_output=True,
                                  timeout=60, env=support.child_env()))

    def run_powershell(self, program):
        command = "& " + self.line
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        return subprocess.run([program, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                               encoded], capture_output=True, timeout=120,
                              env=support.child_env())

    def test_windows_powershell(self):
        program = shutil.which("powershell")
        if program is None:
            self.skipTest("no Windows PowerShell")
        self.check(self.run_powershell(program))

    def test_powershell_7(self):
        program = shutil.which("pwsh")
        if program is None:
            self.skipTest("no PowerShell 7")
        self.check(self.run_powershell(program))

    def test_a_curly_quote_ends_quoted_text_in_powershell(self):
        # Why a curly double quote gets no command line (jobs._UNTYPABLE_RE): in double
        # quotes, Windows PowerShell ends the text at it.
        program = shutil.which("powershell")
        if program is None:
            self.skipTest("no Windows PowerShell")
        name = "The \u201cBest\u201d Pics"
        self.line = " ".join([jobs.quote_argument(sys.executable),
                              jobs.quote_argument(self.script), '"%s"' % name])
        result = self.run_powershell(program)
        self.assertEqual(result.returncode, 0, result.stderr)
        back = json.loads(result.stdout.decode("ascii").strip().splitlines()[-1])
        self.assertNotEqual(back, [name])


class CoverageTest(unittest.TestCase):
    """Every kind of job has a command line (or says why not), and every round trip below
    covers it."""

    def test_every_kind_of_job_has_a_command_line_or_a_reason(self):
        for kind in jobs.RUNNERS:
            with self.subTest(kind=kind):
                self.assertTrue(kind in jobs.COMMAND_BUILDERS or kind in jobs.NO_COMMAND_KINDS,
                                "A new kind of job, %s, needs a command line (jobs.py "
                                "COMMAND_BUILDERS) or the reason it has none "
                                "(NO_COMMAND_KINDS)." % kind)
        for kind in jobs.COMMAND_BUILDERS:
            self.assertIn(kind, jobs.RUNNERS)
            self.assertIn(kind, {case[0] for case in CASES},
                          "No round trip below tries the command line of %s jobs." % kind)

    def test_every_param_the_pages_send_has_a_switch(self):
        for kind, names in admin.JOB_PARAMS.items():
            with self.subTest(kind=kind):
                known = jobs.COMMAND_BUILDERS[kind][1]
                # (save_options is taken out before the job is queued.)
                self.assertEqual(sorted(set(names) - set(known) - {"save_options"}), [])

    def test_every_export_option_has_a_switch(self):
        options = export.default_options()
        options["album"] = 7
        tokens = export.options_arguments(options)
        self.assertIn("#7", tokens)
        tokens = export.options_arguments(dict(options, album=None, home_km=20, story=True),
                                          trip=True)
        self.assertNotIn("--no-album", tokens)
        self.assertIn("--home-km", tokens)
        self.assertIn("--story", tokens)
        self.assertIn("--no-story", export.options_arguments({"story": False}, trip=True))
        for wrong, trip in (({"no such option": 1}, False), ({"home_km": 20}, False),
                            ({"album": 7}, True), ({"story": True}, False)):
            with self.assertRaises(ValueError):
                export.options_arguments(wrong, trip=trip)

    def test_a_job_with_a_param_no_switch_gives_says_so(self):
        message = jobs.command_message("scan", {"library_id": None, "fresh_param": 1}, {})
        self.assertEqual(message, "%s: no switch gives fresh_param." % jobs.NO_COMMAND)
        self.assertEqual(jobs.command_message("unheard_of", {}, {}),
                         "%s: no tool runs a unheard_of job." % jobs.NO_COMMAND)

    def test_the_manage_page_knows_the_first_line(self):
        with open(os.path.join(support.REPO, "static", "manage.js"), encoding="utf-8") as handle:
            self.assertIn('var COMMAND_PREFIX = "%s";' % jobs.COMMAND_PREFIX, handle.read())


class LibraryNameTest(unittest.TestCase):

    def test_a_library_goes_by_its_name_when_that_finds_it(self):
        config = {"libraries": [
            {"id": "lib_00000001", "name": "Family"},
            {"id": "lib_00000002", "name": "50% off"},
            {"id": "lib_00000003", "name": "lib_00000001"},
            {"id": "lib_00000004", "name": "-dash"},
            {"id": "lib_00000005", "name": "Caf\u00e9 Pics"},
            {"id": "lib_00000006", "name": "The \u201cBest\u201d Pics"}]}
        self.assertEqual(jobs.library_argument(config, "lib_00000001"), "Family")
        # Not plain ASCII (a batch file in another code page would garble it), or with a
        # curly quote (PowerShell would end the quotes at it): the id.
        self.assertEqual(jobs.library_argument(config, "lib_00000005"), "lib_00000005")
        self.assertEqual(jobs.library_argument(config, "lib_00000006"), "lib_00000006")
        line = jobs.job_command_line("scan", {"library_id": "lib_00000005"}, config)
        self.assertTrue(line.isascii(), line)
        # Not typable alike in PowerShell and cmd: the id.
        self.assertEqual(jobs.library_argument(config, "lib_00000002"), "lib_00000002")
        # A name that is another library's id would find that one: the id.
        self.assertEqual(jobs.library_argument(config, "lib_00000003"), "lib_00000003")
        # Gone from the settings: the id.
        self.assertEqual(jobs.library_argument(config, "lib_00000009"), "lib_00000009")
        # (The settings in the program folder: no --settings-base, whatever SETTINGS_BASE
        # the tests were started with.)
        with mock.patch.dict(os.environ, {"SETTINGS_BASE": common.BASE_DIR}):
            self.assertEqual(jobs.job_command("faces", {"library_id": "lib_00000004"},
                                              config), ("people.py", ["--library=-dash"]))


class FakeBuilt:
    """trips.Trips as trip_export.load gives it, for a trip whose last export saved
    options."""

    def __init__(self, saved):
        self.saved = saved

    def export_options(self, trip):
        return dict(self.saved)


class FakeTrip:
    id = "t-2019-07-01"
    title = "Lakes"


TRIP_SAVED = {"location": True, "zip": True, "home_km": 20}
ENDPOINT = "local gpu"

# (kind, library name or ORPHAN or None, params). Every kind of COMMAND_BUILDERS appears.
ORPHAN = "Old Pictures"
CASES = [
    ("scan", "Family", {}),
    ("scan", None, {"allow_mass_removal": True}),
    ("process", None, {}),
    ("process", "My Pictures 2019", {"retry_failed": True, "redescribe_stale": True,
                                     "limit": 7, "endpoint": ENDPOINT,
                                     "allow_mass_removal": True}),
    ("describe", "Family", {}),
    ("describe", None, {"retry_failed": True}),
    ("describe", "My Pictures 2019", {"ids": [3, 5], "force": True}),
    ("describe", None, {"ids": [8]}),
    ("describe", "Family", {"redescribe_stale": True, "endpoint": ENDPOINT, "limit": 0}),
    ("refine", "Family", {}),
    ("refine", None, {"retry_failed": True, "limit": 3}),
    ("embed", None, {}),
    ("embed", "Family", {"limit": 10}),
    ("image_embed", "Family", {}),
    ("image_embed", None, {"rebuild": True, "limit": 2}),
    ("previews", "Family", {}),
    ("previews", None, {}),
    ("proxies", "My Pictures 2019", {}),
    ("proxies", None, {}),
    ("drive_previews", "Family", {}),
    ("delete_library", ORPHAN, {"confirm_name": ORPHAN}),
    ("export", "Family", {}),
    ("export", "My Pictures 2019", {"out_dir": "WEB SITE", "options": {
        "title": "", "images": "original", "location": True, "estimated_places": True,
        "routes": False, "routes_trim": 300, "file_names": True, "folders": False,
        "exclude_types": ["document", "video"], "exclude_private": False,
        "exclude_sensitive": True, "exclude_undescribed": False,
        "include_ids": [[3, ["private", "writing"]], 5], "exclude_ids": [9, 11],
        "credits": "-- with thanks, (c) us", "zip": True, "allow_indexing": True,
        "videos": "include", "max_video_mb": 50, "raw_files": True, "similar": False,
        "meaning": False, "looks": False, "album": None, "marks": True,
        "people_names": True, "world_zoom": 6}}),
    ("export", "Family", {"options": {"album": "ALBUM", "exclude_types": [],
                                      "include_ids": [], "exclude_ids": [],
                                      "title": "Summer, at last"}}),
    ("export", None, {"trip": FakeTrip.id, "trip_title": FakeTrip.title}),
    ("export", None, {"trip": FakeTrip.id, "trip_title": FakeTrip.title,
                      "out_dir": "WEB SITE", "options": {"home_km": 0, "zip": False,
                                                         "location": False,
                                                         "people_names": True}}),
    ("export", None, {"trip": FakeTrip.id, "trip_title": FakeTrip.title,
                      "options": {"story": True}}),
    ("places", None, {}),
    ("places", None, {"download": True}),
    ("places", None, {"redo": True}),
    ("faces", "Family", {}),
    ("faces", None, {"retry_failed": True, "limit": 4}),
    ("group_faces", None, {}),
    ("delete_faces", None, {"confirm": True}),
    ("trip_stories", None, {}),
    ("trip_stories", None, {"trip": FakeTrip.id, "trip_title": FakeTrip.title,
                            "redo": True}),
    ("trip_stories", None, {"redo": True}),
    ("maps", None, {}),
    ("maps", None, {"download": True, "expect_bytes": 1234567,
                    "source": "https://build.example/20990101.pmtiles"}),
    ("maps", None, {"download": True, "expect_bytes": 99, "allow_large": True,
                    "source": "http://127.0.0.1:9/p.pmtiles"}),
    ("named_places", None, {}),
    ("named_places", None, {"redo": True}),
]

# What each kind of job hands its work on to: kind -> [(module, function name, value it
# returns)]. RUNNER: the job's own runner (the tool calls jobs.RUNNERS[kind] too).
RUNNER = "runner"
_SCAN_RESULT = {"libraries": [], "describe": None, "embed": None, "stopped": False}
_UPDATE_RESULT = {"configured": True, "error": "", "pending": 0, "stopped": False,
                  "up_to_date": 0}
_DESCRIBED = {"stopped": False}
_EXPORTED = {"count": 0, "output": "", "stopped": False, "failed": 0, "excluded": {}}
PROBES = {
    "scan": [(scan, "run", _SCAN_RESULT)],
    "process": [(scan, "run", _SCAN_RESULT)],
    "describe": [(describe, "describe_pending", _DESCRIBED), (embeddings, "after_run", None)],
    "refine": [(describe, "refine_pending", _DESCRIBED), (embeddings, "after_run", None)],
    "embed": [(embeddings, "update", _UPDATE_RESULT)],
    "image_embed": [(looks, "update", _UPDATE_RESULT)],
    "previews": [RUNNER],
    "proxies": [RUNNER],
    "drive_previews": [RUNNER],
    "delete_library": [RUNNER],
    "export": [(export, "export_library", _EXPORTED), (trip_export, "export_trip", _EXPORTED)],
    "places": [(places, "update", {}), (places, "build", {}),
               (places, "rebuild_if_needed", None),
               (places, "label_pictures", {"aborted": False, "checked": 0}),
               (gps_check, "run_safely", None), (local_times, "run_safely", None),
               (named_places, "run_safely", None)],
    "faces": [(people, "update", _UPDATE_RESULT)],
    "group_faces": [RUNNER],
    "delete_faces": [(people, "delete_all", {"error": "", "faces": 0, "pictures": 0,
                                             "people": 0, "named": 0, "removed": True})],
    "trip_stories": [(trip_stories, "write_stories", trip_stories._new_summary())],
    "maps": [(street_maps, "plan", {
        "source": "https://build.example/20990101.pmtiles", "build": "20990101.pmtiles",
        "version": "4.9.0", "mode": "whole_degrees", "max_zoom": 15, "radius_km": 20,
        "route_km": 5, "spots": 1, "route_points": 0, "tiles": 10, "keep_bytes": 1000,
        "download_bytes": 5000, "requests": 2, "index_bytes": 16384, "squares": 1,
        "large": False, "at": 0}),
        (street_maps, "save_plan", None),
        (street_maps, "download", {"stopped": False, "tiles": 10, "bytes": 1000,
                                   "downloaded": 5000, "build": "20990101.pmtiles"}),
        (named_places, "run_safely", None)],
    "named_places": [(named_places, "update", dict(named_places._empty_summary(), maps=True))],
}
# What a call is compared without: what only says how the work is done.
IGNORED = {"conn", "config", "ctx", "helper", "previews", "sleep", "servers", "now",
           "verbose", "index"}
# How each tool is run: tool -> main(argv) giving the exit code.
TOOLS = {"scan.py": scan.main, "export.py": export.main, "people.py": people.main,
         "make_places.py": make_places.main, "trips.py": trips.main,
         "street_maps.py": street_maps.main, "named_places.py": named_places.main}


class RoundTripTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.libraries = [
            support.library("Family", self.base.library_folder("family"), id="lib_aaaa0001"),
            support.library("My Pictures 2019", self.base.library_folder("pictures"),
                            id="lib_aaaa0002"),
            support.library("Off", self.base.library_folder("off"), id="lib_aaaa0003",
                            enabled=False)]
        # Not a real key or server: a made-up endpoint on this computer, never called.
        self.base.write_config(self.libraries, endpoints=[
            {"name": ENDPOINT, "kind": "openai", "base_url": "http://127.0.0.1:9/v1",
             "model": "fake-model", "api_key": ""}])
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO libraries (id, name, created) VALUES (?, ?, 0)",
                             ("lib_0rphan01", ORPHAN))
                self.album = albums.create_album(conn, "Best of")["id"]
        finally:
            conn.close()
        # Saved export options unlike the ones the jobs below choose: each switch counts.
        self.libraries[1]["export"] = {
            "title": "Saved", "images": "none", "routes": True, "routes_trim": 100,
            "folders": True, "exclude_types": ["screenshot"], "exclude_sensitive": False,
            "include_ids": [7], "exclude_ids": [8], "credits": "saved", "max_video_mb": 99,
            "album": self.album, "similar": True, "meaning": True, "looks": True}
        self.base.write_config(self.libraries, endpoints=[
            {"name": ENDPOINT, "kind": "openai", "base_url": "http://127.0.0.1:9/v1",
             "model": "fake-model", "api_key": ""}])
        self.config = common.load_config()
        # (The place index is there: the job and make_places.py look it up alike.)
        self.base.write_file(os.path.relpath(places.places_db_path(), self.base.path), b"")
        self.web_site = self.base.join("Web site")

    def library_id(self, name):
        if name == ORPHAN:
            return "lib_0rphan01"
        return None if name is None else common.get_library(self.config, name)["id"]

    def params_of(self, params):
        """The case's params with the test folder and album filled in."""
        params = json.loads(json.dumps(params))
        if params.get("out_dir") == "WEB SITE":
            params["out_dir"] = self.web_site + os.sep
        options = params.get("options") or {}
        if options.get("album") == "ALBUM":
            options["album"] = self.album
        return params

    @contextlib.contextmanager
    def recording(self, kind):
        """Replace what the jobs of this kind hand their work on to with recorders; yields
        the list the calls go to: (name, {argument: value})."""
        calls = []
        with contextlib.ExitStack() as stack:
            for probe in PROBES[kind]:
                if probe == RUNNER:
                    def runner(ctx, conn, config, params, helper=None):
                        calls.append(("runner", {"params": dict(params)}))
                        return {}
                    stack.enter_context(mock.patch.dict(jobs.RUNNERS, {kind: runner}))
                    continue
                module, name, value = probe
                signature = inspect.signature(getattr(module, name))

                def record(*args, _name=name, _signature=signature, _value=value, **kwargs):
                    bound = _signature.bind(*args, **kwargs)
                    bound.apply_defaults()
                    calls.append((_name, {key: item for key, item in bound.arguments.items()
                                          if key not in IGNORED}))
                    return json.loads(json.dumps(_value))
                stack.enter_context(mock.patch.object(module, name, record))
            # What the jobs check first: set up.
            stack.enter_context(mock.patch.object(embeddings, "configured", lambda config: True))
            stack.enter_context(mock.patch.object(looks, "configured", lambda config: True))
            stack.enter_context(mock.patch.object(people, "setup_problem", lambda options: ""))
            stack.enter_context(mock.patch.object(people, "enabled", lambda config: True))
            stack.enter_context(mock.patch.object(places, "have_files", lambda *args: True))
            stack.enter_context(mock.patch.object(places, "shared", lambda: object()))
            stack.enter_context(mock.patch.object(trip_export, "load", lambda conn, config,
                                                  trip_id: (FakeBuilt(TRIP_SAVED), FakeTrip(),
                                                            [])))
            yield calls

    def normalized(self, calls):
        """The calls with what may differ but means the same made alike: every enabled
        library for None, a folder as a full path, export options as the export uses
        them."""
        enabled = sorted(library["id"] for library in common.enabled_libraries(self.config))
        result = []
        for name, arguments in calls:
            arguments = dict(arguments)
            if "library_ids" in arguments:
                arguments["library_ids"] = sorted(arguments["library_ids"] or enabled)
            if arguments.get("out_dir"):
                arguments["out_dir"] = os.path.normcase(os.path.abspath(arguments["out_dir"]))
            if name == "export_library":
                library = common.get_library(self.config, arguments["library_id"])
                arguments["options"] = export.library_options(library,
                                                              arguments["options"] or None)
            if name == "export_trip":
                arguments["options"] = trip_export.trip_options(TRIP_SAVED,
                                                                arguments["options"] or None)
            result.append((name, arguments))
        return result

    def run_job(self, kind, params):
        """The job's runner, as the job manager runs it."""
        ctx = jobs.JobContext(kind)
        conn = db.connect()
        try:
            jobs.RUNNERS[kind](ctx, conn, self.config, dict(params), helper=None)
        finally:
            conn.close()

    def run_tool(self, tool, argv):
        """The tool's main(argv); returns (exit code, what it printed)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                if tool == "people.py":
                    code = TOOLS[tool](argv, out=out)
                else:
                    code = TOOLS[tool](argv)
            except SystemExit as stop:
                code = stop.code
        if isinstance(code, dict):
            code = 0        # (export.py returns the summary)
        return code, out.getvalue()

    def test_every_command_line_does_what_its_job_does(self):
        for kind, library, params in CASES:
            params = self.params_of(params)
            job_params = dict(params, library_id=self.library_id(library))
            with self.subTest(kind=kind, library=library, params=params):
                with self.recording(kind) as calls:
                    self.run_job(kind, job_params)
                    job_calls = list(calls)
                    del calls[:]
                    tool, args = jobs.job_command(kind, job_params, self.config)
                    line = jobs.command_text(tool, args)
                    message = jobs.command_message(kind, job_params, self.config)
                    self.assertEqual(message, jobs.COMMAND_PREFIX + line)
                    # As cmd or PowerShell hands it to Python.
                    split = windows_split(line)
                    self.assertEqual(split[:2], ["python", tool])
                    self.assertEqual(split[2:], args)
                    code, printed = self.run_tool(tool, split[2:])
                    cli_calls = list(calls)
                self.assertEqual(code, 0, printed)
                self.assertTrue(job_calls, "the job did nothing")
                self.assertEqual(self.normalized(cli_calls), self.normalized(job_calls),
                                 "%s\n%s" % (line, printed))
                # The tool logs the same first line as the job; the line names the
                # settings folder (not the program folder here).
                lines = [text.split("] ", 1)[-1] for text in printed.splitlines()]
                self.assertIn(message, lines, printed)
                self.assertEqual(split[-2:], [jobs.SETTINGS_BASE_SWITCH, self.base.path])
                self.assertEqual(jobs.settings_base_note(), "")

    def test_the_commands_name_libraries_and_switches_as_expected(self):
        expected = {
            ("scan", "Family"): "python scan.py --no-describe --library Family",
            ("process", "My Pictures 2019"):
                'python scan.py --library "My Pictures 2019" --retry-failed '
                '--redescribe-stale --limit 7 --endpoint "local gpu" --allow-mass-removal',
            ("describe", "My Pictures 2019"):
                'python scan.py --no-scan --library "My Pictures 2019" --ids "3,5" --force',
            ("delete_library", ORPHAN):
                'python scan.py --delete-library-data lib_0rphan01 --confirm-name '
                '"Old Pictures"',
            ("group_faces", None): "python people.py --group",
            ("delete_faces", None): "python people.py --delete-all --yes",
            ("trip_stories", None): "python trips.py --stories",
        }
        seen = set()
        base = " --settings-base " + self.base.path
        for kind, library, params in CASES:
            key = (kind, library)
            if key in expected and key not in seen:
                seen.add(key)
                job_params = dict(self.params_of(params), library_id=self.library_id(library))
                self.assertEqual(jobs.command_text(*jobs.job_command(kind, job_params,
                                                                     self.config)),
                                 expected[key] + base)
        self.assertEqual(seen, set(expected))
        params = [params for kind, library, params in CASES
                  if kind == "export" and params.get("out_dir") and not params.get("trip")][0]
        line = jobs.job_command_line("export", dict(self.params_of(params),
                                                    library_id="lib_aaaa0002"), self.config)
        self.assertEqual(line, 'python export.py --library "My Pictures 2019" --out "'
                         + self.web_site + '" --title= --images original --location '
                         '--estimated-places --no-routes --routes-trim 300 --world-zoom 6 '
                         '--file-names '
                         '--no-folders --exclude-types "document,video" --include-private '
                         '--no-include-sensitive --include-undescribed --include-ids '
                         '"3:private+writing,5" --exclude-ids "9,11" '
                         '"--credits=-- with thanks, (c) us" --zip --allow-indexing --videos '
                         'include --max-video-mb 50 --raw-files --no-similar --no-meaning '
                         '--no-looks --no-album --marks --people-names' + base)

    def test_a_tool_says_where_the_settings_are(self):
        self.assertEqual(jobs.settings_base_tokens(), ["--settings-base", self.base.path])
        self.assertEqual(jobs.settings_base_note(), "")
        with mock.patch.dict(os.environ, {"SETTINGS_BASE": common.BASE_DIR}):
            self.assertEqual(jobs.settings_base_tokens(), [])
            self.assertEqual(jobs.settings_base_note(), "")
            self.assertEqual(jobs.job_command_line("group_faces", {}, {}),
                             "python people.py --group")
        # A folder that cannot be typed alike in PowerShell and cmd: the note says that
        # the command needs SETTINGS_BASE.
        odd = self.base.make_dir("100% data")
        with mock.patch.dict(os.environ, {"SETTINGS_BASE": odd}):
            self.assertEqual(jobs.settings_base_tokens(), [])
            note = jobs.settings_base_note()
            self.assertIn(odd, note)
            self.assertIn("SETTINGS_BASE", note)
            self.assertEqual(jobs.job_command_line("group_faces", {}, {}),
                             "python people.py --group")


class ToolSwitchTest(support.TempBaseMixin, unittest.TestCase):
    """The switches the tools got for the jobs: what goes together, and real runs."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", self.folder, id="lib_aaaa0001")])

    def run_tool(self, main, argv, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = main(argv, **kwargs)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def test_switches_that_do_not_go_together(self):
        for argv in (["--previews", "--no-scan"], ["--previews", "--proxies"],
                     ["--drive-previews", "--limit", "3"], ["--delete-library-data", "lib_x"],
                     ["--delete-library-data", "lib_x", "--confirm-name", "X", "--library",
                      "Family"],
                     ["--confirm-name", "X"], ["--ids", "1,2"], ["--no-scan", "--force"],
                     ["--refine", "--no-scan", "--ids", "4"]):
            with self.subTest(argv=argv):
                code, printed = self.run_tool(scan.main, argv)
                self.assertEqual(code, 2, printed)
                self.assertNotIn(jobs.COMMAND_PREFIX, printed)
        code, printed = self.run_tool(scan.main, ["--no-scan", "--ids", "1,x"])
        self.assertEqual(code, 2)
        for argv in (["--group", "--library", "Family"], ["--group", "--status"],
                     ["--group", "--retry-failed"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                self.assertEqual(people.main(argv, out=out), 2, out.getvalue())
        code, _printed = self.run_tool(export.main, ["--library", "Family", "--album", "A",
                                                     "--no-album"])
        self.assertEqual(code, 2)
        code, _printed = self.run_tool(export.main, ["--trip", "t1", "--no-album"])
        self.assertEqual(code, 2)
        code, _printed = self.run_tool(export.main, ["--library", "Family", "--exclude-types",
                                                     "photo,nonsense"])
        self.assertEqual(code, 2)

    def test_real_runs_of_the_new_switches(self):
        db.init(log=support.quiet)
        code, printed = self.run_tool(scan.main, ["--previews", "--library", "Family"])
        self.assertEqual(code, 0, printed)
        self.assertIn("] Command line: python scan.py --previews --library Family", printed)
        self.assertIn("Done.", printed)
        # A library in the settings cannot be deleted from here, nor one named wrongly.
        code, printed = self.run_tool(scan.main, ["--delete-library-data", "lib_aaaa0001",
                                                  "--confirm-name", "Family"])
        self.assertEqual(code, 1, printed)
        self.assertIn("remove it there first", printed)
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO libraries (id, name, created) VALUES (?, ?, 0)",
                             ("lib_0rphan01", "Old Pictures"))
        finally:
            conn.close()
        code, printed = self.run_tool(scan.main, ["--delete-library-data", "lib_0rphan01",
                                                  "--confirm-name", "Wrong"])
        self.assertEqual(code, 1, printed)
        self.assertIn("does not match", printed)
        code, printed = self.run_tool(scan.main, ["--delete-library-data", "lib_0rphan01",
                                                  "--confirm-name", "old pictures"])
        self.assertEqual(code, 0, printed)
        self.assertIn("Deleted 0 image(s) of Old Pictures", printed)
        # People is off: grouping says so.
        out = io.StringIO()
        self.assertEqual(people.main(["--group"], out=out), 1)
        self.assertIn(people.NOT_ENABLED, out.getvalue())
        self.assertIn("Command line: python people.py --group", out.getvalue())


class JobLogTest(support.TempBaseMixin, unittest.TestCase):
    """The job manager logs the command line as each job's first line."""

    def test_the_first_lines_of_a_job_and_its_snapshot(self):
        folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", folder, id="lib_aaaa0001")])
        db.init(log=support.quiet)
        ran = []

        def runner(ctx, conn, config, params, helper=None):
            ctx.log("working")
            ran.append(params)
            return {}
        manager = jobs.JobManager(runners={"scan": runner, "odd": runner},
                                  poll_interval=0.01, idle_interval=0.01)
        manager.start()
        self.addCleanup(manager.stop)
        job, _added = manager.enqueue("scan", "lib_aaaa0001", {"allow_mass_removal": True})
        odd, _added = manager.enqueue("odd", None, {})
        self.assertTrue(manager.wait_idle(20))
        log = [line for line in manager.buffer.since(0)[0] if line["job"] == job["id"]]
        wanted = ("python scan.py --no-describe --library Family --allow-mass-removal "
                  "--settings-base " + self.base.path)
        self.assertEqual(log[0]["message"], "Command line: " + wanted)
        self.assertTrue(log[1]["message"].startswith("Started job"))
        with open(os.path.join(common.logs_dir(), "job-%d.log" % job["id"]),
                  encoding="utf-8") as handle:
            first = handle.readline()
        self.assertTrue(first.rstrip("\n").endswith("] Command line: " + wanted), first)
        odd_log = [line for line in manager.buffer.since(0)[0] if line["job"] == odd["id"]]
        self.assertEqual(odd_log[0]["message"], "%s: no tool runs a odd job." % jobs.NO_COMMAND)
        config = common.load_config()
        snapshot = manager.snapshot(0, {}, config=config)
        commands = {item["id"]: item["command"] for item in snapshot["recent"]}
        self.assertEqual(commands[job["id"]], wanted)
        self.assertEqual(commands[odd["id"]], "")
        self.assertNotIn("command", manager.snapshot(0, {})["recent"][0])

    def test_the_library_cards_leave_out_jobs_of_no_library(self):
        folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", folder, id="lib_aaaa0001")])
        db.init(log=support.quiet)

        def runner(ctx, conn, config, params, helper=None):
            return {}
        manager = jobs.JobManager(runners={"scan": runner, "export": runner,
                                           "trip_stories": runner},
                                  poll_interval=0.01, idle_interval=0.01)
        manager.start()
        self.addCleanup(manager.stop)
        scan_job, _added = manager.enqueue("scan", None, {})
        manager.enqueue("export", None, {"trip": "t1", "trip_title": "Lake trip"})
        manager.enqueue("trip_stories", None, {"trip": "t1", "trip_title": "Lake trip"})
        self.assertTrue(manager.wait_idle(20))
        latest = manager.latest_by_library()
        # (A scan of every library is the cards' last job; the trip's jobs are no
        # library's.)
        self.assertEqual(latest[None]["id"], scan_job["id"])
        self.assertEqual(set(latest), {None})


class SettingsBaseTest(support.TempBaseMixin, unittest.TestCase):
    """The settings folder on the command line: a copied command runs as it is, from
    anywhere, and one run without its settings folder does not look like one that
    worked."""

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("family")
        self.base.write_config([support.library("Family", self.folder, id="lib_aaaa0001")])
        db.init(log=support.quiet)

    def run_tool(self, main, argv, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = main(argv, **kwargs)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def test_a_copied_command_runs_without_settings_base(self):
        manager = jobs.JobManager(runners={"scan": lambda *args, **kwargs: {}},
                                  poll_interval=0.01, idle_interval=0.01)
        manager.start()
        self.addCleanup(manager.stop)
        job, _added = manager.enqueue("scan", "lib_aaaa0001", {})
        self.assertTrue(manager.wait_idle(20))
        command = [item["command"] for item in manager.snapshot(0, {}, config=common.load_config())
                   ["recent"] if item["id"] == job["id"]][0]
        args = windows_split(command)
        self.assertEqual(args[-2:], ["--settings-base", self.base.path])
        # Run as it is (but with this Python) in another folder, without SETTINGS_BASE: it
        # works on this settings folder, and says so first.
        env = support.child_env()
        env.pop("SETTINGS_BASE", None)
        elsewhere = self.base.make_dir("elsewhere")
        result = subprocess.run([sys.executable, os.path.join(support.REPO, args[1])]
                                + args[2:], cwd=elsewhere, env=env, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("] Command line: " + command, result.stdout)
        self.assertIn("Done.", result.stdout)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_the_tools_log_one_form_of_it(self):
        for argv in (["--settings", self.base.path, "--no-describe"],
                     ["--settings-base=" + self.base.path, "--no-describe"],
                     ["--no-describe"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                jobs.log_tool_command(jobs.ConsoleContext("scan", stream=out), "scan.py", argv)
                self.assertTrue(out.getvalue().rstrip().endswith(
                    "] Command line: python scan.py --no-describe --settings-base "
                    + self.base.path), out.getvalue())
        code, printed = self.run_tool(scan.main, ["--settings-base", self.base.path,
                                                  "--previews", "--library", "Family"])
        self.assertEqual(code, 0, printed)
        self.assertIn("] Command line: python scan.py --previews --library Family "
                      "--settings-base " + self.base.path, printed)

    def test_a_run_without_its_settings_folder_fails(self):
        empty = self.base.make_dir("empty")
        # A folder without settings given: refused.
        for main in (scan.main, export.main, people.main, make_places.main, trips.main):
            with self.subTest(tool=main.__module__):
                code, printed = self.run_tool(main, ["--settings-base", empty])
                self.assertEqual(code, 2, printed)
                self.assertIn("there is no config.json there", printed)
        with mock.patch.dict(os.environ, {"SETTINGS_BASE": empty}):
            # (No libraries: the command line first, then exit status 2.)
            code, printed = self.run_tool(scan.main, ["--no-describe", "--library", "Family"])
            self.assertEqual(code, 2, printed)
            self.assertIn("] Command line: python scan.py --no-describe --library Family "
                          "--settings-base " + empty, printed)
            self.assertIn("There are no settings in %s" % empty, printed)
            code, printed = self.run_tool(trips.main, ["--stories"])
            self.assertEqual(code, 2, printed)
            self.assertIn("There are no settings in", printed)
            code, printed = self.run_tool(make_places.main, [])
            self.assertEqual(code, 2, printed)
            self.assertEqual(os.listdir(empty), [])
        # Settings without libraries: exit status 2 too.
        self.base.write_config([])
        code, printed = self.run_tool(scan.main, ["--no-describe"])
        self.assertEqual(code, 2, printed)
        self.assertIn("No libraries configured", printed)


class PlacesCommandTest(support.TempBaseMixin, unittest.TestCase):
    """make_places.py does what the places job does: nothing is downloaded unless asked
    for, and the job lock is taken first."""

    def setUp(self):
        super().setUp()
        self.base.write_config([support.library("Family", self.base.library_folder("family"),
                                                id="lib_aaaa0001")])
        db.init(log=support.quiet)
        self.calls = []
        for name in ("update", "build", "rebuild_if_needed"):
            patcher = mock.patch.object(places, name, self.recorder(name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def recorder(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return None if name == "rebuild_if_needed" else {}
        return record

    def run_tool(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = make_places.main(argv)
        return code, out.getvalue()

    def test_a_place_index_without_its_files_is_used_as_it_is(self):
        # (Built from files elsewhere: --from DIR.) The job and the command alike only
        # check it; neither downloads.
        self.base.write_file(os.path.relpath(places.places_db_path(), self.base.path), b"")
        self.assertFalse(places.have_files())
        with mock.patch.object(places, "label_pictures", lambda *args, **kwargs: {
                "aborted": False, "checked": 0}), \
                mock.patch.object(places, "shared", lambda: object()):
            ctx = jobs.JobContext("places")
            conn = db.connect()
            try:
                jobs.RUNNERS["places"](ctx, conn, common.load_config(), {"library_id": None})
            finally:
                conn.close()
            job_calls = list(self.calls)
            del self.calls[:]
            code, printed = self.run_tool([])
        self.assertEqual(code, 0, printed)
        # (The local times look at the index again: local_times.run_safely.)
        self.assertEqual(job_calls[0], "rebuild_if_needed")
        self.assertNotIn("update", job_calls)
        self.assertEqual(self.calls, job_calls)

    def test_no_place_names_and_no_files(self):
        code, printed = self.run_tool([])
        self.assertEqual(code, 1, printed)
        self.assertIn("download them with python make_places.py --download", printed)
        self.assertEqual(self.calls, [])
        # The files are there: the index is built from them (nothing downloaded).
        with mock.patch.object(places, "have_files", lambda *args: True), \
                mock.patch.object(places, "label_pictures", lambda *args, **kwargs: {
                    "aborted": False, "checked": 0}):
            code, printed = self.run_tool([])
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.calls[0], "build")
        self.assertNotIn("update", self.calls)

    def test_the_job_lock_comes_first(self):
        lock = jobs.WorkerLock()
        self.assertTrue(lock.acquire("scan job 7"))
        try:
            for argv in ([], ["--download"], ["--redo"]):
                with self.subTest(argv=argv):
                    code, printed = self.run_tool(argv)
                    self.assertEqual(code, 1, printed)
                    self.assertIn("Another job is running: scan job 7", printed)
                    self.assertEqual(self.calls, [])
        finally:
            lock.release()


class ExportSwitchTest(unittest.TestCase):

    def test_every_option_without_a_switch_is_named(self):
        # Options without a switch among the others (as a new option of a later version
        # would be): those are named, not the options after them.
        defaults = dict([("lake_view", False)] + list(export.DEFAULT_OPTIONS.items()))
        with mock.patch.object(export, "DEFAULT_OPTIONS", defaults):
            options = dict(export.default_options(), lake_km=3, lake_view=True)
            with self.assertRaises(ValueError) as raised:
                export.options_arguments(options)
        self.assertEqual(str(raised.exception), "no switch of export.py gives the export "
                                                "option lake_km, lake_view")


if __name__ == "__main__":
    unittest.main()
