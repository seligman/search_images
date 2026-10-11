"""Tests for library_ops.py: library rules, folder helpers and background checks."""

import os
import threading
import time
import unittest
from unittest import mock

import support

import common
import library_ops

BS = chr(92)    # a backslash, for Windows paths built in code


class PathRulesTest(support.TempBaseMixin, unittest.TestCase):

    def test_path_errors(self):
        good = self.base.library_folder("Family")
        self.assertIsNone(library_ops.path_error(good))
        self.assertIsNone(library_ops.path_error("  " + good + "  "))
        self.assertIn("Choose", library_ops.path_error(""))
        self.assertIn("full path", library_ops.path_error("Photos"))
        self.assertIsNone(library_ops.path_error("Photos", require_absolute=False))
        self.assertIn("data folder", library_ops.path_error(os.path.join(common.data_dir(), "x")))
        self.assertIn("program folder", library_ops.path_error(support.REPO))
        self.assertIn("program folder",
                      library_ops.path_error(os.path.join(support.REPO, "sample", "images")))

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_windows_network_and_drive_paths(self):
        server_only = BS * 2 + "server"
        share = BS * 2 + "server" + BS + "share"
        self.assertTrue(library_ops.unc_without_share(server_only))
        self.assertFalse(library_ops.unc_without_share(share))
        self.assertFalse(library_ops.unc_without_share(BS * 2 + "?" + BS + "C:" + BS + "x"))
        self.assertIn("share", library_ops.path_error(server_only))
        self.assertTrue(library_ops.is_full_path(share))
        self.assertTrue(library_ops.is_full_path(share + BS + "Photos"))
        self.assertTrue(library_ops.is_full_path("C:" + BS + "Photos"))
        self.assertFalse(library_ops.is_full_path("C:Photos"))
        self.assertFalse(library_ops.is_full_path(BS + "Photos"))
        self.assertEqual(library_ops.expand_path("C:"), "C:" + BS)
        self.assertEqual(library_ops.parent_folder("C:" + BS), "")
        self.assertEqual(library_ops.parent_folder(share), "")
        self.assertEqual(library_ops.parent_folder(share + BS + "Photos"), share + BS)
        self.assertEqual(library_ops.parent_folder("C:" + BS + "Photos" + BS + "Family"),
                         "C:" + BS + "Photos")

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_other_spellings_of_the_data_folder_are_refused(self):
        data = common.data_dir()
        drive, rest = os.path.splitdrive(data)
        if not drive.endswith(":"):
            self.skipTest("the data folder is not on a drive letter")
        # Device paths name the same folders in a way the rules cannot compare.
        for prefix in ("?", "."):
            device = BS * 2 + prefix + BS + data
            self.assertTrue(library_ops.is_device_path(device))
            self.assertIn("device prefix", library_ops.path_error(device))
            with self.assertRaises(library_ops.LibraryError):
                library_ops.list_folder(device)
            self.assertIn("device prefix", library_ops.check_path(device)["problem"])
        self.assertFalse(library_ops.is_device_path(BS * 2 + "server" + BS + "share"))
        self.assertFalse(library_ops.is_device_path("C:" + BS + "Photos"))
        # The administrative share of this computer is the drive itself.
        for host in ("localhost", "127.0.0.1", "LOCALHOST"):
            share = BS * 2 + host + BS + drive[0] + "$" + rest
            self.assertIn("data folder", library_ops.path_error(share), host)
            self.assertTrue(library_ops.is_within(share, data))
        self.assertFalse(library_ops.is_within(BS * 2 + "server" + BS + drive[0] + "$" + rest,
                                               data))

    def test_export_settings_are_checked(self):
        family = self.base.library_folder("Family")
        config = {"libraries": [support.library("Family", family, id="lib_family1")]}
        library = config["libraries"][0]
        # Replaced as a whole; only what differs from the defaults is kept.
        for value, stored in (({}, {}), (None, {}), ({"images": "web", "zip": False}, {}),
                              ({"images": "original", "title": "  My   site "},
                               {"images": "original", "title": "My site"}),
                              ({"exclude_types": ["screenshot"], "include_ids": [3, 2, 3]},
                               {"exclude_types": ["screenshot"], "include_ids": [2, 3]})):
            changes, _warnings = library_ops.clean_library_fields(config, {"export": value},
                                                                  library)
            self.assertEqual(changes, {"export": stored}, value)
        for value in ({"output": family}, [], "x", {"images": "huge"}, {"zip": "yes"},
                      {"exclude_types": ["selfie"]}, {"include_ids": ["1"]}):
            with self.assertRaises(library_ops.LibraryError) as caught:
                library_ops.clean_library_fields(config, {"export": value}, library)
            self.assertIn("export", caught.exception.fields)

    def test_overlap_warnings(self):
        family = self.base.library_folder("Family")
        config = {"libraries": [support.library("Family", family, id="lib_family1")]}
        inner = os.path.join(family, "2019")
        self.assertTrue(library_ops.path_warnings(config, inner))
        self.assertEqual(library_ops.path_warnings(config, inner, skip_id="lib_family1"), [])
        self.assertEqual(library_ops.path_warnings(config, self.base.library_folder("Trips")), [])
        # The data folder inside a library folder would get its previews indexed.
        warnings = library_ops.path_warnings(config, self.base.path)
        self.assertTrue(any("data folder" in warning for warning in warnings))

    def test_names_and_fields(self):
        config = {"libraries": [{"id": "lib_a", "name": "Family", "path": "x"}]}
        self.assertIsNotNone(library_ops.library_name_error(config, " family "))
        self.assertIsNone(library_ops.library_name_error(config, "FAMILY", skip="lib_a"))
        self.assertIsNone(library_ops.library_name_error(config, "Trips"))
        self.assertIsNotNone(library_ops.library_name_error(config, "  "))
        self.assertEqual(library_ops.parse_ignore("a/*\n\n b\\c \n"), ["a/*", "b/c"])
        self.assertIsNone(library_ops.parse_ignore([1, 2]))
        folder = self.base.library_folder("2014-10 Trips")
        changes, warnings = library_ops.clean_library_fields(config, {"path": folder})
        self.assertEqual(changes, {"path": folder, "name": "Trips"})
        self.assertEqual(warnings, [])
        with self.assertRaises(library_ops.LibraryError) as caught:
            library_ops.clean_library_fields(config, {"path": folder, "name": "family",
                                                      "previews": 1})
        self.assertEqual(set(caught.exception.fields), {"name", "previews"})


