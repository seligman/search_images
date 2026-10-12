#!/usr/bin/env python3
"""Pictures the owner said are not duplicates, kept apart in every stack.

The search page stacks near-duplicate pictures (see
search_index.duplicate_groups). When two of them only look alike -- two
different moments of a burst, two pages of one form -- the Duplicates page
can say so ("Not duplicates"). That decision is kept per pair of pictures,
by their content: the pair of their content hashes, or "#<image id>" for a
row whose hash is not known yet. Exact copies share a content hash, so they
always stay together, and a decision outlives a move, a rename or a rescan.
When a file changes in place (rotated, or tags written into it), the scan
gives its new content the pairs of the old one (carry_over).

The pairs live in data/not-duplicates.json (beside index.db, not in
data/cache: they are the owner's decisions, not something that can be
worked out again):

    {"format": 1, "pairs": [["<key>", "<key>"], ...]}     (each pair sorted)

split_groups() splits duplicate groups so that no pair ends up in one
group; search_index applies it to every group it hands out, so the search
page, the Duplicates page and exports agree.

This module uses only the standard library.
"""

import hashlib
import itertools
import json
import os
import sys
import threading
import time

import common

FILE_NAME = "not-duplicates.json"
FORMAT = 1
# A pair key: a content hash (hex) or "#" and an image id.
MAX_KEY_LENGTH = 80

_lock = threading.Lock()
_memo = {"stamp": None, "pairs": frozenset(), "version": ""}


def path():
    """The file that keeps the pairs: data/not-duplicates.json."""
    return os.path.join(common.data_dir(), FILE_NAME)


def unit_key(image_id, content_hash):
    """The key a picture goes by in a pair: its content hash, or "#<id>" without one."""
    if isinstance(content_hash, str) and content_hash:
        return content_hash
    return "#%d" % int(image_id)


def _pair(first, second):
    return (first, second) if first <= second else (second, first)


def _valid_key(key):
    return isinstance(key, str) and 0 < len(key) <= MAX_KEY_LENGTH


def _stamp(file_path):
    try:
        stat = os.stat(file_path)
    except OSError:
        return (file_path, None, None)
    return (file_path, stat.st_mtime_ns, stat.st_size)


def _read(file_path):
    """The pairs in the file (a set; empty when there is none). Raises ValueError
    when the file is there but is not a pairs file."""
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return set()
    except OSError as error:
        raise ValueError(str(error))
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        raise ValueError("not a list of pairs")
    pairs = set()
    for item in data["pairs"]:
        if (isinstance(item, list) and len(item) == 2 and all(_valid_key(key) for key in item)
                and item[0] != item[1]):
            pairs.add(_pair(item[0], item[1]))
    return pairs


def _load():
    """(pairs frozenset, version text) of the file, read again only when it changes."""
    file_path = path()
    stamp = _stamp(file_path)
    with _lock:
        if _memo["stamp"] == stamp:
            return _memo["pairs"], _memo["version"]
    try:
        pairs = frozenset(_read(file_path))
    except ValueError as error:
        print("Warning: %s cannot be read (%s); no pictures are kept apart as not duplicates."
              % (file_path, error), file=sys.stderr)
        pairs = frozenset()
    version = _version(pairs)
    with _lock:
        _memo.update(stamp=stamp, pairs=pairs, version=version)
    return pairs, version


def _version(pairs):
    if not pairs:
        return ""
    digest = hashlib.sha1()
    for first, second in sorted(pairs):
        digest.update(("%s %s\n" % (first, second)).encode("utf-8"))
    return digest.hexdigest()[:16]


def pairs():
    """The pairs of keys that are not duplicates (a frozenset of sorted tuples)."""
    return _load()[0]


def version():
    """A short text that changes whenever the pairs do ("" when there are none)."""
    return _load()[1]


def pairs_among(keys, apart=None):
    """The pairs that keep pictures apart: every two different keys of keys,
    or, with apart (one of the keys), apart with each of the others."""
    distinct = sorted({key for key in keys if _valid_key(key)})
    if apart is not None:
        return {_pair(apart, key) for key in distinct if key != apart}
    return {_pair(first, second) for first, second in itertools.combinations(distinct, 2)}


