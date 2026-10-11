#!/usr/bin/env python3
"""Example helper module for the image database.

Point settings.py at this file (or your own copy) to wrap the LLM calls.
Every function is optional; remove any you do not need.

The scanner reads several images at once. Unless THREAD_SAFE is True, it
makes sure load_image and stat are only ever called one at a time.
"""

# Set to True if load_image and stat may be called from several threads at
# the same time; reading is then faster.
THREAD_SAFE = False


def before_launch():
    """Run once before the first LLM call of a scan pass.

    Only called when there is at least one image to describe. Use it to start
    a local model server, open a connection, and so on.
    """
    pass


def after_launch():
    """Run once after the last LLM call of a scan pass."""
    pass


def call_api(prompt, image_path, source_path=None):
    """Adjust a request just before it is sent to the LLM.

    image_path is a temporary JPEG holding the image as it will be sent
    (upright and shrunk for the model); it is deleted afterwards.
    source_path is the original file in the library, for hooks that pick
    what to do by folder or file name. (Older versions passed the original
    as image_path; a hook that only takes two arguments still works, it just
    does not get source_path.) Return a (prompt, image_path) pair to change
    what gets sent, or return None to leave the request unchanged.
    """
    return None


def load_image(path):
    """Return the raw bytes for a source image, or None to read from disk.

    Implement this when the source images do not live as plain files (for
    example: pulled from a database, an archive, or a network share). The
    bytes should be the original file contents. Return None to let the
    scanner and server read the file from disk in the usual way.
    """
    return None


def walk_library(root):
    """Yield (dirpath, dirnames, filenames) tuples for one library root.

    Implement this to control how a library is enumerated. The contract
    matches os.walk: callers may modify the dirnames list in place to prune
    the descent. Return None to let the scanner list the folder itself.
    """
    return None


def stat(path):
    """Return an os.stat_result-like object for a source image, or None.

    The object only needs to expose st_size, st_mtime, and st_ctime. The
    scanner asks it for every image file it lists (with or without
    walk_library), and the server for originals it sends. Return None for
    paths the helper does not own so the caller falls back to the file
    system. A None return for a path the helper does own, while the server
    or a walk_library listing asks, signals that the file is missing.
    """
    return None
