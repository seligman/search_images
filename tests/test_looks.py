"""Tests for looks ("Looks like this" and search by an example picture): looks.py,
images.prepare_for_looks, the image_embed job and scan.py --embed-images, /api/looks
and /api/looks/query, the image embeddings model server and the export's looks files.

The image model is tests/fake_image_embedder.py on 127.0.0.1 (pictures that
look alike get close vectors); every test runs in a temporary SETTINGS_BASE.
tests/js/looks_test.js (run here when Node is installed) covers look: and
the example picture in search-core.js and the export review's words, and
tests/js/app_looks_test.js the search page's example picture (app.js).
"""

import base64
import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

import common
import db
import embeddings
import jobs
import llm
import local_server
import looks

try:
    import flask
except ImportError:
    flask = None

if support.pillow_available():
    from PIL import Image
    import fake_image_embedder
    import images
else:
    Image = fake_image_embedder = images = None

NOW = 1700000000.0
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}
JS_TESTS = os.path.join(support.REPO, "tests", "js", "looks_test.js")
APP_JS_TESTS = os.path.join(support.REPO, "tests", "js", "app_looks_test.js")
quiet = support.quiet


def picture(colour, size=(64, 48), stripe=None):
    """JPEG bytes of a picture of one colour (with a stripe of another, if given)."""
    image = Image.new("RGB", size, colour)
    if stripe is not None:
        image.paste(stripe, (0, 0, size[0] // 3, size[1]))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def insert_image(conn, library_id, path, **fields):
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 64, "height": 48,
           "added": NOW, "updated": NOW, "content_hash": "hash-" + path}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute("INSERT INTO images (%s) VALUES (%s)"
                          % (",".join(columns), ",".join("?" * len(columns))),
                          [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


class Context(jobs.JobContext):
    """A job context that keeps its log lines (and can be told to stop after n steps)."""

    def __init__(self, stop_after=None):
        jobs.JobContext.__init__(self, "test")
        self.lines = []
        self.stop_after = stop_after

    def _emit(self, entry):
        self.lines.append((entry["level"], entry["message"]))

    def _progress(self):
        if self.stop_after is not None and self.done >= self.stop_after:
            self.request_stop()

    def text(self):
        return "\n".join(message for _level, message in self.lines)


class SettingsTest(unittest.TestCase):

    def test_defaults_and_values_that_cannot_be_used(self):
        self.assertEqual(looks.settings({}), looks.DEFAULTS)
        options = looks.settings({"image_embedding": {
            "endpoint": " img ", "dims": 3, "max_edge": 99999, "batch": 0,
            "probe_check": "no", "model_file": " \"model.gguf\" "}})
        self.assertEqual(options["endpoint"], "img")
        self.assertEqual((options["dims"], options["max_edge"], options["batch"]),
                         (256, 1024, 8))
        self.assertTrue(options["probe_check"])
        self.assertEqual(options["model_file"], "model.gguf")
        options = looks.settings({"image_embedding": {"dims": 128, "max_edge": 768,
                                                      "batch": 4, "probe_check": False}})
        self.assertEqual((options["dims"], options["max_edge"], options["batch"],
                          options["probe_check"]), (128, 768, 4, False))
        self.assertFalse(looks.configured({"image_embedding": {"endpoint": " "}}))

    def test_only_local_openai_compatible_endpoints(self):
        endpoints = [{"name": "here", "preset": "llamacpp", "base_url": "http://127.0.0.1:1/v1"},
                     {"name": "off", "preset": "llamacpp", "enabled": False},
                     {"name": "claude", "preset": "anthropic"},
                     {"name": "llama", "kind": "ollama", "base_url": "http://127.0.0.1:2"},
                     {"name": "cloud", "preset": "openai", "model": "some-model"}]

        def problem(name):
            config = {"endpoints": endpoints, "image_embedding": {"endpoint": name}}
            return looks.endpoint_for(config)

        self.assertEqual(problem("")[1], looks.NOT_CONFIGURED)
        self.assertIn("image_embedding.endpoint", problem("nothing")[1])
        self.assertIn("turned off", problem("off")[1])
        self.assertIn("cannot embed pictures", problem("claude")[1])
        self.assertIn("cannot embed pictures", problem("llama")[1])
        self.assertIn("not on this computer", problem("cloud")[1])
        self.assertEqual(problem("here")[0]["name"], "here")

    def test_kinds_of_picture(self):
        self.assertEqual(looks.kind_of({"media": "image", "projection": ""}), looks.KIND_PHOTO)
        self.assertEqual(looks.kind_of({"media": "image", "projection": "?"}), looks.KIND_PHOTO)
        self.assertEqual(looks.kind_of({"media": "image", "projection": "dual_fisheye"}),
                         looks.KIND_PANO)
        self.assertEqual(looks.kind_of({"media": "video", "projection": ""}), looks.KIND_POSTER)
        self.assertEqual(looks.look_id("ab" * 20), "l-" + "ab" * 6)

    def test_ollama_cannot_embed_pictures(self):
        endpoint = {"name": "o", "kind": "ollama", "base_url": "http://127.0.0.1:9"}
        with self.assertRaises(llm.ProviderError) as caught:
            llm.embed(endpoint, [looks.image_input(b"jpeg")])
        self.assertIn("cannot embed pictures", str(caught.exception))


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class PrepareTest(unittest.TestCase):

    def size(self, data):
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.info.get("exif"))
            return image.size

    def test_long_edge_and_never_upscaled(self):
        self.assertEqual(self.size(images.prepare_for_looks(picture("red", (3000, 1000)))),
                         (1024, 341))
        self.assertEqual(self.size(images.prepare_for_looks(picture("red", (3000, 1000)), 512)),
                         (512, 171))
        self.assertEqual(self.size(images.prepare_for_looks(picture("red", (100, 50)))),
                         (100, 50))
        with self.assertRaises(images.ImageReadError):
            images.prepare_for_looks(b"not a picture")

    def test_a_panorama_is_seen_as_its_front_view(self):
        width = images.front_view_width(2048, 1024)
        self.assertTrue(700 < width < 800, width)          # no more than the panorama holds
        self.assertEqual(images.front_view_width(8000, 1024), 1024)
        self.assertEqual(images.front_view_width(400, 1024), images.THUMB_SIZE)
        size = self.size(images.prepare_for_looks(picture("blue", (2048, 1024)), 1024,
                                                  panorama=True))
        self.assertEqual(size, (width, round(width * 3 / 4)))

    def test_probe_pictures(self):
        first = looks.probe_pictures(looks.DEFAULTS)
        self.assertEqual([name for name, _jpeg in first], list(looks.PROBE_NAMES))
        self.assertEqual(first, looks.probe_pictures(looks.DEFAULTS))
        self.assertEqual(len({jpeg for _name, jpeg in first}), 3)
        self.assertEqual(self.size(first[0][1]), looks.PROBE_SIZE)


class StoreTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.base.write_config([])
        db.init(log=quiet)

    def set_version(self, value):
        conn = sqlite3.connect(looks.store_path())
        try:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'version'", (value,))
            conn.commit()
        finally:
            conn.close()

    def count(self):
        conn = sqlite3.connect(looks.store_path())
        try:
            return conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]
        finally:
            conn.close()

    def test_layout_and_a_version_change(self):
        store = looks.connect()
        looks.write(store, [("h1", looks.KIND_PHOTO, b"\x01\x02"),
                            ("h1", looks.KIND_POSTER, b"\x03\x04")])
        self.assertEqual(looks.stored_keys(store), {("h1", looks.KIND_PHOTO),
                                                    ("h1", looks.KIND_POSTER)})
        self.assertEqual(looks.vectors_for(store, [("h1", looks.KIND_POSTER), ("h2", "photo")]),
                         {("h1", looks.KIND_POSTER): b"\x03\x04"})
        found = looks.meta(store)
        self.assertEqual((found["count"], found["version"]), (2, looks.LOOKS_VERSION))
        epoch = found["epoch"]
        store.close()
        # A store of a newer version (or one that cannot be told) is never emptied:
        # the readers see no looks, a run that writes stops.
        for version in ("99", "x"):
            self.set_version(version)
            self.assertIsNone(looks.connect(create=False))
            self.assertIsNone(looks.read_meta())
            with self.assertRaises(looks.StoreError):
                looks.connect()
            self.assertEqual(self.count(), 2)
        # An older one: left alone by the readers, emptied by a run that writes.
        self.set_version("0")
        self.assertIsNone(looks.connect(create=False))
        self.assertEqual(self.count(), 2)
        store = looks.connect()
        self.addCleanup(store.close)
        self.assertEqual(looks.stored_keys(store), set())
        self.assertNotEqual(looks.meta(store)["epoch"], epoch)
        self.assertEqual(looks.reset(store, "r", 16), 0)
        self.assertEqual((looks.meta(store)["recipe"], looks.meta(store)["dims"]), ("r", 16))
        self.assertIsNone(looks.connect(self.base.join("data", "none.db"), create=False))

    def test_purge_drops_only_content_no_row_has(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        insert_image(conn, "lib_a", "kept.jpg", content_hash="kept")
        insert_image(conn, "lib_a", "away.jpg", content_hash="away", missing_since=NOW)
        insert_image(conn, "lib_a", "movie.mp4", content_hash="movie", media="video")
        store = looks.connect()
        self.addCleanup(store.close)
        looks.write(store, [("kept", "photo", b"\x01"), ("away", "photo", b"\x01"),
                            ("gone", "photo", b"\x01"), ("movie", "photo", b"\x01"),
                            ("movie", "video-poster", b"\x01")])
        self.assertEqual(looks.purge(store, conn), 2)
        self.assertEqual(looks.stored_keys(store), {("kept", "photo"), ("away", "photo"),
                                                    ("movie", "video-poster")})
        # Right after rows are deleted (Delete indexed data, the scanner's purge).
        conn.execute("DELETE FROM images WHERE content_hash = 'away'")
        self.assertEqual(looks.forget_unreferenced(conn), 1)
        self.assertEqual(looks.stored_keys(store), {("kept", "photo"),
                                                    ("movie", "video-poster")})
        store.close()
        os.remove(looks.store_path())
        self.assertEqual(looks.forget_unreferenced(conn), 0)      # (no store: nothing to do)
        self.assertFalse(os.path.exists(looks.store_path()))

    def test_deleting_a_librarys_data_drops_the_looks_no_row_needs(self):
        import library_ops
        conn = db.connect()
        self.addCleanup(conn.close)
        insert_image(conn, "lib_a", "a.jpg", content_hash="only-a")
        insert_image(conn, "lib_a", "shared.jpg", content_hash="shared")
        insert_image(conn, "lib_b", "shared.jpg", content_hash="shared")
        store = looks.connect()
        self.addCleanup(store.close)
        looks.write(store, [("only-a", "photo", b"\x01"), ("shared", "photo", b"\x01")])
        library_ops.delete_library_data(conn, "lib_a")
        self.assertEqual(looks.stored_keys(store), {("shared", "photo")})


class RecipeTest(support.TempBaseMixin, unittest.TestCase):

    def write(self, name, data):
        path = self.base.join("models", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_the_recipe_follows_the_files_and_settings(self):
        model = self.write("model.gguf", b"GGUF one")
        mmproj = self.write("model.mmproj.gguf", b"proj")
        config = {"image_embedding": {"endpoint": "x", "model_file": model,
                                      "mmproj_file": mmproj}}
        first = looks.recipe(config)
        self.assertEqual(first, looks.recipe(config))
        for change in ({"dims": 128}, {"max_edge": 768}):
            other = {"image_embedding": dict(config["image_embedding"], **change)}
            self.assertNotEqual(looks.recipe(other), first)
        self.write("model.mmproj.gguf", b"another projector")
        self.assertNotEqual(looks.recipe(config), first)
        os.remove(mmproj)
        with self.assertRaises(looks.browser_model.ModelError):
            looks.recipe(config)
        self.assertIn("cannot be read", looks.recipe_problem(config))
        # Nothing known: dims and the way pictures are prepared still count.
        self.assertNotEqual(looks.recipe({}), looks.recipe({"image_embedding": {"dims": 64}}))

    def test_the_files_of_a_managed_image_embeddings_server(self):
        self.write("model.gguf", b"GGUF")
        self.write("model.mmproj.gguf", b"PROJ")
        config = {"endpoints": [{"name": "img", "preset": "llamacpp",
                                 "base_url": "http://127.0.0.1:8097/v1"}],
                  "local_server": [{"enabled": True, "kind": "embeddings", "endpoint": "img",
                                    "binary": "llama-server", "port": 8097,
                                    "models_dir": self.base.join("models"),
                                    "model": "model.gguf", "mmproj": "model.mmproj.gguf"}],
                  "image_embedding": {"endpoint": "img"}}
        model, mmproj, problem = looks.model_files(config)
        self.assertEqual((os.path.basename(model), os.path.basename(mmproj), problem),
                         ("model.gguf", "model.mmproj.gguf", ""))
        settings = local_server.find_server(config, "img")
        command = local_server.build_command(settings)
        at = command.index("--mmproj")
        self.assertEqual(command[at + 1], mmproj)
        self.assertEqual(command[at + 2:at + 4], ["-fa", "off"])
        self.assertIn("--embeddings", command)
        # Without "mmproj" an embeddings server has none, even next to the model.
        del config["local_server"][0]["mmproj"]
        settings = local_server.find_server(config, "img")
        self.assertEqual(local_server.mmproj_path(settings), "")
        self.assertNotIn("--mmproj", local_server.build_command(settings))
        self.assertNotIn("-fa", local_server.build_command(settings))
        config["local_server"][0]["mmproj"] = "mmproj-model.gguf"
        self.assertIn("does not exist", looks.model_files(config)[2])


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class LooksFixture(support.TempBaseMixin):
    """Two libraries with pictures (previews stored), and the fake image model."""

    dims = 16

    def setUp(self):
        super().setUp()
        import cache
        self.server = fake_image_embedder.FakeImageEmbedder()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.url = self.server.base_url
        self.family = self.base.library_folder("family")
        self.trips = self.base.library_folder("trips")
        self.configure()
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.previews = cache.PreviewStore()
        self.red = self.add("lib_family1", "red.jpg", picture("red"))
        self.copy = self.add("lib_family1", "copy/red.jpg", picture("red"),
                             content_hash="hash-red.jpg")
        self.dark_red = self.add("lib_family1", "dark-red.jpg", picture((170, 20, 20)))
        self.hidden = self.add("lib_family1", "private.jpg", picture("green"), hidden=1)
        self.gone = self.add("lib_family1", "gone.jpg", picture("yellow"), missing_since=NOW)
        self.video = self.add("lib_family1", "clip.mp4", picture("blue"), media="video")
        self.pano = self.add("lib_trips01", "pano.jpg", picture("white", (256, 128), "blue"),
                             projection="equirectangular")

    def add(self, library_id, path, jpeg=None, **fields):
        image_id = insert_image(self.conn, library_id, path,
                                has_preview=1 if jpeg is not None else 0, **fields)
        if jpeg is not None:
            self.previews.write(library_id, image_id, jpeg)
        return image_id

    def endpoint(self, **extra):
        entry = {"name": "looks", "preset": "llamacpp", "base_url": self.url}
        entry.update(extra)
        return entry

    def configure(self, image_embedding=None, libraries=None, **extra):
        settings = {"endpoint": "looks", "dims": self.dims, "batch": 2}
        settings.update(image_embedding or {})
        self.base.write_config(libraries or [
            support.library("Family", self.family, id="lib_family1"),
            support.library("Trips", self.trips, id="lib_trips01"),
        ], endpoints=[self.endpoint()], image_embedding=settings, **extra)
        self.config = common.load_config()
        return self.config

    def update(self, ctx=None, **kwargs):
        return looks.update(self.conn, self.config, ctx or Context(),
                            sleep=lambda _seconds: None, **kwargs)

    def stored(self):
        store = looks.connect()
        try:
            return looks.stored_keys(store), looks.meta(store)
        finally:
            store.close()

    def key(self, image_id):
        row = self.conn.execute("SELECT content_hash, media, projection FROM images WHERE id = ?",
                                (image_id,)).fetchone()
        return (row["content_hash"], looks.kind_of(row))


class UpdateTest(LooksFixture, unittest.TestCase):

    def test_every_present_picture_once_per_content(self):
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["failed"], summary["pending"],
                          summary["probe"], summary["error"]), (5, 0, 5, "reference", ""))
        keys, current = self.stored()
        self.assertEqual(keys, {self.key(image_id) for image_id in (
            self.red, self.dark_red, self.hidden, self.video, self.pano)})
        self.assertIn(("hash-clip.mp4", looks.KIND_POSTER), keys)
        self.assertIn(("hash-pano.jpg", looks.KIND_PANO), keys)
        self.assertEqual((current["dims"], current["native_dims"], current["served"]),
                         (self.dims, fake_image_embedder.DIMS, fake_image_embedder.MODEL))
        self.assertEqual([item["name"] for item in current["probe"]], list(looks.PROBE_NAMES))
        # (The reference is kept at the model's own length: other dims keep it.)
        self.assertEqual({len(base64.b64decode(item["vec"])) for item in current["probe"]},
                         {fake_image_embedder.DIMS})
        self.assertEqual(current["probe_key"], looks.probe_key(self.config))
        self.assertIn("first run", current["probe_note"])
        sent = self.server.pictures()
        self.assertEqual(len(sent), 3 + 5)                 # the probe pictures first
        # The 360 photo is sent as its front view (4:3), not its panorama.
        sizes = [Image.open(io.BytesIO(jpeg)).size for jpeg in sent[3:]]
        self.assertIn(True, [abs(width / height - 4 / 3) < 0.02 for width, height in sizes])
        self.assertNotIn(True, [width == 2 * height for width, height in sizes])
        # Nothing to do: the model is not asked again.
        before = len(self.server.embed_requests())
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["up_to_date"], summary["probe"]),
                         (0, 6, ""))
        self.assertEqual(len(self.server.embed_requests()), before)
        # Pictures that look alike have close vectors.
        store = looks.connect()
        self.addCleanup(store.close)
        found = looks.vectors_for(store, looks.stored_keys(store))
        red, dark, blue = (found[self.key(image_id)] for image_id in (
            self.red, self.dark_red, self.video))
        self.assertGreater(embeddings.signed_cosine(red, dark),
                           embeddings.signed_cosine(red, blue))

    def test_a_server_that_makes_other_vectors_stops_the_run(self):
        self.update()
        late = self.add("lib_family1", "late.jpg", picture("orange"))
        self.server.drift = True
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["embedded"], summary["probe"]), (0, "mismatch"))
        self.assertIn("flash attention", summary["error"])
        self.assertIn("-fa off", summary["error"])
        self.assertIn("made by the first run", summary["error"])
        self.assertNotIn(self.key(late), self.stored()[0])
        # The Manage page knows (and offers Rebuild looks); the kept reference is not
        # one a rebuild keeps.
        status = looks.status(self.config)
        self.assertEqual((status["mismatch"], status["changed"], status["reference"]),
                         (summary["error"], False, False))
        # The check can be turned off.
        self.configure({"probe_check": False})
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["probe"], summary["error"]), (1, "off", ""))
        self.assertEqual(looks.status(self.config)["mismatch"], "")
        # The same server again: checked.
        self.server.drift = False
        self.add("lib_family1", "later.jpg", picture("purple"))
        self.configure()
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["probe"]), (1, "checked"))
        self.assertTrue(looks.status(self.config)["reference"])

    def test_a_reference_that_was_wrong_is_rebuilt(self):
        # The first run was on a server with flash attention on (and failed for
        # every picture): its probe vectors became the reference.
        self.server.drift = True
        probes = {jpeg for _name, jpeg in looks.probe_pictures(looks.settings(self.config))}
        self.server.refuse = lambda jpeg: jpeg not in probes
        summary = self.update()
        self.assertEqual((summary["probe"], summary["embedded"]), ("reference", 0))
        # The server is right now: the check fails, and Update looks cannot help.
        self.server.drift = False
        self.server.refuse = None
        summary = self.update()
        self.assertEqual(summary["probe"], "mismatch")
        self.assertTrue(looks.status(self.config)["mismatch"])
        self.assertEqual(self.update()["probe"], "mismatch")
        # Rebuild looks throws away the reference (with no vector kept), and goes on.
        ctx = Context()
        summary = self.update(ctx, rebuild=True)
        self.assertEqual((summary["probe"], summary["embedded"], summary["error"]),
                         ("reference", 5, ""))
        self.assertIn("after a failed probe check", ctx.text())
        self.assertEqual(looks.status(self.config)["mismatch"], "")
        # With looks kept from the wrong server: those go too.
        self.server.drift = True
        late = self.add("lib_family1", "late.jpg", picture("orange"))
        self.assertEqual(self.update()["probe"], "mismatch")
        summary = self.update(rebuild=True)
        self.assertEqual((summary["rebuilt"], summary["embedded"], summary["probe"]),
                         (5, 6, "reference"))
        self.assertIn(self.key(late), self.stored()[0])
        # Now every run checks against the new reference.
        self.assertEqual(self.update(rebuild=True)["rebuilt"], 0)

    def test_a_rebuild_goes_on_where_it_stopped(self):
        self.update()
        self.configure({"dims": 32})
        # Stopped after two pictures (stop_at, a restart): run again, it goes on.
        summary = self.update(Context(stop_after=2), rebuild=True)
        self.assertEqual((summary["rebuilt"], summary["embedded"], summary["stopped"]),
                         (5, 2, True))
        self.assertEqual(summary["probe"], "checked")       # (the reference was kept)
        summary = self.update(rebuild=True)
        self.assertEqual((summary["rebuilt"], summary["embedded"]), (0, 3))
        self.assertEqual(len(self.stored()[0]), 5)
        # One rebuild job per library (each card has its button): the second
        # keeps what the first made.
        self.configure({"dims": 16})
        first = jobs.run_image_embed(Context(), self.conn, self.config,
                                     {"library_id": "lib_family1", "rebuild": True})
        self.assertEqual((first["rebuilt"], first["embedded"]), (5, 4))
        second = jobs.run_image_embed(Context(), self.conn, self.config,
                                      {"library_id": "lib_trips01", "rebuild": True})
        self.assertEqual((second["rebuilt"], second["embedded"]), (0, 1))
        counted = looks.counts(self.conn, self.config, ["lib_family1", "lib_trips01"])
        self.assertEqual(counted["lib_family1"],
                         {"pictures": 5, "embedded": 5, "pending": 0, "views": 0})
        self.assertEqual(counted["lib_trips01"],
                         {"pictures": 1, "embedded": 1, "pending": 0, "views": 0})

    def test_other_settings_keep_the_looks_until_a_rebuild(self):
        self.update()
        kept = self.stored()[0]
        self.configure({"dims": 32})
        summary = self.update()
        self.assertTrue(summary["changed"])
        self.assertIn("Rebuild looks", summary["error"])
        self.assertEqual(summary["embedded"], 0)
        self.assertEqual(self.stored()[0], kept)
        counted = looks.counts(self.conn, self.config, ["lib_family1"])
        self.assertEqual(counted["lib_family1"]["embedded"], 0)
        self.assertTrue(looks.status(self.config)["changed"])
        summary = self.update(rebuild=True)
        self.assertEqual((summary["rebuilt"], summary["embedded"], summary["error"]), (5, 5, ""))
        self.assertEqual(self.stored()[1]["dims"], 32)
        self.assertFalse(looks.status(self.config)["changed"])

    def test_limit_stop_and_counts(self):
        summary = self.update(limit=1)
        self.assertEqual(summary["embedded"], 1)
        self.assertEqual(self.stored()[0], {self.key(self.pano)})      # the newest first
        counted = looks.counts(self.conn, self.config, ["lib_family1", "lib_trips01"])
        self.assertEqual(counted, {
            "lib_family1": {"pictures": 5, "embedded": 0, "pending": 5, "views": 0},
            "lib_trips01": {"pictures": 1, "embedded": 1, "pending": 0, "views": 0}})
        summary = self.update(Context(stop_after=2))
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["embedded"], 2)
        counted = looks.counts(self.conn, self.config, ["lib_family1"])
        self.assertEqual(counted["lib_family1"]["embedded"], 2)

    def test_pictures_that_cannot_be_read_or_are_refused(self):
        broken = self.add("lib_family1", "broken.jpg", b"not a picture at all")
        refused = self.add("lib_family1", "refused.jpg", picture((250, 2, 250)))

        def magenta(jpeg):
            red, green, blue = Image.open(io.BytesIO(jpeg)).convert("RGB").getpixel((5, 5))
            return red > 240 and green < 20 and blue > 240
        self.server.refuse = magenta
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["embedded"], summary["failed"], summary["error"]), (5, 2, ""))
        keys = self.stored()[0]
        self.assertNotIn(self.key(broken), keys)
        self.assertNotIn(self.key(refused), keys)
        self.assertIn("Picture %d could not be embedded" % broken, ctx.text())

    def test_a_picture_without_a_preview_waits_for_its_folder(self):
        lone = self.add("lib_trips01", "far/away.jpg")
        shutil.rmtree(self.trips)
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["offline"], summary["failed"]), (1, 0))
        self.assertNotIn(self.key(lone), self.stored()[0])
        self.assertIn("cannot be reached", ctx.text())
        # Back: read from the original.
        os.makedirs(os.path.join(self.trips, "far"))
        with open(os.path.join(self.trips, "far", "away.jpg"), "wb") as handle:
            handle.write(picture("pink", (300, 200)))
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["offline"]), (1, 0))
        self.assertIn(self.key(lone), self.stored()[0])

    def test_the_server_must_serve_what_the_settings_say(self):
        model = self.base.write_file("models/model.gguf", b"GGUF")
        self.configure({"model_file": model})
        self.server.served = "your-model.gguf"
        summary = self.update()
        self.assertIn("serves the model your-model.gguf", summary["error"])
        self.assertEqual(self.stored()[0], set())
        self.server.served = "model.gguf"
        self.assertEqual(self.update()["error"], "")
        self.configure({"dims": 128})
        summary = self.update(rebuild=True)
        self.assertIn("image_embedding.dims", summary["error"])

    def test_purge_after_rows_go_and_nothing_without_settings(self):
        self.update()
        self.conn.execute("DELETE FROM images WHERE id = ?", (self.dark_red,))
        self.conn.commit()
        summary = self.update()
        self.assertEqual(summary["removed"], 1)
        self.assertNotIn(("hash-dark-red.jpg", looks.KIND_PHOTO), self.stored()[0])
        self.configure({"endpoint": ""})
        self.assertFalse(self.update()["configured"])
        self.server.stop()
        self.configure()
        self.assertEqual(self.update()["error"], "")      # (nothing to do: not asked)
        self.add("lib_family1", "new.jpg", picture("teal"))
        summary = self.update()
        self.assertEqual(summary["embedded"], 0)
        self.assertTrue(summary["error"])

    def test_the_job_the_command_line_and_the_scan_note(self):
        import scan
        self.assertIn("image_embed", jobs.RUNNERS)
        summary = jobs.run_image_embed(Context(), self.conn, self.config, {"limit": 2})
        self.assertEqual(summary["embedded"], 2)
        with mock.patch.object(scan, "db") as fake_db:
            fake_db.connect.side_effect = db.connect
            fake_db.init = lambda *args, **kwargs: None
            fake_db.DatabaseError = db.DatabaseError
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scan.main(["--embed-images", "--library", "Family"]), 0)
        self.assertIn("Made the looks of 3 picture(s)", out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main(["--embed-images", "--embed"]), 2)
            self.assertEqual(scan.main(["--rebuild-looks"]), 2)
        self.add("lib_trips01", "new.jpg", picture("teal"))
        ctx = Context()
        self.assertEqual(scan.looks_pending(self.conn, self.config, ctx,
                                            ["lib_family1", "lib_trips01"]), 1)
        self.assertIn("Update looks", ctx.text())
        # The scan job keeps the number with its summary (the Manage page's job line).
        result = {"libraries": [], "describe": None, "embed": None, "looks_pending": 1,
                  "stopped": False}
        self.assertEqual(jobs._scan_summary(result, {}, offline_fails=True)["looks_pending"], 1)
        result["looks_pending"] = None
        self.assertNotIn("looks_pending", jobs._scan_summary(result, {}, offline_fails=True))
        self.configure({"endpoint": ""})
        with self.assertRaises(jobs.JobError):
            jobs.run_image_embed(Context(), self.conn, self.config, {})
        self.assertIsNone(scan.looks_pending(self.conn, self.config, Context(), ["lib_family1"]))
        import admin
        self.assertEqual(admin.JOB_PARAMS["image_embed"], ("limit", "rebuild"))


