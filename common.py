#!/usr/bin/env python3
"""Shared helpers for the image database tools.

This module uses only the standard library so it can be imported by every
script without pulling in third party dependencies.

Paths are functions, evaluated each time they are called, so tests (and any
long-running process) see the current SETTINGS_BASE rather than the value
that was set when this module was first imported.
"""

import copy
import fnmatch
import importlib.util
import ipaddress
import json
import locale
import math
import os
import re
import secrets
import sys
import threading
import time
import types
import urllib.parse


IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".hif", ".webp",
    ".tif", ".tiff", ".avif", ".bmp",
    ".insp",            # Insta360 360-degree photos (JPEG data)
)
# Formats Pillow can only read through an optional plugin (pillow-heif, or a
# Pillow build with AVIF support), and that browsers may not display.
HEIC_EXTENSIONS = (".heic", ".heif", ".hif", ".avif")

# The folder holding the scripts (and the templates/static folders).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Paths

def settings_base():
    """Folder holding config.json and data/.

    The SETTINGS_BASE environment variable moves both out of the script
    folder. It is read on every call.
    """
    return os.path.abspath(os.environ.get("SETTINGS_BASE") or BASE_DIR)


def config_path():
    return os.path.join(settings_base(), "config.json")


def data_dir():
    return os.path.join(settings_base(), "data")


def db_path():
    """The current database (data/index.db)."""
    return os.path.join(data_dir(), "index.db")


def legacy_db_path():
    """The database written by older versions; only ever read."""
    return os.path.join(data_dir(), "images.db")


def legacy_thumb_dir():
    """The thumbnail shard folder written by older versions; only ever read."""
    return os.path.join(data_dir(), "thumbs")


def thumbs_db_path():
    return os.path.join(data_dir(), "thumbs.db")


def previews_dir():
    return os.path.join(data_dir(), "previews")


def logs_dir():
    return os.path.join(data_dir(), "logs")


def tmp_dir():
    return os.path.join(data_dir(), "tmp")


def exports_dir():
    return os.path.join(data_dir(), "exports")


def reports_dir():
    return os.path.join(data_dir(), "reports")


def tiles_dir():
    return os.path.join(data_dir(), "tiles")


def abort_path():
    """Creating this file asks a running job to stop."""
    return os.path.join(data_dir(), "abort")


def lock_path():
    """Lock file held by whichever process is running jobs."""
    return os.path.join(data_dir(), "worker.lock")


def ensure_dir(path):
    """Create a folder (and its parents) if needed; return the path."""
    os.makedirs(path, exist_ok=True)
    return path


def write_file_atomic(path, data, tmp_path=None):
    """Write bytes (or UTF-8 text) to path so readers never see a partial file.

    The data goes to a temporary file in the same folder first, which then
    replaces the target with os.replace. On Windows the replace can fail for
    a moment while another process has the target open, so it is retried
    briefly before giving up.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if tmp_path is None:
        tmp_path = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Config

DEFAULT_CONFIG = {
    "endpoint": {"kind": "openai", "base_url": "", "api_key": "", "model": ""},
    "helper": "",
    "reader_threads": 6,
    "purge_after_days": 30,
    "libraries": [],
}

# Filled into every library that does not set them. "id" is not here: ids
# are assigned by ensure_library_ids and must then be saved.
LIBRARY_DEFAULTS = {
    "name": "",
    "path": "",
    "ignore": [],
    "default_ignores": True,
    "enabled": True,
    "previews": True,
    "newest_first": True,
    "endpoint": "",
    "metadata": "core",
    "context_notes": "",
    "export": {},
}

LIBRARY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_config_lock = threading.Lock()


class ConfigError(ValueError):
    """config.json exists but could not be read."""


def default_config():
    return copy.deepcopy(DEFAULT_CONFIG)


def _merge_endpoint(stored_endpoint):
    endpoint = dict(DEFAULT_CONFIG["endpoint"])
    if isinstance(stored_endpoint, dict):
        endpoint.update(stored_endpoint)
    return endpoint


def apply_library_defaults(library):
    """Fill missing library settings in place and return the library."""
    for key, value in LIBRARY_DEFAULTS.items():
        if key not in library:
            library[key] = copy.deepcopy(value)
    if not isinstance(library.get("path"), str):
        library["path"] = ""
    if not isinstance(library.get("name"), str) or not library["name"].strip():
        library["name"] = default_library_name(library["path"])
    return library


_warned_encoding = set()


def _read_config_text(path):
    """The text of config.json: UTF-8 (with or without a BOM).

    Older versions read the file in the system's encoding, so a file saved
    that way by hand (for example "ANSI" on Windows) is still accepted, with
    a one-time warning; the next save writes it as UTF-8.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        encoding = locale.getpreferredencoding(False) or "ascii"
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            raise ConfigError("Could not read %s: it is not UTF-8 text (%s)" % (path, error))
        if path not in _warned_encoding:
            _warned_encoding.add(path)
            print("Warning: %s is not UTF-8; it was read as %s. Save it as UTF-8 (the "
                  "next change made by these tools does that)." % (path, encoding),
                  file=sys.stderr)
        return text


