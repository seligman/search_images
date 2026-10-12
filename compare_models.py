#!/usr/bin/env python3
"""Compare how LLM endpoints describe the same images, side by side.

    python compare_models.py --endpoints NAME,NAME,... [--library NAME ...]
                             [--sample N | --ids 1,2,3] [--seed S] [--profile core]
                             [--endpoint-limit NAME=N ...] [--out PATH]
    python compare_models.py --merge FIRST.json SECOND.json ... [--out PATH]

Picks a sample of indexed images (seeded, so a run can be repeated), sends
each one to every listed endpoint -- one endpoint at a time, each with its
configured concurrency -- and writes a self-contained HTML report with the
answers side by side, plus a JSON file with the raw results (per endpoint:
images, failures, seconds per image, images per minute, tokens, HTTP
requests and estimated cost; per image: every endpoint's answer).

The database is only read. Nothing is stored, so a comparison never changes
a library's descriptions.

The sample is ordered so that every first part of it is balanced too: it
takes turns between the libraries, and between likely screenshots or
documents (PNG files, or no camera in the EXIF data) and photos. The former
are oversampled, as they are the hardest cases. --endpoint-limit NAME=N
sends only the first N images to that endpoint (to keep cloud costs down),
which are still a representative mix.

Local model servers often cannot run at the same time. Run the script once
per endpoint with the same --seed and --sample (so the same images are
used), then combine the JSON files into one report with --merge.

Images are prepared and prompted as the describe step does it (the
prepared preview, the system prompt, the library's context notes); the
helper module's hooks are not used. Unless libraries are named with
--library, an endpoint that is not on this computer or the local network
gets no images from libraries whose own "endpoint" setting keeps them on
local endpoints, and hidden pictures are never sent to such an endpoint
(they are listed as not run). An endpoint whose first
MAX_ENDPOINT_FAILURES images all fail (or that fails that many times in a
row) is not sent the rest.
Reports are written to data/reports/ by default. They contain thumbnails
and text from your pictures: keep them private.
"""

import argparse
import base64
import concurrent.futures
import html
import json
import os
import random
import statistics
import sys
import threading
import time

import cache
import common
import db
import describe
import jobs
import llm
import prompts

REPORT_FORMAT = "search-images-compare"
REPORT_VERSION = 1
DEFAULT_SAMPLE = 150
DEFAULT_SEED = 1
STRATA = ("document", "photo")
# Seconds to wait for a local server that is still loading its model.
STARTUP_WAIT = 180.0


# ---------------------------------------------------------------------------
# Choosing the images

def likely_document(row):
    """True for images that are probably screenshots or documents.

    PNG files, and files without a camera in their EXIF data (screenshots,
    downloads, scans and edited images rarely have one).
    """
    return row["name"].lower().endswith(".png") or not row["camera"]


def stratum(row):
    return "document" if likely_document(row) else "photo"


_ROW_SQL = ("SELECT id, library_id, path, name, camera, has_preview, taken_at, "
            "content_hash, width, height, hidden FROM images WHERE missing_since IS NULL ")


def candidate_rows(conn, library_ids):
    """Present images of these libraries, by id."""
    if not library_ids:
        return []
    rows = conn.execute(_ROW_SQL + "AND library_id IN (%s) ORDER BY id"
                        % ",".join("?" * len(library_ids)), list(library_ids)).fetchall()
    # Videos are described from a contact sheet; a model comparison is about pictures.
    return [row for row in rows if not common.is_video_file(row["name"])]


