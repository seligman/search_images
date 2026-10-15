#!/usr/bin/env python3
"""The web server's admin API: libraries, jobs and LLM endpoints (/api/admin/...).

These routes change settings, start work and look at folders, so every
request must:

- come from this computer (a loopback address), and the server must be
  listening on localhost (with --host, other computers get read-only search
  and these routes are off);
- carry the header "X-Search-Images: 1": a page on another site cannot send
  it without first asking this server (a CORS preflight), which is never
  allowed;
- not be marked cross-site by the browser (Sec-Fetch-Site), nor come from
  another origin;
- name a known host (checked for every request by serve.py);
- send its body as JSON.

Routes that take a folder are POST-only, so a link or an image on another
page can never make the server touch a network path. Responses never hold
API keys, only where a key comes from (key_source) and whether one is set
(key_set). Error messages pass through common.redact.

config.json is changed under one lock and re-read first, so changes made
by hand are kept. A request may send the config_token it last saw (in the
body or the X-Config-Token header); when config.json has changed since,
the answer is 409 and the page reloads.

The routes are registered by serve.build_app (see init_app).
"""

import copy
import ipaddress
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse

import albums
import common
import db
import drives
import duplicates
import embeddings
import export
import gps_check
import image_edits
import jobs
import library_ops
import llm
import local_server
import local_times
import looks
import named_places
import not_duplicates
import opening_views
import people
import people_groups
import people_suggest
import place_estimates
import prompts
import refine
import search_index
import settings
import sheet_talk
import trip_export
import trip_stories
import trips
import videos

URL_PREFIX = "/api/admin"
HEADER = "X-Search-Images"
TOKEN_HEADER = "X-Config-Token"
SAMPLE_IMAGE = os.path.join(common.BASE_DIR, "sample", "images", "18-chalkboard-menu.jpg")
TEST_TIMEOUT = 120          # seconds a Test of an endpoint may take
MODELS_TIMEOUT = 20         # seconds for listing an endpoint's models
FAILURE_LIMIT = 100
FAILURE_LIMIT_MAX = 1000
REACHABILITY_TIMEOUT = 3.0  # seconds a save waits to say a folder cannot be reached
FOLDER_STATUS_SECONDS = 60.0
PROBE_SECONDS = 30.0
KEY_FILE_SECONDS = 30.0     # how long a key file's state is remembered
KEY_FILE_WAIT = 0.5         # seconds a request waits for a key file to be read
KEY_FILE_TIMEOUT = 2.0      # seconds a save waits to look at a key file
REGISTER_WAIT = 2.0         # seconds a save waits to record library names in index.db
REGISTER_RETRY = 5.0        # seconds between later tries when that had to wait
REGISTER_TRIES = 60
HIDE_LIMIT = 10000          # pictures one hide/unhide (or paths, or tags) request may name
DESCRIBE_IDS_LIMIT = 1000   # pictures one describe job may name
ID_CHUNK = 500              # ids per SQL statement (older SQLite allows 999 parameters)
IMAGE_ID_MAX = 2 ** 63 - 1  # SQLite integers are 64-bit
# Parameters each kind of job accepts (all optional).
JOB_PARAMS = {
    "scan": ("allow_mass_removal",),
    "describe": ("retry_failed", "redescribe_stale", "limit", "endpoint", "ids", "force"),
    "refine": ("limit", "retry_failed"),
    "embed": ("limit",),
    "image_embed": ("limit", "rebuild"),
    "process": ("allow_mass_removal", "retry_failed", "redescribe_stale", "limit", "endpoint"),
    "previews": (),
    "delete_library": ("confirm_name",),
    "export": ("options", "save_options", "out_dir", "trip"),
    "proxies": (),
    "drive_previews": (),
    "faces": ("limit", "retry_failed"),
    "group_faces": (),
    "delete_faces": ("confirm",),
    "trip_stories": ("trip", "redo"),
}
BOOL_PARAMS = ("allow_mass_removal", "retry_failed", "redescribe_stale", "force", "save_options",
               "rebuild", "redo")
EXPORT_FOLDER_TIMEOUT = 5.0  # seconds to look at an export's output folder
MODEL_LIST_SECONDS = 30.0   # how long a listing of the models folder is reused
MODEL_LIST_WAIT = 1.0       # seconds a request waits for a first listing
ENDPOINT_TEXT_FIELDS = ("label", "kind", "preset", "base_url", "model", "api_key_env",
                        "api_key_file", "group")
# number fields: (type, minimum, maximum or None)
ENDPOINT_NUMBER_FIELDS = {
    "concurrency": (int, 1, common.MAX_CONCURRENCY),
    "max_image_edge": (int, 64, 8000),
    "timeout": (float, 1, 3600),
    "max_output_tokens": (int, 1, 1000000),
    "price_in": (float, 0, None),
    "price_out": (float, 0, None),
}
ENDPOINT_FIELDS = (("name", "extra", "enabled", "api_key", "clear_api_key")
                   + ENDPOINT_TEXT_FIELDS + tuple(ENDPOINT_NUMBER_FIELDS))

# One writer of config.json at a time in this process.
_config_write_lock = threading.Lock()


class AdminError(Exception):
    """A request that cannot be done: answered with status and {"error", "fields", ...}."""

    def __init__(self, status, message, fields=None, **extra):
        Exception.__init__(self, message)
        self.status = status
        self.fields = dict(fields or {})
        self.extra = extra


# ---------------------------------------------------------------------------
# Request checks

