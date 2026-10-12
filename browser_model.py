#!/usr/bin/env python3
"""Search by meaning in a published site: the query model, made ready for a browser.

A static export has no server to turn what a visitor types into a meaning
vector, so with the export's "meaning" option the visitor's browser does
it (static/embed-engine.js). It runs the very model the embedding server
runs (a small BERT-family sentence embedding model in a GGUF file, the
format llama.cpp reads), so the vectors it makes match the pictures'
vectors. This module reads such a file and writes it as the scripts an
export ships (convert), says whether a model can run in a browser at all
(inspect), reads the extra Dense head some models have (read_dense: a
sentence-transformers layer that llama.cpp leaves out, see embeddings.py)
and assembles the small WebAssembly kernel of the engine (kernel_js).

Only small BERT-family models can run in a browser (mean or CLS pooling,
a WordPiece vocabulary, F32, F16 or Q8_0 tensors, about 40 MB of files at
most): mdbr-leaf-ir and snowflake-arctic-embed-xs, say; not
Qwen3-Embedding or EmbeddingGemma. See SETTINGS.md.

The converted files (relative to the export's data folder):

    meaning/model.js   SEARCH_EMBED.model({config, vocab, special, tensors,
                       parts, part_bytes, sums, bytes, dense, dims,
                       query_prefix, max_tokens, probe, id})
    meaning/w-NNN.js   SEARCH_EMBED.part(N, "<base64>"): PART_BYTES of the
                       weights each (sums: the CRC-32 of each part's bytes,
                       so the page notices a damaged one)

The weights are one block of bytes, every tensor at a 4-byte boundary in
the GGUF's order: F32 tensors as they are; matrices as Q8_0 (signed bytes,
then one F16 scale per 32 of them; F16 and F32 matrices are turned into
Q8_0 here, which keeps the vectors within a cosine of about 0.999 of
llama.cpp's); the position table cut to MAX_TOKENS rows; then the first
dims rows of the Dense head and its bias as F32. The vocabulary is
written as WordPiece pieces ("word", "##piece"): llama.cpp's GGUF marks
the first piece of a word with a leading U+2581 instead; special lists the
tokens a text can name by their own text ("[MASK]", as llama.cpp's server
reads them: special_tokens). Conversion is
deterministic: the same model, Dense head, dims, query prefix and probe
give the same bytes, and the id ("m-" + 12 hex digits of a SHA-1 over
everything) changes with any of them.

The kernel is assembled from the instruction list below (flat WebAssembly
text) by a small assembler here, so the bytes in static/embed-kernel.js
can be made again with this module alone:

    python browser_model.py --kernel      rewrites static/embed-kernel.js
    python browser_model.py --inspect MODEL.gguf [--dense FILE] [--dims N]

This module uses only the standard library.
"""

import array
import base64
import gzip
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import zlib

# Tokens a query is cut to ([CLS] and [SEP] included): queries are short,
# and the position table shrinks with it.
MAX_TOKENS = 128
# Bytes of weights per meaning/w-NNN.js file (base64 makes them a third larger).
PART_BYTES = 4 * 1024 * 1024
# The most a browser model may take (all its files, before compression):
# visitors download it, about three quarters of this with gzip.
MAX_FILE_BYTES = 40 * 1024 * 1024
# The level GitHub Pages and most web servers compress with.
GZIP_LEVEL = 6
FORMAT = 1

GGUF_MAGIC = b"GGUF"
# GGUF tensor types (ggml_type) by number; only the first three can be converted.
TENSOR_TYPES = {0: "F32", 1: "F16", 8: "Q8_0", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1",
                10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K",
                16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S", 20: "IQ4_NL",
                21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16", 26: "I32",
                27: "I64", 28: "F64", 29: "IQ1_M", 30: "BF16"}
CONVERTIBLE = ("F32", "F16", "Q8_0")
# llama.cpp's pooling types (LLAMA_POOLING_TYPE_*) the engine can do.
POOLINGS = {1: "mean", 2: "cls"}
QK = 32                         # numbers per Q8_0 block
_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
            10: "<Q", 11: "<q", 12: "<d"}
_WORD_START = "▁"
# A general.name that is only a hash (some converters write one): the file's name says more.
_HASH_NAME = re.compile(r"^[0-9a-fA-F]{32,}$")
# llama.cpp's token types (tokenizer.ggml.token_type) that a text names by their
# own text, before its words are split: unknown, control and user-defined.
SPECIAL_TYPES = (2, 3, 4)


class ModelError(ValueError):
    """A model file (or Dense head) that cannot be read or used; the message says why."""


# ---------------------------------------------------------------------------
# GGUF

class _Reader:
    """Reads a file from the start through a buffer, never past its end."""

    def __init__(self, handle, size):
        self.handle = handle
        self.size = size
        self.buffer = b""
        self.start = 0          # file position of buffer[0]
        self.pos = 0            # file position of the next byte

    def read(self, count):
        if count < 0 or self.pos + count > self.size:
            raise ModelError("ends too early (it is cut short or not a GGUF file)")
        end = self.pos + count
        if end > self.start + len(self.buffer):
            keep = self.buffer[self.pos - self.start:]
            self.handle.seek(self.start + len(self.buffer))
            more = self.handle.read(max(count - len(keep), 1 << 20))
            self.buffer = keep + more
            self.start = self.pos
        data = self.buffer[self.pos - self.start:end - self.start]
        self.pos = end
        return data

    def unpack(self, fmt):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def string(self):
        length = self.unpack("<Q")
        if length > self.size - self.pos:
            raise ModelError("holds a text longer than the file")
        return self.read(length).decode("utf-8", "replace")

    def value(self, kind, depth=0):
        if kind in _SCALARS:
            return self.unpack(_SCALARS[kind])
        if kind == 8:
            return self.string()
        if kind == 9 and depth == 0:
            item = self.unpack("<I")
            count = self.unpack("<Q")
            if count > self.size - self.pos:
                raise ModelError("holds a list longer than the file")
            return [self.value(item, 1) for _ in range(count)]
        raise ModelError("holds a value of an unknown kind (%d)" % kind)


def tensor_bytes(kind, count):
    """The bytes of a tensor of count numbers of a type ("F32", ...), or None when not known."""
    if kind == "F32":
        return 4 * count
    if kind in ("F16", "BF16"):
        return 2 * count
    if kind == "Q8_0":
        return count // QK * (QK + 2) if count % QK == 0 else None
    return None


