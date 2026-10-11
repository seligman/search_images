#!/usr/bin/env python3
"""Manage settings for the image database tools.

Run this script to edit the LLM endpoints, the optional helper module, and
the image libraries to monitor. Settings are stored in config.json (in the
SETTINGS_BASE folder) and are only written when you choose "Save and exit".

At any prompt, press Enter to keep the current value or type "-" to clear it.
API keys are never shown and are typed without echo; better still, keep them
in an environment variable or a key file and only name it here.

Editing endpoints saves them in the newer "endpoints" form (an older
"endpoint" setting is converted, keeping its settings and the name
"default").
"""

import getpass
import os
import sys
import urllib.parse

import common
import db
import library_ops

CLEAR = "-"
# Characters endpoint names may not contain (compare_models.py takes
# "name,name" and "name=N" arguments).
NAME_FORBIDDEN = ",="


# --- Prompts -----------------------------------------------------------------

def pause():
    input("Press Enter to continue...")


def choose(title, options):
    """Show a numbered menu and return the chosen index, or -1 for Back."""
    while True:
        print()
        print(title)
        for index, label in enumerate(options):
            print("  %d. %s" % (index + 1, label))
        print("  0. Back")
        answer = input("Choose: ").strip()
        if answer == "0":
            return -1
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print("Please enter a number from the list.")


def confirm(question):
    """Ask a yes/no question; only "y" or "yes" counts as yes."""
    return input(question + " [y/N]: ").strip().lower() in ("y", "yes")


def apply_answer(answer, current):
    """The new value for a prompt: blank keeps current, "-" clears it."""
    answer = (answer or "").strip()
    if not answer:
        return current
    if answer == CLEAR:
        return ""
    return answer


def prompt_text(label, current, shown=None):
    """Ask for a text value; shown (default: current) is what the prompt displays."""
    shown = (shown if shown is not None else current) or "(empty)"
    return apply_answer(input(label + " [" + shown + "]: "), current)


def secret_status(value):
    """How a secret is shown: whether it is set, never its value."""
    return "set" if value else "(empty)"


def prompt_secret(label, current):
    """Ask for a secret without echoing it or showing the current value."""
    answer = getpass.getpass("%s [%s; typing is hidden]: " % (label, secret_status(current)))
    return apply_answer(answer, current)


# --- Endpoints ---------------------------------------------------------------

def endpoint_entries(config):
    """The config's endpoint list, config["endpoints"] (edit these dicts to edit the config).

    An older "endpoint" setting (one endpoint or a round-robin list) is
    converted first (common.convert_legacy_endpoints), so saving writes the
    newer form.
    """
    common.convert_legacy_endpoints(config)
    endpoints = config.get("endpoints")
    if not isinstance(endpoints, list):
        endpoints = config["endpoints"] = []
    return endpoints


def find_endpoint(config, name):
    """The endpoint entry with this name (case-insensitive) in config["endpoints"], or None."""
    wanted = (name or "").strip().casefold()
    for entry in endpoint_entries(config):
        if str(entry.get("name", "")).strip().casefold() == wanted:
            return entry
    return None


def key_status(endpoint):
    """Where an endpoint's API key comes from ("env NAME", "file", ...); never the key."""
    return common.resolve_api_key(endpoint)[1]


def endpoint_summary(endpoint):
    """One line describing an endpoint. The API key is never included."""
    settings = common.endpoint_settings(endpoint)
    preset = common.ENDPOINT_PRESETS[settings["preset"]]["label"]
    text = "%s: %s, model=%s, base URL=%s, API key: %s" % (
        settings["name"] or "(unnamed)", preset, settings["model"] or "(empty)",
        common.redact(settings["base_url"]) or "(not set)", key_status(endpoint))
    if settings["group"]:
        text += ", group=" + settings["group"]
    if not settings["enabled"]:
        text += ", disabled"
    return text


def endpoint_name_error(config, name, skip=None):
    """Why name cannot be used for an endpoint, or None if it can.

    Names must be non-empty, unique (ignoring case) and free of "," and "=".
    skip is the endpoint being renamed, which may keep its own name.
    """
    name = (name or "").strip()
    if not name:
        return "An endpoint needs a name."
    if any(char in name for char in NAME_FORBIDDEN):
        return "Endpoint names cannot contain %s." % " or ".join(NAME_FORBIDDEN)
    for entry in endpoint_entries(config):
        if entry is not skip and str(entry.get("name", "")).strip().casefold() == name.casefold():
            return "Another endpoint is already called %s." % entry.get("name")
    return None


