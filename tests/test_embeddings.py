"""Tests for search by meaning: embeddings.py, llm.embed, the embed job and step,
/api/vectors and /api/embed, the embeddings model server and the export's
vector files.

The embedding model is tests/fake_embed_server.py on 127.0.0.1 (texts that
share words get close vectors); every test runs in a temporary
SETTINGS_BASE.
"""

import base64
import gzip
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

import fake_embed_server

import common
import db
import embeddings
import image_edits
import jobs
import llm
import local_server
import scan

try:
    import flask
except ImportError:
    flask = None

NOW = 1700000000.0
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}
quiet = support.quiet


def signed(data):
    """The signed numbers of a stored vector (a list of ints)."""
    return [value - 256 if value > 127 else value for value in bytearray(data)]


def cosine(first, second):
    """The cosine of two stored vectors (about: they are rounded)."""
    return sum(a * b for a, b in zip(signed(first), signed(second))) / float(127 * 127)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def info(title, description, **fields):
    """A version 5 description (only what the tests need)."""
    entry = {"title": title, "description": description, "photo_type": "photo",
             "document_type": "none", "text": "", "setting": "outdoor", "scene": "",
             "time_of_day": "day", "event": "none", "people": "none",
             "people_descriptors": [], "subjects": [], "animals": [], "food": [],
             "vehicles": [], "colors": [], "tags": [], "quality_flags": [],
             "sensitive": "none", "private_info": False}
    entry.update(fields)
    return entry


def insert_image(conn, library_id, path, description=None, **fields):
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 64, "height": 48,
           "added": NOW, "updated": NOW}
    if description is not None:
        row.update(info=json.dumps(description), described=1, prompt_ver=5)
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
        self.assertEqual(embeddings.settings({}), embeddings.DEFAULTS)
        options = embeddings.settings({"embedding": {
            "endpoint": " vec ", "model": " m ", "dims": -1, "batch": 1000, "allow_cloud": "yes",
            "query_prefix": ""}})
        self.assertEqual(options["endpoint"], "vec")
        self.assertEqual(options["model"], "m")
        self.assertEqual(options["dims"], 256)
        self.assertEqual(options["batch"], 32)
        self.assertFalse(options["allow_cloud"])
        self.assertEqual(options["query_prefix"], "")
        options = embeddings.settings({"embedding": {"dims": 0, "batch": 32, "allow_cloud": True}})
        self.assertEqual((options["dims"], options["batch"], options["allow_cloud"]),
                         (0, 32, True))
        self.assertFalse(embeddings.configured({"embedding": {"endpoint": " "}}))

    def test_which_endpoints_can_be_used(self):
        endpoints = [{"name": "here", "preset": "llamacpp", "base_url": "http://127.0.0.1:1/v1"},
                     {"name": "off", "preset": "llamacpp", "enabled": False},
                     {"name": "claude", "preset": "anthropic"},
                     {"name": "cloud", "preset": "openai", "model": "text-embedding-3-small"}]

        def problem(name, **extra):
            config = {"endpoints": endpoints, "embedding": dict({"endpoint": name}, **extra)}
            return embeddings.endpoint_for(config)

        self.assertEqual(problem("")[1], embeddings.NOT_CONFIGURED)
        self.assertIn("embedding.endpoint", problem("nothing")[1])
        self.assertIn("turned off", problem("off")[1])
        self.assertIn("cannot make embeddings", problem("claude")[1])
        self.assertIn("allow_cloud", problem("cloud")[1])
        self.assertEqual(problem("cloud", allow_cloud=True)[0]["name"], "cloud")
        endpoint, why = problem("HERE")
        self.assertEqual((endpoint["name"], why), ("here", ""))
        options = embeddings.settings({"embedding": {"endpoint": "here"}})
        self.assertEqual(embeddings.model_key(endpoint, options), "here")
        self.assertEqual(embeddings.model_key(problem("cloud", allow_cloud=True)[0], options),
                         "text-embedding-3-small")
        options["model"] = "chosen"
        self.assertEqual(embeddings.model_key(endpoint, options), "chosen")


class SearchTextTest(unittest.TestCase):

    def test_what_the_description_shows_and_nothing_else(self):
        described = info(
            "Kids sledding", "Two children ride a red sled down a snowy hill.",
            tags=["snow", "sled", "winter"], subjects=["children", "sled"], animals=["dog"],
            food=[], vehicles=[], people_descriptors=["child in a blue coat"],
            scene="snowy hill", setting="outdoor", time_of_day="sunrise_sunset",
            event="none", colors=["white", "red"], photo_type="photo",
            document_type="none", text="SLOW " * 300,
            objects=[{"name": "sled", "box": [1, 2, 3, 4]}], activities=["sledding"],
            landmarks=["Mount Example"], location_guess="Somewhere, Example Country",
            mood="joyful", _model_name="your-model.gguf", private_info=False)
        text = embeddings.search_text(described)
        for wanted in ("Kids sledding.", "Two children ride a red sled", "Tags: snow, sled, "
                       "winter.", "children", "dog", "child in a blue coat", "Objects: sled.",
                       "Activities: sledding.", "Scene: snowy hill, outdoor, sunrise or sunset.",
                       "Kind: photo.", "Colors: white, red.", "Text in the picture: SLOW"):
            self.assertIn(wanted, text)
        for unwanted in ("Mount Example", "Somewhere", "Example Country", "your-model",
                         "joyful", "box", "none", "unknown"):
            self.assertNotIn(unwanted, text)
        ocr = text.split("Text in the picture: ")[1]
        self.assertLessEqual(len(ocr), embeddings.OCR_CHARS)
        self.assertTrue(ocr.endswith("SLOW"))             # cut at a word
        self.assertEqual(embeddings.search_text(None), "")
        self.assertEqual(embeddings.search_text({}), "")
        self.assertEqual(embeddings.search_text(info("", "")).splitlines(),
                         ["Scene: outdoor, day.", "Kind: photo."])
        # A document's kind, an event.
        text = embeddings.search_text(info("Receipt", "A shop receipt.",
                                           photo_type="document", document_type="receipt",
                                           event="birthday_party"))
        self.assertIn("Kind: document, receipt.", text)
        self.assertIn("Event: birthday party.", text)
        # An event of "other" says nothing.
        self.assertNotIn("Event", embeddings.search_text(info("Fire", "A fire.", event="other")))

    def test_a_video_is_a_video(self):
        # The model saw a few of its frames: the kind is "video", as the
        # search records and the info pane say (videos.effective_info).
        described = info("Fireworks", "Rockets burst over a lake.", photo_type="photo")
        text = embeddings.search_text(described, media="video")
        self.assertIn("Kind: video.", text)
        self.assertNotIn("photo", text)
        self.assertEqual(described["photo_type"], "photo")      # (not changed)
        self.assertIn("Kind: photo.", embeddings.search_text(described, media="image"))
        self.assertIn("Kind: photo.", embeddings.search_text(described))
        # The model's own kind follows when it says more than "photo": a
        # screen recording still reads as a screenshot.
        recording = info("Phone screen", "Scrolling through a chat.", photo_type="screenshot",
                         document_type="chat")
        self.assertIn("Kind: video, screenshot, chat.",
                      embeddings.search_text(recording, media="video"))
        self.assertIn("Kind: video.", embeddings.search_text(
            info("Clip", "Something.", photo_type="other"), media="video"))
        # Only the owner's edit: still a video.
        self.assertEqual(embeddings.search_text(None, {"title": "Mine", "description": "",
                                                       "tags_add": [], "tags_remove": []},
                                                media="video"),
                         "Mine.\nKind: video.")

    def test_no_position_written_in_the_text_goes_in(self):
        # A dashcam prints its position over its video and the model read it:
        # the vector holds none (an export ships vectors whatever its
        # location options are). The positions here are made up.
        described = info("Dashcam clip", "A road at S 41.20000 W 150.30000 by the sea.",
                         text="DASHCAM 2023/08/17 04:00:30 S:41.20000 W:150.30000 47MPH",
                         scene="a coastal road at -41.2000, -150.3000")
        text = embeddings.search_text(described)
        for position in ("41.2", "150.3", "S:", "W:"):
            self.assertNotIn(position, text)
        self.assertIn("Text in the picture: DASHCAM 2023/08/17 04:00:30 47MPH", text)
        self.assertIn("A road at by the sea.", text)
        self.assertIn("Scene: a coastal road at", text)
        # Other numbers stay.
        self.assertIn("SPEED 47 KM 120.5", embeddings.search_text(
            info("Sign", "A sign.", text="SPEED 47 KM 120.5")))

    def test_the_owner_edits_are_laid_over(self):
        described = info("Model title", "The model's words.", tags=["beach", "dog"])
        edit = {"title": "Rex at the beach", "description": None, "tags_add": ["rex"],
                "tags_remove": ["dog"]}
        text = embeddings.search_text(described, edit)
        self.assertIn("Rex at the beach.", text)
        self.assertIn("The model's words.", text)
        self.assertIn("Tags: rex, beach.", text)
        self.assertNotIn("Model title", text)
        # Only an edit, no description: still something to embed.
        self.assertEqual(embeddings.search_text(None, {"title": "Mine", "description": "",
                                                       "tags_add": [], "tags_remove": []}),
                         "Mine.")

    def test_the_hash_changes_with_the_text_the_model_and_the_size(self):
        first = embeddings.text_hash("a", "m", 256)
        self.assertEqual(first, embeddings.text_hash("a", "m", 256))
        self.assertNotEqual(first, embeddings.text_hash("b", "m", 256))
        self.assertNotEqual(first, embeddings.text_hash("a", "n", 256))
        self.assertNotEqual(first, embeddings.text_hash("a", "m", 128))