def read_gguf(path):
    """The header of a GGUF file (version 2 or 3): {"path", "version", "meta": {key: value},
    "tensors": [{"name", "dims", "type", "offset" (in the file), "size" (bytes, or None
    for a type that cannot be converted), "count"}]}. Only the header is read; raises
    ModelError when it is not a GGUF file this module understands."""
    try:
        size = os.path.getsize(path)
        handle = open(path, "rb")
    except OSError as error:
        raise ModelError("cannot be read (%s)" % (error.strerror or error))
    with handle:
        reader = _Reader(handle, size)
        if reader.read(4) != GGUF_MAGIC:
            raise ModelError("is not a GGUF file")
        version = reader.unpack("<I")
        if version not in (2, 3):
            raise ModelError("is a GGUF file of version %d (only 2 and 3 can be read)" % version)
        n_tensors = reader.unpack("<Q")
        n_meta = reader.unpack("<Q")
        if n_tensors > size or n_meta > size:
            raise ModelError("has a damaged header")
        meta = {}
        for _ in range(n_meta):
            key = reader.string()
            meta[key] = reader.value(reader.unpack("<I"))
        tensors = []
        for _ in range(n_tensors):
            name = reader.string()
            n_dims = reader.unpack("<I")
            if not 1 <= n_dims <= 4:
                raise ModelError("has a tensor with %d dimensions" % n_dims)
            dims = [reader.unpack("<Q") for _ in range(n_dims)]
            kind = reader.unpack("<I")
            offset = reader.unpack("<Q")
            count = 1
            for dim in dims:
                count *= dim
            type_name = TENSOR_TYPES.get(kind, "type %d" % kind)
            tensors.append({"name": name, "dims": dims, "type": type_name, "offset": offset,
                            "count": count, "size": tensor_bytes(type_name, count)})
        alignment = meta.get("general.alignment", 32)
        if not isinstance(alignment, int) or alignment <= 0:
            raise ModelError("has a damaged header (alignment)")
        start = (reader.pos + alignment - 1) // alignment * alignment
        for tensor in tensors:
            tensor["offset"] += start
            if tensor["size"] is not None and tensor["offset"] + tensor["size"] > size:
                raise ModelError("is cut short (the tensor %s goes past its end)" % tensor["name"])
    return {"path": path, "version": version, "meta": meta, "tensors": tensors}


def _read_at(path, offset, count):
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(count)
    if len(data) != count:
        raise ModelError("is cut short")
    return data


# ---------------------------------------------------------------------------
# The Dense head (sentence-transformers 2_Dense/model.safetensors)

def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _shape(entry):
    """The shape of a safetensors entry (a list of positive whole numbers), or None."""
    shape = entry.get("shape")
    if isinstance(shape, list) and all(_whole(n) and n > 0 for n in shape):
        return shape
    return None


def read_dense(path, rows=0):
    """A sentence-transformers Dense head: {"out", "cols", "weight": array of rows x cols
    F32 numbers (the first rows rows; 0: all), "bias": array of rows numbers}. The file
    is a safetensors file with one F32 matrix [out, cols] (and an F32 bias [out]).
    Raises ModelError with a plain reason otherwise."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(8)
            if len(head) != 8:
                raise ModelError("is not a safetensors file")
            length = struct.unpack("<Q", head)[0]
            if length > min(size - 8, 100 * 1024 * 1024):
                raise ModelError("is not a safetensors file")
            header = json.loads(handle.read(length).decode("utf-8"))
            if not isinstance(header, dict):
                raise ModelError("is not a safetensors file")
            entries = {name: entry for name, entry in header.items()
                       if name != "__metadata__" and isinstance(entry, dict)}
            weight = entries.get("linear.weight")
            bias = entries.get("linear.bias")
            if weight is None:
                matrices = [entry for entry in entries.values()
                            if len(_shape(entry) or ()) == 2]
                vectors = [entry for entry in entries.values()
                           if len(_shape(entry) or ()) == 1]
                if len(matrices) != 1 or len(vectors) > 1:
                    raise ModelError("does not hold one Dense layer (a weight matrix and its "
                                     "bias)")
                weight, bias = matrices[0], (vectors[0] if vectors else None)
            for entry in (weight, bias):
                if entry is not None and entry.get("dtype") != "F32":
                    raise ModelError("holds %s numbers; only F32 can be used"
                                     % str(entry.get("dtype"))[:20])
            shape = _shape(weight)
            if shape is None or len(shape) != 2:
                raise ModelError("has a weight of an unexpected shape")
            out, cols = shape
            if bias is not None and _shape(bias) != [out]:
                raise ModelError("has a bias of another size than its weight")
            rows = min(rows, out) if rows else out
            base = 8 + length

            def numbers(entry, first, count):
                offsets = entry.get("data_offsets")
                if not (isinstance(offsets, list) and len(offsets) == 2
                        and all(_whole(value) for value in offsets)):
                    raise ModelError("is damaged (a tensor has no place in it)")
                begin, end = offsets
                if not (0 <= begin <= end and base + end <= size
                        and end - begin == 4 * (out if entry is bias else out * cols)):
                    raise ModelError("is damaged (a tensor goes past its end)")
                handle.seek(base + begin + 4 * first)
                values = array.array("f")
                values.frombytes(handle.read(4 * count))
                if sys.byteorder != "little":
                    values.byteswap()
                return values

            weights = numbers(weight, 0, rows * cols)
            biases = numbers(bias, 0, rows) if bias is not None else array.array("f", [0.0] * rows)
    except OSError as error:
        raise ModelError("cannot be read (%s)" % (error.strerror or error))
    except ModelError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError,
            RecursionError):
        # (A header of another layout than expected.)
        raise ModelError("is not a safetensors file")
    return {"out": out, "cols": cols, "rows": rows, "weight": weights, "bias": biases}


# ---------------------------------------------------------------------------
# What a model is, and whether it can run in a browser

def _label(gguf):
    """What a model is called in reasons: its own name as it writes it (general.name), or
    its file's name when it has none (or only a hash for one)."""
    name = gguf["meta"].get("general.name")
    if isinstance(name, str) and name.strip() and not _HASH_NAME.match(name.strip()):
        return " ".join(name.split())[:80]
    return os.path.basename(gguf["path"])


