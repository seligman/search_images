#!/usr/bin/env python3
"""Tiny made-up embedding models for the tests (GGUF files, as llama.cpp reads them).

write_bert() writes a deterministic BERT sentence-embedding model: a
WordPiece vocabulary of about 80 pieces (the special tokens, some words,
"##" pieces, letters, digits, punctuation and two CJK characters, stored
the way llama.cpp's converter stores them: U+2581 before the first piece
of a word), hidden size 32, 2 heads, 2 layers, feed-forward 64, a context
of 128 tokens, Q8_0 matrices (or F16 ones) and F32 norms and biases, mean
or CLS pooling. Its numbers come from a fixed seed, so the same call
always writes the same bytes. write_other() writes a model of another
kind (not BERT); write_dense() a sentence-transformers Dense head
(safetensors, F32). None of this is a real model: the vectors mean nothing.
"""

import json
import struct

HIDDEN = 32
HEADS = 2
LAYERS = 2
FFN = 64
CONTEXT = 128
WORD = "\u2581"

# (piece as llama.cpp stores it, token type): 3 is a control token.
SPECIAL = [("[PAD]", 3), ("[UNK]", 3), ("[CLS]", 3), ("[SEP]", 3), ("[MASK]", 3)]
WORDS = ["the", "a", "dog", "cat", "beach", "sun", "snow", "play", "kids", "on", "in",
         "cafe", "cr", "creme", "photo", "of", "at", "red", "car", "house", "sleep",
         "1", "2", "3", "$", ",", ".", "!", "?", "-", "'", "\u65e5", "\u672c", "\u00df"]
PIECES = ["ing", "s", "set", "e", "me", "ed", "er", "34", "56", "0", "1", "2", "3"]
LETTERS = "abcdefghijklmnopqrstuvwxyz"


def vocabulary():
    """[(stored piece, token type)] of the made-up model."""
    tokens = list(SPECIAL)
    tokens += [(WORD + word, 1) for word in WORDS]
    tokens += [(piece, 1) for piece in PIECES]
    tokens += [(WORD + letter, 1) for letter in LETTERS if letter not in ("a",)]
    tokens += [(letter, 1) for letter in LETTERS if letter not in ("e", "s")]
    return tokens


def word_pieces():
    """The vocabulary as WordPiece pieces ("word", "##piece"), in id order."""
    return [piece if kind == 3 else piece[1:] if piece.startswith(WORD) else "##" + piece
            for piece, kind in vocabulary()]


class _Numbers:
    """Deterministic made-up numbers in [-1, 1) (xorshift)."""

    def __init__(self, seed):
        self.state = (seed * 2654435761 + 1) & 0xFFFFFFFF or 1

    def next(self):
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x
        return x / 2147483648.0 - 1.0

    def many(self, count, scale, base=0.0):
        return [base + scale * self.next() for _ in range(count)]


def _string(text):
    data = text.encode("utf-8")
    return struct.pack("<Q", len(data)) + data


def _value(value):
    """(GGUF value type, bytes) of a metadata value."""
    if isinstance(value, bool):
        return 7, struct.pack("<?", value)
    if isinstance(value, int):
        return 4, struct.pack("<I", value)
    if isinstance(value, float):
        return 6, struct.pack("<f", value)
    if isinstance(value, str):
        return 8, _string(value)
    if isinstance(value, list):
        if value and isinstance(value[0], str):
            return 9, struct.pack("<IQ", 8, len(value)) + b"".join(_string(v) for v in value)
        return 9, struct.pack("<IQ", 5, len(value)) + b"".join(struct.pack("<i", v)
                                                               for v in value)
    raise TypeError(value)


def _q8_0(values):
    out = bytearray()
    for start in range(0, len(values), 32):
        block = values[start:start + 32]
        top = max(abs(v) for v in block)
        scale = top / 127.0
        out += struct.pack("<e", scale)
        factor = 127.0 / top if top else 0.0
        out += struct.pack("<32b", *[max(-127, min(127, int(round(v * factor)))) for v in block])
    return bytes(out)


def write_gguf(path, meta, tensors, alignment=32):
    """Write a GGUF (version 3) file: meta {key: value}, tensors [(name, dims, type
    ("F32", "F16" or "Q8_0"), numbers)]."""
    types = {"F32": 0, "F16": 1, "Q8_0": 8}
    blobs = []
    for name, dims, kind, values in tensors:
        if kind == "F32":
            data = struct.pack("<%df" % len(values), *values)
        elif kind == "F16":
            data = struct.pack("<%de" % len(values), *values)
        else:
            data = _q8_0(values)
        blobs.append(data)
    head = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(meta))
    for key, value in meta.items():
        kind, data = _value(value)
        head += _string(key) + struct.pack("<I", kind) + data
    offset = 0
    infos = b""
    for (name, dims, kind, _values), data in zip(tensors, blobs):
        infos += _string(name) + struct.pack("<I", len(dims))
        infos += b"".join(struct.pack("<Q", dim) for dim in dims)
        infos += struct.pack("<IQ", types[kind], offset)
        offset += len(data)
        offset = (offset + alignment - 1) // alignment * alignment
    head += infos
    body = bytearray(head)
    body += b"\0" * ((-len(body)) % alignment)
    for data in blobs:
        body += data
        body += b"\0" * ((-len(body)) % alignment)
    with open(path, "wb") as handle:
        handle.write(bytes(body))
    return path


