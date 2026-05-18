#!/usr/bin/env python3
"""Shared helpers for the image database tools.

This module uses only the standard library so it can be imported by every
script without pulling in third party dependencies.
"""

import importlib.util
import json
import os
import sqlite3


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".heic")
HEIC_EXTENSIONS = (".heic",)

SHARD_LIMIT = 10 * 1024 * 1024
THUMB_SIZE = 320

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The SETTINGS_BASE environment variable optionally moves config.json and the
# data folder out of the script directory.
SETTINGS_BASE = os.path.abspath(os.environ.get("SETTINGS_BASE") or BASE_DIR)
CONFIG_PATH = os.path.join(SETTINGS_BASE, "config.json")
DATA_DIR = os.path.join(SETTINGS_BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "images.db")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")

DEFAULT_CONFIG = {
    "endpoint": {"kind": "openai", "base_url": "", "api_key": "", "model": ""},
    "helper": "",
    "libraries": [],
}


def default_config():
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_config():
    config = default_config()
    if not os.path.exists(CONFIG_PATH):
        return config
    with open(CONFIG_PATH, "r") as handle:
        stored = json.load(handle)
    config.update(stored)
    # Endpoint keys are merged separately so older config files keep working.
    endpoint = dict(DEFAULT_CONFIG["endpoint"])
    endpoint.update(stored.get("endpoint", {}))
    config["endpoint"] = endpoint
    return config


def save_config(config):
    with open(CONFIG_PATH, "w") as handle:
        json.dump(config, handle, indent=4)
        handle.write("\n")


def ensure_data_dirs():
    for path in (DATA_DIR, THUMB_DIR):
        if not os.path.isdir(path):
            os.makedirs(path)


def is_image_file(name):
    return name.lower().endswith(IMAGE_EXTENSIONS)


def is_heic_file(name):
    return name.lower().endswith(HEIC_EXTENSIONS)


def open_db():
    ensure_data_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY,
            library TEXT NOT NULL,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            ctime REAL NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            thumb_shard INTEGER,
            thumb_offset INTEGER,
            thumb_length INTEGER,
            info TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            exif TEXT NOT NULL DEFAULT '',
            described INTEGER NOT NULL DEFAULT 0,
            UNIQUE (library, path)
        )
    """)
    conn.commit()
    return conn


def load_helper(path):
    """Load an optional helper module from a file path, or return None."""
    if not path:
        return None
    if not os.path.isfile(path):
        raise SystemExit("Helper module not found: " + path)
    spec = importlib.util.spec_from_file_location("image_db_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ThumbStore:
    """Append-only shard files holding thumbnail bytes.

    Each shard file is kept at or below SHARD_LIMIT. A thumbnail is located by
    its (shard index, offset, length). The same sharding idea is used by the
    static site packager, so small updates only touch one or two files.
    """

    def __init__(self, directory):
        self.directory = directory
        if not os.path.isdir(directory):
            os.makedirs(directory)

    def shard_path(self, index):
        return os.path.join(self.directory, "thumb-%04d.dat" % index)

    def last_shard(self):
        index = 0
        while os.path.exists(self.shard_path(index + 1)):
            index += 1
        return index

    def append(self, data):
        index = self.last_shard()
        path = self.shard_path(index)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size and size + len(data) > SHARD_LIMIT:
            index += 1
            path = self.shard_path(index)
            size = 0
        with open(path, "ab") as handle:
            handle.write(data)
        return index, size, len(data)

    def read(self, index, offset, length):
        with open(self.shard_path(index), "rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    def rebuild_shard(self, index, entries):
        """Rewrite one shard keeping only the given entries.

        entries is a list of (offset, length) pairs. Returns the matching list
        of new (offset, length) pairs after the gaps have been removed.
        """
        chunks = [self.read(index, offset, length) for offset, length in entries]
        result = []
        with open(self.shard_path(index), "wb") as handle:
            for chunk in chunks:
                result.append((handle.tell(), len(chunk)))
                handle.write(chunk)
        return result