class FolderHelpersTest(support.TempBaseMixin, unittest.TestCase):

    def test_folder_walk_skips_links_and_stops_at_the_deadline(self):
        root = self.base.library_folder("Family")
        for rel in ("a.jpg", "sub/b.jpg", "@eaDir/c.jpg", "sub/notes.txt"):
            self.base.write_file("libraries/Family/" + rel, b"x")
        outside = self.base.library_folder("Outside")
        self.base.write_file("libraries/Outside/d.jpg", b"x")
        try:
            os.symlink(outside, os.path.join(root, "link"), target_is_directory=True)
        except (OSError, NotImplementedError):
            pass        # symlinks need extra rights on Windows
        walk = library_ops.FolderWalk(root, common.DEFAULT_IGNORES)
        self.assertEqual(sorted(rel for rel, _pruned in walk), ["a.jpg", "sub/b.jpg"])
        walk = library_ops.FolderWalk(root, common.DEFAULT_IGNORES, into_pruned=True)
        self.assertIn(("@eaDir/c.jpg", "@eaDir"), list(walk))
        walk = library_ops.FolderWalk(root, deadline=time.monotonic() - 1)
        self.assertEqual(list(walk), [])
        self.assertTrue(walk.truncated)
        walk = library_ops.FolderWalk(self.base.join("nothing"))
        self.assertEqual(list(walk), [])
        self.assertFalse(walk.readable)

    def test_folder_status(self):
        self.assertTrue(library_ops.folder_status(self.base.library_folder("Family"))
                        ["reachable"])
        status = library_ops.folder_status(self.base.join("nothing"))
        self.assertFalse(status["reachable"])
        # A folder that is not there on a drive that is: moved or renamed, not
        # offline (Windows can tell; elsewhere a share is just a folder).
        self.assertIn("does not exist (moved or renamed?)" if os.name == "nt"
                      else "cannot be reached (offline?)", status["message"])
        # A drive (or share) that does not answer: that may well be offline.
        with mock.patch.object(library_ops.os.path, "isdir", return_value=False):
            self.assertIn("cannot be reached (offline?)",
                          library_ops.missing_folder_message(self.base.join("nothing")))
        self.assertFalse(library_ops.folder_status("")["reachable"])

    def test_run_with_timeout(self):
        self.assertEqual(library_ops.run_with_timeout(lambda value: value * 2, 5, 21), 42)
        with self.assertRaises(ZeroDivisionError):
            library_ops.run_with_timeout(lambda: 1 / 0, 5)
        release = threading.Event()
        started = time.monotonic()
        with self.assertRaises(library_ops.TimedOut):
            library_ops.run_with_timeout(release.wait, 0.1, 5)
        self.assertLess(time.monotonic() - started, 2)
        release.set()


