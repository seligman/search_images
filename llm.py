#!/usr/bin/env python3
"""LLM endpoint calls using only the standard library.

Three endpoint kinds are supported: "openai", "claude", and "ollama". A local
llama.cpp server exposes an OpenAI compatible API, so use the "openai" kind
with its base URL for llama.cpp.
"""

import base64
import itertools
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import common
import imageinfo

MAX_TOKENS = 5000 # Max size to allow the response
PROPS_MAX_ATTEMPTS = 3
PROPS_RETRY_DELAY = 1.0

def image_mime(data, path=""):
    """The MIME type to send for image bytes, judged from their content.

    The file extension (and then JPEG) is only a fallback for bytes that
    imageinfo.sniff_mime() does not recognize, so a ".png" file holding
    JPEG data is sent as image/jpeg.
    """
    mime = imageinfo.sniff_mime(data)
    if mime:
        return mime
    guessed = mimetypes.guess_type(path or "")[0]
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/jpeg"


_log_counter = itertools.count(1)
_log_lock = threading.Lock()


def log_error(partial_response, error):
    """Write an error snapshot to data/logs if LOG_ERRORS is set.

    Each snapshot gets its own file, llm-error-<time>-<pid>-<n>.txt, holding
    the error message, a separator line and whatever part of the response
    arrived (the section is there even when empty, so the format stays
    predictable). Credentials are masked. Nothing is written to the current
    directory: replies can contain text read from private photos.
    """
    if "LOG_ERRORS" not in os.environ:
        return
    try:
        folder = common.ensure_dir(common.logs_dir())
        with _log_lock:
            number = next(_log_counter)
        name = "llm-error-%s-%d-%04d.txt" % (
            datetime.now().strftime("%Y%m%d-%H%M%S"), os.getpid(), number)
        with open(os.path.join(folder, name), "x", encoding="utf-8",
                  newline="") as handle:
            handle.write(common.redact(error) + "\n")
            handle.write("-" * 100 + "\n")
            handle.write(common.redact(partial_response))
    except OSError:
        # Debug logging must never break a describe run.
        pass


def _post(url, headers, payload):
    body = json.dumps(payload).encode("ascii")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    raw = ""
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        try:
            raw = error.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log_error(raw, error)
        raise
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        log_error(raw, error)
        raise


def describe_image(endpoint, prompt, image, schema=None, helper=None, mime=None):
    """Send one image to the configured endpoint and return the text reply.

    image is the image bytes to send (normally a JPEG made by
    images.prepare_for_llm), or a file path, which is read through the
    helper's load_image when it has one. mime defaults to the type sniffed
    from the bytes.

    When schema (a JSON Schema dict) is given it is used to constrain the
    reply where the endpoint supports it. Only the openai-kind path honors
    this today; claude and ollama rely on the prompt instead.
    """
    if isinstance(image, (bytes, bytearray, memoryview)):
        data, path = bytes(image), ""
    else:
        data, path = common.load_image_bytes(helper, image), image
    encoded = base64.b64encode(data).decode("ascii")
    kind = endpoint.get("kind", "openai")
    model = endpoint.get("model", "")
    base_url = endpoint.get("base_url", "").rstrip("/")
    api_key = endpoint.get("api_key", "")
    mime = mime or image_mime(data, path)
    if kind == "claude":
        return _call_claude(base_url, api_key, model, prompt, encoded, mime)
    if kind == "ollama":
        return _call_ollama(base_url, model, prompt, encoded)
    return _call_openai(base_url, api_key, model, prompt, encoded, mime, schema)


def _extract(result, path):
    """Walk a parsed JSON reply by keys/indices; log the body if it goes wrong."""
    try:
        node = result
        for step in path:
            node = node[step]
        return node.strip()
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        log_error(json.dumps(result), error)
        raise


def _call_openai(base_url, api_key, model, prompt, encoded, mime, schema=None):
    url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    # Constrain output: schema when given, otherwise any JSON object. Either
    # form structurally blocks the empty reserved tokens that wedge Gemma 3
    # on multi-tile vision inputs.
    if schema is not None:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "reply",
                "schema": schema,
                "strict": True,
            },
        }
    else:
        response_format = {"type": "json_object"}
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "response_format": response_format,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:" + mime + ";base64," + encoded}},
            ],
        }],
    }
    return _stream_openai(url, headers, payload)


def _stream_openai(url, headers, payload):
    """Stream a chat-completions reply, accumulating delta.content text.

    Streaming lets us log whatever arrived so far if the server times out,
    drops the connection, or sends a malformed chunk.
    """
    body = json.dumps(payload).encode("ascii")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    text = ""
    last_line = ""
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            for raw_line in response:
                last_line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                stripped = last_line.strip()
                if not stripped.startswith("data:"):
                    continue
                data = stripped[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str):
                    text += piece
    except urllib.error.HTTPError as error:
        extra = ""
        try:
            extra = error.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log_error(text + (("\n" + extra) if extra else ""), error)
        raise
    except Exception as error:
        log_error(text + (("\n" + last_line) if last_line else ""), error)
        raise
    return text.strip()


def _call_claude(base_url, api_key, model, prompt, encoded, mime):
    url = (base_url or "https://api.anthropic.com") + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": encoded}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    result = _post(url, headers, payload)
    return _extract(result, ("content", 0, "text"))


def _call_ollama(base_url, model, prompt, encoded):
    url = (base_url or "http://localhost:11434") + "/api/chat"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [encoded]}],
    }
    result = _post(url, headers, payload)
    return _extract(result, ("message", "content"))


_model_name_cache = {}


def _try_props(base_url):
    """Ask a llama.cpp-compatible server for the loaded model's file name.

    Retries on transient errors; raises RuntimeError if every attempt fails.
    """
    candidates = [base_url]
    # llama.cpp serves /props at the root, but the OpenAI-compatible base_url
    # often ends in /v1 -- try both.
    if base_url.endswith("/v1"):
        candidates.append(base_url[:-3])
    last_error = None
    for attempt in range(PROPS_MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(PROPS_RETRY_DELAY)
        for url in candidates:
            try:
                request = urllib.request.Request(url + "/props", method="GET")
                with urllib.request.urlopen(request, timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except Exception as error:
                last_error = error
                continue
            for key in ("model_path", "model_alias", "model"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value.replace("\\", "/").split("/")[-1]
            last_error = ValueError("/props response had no model name field")
    raise RuntimeError("could not read model name from " + base_url
                       + "/props after " + str(PROPS_MAX_ATTEMPTS)
                       + " attempts: " + str(last_error))


def probe_model_name(endpoint):
    """Return a display name for the model the endpoint is actually serving.

    For llama.cpp-style OpenAI-compatible endpoints this asks /props for the
    loaded weights file and raises if the probe ultimately fails; for other
    kinds it uses the model name from the endpoint config. Successful results
    are cached per endpoint.
    """
    kind = endpoint.get("kind", "openai")
    base_url = endpoint.get("base_url", "").rstrip("/")
    configured = endpoint.get("model", "")
    key = (kind, base_url, configured)
    if key in _model_name_cache:
        return _model_name_cache[key]
    name = configured
    if kind == "openai" and base_url:
        name = _try_props(base_url)
    _model_name_cache[key] = name
    return name


def extract_json(text):
    """Pull a JSON object out of an LLM reply.

    Raises ValueError if the reply does not contain a valid JSON object. Any
    text or Markdown fences around the object are ignored.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON in the response: " + str(error))
