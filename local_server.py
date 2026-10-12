#!/usr/bin/env python3
"""Start and stop a local model server (llama.cpp's llama-server) for an endpoint.

config.json may hold a "local_server" object (or a list of them, one per
endpoint) that says which endpoint the server answers for, where the
model files are, which program to run and with how many slots; see
SETTINGS.md. The Settings page starts and stops it, a describe or refine
run whose endpoint it serves starts it when it is not running
(auto_start) and stops it again afterwards (auto_stop), and with a
stop_at time the web server stops it every day at that time.

An entry with "kind": "embeddings" runs an embedding model for search by
meaning (embeddings.py): its command has --embeddings (the pooling the
model file says), a context no larger than the model's own, and no
slots, and it is ready once it answers /health. The
embed step starts it like a describe run does; the web server starts it
for the queries typed on the search page (start_for_queries) and, with
idle_stop_minutes, stops it again when no query has used it that long.
Such an entry may also name an image projector ("mmproj", never found by
itself): it then runs an image embedding model for looks (looks.py),
with --mmproj and flash attention off (-fa off: on a GPU it changes the
vectors of pictures).

The server runs as its own process group, with its output in
data/logs/model-server.log. Only a process this module started is ever
stopped: its PID and creation time are kept in data/model-server.json
(changed only while data/model-server.lock is held, as several programs
share it), so a restarted web server (or scan.py) can still stop it,
while a PID that another program has been given since is never touched.
The processes it starts belong to it too (a wrapper in launch_command may
end before the server it started): on Windows those whose parent is one
of them and that started after it, elsewhere its process group. A port
that already answers when a start is asked for is left alone.

Stopping is graceful first (Ctrl+Break to the process group on Windows,
SIGTERM on other systems), then, after GRACE_SECONDS, the whole process
tree is ended (taskkill /T /F, or SIGKILL to the process group).

The program and the command line (binary, models_dir, launch_command,
extra_args) can only be set in config.json: the browser may only choose
a model among the files in models_dir and change the numbers and
switches in UI_FIELDS.

This module uses only the standard library.
"""

import collections
import contextlib
import copy
import itertools
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import common

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None

DEFAULTS = {
    "enabled": False,
    "kind": "describe",
    "endpoint": "",
    "models_dir": "",
    "model": "",
    "mmproj": "",
    "binary": "",
    "port": 8091,
    "slots": 4,
    "ctx_per_slot": 8192,
    "extra_args": [],
    "launch_command": [],
    "auto_start": True,
    "auto_stop": True,
    "stop_at": "",
    "start_timeout": 900,
    "idle_stop_minutes": 0,
}
# What a server runs: a describing (vision) model, or an embedding model.
KINDS = ("describe", "embeddings")
# Settings the Settings page may change; the others only in config.json.
UI_FIELDS = ("model", "mmproj", "slots", "ctx_per_slot", "port", "stop_at", "auto_start",
             "auto_stop", "idle_stop_minutes")
FILE_ONLY_FIELDS = ("enabled", "kind", "endpoint", "binary", "models_dir", "launch_command",
                    "extra_args", "start_timeout")
# (minimum, maximum) of the number settings.
LIMITS = {
    "port": (1024, 65535),
    "slots": (1, 64),
    "ctx_per_slot": (512, 1048576),
    "start_timeout": (10, 86400),
    "idle_stop_minutes": (0, 10080),
}
# Words in a launch_command (or its first argument) replaced when it runs.
PLACEHOLDERS = ("{model_path}", "{mmproj_path}", "{port}", "{slots}", "{ctx}",
                "{ctx_per_slot}", "{stop_at}", "{python}")

GRACE_SECONDS = 20.0        # a graceful stop waits this long before ending the process tree
KILL_WAIT = 10.0            # and then this long for the tree to go
PORT_RELEASE_WAIT = 2.0     # after that, how long the port may take to stop answering
STATE_LOCK_WAIT = 10.0      # seconds to wait for data/model-server.lock
HEALTH_TIMEOUT = 2.0        # seconds for one /health or /props request
CONNECT_TIMEOUT = 0.3       # seconds to connect to 127.0.0.1 (see port_in_use)
START_POLL = 1.0            # seconds between /health checks while starting
PRE_STOP_MINUTES = 10       # jobs using the server stop this long before stop_at
NO_START_MINUTES = 60       # and do not start it again until this long after
SCHEDULE_SECONDS = 30.0     # how often the web server looks at the stop times
QUERY_RETRY_SECONDS = 300.0  # a server that failed to start is not started for searches this soon
LOG_TAIL_LINES = 50
LOG_TAIL_BYTES = 64 * 1024
MAX_LOG_BYTES = 5 * 1024 * 1024     # a bigger log is renamed to .1 when the server starts
MAX_MODEL_FILES = 500       # files listed from models_dir at most
STILL_RUNNING = 259         # Windows exit code of a process that has not exited
STATES = ("not configured", "off", "stopped", "starting", "loading", "ready", "stopping",
          "error", "external")
# Arguments whose value is a secret (masked in logs and on the page).
SECRET_ARGS = ("--api-key", "--api-key-file", "--hf-token", "-hft")

MODEL_EXTENSION = ".gguf"
# A model's own projector next to it: "<model name without .gguf>" + this.
MMPROJ_SUFFIX = ".mmproj" + MODEL_EXTENSION
_SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Requests to the server on this computer never go through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ServerError(Exception):
    """The model server cannot be started or stopped; the message says why."""


# ---------------------------------------------------------------------------
# Settings

def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _int(value, name):
    low, high = LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULTS[name]
    if value != int(value) or not low <= value <= high:
        return DEFAULTS[name]
    return int(value)


def _string_list(value):
    if not isinstance(value, list):
        return []
    return [str(item) for item in value
            if isinstance(item, (str, int, float)) and not isinstance(item, bool)]


def parse_stop_at(text):
    """(hour, minute) from "HH:MM" (24-hour clock), or None."""
    match = _TIME_RE.match(_text(text))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def server_settings(entry):
    """A copy of one local_server entry with every setting filled in and checked.

    Values of the wrong type or out of range fall back to DEFAULTS; a
    stop_at that is not HH:MM becomes "" (none).
    """
    entry = entry if isinstance(entry, dict) else {}
    result = copy.deepcopy(DEFAULTS)
    result["enabled"] = entry.get("enabled") is True
    for key in ("endpoint", "models_dir", "model", "mmproj", "binary"):
        result[key] = _text(entry.get(key))
    for key in ("port", "slots", "ctx_per_slot", "start_timeout", "idle_stop_minutes"):
        if key in entry:
            result[key] = _int(entry[key], key)
    kind = _text(entry.get("kind")).lower()
    result["kind"] = kind if kind in KINDS else DEFAULTS["kind"]
    for key in ("extra_args", "launch_command"):
        result[key] = _string_list(entry.get(key))
    for key in ("auto_start", "auto_stop"):
        if isinstance(entry.get(key), bool):
            result[key] = entry[key]
    stop_at = parse_stop_at(entry.get("stop_at"))
    result["stop_at"] = "%02d:%02d" % stop_at if stop_at else ""
    result["model"] = result["model"].replace("\\", "/")
    result["mmproj"] = result["mmproj"].replace("\\", "/")
    return result


def raw_entries(config):
    """The local_server entries as stored: [] , [the object] or the list's objects."""
    value = config.get("local_server") if isinstance(config, dict) else None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def server_list(config):
    """Every configured server (server_settings), one per endpoint name.

    Entries without an endpoint are left out, and so is a second entry for
    an endpoint that already has one. Turned-off entries are included.
    """
    result, seen = [], set()
    for entry in raw_entries(config):
        settings = server_settings(entry)
        key = settings["endpoint"].casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(settings)
    return result


def find_server(config, name):
    """The server settings for an endpoint name (case-insensitive), or None."""
    wanted = _text(name).casefold()
    for settings in server_list(config):
        if settings["endpoint"].casefold() == wanted:
            return settings
    return None


def managed(config, name):
    """The settings of the turned-on server that serves this endpoint, or None."""
    settings = find_server(config, name)
    return settings if settings is not None and settings["enabled"] else None


def _single(config):
    """True when config.json has one local_server object (not a list)."""
    return isinstance(config, dict) and isinstance(config.get("local_server"), dict)


def log_path(config, name):
    """data/logs/model-server.log (model-server-<endpoint>.log when there are several)."""
    if _single(config):
        file_name = "model-server.log"
    else:
        file_name = "model-server-%s.log" % (_SLUG_RE.sub("-", name).strip("-.") or "server")
    return os.path.join(common.logs_dir(), file_name)


# ---------------------------------------------------------------------------
# Files and the command line

