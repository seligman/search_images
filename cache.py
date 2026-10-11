#!/usr/bin/env python3
"""Thumbnail and preview storage.

ThumbStore keeps the small (320 px) thumbnails as BLOBs in data/thumbs.db,
so a reader never sees a half-written thumbnail and nothing is ever
rewritten in place. PreviewStore keeps the larger (2048 px) previews as plain
JPEG files under data/previews/<library id>/<bucket>/<image id>.jpg.

This module uses only the standard library.
"""

import os
import shutil
import threading

import common
import db


class ThumbStore:
    """Thumbnail JPEGs keyed by image id, in their own SQLite file.

    The store opens its own connection (WAL, or the rollback journal on a
    network drive) and may be shared between threads.
    """

    def __init__(self, path=None):
        self.path = path or common.thumbs_db_path()
        self._lock = threading.RLock()
        self._conn = db.connect(self.path, check_same_thread=False)
        with self._lock:
            db.create_thumbs_schema(self._conn)
            self._conn.commit()

    def get(self, image_id):
        """The JPEG bytes for an image, or None."""
        with self._lock:
            row = self._conn.execute("SELECT jpeg FROM thumbs WHERE image_id = ?",
                                     (int(image_id),)).fetchone()
        return bytes(row[0]) if row else None

    def put(self, image_id, jpeg, commit=True):
        """Store (or replace) the thumbnail for an image.

        Pass commit=False to batch many writes, then call commit().
        """
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO thumbs (image_id, jpeg) VALUES (?, ?)",
                               (int(image_id), _blob(jpeg)))
            if commit:
                self._conn.commit()

    def commit(self):
        with self._lock:
            self._conn.commit()

    def delete(self, ids, commit=True):
        """Remove the thumbnails for one id or an iterable of ids. Returns the count removed."""
        if isinstance(ids, int):
            ids = [ids]
        ids = [int(image_id) for image_id in ids]
        removed = 0
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                cursor = self._conn.execute(
                    "DELETE FROM thumbs WHERE image_id IN (%s)" % ",".join("?" * len(chunk)),
                    chunk)
                removed += max(0, cursor.rowcount)
            if commit:
                self._conn.commit()
        return removed

    def ids(self):
        """The set of image ids that have a thumbnail."""
        with self._lock:
            return {row[0] for row in self._conn.execute("SELECT image_id FROM thumbs")}

    def __contains__(self, image_id):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM thumbs WHERE image_id = ?",
                                      (int(image_id),)).fetchone() is not None

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                finally:
                    self._conn.close()
                    self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _blob(data):
    """bytes-like data as something sqlite3 stores as a BLOB."""
    return data if isinstance(data, (bytes, memoryview)) else bytes(data)


class PreviewStore:
    """Preview files under data/previews/<library id>/<bucket>/<image id>.jpg.

    bucket is "%03d" % (image_id // 1000), which keeps folders to about a
    thousand files each. Previews are JPEGs, except for screenshots and
    drawings whose JPEG would be larger than a PNG (see images.read_image);
    the name stays .jpg, so readers sniff the type (imageinfo.sniff_mime).
    """

    def __init__(self, root=None):
        self.root = root or common.previews_dir()

    def library_dir(self, library_id):
        if not isinstance(library_id, str) or not common.LIBRARY_ID_RE.match(library_id):
            raise ValueError("invalid library id: %r" % (library_id,))
        return os.path.join(self.root, library_id)

    def path_for(self, library_id, image_id):
        image_id = int(image_id)
        if image_id < 0:
            raise ValueError("invalid image id: %r" % (image_id,))
        return os.path.join(self.library_dir(library_id), "%03d" % (image_id // 1000),
                            "%d.jpg" % image_id)

    def write(self, library_id, image_id, data):
        """Store a preview (atomically: readers see the old file or the new one). Returns its path."""
        path = self.path_for(library_id, image_id)
        common.ensure_dir(os.path.dirname(path))
        common.write_file_atomic(path, data)
        return path

    def read(self, library_id, image_id):
        """The preview bytes, or None if there is no preview."""
        try:
            with open(self.path_for(library_id, image_id), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def exists(self, library_id, image_id):
        return os.path.isfile(self.path_for(library_id, image_id))

    def delete(self, library_id, image_id):
        """Remove one preview. Returns True if a file was removed."""
        try:
            os.remove(self.path_for(library_id, image_id))
            return True
        except FileNotFoundError:
            return False

    def delete_library(self, library_id):
        """Remove every preview of a library. Returns True if anything was removed."""
        path = self.library_dir(library_id)
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
        return True