class BackgroundChecksTest(unittest.TestCase):

    def test_results_are_remembered_and_refreshed(self):
        calls = []

        def check(value):
            calls.append(value)
            return {"value": value, "call": len(calls)}

        checks = library_ops.BackgroundChecks(ttl=60, timeout=5)
        first = checks.get("key", check, 1)
        if first is None:       # still running in the background
            first = _wait(lambda: checks.get("key", check, 1))
        self.assertEqual(first["call"], 1)
        self.assertEqual(checks.get("key", check, 1)["call"], 1)    # remembered
        checks.forget("key")
        self.assertEqual(_wait(lambda: (checks.get("key", check, 1) or {}).get("call") == 2
                               and checks.get("key", check, 1)), {"value": 1, "call": 2})

    def test_a_hanging_check_reports_a_timeout_without_blocking(self):
        release = threading.Event()
        self.addCleanup(release.set)
        checks = library_ops.BackgroundChecks(ttl=60, timeout=0.1)
        started = time.monotonic()
        self.assertIsNone(checks.get("share", release.wait, 30, timed_out="slow"))
        self.assertLess(time.monotonic() - started, 1)
        time.sleep(0.2)
        self.assertEqual(checks.get("share", release.wait, 30, timed_out="slow"), "slow")

    def test_a_share_that_stops_answering_is_not_shown_as_online(self):
        hang = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def check():
            if hang.is_set():
                release.wait(30)
                return {"reachable": False}
            return {"reachable": True}
        checks = library_ops.BackgroundChecks(ttl=0.05, timeout=0.2)
        self.assertEqual(_wait(lambda: checks.get("share", check, timed_out="slow")),
                         {"reachable": True})
        hang.set()
        time.sleep(0.1)
        # The refresh hangs: the old answer stands only until the timeout.
        self.assertEqual(checks.get("share", check, timed_out="slow"), {"reachable": True})
        time.sleep(0.3)
        self.assertEqual(checks.get("share", check, timed_out="slow"), "slow")
        release.set()
        self.assertEqual(_wait(lambda: checks.get("share", check, timed_out="slow") ==
                               {"reachable": False}), True)

    def test_waiting_for_a_quick_first_result(self):
        checks = library_ops.BackgroundChecks(ttl=60, timeout=5)
        self.assertEqual(checks.get("key", lambda: "fast", wait=2), "fast")
        release = threading.Event()
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertIsNone(checks.get("slow", release.wait, 30, wait=0.1))
        self.assertLess(time.monotonic() - started, 1)

    def test_forget_drops_answers_of_checks_already_running(self):
        release = threading.Event()
        self.addCleanup(release.set)
        values = iter(["old", "new"])

        def check():
            value = next(values)
            if value == "old":
                release.wait(30)
            return value
        checks = library_ops.BackgroundChecks(ttl=60, timeout=5)
        self.assertIsNone(checks.get("key", check))
        checks.forget("key")
        self.assertEqual(checks.get("key", check, wait=2), "new")
        release.set()
        time.sleep(0.1)
        self.assertEqual(checks.get("key", check), "new")


def _wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    return condition()


if __name__ == "__main__":
    unittest.main()
