#!/usr/bin/env python3
"""Insta360 files: the trailer at their end, and which way was up while recording.

An Insta360 camera ends the files it writes (.insv videos and their LRV
copies, .insp and .jpg photos) with a "trailer": records of data about the
recording, then a 72-byte footer -- 32 reserved bytes, the trailer's size
and its version (little-endian 32-bit numbers), and the fixed mark
TRAILER_MAGIC. Each record is followed by 6 bytes: its format (0 binary, 1
protobuf), its kind and its size (little-endian 32-bit). Newer cameras put
a table of the records last (kind 0): 10 bytes for each, its kind, format,
size and offset from the trailer's start; the records before it are not
always followed by those 6 bytes, so with a table the table is what is
read (walking back from record to record would lose its way). Without one
the records are walked back from the footer.

Two kinds of records are read here:

  1  the camera's details (protobuf): its model (field 2), when the first
     frame was taken on the motion sensor's clock (24), how far the motion
     sensor's clock is behind (28, milliseconds) and the ranges of its
     accelerometer and gyroscope (65: field 1 in g, field 2 in degrees per
     second);
  3  the motion sensor's samples, about 500 or 1000 a second: the time,
     the acceleration along three axes and the rotation speed about them.
     Either 20 bytes each -- the time in microseconds (64-bit), then six
     16-bit readings offset by 32768, full scale being the ranges above
     (cameras that give the ranges, such as the X4) -- or 56 bytes each:
     the time in milliseconds, then six doubles in g and radians per
     second (such as the ONE R);
  6  the times of a timelapse's (or TimeShift's) frames, on the motion
     sensor's clock and in the samples' unit (64-bit each): such a video
     plays many times faster than it was recorded, so its frame k (at k /
     frame rate seconds of the video) shows the moment the record gives,
     not the one at the same time of the recording.

level_track() turns them into the direction of "up" in a video's stitched
panorama over the video's time, so the panorama can be turned to keep the
horizon level as it plays (the viewer does that for each frame; posters,
contact sheets and stitched 360 photos are turned when they are made, see
images.level_equirect). Up is worked out from the accelerometer, which
measures gravity plus the camera's own accelerations: the readings over a
few seconds (LEVEL_WINDOW either side) are averaged, after the gyroscope's
rotations are taken out of them, which leaves gravity and follows the
camera's quick turns. The camera's heading is kept (only its tilt is taken
out; a camera upside down, as on a hanging mount, keeps the heading of
its front: see up_rotation). The axes of the motion sensor in the
panorama were found by looking at the panoramas of tilted cameras
(IMU_AXES), and the sensor's time agrees with the video's to a few
milliseconds (measured by comparing how fast the picture changes with how
fast the camera turned).

This module uses only the standard library.
"""

import array
import bisect
import collections
import io
import json
import math
import os
import re
import struct
import threading

import common

TRAILER_MAGIC = b"8db42d694ccc418790edff439fe026bf"
FOOTER_BYTES = 72               # 32 reserved, size, version, TRAILER_MAGIC
DESCRIPTOR_BYTES = 6            # after each record: format, kind, size
TABLE_ENTRY_BYTES = 10          # kind, format, size, offset
MAX_RECORDS = 256               # a walk back stops after this many records
RECORD_TABLE = 0
RECORD_DETAILS = 1
RECORD_MOTION = 3
RECORD_FRAME_TIMES = 6
MAX_DETAILS_BYTES = 1 << 20
MAX_FRAME_TIMES = 1 << 22       # (64-bit each: a record of up to 32 MB is read)

# The axes of each camera's motion sensor in its stitched panorama (x to
# the right, y up, z ahead: the middle of the panorama; see
# images.dual_fisheye_to_equirect and videos.CAMERAS_360): for the
# panorama's x, y and z, which sensor axis (0, 1, 2) with which sign. Found
# by looking at stitched frames of tilted cameras (a camera held flat over
# a pool, one held low and tipped forward on a walk, one on a tripod) with
# each of the 48 ways of matching the axes: these are the ones whose
# panoramas came out level. The panorama's axes are left-handed, the
# sensor's right-handed, so each mapping is a mirror image and the
# rotation speeds change sign with it (see _axes).
IMU_AXES = {
    "insta360x4": ((2, 1), (0, -1), (1, 1)),
    "insta360oner": ((2, -1), (0, 1), (1, 1)),
}
# The cameras whose unstitched 360 photos (.insp) are levelled too: those
# whose photos are stitched with the lens model the axes above were found
# for (images.INSTA360_PARAMS). (An X4 stitches its own 360 photos, and
# levels them; they are left as they are.)
PHOTO_CAMERAS = ("insta360oner",)

