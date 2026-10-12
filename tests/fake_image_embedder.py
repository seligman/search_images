#!/usr/bin/env python3
"""A fake image embedding server for the tests, listening on 127.0.0.1 only.

It speaks just enough of llama.cpp's server for looks.py:

    POST /v1/embeddings   {"input": [{"content": [{"type": "image_url",
                          "image_url": {"url": "data:image/jpeg;base64,..."}}]}, ...]}
                          -> {"data": [{"index", "embedding"}], "model", "usage"}
    GET  /health          200, or the scripted statuses

A picture's vector is made from its pixels (Pillow): the colours of a 4 x 4
grid and its brightness, so pictures that look alike have close vectors and
the same picture always has the same one. Every request is recorded.

    with fake_image_embedder.FakeImageEmbedder() as server:
        server.queue(fake_image_embedder.HttpError(503))   # one-off answers, in order
        server.drift = True          # every vector a little off (as flash attention on a GPU)
        server.dims = 32             # another length from now on
        server.served = "your-model.gguf"  # the model's name in replies
        server.refuse = lambda jpeg: ...   # True: a batch holding that picture gets a 400
"""

import base64
import io
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

DIMS = 64
MODEL = "fake-image-model"


def picture_vector(jpeg, dims=DIMS, drift=False):
    """The fake model's (unit length) vector of a picture's bytes."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    small = image.resize((4, 4), Image.BOX)
    values = [channel / 255.0 - 0.5 for pixel in small.getdata() for channel in pixel]
    values += [value / 255.0 - 0.5 for value in small.convert("L").getdata()]
    while len(values) < dims:
        values += values[:dims - len(values)]
    values = [value + 0.01 for value in values[:dims]]
    if drift:
        # (A different model's take on it: the vector turned a little.)
        values = [value + (0.35 if index % 3 == 0 else -0.2) for index, value in enumerate(values)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def pictures_of(item):
    """The JPEG bytes of the pictures in one input item (a content list)."""
    found = []
    for part in item.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") or ""
            if url.startswith("data:") and ";base64," in url:
                found.append(base64.b64decode(url.split(";base64,", 1)[1]))
    return found


class HttpError:
    """A queued answer: fail with this status (and optional body)."""

    def __init__(self, status, body=None):
        self.status = status
        if body is None:
            body = {"error": {"message": "fake error %d" % status}}
        self.body = body if isinstance(body, str) else json.dumps(body)


class FakeImageEmbedder:
    """The server; a context manager. base_url is the OpenAI-style base (.../v1)."""

    def __init__(self, dims=DIMS):
        self.dims = dims
        self.served = MODEL
        self.drift = False
        self.refuse = None
        self.requests = []
        self.health = []
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
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def queue(self, *answers):
        with self._lock:
            self._queue.extend(answers)

    def _next(self):
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    def embed_requests(self):
        """The embedding requests' payloads, in order."""
        with self._lock:
            return [entry["payload"] for entry in self.requests
                    if entry["path"] == "/v1/embeddings"]

    def pictures(self):
        """Every picture sent to be embedded (JPEG bytes), in order."""
        out = []
        for payload in self.embed_requests():
            for item in payload.get("input") or []:
                if isinstance(item, dict):
                    out.extend(pictures_of(item))
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
        with owner._lock:
            owner.requests.append({"path": self.path, "payload": None})
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
        with owner._lock:
            owner.requests.append({"path": self.path, "payload": payload})
        if self.path != "/v1/embeddings":
            self._send_json(404, {"error": "not found"})
            return
        queued = owner._next()
        if isinstance(queued, HttpError):
            self._send_json(queued.status, queued.body)
            return
        items = payload.get("input")
        if not isinstance(items, list):
            self._send_json(400, {"error": {"message": "input must be a list"}})
            return
        vectors = []
        for item in items:
            found = pictures_of(item) if isinstance(item, dict) else []
            if len(found) != 1:
                self._send_json(400, {"error": {"message": "expected one picture per input"}})
                return
            if owner.refuse is not None and owner.refuse(found[0]):
                self._send_json(400, {"error": {"code": 400, "message": "failed to load image",
                                                "type": "invalid_request_error"}})
                return
            try:
                vectors.append(picture_vector(found[0], owner.dims, owner.drift))
            except OSError:
                self._send_json(400, {"error": {"message": "failed to decode image"}})
                return
        data = [{"object": "embedding", "index": index, "embedding": vector}
                for index, vector in reversed(list(enumerate(vectors)))]
        self._send_json(200, {"object": "list", "model": owner.served, "data": data,
                              "usage": {"prompt_tokens": 100 * len(vectors), "total_tokens": 0}})