def load_config():
    """Read config.json (UTF-8, with or without a BOM) and fill in defaults.

    Never writes. Returns the defaults when the file does not exist yet.
    """
    config = default_config()
    path = config_path()
    if not os.path.exists(path):
        return config
    try:
        stored = json.loads(_read_config_text(path))
    except OSError as error:
        raise ConfigError("Could not read %s: %s" % (path, error))
    except ValueError as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError("Could not read %s: %s" % (path, error))
    if not isinstance(stored, dict):
        raise ConfigError("Could not read %s: the top level is not an object" % path)
    config.update(stored)
    if "endpoints" in stored and "endpoint" not in stored:
        # Only the newer "endpoints" list: no placeholder for the older
        # "endpoint" key, which would otherwise be saved back into the file.
        config.pop("endpoint", None)
    else:
        # Endpoint keys are merged separately so older config files keep
        # working. A list of endpoints (round robin) merges the defaults
        # into each item.
        stored_endpoint = stored.get("endpoint", {})
        if isinstance(stored_endpoint, list):
            config["endpoint"] = [_merge_endpoint(item) for item in stored_endpoint]
        else:
            config["endpoint"] = _merge_endpoint(stored_endpoint)
    libraries = config.get("libraries")
    if not isinstance(libraries, list):
        libraries = []
    config["libraries"] = [apply_library_defaults(library)
                           for library in libraries if isinstance(library, dict)]
    return config


def save_config(config):
    """Write config.json atomically (indented JSON).

    Non-ASCII characters are written as \\u escapes, so the file is plain
    ASCII: older versions of these tools, which read it in the system's
    encoding, still see library names and folders correctly. It is valid
    UTF-8 all the same, and hand-typed UTF-8 is read as well.
    """
    path = config_path()
    ensure_dir(os.path.dirname(path))
    text = json.dumps(config, indent=4, ensure_ascii=True) + "\n"
    # The temporary name matches the config.*.json ignore rule, so a crash
    # mid-write never leaves an unignored copy of the API keys behind.
    tmp_path = os.path.join(os.path.dirname(path), "config.tmp-%d.json" % os.getpid())
    with _config_lock:
        write_file_atomic(path, text, tmp_path)


def new_library_id(existing=()):
    """Return a fresh random library id not in existing."""
    while True:
        candidate = "lib_" + secrets.token_hex(4)
        if candidate not in existing:
            return candidate


def ensure_library_ids(config):
    """Give every library a unique, valid id. Returns True if any changed.

    Callers save the config when this returns True: an id that was handed out
    but not saved would be a different id on the next run.
    """
    changed = False
    seen = set()
    for library in config.get("libraries", []):
        library_id = library.get("id")
        if (not isinstance(library_id, str) or not LIBRARY_ID_RE.match(library_id)
                or library_id in seen):
            library["id"] = new_library_id(seen)
            changed = True
        seen.add(library["id"])
    return changed


def _folder_key(library):
    path = library.get("path")
    if not isinstance(path, str) or not path.strip():
        return ""
    return os.path.normcase(os.path.normpath(os.path.expanduser(path.strip())))


def _name_key(library):
    name = library.get("name")
    return name.strip().casefold() if isinstance(name, str) else ""


def copy_library_ids(target, source, only_missing=False, keep=()):
    """Give libraries in target the ids their counterparts in source have.

    Used so that two copies of the settings (say, one loaded before another
    process assigned ids and saved them) never end up with different ids for
    the same library: everything indexed under the other id would become an
    orphan. A target library is matched to a source library with the same
    folder, or else the same name, whose id no other target library uses.

    Only target libraries without a valid id change when only_missing is
    True; otherwise also those whose id is not in source. Libraries whose id
    is in keep never change. Returns True if any id changed.
    """
    source_libraries = [library for library in source.get("libraries", [])
                        if isinstance(library.get("id"), str)
                        and LIBRARY_ID_RE.match(library["id"])]
    source_ids = {library["id"] for library in source_libraries}
    libraries = target.get("libraries", [])
    taken = {library.get("id") for library in libraries}
    changed = False
    for library in libraries:
        library_id = library.get("id")
        valid = isinstance(library_id, str) and LIBRARY_ID_RE.match(library_id)
        if valid and (only_missing or library_id in source_ids or library_id in keep):
            continue
        match = None
        for key in (_folder_key, _name_key):
            value = key(library)
            if value:
                match = next((candidate for candidate in source_libraries
                              if candidate["id"] not in taken and key(candidate) == value),
                             None)
            if match is not None:
                break
        if match is not None:
            library["id"] = match["id"]
            taken.add(match["id"])
            changed = True
    return changed


def library_root(library):
    """The folder of a library: its path with ~ expanded ('' if none is set).

    Relative paths stay relative to the current directory, as before.
    """
    path = library.get("path") if isinstance(library, dict) else None
    if not isinstance(path, str) or not path.strip():
        return ""
    return os.path.normpath(os.path.expanduser(path.strip()))


# A leading date: a year (1900-2099), optionally with month and day, using one
# consistent separator (or none) between the parts.
_DATE_PREFIX_RE = re.compile(
    r"^(?:19|20)\d\d"
    r"(?:(?P<sep>[-_. ]?)(?P<month>0[1-9]|1[0-2])"
    r"(?:(?P=sep)(?:0[1-9]|[12]\d|3[01]))?)?"
    r"(?!\d)(?P<rest>.*)$", re.S)
