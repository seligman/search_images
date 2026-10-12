"""Tests for the 3c parts of search by meaning (embeddings.py): models with a Dense head
(raw vectors from the endpoint, the head applied here), the recipe and the probe that tie
the vectors to a model file, texts too long for the model, and the embeddings model
server's context.

The endpoint is tests/fake_embed_server.py on 127.0.0.1 (it gives raw vectors for
"embd_normalize": -1, as llama.cpp does); the model files are the made-up ones of
tests/fake_gguf.py. Every test runs in a temporary SETTINGS_BASE.
"""

import base64
import json
import os
import unittest

import support

import fake_embed_server
import fake_gguf
from test_embeddings import Context, EmbedFixture, info

import embeddings
import image_edits
import llm
import local_server

DENSE_OUT = 48


def dense_by_hand(rows, bias, vector):
    """The Dense head worked out plainly (rows: lists of numbers)."""
    return [sum(w * x for w, x in zip(row, vector)) + b for row, b in zip(rows, bias)]


class DenseFixture(EmbedFixture):
    """EmbedFixture with a made-up model file and a Dense head for the fake server's vectors."""

    def setUp(self):
        super().setUp()
        self.models = self.base.make_dir("models")
        self.model = fake_gguf.write_bert(os.path.join(self.models, "model.gguf"))
        self.dense, self.rows, self.bias = fake_gguf.write_dense(
            os.path.join(self.models, "dense.safetensors"), out=DENSE_OUT,
            cols=fake_embed_server.DIMS)
        self.configure(embedding={"model_file": self.model, "dense_file": self.dense})

    def expected(self, text):
        """The stored vector of a text: the raw vector, the Dense head's first dims rows."""
        raw = fake_embed_server.raw_vector(text)
        return embeddings.quantize(dense_by_hand(self.rows[:self.dims], self.bias[:self.dims],
                                                 raw), self.dims)


class DenseHeadTest(DenseFixture, unittest.TestCase):

    def test_raw_replies_go_through_the_dense_head(self):
        summary = self.update()
        self.assertEqual((summary["embedded"], summary["failed"]), (4, 0))
        requests = self.server.embed_requests()
        self.assertTrue(requests)
        self.assertTrue(all(payload.get("embd_normalize") == -1 for payload in requests))
        stored, meta = self.stored()
        self.assertEqual((meta["native_dims"], meta["dims"]), (DENSE_OUT, self.dims))
        texts = dict((row[0], row[3]) for row in embeddings.library_texts(
            self.conn, ["lib_family1", "lib_trips01"]))
        store = embeddings.connect()
        self.addCleanup(store.close)
        for image_id in (self.dog, self.beach):
            vec = store.execute("SELECT vec FROM vectors WHERE image_id = ?",
                                (image_id,)).fetchone()[0]
            self.assertEqual(bytes(vec), self.expected(texts[image_id]))
        # The probe was made the same way.
        probe = meta["probe"]
        self.assertEqual([item["text"] for item in probe], list(embeddings.PROBE_TEXTS))
        self.assertEqual(base64.b64decode(probe[0]["vec"]),
                         self.expected(embeddings.PROBE_TEXTS[0]))

    def test_normalised_replies_are_refused(self):
        self.server.always_normalise = True
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual(summary["embedded"], 0)
        self.assertIn("answered with normalised vectors", summary["error"])
        self.assertIn("llama.cpp server", summary["error"])
        self.assertIn("embd_normalize", summary["error"])
        self.assertEqual(self.stored()[0], {})

    def test_raw_vectors_are_asked_for_only_with_a_dense_head(self):
        self.configure(embedding={"model_file": self.model})
        self.assertEqual(self.update()["embedded"], 4)
        self.assertFalse(any("embd_normalize" in payload
                             for payload in self.server.embed_requests()))

    def test_an_ollama_endpoint_cannot_use_a_dense_head(self):
        self.configure(embedding={"dense_file": self.dense},
                       endpoint=self.endpoint(preset="ollama", base_url=self.server.root_url))
        endpoint, problem = embeddings.endpoint_for(self.config)
        self.assertIsNone(endpoint)
        self.assertIn("Ollama", problem)
        self.assertIn("embedding.dense_file", problem)
        summary = self.update()
        self.assertEqual(summary["error"], problem)
        self.assertEqual(self.server.embed_requests(), [])

    def test_a_dense_head_that_does_not_fit(self):
        other, _rows, _bias = fake_gguf.write_dense(os.path.join(self.models, "other.safetensors"),
                                                    out=DENSE_OUT, cols=32)
        self.configure(embedding={"model_file": self.model, "dense_file": other})
        summary = self.update()
        self.assertIn("takes 32", summary["error"])
        self.assertIn("belongs to another model", summary["error"])
        self.assertEqual(self.stored()[0], {})
        few, _rows, _bias = fake_gguf.write_dense(os.path.join(self.models, "few.safetensors"),
                                                  out=8, cols=fake_embed_server.DIMS)
        self.configure(embedding={"model_file": self.model, "dense_file": few})
        self.assertIn("fewer than embedding.dims", self.update()["error"])

    def test_queries_go_through_the_dense_head(self):
        self.update()
        embedder = embeddings.QueryEmbedder(servers=_ReadyServers())
        status, payload = embedder.answer(self.config, "dog on a sofa", client="127.0.0.1",
                                          admin=True)
        self.assertEqual(status, 200, payload)
        self.assertEqual(base64.b64decode(payload["vec"]),
                         self.expected(embeddings.DEFAULT_QUERY_PREFIX + "dog on a sofa"))
        self.assertEqual(self.server.embed_requests()[-1].get("embd_normalize"), -1)
        self.assertTrue(embeddings.query_ready(self.config, embeddings.read_meta()))
        # A server that normalises anyway: no vector.
        self.server.always_normalise = True
        status, payload = embedder.answer(self.config, "a cat", client="127.0.0.1", admin=True)
        self.assertEqual((status, payload["state"]), (503, "unavailable"))
        self.assertIn("normalised vectors", payload["error"])

    def test_a_served_model_file_of_another_name_is_refused(self):
        self.server.served = "your-model.gguf"
        summary = self.update()
        self.assertIn("serves the model your-model.gguf", summary["error"])
        self.assertIn("set up for model.gguf", summary["error"])
        self.assertEqual(self.stored()[0], {})
        # A name that is not a file name (an alias) is not checked.
        self.server.served = "my-embeddings"
        self.assertEqual(self.update()["embedded"], 4)