def add_endpoint(config, preset="llamacpp", name=None):
    """Add an endpoint using a preset and return it.

    The name defaults to the preset's name (made unique). The first endpoint
    becomes the default one. Raises ValueError for an unknown preset or a
    name that cannot be used.
    """
    if preset not in common.ENDPOINT_PRESETS:
        raise ValueError("Unknown preset: %s" % preset)
    entries = endpoint_entries(config)
    if name is None:
        name, number = preset, 2
        while find_endpoint(config, name) is not None:
            name = "%s-%d" % (preset, number)
            number += 1
    error = endpoint_name_error(config, name)
    if error:
        raise ValueError(error)
    entry = {"name": name.strip(), "preset": preset, "model": ""}
    entries.append(entry)
    current = config.get("default_endpoint")
    if not common.resolve_endpoints(config, current if isinstance(current, str) else ""):
        # Only a local endpoint, or the very first one, becomes the default on
        # its own: libraries are never moved to a cloud service silently.
        if common.is_local_endpoint(entry) or len(entries) == 1:
            config["default_endpoint"] = entry["name"]
    return entry


def libraries_using(config, name):
    """The libraries whose "endpoint" setting names this endpoint or group."""
    wanted = (name or "").strip().casefold()
    return [library for library in config.get("libraries", [])
            if str(library.get("endpoint") or "").strip().casefold() == wanted and wanted]


def libraries_using_default(config):
    """The libraries that name no endpoint, and so use the default one."""
    return [library for library in config.get("libraries", [])
            if not str(library.get("endpoint") or "").strip()]


def is_default_endpoint(config, name):
    """True when name is the config's default_endpoint (ignoring case)."""
    default = str(config.get("default_endpoint") or "").strip().casefold()
    return bool(default) and default == (name or "").strip().casefold()


def default_after_removal(config, name):
    """The endpoint that becomes the default when the default endpoint name is removed.

    The first other enabled endpoint on this computer or the local network,
    or '' (no default) when there is none: libraries are never moved to a
    cloud service nobody chose for them.
    """
    wanted = (name or "").strip().casefold()
    for entry in endpoint_entries(config):
        settings = common.endpoint_settings(entry)
        if (settings["name"].casefold() != wanted and settings["enabled"]
                and common.is_local_endpoint(settings)):
            return settings["name"]
    return ""


def remove_endpoint(config, name):
    """Remove an endpoint; returns it. Raises ValueError if there is none by that name.

    When it was the default, default_after_removal() picks the new default
    (possibly none). Libraries that name it keep their setting (their images
    are then not described until another endpoint gets that name): images
    are never sent to an endpoint the settings do not name.
    """
    entry = find_endpoint(config, name)
    if entry is None:
        raise ValueError("No endpoint is called %s." % name)
    if is_default_endpoint(config, entry.get("name", "")):
        config["default_endpoint"] = default_after_removal(config, entry.get("name", ""))
    endpoint_entries(config).remove(entry)
    return entry


def rename_endpoint(config, old, new):
    """Rename an endpoint, updating default_endpoint and libraries that name it."""
    entry = find_endpoint(config, old)
    if entry is None:
        raise ValueError("No endpoint is called %s." % old)
    error = endpoint_name_error(config, new, skip=entry)
    if error:
        raise ValueError(error)
    old_key = str(entry.get("name", "")).strip().casefold()
    entry["name"] = new.strip()
    if str(config.get("default_endpoint") or "").strip().casefold() == old_key:
        config["default_endpoint"] = entry["name"]
    for library in config.get("libraries", []):
        if str(library.get("endpoint") or "").strip().casefold() == old_key:
            library["endpoint"] = entry["name"]
    return entry


def set_default_endpoint(config, name):
    """Make an endpoint (or a group) the one libraries use unless they name another."""
    if not common.resolve_endpoints(config, name) and find_endpoint(config, name) is None:
        raise ValueError("No endpoint or group is called %s." % name)
    entry = find_endpoint(config, name)
    config["default_endpoint"] = entry["name"] if entry is not None else name.strip()