def stratified_order(rows, library_ids, seed):
    """All rows in an order whose every prefix is balanced.

    Rows are shuffled (with the seed) within each library and stratum
    (likely documents, photos). The order then takes one row per library in
    turn, alternating the strata from round to round and from library to
    library, and uses the other stratum when one runs out. So the first 2k
    rows of two libraries hold about k of each library and k of each
    stratum, which oversamples documents.
    """
    rng = random.Random(seed)
    library_ids = list(dict.fromkeys(library_ids))      # each library once
    pools = {library_id: {name: [] for name in STRATA} for library_id in library_ids}
    for row in rows:
        if row["library_id"] in pools:
            pools[row["library_id"]][stratum(row)].append(row)
    for library_id in library_ids:
        for name in STRATA:
            rng.shuffle(pools[library_id][name])
    order = []
    active = [library_id for library_id in library_ids
              if any(pools[library_id][name] for name in STRATA)]
    turn = 0
    while active:
        for position, library_id in enumerate(active):
            first = STRATA[(turn + position) % 2]
            second = STRATA[(turn + position + 1) % 2]
            pool = pools[library_id][first] or pools[library_id][second]
            if pool:
                order.append(pool.pop())
        active = [library_id for library_id in active
                  if any(pools[library_id][name] for name in STRATA)]
        turn += 1
    return order


def rows_by_id(conn, ids):
    """The present rows with these ids, in the order given (unknown ids are skipped)."""
    found = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(_ROW_SQL + "AND id IN (%s)" % ",".join("?" * len(chunk)),
                                chunk):
            found[row["id"]] = row
    return [found[image_id] for image_id in ids if image_id in found]


def choose_libraries(config, names):
    """The libraries to sample from: those named (by name or id), else every enabled one."""
    if not names:
        return [library for library in common.enabled_libraries(config) if library.get("id")]
    libraries = []
    for name in names:
        library = common.get_library(config, name)
        if library is None or not library.get("id"):
            raise SystemExit("Unknown library: %s" % name)
        if all(library["id"] != other["id"] for other in libraries):
            libraries.append(library)
    return libraries


def kept_local(config, library):
    """True when a library's own endpoint setting names only local endpoints.

    That is the setting that keeps a library's pictures on this computer or
    the local network; a name that matches nothing counts too (nothing was
    meant to leave). Libraries using the default endpoint are not kept local.
    """
    wanted = library.get("endpoint") if isinstance(library, dict) else None
    if not isinstance(wanted, str) or not wanted.strip():
        return False
    wanted = wanted.strip().casefold()
    members = [endpoint for endpoint in common.endpoint_list(config)
               if endpoint["name"].casefold() == wanted
               or (endpoint["group"] and endpoint["group"].casefold() == wanted)]
    return all(common.is_local_endpoint(endpoint) for endpoint in members)


# ---------------------------------------------------------------------------
# Running one endpoint

def resolve_endpoints(config, names):
    """Endpoint settings for a comma-separated list of endpoint (or group) names."""
    endpoints = []
    for name in [part.strip() for part in names.split(",") if part.strip()]:
        endpoint = common.get_endpoint(config, name)
        members = [endpoint] if endpoint is not None else common.resolve_endpoints(config, name)
        if not members:
            known = ", ".join(item["name"] for item in common.endpoint_list(config)) or "none"
            raise SystemExit("Unknown endpoint: %s (configured: %s)" % (name, known))
        for member in members:
            if not member["model"]:
                raise SystemExit("Endpoint %s has no model set." % member["name"])
            if all(member["name"] != other["name"] for other in endpoints):
                endpoints.append(member)
    if not endpoints:
        raise SystemExit("Name at least one endpoint with --endpoints.")
    return endpoints


def parse_limits(values):
    """{endpoint name (lower case): N} from ["name=N", ...]."""
    limits = {}
    for value in values or ():
        name, separator, number = value.partition("=")
        try:
            count = int(number)
        except ValueError:
            count = -1
        if not separator or not name.strip() or count < 0:
            raise SystemExit("--endpoint-limit takes NAME=N (N >= 0), not %r" % value)
        limits[name.strip().casefold()] = count
    return limits


def _record(result):
    """The part of a describe_jpeg() result kept in the report."""
    return {"ok": result["status"] == "ok", "kind": result.get("kind") or "",
            "error": result.get("error") or "", "info": result.get("info"),
            "model": result.get("model") or "", "finish": result.get("finish") or "",
            "seconds": round(result.get("seconds") or 0.0, 3),
            "requests": result.get("requests", 0), "tokens_in": result.get("tokens_in"),
            "tokens_out": result.get("tokens_out"), "cost": result.get("cost"),
            "content_errors": result.get("content_errors", 0),
            "truncated": bool(result.get("truncated")),
            "repaired": bool(result.get("repaired")), "missing": result.get("missing") or []}