# The samples are averaged over bins this long before anything else ...
BIN_SECONDS = 0.01
# ... gravity over this many seconds either side of a moment ...
LEVEL_WINDOW = 2.0
# ... and up is worked out this many times a second of video, ...
LEVEL_RATE = 30.0
# ... then thinned out: a sample is left out when the others, joined by
# straight lines, stay within this many degrees of it.
LEVEL_TOLERANCE_DEG = 0.5
# Readings must cover at least this share of a video for a track. Where
# they stop early (or start late) by more than TRACK_GAP seconds, the track
# goes back to no turn at all within LEVEL_WINDOW seconds: a tilt held from
# the last reading on may be further off than none.
MIN_COVERAGE = 0.5
TRACK_GAP = 0.5
# Readings that span much more time than the video lasts (more than
# MAX_SPAN times its length and MAX_SPAN_SLACK seconds) belong to a video
# that plays faster than it was recorded: without its frame times (record
# 6) it has no track.
MAX_SPAN = 1.5
MAX_SPAN_SLACK = 10.0
# The frame times of record 6 must start this close to the first frame.
FRAME_TIMES_SLACK = 1.0
# A video's frame rate when it is not known (for record 6's frames).
DEFAULT_FPS = 30.0
# A camera turned further than INVERT_ENTER_DEG from upright counts as
# upside down until it comes back within INVERT_LEAVE_DEG (a level track
# marks those samples; a single moment, as a photo's, is upside down past
# INVERTED_DEG): see up_rotation.
INVERT_ENTER_DEG = 150.0
INVERT_LEAVE_DEG = 120.0
INVERTED_DEG = 135.0
# A photo's samples run from a little before the moment it was taken to
# this many seconds after it (used when its first frame time is stale).
PHOTO_AFTER = 0.75
# A camera at rest reads about 1 g; samples whose readings are far from
# anything a hand-held camera feels (or not numbers) are dropped.
MAX_ACCELERATION = 8.0
MIN_GRAVITY = 0.3
# Reading the samples: this many at a time. A sample further than
# MAX_JUMP seconds after the one before is taken for a damaged one (and
# dropped); no more than MAX_SECONDS of samples are read.
CHUNK_SAMPLES = 8192
MAX_JUMP = 60.0
MAX_SECONDS = 6 * 3600.0
# The level track's numbers: seconds and the up vector's parts, rounded.
TIME_DECIMALS = 3
UP_DECIMALS = 4

Trailer = collections.namedtuple("Trailer", "start size version records")
Details = collections.namedtuple(
    "Details", "model first_frame sensor_offset_ms acc_range gyro_range")


def model_key(model):
    """A camera model as the tables here name it: no spaces, lower case."""
    return re.sub(r"\s+", "", model).lower() if isinstance(model, str) else ""


# ---------------------------------------------------------------------------
# The trailer