class VectorTest(unittest.TestCase):

    def test_quantize_cuts_normalises_and_rounds(self):
        data = embeddings.quantize([3.0, 4.0, 100.0], dims=2)
        self.assertEqual(signed(data), [76, 102])     # 0.6, 0.8 times 127
        self.assertEqual(signed(embeddings.quantize([-1.0, 0.0])), [-127, 0])
        self.assertEqual(len(embeddings.quantize([1.0] * 10, dims=0)), 10)
        for bad in ([], [0.0, 0.0]):
            with self.assertRaises(ValueError):
                embeddings.quantize(bad)
        # Fewer numbers than dims asks for: say so (not a shorter vector).
        with self.assertRaises(embeddings.SizeError) as caught:
            embeddings.quantize([1.0] * 10, dims=16)
        self.assertIn("gives 10 numbers per text", str(caught.exception))
        self.assertEqual(caught.exception.native, 10)
        vector = fake_embed_server.word_vector("a dog on a sofa")
        same = embeddings.quantize(vector, 32)
        self.assertAlmostEqual(cosine(same, same), 1.0, delta=0.02)
        other = embeddings.quantize(fake_embed_server.word_vector("birthday cake"), 32)
        self.assertLess(cosine(same, other), 0.9)

    def test_the_size_and_the_model_of_stored_vectors(self):
        current = {"dims": 0, "native_dims": 1024, "served": "model-a"}
        self.assertEqual(embeddings.vector_size(current), 1024)
        self.assertEqual(embeddings.vector_size(dict(current, dims=256)), 256)
        self.assertEqual(embeddings.vector_size(None), 0)
        self.assertEqual(embeddings.model_changed(current, 1024, "model-a"), "")
        self.assertEqual(embeddings.model_changed(current, 1024, ""), "")
        self.assertIn("768 numbers per text, not 1024",
                      embeddings.model_changed(current, 768, "model-a"))
        self.assertIn("serves model-b, not model-a",
                      embeddings.model_changed(current, 1024, "model-b"))
        # (Nothing known yet: nothing changed.)
        self.assertEqual(embeddings.model_changed({"native_dims": 0, "served": ""}, 8, "x"), "")


class StoreTest(support.TempBaseMixin, unittest.TestCase):

    def test_the_store_and_its_model(self):
        self.assertIsNone(embeddings.connect(create=False))
        store = embeddings.connect()
        self.addCleanup(store.close)
        meta = embeddings.meta(store)
        self.assertEqual((meta["version"], meta["model"], meta["dims"], meta["generation"]),
                         (embeddings.VECTORS_VERSION, "", 0, 0))
        self.assertEqual(len(meta["epoch"]), 16)
        self.assertFalse(embeddings.prepare_model(store, "m", 4))
        embeddings.write(store, [(1, "lib_a", "h1", b"\x01\x02\x03\x04"),
                                 (2, "lib_b", "h2", b"\x05\x06\x07\x08")], native_dims=16)
        self.assertEqual(embeddings.stored(store), {1: ("lib_a", "h1"), 2: ("lib_b", "h2")})
        self.assertEqual(embeddings.stored(store, ["lib_b"]), {2: ("lib_b", "h2")})
        self.assertEqual(embeddings.meta(store)["native_dims"], 16)
        generation = embeddings.meta(store)["generation"]
        self.assertGreater(generation, 0)
        self.assertEqual(embeddings.remove(store, [2, 3]), 1)
        self.assertGreater(embeddings.meta(store)["generation"], generation)
        # Another model (or size) empties the store.
        self.assertFalse(embeddings.prepare_model(store, "m", 4))
        self.assertTrue(embeddings.prepare_model(store, "m", 8))
        self.assertEqual(embeddings.stored(store), {})
        self.assertEqual(embeddings.meta(store)["dims"], 8)

    def test_a_file_of_another_layout_is_emptied(self):
        store = embeddings.connect()
        embeddings.write(store, [(1, "lib_a", "h", b"\x01")])
        epoch = embeddings.meta(store)["epoch"]
        store.execute("UPDATE meta SET value = '99' WHERE key = 'version'")
        store.commit()
        store.close()
        store = embeddings.connect()
        self.addCleanup(store.close)
        self.assertEqual(embeddings.stored(store), {})
        self.assertNotEqual(embeddings.meta(store)["epoch"], epoch)


class EmbedFixture(support.TempBaseMixin):
    """Two libraries with described pictures, and the fake embedding server."""

    dims = 16

    def setUp(self):
        super().setUp()
        self.server = fake_embed_server.FakeEmbedder()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.family = self.base.library_folder("family")
        self.trips = self.base.library_folder("trips")
        self.configure()
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.dog = insert_image(self.conn, "lib_family1", "pets/rex-2019-05-14.jpg",
                                info("Dog asleep on a sofa", "A brown dog sleeps on a sofa.",
                                     tags=["dog", "sofa"]),
                                lat=48.85, lon=2.35, camera="Acme One",
                                taken_at="2019-05-14T10:00:00", place_city="Paris",
                                place_country="France")
        self.cake = insert_image(self.conn, "lib_family1", "party/cake.jpg",
                                 info("Birthday cake", "A cake with candles.",
                                      tags=["cake", "candles"]))
        self.hidden = insert_image(self.conn, "lib_family1", "private/hidden.jpg",
                                   info("Hidden picture", "Something private."), hidden=1)
        self.bare = insert_image(self.conn, "lib_family1", "new/undescribed.jpg")
        self.gone = insert_image(self.conn, "lib_family1", "old/missing.jpg",
                                 info("Missing", "A picture that is not there now."),
                                 missing_since=NOW)
        self.beach = insert_image(self.conn, "lib_trips01", "beach.jpg",
                                  info("Quiet beach at sunset", "Waves on an empty beach.",
                                       tags=["beach", "sunset"]))

    def endpoint(self, **extra):
        entry = {"name": "vec", "preset": "llamacpp", "base_url": self.server.base_url}
        entry.update(extra)
        return entry

    def configure(self, embedding=None, endpoint=None, libraries=None, **extra):
        settings = {"endpoint": "vec", "dims": self.dims, "batch": 2}
        settings.update(embedding or {})
        self.base.write_config(libraries or [
            support.library("Family", self.family, id="lib_family1"),
            support.library("Trips", self.trips, id="lib_trips01"),
        ], endpoints=[endpoint or self.endpoint()], embedding=settings, **extra)
        self.config = common.load_config()
        return self.config

    def update(self, ctx=None, **kwargs):
        return embeddings.update(self.conn, self.config, ctx or Context(),
                                 sleep=lambda _seconds: None, **kwargs)

    def stored(self):
        store = embeddings.connect()
        try:
            return embeddings.stored(store), embeddings.meta(store)
        finally:
            store.close()


