"""People with the real face helper and models (skipped unless they are named).

These tests run people_worker.py in a people environment (numpy and
OpenCV) with the YuNet and SFace model files, on faces drawn in code
(people.probe_picture: ellipses, no photo of anyone). They only run when
both of these are set:

    SEARCH_IMAGES_PEOPLE_PYTHON   the people environment's python (or its folder)
    SEARCH_IMAGES_PEOPLE_MODELS   the folder with the two model files

for instance (Windows):

    set SEARCH_IMAGES_PEOPLE_PYTHON=C:\\path\\to\\people-env
    set SEARCH_IMAGES_PEOPLE_MODELS=C:\\path\\to\\models\\faces
    python -m unittest discover -s tests -p test_people_live.py
"""

import io
import json
import os
import struct
import unittest

import support

import db
import jobs
import people

PYTHON = os.environ.get("SEARCH_IMAGES_PEOPLE_PYTHON", "")
MODELS = os.environ.get("SEARCH_IMAGES_PEOPLE_MODELS", "")
NOW = 1700000000.0


def cosine(first, second):
    a = struct.unpack("<128f", first)
    b = struct.unpack("<128f", second)
    return sum(x * y for x, y in zip(a, b))


@unittest.skipUnless(PYTHON and MODELS and support.pillow_available(),
                     "set SEARCH_IMAGES_PEOPLE_PYTHON and SEARCH_IMAGES_PEOPLE_MODELS to run "
                     "these (see the module notes)")
class LiveTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("Drawn")
        self.config = self.base.write_config(
            [support.library("Drawn", self.folder, id="lib_drawn01")],
            people={"enabled": True, "python": PYTHON, "models_dir": MODELS, "workers": 2})
        db.init(log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def add(self, name, data):
        import cache
        cursor = self.conn.execute(
            "INSERT INTO images (library_id, path, name, size, mtime, ctime, width, height, "
            "added, updated, content_hash, has_preview) VALUES (?, ?, ?, 1, ?, ?, 1, 1, ?, ?, "
            "?, 1)", ("lib_drawn01", name, name, NOW, NOW, NOW, NOW, "hash-" + name))
        self.conn.commit()
        cache.PreviewStore().write("lib_drawn01", cursor.lastrowid, data)
        return cursor.lastrowid

    def test_the_helper_finds_drawn_faces_of_every_size(self):
        from PIL import Image
        result = people.check(self.config)
        self.assertTrue(result["ok"], result["problem"])
        self.assertTrue(result["opencv"])
        # One face per picture, from 48 to 1200 pixels across, in a 2048 x 1536 picture.
        sizes = (48, 96, 200, 400, 800, 1200)
        for side in sizes:
            canvas = Image.new("RGB", (2048, 1536), (90, 140, 90))
            face = Image.open(io.BytesIO(people.probe_picture(side)))
            canvas.paste(face, (300, 100))
            out = io.BytesIO()
            canvas.save(out, "JPEG", quality=92)
            self.add("face%d.jpg" % side, out.getvalue())
        summary = people.update(self.conn, self.config, jobs.JobContext())
        self.assertEqual((summary["looked"], summary["failed"], summary["error"]),
                         (len(sizes), 0, ""))
        store = people.connect(create=False)
        self.addCleanup(store.close)
        rows = {row[0]: row for row in store.execute(
            "SELECT content_hash, x, y, w, h, embedding, score, quality, landmarks FROM faces")}
        self.assertEqual(sorted(rows), sorted("hash-face%d.jpg" % side for side in sizes))
        for side in sizes:
            _key, x, y, w, h, _vec, score, _quality, landmarks = rows["hash-face%d.jpg" % side]
            # The box is around the drawn face (it lies in the square pasted at 300, 100).
            self.assertGreaterEqual(score, 0.7, side)
            self.assertTrue(250 / 2048.0 < x < (300 + side * 0.5) / 2048.0, (side, x))
            self.assertTrue(w * 2048 < side * 1.1, (side, w))
            self.assertEqual(len(json.loads(landmarks)), 5)
        # The same face at every size is the same person (SFace: 0.363 or more).
        vectors = [rows["hash-face%d.jpg" % side][5] for side in sizes]
        for vector in vectors[1:]:
            self.assertGreater(cosine(vectors[0], vector), 0.6)


if __name__ == "__main__":
    unittest.main()