def _open(source):
    """(a binary handle, whether to close it) for a path, bytes or an open handle."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(source)), True
    if isinstance(source, str):
        return open(source, "rb"), True
    return source, False


def _size(handle):
    try:
        return os.fstat(handle.fileno()).st_size
    except (AttributeError, OSError, io.UnsupportedOperation):
        here = handle.tell()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(here)
        return size


def read_trailer(handle):
    """The trailer of an open Insta360 file (binary handle), or None.

    Trailer(start, size, version, records): records maps each kind of
    record to (offset in the file, size, format) of its first one.
    """
    size = _size(handle)
    if size < FOOTER_BYTES + DESCRIPTOR_BYTES:
        return None
    handle.seek(size - FOOTER_BYTES)
    footer = handle.read(FOOTER_BYTES)
    if len(footer) != FOOTER_BYTES or not footer.endswith(TRAILER_MAGIC):
        return None
    length, version = struct.unpack("<II", footer[32:40])
    if length < FOOTER_BYTES + DESCRIPTOR_BYTES or length > size:
        return None
    start = size - length
    end = size - FOOTER_BYTES             # (records end here)
    records = {}
    handle.seek(end - DESCRIPTOR_BYTES)
    fmt, kind, record_size = struct.unpack("<BBI", handle.read(DESCRIPTOR_BYTES))
    table_start = end - DESCRIPTOR_BYTES - record_size
    if (kind == RECORD_TABLE and record_size and not record_size % TABLE_ENTRY_BYTES
            and table_start >= start):
        handle.seek(table_start)
        table = handle.read(record_size)
        for at in range(0, len(table) - TABLE_ENTRY_BYTES + 1, TABLE_ENTRY_BYTES):
            kind, fmt, record_size, offset = struct.unpack_from("<BBII", table, at)
            if (kind != RECORD_TABLE and record_size and kind not in records
                    and start + offset + record_size <= table_start):
                records[kind] = (start + offset, record_size, fmt)
        if records:
            return Trailer(start, length, version, records)
    position = end
    for _ in range(MAX_RECORDS):
        if position - DESCRIPTOR_BYTES < start:
            break
        handle.seek(position - DESCRIPTOR_BYTES)
        fmt, kind, record_size = struct.unpack("<BBI", handle.read(DESCRIPTOR_BYTES))
        record_start = position - DESCRIPTOR_BYTES - record_size
        if record_start < start:
            break
        records.setdefault(kind, (record_start, record_size, fmt))
        position = record_start
    return Trailer(start, length, version, records)


def _varint(data, at):
    value = shift = 0
    while True:
        if at >= len(data) or shift > 63:
            raise ValueError("a cut protobuf number")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, at


def protobuf_fields(data):
    """{field number: its first value} of a protobuf message: numbers for varints,
    floats for 64-bit and 32-bit fields, bytes for the rest. Stops at anything
    it cannot read (what came before stays)."""
    fields = {}
    at = 0
    try:
        while at < len(data):
            key, at = _varint(data, at)
            number, wire = key >> 3, key & 7
            if wire == 0:
                value, at = _varint(data, at)
            elif wire == 1:
                if at + 8 > len(data):
                    break
                value = struct.unpack_from("<d", data, at)[0]
                at += 8
            elif wire == 5:
                if at + 4 > len(data):
                    break
                value = struct.unpack_from("<f", data, at)[0]
                at += 4
            elif wire == 2:
                length, at = _varint(data, at)
                if at + length > len(data):
                    break
                value = bytes(data[at:at + length])
                at += length
            else:
                break
            fields.setdefault(number, value)
    except ValueError:
        pass
    return fields


def _number(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value if math.isfinite(value) else default


def read_details(handle, trailer):
    """The camera's details (Details) from a trailer's record 1, or None."""
    found = trailer.records.get(RECORD_DETAILS) if trailer else None
    if found is None or found[1] > MAX_DETAILS_BYTES:
        return None
    handle.seek(found[0])
    fields = protobuf_fields(handle.read(found[1]))
    model = fields.get(2)
    model = model.decode("ascii", "replace").strip() if isinstance(model, bytes) else ""
    first = fields.get(24)
    ranges = protobuf_fields(fields[65]) if isinstance(fields.get(65), bytes) else {}
    acc_range = _number(ranges.get(1))
    gyro_range = _number(ranges.get(2))
    return Details(model, first if isinstance(first, int) else None,
                   float(_number(fields.get(28), 0.0)),
                   float(acc_range) if acc_range and acc_range > 0 else None,
                   float(gyro_range) if gyro_range and gyro_range > 0 else None)


def camera_model(source):
    """The model a file's trailer names ("Insta360 X4"), or "" (path, bytes or handle)."""
    try:
        handle, own = _open(source)
    except OSError:
        return ""
    try:
        details = read_details(handle, read_trailer(handle))
    except (OSError, struct.error, ValueError):
        details = None
    finally:
        if own:
            handle.close()
    return details.model if details else ""


# ---------------------------------------------------------------------------
# The motion sensor's samples

# How each layout of the samples is read: bytes per sample, struct format,
# seconds per unit of its times, and whether its readings are raw numbers.
_LAYOUTS = {
    20: ("<Q6H", 1e-6, True),
    56: ("<Q6d", 1e-3, False),
}
_LAYOUT_CHECK = 64          # samples looked at to tell the layouts apart


def _layout_order(record_size, details):
    """The sample sizes a motion record of this size may hold, likelier first."""
    sizes = [size for size in (20, 56) if record_size % size == 0]
    if details.acc_range is None:
        sizes.reverse()             # (no ranges: likely doubles)
    return sizes


def _scales(details, raw):
    """(g per reading, radians per second per reading) of a layout."""
    if not raw:
        return 1.0, 1.0
    acc_range = details.acc_range or 16.0
    gyro_range = details.gyro_range or 2000.0
    return acc_range / 32768.0, math.radians(gyro_range) / 32768.0


def _decode(values, raw, acc_scale, gyro_scale):
    if raw:
        return ((values[1] - 32768) * acc_scale, (values[2] - 32768) * acc_scale,
                (values[3] - 32768) * acc_scale, (values[4] - 32768) * gyro_scale,
                (values[5] - 32768) * gyro_scale, (values[6] - 32768) * gyro_scale)
    return values[1:7]