_DASHES = chr(0x2013) + chr(0x2014)     # en and em dash
_NAME_SEPARATORS_RE = re.compile(r"[\s\-_.," + _DASHES + "]*")


def _strip_date_prefix(name):
    match = _DATE_PREFIX_RE.match(name)
    if not match:
        return name
    rest = match.group("rest")
    separator = _NAME_SEPARATORS_RE.match(rest).group(0)
    if rest and not separator:
        # "1999ers" is a name, not a date.
        return name
    if match.group("month") is None and not re.search("[-_." + _DASHES + "]", separator):
        # A bare year needs an explicit separator ("2014 - Trip"), so names
        # such as "2001 Space Odyssey" are left alone.
        return name
    return rest[len(separator):]


def default_library_name(folder):
    """Suggest a library name from a folder path.

    Uses the last path component with a leading date removed, for example
    "2014-10-24 - Garden", "2014-10 Garden" and "20141024_Garden" all become
    "Garden". Falls back to the plain folder name if nothing would be left.
    """
    parts = [part for part in re.split(r"[\\/]+", (folder or "").strip()) if part]
    if not parts:
        return ""
    base = parts[-1].strip()
    name = base
    for _ in range(3):
        stripped = _strip_date_prefix(name).strip()
        if stripped == name:
            break
        name = stripped
    return name or base


def get_library(config, id_or_name):
    """Find a library by id, or else by name (case-insensitive). None if absent."""
    if not id_or_name:
        return None
    libraries = config.get("libraries", [])
    for library in libraries:
        if library.get("id") == id_or_name:
            return library
    wanted = str(id_or_name).casefold()
    for library in libraries:
        if str(library.get("name", "")).casefold() == wanted:
            return library
    return None


def enabled_libraries(config):
    """The libraries that are not disabled, in config order."""
    return [library for library in config.get("libraries", [])
            if library.get("enabled", True)]


def library_label(library):
    """A short human-readable name for a library, for messages."""
    return (library.get("name") or default_library_name(library.get("path", ""))
            or library.get("id") or "(unnamed)")


# ---------------------------------------------------------------------------
# LLM endpoints
#
# config.json lists the services that describe images under "endpoints", each
# with a unique "name", and picks the one libraries use unless they name
# another with "default_endpoint". A library (or default_endpoint) may also
# name a "group": then every enabled endpoint in that group shares the work.
#
# Older files have a single "endpoint" object, or a list of them that share
# the work (round robin). Those keep working without being rewritten:
# endpoint_list() turns them into endpoints named "default", "default-2", ...
# and a list's members all get "group": "default".

ENDPOINT_KINDS = ("openai", "claude", "ollama")

# What each preset fills in when an endpoint does not set it: the API it
# speaks (kind), the base URL, the environment variable read for the API key
# when no key is configured (key_env), and how many requests to keep in
# flight at once (concurrency; None means the server's slot count, from
# llm.probe(), or 1).
ENDPOINT_PRESETS = {
    "llamacpp": {
        "label": "llama.cpp", "kind": "openai", "base_url": "http://127.0.0.1:8080/v1",
        "key_env": "", "concurrency": None,
        "notes": "llama.cpp's llama-server. Requests in flight follow its slot count "
                 "(--parallel), and thinking is turned off for models that have it.",
    },
    "local": {
        "label": "Local server", "kind": "openai", "base_url": "http://127.0.0.1:8080/v1",
        "key_env": "", "concurrency": None,
        "notes": "An OpenAI-compatible server on this computer or the local network, "
                 "treated like llama.cpp. Chosen automatically for such addresses.",
    },
    "lmstudio": {
        "label": "LM Studio", "kind": "openai", "base_url": "http://127.0.0.1:1234/v1",
        "key_env": "", "concurrency": 1,
        "notes": "LM Studio's local server.",
    },
    "vllm": {
        "label": "vLLM", "kind": "openai", "base_url": "http://127.0.0.1:8000/v1",
        "key_env": "", "concurrency": 1,
        "notes": "vLLM or SGLang. They batch requests well: raise the concurrency.",
    },
    "ollama-openai": {
        "label": "Ollama (OpenAI API)", "kind": "openai",
        "base_url": "http://127.0.0.1:11434/v1", "key_env": "", "concurrency": 1,
        "notes": "Ollama through its OpenAI-compatible API. The ollama preset (its own "
                 "API) can also turn thinking off.",
    },
    "ollama": {
        "label": "Ollama", "kind": "ollama", "base_url": "http://127.0.0.1:11434",
        "key_env": "", "concurrency": 1,
        "notes": "Ollama's own chat API.",
    },
    "openai": {
        "label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY", "concurrency": 4,
        "notes": "OpenAI. Images are sent to OpenAI and billed per token.",
    },
    "azure": {
        "label": "Azure OpenAI", "kind": "openai", "base_url": "",
        "key_env": "AZURE_OPENAI_API_KEY", "concurrency": 4,
        "notes": "Azure OpenAI (v1 API). Base URL: https://RESOURCE.openai.azure.com/openai/v1; "
                 "the model is the deployment name.",
    },
    "gemini": {
        "label": "Gemini", "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY", "concurrency": 4,
        "notes": "Google Gemini through its OpenAI-compatible API. Images are sent to Google.",
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY", "concurrency": 4,
        "notes": "OpenRouter. Images are sent to the provider it routes the model to.",
    },
    "anthropic": {
        "label": "Claude", "kind": "claude", "base_url": "https://api.anthropic.com",
        "key_env": "ANTHROPIC_API_KEY", "concurrency": 4,
        "notes": "Anthropic's Claude models. Images are sent to Anthropic and billed per token.",
    },
    "custom": {
        "label": "OpenAI-compatible", "kind": "openai", "base_url": "",
        "key_env": "", "concurrency": 1,
        "notes": "Any other service with an OpenAI-compatible chat completions API.",
    },
}