def is_loopback(address):
    """True for a loopback address (127.x.x.x, ::1, or ::ffff:127.x.x.x)."""
    text = (address or "").strip().strip("[]").split("%")[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def is_loopback_host(host):
    """True when the server is bound to a loopback address (the default 127.0.0.1)."""
    host = (host or "").strip().strip("[]").lower()
    return host == "localhost" or is_loopback(host)


def admin_allowed(request, enabled=True):
    """True when this request may use the admin pages and routes (see the module notes)."""
    return bool(enabled) and is_loopback(request.remote_addr)


def config_token():
    """A token that changes whenever config.json does (see common.config_token)."""
    return common.config_token()


def redact_tree(value, secrets=()):
    """value with common.redact applied to every string inside it."""
    if isinstance(value, str):
        return common.redact(value, secrets)
    if isinstance(value, dict):
        return {key: redact_tree(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_tree(item, secrets) for item in value]
    return value


# ---------------------------------------------------------------------------
# Views of the settings (never with API keys)

def _raw_endpoints(config):
    """The endpoint dicts as stored, in the order common.endpoint_list() uses."""
    endpoints = config.get("endpoints")
    if isinstance(endpoints, list):
        return [item for item in endpoints if isinstance(item, dict)]
    legacy = config.get("endpoint")
    items = legacy if isinstance(legacy, list) else [legacy]
    # (endpoint_list leaves out an unused older placeholder too.)
    return [item for item in items
            if isinstance(item, dict) and not common.is_placeholder_endpoint(item)]


def probe_view(info):
    """A local server's status from llm.probe(): {"state", "model", "slots", "vision", "hint"}."""
    if info is None:
        return {"state": "checking", "model": None, "slots": None, "vision": None, "hint": ""}
    if info.get("reachable") is False:
        state = "not running"
        hint = ("Start the model server (for llama.cpp: llama-server -m model.gguf --mmproj "
                "mmproj.gguf) and check the base URL.")
    elif info.get("loading"):
        state, hint = "loading", "The server is still loading its model."
    elif info.get("reachable"):
        state, hint = "ready", ""
    else:
        state, hint = "unknown", ""
    if state == "ready" and info.get("vision") is False:
        hint = "The loaded model cannot see images (for llama.cpp, add --mmproj)."
    return {"state": state, "model": info.get("model"), "slots": info.get("slots"),
            "vision": info.get("vision"), "hint": hint}


def _key_shaped(settings):
    """settings (a dict) with key sources that look like keys masked.

    A key typed into the variable name or key file field by mistake is
    never shown again (saving one is refused, but config.json may be
    edited by hand).
    """
    masked = dict(settings)
    for field in ("api_key_env", "api_key_file"):
        value = masked.get(field)
        if isinstance(value, str) and common.looks_like_key(value):
            masked[field] = common.REDACTED
    return masked


def endpoint_view(config, endpoint, raw=None, status=None, key_info=None):
    """What the browser may see of an endpoint: its settings without the API key.

    Adds key_source / key_set, where the server is (local, host, insecure),
    the preset's label, whether it is the default, the libraries that name
    it, and the probe status of a local server (status). "stored" holds the
    values as written in config.json (also without the key), for editing.
    key_info is (key, source) when the caller has it (a key file is then
    not read again).
    """
    key, source = key_info if key_info is not None else common.resolve_api_key(endpoint)
    secrets = [key] if key else []
    view = _key_shaped({name: value for name, value in endpoint.items() if name != "api_key"})
    view["base_url"] = common.redact(endpoint["base_url"], secrets)
    view["extra"] = redact_tree(endpoint["extra"], secrets)
    default = common.default_endpoint_name(config)
    local = common.is_local_endpoint(endpoint)
    view.update(
        key_source=source, key_set=bool(key),
        stored_key=bool(isinstance(raw, dict) and raw.get("api_key")),
        preset_label=common.ENDPOINT_PRESETS[endpoint["preset"]]["label"],
        local=local, host=common.url_host(endpoint["base_url"]),
        insecure=common.insecure_endpoint(endpoint),
        default=bool(default) and default.casefold() in (
            endpoint["name"].casefold(), endpoint["group"].casefold()),
        used_by=[library["id"] for library in config.get("libraries", [])
                 if library.get("id") and any(
                     item["name"] == endpoint["name"]
                     for item in common.endpoints_for_library(config, library))],
        refined_by=[library["id"] for library in config.get("libraries", [])
                    if library.get("id") and refine.enabled(library) and any(
                        item["name"] == endpoint["name"]
                        for item in common.resolve_endpoints(
                            config, refine.settings(library)["endpoint"]))],
        status=probe_view(status) if local else None)
    wanted = embeddings.settings(config)["endpoint"]
    # The endpoint of search by meaning: an embedding model, which sees no images.
    view["embedding"] = bool(wanted) and wanted.casefold() == endpoint["name"].casefold()
    if view["embedding"] and view["status"] and view["status"]["vision"] is False:
        view["status"]["hint"] = ""
    if isinstance(raw, dict):
        view["stored"] = redact_tree(_key_shaped({name: value for name, value in raw.items()
                                                  if name != "api_key"}), secrets)
    return view


def _key_source(settings):
    """("env", name) or ("file", full path) of the key an endpoint reads, or None.

    Only the sources resolve_api_key reads from outside config.json; the
    file is not opened.
    """
    if settings["api_key_env"]:
        return ("env", settings["api_key_env"])
    if settings["api_key_file"]:
        full = common.resolve_config_relative(settings["api_key_file"])
        return ("file", os.path.normcase(os.path.abspath(full)))
    return None


def unsaved_key_problem(saved_config, endpoint):
    """Why an endpoint that is not saved must not send the key it names, or None.

    Listing models saves nothing, so its request may name any key file or
    environment variable together with any address. Such a key is only
    sent where a saved endpoint already sends it, or to the cloud service
    of the endpoint's preset (its own address): any program on this
    computer can call these routes, and must not be able to read a file
    or variable through them.
    """
    settings = common.endpoint_settings(endpoint)
    source = _key_source(settings)
    if source is None:
        return None
    if settings["preset"] in common.CLOUD_PRESETS and common.preset_host(
            settings["preset"], common.url_host(settings["base_url"])):
        return None
    for saved in common.endpoint_list(saved_config):
        if _key_source(saved) == source and saved["base_url"] == settings["base_url"]:
            return None
    return ("Save the endpoint first: a key from a file or an environment variable is only "
            "sent to an address a saved endpoint uses, or to the service its kind names.")


def preset_views():
    """{preset name: {label, kind, base_url, key_env, concurrency, notes, cloud}}."""
    return {name: dict(info, cloud=name in common.CLOUD_PRESETS)
            for name, info in common.ENDPOINT_PRESETS.items()}


def key_problem(source):
    """Why no API key was found, in words, from a resolve_api_key() source."""
    if source.startswith("env ") and source.endswith(" not set"):
        name = source[4:-8]
        if name == "(invalid name)":
            return "the name of its environment variable is not valid"
        return "the environment variable %s is not set" % name
    return {"file missing": "its key file is missing or cannot be read",
            "file empty": "its key file is empty",
            "file not responding": "its key file does not answer (an offline share?)",
            }.get(source, "no API key is set")


def library_endpoint_info(config, library, key_info=None, status_of=None):
    """Which endpoints describe a library, whether they can, and where pictures go.

    {"name", "endpoints", "ready", "problem", "warning", "cloud",
    "cloud_hosts", "local"}. ready is False when no endpoint has a model, or
    the only ones are cloud services without an API key (nothing may be
    sent without one); problem then says why. cloud and cloud_hosts only
    count endpoints with a model: one without can send nothing anywhere;
    local is True when one with a model is on this computer or network
    (only those describe hidden pictures). warning
    mentions a local model
    server that is not running (status_of(endpoint) gives its probe_view,
    when the caller knows it): describing works again once it is started.
    key_info(endpoint) gives (key, source) (default common.resolve_api_key).
    """
    key_info = key_info or common.resolve_api_key
    wanted = library.get("endpoint") or common.default_endpoint_name(config)
    endpoints = common.endpoints_for_library(config, library)
    cloud = [endpoint for endpoint in endpoints
             if endpoint["model"] and not common.is_local_endpoint(endpoint)]
    usable, problems, warnings = [], [], []
    for endpoint in endpoints:
        server = local_server.managed(config, endpoint["name"])
        if not endpoint["model"]:
            problems.append("The endpoint %s has no model set." % endpoint["name"])
            continue
        if endpoint["preset"] in common.CLOUD_PRESETS:
            key, source = key_info(endpoint)
            if not key and source != "file checking":
                problems.append("The endpoint %s has no API key: %s."
                                % (endpoint["name"], key_problem(source)))
                continue
        usable.append(endpoint)
        status = status_of(endpoint) if status_of is not None else None
        if server is not None and server["auto_start"]:
            continue        # describing starts its model server (local_server)
        if status and status.get("state") == "not running":
            warnings.append("The model server of %s (%s) is not running."
                            % (endpoint["name"], common.url_host(endpoint["base_url"])))
    if not endpoints:
        problems.append("The endpoint %s is missing or turned off." % library["endpoint"]
                        if library.get("endpoint") else
                        "No AI model is set up for this library yet.")
    return {"name": wanted, "endpoints": [endpoint["name"] for endpoint in endpoints],
            "ready": bool(usable), "problem": "" if usable else " ".join(problems),
            "warning": " ".join(warnings) if usable and len(warnings) == len(usable) else "",
            "cloud": bool(cloud),
            "cloud_hosts": sorted({common.url_host(endpoint["base_url"]) for endpoint in cloud}),
            "local": any(endpoint["model"] and common.is_local_endpoint(endpoint)
                         for endpoint in endpoints)}


def counts_view(counts):
    """A library's counts for the browser.

    total = present + missing; pending = present images still to describe.
    """
    present = counts.get("total", 0)
    return {"total": present + counts.get("missing", 0), "present": present,
            "missing": counts.get("missing", 0), "hidden": counts.get("hidden", 0),
            "described": counts.get("described", 0), "stale": counts.get("stale", 0),
            "failed": counts.get("failed", 0), "gave_up": counts.get("gave_up", 0),
            "pending": max(0, present - counts.get("described", 0) - counts.get("gave_up", 0))}


def refine_endpoints(config, library):
    """The endpoints (with a model) of a library's refine pass; [] when it has none."""
    wanted = refine.settings(library)["endpoint"]
    if not wanted:
        return []
    return [endpoint for endpoint in common.resolve_endpoints(config, wanted)
            if endpoint["model"]]


def refine_view(config, library, counted=None, key_info=None):
    """A library's refine pass for the Manage page.

    {"settings": refine.settings(), "enabled", "ready" (an endpoint of it
    can be used: it has a model, and a cloud one an API key), "problem",
    "cloud", "cloud_hosts", "local" (hidden pictures can be refined: an
    endpoint is on this computer or network), and from counted
    (refine.counts; None when not counted): "pending" (pictures it would
    describe again), "hidden" (hidden pictures it leaves alone because
    they may not be sent to its endpoints) and "failed" (pictures skipped
    after failing; Retry failed tries them again)}. key_info(endpoint)
    gives (key, source) (default common.resolve_api_key).
    """
    key_info = key_info or common.resolve_api_key
    wanted = refine.settings(library)
    endpoints = refine_endpoints(config, library)
    usable, problems = [], []
    for endpoint in endpoints:
        if endpoint["preset"] in common.CLOUD_PRESETS:
            key, source = key_info(endpoint)
            if not key and source != "file checking":
                problems.append("The refine endpoint %s has no API key: %s."
                                % (endpoint["name"], key_problem(source)))
                continue
        usable.append(endpoint)
    cloud = [endpoint for endpoint in usable if not common.is_local_endpoint(endpoint)]
    problem = ""
    if wanted["endpoint"] and not endpoints:
        problem = ("The refine endpoint %s is missing, turned off or has no model."
                   % wanted["endpoint"])
    elif wanted["endpoint"] and not usable:
        problem = " ".join(problems)
    counted = counted or {}
    return {"settings": wanted, "enabled": bool(wanted["endpoint"]), "ready": bool(usable),
            "problem": problem, "cloud": bool(cloud),
            "cloud_hosts": sorted({common.url_host(endpoint["base_url"]) for endpoint in cloud}),
            "local": any(common.is_local_endpoint(endpoint) for endpoint in usable),
            "pending": counted.get("pending"), "hidden": counted.get("hidden"),
            "failed": counted.get("failed")}


def refine_choices():
    """The kinds of picture a refine pass can be asked for (for the library dialog)."""
    return {"photo_types": list(refine.PHOTO_TYPE_CHOICES),
            "document_types": list(refine.DOCUMENT_TYPE_CHOICES),
            "defaults": refine.settings({}),
            "max_output_tokens_range": list(refine.MAX_OUTPUT_TOKENS_RANGE)}


def library_view(config, library, counts, latest, status, endpoint_info=None,
                 refine_info=None, meaning=None):
    """A library as the manage page shows it (settings, counts, status, last job).

    latest is JobManager.latest_by_library(); endpoint_info the library's
    library_endpoint_info() and refine_info its refine_view() when the
    caller has worked them out. meaning is the library's
    embeddings.counts() entry (search by meaning), when counted.
    """
    view = {key: copy.deepcopy(library.get(key)) for key in ("id",) + library_ops.LIBRARY_FIELDS}
    view["root"] = common.library_root(library)
    view["counts"] = counts_view(counts.get(library.get("id"), {}))
    view["status"] = status
    own, everyone = latest.get(library.get("id")), latest.get(None)
    if everyone is not None and (own is None or everyone["id"] > own["id"]):
        own = everyone
    view["last_job"] = own
    view["endpoint_info"] = (endpoint_info if endpoint_info is not None
                             else library_endpoint_info(config, library))
    waiting = view["endpoint_info"].get("hidden_waiting")
    if waiting:
        # Hidden pictures are never sent to the library's (cloud) endpoints:
        # they are not "to describe" there, and are counted on their own.
        view["counts"]["pending"] = max(0, view["counts"]["pending"] - waiting)
        view["counts"]["pending_hidden"] = waiting
    view["export_info"] = export_view(config, library)
    view["refine_info"] = (refine_info if refine_info is not None
                           else refine_view(config, library))
    if meaning is not None:
        view["meaning"] = dict(meaning, pending=meaning["stale"] + meaning["missing"])
    return view


def video_counts(conn):
    """{library id: how many present videos it has}."""
    return {row[0]: row[1] for row in conn.execute(
        "SELECT library_id, COUNT(*) FROM images WHERE media = 'video' "
        "AND missing_since IS NULL GROUP BY library_id")}


def videos_view(config):
    """Videos for the Manage page: {"available", "problem", "ffmpeg", "proxies",
    "drive_previews", "sheet_check"}.

    available: ffmpeg and ffprobe were found (problem says what to do
    otherwise); proxies: the "video_proxies" settings (videos.proxy_settings);
    drive_previews: the "drive_previews" settings (drives.preview_settings);
    sheet_check: a video whose description talks about its frames is asked
    again ("video_sheet_check", see sheet_talk).
    """
    status = videos.tools_status(config)
    return {"available": status["available"], "problem": status["problem"],
            "ffmpeg": status["ffmpeg"], "proxies": videos.proxy_settings(config),
            "drive_previews": drives.preview_settings(config),
            "sheet_check": sheet_talk.enabled(config)}


def library_video_view(conn, config, library, counts, usage, drive_usage=None):
    """A library's videos for the Manage page: {"on", "count", "proxies": {count, bytes,
    pending}, "drives" (drives.library_view, when it has videos)}.

    counts is video_counts(); usage videos.proxy_usage(); drive_usage
    drives.preview_usage(). pending (the videos that would get a playable
    copy) is only counted while playable copies are turned on.
    """
    library_id = library.get("id")
    view = {"on": common.library_videos(library), "count": counts.get(library_id, 0),
            "proxies": dict(usage.get(library_id) or {"count": 0, "bytes": 0}, pending=None)}
    if view["count"] and videos.proxy_settings(config)["enabled"]:
        try:
            view["proxies"]["pending"] = sum(
                1 for row in videos.proxy_candidates(conn, library_id)
                if not videos.existing_proxy(library_id, row["id"], row["content_hash"]))
        except sqlite3.Error:
            pass
    if view["count"]:
        try:
            view["drives"] = drives.library_view(conn, config, library_id, drive_usage)
        except sqlite3.Error:
            pass
    return view


def meaning_view(config):
    """Search by meaning for the Manage page: {"configured", "ready", "problem" (why it
    cannot be used, or how to turn it on), "endpoint"}."""
    options = embeddings.settings(config)
    endpoint, problem = embeddings.endpoint_for(config, options)
    if endpoint is not None:
        # (A model file that cannot be read now: nothing is embedded, and the
        # vectors stay as they are until it can.)
        problem = embeddings.recipe_problem(config, options)
        if problem:
            endpoint = None
    return {"configured": bool(options["endpoint"]), "ready": endpoint is not None,
            "problem": problem, "endpoint": options["endpoint"]}


def export_view(config, library):
    """A library's export for the Manage page.

    {"options": its saved export options with every default filled in,
    "default_output": the folder an export goes to unless another one is
    chosen, "last": export.last_export() (None before the first export),
    "meaning": {"available", "reason"}: whether search by meaning in the
    browser can go with it (export.meaning_status; the review step says it
    for the pictures chosen), "option_warnings": a line for each saved
    option an export leaves at its default (export.library_option_warnings),
    "saved_text": the saved title and credits among them, as saved, for the
    dialog's fields (export.saved_text_options)}.
    """
    options = export.library_options(library)
    status = export.meaning_status(config, options)
    return {"options": options,
            "default_output": export.default_output_dir(config, library),
            "last": export.last_export(library.get("id")) if library.get("id") else None,
            "meaning": {"available": status["available"], "reason": status["reason"]},
            "option_warnings": export.library_option_warnings(library),
            "saved_text": export.saved_text_options(library.get("export"))}


def options_error(error):
    """An AdminError for export options that cannot be used (export.OptionsError)."""
    return AdminError(400, "Some export options cannot be used: %s." % error, error.errors)


def find_library(config, library_id):
    """The library with this id in config (the dict itself); AdminError 404 if there is none."""
    for library in config.get("libraries", []):
        if library.get("id") == library_id:
            return library
    raise AdminError(404, "There is no library with the id %s." % library_id)


def image_ids(value, limit, name="ids"):
    """The image ids sent in a request: a list of whole numbers, without repeats, in order.

    Raises AdminError for anything else, an empty list, or more than limit ids.
    """
    if not isinstance(value, list) or not value:
        raise AdminError(400, "Send %s as a list of picture ids." % name,
                         {name: "A list of picture ids."})
    if len(value) > limit:
        raise AdminError(400, "At most %d pictures can be sent at a time; send them in parts."
                         % limit, {name: "Too many."})
    result = []
    seen = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 < item <= IMAGE_ID_MAX:
            raise AdminError(400, "Picture ids are whole numbers.", {name: "Not a picture id."})
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def id_chunks(ids):
    """ids in slices of at most ID_CHUNK."""
    for start in range(0, len(ids), ID_CHUNK):
        yield ids[start:start + ID_CHUNK]


# ---------------------------------------------------------------------------
# Changing endpoints

def _endpoint_number(name, value, errors):
    kind, low, high = ENDPOINT_NUMBER_FIELDS[name]
    if isinstance(value, str):
        try:
            value = float(value.strip()) if kind is float else int(value.strip())
        except ValueError:
            errors[name] = "Enter a number."
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors[name] = "Enter a number."
        return None
    if kind is int and value != int(value):
        errors[name] = "Enter a whole number."
        return None
    value = int(value) if kind is int else float(value)
    if value < low or (high is not None and value > high):
        errors[name] = ("Enter a number from %s to %s." % (low, high) if high is not None
                        else "Enter a number of at least %s." % low)
        return None
    return value


def _file_check(test, path):
    """test(path) (os.path.exists, os.path.isfile) with a time limit; None if no answer came."""
    try:
        return library_ops.run_with_timeout(test, KEY_FILE_TIMEOUT, path)
    except library_ops.TimedOut:
        return None


MODEL_REQUIRED = ("Enter the model's name. For llama.cpp any name works (it uses the model it "
                  "has loaded), such as the model file's name; List models fills it in.")


def apply_endpoint(config, data, original_name=None, replace=False, require_model=True):
    """Add or change an endpoint in config (in place) from fields sent by the browser.

    The endpoint named original_name is changed; without original_name a
    new one is added (the first endpoint, or a local one when there is no
    usable default, becomes the default). Adding a name that is taken is
    refused unless replace is set: then that endpoint is changed. Fields
    that are not sent keep their values. The API key is write-only: a
    blank api_key keeps the stored key, clear_api_key removes it, and a
    stored key is removed when the base URL moves to another scheme, host
    or port without a new key being given. An endpoint needs a model
    unless require_model is False (listing a server's models). Returns
    (entry, key_cleared, warnings); raises AdminError (with .fields) for
    unusable values.
    """
    if not isinstance(data, dict):
        raise AdminError(400, "Send the endpoint settings as an object.")
    unknown = sorted(set(data) - set(ENDPOINT_FIELDS))
    if unknown:
        raise AdminError(400, "Unknown endpoint setting(s): %s." % ", ".join(unknown))
    errors = {}
    entries = settings.endpoint_entries(config)
    wanted = original_name if original_name else data.get("name")
    existing = settings.find_endpoint(config, wanted) if isinstance(wanted, str) else None
    if original_name and existing is None:
        raise AdminError(404, "There is no endpoint called %s." % original_name)
    if not original_name and existing is not None and not replace:
        taken = existing.get("name") or data.get("name")
        raise AdminError(400, "Another endpoint is already called %s." % taken,
                         {"name": "Another endpoint is already called %s; choose another "
                                  "name, or edit that endpoint instead." % taken})
    name = data.get("name", existing.get("name") if existing is not None else "")
    if not isinstance(name, str):
        errors["name"] = "The name must be text."
    else:
        name = name.strip()
        problem = settings.endpoint_name_error(config, name, skip=existing)
        if problem:
            errors["name"] = problem
    working = copy.deepcopy(existing) if existing is not None else {}
    old_url = common.endpoint_settings(existing)["base_url"] if existing is not None else None
    for field in ENDPOINT_TEXT_FIELDS:
        if field not in data:
            continue
        value = data[field] if data[field] is not None else ""
        if not isinstance(value, str):
            errors[field] = "Must be text."
            continue
        settings.set_setting(working, field, value.strip())
    if data.get("preset") and "kind" not in data:
        # A preset brings its own kind (as settings.set_preset does).
        working.pop("kind", None)
    preset = working.get("preset", "")
    if preset and preset not in common.ENDPOINT_PRESETS:
        errors["preset"] = "Unknown preset."
    kind = working.get("kind", "")
    if kind and kind not in common.ENDPOINT_KINDS:
        errors["kind"] = "Choose openai, claude or ollama."
    env_name = working.get("api_key_env", "")
    if env_name and not common.valid_env_name(env_name):
        working.pop("api_key_env", None)
        errors["api_key_env"] = ("That looks like an API key, not the name of an environment "
                                 "variable; type keys in the API key field."
                                 if common.looks_like_key(env_name) else
                                 "Use letters, digits and _ only.")
    if working.get("api_key_file"):
        # Quotes around a pasted path (Explorer's "Copy as path") are not part of it.
        settings.set_setting(working, "api_key_file", common.unquote_path(working["api_key_file"]))
    key_file = working.get("api_key_file", "")
    if key_file and common.looks_like_key(key_file) and _file_check(
            os.path.exists, common.resolve_config_relative(key_file)) is False:
        working.pop("api_key_file", None)
        errors["api_key_file"] = ("That looks like an API key, not the path of a key file; type "
                                  "keys in the API key field.")
    for field in ENDPOINT_NUMBER_FIELDS:
        if field in data:
            value = data[field]
            if value is None or (isinstance(value, str) and not value.strip()):
                working.pop(field, None)
            else:
                number = _endpoint_number(field, value, errors)
                if number is not None:
                    working[field] = number
    if "extra" in data:
        extra = data["extra"]
        if isinstance(extra, str):
            try:
                extra = json.loads(extra) if extra.strip() else {}
            except ValueError as error:
                errors["extra"] = "Not valid JSON: %s" % error
                extra = None
        if extra is not None and not isinstance(extra, dict):
            errors["extra"] = "Extra settings must be a JSON object."
        elif extra is not None:
            if extra:
                working["extra"] = extra
            else:
                working.pop("extra", None)
    if "enabled" in data:
        if isinstance(data["enabled"], bool):
            if data["enabled"]:
                working.pop("enabled", None)
            else:
                working["enabled"] = False
        else:
            errors["enabled"] = "Must be true or false."
    if require_model and not str(working.get("model") or "").strip():
        errors["model"] = MODEL_REQUIRED
    new_key = data.get("api_key")
    if new_key is not None and not isinstance(new_key, str):
        errors["api_key"] = "Must be text."
        new_key = None
    new_key = (new_key or "").strip()
    if data.get("clear_api_key") is True:
        working.pop("api_key", None)
    if new_key:
        working["api_key"] = new_key
    if errors:
        raise AdminError(400, "Some endpoint settings cannot be used.", errors)
    key_cleared = False
    if existing is not None and not new_key:
        key_cleared = settings.forget_key_if_moved(working, old_url)
    if existing is not None:
        old_name = existing.get("name", "")
        working["name"] = old_name
        existing.clear()
        existing.update(working)
        entry = existing
        if name != old_name:
            settings.rename_endpoint(config, old_name, name)
    else:
        entry = working
        entry["name"] = name
        entries.append(entry)
        current = config.get("default_endpoint")
        if not common.resolve_endpoints(config, current if isinstance(current, str) else ""):
            if common.is_local_endpoint(entry) or len(entries) == 1:
                config["default_endpoint"] = entry["name"]
    warnings = []
    if key_cleared:
        warnings.append("The stored API key was removed because the address changed; enter "
                        "it again if this service needs it.")
    if common.insecure_endpoint(entry):
        warnings.append("http:// to a server outside this computer and the local network is "
                        "not encrypted: pictures and the API key cross the network "
                        "unprotected. Use https:// if the service has it.")
    key_file = entry.get("api_key_file", "")
    if key_file:
        full = common.resolve_config_relative(key_file)
        found = _file_check(os.path.isfile, full)
        if found is None:
            warnings.append("The key file's folder did not answer within %g seconds (an "
                            "offline share?); describing needs it." % KEY_FILE_TIMEOUT)
        elif not found:
            warnings.append("The key file does not exist (yet).")
        if (library_ops.is_within(full, common.BASE_DIR)
                and not library_ops.is_within(full, common.data_dir())):
            warnings.append("The key file is inside the program folder, where git could pick "
                            "it up. Keep key files outside it.")
    return entry, key_cleared, warnings


# ---------------------------------------------------------------------------
# Recording library names in index.db

def _register_now(config, quiet=False):
    try:
        conn = db.connect()
        try:
            conn.execute("PRAGMA busy_timeout=%d" % int(REGISTER_WAIT * 1000))
            db.register_libraries(conn, config)
        finally:
            conn.close()
        return True
    except (sqlite3.Error, db.DatabaseError) as error:
        if not quiet:
            print("The library names could not be recorded in the database yet (%s); "
                  "trying again in the background." % error, file=sys.stderr)
        return False


def _register_later():
    for _ in range(REGISTER_TRIES):
        time.sleep(REGISTER_RETRY)
        try:
            config = common.load_config()
        except common.ConfigError:
            return
        if _register_now(config, quiet=True):
            return


def register_libraries(config):
    """Record the configured libraries (ids and names) in index.db without holding up a request.

    A running scan keeps index.db busy, so this waits at most REGISTER_WAIT
    seconds and otherwise tries again in the background (db.init does it
    too at the next start). config.json is already saved at this point, so
    a busy database never turns a saved change into an error. Returns True
    when the names were recorded now.
    """
    if _register_now(config):
        return True
    threading.Thread(target=_register_later, name="register-libraries", daemon=True).start()
    return False


# ---------------------------------------------------------------------------
# The blueprint

def init_app(app, state, manager, allowed_hosts, enabled=True, model_servers=None):
    """Register the admin routes on a Flask app.

    state is serve.ServerState (config, helper), manager a jobs.JobManager
    (not started here), allowed_hosts the host names requests may name.
    enabled is False when the server listens on another address than
    localhost: every admin route then answers 403. model_servers is the
    local_server.ServerManager behind the Model server panel (default: the
    process's own, which the jobs use too).
    """
    model_servers = model_servers or local_server.default_manager()
    from flask import Blueprint, Response, g, jsonify, request
    from werkzeug.exceptions import HTTPException

    bp = Blueprint("admin", __name__, url_prefix=URL_PREFIX)
    checks = library_ops.BackgroundChecks(ttl=FOLDER_STATUS_SECONDS,
                                          timeout=library_ops.PATH_CHECK_TIMEOUT)
    probes = library_ops.BackgroundChecks(ttl=PROBE_SECONDS, timeout=llm.PROBE_TIMEOUT + 5)
    # Key files are read here, not in the request (one may be on an offline share).
    key_files = library_ops.BackgroundChecks(ttl=KEY_FILE_SECONDS,
                                             timeout=library_ops.PATH_CHECK_TIMEOUT)

    def error_response(status, message, fields=None, **extra):
        payload = {"error": common.redact(message),
                   "fields": {key: common.redact(value) for key, value in (fields or {}).items()}}
        payload.update(extra)
        return jsonify(payload), status

    def origin_allowed(origin):
        # The page's own origin only: same host name and port as this request.
        try:
            parts = urllib.parse.urlsplit(origin)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        return (parts.scheme in ("http", "https") and host in allowed_hosts
                and parts.netloc.lower() == request.host.lower())

    @bp.before_request
    def guard():
        if not enabled:
            return error_response(403, "Manage and Settings are turned off because the server "
                                       "listens on an address other computers can reach. Start "
                                       "it without --host to use them.")
        if not is_loopback(request.remote_addr):
            return error_response(403, "Manage and Settings only work on this computer.")
        if request.headers.get(HEADER) != "1":
            return error_response(403, "This request needs the %s header." % HEADER)
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return error_response(403, "Requests from other sites are not allowed.")
        origin = request.headers.get("Origin")
        if origin is not None and not origin_allowed(origin):
            return error_response(403, "Requests from other sites are not allowed.")
        if request.method not in ("GET", "HEAD") and request.mimetype != "application/json":
            return error_response(415, "Send the request body as JSON "
                                       "(Content-Type: application/json).")
        return None

    @bp.after_request
    def no_store(response):
        # (But for an answer asked again by its ETag: people/suggest.)
        if not g.get("by_etag"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @bp.errorhandler(AdminError)
    def admin_error(error):
        return error_response(error.status, str(error), error.fields, **error.extra)

    @bp.errorhandler(library_ops.LibraryError)
    def library_error(error):
        return error_response(error.status, str(error), error.fields)

    @bp.errorhandler(common.ConfigError)
    def config_error(error):
        return error_response(500, "config.json cannot be read: %s" % error)

    @bp.errorhandler(Exception)
    def unexpected(error):
        if isinstance(error, HTTPException):
            return error_response(error.code or 500, error.description or error.name)
        message = common.redact("%s: %s" % (type(error).__name__, error))
        print("Admin request %s failed: %s" % (request.path, message), file=sys.stderr)
        return error_response(500, message)

    # -- helpers --------------------------------------------------------------

    def get_conn():
        conn = g.get("db_conn")
        if conn is None:
            conn = g.db_conn = db.connect()
        return conn

    @bp.teardown_request
    def close_conn(_error):
        # (serve.py closes the same connection when the app context ends;
        # whichever runs first closes it.)
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise AdminError(400, "Send a JSON object.")
        return data

    def token_of(data):
        token = data.get("config_token") if isinstance(data, dict) else None
        token = token or request.headers.get(TOKEN_HEADER)
        return token if isinstance(token, str) and token else None

    def write_config(mutate, token):
        """Re-read config.json, change it with mutate(config), save it: (result, new token).

        Nothing after the save can fail the request: the page must get the
        new token, or its next change would look like a hand edit.
        """
        with _config_write_lock:
            if token is not None and token != config_token():
                raise AdminError(409, "config.json was changed outside this page. Reload to see "
                                      "the current settings.", reload=True)
            config = common.load_config()
            result = mutate(config)
            common.ensure_library_ids(config)
            common.save_config(config)
            new_token = config_token()
        register_libraries(config)
        return result, new_token

    def library_names(config):
        names = db.library_names(get_conn())
        for library in config.get("libraries", []):
            if library.get("id"):
                names[library["id"]] = common.library_label(library)
        return names

    def folder_status(library):
        root = common.library_root(library)
        status = checks.get(("folder", root), library_ops.folder_status, root,
                            timed_out={"reachable": False, "checked_at": None,
                                       "message": "The folder is not responding (an offline "
                                                  "share?)."})
        return status or {"reachable": None, "checked_at": None, "message": "Checking..."}

    def resolve_key(endpoint):
        """(key, source) like common.resolve_api_key, without waiting long on a key file.

        A key file is read in the background; until it has been read the
        source is "file checking", and "file not responding" when that takes
        too long.
        """
        def text(name):
            value = endpoint.get(name)
            return value.strip() if isinstance(value, str) else ""
        file_name = text("api_key_file")
        env_name = text("api_key_env")
        if (not file_name or (common.valid_env_name(env_name)
                              and os.environ.get(env_name, "").strip())):
            return common.resolve_api_key(endpoint)
        found = key_files.get(("key file", common.resolve_config_relative(file_name)),
                              common.resolve_api_key, {"api_key_file": file_name},
                              timed_out=("", "file not responding"), wait=KEY_FILE_WAIT)
        return found if found is not None else ("", "file checking")

    def probe_status(endpoint):
        """llm.probe() of a local endpoint, from the background checks (None until known)."""
        return probes.get(("probe", endpoint["name"], endpoint["base_url"], endpoint["preset"]),
                          llm.probe, endpoint)

    def endpoint_views(config):
        views = []
        for endpoint, raw in zip(common.endpoint_list(config), _raw_endpoints(config)):
            status = probe_status(endpoint) if common.is_local_endpoint(endpoint) else None
            view = endpoint_view(config, endpoint, raw, status, resolve_key(endpoint))
            server = local_server.managed(config, endpoint["name"])
            view["managed"] = server is not None
            if server is not None and view["status"] and \
                    view["status"]["state"] == "not running":
                view["status"]["hint"] = (
                    "Start it under Model server below%s."
                    % (", or let describing start it" if server["auto_start"] else ""))
            views.append(view)
        return views

    def endpoint_info(config, library):
        def status_of(endpoint):
            if not common.is_local_endpoint(endpoint):
                return None
            found = probe_status(endpoint)
            return probe_view(found) if found is not None else None
        info = library_endpoint_info(config, library, resolve_key, status_of)
        if info["cloud"] and not info["local"] and library.get("id"):
            # Hidden pictures still to describe, which these endpoints may not be sent.
            try:
                info["hidden_waiting"] = get_conn().execute(
                    "SELECT COUNT(*) FROM images WHERE library_id = ? AND missing_since IS NULL "
                    "AND hidden = 1 AND described = 0 AND describe_attempts < ?",
                    (library["id"], common.MAX_DESCRIBE_FAILURES)).fetchone()[0]
            except sqlite3.Error:
                pass
        return info

    def snapshot(config, since=0):
        return manager.snapshot(since, library_names(config), config=config)

    def since_arg():
        try:
            return max(0, int(request.args.get("since", 0)))
        except (TypeError, ValueError):
            return 0

    def refine_info(config, library):
        counted = None
        if refine.enabled(library) and library.get("id"):
            # Hidden pictures only count when an endpoint may be sent them.
            local = any(common.is_local_endpoint(endpoint)
                        for endpoint in refine_endpoints(config, library))
            try:
                counted = refine.counts(get_conn(), library, include_hidden=local)
            except sqlite3.Error:
                counted = None
        return refine_view(config, library, counted, key_info=resolve_key)

    def view_of(config, library):
        view = library_view(config, library,
                            db.library_counts(get_conn(), stale_sql=prompts.stale_sql(config)),
                            manager.latest_by_library(), folder_status(library),
                            endpoint_info(config, library), refine_info(config, library),
                            meaning_counts(config).get(library.get("id")))
        view["video_info"] = library_video_view(get_conn(), config, library,
                                                video_counts(get_conn()), videos.proxy_usage(),
                                                drives.preview_usage())
        found = looks_counts(config).get(library.get("id"))
        if found is not None:
            view["looks"] = found
        faces = faces_counts(config).get(library.get("id"))
        if faces is not None:
            view["faces"] = faces
        return view

    faces_memo = {"key": None, "value": {}}

    def faces_counts(config):
        """people.counts() of every library ({} when People is off and no face data is
        kept), worked out again only when index.db, data/faces.db or the settings
        changed."""
        current = people.read_meta()
        options = people.settings(config)
        if not options["enabled"] and current is None:
            return {}
        ids = [library["id"] for library in config.get("libraries", []) if library.get("id")]
        conn = get_conn()
        try:
            key = json.dumps([ids, search_index.data_state(conn, ids), current, options,
                              people.setup_problem(options)], sort_keys=True)
            with meaning_lock:
                if faces_memo["key"] == key:
                    return faces_memo["value"]
            value = people.counts(conn, config, ids)
        except sqlite3.Error:
            return {}
        with meaning_lock:
            faces_memo.update(key=key, value=value)
        return value

    looks_memo = {"key": None, "value": {}}

    def looks_counts(config):
        """looks.counts() of every library ({} when looks are off and none are kept),
        worked out again only when index.db, the looks or their settings changed."""
        current = looks.read_meta()
        if not looks.configured(config) and not (current and current["count"]):
            return {}
        ids = [library["id"] for library in config.get("libraries", []) if library.get("id")]
        conn = get_conn()
        try:
            key = json.dumps([ids, search_index.data_state(conn, ids), current,
                              looks.settings(config), looks.recipe_problem(config)],
                             sort_keys=True)
            with meaning_lock:
                if looks_memo["key"] == key:
                    return looks_memo["value"]
            value = looks.counts(conn, config, ids)
        except sqlite3.Error:
            return {}
        with meaning_lock:
            looks_memo.update(key=key, value=value)
        return value

    meaning_memo = {"key": None, "value": {}}
    meaning_lock = threading.Lock()
    # (The search text hashes /api/vectors keeps up to date, else our own.)
    text_hashes = getattr(getattr(state, "vectors", None), "hashes", None) or \
        embeddings.TextHashes()

    def meaning_counts(config):
        """embeddings.counts() of every library (or {} when search by meaning is off),
        worked out again only when index.db, vectors.db or the settings changed (and
        then only for the pictures that changed: text_hashes)."""
        if not embeddings.configured(config):
            return {}
        ids = [library["id"] for library in config.get("libraries", []) if library.get("id")]
        conn = get_conn()
        try:
            store = embeddings.connect(create=False)
        except sqlite3.Error:
            store = None
        try:
            stored_meta = embeddings.meta(store) if store is not None else None
            key = json.dumps([ids, search_index.data_state(conn, ids), stored_meta,
                              meaning_view(config)], sort_keys=True)
            with meaning_lock:
                if meaning_memo["key"] == key:
                    return meaning_memo["value"]
            value = embeddings.counts(conn, config, ids, store=store, hashes=text_hashes)
        except sqlite3.Error:
            return {}
        finally:
            if store is not None:
                store.close()
        with meaning_lock:
            meaning_memo.update(key=key, value=value)
        return value

    def clean_job(config, kind, library_id, params):
        """Check a job request; returns the params to store (defaults left out)."""
        if kind not in JOB_PARAMS or kind not in manager.runners:
            raise AdminError(400, "Unknown kind of job: %s." % kind, {"kind": "Unknown."})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise AdminError(400, "Job parameters must be an object.")
        unknown = sorted(set(params) - set(JOB_PARAMS[kind]))
        if unknown:
            raise AdminError(400, "Unknown parameter(s) for a %s job: %s."
                             % (kind, ", ".join(unknown)))
        if library_id is not None and not isinstance(library_id, str):
            raise AdminError(400, "library_id must be a library id or null.")
        cleaned = {}
        for name in BOOL_PARAMS:
            if name in params:
                if not isinstance(params[name], bool):
                    raise AdminError(400, "%s must be true or false." % name, {name: "Must be "
                                                                             "true or false."})
                if params[name]:
                    cleaned[name] = True
        if params.get("limit") is not None:
            limit = params["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise AdminError(400, "limit must be a whole number of at least 0.",
                                 {"limit": "Enter a whole number."})
            cleaned["limit"] = limit
        if params.get("endpoint"):
            name = params["endpoint"]
            if not isinstance(name, str) or not common.resolve_endpoints(config, name):
                raise AdminError(400, "No enabled endpoint or group is called %s." % name,
                                 {"endpoint": "Unknown endpoint."})
            cleaned["endpoint"] = name.strip()
        if kind == "export" and not library_id and not params.get("trip"):
            raise AdminError(400, "An export is made of one library (or a trip); choose which.",
                             {"library_id": "Choose a library."})
        if kind == "export" and library_id and params.get("trip"):
            raise AdminError(400, "A trip's export takes its pictures from its own libraries: "
                                  "send no library with it.", {"library_id": "Leave it out."})
        if kind == "delete_library":
            check_orphan_delete(config, library_id, params.get("confirm_name"))
            cleaned["confirm_name"] = db.library_names(get_conn()).get(library_id) or library_id
        elif library_id is not None:
            library = find_library(config, library_id)
            if not library.get("enabled", True):
                raise AdminError(400, "Library %s is disabled; enable it first."
                                 % common.library_label(library))
        if params.get("ids") is not None:
            cleaned["ids"] = describable_ids(config, library_id,
                                             image_ids(params["ids"], DESCRIBE_IDS_LIMIT))
        if kind == "refine":
            check_refine(config, library_id)
        if kind == "proxies" and not videos.proxy_settings(config)["enabled"]:
            raise AdminError(400, "Playable copies of videos are turned off: set "
                                  "\"video_proxies\": {\"enabled\": true} in config.json first.")
        if kind == "drive_previews" and not drives.preview_settings(config)["enabled"]:
            raise AdminError(400, "Sped-up previews of drives are turned off: set "
                                  "\"drive_previews\": {\"enabled\": true} in config.json first.")
        if kind == "embed":
            endpoint, problem = embeddings.endpoint_for(config)
            if endpoint is None:
                raise AdminError(400, problem, {"embedding": problem})
        if kind == "image_embed":
            endpoint, problem = looks.endpoint_for(config)
            if endpoint is None:
                raise AdminError(400, problem, {"image_embedding": problem})
        if kind == "faces":
            problem = people.setup_problem(people.settings(config))
            if problem:
                raise AdminError(400, problem, {"people": problem})
        if kind == "group_faces":
            # (The faces of every library are grouped together.)
            if library_id is not None:
                raise AdminError(400, "Faces are grouped for every library at once (send no "
                                      "library).", {"library": "None."})
            if not people.enabled(config):
                raise AdminError(400, people.NOT_ENABLED, {"people": people.NOT_ENABLED})
        if kind == "delete_faces":
            # (All face data goes, of every library: only when asked for in so many words.)
            if library_id is not None or params.get("confirm") is not True:
                raise AdminError(400, "Deleting the face data needs confirm: true (and no "
                                      "library: it is all deleted).", {"confirm": "Needed."})
        if kind == "trip_stories":
            # (A trip's pictures come from its own libraries: no library.)
            if library_id is not None:
                raise AdminError(400, "Trip stories are written for a trip or for every "
                                      "trip (send no library).", {"library": "None."})
            if params.get("trip") is not None:
                _built, trip, _libraries = load_trip(config, params.get("trip"))
                cleaned.update(trip=trip.id, trip_title=trip.title)
        if cleaned.get("force") and "ids" not in cleaned:
            # Describing a whole library again is "Re-describe stale" (or a
            # new prompt version), never a single click.
            raise AdminError(400, "force only works with ids: the pictures to describe again.",
                             {"force": "Needs ids."})
        return cleaned

    def export_target(config, library, out_dir, default=None):
        """(output folder, default folder) of an export: out_dir when given, else the default
        (a library's, or the one given)."""
        if default is None:
            default = export.default_output_dir(config, library)
        if out_dir is None or out_dir == "":
            return default, default
        full = library_ops.expand_path(out_dir) if isinstance(out_dir, str) else ""
        if (not library_ops.is_full_path(full) or library_ops.is_device_path(out_dir)
                or library_ops.unc_without_share(full)):
            problem = "Type the whole path of a folder, such as %s." % library_ops.FULL_PATH_EXAMPLES
            raise AdminError(400, problem, {"out_dir": problem})
        return os.path.abspath(full), default

    def check_target(config, target, owner, zip_too):
        """Raise AdminError unless an export (of owner) may write to target, and in time."""
        def check():
            export.check_output_dir(target, config, owner)
            export.check_writable(target)
            if zip_too:
                export.check_zip_path(export.zip_path_for(target), config)
        try:
            library_ops.run_with_timeout(check, EXPORT_FOLDER_TIMEOUT)
        except library_ops.TimedOut:
            raise AdminError(504, "The output folder did not answer within %g seconds (an "
                                  "offline share?)." % EXPORT_FOLDER_TIMEOUT,
                             {"out_dir": "Does not answer."})
        except export.ExportError as error:
            raise AdminError(400, str(error), {"out_dir": str(error)})
        except OSError as error:
            message = export.os_error_text(error, target)
            raise AdminError(400, message, {"out_dir": message})

    def load_trip(config, trip_id):
        """trip_export.load of the enabled libraries' trips; AdminError 404 when there is no
        such trip."""
        if not isinstance(trip_id, str) or not trips.ID_RE.match(trip_id):
            raise AdminError(404, "There is no trip %s." % (trip_id,), {"trip": "Unknown."})
        try:
            return trip_export.load(get_conn(), config, trip_id)
        except export.ExportError as error:
            raise AdminError(404, str(error), {"trip": "Unknown."})

    def trip_export_job(config, params, save):
        """export_job of a trip (params "trip"): (params to store, with the trip's title for
        the job list, and the options to save or None)."""
        built, trip, _libraries = load_trip(config, params.get("trip"))
        options = params.get("options")
        if options is not None and not isinstance(options, dict):
            raise AdminError(400, "Send the export options as an object.",
                             {"options": "Must be an object."})
        try:
            merged = trip_export.trip_options(built.export_options(trip), options)
        except export.OptionsError as error:
            raise options_error(error)
        cleaned = {"trip": trip.id, "trip_title": trip.title}
        if options and not save:
            cleaned["options"] = {key: merged[key] for key in options}
        target, default = export_target(config, None, params.get("out_dir"),
                                        trip_export.default_output_dir(config, built, trip))
        if os.path.normcase(target) != os.path.normcase(os.path.abspath(default)):
            cleaned["out_dir"] = target
        check_target(config, target, trip_export.owner_key(built, trip), merged["zip"])
        return cleaned, ((options or {}) if save else None)

    def export_job(config, library_id, params, save):
        """Check an export job's params: (params to store, options to save or None).

        options override the library's saved export options; with save
        they become the saved ones (and the job uses what is saved when it
        starts). out_dir is the output folder (default: the library's
        folder in data/exports), which must be one an export of this
        library may write to (export.check_output_dir) and where files can
        be made (export.check_writable).
        """
        library = find_library(config, library_id)
        options = params.get("options")
        if options is not None and not isinstance(options, dict):
            raise AdminError(400, "Send the export options as an object.",
                             {"options": "Must be an object."})
        try:
            merged = export.library_options(library, options)
        except export.OptionsError as error:
            raise options_error(error)
        cleaned = {}
        if options and not save:
            cleaned["options"] = {key: merged[key] for key in options}
        target, default = export_target(config, library, params.get("out_dir"))
        if os.path.normcase(target) != os.path.normcase(os.path.abspath(default)):
            cleaned["out_dir"] = target
        check_target(config, target, library["id"], merged["zip"])
        return cleaned, ((options or {}) if save else None)

    def free_space(folder):
        """Free bytes on the disk of folder (or of the nearest folder above it), or None."""
        def look():
            path = folder
            while not os.path.isdir(path):
                parent = os.path.dirname(path)
                if parent == path:
                    return None
                path = parent
            return shutil.disk_usage(path).free
        try:
            return library_ops.run_with_timeout(look, EXPORT_FOLDER_TIMEOUT)
        except (library_ops.TimedOut, OSError):
            return None

    def describable_ids(config, library_id, ids):
        """The ids (sorted) of present pictures in enabled libraries (library_id's, when given)."""
        allowed = {library["id"] for library in common.enabled_libraries(config)
                   if library.get("id") and library_id in (None, library["id"])}
        found = []
        conn = get_conn()
        for part in id_chunks(ids):
            found.extend(row["id"] for row in conn.execute(
                "SELECT id, library_id FROM images WHERE missing_since IS NULL AND id IN (%s)"
                % ",".join("?" * len(part)), part) if row["library_id"] in allowed)
        if not found:
            raise AdminError(404, "None of these pictures can be described: they are gone, or "
                                  "their library is disabled or not set up.",
                             {"ids": "No such pictures."})
        return sorted(found)

    def check_refine(config, library_id):
        """A refine job needs a library with a refine endpoint (any enabled one, for all)."""
        if library_id is not None:
            library = find_library(config, library_id)
            if not refine.enabled(library):
                raise AdminError(400, "Library %s has no refine endpoint: choose one in its "
                                      "settings first." % common.library_label(library),
                                 {"refine": "Choose a refine endpoint."})
        elif not any(refine.enabled(library) for library in common.enabled_libraries(config)):
            raise AdminError(400, "No library has a refine endpoint: choose one in a "
                                  "library's settings first.", {"refine": "None set."})

    def check_orphan_delete(config, library_id, confirm):
        conn = get_conn()
        if not library_id or library_id not in db.orphan_library_ids(conn, config):
            raise AdminError(404, "There is no unattached indexed data with that id.")
        stored = db.library_names(conn).get(library_id) or library_id
        if not library_ops.names_match(confirm, stored):
            raise AdminError(400, "Type the name (%s) to confirm." % stored,
                             {"confirm_name": "Does not match."})
        return stored

    # -- overview -------------------------------------------------------------

    @bp.route("/state")
    def api_state():
        config = state.config()
        conn = get_conn()
        counts = db.library_counts(conn, stale_sql=prompts.stale_sql(config))
        latest = manager.latest_by_library()
        meaning = meaning_counts(config)
        libraries = [library_view(config, library, counts, latest, folder_status(library),
                                  endpoint_info(config, library), refine_info(config, library),
                                  meaning.get(library["id"]))
                     for library in config.get("libraries", []) if library.get("id")]
        with_videos, usage = video_counts(conn), videos.proxy_usage()
        drive_usage = drives.preview_usage()
        looked = looks_counts(config)
        faced = faces_counts(config)
        for view, library in zip(libraries, [library for library in config.get("libraries", [])
                                             if library.get("id")]):
            view["video_info"] = library_video_view(conn, config, library, with_videos, usage,
                                                    drive_usage)
            if library["id"] in looked:
                view["looks"] = looked[library["id"]]
            if library["id"] in faced:
                view["faces"] = faced[library["id"]]
        return jsonify({
            "libraries": libraries,
            "videos": videos_view(config),
            "meaning": meaning_view(config),
            "looks": looks.status(config),
            "people": people.status(config),
            "orphans": library_ops.orphans(conn, config, counts),
            "endpoints": endpoint_views(config),
            "default_endpoint": common.default_endpoint_name(config),
            "presets": preset_views(),
            "jobs": snapshot(config, since_arg()),
            "config_token": config_token(),
            "version": {"schema": db.SCHEMA_VERSION, "prompt": prompts.PROMPT_VER},
            "refine_choices": refine_choices(),
        })

    # -- libraries ------------------------------------------------------------

    def library_fields(data):
        return {key: value for key, value in data.items() if key != "config_token"}

    @bp.route("/libraries", methods=["POST"])
    def add_library():
        data = body()
        fields = library_fields(data)

        def mutate(config):
            changes, warnings = library_ops.clean_library_fields(config, fields)
            library = common.apply_library_defaults(changes)
            config.setdefault("libraries", []).append(library)
            common.ensure_library_ids(config)
            return config, library, warnings

        (config, library, warnings), token = write_config(mutate, token_of(data))
        warning = library_ops.reachability_warning(library["path"], REACHABILITY_TIMEOUT)
        if warning:
            warnings.append(warning)
        checks.forget(("folder", common.library_root(library)))
        return jsonify({"library": view_of(config, library), "warnings": warnings,
                        "config_token": token}), 201

    @bp.route("/libraries/<library_id>", methods=["POST"])
    def edit_library(library_id):
        data = body()
        fields = library_fields(data)

        def mutate(config):
            library = find_library(config, library_id)
            old_root = common.library_root(library)
            changes, warnings = library_ops.clean_library_fields(config, fields, library)
            library.update(changes)
            return config, library, warnings, old_root

        (config, library, warnings, old_root), token = write_config(mutate, token_of(data))
        root = common.library_root(library)
        if root != old_root:
            warning = library_ops.reachability_warning(library["path"], REACHABILITY_TIMEOUT)
            if warning:
                warnings.append(warning)
            checks.forget(("folder", root))
        return jsonify({"library": view_of(config, library), "warnings": warnings,
                        "config_token": token})

    @bp.route("/libraries/<library_id>/remove", methods=["POST"])
    def remove_library(library_id):
        data = body()
        keep = data.get("keep_data")
        if not isinstance(keep, bool):
            raise AdminError(400, "Choose whether to keep the indexed data.",
                             {"keep_data": "Choose one."})

        def mutate(config):
            library = find_library(config, library_id)
            if not library_ops.names_match(data.get("confirm_name"),
                                           common.library_label(library)):
                raise AdminError(400, "Type the library's name to confirm.",
                                 {"confirm_name": "Does not match."})
            # The name stored in the database is what a later delete checks
            # (best effort: a scan may keep the database busy).
            _register_now(config, quiet=True)
            config["libraries"].remove(library)
            return library

        library, token = write_config(mutate, token_of(data))
        # The library is gone from config.json now; what follows may fail
        # without undoing that, so the answer always carries the new token.
        warnings = []
        job = None
        try:
            manager.cancel_library_jobs(library_id)
        except Exception as error:
            warnings.append("Its waiting jobs could not be cancelled (%s); they fail when they "
                            "start." % common.redact(error))
        if not keep:
            try:
                stored = db.library_names(get_conn()).get(library_id) or library_id
                job, _added = manager.enqueue("delete_library", library_id,
                                              {"confirm_name": stored})
            except Exception as error:
                warnings.append("Deleting its indexed data could not be started (%s). The data "
                                "is kept as unattached data, which can be deleted on this page."
                                % common.redact(error))
        return jsonify({"removed": library_id, "kept_data": keep, "job": job,
                        "warnings": warnings, "config_token": token})

    @bp.route("/orphans/<orphan_id>/reattach", methods=["POST"])
    def reattach_orphan(orphan_id):
        data = body()
        target = data.get("library_id")
        if not isinstance(target, str) or not target:
            raise AdminError(400, "Choose the library to re-attach to.",
                             {"library_id": "Choose a library."})
        # Rows must not move under a running scan.
        lock = jobs.WorkerLock()
        if not lock.acquire("re-attaching indexed data"):
            raise AdminError(409, "Wait until the running job has finished (%s)."
                             % (lock.holder() or "another process"))
        try:
            result = library_ops.reattach_orphan(get_conn(), orphan_id, target,
                                                 common.load_config())
        finally:
            lock.release()
        return jsonify(dict(result, orphan=orphan_id, library_id=target))

    @bp.route("/orphans/<orphan_id>/delete", methods=["POST"])
    def delete_orphan(orphan_id):
        data = body()
        config = common.load_config()
        stored = check_orphan_delete(config, orphan_id, data.get("confirm_name"))
        job, added = manager.enqueue("delete_library", orphan_id, {"confirm_name": stored})
        return jsonify({"job": job, "added": added}), 202

    # -- folders --------------------------------------------------------------

    def text_field(data, name, required=True):
        value = data.get(name, "" if not required else None)
        if not isinstance(value, str) or (required and not value.strip()):
            raise AdminError(400, "%s is required." % name, {name: "Required."})
        return value

    @bp.route("/paths/check", methods=["POST"])
    def check_path():
        path = text_field(body(), "path")
        result = library_ops.check_path(path)
        # The name a new library in this folder gets when none is typed.
        result["name"] = common.default_library_name(result.get("path") or path)
        return jsonify(result)

    @bp.route("/fs/list", methods=["POST"])
    def list_folder():
        return jsonify(library_ops.list_folder(text_field(body(), "path", required=False)))

    @bp.route("/ignore-preview", methods=["POST"])
    def ignore_preview():
        data = body()
        library = None
        if data.get("library_id"):
            library = find_library(state.config(), data["library_id"])
            root = common.library_root(library)
        else:
            root = text_field(data, "path")
        problem = library_ops.path_error(root)
        if problem:
            raise AdminError(400, problem, {"path": problem})
        patterns = library_ops.parse_ignore(
            data.get("ignore", library.get("ignore", []) if library is not None else []))
        if patterns is None:
            raise AdminError(400, "Ignore patterns must be text, one pattern per line.",
                             {"ignore": "Must be text."})
        defaults = data.get("default_ignores",
                            library.get("default_ignores", True) if library is not None
                            else True)
        if not isinstance(defaults, bool):
            raise AdminError(400, "default_ignores must be true or false.")
        if defaults:
            patterns = patterns + list(common.DEFAULT_IGNORES)
        return jsonify(library_ops.ignore_preview(root, patterns))

    # -- jobs -----------------------------------------------------------------

    @bp.route("/jobs", methods=["GET"])
    def jobs_snapshot():
        result = snapshot(state.config(), since_arg())
        if request.args.get("log") == "0":
            # The pages' job indicator only needs the jobs, not the log lines.
            result["log"], result["errors"] = [], []
        return jsonify(result)

    @bp.route("/jobs", methods=["POST"])
    def add_job():
        data = body()
        config = state.config()
        kind = data.get("kind")
        library_id = data.get("library_id") or None
        params = clean_job(config, kind, library_id, data.get("params"))
        answer = {}
        if kind == "export" and (data.get("params") or {}).get("trip"):
            params, save = trip_export_job(config, data.get("params") or {},
                                           params.pop("save_options", False))
            if save is not None:
                try:
                    trip_export.save_options(get_conn(), config, params["trip"], save)
                except export.OptionsError as error:
                    raise options_error(error)
                except (export.ExportError, trips.TripError, OSError) as error:
                    raise AdminError(400, "The trip's export options cannot be saved: %s"
                                     % error)
        elif kind == "export":
            params, save = export_job(config, library_id, data.get("params") or {},
                                      params.pop("save_options", False))
            if save is not None:
                def mutate(fresh):
                    library = find_library(fresh, library_id)
                    try:
                        library["export"] = export.options_to_save(
                            export.library_options(library, save))
                    except export.OptionsError as error:
                        raise options_error(error)
                    return fresh
                config, answer["config_token"] = write_config(mutate, token_of(data))
        job, added = manager.enqueue(kind, library_id, params)
        if kind == "delete_faces":
            forget_undo()       # (the names an undo kept go with the face data)
            people_suggest.forget()     # (and those the suggestions keep)
        answer.update(job=job, added=added, jobs=snapshot(config))
        return jsonify(answer), 202

    @bp.route("/jobs/<int:job_id>/<action>", methods=["POST"])
    def job_action(job_id, action):
        body()
        if action == "pause":
            done = manager.pause(job_id)
            message = "That job is not running."
        elif action == "resume":
            done = manager.resume(job_id) is not None
            message = "That job is neither paused nor interrupted."
        elif action == "cancel":
            done = manager.cancel(job_id)
            message = "That job has already finished."
        else:
            raise AdminError(404, "Unknown action: %s." % action)
        if not done:
            job = manager.get(job_id)
            if job is None:
                raise AdminError(404, "There is no job %d." % job_id)
            if job["state"] in jobs.ACTIVE_STATES and not manager.owns(job_id):
                raise AdminError(409, "This job is run by another server that uses the same "
                                      "data folder; pause or cancel it on that server's Manage "
                                      "page.")
            raise AdminError(409, message)
        return jsonify({"ok": True, "jobs": snapshot(state.config())})

    @bp.route("/people/check", methods=["POST"])
    def people_check():
        """Start the face helper, load its models and find the test face (people.check)."""
        body()
        return jsonify(people.check(state.config()))

    # -- People: the people the faces show (people_groups.py) -------------------

    def people_libraries(config):
        return [library["id"] for library in common.enabled_libraries(config)
                if library.get("id")]

    def with_people(work, write=False):
        """work(store) with data/faces.db open; GroupError and a busy store answered. A
        change needs the store (404 without one)."""
        exists = os.path.isfile(people.store_path())
        if write and not exists:
            raise AdminError(404, "There is no face data yet: find faces first (Manage).")
        try:
            # (A change brings an older file up to date; a look only reads.)
            store = people.connect(create=write) if exists else None
        except sqlite3.Error as error:
            raise AdminError(500, "The face data cannot be read: %s" % common.redact(str(error)))
        if store is None and write:
            raise AdminError(404, "There is no face data yet: find faces first (Manage).")
        try:
            return work(store)
        except people_groups.GroupError as error:
            raise AdminError(error.status, str(error), error.fields, **error.extra)
        except sqlite3.OperationalError as error:
            raise busy(error)
        finally:
            if store is not None:
                store.close()

    def person_id_of(value, name="person"):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= IMAGE_ID_MAX:
            raise AdminError(400, "%s is a person's id (a number)." % name,
                             {name: "A person's id."})
        return value

    @bp.route("/people", methods=["GET"])
    def people_state():
        """People's state for the People page: people.status() (finding faces), "groups":
        people_groups.status() (the grouping) and the settings that group them."""
        config = state.config()
        options = people.settings(config)
        return jsonify({
            "people": people.status(config),
            "groups": with_people(lambda store: people_groups.status(store, config)),
            "settings": {"match": options["match"], "group_quality": options["group_quality"],
                         "min_faces": options["min_faces"], "visitors": options["visitors"]}})

    @bp.route("/people/<int:person_id>", methods=["GET"])
    def person_detail(person_id):
        """One person: its faces (best first, each with a picture it is in) and the people
        whose faces look most like its own (people_groups.person_view)."""
        config = state.config()
        return jsonify(with_people(lambda store: people_groups.person_view(
            store, get_conn(), config, person_id, people_libraries(config)), write=True))

    # The last change made with "undo": true (the viewer's, when a face is named there):
    # {"token", "snap"} (people_groups.snapshot), until it is undone, another replaces it,
    # an undo of it is refused, or the face data is deleted. Kept in memory only: a
    # server started again has none.
    undo_lock = threading.Lock()
    last_undo = {}

    def undo_token(store, snap, made=()):
        """The token that undoes the change snap was taken before (POST /people/undo), or
        None when it cannot be undone (another change came in between, too many faces)."""
        if not people_groups.settle_snapshot(store, snap, made):
            return None
        token = os.urandom(12).hex()
        with undo_lock:
            last_undo.clear()
            last_undo.update({"token": token, "snap": snap})
        return token

    def forget_undo(token=None):
        """No undo any more (of token's change only, when given): its snapshot -- names
        among it -- is let go."""
        with undo_lock:
            if token is None or last_undo.get("token") == token:
                last_undo.clear()

    @bp.route("/people/<int:person_id>", methods=["POST"])
    def person_change(person_id):
        """{name?, hidden?, cover?, face?, undo?}: name a person ("" takes its name away),
        hide it, or choose the face that stands for it (null: the best one). face (the
        viewer's, naming a person by one of its faces): refused (409) when that face is
        not in this person (any more). With "undo": true the answer's "undo" is a token
        that undoes it (POST /people/undo)."""
        changes = body()
        face = changes.get("face")
        if face is not None and (isinstance(face, bool) or not isinstance(face, int)
                                 or not 0 < face <= IMAGE_ID_MAX):
            raise AdminError(400, "face is a face's id (a number).", {"face": "A face's id."})

        def change(store):
            if face is not None and people_groups.face_person(store, face) != person_id:
                raise AdminError(409, "That face is not in this person any more (a grouping "
                                      "or another change moved it): look at it again.")
            return {"person": people_groups.update_person(
                store, person_id, {key: changes[key] for key in ("name", "hidden", "cover")
                                   if key in changes})}

        def work(store):
            if changes.get("undo") is not True:
                return change(store)
            reply, snap = people_groups.undoable(store, (), [person_id],
                                                 lambda: change(store))
            reply["undo"] = undo_token(store, snap)
            return reply
        return jsonify(with_people(work, write=True))

    @bp.route("/people/<int:person_id>/merge", methods=["POST"])
    def person_merge(person_id):
        """{into: person id, face?, undo?}: every face of this person goes to the other
        one. face (the viewer's "Name Person 12 as X", asked about one of its faces):
        refused (409) when that face is not in this person (any more). With "undo": true
        the answer's "undo" is a token that undoes it (POST /people/undo; null when it
        cannot be: the two hold more than people_groups.UNDO_MERGE_FACES faces)."""
        data = body()
        target = person_id_of(data.get("into"), "into")
        face = data.get("face")
        if face is not None and (isinstance(face, bool) or not isinstance(face, int)
                                 or not 0 < face <= IMAGE_ID_MAX):
            raise AdminError(400, "face is a face's id (a number).", {"face": "A face's id."})
        quality = people.settings(state.config())["group_quality"]

        def work(store):
            def change():
                if face is not None and people_groups.face_person(store, face) != person_id:
                    raise AdminError(409, "That face is not in this person any more (a "
                                          "grouping or another change moved it): look at it "
                                          "again.")
                return {"person": people_groups.merge(store, person_id, target,
                                                      group_quality=quality)}
            if data.get("undo") is not True:
                return change()
            # (Every face of both: a merge makes the clear ones the owner's.)
            reply, snap = people_groups.undoable(
                store, lambda: (people_groups.person_faces(store, person_id) +
                                people_groups.person_faces(store, target)),
                [person_id, target], change, limit=people_groups.UNDO_MERGE_FACES)
            reply["undo"] = undo_token(store, snap) if snap is not None else None
            return reply
        return jsonify(with_people(work, write=True))

    @bp.route("/people/suggest", methods=["GET"])
    def people_suggestions():
        """?face=<id>: the named people that face most likely is, for the viewer's naming
        popover (people_suggest.suggest; a face of an unnamed person: what that person
        most likely is). The browser keeps the answer and asks again by its ETag (the
        face data's state, the settings): 304 while the people are as they were."""
        try:
            face_id = int(request.args.get("face", ""))
        except ValueError:
            face_id = 0
        if not 0 < face_id <= IMAGE_ID_MAX:
            raise AdminError(400, "face is a face's id (a number).", {"face": "A face's id."})
        config = state.config()

        def work(store):
            if store is None:
                people_suggest.forget()     # (no face data: no names kept either)
                return None, None
            tag = people_suggest.etag(store, config, face_id)
            if request.if_none_match.contains(tag):
                return tag, None
            return tag, people_suggest.suggest(store, face_id, config)
        tag, answer = with_people(work)
        if tag is None:
            return jsonify({"face": face_id, "basis": "missing", "people": [],
                            "preselect": None})
        response = jsonify(answer) if answer is not None else Response(status=304)
        response.set_etag(tag)
        response.headers["Cache-Control"] = "private, no-cache"
        g.by_etag = True
        return response

    @bp.route("/people/faces", methods=["POST"])
    def people_faces():
        """{action, faces (face ids, or "all" with person), person?, to?, name?, undo?}:
        "move" the faces to person "to" (null: a new person called name), "reject" them
        from person ("not this person"), "not_face" ("not a face") or "is_face" (a face
        after all). With person, the answer's "source" is that person now (null when it
        went: its last faces left). With "undo": true (the viewer's) the answer's "undo"
        is a token that undoes it (POST /people/undo; null when it cannot be)."""
        data = body()
        action = data.get("action")
        if action not in ("move", "reject", "not_face", "is_face"):
            raise AdminError(400, "action is move, reject, not_face or is_face.",
                             {"action": "move, reject, not_face or is_face."})
        person = data.get("person")
        if person is not None:
            person = person_id_of(person)

        def work(store):
            faces = data.get("faces")
            if faces == "all":
                if person is None:
                    raise AdminError(400, "\"all\" faces needs the person.",
                                     {"person": "Needed."})
                faces = people_groups.person_faces(store, person)
                if not faces:
                    raise AdminError(404, "That person has no faces (any more).")
            to = data.get("to") if action == "move" else None
            if to is not None:
                to = person_id_of(to, "to")
            if action == "reject" and person is None:
                raise AdminError(400, "Say which person they are not (person).",
                                 {"person": "Needed."})

            def change():
                if action == "move":
                    return {"person": people_groups.move_faces(store, faces, to,
                                                               data.get("name") or "")}
                if action == "reject":
                    return people_groups.reject_faces(store, faces, person)
                return {"changed": people_groups.mark_not_face(store, faces,
                                                               action == "not_face")}

            if data.get("undo") is True:
                # (The snapshot and the change in one write: see people_groups.undoable.)
                reply, snap = people_groups.undoable(
                    store, faces if isinstance(faces, list) else (),
                    [value for value in (person, to) if value is not None], change)
                made = [reply["person"]["id"]] if action == "move" and to is None else []
                reply["undo"] = undo_token(store, snap, made) if snap is not None else None
            else:
                reply = change()
            if person is not None:
                # (The person the faces came from, or null when it went with them.)
                try:
                    reply["source"] = people_groups.person_info(store, person)
                except people_groups.GroupError:
                    reply["source"] = None
            return reply
        return jsonify(with_people(work, write=True))

    @bp.route("/people/undo", methods=["POST"])
    def people_undo():
        """{undo: the token of the last change made with "undo": true}: the people as
        they were before it (people_groups.restore). Only that change, once, only while
        nothing else changed the people since, and not after the server was started
        again (it keeps the change in memory): 409 else."""
        token = body().get("undo")
        with undo_lock:
            entry = dict(last_undo)
        if not isinstance(token, str) or not entry or token != entry["token"]:
            raise AdminError(409, "That change cannot be undone here any more (only the last "
                                  "change made in the viewer can be, once, and not after the "
                                  "server was started again). Put it right on the People "
                                  "page.")

        def work(store):
            try:
                people_groups.restore(store, entry["snap"])
            except people_groups.GroupError:
                forget_undo(token)      # (refused once, it is refused for good)
                raise
            forget_undo(token)
            return {"undone": True}
        return jsonify(with_people(work, write=True))

    @bp.route("/people/other", methods=["GET"])
    def people_other():
        """?which=other (faces in no person, best first; the default) or not_face (marked
        "not a face"), &offset=: one page of them."""
        config = state.config()
        which = request.args.get("which", "other")
        if which not in ("other", "not_face"):
            raise AdminError(400, "which is other or not_face.", {"which": "other or not_face."})
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            raise AdminError(400, "offset is a number.", {"offset": "A number."})
        answer = with_people(lambda store: people_groups.other_faces(
            store, get_conn(), people_libraries(config), which, offset) if store is not None
            else {"faces": [], "total": 0, "offset": 0})
        return jsonify(answer)

    @bp.route("/abort-other", methods=["POST"])
    def abort_other():
        body()
        return jsonify(manager.abort_other())

    @bp.route("/failures", methods=["GET"])
    def failures():
        library_id = request.args.get("library") or None
        try:
            limit = int(request.args.get("limit", FAILURE_LIMIT))
        except (TypeError, ValueError):
            limit = FAILURE_LIMIT
        limit = max(1, min(FAILURE_LIMIT_MAX, limit))
        sql = ("SELECT id, library_id, path, describe_attempts, describe_error FROM images "
               "WHERE missing_since IS NULL AND described = 0 AND describe_attempts > 0")
        args = []
        if library_id:
            sql += " AND library_id = ?"
            args.append(library_id)
        sql += " ORDER BY describe_attempts DESC, id LIMIT ?"
        args.append(limit)
        rows = get_conn().execute(sql, args).fetchall()
        return jsonify([{"id": row["id"], "library_id": row["library_id"], "path": row["path"],
                         "attempts": row["describe_attempts"],
                         "error": common.redact(row["describe_error"])} for row in rows])

    @bp.route("/failures/retry", methods=["POST"])
    def retry_failures():
        data = body()
        config = state.config()
        library_id = data.get("library_id") or None
        params = clean_job(config, "describe", library_id, {"retry_failed": True})
        job, added = manager.enqueue("describe", library_id, params)
        return jsonify({"job": job, "added": added}), 202

    # -- place names (places.py) --------------------------------------------

    def places_view():
        """places.status() with the places job that is waiting or running ("job") and
        the last one that finished ("last")."""
        import places
        # (From the latest log line on: the jobs without their log. Looked
        # at before the pictures, so a job that ends in between is not
        # missed: its counts are then the new ones.)
        jobs_now = manager.snapshot(manager.buffer.latest)
        result = places.status(get_conn())
        waiting = [job for job in [jobs_now["current"]] + jobs_now["queue"]
                   if job is not None and job["kind"] == "places"]
        finished = [job for job in jobs_now["recent"] if job["kind"] == "places"]
        result["job"] = waiting[0] if waiting else None
        result["last"] = finished[0] if finished else None
        return result

    @bp.route("/places", methods=["GET"])
    def places_state():
        return jsonify(places_view())

    @bp.route("/places/<action>", methods=["POST"])
    def places_action(action):
        """download: fetch the place names (GeoNames) and name every located picture's place;
        label: name the places of the pictures not looked up yet (redo: of all of them)."""
        data = body()
        redo = data.get("redo", False)
        if not isinstance(redo, bool):
            raise AdminError(400, "redo must be true or false.", {"redo": "Must be true or "
                                                                       "false."})
        if action == "download":
            params = {"download": True}
        elif action == "label":
            import places
            known = places.status()
            if not known["available"] and not known["rebuild"]:
                raise AdminError(409, "There are no place names yet: download them first.")
            params = {"redo": True} if redo else {}
        else:
            raise AdminError(404, "Unknown action: %s." % action)
        job, added = manager.enqueue("places", None, params)
        return jsonify({"job": job, "added": added, "places": places_view()}), 202

    # -- street maps (street_maps.py) -----------------------------------------

    def maps_view(full=False):
        """street_maps.status() (full: with how many places of pictures have no street
        map) with the maps job that is waiting or running ("job") and the last one that
        finished ("last")."""
        import street_maps
        jobs_now = manager.snapshot(manager.buffer.latest)
        config = state.config()
        result = street_maps.status(get_conn() if full else None, config)
        waiting = [job for job in [jobs_now["current"]] + jobs_now["queue"]
                   if job is not None and job["kind"] == "maps"]
        finished = [job for job in jobs_now["recent"] if job["kind"] == "maps"]
        result["job"] = waiting[0] if waiting else None
        result["last"] = finished[0] if finished else None
        # The named places found on them (named_places.py), with their job.
        named = named_places.status(get_conn(), config)
        named["job"] = next((job for job in [jobs_now["current"]] + jobs_now["queue"]
                             if job is not None and job["kind"] == "named_places"), None)
        named["last"] = next((job for job in jobs_now["recent"]
                              if job["kind"] == "named_places"), None)
        result["named"] = named
        return result

    @bp.route("/maps", methods=["GET"])
    def maps_state():
        return jsonify(maps_view(request.args.get("full") == "1"))

    @bp.route("/maps/<action>", methods=["POST"])
    def maps_action(action):
        """plan: check the size of a download of the street maps (a job; it reads the
        map's directories); download {expect_bytes, source, allow_large}: download them
        as the last size check said (a job; one of street_maps.LARGE_BYTES or more only
        with allow_large: true); remove: delete them (not while a job, or a command
        line tool, holds the job lock: a download may be running); named {redo}: find
        the named places on them (a "named_places" job: the pictures whose position
        changed, or with redo: true every picture again)."""
        import street_maps
        data = body()
        if action == "named":
            if not named_places.maps_key():
                raise AdminError(409, "There are no street maps yet: download them first.")
            if not isinstance(data.get("redo", False), bool):
                raise AdminError(400, "redo must be true or false.")
            job, added = manager.enqueue("named_places", None,
                                         {"redo": True} if data.get("redo") else {})
            return jsonify({"job": job, "added": added, "maps": maps_view()}), 202
        if action == "plan":
            params = {}
        elif action == "download":
            last = street_maps.status()["plan"]
            expect = data.get("expect_bytes")
            if not last or data.get("source") != last.get("source"):
                raise AdminError(409, "Check the size of the download first.")
            if isinstance(expect, bool) or not isinstance(expect, int) or expect <= 0:
                raise AdminError(400, "expect_bytes must be the size agreed to, in bytes.")
            if not isinstance(data.get("allow_large", False), bool):
                raise AdminError(400, "allow_large must be true or false.")
            params = {"download": True, "expect_bytes": expect, "source": last["source"]}
            if last.get("large"):
                if data.get("allow_large") is not True:
                    raise AdminError(409, "The street maps would be %s or more: allow "
                                          "that first." % street_maps.human_bytes(
                                              street_maps.LARGE_BYTES))
                params["allow_large"] = True
        elif action == "remove":
            if maps_view()["job"] is not None:
                raise AdminError(409, "A street maps job is waiting or running; stop it "
                                      "first.")
            lock = jobs.WorkerLock()
            if not lock.acquire("deleting the street maps"):
                raise AdminError(409, "Wait until the running job has finished (%s)."
                                 % (lock.holder() or "another process"))
            try:
                removed = street_maps.remove()
                # (The named places found on them go too.)
                named_places.clear(get_conn())
            except OSError as error:
                raise AdminError(409, "The street maps could not be deleted: %s"
                                 % common.redact(str(error)))
            except sqlite3.OperationalError as error:
                raise AdminError(503, "The street maps are deleted, but their named places "
                                      "could not be forgotten (%s); the next scan does it."
                                 % error)
            finally:
                lock.release()
            return jsonify({"removed": removed, "maps": maps_view()})
        else:
            raise AdminError(404, "Unknown action: %s." % action)
        job, added = manager.enqueue("maps", None, params)
        return jsonify({"job": job, "added": added, "maps": maps_view()}), 202

    # -- pictures -------------------------------------------------------------

    @bp.route("/images/hide", methods=["POST"])
    def hide_images():
        """{ids, hidden}: hide pictures from searches (and exports), or show them again."""
        data = body()
        ids = image_ids(data.get("ids"), HIDE_LIMIT)
        hidden = data.get("hidden")
        if not isinstance(hidden, bool):
            raise AdminError(400, "hidden must be true or false.",
                             {"hidden": "Must be true or false."})
        conn = get_conn()
        found = changed = 0
        try:
            with conn:
                for part in id_chunks(ids):
                    marks = ",".join("?" * len(part))
                    found += conn.execute("SELECT COUNT(*) FROM images WHERE id IN (%s)" % marks,
                                          part).fetchone()[0]
                    changed += conn.execute(
                        "UPDATE images SET hidden = ? WHERE hidden <> ? AND id IN (%s)" % marks,
                        [int(hidden), int(hidden)] + part).rowcount
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)
        return jsonify({"hidden": hidden, "changed": changed, "found": found,
                        "unknown": len(ids) - found})

    # -- duplicates (the Duplicates page) -------------------------------------

    def int_arg(name, default, low, high):
        value = request.args.get(name)
        if value in (None, ""):
            return default
        try:
            number = int(value)
        except ValueError:
            raise AdminError(400, "%s must be a whole number." % name, {name: "A whole number."})
        if not low <= number <= high:
            raise AdminError(400, "%s must be from %d to %d." % (name, low, high),
                             {name: "From %d to %d." % (low, high)})
        return number

    @bp.route("/duplicates", methods=["GET"])
    def duplicate_stacks():
        """?lib=&kind=&min=&page=&stack=: the stacks of alike pictures to review."""
        config = state.config()
        library_ids = [library["id"] for library in common.enabled_libraries(config)
                       if library.get("id")]
        wanted = request.args.get("lib", "")
        if wanted not in ("", "all"):
            if wanted not in library_ids:
                raise AdminError(404, "There is no enabled library with that id.")
            library_ids = [wanted]
        kind = request.args.get("kind") or "all"
        if kind not in duplicates.KINDS:
            raise AdminError(400, "kind must be one of %s." % ", ".join(duplicates.KINDS))
        min_size = int_arg("min", 2, 2, duplicates.MIN_SIZE_LIMIT)
        page = int_arg("page", 1, 1, IMAGE_ID_MAX)
        stack = int_arg("stack", None, 1, IMAGE_ID_MAX)
        try:
            answer = duplicates.review_page(get_conn(), config, library_ids, kind, min_size,
                                            page, stack)
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)
        return jsonify(answer)

    @bp.route("/duplicates/not-duplicates", methods=["POST"])
    def keep_apart():
        """{ids, apart?, together?, undo?}: say pictures are not duplicates (undo: take
        that back).

        Every two of the pictures are kept apart, or with apart (one of the
        ids) only that one from each of the others; see not_duplicates.py.
        together (lists of some of the ids) leaves out the pairs within one
        list: "Same picture, any date" sends the pictures that share a stack
        of the search page, which stay stacked.
        """
        data = body()
        apart = data.get("apart")
        # One picture apart from the others makes a pair with each; all of
        # them apart make a pair of every two, so they are fewer.
        limit = HIDE_LIMIT if apart is not None else duplicates.NOT_DUPLICATES_LIMIT
        if apart is None and isinstance(data.get("ids"), list) and len(data["ids"]) > limit:
            raise AdminError(400, "At most %d pictures can be kept apart from each other at "
                                  "once; take single pictures out of the stack instead."
                             % limit, {"ids": "Too many."})
        ids = image_ids(data.get("ids"), limit)
        if apart is not None and (isinstance(apart, bool) or apart not in ids):
            raise AdminError(400, "apart must be one of the ids.", {"apart": "One of the ids."})
        undo = data.get("undo", False)
        if not isinstance(undo, bool):
            raise AdminError(400, "undo must be true or false.", {"undo": "True or false."})
        together = data.get("together")
        if together is not None:
            if not isinstance(together, list) or len(together) > len(ids):
                raise AdminError(400, "together must be lists of the ids.",
                                 {"together": "Lists of the ids."})
            together = [image_ids(group, len(ids), "together") for group in together]
            known = set(ids)
            if any(image_id not in known for group in together for image_id in group):
                raise AdminError(400, "together must be lists of the ids.",
                                 {"together": "Lists of the ids."})
        keys = {}
        conn = get_conn()
        for chunk in id_chunks(ids):
            for row in conn.execute("SELECT id, content_hash FROM images "
                                    "WHERE missing_since IS NULL AND id IN (%s)"
                                    % ",".join("?" * len(chunk)), chunk):
                keys[row["id"]] = not_duplicates.unit_key(row["id"], row["content_hash"])
        if apart is not None and apart not in keys:
            raise AdminError(404, "That picture is not in the index.")
        if len(set(keys.values())) < 2:
            raise AdminError(400, "These are identical files (or pictures that are not in the "
                                  "index): identical files always stay together.")
        pairs = not_duplicates.pairs_among(keys.values(),
                                           keys[apart] if apart is not None else None)
        for group in together or ():
            pairs -= not_duplicates.pairs_among([keys[image_id] for image_id in group
                                                 if image_id in keys])
        if not pairs:
            raise AdminError(400, "There is nothing to keep apart: these pictures are "
                                  "identical files, or share a stack of the search page.")
        try:
            changed = not_duplicates.change(pairs, add=not undo)
        except OSError as error:
            raise AdminError(500, "Could not save %s: %s"
                             % (not_duplicates.FILE_NAME, export.os_error_text(error)))
        return jsonify({"pairs": len(pairs), "changed": changed, "undo": undo,
                        "unknown": len(ids) - len(keys)})

    # -- the owner's titles, descriptions and tags (image_edits) ---------------

    def model_info(conn, image_id):
        """(present, the model's description dict or None) of a picture."""
        row = conn.execute("SELECT info FROM images WHERE id = ? AND missing_since IS NULL",
                           (image_id,)).fetchone() if image_id <= IMAGE_ID_MAX else None
        if row is None:
            return False, None
        try:
            info = json.loads(row["info"] or "null", parse_constant=lambda _name: None)
        except ValueError:
            info = None
        return True, info if isinstance(info, dict) else None

    def edit_answer(conn, image_id, edit, model):
        """What the edit routes answer: the edit, the description with it and the record keys."""
        record = search_index.picture_record(conn, image_id, edit, state.config()) or {}
        return {"id": image_id, "edited": not image_edits.is_empty(edit),
                "edits": image_edits.view(edit), "info": image_edits.apply(model, edit),
                "model_info": model,
                # The keys of the search record an edit can change (absent: none).
                "record": {key: record[key] for key in ("t", "d", "g", "ed") if key in record}}

    def edit_error(error):
        return AdminError(400, str(error), error.fields)

    @bp.route("/images/<int:image_id>/edits", methods=["GET"])
    def get_edits(image_id):
        """The owner's edit of a picture, with the model's description and the one shown."""
        conn = get_conn()
        present, model = model_info(conn, image_id)
        if not present:
            raise AdminError(404, "There is no picture %d." % image_id)
        return jsonify(edit_answer(conn, image_id, image_edits.load(conn, image_id), model))

    @bp.route("/images/<int:image_id>/edits", methods=["POST"])
    def save_edits(image_id):
        """{title?, description?, tags_add?, tags_remove?}: change a picture's edit.

        Only the fields sent change; null (or "") clears one, so the
        model's text is used again. An edit left empty is deleted.
        """
        changes = body()
        conn = get_conn()
        present, model = model_info(conn, image_id)
        if not present:
            raise AdminError(404, "There is no picture %d." % image_id)
        try:
            with conn:
                edit = image_edits.save(conn, image_id, changes)
        except image_edits.EditError as error:
            raise edit_error(error)
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)
        return jsonify(edit_answer(conn, image_id, edit, model))

    @bp.route("/images/tags", methods=["POST"])
    def change_tags():
        """{ids, add, remove}: add and remove tags on many pictures (the owner's edits)."""
        data = body()
        ids = image_ids(data.get("ids"), HIDE_LIMIT)
        try:
            add = image_edits.clean_tags(data.get("add"), "add")
            remove = image_edits.clean_tags(data.get("remove"), "remove")
        except image_edits.EditError as error:
            raise edit_error(error)
        if not add and not remove:
            raise AdminError(400, "Name a tag to add or remove.",
                             {"add": "Name a tag.", "remove": "Name a tag."})
        conn = get_conn()
        try:
            with conn:
                result = image_edits.change_tags(conn, ids, add, remove)
        except image_edits.EditError as error:
            raise edit_error(error)
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)
        return jsonify({"changed": result["changed"], "found": result["found"],
                        "unknown": len(ids) - result["found"],
                        "tags": {str(image_id): search_index.record_tags(tags)
                                 for image_id, tags in result["tags"].items()},
                        "edited": {str(image_id): edited
                                   for image_id, edited in result["edited"].items()}})

    # -- the GPS check (gps_check.py) ---------------------------------------------

    @bp.route("/images/<int:image_id>/location", methods=["POST"])
    def choose_location(image_id):
        """{use: true or false}: use a picture's GPS position anyway, or ignore it.

        The choice is made for every copy of the shot and kept by content
        (gps_check.set_choice: only while it differs from what the check
        decides), and the check runs again at once, as the position may be
        another picture's reference; so do the local times (local_times.py:
        a time worked out from a position set aside is the camera's again).
        Answers the picture's location_check (as /api/image has it), its
        "time" (local_times.detail: taken_at, tz_offset, clock), the keys of
        its search record a position changes (absent: none) -- and those of
        its time when it changed --, "copies": how many copies the choice was
        also made for, and "others": {id: those keys} of the other pictures
        whose position, place or time changed (at most gps_check.OTHERS_MAX;
        "more": how many more there are).
        """
        use = body().get("use")
        if not isinstance(use, bool):
            raise AdminError(400, "Send use: true (use the position) or false (ignore it).",
                             {"use": "Send true or false."})
        conn = get_conn()
        config = state.config()
        changed = set()
        try:
            with conn:
                ids = (gps_check.set_choice(conn, config, image_id, use)
                       if image_id <= IMAGE_ID_MAX else [])
            if not ids:
                raise AdminError(404, "Picture %d has no GPS position." % image_id)
            gps_check.run(conn, config, changed=changed)
            times = local_times.after_check(conn, config, changed)
            # (A position used or ignored names another place, or none.)
            named_places.after_check(conn, config, changed)
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)

        def keys(picture_id):
            record = search_index.picture_record(conn, picture_id,
                                                 image_edits.load(conn, picture_id),
                                                 config) or {}
            wanted = gps_check.RECORD_KEYS + named_places.RECORD_KEYS + (
                local_times.RECORD_KEYS if picture_id in times else ())
            return {key: record[key] for key in wanted if key in record}

        others = sorted((set(ids) | changed) - {image_id})
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        return jsonify({"id": image_id, "location_check": gps_check.picture_view(conn, row),
                        "time": local_times.detail(conn, row),
                        "record": keys(image_id), "copies": len(ids) - 1,
                        "others": {str(other): keys(other)
                                   for other in others[:gps_check.OTHERS_MAX]},
                        "more": max(0, len(others) - gps_check.OTHERS_MAX)})

    # -- where 360 pictures open (opening_views.py) -----------------------------------

    @bp.route("/images/<int:image_id>/opening-view", methods=["POST"])
    def set_opening_view(image_id):
        """{yaw, pitch, fov?} (degrees: right of the panorama's middle, up, across the
        view): a 360 picture opens there from now on ("Open here"), and so do its
        copies; {clear: true}: forget it (the automatic view, or the front, again).
        Their thumbnails show the new view. Answers {"id", "opening_view" (as
        /api/image has it), "record" ({"ov": ...}, or {} for the front), "ids" (the
        pictures it was set for: the copies too)}."""
        data = body()
        conn = get_conn()
        try:
            with conn:
                if data.get("clear") is True:
                    row = (opening_views.clear_owner(conn, image_id)
                           if image_id <= IMAGE_ID_MAX else None)
                else:
                    row = (opening_views.set_owner(conn, image_id, data.get("yaw"),
                                                   data.get("pitch"), data.get("fov"))
                           if image_id <= IMAGE_ID_MAX else None)
        except opening_views.ViewError as error:
            raise AdminError(400, str(error), error.fields)
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)
        if row is None:
            raise AdminError(404, "There is no 360-degree picture %d." % image_id)
        ids = opening_views.copies(conn, image_id)
        thumbs = state.thumbs()
        for picture_id in ids:
            opening_views.remake_thumbnail(conn, picture_id, state.previews, thumbs,
                                           commit=False)
        thumbs.commit()
        view = opening_views.detail(conn, image_id, row["content_hash"], owner=True)
        value = opening_views.record_value(dict(view, owner=view["owner"]))
        return jsonify({"id": image_id, "opening_view": view,
                        "record": {"ov": value} if value is not None else {}, "ids": ids})
    # -- estimated places (place_estimates.py) ---------------------------------------

    @bp.route("/images/<int:image_id>/place", methods=["POST"])
    def choose_place(image_id):
        """{choice, lat?, lon?}: the owner's place for a picture without a usable position.

        choice "accept" (the estimate becomes the owner's place), "set" (the
        position lat, lon: a town picked from /places/towns, or one typed),
        "reject" (no estimate) or "clear" (the estimates decide again); see
        place_estimates.set_choice. The GPS check and the estimates run again
        at once, and so do the local times (local_times.after_check) and the
        named places (the other pictures of its visit may take its place).
        Answers the picture's "place" and "place_estimate" (as /api/image has
        them), its "time" (local_times.detail) and "record", the keys of its
        search record a place changes (absent: none), and those of its time
        when it changed -- and "others": {id: those keys} of the other
        pictures whose position, place or time changed (at most
        gps_check.OTHERS_MAX; "more": how many more there are).
        """
        data = body()
        choice = data.get("choice")
        if choice not in ("accept", "set", "reject", "clear"):
            raise AdminError(400, "Send choice: accept, set, reject or clear.",
                             {"choice": "accept, set, reject or clear."})
        lat, lon = data.get("lat"), data.get("lon")
        if choice == "set" and not all(isinstance(value, (int, float))
                                       and not isinstance(value, bool) for value in (lat, lon)):
            raise AdminError(400, "Send the position as numbers: lat and lon.",
                             {"lat": "A number.", "lon": "A number."})
        if image_id > IMAGE_ID_MAX:
            raise AdminError(404, "There is no picture %d." % image_id)
        conn = get_conn()
        config = state.config()
        changed = set()
        try:
            with conn:
                place_estimates.set_choice(conn, image_id, choice, lat, lon)
            gps_check.run(conn, config, changed=changed)
            times = local_times.after_check(conn, config, changed)
            named_places.after_check(conn, config, changed)
        except place_estimates.ChoiceError as error:
            raise AdminError(error.status, str(error))
        except sqlite3.OperationalError as error:
            raise AdminError(503, "The database is busy (%s); try again in a moment." % error)
        import serve

        def keys(picture_id):
            record = search_index.picture_record(conn, picture_id,
                                                 image_edits.load(conn, picture_id),
                                                 config) or {}
            wanted = gps_check.RECORD_KEYS + named_places.RECORD_KEYS + (
                local_times.RECORD_KEYS if picture_id in times else ())
            return {key: record[key] for key in wanted if key in record}

        others = sorted(changed - {image_id})
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        estimate = place_estimates.for_picture(conn, image_id)
        return jsonify({"id": image_id, "place": serve.place_of(row, estimate),
                        "place_estimate": place_estimates.view(conn, row, estimate, admin=True),
                        "time": local_times.detail(conn, row), "record": keys(image_id),
                        "others": {str(other): keys(other)
                                   for other in others[:gps_check.OTHERS_MAX]},
                        "more": max(0, len(others) - gps_check.OTHERS_MAX)})

    @bp.route("/places/towns", methods=["GET"])
    def find_towns():
        """?q=: the towns whose names start with q (places.PlaceIndex.find_towns), for
        setting a picture's place: {"towns": [{name, region, country, cc, lat, lon,
        population}], "position": {lat, lon, place} when q is a position ("lat, lon"),
        "available": whether there are place names}."""
        import places
        text = (request.args.get("q") or "")[:200]
        index = places.shared()
        answer = {"towns": [], "position": None, "available": index is not None}
        position = place_estimates.parse_position(text)
        if position is not None:
            answer["position"] = {"lat": position[0], "lon": position[1],
                                  "place": gps_check.place_text(
                                      places.place_columns(position[0], position[1], index))
                                  if index is not None else ""}
        elif index is not None:
            try:
                answer["towns"] = index.find_towns(text)
            except places.PlacesError as error:
                raise AdminError(503, str(error))
        return jsonify(answer)

    # -- favorites, ratings and albums (albums.py) --------------------------------

    def album_error(error):
        return AdminError(error.status, str(error), error.fields)

    def busy(error):
        return AdminError(503, "The database is busy (%s); try again in a moment." % error)

    def album_change(change):
        """change(conn) in one transaction; AlbumError and a busy database answered."""
        conn = get_conn()
        try:
            with conn:
                return change(conn)
        except albums.AlbumError as error:
            raise album_error(error)
        except sqlite3.IntegrityError as error:
            raise AdminError(409, "That cannot be saved (%s)." % error)
        except sqlite3.OperationalError as error:
            raise busy(error)

    @bp.route("/marks", methods=["POST"])
    def set_marks():
        """{ids, favorite?, rating?}: make pictures favorites (or not) and rate them (0: none)."""
        data = body()
        ids = image_ids(data.get("ids"), albums.MARK_LIMIT)
        favorite, rating = data.get("favorite"), data.get("rating")
        if favorite is None and rating is None:
            raise AdminError(400, "Send favorite (true or false) or a rating (0 to 5).",
                             {"favorite": "Send one.", "rating": "Send one."})
        result = album_change(lambda conn: albums.set_marks(conn, ids, favorite, rating))
        return jsonify({"changed": result["changed"], "found": result["found"],
                        "unknown": len(ids) - result["found"],
                        "marks": {str(image_id): mark
                                  for image_id, mark in result["marks"].items()}})

    @bp.route("/albums", methods=["GET"])
    def album_list():
        """Every album with its count (manual albums), by name."""
        return jsonify({"albums": albums.list_albums(get_conn())})

    @bp.route("/albums", methods=["POST"])
    def new_album():
        """{name, kind ("manual" or "smart"), query?, options? (smart), ids? (manual)}."""
        data = body()
        ids = data.get("ids")
        ids = image_ids(ids, albums.MARK_LIMIT) if ids not in (None, []) else []
        album = album_change(lambda conn: albums.create_album(
            conn, data.get("name"), data.get("kind") or "manual", data.get("query"),
            data.get("options"), ids))
        return jsonify({"album": album}), 201

    @bp.route("/albums/<int:album_id>", methods=["GET"])
    def album_detail(album_id):
        """One album, and the ids of a manual album's pictures in order."""
        conn = get_conn()
        album = albums.get_album(conn, album_id) if album_id <= IMAGE_ID_MAX else None
        if album is None:
            raise AdminError(404, "There is no album %d." % album_id)
        items = albums.album_items(conn, album_id) if album["kind"] == "manual" else []
        if album["kind"] == "manual":
            album["count"] = len(items)
        return jsonify({"album": album, "items": items})

    @bp.route("/albums/<int:album_id>", methods=["POST"])
    def edit_album(album_id):
        """{name?, cover?, query?, options?}: rename an album, choose its cover, or change
        the search of a smart album (only the fields sent change)."""
        changes = body()
        album = album_change(lambda conn: albums.update_album(conn, album_id, changes))
        return jsonify({"album": album})

    @bp.route("/albums/<int:album_id>/delete", methods=["POST"])
    def remove_album(album_id):
        """Delete an album; its pictures stay where they are."""
        body()
        count = album_change(lambda conn: albums.delete_album(conn, album_id))
        return jsonify({"deleted": album_id, "pictures": count})

    @bp.route("/albums/<int:album_id>/items", methods=["POST"])
    def album_items_change(album_id):
        """{add?: ids, remove?: ids}: put pictures in a manual album (at the end), or take
        them out."""
        data = body()
        add = data.get("add")
        remove = data.get("remove")
        add = image_ids(add, albums.MARK_LIMIT, "add") if add not in (None, []) else []
        remove = (image_ids(remove, albums.MARK_LIMIT, "remove") if remove not in (None, [])
                  else [])
        if not add and not remove:
            raise AdminError(400, "Send the pictures to add or remove.",
                             {"add": "Send some.", "remove": "Send some."})

        def change(conn):
            removed = albums.remove_items(conn, album_id, remove) if remove else 0
            added = albums.add_items(conn, album_id, add) if add else {"added": 0, "found": 0}
            return {"added": added["added"], "removed": removed,
                    "count": len(albums.album_items(conn, album_id)),
                    "album": albums.get_album(conn, album_id)}

        return jsonify(album_change(change))

    @bp.route("/albums/<int:album_id>/order", methods=["POST"])
    def album_order(album_id):
        """{ids, before?}: move pictures of a manual album to just before picture before
        (null or left out: to the end); or {order}: all its pictures in a new order."""
        data = body()
        if "order" in data:
            order = image_ids(data.get("order"), albums.ITEMS_LIMIT, "order")
            new = album_change(lambda conn: albums.set_order(conn, album_id, order))
        else:
            ids = image_ids(data.get("ids"), albums.MARK_LIMIT)
            before = data.get("before")
            if before is not None and (isinstance(before, bool) or not isinstance(before, int)):
                raise AdminError(400, "before is a picture id (or null).",
                                 {"before": "A picture id."})
            new = album_change(lambda conn: albums.move_items(conn, album_id, ids, before))
        return jsonify({"order": new})

    # -- trips (trips.py) ---------------------------------------------------------------

    def trip_change(change):
        """change(conn, config, library ids) of the trips of the enabled libraries; a
        TripError answered with its status."""
        config = state.config()
        library_ids = [library["id"] for library in common.enabled_libraries(config)
                       if library.get("id")]
        try:
            return change(get_conn(), config, library_ids)
        except trips.TripError as error:
            raise AdminError(error.status, str(error))
        except OSError as error:
            raise AdminError(500, "The trips' changes cannot be saved: %s" % error)

    def trip_id_arg(trip_id):
        if not trips.ID_RE.match(trip_id or ""):
            raise AdminError(404, "There is no trip %s." % trip_id)
        return trip_id

    @bp.route("/trips/<trip_id>/story", methods=["POST"])
    def edit_trip_story(trip_id):
        """{days?: [{date, text}], hidden?: bool, forget?: true}: the owner's change of a
        trip's story (trip_stories.change_story); answers its story_view."""
        data = body()
        trip_id = trip_id_arg(trip_id)
        if not any(key in data for key in ("days", "hidden", "forget")):
            raise AdminError(400, "Send days, hidden or forget.")

        def change(conn, config, ids):
            try:
                return trip_stories.change_story(conn, config, ids, trip_id,
                                                 days=data.get("days"),
                                                 hidden=data.get("hidden"),
                                                 forget_it=data.get("forget") is True)
            except trip_stories.StoryError as error:
                raise AdminError(error.status, str(error))
        return jsonify({"story": trip_change(change)})

    @bp.route("/trips/stories/plan", methods=["GET"])
    def trip_stories_plan():
        """?trip=ID&redo=1: what a trip_stories job would write, with which endpoints and
        about what it costs (trip_stories.plan), for the pages to show before it starts."""
        trip_id = request.args.get("trip") or None
        if trip_id is not None:
            trip_id = trip_id_arg(trip_id)
        redo = request.args.get("redo") in ("1", "true")
        return jsonify(trip_change(lambda conn, config, ids: trip_stories.plan(
            conn, config, trip_id, redo)))

    @bp.route("/trips/<trip_id>", methods=["POST"])
    def edit_trip(trip_id):
        """{title?, hidden?}: rename a trip ("" gives it back its own title), hide it or
        show it again."""
        data = body()
        trip_id = trip_id_arg(trip_id)
        if "title" not in data and "hidden" not in data:
            raise AdminError(400, "Send a title or hidden.")
        trip = trip_change(lambda conn, config, ids: trips.set_trip(
            conn, config, ids, trip_id, title=data.get("title"), hidden=data.get("hidden")))
        return jsonify({"trip": trip})

    @bp.route("/trips/<trip_id>/split", methods=["POST"])
    def split_trip(trip_id):
        """{date}: split a trip before that day (one of its days after the first)."""
        data = body()
        trip_id = trip_id_arg(trip_id)
        parts = trip_change(lambda conn, config, ids: trips.split_trip(
            conn, config, ids, trip_id, data.get("date")))
        return jsonify({"ids": parts})

    @bp.route("/trips/merge", methods=["POST"])
    def merge_trips():
        """{ids}: make one trip of two or more (parts of a split trip are joined again)."""
        data = body()
        merged = trip_change(lambda conn, config, ids: trips.merge_trips(
            conn, config, ids, data.get("ids")))
        return jsonify({"id": merged})

    @bp.route("/trips/<trip_id>/reset", methods=["POST"])
    def reset_trip(trip_id):
        """Forget the changes made to a trip: it shows as it was found."""
        body()
        trip_id = trip_id_arg(trip_id)
        members = trip_change(lambda conn, config, ids: trips.reset_trip(
            conn, config, ids, trip_id))
        return jsonify({"ids": members})

    def picture_file(config, image_id):
        """The full path of a present picture of a configured library; AdminError 404 otherwise."""
        row = get_conn().execute("SELECT id, library_id, path FROM images "
                                 "WHERE id = ? AND missing_since IS NULL",
                                 (image_id,)).fetchone() if image_id <= IMAGE_ID_MAX else None
        library = next((item for item in config.get("libraries", [])
                        if row is not None and item.get("id") == row["library_id"]), None)
        full = (library_ops.library_file(common.library_root(library), row["path"])
                if library is not None else None)
        if full is None:
            raise AdminError(404, "There is no picture %d in a configured library." % image_id)
        return full

    @bp.route("/images/<int:image_id>/open-folder", methods=["POST"])
    def open_folder(image_id):
        """Show a picture's file in Explorer (Finder; elsewhere its folder).

        The file is found from the image id alone; nothing in the request
        names a path.
        """
        body()
        full = picture_file(state.config(), image_id)
        try:
            library_ops.reveal_file(full)
        except library_ops.TimedOut:
            raise AdminError(504, "The folder did not answer (an offline share?).")
        return jsonify({"ok": True, "id": image_id})

    @bp.route("/images/paths", methods=["POST"])
    def image_paths():
        """{ids}: the full paths of pictures ("Copy path"), in the order asked."""
        config = state.config()
        result, unknown = [], []
        for image_id in image_ids(body().get("ids"), HIDE_LIMIT):
            try:
                result.append({"id": image_id, "path": picture_file(config, image_id)})
            except AdminError:
                unknown.append(image_id)
        return jsonify({"paths": result, "unknown": unknown})

    # -- exports --------------------------------------------------------------

    def export_options_arg(data):
        options = data.get("options")
        if options is not None and not isinstance(options, dict):
            raise AdminError(400, "Send the export options as an object.",
                             {"options": "Must be an object."})
        return options

    @bp.route("/libraries/<library_id>/export/preview", methods=["POST"])
    def export_preview(library_id):
        """{options, flagged, out_dir}: what an export would publish (export.preview_export).

        options change the library's saved ones for this answer only
        (nothing is saved); flagged: false leaves out the list of flagged
        pictures (for a quick count). With out_dir (the folder the dialog
        would export to; blank: the default) the answer holds free_bytes,
        the free space on its disk (None when unknown). Whether the
        library's folder can be read comes from the Manage page's folder
        checks (never waited for here). The answer also holds export_view().
        """
        data = body()
        config = state.config()
        library = find_library(config, library_id)
        options = export_options_arg(data)
        reachable = folder_status(library).get("reachable")
        try:
            result = export.preview_export(get_conn(), config, library["id"], options,
                                           reachable=reachable is not False)
        except export.OptionsError as error:
            raise options_error(error)
        except export.ExportError as error:
            raise AdminError(400, str(error))
        if data.get("flagged") is False:
            result.pop("flagged", None)
        if "out_dir" in data:
            target, _default = export_target(config, library, data.get("out_dir"))
            result["free_bytes"] = free_space(target)
        view = export_view(config, library)
        result.update(default_output=view["default_output"], last=view["last"],
                      saved_options=view["options"])
        return jsonify(result)

    @bp.route("/trips/<trip_id>/export", methods=["GET"])
    def trip_export_info(trip_id):
        """A trip's export for the export dialog (trip_export.export_info)."""
        config = state.config()
        load_trip(config, trip_id)
        try:
            return jsonify(trip_export.export_info(get_conn(), config, trip_id))
        except export.ExportError as error:
            raise AdminError(404, str(error))

    @bp.route("/trips/<trip_id>/export/preview", methods=["POST"])
    def trip_export_preview(trip_id):
        """{options, flagged, out_dir}: what an export of a trip would publish, as
        /libraries/<id>/export/preview (trip_export.preview_trip; nothing is saved)."""
        data = body()
        config = state.config()
        _built, _trip, libraries = load_trip(config, trip_id)
        options = export_options_arg(data)
        reachable = {library["id"]: folder_status(library).get("reachable") is not False
                     for library in libraries}
        try:
            result = trip_export.preview_trip(get_conn(), config, trip_id, options,
                                              reachable=reachable)
        except export.OptionsError as error:
            raise options_error(error)
        except export.ExportError as error:
            raise AdminError(400, str(error))
        if data.get("flagged") is False:
            result.pop("flagged", None)
        info = trip_export.export_info(get_conn(), config, trip_id)
        if "out_dir" in data:
            target, _default = export_target(config, None, data.get("out_dir"),
                                             info["default_output"])
            result["free_bytes"] = free_space(target)
        result.update(default_output=info["default_output"], last=info["last"],
                      saved_options=info["options"])
        return jsonify(result)

    @bp.route("/trips/<trip_id>/export/open-folder", methods=["POST"])
    def open_trip_export_folder(trip_id):
        """Show a trip's exported site (its last export's folder) in the file manager."""
        body()
        config = state.config()
        load_trip(config, trip_id)
        last = trip_export.export_info(get_conn(), config, trip_id)["last"]
        if not last:
            raise AdminError(404, "This trip has not been exported yet.")
        try:
            there = library_ops.run_with_timeout(os.path.isdir, EXPORT_FOLDER_TIMEOUT,
                                                 last["output"])
        except library_ops.TimedOut:
            raise AdminError(504, "The export folder did not answer (an offline share?).")
        return reveal_export(last["output"], not there)

    @bp.route("/libraries/<library_id>/export/options", methods=["POST"])
    def export_options(library_id):
        """{options}: change a library's saved export options (those sent; the rest stay)."""
        data = body()
        options = export_options_arg(data)
        if options is None:
            raise AdminError(400, "Send the export options to save.", {"options": "Required."})

        def mutate(config):
            library = find_library(config, library_id)
            try:
                merged = export.library_options(library, options)
            except export.OptionsError as error:
                raise options_error(error)
            library["export"] = export.options_to_save(merged)
            return config, library, merged

        (config, library, merged), token = write_config(mutate, token_of(data))
        return jsonify({"options": merged, "saved": library["export"],
                        "export_info": export_view(config, library), "config_token": token})

    @bp.route("/exports/<library_id>/open-folder", methods=["POST"])
    def open_export_folder(library_id):
        """Show a library's exported site in the file manager.

        The folder is the one its last export wrote (from the export state
        in the data root), else its default folder; nothing in the request
        names a path.
        """
        body()
        config = state.config()
        find_library(config, library_id)
        try:
            folder, missing = library_ops.run_with_timeout(
                export.last_export_dir, EXPORT_FOLDER_TIMEOUT, config, library_id)
        except library_ops.TimedOut:
            raise AdminError(504, "The export folder did not answer (an offline share?).")
        if folder is None:
            raise AdminError(404, "This library has not been exported yet.")
        return reveal_export(folder, missing)

    def reveal_export(folder, missing):
        """Show an exported site's index.html (or its folder) in the file manager."""
        gone = ("The folder of the last export (%s) is not there any more: it was moved or "
                "deleted, or its drive is not connected.")
        try:
            if missing:
                raise AdminError(404, gone % folder)
            index = os.path.join(folder, "index.html")
            if library_ops.run_with_timeout(os.path.isfile, EXPORT_FOLDER_TIMEOUT, index):
                library_ops.reveal_file(index, EXPORT_FOLDER_TIMEOUT)
            else:
                library_ops.open_folder(folder, EXPORT_FOLDER_TIMEOUT)
        except library_ops.TimedOut:
            raise AdminError(504, "The export folder did not answer (an offline share?).")
        except library_ops.LibraryError as error:
            if error.status != 404:
                raise
            raise AdminError(404, gone % folder)
        return jsonify({"ok": True, "path": folder})

    # -- endpoints ------------------------------------------------------------

    @bp.route("/endpoints", methods=["GET"])
    def endpoints():
        config = state.config()
        return jsonify({"endpoints": endpoint_views(config),
                        "default_endpoint": common.default_endpoint_name(config),
                        "presets": preset_views(), "config_token": config_token()})

    @bp.route("/endpoints", methods=["POST"])
    def save_endpoint():
        data = body()

        def mutate(config):
            entry, cleared, warnings = apply_endpoint(config, data.get("endpoint"),
                                                      data.get("original_name"),
                                                      replace=data.get("replace") is True)
            return config, entry["name"], cleared, warnings

        (config, name, cleared, warnings), token = write_config(mutate, token_of(data))
        probes.forget()
        key_files.forget()
        view = next((item for item in endpoint_views(config) if item["name"] == name), None)
        return jsonify({"endpoint": view, "key_cleared": cleared, "warnings": warnings,
                        "default_endpoint": common.default_endpoint_name(config),
                        "config_token": token})

    @bp.route("/endpoints/models", methods=["POST"])
    def endpoint_models():
        data = body()
        fields = data.get("endpoint") or {}
        if not isinstance(fields, dict):
            raise AdminError(400, "Send the endpoint settings as an object.")
        fields = dict(fields)
        original = data.get("original_name")
        config = copy.deepcopy(state.config())
        if not original:
            # A new endpoint, whatever name was typed so far (this saves nothing).
            taken = {str(entry.get("name", "")).strip().casefold()
                     for entry in settings.endpoint_entries(config)}
            number = 1
            while "new-endpoint-%d" % number in taken:
                number += 1
            fields["name"] = "new-endpoint-%d" % number
        entry, _cleared, _warnings = apply_endpoint(config, fields, original,
                                                    require_model=False)
        problem = unsaved_key_problem(state.config(), entry)
        if problem:
            raise AdminError(400, problem)
        try:
            models = library_ops.run_with_timeout(llm.list_models, MODELS_TIMEOUT,
                                                  common.endpoint_settings(entry))
        except library_ops.TimedOut:
            raise AdminError(504, "The server did not list its models within %d seconds."
                             % MODELS_TIMEOUT)
        return jsonify({"models": models})

    @bp.route("/endpoints/default", methods=["POST"])
    def default_endpoint():
        data = body()
        name = text_field(data, "name")

        def mutate(config):
            try:
                settings.set_default_endpoint(config, name)
            except ValueError as error:
                raise AdminError(400, str(error), {"name": str(error)})
            return config

        config, token = write_config(mutate, token_of(data))
        return jsonify({"default_endpoint": common.default_endpoint_name(config),
                        "config_token": token})

    @bp.route("/endpoints/<path:name>/remove", methods=["POST"])
    def remove_endpoint(name):
        data = body()

        def mutate(config):
            try:
                settings.remove_endpoint(config, name)
            except ValueError as error:
                raise AdminError(404, str(error))
            return config, settings.libraries_using(config, name)

        (config, users), token = write_config(mutate, token_of(data))
        warnings = []
        if users:
            warnings.append("Libraries %s name this endpoint; their images are not described "
                            "until you choose another one for them."
                            % ", ".join(common.library_label(library) for library in users))
        return jsonify({"removed": name, "warnings": warnings,
                        "default_endpoint": common.default_endpoint_name(config),
                        "config_token": token})

    @bp.route("/endpoints/<path:name>/test", methods=["POST"])
    def test_endpoint(name):
        data = body()
        config = state.config()
        endpoint = common.get_endpoint(config, name)
        if endpoint is None:
            raise AdminError(404, "There is no endpoint called %s." % name)
        try:
            import describe
            import images
        except (ImportError, SystemExit):
            raise AdminError(501, "Testing an endpoint needs Pillow (python -m pip install "
                                  "Pillow).")
        local = common.is_local_endpoint(endpoint)
        image_id = data.get("image_id")
        try:
            if image_id is not None:
                if not local:
                    raise AdminError(400, "Only the bundled sample picture is sent to an "
                                          "endpoint outside this computer and network.")
                jpeg = library_image(config, image_id, endpoint, describe)
                sent = "library image %d" % image_id
            else:
                with open(SAMPLE_IMAGE, "rb") as handle:
                    jpeg = images.prepare_for_llm(handle.read(),
                                                  max_edge=endpoint["max_image_edge"])
                sent = "sample picture"
        except (OSError, images.ImageReadError) as error:
            raise AdminError(400, "Cannot read the test picture: %s" % error)
        trial = dict(endpoint, timeout=min(endpoint["timeout"], TEST_TIMEOUT))
        try:
            result = library_ops.run_with_timeout(
                llm.test_endpoint, TEST_TIMEOUT + 5, trial, jpeg,
                parse=lambda text: describe.parse_reply(text, "core"))
        except library_ops.TimedOut:
            result = {"ok": False, "error_kind": "timeout",
                      "error": "No answer within %d seconds." % TEST_TIMEOUT,
                      "latency_ms": None, "served_model": "", "parsed": None,
                      "usage": {"input_tokens": None, "output_tokens": None}, "finish": "",
                      "est_cost_per_image": None,
                      "key_source": resolve_key(endpoint)[1]}
        pending = pending_for_endpoint(config, endpoint["name"])
        cost = result.get("est_cost_per_image")
        result.update(image=sent, local=local, host=common.url_host(endpoint["base_url"]),
                      pending_images=pending,
                      est_cost_pending=round(cost * pending, 4) if cost is not None else None)
        key, _source = resolve_key(endpoint)
        return jsonify(redact_tree(result, [key] if key else []))

    def library_image(config, image_id, endpoint, describe):
        if isinstance(image_id, bool) or not isinstance(image_id, int):
            raise AdminError(400, "image_id must be an image id.")
        row = get_conn().execute(
            "SELECT id, library_id, path, has_preview, projection FROM images "
            "WHERE id = ? AND missing_since IS NULL", (image_id,)).fetchone()
        library = next((item for item in config.get("libraries", [])
                        if row is not None and item.get("id") == row["library_id"]), None)
        if library is None:
            raise AdminError(404, "There is no image %d in a configured library." % image_id)
        import cache
        return describe.image_for_llm(row, common.library_root(library), cache.PreviewStore(),
                                      state.helper, max_edge=endpoint["max_image_edge"])

    def pending_for_endpoint(config, name):
        counts = db.library_counts(get_conn(), stale_sql=prompts.stale_sql(config))
        total = 0
        for library in common.enabled_libraries(config):
            names = [item["name"] for item in common.endpoints_for_library(config, library)]
            if name in names:
                total += counts_view(counts.get(library.get("id"), {}))["pending"]
        return total

    # -- the model server -----------------------------------------------------

    # Listing the models folder (which may be on a share) happens in the background.
    model_lists = library_ops.BackgroundChecks(ttl=MODEL_LIST_SECONDS,
                                               timeout=library_ops.PATH_CHECK_TIMEOUT)

    def model_choices(settings, wait=MODEL_LIST_WAIT):
        root = local_server.models_root(settings)
        found = model_lists.get(("models", root), local_server.model_choices, settings,
                                timed_out={"models": [], "mmprojs": [], "error": "The models "
                                           "folder does not answer (an offline share?)."},
                                wait=wait)
        if found is None:
            return {"models": [], "mmprojs": [], "error": "", "checking": True}
        return found

    def server_of(config, data):
        name = data.get("endpoint")
        settings = local_server.find_server(config, name) if isinstance(name, str) else None
        if settings is None:
            raise AdminError(404, "No model server is set up for the endpoint %s in "
                                  "config.json." % name, {"endpoint": "Unknown."})
        return settings

    # So is looking at its files (the model, the projector, the program).
    file_checks = library_ops.BackgroundChecks(ttl=MODEL_LIST_SECONDS,
                                               timeout=library_ops.PATH_CHECK_TIMEOUT)

    def server_facts(settings, wait=MODEL_LIST_WAIT):
        """local_server.file_facts() of a server, worked out in the background."""
        key = ("facts", json.dumps(settings, sort_keys=True))
        found = file_checks.get(key, local_server.file_facts, settings,
                                timed_out={"problem": "The models folder does not answer (an "
                                           "offline share?).", "command": None,
                                           "mmproj_auto": "", "offline": True},
                                wait=wait)
        if found is None:
            return {"problem": "", "command": None, "mmproj_auto": "", "checking": True}
        return found

    def server_view(config, name, until=None):
        """A server's status for the panel; until: when to stop waiting for file checks."""
        settings = local_server.find_server(config, name)
        if settings is None:
            return model_servers.status(config, name)
        until = time.monotonic() + MODEL_LIST_WAIT if until is None else until
        view = model_servers.status(config, name, facts=server_facts(
            settings, wait=max(0.0, until - time.monotonic())))
        view["choices"] = model_choices(settings, wait=max(0.0, until - time.monotonic()))
        return view

    seen_states = {}

    @bp.route("/model-server", methods=["GET"])
    def model_server_state():
        """{"servers": [status], "setup": an example for config.json, "limits", "ui_fields"}."""
        config = state.config()
        # (One wait for the file checks of every server, not one each.)
        until = time.monotonic() + MODEL_LIST_WAIT
        servers = [server_view(config, settings["endpoint"], until)
                   for settings in local_server.server_list(config)]
        states = {server["endpoint"]: server["state"] for server in servers}
        if states != seen_states:
            # A server started or stopped: the endpoints' status is looked at again.
            seen_states.clear()
            seen_states.update(states)
            probes.forget()
        return jsonify({"servers": servers,
                        "setup": model_server_example(config),
                        "limits": {name: list(value)
                                   for name, value in local_server.LIMITS.items()},
                        "ui_fields": list(local_server.UI_FIELDS),
                        "config_token": config_token()})

    @bp.route("/model-server/start", methods=["POST"])
    def model_server_start():
        config = state.config()
        settings = server_of(config, body())
        facts = server_facts(settings)
        if facts.get("checking") or facts.get("offline"):
            # (Starting would look at the same files, on the request's thread.)
            raise AdminError(409, facts.get("problem") or "The model files are still being "
                                  "looked at; try again in a moment.")
        ok, message = model_servers.start(config, settings["endpoint"], by="you", wait=False)
        if not ok:
            raise AdminError(409, message)
        return jsonify({"ok": True, "message": message,
                        "server": server_view(config, settings["endpoint"])}), 202

    @bp.route("/model-server/stop", methods=["POST"])
    def model_server_stop():
        config = state.config()
        settings = server_of(config, body())
        ok, message = model_servers.stop(config, settings["endpoint"], wait=False,
                                         reason="you asked")
        if not ok:
            raise AdminError(409, message)
        return jsonify({"ok": True, "message": message,
                        "server": server_view(config, settings["endpoint"])}), 202

    @bp.route("/model-server/settings", methods=["POST"])
    def model_server_settings():
        """{endpoint, settings: {...}}: change the settings the page may change (UI_FIELDS)."""
        data = body()
        current = server_of(state.config(), data)
        fields = data.get("settings")
        if not isinstance(fields, dict) or not fields:
            raise AdminError(400, "Send the settings to change as an object.")
        unknown = sorted(set(fields) - set(local_server.UI_FIELDS))
        if unknown:
            raise AdminError(400, "%s can only be changed in config.json." % ", ".join(unknown),
                             {name: "Only in config.json." for name in unknown})
        changes, errors = clean_server_fields(current, fields)
        if errors:
            raise AdminError(400, "Some model server settings cannot be used.", errors)
        name = current["endpoint"]
        if ("port" in changes and changes["port"] != current["port"]
                and (model_servers.owned(name) or model_servers.busy(name))):
            raise AdminError(409, "Stop the model server before changing its port.",
                             {"port": "Stop the server first."})
        notes = []

        def mutate(config):
            for entry in local_server.raw_entries(config):
                if str(entry.get("endpoint") or "").strip().casefold() == name.casefold():
                    old_port = local_server.server_settings(entry)["port"]
                    entry.update(changes)
                    if "port" in changes and changes["port"] != old_port:
                        moved = move_endpoint_port(config, name, old_port, changes["port"])
                        if moved:
                            notes.append("The endpoint %s now uses port %d too."
                                         % (name, changes["port"]))
                        if moved == "key removed":
                            notes.append("Its stored API key was removed, as keys are only "
                                         "sent to the address they were entered for: enter "
                                         "it again if the server needs one.")
                    return config
            raise AdminError(404, "No model server is set up for the endpoint %s." % name)

        config, token = write_config(mutate, token_of(data))
        probes.forget()
        file_checks.forget()
        if model_servers.owned(name) and set(changes) & {"model", "mmproj", "slots",
                                                          "ctx_per_slot"}:
            notes.append("The running server keeps its old settings until it is started again.")
        return jsonify({"server": server_view(config, name), "notes": notes,
                        "config_token": token})

    def clean_server_fields(current, fields):
        """(changes, errors) for the UI_FIELDS sent by the page."""
        changes, errors = {}, {}
        choices = None
        for name in ("model", "mmproj"):
            if name not in fields:
                continue
            value = fields[name] if fields[name] is not None else ""
            if not isinstance(value, str):
                errors[name] = "Must be text."
                continue
            value = value.strip().replace("\\", "/")
            if value == current[name]:
                changes[name] = value
                continue
            if name == "mmproj" and local_server.embeddings_server(current):
                # (A projector makes an embeddings server an image model for looks:
                # set in config.json only, see SETTINGS.md.)
                errors[name] = ("The image projector of an embeddings server is set in "
                                "config.json only.")
                continue
            if name == "mmproj" and not value:
                changes[name] = value
                continue
            if choices is None:
                try:
                    choices = library_ops.run_with_timeout(local_server.model_choices,
                                                           library_ops.PATH_CHECK_TIMEOUT,
                                                           current)
                except library_ops.TimedOut:
                    choices = {"models": [], "mmprojs": [],
                               "error": "The models folder does not answer."}
            if value not in choices["models" if name == "model" else "mmprojs"]:
                errors[name] = ("Choose a file from the models folder."
                                + (" " + choices["error"] if choices["error"] else ""))
                continue
            changes[name] = value
        for name in ("slots", "ctx_per_slot", "port", "idle_stop_minutes"):
            if name not in fields:
                continue
            value = fields[name]
            if isinstance(value, str):
                try:
                    value = int(value.strip())
                except ValueError:
                    value = None
            low, high = local_server.LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                errors[name] = "Enter a whole number from %d to %d." % (low, high)
                continue
            changes[name] = value
        if "stop_at" in fields:
            value = fields["stop_at"] if fields["stop_at"] is not None else ""
            if not isinstance(value, str) or (value.strip()
                                              and not local_server.parse_stop_at(value)):
                errors["stop_at"] = "Enter a time such as 03:30 (24-hour clock), or nothing."
            else:
                when = local_server.parse_stop_at(value)
                changes["stop_at"] = "%02d:%02d" % when if when else ""
        for name in ("auto_start", "auto_stop"):
            if name in fields:
                if isinstance(fields[name], bool):
                    changes[name] = fields[name]
                else:
                    errors[name] = "Must be true or false."
        return changes, errors

    app.register_blueprint(bp)
    app.extensions["search_images_jobs"] = manager
    app.extensions["search_images_model_servers"] = model_servers
    return bp