def bert_tensors(seed=1, matrices="Q8_0", context=CONTEXT):
    """The tensors of the made-up BERT model: [(name, dims, type, numbers)]."""
    numbers = _Numbers(seed)
    vocab = len(vocabulary())
    tensors = [
        ("token_embd.weight", [HIDDEN, vocab], matrices, numbers.many(HIDDEN * vocab, 1.0)),
        ("position_embd.weight", [HIDDEN, context], "F32", numbers.many(HIDDEN * context, 0.2)),
        ("token_types.weight", [HIDDEN, 2], "F32", numbers.many(HIDDEN * 2, 0.1)),
        ("token_embd_norm.weight", [HIDDEN], "F32", numbers.many(HIDDEN, 0.1, 1.0)),
        ("token_embd_norm.bias", [HIDDEN], "F32", numbers.many(HIDDEN, 0.05)),
    ]
    for layer in range(LAYERS):
        p = "blk.%d." % layer
        for part in ("attn_q", "attn_k", "attn_v", "attn_output"):
            tensors.append((p + part + ".weight", [HIDDEN, HIDDEN], matrices,
                            numbers.many(HIDDEN * HIDDEN, 0.3)))
            tensors.append((p + part + ".bias", [HIDDEN], "F32", numbers.many(HIDDEN, 0.05)))
        tensors.append((p + "attn_output_norm.weight", [HIDDEN], "F32",
                        numbers.many(HIDDEN, 0.1, 1.0)))
        tensors.append((p + "attn_output_norm.bias", [HIDDEN], "F32", numbers.many(HIDDEN, 0.05)))
        tensors.append((p + "ffn_up.weight", [HIDDEN, FFN], matrices,
                        numbers.many(HIDDEN * FFN, 0.3)))
        tensors.append((p + "ffn_up.bias", [FFN], "F32", numbers.many(FFN, 0.05)))
        tensors.append((p + "ffn_down.weight", [FFN, HIDDEN], matrices,
                        numbers.many(FFN * HIDDEN, 0.2)))
        tensors.append((p + "ffn_down.bias", [HIDDEN], "F32", numbers.many(HIDDEN, 0.05)))
        tensors.append((p + "layer_output_norm.weight", [HIDDEN], "F32",
                        numbers.many(HIDDEN, 0.1, 1.0)))
        tensors.append((p + "layer_output_norm.bias", [HIDDEN], "F32", numbers.many(HIDDEN, 0.05)))
    return tensors


def bert_meta(pooling="mean", context=CONTEXT, name="Fake Bert"):
    tokens = vocabulary()
    return {
        "general.architecture": "bert", "general.name": name, "general.file_type": 7,
        "bert.block_count": LAYERS, "bert.context_length": context,
        "bert.embedding_length": HIDDEN, "bert.feed_forward_length": FFN,
        "bert.attention.head_count": HEADS, "bert.attention.layer_norm_epsilon": 1e-12,
        "bert.attention.causal": False, "bert.pooling_type": {"mean": 1, "cls": 2}[pooling],
        "tokenizer.ggml.token_type_count": 2, "tokenizer.ggml.model": "bert",
        "tokenizer.ggml.pre": "jina-v2-en",
        "tokenizer.ggml.tokens": [piece for piece, _kind in tokens],
        "tokenizer.ggml.token_type": [kind for _piece, kind in tokens],
        "tokenizer.ggml.bos_token_id": 2, "tokenizer.ggml.eos_token_id": 3,
        "tokenizer.ggml.unknown_token_id": 1, "tokenizer.ggml.seperator_token_id": 3,
        "tokenizer.ggml.padding_token_id": 0, "tokenizer.ggml.mask_token_id": 4,
        "tokenizer.ggml.add_bos_token": True, "tokenizer.ggml.add_eos_token": True,
    }


def write_bert(path, pooling="mean", matrices="Q8_0", seed=1, context=CONTEXT):
    """Write the made-up BERT model; returns path."""
    return write_gguf(path, bert_meta(pooling, context),
                      bert_tensors(seed, matrices, context))


def write_other(path, arch="qwen3", name="Qwen3 Embedding 0.6B"):
    """Write a model of another kind than BERT (a header and one tensor)."""
    meta = {"general.architecture": arch, "general.name": name,
            "%s.context_length" % arch: 32768, "%s.embedding_length" % arch: 32,
            "tokenizer.ggml.model": "gpt2", "tokenizer.ggml.tokens": ["a", "b"]}
    return write_gguf(path, meta, [("token_embd.weight", [32, 2], "F32", [0.5] * 64)])


def write_dense(path, out=48, cols=HIDDEN, seed=7, dtype="F32", bias=True):
    """Write a sentence-transformers Dense head (2_Dense/model.safetensors): weight
    [out, cols] and bias [out]; returns (path, weight rows, bias)."""
    numbers = _Numbers(seed)
    weight = numbers.many(out * cols, 0.4)
    biases = numbers.many(out, 0.05)
    size = 4 if dtype == "F32" else 2
    code = "<%d%s" % (out * cols, "f" if dtype == "F32" else "e")
    data = struct.pack(code, *weight)
    header = {"linear.weight": {"dtype": dtype, "shape": [out, cols],
                                "data_offsets": [0, out * cols * size]}}
    if bias:
        header["linear.bias"] = {"dtype": dtype, "shape": [out],
                                 "data_offsets": [len(data), len(data) + out * size]}
        data += struct.pack("<%d%s" % (out, "f" if dtype == "F32" else "e"), *biases)
    text = json.dumps(header).encode("utf-8")
    text += b" " * ((-len(text)) % 8)
    with open(path, "wb") as handle:
        handle.write(struct.pack("<Q", len(text)) + text + data)
    rows = [struct.unpack("<f", struct.pack("<f", v))[0] for v in weight]
    return path, [rows[r * cols:(r + 1) * cols] for r in range(out)], \
        [struct.unpack("<f", struct.pack("<f", v))[0] for v in biases] if bias else [0.0] * out
