#!/usr/bin/env python3
"""Example helper module for the image database.

Point settings.py at this file (or your own copy) to wrap the LLM calls.
Every function is optional; remove any you do not need.
"""


def before_launch():
    """Run once before the first LLM call of a scan pass.

    Only called when there is at least one image to describe. Use it to start
    a local model server, open a connection, and so on.
    """
    pass


def after_launch():
    """Run once after the last LLM call of a scan pass."""
    pass


def call_api(prompt, image_path):
    """Adjust a request just before it is sent to the LLM.

    Return a (prompt, image_path) pair to change what gets sent, or return
    None to leave the request unchanged.
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
    the descent. Return None to let the scanner fall back to os.walk.
    """
    return None


def stat(path):
    """Return an os.stat_result-like object for a source image, or None.

    The object only needs to expose st_size, st_mtime, and st_ctime. Return
    None for paths the helper does not own so the caller falls back to
    os.stat. A None return for a path the helper does own signals that the
    file is missing.
    """
    return None