def _origin(url):
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)
    except ValueError:
        return None


def forget_key_if_moved(endpoint, old_url):
    """Remove a typed API key when the endpoint's address is no longer old_url's.

    The address counts as moved when the scheme, host or port differs.
    Returns True if a key was removed.
    """
    new = common.endpoint_settings(endpoint)["base_url"]
    if endpoint.get("api_key") and _origin(old_url) != _origin(new):
        endpoint.pop("api_key", None)
        return True
    return False


def set_base_url(endpoint, url):
    """Change an endpoint's base URL. Returns True if its stored API key was cleared.

    A key typed into the settings (api_key) is only for the service it was
    entered for: when the URL points somewhere else (scheme, host or port),
    it is removed so it is never sent to the new address. Keys read from an
    environment variable or a file are kept.
    """
    old = common.endpoint_settings(endpoint)["base_url"]
    if url:
        endpoint["base_url"] = url
    else:
        endpoint.pop("base_url", None)
    return forget_key_if_moved(endpoint, old)


def set_preset(endpoint, preset):
    """Change an endpoint's kind of service. Returns True if its stored API key was cleared.

    Without a base URL of its own, the endpoint then uses the new preset's
    address, so a typed key is removed when the address changes, as with
    set_base_url.
    """
    if preset not in common.ENDPOINT_PRESETS:
        raise ValueError("Unknown preset: %s" % preset)
    old = common.endpoint_settings(endpoint)["base_url"]
    endpoint["preset"] = preset
    endpoint.pop("kind", None)
    return forget_key_if_moved(endpoint, old)