def _sentence(text, label):
    """A reason as a sentence: a capital first (unless it starts with a model's own name)
    and a full stop."""
    if not text.startswith(label):
        text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def _megabytes(value):
    """Bytes in MB of 1024 * 1024 (as the export review says sizes)."""
    return "%.0f MB" % (value / 1048576.0)


def _model_config(gguf):
    """The numbers of a BERT model from its GGUF header, or raises ModelError."""
    meta = gguf["meta"]
    arch = meta.get("general.architecture")
    if arch != "bert":
        raise ModelError("%s cannot run in a browser: only small BERT-family models can (see "
                         "SETTINGS.md)" % _label(gguf))
    if meta.get("tokenizer.ggml.model") != "bert":
        raise ModelError("%s cannot run in a browser: its vocabulary is not a WordPiece one "
                         "(see SETTINGS.md)" % _label(gguf))

    def number(key, default=None):
        value = meta.get("%s.%s" % (arch, key), default)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)):
            raise ModelError("%s does not say its %s" % (_label(gguf), key.replace("_", " ")))
        return value

    def token(keys, default):
        # (A special token's id: a whole number, if the file says it.)
        for key in keys:
            value = meta.get("tokenizer.ggml.%s" % key)
            if value is None:
                continue
            if not _whole(value) or value < 0:
                raise ModelError("%s has a damaged header (tokenizer.ggml.%s)"
                                 % (_label(gguf), key))
            return value
        return default

    pooling = meta.get("%s.pooling_type" % arch)
    if pooling not in POOLINGS:
        raise ModelError("%s cannot run in a browser: it pools its tokens in a way the browser "
                         "engine does not (only mean and CLS pooling)" % _label(gguf))
    lowercase = meta.get("tokenizer.ggml.normalizer.lowercase", True)
    strip = meta.get("tokenizer.ggml.normalizer.strip_accents", lowercase)
    config = {"arch": arch, "layers": int(number("block_count")),
              "hidden": int(number("embedding_length")),
              "heads": int(number("attention.head_count")),
              "ffn": int(number("feed_forward_length")),
              "eps": float(number("attention.layer_norm_epsilon")),
              "context": int(number("context_length")),
              "pooling": POOLINGS[pooling],
              "cls": token(("bos_token_id",), 101),
              "sep": token(("seperator_token_id", "eos_token_id"), 102),
              "unk": token(("unknown_token_id",), 100),
              "lowercase": bool(lowercase), "strip_accents": bool(strip)}
    config["max_tokens"] = max(2, min(MAX_TOKENS, config["context"]))
    hidden, heads = config["hidden"], config["heads"]
    if (config["layers"] < 1 or hidden < QK or hidden % QK or config["ffn"] < QK
            or config["ffn"] % QK or heads < 1 or hidden % heads):
        raise ModelError("%s has sizes the browser engine cannot use" % _label(gguf))
    # (Each layer has 16 tensors: a file that claims more layers than it could
    # hold is damaged, and is not worth the list of what it lacks.)
    if config["layers"] > len(gguf["tensors"]) // 16:
        raise ModelError("%s has a damaged header (it claims %d layers)"
                         % (_label(gguf), config["layers"]))
    return config


def _tensor_plan(gguf, config):
    """[(tensor, how)] of the tensors the engine uses, how being "f32" (kept as numbers,
    rows cut for the position table) or "q8" (a matrix, as Q8_0). Raises ModelError."""
    by_name = {tensor["name"]: tensor for tensor in gguf["tensors"]}
    label = _label(gguf)
    wanted = ["token_embd.weight", "position_embd.weight", "token_embd_norm.weight",
              "token_embd_norm.bias"]
    for layer in range(config["layers"]):
        for part in ("attn_q", "attn_k", "attn_v", "attn_output", "ffn_up", "ffn_down",
                     "attn_output_norm", "layer_output_norm"):
            wanted += ["blk.%d.%s.weight" % (layer, part), "blk.%d.%s.bias" % (layer, part)]
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise ModelError("%s cannot run in a browser: it has no %s (only plain BERT models "
                         "can)" % (label, missing[0]))
    plan = []
    for tensor in gguf["tensors"]:
        name = tensor["name"]
        if name.startswith("blk."):
            number = name.split(".")[1]
            if not number.isdigit() or int(number) >= config["layers"]:
                continue
        elif name not in ("token_embd.weight", "position_embd.weight", "token_types.weight",
                          "token_embd_norm.weight", "token_embd_norm.bias"):
            continue
        if tensor["type"] not in CONVERTIBLE:
            raise ModelError("%s holds %s tensors: only F32, F16 and Q8_0 ones can be converted "
                             "for a browser" % (label, tensor["type"]))
        matrix = name.endswith(".weight") and len(tensor["dims"]) == 2 and \
            name not in ("position_embd.weight", "token_types.weight")
        if matrix and tensor["dims"][0] % QK:
            raise ModelError("%s has a matrix the browser engine cannot use (%s)"
                             % (label, name))
        plan.append((tensor, "q8" if matrix else "f32"))
    hidden = config["hidden"]
    for name in ("blk.0.attn_q.weight", "blk.0.ffn_up.weight", "blk.0.ffn_down.weight"):
        dims = by_name[name]["dims"]
        expected = {"blk.0.attn_q.weight": [hidden, hidden],
                    "blk.0.ffn_up.weight": [hidden, config["ffn"]],
                    "blk.0.ffn_down.weight": [config["ffn"], hidden]}[name]
        if dims != expected:
            raise ModelError("%s has a %s of an unexpected shape" % (label, name))
    return plan


def _kept_count(tensor, max_tokens):
    """How many numbers of a tensor go to the browser (the position table is cut)."""
    if tensor["name"] == "position_embd.weight" and len(tensor["dims"]) == 2:
        return tensor["dims"][0] * min(tensor["dims"][1], max_tokens)
    return tensor["count"]


def _align(value):
    return (value + 3) // 4 * 4


def _blob_size(plan, dense_rows, hidden, max_tokens):
    size = 0
    for tensor, how in plan:
        count = _kept_count(tensor, max_tokens)
        size = _align(size)
        if how == "q8":
            size = _align(size + count) + count // QK * 2
        else:
            size += 4 * count
    size = _align(size)
    if dense_rows:
        size += 4 * dense_rows * hidden + 4 * dense_rows
    return _align(size)