def _rewrite(work):
    """Re-read the file, let work(current pairs) say what to add and remove
    (two sets), and replace the file in one go (under a lock, so two
    requests do not lose each other's pairs). A file that cannot be read
    is kept as not-duplicates.json.broken-<time> before it is replaced.
    Returns how many pairs changed. Raises OSError when it cannot be written.
    """
    file_path = path()
    with _lock:
        try:
            current = _read(file_path)
        except ValueError:
            current = set()
            if os.path.exists(file_path):
                os.replace(file_path, "%s.broken-%d" % (file_path, int(time.time())))
        add, remove = work(current)
        changed = (add - current) | (remove & current)
        if changed:
            result = (current - remove) | add
            common.ensure_dir(os.path.dirname(file_path))
            text = json.dumps({"format": FORMAT, "pairs": [list(pair) for pair in sorted(result)]},
                              separators=(",", ":"))
            common.write_file_atomic(file_path, text + "\n")
        _memo["stamp"] = None
    return len(changed)


def _clean(pairs):
    """The well-formed pairs of pairs, each sorted."""
    return {_pair(*pair) for pair in pairs
            if len(pair) == 2 and all(_valid_key(key) for key in pair) and pair[0] != pair[1]}


def change(wanted, add=True):
    """Add (or with add=False remove) pairs; returns how many were not so before.

    Raises OSError when the file cannot be written (see _rewrite).
    """
    wanted = _clean(wanted)
    if add:
        return _rewrite(lambda current: (wanted, set()))
    return _rewrite(lambda current: (set(), wanted))


def carry_over(renamed):
    """Give pictures whose key changed the pairs of their old key too.

    renamed maps old keys to new ones: a file whose content changed in
    place (rotated, re-saved, tags written into it) is still the picture
    the owner kept apart from others. The old pairs stay (the old content
    may live on in another file). Returns how many pairs were added;
    nothing is written when no pair has an old key. Raises OSError when
    the file cannot be written.
    """
    renamed = {old: new for old, new in renamed.items()
               if _valid_key(old) and _valid_key(new) and old != new}
    if not renamed or not any(first in renamed or second in renamed
                              for first, second in pairs()):
        return 0

    def work(current):
        return _clean((renamed.get(first, first), renamed.get(second, second))
                      for first, second in current), set()
    return _rewrite(work)


def split_groups(groups, keys, kept_apart=None):
    """groups with no two pictures of a not-duplicates pair left in one group.

    groups are lists of image ids (search_index.duplicate_groups); keys maps
    an image id to its pair key (unit_key); kept_apart is the set of pairs
    (default: the file's). A group none of whose pairs is kept apart stays
    as it is. Otherwise its pictures, by key (exact copies move together),
    oldest first, each join the first part that holds nothing kept apart
    from them, or start a new part; parts of one picture drop out. Returns
    the groups (sorted id lists of two or more) sorted by first id.
    """
    kept_apart = pairs() if kept_apart is None else kept_apart
    if not kept_apart:
        return groups
    marked = set()
    for first, second in kept_apart:
        marked.add(first)
        marked.add(second)
    result = []
    changed = False
    for ids in groups:
        units = {}                              # key -> [ids], in order of first id
        for image_id in sorted(ids):
            units.setdefault(keys.get(image_id) or "#%d" % image_id, []).append(image_id)
        if sum(1 for key in units if key in marked) < 2:
            result.append(ids)
            continue
        parts = []                              # [(set of keys, [ids])]
        for key, members in units.items():
            for part_keys, part_ids in parts:
                if not any(_pair(key, other) in kept_apart for other in part_keys):
                    part_keys.add(key)
                    part_ids.extend(members)
                    break
            else:
                parts.append(({key}, list(members)))
        if len(parts) == 1:
            result.append(ids)
            continue
        changed = True
        result.extend(sorted(part_ids) for _keys, part_ids in parts if len(part_ids) > 1)
    if changed:
        result.sort(key=lambda ids: ids[0])
    return result