def run_endpoint(endpoint, items, profile, ctx, previews, sleep=time.sleep):
    """Describe items ([{"row", "library", "root"}]) with one endpoint.

    Returns the endpoint's report entry: its settings summary, "results"
    ({image id (str): record}) and "summary" (see summarize). A fatal or
    endpoint error stops this endpoint, and so do describe.MAX_ENDPOINT_FAILURES
    failures in a row before its first success (or retryable ones at any
    time); its remaining images, and those cut short by the stop, are
    recorded as not run.
    """
    settings = common.endpoint_settings(endpoint)
    entry = {"name": settings["name"], "label": settings["label"], "kind": settings["kind"],
             "preset": settings["preset"], "model": settings["model"],
             "served_model": "", "concurrency": 1, "max_image_edge": settings["max_image_edge"],
             "price_in": settings["price_in"], "price_out": settings["price_out"],
             "stopped": "", "results": {}, "wall_seconds": 0.0}
    info = llm.probe(settings)
    if info["loading"]:
        ctx.log("Waiting for %s to load its model..." % settings["name"])
        llm.wait_until_ready(settings, STARTUP_WAIT, should_stop=ctx.should_stop, sleep=sleep)
        info = llm.probe(settings)
    entry["served_model"] = info["model"] or ""
    entry["concurrency"] = common.endpoint_concurrency(settings, info["slots"])
    if info["reachable"] is False:
        entry["stopped"] = "the server cannot be reached at %s" % common.redact(
            settings["base_url"])
    ctx.stage("endpoint %s" % settings["name"], total=len(items))
    ctx.log("Endpoint %s: model %s, %d image(s), %d at a time."
            % (settings["name"], entry["served_model"] or settings["model"], len(items),
               entry["concurrency"]))
    stop = threading.Event()

    def should_stop():
        return stop.is_set() or ctx.should_stop()

    def work(item):
        row = item["row"]
        try:
            jpeg = describe.image_for_llm(row, item["root"], previews, None,
                                          max_edge=settings["max_image_edge"])
        except Exception as error:
            return {"status": "error", "kind": "local", "error": str(error)[:500],
                    "requests": 0}
        return describe.describe_jpeg(settings, jpeg, profile=profile,
                                      user_text=prompts.user_text(
                                          item["library"],
                                          projection=describe._projection(row)),
                                      should_stop=should_stop, sleep=sleep)

    def give_up(reason):
        if not entry["stopped"]:
            entry["stopped"] = reason
            ctx.log("Stopped using %s: %s" % (settings["name"], reason), "error")
            stop.set()

    started = time.monotonic()
    pending = list(items)
    succeeded = False       # described an image
    in_a_row = 0            # failures in a row that are probably not the images'
    if not entry["stopped"]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=entry["concurrency"]) as pool:
            in_flight = {}
            while pending or in_flight:
                while pending and len(in_flight) < entry["concurrency"] and not should_stop():
                    item = pending.pop(0)
                    in_flight[pool.submit(work, item)] = item
                if not in_flight:
                    break
                done, _ = concurrent.futures.wait(
                    in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    item = in_flight.pop(future)
                    result = future.result()
                    record = _record(result)
                    ctx.step(item=item["row"]["path"])
                    if should_stop() and record["kind"] in ("retryable", "content"):
                        # Cut short by a stop: recorded as not run, not as a failure.
                        continue
                    entry["results"][str(item["row"]["id"])] = record
                    if record["ok"]:
                        succeeded, in_a_row = True, 0
                    elif record["kind"] in ("fatal", "endpoint"):
                        give_up(llm.explain_error(result.get("exception"), settings))
                    elif record["kind"] == "retryable" or (
                            not succeeded and record["kind"] in ("content", "image")):
                        # As in the describe step: an endpoint that has not
                        # worked yet (or keeps failing) is most likely
                        # misconfigured or down, so it is not sent (and, for
                        # a cloud service, not paid for) the whole sample.
                        in_a_row += 1
                        if in_a_row >= describe.MAX_ENDPOINT_FAILURES:
                            give_up("%d images in a row failed, the last with: %s"
                                    % (in_a_row, record["error"]))
    entry["wall_seconds"] = round(time.monotonic() - started, 3)
    reason = entry["stopped"] or ("stopped" if ctx.should_stop() else "")
    for item in items:
        key = str(item["row"]["id"])
        if key not in entry["results"]:
            entry["results"][key] = {"ok": False, "kind": "not_run", "error": reason,
                                     "info": None, "requests": 0}
    entry["summary"] = summarize(entry)
    return entry


def _mean(values):
    return round(statistics.mean(values), 3) if values else None


def summarize(entry):
    """Per-endpoint numbers: images, failures, timing, tokens, requests, cost."""
    records = [record for record in entry["results"].values() if record["kind"] != "not_run"]
    described = [record for record in records if record["ok"]]
    seconds = [record["seconds"] for record in described]
    tokens_in = [record["tokens_in"] for record in records if record.get("tokens_in") is not None]
    tokens_out = [record["tokens_out"] for record in records
                  if record.get("tokens_out") is not None]
    costs = [record["cost"] for record in records if record.get("cost") is not None]
    wall = entry.get("wall_seconds") or 0.0
    summary = {
        "images": len(records), "described": len(described),
        "failures": len(records) - len(described),
        "parse_failures": sum(1 for record in records if record["kind"] == "content"),
        # Used, but cut off at the length limit (or with broken JSON closed):
        # the fields after the cut are empty.
        "truncated": sum(1 for record in described
                         if record.get("truncated") or record.get("repaired")),
        "content_retries": sum(record.get("content_errors", 0) for record in records),
        "not_run": len(entry["results"]) - len(records),
        "mean_seconds": _mean(seconds),
        "median_seconds": round(statistics.median(seconds), 3) if seconds else None,
        "wall_seconds": wall,
        "images_per_minute": round(len(records) / wall * 60.0, 2) if wall > 0 else None,
        "tokens_in": sum(tokens_in) if tokens_in else None,
        "tokens_out": sum(tokens_out) if tokens_out else None,
        "mean_tokens_in": _mean(tokens_in), "mean_tokens_out": _mean(tokens_out),
        "requests": sum(record.get("requests", 0) for record in records),
        "cost": round(sum(costs), 6) if costs else None,
        "cost_per_10k": (round(sum(costs) / len(records) * 10000, 2)
                         if costs and records else None),
    }
    return summary


# ---------------------------------------------------------------------------
# The report

def thumbnail_uri(thumbs, image_id):
    if thumbs is None:
        return ""
    row = thumbs.execute("SELECT jpeg FROM thumbs WHERE image_id = ?", (image_id,)).fetchone()
    if row is None:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(bytes(row[0])).decode("ascii")


def image_entry(row, library, thumbs):
    return {"id": row["id"], "library": common.library_label(library), "path": row["path"],
            "name": row["name"], "stratum": stratum(row), "taken_at": row["taken_at"],
            "thumb": thumbnail_uri(thumbs, row["id"])}


_STYLE = """
body { font-family: Arial, Helvetica, sans-serif; margin: 16px; color: #1d1f23;
       background: #fafafa; font-size: 13px; }
h1 { font-size: 22px; } h2 { font-size: 17px; margin-top: 28px; }
table { border-collapse: collapse; background: #fff; }
th, td { border: 1px solid #d5d7db; padding: 6px 8px; vertical-align: top; text-align: left; }
th { background: #eef0f3; }
table.summary td.number { text-align: right; white-space: nowrap; }
table.images { table-layout: fixed; width: 100%; }
table.images td.image { width: 220px; }
table.images img { max-width: 200px; max-height: 200px; display: block; }
.title { font-weight: bold; font-size: 14px; margin-bottom: 4px; }
.type, .meta, .stats, .path { color: #5b6068; }
.text { white-space: pre-wrap; background: #f3f4f6; padding: 4px; margin: 4px 0;
        max-height: 160px; overflow: auto; }
.error { color: #a1260d; }
.warn { color: #8a5300; font-weight: bold; }
.tags { color: #2c4f7c; }
p { margin: 4px 0; }
"""


def _escape(value):
    return html.escape("" if value is None else str(value))


def _number(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return ("%." + str(digits) + "f") % value
    return str(value)


def _answer_cell(record):
    if record is None:
        return '<td class="meta">not sent</td>'
    if not record.get("ok"):
        kind = record.get("kind") or "error"
        label = "not run" if kind == "not_run" else "failed (%s)" % kind
        parts = ['<td><p class="error">%s</p>' % _escape(label)]
        if record.get("error"):
            parts.append('<p class="error">%s</p>' % _escape(record["error"]))
    else:
        info = record.get("info") or {}
        parts = ["<td>"]
        if record.get("truncated") or record.get("repaired"):
            parts.append('<p class="warn">%s</p>' % _escape(
                ("cut off at the length limit" if record.get("truncated")
                 else "broken JSON, closed") + (": no %s" % ", ".join(record["missing"])
                                                if record.get("missing") else "")))
        if info.get("title"):
            parts.append('<div class="title">%s</div>' % _escape(info["title"]))
        kind = [info.get("photo_type")]
        if info.get("document_type") not in (None, "", "none"):
            kind.append(info["document_type"])
        for key, nothing in (("setting", "unknown"), ("scene", ""), ("time_of_day", "unknown"),
                             ("event", "none")):
            if info.get(key) not in (None, "", nothing):
                kind.append(info[key])
        if info.get("people") not in (None, "", "none"):
            kind.append("people: %s" % info["people"])
        parts.append('<div class="type">%s</div>' % _escape(" · ".join(
            str(value) for value in kind if value)))
        markers = []
        if info.get("sensitive") not in (None, "", "none"):
            markers.append("sensitive: %s" % info["sensitive"])
        if info.get("private_info"):
            markers.append("private info")
        if markers:
            parts.append('<p class="warn">%s</p>' % _escape(", ".join(markers)))
        parts.append("<p>%s</p>" % _escape(info.get("description")))
        if info.get("tags"):
            parts.append('<p class="tags">%s</p>' % _escape(", ".join(info["tags"])))
        for key in ("subjects", "people_descriptors", "animals", "food", "vehicles",
                    "activities", "landmarks"):
            if info.get(key):
                parts.append('<p><span class="meta">%s:</span> %s</p>'
                             % (_escape(key.replace("_", " ")), _escape(", ".join(info[key]))))
        if info.get("objects"):
            parts.append('<p><span class="meta">objects:</span> %s</p>' % _escape(", ".join(
                "%s ×%s" % (item.get("name"), item.get("count")) for item in info["objects"])))
        if info.get("text"):
            parts.append('<div class="text">%s</div>' % _escape(info["text"]))
        if info.get("quality_flags"):
            parts.append('<p class="meta">quality: %s</p>'
                         % _escape(", ".join(info["quality_flags"])))
        scores = ["%s %s" % (name, info.get(name)) for name in prompts.SCORE_FIELDS
                  if info.get(name) is not None]
        if scores:
            parts.append('<p class="meta">%s</p>' % _escape(" · ".join(scores)))
    stats = "%s s · %s request(s)" % (_number(record.get("seconds")),
                                           record.get("requests", 0))
    if record.get("tokens_in") is not None or record.get("tokens_out") is not None:
        stats += " · %s / %s tokens" % (_number(record.get("tokens_in")),
                                             _number(record.get("tokens_out")))
    if record.get("content_errors"):
        stats += " · %d unusable repl%s" % (record["content_errors"],
                                                 "y" if record["content_errors"] == 1 else "ies")
    parts.append('<p class="stats">%s</p></td>' % _escape(stats))
    return "".join(parts)


def render_html(report):
    """The report as a self-contained HTML page (no scripts, no external files)."""
    endpoints = report["endpoints"]
    out = ['<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n',
           "<title>Model comparison</title>\n<style>%s</style>\n</head>\n<body>\n" % _STYLE,
           "<h1>Model comparison</h1>\n"]
    out.append('<p class="meta">Created %s. Prompt version %s (%s), profile %s, %d image(s)'
               % (_escape(report.get("created")), _escape(report.get("prompt_ver")),
                  _escape(report.get("prompt_hash")), _escape(report.get("profile")),
                  len(report["images"])))
    if report.get("seed") is not None:
        out.append(", seed %s" % _escape(report["seed"]))
    out.append(".</p>\n<h2>Summary</h2>\n<table class=\"summary\">\n<tr>")
    for heading in ("Endpoint", "Model", "Images", "Failed", "Unusable replies",
                    "Cut off (used)", "Mean s/image", "Median s/image",
                    "Images/min (wall clock)", "Mean tokens in / out", "Requests",
                    "Est. cost per 10k images", "Notes"):
        out.append("<th>%s</th>" % _escape(heading))
    out.append("</tr>\n")
    for entry in endpoints:
        summary = entry["summary"]
        cost = summary.get("cost_per_10k")
        cells = [
            _escape(entry["name"]),
            _escape(entry.get("served_model") or entry.get("model")),
            _number(summary["images"]), _number(summary["failures"]),
            _number(summary["parse_failures"]), _number(summary.get("truncated", 0)),
            _number(summary["mean_seconds"]),
            _number(summary["median_seconds"]), _number(summary["images_per_minute"], 1),
            "%s / %s" % (_number(summary["mean_tokens_in"], 0),
                         _number(summary["mean_tokens_out"], 0)),
            _number(summary["requests"]),
            "$%.2f" % cost if cost is not None else "-",
            _escape(entry.get("stopped") or ""),
        ]
        out.append("<tr>%s</tr>\n" % "".join(
            '<td class="number">%s</td>' % cell if 2 <= index <= 11 else "<td>%s</td>" % cell
            for index, cell in enumerate(cells)))
    out.append("</table>\n<h2>Images</h2>\n<table class=\"images\">\n<tr><th>Image</th>")
    for entry in endpoints:
        out.append("<th>%s</th>" % _escape(entry["name"]))
    out.append("</tr>\n")
    for image in report["images"]:
        key = str(image["id"])
        out.append('<tr><td class="image">')
        if image.get("thumb", "").startswith("data:image/"):
            out.append('<img src="%s" alt="">' % _escape(image["thumb"]))
        out.append('<div class="path">%s / %s</div><div class="meta">#%s, %s%s</div></td>'
                   % (_escape(image.get("library")), _escape(image.get("path")),
                      _escape(image["id"]), _escape(image.get("stratum")),
                      ", " + _escape(image["taken_at"][:10]) if image.get("taken_at") else ""))
        for entry in endpoints:
            out.append(_answer_cell(entry["results"].get(key)))
        out.append("</tr>\n")
    out.append("</table>\n</body>\n</html>\n")
    return "".join(out)


def output_paths(out=None):
    """(html path, json path) for --out (default: data/reports/compare-<time>)."""
    if not out:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = os.path.join(common.reports_dir(), "compare-%s.html" % stamp)
    base, extension = os.path.splitext(out)
    if extension.lower() not in (".html", ".htm", ".json"):
        base, extension = out, ".html"
    html_path = base + (extension if extension.lower() in (".html", ".htm") else ".html")
    return html_path, base + ".json"


def write_report(report, out=None):
    """Write the HTML report and the raw JSON next to it. Returns (html path, json path)."""
    html_path, json_path = output_paths(out)
    for path in (html_path, json_path):
        common.ensure_dir(os.path.dirname(os.path.abspath(path)))
    common.write_file_atomic(json_path, json.dumps(report, indent=1, ensure_ascii=False,
                                                   allow_nan=False) + "\n")
    common.write_file_atomic(html_path, render_html(report))
    return html_path, json_path


def load_report(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("format") != REPORT_FORMAT:
        raise SystemExit("%s is not a compare_models.py result file." % path)
    return data


def merge_reports(reports):
    """One report from several (same images, different endpoints).

    Images are combined by id (first seen first); endpoints are listed in
    order, a repeated name getting " (2)", " (3)", ... added.
    """
    merged = {"format": REPORT_FORMAT, "version": REPORT_VERSION,
              "created": time.strftime("%Y-%m-%d %H:%M:%S"), "merged": True,
              "images": [], "endpoints": []}
    for key in ("prompt_ver", "prompt_hash", "profile", "seed"):
        values = []
        for report in reports:
            if report.get(key) not in values:
                values.append(report.get(key))
        merged[key] = values[0] if len(values) == 1 else ", ".join(str(v) for v in values)
    seen_images = set()
    names = set()
    for report in reports:
        for image in report.get("images", []):
            if image["id"] not in seen_images:
                seen_images.add(image["id"])
                merged["images"].append(image)
        for entry in report.get("endpoints", []):
            entry = dict(entry)
            name, number = entry["name"], 2
            while entry["name"].casefold() in names:
                entry["name"] = "%s (%d)" % (name, number)
                number += 1
            names.add(entry["name"].casefold())
            merged["endpoints"].append(entry)
    return merged


# ---------------------------------------------------------------------------
# Command line

def compare(config, endpoint_names, library_names=None, sample=DEFAULT_SAMPLE, ids=None,
            seed=DEFAULT_SEED, profile="core", limits=None, ctx=None, sleep=time.sleep):
    """Run the comparison and return the report dict (see the module docstring)."""
    ctx = ctx or jobs.ConsoleContext("compare")
    endpoints = resolve_endpoints(config, endpoint_names)
    limits = limits or {}
    libraries = choose_libraries(config, library_names)
    by_id = {library["id"]: library for library in common.enabled_libraries(config)
             if library.get("id")}
    by_id.update({library["id"]: library for library in libraries})
    conn = db.connect_readonly()
    thumbs = None
    try:
        if os.path.isfile(common.thumbs_db_path()):
            thumbs = db.connect_readonly(common.thumbs_db_path())
        if ids:
            rows = [row for row in rows_by_id(conn, ids) if row["library_id"] in by_id]
        else:
            library_ids = [library["id"] for library in libraries]
            rows = stratified_order(candidate_rows(conn, library_ids), library_ids,
                                    seed)[:max(0, sample)]
        if not rows:
            raise SystemExit("No indexed images to compare. Run scan.py first.")
        report = {"format": REPORT_FORMAT, "version": REPORT_VERSION,
                  "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "prompt_ver": prompts.PROMPT_VER, "prompt_hash": prompts.prompt_hash(profile),
                  "profile": profile, "seed": None if ids else seed,
                  "images": [image_entry(row, by_id[row["library_id"]], thumbs)
                             for row in rows],
                  "endpoints": []}
    finally:
        conn.close()
        if thumbs is not None:
            thumbs.close()
    items = [{"row": row, "library": by_id[row["library_id"]],
              "root": common.library_root(by_id[row["library_id"]])} for row in rows]
    previews = cache.PreviewStore()
    for endpoint in endpoints:
        if ctx.should_stop():
            break
        chosen, held = items, []
        if not library_names and not common.is_local_endpoint(endpoint):
            # Libraries kept on local endpoints are only sent elsewhere
            # when they are named with --library.
            local_ids = {library_id for library_id, library in by_id.items()
                         if kept_local(config, library)}
            held = [item for item in items if item["row"]["library_id"] in local_ids]
            chosen = [item for item in items if item["row"]["library_id"] not in local_ids]
            if held:
                ctx.log("Not sending %d image(s) to %s: their libraries (%s) are set to use "
                        "local endpoints only. Name them with --library to include them."
                        % (len(held), endpoint["name"], ", ".join(sorted(
                            {common.library_label(item["library"]) for item in held}))),
                        "warn")
        hidden = []
        if not common.is_local_endpoint(endpoint):
            # Hidden pictures never leave this computer and network.
            hidden = [item for item in chosen if item["row"]["hidden"]]
            chosen = [item for item in chosen if not item["row"]["hidden"]]
            if hidden:
                ctx.log("%d hidden picture(s) were not sent to %s (%s): hidden pictures are "
                        "only described by endpoints on this computer or the local network."
                        % (len(hidden), endpoint["name"], common.url_host(endpoint["base_url"])),
                        "warn")
        limit = limits.get(endpoint["name"].casefold())
        if limit is not None:
            chosen = chosen[:limit]
        for key in (endpoint.get("api_key"), common.resolve_api_key(endpoint)[0]):
            ctx.add_secret(key)
        entry = run_endpoint(endpoint, chosen, profile, ctx, previews, sleep)
        for item in held:
            entry["results"][str(item["row"]["id"])] = {
                "ok": False, "kind": "not_run", "info": None, "requests": 0,
                "error": "its library is kept on local endpoints"}
        for item in hidden:
            entry["results"][str(item["row"]["id"])] = {
                "ok": False, "kind": "not_run", "info": None, "requests": 0,
                "error": "hidden pictures are not sent outside this computer and network"}
        if held or hidden:
            entry["summary"] = summarize(entry)
        report["endpoints"].append(entry)
    return report


def main(argv=None):
    """Command line entry point. Returns the process exit code."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(
        description="Compare how LLM endpoints describe the same images. The database is "
                    "only read.")
    parser.add_argument("--endpoints", metavar="NAME,NAME",
                        help="Endpoints (or groups) to compare, separated by commas.")
    parser.add_argument("--library", action="append", metavar="NAME_OR_ID",
                        help="Sample from this library (repeat for more; default: all "
                             "enabled libraries).")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, metavar="N",
                       help="How many images to sample (default %d)." % DEFAULT_SAMPLE)
    which.add_argument("--ids", metavar="1,2,3", help="Use these image ids instead.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="Random seed for the sample (default %d)." % DEFAULT_SEED)
    parser.add_argument("--profile", choices=prompts.PROFILES, default="core",
                        help="Metadata profile to ask for (default core).")
    parser.add_argument("--endpoint-limit", action="append", metavar="NAME=N",
                        help="Send only the first N sampled images to this endpoint.")
    parser.add_argument("--out", metavar="PATH",
                        help="Report file (default data/reports/compare-<time>.html); the "
                             "raw JSON is written next to it.")
    parser.add_argument("--merge", nargs="+", metavar="FILE.json",
                        help="Combine earlier JSON results into one report instead.")
    args = parser.parse_args(argv)
    try:
        if args.merge:
            report = merge_reports([load_report(path) for path in args.merge])
            html_path, json_path = write_report(report, args.out)
            print("Report: %s\nResults: %s" % (html_path, json_path))
            return 0
        if not args.endpoints:
            parser.error("--endpoints is required (or use --merge)")
        ids = None
        if args.ids:
            try:
                ids = [int(part) for part in args.ids.split(",") if part.strip()]
            except ValueError:
                parser.error("--ids takes numbers separated by commas")
        config = common.load_config()
        report = compare(config, args.endpoints, args.library, args.sample, ids, args.seed,
                         args.profile, parse_limits(args.endpoint_limit))
    except (common.ConfigError, db.DatabaseError) as error:
        print("Error: %s" % error)
        return 2
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C); no report was written.")
        return 130
    except SystemExit as error:
        if isinstance(error.code, str):
            print("Error: %s" % error.code)
            return 2
        raise
    html_path, json_path = write_report(report, args.out)
    for entry in report["endpoints"]:
        summary = entry["summary"]
        print("%s: %d described%s, %d failed, %s s/image (median), %s requests"
              % (entry["name"], summary["described"],
                 " (%d cut off)" % summary["truncated"] if summary.get("truncated") else "",
                 summary["failures"], _number(summary["median_seconds"]),
                 summary["requests"]))
    print("Report: %s\nResults: %s" % (html_path, json_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