# Presets for servers that normally run on this computer or the local
# network, presets for cloud services, and those that behave like llama.cpp
# (/props, /health, chat_template_kwargs).
LOCAL_PRESETS = ("llamacpp", "local", "lmstudio", "vllm", "ollama-openai", "ollama")
CLOUD_PRESETS = ("openai", "azure", "gemini", "openrouter", "anthropic")
LLAMACPP_PRESETS = ("llamacpp", "local")

# Hosts of the cloud services, used to pick a preset for endpoints without one.
_PRESET_HOSTS = (
    ("api.openai.com", "openai"),
    (".openai.azure.com", "azure"),
    (".cognitiveservices.azure.com", "azure"),
    (".services.ai.azure.com", "azure"),
    ("generativelanguage.googleapis.com", "gemini"),
    ("openrouter.ai", "openrouter"),
    ("api.anthropic.com", "anthropic"),
)

DEFAULT_MAX_IMAGE_EDGE = 1536
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_OUTPUT_TOKENS = 4000
# Thinking counts against the output tokens, so models that always think get more.
THINKING_MAX_OUTPUT_TOKENS = 8000
MAX_CONCURRENCY = 32
# Images that failed to be described in this many runs are skipped until a
# run asks to retry them (describe.py, progress.py).
MAX_DESCRIBE_FAILURES = 3

# How to keep thinking to a minimum on Claude models (matched by id prefix):
# "disabled" sends thinking {"type": "disabled"}, "between_tools" sends
# thinking {"type": "between_tools"}, and "effort" (models that always
# think) sends no thinking settings but a low effort. Other models get
# nothing; an endpoint's "extra" settings can add what they need.
CLAUDE_THINKING = (
    ("claude-haiku-5-5", "disabled"),
    ("claude-sonnet-5-5", "between_tools"),
    ("claude-opus-5-5", "effort"),
    ("claude-fable-5-1", "effort"),
)

# Every endpoint setting and its default. None means "automatic".
ENDPOINT_DEFAULTS = {
    "name": "",
    "label": "",
    "kind": "",
    "preset": "",
    "base_url": "",
    "model": "",
    "api_key": "",              # the key itself; prefer api_key_env or api_key_file
    "api_key_env": "",          # name of an environment variable holding the key
    "api_key_file": "",         # file holding only the key (relative: next to config.json)
    "concurrency": None,        # requests in flight at once
    "max_image_edge": DEFAULT_MAX_IMAGE_EDGE,
    "timeout": DEFAULT_TIMEOUT,  # seconds without an answer before a request fails
    "max_output_tokens": None,
    "extra": {},                # merged into every request body
    "price_in": None,           # USD per million input tokens, for cost estimates
    "price_out": None,          # USD per million output tokens
    "enabled": True,
    "group": "",
}


def claude_thinking(model):
    """How thinking is kept down for a Claude model id: a CLAUDE_THINKING mode or ''."""
    model = model.strip().lower() if isinstance(model, str) else ""
    for prefix, mode in CLAUDE_THINKING:
        if model == prefix or model.startswith(prefix + "-"):
            return mode
    return ""


def url_host(url):
    """The lower-case host name of a URL ('' if there is none)."""
    url = url.strip() if isinstance(url, str) else ""
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_local_host(host):
    """True for a host on this computer or the local network.

    That is loopback, private, link-local and other non-public addresses,
    "localhost", and names without a dot or ending in .local, .lan,
    .home.arpa or .internal.
    """
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".local", ".lan", ".home.arpa", ".internal"))
    return not address.is_global


def infer_preset(endpoint):
    """The preset of an endpoint: its "preset" setting, or one inferred.

    Without a (known) preset: kind claude means "anthropic", kind ollama
    "ollama"; an openai-kind endpoint without a base URL is "openai", one on
    a cloud service's host gets that service's preset, one on this computer
    or the local network is "local" (treated like llama.cpp), and anything
    else is "custom".
    """
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    preset = endpoint.get("preset")
    preset = preset.strip().lower() if isinstance(preset, str) else ""
    if preset in ENDPOINT_PRESETS:
        return preset
    kind = endpoint.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind == "claude":
        return "anthropic"
    if kind == "ollama":
        return "ollama"
    base_url = endpoint.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return "openai"
    host = url_host(base_url)
    for _suffix, name in _PRESET_HOSTS:
        if preset_host(name, host):
            return name
    if is_local_host(host):
        return "local"
    return "custom"


def preset_host(preset, host):
    """True when host is (a host of) the cloud service of this preset."""
    for suffix, name in _PRESET_HOSTS:
        if name != preset:
            continue
        if suffix.startswith("."):
            if host.endswith(suffix):
                return True
        elif host == suffix or host.endswith("." + suffix):
            return True
    return False