def _inside(path, folder):
    path = os.path.normcase(os.path.abspath(path))
    folder = os.path.normcase(os.path.abspath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def set_setting(endpoint, key, value):
    """Set an endpoint setting; None or "" removes it (so the default applies)."""
    if value is None or value == "":
        endpoint.pop(key, None)
    else:
        endpoint[key] = value


def parse_number(text, kind=int, minimum=0):
    """A number typed at a prompt: None for blank, kind(text) otherwise.

    Raises ValueError for text that is not a number of that kind or is
    below minimum.
    """
    text = (text or "").strip()
    if not text:
        return None
    value = kind(text)
    if value < minimum:
        raise ValueError("The value must be at least %s." % minimum)
    return value


def prompt_number(label, current, kind=int, minimum=0, automatic="automatic"):
    """Ask for a number. Enter keeps current; "-" goes back to the default (None)."""
    shown = automatic if current is None else str(current)
    while True:
        answer = input("%s [%s]: " % (label, shown)).strip()
        if not answer:
            return current
        if answer == CLEAR:
            return None
        try:
            return parse_number(answer, kind, minimum)
        except ValueError:
            print("Please enter a number (at least %s), or - for %s." % (minimum, automatic))


def choose_preset(current=None):
    """Ask which preset an endpoint uses. Returns the preset name, or None for Back."""
    names = list(common.ENDPOINT_PRESETS)
    options = []
    for name in names:
        info = common.ENDPOINT_PRESETS[name]
        options.append("%s%s - %s" % (info["label"], " (now)" if name == current else "",
                                      info["notes"]))
    pick = choose("Which kind of service?", options)
    return None if pick == -1 else names[pick]


def configure_key(endpoint):
    """Choose where an endpoint's API key comes from. The key is never shown."""
    pick = choose("API key (now: %s)" % key_status(endpoint), [
        "Read it from an environment variable (recommended)",
        "Read it from a file that holds only the key",
        "Type it in (stored in config.json)",
        "Clear these settings (the service's usual variable, if any, still applies)"])
    if pick == 0:
        preset = common.endpoint_settings(endpoint)["preset"]
        suggested = endpoint.get("api_key_env") or common.ENDPOINT_PRESETS[preset]["key_env"]
        # Never show a key that was pasted here by mistake earlier.
        shown = suggested if common.valid_env_name(suggested) else "(not a valid name)"
        name = prompt_text("Environment variable name", suggested or "", shown=shown)
        if name and not common.valid_env_name(name):
            if common.looks_like_key(name):
                print("That looks like a key, not a variable name; nothing was saved. Use "
                      "the key file or type-it-in option (typing is hidden) instead.")
            else:
                print("That is not a valid environment variable name (letters, digits "
                      "and _); nothing was saved.")
            return
        set_setting(endpoint, "api_key_env", name)
        if name:
            endpoint.pop("api_key", None)
            endpoint.pop("api_key_file", None)
    elif pick == 1:
        path = prompt_text("Key file (relative paths are next to config.json)",
                           endpoint.get("api_key_file", ""))
        set_setting(endpoint, "api_key_file", path)
        if path:
            endpoint.pop("api_key", None)
            endpoint.pop("api_key_env", None)
            full = common.resolve_config_relative(path)
            if not os.path.isfile(full):
                print("Warning: that file does not exist yet.")
            if _inside(full, common.BASE_DIR) and not _inside(full, common.data_dir()):
                print("Warning: that file is inside the program folder, where git could "
                      "pick it up. Keep key files outside it.")
    elif pick == 2:
        key = prompt_secret("API key", endpoint.get("api_key", ""))
        set_setting(endpoint, "api_key", key)
        if key:
            endpoint.pop("api_key_env", None)
            endpoint.pop("api_key_file", None)
    elif pick == 3:
        for name in ("api_key", "api_key_env", "api_key_file"):
            endpoint.pop(name, None)


def test_endpoint_now(endpoint):
    """Describe the sample image with an endpoint and print the outcome."""
    # Imported here: they need Pillow, which the rest of the settings do not.
    import scan
    config = {"endpoints": [dict(endpoint)]}
    scan.test_endpoint(config, endpoint.get("name", ""))


def edit_endpoint(config, endpoint):
    while True:
        settings = common.endpoint_settings(endpoint)
        preset = common.ENDPOINT_PRESETS[settings["preset"]]
        concurrency = endpoint.get("concurrency")
        options = [
            "Name (now: %s)" % (endpoint.get("name") or "(none)"),
            "Kind of service (now: %s)" % preset["label"],
            "Model (now: %s)" % (settings["model"] or "(empty)"),
            "Base URL (now: %s)" % (common.redact(settings["base_url"]) or "(not set)"),
            "API key (now: %s)" % key_status(endpoint),
            "Requests at once (now: %s)" % (concurrency or "automatic"),
            "Largest image edge in pixels (now: %d)" % settings["max_image_edge"],
            "Timeout in seconds (now: %g)" % settings["timeout"],
            "Most output tokens (now: %d)" % settings["max_output_tokens"],
            "Prices per million tokens, for cost estimates (now: %s / %s)" % (
                settings["price_in"] if settings["price_in"] is not None else "-",
                settings["price_out"] if settings["price_out"] is not None else "-"),
            "Group (now: %s)" % (settings["group"] or "none"),
            "Disable" if settings["enabled"] else "Enable",
            "Test it now (sends a sample picture)",
        ]
        index = choose("Endpoint %s" % endpoint_summary(endpoint), options)
        if index == -1:
            return
        try:
            if index == 0:
                name = input("New name [%s]: " % endpoint.get("name", "")).strip()
                if name:
                    rename_endpoint(config, endpoint.get("name", ""), name)
            elif index == 1:
                picked = choose_preset(settings["preset"])
                if picked is not None and set_preset(endpoint, picked):
                    print("The stored API key was removed because the address changed; "
                          "enter it again if this service needs it.")
            elif index == 2:
                set_setting(endpoint, "model",
                            prompt_text("Model name", endpoint.get("model", "")))
            elif index == 3:
                current = endpoint.get("base_url", "")
                # A key in the URL (?key=..., user:password@) is not shown.
                url = prompt_text("Base URL (blank for the preset's default)", current,
                                  shown=common.redact(current))
                if set_base_url(endpoint, url):
                    print("The stored API key was removed because the address changed; "
                          "enter it again if this service needs it.")
                if common.insecure_endpoint(endpoint):
                    print("Warning: http:// to a server outside this computer and the local "
                          "network is not encrypted: pictures and the API key would cross "
                          "the network unprotected. Use https:// if the service has it.")
            elif index == 4:
                configure_key(endpoint)
            elif index == 5:
                set_setting(endpoint, "concurrency", prompt_number(
                    "Requests at once (- for automatic)", concurrency, minimum=1))
            elif index == 6:
                set_setting(endpoint, "max_image_edge", prompt_number(
                    "Largest image edge", endpoint.get("max_image_edge"), minimum=64,
                    automatic=str(common.DEFAULT_MAX_IMAGE_EDGE)))
            elif index == 7:
                set_setting(endpoint, "timeout", prompt_number(
                    "Timeout in seconds", endpoint.get("timeout"), kind=float, minimum=1,
                    automatic=str(common.DEFAULT_TIMEOUT)))
            elif index == 8:
                set_setting(endpoint, "max_output_tokens", prompt_number(
                    "Most output tokens", endpoint.get("max_output_tokens"), minimum=1))
            elif index == 9:
                set_setting(endpoint, "price_in", prompt_number(
                    "USD per million input tokens", endpoint.get("price_in"), kind=float,
                    automatic="unknown"))
                set_setting(endpoint, "price_out", prompt_number(
                    "USD per million output tokens", endpoint.get("price_out"), kind=float,
                    automatic="unknown"))
            elif index == 10:
                set_setting(endpoint, "group", prompt_text(
                    "Group (endpoints in one group share the work)", endpoint.get("group", "")))
            elif index == 11:
                if settings["enabled"]:
                    endpoint["enabled"] = False
                else:
                    endpoint.pop("enabled", None)
            elif index == 12:
                test_endpoint_now(endpoint)
                pause()
        except ValueError as error:
            print(error)


def configure_endpoints(config):
    while True:
        entries = endpoint_entries(config)
        default = common.default_endpoint_name(config) or "(none)"
        options = ["Edit %s" % endpoint_summary(entry) for entry in entries]
        options += ["Add an endpoint", "Remove an endpoint", "Set the default endpoint"]
        index = choose("LLM endpoints (default: %s)" % default, options)
        if index == -1:
            return
        if index < len(entries):
            edit_endpoint(config, entries[index])
        elif index == len(entries):
            preset = choose_preset()
            if preset is not None:
                entry = add_endpoint(config, preset)
                print("Added endpoint %s. Set its model next." % entry["name"])
                if is_default_endpoint(config, entry["name"]):
                    print("It is the default endpoint: libraries that name none use it.")
                edit_endpoint(config, entry)
        elif not entries:
            print("There are no endpoints yet.")
        elif index == len(entries) + 1:
            pick = choose("Remove which endpoint?", [entry.get("name", "") for entry in entries])
            if pick == -1:
                continue
            name = entries[pick].get("name", "")
            users = libraries_using(config, name)
            question = "Remove endpoint %s?" % name
            if users:
                question += (" Libraries %s name it; their images are not described until "
                             "you pick another endpoint for them."
                             % ", ".join(common.library_label(library) for library in users))
            if is_default_endpoint(config, name):
                relying = libraries_using_default(config)
                after = default_after_removal(config, name)
                question += " It is the default endpoint%s; %s." % (
                    " (used by %s)" % ", ".join(common.library_label(library)
                                                for library in relying) if relying else "",
                    ("%s becomes the default" % after) if after else
                    "no endpoint will be the default, so libraries that name none are not "
                    "described until you choose one")
            if confirm(question):
                remove_endpoint(config, name)
        else:
            pick = choose("Which endpoint should libraries use by default?",
                          [entry.get("name", "") for entry in entries])
            if pick != -1:
                set_default_endpoint(config, entries[pick].get("name", ""))


def configure_helper(config):
    config["helper"] = prompt_text(
        "Helper module path (blank for none)", config.get("helper", ""))
    if config["helper"] and not os.path.isfile(
            common.resolve_config_relative(config["helper"])):
        print("Warning: that file does not exist yet. A relative path is "
              "relative to the folder holding config.json.")
        pause()


# --- Libraries ---------------------------------------------------------------

def library_name_error(config, name, skip=None):
    """Why name cannot be used for a library, or None if it can.

    Names must be non-empty and unique (ignoring case). skip is the library
    being renamed, which may keep its own name. The web UI uses the same
    rule (library_ops.library_name_error).
    """
    return library_ops.library_name_error(config, name, skip)


def library_folder_error(path):
    """Why a folder cannot be used for a library, or None (see library_ops.path_error).

    Unlike the web UI, a relative path is accepted, as before: it is
    relative to the folder the tools are run from.
    """
    if not (path or "").strip():
        return "A library needs a folder."
    return library_ops.path_error(path, require_absolute=False)


def add_library(config, path, name=None):
    """Add a library and give it an id; return it.

    The name defaults to common.default_library_name(path). Raises
    ValueError if the folder is empty or cannot be used (inside the data
    or program folder), or the name is missing or taken.
    """
    path = (path or "").strip()
    error = library_folder_error(path)
    if error:
        raise ValueError(error)
    name = (name or "").strip() or common.default_library_name(path)
    error = library_name_error(config, name)
    if error:
        raise ValueError(error)
    library = common.apply_library_defaults({"name": name, "path": path})
    config.setdefault("libraries", []).append(library)
    common.ensure_library_ids(config)
    return library


def rename_library(config, index, name):
    """Rename a library. Its id, and so its indexed images, stay the same."""
    library = config["libraries"][index]
    error = library_name_error(config, name, skip=library)
    if error:
        raise ValueError(error)
    library["name"] = name.strip()
    return library


def set_library_folder(config, index, path):
    """Point a library at another folder (for example after a move).

    Images are matched by their path inside the library, so descriptions
    are kept for files whose relative path is unchanged.
    """
    path = (path or "").strip()
    error = library_folder_error(path)
    if error:
        raise ValueError(error)
    library = config["libraries"][index]
    library["path"] = path
    return library


def remove_library(config, index):
    """Remove a library from the settings and return it. The database is not touched."""
    return config["libraries"].pop(index)


def removal_warning(library):
    return ("Remove %s from the settings? Its indexed images, descriptions and "
            "thumbnails stay in the database as an orphan library until you "
            "delete them (or re-attach them to a library) from the web UI."
            % common.library_label(library))


def library_lines(config):
    """Lines describing each library: number, name, folder, id, state."""
    libraries = config.get("libraries", [])
    if not libraries:
        return ["  (none)"]
    lines = []
    for number, library in enumerate(libraries, 1):
        state = "" if library.get("enabled", True) else ", disabled"
        lines.append("  %d. %s -> %s  (id %s%s)" % (
            number, common.library_label(library), library.get("path", ""),
            library.get("id") or "assigned when saved", state))
    return lines


def set_library_endpoint(config, index, name):
    """Make a library use an endpoint or group ("" for the default one)."""
    name = (name or "").strip()
    if name and not common.resolve_endpoints(config, name):
        raise ValueError("No enabled endpoint or group is called %s." % name)
    entry = find_endpoint(config, name) if name else None
    library = config["libraries"][index]
    library["endpoint"] = entry["name"] if entry is not None else name
    return library


def edit_library(config, index):
    while True:
        library = config["libraries"][index]
        enabled = library.get("enabled", True)
        notes = library.get("context_notes") or ""
        pick = choose("Edit library %s" % common.library_label(library), [
            "Rename (now: %s)" % library.get("name", ""),
            "Change folder (now: %s)" % library.get("path", ""),
            "Disable (hide it and stop scanning it)" if enabled else "Enable",
            "Describe with endpoint (now: %s)" % (library.get("endpoint") or "the default"),
            "Metadata (now: %s)" % (library.get("metadata") or "core"),
            "Context notes for the LLM (now: %s)" % ("set" if notes else "none"),
        ])
        if pick == -1:
            return
        try:
            if pick == 0:
                name = input("New name [%s]: " % library.get("name", "")).strip()
                if name:
                    rename_library(config, index, name)
            elif pick == 1:
                path = input("New folder [%s]: " % library.get("path", "")).strip()
                if path:
                    set_library_folder(config, index, path)
            elif pick == 2:
                library["enabled"] = not enabled
            elif pick == 3:
                name = prompt_text("Endpoint or group name (- for the default)",
                                   library.get("endpoint", ""))
                set_library_endpoint(config, index, name)
            elif pick == 4:
                choice = choose("Metadata to ask for", [
                    "core - title, description, text, type, setting, people, tags, scores",
                    "extended - core plus counted objects, activities, landmarks, a "
                    "location guess and mood (slower, more tokens)"])
                if choice != -1:
                    library["metadata"] = ("core", "extended")[choice]
            else:
                print("Context notes are sent to the LLM service with every image of this "
                      "library (for example \"Family photos from a coastal town\").")
                library["context_notes"] = prompt_text("Context notes", notes)
        except ValueError as error:
            print(error)


def manage_libraries(config):
    while True:
        libraries = config.setdefault("libraries", [])
        print()
        print("Image libraries:")
        for line in library_lines(config):
            print(line)
        index = choose("Library actions",
                       ["Add a library", "Edit a library", "Remove a library"])
        if index == -1:
            return
        if index == 0:
            path = input("Folder to scan: ").strip()
            if not path:
                print("A folder is required.")
                continue
            error = library_folder_error(path)
            if error:
                print(error)
                continue
            warning = library_ops.reachability_warning(path)
            if warning:
                print("Warning: " + warning)
            for warning in library_ops.path_warnings(config, path):
                print("Warning: " + warning)
            suggested = common.default_library_name(path)
            name = input("Library name [%s]: " % suggested).strip() or suggested
            try:
                library = add_library(config, path, name)
            except ValueError as error:
                print(error)
                continue
            print("Added %s." % library["name"])
        elif index == 1 and libraries:
            pick = choose("Edit which library?",
                          [common.library_label(library) for library in libraries])
            if pick != -1:
                edit_library(config, pick)
        elif index == 2 and libraries:
            pick = choose("Remove which library?",
                          [common.library_label(library) for library in libraries])
            if pick != -1 and confirm(removal_warning(libraries[pick])):
                remove_library(config, pick)


# --- Main menu ---------------------------------------------------------------

def settings_lines(config):
    """The current settings as lines of text. API keys are never included."""
    lines = ["LLM endpoints (default: %s):" % (common.default_endpoint_name(config) or "none")]
    endpoints = common.endpoint_list(config)
    if not endpoints:
        lines.append("  (none)")
    for number, endpoint in enumerate(endpoints, 1):
        lines.append("  %d. %s" % (number, endpoint_summary(endpoint)))
    lines.append("Helper module: " + (config.get("helper", "") or "(none)"))
    lines.append("Libraries:")
    lines.extend(library_lines(config))
    return lines


def show_settings(config):
    print()
    for line in settings_lines(config):
        print(line)
    pause()


def save(config, start_ids=None):
    """Give new libraries their ids and write config.json.

    Ids already saved in config.json win: a library this session loaded
    (its id is in start_ids) whose id is not the saved one takes the saved
    id of the library with the same folder or name, so its indexed images
    never become an orphan. Libraries added in this session keep theirs.
    """
    if os.path.exists(common.config_path()):
        try:
            saved = common.load_config()
        except common.ConfigError:
            saved = None
        if saved is not None:
            current = {library.get("id") for library in config.get("libraries", [])}
            common.copy_library_ids(config, saved, keep=current - set(start_ids or ()))
    common.ensure_library_ids(config)
    common.save_config(config)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        # Done first, before anything can be renamed: the first run imports
        # the old database, matching its libraries by their current names,
        # and gives every library its permanent id.
        db.init()
    except common.ConfigError as error:
        raise SystemExit("Error: %s" % error)
    except db.DatabaseError as error:
        print("Warning: %s" % error)
    try:
        config = common.load_config()
    except common.ConfigError as error:
        raise SystemExit("Error: %s" % error)
    common.ensure_library_ids(config)
    start_ids = {library["id"] for library in config.get("libraries", [])}
    print("Settings file: " + common.config_path())
    print("At any prompt, press Enter to keep the current value or type - to clear it.")
    while True:
        index = choose("Image database settings", [
            "Configure LLM endpoints",
            "Configure helper module",
            "Manage image libraries",
            "Show current settings",
            "Save and exit"])
        if index == 0:
            configure_endpoints(config)
        elif index == 1:
            configure_helper(config)
        elif index == 2:
            manage_libraries(config)
        elif index == 3:
            show_settings(config)
        elif index == 4:
            save(config, start_ids)
            print("Saved to " + common.config_path())
            return
        elif index == -1:
            if confirm("Exit without saving?"):
                return


if __name__ == "__main__":
    main()
