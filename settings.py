#!/usr/bin/env python3
"""Manage settings for the image database tools.

Run this script to edit the LLM endpoint, the optional helper module, and the
image libraries to monitor. Settings are stored in config.json.
"""

import os

import common


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


def prompt_text(label, current):
    shown = current if current else "(empty)"
    value = input(label + " [" + shown + "]: ").strip()
    return value if value else current


def configure_endpoint(config):
    endpoint = config["endpoint"]
    while True:
        index = choose(
            "LLM endpoint (kind=%s, model=%s)" % (
                endpoint.get("kind", ""), endpoint.get("model", "") or "(empty)"),
            ["Set endpoint kind", "Set model name", "Set base URL", "Set API key"])
        if index == -1:
            return
        if index == 0:
            kinds = ["openai", "claude", "ollama"]
            pick = choose("Endpoint kind (use openai for a llama.cpp server)", kinds)
            if pick != -1:
                endpoint["kind"] = kinds[pick]
        elif index == 1:
            endpoint["model"] = prompt_text("Model name", endpoint.get("model", ""))
        elif index == 2:
            endpoint["base_url"] = prompt_text(
                "Base URL (blank for the service default)",
                endpoint.get("base_url", ""))
        elif index == 3:
            endpoint["api_key"] = prompt_text("API key", endpoint.get("api_key", ""))


def configure_helper(config):
    config["helper"] = prompt_text(
        "Helper module path (blank for none)", config.get("helper", ""))
    if config["helper"] and not os.path.isfile(config["helper"]):
        print("Warning: that file does not exist yet.")
        pause()


def manage_libraries(config):
    while True:
        libraries = config["libraries"]
        print()
        print("Image libraries:")
        if not libraries:
            print("  (none)")
        for index, library in enumerate(libraries):
            print("  %d. %s -> %s" % (index + 1, library["name"], library["path"]))
        index = choose("Library actions",
                       ["Add a library", "Edit a library", "Remove a library"])
        if index == -1:
            return
        if index == 0:
            name = input("Library name: ").strip()
            path = input("Folder to scan: ").strip()
            if name and path:
                libraries.append({"name": name, "path": path})
            else:
                print("Both a name and a folder are required.")
        elif index == 1 and libraries:
            pick = choose("Edit which library?", [l["name"] for l in libraries])
            if pick != -1:
                libraries[pick]["name"] = prompt_text("Name", libraries[pick]["name"])
                libraries[pick]["path"] = prompt_text("Folder", libraries[pick]["path"])
        elif index == 2 and libraries:
            pick = choose("Remove which library?", [l["name"] for l in libraries])
            if pick != -1:
                del libraries[pick]


def show_settings(config):
    endpoint = config["endpoint"]
    print()
    print("LLM endpoint:")
    print("  kind:     " + endpoint.get("kind", ""))
    print("  model:    " + (endpoint.get("model", "") or "(empty)"))
    print("  base URL: " + (endpoint.get("base_url", "") or "(service default)"))
    print("  API key:  " + ("set" if endpoint.get("api_key") else "(empty)"))
    print("Helper module: " + (config.get("helper", "") or "(none)"))
    print("Libraries:")
    if not config["libraries"]:
        print("  (none)")
    for library in config["libraries"]:
        print("  " + library["name"] + " -> " + library["path"])
    pause()


def main():
    config = common.load_config()
    while True:
        index = choose("Image database settings", [
            "Configure LLM endpoint",
            "Configure helper module",
            "Manage image libraries",
            "Show current settings",
            "Save and exit"])
        if index == 0:
            configure_endpoint(config)
        elif index == 1:
            configure_helper(config)
        elif index == 2:
            manage_libraries(config)
        elif index == 3:
            show_settings(config)
        elif index == 4:
            common.save_config(config)
            print("Saved to " + common.CONFIG_PATH)
            return
        elif index == -1:
            if input("Exit without saving? [y/N]: ").strip().lower() == "y":
                return


if __name__ == "__main__":
    main()