class UpdateTest(EmbedFixture, unittest.TestCase):

    def test_described_pictures_are_embedded_once(self):
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["failed"], summary["pending"]), (4, 0, 4))
        self.assertEqual((summary["model"], summary["dims"]), ("vec", self.dims))
        stored, meta = self.stored()
        self.assertEqual(sorted(stored), sorted([self.dog, self.cake, self.hidden, self.beach]))
        self.assertEqual(stored[self.beach][0], "lib_trips01")
        self.assertEqual((meta["model"], meta["dims"], meta["native_dims"]),
                         ("vec", self.dims, fake_embed_server.DIMS))
        # The probe texts first, then batches of two, newest pictures first;
        # no file names, folders, dates, places or cameras in what was sent.
        sizes = [len(payload["input"]) for payload in self.server.embed_requests()]
        self.assertEqual(sizes, [len(embeddings.PROBE_TEXTS), 2, 2])
        self.assertEqual(self.server.embed_requests()[0]["input"], list(embeddings.PROBE_TEXTS))
        self.assertEqual([item["text"] for item in self.stored()[1]["probe"]],
                         list(embeddings.PROBE_TEXTS))
        sent = "\n".join(self.server.texts())
        for private in ("rex", "pets", "2019", "Paris", "France", "Acme", "48.85", ".jpg",
                        "Family", "Trips"):
            self.assertNotIn(private, sent)
        self.assertNotIn("model", self.server.embed_requests()[0])
        # Nothing changed: nothing is asked of the server.
        before = len(self.server.requests)
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["up_to_date"], summary["pending"]),
                         (0, 4, 0))
        self.assertEqual(len(self.server.requests), before)
        store = embeddings.connect()
        self.addCleanup(store.close)
        vec = store.execute("SELECT vec FROM vectors WHERE image_id = ?",
                            (self.dog,)).fetchone()[0]
        self.assertEqual(len(vec), self.dims)
        expected = embeddings.quantize(fake_embed_server.word_vector(
            embeddings.library_texts(self.conn, ["lib_family1"])[0][3]), self.dims)
        self.assertEqual(bytes(vec), expected)

    def test_a_described_video_is_embedded_as_a_video(self):
        described = info("Fireworks over a lake", "Rockets burst over the water.",
                         photo_type="artwork", tags=["fireworks"])
        clip = insert_image(self.conn, "lib_family1", "clips/2019-07-04 fireworks.mp4",
                            described, media="video", duration=12.5,
                            taken_at="2019-07-04T21:30:00")
        # A vector made before (from the model's kind) is out of date.
        old_text = embeddings.search_text(described)
        self.assertIn("Kind: artwork.", old_text)
        store = embeddings.connect()
        embeddings.prepare_model(store, "vec", self.dims)
        embeddings.write(store, [(clip, "lib_family1", embeddings.text_hash(old_text, "vec",
                                                                            self.dims),
                                  embeddings.quantize([1.0] * self.dims))])
        store.close()
        self.assertEqual(embeddings.counts(self.conn, self.config, ["lib_family1"])
                         ["lib_family1"]["stale"], 1)
        self.assertEqual(self.update()["embedded"], 5)
        sent = [text for text in self.server.texts() if "Fireworks" in text]
        self.assertEqual(len(sent), 1)
        self.assertIn("Kind: video, artwork.", sent[0])
        for private in ("2019", "07-04", "21:30", "12.5", ".mp4", "clips"):
            self.assertNotIn(private, sent[0])
        text = dict((row[0], row[3]) for row in
                    embeddings.library_texts(self.conn, ["lib_family1"]))[clip]
        self.assertEqual(text, sent[0])
        self.assertEqual(self.stored()[0][clip][1],
                         embeddings.text_hash(text, "vec", self.dims))
        # Up to date for the Manage page and an export too.
        self.assertEqual(embeddings.counts(self.conn, self.config, ["lib_family1"],
                                           hashes=embeddings.TextHashes())
                         ["lib_family1"]["stale"], 0)
        found = embeddings.fresh_vectors(self.conn, "lib_family1", [clip])
        self.assertEqual((len(found["vectors"]), found["stale"]), (1, 0))

    def test_only_what_changed_is_embedded_again(self):
        self.update()
        image_edits.save(self.conn, self.cake, {"title": "Grandma's birthday cake"})
        self.conn.execute("UPDATE images SET info = '', described = 0 WHERE id = ?", (self.dog,))
        self.conn.execute("DELETE FROM images WHERE id = ?", (self.beach,))
        self.conn.commit()
        self.server.requests.clear()
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["removed"]), (1, 2))
        self.assertEqual(len(self.server.texts()), 1 + len(embeddings.PROBE_TEXTS))
        self.assertIn("Grandma's birthday cake.", self.server.texts()[-1])
        stored, _meta = self.stored()
        self.assertEqual(sorted(stored), sorted([self.cake, self.hidden]))

    def test_a_missing_picture_keeps_its_vector(self):
        self.update()
        self.conn.execute("UPDATE images SET missing_since = ? WHERE id = ?", (NOW, self.cake))
        self.conn.commit()
        summary = self.update()
        self.assertEqual(summary["removed"], 0)
        self.assertIn(self.cake, self.stored()[0])

    def test_another_model_or_size_embeds_everything_again(self):
        self.update()
        self.configure(embedding={"dims": 8})
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual(summary["embedded"], 4)
        self.assertIn("every picture is embedded again", ctx.text())
        self.assertEqual(self.stored()[1]["dims"], 8)
        self.configure(embedding={"dims": 8, "model": "other"})
        self.server.requests.clear()
        self.assertEqual(self.update()["embedded"], 4)
        self.assertEqual(self.server.embed_requests()[0]["model"], "other")

    def test_hidden_pictures_only_go_to_endpoints_on_this_network(self):
        # Not a real key: a made-up value used only by this test.
        key = "sk-test-not-a-real-key-1234567890"
        self.configure(embedding={"allow_cloud": True},
                       endpoint=self.endpoint(base_url="https://embed.example.com/v1",
                                              preset="custom", api_key=key))
        ctx = Context()
        with mock.patch.object(llm, "embed_with_retries") as call:
            call.side_effect = lambda endpoint, texts, **kwargs: {
                "vectors": [fake_embed_server.word_vector(text) for text in texts],
                "tokens": 1}
            summary = self.update(ctx)
        sent = [text for args, _kwargs in call.call_args_list for text in args[1]]
        self.assertEqual(summary["skipped_hidden"], 1)
        self.assertEqual(summary["embedded"], 3)
        self.assertFalse(any("Hidden picture" in text for text in sent))
        self.assertIn("1 hidden picture(s) were not sent to embed.example.com", ctx.text())
        self.assertNotIn(key, ctx.text())

    def test_a_cloud_endpoint_needs_allow_cloud(self):
        self.configure(endpoint=self.endpoint(base_url="https://embed.example.com/v1",
                                              preset="custom"))
        ctx = Context()
        summary = self.update(ctx)
        self.assertIn("allow_cloud", summary["error"])
        self.assertEqual(self.server.requests, [])

    def test_a_text_the_server_refuses_is_left_for_later(self):
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (json.dumps(info("LONG text", "Far too long.")), self.cake))
        self.conn.commit()
        self.server.too_long = "LONG"
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["embedded"], summary["failed"]), (3, 1))
        self.assertNotIn(self.cake, self.stored()[0])
        self.assertIn("Picture %d could not be embedded" % self.cake, ctx.text())
        # Fixed (a shorter text, or a larger server): the next run does it.
        self.server.too_long = None
        self.assertEqual(self.update()["embedded"], 1)

    def test_a_server_that_still_loads_is_waited_for(self):
        self.server.queue(fake_embed_server.HttpError(503), fake_embed_server.HttpError(503))
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual(summary["embedded"], 4)
        self.assertIn("trying again", ctx.text())

    def test_an_endpoint_that_cannot_be_used_stops_the_run(self):
        self.server.queue(fake_embed_server.HttpError(404))
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual(summary["embedded"], 0)
        self.assertIn("does not know the model or the URL", summary["error"])
        self.server.stop()
        summary = self.update()
        self.assertIn("unavailable", summary["error"])

    def test_limit_and_stop(self):
        self.assertEqual(self.update(limit=1)["embedded"], 1)
        ctx = Context(stop_after=1)
        summary = self.update(ctx)
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["embedded"], 2)        # the batch it was doing
        self.assertEqual(len(self.stored()[0]), 3)
        self.assertEqual(self.update()["embedded"], 1)

    def test_only_enabled_and_chosen_libraries(self):
        summary = self.update(library_ids=["lib_trips01"])
        self.assertEqual(summary["embedded"], 1)
        self.configure(libraries=[
            support.library("Family", self.family, id="lib_family1", enabled=False),
            support.library("Trips", self.trips, id="lib_trips01")])
        self.assertEqual(self.update()["embedded"], 0)

    def test_not_set_up(self):
        self.configure(embedding={"endpoint": ""})
        summary = self.update()
        self.assertFalse(summary["configured"])
        self.assertIsNone(embeddings.after_run(self.conn, self.config, Context()))
        self.assertFalse(os.path.exists(embeddings.vectors_path()))

    def test_ollama_and_its_own_api(self):
        self.configure(endpoint=self.endpoint(preset="ollama", base_url=self.server.root_url,
                                              model="nomic-embed-text"))
        self.assertEqual(self.update()["embedded"], 4)
        self.assertEqual(self.server.requests[0]["path"], "/api/embed")
        self.assertEqual(self.server.embed_requests()[0]["model"], "nomic-embed-text")
        self.assertEqual(self.stored()[1]["model"], "nomic-embed-text")

    def test_counts_for_the_manage_page(self):
        counts = embeddings.counts(self.conn, self.config, ["lib_family1", "lib_trips01"])
        self.assertEqual(counts["lib_family1"], {"pictures": 3, "embedded": 0, "stale": 0,
                                                 "missing": 3, "held_back": 0})
        self.update()
        hashes = embeddings.TextHashes()
        ids = ["lib_family1", "lib_trips01"]
        self.assertEqual(embeddings.counts(self.conn, self.config, ids, hashes=hashes)
                         ["lib_family1"]["embedded"], 3)
        image_edits.save(self.conn, self.cake, {"description": "A chocolate cake."})
        # Only the edited picture's text is made again.
        with mock.patch.object(embeddings, "search_text",
                               wraps=embeddings.search_text) as made:
            counts = embeddings.counts(self.conn, self.config, ids, hashes=hashes)
        self.assertEqual(made.call_count, 1)
        self.assertEqual(counts["lib_family1"], {"pictures": 3, "embedded": 2, "stale": 1,
                                                 "missing": 0, "held_back": 0})
        self.assertEqual(counts["lib_trips01"]["embedded"], 1)
        self.assertEqual(counts, embeddings.counts(self.conn, self.config, ids))
        # A cloud endpoint never gets the hidden picture: it is not "to update".
        self.configure(embedding={"allow_cloud": True},
                       endpoint=self.endpoint(base_url="https://embed.example.com/v1",
                                              preset="custom", model="vec"))
        image_edits.save(self.conn, self.hidden, {"title": "Still private"})
        counts = embeddings.counts(self.conn, self.config, ids, hashes=hashes)
        self.assertEqual(counts["lib_family1"], {"pictures": 3, "embedded": 1, "stale": 1,
                                                 "missing": 0, "held_back": 1})

    def test_dims_0_keeps_every_number(self):
        self.configure(embedding={"dims": 0})
        self.assertEqual(self.update()["embedded"], 4)
        _stored, meta = self.stored()
        native = fake_embed_server.DIMS
        self.assertEqual((meta["dims"], meta["native_dims"], embeddings.vector_size(meta)),
                         (0, native, native))
        self.assertEqual(embeddings.counts(self.conn, self.config, ["lib_family1"])
                         ["lib_family1"]["embedded"], 3)
        found = embeddings.fresh_vectors(self.conn, "lib_family1", [self.dog, self.cake])
        self.assertEqual((len(found["vectors"]), found["dims"], found["stale"]), (2, native, 0))
        self.assertEqual(len(found["vectors"][self.dog]), native)

    def test_dims_larger_than_the_model_gives_is_an_error(self):
        self.configure(embedding={"dims": 128})
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual(summary["embedded"], 0)
        self.assertIn("gives %d numbers per text" % fake_embed_server.DIMS, summary["error"])
        self.assertIn("set \"dims\"", summary["error"])
        self.assertIn(summary["error"], ctx.text())
        self.assertEqual(self.stored()[0], {})

    def test_another_model_behind_the_same_endpoint_embeds_everything_again(self):
        self.update()
        _stored, meta = self.stored()
        self.assertEqual((meta["native_dims"], meta["served"]),
                         (fake_embed_server.DIMS, fake_embed_server.MODEL))
        # Another model is loaded behind the endpoint (same name, no model
        # set): the next run that has something to embed notices.
        self.server.dims, self.server.served = 48, "another-model"
        image_edits.save(self.conn, self.cake, {"title": "A new title"})
        ctx = Context()
        summary = self.update(ctx)
        self.assertIn("changed (the model now gives 48 numbers per text, not 64): every "
                      "picture is embedded again", ctx.text())
        self.assertEqual((summary["embedded"], summary["pending"]), (4, 4))
        stored, meta = self.stored()
        self.assertEqual((meta["native_dims"], meta["served"], len(stored)),
                         (48, "another-model", 4))
        # The same length under another name: noticed too.
        self.server.served = "third-model"
        image_edits.save(self.conn, self.cake, {"title": "Another title"})
        ctx = Context()
        self.assertEqual(self.update(ctx)["embedded"], 4)
        self.assertIn("now serves third-model, not another-model", ctx.text())
        # Nothing to embed: the model is not asked (and nothing is noticed).
        before = len(self.server.requests)
        self.assertEqual(self.update()["pending"], 0)
        self.assertEqual(len(self.server.requests), before)

    def test_the_automatic_step_gives_up_quickly_on_a_server_that_is_off(self):
        ctx = Context()
        down = {"model": None, "slots": None, "vision": None, "server": "",
                "reachable": False, "loading": False}
        with mock.patch.object(llm, "probe", return_value=down), \
                mock.patch.object(embeddings, "UNREACHABLE_WAIT", 0):
            summary = embeddings.after_run(self.conn, self.config, ctx)
        self.assertIn("Search by meaning was not updated: the embedding endpoint vec does not "
                      "answer", summary["error"])
        self.assertEqual(self.server.embed_requests(), [])
        warnings = [message for level, message in ctx.lines if level in ("warn", "error")]
        self.assertEqual(warnings, [summary["error"]])
        # Really closed: still one warning, and no minutes of retries.
        self.server.stop()
        ctx = Context()
        started = time.monotonic()
        with mock.patch.object(embeddings, "UNREACHABLE_WAIT", 0):
            summary = embeddings.after_run(self.conn, self.config, ctx)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("does not answer", summary["error"])
        self.assertNotIn("trying again", ctx.text())
        # The Update meaning search job still tries in earnest (and retries).
        summary = self.update()
        self.assertIn("unavailable", summary["error"])

    def test_deleted_rows_lose_their_vectors(self):
        import library_ops
        self.update()
        self.assertEqual(embeddings.forget([]), 0)
        library_ops.delete_library_data(self.conn, "lib_family1")
        self.assertEqual(sorted(self.stored()[0]), [self.beach])
        os.remove(embeddings.vectors_path())
        self.assertEqual(embeddings.forget([self.beach]), 0)
        self.assertFalse(os.path.exists(embeddings.vectors_path()))

    def test_files_missing_too_long_lose_their_vectors(self):
        if not support.pillow_available():
            self.skipTest("Pillow is not installed")
        store = embeddings.connect()
        embeddings.write(store, [(self.gone, "lib_family1", "h", b"\x01" * self.dims)])
        store.close()
        with open(os.path.join(self.family, "here.jpg"), "wb") as handle:
            handle.write(support.gradient_jpeg(32, 24))
        summary = scan.run(self.config, Context(), library_ids=["lib_family1"],
                           describe_images=False, allow_mass_removal=True, conn=self.conn,
                           embed_vectors=False)
        self.assertGreaterEqual(summary["libraries"][0]["purged"], 1)
        self.assertNotIn(self.gone, self.stored()[0])

    def test_fresh_vectors_for_an_export(self):
        self.update()
        image_edits.save(self.conn, self.cake, {"description": "A chocolate cake."})
        found = embeddings.fresh_vectors(self.conn, "lib_family1",
                                         [self.dog, self.cake, self.bare])
        self.assertEqual(sorted(found["vectors"]), [self.dog])
        self.assertEqual((found["stale"], found["missing"], found["dims"], found["model"]),
                         (1, 0, self.dims, "vec"))