class ExampleTest(LooksFixture, unittest.TestCase):

    class Servers:
        """A stand-in for the model server manager."""

        def __init__(self, state="ready"):
            self.state = state
            self.touched = []

        def start_for_queries(self, config, name):
            return self.state, "The model server is loading its model."

        def start_scheduler(self, loader):
            pass

        def touch(self, name):
            self.touched.append(name)

    def test_an_example_picture_is_embedded_like_the_pictures(self):
        self.update()
        servers = self.Servers()
        examples = looks.ExampleEmbedder(servers=servers)
        status, payload = examples.answer(self.config, picture("red", (640, 480)), "me")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["dims"], self.dims)
        self.assertTrue(payload["matches"])
        self.assertEqual(payload["model"], looks.look_id(looks.recipe(self.config)))
        store = looks.connect()
        self.addCleanup(store.close)
        red = looks.vectors_for(store, [self.key(self.red)])[self.key(self.red)]
        self.assertGreater(embeddings.signed_cosine(base64.b64decode(payload["vec"]), red), 0.99)
        self.assertEqual(servers.touched, ["looks"])
        # A small picture of it, for the page to show (it may not show a HEIC).
        self.assertTrue(payload["thumb"].startswith("data:image/jpeg;base64,"))
        with Image.open(io.BytesIO(base64.b64decode(payload["thumb"].split(",", 1)[1]))) as chip:
            self.assertEqual((chip.format, max(chip.size)), ("JPEG", images.EXAMPLE_CHIP_EDGE))
        self.assertEqual(examples.answer(self.config, b"", "me")[0], 400)
        status, payload = examples.answer(self.config, b"no picture", "me")
        self.assertEqual(status, 415)
        self.assertIn("cannot be compared", payload["error"])
        status, payload = examples.answer(self.config, picture("red", (640, 480))[:300], "me")
        self.assertEqual(status, 400)
        self.assertIn("cannot be read", payload["error"])
        with mock.patch.object(looks, "QUERY_MAX_BYTES", 10):
            self.assertEqual(examples.answer(self.config, picture("red"), "me")[0], 413)
        status, payload = looks.ExampleEmbedder(servers=self.Servers("starting")).answer(
            self.config, picture("red"), "me")
        self.assertEqual((status, payload["state"]), (503, "starting"))
        limited = looks.ExampleEmbedder(servers=servers, burst=1, rate=0)
        self.assertEqual(limited.answer(self.config, picture("red"), "me")[0], 200)
        self.assertEqual(limited.answer(self.config, picture("red"), "me")[0], 429)
        self.assertEqual(limited.answer(self.config, picture("red"), "other")[0], 200)
        self.configure({"endpoint": ""})
        status, payload = examples.answer(self.config, picture("red"), "me")
        self.assertEqual((status, payload["state"]), (404, "off"))
        # Kept looks of other settings: the vector comes, but does not match them.
        self.configure({"max_edge": 512})
        self.assertFalse(examples.answer(self.config, picture("red"), "me")[1]["matches"])

    def test_only_common_kinds_of_picture_are_read(self):
        from PIL import EpsImagePlugin
        examples = looks.ExampleEmbedder(servers=self.Servers(), burst=100)
        postscript = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
                      b"newpath 0 0 moveto 10 10 lineto stroke\nshowpage\n")
        with mock.patch.object(EpsImagePlugin.EpsImageFile, "_open",
                               side_effect=AssertionError("EPS reader")) as eps:
            status, payload = examples.answer(self.config, postscript, "me")
            self.assertEqual(status, 415)
            self.assertIn("JPEG, PNG", payload["error"])
            # Named a JPEG (it starts like one): only the JPEG reader tries it.
            status, payload = examples.answer(self.config, b"\xff\xd8\xff\xe0" + postscript,
                                              "me")
            self.assertEqual(status, 400)
            self.assertFalse(eps.called)
        for name, options in (("TGA", {}), ("PCX", {}), ("PPM", {})):
            buffer = io.BytesIO()
            Image.new("RGB", (32, 24), "red").save(buffer, name, **options)
            self.assertEqual(examples.answer(self.config, buffer.getvalue(), "me")[0], 415,
                             name)
        for name in ("PNG", "WEBP", "GIF", "BMP", "TIFF"):
            buffer = io.BytesIO()
            Image.new("RGB", (32, 24), "red").save(buffer, name)
            self.assertEqual(examples.answer(self.config, buffer.getvalue(), "me")[0], 200,
                             name)
        # More pixels than may be decoded for an example (a JPEG is decoded at a
        # fraction of its size, so it may have more).
        buffer = io.BytesIO()
        Image.new("RGB", (400, 300), "red").save(buffer, "PNG")
        with mock.patch.object(images, "EXAMPLE_MAX_PIXELS", 100000):
            status, payload = examples.answer(self.config, buffer.getvalue(), "me")
            self.assertEqual(status, 413)
            self.assertIn("too large", payload["error"])
            self.assertEqual(examples.answer(self.config, picture("red", (400, 300)), "me")[0],
                             200)

    def test_a_360_photo_is_seen_as_its_front_view(self):
        import imagegen
        self.update()
        panorama = imagegen.equirect_image((1024, 512), marker=True)
        flat = imagegen.jpeg_bytes(panorama)
        dropped = imagegen.jpeg_with_xmp(panorama, imagegen.gpano_xmp())
        # Prepared as the library's 360 photos are: the front view of the panorama.
        prepared = images.prepare_example_for_looks(dropped, 1024)
        expected = images.prepare_for_looks(flat, 1024, panorama=True)
        with Image.open(io.BytesIO(prepared)) as one, Image.open(io.BytesIO(expected)) as two:
            self.assertEqual(one.size, two.size)
            self.assertEqual(one.size[0] * 3, one.size[1] * 4)
        # A 2:1 picture that does not say it is a 360 photo is seen whole.
        with Image.open(io.BytesIO(images.prepare_example_for_looks(flat, 1024))) as whole:
            self.assertEqual(whole.size, (1024, 512))
        # Its vector is the one of the same panorama in the library.
        pano = self.add("lib_trips01", "pano2.jpg", flat, projection="equirectangular")
        self.update()
        status, payload = looks.ExampleEmbedder(servers=self.Servers()).answer(
            self.config, dropped, "me")
        self.assertEqual(status, 200)
        store = looks.connect()
        self.addCleanup(store.close)
        kept = looks.vectors_for(store, [self.key(pano)])[self.key(pano)]
        self.assertGreater(embeddings.signed_cosine(base64.b64decode(payload["vec"]), kept), 0.99)


