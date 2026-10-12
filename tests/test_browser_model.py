"""Tests for browser_model.py (the query model of search by meaning, made ready for a
browser) and static/embed-engine.js, the engine that runs it in exported pages.

The models are the tiny made-up ones of tests/fake_gguf.py. The engine's
tests (tests/js/embed_engine_test.js, Node) compare it with what llama.cpp
made of the same model (tests/fixtures/fake_bert_llamacpp.json, see
tests/make_embed_fixture.py); they are skipped when node is not installed.
"""

import base64
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from unittest import mock

import support

import fake_gguf

import browser_model
import embeddings

NODE = shutil.which("node")
ENGINE_TESTS = os.path.join(support.REPO, "tests", "js", "embed_engine_test.js")
FIXTURE = os.path.join(support.REPO, "tests", "fixtures", "fake_bert_llamacpp.json")
PREFIX = "query: "


class Folder:
    """A temporary folder for model files."""

    def __init__(self, test):
        self.path = tempfile.mkdtemp(prefix="si-model-")
        test.addCleanup(shutil.rmtree, self.path, True)

    def join(self, name):
        return os.path.join(self.path, name)


def model_numbers(text):
    """The JSON of meaning/model.js."""
    return json.loads(text[len("SEARCH_EMBED.model("):-len(");\n")])


def part_bytes(text):
    """The bytes of a meaning/w-NNN.js file."""
    return base64.b64decode(text.split('"')[1])


