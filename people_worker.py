#!/usr/bin/env python3
"""The face helper: finds the faces in a picture and makes a vector of each (people.py).

people.py starts this program with the python of the people environment
(config.json "people": "python", see SETTINGS.md), a Python of its own
that has numpy and OpenCV (opencv-python-headless); the program itself
needs nothing else, and the search programs never import those. It
talks JSON over its standard input and output: one request per line, one
answer per line, in order, each answer carrying the request's "id".

    {"op": "hello"}
        -> {"ok": true, "worker": WORKER_VERSION, "python": ..., "opencv": ...,
            "numpy": ...}   (also when OpenCV is missing: then "problem" says so)
    {"op": "load", "detector": path, "recognizer": path, "threads": n}
        -> {"ok": true}     loads the two models (n: OpenCV's threads, 0 = its own
                            choice)
    {"op": "faces", "image": base64 JPEG or PNG, "max_edge": px, "min_edge": px,
     "min_size": px, "min_score": s, "max_faces": n}
        -> {"ok": true, "width", "height", "faces": [...], "seconds"}
    {"op": "quit"}  -> {"ok": true}, then the program ends (so does it when its
                       input closes: the program that started it is gone)

Any request that fails gets {"ok": false, "error": "..."}; the program
goes on with the next one.

How faces are found (find_faces): the picture, upright as people.py sends
it, is looked at several times, at long edges of max_edge, half of it, a
quarter, ... down to min_edge (an image pyramid), with the YuNet face
detector: YuNet finds faces of about 10 to 300 pixels, so each size only
keeps the faces whose box is between BAND_LOW and BAND_HIGH pixels on its
short side there (the largest size also the smaller ones, the smallest
also the larger ones). A face seen at several sizes is kept once, as the
size that was surest of it saw it; a box that lies mostly inside a surer
one (a part of a large face seen as a face of its own) goes. At most
max_faces faces are kept, the surest first, and only those at least
min_size pixels on their short side (in the picture as sent) with a score
of at least min_score.

Each face is then cut out of the picture as sent, turned upright and
scaled by its five landmarks (eyes, nose tip, mouth corners) to the 112 x
112 pixels the SFace recogniser wants (OpenCV's alignCrop), and SFace
makes its vector: 128 numbers, L2-normalised (copies of one person's face
are close: a cosine of about 0.36 or more), sent as base64 of
little-endian float32. Per face the answer has:

    box        [x, y, w, h] in pixels of the picture as sent
    landmarks  [[x, y] x 5]: right eye, left eye (of the person), nose tip,
               right and left mouth corner, in the same pixels
    score      the detector's confidence (0 to 1)
    embedding  the vector (base64)
    norm       the length of SFace's vector before it was normalised
    sharpness  the variance of the Laplacian of the grey 112 x 112 face (how
               sharp it is; blurred faces have low values)
    edge       the long edge of the size that found it

This file runs with any Python 3.9 or later; numpy and OpenCV are only
imported when a request needs them, so the protocol can be tested
without them (see tests/test_people_worker.py).
"""

import base64
import json
import os
import sys
import time

WORKER_VERSION = 1
# The face sizes (short side of the box, in pixels of one size of the
# pyramid) each size keeps: YuNet was trained on faces of about 10 to 300
# pixels and misses or splits larger ones.
BAND_LOW = 16
BAND_HIGH = 288
# Sizes of the pyramid: from max_edge, halved down to min_edge.
DEFAULT_MAX_EDGE = 2048
DEFAULT_MIN_EDGE = 256
SMALLEST_EDGE = 64
LARGEST_EDGE = 8192
# Detections kept per size before merging, and the detector's own limits.
DETECTOR_TOP_K = 200
DETECTOR_SCORE = 0.5
DETECTOR_NMS = 0.3
# Two boxes are one face when they overlap this much (intersection over
# union), or when this much of the smaller one lies inside the other.
SAME_IOU = 0.3
INSIDE = 0.6
DEFAULT_MIN_SIZE = 20
DEFAULT_MIN_SCORE = 0.7
DEFAULT_MAX_FACES = 64
MAX_FACES = 500
# The longest request line read (a base64 picture).
MAX_LINE = 96 * 1024 * 1024
EMBEDDING_DIMS = 128


class WorkerError(Exception):
    """A request that cannot be done (said in its answer)."""


# ---------------------------------------------------------------------------
# Boxes (plain Python: tested without numpy)

def pyramid(width, height, max_edge=DEFAULT_MAX_EDGE, min_edge=DEFAULT_MIN_EDGE):
    """The sizes a picture is looked at, largest first: [(scale, width, height)].

    The first has a long edge of max_edge (never larger than the picture);
    each next one is half as large, down to min_edge (the last one at least
    min_edge, or the picture's own size when that is smaller).
    """
    long_edge = max(width, height)
    if width <= 0 or height <= 0:
        return []
    max_edge = max(SMALLEST_EDGE, min(LARGEST_EDGE, int(max_edge)))
    min_edge = max(SMALLEST_EDGE, min(max_edge, int(min_edge)))
    edge = min(long_edge, max_edge)
    sizes = []
    while True:
        scale = edge / float(long_edge)
        sizes.append((scale, max(1, int(round(width * scale))),
                      max(1, int(round(height * scale)))))
        if edge / 2.0 < min_edge:
            break
        edge = edge / 2.0
    return sizes


