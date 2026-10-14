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

    def test_a_wide_panorama_in_full_size(self):
        import cache
        from PIL import Image
        # A panorama 12000 x 3000 with faces from 50 to 300 pixels across (6 to 50 in
        # its 2048 x 512 preview), two of them across the boundaries of tiles.
        size = (12000, 3000)
        canvas = Image.new("RGB", size, (90, 140, 90))
        places = [(300, 1200, 50), (1500, 900, 70), (1960, 2000, 100), (3500, 500, 140),
                  (3700, 2200, 200), (6000, 1300, 300), (8100, 1800, 90), (10500, 600, 60)]
        for x, y, side in places:
            canvas.paste(Image.open(io.BytesIO(people.probe_picture(side))), (x, y))
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=92)
        with open(os.path.join(self.folder, "wide.jpg"), "wb") as handle:
            handle.write(out.getvalue())
        preview = io.BytesIO()
        canvas.resize((2048, 512), Image.LANCZOS).save(preview, "JPEG", quality=85)
        cursor = self.conn.execute(
            "INSERT INTO images (library_id, path, name, size, mtime, ctime, width, height, "
            "added, updated, content_hash, has_preview) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, "
            "?, 1)", ("lib_drawn01", "wide.jpg", "wide.jpg", NOW, NOW, size[0], size[1], NOW,
                      NOW, "hash-wide"))
        self.conn.commit()
        cache.PreviewStore().write("lib_drawn01", cursor.lastrowid, preview.getvalue())

        def found():
            store = people.connect(create=False)
            try:
                return [tuple(row) for row in store.execute(
                    "SELECT f.x, f.y, f.w, f.h, f.quality, c.face_id IS NOT NULL FROM faces f "
                    "LEFT JOIN face_crops c ON c.face_id = f.id ORDER BY f.x")]
            finally:
                store.close()
        options = dict(self.config["people"], full_size=False)
        before = people.update(self.conn, dict(self.config, people=options), jobs.JobContext())
        self.assertEqual((before["looked"], before["full"]), (1, 0))
        in_preview = found()
        summary = people.update(self.conn, self.config, jobs.JobContext())
        self.assertEqual((summary["looked"], summary["full"], summary["failed"]), (1, 1, 0))
        in_full = found()
        self.assertGreater(len(in_full), len(in_preview))
        for x, y, side in places:
            middle = ((x + side / 2.0) / size[0], (y + side / 2.0) / size[1])
            near = [face for face in in_full if abs(face[0] + face[2] / 2 - middle[0]) * size[0]
                    < side * 0.3 and abs(face[1] + face[3] / 2 - middle[1]) * size[1]
                    < side * 0.4]
            self.assertEqual(len(near), 1, (x, y, side, in_full))
            self.assertTrue(near[0][5])         # (a picture of it is kept)

    def test_the_picture_of_a_large_face_takes_little_memory(self):
        # A portrait of 61 million pixels (looked at in full size) with a face 3000 x
        # 4000 pixels: its picture is cut and scaled without a copy at full size.
        import subprocess
        code = "\n".join((
            "import json, sys, tracemalloc",
            "sys.path.insert(0, %r)" % os.path.dirname(os.path.abspath(people.__file__)),
            "import people_worker",
            "backend = people_worker.OpenCVBackend()",
            "np, cv2 = backend.np, backend.cv2",
            "image = np.full((9504, 6336, 3), 128, dtype=np.uint8)",
            "image[3000:4000, 2500:3500] = (40, 200, 90)",
            "tracemalloc.start()",
            "data = backend.crop(image, (1700.0, 1500.0, 3000.0, 4000.0), 160, 0.45)",
            "peak = tracemalloc.get_traced_memory()[1]",
            "tracemalloc.stop()",
            "picture = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)",
            "edge = backend.crop(image, (-50.0, -50.0, 200.0, 200.0), 160, 0.45)",
            "corner = cv2.imdecode(np.frombuffer(edge, np.uint8), cv2.IMREAD_COLOR)",
            "print(json.dumps({'peak': peak, 'size': list(picture.shape),",
            "                  'middle': picture[80, 80].tolist(), 'black': corner[5, 5].tolist(),",
            "                  'grey': corner[150, 150].tolist()}))"))
        python = people.python_path(people.settings(self.config))
        answer = json.loads(subprocess.run([python, "-I", "-B", "-c", code], check=True,
                                           capture_output=True, timeout=120).stdout)
        self.assertLess(answer["peak"], 20 * 1000 * 1000)
        self.assertEqual(answer["size"], [160, 160, 3])
        # (Its middle shows the face's middle; beyond the picture's edge, black.)
        self.assertTrue(all(abs(a - b) < 12 for a, b in zip(answer["middle"], (40, 200, 90))),
                        answer)
        self.assertTrue(all(value < 12 for value in answer["black"]), answer)
        self.assertTrue(all(abs(value - 128) < 12 for value in answer["grey"]), answer)


if __name__ == "__main__":
    unittest.main()
