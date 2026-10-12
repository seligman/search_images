#!/usr/bin/env python3
"""Talk to the LLM services that describe images, using only the standard library.

Three kinds of endpoint are supported (common.ENDPOINT_PRESETS lists the
presets built on them):

    openai   the chat completions API of OpenAI, also spoken by Azure OpenAI,
             Gemini, OpenRouter and local servers (llama.cpp, LM Studio,
             vLLM, Ollama's /v1)
    claude   Anthropic's Messages API
    ollama   Ollama's own /api/chat

embed() turns texts into vectors for search by meaning (embeddings.py),
through an OpenAI-compatible /embeddings API or Ollama's /api/embed.

describe() makes one request and returns the reply. Failures raise
ProviderError, whose kind says what to do about them:

    retryable  ask again after a pause (rate limits, overload, server errors,
               dropped connections, timeouts), as describe_with_retries() does
    fatal      no request can succeed (rejected key, no credit): stop the run
    endpoint   this endpoint's settings are wrong (unknown model, a parameter
               or schema the service rejects, no image support): stop using it
    image      the service rejected this image (too large, unreadable)
    content    the reply arrived but is unusable; raised by callers that parse
               replies (describe() reports cut-off and refused replies in its
               "finish" value instead of raising)

API keys come from common.resolve_api_key(). They are only ever sent in
request headers (redirects are never followed, so those headers never go to
another address), and every error message and debug log masks them with
common.redact(). Requests carry the prepared image and the prompt only: no
file name, path or EXIF data.
"""

import base64
import copy
import email.utils
import http.client
import itertools
import json
import math
import mimetypes
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import common
import imageinfo
import prompts

# Endpoint presets (kind, base URL, key variable, concurrency, notes).
PRESETS = common.ENDPOINT_PRESETS

ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = "search-images"
# Seconds allowed for the small requests of probe(), list_models() and
# wait_until_ready().
PROBE_TIMEOUT = 10
# describe_with_retries() waits at most this long between retries unless the
# server asks for longer...
MAX_BACKOFF = 60
# ...and does not wait at all when the server asks for more than this.
MAX_RETRY_AFTER = 600
# Characters of a provider's error message kept in a ProviderError.
ERROR_TEXT_LIMIT = 300

# Name given to the reply schema in OpenAI requests.
SCHEMA_NAME = "reply"

# 4xx statuses worth asking again for; every 5xx is too, except these two
# (the server does not implement what was asked, or not in this HTTP version).
RETRYABLE_STATUSES = (408, 409, 425, 429, 499)
NOT_RETRYABLE_SERVER_STATUSES = (501, 505)

# Error codes and texts that mean the money ran out: waiting does not help,
# and no endpoint of the account can work. (Ordinary rate-limit messages
# also mention quotas and billing -- "check your plan and billing details"
# -- so only these explicit ones count.)
_BILLING_MARKERS = (
    "enforced_spend_limit_reached", "insufficient_quota", "billing_hard_limit",
    "billing_not_active", "credit balance", "out of credits", "insufficient credits",
    "spend limit", "spending limit",
)
# A 429 for a daily quota: asking again soon does not help (a rolling daily
# limit frees one request now and then), but other endpoints still work, so
# this endpoint is set aside for the run instead of stopping it.
_DAILY_MARKERS = ("perday", "per_day", "per day")
# The service's content policy (moderation) blocked this image or the reply
# about it: the image's problem, not the endpoint's.
_POLICY_MARKERS = (
    "content_filter", "content filter", "content management policy",
    "responsibleaipolicyviolation", "content filtering policy", "content_policy",
    "content policy", "flagged", "moderation",
)
# A rejected API key, which some services report as 400 (Gemini).
_KEY_MARKERS = ("api_key_invalid", "api key not valid", "invalid api key",
                "incorrect api key", "invalid x-api-key")
# A rejected request that blames the model for not taking images at all.
_NO_VISION_MARKERS = (
    "mmproj", "image input is not supported", "does not support image",
    "do not support image", "not support vision", "vision is not supported",
    "images are not supported", "no image support", "only supported by certain models",
    "multimodal",
)
# A rejected request that blames the settings (checked before the image
# markers, since such messages may mention sizes too).
_SETTINGS_MARKERS = (
    "schema", "response_format", "output_config", "json_schema", "parameter",
    "max_tokens", "max_completion_tokens", "thinking", "chat_template", "stream_options",
    "grammar",
)
# A rejected request that blames the image.
_IMAGE_MARKERS = ("image", "too large", "dimension", "pixel", "size")

# Schema keywords providers reject in structured-output schemas.
_CLAUDE_DROPPED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                   "multipleOf", "minLength", "maxLength", "minItems", "maxItems")
_CLOUD_DROPPED = ("minLength", "maxLength")
# Schema keywords whose value is a map of names to schemas, one schema, or a
# list of schemas (everything else is copied as it is).
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions",
                "dependentSchemas")
_SCHEMA_ONE = ("items", "additionalProperties", "not", "contains", "if", "then", "else",
               "propertyNames", "additionalItems", "unevaluatedItems",
               "unevaluatedProperties")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")

# A DNS answer of "try again later" (EAI_AGAIN; WSATRY_AGAIN on Windows):
# a passing problem, unlike a name that does not exist.
_DNS_TRY_AGAIN = frozenset((getattr(socket, "EAI_AGAIN", -3), -3, 11002))
# TLS errors that mean a wrong address rather than a dropped connection
# (https:// to a plain-HTTP server, or the other way round).
_TLS_SETUP_MARKERS = ("WRONG_VERSION_NUMBER", "RECORD_LAYER_FAILURE",
                      "PACKET_LENGTH_TOO_LONG", "UNKNOWN_PROTOCOL", "HTTP_REQUEST")

_OPENAI_FINISH = {"stop": "ok", "length": "length", "content_filter": "filtered",
                  "tool_calls": "ok", "function_call": "ok"}
_CLAUDE_FINISH = {"max_tokens": "length", "model_context_window_exceeded": "length",
                  "refusal": "refusal"}

# Endpoints whose server rejected stream_options (keyed by base URL, name and
# model); they are sent requests without it from then on.
_no_stream_options = set()
_state_lock = threading.Lock()


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the API key headers (and the
    picture) to whatever address the reply names. A redirect reply is
    raised as an HTTPError instead, and reported as a wrong base URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _urlopen(request, timeout):
    """Open a request with the module's opener (no redirects)."""
    return _OPENER.open(request, timeout=timeout)