def _plausible(data, layout, details):
    """True when the first samples of data read sensibly with a layout: times
    that go up in small steps and an acceleration near 1 g (most of them:
    a few bad samples are dropped later)."""
    sample, fmt, unit, raw = layout
    acc_scale, gyro_scale = _scales(details, raw)
    count = min(_LAYOUT_CHECK, len(data) // sample)
    if count < 2:
        return False
    times, near = [], 0
    for values in struct.iter_unpack(fmt, data[:count * sample]):
        reading = _decode(values, raw, acc_scale, gyro_scale)
        times.append(values[0])
        if (all(math.isfinite(value) for value in reading)
                and 0.5 <= math.sqrt(sum(value * value for value in reading[:3])) <= 2.0):
            near += 1
    steps = sum(1 for earlier, later in zip(times, times[1:])
                if 0 < (later - earlier) * unit < 0.1)
    return steps >= 0.8 * (count - 1) and near * 2 >= count


def _sample_layout(handle, trailer, details):
    """(bytes per sample, struct format, seconds per unit of its times, raw) of a
    trailer's motion samples, or None (no samples, or none that read sensibly)."""
    found = trailer.records.get(RECORD_MOTION) if trailer else None
    if found is None or details is None or details.first_frame is None:
        return None
    offset, record_size, _ = found
    handle.seek(offset)
    head = handle.read(_LAYOUT_CHECK * 56)
    for sample in _layout_order(record_size, details):
        candidate = (sample,) + _LAYOUTS[sample]
        if _plausible(head, candidate, details):
            return candidate
    return None


def read_frame_times(handle, trailer, details):
    """When each frame of a timelapse was taken (record 6), or None.

    Returns an array of seconds after the first frame was taken, on the
    clock of read_motion's times, one for each frame of the video in
    order. None without the record, or when its times do not go up or do
    not start at the first frame.
    """
    found = trailer.records.get(RECORD_FRAME_TIMES) if trailer else None
    if found is None or details is None or details.first_frame is None:
        return None
    offset, record_size, _ = found
    count = record_size // 8
    if count < 2 or count > MAX_FRAME_TIMES:
        return None
    layout = _sample_layout(handle, trailer, details)
    if layout is None:
        return None
    unit = layout[2]
    handle.seek(offset)
    data = handle.read(count * 8)
    if len(data) < count * 8:
        return None
    first = details.first_frame
    times = array.array("d", ((stamp - first) * unit
                              for (stamp,) in struct.iter_unpack("<Q", data)))
    if abs(times[0]) > FRAME_TIMES_SLACK:
        return None
    for earlier, later in zip(times, times[1:]):
        if not later > earlier:
            return None
    return times


def read_motion(handle, trailer, details, bin_seconds=BIN_SECONDS):
    """The motion sensor's samples, averaged over bins of bin_seconds, or None.

    Returns (times, readings): times (seconds after the first frame was
    taken, on the video's clock) of the bins' middles, and for each six
    numbers (acceleration x, y, z in g, rotation speed x, y, z in radians
    per second, in the sensor's axes), as arrays. The samples are read a
    few thousand at a time. Samples with times out of order or readings
    no camera feels are dropped.
    """
    layout = _sample_layout(handle, trailer, details)
    if layout is None:
        return None
    offset, record_size, _ = trailer.records[RECORD_MOTION]
    sample, fmt, unit, raw = layout
    acc_scale, gyro_scale = _scales(details, raw)
    shift = details.first_frame * unit + details.sensor_offset_ms / 1000.0
    sums = [array.array("d") for _ in range(6)]
    counts = array.array("l")
    first_bin = None
    last_time = None
    remaining = record_size // sample
    handle.seek(offset)
    while remaining > 0:
        count = min(CHUNK_SAMPLES, remaining)
        data = handle.read(count * sample)
        count = len(data) // sample
        if not count:
            break
        remaining -= count
        for values in struct.iter_unpack(fmt, data[:count * sample]):
            stamp = values[0]
            if last_time is not None and stamp <= last_time:
                continue
            reading = _decode(values, raw, acc_scale, gyro_scale)
            if not all(math.isfinite(value) and abs(value) < 1e3 for value in reading):
                continue
            if (reading[0] * reading[0] + reading[1] * reading[1] + reading[2] * reading[2]
                    > MAX_ACCELERATION * MAX_ACCELERATION):
                continue
            seconds = stamp * unit - shift
            index = int(math.floor(seconds / bin_seconds))
            if first_bin is None:
                first_bin = index
            index -= first_bin
            if index < 0 or index - len(counts) > MAX_JUMP / bin_seconds:
                continue
            if index > MAX_SECONDS / bin_seconds:
                remaining = 0
                break
            last_time = stamp
            if index >= len(counts):
                grow = index + 1 - len(counts)
                counts.extend([0] * grow)
                for column in sums:
                    column.extend([0.0] * grow)
            counts[index] += 1
            for column, value in zip(sums, reading):
                column[index] += value
    times = array.array("d")
    readings = [array.array("d") for _ in range(6)]
    for index, count in enumerate(counts):
        if count:
            times.append((first_bin + index + 0.5) * bin_seconds)
            for column, total in zip(readings, sums):
                column.append(total[index] / count)
    if len(times) < 2:
        return None
    return times, readings


# ---------------------------------------------------------------------------
# Which way is up


def _axes(model):
    """(pick, signs, gyro sign) for a camera's IMU_AXES, or None."""
    axes = IMU_AXES.get(model_key(model))
    if axes is None:
        return None
    pick = [axis for axis, _ in axes]
    signs = [sign for _, sign in axes]
    # (The determinant of the signed permutation: a mirror image turns
    # the rotation speeds round.)
    parity = 1
    order = list(pick)
    for i in range(3):
        for j in range(i + 1, 3):
            if order[i] > order[j]:
                parity = -parity
    gyro_sign = parity * signs[0] * signs[1] * signs[2]
    return pick, signs, gyro_sign


def _quat_multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def _quat_rotate(q, v):
    """v turned by the unit quaternion q."""
    w, x, y, z = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v + w * t + cross(q.xyz, t)
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty),
            vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx))


