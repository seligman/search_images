#!/usr/bin/env python3
"""LLM endpoint calls using only the standard library.

Three endpoint kinds are supported: "openai", "claude", and "ollama". A local
llama.cpp server exposes an OpenAI compatible API, so use the "openai" kind
with its base URL for llama.cpp.
"""

import base64
import json
import os
import time
import urllib.request

MAX_TOKENS = 5000 # Max size to allow the response
PROPS_MAX_ATTEMPTS = 3
PROPS_RETRY_DELAY = 1.0

def _mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


def _post(url, headers, payload):
    body = json.dumps(payload).encode("ascii")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def describe_image(endpoint, prompt, image_path):
    """Send one image to the configured endpoint and return the text reply."""
    with open(image_path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    kind = endpoint.get("kind", "openai")
    model = endpoint.get("model", "")
    base_url = endpoint.get("base_url", "").rstrip("/")
    api_key = endpoint.get("api_key", "")
    mime = _mime_for(image_path)
    if kind == "claude":
        return _call_claude(base_url, api_key, model, prompt, encoded, mime)
    if kind == "ollama":
        return _call_ollama(base_url, model, prompt, encoded)
    return _call_openai(base_url, api_key, model, prompt, encoded, mime)


def _call_openai(base_url, api_key, model, prompt, encoded, mime):
    url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:" + mime + ";base64," + encoded}},
            ],
        }],
    }
    result = _post(url, headers, payload)
    return result["choices"][0]["message"]["content"].strip()


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
    return result["content"][0]["text"].strip()


def _call_ollama(base_url, model, prompt, encoded):
    url = (base_url or "http://localhost:11434") + "/api/chat"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [encoded]}],
    }
    result = _post(url, headers, payload)
    return result["message"]["content"].strip()


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
