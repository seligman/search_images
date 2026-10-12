#!/usr/bin/env python3
"""Make tests/fixtures/fake_bert_llamacpp.json: what llama.cpp makes of the made-up model.

tests/js/embed_engine_test.js checks the browser engine (static/
embed-engine.js) against this file: the token ids and vectors llama.cpp's
llama-server gives for the tiny made-up BERT models of tests/fake_gguf.py
(mean and CLS pooling), for texts that try the tokenizer (accents,
punctuation, CJK, emoji, numbers, control characters, long words, a text
longer than the model takes, the special tokens' own texts such as
"[MASK]", which llama.cpp's server reads as those tokens). The vectors are
llama.cpp's raw pooled ones
(the request field "embd_normalize": -1).

Only run when the file needs making again (a change to tests/fake_gguf.py):

    SEARCH_IMAGES_LLAMA_SERVER=/path/to/llama-server python tests/make_embed_fixture.py

Without SEARCH_IMAGES_LLAMA_SERVER it says so and does nothing. It starts
the server on 127.0.0.1 (a free port, CPU only: --device none) and stops
it again.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_gguf  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "fake_bert_llamacpp.json")
TEXTS = [
    "A dog on the beach",
    "Kids playing in the SNOW!",
    "Caf\u00e9 cr\u00e8me at the house",
    "the red car, the red house.",
    "$1,234.56 on 1 car",
    "\u65e5\u672c the cat",
    "\U0001F389 dog \u00df",
    "red\u0007car\u00a0sun\u200bset",
    "sleeping cats",
    "the " + "sleep" * 25 + " dog",
    "x" * 150,
    "a?b!c-d's",
    " ".join(["the dog on the beach at sunset"] * 30),
    "[MASK] [SEP] [CLS]",
    "the [UNK] dog[PAD]cat",
    "[mask] [Sep] dog [CLS",
]


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def post(port, path, body):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     json.dumps(body).encode("utf-8"),
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def run(binary, model, context):
    port = free_port()
    process = subprocess.Popen([binary, "-m", model, "--embeddings", "--host", "127.0.0.1",
                                "--port", str(port), "-c", str(context), "-ub", str(context),
                                "-b", str(context), "--device", "none", "-t", "2", "--no-webui"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2):
                    break
            except OSError:
                time.sleep(0.2)
        result = []
        for text in TEXTS:
            ids = post(port, "/tokenize", {"content": text, "add_special": True})["tokens"]
            entry = {"text": text, "ids": ids}
            if len(ids) <= context:
                reply = post(port, "/v1/embeddings", {"input": [text], "embd_normalize": -1})
                entry["vec"] = reply["data"][0]["embedding"]
            result.append(entry)
        return result
    finally:
        process.terminate()
        process.wait(timeout=30)


def main():
    binary = os.environ.get("SEARCH_IMAGES_LLAMA_SERVER", "")
    if not binary:
        print("Skipped: set SEARCH_IMAGES_LLAMA_SERVER to a llama-server program to make %s."
              % FIXTURE)
        return 0
    data = {"note": "llama.cpp's token ids and raw pooled vectors (embd_normalize -1) for the "
                    "made-up models of tests/fake_gguf.py; made by tests/make_embed_fixture.py",
            "context": fake_gguf.CONTEXT, "models": {}}
    with tempfile.TemporaryDirectory() as folder:
        for pooling in ("mean", "cls"):
            os.makedirs(os.path.join(folder, pooling))
            model = fake_gguf.write_bert(os.path.join(folder, pooling, "model.gguf"), pooling)
            data["models"][pooling] = run(binary, model, fake_gguf.CONTEXT)
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="ascii", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=True, indent=1)
        handle.write("\n")
    print("Wrote %s." % FIXTURE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