def _with_scheme(base_url):
    """A base URL with its scheme settled.

    A URL typed without one gets http:// for hosts on this computer or the
    local network ("localhost:8080/v1") and https:// for everything else, so
    an API key and the pictures never cross the internet unencrypted
    because a scheme was left out. The cloud services' own hosts always get
    https://, even when http:// was typed: they only serve https.
    """
    if not base_url:
        return base_url
    if "://" not in base_url:
        scheme = "http" if is_local_host(url_host(base_url)) else "https"
        base_url = "%s://%s" % (scheme, base_url)
    scheme, rest = base_url.split("://", 1)
    if scheme.lower() == "http" and any(preset_host(name, url_host(base_url))
                                        for name in CLOUD_PRESETS):
        return "https://" + rest
    return base_url


def insecure_endpoint(endpoint):
    """True when an endpoint sends requests unencrypted (http://) beyond the local network."""
    base_url = endpoint_settings(endpoint)["base_url"]
    return (base_url.lower().startswith("http://")
            and not is_local_host(url_host(base_url)))


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def endpoint_settings(endpoint):
    """A copy of an endpoint with every setting filled in (see ENDPOINT_DEFAULTS).

    The preset is resolved (infer_preset) and supplies the kind (an explicit
    preset wins over the kind), the base URL when none is set, and the
    concurrency when none is set (None for llama.cpp-like servers: see
    endpoint_concurrency). A base URL without a scheme gets http:// on this
    computer or the local network and https:// elsewhere (see _with_scheme).
    max_output_tokens defaults to 4000, or 8000 for
    Claude models that cannot turn thinking off. Invalid values fall back to
    the defaults. The input is not changed, and calling this again on the
    result changes nothing.
    """
    source = endpoint if isinstance(endpoint, dict) else {}
    result = copy.deepcopy(ENDPOINT_DEFAULTS)
    for key, value in source.items():
        result[key] = copy.deepcopy(value)
    for key in ("name", "label", "kind", "preset", "base_url", "model", "api_key",
                "api_key_env", "api_key_file", "group"):
        value = result.get(key)
        result[key] = value.strip() if isinstance(value, str) else ""
    preset = infer_preset(result)
    info = ENDPOINT_PRESETS[preset]
    explicit = result["preset"].lower() == preset
    kind = result["kind"].lower()
    if explicit or not kind:
        kind = info["kind"]
    elif kind not in ENDPOINT_KINDS:
        kind = "openai"         # as older versions did with unknown kinds
    result["kind"] = kind
    result["preset"] = preset
    result["base_url"] = _with_scheme((result["base_url"] or info["base_url"]).rstrip("/"))
    if not _positive_int(result["concurrency"]):
        result["concurrency"] = info["concurrency"]
    edge = result["max_image_edge"]
    result["max_image_edge"] = (int(edge) if _number(edge) and 64 <= edge <= 8000
                                else DEFAULT_MAX_IMAGE_EDGE)
    timeout = result["timeout"]
    result["timeout"] = timeout if _number(timeout) and timeout > 0 else DEFAULT_TIMEOUT
    if not _positive_int(result["max_output_tokens"]):
        result["max_output_tokens"] = (
            THINKING_MAX_OUTPUT_TOKENS
            if kind == "claude" and claude_thinking(result["model"]) == "effort"
            else DEFAULT_MAX_OUTPUT_TOKENS)
    if not isinstance(result["extra"], dict):
        result["extra"] = {}
    for key in ("price_in", "price_out"):
        value = result[key]
        result[key] = float(value) if _number(value) and value >= 0 else None
    enabled = result["enabled"]
    result["enabled"] = enabled if isinstance(enabled, bool) else enabled not in (0, None, "")
    result["label"] = result["label"] or result["name"]
    return result


def _unique_name(wanted, taken):
    name = wanted
    number = 2
    while name.casefold() in taken:
        name = "%s-%d" % (wanted, number)
        number += 1
    taken.add(name.casefold())
    return name


def endpoint_list(config):
    """Every configured endpoint, as filled-in copies (endpoint_settings).

    Uses config["endpoints"] when the config has that list; otherwise the
    older config["endpoint"] (one object, or a round-robin list whose
    members become the group "default"), named "default", "default-2", ...
    Names are made unique (case-insensitively) by adding "-2", "-3", ...
    Disabled endpoints are included. Change config["endpoints"] (or
    config["endpoint"]) to change settings, not these copies.
    """
    endpoints = config.get("endpoints") if isinstance(config, dict) else None
    if isinstance(endpoints, list):
        items = [item for item in endpoints if isinstance(item, dict)]
        legacy_group = None
    else:
        legacy = config.get("endpoint") if isinstance(config, dict) else None
        if isinstance(legacy, list):
            items = [item for item in legacy if isinstance(item, dict)]
            legacy_group = "default"
        else:
            items = [legacy] if isinstance(legacy, dict) else []
            legacy_group = ""
    taken = set()
    result = []
    for position, item in enumerate(items, 1):
        endpoint = endpoint_settings(item)
        if legacy_group is None:
            wanted = endpoint["name"] or "endpoint-%d" % position
        else:
            wanted = endpoint["name"] or "default"
            if legacy_group and not endpoint["group"]:
                endpoint["group"] = legacy_group
        label_was_name = endpoint["label"] == endpoint["name"]
        endpoint["name"] = _unique_name(wanted, taken)
        if label_was_name:
            endpoint["label"] = endpoint["name"]
        result.append(endpoint)
    return result


