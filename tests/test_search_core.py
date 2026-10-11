"""Tests for static/search-core.js, the search engine of the search page.

The engine is JavaScript, so its tests are too: tests/js/search_core_test.js
runs under Node.js (a development tool only; these tests are skipped when
`node` is not installed). That script covers the query parser, matching and
ranking, facets, duplicate stacks, highlights, "more like this", "did you
mean" and the speed on 10,000 made-up records.

The checks here read the file as text: it must stay plain ES5-style code
with no page (DOM) access and nothing that a strict Content-Security-Policy
forbids, because the same file runs in the server pages, in a static export
opened from file:// and under Node.
"""

import os
import re
import shutil
import subprocess
import unittest

import support

SEARCH_CORE = os.path.join(support.REPO, "static", "search-core.js")
NODE_TESTS = os.path.join(support.REPO, "tests", "js", "search_core_test.js")
NODE = shutil.which("node")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


code_only = support.code_only


class SearchCoreSourceTest(unittest.TestCase):

    def setUp(self):
        self.text = read(SEARCH_CORE)
        self.code = code_only(self.text)

    def test_no_page_access_and_nothing_a_strict_policy_forbids(self):
        for pattern in (r"\bdocument\s*\.", r"\bwindow\s*\.", r"\blocalStorage\b",
                        r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML",
                        r"\beval\s*\(", r"\bnew\s+Function\b", r"\bsetTimeout\s*\(\s*\"",
                        r"\bfetch\s*\(", r"\bXMLHttpRequest\b"):
            self.assertIsNone(re.search(pattern, self.code), pattern)

    def test_plain_es5_syntax(self):
        # No arrow functions, let/const, classes, template strings, spread or
        # for...of, like the other page scripts.
        for pattern in (r"=>", r"\blet\s+[A-Za-z_$]", r"\bconst\s+[A-Za-z_$]",
                        r"\bclass\s+[A-Za-z_$]", r"`", r"\.\.\.[A-Za-z_$\[]",
                        r"\bfor\s*\([^;)]*\bof\b", r"\bimport\s", r"\bexport\s"):
            self.assertIsNone(re.search(pattern, self.code), pattern)

    def test_ascii_source(self):
        # Characters outside ASCII are written as backslash-u escapes, so
        # the file reads the same whatever encoding a browser assumes for it.
        self.assertTrue(all(ord(ch) < 128 for ch in self.text))

    def test_works_as_a_page_script_and_a_node_module(self):
        self.assertTrue(self.text.lstrip().startswith("/*"))
        self.assertIn("var SearchCore = (function () {", self.text)
        self.assertIn('"use strict";', self.text)
        self.assertIn("module.exports = SearchCore;", self.text)


@unittest.skipIf(NODE is None, "Node.js is not installed")
class SearchCoreNodeTest(unittest.TestCase):

    def test_search_core(self):
        result = subprocess.run([NODE, NODE_TESTS], cwd=support.REPO, capture_output=True,
                                timeout=600)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, "\n" + output)
        self.assertRegex(output, r"(\d+) of \1 passed")


if __name__ == "__main__":
    unittest.main()