def models_root(settings):
    """The models folder as a full path ('' when not set). Relative: next to config.json."""
    folder = settings["models_dir"]
    if not folder:
        return ""
    folder = os.path.expanduser(common.unquote_path(folder))
    if not os.path.isabs(folder):
        folder = common.resolve_config_relative(folder)
    return os.path.normpath(os.path.abspath(folder))


def _is_mmproj(name):
    return "mmproj" in name.lower()


def _skip_part(name):
    """True for the second and later parts of a split model (...-00002-of-00003 and so on)."""
    match = _SPLIT_RE.search(name)
    return bool(match) and int(match.group(1)) != 1


def model_choices(settings, limit=MAX_MODEL_FILES):
    """The .gguf files in models_dir and its sub-folders (one level down).

    Returns {"models": [...], "mmprojs": [...], "error": ""}, paths relative
    to models_dir with "/" separators; files with "mmproj" in their name
    are vision projectors, the others models (of a split model only its
    first part). error says why nothing could be listed.
    """
    result = {"models": [], "mmprojs": [], "error": ""}
    root = models_root(settings)
    if not root:
        result["error"] = "models_dir is not set in config.json."
        return result
    if not os.path.isdir(root):
        result["error"] = "The models folder %s does not exist (or cannot be reached)." % root
        return result
    found = []

    def scan(folder, prefix, depth):
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(found) >= limit:
                return
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth == 0 and not entry.name.startswith("."):
                        scan(entry.path, prefix + entry.name + "/", depth + 1)
                elif entry.name.lower().endswith(MODEL_EXTENSION) and entry.is_file():
                    found.append(prefix + entry.name)
            except OSError:
                continue

    scan(root, "", 0)
    for rel in found:
        name = rel.rsplit("/", 1)[-1]
        if _is_mmproj(name):
            result["mmprojs"].append(rel)
        elif not _skip_part(name):
            result["models"].append(rel)
    return result


def _inside(root, rel):
    """The full path of rel inside root, or None when rel would leave it."""
    if not root or not rel:
        return None
    full = os.path.normpath(os.path.join(root, *rel.split("/")))
    try:
        if os.path.commonpath([os.path.normcase(full), os.path.normcase(root)]) != \
                os.path.normcase(root):
            return None
    except ValueError:
        return None
    return full


def model_path(settings):
    """The full path of the chosen model; raises ServerError when it cannot be used."""
    if not settings["model"]:
        raise ServerError("No model is chosen: pick one on the Settings page (or set \"model\" "
                          "in config.json).")
    full = _inside(models_root(settings), settings["model"])
    if full is None:
        raise ServerError("The model %s is not inside the models folder." % settings["model"])
    if not os.path.isfile(full):
        raise ServerError("The model file %s does not exist." % full)
    return full


def embeddings_server(settings):
    """True for a server that runs an embedding model ("kind": "embeddings")."""
    return settings.get("kind") == "embeddings"


def auto_mmproj(settings):
    """The vision projector found for the model (a path relative to models_dir), or ''.

    Next to the model: its name with MMPROJ_SUFFIX in place of the
    extension (model.mmproj.gguf for model.gguf); for a model in a sub-folder of
    models_dir also the (first, by name) file there with "mmproj" in its
    name. An embeddings server has none (its "mmproj" is only used when set).
    """
    root = models_root(settings)
    model = settings["model"]
    if not root or not model or embeddings_server(settings):
        return ""
    folder, _, name = model.rpartition("/")
    stem = name[:-len(MODEL_EXTENSION)] if name.lower().endswith(MODEL_EXTENSION) else name
    prefix = folder + "/" if folder else ""
    sibling = prefix + stem + MMPROJ_SUFFIX
    full = _inside(root, sibling)
    if full is not None and os.path.isfile(full):
        return sibling
    if folder:
        try:
            names = sorted(os.listdir(_inside(root, folder) or ""), key=str.lower)
        except OSError:
            names = []
        for other in names:
            if _is_mmproj(other) and other.lower().endswith(MODEL_EXTENSION):
                return prefix + other
    return ""


def mmproj_path(settings):
    """The full path of the vision projector ('' for none); raises ServerError for a bad one.

    An embeddings server has one only when its "mmproj" names it (an image
    embedding model, see looks.py).
    """
    chosen = settings["mmproj"] if embeddings_server(settings) else (
        settings["mmproj"] or auto_mmproj(settings))
    if not chosen:
        return ""
    full = _inside(models_root(settings), chosen)
    if full is None:
        raise ServerError("The projector %s is not inside the models folder." % chosen)
    if not os.path.isfile(full):
        raise ServerError("The projector file %s does not exist." % full)
    return full


def binary_path(settings):
    """The llama-server program to run (a full path when it was given relative to config.json)."""
    binary = common.unquote_path(settings["binary"])
    if not binary:
        return ""
    binary = os.path.expanduser(binary)
    if os.path.isabs(binary):
        return binary
    if os.sep in binary or "/" in binary:
        return common.resolve_config_relative(binary)
    return binary       # a program name found on the PATH


def model_context(path):
    """The context length a GGUF model file was trained with (<arch>.context_length), or
    None when the file does not say (or cannot be read)."""
    import browser_model     # stdlib only
    try:
        meta = browser_model.read_gguf(path)["meta"]
    except browser_model.ModelError:
        return None
    value = meta.get("%s.context_length" % meta.get("general.architecture"))
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def build_command(settings):
    """The command line that starts the server (a list); raises ServerError.

    launch_command, when set, is used as it is with its placeholders
    replaced ({model_path}, {mmproj_path}, {port}, {slots}, {ctx},
    {ctx_per_slot}, {stop_at} ('' when there is none), {python});
    otherwise binary is started with the model, its projector,
    127.0.0.1:port, slots, slots * ctx_per_slot of context and extra_args.
    An embeddings server gets --embeddings (llama-server then pools as the
    model file says: mean for mdbr-leaf-ir, CLS for snowflake-arctic-embed,
    last for Qwen3-Embedding) and ctx_per_slot of context, or the model's own
    context when that is smaller (512 for small BERT models), also as its
    batch sizes so a long text fits in one batch; no slots, and a projector
    only when its mmproj is set (an image embedding model): then also
    -fa off, as flash attention changes the vectors of pictures on a GPU.
    {ctx} is that context for it.
    """
    model = model_path(settings)
    mmproj = mmproj_path(settings)
    embedding = embeddings_server(settings)
    ctx = settings["ctx_per_slot"] if embedding else settings["slots"] * settings["ctx_per_slot"]
    if embedding:
        ctx = min(ctx, model_context(model) or ctx)
    if settings["launch_command"]:
        values = {"{model_path}": model, "{mmproj_path}": mmproj,
                  "{port}": str(settings["port"]), "{slots}": str(settings["slots"]),
                  "{ctx}": str(ctx), "{ctx_per_slot}": str(settings["ctx_per_slot"]),
                  "{stop_at}": settings["stop_at"], "{python}": sys.executable or "python"}
        command = []
        for arg in settings["launch_command"]:
            for placeholder, value in values.items():
                arg = arg.replace(placeholder, value)
            command.append(arg)
        return command
    binary = binary_path(settings)
    if not binary:
        raise ServerError("No program is set: add \"binary\" (the path of llama-server) or "
                          "\"launch_command\" to local_server in config.json.")
    command = [binary, "-m", model]
    if embedding:
        command += ["--embeddings", "--host", "127.0.0.1",
                    "--port", str(settings["port"]), "-c", str(ctx), "-ub", str(ctx),
                    "-b", str(ctx), "--no-webui"]
        if mmproj:
            command += ["--mmproj", mmproj, "-fa", "off"]
        return command + list(settings["extra_args"])
    if mmproj:
        command += ["--mmproj", mmproj]
    command += ["--host", "127.0.0.1", "--port", str(settings["port"]),
                "-np", str(settings["slots"]), "-c", str(ctx), "-fa", "on",
                "--reasoning", "off", "--no-webui"]
    return command + list(settings["extra_args"])


def masked_args(command, secrets=()):
    """The arguments with secret ones (an --api-key value, keys) masked, as a list."""
    shown = []
    hide_next = False
    for arg in command:
        if hide_next:
            shown.append(common.REDACTED)
            hide_next = False
            continue
        name, equals, _value = arg.partition("=")
        if name in SECRET_ARGS:
            if equals:
                shown.append(name + "=" + common.REDACTED)
            else:
                shown.append(arg)
                hide_next = True
            continue
        shown.append(common.redact(arg, secrets))
    return shown


def masked_command(command, secrets=()):
    """The command as text for logs and the page: secret arguments and keys masked."""
    return " ".join(('"%s"' % arg) if " " in arg else arg
                    for arg in masked_args(command, secrets))