class ProviderError(Exception):
    """A request to an LLM endpoint failed.

    kind is one of KINDS (see the module docstring), status the HTTP status
    (None when no reply arrived) and retry_after the pause the server asked
    for, in seconds (None if it did not say). timeout is True when the
    server did not answer in time. The message is redacted: the secrets
    given, and anything shaped like a key, are masked.
    """

    KINDS = ("retryable", "fatal", "endpoint", "image", "content")

    def __init__(self, kind, message, status=None, retry_after=None, secrets=(),
                 timeout=False):
        message = common.redact(message, secrets)
        Exception.__init__(self, message)
        self.kind = kind if kind in self.KINDS else "endpoint"
        self.status = status
        self.retry_after = retry_after
        self.timeout = bool(timeout)
        self.message = message


# ---------------------------------------------------------------------------
# Small helpers

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


def log_error(partial_response, error, secrets=()):
    """Write an error snapshot to data/logs if LOG_ERRORS is set.

    Each snapshot gets its own file, llm-error-<time>-<pid>-<n>.txt, holding
    the error message, a separator line and whatever part of the response
    arrived (the section is there even when empty, so the format stays
    predictable). Credentials (the secrets given, and anything shaped like
    a key) are masked. Nothing is written to the current directory: replies
    can contain text read from private photos.
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
            handle.write(common.redact(error, secrets) + "\n")
            handle.write("-" * 100 + "\n")
            handle.write(common.redact(partial_response, secrets))
    except OSError:
        # Debug logging must never break a describe run.
        pass


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


# How many shorter versions of a cut-off reply repair_json tries at most.
REPAIR_TRIES = 200


def _closers(stack):
    return "".join("}" if opener == "{" else "]" for opener in reversed(stack))


def repair_json(text):
    """The JSON object a reply starts, closed and parsed when it was cut off.

    For a reply that stopped at the length limit (or broke off): an open
    string is closed (an unfinished escape dropped, and so is the first
    half of an escaped surrogate pair whose second half is missing), then
    open arrays and objects; when that is not valid JSON (the cut came
    after a key, a colon or inside a number or a word), the text is cut
    back to the last comma that ends a complete value, and so on. A number
    at the very end may have been cut short, so it is never used as it is
    (true, false and null are). A reply that holds a whole object is
    parsed as it is. Returns a dict; raises ValueError when nothing usable
    is found.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in the response")
    body = text[start:]
    stack = []
    in_string = escape = False
    hex_left = 0        # digits still to come of a \uXXXX escape
    escape_at = 0
    high = None         # (start, end) of the last complete \uD800-\uDBFF escape
    cuts = []           # (index of a comma, the openers before it)
    for index, char in enumerate(body):
        if in_string:
            if hex_left:
                hex_left -= 1
                if not hex_left and body[escape_at + 2:escape_at + 4].upper() in (
                        "D8", "D9", "DA", "DB"):
                    high = (escape_at, index + 1)
            elif escape:
                escape = False
                if char == "u":
                    hex_left = 4
            elif char == "\\":
                escape, escape_at = True, index
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or (char == "}") != (stack[-1] == "{"):
                raise ValueError("invalid JSON in the response")
            stack.pop()
            if not stack:
                try:
                    data = json.loads(body[:index + 1])
                except ValueError as error:
                    raise ValueError("invalid JSON in the response: %s" % error)
                if not isinstance(data, dict):
                    raise ValueError("the reply is not a JSON object")
                return data
        elif char == ",":
            cuts.append((index, tuple(stack)))
    candidates = []
    if in_string:
        # An unfinished escape (a lone backslash, part of \\uXXXX) is dropped,
        # and so is a high surrogate whose low half did not come (a lone
        # surrogate cannot be stored as UTF-8).
        kept = body[:escape_at] if escape or hex_left else body
        if high is not None and high[1] == len(kept):
            kept = body[:high[0]]
        candidates.append(kept + '"' + _closers(stack))
    else:
        tail = body.rstrip()
        word = re.search(r"[A-Za-z0-9.+-]+$", tail)
        if word is None or word.group(0) in ("true", "false", "null"):
            # (A number at the end may have been cut short: 12 cut to 1.)
            candidates.append(tail + _closers(stack))
    candidates.extend(body[:index] + _closers(openers)
                      for index, openers in reversed(cuts[-REPAIR_TRIES:]))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("the reply was cut off before any complete field")


def merge_body(body, extra):
    """A copy of a request body with extra merged in.

    Nested objects are merged key by key, other values replace what is
    there, and a None value removes the key (so an endpoint's "extra"
    setting can drop a field this module sends by default).
    """
    result = copy.deepcopy(body)
    for key, value in (extra or {}).items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_body(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def sanitize_schema(schema, endpoint):
    """A copy of a JSON schema without the keywords the endpoint's provider rejects.

    Claude drops number bounds (minimum, maximum, multipleOf, ...), string
    lengths (minLength, maxLength) and array lengths (minItems, maxItems).
    The other cloud presets (OpenAI, Azure, Gemini, OpenRouter, and custom
    services) drop the string lengths. Local servers keep everything: their
    grammars use the bounds to keep replies in shape. Bounds that are
    dropped are enforced again when the reply is parsed. Property names are
    never touched. None stays None.
    """
    if schema is None:
        return None
    settings = common.endpoint_settings(endpoint)
    if settings["kind"] == "claude":
        dropped = _CLAUDE_DROPPED
    elif settings["kind"] == "ollama" or settings["preset"] in common.LOCAL_PRESETS:
        dropped = ()
    else:
        dropped = _CLOUD_DROPPED
    return _strip_schema(schema, frozenset(dropped))


def _strip_schema(node, dropped):
    if isinstance(node, list):
        return [_strip_schema(item, dropped) for item in node]
    if not isinstance(node, dict):
        return copy.deepcopy(node)
    result = {}
    for key, value in node.items():
        if key in dropped:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: _strip_schema(sub, dropped) for name, sub in value.items()}
        elif key in _SCHEMA_ONE or key in _SCHEMA_LISTS:
            result[key] = _strip_schema(value, dropped)
        else:
            result[key] = copy.deepcopy(value)
    return result


def estimate_cost(endpoint, usage):
    """Estimated USD cost of one request from its usage, or None.

    None when the endpoint has no prices (price_in / price_out, USD per
    million tokens) or the usage is unknown.
    """
    settings = common.endpoint_settings(endpoint)
    price_in, price_out = settings["price_in"], settings["price_out"]
    if price_in is None and price_out is None:
        return None
    usage = usage if isinstance(usage, dict) else {}
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if tokens_in is None and tokens_out is None:
        return None
    cost = ((tokens_in or 0) * (price_in or 0.0) + (tokens_out or 0) * (price_out or 0.0)) / 1e6
    return round(cost, 8)


