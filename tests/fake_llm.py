#!/usr/bin/env python3
"""A fake LLM server for the tests, listening on 127.0.0.1 only.

It speaks just enough of three APIs for llm.py:

    POST /v1/chat/completions   OpenAI-compatible, streamed (server-sent events)
    POST /v1/messages           Claude Messages API (not streamed)
    POST /api/chat              Ollama native chat (not streamed)
    GET  /props, /v1/props      llama.cpp's model information

Every request is recorded (prompt, decoded image bytes, MIME type, headers,
payload), so tests can check exactly what would have been sent to a model.

    with fake_llm.FakeLLM() as server:
        server.reply = json.dumps({...})        # the default reply text
        server.queue(fake_llm.HttpError(500))  # one-off responses, in order
        endpoint = {"kind": "openai", "base_url": server.base_url, "model": "m"}
"""

import base64
import collections
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A reply that parses as a valid version 4 description.
DEFAULT_REPLY = {
    "description": "A small test picture with a red square on a blue background.",
    "subjects": ["square", "background"],
    "text": "",
    "tags": ["test", "red", "blue"],
    "colors": ["red", "blue"],
    "aesthetic_score": 0.4,
    "sharpness": 0.9,
    "exposure_score": 0.8,
    "watermark_score": 1.0,
}

MODEL_PATH = "/models/model.gguf"


class HttpError:
    """A queued response: fail with this HTTP status (and optional body)."""

    def __init__(self, status, body=None):
        self.status = status
        self.body = body if body is not None else json.dumps(
            {"error": {"message": "fake error %d" % status}})


class Disconnect:
    """A queued response: close the connection without answering."""


class FakeLLM:
    """A threaded HTTP server on an ephemeral 127.0.0.1 port."""

    def __init__(self, reply=None, props=True):
        self.reply = reply if reply is not None else json.dumps(DEFAULT_REPLY)
        self.props = props            # False: /props answers 404 (not llama.cpp)
        self.requests = []
        self._script = collections.deque()
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        owner = self

        class Handler(_Handler):
            fake = owner

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(10)
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    @property
    def root_url(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    @property
    def base_url(self):
        """The OpenAI-style base URL (ends in /v1)."""
        return self.root_url + "/v1"

    # -- scripting and inspection ---------------------------------------------

    def queue(self, *responses):
        """Queue one-off responses: reply strings, HttpError or Disconnect objects."""
        with self._lock:
            self._script.extend(responses)

    def _next_response(self):
        with self._lock:
            if self._script:
                return self._script.popleft()
            return self.reply

    def _record(self, entry):
        with self._lock:
            self.requests.append(entry)

    def describe_requests(self):
        """The recorded describe requests (not /props probes)."""
        with self._lock:
            return [entry for entry in self.requests if entry["kind"] != "props"]

    def images(self):
        """Decoded image bytes of every describe request, in arrival order."""
        return [entry["image"] for entry in self.describe_requests()]


def _split_data_url(url):
    """('image/jpeg', bytes) from 'data:image/jpeg;base64,....'."""
    header, _, data = url.partition(",")
    mime = header[5:].split(";")[0] if header.startswith("data:") else ""
    return mime, base64.b64decode(data)


class _Handler(BaseHTTPRequestHandler):
    fake = None

    def log_message(self, *args):
        pass

    def _headers(self):
        """Request headers with lower-case names."""
        return {name.lower(): value for name, value in self.headers.items()}

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error):
        body = error.body.encode("utf-8")
        self.send_response(error.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/props", "/v1/props"):
            self.fake._record({"kind": "props", "path": self.path,
                               "headers": self._headers()})
            if self.fake.props:
                self._send_json(200, {"model_path": MODEL_PATH, "total_slots": 1})
            else:
                self._send_json(404, {"error": "not found"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        path = self.path.rstrip("/")
        if path.endswith("/chat/completions"):
            kind = "openai"
            prompt, mime, image = self._parse_openai(payload)
        elif path.endswith("/v1/messages"):
            kind = "claude"
            prompt, mime, image = self._parse_claude(payload)
        elif path.endswith("/api/chat"):
            kind = "ollama"
            message = payload["messages"][0]
            prompt, mime, image = message.get("content", ""), "", base64.b64decode(
                message["images"][0])
        else:
            self._send_json(404, {"error": "unknown path"})
            return
        self.fake._record({"kind": kind, "path": self.path, "payload": payload,
                           "headers": self._headers(), "prompt": prompt,
                           "mime": mime, "image": image})
        response = self.fake._next_response()
        if isinstance(response, Disconnect):
            self.close_connection = True
            return
        if isinstance(response, HttpError):
            self._send_error(response)
            return
        if kind == "openai":
            self._stream_openai(response)
        elif kind == "claude":
            self._send_json(200, {"type": "message", "role": "assistant",
                                  "content": [{"type": "text", "text": response}],
                                  "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 10, "output_tokens": 20}})
        else:
            self._send_json(200, {"message": {"role": "assistant", "content": response},
                                  "done": True})

    @staticmethod
    def _parse_openai(payload):
        prompt, mime, image = "", "", b""
        for part in payload["messages"][0]["content"]:
            if part.get("type") == "text":
                prompt = part["text"]
            elif part.get("type") == "image_url":
                mime, image = _split_data_url(part["image_url"]["url"])
        return prompt, mime, image

    @staticmethod
    def _parse_claude(payload):
        prompt, mime, image = "", "", b""
        for part in payload["messages"][0]["content"]:
            if part.get("type") == "text":
                prompt = part["text"]
            elif part.get("type") == "image":
                mime = part["source"]["media_type"]
                image = base64.b64decode(part["source"]["data"])
        return prompt, mime, image

    def _stream_openai(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        size = max(1, len(text) // 3)
        pieces = [text[i:i + size] for i in range(0, len(text), size)] or [""]
        for piece in pieces:
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode("utf-8"))
        final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(("data: %s\n\n" % json.dumps(final)).encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
