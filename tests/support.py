"""Shared helpers for the tests.

Every test that touches files runs inside a temporary SETTINGS_BASE, so it
never sees (or changes) a real config.json or data folder:

    class MyTest(support.TempBaseMixin, unittest.TestCase):
        def test_something(self):
            self.base.write_config([support.library("Family", folder)])

Importing this module also puts the repository folder on sys.path, so tests
can import the project modules when run with
python -m unittest discover -s tests.
"""

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.dont_write_bytecode = True


def library(name, path, **settings):
    """A library entry for config.json."""
    entry = {"name": name, "path": path}
    entry.update(settings)
    return entry


def remove_tree(path):
    """Remove a folder, retrying briefly while Windows still has files open."""
    for attempt in range(20):
        if not os.path.exists(path):
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


def child_env(**extra):
    """Environment for a child Python process (no bytecode files)."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def run_python(code, *args, **kwargs):
    """Run a snippet in a fresh interpreter with the repo importable."""
    return subprocess.run([sys.executable, "-c", code] + list(args), cwd=REPO,
                          env=child_env(), capture_output=True, text=True,
                          timeout=kwargs.get("timeout", 60))


class TempBase:
    """Context manager: a temporary SETTINGS_BASE folder.

    Sets os.environ["SETTINGS_BASE"] on entry and restores the previous value
    (or its absence) on exit, then deletes the folder.
    """

    def __init__(self, prefix="search-images-test-"):
        self.prefix = prefix
        self.path = None
        self._saved = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix=self.prefix)
        self._saved = os.environ.get("SETTINGS_BASE")
        os.environ["SETTINGS_BASE"] = self.path
        return self

    def __exit__(self, *exc_info):
        if self._saved is None:
            os.environ.pop("SETTINGS_BASE", None)
        else:
            os.environ["SETTINGS_BASE"] = self._saved
        remove_tree(self.path)
        return False

    def join(self, *parts):
        return os.path.join(self.path, *parts)

    def make_dir(self, *parts):
        path = self.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    def write_file(self, rel_path, data):
        """Write bytes (or UTF-8 text) to a file under the base; returns its path."""
        path = self.join(*rel_path.replace("\\", "/").split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def library_folder(self, name):
        """An (empty) folder to use as a library root, outside data/."""
        return self.make_dir("libraries", name)

    def write_config(self, libraries=(), bom=False, **extra):
        """Write config.json with the given libraries and top-level keys; returns the dict."""
        config = {"helper": "", "libraries": list(libraries)}
        config.update(extra)
        text = json.dumps(config, indent=4, ensure_ascii=False)
        data = text.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        self.write_file("config.json", data)
        return config


class TempBaseMixin:
    """unittest mixin: self.base is a fresh TempBase for every test."""

    def setUp(self):
        super().setUp()
        self.base = TempBase()
        self.base.__enter__()
        self.addCleanup(self.base.__exit__, None, None, None)


# ---------------------------------------------------------------------------
# Small helpers several test modules use

def quiet(*_args, **_kwargs):
    """A log function that drops everything."""


def wait_for(condition, timeout=10.0, interval=0.01):
    """Poll condition() until it is true; returns its last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


def strict_json(text):
    """json.loads that rejects NaN/Infinity, like a browser's JSON.parse."""
    def reject(token):
        raise ValueError("non-finite number in JSON: " + token)
    return json.loads(text, parse_constant=reject)


def page_config(html):
    """The page settings a page carries in its JSON block (read strictly)."""
    match = re.search(r'<script type="application/json" id="app-config">(.*?)</script>',
                      html, re.S)
    return strict_json(match.group(1))


def code_only(text):
    """JavaScript text without comments and string literals (rough, but enough here)."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    text = re.sub(r"\s//[^\n]*", " ", text)
    return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)


# ---------------------------------------------------------------------------
# Image fixtures (need Pillow; tests that use them skip without it)

def pillow_available():
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def gradient_jpeg(width, height, descending=False, quality=90):
    """JPEG bytes of a horizontal gray ramp (dark to light, or the reverse).

    The two directions have opposite dHashes, which makes them handy for
    "right picture / wrong picture" checks.
    """
    from PIL import Image
    image = Image.new("L", (width, height))
    row = bytes(int(255 * (width - 1 - x if descending else x) / max(1, width - 1))
                for x in range(width))
    image.frombytes(row * height)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def dhash_of(data):
    """The scanner's dHash (9x8 grayscale, horizontal neighbours) of image bytes."""
    from PIL import Image
    resample = getattr(Image, "Resampling", Image).LANCZOS
    with Image.open(io.BytesIO(data)) as image:
        pixels = image.convert("L").resize((9, 8), resample).tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)
    return "%016x" % bits


def thumbnail_jpeg(data, size=320):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((size, size))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# A database in the format written by older versions (data/images.db plus
# data/thumbs/thumb-NNNN.dat), for the import tests.

LEGACY_SCHEMA = """
    CREATE TABLE images (
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
        prompt_ver INTEGER NOT NULL DEFAULT 0,
        dhash TEXT NOT NULL DEFAULT '',
        UNIQUE (library, path)
    )
"""


class LegacyBuilder:
    """Builds data/images.db and thumb shard files under a data folder."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.thumb_dir = os.path.join(data_dir, "thumbs")
        os.makedirs(self.thumb_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "images.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(LEGACY_SCHEMA)

    def append_thumb(self, data, shard=0):
        """Append bytes to a shard file; returns (shard, offset, length)."""
        path = os.path.join(self.thumb_dir, "thumb-%04d.dat" % shard)
        offset = os.path.getsize(path) if os.path.exists(path) else 0
        with open(path, "ab") as handle:
            handle.write(data)
        return shard, offset, len(data)

    def add(self, image_id, library_name, path, thumb=None, exif="", info="",
            described=0, prompt_ver=0, dhash="", width=64, height=48,
            size=1000, mtime=1557843781.0, ctime=1557843781.0):
        """Insert one old-format row; thumb bytes go into the shard file."""
        shard = offset = length = None
        if thumb is not None:
            shard, offset, length = self.append_thumb(thumb)
        self.conn.execute(
            "INSERT INTO images (id, library, path, name, size, mtime, ctime, width, "
            "height, thumb_shard, thumb_offset, thumb_length, info, search_text, exif, "
            "described, prompt_ver, dhash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (image_id, library_name, path, path.split("/")[-1], size, mtime, ctime,
             width, height, shard, offset, length, info, path.lower(), exif,
             described, prompt_ver, dhash))

    def close(self):
        self.conn.commit()
        self.conn.close()
