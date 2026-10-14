#!/usr/bin/env python3
"""A stand-in for the face helper (people_worker.py) that needs no numpy or OpenCV.

It speaks the real protocol (people_worker.serve) with a backend that
"detects" faces drawn as solid squares: a face is a square of colour
(255, 0, 32 * k), k = 1..7 being who it is, and its vector says k (two
squares of one colour are the same person, in any picture; a picture
looked at in tiles finds a square in each tile it is in, as a whole or a
part). The test face people.py draws (people.probe_picture) is found as
one face in its middle. Draw the pictures as PNG, or as JPEG of good
quality: the colours are matched with some slack.

The top-left pixel of a picture makes the helper misbehave:
(0, 255, 0) ends the program, (0, 0, 255) makes the picture fail,
(255, 255, 0) makes it wait 60 seconds. FAKE_PEOPLE_MODE in the
environment: "problem" (hello says OpenCV is missing), "version" (it
says another version), "noprobe" (no face in the test face).
"""

import io
import json
import os
import struct
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
# The tests run it isolated (-I), as people.py does: the folders of the test
# run's own Python (where Pillow is) come in FAKE_PEOPLE_SYS_PATH.
for folder in json.loads(os.environ.get("FAKE_PEOPLE_SYS_PATH") or "[]"):
    if folder not in sys.path:
        sys.path.append(folder)
sys.dont_write_bytecode = True

import people_worker  # noqa: E402

MODE = os.environ.get("FAKE_PEOPLE_MODE", "")
PROBE_BACKGROUND = (90, 140, 90)


def face_colour(k):
    """The colour of a face of person k (1..7)."""
    return (255, 0, 32 * k)


def vector(k):
    """The vector of person k: unit length, mostly along axis k."""
    values = [0.0] * people_worker.EMBEDDING_DIMS
    values[k] = 1.0
    values[(k + 1) % len(values)] = 0.1
    length = sum(value * value for value in values) ** 0.5
    return struct.pack("<%df" % len(values), *[value / length for value in values])


class FakeBackend:

    def __init__(self):
        if MODE == "problem":
            error = ImportError("No module named 'cv2'")
            error.name = "cv2"
            raise error
        from PIL import Image
        self.Image = Image
        self.loaded = None

    def versions(self):
        # (The names of the environment variables it got: see test_people.)
        return {"opencv": "fake", "numpy": "fake", "environment": sorted(os.environ)}

    def load(self, detector, recognizer, threads=0):
        for path in (detector, recognizer):
            if not os.path.isfile(path or ""):
                raise people_worker.WorkerError("There is no model file %s." % path)
        self.loaded = (detector, recognizer, threads)

    def decode(self, data):
        try:
            image = self.Image.open(io.BytesIO(data))
            image = image.convert("RGB")
        except Exception:
            raise people_worker.WorkerError("The picture cannot be decoded.")
        corner = image.getpixel((0, 0))
        if corner == (0, 255, 0):
            os._exit(3)
        if corner == (0, 0, 255):
            raise people_worker.WorkerError("this test picture fails")
        if corner == (255, 255, 0):
            time.sleep(60)
        return image, image.width, image.height

    def scaled(self, image, width, height):
        if (width, height) == image.size:
            return image
        return image.resize((width, height), self.Image.NEAREST)

    def detect(self, image, width, height):
        if image.getpixel((1, 1)) == PROBE_BACKGROUND:
            if MODE == "noprobe":
                return []
            side = min(width, height) * 0.5
            return [self._row(width / 2.0 - side / 2, height / 2.0 - side / 2, side, side)]
        return self._squares(self.scaled(image, width, height))

    def detect_part(self, image, x, y, width, height):
        return self._squares(image.crop((x, y, x + width, y + height)))

    def crop(self, image, box, edge, margin):
        x, y, w, h = box
        side = max(w, h) * (1 + 2 * margin)
        middle_x, middle_y = x + w / 2.0, y + h / 2.0
        face = image.crop((int(round(middle_x - side / 2)), int(round(middle_y - side / 2)),
                           int(round(middle_x + side / 2)), int(round(middle_y + side / 2))))
        out = io.BytesIO()
        face.resize((edge, edge)).save(out, "JPEG", quality=85)
        return out.getvalue()

    def _squares(self, scaled):
        red, green, blue = scaled.split()
        red = red.point(lambda value: 255 if value > 200 else 0)
        green = green.point(lambda value: 255 if value < 60 else 0)
        from PIL import ImageChops, ImageFilter
        both = ImageChops.multiply(red, green)
        rows = []
        for k in range(1, 8):
            target = 32 * k
            band = blue.point(lambda value, target=target: 255 if abs(value - target) <= 12
                              else 0)
            # (Pixels at the edges of other colours, where a JPEG blurs them, go.)
            mask = ImageChops.multiply(both, band).filter(ImageFilter.MinFilter(5))
            box = mask.getbbox()
            if box is not None:
                left, top, right, bottom = box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2
                rows.append(self._row(left, top, right - left, bottom - top))
        return rows

    @staticmethod
    def _row(x, y, w, h):
        marks = [(0.3, 0.4), (0.7, 0.4), (0.5, 0.6), (0.35, 0.8), (0.65, 0.8)]
        row = [float(x), float(y), float(w), float(h)]
        for fx, fy in marks:
            row += [x + fx * w, y + fy * h]
        return row + [0.95]

    def embed(self, image, face):
        x, y, w, h = face["box"]
        red, green, blue = image.getpixel((int(x + w / 2), int(y + h / 2)))
        k = max(1, min(7, int(round(blue / 32.0)))) if red > 200 and green < 60 else 0
        return vector(k), 10.0, 150.0


if __name__ == "__main__":
    if MODE == "version":
        people_worker.WORKER_VERSION = 99
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    people_worker.serve(sys.stdin.buffer, protocol, FakeBackend)