def convert_legacy_endpoints(config):
    """Rewrite an older "endpoint" setting as "endpoints" (in place). True if changed.

    For tools that edit endpoints, which save the newer form. Each older
    endpoint keeps its settings and gets the name endpoint_list() gives it
    ("default", "default-2", ...); a round-robin list's members also get
    "group": "default". default_endpoint becomes "default" (unless set), so
    libraries keep using the same endpoints. An unused placeholder (no
    model, base URL or key) is dropped. Configs that already have
    "endpoints" are left alone.
    """
    if not isinstance(config, dict) or isinstance(config.get("endpoints"), list):
        return False
    legacy = config.pop("endpoint", None)
    items = legacy if isinstance(legacy, list) else [legacy]
    endpoints = []
    taken = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = copy.deepcopy(item)
        name = entry.get("name") if isinstance(entry.get("name"), str) else ""
        entry["name"] = _unique_name(name.strip() or "default", taken)
        if isinstance(legacy, list) and not entry.get("group"):
            entry["group"] = "default"
        if not any(isinstance(entry.get(key), str) and entry[key].strip()
                   for key in ("model", "base_url", "api_key", "api_key_env", "api_key_file")):
            continue
        endpoints.append(entry)
    config["endpoints"] = endpoints
    name = config.get("default_endpoint")
    if not isinstance(name, str) or not name.strip():
        config["default_endpoint"] = "default"
    return True


def get_endpoint(config, name):
    """The endpoint with this name (exact, else case-insensitive), or None."""
    if not isinstance(name, str) or not name.strip():
        return None
    endpoints = endpoint_list(config)
    for endpoint in endpoints:
        if endpoint["name"] == name:
            return endpoint
    wanted = name.strip().casefold()
    for endpoint in endpoints:
        if endpoint["name"].casefold() == wanted:
            return endpoint
    return None


