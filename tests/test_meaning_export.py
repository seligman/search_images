"""Tests for search by meaning in exports (export.py's "meaning" option): the query model
goes along (browser_model.py), with its engine, license and credit; what the review step
says about it; and why it cannot go when it cannot.

The embedding endpoint is tests/fake_embed_server.py on 127.0.0.1 and the model the
made-up one of tests/fake_gguf.py (with a Dense head). When node is installed, the
exported page's script (site.js) loads the model and embeds a query (tests/js/
export_test.js). Every test runs in a temporary SETTINGS_BASE.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import unittest
from unittest import mock

import support

import fake_embed_server
import fake_gguf
from test_embeddings import Context, EmbedFixture, info, insert_image

import admin
import embeddings
import export

NODE = shutil.which("node")
CREDIT = "Fake Bert by nobody (Apache-2.0)"
LICENSE = b"A made-up license text for a made-up model.\n\nApache-2.0 \xc2\xa9 nobody\n"
MEANING_FILES = ("data/meaning/model.js", "data/meaning/w-000.js", "data/meaning/LICENSE.txt",
                 "assets/embed-engine.js", "assets/embed-kernel.js")


def read_manifest(out):
    with open(os.path.join(out, "data", "manifest.js"), encoding="ascii") as handle:
        return json.loads(handle.read().split("=", 1)[1].strip().rstrip(";"))


def vector_parts(out, manifest):
    parts = []
    for name in manifest.get("vectors") or []:
        with open(os.path.join(out, "data", name), encoding="ascii") as handle:
            parts.append(json.loads(handle.read()[len("SEARCH_VECTORS.push("):-3]))
    return parts


class MeaningExportFixture(EmbedFixture):
    """Described pictures with thumbnails, a made-up model with its Dense head and license,
    and vectors made by the fake endpoint (32 numbers, as the model gives)."""

    dims = 16

    def setUp(self):
        super().setUp()
        if not support.pillow_available():
            self.skipTest("Pillow is not installed")
        self.server.dims = fake_gguf.HIDDEN
        self.models = self.base.make_dir("models")
        self.model = fake_gguf.write_bert(os.path.join(self.models, "model.gguf"))
        self.dense, _rows, _bias = fake_gguf.write_dense(
            os.path.join(self.models, "dense.safetensors"), out=48, cols=fake_gguf.HIDDEN)
        self.license = os.path.join(self.models, "LICENSE.txt")
        with open(self.license, "wb") as handle:
            handle.write(LICENSE)
        self.settings = {"model_file": self.model, "dense_file": self.dense,
                         "license_file": self.license, "credit": CREDIT}
        self.configure(embedding=self.settings)
        import cache
        thumbs = cache.ThumbStore()
        self.addCleanup(thumbs.close)
        for image_id in (self.dog, self.cake, self.beach):
            thumbs.put(image_id, support.gradient_jpeg(32, 24))
        self.options = {"images": "none"}
        self.out = self.base.join("site")

    def export(self, options=None, ctx=None):
        return export.export_library(ctx or Context(), self.conn, self.config, "lib_family1",
                                     self.out, dict(self.options, **(options or {})))

    def preview(self, options=None):
        return export.preview_export(self.conn, self.config, "lib_family1",
                                     dict(self.options, **(options or {})), reachable=True)

    def assert_nothing_private(self, out):
        """Scan every file of an export for what must never be in one (as
        test_export.assert_private does): where the data, the libraries and
        the model files are, the file names and folders, the other library,
        the hidden picture, places, the endpoint and its model.
        Only the vocabulary of data/meaning/model.js is left out (common
        words and first names); the rest of it is scanned, and so are its
        parts, LICENSE.txt, the manifest, the vector files and the pages."""
        paths = [self.base.path, self.family, self.trips, self.models, self.model, self.dense,
                 self.license]
        texts = [text for path in paths for text in (path, path.replace("\\", "/"))]
        texts += ["rex-2019-05-14", "pets/", "party/", "Hidden picture", "Something private",
                  "Quiet beach", "lib_family1", "lib_trips01", "Trips", "Paris",
                  "France", "48.85", self.server.base_url,
                  fake_embed_server.MODEL, "model.gguf", "dense.safetensors"]
        scanned = []
        for folder, _dirs, names in os.walk(out):
            for name in names:
                rel = os.path.relpath(os.path.join(folder, name), out).replace(os.sep, "/")
                with open(os.path.join(folder, name), "rb") as handle:
                    data = handle.read()
                if rel.startswith("assets/"):
                    # The page scripts, exactly as the program has them.
                    with open(os.path.join(support.REPO, "static", name), "rb") as handle:
                        self.assertEqual(data, handle.read(), rel)
                    continue
                if rel == "data/meaning/model.js":
                    model = json.loads(data.decode("ascii")[len("SEARCH_EMBED.model("):-3])
                    self.assertGreater(len(model.pop("vocab")), 50)
                    data = json.dumps(model).encode("ascii")
                text = data.decode("utf-8", "replace").lower()
                for value in texts:
                    for form in (value, value.replace("\\", "\\\\")):
                        form = form.lower()
                        if re.match(r"^[a-z_]+$", form):
                            pattern = r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(form)
                            self.assertIsNone(re.search(pattern, text), "%s in %s" % (form, rel))
                        else:
                            self.assertNotIn(form, text, "%s in %s" % (form, rel))
                if rel.endswith((".html", ".js", ".txt")):
                    self.assertIsNone(re.search(r"\b[a-z]:(\\\\|\\|/)", text), rel)
                scanned.append(rel)
        return scanned


class MeaningExportTest(MeaningExportFixture, unittest.TestCase):

    def test_the_model_goes_with_the_export(self):
        self.update()
        ctx = Context()
        summary = self.export(ctx=ctx)
        for rel in MEANING_FILES:
            self.assertTrue(os.path.isfile(os.path.join(self.out, *rel.split("/"))), rel)
        with open(os.path.join(self.out, "data", "meaning", "LICENSE.txt"), "rb") as handle:
            self.assertEqual(handle.read(), LICENSE)
        manifest = read_manifest(self.out)
        meaning = manifest["meaning"]
        self.assertRegex(meaning["id"], r"^m-[0-9a-f]{12}$")
        self.assertEqual((meaning["dims"], meaning["model"], meaning["parts"],
                          meaning["license"], meaning["credit"]),
                         (self.dims, "meaning/model.js", ["meaning/w-000.js"],
                          "meaning/LICENSE.txt", CREDIT))
        self.assertGreater(meaning["bytes"], meaning["gzip"])
        self.assertEqual(sorted(meaning["assets"]), ["embed-engine.js", "embed-kernel.js"])
        for name in ("meaning/model.js", "meaning/w-000.js", "meaning/LICENSE.txt"):
            self.assertRegex(manifest["versions"][name], r"^[0-9a-f]{10}$")
        self.assertEqual(summary["meaning"]["id"], meaning["id"])
        self.assertIn("Search by meaning in the browser:", ctx.text())
        self.assertIn("visitors download about", ctx.text())
        # The vector files carry the model's id and the typical cosine.
        parts = vector_parts(self.out, manifest)
        self.assertTrue(parts)
        self.assertTrue(all(part["model"] == meaning["id"] for part in parts))
        # The foot of the pages credits the model and links to its license.
        for page in ("index.html", "detail.html"):
            with open(os.path.join(self.out, page), encoding="utf-8") as handle:
                html = handle.read()
            self.assertIn('Search by meaning: Fake Bert by nobody (Apache-2.0) &middot; <a href='
                          '"data/meaning/LICENSE.txt" rel="license">license</a>', html)
        # Nothing of where the data and the model live, nor of what is not
        # published.
        scanned = self.assert_nothing_private(self.out)
        for rel in ("data/meaning/model.js", "data/meaning/w-000.js", "data/meaning/LICENSE.txt",
                    "data/manifest.js", "data/vectors-000.js", "index.html", "detail.html"):
            self.assertIn(rel, scanned)
        # The vector files: the vectors and the model's id, nothing more.
        self.assertTrue(all(sorted(part) == ["data", "dims", "ids", "model"] for part in parts))
        # Nothing changed: nothing written.
        self.assertEqual(self.export()["written"], 0)
        # Off: the model files go, the vector files stay (without the id).
        self.export({"meaning": False})
        for rel in MEANING_FILES:
            self.assertFalse(os.path.exists(os.path.join(self.out, *rel.split("/"))), rel)
        manifest = read_manifest(self.out)
        self.assertNotIn("meaning", manifest)
        # (The vector files as without the option.)
        self.assertTrue(all(sorted(part) == ["data", "dims", "ids"]
                            for part in vector_parts(self.out, manifest)))
        with open(os.path.join(self.out, "index.html"), encoding="utf-8") as handle:
            self.assertNotIn("Search by meaning", handle.read())

    def test_other_pictures_do_not_change_the_export(self):
        # Pictures that are not published (another library's, a hidden one)
        # leave the exported files as they are: nothing of them is in it.
        self.update()
        self.assertGreater(self.export()["written"], 0)
        insert_image(self.conn, "lib_trips01", "boat.jpg",
                     info("Sailing boat", "A boat on a lake.", tags=["boat"]))
        insert_image(self.conn, "lib_family1", "private/other.jpg",
                     info("Another hidden one", "Private again."), hidden=1)
        self.assertEqual(self.update()["embedded"], 2)
        summary = self.export()
        self.assertEqual(summary["written"], 0)
        self.assertIsNotNone(summary["meaning"])

    def test_the_review_step(self):
        self.update()
        preview = self.preview()
        meaning = preview["counts"]["meaning"]
        self.assertTrue(meaning["available"], meaning)
        self.assertEqual(meaning["reason"], "")
        self.assertGreater(meaning["bytes"], meaning["gzip"])
        self.assertEqual(preview["size_parts"]["meaning"], meaning["bytes"])
        # With the option off, the model is not converted for its size.
        with mock.patch.object(export, "meaning_files",
                               side_effect=AssertionError("converted")):
            off = self.preview({"meaning": False})
        self.assertEqual(off["size_parts"]["meaning"], 0)
        self.assertTrue(off["counts"]["meaning"]["available"])
        # What the review step's line says (the same as the log's).
        self.assertIn("visitors download about", export.meaning_text(meaning))
        library = self.config["libraries"][0]
        self.assertEqual(admin.export_view(self.config, library)["meaning"],
                         {"available": True, "reason": ""})

    def test_why_it_cannot_go(self):
        def reason(**settings):
            self.configure(embedding=dict(self.settings, **settings))
            preview = self.preview()
            self.assertFalse(preview["counts"]["meaning"]["available"])
            self.assertEqual(preview["size_parts"]["meaning"], 0)
            summary = self.export()
            self.assertIsNone(summary["meaning"])
            self.assertFalse(os.path.exists(os.path.join(self.out, "data", "meaning")))
            return preview["counts"]["meaning"]["reason"]

        # No vectors yet.
        self.assertIn("No picture to publish has an up-to-date meaning vector", reason())
        self.update()
        self.assertIn("needs More like this", self.preview({"similar": False})["counts"]
                      ["meaning"]["reason"])
        self.assertIn("Set embedding.model_file", reason(model_file=""))
        other = fake_gguf.write_other(os.path.join(self.models, "other.model"))
        self.assertIn("cannot run in a browser", reason(model_file=other))
        self.assertIn("Set embedding.license_file", reason(license_file=""))
        empty = os.path.join(self.models, "empty.txt")
        open(empty, "wb").close()
        self.assertIn("cannot be read or is empty", reason(license_file=empty))
        bad = os.path.join(self.models, "bad.safetensors")
        with open(bad, "wb") as handle:
            handle.write(b"\x00" * 16)
        self.assertIn("Dense head", reason(dense_file=bad))
        # Vectors made with another model file than the one set now.
        another = fake_gguf.write_bert(os.path.join(self.models, "another.model"), seed=3)
        self.assertIn("Update meaning search", reason(model_file=another))
        # Vectors without a probe (made before 3c, say).
        self.configure(embedding=self.settings)
        store = embeddings.connect()
        with store:
            store.execute("UPDATE meta SET value = '' WHERE key = 'probe'")
        store.close()
        self.assertIn("it checks the model", reason())

    @unittest.skipIf(NODE is None, "node is not installed")
    def test_the_page_embeds_queries_with_the_model(self):
        self.update()
        # The probe as the engine makes it (the fake endpoint is not the
        # model): the page's self-check then passes.
        made = export.meaning_files(export.meaning_status(
            self.config, export.validate_options(self.options), 1))
        folder = self.base.make_dir("converted")
        for rel, data in made["files"].items():
            path = os.path.join(folder, *rel[len("data/"):].split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js", "embed_text.js"),
                                 folder] + list(embeddings.PROBE_TEXTS), cwd=support.REPO,
                                capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = json.loads(result.stdout.decode("ascii"))
        store = embeddings.connect()
        with store:
            store.execute("UPDATE meta SET value = ? WHERE key = 'probe'", (json.dumps(probe),))
        store.close()
        self.export()
        manifest = read_manifest(self.out)
        expected = self.base.write_file("expected.json", json.dumps(
            {"page": "index.html", "count": 2, "vectors": 2, "queries": [],
             "meaning": {"query": "a dog asleep on a sofa", "id": manifest["meaning"]["id"],
                         "dims": self.dims}}))
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "export_test.js"), self.out, expected],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("ok   search by meaning runs in the page", output)
        self.assertIn("ok   nothing of the model loads before a search by meaning", output)
        # A probe the page cannot match: word search only.
        store = embeddings.connect()
        bad = [dict(item, vec=base64.b64encode(bytes([1] * self.dims)).decode("ascii"))
               for item in probe]
        with store:
            store.execute("UPDATE meta SET value = ? WHERE key = 'probe'", (json.dumps(bad),))
        store.close()
        self.export()
        with open(expected, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"page": "index.html", "count": 2, "vectors": 2, "queries": [],
                       "meaning": {"query": "a dog", "state": "off"}}, handle)
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                    "export_test.js"), self.out, expected],
                                cwd=support.REPO, capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("ok   search by meaning runs in the page", output)


if __name__ == "__main__":
    unittest.main()