def in_band(short_side, index, count, low=BAND_LOW, high=BAND_HIGH):
    """True when a face this many pixels across (short side, at that size) is one the
    size (index of count, largest first) keeps."""
    if short_side < low and index != 0:
        return False
    if short_side > high and index != count - 1:
        return False
    return True


def _area(box):
    return max(0.0, box[2]) * max(0.0, box[3])


def overlap(first, second):
    """(intersection over union, intersection over the smaller box) of two [x, y, w, h]."""
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    if inter <= 0:
        return 0.0, 0.0
    union = _area(first) + _area(second) - inter
    smaller = min(_area(first), _area(second))
    return (inter / union if union > 0 else 0.0), (inter / smaller if smaller > 0 else 0.0)


def merge(candidates, same_iou=SAME_IOU, inside=INSIDE):
    """The candidates ({"box", "score", ...}) that stay: the surest first, each one
    dropped when it overlaps one kept before it (see SAME_IOU, INSIDE)."""
    kept = []
    for candidate in sorted(candidates, key=lambda item: -item["score"]):
        if not any(_same(candidate["box"], other["box"], same_iou, inside) for other in kept):
            kept.append(candidate)
    return kept


def _same(first, second, same_iou, inside):
    iou, covered = overlap(first, second)
    return iou > same_iou or covered > inside