def default_endpoint_name(config):
    """The endpoint (or group) name libraries use when they do not name one.

    config["default_endpoint"] when set; otherwise "default" for the older
    single-endpoint or round-robin forms. Without a default_endpoint, the
    newer form falls back to the first enabled endpoint on this computer or
    the local network, and to none ('') when there is no such endpoint:
    pictures are never sent to a cloud service nobody chose for them.
    """
    name = config.get("default_endpoint") if isinstance(config, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    if isinstance(config, dict) and not isinstance(config.get("endpoints"), list):
        return "default"
    for endpoint in endpoint_list(config):
        if endpoint["enabled"] and is_local_endpoint(endpoint):
            return endpoint["name"]
    return ""


def resolve_endpoints(config, name):
    """The enabled endpoints a name refers to: a group's members, else one endpoint.

    A group wins over an endpoint of the same name (the older round-robin
    form names its first endpoint and its group "default"). Unknown or
    disabled names give an empty list: images are never sent somewhere
    other than where the settings say.
    """
    if not isinstance(name, str) or not name.strip():
        return []
    wanted = name.strip().casefold()
    endpoints = endpoint_list(config)
    members = [endpoint for endpoint in endpoints
               if endpoint["group"] and endpoint["group"].casefold() == wanted]
    if not members:
        endpoint = get_endpoint(config, name)
        members = [endpoint] if endpoint is not None else []
    return [endpoint for endpoint in members if endpoint["enabled"]]


def endpoints_for_library(config, library=None):
    """The enabled endpoints that describe a library's images.

    The library's "endpoint" setting (an endpoint or group name), or the
    default endpoint when it is empty (or no library is given). An empty
    list when the name matches nothing enabled.
    """
    wanted = library.get("endpoint") if isinstance(library, dict) else None
    if not isinstance(wanted, str) or not wanted.strip():
        wanted = default_endpoint_name(config)
    return resolve_endpoints(config, wanted)


def endpoint_concurrency(endpoint, slots=None):
    """How many requests to keep in flight at once on an endpoint.

    The endpoint's "concurrency" setting when it has one; otherwise its
    preset's default, where llama.cpp-like servers use their slot count
    (slots, from llm.probe()) and fall back to 1. At most MAX_CONCURRENCY.
    """
    settings = endpoint_settings(endpoint)
    value = settings["concurrency"]
    if not _positive_int(value):
        value = slots if settings["preset"] in LLAMACPP_PRESETS and _positive_int(slots) else 1
    return min(value, MAX_CONCURRENCY)


def is_local_endpoint(endpoint):
    """True when an endpoint's server is on this computer or the local network.

    Cloud presets never are; otherwise the base URL's host decides, so a
    llama.cpp server rented on the internet does not count as local.
    """
    settings = endpoint_settings(endpoint)
    if settings["preset"] in CLOUD_PRESETS:
        return False
    return is_local_host(url_host(settings["base_url"]))


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Long hexadecimal text (older Azure OpenAI keys are 32 hex digits).
_HEX_KEY_RE = re.compile(r"^[0-9A-Fa-f]{24,}$")
# Long runs of letters, digits and -+/= with no underscore (variable names
# use underscores) and no . \ : (file paths have those).
_TOKEN_KEY_RE = re.compile(r"^[A-Za-z0-9+/=-]{20,}$")


def looks_like_key(text):
    """True for text shaped like an API key rather than a variable or file name."""
    text = (text or "").strip()
    if text.startswith(("sk-", "sk_", "AIza")):
        return True
    if "\\" in text or re.match(r"^([A-Za-z]:|~|\.{1,2})?[\\/]", text):
        return False            # a file path
    if _HEX_KEY_RE.match(text):
        return True
    if (_TOKEN_KEY_RE.match(text) and sum(char.isdigit() for char in text) >= 4
            and any(char.isalpha() for char in text)):
        return True
    return (len(text) >= 24 and any(char.islower() for char in text)
            and any(char.isupper() for char in text) and any(char.isdigit() for char in text))


def valid_env_name(name):
    """True for a usable environment variable name (that does not look like a key)."""
    return bool(ENV_NAME_RE.match(name or "")) and not looks_like_key(name)


def resolve_api_key(endpoint):
    """The API key for an endpoint and where it came from: (key, source).

    Looked up in this order: the environment variable named by api_key_env,
    the file named by api_key_file (its first line; a relative path is next
    to config.json), the literal api_key, and finally the preset's usual
    environment variable (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...) -- that
    last one only when the endpoint configures no key at all and its base
    URL is the preset's own service (a gateway or proxy at another address
    never gets it). A missing or unreadable key file gives ("", "file
    missing"): another key is never used in its place.

    source describes where the key came from ("env NAME", "file", "config",
    "none", "file missing", "file empty", "env NAME not set", "env (invalid
    name) not set"); it never contains the key. An api_key_env that is not
    a valid variable name (a key pasted there by mistake) is never echoed.
    Keys must never be logged, printed or stored.
    """
    endpoint = endpoint if isinstance(endpoint, dict) else {}

    def text(key):
        value = endpoint.get(key)
        return value.strip() if isinstance(value, str) else ""

    env_name = text("api_key_env")
    env_valid = valid_env_name(env_name)
    if env_name and env_valid:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, "env " + env_name
    file_name = text("api_key_file")
    if file_name:
        try:
            with open(resolve_config_relative(file_name), "r", encoding="utf-8-sig") as handle:
                content = handle.read()
        except (OSError, UnicodeDecodeError):
            return "", "file missing"
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if lines:
            return lines[0], "file"
        return "", "file empty"
    literal = text("api_key")
    if literal:
        return literal, "config"
    if env_name:
        return "", "env %s not set" % (env_name if env_valid else "(invalid name)")
    settings = endpoint_settings(endpoint)
    preset_env = ENDPOINT_PRESETS[settings["preset"]]["key_env"]
    if preset_env and preset_host(settings["preset"], url_host(settings["base_url"])):
        value = os.environ.get(preset_env, "").strip()
        if value:
            return value, "env " + preset_env
    return "", "none"


# ---------------------------------------------------------------------------
# File types and ignore patterns

def is_image_file(name):
    return name.lower().endswith(IMAGE_EXTENSIONS)


def is_heic_like(name):
    """True for HEIC/HEIF/AVIF names, which need a plugin and browser conversion."""
    return name.lower().endswith(HEIC_EXTENSIONS)


# Junk that NAS boxes and operating systems leave in photo folders. Each
# entry is used both at the library root and at any depth.
_DEFAULT_IGNORE_NAMES = (
    "@eaDir", ".@__thumb", "#recycle", "$RECYCLE.BIN", ".Trashes",
    ".thumbnails", "._*", "Thumbs.db",
)
DEFAULT_IGNORES = tuple(
    pattern for name in _DEFAULT_IGNORE_NAMES for pattern in (name, "*/" + name))


def normalize_ignore(patterns):
    """Return a clean list of ignore patterns with forward-slash separators.

    Either separator (/ or backslash) is accepted in the config so the same
    library entry works on every platform. Leading "./" or "/" and trailing
    "/" are dropped, since patterns are matched against relative paths.
    """
    result = []
    if isinstance(patterns, str):
        patterns = [patterns]
    for pattern in patterns or []:
        if not isinstance(pattern, str):
            continue
        pattern = re.sub("/+", "/", pattern.strip().replace("\\", "/"))
        while pattern.startswith("./"):
            pattern = pattern[2:]
        pattern = pattern.strip("/")
        if pattern:
            result.append(pattern)
    return result


def ignore_patterns(library):
    """All ignore patterns for a library: its own plus the default ones."""
    patterns = normalize_ignore(library.get("ignore"))
    if library.get("default_ignores", True):
        patterns.extend(DEFAULT_IGNORES)
    return patterns


def matches_ignore(rel_path, patterns):
    """True if a relative path ('/' separators) matches any pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def should_prune_dir(rel_dir, patterns):
    """True if the walk should not descend into a directory at all.

    A directory is pruned only when a pattern names it (its relative path
    matches the pattern) or a pattern ending in "/*" covers everything inside
    it (the part before "/*" matches it). Patterns that merely could match
    some file inside, such as "*_*", do not prune; files are still matched
    one by one with matches_ignore.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(rel_dir, pattern):
            return True
        if pattern.endswith("/*") and fnmatch.fnmatch(rel_dir, pattern[:-2]):
            return True
    return False


# ---------------------------------------------------------------------------
# Helper module and file access

def resolve_config_relative(path):
    """Resolve a path from config.json: relative paths are relative to its folder."""
    if not path:
        return path
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(config_path()), path)
    return os.path.normpath(path)


