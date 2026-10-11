#!/usr/bin/env python3
"""A fake LLM server for the tests, listening on 127.0.0.1 only.

It speaks just enough of three APIs for llm.py:

    POST /v1/chat/completions   OpenAI-compatible, streamed (server-sent
                                events) or not, as the request asks
    POST /v1/messages           Claude Messages API (not streamed)
    POST /api/chat              Ollama native chat (not streamed)
    POST /api/show              Ollama model details (capabilities)
    GET  /props, /v1/props      llama.cpp's model information
    GET  /health                llama.cpp's readiness (scripted statuses)
    GET  /v1/models, /models    model list (OpenAI and Anthropic form)
    GET  /api/tags, /api/version  Ollama model list and version

Every request is recorded (system prompt, user text, decoded image bytes,
MIME type, the order of the content parts, headers, payload), so tests can
check exactly what would have been sent to a model.

    with fake_llm.FakeLLM() as server:
        server.reply = json.dumps({...})          # the default reply text
        server.queue(fake_llm.HttpError(500))    # one-off responses, in order
        server.queue(fake_llm.Reply("{...}", finish="length"))
        endpoint = {"kind": "openai", "base_url": server.base_url, "model": "m"}

Queued (or default) responses are reply strings, Reply objects (a reply
with options), HttpError objects (see the helpers below for common
provider errors), Disconnect, Redirect or RawStream objects.
"""

import base64
import collections
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A reply that is a valid (core profile) version 5 description.
DEFAULT_REPLY = {
    "description": "A small test picture with a red square on a blue background.",
    "title": "Red square on a blue background",
    "photo_type": "artwork",
    "document_type": "none",
    "text": "",
    "text_language": "",
    "setting": "unknown",
    "scene": "",
    "time_of_day": "unknown",
    "event": "none",
    "people": "none",
    "people_descriptors": [],
    "subjects": ["square", "background"],
    "animals": [],
    "food": [],
    "vehicles": [],
    "colors": ["red", "blue"],
    "tags": ["test", "red", "blue"],
    "quality_flags": [],
    "sensitive": "none",
    "private_info": False,
    "aesthetic": 2,
    "sharpness": 5,
    "exposure": 4,
}

# A description in the version 4 format (older rows in a database).
V4_REPLY = {
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
DESCRIBE_KINDS = ("openai", "claude", "ollama")


class HttpError:
    """A queued response: fail with this HTTP status (and optional body and headers)."""

    def __init__(self, status, body=None, headers=None):
        self.status = status
        if body is None:
            body = {"error": {"message": "fake error %d" % status}}
        self.body = body if isinstance(body, str) else json.dumps(body)
        self.headers = dict(headers or {})


class Disconnect:
    """A queued response: close the connection without answering."""


class Redirect:
    """A queued response: redirect (301, 302, 307, ...) to location."""

    def __init__(self, location, status=302):
        self.location = location
        self.status = status


class RawStream:
    """A queued response: 200 text/event-stream with exactly this body text."""

    def __init__(self, text):
        self.text = text


class Reply:
    """A queued (or default) successful reply with options.

    text: the reply text (None: the server's default reply).
    finish: "stop", "length", "refusal" or "filtered", sent in each API's
        own terms (finish_reason, stop_reason, done_reason).
    refusal: the refusal message (OpenAI's refusal, Claude's stop_details).
    thinking: thinking text sent before the answer (a Claude thinking
        block, an OpenAI reasoning_content delta, Ollama's message.thinking).
    usage: (input tokens, output tokens), or None to report no usage.
    model: the model name the reply reports (None: the requested one).
    delay: seconds to wait before answering.
    stream_error: an error object sent in the middle of a streamed reply
        (as OpenRouter does), after the first piece of text.
    done: False to end a stream without a finish reason or [DONE].
    """

    def __init__(self, text=None, finish="stop", refusal=None, thinking=None,
                 usage=(10, 20), model=None, delay=0.0, stream_error=None, done=True):
        self.text = text
        self.finish = finish
        self.refusal = refusal
        self.thinking = thinking
        self.usage = usage
        self.model = model
        self.delay = delay
        self.stream_error = stream_error
        self.done = done


# -- common provider errors -----------------------------------------------------

def rate_limited(retry_after=None, retry_after_ms=None, message="Rate limit reached"):
    """429 with an optional Retry-After (seconds or date) / retry-after-ms header."""
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    if retry_after_ms is not None:
        headers["retry-after-ms"] = str(retry_after_ms)
    return HttpError(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                       "message": message}}, headers)


