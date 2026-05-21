#!/usr/bin/env python3
"""LLM endpoint calls using only the standard library.

Three endpoint kinds are supported: "openai", "claude", and "ollama". A local
llama.cpp server exposes an OpenAI compatible API, so use the "openai" kind
with its base URL for llama.cpp.
"""

import base64
import json
import os
import urllib.request

MAX_TOKENS = 5000 # Max size to allow the response

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