class _ReadyServers:
    """A model server manager whose server is always ready (QueryEmbedder)."""

    def start_for_queries(self, config, name):
        return "ready", ""

    def touch(self, name):
        pass

    def start_scheduler(self, loader):
        pass


class RecipeTest(DenseFixture, unittest.TestCase):

    def test_another_model_file_empties_the_store(self):
        self.update()
        recipe = self.stored()[1]["recipe"]
        self.assertEqual(len(recipe), 40)
        self.assertEqual(recipe, embeddings.recipe(self.config))
        other = fake_gguf.write_bert(os.path.join(self.models, "your-model.gguf"), seed=2)
        self.configure(embedding={"model_file": other, "dense_file": self.dense})
        self.server.served = "your-model.gguf"
        self.assertNotEqual(embeddings.recipe(self.config), recipe)
        self.assertFalse(embeddings.query_ready(self.config, embeddings.read_meta()))
        counts = embeddings.counts(self.conn, self.config, ["lib_family1"])["lib_family1"]
        self.assertEqual((counts["embedded"], counts["stale"]), (0, 3))
        ctx = Context()
        self.assertEqual(self.update(ctx)["embedded"], 4)
        self.assertIn("every picture is embedded again", ctx.text())
        self.assertEqual(self.stored()[1]["recipe"], embeddings.recipe(self.config))
        # Without a model file or Dense head there is no recipe (as before 3c).
        self.configure(embedding={"model_file": "", "dense_file": ""})
        self.assertEqual(embeddings.recipe(self.config), "")

    def test_a_model_file_that_cannot_be_read_keeps_the_vectors(self):
        # A renamed folder or a drive that is not there: nothing is embedded,
        # nothing is thrown away, and when the file is back, nothing is
        # embedded again.
        import admin
        import export
        self.update()
        before = self.stored()
        moved = self.model + ".moved"
        os.rename(self.model, moved)
        self.addCleanup(lambda: os.path.exists(moved) and os.rename(moved, self.model))
        with self.assertRaisesRegex(embeddings.browser_model.ModelError, "cannot be read"):
            embeddings.recipe(self.config)
        requests = len(self.server.requests)
        for quick in (True, False):
            ctx = Context()
            summary = self.update(ctx, quick=quick)
            self.assertEqual(summary["embedded"], 0)
            self.assertIn("cannot be read", summary["error"])
            self.assertNotIn("every picture is embedded again", ctx.text())
        self.assertEqual(len(self.server.requests), requests)
        self.assertEqual(self.stored(), before)
        # The Manage page: the vectors count as they are, and it says why
        # nothing can be updated; queries wait for the file.
        counts = embeddings.counts(self.conn, self.config, ["lib_family1"])["lib_family1"]
        self.assertEqual((counts["embedded"], counts["stale"]), (3, 0))
        view = admin.meaning_view(self.config)
        self.assertFalse(view["ready"])
        self.assertIn("cannot be read", view["problem"])
        self.assertFalse(embeddings.query_ready(self.config, embeddings.read_meta()))
        status = export.meaning_status(self.config, {"similar": True}, 3)
        self.assertIn("cannot be read", status["reason"])
        os.rename(moved, self.model)
        self.assertTrue(embeddings.query_ready(self.config, embeddings.read_meta()))
        self.assertEqual(self.update()["embedded"], 0)
        self.assertEqual(self.stored(), before)
        # The model of a managed server that cannot be found: the same.
        self.configure(embedding={"dense_file": self.dense}, local_server={
            "enabled": True, "kind": "embeddings", "endpoint": "vec", "binary": "llama-server",
            "models_dir": self.models, "model": "your-model.gguf", "port": 8099})
        summary = self.update(quick=True)
        self.assertIn("your-model.gguf", summary["error"])
        self.assertEqual(self.stored(), before)
        self.assertIn("your-model.gguf", embeddings.recipe_problem(self.config))

    def test_the_model_file_of_a_managed_server(self):
        self.configure(embedding={"dense_file": self.dense}, local_server={
            "enabled": True, "kind": "embeddings", "endpoint": "vec", "binary": "llama-server",
            "models_dir": self.models, "model": "model.gguf", "port": 8099})
        self.assertEqual(os.path.normcase(embeddings.model_file(self.config)),
                         os.path.normcase(self.model))
        self.configure(embedding={"dense_file": self.dense, "model_file": "models/other.model"})
        self.assertEqual(embeddings.model_file(self.config),
                         os.path.normpath(self.base.join("models", "other.model")))

    def test_the_probe_notices_another_model(self):
        self.update()
        before = self.stored()[1]["probe"]
        # Nothing to embed: the endpoint is not asked (and nothing is noticed).
        self.server.twist = True
        requests = len(self.server.requests)
        self.assertEqual(self.update()["pending"], 0)
        self.assertEqual(len(self.server.requests), requests)
        # Something to embed: the probe comes out differently, so every
        # picture is embedded again.
        image_edits.save(self.conn, self.cake, {"title": "A new title"})
        ctx = Context()
        summary = self.update(ctx)
        self.assertIn("it embeds the same text differently now", ctx.text())
        self.assertEqual((summary["embedded"], summary["pending"]), (4, 4))
        self.assertNotEqual(self.stored()[1]["probe"], before)
        # A store whose vectors have no probe gets one, with nothing else to
        # do; but not from the quick step after a scan, which leaves the
        # endpoint alone then.
        store = embeddings.connect()
        with store:
            store.execute("UPDATE meta SET value = '' WHERE key = 'probe'")
        store.close()
        requests = len(self.server.requests)
        self.assertEqual(self.update(quick=True)["pending"], 0)
        self.assertEqual(len(self.server.requests), requests)
        self.assertEqual(self.stored()[1]["probe"], [])
        summary = self.update()
        self.assertEqual(summary["embedded"], 0)
        self.assertEqual(len(self.stored()[1]["probe"]), 2)
        self.assertEqual(embeddings.probe_changed(before, before), "")
        self.assertEqual(embeddings.probe_changed([], before), "")


