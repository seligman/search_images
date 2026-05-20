#!/usr/bin/env python3
"""Scan image libraries, build thumbnails, and request descriptions.

For each configured library this script finds new or changed images, stores
their metadata and EXIF data in the SQLite database, builds a thumbnail in the
shard files, and removes images that no longer exist on disk. It then asks the
LLM to describe each image, expecting a JSON reply that is parsed and stored so
the images can be searched.
"""

import argparse
import concurrent.futures
import fnmatch
import json
import os
import queue
import sys
import threading
from datetime import datetime
if sys.version_info >= (3, 11): from datetime import UTC
else: import datetime as datetime_fix; UTC=datetime_fix.timezone.utc

import common
import images
import llm


DESCRIBE_PROMPT = (
    "Look at this image and reply with a single JSON object and nothing else. "
    "Use exactly these keys:\n"
    '  "description": two or three sentences describing the image.\n'
    '  "subjects": a list of the main subjects or objects shown.\n'
    '  "text": every piece of text visible in the image, transcribed exactly, '
    "or an empty string if there is no text.\n"
    '  "tags": a list of short keywords useful for searching.\n'
    '  "colors": a list of the most prominent colors.\n'
    "Reply with only the JSON object. Do not wrap it in Markdown code fences.")

MAX_DESCRIBE_ATTEMPTS = 4

# Scanning stops gracefully when a file with one of these names exists in the
# working directory. Progress is committed, so a later run resumes.
ABORT_FILES = ("abort", "abort.txt")


def abort_requested():
    return any(os.path.exists(name) for name in ABORT_FILES)


def normalize_ignore(patterns):
    """Return a clean list of ignore patterns with forward-slash separators.

    Either separator (/ or backslash) is accepted in the config so the same
    library entry works on every platform.
    """
    result = []
    for pattern in patterns or []:
        if isinstance(pattern, str) and pattern.strip():
            result.append(pattern.strip().replace("\\", "/"))
    return result


def matches_ignore(rel_path, patterns):
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def as_string_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def parse_description(reply):
    """Turn an LLM reply into a validated description dict."""
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        raise ValueError("response was not a JSON object")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("response is missing a text description")
    text = data.get("text")
    return {
        "description": description.strip(),
        "subjects": as_string_list(data.get("subjects")),
        "text": text.strip() if isinstance(text, str) else "",
        "tags": as_string_list(data.get("tags")),
        "colors": as_string_list(data.get("colors")),
    }


def build_search_text(name, path, info):
    parts = [name, path, info["description"], info["text"]]
    parts.extend(info["subjects"])
    parts.extend(info["tags"])
    parts.extend(info["colors"])
    return " ".join(parts).lower()


def request_description(endpoint, helper, image_path, helper_lock=None):
    """Ask the LLM for a description, retrying until the reply parses.

    Raises an exception if no valid JSON reply arrives within a few attempts.
    helper_lock, when given, serializes helper calls across worker threads.
    """
    last_error = None
    for _attempt in range(MAX_DESCRIBE_ATTEMPTS):
        prompt = DESCRIBE_PROMPT
        path = image_path
        if helper and hasattr(helper, "call_api"):
            if helper_lock is not None:
                with helper_lock:
                    changed = helper.call_api(prompt, path)
            else:
                changed = helper.call_api(prompt, path)
            if changed is not None:
                prompt, path = changed
        try:
            return parse_description(llm.describe_image(endpoint, prompt, path))
        except Exception as error:
            last_error = error
    raise Exception("no valid description after %d attempts: %s"
                    % (MAX_DESCRIBE_ATTEMPTS, last_error))


def process_file(conn, store, dirty, library, root, rel, full, filename):
    if common.is_heic_file(filename) and not images.ensure_heif():
        return
    try:
        stat = os.stat(full)
    except OSError:
        return
    row = conn.execute("SELECT * FROM images WHERE library = ? AND path = ?",
                        (library, rel)).fetchone()
    if row and row["size"] == stat.st_size and abs(row["mtime"] - stat.st_mtime) < 1.0:
        return
    try:
        width, height, thumb, exif = images.read_image(full)
    except Exception as error:
        print("  Could not read " + rel + ": " + str(error))
        return
    shard, offset, length = store.append(thumb)
    search_text = (filename + " " + rel).lower()
    exif_json = json.dumps(exif)
    if row:
        if row["thumb_shard"] is not None:
            dirty.add(row["thumb_shard"])
        conn.execute(
            "UPDATE images SET size=?, mtime=?, ctime=?, width=?, height=?, "
            "thumb_shard=?, thumb_offset=?, thumb_length=?, exif=?, info='', "
            "search_text=?, described=0 WHERE id=?",
            (stat.st_size, stat.st_mtime, stat.st_ctime, width, height,
             shard, offset, length, exif_json, search_text, row["id"]))
        print("  Updated " + rel)
    else:
        conn.execute(
            "INSERT INTO images (library, path, name, size, mtime, ctime, "
            "width, height, thumb_shard, thumb_offset, thumb_length, exif, "
            "search_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (library, rel, filename, stat.st_size, stat.st_mtime, stat.st_ctime,
             width, height, shard, offset, length, exif_json, search_text))
        print("  Added " + rel)


