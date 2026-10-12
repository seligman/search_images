#!/usr/bin/env python3
"""A fake embeddings server for the tests, listening on 127.0.0.1 only.

It speaks just enough of two APIs for llm.embed():

    POST /v1/embeddings   OpenAI-compatible ({"input": [...]} ->
                          {"data": [{"index", "embedding"}], "model", "usage"})
    POST /api/embed       Ollama's own ({"input": [...]} -> {"embeddings": [...]})
    GET  /health          llama.cpp's readiness (200, or the scripted statuses)

A text's vector is made from its words (each word adds to a few of the
DIMS numbers, chosen by its hash), so texts that share words have close
vectors, like a real model's for texts that mean the same. Every request
is recorded (path, payload, headers). Like llama.cpp, a request with
"embd_normalize": -1 gets the raw vectors (raw_vector: not of unit length).

    with fake_embed_server.FakeEmbedder() as server:
        endpoint = {"name": "embed", "preset": "llamacpp", "base_url": server.base_url}
        server.queue(fake_embed_server.HttpError(503))     # one-off answers, in order
        server.too_long = "LONG"     # any batch holding a text with this is refused (400;
        server.too_long_status = 500  # or 500, as llama.cpp says it beyond its batch size)
        server.dims, server.served = 32, "other-model"     # another model from now on
        server.always_normalise = True   # unit vectors even when raw ones are asked for
        server.twist = True          # another model with the same name and size
"""

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMS = 64
MODEL = "fake-embedding"
_WORD_RE = re.compile(r"[a-z0-9]+")


def raw_vector(text, dims=DIMS, twist=False):
    """The fake model's raw vector of a text (as llama.cpp's with embd_normalize -1):
    word_vector times a length from 0.2 to 0.6 that depends on the text."""
    scale = 0.2 + 0.1 * (len(text) % 5)
    values = word_vector(text, dims, twist)
    return [value * scale for value in values]


def word_vector(text, dims=DIMS, twist=False):
    """The fake model's (unit length) vector of a text: a sum over its words."""
    values = [0.0] * dims
    for word in _WORD_RE.findall(text.lower()):
        digest = hashlib.sha1(word.encode("utf-8")).digest()
        for k in range(3):
            slot = digest[k] % dims
            values[slot] += 1.0 if digest[k + 8] & 1 else -1.0
    # A little of every dimension, so no text gives a zero vector.
    values = [value + 0.01 for value in values]
    if twist:
        values = values[::-1]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class HttpError:
    """A queued answer: fail with this status (and optional body)."""

    def __init__(self, status, body=None):
        self.status = status
        if body is None:
            body = {"error": {"message": "fake error %d" % status}}
        self.body = body if isinstance(body, str) else json.dumps(body)


class FakeEmbedder:
    """The server; a context manager. base_url is the OpenAI-style base (.../v1)."""

    def __init__(self, dims=DIMS):
        self.dims = dims
        self.served = MODEL     # the model's name in replies (unless the request names one)
        self.requests = []
        self.health = []        # statuses /health answers in turn (then 200)
        self.too_long = None
        self.too_long_status = 400
        self.always_normalise = False
        self.twist = False
        self._queue = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    def start(self):
        owner = self

        class Handler(_Handler):
            server_owner = owner
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def root_url(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def base_url(self):
        return self.root_url + "/v1"

    def queue(self, *answers):
        with self._lock:
            self._queue.extend(answers)

    def _next(self):
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    def _record(self, entry):
        with self._lock:
            self.requests.append(entry)

    def embed_requests(self):
        """The embedding requests' payloads, in order."""
        with self._lock:
            return [entry["payload"] for entry in self.requests
                    if entry["path"] in ("/v1/embeddings", "/api/embed")]

    def texts(self):
        """Every text sent to be embedded, in order."""
        out = []
        for payload in self.embed_requests():
            out.extend(payload.get("input") or [])
        return out


class _Handler(BaseHTTPRequestHandler):
    server_owner = None

    def log_message(self, *args):
        pass

    def _send_json(self, status, payload):
        body = payload if isinstance(payload, str) else json.dumps(payload)
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        owner = self.server_owner
        owner._record({"path": self.path, "payload": None, "headers": dict(self.headers)})
        if self.path == "/health":
            with owner._lock:
                status = owner.health.pop(0) if owner.health else 200
            self._send_json(status, {"status": "ok" if status == 200 else "loading"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        owner = self.server_owner
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        owner._record({"path": self.path, "payload": payload, "headers": dict(self.headers)})
        if self.path not in ("/v1/embeddings", "/api/embed"):
            self._send_json(404, {"error": "not found"})
            return
        queued = owner._next()
        if isinstance(queued, HttpError):
            self._send_json(queued.status, queued.body)
            return
        texts = payload.get("input")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list):
            self._send_json(400, {"error": {"message": "input must be a list"}})
            return
        if owner.too_long and any(owner.too_long in text for text in texts):
            if owner.too_long_status == 500:
                self._send_json(500, {"error": {"code": 500, "message": "input (900 tokens) is "
                                                "too large to process. increase the physical "
                                                "batch size (current batch size: 512)",
                                                "type": "server_error"}})
                return
            self._send_json(400, {"error": {"code": 400, "message": "request exceeds the "
                                            "available context size, try increasing it",
                                            "type": "exceed_context_size_error"}})
            return
        if payload.get("embd_normalize") == -1 and not owner.always_normalise:
            vectors = [raw_vector(text, owner.dims, owner.twist) for text in texts]
        else:
            vectors = [word_vector(text, owner.dims, owner.twist) for text in texts]
        model = payload.get("model") or owner.served
        if self.path == "/api/embed":
            self._send_json(200, {"model": model, "embeddings": vectors,
                                  "prompt_eval_count": sum(len(text.split()) for text in texts)})
            return
        # (Out of order, as the API may answer: each item says which text it is.)
        data = [{"object": "embedding", "index": index, "embedding": vector}
                for index, vector in reversed(list(enumerate(vectors)))]
        self._send_json(200, {"object": "list", "model": model, "data": data,
                              "usage": {"prompt_tokens": sum(len(text.split()) for text in texts),
                                        "total_tokens": 0}})