class LongTextTest(EmbedFixture, unittest.TestCase):

    def test_a_text_too_long_is_cut_and_tried_again(self):
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?", (json.dumps(info(
            "Shopping list", "A list on the fridge.", text="LONG list of things to buy")),
            self.cake))
        long_end = " ".join(["word"] * 40) + " LONG"
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?", (json.dumps(info(
            "Notes", long_end)), self.dog))
        self.conn.commit()
        self.server.too_long = "LONG"
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["embedded"], summary["failed"], summary["cut"]), (4, 0, 2))
        self.assertIn("2 cut to fit the model", ctx.text())
        sent = self.server.texts()
        self.assertTrue(any(text.startswith("Shopping list.") and "Text in the picture"
                            not in text for text in sent))
        stored, _meta = self.stored()
        # Stored as the whole text's vector (it is up to date).
        self.assertEqual(self.update()["embedded"], 0)
        self.assertIn(self.cake, stored)

    def test_llama_cpp_s_batch_size_error_is_not_waited_for(self):
        # (llama.cpp says "too large to process" with a 500: not worth retrying.)
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?", (json.dumps(info(
            "Shopping list", "A list on the fridge.", text="LONG list of things to buy")),
            self.cake))
        self.conn.commit()
        self.server.too_long = "LONG"
        self.server.too_long_status = 500
        ctx = Context()
        summary = self.update(ctx)
        self.assertEqual((summary["embedded"], summary["failed"], summary["cut"]), (4, 0, 1))
        self.assertNotIn("trying again", ctx.text())

    def test_what_is_cut(self):
        text = "Title.\nA description.\nText in the picture: far too much"
        self.assertEqual(embeddings.shorter(text), "Title.\nA description.")
        cut = embeddings.shorter("one two three four five six seven eight")
        self.assertEqual(cut, "one two three four five six")       # (3/4, at a word)
        self.assertTrue(embeddings.too_long(llm.ProviderError(
            "endpoint", "HTTP 400: input (900 tokens) is too large to process. increase the "
            "physical batch size", status=400)))
        self.assertFalse(embeddings.too_long(llm.ProviderError("endpoint", "HTTP 400: bad")))