def gemini_rate_limited(delay="27s"):
    """Gemini's 429: a list body with RetryInfo.retryDelay and a quota message."""
    return HttpError(429, [{"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, please check your plan and billing "
                   "details.",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                     "retryDelay": delay}]}}])


def overloaded():
    """Claude's 529 overloaded_error."""
    return HttpError(529, {"type": "error", "error": {"type": "overloaded_error",
                                                       "message": "Overloaded"}})


def unauthorized(echo=""):
    """401; echo is put in the message (as services do with part of a bad key)."""
    message = "Incorrect API key provided: %s" % echo if echo else "invalid x-api-key"
    return HttpError(401, {"type": "error", "error": {"type": "authentication_error",
                                                       "message": message}})


def no_credit():
    """402 (OpenRouter: out of credits)."""
    return HttpError(402, {"error": {"code": 402, "message": "Insufficient credits"}})


def spend_limit():
    """Claude's 429 for a reached spend limit (no Retry-After): fatal."""
    return HttpError(429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": "You have reached your spend limit.",
        "details": {"error_code": "enforced_spend_limit_reached"}}})


def insufficient_quota():
    """OpenAI's 429 when the account has no quota left: fatal."""
    return HttpError(429, {"error": {
        "message": "You exceeded your current quota, please check your plan and billing "
                   "details.", "type": "insufficient_quota", "code": "insufficient_quota"}})


def schema_error():
    """400 for a schema keyword the service does not accept."""
    return HttpError(400, {"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "output_config.format.schema: For 'integer' type, property 'maximum' is "
                   "not supported"}})