def _root_url(base_url, ollama=False):
    """A base URL without a trailing /v1: where llama.cpp serves /props and
    /health and Claude /v1/messages. For Ollama (ollama=True) a trailing
    /api goes too: it serves /api/chat. Other base paths are kept (a
    gateway's https://host/api for Claude means https://host/api/v1/messages)."""
    base = base_url.rstrip("/")
    for suffix in ("/v1", "/api") if ollama else ("/v1",):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _headers(settings, key):
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if settings["kind"] == "claude":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _usage(tokens_in, tokens_out):
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    return {"input_tokens": number(tokens_in), "output_tokens": number(tokens_out)}


def _read_body(response):
    try:
        return response.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _json_or_none(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Errors

def _header(headers, name):
    """A header value (case-insensitive name) from a message or dict, or None."""
    if headers is None:
        return None
    value = None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None and isinstance(headers, dict):
        for key, item in headers.items():
            if str(key).lower() == name:
                value = item
                break
    return value.strip() if isinstance(value, str) else None


_TRY_AGAIN_RE = re.compile(
    r"(?i)\b(?:try again|retry) in\s+((?:\d+(?:\.\d+)?\s*(?:ms|h|m|s)(?![a-z])\s*)+)")
_DURATION_PART_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(ms|h|m|s)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def retry_after_seconds(headers=None, body=""):
    """The pause a rate-limited or busy server asked for, in seconds, or None.

    Reads retry-after-ms, then Retry-After (seconds or an HTTP date), then
    Gemini's RetryInfo "retryDelay" ("27s") in the body, then a message
    saying "try again in 1.5s" / "retry in 7m12s" (OpenAI, Gemini).
    """
    value = _header(headers, "retry-after-ms")
    if value:
        try:
            return max(0.0, float(value) / 1000.0)
        except ValueError:
            pass
    value = _header(headers, "retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body or "")
    if match:
        return float(match.group(1))
    match = _TRY_AGAIN_RE.search(body or "")
    if match:
        return round(sum(float(number) * _DURATION_UNITS[unit.lower()]
                         for number, unit in _DURATION_PART_RE.findall(match.group(1))), 3)
    return None


def _has(text, markers):
    return any(marker in text for marker in markers)


# What llama.cpp's server says (with a 500) about an input too long to embed or
# describe in one batch: the input's problem, not a passing one.
_TOO_LARGE_MARKERS = ("is too large to process",)


def classify_http(status, body="", headers=None):
    """How to treat an HTTP error reply: returns (kind, retry_after).

    kind is a ProviderError kind; retry_after the pause the server asked for
    (seconds) or None. In order: a content-policy block (moderation) is the
    image's problem; a rejected key (401, or a key error in a 400), a
    billing code or 402/403 are fatal; a daily quota (429) sets the endpoint
    aside; an input too long for llama.cpp (a 500 saying so) is the image's
    (or text's); 408, 409, 425, 429, 499 and 5xx (but 501 and 505) are worth
    retrying; 413 and 400/422 blaming the image are the image's; the rest
    (wrong model, URL, parameters, schema, no image support) the endpoint's.
    """
    text = (body or "").lower()
    retry_after = retry_after_seconds(headers, body)
    if status in (400, 403, 422, 451) and _has(text, _POLICY_MARKERS):
        return "image", retry_after
    if status == 401 or (status in (400, 403) and _has(text, _KEY_MARKERS)):
        return "fatal", retry_after
    if 400 <= status < 500 and _has(text, _BILLING_MARKERS):
        return "fatal", retry_after
    if status in (402, 403):
        return "fatal", retry_after
    if status == 429 and _has(text, _DAILY_MARKERS):
        return "endpoint", retry_after
    if status >= 500 and _has(text, _TOO_LARGE_MARKERS):
        # (llama.cpp: an input longer than its batch; asking again cannot help.)
        return "image", retry_after
    if status in RETRYABLE_STATUSES or (
            status >= 500 and status not in NOT_RETRYABLE_SERVER_STATUSES):
        return "retryable", retry_after
    if status == 413:
        return "image", retry_after
    if status in (400, 422):
        if any(marker in text for marker in _NO_VISION_MARKERS):
            return "endpoint", retry_after
        if any(marker in text for marker in _SETTINGS_MARKERS):
            return "endpoint", retry_after
        if any(marker in text for marker in _IMAGE_MARKERS):
            return "image", retry_after
    return "endpoint", retry_after


def _error_details(body):
    """(type, message) from an error reply, whatever the provider's format."""
    data = _json_or_none(body)
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, str):
            return "", error
        if isinstance(error, dict):
            kind = error.get("type") or error.get("status") or error.get("code") or ""
            message = error.get("message") or error.get("detail") or ""
            if isinstance(message, (dict, list)):
                message = json.dumps(message)
            return str(kind), str(message)
    return "", (body or "").strip()


def _clip(text, secrets=(), limit=ERROR_TEXT_LIMIT):
    """A provider's error text on one line, with keys masked and then cut to limit.

    Masking comes first: a key cut in two by the limit would no longer be
    recognized, and its first part would show.
    """
    text = common.redact(" ".join(str(text or "").split()), secrets)
    return text if len(text) <= limit else text[:limit] + "..."


def error_from_http(status, body="", headers=None, secrets=()):
    """The ProviderError for an HTTP error reply (status, body text, headers)."""
    kind, retry_after = classify_http(status, body, headers)
    text = "HTTP %d" % status
    if 300 <= status < 400:
        location = _header(headers, "location") or "another address"
        text += (": the server redirects the request to %s (redirects are not followed; "
                 "set the base URL to the address the service really uses)"
                 % _clip(location, secrets))
        return ProviderError(kind, text, status=status, secrets=secrets)
    error_type, message = _error_details(body)
    if error_type and error_type != str(status):
        text += " (%s)" % _clip(error_type, secrets, 60)
    message = _clip(message, secrets)
    if message:
        text += ": " + message
    return ProviderError(kind, text, status=status, retry_after=retry_after, secrets=secrets)


def _transport_error(error, timeout, secrets):
    """The ProviderError for a request that got no (complete) HTTP reply."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return ProviderError("retryable", "no answer within %g seconds" % timeout,
                             secrets=secrets, timeout=True)
    if isinstance(reason, socket.gaierror):
        if reason.errno in _DNS_TRY_AGAIN:
            # The name server did not answer in time: try again later.
            return ProviderError("retryable", "could not look up the server just now (%s)"
                                 % reason, secrets=secrets)
        return ProviderError("endpoint", "could not find the server (%s)" % reason,
                             secrets=secrets)
    if isinstance(reason, ssl.SSLError):
        if isinstance(reason, ssl.SSLCertVerificationError) or _has(
                str(reason).upper(), _TLS_SETUP_MARKERS):
            # A certificate that does not check out, or https:// to a plain
            # HTTP server: asking again will not help.
            return ProviderError("endpoint", "secure connection failed (%s)" % reason,
                                 secrets=secrets)
        # A secure connection that dropped (TLS EOF, reset): a passing problem.
        return ProviderError("retryable", "the secure connection dropped (%s)" % reason,
                             secrets=secrets)
    if isinstance(reason, (OSError, http.client.HTTPException)):
        return ProviderError("retryable", "connection problem: %s"
                             % (str(reason) or type(reason).__name__), secrets=secrets)
    return ProviderError("endpoint", "%s: %s" % (type(error).__name__, reason),
                         secrets=secrets)


def _send(url, headers, payload, timeout, secrets):
    """POST a JSON payload; return the open response (use it in a with block).

    HTTP error replies and connection problems raise ProviderError (with the
    original exception as its __cause__).
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        return _urlopen(request, timeout)
    except urllib.error.HTTPError as error:
        body = _read_body(error)
        error.close()
        log_error(body, "HTTP %d from %s" % (error.code, url), secrets)
        raise error_from_http(error.code, body, error.headers, secrets) from error
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        raise _transport_error(error, timeout, secrets) from error


def _http(url, headers, payload=None, timeout=PROBE_TIMEOUT):
    """(status, body text) of a small request, whatever the HTTP status.

    Connection problems raise as they are (URLError, OSError, ...).
    """
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
    try:
        with _urlopen(request, timeout) as response:
            return response.status, _read_body(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, _read_body(error)
        finally:
            error.close()


def explain_error(error, endpoint=None):
    """One sentence about a failed request for people, naming the endpoint.

    For example "The OpenAI endpoint 'cloud' rejected the API key (401)."
    The key itself never appears; where it came from may.
    """
    if endpoint is not None:
        settings = common.endpoint_settings(endpoint)
        who = "The %s endpoint '%s'" % (PRESETS[settings["preset"]]["label"],
                                        settings["name"] or settings["model"])
        source = common.resolve_api_key(settings)[1]
    else:
        settings, who, source = None, "The endpoint", ""
    status = getattr(error, "status", None)
    kind = getattr(error, "kind", "")
    code = " (%d)" % status if status else ""
    detail = str(error)
    lowered = detail.lower()
    if status == 401 or (kind == "fatal" and _has(lowered, _KEY_MARKERS)):
        text = "%s rejected the API key%s." % (who, code)
        if source == "none":
            text += " No API key is set."
        elif source:
            text += " Check the key (%s)." % source
        return text
    if kind == "image" and _has(lowered, _POLICY_MARKERS):
        return "%s blocked the image under its content policy%s: %s" % (who, code, detail)
    if status == 403:
        return "%s refused access%s: %s" % (who, code, detail)
    if kind == "fatal":
        return "%s reports no credit or a spending limit%s: %s" % (who, code, detail)
    if kind == "endpoint":
        if status == 429:
            return "%s reached a usage limit%s: %s" % (who, code, detail)
        if status and 300 <= status < 400:
            return "%s redirects requests elsewhere%s: %s" % (who, code, detail)
        if "data policy" in lowered:
            return ("%s has no provider for the model that meets the privacy setting "
                    "(provider.data_collection is \"deny\")%s: %s" % (who, code, detail))
        if status == 404:
            return "%s does not know the model or the URL%s: %s" % (who, code, detail)
        if status is None:
            return "%s cannot be used: %s" % (who, detail)
        return "%s rejected the request settings%s: %s" % (who, code, detail)
    if kind == "image":
        return "%s rejected the image%s: %s" % (who, code, detail)
    if kind == "retryable":
        return "%s is unavailable or busy%s: %s" % (who, code, detail)
    return "%s gave an unusable reply: %s" % (who, detail)


# ---------------------------------------------------------------------------
# Requests, one kind of endpoint at a time

def _openai_request(settings, key, system, user_text, encoded, mime, schema, stream_options):
    content = [{"type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (mime, encoded), "detail": "high"}}]
    if user_text:
        content.append({"type": "text", "text": user_text})
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": content})
    payload = {"model": settings["model"], "messages": messages, "stream": True}
    if stream_options:
        payload["stream_options"] = {"include_usage": True}
    # OpenAI's own reasoning models only take max_completion_tokens; local
    # servers know max_tokens.
    tokens = ("max_completion_tokens" if settings["preset"] in ("openai", "azure")
              else "max_tokens")
    payload[tokens] = settings["max_output_tokens"]
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True,
                            "schema": sanitize_schema(schema, settings)},
        }
    else:
        # Any JSON object. Either form structurally blocks the empty
        # reserved tokens that wedge some models on multi-tile images.
        payload["response_format"] = {"type": "json_object"}
    if settings["preset"] in common.LLAMACPP_PRESETS:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if settings["preset"] == "openrouter":
        # Only route to providers that honour the response schema, and to
        # none that store or train on the pictures (override through the
        # endpoint's "extra" setting: {"provider": {"data_collection": "allow"}}).
        payload["provider"] = {"require_parameters": True, "data_collection": "deny"}
    return settings["base_url"] + "/chat/completions", _headers(settings, key), payload


def _describe_openai(settings, key, secrets, system, user_text, encoded, mime, schema,
                     request_extra, timeout):
    state_key = (settings["base_url"], settings["name"], settings["model"])
    with _state_lock:
        stream_options = state_key not in _no_stream_options
    url, headers, payload = _openai_request(settings, key, system, user_text, encoded, mime,
                                            schema, stream_options)
    payload = merge_body(merge_body(payload, settings["extra"]), request_extra)
    try:
        return _post_openai(url, headers, payload, timeout, secrets)
    except ProviderError as error:
        message = str(error).lower()
        if not ("stream_options" in payload and error.status in (400, 422)
                and ("stream_options" in message or "include_usage" in message)):
            raise
    # The server does not take stream_options: ask once more without it,
    # and never send it to this endpoint again.
    with _state_lock:
        _no_stream_options.add(state_key)
    payload.pop("stream_options", None)
    return _post_openai(url, headers, payload, timeout, secrets)


def _post_openai(url, headers, payload, timeout, secrets):
    with _send(url, headers, payload, timeout, secrets) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            # The server ignored "stream": one JSON reply.
            body = _read_reply_body(response, timeout, secrets)
            return _openai_message(_parse_reply_json(body, secrets), secrets)
        return _read_openai_stream(response, timeout, secrets)


def _read_reply_body(response, timeout, secrets):
    try:
        return response.read().decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise _transport_error(error, timeout, secrets) from error


def _parse_reply_json(body, secrets):
    try:
        data = json.loads(body)
    except ValueError as error:
        log_error(body, error, secrets)
        raise ProviderError("endpoint", "the reply is not JSON: %s" % error,
                            secrets=secrets) from error
    if not isinstance(data, dict):
        log_error(body, "the reply is not a JSON object", secrets)
        raise ProviderError("endpoint", "the reply is not a JSON object", secrets=secrets)
    return data


def _openai_finish(reason, refusal):
    if refusal:
        return "refusal"
    return _OPENAI_FINISH.get(reason or "stop", "ok")


def _openai_message(data, secrets):
    """The reply of a non-streamed chat completion."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        if isinstance(data.get("error"), dict):
            raise _stream_error(data["error"], secrets)
        log_error(json.dumps(data), "the reply has no choices", secrets)
        raise ProviderError("endpoint", "the reply has no choices", secrets=secrets)
    choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = message.get("content") if isinstance(message.get("content"), str) else ""
    refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else ""
    reason = choice.get("finish_reason") or ""
    if reason == "error":
        raise ProviderError("retryable", "the service failed while answering",
                            secrets=secrets)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {"text": text, "model": str(data.get("model") or ""),
            "finish": _openai_finish(reason, refusal), "reason": reason, "detail": refusal,
            "usage": _usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))}


def _stream_error(error, secrets):
    """The ProviderError for an error object sent inside a 200 reply."""
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 600:
        return error_from_http(code, json.dumps({"error": error}), None, secrets)
    _, message = _error_details(json.dumps({"error": error}))
    return ProviderError("retryable", "the service reported an error: %s"
                         % _clip(message, secrets), secrets=secrets)


def _error_object(text):
    """The error object of an error event in a reply stream ({"message": text} if not JSON)."""
    data = _json_or_none(text)
    if isinstance(data, dict):
        return data["error"] if isinstance(data.get("error"), (dict, str)) else data
    return {"message": text}


def _read_openai_stream(response, timeout, secrets):
    """Collect a streamed chat completion (server-sent events).

    Errors sent inside the stream count like an HTTP error with their
    "code": a data chunk with an "error" object (OpenRouter), an "error:"
    field (llama.cpp's llama-server), or the data of an "event: error".
    """
    text, refusal = [], []
    reason = None
    usage = {}
    model = ""
    done = False
    event = ""
    try:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                event = ""          # the end of an event
                continue
            if line.startswith("event:"):
                event = line[6:].strip().lower()
                continue
            if line.startswith("error:"):
                raise _stream_error(_error_object(line[6:].strip()), secrets)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if event == "error":
                raise _stream_error(_error_object(data), secrets)
            if data == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except ValueError as error:
                raise ProviderError("retryable", "malformed reply stream: %s" % error,
                                    secrets=secrets) from error
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise _stream_error(chunk["error"], secrets)
            if not model and isinstance(chunk.get("model"), str):
                model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                if isinstance(delta.get("content"), str):
                    text.append(delta["content"])
                if isinstance(delta.get("refusal"), str):
                    refusal.append(delta["refusal"])
                if choice.get("finish_reason"):
                    reason = choice["finish_reason"]
    except ProviderError as error:
        log_error("".join(text), error, secrets)
        raise
    except (OSError, http.client.HTTPException, ValueError) as error:
        log_error("".join(text), error, secrets)
        raise _transport_error(error, timeout, secrets) from error
    if reason == "error":
        log_error("".join(text), "finish_reason error", secrets)
        raise ProviderError("retryable", "the service failed while answering",
                            secrets=secrets)
    if reason is None and not done:
        log_error("".join(text), "the reply stream ended early", secrets)
        raise ProviderError("retryable", "the reply stream ended early", secrets=secrets)
    refusal_text = "".join(refusal)
    return {"text": "".join(text), "model": model,
            "finish": _openai_finish(reason, refusal_text), "reason": reason or "",
            "detail": refusal_text,
            "usage": _usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))}


def _claude_request(settings, key, system, user_text, encoded, mime, schema):
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": mime, "data": encoded}}]
    if user_text:
        content.append({"type": "text", "text": user_text})
    payload = {"model": settings["model"], "max_tokens": settings["max_output_tokens"],
               "messages": [{"role": "user", "content": content}]}
    if system:
        payload["system"] = system
    output_config = {}
    if schema is not None:
        output_config["format"] = {"type": "json_schema",
                                   "schema": sanitize_schema(schema, settings)}
    # Keep thinking to the least each model allows (see common.CLAUDE_THINKING).
    mode = common.claude_thinking(settings["model"])
    if mode in ("disabled", "between_tools"):
        payload["thinking"] = {"type": mode}
    elif mode == "effort":
        output_config["effort"] = "low"
    if output_config:
        payload["output_config"] = output_config
    url = _root_url(settings["base_url"]) + "/v1/messages"
    return url, _headers(settings, key), payload


def _describe_claude(settings, key, secrets, system, user_text, encoded, mime, schema,
                     request_extra, timeout):
    url, headers, payload = _claude_request(settings, key, system, user_text, encoded, mime,
                                            schema)
    payload = merge_body(merge_body(payload, settings["extra"]), request_extra)
    with _send(url, headers, payload, timeout, secrets) as response:
        body = _read_reply_body(response, timeout, secrets)
    data = _parse_reply_json(body, secrets)
    blocks = data.get("content")
    if not isinstance(blocks, list):
        if isinstance(data.get("error"), dict):
            raise _stream_error(data["error"], secrets)
        log_error(body, "the reply has no content", secrets)
        raise ProviderError("endpoint", "the reply has no content", secrets=secrets)
    # Thinking blocks may come first: the answer is the first text block.
    text = next((block["text"] for block in blocks
                 if isinstance(block, dict) and block.get("type") == "text"
                 and isinstance(block.get("text"), str)), "")
    reason = data.get("stop_reason") or ""
    detail = ""
    if reason == "refusal" and isinstance(data.get("stop_details"), dict):
        detail = str(data["stop_details"].get("explanation") or "")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens_in = None
    for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            tokens_in = (tokens_in or 0) + value
    return {"text": text, "model": str(data.get("model") or ""),
            "finish": _CLAUDE_FINISH.get(reason, "ok"), "reason": reason, "detail": detail,
            "usage": _usage(tokens_in, usage.get("output_tokens"))}


def _ollama_request(settings, key, system, user_text, encoded, schema):
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": user_text or "", "images": [encoded]})
    payload = {"model": settings["model"], "messages": messages, "stream": False,
               "think": False, "format": sanitize_schema(schema, settings) or "json",
               "options": {"num_predict": settings["max_output_tokens"]}}
    url = _root_url(settings["base_url"], ollama=True) + "/api/chat"
    return url, _headers(settings, key), payload


def _describe_ollama(settings, key, secrets, system, user_text, encoded, mime, schema,
                     request_extra, timeout):
    url, headers, payload = _ollama_request(settings, key, system, user_text, encoded, schema)
    payload = merge_body(merge_body(payload, settings["extra"]), request_extra)
    with _send(url, headers, payload, timeout, secrets) as response:
        body = _read_reply_body(response, timeout, secrets)
    data = _parse_reply_json(body, secrets)
    if isinstance(data.get("error"), str):
        raise ProviderError("endpoint", "the server reported an error: %s"
                            % _clip(data["error"], secrets), secrets=secrets)
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    text = message.get("content") if isinstance(message.get("content"), str) else ""
    reason = data.get("done_reason") or ""
    return {"text": text, "model": str(data.get("model") or ""),
            "finish": "length" if reason == "length" else "ok", "reason": reason,
            "detail": "",
            "usage": _usage(data.get("prompt_eval_count"), data.get("eval_count"))}


_DESCRIBERS = {"openai": _describe_openai, "claude": _describe_claude,
               "ollama": _describe_ollama}


def describe(endpoint, system, user_text, jpeg, schema, timeout=None, mime=None,
             request_extra=None):
    """Ask an endpoint to describe one image: a single request, no retries.

    endpoint is an endpoint dict (see common.endpoint_settings); system the
    system prompt ('' for none); user_text the text sent after the image;
    jpeg the image bytes, normally from images.prepare_for_llm (mime is
    sniffed from them unless given); schema the JSON schema the reply must
    follow (cut down to what the provider accepts by sanitize_schema), or
    None for any JSON object. timeout overrides the endpoint's. request_extra
    is merged into the request body after the endpoint's own "extra"
    settings (see merge_body).

    Returns {"text", "model" (as the server reports it; '' if it does
    not), "finish" ("ok", "length", "refusal" or "filtered"), "reason" (the
    provider's own stop reason), "detail" (a refusal's explanation, if any),
    "usage": {"input_tokens", "output_tokens"} (None when not reported),
    "seconds"}. Raises ProviderError.
    """
    settings = common.endpoint_settings(endpoint)
    key, _source = common.resolve_api_key(settings)
    secrets = [key] if key else []
    if not settings["base_url"]:
        raise ProviderError("endpoint", "no base URL is set (the %s preset needs one)"
                            % settings["preset"])
    data = bytes(jpeg)
    mime = mime or image_mime(data)
    encoded = base64.b64encode(data).decode("ascii")
    timeout = timeout or settings["timeout"]
    started = time.monotonic()
    reply = _DESCRIBERS[settings["kind"]](settings, key, secrets, system, user_text, encoded,
                                          mime, schema, request_extra, timeout)
    reply["seconds"] = round(time.monotonic() - started, 3)
    return reply


def _pause(seconds, sleep, should_stop):
    """Sleep, checking should_stop every half second. False if it asked to stop."""
    if should_stop is None:
        sleep(seconds)
        return True
    remaining = seconds
    while remaining > 0:
        if should_stop():
            return False
        step = min(remaining, 0.5)
        sleep(step)
        remaining -= step
    return not should_stop()


def describe_with_retries(endpoint, system, user_text, jpeg, schema, should_stop=None,
                          max_retries=6, sleep=time.sleep, timeout=None, mime=None,
                          request_extra=None, on_retry=None, max_timeout_retries=None):
    """describe(), asked again after retryable failures.

    Before retry n (0, 1, ...) it waits max(retry_after, uniform(0,
    min(60, 2**n))) seconds: at least what the server asked for, otherwise
    a random, growing backoff. Other kinds of failure, the last retryable
    one after max_retries retries, and one whose server asks for a pause
    longer than MAX_RETRY_AFTER are raised. Requests that ran out of time
    are only repeated max_timeout_retries times (default: max_retries),
    since each one takes the whole timeout. Stops early (raising the last
    error) when should_stop() returns True. on_retry(error, delay), when
    given, is called before each pause.

    Returns describe()'s dict plus "retries" (how many it took).
    """
    if max_timeout_retries is None:
        max_timeout_retries = max_retries
    retries = 0
    timeouts = 0
    while True:
        try:
            reply = describe(endpoint, system, user_text, jpeg, schema, timeout=timeout,
                             mime=mime, request_extra=request_extra)
            reply["retries"] = retries
            return reply
        except ProviderError as error:
            if error.kind != "retryable" or retries >= max_retries:
                raise
            if error.timeout:
                if timeouts >= max_timeout_retries:
                    raise
                timeouts += 1
            if error.retry_after is not None and error.retry_after > MAX_RETRY_AFTER:
                raise
            if should_stop is not None and should_stop():
                raise
            delay = max(error.retry_after or 0.0,
                        random.uniform(0, min(MAX_BACKOFF, 2 ** retries)))
            if on_retry is not None:
                on_retry(error, delay)
            if not _pause(delay, sleep, should_stop):
                raise
            retries += 1


# ---------------------------------------------------------------------------
# Embeddings (search by meaning, see embeddings.py)

def _embedding_list(items, count, secrets):
    """The vectors of an embeddings reply, checked: count lists of finite numbers, one length."""
    if not isinstance(items, list) or len(items) != count:
        raise ProviderError("content", "the reply has %s vectors for %d texts"
                            % (len(items) if isinstance(items, list) else "no", count),
                            secrets=secrets)
    length = None
    for vector in items:
        if (not isinstance(vector, list) or not vector
                or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and math.isfinite(value) for value in vector)):
            raise ProviderError("content", "the reply holds a vector that is not a list of "
                                "numbers", secrets=secrets)
        if length is not None and len(vector) != length:
            raise ProviderError("content", "the reply's vectors differ in length",
                                secrets=secrets)
        length = len(vector)
    return items


def embed(endpoint, texts, model=None, timeout=None, extra=None):
    """Turn texts into embedding vectors: a single request, no retries.

    endpoint is an endpoint dict (see common.endpoint_settings); texts a
    list of strings; model the model name to ask for (None: the endpoint's
    own; '' sends none, which llama.cpp accepts). OpenAI-compatible
    endpoints get POST {base}/embeddings (llama.cpp with --embeddings,
    Ollama's /v1, LM Studio, vLLM, OpenAI), Ollama's own kind POST
    /api/embed. Anthropic's API has no embeddings. extra is merged into the
    request body (merge_body), such as {"embd_normalize": -1}: llama.cpp's
    raw pooled vectors. An item of texts may also be a dict: a multimodal
    input as llama.cpp's server takes it ({"content": [parts]}, see
    looks.image_input), which only OpenAI-compatible endpoints are sent.

    Returns {"vectors": [[float, ...] per text, in order], "model" (as the
    server names it; '' if it does not), "tokens" (input tokens, or None),
    "seconds"}. Raises ProviderError (kind "content" for a reply that is
    not a list of vectors).
    """
    settings = common.endpoint_settings(endpoint)
    key, _source = common.resolve_api_key(settings)
    secrets = [key] if key else []
    if not settings["base_url"]:
        raise ProviderError("endpoint", "no base URL is set (the %s preset needs one)"
                            % settings["preset"])
    if settings["kind"] == "claude":
        raise ProviderError("endpoint", "Anthropic's API cannot make embeddings: choose an "
                            "OpenAI-compatible or Ollama endpoint")
    texts = [text if isinstance(text, (str, dict)) else "" for text in texts]
    if settings["kind"] == "ollama" and any(isinstance(text, dict) for text in texts):
        raise ProviderError("endpoint", "Ollama's own API cannot embed pictures: use a llama.cpp "
                            "server (its OpenAI-compatible API)")
    model = settings["model"] if model is None else model
    timeout = timeout or settings["timeout"]
    headers = _headers(settings, key)
    started = time.monotonic()
    if settings["kind"] == "ollama":
        url = _root_url(settings["base_url"], ollama=True) + "/api/embed"
        payload = {"model": model, "input": texts}
    else:
        url = settings["base_url"] + "/embeddings"
        payload = {"input": texts, "encoding_format": "float"}
        if model:
            payload["model"] = model
    if extra:
        payload = merge_body(payload, extra)
    with _send(url, headers, payload, timeout, secrets) as response:
        body = _read_reply_body(response, timeout, secrets)
    data = _parse_reply_json(body, secrets)
    if settings["kind"] == "ollama":
        vectors = data.get("embeddings")
        tokens = data.get("prompt_eval_count")
    else:
        items = data.get("data")
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            # (The API may answer out of order: each item says which text it is.)
            if all(isinstance(item.get("index"), int) for item in items):
                items = sorted(items, key=lambda item: item["index"])
            vectors = [item.get("embedding") for item in items]
        else:
            vectors = None
        usage = data.get("usage")
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    vectors = _embedding_list(vectors, len(texts), secrets)
    served = data.get("model")
    return {"vectors": vectors,
            "model": served.replace("\\", "/").split("/")[-1] if isinstance(served, str) else "",
            "tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool)
            else None,
            "seconds": round(time.monotonic() - started, 3)}


def embed_with_retries(endpoint, texts, model=None, should_stop=None, max_retries=6,
                       sleep=time.sleep, timeout=None, on_retry=None, extra=None):
    """embed(), asked again after retryable failures (a server still loading its
    model answers 503), with the pauses of describe_with_retries. Other
    failures, and the last retryable one, are raised."""
    retries = 0
    while True:
        try:
            reply = embed(endpoint, texts, model=model, timeout=timeout, extra=extra)
            reply["retries"] = retries
            return reply
        except ProviderError as error:
            if error.kind != "retryable" or retries >= max_retries:
                raise
            if error.retry_after is not None and error.retry_after > MAX_RETRY_AFTER:
                raise
            if should_stop is not None and should_stop():
                raise
            delay = max(error.retry_after or 0.0,
                        random.uniform(0, min(MAX_BACKOFF, 2 ** retries)))
            if on_retry is not None:
                on_retry(error, delay)
            if not _pause(delay, sleep, should_stop):
                raise
            retries += 1


# ---------------------------------------------------------------------------
# What a server says about itself

def _model_ids(data):
    """Model ids from a model list reply (OpenAI, Anthropic or Ollama form)."""
    if not isinstance(data, dict):
        return []
    items = data.get("data")
    if not isinstance(items, list):
        items = data.get("models")
    ids = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
            if isinstance(value, str) and value:
                ids.append(value)
    return ids


def _read_props(data, result):
    for key in ("model_path", "model_alias", "model"):
        value = data.get(key)
        if isinstance(value, str) and value:
            result["model"] = value.replace("\\", "/").split("/")[-1]
            break
    slots = data.get("total_slots")
    if isinstance(slots, int) and not isinstance(slots, bool) and slots > 0:
        result["slots"] = slots
    modalities = data.get("modalities")
    if isinstance(modalities, dict) and isinstance(modalities.get("vision"), bool):
        result["vision"] = modalities["vision"]
    result["server"] = "llama.cpp"


def _probe_llamacpp(settings, key, result):
    base = settings["base_url"]
    root = _root_url(base)
    urls = [root + "/props"] + ([base + "/props"] if base != root else [])
    headers = _headers(settings, key)
    for url in urls:
        try:
            status, body = _http(url, headers)
        except Exception:
            result["reachable"] = False
            return
        result["reachable"] = True
        if status == 503:
            result["loading"] = True
            result["server"] = "llama.cpp"
            return
        data = _json_or_none(body) if status == 200 else None
        if isinstance(data, dict):
            _read_props(data, result)
            return


def _probe_ollama(settings, key, result):
    root = _root_url(settings["base_url"], ollama=True)
    headers = _headers(settings, key)
    try:
        status, body = _http(root + "/api/version", headers)
    except Exception:
        result["reachable"] = False
        return
    result["reachable"] = True
    data = _json_or_none(body) if status == 200 else None
    version = data.get("version") if isinstance(data, dict) else None
    result["server"] = "ollama %s" % version if isinstance(version, str) else "ollama"
    if not settings["model"]:
        return
    try:
        status, body = _http(root + "/api/show", headers, {"model": settings["model"]})
    except Exception:
        return
    data = _json_or_none(body) if status == 200 else None
    if isinstance(data, dict):
        result["model"] = settings["model"]
        capabilities = data.get("capabilities")
        if isinstance(capabilities, list):
            result["vision"] = "vision" in capabilities


def _probe_models(settings, key, result):
    try:
        status, body = _http(settings["base_url"] + "/models", _headers(settings, key))
    except Exception:
        result["reachable"] = False
        return
    result["reachable"] = True
    result["server"] = settings["preset"]
    ids = _model_ids(_json_or_none(body)) if status == 200 else []
    if len(ids) == 1:
        # A server that offers one model is serving that one.
        result["model"] = ids[0]


def probe(endpoint):
    """What an endpoint's server says about itself (best effort; never raises).

    Returns {"model": the served model's name or None, "slots": parallel
    request slots (llama.cpp) or None, "vision": True/False, or None when
    unknown, "server": a short description ('' if unknown), "reachable":
    True/False, or None when not checked, "loading": True while llama.cpp
    is still loading its model}. llama.cpp-like servers are asked GET /props
    (with the API key when one is set), Ollama /api/version and /api/show,
    other local servers /models. Cloud services are not contacted.
    """
    result = {"model": None, "slots": None, "vision": None, "server": "",
              "reachable": None, "loading": False}
    try:
        settings = common.endpoint_settings(endpoint)
        key, _source = common.resolve_api_key(settings)
        if settings["preset"] in common.LLAMACPP_PRESETS:
            _probe_llamacpp(settings, key, result)
        elif settings["kind"] == "ollama":
            _probe_ollama(settings, key, result)
        elif settings["preset"] in common.CLOUD_PRESETS or not common.is_local_endpoint(settings):
            result["server"] = settings["preset"]
        else:
            _probe_models(settings, key, result)
    except Exception:
        pass        # a probe must never stop anything
    return result


def wait_until_ready(endpoint, timeout, should_stop=None, interval=1.0, sleep=time.sleep):
    """Wait (up to timeout seconds) until a local server can take requests.

    llama.cpp-like servers are polled at /health (503 means the model is
    still loading), Ollama at /api/version and other local servers at
    /models; any other HTTP answer counts as ready. Cloud services count as
    ready without being contacted. Returns {"ready", "loading" (the last
    answer said the model is loading), "waited" (seconds), "status" (the
    last HTTP status or None), "error" (why it is not ready, '' if it is)}.
    Never raises.
    """
    result = {"ready": False, "loading": False, "waited": 0.0, "status": None, "error": ""}
    started = time.monotonic()
    try:
        settings = common.endpoint_settings(endpoint)
        key, _source = common.resolve_api_key(settings)
    except Exception as error:
        result["error"] = common.redact(error)
        return result
    secrets = [key] if key else []
    if settings["preset"] in common.CLOUD_PRESETS:
        result["ready"] = True
        return result
    if settings["preset"] in common.LLAMACPP_PRESETS:
        url = _root_url(settings["base_url"]) + "/health"
    elif settings["kind"] == "ollama":
        url = _root_url(settings["base_url"], ollama=True) + "/api/version"
    else:
        url = settings["base_url"] + "/models"
    headers = _headers(settings, key)
    while True:
        elapsed = time.monotonic() - started
        try:
            status, _body = _http(url, headers,
                                  timeout=max(1.0, min(PROBE_TIMEOUT, timeout - elapsed)))
        except Exception as error:
            status = None
            result["loading"] = False
            result["error"] = common.redact("cannot connect: %s" % error, secrets)
        if status is not None:
            result["status"] = status
            if status == 503:
                result["loading"] = True
                result["error"] = "the server is still loading the model"
            else:
                result.update(ready=True, loading=False, error="")
                break
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            break
        if should_stop is not None and should_stop():
            result["error"] = "stopped"
            break
        sleep(max(0.0, min(interval, timeout - elapsed)))
    result["waited"] = round(time.monotonic() - started, 3)
    return result


def list_models(endpoint):
    """The model ids an endpoint offers (best effort: [] when it cannot tell).

    Asks GET {base}/models (OpenAI-compatible servers), /v1/models
    (Anthropic) or /api/tags (Ollama).
    """
    try:
        settings = common.endpoint_settings(endpoint)
        key, _source = common.resolve_api_key(settings)
        headers = _headers(settings, key)
        root = _root_url(settings["base_url"], ollama=settings["kind"] == "ollama")
        if settings["kind"] == "ollama":
            status, body = _http(root + "/api/tags", headers)
            return _model_ids(_json_or_none(body)) if status == 200 else []
        if settings["kind"] != "claude":
            status, body = _http(settings["base_url"] + "/models", headers)
            return _model_ids(_json_or_none(body)) if status == 200 else []
        ids = []
        url = root + "/v1/models?limit=1000"
        for _page in range(20):
            status, body = _http(url, headers)
            data = _json_or_none(body) if status == 200 else None
            if not isinstance(data, dict):
                break
            ids.extend(_model_ids(data))
            last = data.get("last_id")
            if not data.get("has_more") or not isinstance(last, str) or not last:
                break
            url = root + "/v1/models?limit=1000&after_id=" + urllib.parse.quote(last)
        return ids
    except Exception:
        return []


def _test_prompt(system, user_text, schema):
    """The current prompt and schema (core profile, no library notes) where not given."""
    if system is None:
        system = prompts.SYSTEM_PROMPT
    if user_text is None:
        user_text = prompts.user_text(None)
    if schema is None:
        schema = prompts.schema("core")
    return system, user_text, schema


def test_endpoint(endpoint, jpeg, system=None, user_text=None, schema=None, parse=None):
    """Describe one image for real, to check an endpoint's settings.

    Uses the current prompt and schema (or those given) and, when given,
    parse(text) -> dict to check the reply (else any JSON object passes).
    Never raises. Returns {"ok", "error_kind" (a ProviderError kind, '' when
    ok), "error" (redacted), "latency_ms", "served_model", "parsed" (dict or
    None), "usage", "finish", "est_cost_per_image" (None without prices),
    "key_source" (where the key came from; never the key)}.
    """
    result = {"ok": False, "error_kind": "", "error": "", "latency_ms": None,
              "served_model": "", "parsed": None,
              "usage": {"input_tokens": None, "output_tokens": None}, "finish": "",
              "est_cost_per_image": None, "key_source": "none"}
    secrets = []
    started = time.monotonic()
    try:
        settings = common.endpoint_settings(endpoint)
        key, result["key_source"] = common.resolve_api_key(settings)
        secrets = [key] if key else []
        system, user_text, schema = _test_prompt(system, user_text, schema)
        reply = describe(settings, system, user_text, jpeg, schema)
    except ProviderError as error:
        result.update(error_kind=error.kind, error=str(error),
                      latency_ms=int((time.monotonic() - started) * 1000))
        return result
    except Exception as error:
        result.update(error_kind="endpoint",
                      error=common.redact("%s: %s" % (type(error).__name__, error), secrets),
                      latency_ms=int((time.monotonic() - started) * 1000))
        return result
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    served = reply["model"]
    if settings["preset"] in common.LLAMACPP_PRESETS:
        # llama.cpp names whatever model the request asked for; /props
        # knows which one it really loaded.
        served = probe(settings)["model"] or served
    result.update(served_model=served or settings["model"], usage=reply["usage"],
                  finish=reply["finish"],
                  est_cost_per_image=estimate_cost(settings, reply["usage"]))
    if reply["finish"] != "ok":
        explanation = {"length": "the reply was cut off at max_output_tokens",
                       "refusal": "the model refused to describe the image",
                       "filtered": "the service's content filter blocked the reply"}
        text = explanation.get(reply["finish"], reply["finish"])
        if reply.get("detail"):
            text += ": " + reply["detail"]
        result.update(error_kind="content", error=common.redact(text, secrets))
        return result
    try:
        parsed = parse(reply["text"]) if parse is not None else extract_json(reply["text"])
    except Exception as error:
        result.update(error_kind="content", error=common.redact(
            "the reply could not be used: %s" % error, secrets))
        return result
    if not isinstance(parsed, dict):
        result.update(error_kind="content", error="the reply is not a JSON object")
        return result
    result.update(ok=True, parsed=parsed)
    return result

