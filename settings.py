#!/usr/bin/env python3
"""Manage settings for the image database tools.

Run this script to edit the LLM endpoint(s), the optional helper module, and
the image libraries to monitor. Settings are stored in config.json (in the
SETTINGS_BASE folder) and are only written when you choose "Save and exit".

At any prompt, press Enter to keep the current value or type "-" to clear it.
API keys are never shown and are typed without echo.
"""

import getpass
import os
import sys

import common
import db

ENDPOINT_KINDS = ["openai", "claude", "ollama"]
CLEAR = "-"


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

def endpoint_list(config):
    """The configured endpoints as a list.

    config["endpoint"] is either one endpoint or a list of them (requests
    then rotate between them). The dicts returned are the ones in config,
    so editing them edits the config.
    """
    endpoint = config.get("endpoint")
    if isinstance(endpoint, list):
        return endpoint
    if not isinstance(endpoint, dict):
        endpoint = config["endpoint"] = common.default_config()["endpoint"]
    return [endpoint]


def endpoint_summary(endpoint):
    """One line describing an endpoint. The API key is never included."""
    return "kind=%s, model=%s, base URL=%s, API key %s" % (
        endpoint.get("kind", "") or "(empty)",
        endpoint.get("model", "") or "(empty)",
        common.redact(endpoint.get("base_url", "")) or "(service default)",
        secret_status(endpoint.get("api_key")))


def add_endpoint(config):
    """Add an endpoint (turning a single endpoint into a list); return it."""
    endpoints = endpoint_list(config)
    new = common.default_config()["endpoint"]
    if not isinstance(config.get("endpoint"), list):
        config["endpoint"] = endpoints = list(endpoints)
    endpoints.append(new)
    return new


def remove_endpoint(config, index):
    """Remove one endpoint from a list; at least one must remain."""
    endpoints = endpoint_list(config)
    if len(endpoints) <= 1:
        raise ValueError("At least one endpoint is needed.")
    return endpoints.pop(index)


def edit_endpoint(endpoint):
    while True:
        index = choose(
            "LLM endpoint (%s)" % endpoint_summary(endpoint),
            ["Set endpoint kind", "Set model name", "Set base URL", "Set API key"])
        if index == -1:
            return
        if index == 0:
            pick = choose("Endpoint kind (use openai for a llama.cpp server)",
                          ENDPOINT_KINDS)
            if pick != -1:
                endpoint["kind"] = ENDPOINT_KINDS[pick]
        elif index == 1:
            endpoint["model"] = prompt_text("Model name", endpoint.get("model", ""))
        elif index == 2:
            current = endpoint.get("base_url", "")
            # A key in the URL (?key=..., user:password@) is not shown.
            endpoint["base_url"] = prompt_text(
                "Base URL (blank for the service default)", current,
                shown=common.redact(current))
        elif index == 3:
            endpoint["api_key"] = prompt_secret("API key", endpoint.get("api_key", ""))


def configure_endpoints(config):
    while True:
        endpoints = endpoint_list(config)
        options = ["Edit endpoint %d: %s" % (number, endpoint_summary(endpoint))
                   for number, endpoint in enumerate(endpoints, 1)]
        options.append("Add another endpoint (requests rotate between them)")
        if len(endpoints) > 1:
            options.append("Remove an endpoint")
        index = choose("LLM endpoints", options)
        if index == -1:
            return
        if index < len(endpoints):
            edit_endpoint(endpoints[index])
        elif index == len(endpoints):
            edit_endpoint(add_endpoint(config))
        else:
            pick = choose("Remove which endpoint?",
                          [endpoint_summary(endpoint) for endpoint in endpoints])
            if pick != -1 and confirm("Remove endpoint %d?" % (pick + 1)):
                remove_endpoint(config, pick)


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
    being renamed, which may keep its own name.
    """
    name = (name or "").strip()
    if not name:
        return "A library needs a name."
    for library in config.get("libraries", []):
        if library is not skip and library.get("name", "").strip().casefold() == name.casefold():
            return "Another library is already called %s." % library.get("name")
    return None


def add_library(config, path, name=None):
    """Add a library and give it an id; return it.

    The name defaults to common.default_library_name(path). Raises
    ValueError if the folder is empty or the name is missing or taken.
    """
    path = (path or "").strip()
    if not path:
        raise ValueError("A library needs a folder.")
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
    if not path:
        raise ValueError("A library needs a folder.")
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


def edit_library(config, index):
    while True:
        library = config["libraries"][index]
        enabled = library.get("enabled", True)
        pick = choose("Edit library %s" % common.library_label(library), [
            "Rename (now: %s)" % library.get("name", ""),
            "Change folder (now: %s)" % library.get("path", ""),
            "Disable (hide it and stop scanning it)" if enabled else "Enable",
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
            else:
                library["enabled"] = not enabled
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
            if not os.path.isdir(os.path.expanduser(path)):
                print("Warning: that folder cannot be reached right now.")
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
    lines = ["LLM endpoints:"]
    for number, endpoint in enumerate(endpoint_list(config), 1):
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