def scan_library(conn, store, dirty, name, root, ignore=None):
    """Scan one library. Returns False if an abort file interrupted it."""
    patterns = normalize_ignore(ignore)
    seen = set()
    aborted = False
    for current, dirs, files in os.walk(root):
        if aborted:
            break
        rel_dir = os.path.relpath(current, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        if patterns:
            # Prune subdirectories whose contents are fully ignored. A made-up
            # placeholder name probes whether anything inside the dir could
            # match an ignore pattern.
            kept = []
            for d in dirs:
                sub = prefix + d
                if (matches_ignore(sub, patterns)
                        or matches_ignore(sub + "/_", patterns)):
                    continue
                kept.append(d)
            dirs[:] = kept
        for filename in files:
            if not common.is_image_file(filename):
                continue
            if abort_requested():
                aborted = True
                break
            full = os.path.join(current, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if matches_ignore(rel, patterns):
                continue
            seen.add(rel)
            process_file(conn, store, dirty, name, root, rel, full, filename)
    conn.commit()
    if aborted:
        return False
    # Missing files are removed only after a complete walk, so a partial scan
    # never deletes images it simply has not reached yet.
    rows = conn.execute("SELECT id, path, thumb_shard FROM images WHERE library = ?",
                        (name,)).fetchall()
    for row in rows:
        if row["path"] not in seen:
            if row["thumb_shard"] is not None:
                dirty.add(row["thumb_shard"])
            conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
            print("  Removed " + row["path"])
    conn.commit()
    return True


def rebuild_dirty(conn, store, dirty):
    """Compact the shard files that lost or replaced thumbnails."""
    for index in sorted(dirty):
        rows = conn.execute(
            "SELECT id, thumb_offset, thumb_length FROM images "
            "WHERE thumb_shard = ? ORDER BY thumb_offset", (index,)).fetchall()
        entries = [(r["thumb_offset"], r["thumb_length"]) for r in rows]
        if not entries:
            open(store.shard_path(index), "wb").close()
            continue
        rebuilt = store.rebuild_shard(index, entries)
        for row, (offset, length) in zip(rows, rebuilt):
            conn.execute(
                "UPDATE images SET thumb_offset=?, thumb_length=? WHERE id=?",
                (offset, length, row["id"]))
    conn.commit()


def compute_description(endpoint, helper, helper_lock, row, full):
    """Run the LLM for one image. Returns the info dict, or None on failure.

    This does no database work, so it is safe to call from worker threads.
    """
    temp = None
    image_path = full
    if common.is_heic_file(full):
        if not images.ensure_heif():
            print("  Skipping " + row["path"] + " (pillow-heif not installed)")
            return None
        try:
            temp = images.heic_to_temp_jpeg(full)
            image_path = temp
        except Exception as error:
            print("  Could not convert " + row["path"] + ": " + str(error))
            return None
    try:
        return request_description(endpoint, helper, image_path, helper_lock)
    except Exception as error:
        print("  Failed to describe " + row["path"] + ": " + str(error))
        return None
    finally:
        if temp and os.path.exists(temp):
            os.remove(temp)


def store_description(conn, row, info, remaining):
    """Save a computed description. Called only from the main thread."""
    search_text = build_search_text(row["name"], row["path"], info)
    conn.execute(
        "UPDATE images SET info=?, search_text=?, described=1 WHERE id=?",
        (json.dumps(info), search_text, row["id"]))
    conn.commit()
    print("%s: %5d left | Described %s"
          % (datetime.now(UTC).strftime("%d %H:%M:%S"), remaining, row["path"]))


def describe_one(conn, endpoint, helper, row, full, remaining):
    info = compute_description(endpoint, helper, None, row, full)
    if info is not None:
        store_description(conn, row, info, remaining)


def endpoint_list(config):
    """Return the configured endpoints as a list.

    A single endpoint dict becomes a one item list; a list (round robin) is
    kept as is. Only endpoints that name a model are returned.
    """
    endpoint = config.get("endpoint", {})
    items = endpoint if isinstance(endpoint, list) else [endpoint]
    return [item for item in items
            if isinstance(item, dict) and item.get("model")]


def describe_in_series(conn, endpoint, helper, roots, rows):
    """Describe images one at a time. Returns True if an abort file stopped it."""
    aborted = False
    remaining = len(rows)
    for row in rows:
        if abort_requested():
            aborted = True
            break
        root = roots.get(row["library"])
        if not root:
            continue
        full = os.path.join(root, *row["path"].split("/"))
        remaining -= 1
        describe_one(conn, endpoint, helper, row, full, remaining)
    return aborted


def describe_in_parallel(conn, endpoints, helper, roots, rows):
    """Describe images using several endpoints at once (round robin).

    Each endpoint handles one request at a time, so images spread across the
    endpoints and the slow LLM calls overlap. New work stops as soon as an
    abort file appears; requests already in flight are allowed to finish and
    their results are saved. Returns True if an abort file stopped it.
    """
    tasks = []
    for row in rows:
        root = roots.get(row["library"])
        if root:
            full = os.path.join(root, *row["path"].split("/"))
            tasks.append((row, full))
    helper_lock = threading.Lock()
    free_endpoints = queue.Queue()
    for endpoint in endpoints:
        free_endpoints.put(endpoint)

    def work(row, full):
        endpoint = free_endpoints.get()
        try:
            return compute_description(endpoint, helper, helper_lock, row, full)
        finally:
            free_endpoints.put(endpoint)

    aborted = False
    remaining = len(tasks)
    task_iter = iter(tasks)
    in_flight = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(endpoints)) as pool:
        def submit_next():
            for row, full in task_iter:
                in_flight[pool.submit(work, row, full)] = row
                return True
            return False
        for _ in endpoints:
            if abort_requested():
                aborted = True
                break
            if not submit_next():
                break
        while in_flight:
            done, _ = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                row = in_flight.pop(future)
                info = future.result()
                remaining -= 1
                if info is not None:
                    store_description(conn, row, info, remaining)
                if not aborted and abort_requested():
                    aborted = True
                if not aborted:
                    submit_next()
    return aborted


def describe_pending(conn, config):
    """Describe images that need it. Returns True if an abort file stopped it."""
    rows = conn.execute("SELECT * FROM images WHERE described = 0").fetchall()
    if not rows:
        print("All images already have descriptions.")
        return False
    endpoints = endpoint_list(config)
    if not endpoints:
        print("No LLM model configured; skipping descriptions. Run settings.py.")
        return False
    helper = common.load_helper(config.get("helper", ""))
    roots = {}
    for library in config.get("libraries", []):
        roots[library.get("name", "")] = library.get("path", "")
    print("Describing %d image(s)..." % len(rows))
    if helper and hasattr(helper, "before_launch"):
        helper.before_launch()
    if len(endpoints) > 1:
        aborted = describe_in_parallel(conn, endpoints, helper, roots, rows)
    else:
        aborted = describe_in_series(conn, endpoints[0], helper, roots, rows)
    if helper and hasattr(helper, "after_launch"):
        helper.after_launch()
    if aborted:
        print("Abort requested. Descriptions saved; "
              "run scan.py again to continue.")
    return aborted


def run_scan(scan_images, describe):
    config = common.load_config()
    libraries = config.get("libraries", [])
    if not libraries:
        print("No libraries configured. Run settings.py first.")
        return
    if abort_requested():
        print("Abort file present; remove 'abort' or 'abort.txt' first.")
        return
    conn = common.open_db()
    if scan_images:
        store = common.ThumbStore(common.THUMB_DIR)
        dirty = set()
        configured = set()
        aborted = False
        for library in libraries:
            name = library.get("name", "")
            root = library.get("path", "")
            ignore = library.get("ignore", [])
            configured.add(name)
            if not name or not os.path.isdir(root):
                print("Skipping library (missing folder): " + str(name))
                continue
            print("Scanning library: " + name)
            if not scan_library(conn, store, dirty, name, root, ignore):
                aborted = True
                break
        if aborted:
            rebuild_dirty(conn, store, dirty)
            conn.close()
            print("Abort requested. Progress saved; run scan.py again to continue.")
            return
        # Drop images that belong to libraries no longer in the config.
        for row in conn.execute("SELECT id, library, thumb_shard FROM images").fetchall():
            if row["library"] not in configured:
                if row["thumb_shard"] is not None:
                    dirty.add(row["thumb_shard"])
                conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
        conn.commit()
        rebuild_dirty(conn, store, dirty)
    if describe and describe_pending(conn, config):
        conn.close()
        return
    conn.close()
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Scan the image libraries.")
    parser.add_argument("--no-scan", action="store_true",
                        help="Skip the index image step, and only run LLM descriptions.")
    parser.add_argument("--no-describe", action="store_true",
                        help="Index images without requesting LLM descriptions.")
    args = parser.parse_args()
    run_scan(scan_images=not args.no_scan, describe=not args.no_describe)


if __name__ == "__main__":
    main()