def command_secrets(settings):
    """The values of secret arguments (SECRET_ARGS) in launch_command and extra_args."""
    found = []
    for args in (settings.get("launch_command") or [], settings.get("extra_args") or []):
        for index, arg in enumerate(args):
            name, equals, value = arg.partition("=")
            if name in SECRET_ARGS:
                if not equals and index + 1 < len(args):
                    value = args[index + 1]
                if value and value not in found:
                    found.append(value)
    return found


# A secret argument and its value in text (a program that echoes its own
# command line puts them in the log): "--api-key VALUE" or "--api-key=VALUE".
_SECRET_ARG_RE = re.compile(r"(?<![\w-])((?:%s)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)" % "|".join(
    re.escape(arg) for arg in sorted(SECRET_ARGS, key=len, reverse=True)))


def mask_secret_args(text):
    """text with the value after every secret argument (SECRET_ARGS) masked."""
    return _SECRET_ARG_RE.sub(lambda match: match.group(1) + common.REDACTED, text)


def endpoint_problem(config, settings):
    """Why the server's endpoint cannot use it, or ''.

    The endpoint must exist and send its requests to this computer at the
    server's port.
    """
    endpoint = common.get_endpoint(config, settings["endpoint"])
    if endpoint is None:
        return ("There is no endpoint called %s: add it on the Settings page (base URL "
                "http://127.0.0.1:%d/v1), or name another one in local_server."
                % (settings["endpoint"], settings["port"]))
    url = endpoint["base_url"]
    host = common.url_host(url)
    try:
        port = urllib.parse.urlsplit(url).port
    except ValueError:
        port = None
    if host not in ("127.0.0.1", "localhost", "::1") or port != settings["port"]:
        return ("The endpoint %s sends its requests to %s, but the server starts on port %d "
                "of this computer. Set the endpoint's base URL to http://127.0.0.1:%d/v1, or "
                "change the port here." % (endpoint["name"], common.redact(url), settings["port"],
                                            settings["port"]))
    return ""


def file_facts(settings):
    """What a server's files say: the parts of its status that look at models_dir.

    {"problem": why it cannot be started with these files ('' if it can),
    "command": its command line (a list; None when it cannot be built),
    "mmproj_auto": the projector found when mmproj is ''}. This can be
    slow when models_dir is on a share (the web server works it out in the
    background).
    """
    problem, command = "", None
    try:
        command = build_command(settings)
    except ServerError as error:
        problem = str(error)
    if command and not settings["launch_command"]:
        binary = command[0]
        if os.path.isabs(binary) and not os.path.isfile(binary):
            problem = "The program %s does not exist." % binary
    return {"problem": problem, "command": command,
            "mmproj_auto": auto_mmproj(settings) if not settings["mmproj"] else ""}


def setup_problem(config, settings, facts=None):
    """Why the server cannot be started with these settings, or ''.

    facts is file_facts(settings) when the caller has it.
    """
    if not settings["enabled"]:
        return "The model server is turned off: set \"enabled\": true in local_server."
    problem = endpoint_problem(config, settings)
    if problem:
        return problem
    facts = file_facts(settings) if facts is None else facts
    return facts.get("problem") or ""


# ---------------------------------------------------------------------------
# Stop times

def _at(day, hour_minute):
    return datetime(day.year, day.month, day.day, hour_minute[0], hour_minute[1])


def stop_window(settings, now=None):
    """The stop time whose quiet window holds now, or None.

    The window runs from PRE_STOP_MINUTES before stop_at until
    NO_START_MINUTES after it: jobs that use the server do not run then,
    and it is not started for them.
    """
    when = parse_stop_at(settings.get("stop_at"))
    if when is None:
        return None
    now = now or datetime.now()
    for offset in (-1, 0, 1):
        stop = _at(now + timedelta(days=offset), when)
        if (stop - timedelta(minutes=PRE_STOP_MINUTES) <= now
                < stop + timedelta(minutes=NO_START_MINUTES)):
            return stop
    return None


def next_pre_stop(settings, now=None):
    """When jobs using the server must next stop (PRE_STOP_MINUTES before stop_at), or None."""
    when = parse_stop_at(settings.get("stop_at"))
    if when is None:
        return None
    now = now or datetime.now()
    for offset in (0, 1, 2):
        moment = _at(now + timedelta(days=offset), when) - timedelta(minutes=PRE_STOP_MINUTES)
        if moment > now:
            return moment
    return None


def window_message(settings, stop):
    """Why a job may not use the server now (inside the stop_at window)."""
    resume = stop + timedelta(minutes=NO_START_MINUTES)
    return ("The model server for %s stops at %s every day, so it is not used from %s until "
            "%s. Run the job again after that (or clear the stop time on the Settings page)."
            % (settings["endpoint"], stop.strftime("%H:%M"),
               (stop - timedelta(minutes=PRE_STOP_MINUTES)).strftime("%H:%M"),
               resume.strftime("%H:%M")))


# ---------------------------------------------------------------------------
# Processes (stdlib only; never os.kill(pid, 0) on Windows: that ends the process)