_warned_helper = set()


def find_helper(path):
    """The file a helper path from config.json refers to, as (full path, tried).

    A relative path is looked up next to config.json first. Older versions
    took it relative to the current directory, so that, and then the program
    folder, are tried next (with a one-time warning). full is None when no
    candidate exists; tried lists every path that was checked.
    """
    first = resolve_config_relative(path)
    tried = [first]
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        for folder in (os.getcwd(), BASE_DIR):
            candidate = os.path.normpath(os.path.join(folder, expanded))
            if candidate not in tried:
                tried.append(candidate)
    for candidate in tried:
        if os.path.isfile(candidate):
            if candidate != first and path not in _warned_helper:
                _warned_helper.add(path)
                print("Warning: the helper %r is not next to config.json (%s); using %s. "
                      "Give an absolute path, or one relative to the folder holding "
                      "config.json." % (path, os.path.dirname(config_path()), candidate),
                      file=sys.stderr)
            return candidate, tried
    return None, tried


def load_helper(path):
    """Load an optional helper module from a file path, or return None.

    A relative path is resolved against the folder holding config.json (see
    find_helper for the older locations that are also accepted). Exits with
    a message listing the paths tried when the file cannot be found.
    """
    if not path:
        return None
    full, tried = find_helper(path)
    if full is None:
        raise SystemExit("Helper module not found: %s (looked for %s)"
                         % (path, ", ".join(tried)))
    spec = importlib.util.spec_from_file_location("image_db_helper", full)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_image_bytes(helper, path):
    """Return the raw bytes for a source image file.

    The helper module can supply a `load_image(path)` function that returns
    the bytes for a source image. If the helper is absent, has no such
    function, or that function returns None, the file is read from disk.
    """
    if helper is not None:
        func = getattr(helper, "load_image", None)
        if func is not None:
            data = func(path)
            if data is not None:
                return data
    with open(path, "rb") as handle:
        return handle.read()


def thread_safe_helper(helper):
    """A stand-in for the helper whose load_image and stat hooks run one at a time.

    Files are read by several threads at once (scanning, describing), and
    helpers written for the old one-at-a-time scanner may not expect that.
    Decoding still runs in parallel. A helper module that sets THREAD_SAFE =
    True is used as is. The stand-in only has load_image and stat; use the
    helper itself for its other hooks.
    """
    if helper is None or getattr(helper, "THREAD_SAFE", False):
        return helper
    lock = threading.Lock()
    proxy = types.SimpleNamespace()

    def locked(func):
        def call(*args, **kwargs):
            with lock:
                return func(*args, **kwargs)
        return call

    for name in ("load_image", "stat"):
        func = getattr(helper, name, None)
        if func is not None:
            setattr(proxy, name, locked(func))
    return proxy


def stat_path(helper, path):
    """Return an os.stat_result-like object, or None if the path is missing.

    The helper module can supply a `stat(path)` function returning an object
    with at least st_size, st_mtime, and st_ctime attributes. If the helper
    is absent, has no such function, or that function returns None, os.stat
    is used; a missing file is reported as None rather than raising.
    """
    if helper is not None:
        func = getattr(helper, "stat", None)
        if func is not None:
            result = func(path)
            if result is not None:
                return result
    try:
        return os.stat(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Keeping secrets out of logs and error messages

REDACTED = "***"

_REDACT_PATTERNS = (
    # Bearer <token>, wherever it appears
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"),
    # Authorization: <credentials> (including "Basic <credentials>")
    re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?(?:basic\s+)?)"
               r"(?!bearer\b)[^\s\"',;&}]+"),
    # x-api-key: <key>, "api_key": "<key>", api-key=<key>, apikey=<key>
    re.compile(r"(?i)((?:x-)?api[-_]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"),
    # ...?key=<key> or &access_token=<key> in URLs
    re.compile(r"(?i)([?&](?:key|token|access_token|api[-_]?key)=)[^&#\s\"']+"),
    # Common provider key shapes (sk-..., sk-ant-...) wherever they appear
    re.compile(r"(\b)sk-[A-Za-z0-9_-]{16,}"),
    # user:password@ in a URL
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@:\s\"']+:[^/@\s\"']+(?=@)"),
)


def redact(text, secrets_list=()):
    """Return text with API keys and similar secrets masked.

    Every value in secrets_list (short values under 4 characters are not
    treated as secrets) is replaced, and so are the usual credential shapes:
    "Bearer xxx", "x-api-key: xxx", "api_key": "xxx", "?key=xxx",
    "api-key=xxx" and the user:password part of a URL.
    """
    if text is None:
        return ""
    text = str(text)
    if isinstance(secrets_list, str):
        secrets_list = [secrets_list]
    values = sorted({value for value in (secrets_list or ())
                     if isinstance(value, str) and len(value.strip()) >= 4},
                    key=len, reverse=True)
    for value in values:
        text = text.replace(value, REDACTED)
        stripped = value.strip()
        if stripped != value:
            text = text.replace(stripped, REDACTED)
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
    return text