def image_error():
    """400 for an image the service will not take."""
    return HttpError(400, {"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "messages.0.content.0.image.source.base64: image exceeds 5 MB maximum"}})


def no_vision_error():
    """400 from a llama.cpp server started without its vision projector."""
    return HttpError(400, {"error": {
        "code": 400, "type": "invalid_request_error",
        "message": "image input is not supported - hint: if this is unexpected, you may "
                   "need to provide the mmproj"}})


class FakeLLM:
    """A threaded HTTP server on an ephemeral 127.0.0.1 port."""

    def __init__(self, reply=None, props=True):
        self.reply = reply if reply is not None else json.dumps(DEFAULT_REPLY)
        self.props = props            # False: /props answers 404 (not llama.cpp)
        self.props_data = {"model_path": MODEL_PATH, "total_slots": 1,
                           "modalities": {"vision": True}}
        self.props_status = 200       # 503: still loading
        self.health_status = 200      # /health once the script below is used up
        self.health_script = collections.deque()
        self.models = ["fake-model", "other-model"]
        self.capabilities = ["completion", "vision"]
        self.reject_stream_options = False
        self.redirect = None          # a Redirect: every request (GET or POST) gets it
        self.requests = []
        self.in_flight = 0
        self.max_in_flight = 0
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
        """Queue one-off responses: reply strings, Reply, HttpError or Disconnect objects."""
        with self._lock:
            self._script.extend(responses)

    def _next_response(self):
        with self._lock:
            if self._script:
                return self._script.popleft()
            return self.reply

    def _next_health(self):
        with self._lock:
            if self.health_script:
                return self.health_script.popleft()
            return self.health_status

    def _record(self, entry):
        with self._lock:
            self.requests.append(entry)

    def _enter(self):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def _leave(self):
        with self._lock:
            self.in_flight -= 1

    def describe_requests(self):
        """The recorded describe requests (not probes, health checks or model lists)."""
        with self._lock:
            return [entry for entry in self.requests if entry["kind"] in DESCRIBE_KINDS]

    def other_requests(self):
        """The recorded requests that are not describe requests."""
        with self._lock:
            return [entry for entry in self.requests if entry["kind"] not in DESCRIBE_KINDS]

    def images(self):
        """Decoded image bytes of every describe request, in arrival order."""
        return [entry["image"] for entry in self.describe_requests()]


def _split_data_url(url):
    """('image/jpeg', bytes) from 'data:image/jpeg;base64,....'."""
    header, _, data = url.partition(",")
    mime = header[5:].split(";")[0] if header.startswith("data:") else ""
    return mime, base64.b64decode(data)


def _user_message(messages):
    for message in reversed(messages):
        if message.get("role") == "user":
            return message
    return messages[-1]


def _system_text(messages):
    return "".join(message.get("content", "") for message in messages
                   if message.get("role") == "system" and isinstance(message.get("content"), str))


class _Handler(BaseHTTPRequestHandler):
    fake = None

    def log_message(self, *args):
        pass

    def _headers(self):
        """Request headers with lower-case names."""
        return {name.lower(): value for name, value in self.headers.items()}

    def _send_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error):
        body = error.body.encode("utf-8")
        self.send_response(error.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in error.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, redirect):
        self.send_response(redirect.status)
        self.send_header("Location", redirect.location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        fake = self.fake
        if fake.redirect is not None:
            fake._record({"kind": "redirected", "path": self.path, "headers": self._headers()})
            self._send_redirect(fake.redirect)
            return
        if path in ("/props", "/v1/props"):
            fake._record({"kind": "props", "path": self.path, "headers": self._headers()})
            if not fake.props:
                self._send_json(404, {"error": "not found"})
            elif fake.props_status != 200:
                self._send_json(fake.props_status, {"error": {
                    "code": fake.props_status, "message": "Loading model",
                    "type": "unavailable_error"}})
            else:
                self._send_json(200, fake.props_data)
            return
        if path in ("/health", "/v1/health"):
            fake._record({"kind": "health", "path": self.path, "headers": self._headers()})
            status = fake._next_health()
            if status == 200:
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(status, {"error": {"code": status, "message": "Loading model",
                                                   "type": "unavailable_error"}})
            return
        if path in ("/v1/models", "/models"):
            fake._record({"kind": "models", "path": self.path, "headers": self._headers()})
            self._send_json(200, {"object": "list", "has_more": False,
                                  "data": [{"id": name, "object": "model", "type": "model"}
                                           for name in fake.models]})
            return
        if path == "/api/tags":
            fake._record({"kind": "models", "path": self.path, "headers": self._headers()})
            self._send_json(200, {"models": [{"name": name, "model": name}
                                             for name in fake.models]})
            return
        if path == "/api/version":
            fake._record({"kind": "version", "path": self.path, "headers": self._headers()})
            self._send_json(200, {"version": "0.0.1-fake"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        path = self.path.split("?")[0].rstrip("/")
        fake = self.fake
        if path == "/api/show":
            fake._record({"kind": "show", "path": self.path, "payload": payload,
                          "headers": self._headers()})
            self._send_json(200, {"capabilities": list(fake.capabilities)})
            return
        if path.endswith("/chat/completions"):
            kind = "openai"
            entry = self._parse_openai(payload)
        elif path.endswith("/v1/messages"):
            kind = "claude"
            entry = self._parse_claude(payload)
        elif path.endswith("/api/chat"):
            kind = "ollama"
            entry = self._parse_ollama(payload)
        else:
            self._send_json(404, {"error": "unknown path"})
            return
        entry.update({"kind": kind, "path": self.path, "payload": payload,
                      "headers": self._headers()})
        fake._record(entry)
        if kind == "openai" and fake.reject_stream_options and "stream_options" in payload:
            self._send_json(400, {"error": {"code": 400, "type": "invalid_request_error",
                                            "message": "Unrecognized request argument "
                                                       "supplied: stream_options"}})
            return
        response = fake.redirect if fake.redirect is not None else fake._next_response()
        if isinstance(response, str):
            response = Reply(response)
        if isinstance(response, Disconnect):
            self.close_connection = True
            return
        if isinstance(response, Redirect):
            self._send_redirect(response)
            return
        if isinstance(response, RawStream):
            body = response.text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if isinstance(response, HttpError):
            self._send_error(response)
            return
        fake._enter()
        try:
            if response.delay:
                time.sleep(response.delay)
            text = response.text
            if text is None:
                text = fake.reply if isinstance(fake.reply, str) else json.dumps(DEFAULT_REPLY)
            model = response.model if response.model is not None else payload.get("model", "")
            if kind == "openai":
                if payload.get("stream"):
                    want_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
                    self._stream_openai(response, text, model, want_usage)
                else:
                    self._send_openai(response, text, model)
            elif kind == "claude":
                self._send_claude(response, text, model)
            else:
                self._send_ollama(response, text, model)
        finally:
            fake._leave()

    # -- request parsing -------------------------------------------------------

    @staticmethod
    def _parse_openai(payload):
        messages = payload["messages"]
        entry = {"system": _system_text(messages), "prompt": "", "mime": "", "image": b"",
                 "parts": []}
        for part in _user_message(messages)["content"]:
            entry["parts"].append(part.get("type"))
            if part.get("type") == "text":
                entry["prompt"] = part["text"]
            elif part.get("type") == "image_url":
                entry["mime"], entry["image"] = _split_data_url(part["image_url"]["url"])
                entry["detail"] = part["image_url"].get("detail")
        return entry

    @staticmethod
    def _parse_claude(payload):
        system = payload.get("system", "")
        entry = {"system": system if isinstance(system, str) else json.dumps(system),
                 "prompt": "", "mime": "", "image": b"", "parts": []}
        for part in _user_message(payload["messages"])["content"]:
            entry["parts"].append(part.get("type"))
            if part.get("type") == "text":
                entry["prompt"] = part["text"]
            elif part.get("type") == "image":
                entry["mime"] = part["source"]["media_type"]
                entry["image"] = base64.b64decode(part["source"]["data"])
        return entry

    @staticmethod
    def _parse_ollama(payload):
        messages = payload["messages"]
        message = _user_message(messages)
        images = message.get("images") or []
        return {"system": _system_text(messages), "prompt": message.get("content", ""),
                "mime": "", "image": base64.b64decode(images[0]) if images else b"",
                "parts": ["images", "text"] if images else ["text"]}

    # -- replies ----------------------------------------------------------------

    def _stream_openai(self, reply, text, model, want_usage):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(chunk):
            self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode("utf-8"))

        def delta_chunk(delta):
            return {"object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}

        if reply.thinking:
            send(delta_chunk({"reasoning_content": reply.thinking}))
        size = max(1, len(text) // 3)
        pieces = [text[i:i + size] for i in range(0, len(text), size)] or [""]
        for number, piece in enumerate(pieces):
            send(delta_chunk({"content": piece}))
            if number == 0 and reply.stream_error is not None:
                send({"error": reply.stream_error})
                self.wfile.flush()
                return
        if reply.refusal:
            send(delta_chunk({"refusal": reply.refusal}))
        if not reply.done:
            self.wfile.flush()
            return
        finish = {"stop": "stop", "length": "length", "filtered": "content_filter",
                  "refusal": "stop"}.get(reply.finish, reply.finish)
        send({"object": "chat.completion.chunk", "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if want_usage and reply.usage is not None:
            send({"object": "chat.completion.chunk", "model": model, "choices": [],
                  "usage": {"prompt_tokens": reply.usage[0],
                            "completion_tokens": reply.usage[1],
                            "total_tokens": sum(reply.usage)}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_openai(self, reply, text, model):
        finish = {"stop": "stop", "length": "length", "filtered": "content_filter",
                  "refusal": "stop"}.get(reply.finish, reply.finish)
        body = {"object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": finish,
                             "message": {"role": "assistant",
                                         "content": None if reply.refusal else text,
                                         "refusal": reply.refusal}}]}
        if reply.usage is not None:
            body["usage"] = {"prompt_tokens": reply.usage[0],
                             "completion_tokens": reply.usage[1]}
        self._send_json(200, body)

    def _send_claude(self, reply, text, model):
        content = []
        if reply.thinking is not None:
            content.append({"type": "thinking", "thinking": reply.thinking,
                            "signature": "fake-signature"})
        if reply.finish != "refusal":
            content.append({"type": "text", "text": text})
        stop = {"stop": "end_turn", "length": "max_tokens", "refusal": "refusal",
                "filtered": "refusal"}.get(reply.finish, reply.finish)
        body = {"type": "message", "role": "assistant", "model": model,
                "content": content, "stop_reason": stop}
        if stop == "refusal":
            body["stop_details"] = {"type": "refusal", "category": "other",
                                    "explanation": reply.refusal or "refused"}
        if reply.usage is not None:
            body["usage"] = {"input_tokens": reply.usage[0], "output_tokens": reply.usage[1],
                             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self._send_json(200, body)

    def _send_ollama(self, reply, text, model):
        message = {"role": "assistant", "content": text}
        if reply.thinking:
            message["thinking"] = reply.thinking
        body = {"model": model, "message": message, "done": True,
                "done_reason": "length" if reply.finish == "length" else "stop"}
        if reply.usage is not None:
            body["prompt_eval_count"], body["eval_count"] = reply.usage
        self._send_json(200, body)