def _windows_identity(pid):
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
    if not handle:
        return None
    try:
        if kernel32.WaitForSingleObject(handle, 0) != 0x102:     # WAIT_TIMEOUT: still running
            return None
        times = [FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
            return None
        return "w%d" % ((times[0].high << 32) | times[0].low)
    finally:
        kernel32.CloseHandle(handle)


def _posix_identity(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass            # someone else's process: it exists, but is not ours
    except OSError:
        return None
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return "p" + fields[19]
    except (OSError, IndexError):
        pass
    try:
        output = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return ("s" + output) if output else None


def process_identity(pid):
    """A text that tells this process apart from a later one with the same PID, or None.

    None when no such process is running. On Windows it is the creation
    time, on Linux the start time from /proc, elsewhere ps's start time.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if os.name == "nt":
            return _windows_identity(pid)
        return _posix_identity(pid)
    except Exception:
        return None


def is_running(pid, identity):
    """True when the process with this PID is running and is the one recorded."""
    return bool(identity) and process_identity(pid) == identity


def _created(identity):
    """The creation time in a Windows identity ("w<FILETIME>"), or None."""
    if isinstance(identity, str) and identity.startswith("w") and identity[1:].isdigit():
        return int(identity[1:])
    return None


def _windows_parents():
    """{pid: parent pid} of every running process (a Toolhelp snapshot), or {}."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:    # INVALID_HANDLE_VALUE
        return {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        parents = {}
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return parents
    finally:
        kernel32.CloseHandle(snapshot)


def _posix_group(pgid):
    """[(pid, identity)] of the processes (not zombies) in a process group."""
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        names = None
    if names is not None:
        for name in names:
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % name, "r", encoding="utf-8",
                          errors="replace") as handle:
                    fields = handle.read().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if len(fields) > 19 and fields[0] != "Z" and fields[2] == str(pgid):
                found.append((int(name), "p" + fields[19]))
        return found
    try:
        output = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True,
                                text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == str(pgid) and parts[0].isdigit():
            identity = process_identity(int(parts[0]))
            if identity:
                found.append((int(parts[0]), identity))
    return found


def find_tree(pid, identity, members=(), pinned=False):
    """[(pid, identity)] of a recorded server's processes that still run.

    The server itself (when its PID still names the process recorded),
    the processes recorded as its members earlier (members: [pid,
    identity] pairs), and what any of those that runs has started: on
    Windows the processes whose parent it is and that were created after
    it (a live process's PID is never reused, so this cannot adopt a
    stranger), elsewhere the processes in the server's process group
    (its PID, as it started a session of its own) while one of them runs.
    A member that has ended is not followed: record members while they
    run (see ServerManager), so that a wrapper that ends before the server
    it started does not leave it behind. pinned: the caller started the
    server and still holds it (a subprocess.Popen), so on Windows its PID
    is not given to another process even after it has ended, and what it
    started is still found through it.
    """
    known = [(pid, identity)] + [tuple(pair) for pair in members or ()
                                 if isinstance(pair, (list, tuple)) and len(pair) == 2]
    live = [(number, ident) for number, ident in known
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
            and ident and process_identity(number) == ident]
    roots = list(live)
    if pinned and os.name == "nt" and (pid, identity) not in roots and _created(identity):
        roots.append((pid, identity))
    if not roots:
        return []
    found = dict(live)
    try:
        _descendants(found, pid, roots)
    except Exception:
        pass            # (what is known to run is still reported)
    return sorted(found.items())


def _descendants(found, pid, roots):
    """Add what the roots started to found ({pid: identity}); see find_tree."""
    if os.name == "nt":
        parents = _windows_parents()
        frontier = [(number, _created(ident)) for number, ident in roots]
        while frontier:
            parent, lower = frontier.pop()
            if lower is None:
                continue
            for child, its_parent in parents.items():
                if its_parent != parent or child in found or child == parent or child == pid:
                    continue
                child_identity = process_identity(child)
                created = _created(child_identity)
                if created is None or created < lower:
                    continue        # (a child of an earlier process with that PID)
                found[child] = child_identity
                frontier.append((child, created))
    elif roots:         # (only processes that run: pinning is for Windows)
        for number, ident in _posix_group(pid):
            found.setdefault(number, ident)


# Run in a separate Python process: it attaches to the server's (hidden)
# console and sends Ctrl+Break to the server's process group. Doing this
# here would detach the web server from its own console. The console is
# reached through a process of the group that still runs (argv[2]).
_CTRL_BREAK_CODE = r"""
import ctypes, sys
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
group, attach = int(sys.argv[1]), int(sys.argv[2])
kernel32.FreeConsole()
if not kernel32.AttachConsole(attach):
    sys.exit(2)
kernel32.SetConsoleCtrlHandler(None, True)
sent = kernel32.GenerateConsoleCtrlEvent(1, group)
kernel32.FreeConsole()
sys.exit(0 if sent else 3)
"""


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def send_graceful_stop(pid, identity=None, members=()):
    """Ask a process group to stop: Ctrl+Break on Windows, SIGTERM elsewhere. True if sent.

    With identity (the leader's), only the recorded server's processes
    (find_tree) are asked, also after the leader itself has ended.
    """
    try:
        tree = find_tree(pid, identity, members) if identity else [(pid, None)]
        if not tree:
            return False
        if os.name == "nt":
            if not sys.executable:
                return False
            # The leader's console first; some processes (such as the console's
            # own conhost.exe) cannot be attached to: the next one is tried then.
            targets = sorted((number for number, _ident in tree), key=lambda item: item != pid)
            for target in targets[:5]:
                done = subprocess.run([sys.executable, "-I", "-c", _CTRL_BREAK_CODE, str(pid),
                                       str(target)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=_no_window(),
                                      timeout=15)
                if done.returncode != 2:        # 2: could not attach to its console
                    return done.returncode == 0
            return False
        sent = False
        if not identity or is_running(pid, identity):
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            return True
        for number, ident in tree:
            if is_running(number, ident):
                os.kill(number, signal.SIGTERM)
                sent = True
        return sent
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def kill_tree(pid, identity=None, members=()):
    """End a process and every process it started (taskkill /T /F, or SIGKILL).

    With identity (the leader's), exactly the recorded server's processes
    (find_tree) are ended, also after the leader itself has ended (a few
    rounds, for what they start meanwhile); without, the process pid and
    its process tree (or group).
    """
    def end(number, whole):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(number), "/F"] + (["/T"] if whole else []),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=_no_window(), timeout=30)
        elif number == pid and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(number, signal.SIGKILL)

    try:
        if not identity:
            end(pid, True)
            return
        for _round in range(3):
            tree = find_tree(pid, identity, members)
            if not tree:
                return
            for number, ident in tree:
                if is_running(number, ident):
                    try:
                        end(number, False)
                    except (OSError, subprocess.SubprocessError, ValueError):
                        pass
            time.sleep(0.2)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Talking to the server

def _get(port, path, timeout=HEALTH_TIMEOUT):
    """(status, body text) of GET http://127.0.0.1:port/path; status None without an answer."""
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     headers={"User-Agent": "search-images"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read(65536).decode("utf-8", "replace")
        except OSError:
            return error.code, ""
        finally:
            error.close()
    except (OSError, ValueError, urllib.error.URLError):
        return None, ""
    except Exception:
        return None, ""


def health(port, timeout=HEALTH_TIMEOUT):
    """The HTTP status of /health (200 ready, 503 loading), or None when nothing answers.

    A port nothing listens on is found out at once (port_in_use), not
    after the seconds Windows takes to refuse a connection.
    """
    if not port_in_use(port):
        return None
    return _get(port, "/health", timeout)[0]


def props(port, timeout=HEALTH_TIMEOUT):
    """{"model", "slots", "vision"} from llama.cpp's /props (values None when unknown)."""
    result = {"model": None, "slots": None, "vision": None}
    status, body = _get(port, "/props", timeout)
    if status != 200:
        return result
    try:
        data = json.loads(body)
    except ValueError:
        return result
    if not isinstance(data, dict):
        return result
    for key in ("model_path", "model_alias", "model"):
        if isinstance(data.get(key), str) and data[key]:
            result["model"] = data[key].replace("\\", "/").split("/")[-1]
            break
    slots = data.get("total_slots")
    if isinstance(slots, int) and not isinstance(slots, bool):
        result["slots"] = slots
    modalities = data.get("modalities")
    if isinstance(modalities, dict) and isinstance(modalities.get("vision"), bool):
        result["vision"] = modalities["vision"]
    return result


def port_in_use(port, timeout=None):
    """True when something accepts connections on 127.0.0.1:port.

    A server on this computer accepts a connection at once, so a short
    timeout (CONNECT_TIMEOUT) is enough.
    """
    timeout = CONNECT_TIMEOUT if timeout is None else timeout
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def log_tail(path, lines=LOG_TAIL_LINES, secrets=(), start=None):
    """The last lines of a log file (redacted), or [].

    Secret arguments a program echoes ("--api-key VALUE") are masked, and so
    are the secrets given. With start (a position in the file: its size
    when a server was started), only what was written after it is read,
    without the lines this module writes itself (===== and $ lines).
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            begin = max(0, size - LOG_TAIL_BYTES)
            if start is not None:
                begin = max(begin, min(start, size))
            handle.seek(begin)
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    found = text.split("\n")
    if begin > 0 and begin != start and found:
        found = found[1:]           # the first line is probably cut
    found = [line for line in found if line.strip()]
    if start is not None:
        found = [line for line in found if not line.startswith(("=====", "$ "))]
    return [common.redact(mask_secret_args(line), secrets)[:500] for line in found[-lines:]]


# ---------------------------------------------------------------------------
# The record of started servers (data/model-server.json)

def state_path():
    return os.path.join(common.data_dir(), "model-server.json")


def load_state():
    """{endpoint name (lower case): record} of the servers started here and not yet stopped."""
    try:
        with open(state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return {key: value for key, value in (servers or {}).items() if isinstance(value, dict)}


def save_state(servers):
    common.ensure_dir(common.data_dir())
    text = json.dumps({"servers": servers}, indent=2, ensure_ascii=True) + "\n"
    common.write_file_atomic(state_path(), text, fsync=False)


_lock_depth = threading.local()


def _lock_file(fd, unlock=False):
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    elif fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextlib.contextmanager
def state_lock(timeout=None):
    """Hold data/model-server.lock while data/model-server.json is read and changed.

    The web server, its jobs and scan.py all change that file; without
    the lock one could save over a record another has just added. Waits
    up to timeout (STATE_LOCK_WAIT) seconds, and then goes on without it
    (a stuck holder must not keep a server from being stopped). The
    operating system frees the lock of a program that ends. Re-entrant
    within a thread. Yields True when the lock is held.
    """
    depth = getattr(_lock_depth, "value", 0)
    if depth:
        _lock_depth.value = depth + 1
        try:
            yield True
        finally:
            _lock_depth.value = depth
        return
    fd = None
    locked = False
    try:
        common.ensure_dir(common.data_dir())
        fd = os.open(os.path.join(common.data_dir(), "model-server.lock"),
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    except OSError:
        fd = None
    if fd is not None:
        deadline = time.monotonic() + (STATE_LOCK_WAIT if timeout is None else timeout)
        while True:
            try:
                _lock_file(fd)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
    _lock_depth.value = 1
    try:
        yield locked
    finally:
        _lock_depth.value = 0
        if fd is not None:
            if locked:
                try:
                    _lock_file(fd, unlock=True)
                except OSError:
                    pass
            os.close(fd)


def update_state(change):
    """Change the record under state_lock: change(servers) edits it; saved if it returns True."""
    with state_lock():
        servers = load_state()
        if change(servers):
            save_state(servers)


# ---------------------------------------------------------------------------
# The manager

def _append_log(path, text):
    """Add a line of our own to the server's log (best effort)."""
    if not path:
        return
    try:
        with open(path, "ab") as handle:
            handle.write(text.encode("utf-8", "replace"))
    except OSError:
        pass


class ServerManager:
    """Starts, watches and stops the configured model servers of one data folder.

    One per process (see default_manager); the web server, its jobs and
    scan.py each use theirs, and they share data/model-server.json, so any
    of them can stop a server another one started. Thread-safe.

    Servers are told apart by their endpoint's name (case-insensitive).
    A start or stop in progress is an "operation" that status() reports
    (starting, loading, stopping) and that other callers can wait for.
    """

    def __init__(self, clock=None):
        self._lock = threading.RLock()
        self._procs = {}            # key -> subprocess.Popen started by this process
        self._records = {}          # key -> the record of a server this process started
        self._ops = {}              # key -> {"state", "message", "done" (Event), "ok"}
        self._notes = {}            # key -> (state, message) after the last start or stop
        self._stopping = set()      # keys whose start was cut short by a stop
        self._leases = collections.defaultdict(set)
        self._query_started = set()  # keys of servers started here for search queries
        self._last_used = {}        # key -> time.monotonic() of the last query that used it
        self._failed = {}           # key -> (time.monotonic(), message) of the last failed start
        self._identity = None       # process_identity() of this process (see _mark_user)
        self._user = "%d.%d" % (os.getpid(), next(_MANAGER_NUMBERS))    # (see _mark_user)
        self._clock = clock or datetime.now
        self._scheduler = None
        self._scheduler_stop = threading.Event()

    # -- records --------------------------------------------------------------

    @staticmethod
    def _key(name):
        return _text(name).casefold()

    def _record(self, key):
        """The record of a running server started here, or None (a stale record is removed).

        A server counts as running while it, or a process of it, runs
        (find_tree; what it has started is recorded as it goes, see
        _track). One this process started is trusted even when the file
        lost its record (it is written again then).
        """
        with self._lock:
            proc = self._procs.get(key)
            mine = self._records.get(key)
            if mine is not None:
                tree = self._track(key, mine, pinned=proc is not None)
                if (proc is not None and proc.poll() is None) or tree:
                    entry = load_state().get(key)
                    if entry is None or entry.get("pid") != mine["pid"]:
                        def restore(servers):
                            entry = servers.get(key)
                            if entry is not None and entry.get("pid") == mine["pid"]:
                                return False
                            servers[key] = mine
                            return True
                        try:
                            update_state(restore)
                        except OSError:
                            pass
                    return mine
            record = load_state().get(key)
            if record is None:
                if mine is not None:
                    self._forget(key)
                return None
            if not (mine is not None and record.get("pid") == mine["pid"]) and \
                    self._track(key, record):
                return record
            self._clear_record(key, record.get("pid"))
            return None

    def _track(self, key, record, pinned=False):
        """The server's processes that run (find_tree); new ones are added to its record.

        So a process the server started stays known as part of it after
        the process that started it ends (a wrapper that leaves before the
        llama-server it started).
        """
        pid = record.get("pid")
        tree = find_tree(pid, record.get("identity"), record.get("members") or (), pinned)
        if not tree:
            return tree
        members = sorted([number, ident] for number, ident in tree if number != pid)
        if members != sorted(list(pair) for pair in record.get("members") or ()):
            record["members"] = members

            def change(servers):
                entry = servers.get(key)
                if entry is None or entry.get("pid") != pid:
                    return False
                entry["members"] = members
                return True
            try:
                update_state(change)
            except OSError:
                pass
        return tree

    def _forget(self, key):
        """Let go of this process's handle on a server (it is reaped on POSIX)."""
        with self._lock:
            proc = self._procs.pop(key, None)
            self._records.pop(key, None)
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def _clear_record(self, key, pid=None):
        """Remove a server's record (only the one of that PID, when pid is given)."""
        def remove(servers):
            entry = servers.get(key)
            if entry is None or (pid is not None and entry.get("pid") != pid):
                return False
            del servers[key]
            return True
        try:
            update_state(remove)
        except OSError:
            pass
        with self._lock:
            mine = self._records.get(key)
            if mine is None or pid is None or mine["pid"] == pid:
                self._forget(key)

    def owned(self, name):
        """True while a server started here (by this or another process) is running."""
        return self._record(self._key(name)) is not None

    # -- operations -----------------------------------------------------------

    def _begin_op(self, key, state, message):
        """Start an operation; returns its dict, or None when another one is under way."""
        with self._lock:
            if key in self._ops:
                return None
            op = {"state": state, "message": message, "done": threading.Event(), "ok": False}
            self._ops[key] = op
            self._notes.pop(key, None)
            return op

    def _set_op(self, key, state, message):
        with self._lock:
            op = self._ops.get(key)
            if op is not None and not op.get("cancelled"):     # (a stop took it over)
                op.update(state=state, message=message)

    def _end_op(self, key, ok, state, message):
        with self._lock:
            op = self._ops.pop(key, None)
            self._notes[key] = (state, message)
            if ok:
                self._failed.pop(key, None)
            elif state == "error":
                self._failed[key] = (time.monotonic(), message)
        if op is not None:
            op.update(ok=ok, message=message)
            op["done"].set()

    def _current_op(self, key):
        with self._lock:
            return self._ops.get(key)

    def busy(self, name):
        """True while a start or stop of this endpoint's server is under way here."""
        return self._current_op(self._key(name)) is not None

    # -- status ---------------------------------------------------------------

    def status(self, config, name, facts=None):
        """What the Settings page shows about one server.

        facts is file_facts() of its settings when the caller has it (the
        web server works it out in the background, as models_dir may be on
        a share); {"checking": True} while that is not done yet.

        {"endpoint", "configured", "state" (one of STATES), "message",
        "problem" (why it cannot be started, '' if it can), "checking",
        every setting (launch_command and extra_args masked), "command"
        (masked), "endpoint_exists", "endpoint_url", "owned", "pid",
        "started", "started_by", "served" ({model, slots, vision} while
        ready), "external", "in_use_by" (runs using it), "mmproj_auto" (the
        projector found when mmproj is ''), "stop_window" (a message while
        jobs may not use it), "log_tail"}.
        """
        key = self._key(name)
        settings = find_server(config, name)
        if settings is None:
            return {"endpoint": name, "configured": False, "state": "not configured",
                    "message": "", "problem": ""}
        facts = file_facts(settings) if facts is None else facts
        endpoint = common.get_endpoint(config, settings["endpoint"])
        secrets = list(command_secrets(settings))
        if endpoint is not None:
            api_key = common.resolve_api_key(endpoint)[0]
            if api_key:
                secrets.append(api_key)
        view = {key_name: settings[key_name] for key_name in DEFAULTS}
        view.update(configured=True, endpoint_exists=endpoint is not None,
                    endpoint_url=common.redact(endpoint["base_url"], secrets)
                    if endpoint is not None else "",
                    problem=setup_problem(config, settings, facts),
                    checking=bool(facts.get("checking")), pid=None, started=None,
                    started_by="", owned=False, served=None, external=None,
                    log_tail=log_tail(log_path(config, settings["endpoint"]), secrets=secrets),
                    in_use_by=sorted(lease.by for lease in self.leases(name)),
                    mmproj_auto=facts.get("mmproj_auto") or "",
                    command="", message="", state="stopped")
        view["launch_command"] = masked_args(settings["launch_command"], secrets)
        view["extra_args"] = masked_args(settings["extra_args"], secrets)
        if facts.get("command"):
            view["command"] = masked_command(facts["command"], secrets)
        stop = stop_window(settings, self._clock())
        view["stop_window"] = window_message(settings, stop) if stop is not None else ""
        record = self._record(key)
        if record is not None:
            view.update(owned=True, pid=record.get("pid"), started=record.get("started"),
                        started_by=record.get("by", ""))
        op = self._current_op(key)
        if op is not None:
            view.update(state=op["state"], message=op["message"])
            return view
        if not settings["enabled"] and record is None:
            view.update(state="off", message=view["problem"])
            return view
        listening = port_in_use(settings["port"])
        code = _get(settings["port"], "/health")[0] if listening else None
        if record is not None:
            if code == 200:
                view.update(state="ready", served=props(settings["port"]))
            elif code == 503:
                view.update(state="loading", message="The server is loading its model.")
            else:
                view.update(state="starting", message="The server is starting.")
            return view
        if listening:
            view.update(state="external", served=props(settings["port"]) if code == 200 else None,
                        external={"status": code},
                        message="Port %d already answers, but that server was not started "
                                "here, so it cannot be stopped here either. %s uses it as it "
                                "is." % (settings["port"], ("Looks" if settings["mmproj"]
                                                             else "Search by meaning")
                                         if embeddings_server(settings) else "Describing"))
            return view
        with self._lock:
            note = self._notes.get(key)
        if note is not None:
            view.update(state=note[0], message=note[1])
        return view

    def statuses(self, config):
        return [self.status(config, settings["endpoint"]) for settings in server_list(config)]

    # -- starting -------------------------------------------------------------

    def start(self, config, name, by="you", wait=False, should_stop=None, log=None,
              timeout=None):
        """Start the server of an endpoint. Returns (ok, message).

        With wait, returns once it answers /health with 200 (and, with a
        projector, says it can see images), fails, or should_stop() asks to
        stop (the server is then stopped again); without, the waiting
        happens in a background thread and status() follows it. A server
        that is already running (or starting) is not started again: with
        wait, it is waited for. log(message, level) gets progress lines.
        """
        log = log or _no_log
        settings = find_server(config, name)
        if settings is None:
            return False, "No model server is set up for the endpoint %s." % name
        key = self._key(settings["endpoint"])
        op = self._current_op(key)
        if op is not None:
            if op["state"] == "stopping":
                return False, "The model server is stopping; try again in a moment."
            if not wait:
                return True, "The model server is starting."
            return self._wait_op(op, should_stop)
        problem = setup_problem(config, settings)
        if problem:
            return False, problem
        if self._record(key) is not None:
            if not wait:
                return True, "The model server is already running."
            return self._wait_ready(key, settings, should_stop, timeout)
        if port_in_use(settings["port"]):
            served = props(settings["port"])
            what = " (llama.cpp with %s)" % served["model"] if served["model"] else ""
            return False, ("Port %d is already in use%s by a program that was not started here. "
                           "Stop it first, or choose another port." % (settings["port"], what))
        try:
            command = build_command(settings)
        except ServerError as error:
            return False, str(error)
        endpoint = common.get_endpoint(config, settings["endpoint"])
        secrets = list(command_secrets(settings))
        api_key = common.resolve_api_key(endpoint)[0] if endpoint is not None else ""
        if api_key:
            secrets.append(api_key)
        path = log_path(config, settings["endpoint"])
        op = self._begin_op(key, "starting", "Starting the model server...")
        if op is None:
            return False, "The model server is already starting or stopping."
        with self._lock:
            self._stopping.discard(key)
        try:
            proc, offset = self._launch(key, settings, command, path, secrets, by)
        except ServerError as error:
            self._end_op(key, False, "error", str(error))
            return False, str(error)
        log("Started the model server for %s (pid %d): %s"
            % (settings["endpoint"], proc.pid, masked_command(command, secrets)))
        timeout = settings["start_timeout"] if timeout is None else timeout
        args = (key, settings, proc, path, offset, secrets, timeout)
        if not wait:
            threading.Thread(target=self._watch_start, name="model-server-start",
                             args=args + (None, log), daemon=True).start()
            return True, "Starting the model server..."
        return self._watch_start(*(args + (should_stop, log)))

    def _launch(self, key, settings, command, path, secrets, by):
        """Start the process (its output goes to the log) and record it.

        Returns (the Popen, the log's size before this start's lines).
        """
        common.ensure_dir(os.path.dirname(path))
        try:
            if os.path.getsize(path) > MAX_LOG_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            handle = open(path, "ab")
        except OSError as error:
            raise ServerError("Cannot write the log file %s: %s" % (path, error))
        try:
            handle.seek(0, os.SEEK_END)
            offset = handle.tell()
            handle.write(("\n===== %s: starting the model server for %s (by %s) =====\n$ %s\n"
                          % (time.strftime("%Y-%m-%d %H:%M:%S"), settings["endpoint"], by,
                             masked_command(command, secrets))).encode("utf-8", "replace"))
            handle.flush()
            options = {}
            if os.name == "nt":
                options["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                                            | _no_window())
            else:
                options["start_new_session"] = True
            binary = command[0]
            cwd = None
            if (not settings["launch_command"] and os.path.isabs(binary)
                    and os.path.isdir(os.path.dirname(binary))):
                cwd = os.path.dirname(binary)
            try:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=handle,
                                        stderr=subprocess.STDOUT, cwd=cwd,
                                        env=dict(os.environ, PYTHONUNBUFFERED="1"), **options)
            except (OSError, ValueError) as error:
                handle.write(("Could not start it: %s\n" % error).encode("utf-8", "replace"))
                raise ServerError("The model server could not be started: %s"
                                  % common.redact(str(error), secrets))
        finally:
            handle.close()      # the server has its own copy
        record = {"pid": proc.pid, "identity": process_identity(proc.pid),
                  "started": self._clock().timestamp(), "by": by,
                  "endpoint": settings["endpoint"], "port": settings["port"],
                  "model": settings["model"], "log": path, "members": []}

        def add(servers):
            servers[key] = record
            return True
        with self._lock:
            self._procs[key] = proc
            self._records[key] = record
            try:
                update_state(add)
            except OSError as error:
                self._procs.pop(key, None)
                self._records.pop(key, None)
                kill_tree(proc.pid)
                raise ServerError("Cannot record the started server in %s: %s"
                                  % (state_path(), error))
        return proc, offset

    def _watch_start(self, key, settings, proc, path, offset, secrets, timeout, should_stop,
                     log):
        """Wait until a just-started server is ready (or fails); returns (ok, message)."""
        began = time.monotonic()
        deadline = began + max(1.0, float(timeout))
        port = settings["port"]
        with self._lock:
            record = self._records.get(key)
        announced = False
        while True:
            with self._lock:
                cut_short = key in self._stopping
            if cut_short:
                return False, "The model server was stopped before it was ready."
            # (What the server starts is recorded as it goes; a wrapper that
            # has ended while the server it started runs on is not done.)
            tree = self._track(key, record, pinned=True) if record is not None else []
            if proc.poll() is not None and not tree:
                with self._lock:
                    if key in self._stopping:       # (ended by the stop that took over)
                        return False, "The model server was stopped before it was ready."
                tail = log_tail(path, 5, secrets, start=offset)
                message = "The model server stopped while starting (exit code %s)." % proc.poll()
                if tail:
                    message += " The end of its log: " + " | ".join(tail)
                self._clear_record(key, proc.pid)
                self._end_op(key, False, "error", message)
                log(message, "error")
                return False, message
            code = health(port)
            if code == 200:
                break
            if code == 503:
                self._set_op(key, "loading", "The server is loading its model...")
                if not announced:
                    announced = True
                    log("Waiting for the model server to load its model...")
            if should_stop is not None and should_stop():
                return self._give_up(key, settings, "Stopped before the model server was ready.",
                                     "stopped", "the run was stopped", log, level="info")
            if time.monotonic() >= deadline:
                return self._give_up(key, settings, "The model server did not become ready "
                                     "within %d seconds; it was stopped again. See its log on "
                                     "the Settings page." % int(round(timeout)), "error",
                                     "it did not start in time", log)
            # (Often at first, to see what a wrapper starts.)
            time.sleep(START_POLL if time.monotonic() - began > 3 else min(START_POLL, 0.2))
        served = props(port)
        try:
            wants_vision = bool(mmproj_path(settings))
        except ServerError:
            wants_vision = False
        if wants_vision and served["vision"] is False and not embeddings_server(settings):
            return self._give_up(key, settings, "The model server started but cannot see "
                                 "images (its projector was not loaded); it was stopped again. "
                                 "Check the mmproj setting and its log.", "error",
                                 "it cannot see images", log)
        message = "The model server for %s is ready%s." % (
            settings["endpoint"], " (%s)" % served["model"] if served["model"] else "")
        self._end_op(key, True, "ready", message)
        log(message)
        return True, message

    def _give_up(self, key, settings, message, state, reason, log, level="error"):
        """End a start that failed: stop the process, then report message as state."""
        self._set_op(key, "stopping", "Stopping the model server...")
        record = self._record(key)
        if record is not None:
            self._stop_process(key, settings["endpoint"], record, reason, _no_log, None,
                               end_op=False)
        self._end_op(key, False, state, message)
        log(message, level)
        return False, message

    @staticmethod
    def _wait_op(op, should_stop):
        """Wait for a start that is under way here; returns its (ok, message)."""
        while not op["done"].wait(0.5):
            if op.get("cancelled"):
                break
            if should_stop is not None and should_stop():
                return False, "Stopped while the model server was starting."
        if op.get("cancelled"):
            # A stop took the start over: the server did not start.
            return False, "The model server was stopped before it was ready."
        return op["ok"], op["message"]

    def _wait_ready(self, key, settings, should_stop, timeout):
        """Wait for a server started earlier (by another process, say) to be ready."""
        deadline = time.monotonic() + (settings["start_timeout"] if timeout is None else timeout)
        while True:
            if health(settings["port"]) == 200:
                return True, "The model server is ready."
            if self._record(key) is None:
                return False, "The model server stopped."
            if time.monotonic() >= deadline:
                return False, "The model server did not become ready in time."
            if should_stop is not None and should_stop():
                return False, "Stopped while the model server was starting."
            time.sleep(START_POLL)

    # -- stopping -------------------------------------------------------------

    def stop(self, config, name, wait=True, reason="", log=None, grace=None):
        """Stop the server of an endpoint, if it was started here. Returns (ok, message).

        A server that was not started here is never stopped. A start under
        way is cut short (those waiting for it hear that it did not start).
        Without wait the stopping happens in a background thread. config is
        not needed (None is fine).
        """
        log = log or _no_log
        key = self._key(name)
        record = self._record(key)
        if record is None:
            return False, "No model server started here is running for %s." % name
        with self._lock:
            op = self._ops.get(key)
            if op is not None and op["state"] == "stopping":
                waiting = op
            else:
                waiting = None
                if op is None:
                    self._ops[key] = {"state": "stopping", "ok": False,
                                      "message": "Stopping the model server...",
                                      "done": threading.Event()}
                else:
                    # A start under way: its watcher gives up.
                    self._stopping.add(key)
                    op.update(state="stopping", message="Stopping the model server...",
                              cancelled=True)
        if waiting is not None:
            if wait:
                waiting["done"].wait(GRACE_SECONDS + KILL_WAIT + PORT_RELEASE_WAIT + 10)
                if waiting.get("cancelled"):
                    with self._lock:
                        note = self._notes.get(key)
                    if note is not None:
                        return note[0] == "stopped", note[1]
                return waiting["ok"], waiting["message"]
            return True, "The model server is stopping."
        if not wait:
            threading.Thread(target=self._stop_process, name="model-server-stop",
                             args=(key, name, record, reason, log, grace),
                             daemon=True).start()
            return True, "Stopping the model server..."
        return self._stop_process(key, name, record, reason, log, grace)

    def _stop_process(self, key, name, record, reason, log, grace, end_op=True):
        pid = record["pid"]
        identity = record.get("identity")
        with self._lock:
            mine = self._records.get(key)
            proc = self._procs.get(key) if mine is not None and mine["pid"] == pid else None
        # Everything the server runs now is recorded before it is asked to
        # stop: a wrapper may end at once and leave what it started behind.
        self._track(key, record, pinned=proc is not None)
        members = [list(pair) for pair in record.get("members") or ()]

        def gone():
            alive = proc is not None and proc.poll() is None
            tree = find_tree(pid, identity, members, pinned=proc is not None)
            for number, ident in tree:
                if number != pid and [number, ident] not in members:
                    members.append([number, ident])     # (started while it stops)
            return not alive and not tree

        grace = GRACE_SECONDS if grace is None else grace
        forced = False
        if not gone():
            send_graceful_stop(pid, identity, members)
            deadline = time.monotonic() + grace
            while not gone() and time.monotonic() < deadline:
                time.sleep(0.25)
        if not gone():
            forced = True
            kill_tree(pid, identity, members)
            deadline = time.monotonic() + KILL_WAIT
            while not gone() and time.monotonic() < deadline:
                time.sleep(0.25)
        port = record.get("port")
        if gone():
            self._clear_record(key, pid)
            message = "Stopped the model server for %s%s%s." % (
                name, " because %s" % reason if reason else "",
                " (it did not stop by itself within %d seconds, so it was ended)" % grace
                if forced else "")
            ok, state = True, "stopped"
            deadline = time.monotonic() + PORT_RELEASE_WAIT
            while (isinstance(port, int) and port_in_use(port)
                   and time.monotonic() < deadline):
                time.sleep(0.1)
            if isinstance(port, int) and port_in_use(port):
                # Something it started may have left the tree (or another
                # program took the port): say so rather than "stopped".
                message = ("The processes started for %s have ended, but port %d still "
                           "answers: a program they started may still run (look for it in "
                           "the task list), or another program uses the port." % (name, port))
                ok, state = False, "error"
        else:
            message = "The model server for %s (pid %d) did not stop." % (name, pid)
            ok, state = False, "error"
        _append_log(record.get("log") or "", "===== %s: %s =====\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        if end_op:
            self._end_op(key, ok, state, message)
            log(message, "info" if ok else "error")
        return ok, message

    # -- runs that use a server ---------------------------------------------------

    def lease(self, ctx, config, names):
        """A Lease on the managed servers among these endpoint names (see Lease)."""
        return Lease(self, ctx, config, names)

    def _add_lease(self, key, lease):
        with self._lock:
            self._leases[key].add(lease)

    def _drop_lease(self, key, lease):
        with self._lock:
            group = self._leases.get(key)
            last = False
            if group is not None:
                group.discard(lease)
                if not group:
                    self._leases.pop(key, None)
                    last = True
        if last:
            self._mark_user(key, False)

    def _mark_user(self, key, using):
        """Note in data/model-server.json that this manager (of this process) uses the
        server of key, or no longer does, so another one's idle stop (check_idle)
        leaves it running. Users whose process no longer runs are dropped on the way.
        A user is "<pid>.<number>": its process, and which manager of it."""
        if self._identity is None:
            self._identity = process_identity(os.getpid()) or ""

        def change(servers):
            entry = servers.get(key)
            if entry is None:
                return False
            users = entry.get("users") if isinstance(entry.get("users"), dict) else {}
            kept = {user: identity for user, identity in users.items()
                    if user != self._user and _user_running(user, identity)}
            if using:
                kept[self._user] = self._identity
            if kept == users:
                return False
            entry["users"] = kept
            return True
        try:
            update_state(change)
        except OSError:
            pass

    def _other_users(self, key):
        """The other managers (of running processes) that use the server of key now."""
        entry = load_state().get(key) or {}
        users = entry.get("users") if isinstance(entry.get("users"), dict) else {}
        return [user for user, identity in users.items()
                if user != self._user and _user_running(user, identity)]

    def leases(self, name):
        """The leases (runs) that use the server of this endpoint now."""
        with self._lock:
            return list(self._leases.get(self._key(name), ()))

    # -- servers started for the search page's queries ---------------------------

    def touch(self, name):
        """Note that a search query used the server of this endpoint just now."""
        with self._lock:
            self._last_used[self._key(name)] = time.monotonic()

    def start_for_queries(self, config, name):
        """Make sure the managed server of an endpoint runs for the search page's queries.

        Returns (state, message): "ready" (it answers), "starting" (it is
        starting or loading its model: ask again soon) or "off" (it does
        not run and is not started: auto_start is off, its stop_at window
        is on, or it cannot be started; message says why). An endpoint
        without a managed server is "ready" (whatever answers there is
        used as it is). A server started here for queries is stopped by
        check_idle after idle_stop_minutes without a query.
        """
        settings = managed(config, name)
        if settings is None:
            return "ready", ""
        key = self._key(settings["endpoint"])
        self.touch(name)
        op = self._current_op(key)
        if op is not None:
            if op["state"] == "stopping":
                return "off", "The model server is stopping."
            return "starting", op["message"]
        code = health(settings["port"])
        if code == 503:
            return "starting", "The model server is loading its model."
        if code is not None:
            return "ready", ""
        if not embeddings_server(settings):
            return "off", ("The model server for %s is not an embeddings server (\"kind\": "
                           "\"embeddings\" in config.json's local_server), so a search does not "
                           "start it." % settings["endpoint"])
        if not settings["auto_start"]:
            return "off", ("The model server for %s is not running and auto start is off: start "
                           "it on the Settings page." % settings["endpoint"])
        stop = stop_window(settings, self._clock())
        if stop is not None:
            return "off", window_message(settings, stop)
        with self._lock:
            failed = self._failed.get(key)
        if failed is not None and time.monotonic() - failed[0] < QUERY_RETRY_SECONDS:
            # (Not loaded again and again for every search; Start on the Settings
            # page, or a job, still tries at once.)
            return "unavailable", ("The model server could not be started a moment ago: %s "
                                   "Searches try again in a few minutes." % failed[1])
        ok, message = self.start(config, settings["endpoint"], by="search", wait=False)
        if not ok:
            return "off", message
        with self._lock:
            self._query_started.add(key)
        return "starting", message

    def check_idle(self, config, log=None):
        """Stop the servers started for queries that no query has used for their
        idle_stop_minutes (0: never), unless a run uses them. Returns the endpoint
        names whose server was stopped."""
        log = log or _no_log
        stopped = []
        with self._lock:
            keys = list(self._query_started)
        for key in keys:
            settings = find_server(config, key)
            if settings is None or (self._record(key) is None and self._current_op(key) is None):
                with self._lock:
                    self._query_started.discard(key)
                continue
            minutes = settings["idle_stop_minutes"]
            if not minutes or self.leases(key) or self._current_op(key) is not None:
                continue
            if self._other_users(key):
                continue        # (scan.py, say, embeds with it now)
            with self._lock:
                last = self._last_used.get(key, 0.0)
            if time.monotonic() - last < minutes * 60:
                continue
            ok, message = self.stop(config, settings["endpoint"], wait=True,
                                    reason="no search used it for %d minute%s"
                                    % (minutes, "" if minutes == 1 else "s"))
            log(message, "info" if ok else "error")
            if ok:
                with self._lock:
                    self._query_started.discard(key)
                stopped.append(settings["endpoint"])
        return stopped

    # -- the daily stop time ----------------------------------------------------

    def check_schedule(self, config, now=None, log=None):
        """Act on the stop times; returns the endpoint names whose server was stopped.

        From PRE_STOP_MINUTES before a server's stop_at, the runs using it
        are stopped (their own timers normally did that already). Once
        stop_at has come, a server started here before that time is
        stopped, and tried again on every check in the NO_START_MINUTES
        after it until it has stopped; one started (by hand) after the stop
        time keeps running. The record says when it was started, so this
        also holds after the web server restarts.
        """
        log = log or _no_log
        now = now or self._clock()
        stopped = []
        for settings in server_list(config):
            if not settings["enabled"]:
                continue
            stop = stop_window(settings, now)
            if stop is None:
                continue
            for lease in self.leases(settings["endpoint"]):
                lease.deadline(settings)
            if now < stop:
                continue
            key = self._key(settings["endpoint"])
            record = self._record(key)
            if record is None:
                continue
            started = record.get("started")
            if (isinstance(started, (int, float)) and not isinstance(started, bool)
                    and started >= stop.timestamp()):
                continue
            op = self._current_op(key)
            if op is not None and op["state"] == "stopping":
                continue
            ok, message = self.stop(config, settings["endpoint"], wait=True,
                                    reason="its stop time (%s) came" % settings["stop_at"])
            log(message, "info" if ok else "error")
            if ok:
                stopped.append(settings["endpoint"])
        return stopped

    def start_scheduler(self, config_loader, interval=SCHEDULE_SECONDS, log=None):
        """Call check_schedule every interval seconds in a daemon thread (the web server does)."""
        log = log or _print_log
        with self._lock:
            if self._scheduler is not None and self._scheduler.is_alive():
                return
            self._scheduler_stop.clear()

            def loop():
                while not self._scheduler_stop.wait(interval):
                    try:
                        config = config_loader()
                        self.check_schedule(config, log=log)
                        self.check_idle(config, log=log)
                    except Exception as error:
                        log("Checking the model server's stop time failed: %s"
                            % common.redact(str(error)), "warn")

            self._scheduler = threading.Thread(target=loop, name="model-server-schedule",
                                               daemon=True)
            self._scheduler.start()

    def shutdown(self, grace=10.0, config=None):
        """When the program stops: stop the scheduler, and the servers runs started
        (auto_stop); with config, also those started for search queries whose
        auto_stop is on."""
        self._scheduler_stop.set()
        with self._lock:
            leases = {lease for group in self._leases.values() for lease in group}
            queried = list(self._query_started)
        for lease in leases:
            lease.release(grace=grace)
        for key in queried if config is not None else ():
            settings = find_server(config, key)
            if settings is not None and settings["auto_stop"] and not self.leases(key):
                self.stop(config, settings["endpoint"], wait=True, grace=grace,
                          reason="the web server stopped")


_MANAGER_NUMBERS = itertools.count(1)


def _user_running(user, identity):
    """True when the process of a recorded user ("<pid>.<number>") still runs."""
    pid = str(user).split(".")[0]
    return pid.isdigit() and is_running(int(pid), identity)


def _no_log(message, level="info"):
    pass


def _print_log(message, level="info"):
    prefix = {"warn": "Warning: ", "error": "Error: "}.get(level, "")
    print(prefix + message, file=sys.stderr)


class Lease:
    """The model servers one describe or refine run uses.

    acquire() makes sure the server of each managed endpoint among the
    run's endpoints is running: one that already answers is used as it is
    (whoever started it, even while it still loads its model: describing
    waits for that); otherwise it is started (auto_start) and waited for.
    It returns {endpoint name: problem} for those that cannot be used: not
    running with auto_start off, inside the stop_at window (see
    stop_window), or failing to start. While the run lasts, a timer stops
    it PRE_STOP_MINUTES before the stop_at of a server it uses as a run
    that can be resumed (ctx.interrupt). release() stops the servers
    acquire() started, when their auto_stop is on and no other run uses
    them.
    """

    def __init__(self, manager, ctx, config, names):
        self.manager = manager
        self.ctx = ctx
        self.config = config
        self.names = list(dict.fromkeys(name for name in names if name))
        job_id = getattr(ctx, "job_id", None)
        self.by = ("job %d" % job_id if isinstance(job_id, int) and not isinstance(job_id, bool)
                   else "scan.py")
        self.started = []           # endpoint names whose server this run started
        self._held = []
        self._timers = {}           # key -> the timer of the server's stop time
        self._released = False
        self._deadline_hit = False
        self._lock = threading.Lock()

    def _log(self, message, level="info"):
        log = getattr(self.ctx, "log", None)
        if log is not None:
            log(message, level)

    def _should_stop(self):
        check = getattr(self.ctx, "should_stop", None)
        return bool(check is not None and check())

    def acquire(self):
        """Make sure the managed servers are running; returns {endpoint name: problem}."""
        problems = {}
        for name in self.names:
            settings = managed(self.config, name)
            if settings is None:
                continue
            key = self.manager._key(settings["endpoint"])
            stop = stop_window(settings, self.manager._clock())
            if stop is not None:
                problems[name] = window_message(settings, stop)
                continue
            # Held while it starts, so that another run's release does not stop it.
            self.manager._add_lease(key, self)
            self._held.append(key)
            self._arm_timer(key, settings)
            owned = self.manager.owned(name)
            if not owned and self.manager._current_op(key) is None:
                code = health(settings["port"])
                if code is not None and code != 404:
                    self._log("The model server for %s is already running on port %d (not "
                              "started here%s); using it." % (
                                  name, settings["port"],
                                  ", still loading its model" if code == 503 else ""))
                    continue
            if not owned and not settings["auto_start"]:
                problems[name] = ("The model server for %s is not running and auto start is "
                                  "off: start it on the Settings page first." % name)
                self._let_go(key)
                continue
            if not owned:
                self._log("Starting the model server for %s..." % name)
            ok, message = self.manager.start(self.config, name, by=self.by, wait=True,
                                             should_stop=self._should_stop, log=self._log)
            if not ok:
                problems[name] = message
                self._let_go(key)
                continue
            if not owned and self.manager.owned(name):
                self.started.append(name)
            self.manager._mark_user(key, True)
        return problems

    def _let_go(self, key):
        """Stop holding a server the run cannot use (no stop-time timer, not "in use")."""
        self.manager._drop_lease(key, self)
        if key in self._held:
            self._held.remove(key)
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _arm_timer(self, key, settings):
        moment = next_pre_stop(settings, self.manager._clock())
        if moment is None:
            return
        seconds = max(0.0, (moment - self.manager._clock()).total_seconds())
        timer = threading.Timer(seconds, self.deadline, args=(settings,))
        timer.daemon = True
        timer.start()
        self._timers[key] = timer

    def deadline(self, settings):
        """The server's stop time is near: stop the run (once), so that it can be resumed later."""
        with self._lock:
            if self._released or self._deadline_hit:
                return
            self._deadline_hit = True
        now = self.manager._clock()
        stop = stop_window(settings, now)
        if stop is None:
            moment = next_pre_stop(settings, now)
            stop = moment + timedelta(minutes=PRE_STOP_MINUTES) if moment else now
        message = ("Stopping for the model server's daily stop time (%s): this job can be "
                   "resumed after %s." % (settings["stop_at"], (
                       stop + timedelta(minutes=NO_START_MINUTES)).strftime("%H:%M")))
        interrupt = getattr(self.ctx, "interrupt", None)
        if interrupt is not None:
            interrupt(message)
            return
        self._log(message, "warn")
        request = getattr(self.ctx, "request_stop", None)
        if request is not None:
            request()

    def release(self, grace=None):
        """Stop the timers, and the servers this run started (when auto_stop is on)."""
        with self._lock:
            if self._released:
                return
            self._released = True
        for timer in self._timers.values():
            timer.cancel()
        for key in self._held:
            self.manager._drop_lease(key, self)
        for name in self.started:
            settings = find_server(self.config, name)
            if settings is None or not settings["auto_stop"] or self.manager.leases(name):
                continue
            self.manager.stop(self.config, name, wait=True, log=self._log, grace=grace,
                              reason="the job that started it has finished")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.release()


_managers = {}
_managers_lock = threading.Lock()


def default_manager():
    """The ServerManager of this process for the current data folder."""
    key = os.path.normcase(os.path.abspath(common.data_dir()))
    with _managers_lock:
        manager = _managers.get(key)
        if manager is None:
            manager = _managers[key] = ServerManager()
        return manager


def lease_for(ctx, config, names, manager=None):
    """A Lease of the managed servers among names (manager: default_manager())."""
    return (manager or default_manager()).lease(ctx, config, names)