def _float(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(low, min(high, number))


def options_of(request):
    """The numbers a "faces" request may give, checked and filled in."""
    max_edge = int(_float(request.get("max_edge"), DEFAULT_MAX_EDGE, SMALLEST_EDGE, LARGEST_EDGE))
    return {
        "max_edge": max_edge,
        "min_edge": int(_float(request.get("min_edge"), DEFAULT_MIN_EDGE, SMALLEST_EDGE,
                               max_edge)),
        "min_size": _float(request.get("min_size"), DEFAULT_MIN_SIZE, 1, LARGEST_EDGE),
        "min_score": _float(request.get("min_score"), DEFAULT_MIN_SCORE, 0.0, 1.0),
        "max_faces": int(_float(request.get("max_faces"), DEFAULT_MAX_FACES, 1, MAX_FACES)),
    }


def find_faces(backend, data, options):
    """{"width", "height", "faces": [...]} of a picture (bytes; see the module notes).

    backend decodes, detects and embeds (OpenCVBackend, or a stand-in in
    tests). Raises WorkerError for a picture that cannot be read.
    """
    image, width, height = backend.decode(data)
    sizes = pyramid(width, height, options["max_edge"], options["min_edge"])
    candidates = []
    for index, (scale, scaled_width, scaled_height) in enumerate(sizes):
        for row in backend.detect(image, scaled_width, scaled_height):
            x, y, w, h = row[0], row[1], row[2], row[3]
            if not in_band(min(w, h), index, len(sizes)):
                continue
            score = float(row[14])
            if score < options["min_score"]:
                continue
            back = 1.0 / scale
            candidates.append({
                "box": [x * back, y * back, w * back, h * back],
                "landmarks": [[row[4 + 2 * k] * back, row[5 + 2 * k] * back] for k in range(5)],
                "score": score,
                "edge": max(scaled_width, scaled_height),
            })
    kept = [item for item in merge(candidates)
            if min(item["box"][2], item["box"][3]) >= options["min_size"]]
    kept = kept[:options["max_faces"]]
    faces = []
    for item in kept:
        vector, norm, sharpness = backend.embed(image, item)
        faces.append({
            "box": [round(value, 2) for value in item["box"]],
            "landmarks": [[round(x, 2), round(y, 2)] for x, y in item["landmarks"]],
            "score": round(item["score"], 4),
            "embedding": base64.b64encode(vector).decode("ascii"),
            "norm": round(norm, 4),
            "sharpness": round(sharpness, 2),
            "edge": item["edge"],
        })
    return {"width": width, "height": height, "faces": faces}


# ---------------------------------------------------------------------------
# OpenCV

class OpenCVBackend:
    """YuNet and SFace with OpenCV's dnn module (on the processor)."""

    def __init__(self):
        import cv2
        import numpy
        self.cv2 = cv2
        self.np = numpy
        self.detector = None
        self.recognizer = None
        self.detector_size = None

    def versions(self):
        return {"opencv": self.cv2.__version__, "numpy": self.np.__version__}

    def load(self, detector, recognizer, threads=0):
        for path in (detector, recognizer):
            if not isinstance(path, str) or not os.path.isfile(path):
                raise WorkerError("There is no model file %s." % path)
        if isinstance(threads, int) and threads > 0:
            self.cv2.setNumThreads(threads)
        try:
            self.detector = self.cv2.FaceDetectorYN.create(
                detector, "", (320, 320), DETECTOR_SCORE, DETECTOR_NMS, DETECTOR_TOP_K)
            self.recognizer = self.cv2.FaceRecognizerSF.create(recognizer, "")
        except self.cv2.error as error:
            self.detector = self.recognizer = None
            raise WorkerError("OpenCV cannot load the models: %s" % _short(error))
        self.detector_size = (320, 320)

    def decode(self, data):
        cv2, np = self.cv2, self.np
        image = cv2.imdecode(np.frombuffer(data, np.uint8),
                             cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if image is None or image.size == 0:
            raise WorkerError("The picture cannot be decoded.")
        height, width = image.shape[:2]
        return image, width, height

    def detect(self, image, width, height):
        """The detector's rows (x, y, w, h, 5 landmarks, score) at that size."""
        if self.detector is None:
            raise WorkerError("The models are not loaded.")
        cv2 = self.cv2
        if (width, height) != (image.shape[1], image.shape[0]):
            image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        if self.detector_size != (width, height):
            self.detector.setInputSize((width, height))
            self.detector_size = (width, height)
        _count, found = self.detector.detect(image)
        if found is None:
            return []
        return [list(map(float, row)) for row in found]

    def embed(self, image, face):
        """(vector bytes, norm, sharpness) of a face ({"box", "landmarks", "score"})."""
        if self.recognizer is None:
            raise WorkerError("The models are not loaded.")
        cv2, np = self.cv2, self.np
        row = list(face["box"])
        for x, y in face["landmarks"]:
            row += [x, y]
        row.append(face["score"])
        crop = self.recognizer.alignCrop(image, np.array(row, dtype=np.float32))
        feature = self.recognizer.feature(crop).astype(np.float32).reshape(-1)
        norm = float(np.linalg.norm(feature))
        unit = feature / norm if norm > 0 else feature
        grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(grey, cv2.CV_64F).var())
        return unit.astype("<f4").tobytes(), norm, sharpness


def _short(error, limit=300):
    text = " ".join(str(error).split())
    return text[:limit]


# ---------------------------------------------------------------------------
# The protocol

def answer(request, backend_factory, state):
    """The answer (a dict) to one request; state holds the backend between requests."""
    op = request.get("op")
    if op == "hello":
        reply = {"ok": True, "worker": WORKER_VERSION,
                 "python": "%d.%d.%d" % sys.version_info[:3]}
        try:
            backend = state.get("backend") or backend_factory()
            state["backend"] = backend
            reply.update(backend.versions())
        except ImportError as error:
            reply["problem"] = ("This Python has no %s: install the people environment's "
                                "packages (see SETTINGS.md)." % (error.name or error))
        return reply
    if op == "load":
        backend = state.get("backend") or backend_factory()
        state["backend"] = backend
        threads = request.get("threads")
        backend.load(request.get("detector"), request.get("recognizer"),
                     threads if isinstance(threads, int) and not isinstance(threads, bool) else 0)
        state["loaded"] = True
        return {"ok": True}
    if op == "faces":
        if not state.get("loaded"):
            raise WorkerError("The models are not loaded.")
        try:
            data = base64.b64decode(request.get("image") or "", validate=True)
        except (ValueError, TypeError):
            raise WorkerError("The picture is not base64.")
        if not data:
            raise WorkerError("There is no picture.")
        started = time.perf_counter()
        result = find_faces(state["backend"], data, options_of(request))
        result["ok"] = True
        result["seconds"] = round(time.perf_counter() - started, 4)
        return result
    if op == "quit":
        return {"ok": True}
    raise WorkerError("Unknown request: %r." % (op,))


def serve(reader, writer, backend_factory=OpenCVBackend):
    """Answer requests (lines of JSON from reader) on writer until "quit" or the end
    of the input. Both are binary streams."""
    state = {}
    while True:
        line = reader.readline(MAX_LINE + 1)
        if not line:
            return
        request_id = op = None
        try:
            if len(line) > MAX_LINE:
                # (The rest of an over-long line is skipped.)
                while line and not line.endswith(b"\n"):
                    line = reader.readline(MAX_LINE + 1)
                raise WorkerError("The request is too long.")
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise WorkerError("A request is a JSON object.")
            request_id, op = request.get("id"), request.get("op")
            reply = answer(request, backend_factory, state)
        except WorkerError as error:
            reply = {"ok": False, "error": str(error)}
        except ValueError as error:
            reply = {"ok": False, "error": "The request cannot be read: %s" % _short(error)}
        except MemoryError:
            reply = {"ok": False, "error": "Out of memory."}
        except Exception as error:     # (anything OpenCV raises: the next picture may work)
            reply = {"ok": False, "error": "%s: %s" % (type(error).__name__, _short(error))}
        reply["id"] = request_id
        writer.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
        writer.flush()
        if op == "quit":
            return


def main():
    # Answers go to the real standard output; anything else written there
    # (by OpenCV's native code, say) goes to standard error instead, so it
    # can never be mistaken for an answer.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    serve(sys.stdin.buffer, protocol)
    return 0


if __name__ == "__main__":
    sys.exit(main())
