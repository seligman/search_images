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

import common
import db
import export
import jobs
import library_ops
import llm
import prompts
import settings

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
HIDE_LIMIT = 10000          # pictures one hide/unhide (or paths) request may name
DESCRIBE_IDS_LIMIT = 1000   # pictures one describe job may name
ID_CHUNK = 500              # ids per SQL statement (older SQLite allows 999 parameters)
IMAGE_ID_MAX = 2 ** 63 - 1  # SQLite integers are 64-bit
# Parameters each kind of job accepts (all optional).
JOB_PARAMS = {
    "scan": ("allow_mass_removal",),
    "describe": ("retry_failed", "redescribe_stale", "limit", "endpoint", "ids", "force"),
    "process": ("allow_mass_removal", "retry_failed", "redescribe_stale", "limit", "endpoint"),
    "previews": (),
    "delete_library": ("confirm_name",),
    "export": ("options", "save_options", "out_dir"),
}
BOOL_PARAMS = ("allow_mass_removal", "retry_failed", "redescribe_stale", "force", "save_options")
EXPORT_FOLDER_TIMEOUT = 5.0  # seconds to look at an export's output folder
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
    """A token that changes whenever config.json does (its modification time and size)."""
    try:
        stat = os.stat(common.config_path())
    except OSError:
        return "none"
    return "%d-%d" % (stat.st_mtime_ns, stat.st_size)


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
    if isinstance(legacy, list):
        return [item for item in legacy if isinstance(item, dict)]
    return [legacy] if isinstance(legacy, dict) else []


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
        status=probe_view(status) if local else None)
    if isinstance(raw, dict):
        view["stored"] = redact_tree(_key_shaped({name: value for name, value in raw.items()
                                                  if name != "api_key"}), secrets)
    return view


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
    "cloud_hosts"}. ready is False when no endpoint has a model, or the only
    ones are cloud services without an API key (nothing may be sent
    without one); problem then says why. warning mentions a local model
    server that is not running (status_of(endpoint) gives its probe_view,
    when the caller knows it): describing works again once it is started.
    key_info(endpoint) gives (key, source) (default common.resolve_api_key).
    """
    key_info = key_info or common.resolve_api_key
    wanted = library.get("endpoint") or common.default_endpoint_name(config)
    endpoints = common.endpoints_for_library(config, library)
    cloud = [endpoint for endpoint in endpoints if not common.is_local_endpoint(endpoint)]
    usable, problems, warnings = [], [], []
    for endpoint in endpoints:
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
            "cloud_hosts": sorted({common.url_host(endpoint["base_url"]) for endpoint in cloud})}


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


def library_view(config, library, counts, latest, status, endpoint_info=None):
    """A library as the manage page shows it (settings, counts, status, last job).

    latest is JobManager.latest_by_library(); endpoint_info the library's
    library_endpoint_info() when the caller has worked it out.
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
    view["export_info"] = export_view(config, library)
    return view


def export_view(config, library):
    """A library's export for the Manage page.

    {"options": its saved export options with every default filled in,
    "default_output": the folder an export goes to unless another one is
    chosen, "last": export.last_export() (None before the first export)}.
    """
    return {"options": export.library_options(library),
            "default_output": export.default_output_dir(config, library),
            "last": export.last_export(library.get("id")) if library.get("id") else None}


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

def init_app(app, state, manager, allowed_hosts, enabled=True):
    """Register the admin routes on a Flask app.

    state is serve.ServerState (config, helper), manager a jobs.JobManager
    (not started here), allowed_hosts the host names requests may name.
    enabled is False when the server listens on another address than
    localhost: every admin route then answers 403.
    """
    from flask import Blueprint, g, jsonify, request
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
            views.append(endpoint_view(config, endpoint, raw, status, resolve_key(endpoint)))
        return views

    def endpoint_info(config, library):
        def status_of(endpoint):
            if not common.is_local_endpoint(endpoint):
                return None
            found = probe_status(endpoint)
            return probe_view(found) if found is not None else None
        return library_endpoint_info(config, library, resolve_key, status_of)

    def snapshot(config, since=0):
        return manager.snapshot(since, library_names(config))

    def since_arg():
        try:
            return max(0, int(request.args.get("since", 0)))
        except (TypeError, ValueError):
            return 0

    def view_of(config, library):
        return library_view(config, library, db.library_counts(get_conn(), prompts.PROMPT_VER),
                            manager.latest_by_library(), folder_status(library),
                            endpoint_info(config, library))

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
        if kind == "export" and not library_id:
            raise AdminError(400, "An export is made of one library; choose which.",
                             {"library_id": "Choose a library."})
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
        if cleaned.get("force") and "ids" not in cleaned:
            # Describing a whole library again is "Re-describe stale" (or a
            # new prompt version), never a single click.
            raise AdminError(400, "force only works with ids: the pictures to describe again.",
                             {"force": "Needs ids."})
        return cleaned

    def export_target(config, library, out_dir):
        """(output folder, default folder) of an export: out_dir when given, else the default."""
        default = export.default_output_dir(config, library)
        if out_dir is None or out_dir == "":
            return default, default
        full = library_ops.expand_path(out_dir) if isinstance(out_dir, str) else ""
        if (not library_ops.is_full_path(full) or library_ops.is_device_path(out_dir)
                or library_ops.unc_without_share(full)):
            problem = "Type the whole path of a folder, such as %s." % library_ops.FULL_PATH_EXAMPLES
            raise AdminError(400, problem, {"out_dir": problem})
        return os.path.abspath(full), default

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

        def check():
            export.check_output_dir(target, config, library["id"])
            export.check_writable(target)
            if merged["zip"]:
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
        counts = db.library_counts(conn, prompts.PROMPT_VER)
        latest = manager.latest_by_library()
        libraries = [library_view(config, library, counts, latest, folder_status(library),
                                  endpoint_info(config, library))
                     for library in config.get("libraries", []) if library.get("id")]
        return jsonify({
            "libraries": libraries,
            "orphans": library_ops.orphans(conn, config, counts),
            "endpoints": endpoint_views(config),
            "default_endpoint": common.default_endpoint_name(config),
            "presets": preset_views(),
            "jobs": snapshot(config, since_arg()),
            "config_token": config_token(),
            "version": {"schema": db.SCHEMA_VERSION, "prompt": prompts.PROMPT_VER},
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
        if kind == "export":
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
        gone = ("The folder of the last export (%s) is not there any more: it was moved or "
                "deleted, or its drive is not connected.")
        try:
            folder, missing = library_ops.run_with_timeout(
                export.last_export_dir, EXPORT_FOLDER_TIMEOUT, config, library_id)
            if folder is None:
                raise AdminError(404, "This library has not been exported yet.")
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
        counts = db.library_counts(get_conn(), prompts.PROMPT_VER)
        total = 0
        for library in common.enabled_libraries(config):
            names = [item["name"] for item in common.endpoints_for_library(config, library)]
            if name in names:
                total += counts_view(counts.get(library.get("id"), {}))["pending"]
        return total

    app.register_blueprint(bp)
    app.extensions["search_images_jobs"] = manager
    return bp
