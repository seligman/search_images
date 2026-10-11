#!/usr/bin/env python3
"""Example helper module for the image database.

Point settings.py (or "helper" in config.json) at your own copy of this
file to wrap the LLM calls and the file access. Every function is optional;
remove any you do not need.

Which tools call which hooks:

- before_launch, after_launch and call_api: describe runs only, that is
  the describe step of scan.py and the Manage page's describe jobs
  (Describe, Scan + describe, Process all, Re-describe, and "Describe
  again" in the viewer). Manage jobs run inside the web server's process.
  The endpoint tests (Test on the Settings page, settings.py's test,
  scan.py --test-endpoint) and compare_models.py never call them, so start
  the model server yourself before using those.
- walk_library: scans (and the Build previews job), to list a library.
- load_image and stat: everything that reads originals: scans, describing,
  the search server and exports.

The scanner, the describe step, the export and the web server read several
images at once. Unless THREAD_SAFE is True, they make sure load_image and
stat are only ever called one at a time within a program (the web server's
requests and the jobs it runs take turns too); call_api is always called one
at a time. A scan run from the command line while the web server is running
is another program, and may call them at the same time as the server.
"""

# Set to True if load_image and stat may be called from several threads at
# the same time; reading is then faster.
THREAD_SAFE = False


def before_launch():
    """Run once before the first LLM call of a describe run.

    Only called when there is at least one image to describe. Use it to start
    a local model server, open a connection, and so on. It may return as soon
    as the server process is started: a local server that is still loading
    its model is waited for (up to three minutes) before the first request.
    """
    pass


def after_launch():
    """Run once after the last LLM call of a describe run, even one that failed."""
    pass


def call_api(prompt, image_path, source_path=None):
    """Adjust a request just before it is sent to the LLM.

    Called before each image is sent in a describe run (and again when an
    unusable reply makes it ask again). prompt is the text sent after the
    image ("Describe this image.", for 360-degree photos a note that the
    image is an equirectangular panorama, and the library's context notes);
    the instructions themselves go in the system prompt, which is sent
    unchanged. image_path is a temporary JPEG holding the image as it will
    be sent (upright and shrunk for the model); it is deleted afterwards.
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