def _part_text_size(number, count):
    return len('SEARCH_EMBED.part(%d,"' % number) + 4 * ((count + 2) // 3) + len('");\n')


def vocabulary(gguf):
    """The model's vocabulary as WordPiece pieces, in id order (see the module notes)."""
    tokens = gguf["meta"].get("tokenizer.ggml.tokens")
    kinds = gguf["meta"].get("tokenizer.ggml.token_type")
    if not isinstance(tokens, list) or not tokens:
        raise ModelError("%s has no vocabulary" % _label(gguf))
    if not isinstance(kinds, list) or len(kinds) != len(tokens):
        kinds = [1] * len(tokens)
    pieces = []
    for token, kind in zip(tokens, kinds):
        token = token if isinstance(token, str) else ""
        if kind == 3:                       # a control token ([CLS], [SEP], ...)
            pieces.append(token)
        elif token.startswith(_WORD_START):
            pieces.append(token[len(_WORD_START):])
        else:
            pieces.append("##" + token)
    return pieces


def special_tokens(gguf):
    """[[text, id]] of the tokens a text names by their own text ("[CLS]", "[MASK]", ...:
    the unknown, control and user-defined ones, SPECIAL_TYPES), longest first, as
    llama.cpp's server finds them in a text before it splits the rest into words."""
    tokens = gguf["meta"].get("tokenizer.ggml.tokens")
    kinds = gguf["meta"].get("tokenizer.ggml.token_type")
    if not isinstance(tokens, list) or not isinstance(kinds, list) or len(kinds) != len(tokens):
        return []
    found = [[token, number] for number, (token, kind) in enumerate(zip(tokens, kinds))
             if kind in SPECIAL_TYPES and isinstance(token, str) and token]
    return sorted(found, key=lambda item: (-len(item[0].encode("utf-8")), item[1]))


_inspected = {}
_inspected_lock = threading.Lock()


def _signature(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns)


def inspect(model_file, dense_file=None, dims=0):
    """What a model file is and whether it can run in a browser (remembered by the files'
    paths, sizes and times: see _inspect)."""
    key = (_signature(model_file), _signature(dense_file) if dense_file else None, dims)
    with _inspected_lock:
        found = _inspected.get(key)
    if found is None:
        found = _inspect(model_file, dense_file, dims)
        if key[0] is not None:
            with _inspected_lock:
                if len(_inspected) > 32:
                    _inspected.clear()
                _inspected[key] = found
    return dict(found)


def _inspect(model_file, dense_file=None, dims=0):
    """What a model file is and whether it can run in a browser.

    Returns {"supported", "why" ('' when supported), "arch", "pooling",
    "layers", "hidden", "heads", "ffn", "vocab", "context", "params",
    "bytes" (about how many bytes its converted files take)}; the numbers
    are None when the file could not be read that far. dense_file is the
    model's Dense head ('' or None: none) and dims how many of its numbers
    the vectors keep (0: all); a Dense head of another size than the model,
    or with fewer than dims rows, is not supported.
    """
    result = {"supported": False, "why": "", "arch": None, "pooling": None, "layers": None,
              "hidden": None, "heads": None, "ffn": None, "vocab": None, "context": None,
              "params": None, "bytes": None}
    try:
        gguf = read_gguf(model_file)
    except ModelError as error:
        result["why"] = "The model file %s %s." % (os.path.basename(model_file), error)
        return result
    label = _label(gguf)
    result["arch"] = gguf["meta"].get("general.architecture")
    result["params"] = sum(tensor["count"] for tensor in gguf["tensors"])
    tokens = gguf["meta"].get("tokenizer.ggml.tokens")
    result["vocab"] = len(tokens) if isinstance(tokens, list) else None
    try:
        config = _model_config(gguf)
        result.update(pooling=config["pooling"], layers=config["layers"],
                      hidden=config["hidden"], heads=config["heads"], ffn=config["ffn"],
                      context=config["context"])
        plan = _tensor_plan(gguf, config)
        dense_rows = 0
        if dense_file:
            try:
                dense = read_dense(dense_file, 1)
            except ModelError as error:
                raise ModelError("The Dense head %s %s" % (os.path.basename(dense_file), error))
            if dense["cols"] != config["hidden"]:
                raise ModelError("The Dense head %s takes %d numbers, but the model gives %d: it "
                                 "belongs to another model" % (os.path.basename(dense_file),
                                                               dense["cols"], config["hidden"]))
            if dims and dense["out"] < dims:
                raise ModelError("The Dense head %s gives %d numbers, fewer than "
                                 "embedding.dims (%d)" % (os.path.basename(dense_file),
                                                          dense["out"], dims))
            dense_rows = dims or dense["out"]
        elif dims and dims > config["hidden"]:
            raise ModelError("%s gives %d numbers, fewer than embedding.dims (%d)"
                             % (label, config["hidden"], dims))
        blob = _blob_size(plan, dense_rows, config["hidden"], config["max_tokens"])
        parts = (blob + PART_BYTES - 1) // PART_BYTES
        vocab_text = len(json.dumps(vocabulary(gguf), ensure_ascii=True))
        result["bytes"] = sum(_part_text_size(number, min(PART_BYTES, blob - number * PART_BYTES))
                              for number in range(parts)) + vocab_text + 8192
        if result["bytes"] > MAX_FILE_BYTES:
            raise ModelError("%s is too large for a browser: %s of files, at most %s"
                             % (label, _megabytes(result["bytes"]), _megabytes(MAX_FILE_BYTES)))
    except ModelError as error:
        result["why"] = _sentence(str(error), label)
        return result
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        # (Header values of kinds no model file has.)
        result["why"] = "The model file %s has a damaged header." % os.path.basename(model_file)
        return result
    result["supported"] = True
    return result


# ---------------------------------------------------------------------------
# Converting

def _half(value):
    """value rounded to the nearest F16 (as F16 bytes)."""
    try:
        return struct.pack("<e", value)
    except OverflowError:
        return struct.pack("<e", 65504.0 if value > 0 else -65504.0)


def _quantize_blocks(values):
    """Numbers (a multiple of 32 of them) as Q8_0: (signed bytes, F16 scales)."""
    quants = bytearray(len(values))
    scales = bytearray()
    for start in range(0, len(values), QK):
        block = values[start:start + QK]
        top = max(abs(value) for value in block)
        scales += _half(top / 127.0)
        if top:
            factor = 127.0 / top
            quants[start:start + QK] = bytes(max(-127, min(127, int(round(value * factor)))) & 0xFF
                                             for value in block)
    return bytes(quants), bytes(scales)


def _numbers(path, tensor, count):
    """The first count numbers of an F32, F16 or Q8_0 tensor (an array of floats)."""
    kind = tensor["type"]
    if kind == "F32":
        values = array.array("f")
        values.frombytes(_read_at(path, tensor["offset"], 4 * count))
        if sys.byteorder != "little":
            values.byteswap()
        return values
    if kind == "F16":
        raw = _read_at(path, tensor["offset"], 2 * count)
        return array.array("f", struct.unpack("<%de" % count, raw))
    raw = _read_at(path, tensor["offset"], count // QK * (QK + 2))
    values = array.array("f")
    for block in range(count // QK):
        at = block * (QK + 2)
        scale = struct.unpack_from("<e", raw, at)[0]
        values.extend(quant * scale for quant in struct.unpack_from("<32b", raw, at + 2))
    return values


def _q8(path, tensor, count):
    """(signed bytes, F16 scales) of a matrix: a Q8_0 one as it is, others quantised here."""
    if tensor["type"] == "Q8_0":
        raw = memoryview(_read_at(path, tensor["offset"], count // QK * (QK + 2)))
        step = QK + 2
        blocks = range(0, len(raw), step)
        return (b"".join(raw[at + 2:at + step] for at in blocks),
                b"".join(raw[at:at + 2] for at in blocks))
    quants, scales = bytearray(), bytearray()
    chunk = QK * 4096
    for start in range(0, count, chunk):
        part = _numbers_slice(path, tensor, start, min(chunk, count - start))
        more = _quantize_blocks(part)
        quants += more[0]
        scales += more[1]
    return bytes(quants), bytes(scales)


def _numbers_slice(path, tensor, start, count):
    size = 4 if tensor["type"] == "F32" else 2
    raw = _read_at(path, tensor["offset"] + size * start, size * count)
    if tensor["type"] == "F32":
        return struct.unpack("<%df" % count, raw)
    return struct.unpack("<%de" % count, raw)


def _js_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def convert(model_file, dense_file, dims, query_prefix, probe):
    """The files that let a browser embed queries like the embedding server does.

    model_file is the GGUF the server runs, dense_file its Dense head ('' or
    None: none), dims how many numbers the vectors keep (0: all), query_prefix
    what queries are prefixed with, probe [{"text", "vec" (base64 of the
    stored signed bytes)}] (the page checks itself with them). Returns
    {"files": {"meaning/model.js": text, "meaning/w-000.js": text, ...},
    "id", "bytes" (of all the files), "gzip" (the same compressed at
    GZIP_LEVEL, as web servers send them), "parts", "dims"}. Deterministic.
    Raises ModelError when the model cannot run in a browser (inspect).
    """
    checked = inspect(model_file, dense_file, dims)
    if not checked["supported"]:
        raise ModelError(checked["why"])
    gguf = read_gguf(model_file)
    config = _model_config(gguf)
    plan = _tensor_plan(gguf, config)
    hidden = config["hidden"]
    blob = bytearray()
    table = {}

    def pad():
        blob.extend(b"\0" * (_align(len(blob)) - len(blob)))

    for tensor, how in plan:
        count = _kept_count(tensor, config["max_tokens"])
        dims_kept = list(tensor["dims"])
        if tensor["name"] == "position_embd.weight" and len(dims_kept) == 2:
            dims_kept[1] = min(dims_kept[1], config["max_tokens"])
        pad()
        entry = {"type": how, "dims": dims_kept, "off": len(blob)}
        if how == "q8":
            quants, scales = _q8(model_file, tensor, count)
            blob.extend(quants)
            pad()
            entry["soff"] = len(blob)
            blob.extend(scales)
        else:
            values = _numbers(model_file, tensor, count)
            if sys.byteorder != "little":
                values.byteswap()
            blob.extend(values.tobytes())
        table[tensor["name"]] = entry
    pad()
    dense = None
    out_dims = dims or hidden
    if dense_file:
        head = read_dense(dense_file, dims)
        out_dims = head["rows"]
        weights, biases = head["weight"], head["bias"]
        if sys.byteorder != "little":
            weights.byteswap()
            biases.byteswap()
        dense = {"rows": head["rows"], "cols": head["cols"], "offset": len(blob),
                 "bias": len(blob) + 4 * head["rows"] * head["cols"]}
        blob.extend(weights.tobytes())
        blob.extend(biases.tobytes())
        pad()
    parts = []
    sums = []
    for number, start in enumerate(range(0, len(blob), PART_BYTES)):
        chunk = bytes(blob[start:start + PART_BYTES])
        sums.append(zlib.crc32(chunk) & 0xFFFFFFFF)
        parts.append(('meaning/w-%03d.js' % number,
                      'SEARCH_EMBED.part(%d,"%s");\n' % (number,
                                                         base64.b64encode(chunk).decode("ascii"))))
    model = {"format": FORMAT,
             "config": {key: config[key] for key in (
                 "arch", "layers", "hidden", "heads", "ffn", "eps", "pooling", "cls", "sep",
                 "unk", "lowercase", "strip_accents", "max_tokens")},
             "vocab": vocabulary(gguf), "special": special_tokens(gguf), "tensors": table,
             "parts": len(parts), "part_bytes": PART_BYTES, "sums": sums, "bytes": len(blob),
             "dense": dense, "dims": out_dims,
             "query_prefix": query_prefix or "", "max_tokens": config["max_tokens"],
             "probe": [{"text": item["text"], "vec": item["vec"]} for item in (probe or [])]}
    digest = hashlib.sha1(_js_json(model).encode("ascii"))
    for _rel, text in parts:
        digest.update(text.encode("ascii"))
    model["id"] = "m-" + digest.hexdigest()[:12]
    files = {"meaning/model.js": "SEARCH_EMBED.model(%s);\n" % _js_json(model)}
    files.update(parts)
    total = sum(len(text) for text in files.values())
    packed = sum(len(gzip.compress(text.encode("ascii"), GZIP_LEVEL, mtime=0))
                 for text in files.values())
    return {"files": files, "id": model["id"], "bytes": total, "gzip": packed,
            "parts": len(parts), "dims": out_dims}


# ---------------------------------------------------------------------------
# File digests (embeddings.recipe)

_digests = {}
_digests_lock = threading.Lock()


def file_sha256(path):
    """The SHA-256 (hex) of a file, remembered by its path, size and time; '' when it
    cannot be read."""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns)
    with _digests_lock:
        found = _digests.get(key)
    if found is not None:
        return found
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    value = digest.hexdigest()
    with _digests_lock:
        if len(_digests) > 64:
            _digests.clear()
        _digests[key] = value
    return value


# ---------------------------------------------------------------------------
# The WebAssembly kernel (static/embed-kernel.js)
#
# One function does the int8 matrix products of the model, as llama.cpp's
# CPU code does them: weights in Q8_0 (signed bytes and one scale per 32),
# the input (activations) quantised to Q8_0 by the engine first (signed
# bytes xq, row stride `in`, and scales xs[block * ts + token]). For two
# weight rows and four tokens at a time, each block of 32 gives an exact
# integer dot product (i32x4.dot_i16x8_s), which is then scaled and summed
# in f32 in a fixed order:
#
#     acc = fround(acc + fround(sum * fround(dw * dx)))     per block
#     y[t * out + o] = fround(acc + b[o])
#
# The engine's plain JavaScript path does exactly these operations, so the
# two give the same bits. T is the number of tokens rounded up to 4 (the
# engine's buffers have room for the extra rows, whose results it ignores),
# `in` a multiple of 32 and `out` even.
#
# The function bodies below are flat WebAssembly text, one instruction a
# line (a ";;" comment ends a line); wat2wasm accepts kernel_wat() as it is.

_KERNEL_MATMUL = """
    block $odone
    loop $oloop
      local.get $o
      local.get $out
      i32.ge_u
      br_if $odone
      ;; the two weight rows, their scales, and the bytes per scale row
      local.get $wq
      local.get $o
      local.get $in
      i32.mul
      i32.add
      local.tee $ra
      local.get $in
      i32.add
      local.set $rb
      local.get $in
      i32.const 3
      i32.shr_u
      local.set $nb4
      i32.const 0
      local.set $t
      block $tdone
      loop $tloop
        local.get $t
        local.get $T
        i32.ge_u
        br_if $tdone
        ;; the four token rows of xq
        local.get $xq
        local.get $t
        local.get $in
        i32.mul
        i32.add
        local.tee $x0
        local.get $in
        i32.add
        local.tee $x1
        local.get $in
        i32.add
        local.tee $x2
        local.get $in
        i32.add
        local.set $x3
        local.get $zero
        local.tee $acca
        local.set $accb
        local.get $wd
        local.get $o
        local.get $nb4
        i32.mul
        i32.add
        local.tee $da
        local.get $nb4
        i32.add
        local.set $db
        local.get $xs
        local.get $t
        i32.const 2
        i32.shl
        i32.add
        local.set $sp
        i32.const 0
        local.set $k
        block $kdone
        loop $kloop
          local.get $k
          local.get $in
          i32.ge_u
          br_if $kdone
          local.get $zero
          local.tee $a0
          local.tee $a1
          local.tee $a2
          local.tee $a3
          local.tee $c0
          local.tee $c1
          local.tee $c2
          local.set $c3
          local.get $k
          local.set $h
          local.get $k
          i32.const 32
          i32.add
          local.set $e
          block $hdone
          loop $hloop
            local.get $h
            local.get $e
            i32.ge_u
            br_if $hdone
            local.get $ra
            local.get $h
            i32.add
            v128.load align=1
            local.tee $v
            i16x8.extend_low_i8x16_s
            local.set $wal
            local.get $v
            i16x8.extend_high_i8x16_s
            local.set $wah
            local.get $rb
            local.get $h
            i32.add
            v128.load align=1
            local.tee $v
            i16x8.extend_low_i8x16_s
            local.set $wbl
            local.get $v
            i16x8.extend_high_i8x16_s
            local.set $wbh
            ;; token 0
            local.get $x0
            local.get $h
            i32.add
            v128.load align=1
            local.tee $v
            i16x8.extend_low_i8x16_s
            local.set $xl
            local.get $v
            i16x8.extend_high_i8x16_s
            local.set $xh
            local.get $a0
            local.get $wal
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wah
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $a0
            local.get $c0
            local.get $wbl
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wbh
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $c0
            ;; token 1
            local.get $x1
            local.get $h
            i32.add
            v128.load align=1
            local.tee $v
            i16x8.extend_low_i8x16_s
            local.set $xl
            local.get $v
            i16x8.extend_high_i8x16_s
            local.set $xh
            local.get $a1
            local.get $wal
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wah
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $a1
            local.get $c1
            local.get $wbl
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wbh
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $c1
            ;; token 2
            local.get $x2
            local.get $h
            i32.add
            v128.load align=1
            local.tee $v
            i16x8.extend_low_i8x16_s
            local.set $xl
            local.get $v
            i16x8.extend_high_i8x16_s
            local.set $xh
            local.get $a2
            local.get $wal
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wah
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $a2
            local.get $c2
            local.get $wbl
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wbh
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $c2
            ;; token 3
            local.get $x3
            local.get $h
            i32.add
            v128.load align=1
            local.tee $v
            i16x8.extend_low_i8x16_s
            local.set $xl
            local.get $v
            i16x8.extend_high_i8x16_s
            local.set $xh
            local.get $a3
            local.get $wal
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wah
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $a3
            local.get $c3
            local.get $wbl
            local.get $xl
            i32x4.dot_i16x8_s
            i32x4.add
            local.get $wbh
            local.get $xh
            i32x4.dot_i16x8_s
            i32x4.add
            local.set $c3
            local.get $h
            i32.const 16
            i32.add
            local.set $h
            br $hloop
          end
          end
          ;; acca += f32(sums of row a for tokens t..t+3) * (dw * dx)
          local.get $a0
          local.get $a1
          local.get $a2
          local.get $a3
          call $hsum4
          f32x4.convert_i32x4_s
          local.get $da
          f32.load
          f32x4.splat
          local.get $sp
          v128.load align=4
          local.tee $dx
          f32x4.mul
          f32x4.mul
          local.get $acca
          f32x4.add
          local.set $acca
          local.get $c0
          local.get $c1
          local.get $c2
          local.get $c3
          call $hsum4
          f32x4.convert_i32x4_s
          local.get $db
          f32.load
          f32x4.splat
          local.get $dx
          f32x4.mul
          f32x4.mul
          local.get $accb
          f32x4.add
          local.set $accb
          local.get $da
          i32.const 4
          i32.add
          local.set $da
          local.get $db
          i32.const 4
          i32.add
          local.set $db
          local.get $sp
          local.get $ts
          i32.const 2
          i32.shl
          i32.add
          local.set $sp
          local.get $k
          i32.const 32
          i32.add
          local.set $k
          br $kloop
        end
        end
        ;; + the biases, stored for the four tokens
        local.get $acca
        local.get $b
        local.get $o
        i32.const 2
        i32.shl
        i32.add
        local.tee $bo
        f32.load
        f32x4.splat
        f32x4.add
        local.set $acca
        local.get $accb
        local.get $bo
        f32.load offset=4
        f32x4.splat
        f32x4.add
        local.set $accb
        local.get $out
        i32.const 2
        i32.shl
        local.set $outb
        local.get $y
        local.get $t
        local.get $out
        i32.mul
        local.get $o
        i32.add
        i32.const 2
        i32.shl
        i32.add
        local.tee $yo
        local.get $acca
        f32x4.extract_lane 0
        f32.store
        local.get $yo
        local.get $accb
        f32x4.extract_lane 0
        f32.store offset=4
        local.get $yo
        local.get $outb
        i32.add
        local.tee $yo
        local.get $acca
        f32x4.extract_lane 1
        f32.store
        local.get $yo
        local.get $accb
        f32x4.extract_lane 1
        f32.store offset=4
        local.get $yo
        local.get $outb
        i32.add
        local.tee $yo
        local.get $acca
        f32x4.extract_lane 2
        f32.store
        local.get $yo
        local.get $accb
        f32x4.extract_lane 2
        f32.store offset=4
        local.get $yo
        local.get $outb
        i32.add
        local.tee $yo
        local.get $acca
        f32x4.extract_lane 3
        f32.store
        local.get $yo
        local.get $accb
        f32x4.extract_lane 3
        f32.store offset=4
        local.get $t
        i32.const 4
        i32.add
        local.set $t
        br $tloop
      end
      end
      local.get $o
      i32.const 2
      i32.add
      local.set $o
      br $oloop
    end
    end
"""

# The sums of four i32x4 vectors' lanes, as one i32x4: [sum(a), sum(b), sum(c), sum(d)].
_KERNEL_HSUM4 = """
    local.get $a
    local.get $b
    i8x16.shuffle 0 1 2 3 4 5 6 7 16 17 18 19 20 21 22 23
    local.get $a
    local.get $b
    i8x16.shuffle 8 9 10 11 12 13 14 15 24 25 26 27 28 29 30 31
    i32x4.add
    local.set $a
    local.get $c
    local.get $d
    i8x16.shuffle 0 1 2 3 4 5 6 7 16 17 18 19 20 21 22 23
    local.get $c
    local.get $d
    i8x16.shuffle 8 9 10 11 12 13 14 15 24 25 26 27 28 29 30 31
    i32x4.add
    local.set $c
    local.get $a
    local.get $c
    i8x16.shuffle 0 1 2 3 8 9 10 11 16 17 18 19 24 25 26 27
    local.get $a
    local.get $c
    i8x16.shuffle 4 5 6 7 12 13 14 15 20 21 22 23 28 29 30 31
    i32x4.add
"""

# (name, export name or None, params, results, locals, body)
_KERNEL_FUNCTIONS = (
    ("hsum4", None, (("a", "v128"), ("b", "v128"), ("c", "v128"), ("d", "v128")), ("v128",),
     (), _KERNEL_HSUM4),
    ("matmul", "matmul",
     tuple((name, "i32") for name in ("xq", "xs", "T", "in", "wq", "wd", "b", "out", "y", "ts")),
     (),
     tuple((name, "i32") for name in ("o", "t", "k", "h", "e", "nb4", "ra", "rb", "da", "db",
                                      "x0", "x1", "x2", "x3", "sp", "bo", "yo", "outb"))
     + tuple((name, "v128") for name in ("zero", "v", "wal", "wah", "wbl", "wbh", "xl", "xh",
                                         "a0", "a1", "a2", "a3", "c0", "c1", "c2", "c3",
                                         "acca", "accb", "dx")),
     _KERNEL_MATMUL),
)

_VALUE_TYPES = {"i32": 0x7F, "v128": 0x7B}
# Opcodes: (bytes, immediate): None, "label", "local", "func", "i32", "mem" (natural
# alignment as log2), "lane", "lanes".
_OPCODES = {
    "block": (b"\x02", "block"), "loop": (b"\x03", "block"), "end": (b"\x0b", None),
    "br": (b"\x0c", "label"), "br_if": (b"\x0d", "label"), "call": (b"\x10", "func"),
    "local.get": (b"\x20", "local"), "local.set": (b"\x21", "local"),
    "local.tee": (b"\x22", "local"),
    "f32.load": (b"\x2a", ("mem", 2)), "f32.store": (b"\x38", ("mem", 2)),
    "i32.const": (b"\x41", "i32"), "i32.ge_u": (b"\x4f", None), "i32.add": (b"\x6a", None),
    "i32.mul": (b"\x6c", None), "i32.shl": (b"\x74", None), "i32.shr_u": (b"\x76", None),
    "v128.load": (b"\xfd\x00", ("mem", 4)), "i8x16.shuffle": (b"\xfd\x0d", "lanes"),
    "f32x4.splat": (b"\xfd\x13", None), "f32x4.extract_lane": (b"\xfd\x1f", "lane"),
    "i16x8.extend_low_i8x16_s": (b"\xfd\x87\x01", None),
    "i16x8.extend_high_i8x16_s": (b"\xfd\x88\x01", None),
    "i32x4.add": (b"\xfd\xae\x01", None), "i32x4.dot_i16x8_s": (b"\xfd\xba\x01", None),
    "f32x4.add": (b"\xfd\xe4\x01", None), "f32x4.mul": (b"\xfd\xe6\x01", None),
    "f32x4.convert_i32x4_s": (b"\xfd\xfa\x01", None),
}


def _uleb(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _sleb(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if (value == 0 and not byte & 0x40) or (value == -1 and byte & 0x40):
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _vector(items):
    return _uleb(len(items)) + b"".join(items)


def _section(number, payload):
    return bytes([number]) + _uleb(len(payload)) + payload


def _assemble_body(text, locals_, functions):
    """The code of a function body (flat text, see _KERNEL_MATMUL)."""
    code = bytearray()
    labels = []
    for line in text.splitlines():
        line = line.split(";;")[0].strip()
        if not line:
            continue
        words = line.split()
        name, args = words[0], words[1:]
        if name not in _OPCODES:
            raise ValueError("unknown instruction %s" % name)
        opcode, immediate = _OPCODES[name]
        code += opcode
        if immediate == "block":
            labels.append(args[0] if args else None)
            code += b"\x40"
        elif name == "end":
            labels.pop()
        elif immediate == "label":
            if labels.count(args[0]) != 1:
                raise ValueError("unknown or repeated label %s" % args[0])
            code += _uleb(len(labels) - 1 - labels.index(args[0]))
        elif immediate == "local":
            code += _uleb(locals_[args[0].lstrip("$")])
        elif immediate == "func":
            code += _uleb(functions[args[0].lstrip("$")])
        elif immediate == "i32":
            code += _sleb(int(args[0]))
        elif immediate == "lane":
            code += bytes([int(args[0])])
        elif immediate == "lanes":
            if len(args) != 16:
                raise ValueError("i8x16.shuffle needs 16 lanes")
            code += bytes(int(arg) for arg in args)
        elif isinstance(immediate, tuple):
            align, offset = immediate[1], 0
            for arg in args:
                key, _, value = arg.partition("=")
                if key == "offset":
                    offset = int(value)
                elif key == "align":
                    align = int(value).bit_length() - 1
                else:
                    raise ValueError("unknown memory argument %s" % arg)
            code += _uleb(align) + _uleb(offset)
    if labels:
        raise ValueError("a block is not ended")
    return bytes(code) + b"\x0b"


def kernel_wasm():
    """The kernel module's bytes (see the notes above _KERNEL_MATMUL)."""
    functions = {entry[0]: number for number, entry in enumerate(_KERNEL_FUNCTIONS)}
    types, signatures, exports, bodies = [], [], [], []
    for name, export, params, results, locals_, body in _KERNEL_FUNCTIONS:
        signature = (b"\x60" + _vector([bytes([_VALUE_TYPES[kind]]) for _n, kind in params])
                     + _vector([bytes([_VALUE_TYPES[kind]]) for kind in results]))
        if signature not in types:
            types.append(signature)
        signatures.append(_uleb(types.index(signature)))
        if export:
            exports.append(_uleb(len(export)) + export.encode("ascii") + b"\x00"
                           + _uleb(functions[name]))
        numbers = {}
        for number, (local, _kind) in enumerate(tuple(params) + tuple(locals_)):
            numbers[local] = number
        groups = []
        for _local, kind in locals_:
            if groups and groups[-1][1] == kind:
                groups[-1][0] += 1
            else:
                groups.append([1, kind])
        declared = _vector([_uleb(count) + bytes([_VALUE_TYPES[kind]])
                            for count, kind in groups])
        code = declared + _assemble_body(body, numbers, functions)
        bodies.append(_uleb(len(code)) + code)
    memory_export = _uleb(3) + b"mem" + b"\x02" + _uleb(0)
    module = b"\x00asm\x01\x00\x00\x00"
    module += _section(1, _vector(types))
    module += _section(3, _vector(signatures))
    module += _section(5, _vector([b"\x00" + _uleb(1)]))
    module += _section(7, _vector([memory_export] + exports))
    module += _section(10, _vector(bodies))
    return module


def kernel_wat():
    """The kernel as WebAssembly text (what wat2wasm makes the same bytes from)."""
    lines = ["(module", '  (memory (export "mem") 1)']
    for name, export, params, results, locals_, body in _KERNEL_FUNCTIONS:
        head = "  (func $%s" % name
        if export:
            head += ' (export "%s")' % export
        head += "".join(" (param $%s %s)" % param for param in params)
        head += "".join(" (result %s)" % kind for kind in results)
        lines.append(head)
        if locals_:
            lines.append("   " + "".join(" (local $%s %s)" % local for local in locals_))
        lines.extend(line.rstrip() for line in body.strip("\n").splitlines())
        lines.append("  )")
    lines.append(")")
    return "\n".join(lines) + "\n"


KERNEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static",
                           "embed-kernel.js")


def kernel_js():
    """The text of static/embed-kernel.js."""
    data = base64.b64encode(kernel_wasm()).decode("ascii")
    lines = ["/* The WebAssembly kernel of static/embed-engine.js: its int8 matrix products",
             "   (%d bytes of WebAssembly, base64). Made by browser_model.py (its" % len(
                 kernel_wasm()),
             "   kernel_wasm(), which holds the instructions as text): run",
             "   python browser_model.py --kernel to make this file again. */"]
    chunks = [data[start:start + 76] for start in range(0, len(data), 76)]
    lines.append('var EMBED_KERNEL = "' + '" +\n    "'.join(chunks) + '";')
    return "\n".join(lines) + "\n"


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="The query model of search by meaning in "
                                                 "published sites.")
    parser.add_argument("--kernel", action="store_true",
                        help="write static/embed-kernel.js (the engine's WebAssembly kernel)")
    parser.add_argument("--wat", action="store_true", help="print the kernel as text")
    parser.add_argument("--inspect", metavar="MODEL", help="say whether a GGUF model file can "
                                                           "run in a browser")
    parser.add_argument("--dense", default="", help="its Dense head (safetensors)")
    parser.add_argument("--dims", type=int, default=0, help="numbers the vectors keep")
    args = parser.parse_args(argv)
    if args.kernel:
        with open(KERNEL_FILE, "w", encoding="ascii", newline="\n") as handle:
            handle.write(kernel_js())
        print("Wrote %s (%d bytes of WebAssembly)." % (KERNEL_FILE, len(kernel_wasm())))
    if args.wat:
        sys.stdout.write(kernel_wat())
    if args.inspect:
        print(json.dumps(inspect(args.inspect, args.dense or None, args.dims), indent=2))
    if not (args.kernel or args.wat or args.inspect):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
