#!/usr/bin/env python3
"""Guard against personal data leaking into the public repository.

Every file that git would commit (tracked files plus untracked files that are
not ignored) is checked for things that usually point at one person's
machine: absolute drive or UNC paths that are not documentation
placeholders, user-profile paths, non-loopback IP addresses, and model file
names.

You can also keep a private list of words that must never appear in the repo
(a NAS name, a share, library names, ...). Put one term per line in either of
these files, which are never committed:

    <SETTINGS_BASE>/private-terms.txt
    .git/info/private-terms

Run with:  python -m unittest discover -s tests
"""

import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This file holds deliberately leaky examples for its own self-test.
SELF = "tests/test_repo_hygiene.py"

BINARY_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".avif",
    ".tif", ".tiff", ".bmp", ".ico", ".dat", ".db", ".zip", ".gz", ".pyc",
    ".gguf", ".woff", ".woff2",
)

# Placeholders that documentation may use freely.
ALLOWED_DRIVE_PREFIXES = (
    "c:\\photos", "d:\\photos", "c:\\path\\to", "c:\\path", "d:\\path",
    "x:\\", "c:\\example",
)
ALLOWED_USERS = {
    "you", "yourname", "username", "user", "me", "name", "<user>",
    "%username%", "example", "public", "default",
}
ALLOWED_UNC_HOSTS = {
    "server", "nas", "host", "host-a", "host-b", "example", "localhost",
    "computer", "machine",
}
ALLOWED_IPS = ("127.", "0.0.0.0", "255.255.255.", "192.0.2.", "198.51.100.",
               "203.0.113.")
ALLOWED_GGUF = {
    "model.gguf", "your-model.gguf", "model.mmproj.gguf", "mmproj.gguf",
    "your-model.mmproj.gguf", "mmproj-model.gguf",
}

DRIVE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]:\\[^\s\"'<>|*?`)\]]*)")
PROFILE_RE = re.compile(
    r"(?i)(?:[A-Za-z]:)?[\\/](?:users|home|documents and settings)[\\/]([^\\/\s\"'`]+)")
# A UNC path starts with two backslashes that do not follow a drive colon,
# another backslash, or a word character (so "C:\\Photos" in JSON is not one).
UNC_RE = re.compile(r"(?<![\\:\w])\\\\([A-Za-z0-9_.$-]+)\\")
IP_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
GGUF_RE = re.compile(r"([A-Za-z0-9_.+-]+\.gguf)\b", re.IGNORECASE)
# Strings shaped like real API keys (OpenAI, Anthropic, OpenRouter, Gemini).
KEY_RE = re.compile(r"(?<![A-Za-z0-9])(sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,})")
# Made-up keys in tests and docs carry one of these.
ALLOWED_KEY_MARKERS = ("test", "fake", "example", "abcdef", "0123456789", "xxxx",
                       "your", "pasted")