class LlmEmbedTest(EmbedFixture, unittest.TestCase):

    def test_replies_are_checked_and_put_in_order(self):
        reply = llm.embed(self.endpoint(), ["one", "two words"], model="")
        self.assertEqual(len(reply["vectors"]), 2)
        self.assertEqual(reply["vectors"][1], fake_embed_server.word_vector("two words"))
        self.assertEqual(reply["model"], fake_embed_server.MODEL)
        with mock.patch.object(llm, "_parse_reply_json", return_value={"data": [
                {"index": 0, "embedding": [1.0, "x"]}]}):
            with self.assertRaises(llm.ProviderError) as caught:
                llm.embed(self.endpoint(), ["one"])
        self.assertEqual(caught.exception.kind, "content")
        with mock.patch.object(llm, "_parse_reply_json", return_value={"data": []}):
            with self.assertRaises(llm.ProviderError):
                llm.embed(self.endpoint(), ["one"])
        with self.assertRaises(llm.ProviderError) as caught:
            llm.embed({"name": "c", "preset": "anthropic"}, ["one"])
        self.assertEqual(caught.exception.kind, "endpoint")


class StepTest(EmbedFixture, unittest.TestCase):
    """The embed step after scans, describe and refine jobs, and its own job and CLI."""

    def test_after_a_describe_job(self):
        ctx = Context()
        summary = jobs.run_describe(ctx, self.conn, self.config, {})
        self.assertEqual(summary["embed"]["embedded"], 4)
        self.assertIn("Embedding 4 picture(s) for search by meaning", ctx.text())

    def test_after_a_scan(self):
        ctx = Context()
        summary = scan.run(self.config, ctx, describe_images=False, conn=self.conn)
        self.assertEqual(summary["embed"]["embedded"], 4)
        self.assertEqual(scan.run(self.config, Context(), describe_images=False,
                                  conn=self.conn)["embed"]["pending"], 0)

    def test_the_embed_job(self):
        summary = jobs.run_embed(Context(), self.conn, self.config,
                                 {"library_id": "lib_trips01", "limit": 5})
        self.assertEqual(summary["embedded"], 1)
        ctx = Context()
        jobs.run_embed(ctx, self.conn, self.config, {})
        self.assertIn("Embedded 3 picture(s)", ctx.text())
        ctx = Context()
        jobs.run_embed(ctx, self.conn, self.config, {})
        self.assertIn("Search by meaning is up to date (4 picture(s))", ctx.text())
        self.configure(embedding={"endpoint": ""})
        with self.assertRaises(jobs.JobError):
            jobs.run_embed(Context(), self.conn, self.config, {})

    def test_the_command_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(scan.main(["--embed", "--library", "Trips"]), 0)
            self.assertEqual(scan.main(["--embed", "--refine"]), 2)
        self.assertEqual(sorted(self.stored()[0]), [self.beach])
        self.configure(embedding={"endpoint": ""})
        with redirect_stdout(out):
            self.assertEqual(scan.main(["--embed"]), 1)


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(EmbedFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import serve
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def vectors(self, url="/api/vectors", **kwargs):
        response = self.client.get(url, **kwargs)
        self.assertEqual(response.status_code, 200)
        return response, support.strict_json(response.get_data(as_text=True))

    def test_vectors_follow_what_the_caller_may_see(self):
        _response, payload = self.vectors()
        self.assertEqual(payload["ids"], [])
        self.assertEqual(payload["data"], "")
        self.update()
        response, payload = self.vectors(environ_base=REMOTE)
        self.assertEqual(payload["ids"], [self.dog, self.cake, self.beach])
        self.assertEqual((payload["model"], payload["dims"], payload["query"]),
                         ("vec", self.dims, True))
        self.assertEqual(len(base64.b64decode(payload["data"])), 3 * self.dims)
        self.assertIn("private", response.headers["Cache-Control"])
        # The admin (this computer) also gets the hidden picture's.
        _response, payload = self.vectors()
        self.assertEqual(payload["ids"], [self.dog, self.cake, self.hidden, self.beach])
        _response, payload = self.vectors("/api/vectors?lib=lib_trips01", environ_base=REMOTE)
        self.assertEqual(payload["ids"], [self.beach])
        self.assertEqual(self.client.get("/api/vectors?lib=lib_nothing").status_code, 404)
        # Hiding a picture changes the answer (and its ETag).
        etag = response.headers["ETag"]
        self.conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (self.cake,))
        self.conn.commit()
        response, payload = self.vectors(environ_base=REMOTE)
        self.assertEqual(payload["ids"], [self.dog, self.beach])
        self.assertNotEqual(response.headers["ETag"], etag)

    def test_etag_and_gzip(self):
        self.update()
        first = self.client.get("/api/vectors", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(first.headers["Content-Encoding"], "gzip")
        payload = json.loads(gzip.decompress(first.get_data()))
        self.assertEqual(len(payload["ids"]), 4)
        again = self.client.get("/api/vectors", headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(again.status_code, 304)
        state = self.app.extensions["search_images"]
        builds = state.vectors.builds
        self.client.get("/api/vectors", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(state.vectors.builds, builds)
        # New vectors: a new answer.
        image_edits.save(self.conn, self.cake, {"title": "Another cake"})
        self.update()
        changed = self.client.get("/api/vectors", headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(changed.status_code, 200)

    def test_query_vectors(self):
        response = self.client.get("/api/embed?q=dog+on+a+sofa")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload["model"], payload["dims"]), ("vec", self.dims))
        expected = embeddings.quantize(fake_embed_server.word_vector(
            embeddings.DEFAULT_QUERY_PREFIX + "dog on a sofa"), self.dims)
        self.assertEqual(base64.b64decode(payload["vec"]), expected)
        # (What was typed stays out of the browser's cache.)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        # Remembered: the same query does not go to the model again.
        count = len(self.server.texts())
        self.assertEqual(self.client.get("/api/embed?q=dog%20on%20a%20%20sofa").status_code, 200)
        self.assertEqual(len(self.server.texts()), count)
        self.assertEqual(self.client.get("/api/embed?q=%20").status_code, 400)
        # A server that is still loading its model.
        self.server.queue(fake_embed_server.HttpError(503))
        response = self.client.get("/api/embed?q=cake")
        self.assertEqual((response.status_code, response.get_json()["state"]),
                         (503, "starting"))
        self.server.stop()
        response = self.client.get("/api/embed?q=beach")
        self.assertEqual((response.status_code, response.get_json()["state"]),
                         (503, "unavailable"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_a_query_and_its_words_in_one_request(self):
        # (search-core.js meaningTexts: the query, then its words on their own.)
        def expected(text):
            return embeddings.quantize(fake_embed_server.word_vector(
                embeddings.DEFAULT_QUERY_PREFIX + text), self.dims)
        before = len(self.server.embed_requests())
        response = self.client.get("/api/embed?q=dog+on+a+sofa&q=dog&q=sofa")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = response.get_json()
        self.assertEqual((payload["model"], payload["dims"]), ("vec", self.dims))
        self.assertEqual([base64.b64decode(vec) for vec in payload["vecs"]],
                         [expected(text) for text in ("dog on a sofa", "dog", "sofa")])
        self.assertEqual(payload["vec"], payload["vecs"][0])
        # One call to the model for all of them.
        self.assertEqual(len(self.server.embed_requests()) - before, 1)
        # Remembered one by one: a known text costs nothing, a new set only its new texts.
        count = len(self.server.texts())
        single = self.client.get("/api/embed?q=sofa").get_json()
        self.assertEqual(single["vec"], payload["vecs"][2])
        self.assertNotIn("vecs", single)
        self.assertEqual(len(self.server.texts()), count)
        again = self.client.get("/api/embed?q=dog+on+a+rug&q=dog&q=rug").get_json()
        self.assertEqual([base64.b64decode(vec) for vec in again["vecs"]],
                         [expected(text) for text in ("dog on a rug", "dog", "rug")])
        self.assertEqual(self.server.texts()[count:],
                         [embeddings.DEFAULT_QUERY_PREFIX + text for text in ("dog on a rug",
                                                                              "rug")])
        # An empty one, or none: nothing to look for; QUERY_TEXTS at most (the
        # pages never ask for more: search-core.js MEANING_TEXTS).
        self.assertEqual(self.client.get("/api/embed?q=dog&q=%20").status_code, 400)
        texts = ["word%d" % n for n in range(embeddings.QUERY_TEXTS + 1)]
        many = self.client.get("/api/embed?" + "&".join("q=" + text for text in texts))
        self.assertEqual(many.status_code, 400)
        enough = self.client.get("/api/embed?" + "&".join("q=" + text for text in texts[1:]))
        self.assertEqual(len(enough.get_json()["vecs"]), embeddings.QUERY_TEXTS)
        # Every QUERY_TEXTS_PER_TOKEN texts the model must embed count as one query
        # against the client's bucket; texts it knows cost nothing.
        self.assertEqual(embeddings.QUERY_TEXTS_PER_TOKEN, 3)
        embedder = self.app.extensions["search_images"].embedder
        with mock.patch.object(embedder, "_burst", 3.0), mock.patch.object(embedder, "_rate",
                                                                               0.0001):
            first = self.client.get("/api/embed?q=a+b+c&q=a&q=b&q=c", environ_base=REMOTE)
            self.assertEqual((first.status_code, len(first.get_json()["vecs"])), (200, 4))
            self.assertEqual(self.client.get("/api/embed?q=d+e&q=d&q=e",
                                             environ_base=REMOTE).status_code, 200)
            self.assertEqual(self.client.get("/api/embed?q=a+b+c&q=a",
                                             environ_base=REMOTE).status_code, 200)
            self.assertEqual(self.client.get("/api/embed?q=f",
                                             environ_base=REMOTE).status_code, 429)

    def test_not_set_up_and_too_many_queries(self):
        self.configure(embedding={"endpoint": ""})
        response = self.client.get("/api/embed?q=dog")
        self.assertEqual((response.status_code, response.get_json()["state"]), (404, "off"))
        self.assertIn("embedding", response.get_json()["error"])
        self.configure()
        embedder = self.app.extensions["search_images"].embedder
        with mock.patch.object(embedder, "_burst", 2.0), mock.patch.object(embedder, "_rate",
                                                                               0.0001):
            codes = [self.client.get("/api/embed?q=query%d" % n, environ_base=REMOTE).status_code
                     for n in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            # Another computer has a bucket of its own; remembered answers are free.
            self.assertEqual(self.client.get("/api/embed?q=query0", environ_base=REMOTE)
                             .status_code, 200)
            self.assertEqual(self.client.get("/api/embed?q=more").status_code, 200)

    def test_other_computers_hear_no_details(self):
        self.configure(embedding={"endpoint": "nothing"})
        with redirect_stdout(io.StringIO()):
            response = self.client.get("/api/embed?q=dog", environ_base=REMOTE)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(),
                         {"state": "off", "error": embeddings.PUBLIC_MESSAGES["off"]})
        self.assertIn("nothing", self.client.get("/api/embed?q=dog").get_json()["error"])
        # A model server that cannot be started (its program is missing): the
        # admin hears why, other computers only that it is not there.
        models = self.base.make_dir("models")
        with open(os.path.join(models, "model.gguf"), "wb") as handle:
            handle.write(b"GGUF")
        program = self.base.join("private-tools", "llama-server.exe")
        port = free_port()
        self.configure(endpoint=self.endpoint(base_url="http://127.0.0.1:%d/v1" % port),
                       local_server={"enabled": True, "kind": "embeddings", "endpoint": "vec",
                                     "port": port, "binary": program,
                                     "models_dir": models, "model": "model.gguf"})
        printed = io.StringIO()
        with redirect_stdout(printed):
            response = self.client.get("/api/embed?q=dog", environ_base=REMOTE)
            again = self.client.get("/api/embed?q=cat", environ_base=REMOTE)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(),
                         {"state": "off", "error": embeddings.PUBLIC_MESSAGES["off"]})
        self.assertEqual(again.get_json(), response.get_json())
        self.assertNotIn("private-tools", response.get_data(as_text=True))
        # (The details go to the server's own output, once.)
        self.assertEqual(printed.getvalue().count("private-tools"), 1)
        mine = self.client.get("/api/embed?q=dog").get_json()
        self.assertIn("private-tools", mine["error"])
        # An endpoint that does not answer.
        self.configure()
        self.server.stop()
        with redirect_stdout(io.StringIO()):
            response = self.client.get("/api/embed?q=beach", environ_base=REMOTE)
        self.assertEqual(response.get_json(),
                         {"state": "unavailable",
                          "error": embeddings.PUBLIC_MESSAGES["unavailable"]})

    def test_pages_of_other_sites_cannot_ask(self):
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Origin": "https://elsewhere.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:9"}):
            response = self.client.get("/api/embed?q=dog", headers=headers)
            self.assertEqual(response.status_code, 403, headers)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.server.embed_requests(), [])
        for headers in ({}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                        {"Origin": "http://localhost"}):
            self.assertEqual(self.client.get("/api/embed?q=dog", headers=headers).status_code,
                             200, headers)

    def test_one_computer_never_learns_what_another_searched(self):
        self.assertEqual(self.client.get("/api/embed?q=papers+about+the+house").status_code, 200)
        self.server.stop()
        # Remembered for this computer only.
        self.assertEqual(self.client.get("/api/embed?q=papers+about+the+house").status_code, 200)
        with redirect_stdout(io.StringIO()):
            response = self.client.get("/api/embed?q=papers+about+the+house",
                                       environ_base=REMOTE)
        self.assertEqual(response.status_code, 503)

    def test_an_edit_takes_the_old_vector_out_at_once(self):
        self.update()
        _response, payload = self.vectors(environ_base=REMOTE)
        self.assertIn(self.cake, payload["ids"])
        image_edits.save(self.conn, self.cake, {"title": "A plate", "description": "A plate.",
                                                "tags_remove": ["cake", "candles"]})
        self.conn.commit()
        for environ in (REMOTE, {}):
            _response, payload = self.vectors(environ_base=environ)
            self.assertNotIn(self.cake, payload["ids"])
            self.assertEqual(len(base64.b64decode(payload["data"])),
                             len(payload["ids"]) * self.dims)
        self.update()
        _response, payload = self.vectors(environ_base=REMOTE)
        self.assertIn(self.cake, payload["ids"])

    def test_dims_0_and_too_many_dims(self):
        native = fake_embed_server.DIMS
        self.configure(embedding={"dims": 0})
        self.update()
        _response, payload = self.vectors(environ_base=REMOTE)
        self.assertEqual((payload["dims"], payload["count"], payload["query"]), (native, 3, True))
        answer = self.client.get("/api/embed?q=dog").get_json()
        self.assertEqual(answer["dims"], native)
        self.assertEqual(len(base64.b64decode(answer["vec"])), native)
        self.configure(embedding={"dims": 128})
        response = self.client.get("/api/embed?q=cat")
        self.assertEqual((response.status_code, response.get_json()["state"]),
                         (503, "unavailable"))
        self.assertIn("gives %d numbers per text" % native, response.get_json()["error"])

    def test_queries_notice_another_model_behind_the_endpoint(self):
        self.update()
        self.assertEqual(self.client.get("/api/embed?q=dog").status_code, 200)
        self.server.dims = 48
        response = self.client.get("/api/embed?q=a+new+query")
        self.assertEqual((response.status_code, response.get_json()["state"]),
                         (503, "unavailable"))
        self.assertIn("changed since the pictures were embedded", response.get_json()["error"])
        # Every vector is out of date now: Manage offers to update them, and
        # the page does not ask for query vectors that cannot match.
        counts = embeddings.counts(self.conn, self.config, ["lib_family1", "lib_trips01"])
        self.assertEqual((counts["lib_family1"]["stale"], counts["lib_trips01"]["stale"]), (3, 1))
        _response, payload = self.vectors(environ_base=REMOTE)
        self.assertFalse(payload["query"])
        self.assertEqual(payload["count"], 3)       # (More like this still works)
        # Embedded again with it: queries work again.
        ctx = Context()
        self.assertEqual(self.update(ctx)["embedded"], 4)
        self.assertIn("changed (the model now gives 48 numbers per text, not 64)", ctx.text())
        _response, payload = self.vectors(environ_base=REMOTE)
        self.assertTrue(payload["query"])
        response = self.client.get("/api/embed?q=a+new+query")
        self.assertEqual((response.status_code, response.get_json()["dims"]), (200, self.dims))

    def test_the_request_log_leaves_out_what_was_typed(self):
        import serve
        fields = ("127.0.0.1", "11/Oct/2026 19:03:45")

        def record(line):
            return logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                                     '%s - - [%s] "%s" %s %s', fields + (line, 200, "-"), None)
        typed = record("GET /api/embed?q=pregnancy%20test%20results HTTP/1.1")
        self.assertTrue(serve.QueryLogFilter().filter(typed))
        text = typed.getMessage()
        self.assertNotIn("pregnancy", text)
        self.assertIn('"GET /api/embed?q=... HTTP/1.1" 200 -', text)
        other = record("GET /api/index?lib=all HTTP/1.1")
        serve.QueryLogFilter().filter(other)
        self.assertIn("/api/index?lib=all", other.getMessage())
        logger = logging.getLogger("werkzeug")
        self.addCleanup(lambda: [logger.removeFilter(item) for item in list(logger.filters)
                                 if isinstance(item, serve.QueryLogFilter)])
        serve.quiet_query_log()
        serve.quiet_query_log()
        self.assertEqual(len([item for item in logger.filters
                              if isinstance(item, serve.QueryLogFilter)]), 1)

    def test_a_managed_embeddings_server(self):
        settings = {"enabled": True, "kind": "embeddings", "endpoint": "vec",
                    "port": self.server.port, "binary": "llama-server", "models_dir": ".",
                    "model": "model.gguf", "auto_start": False}
        self.configure(local_server=settings)
        self.server.health = [503]
        response = self.client.get("/api/embed?q=dog")
        self.assertEqual((response.status_code, response.get_json()["state"]), (503, "starting"))
        response = self.client.get("/api/embed?q=dog")
        self.assertEqual(response.status_code, 200)
        self.server.stop()
        response = self.client.get("/api/embed?q=cat")
        self.assertEqual((response.status_code, response.get_json()["state"]), (503, "off"))
        self.assertIn("auto start is off", response.get_json()["error"])

    def test_the_model_server_panel(self):
        settings = {"enabled": True, "kind": "embeddings", "endpoint": "vec",
                    "port": self.server.port, "binary": "llama-server",
                    "models_dir": self.base.make_dir("models"), "model": "model.gguf"}
        self.configure(local_server=settings)
        headers = {"X-Search-Images": "1"}
        panel = self.client.get("/api/admin/model-server", headers=headers).get_json()
        server = panel["servers"][0]
        self.assertEqual((server["kind"], server["idle_stop_minutes"]), ("embeddings", 0))
        self.assertIn("idle_stop_minutes", panel["ui_fields"])
        response = self.client.post("/api/admin/model-server/settings", headers=headers,
                                    json={"endpoint": "vec",
                                          "settings": {"idle_stop_minutes": "30"}})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(common.load_config()["local_server"]["idle_stop_minutes"], 30)
        for bad in ("-1", "a week", 99999):
            response = self.client.post("/api/admin/model-server/settings", headers=headers,
                                        json={"endpoint": "vec",
                                              "settings": {"idle_stop_minutes": bad}})
            self.assertEqual(response.status_code, 400)
            self.assertIn("idle_stop_minutes", response.get_json()["fields"])
        response = self.client.post("/api/admin/model-server/settings", headers=headers,
                                    json={"endpoint": "vec", "settings": {"kind": "describe"}})
        self.assertEqual(response.status_code, 400)

    def test_the_manage_page_counts(self):
        state = self.client.get("/api/admin/state", headers={"X-Search-Images": "1"}).get_json()
        self.assertEqual(state["meaning"]["endpoint"], "vec")
        self.assertTrue(state["meaning"]["ready"])
        family = [lib for lib in state["libraries"] if lib["id"] == "lib_family1"][0]
        self.assertEqual(family["meaning"]["pending"], 3)
        self.update()
        state = self.client.get("/api/admin/state", headers={"X-Search-Images": "1"}).get_json()
        family = [lib for lib in state["libraries"] if lib["id"] == "lib_family1"][0]
        self.assertEqual((family["meaning"]["embedded"], family["meaning"]["pending"]), (3, 0))
        response = self.client.post("/api/admin/jobs", json={"kind": "embed",
                                                             "library_id": "lib_family1"},
                                    headers={"X-Search-Images": "1"})
        self.assertIn(response.status_code, (200, 202))
        self.configure(embedding={"endpoint": ""})
        state = self.client.get("/api/admin/state", headers={"X-Search-Images": "1"}).get_json()
        self.assertFalse(state["meaning"]["configured"])
        self.assertNotIn("meaning", state["libraries"][0])
        response = self.client.post("/api/admin/jobs", json={"kind": "embed"},
                                    headers={"X-Search-Images": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("embedding", response.get_json()["error"])


class ModelServerTest(support.TempBaseMixin, unittest.TestCase):

    def test_an_embeddings_server_command(self):
        models = self.base.make_dir("models")
        for name in ("model.gguf", "model.mmproj.gguf"):
            with open(os.path.join(models, name), "wb") as handle:
                handle.write(b"GGUF")
        settings = local_server.server_settings({
            "enabled": True, "kind": "Embeddings", "endpoint": "vec", "binary": "llama-server",
            "models_dir": models, "model": "model.gguf", "port": 8099, "slots": 4,
            "ctx_per_slot": 8192, "idle_stop_minutes": 15, "extra_args": ["-t", "8"]})
        self.assertEqual((settings["kind"], settings["idle_stop_minutes"]), ("embeddings", 15))
        self.assertTrue(local_server.embeddings_server(settings))
        self.assertEqual(local_server.auto_mmproj(settings), "")
        self.assertEqual(local_server.mmproj_path(settings), "")
        command = local_server.build_command(settings)
        self.assertEqual(command[1:], ["-m", os.path.join(models, "model.gguf"), "--embeddings",
                                       "--host", "127.0.0.1", "--port",
                                       "8099", "-c", "8192", "-ub", "8192", "-b", "8192",
                                       "--no-webui", "-t", "8"])
        self.assertNotIn("--mmproj", command)
        self.assertNotIn("-np", command)
        self.assertEqual(local_server.server_settings({"kind": "chat"})["kind"], "describe")
        self.assertEqual(local_server.server_settings({"idle_stop_minutes": -5})
                         ["idle_stop_minutes"], 0)

    def test_idle_servers_started_for_queries_are_stopped(self):
        config = {"local_server": {"enabled": True, "kind": "embeddings", "endpoint": "vec",
                                   "idle_stop_minutes": 10, "port": 8099}}
        manager = local_server.ServerManager()
        manager._query_started.add("vec")
        stops = []
        with mock.patch.object(manager, "_record", return_value={"pid": 1}), \
                mock.patch.object(manager, "stop", side_effect=lambda *args, **kwargs:
                                  stops.append(kwargs.get("reason")) or (True, "stopped")):
            manager.touch("vec")
            self.assertEqual(manager.check_idle(config), [])
            manager._last_used["vec"] = time.monotonic() - 9 * 60
            self.assertEqual(manager.check_idle(config), [])
            manager._last_used["vec"] = time.monotonic() - 11 * 60
            lease = object()
            manager._add_lease("vec", lease)
            self.assertEqual(manager.check_idle(config), [])     # a job uses it
            manager._drop_lease("vec", lease)
            self.assertEqual(manager.check_idle(config), ["vec"])
        self.assertEqual(stops, ["no search used it for 10 minutes"])
        self.assertNotIn("vec", manager._query_started)
        # 0 minutes: never stopped for being idle.
        config["local_server"]["idle_stop_minutes"] = 0
        manager._query_started.add("vec")
        with mock.patch.object(manager, "_record", return_value={"pid": 1}), \
                mock.patch.object(manager, "stop") as stop:
            manager._last_used["vec"] = 0.0
            self.assertEqual(manager.check_idle(config), [])
            stop.assert_not_called()


class QueryStartTest(support.TempBaseMixin, unittest.TestCase):
    """Starting an embeddings server for searches: only that kind, and not again and
    again after it failed; and another process that uses it keeps it running."""

    def config(self, **extra):
        entry = {"enabled": True, "kind": "embeddings", "endpoint": "vec", "auto_start": True,
                 "port": 8099, "idle_stop_minutes": 10}
        entry.update(extra)
        return {"local_server": entry}

    def test_a_failed_start_is_not_tried_again_for_every_search(self):
        manager = local_server.ServerManager()
        config = self.config()
        with mock.patch.object(local_server, "health", return_value=None), \
                mock.patch.object(manager, "start", return_value=(True, "Starting...")) as start:
            self.assertEqual(manager.start_for_queries(config, "vec")[0], "starting")
            # The start fails (the process exits while loading).
            manager._end_op("vec", False, "error", "The model server stopped while starting "
                                                   "(exit code 1).")
            for _ in range(3):
                state, message = manager.start_for_queries(config, "vec")
                self.assertEqual(state, "unavailable")
                self.assertIn("exit code 1", message)
            self.assertEqual(start.call_count, 1)
            # Minutes later, a search tries again.
            manager._failed["vec"] = (time.monotonic() - local_server.QUERY_RETRY_SECONDS - 1,
                                      "old")
            self.assertEqual(manager.start_for_queries(config, "vec")[0], "starting")
            self.assertEqual(start.call_count, 2)
            # A start that worked (Start on the Settings page, a job) clears it.
            manager._end_op("vec", False, "error", "Failed again.")
            manager._end_op("vec", True, "ready", "Ready.")
            self.assertEqual(manager.start_for_queries(config, "vec")[0], "starting")
            self.assertEqual(start.call_count, 3)

    def test_a_search_never_starts_another_kind_of_server(self):
        manager = local_server.ServerManager()
        config = self.config(kind="describe")
        with mock.patch.object(local_server, "health", return_value=None), \
                mock.patch.object(manager, "start") as start:
            state, message = manager.start_for_queries(config, "vec")
        self.assertEqual(state, "off")
        self.assertIn("not an embeddings server", message)
        start.assert_not_called()
        # Running anyway (started by hand): used as it is.
        with mock.patch.object(local_server, "health", return_value=200):
            self.assertEqual(manager.start_for_queries(config, "vec")[0], "ready")

    def test_another_process_using_the_server_keeps_it_running(self):
        config = self.config()
        local_server.save_state({"vec": {"pid": 1, "identity": "x", "endpoint": "vec"}})
        # scan.py (another manager, as in another process) embeds with it.
        other = local_server.ServerManager()
        lease = other.lease(Context(), config, ["vec"])
        with mock.patch.object(other, "owned", return_value=True), \
                mock.patch.object(other, "start", return_value=(True, "ready")):
            self.assertEqual(lease.acquire(), {})
        self.assertIn(other._user, local_server.load_state()["vec"]["users"])
        self.assertTrue(other._user.startswith("%d." % os.getpid()))
        # The web server started it for searches; nobody searched for 11 minutes.
        web = local_server.ServerManager()
        web._query_started.add("vec")
        web._last_used["vec"] = time.monotonic() - 11 * 60
        others = {"pid": 424242}

        def running(pid, identity):
            return pid == others["pid"]
        state = local_server.load_state()
        state["vec"]["users"] = {"424242.1": "y"}
        local_server.save_state(state)
        with mock.patch.object(web, "_record", return_value={"pid": 1}), \
                mock.patch.object(local_server, "is_running", side_effect=running), \
                mock.patch.object(web, "stop", return_value=(True, "stopped")) as stop:
            self.assertEqual(web.check_idle(config), [])
            stop.assert_not_called()
            others["pid"] = None          # it has ended
            self.assertEqual(web.check_idle(config), ["vec"])
        # The run lets go of it when it ends.
        state = local_server.load_state()
        state["vec"]["users"] = {other._user: other._identity}
        local_server.save_state(state)
        lease.release()
        self.assertEqual(local_server.load_state()["vec"]["users"], {})


class ExportTest(EmbedFixture, unittest.TestCase):

    def test_the_similar_option_ships_fresh_vectors(self):
        import export
        if not support.pillow_available():
            self.skipTest("Pillow is not installed")
        import cache
        thumbs = cache.ThumbStore()
        self.addCleanup(thumbs.close)
        for image_id in (self.dog, self.cake, self.beach):
            thumbs.put(image_id, support.gradient_jpeg(32, 24))
        self.update()
        image_edits.save(self.conn, self.cake, {"title": "Changed since"})
        options = {"images": "none", "exclude_undescribed": True}
        preview = export.preview_export(self.conn, self.config, "lib_family1", options,
                                        reachable=True)
        self.assertEqual(preview["counts"]["similar"], {"vectors": 1, "stale": 1, "missing": 0})
        self.assertEqual(preview["size_parts"]["vectors"], export.vector_bytes(self.dims))
        out = self.base.join("site")
        summary = export.export_library(Context(), self.conn, self.config, "lib_family1", out,
                                        options)
        self.assertEqual((summary["count"], summary["vectors"]), (2, 1))
        with open(os.path.join(out, "data", "manifest.js"), encoding="ascii") as handle:
            manifest = json.loads(handle.read().split("=", 1)[1].strip().rstrip(";"))
        self.assertTrue(manifest["vectors"])
        self.assertNotIn("similar", manifest["capabilities"])
        texts = []
        for name in manifest["vectors"]:
            with open(os.path.join(out, "data", name), encoding="ascii") as handle:
                texts.append(handle.read())
        parts = [json.loads(text[len("SEARCH_VECTORS.push("):-3]) for text in texts]
        ids = [xid for part in parts for xid in part["ids"]]
        self.assertEqual(len(ids), 1)
        self.assertEqual(parts[0]["dims"], self.dims)
        self.assertNotIn("model", parts[0])
        self.assertNotIn(str(self.dog), ids)            # export ids, never database ids
        with open(os.path.join(out, "data", manifest["shards"][0]), encoding="ascii") as handle:
            self.assertIn('"i":"%s"' % ids[0], handle.read())
        # The pages load them (from disk) and "More like this" works with them.
        node = shutil.which("node")
        if node is not None:
            expected = self.base.write_file("expected.json", json.dumps(
                {"page": "index.html", "count": 2, "vectors": 1, "queries": []}))
            result = subprocess.run([node, os.path.join(support.REPO, "tests", "js",
                                                        "export_test.js"), out, expected],
                                    cwd=support.REPO, capture_output=True, timeout=300)
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("ok   meaning vectors only with the similar option", output)
        # Off: no vector files (and the earlier ones are removed).
        options["similar"] = False
        export.export_library(Context(), self.conn, self.config, "lib_family1", out, options)
        self.assertFalse([name for name in os.listdir(os.path.join(out, "data"))
                          if name.startswith("vectors-")])


if __name__ == "__main__":
    unittest.main()
