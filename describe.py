#!/usr/bin/env python3
"""Ask an LLM to describe indexed images and store the replies.

For every image that has no description yet (or one made with an older
prompt), the cached preview -- or the original when there is no preview --
is shrunk with images.prepare_for_llm() and sent as a JPEG with the prompt
below. The JSON reply is validated and stored with the image, so the images
can be searched by what they show.

Failures are recorded per image (describe_attempts, describe_error). An
image that failed MAX_FAILURES times is skipped until a run asks to retry
failed images. Problems with the endpoint itself (server not running, bad
key, every request refused before it has described a single image) are not
counted against images; they stop the run instead.

The command line entry point is scan.py, which indexes and then describes.
"""

import collections
import concurrent.futures
import http.client
import inspect
import json
import math
import os
import socket
import threading
import time
import urllib.error

import cache
import common
import imageinfo
import images
import llm
# The prompt and its schema live in prompts.py (no Pillow needed there).
from prompts import (  # noqa: F401
    DESCRIBE_PROMPT, DESCRIBE_SCHEMA, PROMPT_VER, SCORE_FIELDS,
)

# Requests per image in one run before it counts as a failure (a reply that
# does not parse is asked for again).
REPLY_ATTEMPTS = 4
# Images that failed this many runs are skipped unless retry_failed is set.
MAX_FAILURES = 3
# An endpoint that fails this many requests in a row (connection refused,
# rate limited, ...) is not used for the rest of the run.
MAX_ENDPOINT_FAILURES = 3

# HTTP statuses that mean the endpoint cannot work at all (bad key, no
# credit, no access, wrong URL or model), and those that mean this one
# request (this image) was rejected.
FATAL_HTTP = (401, 402, 403, 404)
IMAGE_HTTP = (400, 413, 415, 422)

TMP_PREFIX = "describe-"


class EndpointError(Exception):
    """The endpoint failed in a way that is not the image's fault."""


class FatalEndpointError(EndpointError):
    """The endpoint rejects every request (bad key, wrong model, ...)."""


class DescribeError(Exception):
    """No valid description could be made for this image.

    http_status is the HTTP status of the last rejected request, when the
    endpoint refused it (400, 413, ...), else None.
    """

    def __init__(self, message, http_status=None):
        Exception.__init__(self, message)
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Replies

def as_string_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def as_unit_float(value, name):
    """Validate a 0..1 subjective score. Clamps slightly out-of-range values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("response field " + name + " is not a number")
    if not math.isfinite(value):
        raise ValueError("response field " + name + " is not a finite number")
    return max(0.0, min(1.0, float(value)))


def parse_description(reply):
    """Turn an LLM reply into a validated description dict."""
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        raise ValueError("response was not a JSON object")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("response is missing a text description")
    text = data.get("text")
    result = {
        "description": description.strip(),
        "subjects": as_string_list(data.get("subjects")),
        "text": text.strip() if isinstance(text, str) else "",
        "tags": as_string_list(data.get("tags")),
        "colors": as_string_list(data.get("colors")),
    }
    for field in SCORE_FIELDS:
        result[field] = as_unit_float(data.get(field), field)
    return result


# ---------------------------------------------------------------------------
# What to describe, and in which order

def endpoint_list(config):
    """Return the configured endpoints as a list.

    A single endpoint dict becomes a one item list; a list (round robin) is
    kept as is. Only endpoints that name a model are returned.
    """
    endpoint = config.get("endpoint", {})
    items = endpoint if isinstance(endpoint, list) else [endpoint]
    return [item for item in items
            if isinstance(item, dict) and item.get("model")]


def selected_libraries(config, library_ids=None):
    """Enabled libraries, limited to library_ids (ids) when given."""
    libraries = common.enabled_libraries(config)
    if library_ids is not None:
        wanted = set(library_ids)
        libraries = [library for library in libraries if library.get("id") in wanted]
    return [library for library in libraries if library.get("id")]


def capture_sort_key(row):
    """Sort key for describe order: the capture date as text, then the id.

    Comparing 'YYYY-MM-DDTHH:MM:SS' strings never converts to epoch seconds,
    so dates before 1970 work everywhere.
    """
    return (row["taken_at"] or imageinfo.local_iso(row["mtime"]), row["id"])


_PENDING_SQL = """
    SELECT id, library_id, path, name, content_hash, taken_at, mtime, described,
           prompt_ver, describe_attempts, has_preview
    FROM images
    WHERE library_id = ? AND missing_since IS NULL AND (described = 0 OR prompt_ver < ?)
    ORDER BY id"""


def pending_rows(conn, libraries, newest_first=None, retry_failed=False):
    """The images to describe, in order. Returns (rows, skipped_failed).

    Images that have never been described come first, then those described
    with an older prompt. Within each group, libraries keep their config
    order, and each library is sorted by capture date: newest first when
    newest_first is True, oldest first when False, and by the library's own
    "newest_first" setting when None. Missing files are left out, and so are
    images that failed MAX_FAILURES times unless retry_failed is set; those
    are counted in skipped_failed.
    """
    fresh, stale = [], []
    skipped_failed = 0
    for library in libraries:
        rows = conn.execute(_PENDING_SQL, (library["id"], PROMPT_VER)).fetchall()
        if not retry_failed:
            kept = [row for row in rows if row["describe_attempts"] < MAX_FAILURES]
            skipped_failed += len(rows) - len(kept)
            rows = kept
        order = library.get("newest_first", True) if newest_first is None else newest_first
        rows.sort(key=capture_sort_key, reverse=bool(order))
        fresh.extend(row for row in rows if not row["described"])
        stale.extend(row for row in rows if row["described"])
    return fresh + stale, skipped_failed


# ---------------------------------------------------------------------------
# One image

def _error_text(error):
    text = str(error) or type(error).__name__
    return text[:500]


def _classify(error):
    """How to treat a failed request: 'fatal', 'endpoint', 'image' or 'retry'."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code in FATAL_HTTP:
            return "fatal"
        if error.code == 429:
            return "endpoint"
        if error.code in IMAGE_HTTP:
            return "image"
        return "retry"
    timeouts = (socket.timeout, TimeoutError)
    if isinstance(error, timeouts):
        # A request that ran out of time; asking again would take as long.
        return "image"
    if isinstance(error, urllib.error.URLError):
        return "image" if isinstance(error.reason, timeouts) else "endpoint"
    if isinstance(error, (ConnectionError, http.client.HTTPException)):
        return "endpoint"
    return "retry"