@unittest.skipUnless(flask is not None and support.pillow_available(),
                     "Flask or Pillow is not installed")
class ServerTest(LooksFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import serve
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.state = self.app.extensions["search_images"]
        self.addCleanup(self.state.close)
        self.state.examples = looks.ExampleEmbedder(servers=ExampleTest.Servers())
        self.client = self.app.test_client()

    def looks(self, url="/api/looks", **kwargs):
        response = self.client.get(url, **kwargs)
        self.assertEqual(response.status_code, 200)
        return response, support.strict_json(response.get_data(as_text=True))

    def test_looks_follow_what_the_caller_may_see(self):
        _response, payload = self.looks()
        self.assertEqual((payload["ids"], payload["data"], payload["dims"]), ([], "", 0))
        self.update()
        response, payload = self.looks(environ_base=REMOTE)
        self.assertEqual(payload["ids"], [self.red, self.copy, self.dark_red, self.video,
                                          self.pano])
        self.assertEqual((payload["dims"], payload["query"], payload["count"]),
                         (self.dims, False, 5))
        self.assertTrue(payload["model"].startswith("l-"))
        data = base64.b64decode(payload["data"])
        self.assertEqual(data[:self.dims], data[self.dims:2 * self.dims])  # the copy shares it
        self.assertIn("private", response.headers["Cache-Control"])
        # The admin (this computer) also gets the hidden picture's, and may
        # search by an example picture.
        response, payload = self.looks()
        self.assertIn(self.hidden, payload["ids"])
        self.assertTrue(payload["query"])
        _response, payload = self.looks("/api/looks?lib=lib_trips01", environ_base=REMOTE)
        self.assertEqual(payload["ids"], [self.pano])
        self.assertEqual(self.client.get("/api/looks?lib=nope").status_code, 404)
        # ETag, 304 and gzip.
        response = self.client.get("/api/looks", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertIn(self.hidden, json.loads(gzip.decompress(response.get_data()))["ids"])
        again = self.client.get("/api/looks", headers={"If-None-Match": response.headers["ETag"]})
        self.assertEqual(again.status_code, 304)
        # Hiding a picture changes the answer.
        self.conn.execute("UPDATE images SET hidden = 1, updated = ? WHERE id = ?",
                          (NOW + 5, self.red))
        self.conn.commit()
        builds = self.state.looks.builds
        _response, payload = self.looks(environ_base=REMOTE)
        self.assertNotIn(self.red, payload["ids"])
        self.assertEqual(self.state.looks.builds, builds + 1)
        self.looks(environ_base=REMOTE)
        self.assertEqual(self.state.looks.builds, builds + 1)       # (the same answer kept)
        # A store written by a newer version: no looks, and the file is left as it is.
        store = looks.connect()
        store.execute("UPDATE meta SET value = '99' WHERE key = 'version'")
        store.commit()
        store.close()
        _response, payload = self.looks(environ_base=REMOTE)
        self.assertEqual((payload["ids"], payload["dims"]), ([], 0))
        self.assertIsNone(looks.read_meta())
        conn = sqlite3.connect(looks.store_path())
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0], 5)

    def test_the_manage_page_counts_looks_and_queues_the_job(self):
        self.update(limit=1)
        admin_headers = {"X-Search-Images": "1"}
        state = self.client.get("/api/admin/state", headers=admin_headers).get_json()
        self.assertEqual((state["looks"]["configured"], state["looks"]["ready"],
                          state["looks"]["changed"], state["looks"]["stored"]),
                         (True, True, False, 1))
        views = {view["id"]: view for view in state["libraries"]}
        self.assertEqual(views["lib_trips01"]["looks"],
                         {"pictures": 1, "embedded": 1, "pending": 0, "views": 0})
        self.assertEqual(views["lib_family1"]["looks"]["pending"], 5)
        response = self.client.post("/api/admin/jobs", headers=admin_headers, json={
            "kind": "image_embed", "library_id": "lib_family1",
            "params": {"limit": 3, "rebuild": True}})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["job"]["params"], {"limit": 3, "rebuild": True})
        response = self.client.post("/api/admin/jobs", headers=admin_headers, json={
            "kind": "image_embed", "params": {"rebuild": "yes"}})
        self.assertEqual(response.status_code, 400)
        self.configure({"endpoint": ""})
        response = self.client.post("/api/admin/jobs", headers=admin_headers,
                                    json={"kind": "image_embed"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("image_embedding", response.get_json()["error"])

    def test_the_projector_of_an_embeddings_server_is_set_in_config_json_only(self):
        models = self.base.join("models")
        os.makedirs(models)
        for name in ("model.gguf", "model.mmproj.gguf", "mmproj-model.gguf"):
            with open(os.path.join(models, name), "wb") as handle:
                handle.write(b"GGUF")
        self.configure(local_server=[
            {"enabled": True, "kind": "embeddings", "endpoint": "looks", "binary": "llama-server",
             "port": 8097, "models_dir": models, "model": "model.gguf"},
            {"enabled": True, "endpoint": "describe", "binary": "llama-server", "port": 8098,
             "models_dir": models, "model": "model.gguf"}])
        admin_headers = {"X-Search-Images": "1"}

        def change(endpoint, settings):
            return self.client.post("/api/admin/model-server/settings", headers=admin_headers,
                                    json={"endpoint": endpoint, "settings": settings})
        response = change("looks", {"mmproj": "model.mmproj.gguf"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("config.json", response.get_json()["fields"]["mmproj"])
        self.assertNotIn("mmproj", common.load_config()["local_server"][0])
        # Unchanged is fine (the page sends what it shows), and a describe server's
        # projector is still chosen on the Settings page.
        self.assertEqual(change("looks", {"mmproj": "", "port": 8099}).status_code, 200)
        self.assertEqual(change("describe", {"mmproj": "mmproj-model.gguf"}).status_code, 200)
        self.assertEqual(common.load_config()["local_server"][1]["mmproj"], "mmproj-model.gguf")

    def query(self, data=b"", headers=None, **kwargs):
        sent = {"X-Search-Images": "1", "Content-Type": "image/jpeg"}
        sent.update(headers or {})
        return self.client.post("/api/looks/query", data=data,
                                headers={key: value for key, value in sent.items()
                                         if value is not None}, **kwargs)

    def test_search_by_an_example_picture_is_the_admins_only(self):
        self.update()
        response = self.query(picture("red"))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(len(base64.b64decode(payload["vec"])), self.dims)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.query(picture("red"), environ_base=REMOTE).status_code, 403)
        self.assertEqual(self.query(picture("red"), {"X-Search-Images": None}).status_code, 403)
        self.assertEqual(self.query(picture("red"), {"Sec-Fetch-Site": "cross-site"})
                         .status_code, 403)
        self.assertEqual(self.query(picture("red"), {"Origin": "http://evil.example"})
                         .status_code, 403)
        self.assertEqual(self.client.get("/api/looks/query").status_code, 405)
        self.assertEqual(self.query(b"no picture").status_code, 415)
        self.assertEqual(self.query(picture("red")[:200]).status_code, 400)
        with mock.patch.object(looks, "QUERY_MAX_BYTES", 100):
            self.assertEqual(self.query(picture("red")).status_code, 413)
        # Nothing about the picture is kept in the data folder.
        names = []
        for _folder, _dirs, files in os.walk(self.base.join("data")):
            names.extend(files)
        self.assertFalse([name for name in names if "example" in name or "query" in name])


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class ExportTest(LooksFixture, unittest.TestCase):

    def test_the_looks_option_ships_the_exported_pictures_looks(self):
        import cache
        import export
        thumbs = cache.ThumbStore()
        self.addCleanup(thumbs.close)
        for image_id in (self.red, self.copy, self.dark_red, self.hidden, self.video):
            thumbs.put(image_id, support.gradient_jpeg(32, 24))
        options = {"images": "none", "exclude_undescribed": False}
        preview = export.preview_export(self.conn, self.config, "lib_family1", options,
                                        reachable=True)
        self.assertEqual(preview["counts"]["looks"], {"vectors": 0, "missing": 4})
        self.update()
        preview = export.preview_export(self.conn, self.config, "lib_family1", options,
                                        reachable=True)
        self.assertEqual(preview["counts"]["looks"], {"vectors": 4, "missing": 0})
        self.assertEqual(preview["size_parts"]["looks"], 4 * export.vector_bytes(self.dims))
        out = self.base.join("site")
        summary = export.export_library(Context(), self.conn, self.config, "lib_family1", out,
                                        options)
        self.assertEqual((summary["count"], summary["looks"]), (4, 4))
        with open(os.path.join(out, "data", "manifest.js"), encoding="ascii") as handle:
            manifest = json.loads(handle.read().split("=", 1)[1].strip().rstrip(";"))
        self.assertTrue(manifest["looks"])
        parts = []
        for name in manifest["looks"]:
            with open(os.path.join(out, "data", name), encoding="ascii") as handle:
                text = handle.read()
            self.assertTrue(text.startswith("SEARCH_LOOKS.push("))
            parts.append(json.loads(text[len("SEARCH_LOOKS.push("):-3]))
        ids = [xid for part in parts for xid in part["ids"]]
        self.assertEqual(len(ids), 4)
        self.assertEqual({part["dims"] for part in parts}, {self.dims})
        self.assertEqual({part["model"] for part in parts},
                         {looks.look_id(looks.recipe(self.config))})
        for image_id in (self.red, self.hidden):
            self.assertNotIn(str(image_id), ids)    # export ids, never database ids
        # A second export writes nothing; the option off removes the files.
        again = export.export_library(Context(), self.conn, self.config, "lib_family1", out,
                                      options)
        self.assertEqual(again["written"], 0)
        options["looks"] = False
        export.export_library(Context(), self.conn, self.config, "lib_family1", out, options)
        self.assertFalse([name for name in os.listdir(os.path.join(out, "data"))
                          if name.startswith("looks-")])


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class NodeTest(unittest.TestCase):

    def run_script(self, script):
        result = subprocess.run([shutil.which("node"), script], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)

    def test_looks_in_the_page_scripts(self):
        self.run_script(JS_TESTS)

    def test_search_by_an_example_picture_on_the_search_page(self):
        self.run_script(APP_JS_TESTS)


if __name__ == "__main__":
    unittest.main()