class SettingsTest(support.TempBaseMixin, unittest.TestCase):

    def test_the_new_settings(self):
        options = embeddings.settings({"embedding": {
            "model_file": ' "C:\\path\\to\\model.gguf" ', "dense_file": "dense.safetensors",
            "license_file": 5, "credit": "  A model  by   someone " + "x" * 300}})
        self.assertEqual(options["model_file"], "C:\\path\\to\\model.gguf")
        self.assertEqual(options["dense_file"], "dense.safetensors")
        self.assertEqual(options["license_file"], "")
        self.assertTrue(options["credit"].startswith("A model by someone x"))
        self.assertEqual(len(options["credit"]), embeddings.MAX_CREDIT)
        defaults = embeddings.settings({})
        self.assertEqual((defaults["batch"], defaults["query_prefix"]),
                         (32, "Represent this sentence for searching relevant passages: "))


class ModelServerContextTest(support.TempBaseMixin, unittest.TestCase):

    def test_the_context_is_the_model_s_at_most(self):
        models = self.base.make_dir("models")
        fake_gguf.write_bert(os.path.join(models, "model.gguf"))
        settings = local_server.server_settings({
            "enabled": True, "kind": "embeddings", "endpoint": "vec", "binary": "llama-server",
            "models_dir": models, "model": "model.gguf", "port": 8099, "ctx_per_slot": 8192})
        command = local_server.build_command(settings)
        self.assertEqual(command[command.index("-c") + 1], str(fake_gguf.CONTEXT))
        self.assertEqual(command[command.index("-ub") + 1], str(fake_gguf.CONTEXT))
        self.assertNotIn("--pooling", command)
        self.assertEqual(local_server.model_context(os.path.join(models, "model.gguf")),
                         fake_gguf.CONTEXT)
        # A smaller ctx_per_slot stays; a file without a context says nothing.
        settings["ctx_per_slot"] = 64
        command = local_server.build_command(settings)
        self.assertEqual(command[command.index("-c") + 1], "64")
        with open(os.path.join(models, "bad.model"), "wb") as handle:
            handle.write(b"GGUF")
        self.assertIsNone(local_server.model_context(os.path.join(models, "bad.model")))


if __name__ == "__main__":
    unittest.main()