def _tmp_path(image_id):
    name = "%s%d-%d-%d.jpg" % (TMP_PREFIX, image_id, os.getpid(), threading.get_ident())
    return os.path.join(common.ensure_dir(common.tmp_dir()), name)


def clean_tmp():
    """Delete temporary describe files left behind by an earlier run.

    Only call this while holding the worker lock, when no other describe
    run can be using them.
    """
    folder = common.tmp_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(TMP_PREFIX):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def _source_path_style(call_api):
    """How a call_api hook takes the original's path: "keyword", "positional" or None.

    Hooks written as call_api(prompt, image_path) only get the temporary
    JPEG. A hook with a source_path parameter (or **kwargs) gets it as
    source_path=...; one with a third positional parameter gets it there.
    """
    try:
        parameters = list(inspect.signature(call_api).parameters.values())
    except (TypeError, ValueError):
        return None
    kinds = inspect.Parameter
    if any(parameter.kind == kinds.VAR_KEYWORD or (
            parameter.name == "source_path"
            and parameter.kind in (kinds.POSITIONAL_OR_KEYWORD, kinds.KEYWORD_ONLY))
           for parameter in parameters):
        return "keyword"
    positional = [parameter for parameter in parameters
                  if parameter.kind in (kinds.POSITIONAL_ONLY, kinds.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3 or any(parameter.kind == kinds.VAR_POSITIONAL
                                   for parameter in parameters):
        return "positional"
    return None


def _helper_request(helper, helper_lock, prompt, jpeg, image_id, source_path=""):
    """Run the helper's call_api hook. Returns (prompt, image bytes, mime).

    The hook takes (prompt, image_path), so the prepared JPEG is written to a
    temporary file under data/tmp for it. Hooks that accept a third argument
    (see _source_path_style) also get the original file's path. The hook may
    return None (send as is), or a new (prompt, image_path) pair; a
    different path is read through the helper's load_image like any original.
    """
    tmp = _tmp_path(image_id)
    try:
        with open(tmp, "wb") as handle:
            handle.write(jpeg)
        style = _source_path_style(helper.call_api)
        with helper_lock:
            if style == "keyword":
                changed = helper.call_api(prompt, tmp, source_path=source_path)
            elif style == "positional":
                changed = helper.call_api(prompt, tmp, source_path)
            else:
                changed = helper.call_api(prompt, tmp)
        if changed is None:
            return prompt, jpeg, "image/jpeg"
        prompt, path = changed
        if os.path.abspath(path) == os.path.abspath(tmp):
            # The hook may have edited the file in place.
            with open(tmp, "rb") as handle:
                data = handle.read()
        else:
            data = common.load_image_bytes(helper, path)
        return prompt, data, llm.image_mime(data, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def request_description(endpoint, jpeg, helper=None, helper_lock=None, image_id=0,
                        source_path=""):
    """Ask the LLM to describe JPEG bytes, retrying until the reply parses.

    source_path (the original file) is only passed on to a helper's call_api
    hook. Returns the description dict. Raises FatalEndpointError or
    EndpointError when the endpoint is at fault, and DescribeError when this
    image could not be described within REPLY_ATTEMPTS requests.
    """
    helper_lock = helper_lock or threading.Lock()
    last_error = None
    attempts = 0
    for _attempt in range(REPLY_ATTEMPTS):
        attempts += 1
        response = ""
        prompt, data, mime = DESCRIBE_PROMPT, jpeg, "image/jpeg"
        if helper is not None and hasattr(helper, "call_api"):
            prompt, data, mime = _helper_request(helper, helper_lock, prompt, jpeg, image_id,
                                                 source_path)
        try:
            response = llm.describe_image(endpoint, prompt, data, DESCRIBE_SCHEMA,
                                          helper, mime=mime)
            return parse_description(response)
        except Exception as error:
            llm.log_error(response, error)
            kind = _classify(error)
            if kind == "fatal":
                raise FatalEndpointError(_error_text(error)) from error
            if kind == "endpoint":
                raise EndpointError(_error_text(error)) from error
            last_error = error
            if kind == "image":
                break
    status = last_error.code if isinstance(last_error, urllib.error.HTTPError) else None
    raise DescribeError("no valid description after %d attempt(s): %s"
                        % (attempts, _error_text(last_error)), http_status=status)


def image_for_llm(row, root, previews, helper=None):
    """The JPEG bytes to send for an image row.

    Uses the cached preview when there is one, else the original (read
    through the helper's load_image when it has one). Either way the image is
    upright, flattened and shrunk by images.prepare_for_llm(). Raises
    images.SourceReadError when the original cannot be read and
    images.ImageReadError when it cannot be decoded.
    """
    if row["has_preview"] == 1:
        data = previews.read(row["library_id"], row["id"])
        if data is not None:
            try:
                return images.prepare_for_llm(data)
            except images.ImageReadError:
                pass        # a damaged preview: fall back to the original
    full = os.path.join(root, *row["path"].split("/"))
    return images.prepare_for_llm(images.read_source_bytes(full, helper))


def _describe_one(endpoint, row, root, previews, helper, helper_lock):
    """Worker: prepare and describe one image. Returns a result dict; never raises."""
    started = time.monotonic()
    source_path = os.path.join(root, *row["path"].split("/"))
    try:
        jpeg = image_for_llm(row, root, previews, helper)
    except images.SourceReadError as error:
        return {"status": "skipped", "error": _error_text(error)}
    except Exception as error:
        text = _error_text(error)
        if images.HEIF_MISSING in text or images.AVIF_MISSING in text:
            # Not the image's fault: it works once the decoder is installed.
            return {"status": "skipped", "error": text}
        return {"status": "failed", "error": text}
    try:
        info = request_description(endpoint, jpeg, helper, helper_lock, row["id"],
                                   source_path)
    except FatalEndpointError as error:
        return {"status": "fatal", "error": _error_text(error)}
    except EndpointError as error:
        return {"status": "endpoint", "error": _error_text(error)}
    except DescribeError as error:
        return {"status": "failed", "error": _error_text(error),
                "http_status": error.http_status}
    except Exception as error:
        return {"status": "failed", "error": _error_text(error)}
    return {"status": "ok", "info": info, "seconds": time.monotonic() - started}


# ---------------------------------------------------------------------------
# Storing results

_STORE_SQL = """
    UPDATE images SET info = ?, described = 1, prompt_ver = ?, provenance = ?,
        describe_attempts = 0, describe_error = '', updated = ?
    WHERE id = ? AND content_hash = ?"""

# Exact copies (same content hash) that still need a description get the
# same one, so each picture is only sent once.
_SHARE_SQL = """
    UPDATE images SET info = ?, described = 1, prompt_ver = ?, provenance = ?,
        describe_attempts = 0, describe_error = '', updated = ?
    WHERE content_hash = ? AND id <> ? AND missing_since IS NULL
        AND (described = 0 OR prompt_ver < ?)"""


def store_description(conn, row, info, provenance):
    """Save a description for a row and its exact copies. Returns (stored, shared).

    stored is False when the row changed or vanished while it was being
    described (its content hash no longer matches); nothing is saved then.
    """
    now = time.time()
    info_text = json.dumps(info, ensure_ascii=False, allow_nan=False)
    provenance_text = json.dumps(provenance, ensure_ascii=False, allow_nan=False)
    stored = conn.execute(_STORE_SQL, (info_text, PROMPT_VER, provenance_text, now,
                                       row["id"], row["content_hash"])).rowcount == 1
    shared = 0
    if stored and row["content_hash"]:
        shared = conn.execute(_SHARE_SQL, (info_text, PROMPT_VER, provenance_text, now,
                                           row["content_hash"], row["id"],
                                           PROMPT_VER)).rowcount
    conn.commit()
    return stored, max(0, shared)


def record_failure(conn, row, error):
    """Count a failed describe attempt for a row and keep its last error."""
    conn.execute("UPDATE images SET describe_attempts = describe_attempts + 1, "
                 "describe_error = ? WHERE id = ?", (error[:500], row["id"]))
    conn.commit()


def model_name(endpoint, ctx=None):
    """The model name to record for an endpoint (best effort, never raises).

    llama.cpp servers are asked which weights they have loaded; any failure
    falls back to the configured model name.
    """
    try:
        return llm.probe_model_name(endpoint)
    except Exception as error:
        if ctx is not None:
            ctx.log("Could not ask %s for its model name (%s); using %r."
                    % (endpoint.get("kind", "openai"), _error_text(error),
                       endpoint.get("model", "")), "warn")
        return endpoint.get("model", "")


# ---------------------------------------------------------------------------
# The describe run

def describe_pending(conn, config, ctx, helper=None, library_ids=None, newest_first=None,
                     retry_failed=False, previews=None):
    """Describe the images that need it. Returns a summary dict.

    conn is an index.db connection; ctx a jobs.JobContext. Only enabled
    libraries are described (limited to library_ids when given). Each
    configured endpoint handles one request at a time; with several
    endpoints (round robin) requests run in parallel. Stops early, keeping
    every saved description, when ctx asks to stop or no endpoint is usable.
    The helper's before_launch and after_launch hooks bracket the run;
    after_launch runs even when the run fails.

    Summary keys: described, shared (copies that got the same description),
    failed, skipped, skipped_failed, pending, stopped, error.
    """
    summary = {"described": 0, "shared": 0, "failed": 0, "skipped": 0,
               "skipped_failed": 0, "pending": 0, "stopped": False, "error": ""}
    libraries = selected_libraries(config, library_ids)
    rows, summary["skipped_failed"] = pending_rows(conn, libraries, newest_first,
                                                   retry_failed)
    summary["pending"] = len(rows)
    if summary["skipped_failed"]:
        ctx.log("Skipping %d image(s) that failed %d times; retry them with "
                "--retry-failed." % (summary["skipped_failed"], MAX_FAILURES))
    if not rows:
        ctx.log("All images already have descriptions.")
        return summary
    endpoints = endpoint_list(config)
    if not endpoints:
        ctx.log("No LLM model configured; skipping descriptions. Run settings.py.")
        return summary
    for endpoint in endpoints:
        ctx.add_secret(endpoint.get("api_key"))
    previews = previews or cache.PreviewStore()
    roots = {library["id"]: common.library_root(library) for library in libraries}
    clean_tmp()
    ctx.stage("describe", total=len(rows))
    ctx.log("Describing %d image(s)..." % len(rows))
    try:
        if helper is not None and hasattr(helper, "before_launch"):
            helper.before_launch()
        names = []
        for endpoint in endpoints:
            names.append(model_name(endpoint, ctx))
            ctx.log("Using model: " + names[-1])
        _run_queue(conn, ctx, endpoints, names, rows, roots, previews, helper, summary)
    finally:
        if helper is not None and hasattr(helper, "after_launch"):
            helper.after_launch()
    parts = ["Described %d image(s)" % summary["described"]]
    if summary["shared"]:
        parts.append("%d exact copies shared a description" % summary["shared"])
    if summary["failed"]:
        parts.append("%d failed" % summary["failed"])
    if summary["skipped"]:
        parts.append("%d skipped" % summary["skipped"])
    ctx.log(", ".join(parts) + ".")
    if summary["stopped"]:
        ctx.log("Stopped. Descriptions so far are saved; run again to continue.")
    return summary


def _run_queue(conn, ctx, endpoints, names, rows, roots, previews, helper, summary):
    """Feed rows to the endpoints and store results on this (the calling) thread."""
    helper_lock = threading.Lock()
    queue = collections.deque(rows)
    free = list(range(len(endpoints)))       # endpoints not busy right now
    failures = [0] * len(endpoints)          # consecutive endpoint failures
    succeeded = [False] * len(endpoints)     # described at least one image this run
    retired = set()
    done_hashes = set()
    in_flight = {}

    def submit_next(pool):
        while free and queue:
            row = queue.popleft()
            if row["content_hash"] and row["content_hash"] in done_hashes:
                # An exact copy was just described; it already has the description.
                ctx.step(item=row["path"])
                continue
            index = free.pop(0)
            root = roots.get(row["library_id"], "")
            future = pool.submit(_describe_one, endpoints[index], row, root, previews,
                                 helper, helper_lock)
            in_flight[future] = (row, index)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints))
    try:
        if ctx.should_stop():
            summary["stopped"] = True
        else:
            submit_next(pool)
        while in_flight:
            done, _ = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                row, index = in_flight.pop(future)
                result = future.result()
                status = result["status"]
                if status in ("fatal", "endpoint"):
                    failures[index] += 1
                    if status == "fatal" or failures[index] >= MAX_ENDPOINT_FAILURES:
                        retired.add(index)
                        ctx.log("Stopped using %s: %s" % (names[index], result["error"]),
                                "error")
                    else:
                        ctx.log("The LLM endpoint failed on %s: %s"
                                % (row["path"], result["error"]), "warn")
                    if len(retired) < len(endpoints):
                        queue.appendleft(row)        # try it again on a working endpoint
                    else:
                        summary["skipped"] += 1
                        ctx.step(item=row["path"])
                elif (status == "failed" and result.get("http_status")
                      and not succeeded[index]):
                    # This endpoint has refused every request of the run so
                    # far: most likely its settings (a parameter the service
                    # does not accept), not this image. The image is not
                    # charged a failed attempt, and after a few such refusals
                    # the endpoint is not used for the rest of the run.
                    failures[index] += 1
                    summary["skipped"] += 1
                    if failures[index] >= MAX_ENDPOINT_FAILURES:
                        retired.add(index)
                        ctx.log("Stopped using %s: it refused every request so far (%s). "
                                "Check the endpoint settings." % (names[index], result["error"]),
                                "error")
                    else:
                        ctx.log("The LLM endpoint refused %s: %s" % (row["path"], result["error"]),
                                "warn")
                    ctx.step(item=row["path"])
                else:
                    if status == "ok":
                        succeeded[index] = True
                    failures[index] = 0
                    _handle_result(conn, ctx, row, result, endpoints[index], names[index],
                                   summary, done_hashes)
                    ctx.step(item=row["path"])
                if index not in retired:
                    free.append(index)
            if len(retired) == len(endpoints):
                summary["error"] = "No usable LLM endpoint is left."
                ctx.log(summary["error"] + " Check that the model server is running "
                        "and the endpoint settings are right.", "error")
                queue.clear()
            if ctx.should_stop():
                summary["stopped"] = True
                queue.clear()
            else:
                ctx.wait_if_paused()
                submit_next(pool)
    finally:
        pool.shutdown(wait=True)


def _handle_result(conn, ctx, row, result, endpoint, name, summary, done_hashes):
    status = result["status"]
    if status == "ok":
        info = dict(result["info"])
        info["_model_name"] = name
        provenance = {"model": name,
                      "endpoint": endpoint.get("name") or endpoint.get("kind", "openai"),
                      "seconds": round(result["seconds"], 2), "prompt_ver": PROMPT_VER}
        stored, shared = store_description(conn, row, info, provenance)
        if stored:
            summary["described"] += 1
            summary["shared"] += shared
            if row["content_hash"]:
                done_hashes.add(row["content_hash"])
            ctx.log("Described %s (%.1f s)" % (row["path"], result["seconds"]))
        else:
            summary["skipped"] += 1
            ctx.log("Not saved: %s changed while it was being described." % row["path"],
                    "warn")
    elif status == "failed":
        error = common.redact(result["error"], [endpoint.get("api_key", "")])
        record_failure(conn, row, error)
        summary["failed"] += 1
        ctx.log("Failed to describe %s: %s" % (row["path"], error), "warn")
    else:
        summary["skipped"] += 1
        ctx.log("Skipped %s: %s" % (row["path"], result["error"]), "warn")