def candidate_files():
    """Files git would commit: tracked plus untracked-but-not-ignored."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    return sorted(set(names))


def private_terms():
    paths = [os.path.join(REPO, ".git", "info", "private-terms")]
    base = os.environ.get("SETTINGS_BASE")
    if base:
        paths.append(os.path.join(base, "private-terms.txt"))
    terms = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line.lower())
    return terms


def text_variants(line):
    """The raw line plus one with escaped backslashes collapsed (JSON/code)."""
    collapsed = line.replace("\\\\", "\\")
    return [line] if collapsed == line else [line, collapsed]


def scan_line(line):
    """Return a list of human-readable problems found in one line."""
    problems = []
    for text in text_variants(line):
        for match in DRIVE_RE.finditer(text):
            path = re.sub(r"\\+", r"\\", match.group(1).lower())
            if not path.startswith(ALLOWED_DRIVE_PREFIXES):
                problems.append("absolute path " + match.group(1))
        for match in PROFILE_RE.finditer(text):
            if match.group(1).lower() not in ALLOWED_USERS:
                problems.append("user profile path " + match.group(0))
        for match in UNC_RE.finditer(text):
            if match.group(1).lower() not in ALLOWED_UNC_HOSTS:
                problems.append("UNC path \\\\" + match.group(1))
    for match in IP_RE.finditer(line):
        address = match.group(1)
        parts = address.split(".")
        if all(0 <= int(p) <= 255 for p in parts) and not address.startswith(ALLOWED_IPS):
            problems.append("IP address " + address)
    for match in GGUF_RE.finditer(line):
        if match.group(1).lower() not in ALLOWED_GGUF:
            problems.append("model file name " + match.group(1))
    for match in KEY_RE.finditer(line):
        key = match.group(1)
        if not any(marker in key.lower() for marker in ALLOWED_KEY_MARKERS):
            # Never print the key itself.
            problems.append("something shaped like an API key (%s...)" % key[:6])
    return sorted(set(problems))


class RepoHygieneTest(unittest.TestCase):

    def setUp(self):
        self.files = candidate_files()
        if self.files is None:
            self.skipTest("git is not available")

    def iter_text_files(self):
        for name in self.files:
            if name == SELF or name.lower().endswith(BINARY_EXTENSIONS):
                continue
            path = os.path.join(REPO, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                data = handle.read()
            if b"\0" in data[:4096]:
                continue
            yield name, data.decode("utf-8", "replace")

    def test_no_personal_paths_or_hosts(self):
        failures = []
        for name, text in self.iter_text_files():
            for number, line in enumerate(text.splitlines(), 1):
                for problem in scan_line(line):
                    failures.append("%s:%d: %s" % (name, number, problem))
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_no_private_terms(self):
        terms = private_terms()
        if not terms:
            self.skipTest("no private-terms list configured")
        failures = []
        for name, text in self.iter_text_files():
            lowered_name = name.lower()
            for term in terms:
                if term in lowered_name:
                    failures.append("%s: file name contains a private term" % name)
            for number, line in enumerate(text.lower().splitlines(), 1):
                for term in terms:
                    if term in line:
                        failures.append("%s:%d: private term %r" % (name, number, term))
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_gitignore_covers_personal_data(self):
        must_ignore = [
            "config.json", "data/images.db", "data/thumbs/thumb-0000.dat",
            "site/index.html", "exports/x/index.html", "abort", "abort.txt",
            "_error_0001.txt", "world_map.png", "__pycache__/x.cpython-314.pyc",
            "my_helper.py", "local/config.json", "models/x.gguf",
            # Key files a user might create next to config.json.
            "openai.key", "keys/openai.txt", "anthropic_key.txt", "api-key.txt",
            ".service_token",
        ]
        must_keep = ["example_helper.py", "sample/images/01-cat.jpg",
                     "static/worldmap.jpg", "static/app.js"]
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin"],
            input="\n".join(must_ignore + must_keep).encode("utf-8"),
            cwd=REPO, capture_output=True)
        ignored = set(result.stdout.decode("utf-8").split())
        self.assertEqual(sorted(set(must_ignore) - ignored), [],
                         "these personal paths are not ignored")
        self.assertEqual(sorted(set(must_keep) & ignored), [],
                         "these project files are wrongly ignored")


class ScannerSelfTest(unittest.TestCase):
    """Make sure the patterns above catch what they are meant to catch."""

    def test_detects_leaks(self):
        leaky = [
            r'"path": "C:\\Users\\alice\\Pictures\\Trip"',
            r"see \\fileserver\photos\2019",
            "server at 192.168.1.20",
            "load Llama-3-8B-Q4_K_M.gguf",
            "E:\\Media\\Pictures",
            'api_key = "sk-proj-Q8v2LmN4rT6yW1zX3cV5bN7mK9pL0qR2"',
            "GEMINI_API_KEY=AIzaSyQ8v2LmN4rT6yW1zX3cV5bN7mK9pL0qR2s",
        ]
        for line in leaky:
            self.assertTrue(scan_line(line), line)

    def test_allows_placeholders(self):
        clean = [
            r'"path": "C:\\Photos\\Family"',
            r"\\server\share\Pictures",
            "http://127.0.0.1:8080/",
            "/home/you/Pictures",
            "point it at model.gguf and model.mmproj.gguf",
            'SECRET = "sk-test-0123456789abcdefSECRET"',
            "export OPENAI_API_KEY=sk-your-key-here-xxxxxxxx",
        ]
        for line in clean:
            self.assertEqual(scan_line(line), [], line)


if __name__ == "__main__":
    unittest.main()