def _normalised(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if not length or not math.isfinite(length):
        return None
    return (v[0] / length, v[1] / length, v[2] / length)


class UpFinder:
    """Up in the panorama's axes at the bins of binned samples (read_motion).

    Each bin's rotation is followed from the gyroscope; the acceleration
    is turned into one frame with it, averaged over window seconds either
    side of a moment, and turned back: gravity at that moment as the camera
    saw it, with its own short accelerations averaged out. Make one with
    UpFinder.of() (None for a camera whose axes are not known).
    """

    def __init__(self, times, turns, sums, window):
        self.times = times
        self.turns = turns
        self.sums = sums
        self.window = window

    @classmethod
    def of(cls, times, readings, model, window=LEVEL_WINDOW):
        axes = _axes(model)
        if axes is None:
            return None
        pick, signs, gyro_sign = axes
        accel = [(readings[pick[k]], signs[k]) for k in range(3)]
        spin = [(readings[3 + pick[k]], gyro_sign * signs[k]) for k in range(3)]
        count = len(times)
        turns = array.array("d")           # w, x, y, z of each bin (camera to a fixed frame)
        sums = [array.array("d", [0.0]) for _ in range(3)]
        q = (1.0, 0.0, 0.0, 0.0)
        for i in range(count):
            turns.extend(q)
            x, y, z = _quat_rotate(q, [column[i] * sign for column, sign in accel])
            sums[0].append(sums[0][-1] + x)
            sums[1].append(sums[1][-1] + y)
            sums[2].append(sums[2][-1] + z)
            if i + 1 < count:
                step = times[i + 1] - times[i]
                wx, wy, wz = [column[i] * sign for column, sign in spin]
                speed = math.sqrt(wx * wx + wy * wy + wz * wz)
                half = speed * step / 2.0
                if half > 1e-12:
                    scale = math.sin(half) / speed
                    q = _quat_multiply(q, (math.cos(half), wx * scale, wy * scale, wz * scale))
                    norm = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
                    q = (q[0] / norm, q[1] / norm, q[2] / norm, q[3] / norm)
        return cls(times, turns, sums, window)

    def nearest(self, seconds):
        """The index of the bin nearest to seconds."""
        times = self.times
        at = bisect.bisect_left(times, seconds)
        if at >= len(times):
            return len(times) - 1
        if at > 0 and seconds - times[at - 1] < times[at] - seconds:
            return at - 1
        return at

    def up(self, index):
        """Up (unit x, y, z) at a bin, or None."""
        times, sums = self.times, self.sums
        low = bisect.bisect_left(times, times[index] - self.window)
        high = bisect.bisect_right(times, times[index] + self.window)
        total = (sums[0][high] - sums[0][low], sums[1][high] - sums[1][low],
                 sums[2][high] - sums[2][low])
        w, x, y, z = self.turns[4 * index:4 * index + 4]
        return _normalised(_quat_rotate((w, -x, -y, -z), total))


def _nlerp(a, b, f):
    v = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
    return _normalised(v) or a


def _fits(samples, first, last, cos_tolerance):
    """True when samples between first and last lie within the tolerance of the
    straight (normalised) line between those two."""
    t0, u0 = samples[first]
    t1, u1 = samples[last]
    span = t1 - t0
    for k in range(first + 1, last):
        t, u = samples[k]
        v = _nlerp(u0, u1, (t - t0) / span if span > 0 else 0.0)
        if v[0] * u[0] + v[1] * u[1] + v[2] * u[2] < cos_tolerance:
            return False
    return True


def simplify(samples, tolerance_deg=LEVEL_TOLERANCE_DEG):
    """The samples [(seconds, up)] a track keeps: the first, the last, and each
    one needed so that straight lines between kept ones stay within
    tolerance_deg of all of them (greedily: each line as long as it may be,
    found by doubling then halving)."""
    if len(samples) <= 2:
        return list(samples)
    cos_tolerance = math.cos(math.radians(tolerance_deg))
    kept = [samples[0]]
    first = 0
    last_index = len(samples) - 1
    while first < last_index:
        good, step = first + 1, 1
        while True:
            probe = min(first + step * 2, last_index)
            if probe == good or not _fits(samples, first, probe, cos_tolerance):
                bad = probe
                break
            good = probe
            if probe == last_index:
                bad = None
                break
            step *= 2
        if bad is not None and bad != good:
            low, high = good, bad
            while high - low > 1:
                middle = (low + high) // 2
                if _fits(samples, first, middle, cos_tolerance):
                    low = middle
                else:
                    high = middle
            good = low
        kept.append(samples[good])
        first = good
    return kept


def _track_samples(finder, start, stop, rate):
    """[(seconds, up)] every 1 / rate seconds from start to stop (at the nearest bin)."""
    samples = []
    count = int(math.floor((stop - start) * rate + 1e-9)) + 1
    moments = [start + n / rate for n in range(count)]
    if moments[-1] < stop - 1e-6:
        moments.append(stop)
    for seconds in moments:
        up = finder.up(finder.nearest(seconds))
        if up is not None:
            samples.append((seconds, up))
    return samples


def _frame_samples(finder, frames, times, rate, duration):
    """([(seconds of the video, up)], the share of its frames the readings cover)
    for a timelapse whose frame k, at k / rate seconds of the video, was taken
    at frames[k] seconds of the readings' clock (read_frame_times; times: the
    readings', see read_motion): up when each frame was taken, at about
    LEVEL_RATE frames a second (and the last one), up to duration when it is
    known."""
    step = max(1, int(round(rate / LEVEL_RATE)))
    picked = list(range(0, len(frames), step))
    if picked[-1] != len(frames) - 1:
        picked.append(len(frames) - 1)
    low, high = times[0] - BIN_SECONDS, times[-1] + BIN_SECONDS
    samples, moments = [], 0
    for k in picked:
        seconds = k / rate
        if duration and seconds > duration + 1e-6:
            break
        moments += 1
        if low <= frames[k] <= high:
            up = finder.up(finder.nearest(frames[k]))
            if up is not None:
                samples.append((seconds, up))
    return samples, (len(samples) / float(moments) if moments else 0.0)


def _upside_down(samples):
    """For each of a track's samples [(seconds, up)]: is the camera upside down then?
    It starts so when turned more than INVERTED_DEG from upright, becomes so past
    INVERT_ENTER_DEG and stops being so within INVERT_LEAVE_DEG (so a camera that
    wobbles about one tilt does not switch back and forth; see up_rotation)."""
    flags = []
    inverted = None
    for _, up in samples:
        tilt = tilt_degrees(up)
        if inverted is None:
            inverted = tilt > INVERTED_DEG
        elif inverted and tilt < INVERT_LEAVE_DEG:
            inverted = False
        elif not inverted and tilt > INVERT_ENTER_DEG:
            inverted = True
        flags.append(inverted)
    return flags


def _faded(samples, flags, duration):
    """A track's samples and flags with no turn at the ends the readings do not
    cover: where they start TRACK_GAP seconds or more into the video, or stop
    that long before its end (duration, when known), the track goes back to
    up straight up (straight down for a camera upside down: see up_rotation)
    within LEVEL_WINDOW seconds, and stays there."""
    samples, flags = list(samples), list(flags)
    if samples[0][0] > TRACK_GAP:
        samples.insert(0, (max(0.0, samples[0][0] - LEVEL_WINDOW),
                           (0.0, -1.0 if flags[0] else 1.0, 0.0)))
        flags.insert(0, flags[0])
    if duration and samples[-1][0] < duration - TRACK_GAP:
        samples.append((min(float(duration), samples[-1][0] + LEVEL_WINDOW),
                        (0.0, -1.0 if flags[-1] else 1.0, 0.0)))
        flags.append(flags[-1])
    return samples, flags


def _track_of(samples, flags):
    """The level track of samples [(seconds, up)] and their flags (_upside_down):
    each run of samples of one flag thinned out (simplify, which keeps a run's
    first and last), rounded; a sample whose time rounds to its predecessor's
    is left out."""
    track = []
    start = 0
    for end in range(1, len(samples) + 1):
        if end < len(samples) and flags[end] == flags[start]:
            continue
        for seconds, up in simplify(samples[start:end]):
            seconds = round(seconds, TIME_DECIMALS)
            if track and seconds <= track[-1][0]:
                continue
            track.append([seconds] + [round(part, UP_DECIMALS) for part in up]
                         + ([1] if flags[start] else []))
        start = end
    return track


def _motion_of(source):
    """(Details, read_motion's result, read_frame_times's) of a file (path, bytes or
    handle); (details, None, None) without motion samples, (None, None, None) when
    it cannot be read."""
    try:
        handle, own = _open(source)
    except OSError:
        return None, None, None
    try:
        trailer = read_trailer(handle)
        details = read_details(handle, trailer)
        motion = read_motion(handle, trailer, details) if details else None
        frames = read_frame_times(handle, trailer, details) if motion else None
    except (OSError, struct.error, ValueError, MemoryError):
        return None, None, None
    finally:
        if own:
            handle.close()
    return details, motion, frames


def _positive(value):
    """value as a float when it is a positive finite number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def level_track(source, model=None, duration=None, fps=None):
    """A video's level track: [[seconds, x, y, z], ...], or None.

    (x, y, z) is the direction of up in the video's stitched panorama (x
    to the right, y up, z ahead) at that time of the video; between two
    samples it moves in a straight line (normalised), and before the first
    and after the last it stays. A fifth number, 1, marks the samples of a
    camera upside down (up to the next sample: see up_rotation). source is
    the .insv (or its LRV: both hold the same samples) as a path, bytes or
    an open handle; model the camera ("Insta360 X4"; default: the one the
    trailer names) -- only cameras of IMU_AXES have a track; duration the
    video's length in seconds (the track ends there) and fps its frame
    rate (for a timelapse, whose frame times the trailer gives: see
    read_frame_times; default its frames over its duration). None also
    when the file has no motion samples, when they cover less than
    MIN_COVERAGE of the video, or when (without frame times) they span far
    more time than it lasts (MAX_SPAN). Where the samples stop early (or
    start late), the track turns back to no turn (TRACK_GAP).
    """
    details, motion, frames = _motion_of(source)
    if motion is None:
        return None
    model = model if model else details.model
    times, readings = motion
    finder = UpFinder.of(times, readings, model)
    if finder is None:
        return None
    duration = _positive(duration)
    if frames is not None:
        rate = _positive(fps) or (len(frames) / duration if duration else DEFAULT_FPS)
        samples, covered = _frame_samples(finder, frames, times, rate, duration)
        if covered < MIN_COVERAGE:
            return None
    else:
        # (times are the middles of the bins.)
        start = max(0.0, times[0] - BIN_SECONDS / 2.0)
        stop = times[-1]
        if duration:
            if stop - start < MIN_COVERAGE * duration:
                return None
            if stop - start > MAX_SPAN * duration + MAX_SPAN_SLACK:
                return None
            stop = min(stop, duration)
        if stop <= start:
            return None
        samples = _track_samples(finder, start, stop, LEVEL_RATE)
    if len(samples) < 2:
        return None
    samples, flags = _faded(samples, _upside_down(samples), duration)
    track = _track_of(samples, flags)
    return track if len(track) >= 2 else None


def capture_up(source, model=None):
    """Up (x, y, z) in a 360 photo's stitched panorama when it was taken, or None.

    From its trailer's motion samples around the moment the picture was
    taken (time 0: its first frame), as level_track works it out. A camera
    taking photos in quick succession may leave the first frame time of
    the photo before in the trailer; the samples (which cover only this
    photo, from a little before it was taken to PHOTO_AFTER seconds after)
    then say when it was. Only for cameras of PHOTO_CAMERAS (model:
    default the trailer's).
    """
    details, motion, _ = _motion_of(source)
    if motion is None:
        return None
    model = model if model else details.model
    if model_key(model) not in PHOTO_CAMERAS:
        return None
    times, readings = motion
    finder = UpFinder.of(times, readings, model)
    if finder is None:
        return None
    moment = 0.0
    if not times[0] <= moment <= times[-1]:
        moment = max(times[0], times[-1] - PHOTO_AFTER)
    up = finder.up(finder.nearest(moment))
    return tuple(round(part, UP_DECIMALS + 2) for part in up) if up else None


# ---------------------------------------------------------------------------
# Using a track


def checked_track(track):
    """A level track (level_track's shape) if it is one, else None: at least one
    sample, times going up, finite parts, up vectors of about unit length, and
    a fifth number (upside down) only 1 or 0."""
    if not isinstance(track, list) or not track:
        return None
    last = None
    for sample in track:
        if (not isinstance(sample, list) or len(sample) not in (4, 5)
                or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and math.isfinite(value) for value in sample)):
            return None
        if len(sample) == 5 and sample[4] not in (0, 1):
            return None
        if last is not None and sample[0] <= last:
            return None
        if not 0.9 < math.sqrt(sum(value * value for value in sample[1:4])) < 1.1:
            return None
        last = sample[0]
    return track


def _sample_at(track, seconds):
    """The index of a track's last sample at or before seconds (-1: none)."""
    return bisect.bisect_right([sample[0] for sample in track], seconds) - 1


def up_at(track, seconds):
    """Up (x, y, z) at seconds of a video from its level track (see level_track)."""
    if not track:
        return (0.0, 1.0, 0.0)
    at = _sample_at(track, seconds) + 1
    if at <= 0:
        return _normalised(track[0][1:4]) or (0.0, 1.0, 0.0)
    if at >= len(track):
        return _normalised(track[-1][1:4]) or (0.0, 1.0, 0.0)
    before, after = track[at - 1], track[at]
    span = after[0] - before[0]
    return _nlerp(_normalised(before[1:4]) or (0.0, 1.0, 0.0),
                  _normalised(after[1:4]) or (0.0, 1.0, 0.0),
                  (seconds - before[0]) / span if span > 0 else 0.0)


def inverted_at(track, seconds):
    """Whether the camera counts as upside down at seconds of a video: the mark of
    its level track's sample at or before then (the first sample's before it)."""
    if not track:
        return False
    sample = track[max(0, _sample_at(track, seconds))]
    return len(sample) > 4 and sample[4] == 1


def _smallest_turn(x, y, z):
    """The smallest rotation (3 rows) that turns (0, 1, 0) into the unit vector
    (x, y, z); half round the x axis for (0, -1, 0)."""
    # The axis is (0, 1, 0) x up = (z, 0, -x); Rodrigues' formula.
    s = math.sqrt(x * x + z * z)
    c = y
    if s < 1e-9:
        if c > 0:
            return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    kx, kz = z / s, -x / s
    one = 1.0 - c
    return ((c + kx * kx * one, -kz * s, kx * kz * one),
            (kz * s, c, -kx * s),
            (kx * kz * one, kx * s, c + kz * kz * one))


def up_rotation(up, inverted=None):
    """The rotation (3 rows) that takes a level panorama's directions to the camera's.

    It turns (0, 1, 0) into up. For a camera upright or tilted, by the
    smallest turn, so the heading is kept. For one upside down (inverted;
    None: when up is more than INVERTED_DEG from straight up) that turn
    is no use -- its heading would swing round with every small lean --
    so it is half a turn about the forward axis (z) followed by the
    smallest turn of (0, -1, 0) onto up, which keeps the heading of the
    camera's front. Directions are x to the right, y up, z ahead.
    """
    up = _normalised(up[:3]) if up else None
    if up is None:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    x, y, z = up
    if inverted is None:
        inverted = y < math.cos(math.radians(INVERTED_DEG))
    if not inverted:
        return _smallest_turn(x, y, z)
    # (The smallest turn of (0, -1, 0) onto up is that of (0, 1, 0) onto
    # -up; the half turn about z turns its x and y columns round.)
    return tuple((-row[0], -row[1], row[2]) for row in _smallest_turn(-x, -y, -z))


def turn(rotation, direction):
    """A direction (x, y, z) turned by a rotation (up_rotation's rows)."""
    x, y, z = direction
    return tuple(row[0] * x + row[1] * y + row[2] * z for row in rotation)


def tilt_degrees(up):
    """How far up is from straight up, in degrees."""
    up = _normalised(up[:3]) if up else None
    return math.degrees(math.acos(max(-1.0, min(1.0, up[1])))) if up else 0.0


# ---------------------------------------------------------------------------
# The setting

LEVEL_DEFAULT = True


def level_option(config):
    """The "level_horizon" setting of a config dict (default LEVEL_DEFAULT): False
    only for an explicit false."""
    value = config.get("level_horizon", LEVEL_DEFAULT) if isinstance(config, dict) else None
    return value if isinstance(value, bool) else LEVEL_DEFAULT


_setting_lock = threading.Lock()
_setting_memo = [None, LEVEL_DEFAULT]     # [config.json's (path, mtime, size), value]


def level_enabled():
    """level_option() of config.json, read again only when the file changes."""
    path = common.config_path()
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (path, None, None)
    with _setting_lock:
        if _setting_memo[0] == key:
            return _setting_memo[1]
    try:
        config = common.load_config()
    except common.ConfigError:
        config = {}
    value = level_option(config)
    with _setting_lock:
        _setting_memo[0], _setting_memo[1] = key, value
    return value


def main(argv=None):
    """python insta360.py FILE...: what the trailer of each file says, and its level."""
    import sys
    sys.stdout.reconfigure(errors="backslashreplace")
    for path in (argv if argv is not None else sys.argv[1:]):
        with open(path, "rb") as handle:
            trailer = read_trailer(handle)
            if trailer is None:
                print("%s: no Insta360 trailer" % path)
                continue
            details = read_details(handle, trailer)
        print("%s: trailer of %d bytes (version %d), records %s"
              % (path, trailer.size, trailer.version, sorted(trailer.records)))
        if details:
            print("  camera %s" % (details.model or "?"))
        track = level_track(path)
        if track:
            tilts = [tilt_degrees(sample[1:]) for sample in track]
            print("  level track: %d samples over %.1f s, %d bytes; tilt %.1f to %.1f degrees"
                  % (len(track), track[-1][0] - track[0][0],
                     len(json.dumps(track, separators=(",", ":"))), min(tilts), max(tilts)))
        else:
            up = capture_up(path)
            if up:
                print("  up when taken: %s (tilt %.1f degrees)" % (up, tilt_degrees(up)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