class InspectTest(unittest.TestCase):

    def setUp(self):
        self.folder = Folder(self)

    def test_a_small_bert_model_can_run_in_a_browser(self):
        path = fake_gguf.write_bert(self.folder.join("model.gguf"))
        info = browser_model.inspect(path)
        self.assertTrue(info["supported"], info["why"])
        self.assertEqual(info["why"], "")
        self.assertEqual((info["arch"], info["pooling"], info["layers"], info["hidden"],
                          info["heads"], info["ffn"], info["context"]),
                         ("bert", "mean", fake_gguf.LAYERS, fake_gguf.HIDDEN, fake_gguf.HEADS,
                          fake_gguf.FFN, fake_gguf.CONTEXT))
        self.assertEqual(info["vocab"], len(fake_gguf.vocabulary()))
        self.assertGreater(info["params"], 20000)
        self.assertGreater(info["bytes"], 20000)
        cls = browser_model.inspect(fake_gguf.write_bert(self.folder.join("cls.model"), "cls"))
        self.assertEqual((cls["supported"], cls["pooling"]), (True, "cls"))
        f16 = browser_model.inspect(fake_gguf.write_bert(self.folder.join("f16.model"),
                                                         matrices="F16"))
        self.assertTrue(f16["supported"], f16["why"])

    def test_what_cannot_run_in_a_browser_and_why(self):
        other = browser_model.inspect(fake_gguf.write_other(self.folder.join("other.model")))
        self.assertFalse(other["supported"])
        self.assertEqual(other["why"], "Qwen3 Embedding 0.6B cannot run in a browser: only "
                                       "small BERT-family models can (see SETTINGS.md).")
        # Another pooling than mean or CLS.
        meta = fake_gguf.bert_meta()
        meta["bert.pooling_type"] = 3
        path = fake_gguf.write_gguf(self.folder.join("last.model"), meta, fake_gguf.bert_tensors())
        self.assertIn("only mean and CLS pooling", browser_model.inspect(path)["why"])
        # Another kind of vocabulary.
        meta = fake_gguf.bert_meta()
        meta["tokenizer.ggml.model"] = "gpt2"
        path = fake_gguf.write_gguf(self.folder.join("bpe.model"), meta, fake_gguf.bert_tensors())
        self.assertIn("not a WordPiece one", browser_model.inspect(path)["why"])
        # A tensor type the converter does not know.
        path = fake_gguf.write_bert(self.folder.join("q4.model"))
        with open(path, "r+b") as handle:
            data = handle.read()
            name = b"blk.0.attn_q.weight"
            at = data.index(name) + len(name) + 4 + 2 * 8
            handle.seek(at)
            handle.write(struct.pack("<I", 12))         # Q4_K
        self.assertIn("holds Q4_K tensors", browser_model.inspect(path)["why"])
        # Not a model file at all, or one cut short.
        with open(self.folder.join("text.model"), "wb") as handle:
            handle.write(b"not a model")
        self.assertIn("is not a GGUF file", browser_model.inspect(self.folder.join(
            "text.model"))["why"])
        whole = fake_gguf.write_bert(self.folder.join("whole.model"))
        with open(whole, "rb") as handle:
            data = handle.read()
        with open(self.folder.join("cut.model"), "wb") as handle:
            handle.write(data[:len(data) // 2])
        self.assertIn("cut short", browser_model.inspect(self.folder.join("cut.model"))["why"])
        with open(self.folder.join("head.model"), "wb") as handle:
            handle.write(data[:20])
        self.assertEqual(browser_model.inspect(self.folder.join("head.model"))["why"],
                         "The model file head.model ends too early (it is cut short or not a "
                         "GGUF file).")
        self.assertIn("cannot be read", browser_model.inspect(self.folder.join("none.model"))
                      ["why"])
        # Too large for visitors to download.
        with mock.patch.object(browser_model, "MAX_FILE_BYTES", 2 * 1024 * 1024):
            with mock.patch.object(browser_model, "_blob_size", return_value=3 * 1024 * 1024):
                info = browser_model.inspect(whole)
        self.assertFalse(info["supported"])
        self.assertRegex(info["why"], r"^Fake Bert is too large for a browser: 4 MB of files, "
                                      r"at most 2 MB\.$")

    def test_reasons_name_the_model_as_it_names_itself(self):
        # Its own name as written; its file's name when it has only a hash
        # for one.
        meta = fake_gguf.bert_meta(name="embeddinggemma 300m")
        meta["general.architecture"] = "gemma-embedding"
        path = fake_gguf.write_gguf(self.folder.join("gemma.model"), meta, [])
        self.assertEqual(browser_model.inspect(path)["why"],
                         "embeddinggemma 300m cannot run in a browser: only small BERT-family "
                         "models can (see SETTINGS.md).")
        meta = fake_gguf.bert_meta(name="3b5a16eaf17e47bd997da998988dce5877a57092")
        meta["bert.pooling_type"] = 3
        path = fake_gguf.write_gguf(self.folder.join("hashed.model"), meta,
                                    fake_gguf.bert_tensors())
        self.assertTrue(browser_model.inspect(path)["why"].startswith(
            "hashed.model cannot run in a browser"))

    def test_odd_header_values_are_reasons_not_errors(self):
        # Header values of kinds no model file has, or numbers it cannot
        # have: inspect says why, convert refuses, and nothing else is raised.
        cases = {"bos_token_id": ("tokenizer.ggml.bos_token_id", "abc"),
                 "unknown_token_id": ("tokenizer.ggml.unknown_token_id", [1, 2]),
                 "block_count": ("bert.block_count", 400000)}
        for name, (key, value) in cases.items():
            meta = fake_gguf.bert_meta()
            meta[key] = value
            path = fake_gguf.write_gguf(self.folder.join(name + ".model"), meta,
                                        fake_gguf.bert_tensors())
            info = browser_model.inspect(path)
            self.assertFalse(info["supported"], name)
            self.assertIn("damaged header", info["why"], name)
            with self.assertRaisesRegex(browser_model.ModelError, "damaged header"):
                browser_model.convert(path, None, 0, "", [])
        self.assertIn("(it claims 400000 layers)", info["why"])

    def test_the_dense_head_must_fit_the_model(self):
        path = fake_gguf.write_bert(self.folder.join("model.gguf"))
        dense, _rows, _bias = fake_gguf.write_dense(self.folder.join("dense.safetensors"), out=48)
        self.assertTrue(browser_model.inspect(path, dense, 16)["supported"])
        self.assertTrue(browser_model.inspect(path, dense, 0)["supported"])
        fewer = browser_model.inspect(path, dense, 64)
        self.assertIn("gives 48 numbers, fewer than embedding.dims (64)", fewer["why"])
        other, _rows, _bias = fake_gguf.write_dense(self.folder.join("other.safetensors"),
                                                    out=48, cols=16)
        self.assertIn("takes 16 numbers, but the model gives 32",
                      browser_model.inspect(path, other, 16)["why"])
        # Without a Dense head, dims cannot be more than the model's size.
        self.assertIn("fewer than embedding.dims (64)", browser_model.inspect(path, None, 64)
                      ["why"])


class DenseTest(unittest.TestCase):

    def setUp(self):
        self.folder = Folder(self)

    def test_the_rows_and_bias_are_read(self):
        path, rows, bias = fake_gguf.write_dense(self.folder.join("dense.safetensors"),
                                                 out=10, cols=4)
        head = browser_model.read_dense(path)
        self.assertEqual((head["out"], head["cols"], head["rows"]), (10, 4, 10))
        self.assertEqual(list(head["weight"]), [value for row in rows for value in row])
        self.assertEqual(list(head["bias"]), bias)
        cut = browser_model.read_dense(path, 3)
        self.assertEqual(cut["rows"], 3)
        self.assertEqual(list(cut["weight"]), [value for row in rows[:3] for value in row])
        self.assertEqual(list(cut["bias"]), bias[:3])
        _path, _rows, _bias = fake_gguf.write_dense(self.folder.join("nobias.safetensors"),
                                                    out=5, cols=4, bias=False)
        self.assertEqual(list(browser_model.read_dense(_path)["bias"]), [0.0] * 5)

    def test_what_cannot_be_used(self):
        path, _rows, _bias = fake_gguf.write_dense(self.folder.join("half.safetensors"),
                                                   dtype="F16")
        with self.assertRaisesRegex(browser_model.ModelError, "holds F16 numbers; only F32"):
            browser_model.read_dense(path)

        def write(name, header, data=b""):
            text = json.dumps(header).encode("utf-8")
            with open(self.folder.join(name), "wb") as handle:
                handle.write(struct.pack("<Q", len(text)) + text + data)
            return self.folder.join(name)

        shape = write("shape.safetensors", {"linear.weight": {
            "dtype": "F32", "shape": [4, 2, 2], "data_offsets": [0, 64]}}, b"\0" * 64)
        with self.assertRaisesRegex(browser_model.ModelError, "unexpected shape"):
            browser_model.read_dense(shape)
        bias = write("bias.safetensors", {
            "linear.weight": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]},
            "linear.bias": {"dtype": "F32", "shape": [3], "data_offsets": [16, 28]}},
            b"\0" * 28)
        with self.assertRaisesRegex(browser_model.ModelError, "bias of another size"):
            browser_model.read_dense(bias)
        short = write("short.safetensors", {
            "linear.weight": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]}},
            b"\0" * 8)
        with self.assertRaisesRegex(browser_model.ModelError, "damaged"):
            browser_model.read_dense(short)
        many = write("many.safetensors", {
            "a": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]},
            "b": {"dtype": "F32", "shape": [2, 2], "data_offsets": [16, 32]}}, b"\0" * 32)
        with self.assertRaisesRegex(browser_model.ModelError, "does not hold one Dense layer"):
            browser_model.read_dense(many)
        with open(self.folder.join("junk.safetensors"), "wb") as handle:
            handle.write(b"\xff" * 64)
        with self.assertRaisesRegex(browser_model.ModelError, "not a safetensors file"):
            browser_model.read_dense(self.folder.join("junk.safetensors"))
        # Headers of another layout: a reason, never another error.
        for number, entry in enumerate((
                {"dtype": "F32", "shape": [2, 2], "data_offsets": 7},
                {"dtype": "F32", "shape": [2, 2], "data_offsets": {"a": 1}},
                {"dtype": "F32", "shape": [2, 2], "data_offsets": [0.5, 16]},
                {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, True]},
                {"dtype": "F32", "shape": 4, "data_offsets": [0, 16]},
                {"dtype": "F32", "shape": [2, "2"], "data_offsets": [0, 16]},
                {"dtype": ["F32"], "shape": [2, 2], "data_offsets": [0, 16]})):
            odd = write("odd%d.safetensors" % number, {"linear.weight": entry}, b"\0" * 16)
            with self.assertRaises(browser_model.ModelError, msg=json.dumps(entry)):
                browser_model.read_dense(odd)
        listed = write("list.safetensors", [1, 2])
        with self.assertRaisesRegex(browser_model.ModelError, "not a safetensors file"):
            browser_model.read_dense(listed)
        # (And the model's check says so.)
        model = fake_gguf.write_bert(self.folder.join("model.gguf"))
        self.assertIn("The Dense head odd0.safetensors is damaged",
                      browser_model.inspect(model, self.folder.join("odd0.safetensors"))["why"])