def move_endpoint_port(config, name, old_port, new_port):
    """Point an endpoint that used 127.0.0.1:old_port at new_port instead.

    Only an endpoint on this computer (localhost, 127.0.0.1, ::1) at the
    old port is changed, so the endpoint keeps reaching the model server
    whose port was changed. As for any change of address, a typed API key
    is removed (settings.forget_key_if_moved): keys are only sent to the
    address they were entered for. Returns False (not changed), True, or
    "key removed".
    """
    entry = settings.find_endpoint(config, name)
    if entry is None:
        return False
    current = common.endpoint_settings(entry)
    parts = urllib.parse.urlsplit(current["base_url"])
    try:
        port = parts.port
    except ValueError:
        return False
    if (parts.hostname or "") not in ("127.0.0.1", "localhost", "::1") or port != old_port:
        return False
    host = parts.hostname if ":" not in (parts.hostname or "") else "[%s]" % parts.hostname
    old_url = current["base_url"]
    entry["base_url"] = urllib.parse.urlunsplit(
        (parts.scheme, "%s:%d" % (host, new_port), parts.path, parts.query, parts.fragment))
    return "key removed" if settings.forget_key_if_moved(entry, old_url) else True


def model_server_example(config, windows=None):
    """What to add to config.json to let the Settings page run a model server.

    {"endpoint": the local llama.cpp endpoint it would serve ('' if none),
    "port", "snippet": JSON text with placeholder paths (Windows ones on
    Windows), "windows", "config_path": where config.json is}. windows
    defaults to this computer's kind.
    """
    windows = os.name == "nt" if windows is None else windows
    endpoint = next((item for item in common.endpoint_list(config)
                     if item["preset"] in common.LLAMACPP_PRESETS
                     and common.url_host(item["base_url"]) in ("127.0.0.1", "localhost")), None)
    port = local_server.DEFAULTS["port"]
    if endpoint is not None:
        try:
            port = urllib.parse.urlsplit(endpoint["base_url"]).port or port
        except ValueError:
            pass
    if windows:
        binary, models = "C:\\path\\to\\llama-server.exe", "C:\\path\\to\\models"
    else:
        binary, models = "/path/to/llama-server", "/path/to/models"
    example = {"enabled": True, "endpoint": endpoint["name"] if endpoint else "local",
               "binary": binary, "models_dir": models,
               "model": "model.gguf", "port": port, "slots": local_server.DEFAULTS["slots"],
               "ctx_per_slot": local_server.DEFAULTS["ctx_per_slot"], "stop_at": ""}
    return {"endpoint": endpoint["name"] if endpoint else "", "port": port,
            "snippet": "\"local_server\": " + json.dumps(example, indent=4),
            "windows": bool(windows), "config_path": common.config_path()}