class ConvertTest(unittest.TestCase):

    def setUp(self):
        self.folder = Folder(self)
        self.model = fake_gguf.write_bert(self.folder.join("model.gguf"))
        self.dense, self.rows, self.bias = fake_gguf.write_dense(
            self.folder.join("dense.safetensors"), out=48)

    def convert(self, model=None, dense=None, dims=0, prefix=PREFIX, probe=()):
        return browser_model.convert(model or self.model, dense, dims, prefix, list(probe))

    def test_the_files_are_the_same_every_time(self):
        first = self.convert()
        self.assertEqual(first, self.convert())
        self.assertRegex(first["id"], r"^m-[0-9a-f]{12}$")
        self.assertEqual(sorted(first["files"]), ["meaning/model.js", "meaning/w-000.js"])
        self.assertEqual(first["bytes"], sum(len(text) for text in first["files"].values()))
        self.assertLess(first["gzip"], first["bytes"])
        model = model_numbers(first["files"]["meaning/model.js"])
        self.assertEqual(model["id"], first["id"])
        self.assertEqual((model["parts"], model["dims"], model["query_prefix"],
                          model["max_tokens"], model["dense"]),
                         (1, fake_gguf.HIDDEN, PREFIX, browser_model.MAX_TOKENS, None))
        self.assertEqual(model["vocab"], fake_gguf.word_pieces())
        self.assertEqual(model["vocab"][:3], ["[PAD]", "[UNK]", "[CLS]"])
        self.assertIn("##ing", model["vocab"])
        self.assertIn("dog", model["vocab"])
        self.assertEqual(model["config"]["pooling"], "mean")
        # Every tensor 4-byte aligned; matrices as int8 + scales, the rest as F32.
        table = model["tensors"]
        self.assertEqual(table["blk.0.attn_q.weight"]["type"], "q8")
        self.assertEqual(table["blk.0.attn_q.bias"]["type"], "f32")
        self.assertTrue(all(entry["off"] % 4 == 0 for entry in table.values()))
        self.assertEqual(len(part_bytes(first["files"]["meaning/w-000.js"])), model["bytes"])
        # The special tokens a text can name ("[MASK]"), longest first.
        self.assertEqual(model["special"], [["[MASK]", 4], ["[PAD]", 0], ["[UNK]", 1],
                                            ["[CLS]", 2], ["[SEP]", 3]])
        # The CRC-32 of each part's bytes (the page checks them).
        self.assertEqual(model["sums"], [zlib.crc32(part_bytes(
            first["files"]["meaning/w-000.js"]))])
        # Nothing of the computer it was made on.
        for text in first["files"].values():
            self.assertNotIn(self.folder.path, text)
            self.assertNotIn("model.gguf", text)

    def test_parts_and_what_changes_the_id(self):
        base = self.convert()
        with mock.patch.object(browser_model, "PART_BYTES", 4096):
            parted = self.convert()
        model = model_numbers(parted["files"]["meaning/model.js"])
        parts = sorted(name for name in parted["files"] if "/w-" in name)
        self.assertEqual(len(parts), model["parts"])
        self.assertGreater(len(parts), 3)
        sizes = [len(part_bytes(parted["files"][name])) for name in parts]
        self.assertEqual(sizes[:-1], [4096] * (len(parts) - 1))
        self.assertEqual(model["sums"], [zlib.crc32(part_bytes(parted["files"][name]))
                                         for name in parts])
        self.assertEqual(sum(sizes), model["bytes"])
        self.assertEqual(b"".join(part_bytes(parted["files"][name]) for name in parts),
                         part_bytes(base["files"]["meaning/w-000.js"]))
        other = fake_gguf.write_bert(self.folder.join("other.model"), seed=2)
        ids = {base["id"], parted["id"], self.convert(model=other)["id"],
               self.convert(dense=self.dense, dims=16)["id"],
               self.convert(dense=self.dense, dims=24)["id"],
               self.convert(prefix="search: ")["id"],
               self.convert(probe=[{"text": "a", "vec": "AQI="}])["id"]}
        self.assertEqual(len(ids), 7)

    def test_the_dense_rows_are_cut_to_dims(self):
        made = self.convert(dense=self.dense, dims=16)
        model = model_numbers(made["files"]["meaning/model.js"])
        blob = part_bytes(made["files"]["meaning/w-000.js"])
        dense = model["dense"]
        self.assertEqual((dense["rows"], dense["cols"], model["dims"]), (16, fake_gguf.HIDDEN, 16))
        weights = struct.unpack_from("<%df" % (16 * fake_gguf.HIDDEN), blob, dense["offset"])
        self.assertEqual(list(weights), [value for row in self.rows[:16] for value in row])
        self.assertEqual(list(struct.unpack_from("<16f", blob, dense["bias"])), self.bias[:16])
        every = model_numbers(self.convert(dense=self.dense, dims=0)["files"]["meaning/model.js"])
        self.assertEqual((every["dense"]["rows"], every["dims"]), (48, 48))

    def test_f16_matrices_become_q8_0(self):
        path = fake_gguf.write_bert(self.folder.join("f16.model"), matrices="F16")
        model = model_numbers(self.convert(model=path)["files"]["meaning/model.js"])
        self.assertEqual(model["tensors"]["blk.1.ffn_down.weight"]["type"], "q8")
        self.assertEqual(model["tensors"]["token_embd.weight"]["type"], "q8")

    def test_a_model_that_cannot_run_is_refused(self):
        other = fake_gguf.write_other(self.folder.join("other.model"))
        with self.assertRaisesRegex(browser_model.ModelError, "cannot run in a browser"):
            self.convert(model=other)

    def test_file_digests_are_remembered(self):
        digest = browser_model.file_sha256(self.model)
        self.assertEqual(len(digest), 64)
        with mock.patch("builtins.open", side_effect=AssertionError("read again")):
            self.assertEqual(browser_model.file_sha256(self.model), digest)
        self.assertEqual(browser_model.file_sha256(self.folder.join("none")), "")


class KernelTest(unittest.TestCase):

    def test_the_committed_kernel_is_the_assembled_one(self):
        with open(browser_model.KERNEL_FILE, encoding="ascii") as handle:
            self.assertEqual(handle.read(), browser_model.kernel_js())
        wasm = browser_model.kernel_wasm()
        self.assertEqual(wasm[:8], b"\x00asm\x01\x00\x00\x00")
        self.assertLess(len(browser_model.kernel_js()), 3 * 1024)
        self.assertIn("(func $matmul (export \"matmul\")", browser_model.kernel_wat())

    def test_the_assembler_refuses_what_it_does_not_know(self):
        with self.assertRaisesRegex(ValueError, "unknown instruction"):
            browser_model._assemble_body("f64.sqrt", {}, {})
        with self.assertRaisesRegex(ValueError, "unknown or repeated label"):
            browser_model._assemble_body("block $a\nbr $b\nend", {}, {})
        with self.assertRaisesRegex(ValueError, "not ended"):
            browser_model._assemble_body("block $a", {}, {})
        self.assertEqual(browser_model._uleb(624485), b"\xe5\x8e\x26")
        self.assertEqual(browser_model._sleb(-123456), b"\xc0\xbb\x78")


@unittest.skipIf(NODE is None, "node is not installed")
class EngineTest(unittest.TestCase):
    """static/embed-engine.js against llama.cpp's numbers for the made-up models."""

    def test_the_engine(self):
        folder = Folder(self)
        models = {}
        for name, kwargs in (("mean", {}), ("cls", {"pooling": "cls"}),
                             ("f16", {"matrices": "F16"})):
            models[name] = fake_gguf.write_bert(folder.join(name + ".gguf"), **kwargs)
        dense, rows, bias = fake_gguf.write_dense(folder.join("dense.safetensors"), out=48)
        made = {
            "mean": browser_model.convert(models["mean"], None, 0, PREFIX, []),
            "cls": browser_model.convert(models["cls"], None, 0, PREFIX, []),
            "f16": browser_model.convert(models["f16"], None, 0, PREFIX, []),
            "dense": browser_model.convert(models["mean"], dense, 16, PREFIX, []),
        }
        for name, result in made.items():
            for rel, text in result["files"].items():
                path = os.path.join(folder.path, name, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="ascii", newline="\n") as handle:
                    handle.write(text)
        # Rounding ties: the server's quantize and the engine's must agree.
        ties = []
        for values in ([0.5, -0.5, 1.5, 2.5] + [0.0] * 4, [3.0, 4.0, 0.0, 0.0],
                       [1.0, 1.0, 1.0, 1.0], [127.5, -126.5, 0.25, 1.0],
                       [0.0039370078740157, 1.0, -0.0118110236220472, 0.5]):
            ties.append({"values": values, "int8": [
                value - 256 if value > 127 else value
                for value in bytearray(embeddings.quantize(values))]})
        expect = {"fixture": FIXTURE, "prefix": PREFIX, "dense": {"rows": rows[:16],
                                                                  "bias": bias[:16]},
                  "ties": ties}
        with open(folder.join("expect.json"), "w", encoding="ascii", newline="\n") as handle:
            json.dump(expect, handle)
        result = subprocess.run([NODE, ENGINE_TESTS, folder.path, folder.join("expect.json")],
                                cwd=support.REPO, capture_output=True, timeout=600)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("passed", output)


if __name__ == "__main__":
    unittest.main()
