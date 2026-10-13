"""Made-up Insta360 trailers for the tests (see insta360.py for the layout).

trailer(records, table) is the end of a file: the records, each followed
by its 6-byte descriptor (format, kind, size) -- or, with table=True, as a
newer camera writes them: the records without descriptors and a table of
them last -- then the 72-byte footer. details() is a camera's details
record (protobuf), raw_motion() and double_motion() a motion record in
either sample layout, frame_times() a timelapse's frame times.
camera_samples() makes the samples of a camera whose up (in its panorama's
axes, x right, y up, z ahead) and turning are known, in its sensor's axes
(insta360.IMU_AXES); rolled() those of one rolling to and fro.
"""

import math
import struct

MAGIC = b"8db42d694ccc418790edff439fe026bf"


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def protobuf(fields):
    """A protobuf message: {number: int (varint) | float (double) | bytes | str | dict}."""
    out = bytearray()
    for number, value in fields.items():
        if isinstance(value, bool) or isinstance(value, int):
            out += _varint(number << 3) + _varint(int(value))
        elif isinstance(value, float):
            out += _varint(number << 3 | 1) + struct.pack("<d", value)
        else:
            if isinstance(value, dict):
                value = protobuf(value)
            elif isinstance(value, str):
                value = value.encode("ascii")
            out += _varint(number << 3 | 2) + _varint(len(value)) + bytes(value)
    return bytes(out)


def details(model="Insta360 X4", first_frame=None, sensor_offset_ms=None, ranges=None,
            serial="SERIAL0000"):
    """A camera's details record: model (field 2), first frame time (24), the
    sensor's offset in ms (28) and ranges ((g, degrees per second): field 65)."""
    fields = {1: serial, 2: model, 3: "v0.0.0_build0"}
    if first_frame is not None:
        fields[24] = int(first_frame)
    if sensor_offset_ms is not None:
        fields[28] = float(sensor_offset_ms)
    if ranges is not None:
        fields[65] = {1: int(ranges[0]), 2: int(ranges[1])}
    return protobuf(fields)


def raw_motion(samples, acc_range=32.0, gyro_range=2000.0):
    """20-byte samples: (time in microseconds, ax, ay, az in g, gx, gy, gz in radians
    per second) as raw readings offset by 32768."""
    out = bytearray()
    for stamp, ax, ay, az, gx, gy, gz in samples:
        acc = [int(round(value * 32768.0 / acc_range)) + 32768 for value in (ax, ay, az)]
        spin = [int(round(math.degrees(value) * 32768.0 / gyro_range)) + 32768
                for value in (gx, gy, gz)]
        out += struct.pack("<Q6H", int(stamp), *[min(65535, max(0, value))
                                                 for value in acc + spin])
    return bytes(out)


def double_motion(samples):
    """56-byte samples: (time in milliseconds, then six doubles)."""
    return b"".join(struct.pack("<Q6d", int(stamp), *values[:6])
                    for stamp, *values in samples)


def frame_times(stamps):
    """A timelapse's frame times record (kind 6): each frame's time on the
    sensor's clock (in its samples' unit), 64-bit."""
    return b"".join(struct.pack("<Q", int(stamp)) for stamp in stamps)


def trailer(records, table=False, version=3, pad=b""):
    """The end of a file: records [(kind, format, bytes)], then the footer.

    table=True: the records are laid end to end with a table of them last
    (kind 0: kind, format, size, offset from the trailer's start). pad
    goes first (part of the trailer).
    """
    body = bytearray(pad)
    if table:
        entries = bytearray()
        for kind, fmt, data in records:
            entries += struct.pack("<BBII", kind, fmt, len(data), len(body))
            body += data
        # (A cleared entry, as cameras leave them.)
        entries += struct.pack("<BBII", 0, 0, 0, 0)
        body += entries + struct.pack("<BBI", 0, 0, len(entries))
    else:
        for kind, fmt, data in records:
            body += data + struct.pack("<BBI", fmt, kind, len(data))
    size = len(body) + 72
    return bytes(body) + bytes(32) + struct.pack("<II", size, version) + MAGIC


def rodrigues(axis, angle):
    """The rotation matrix (rows) about a unit axis by angle (radians)."""
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    one = 1.0 - c
    return ((c + x * x * one, x * y * one - z * s, x * z * one + y * s),
            (y * x * one + z * s, c + y * y * one, y * z * one - x * s),
            (z * x * one - y * s, z * y * one + x * s, c + z * z * one))


def apply(matrix, vector):
    return tuple(sum(row[k] * vector[k] for k in range(3)) for row in matrix)


def to_sensor(axes, pano, spin=False):
    """A vector in the panorama's axes as the sensor of a camera with these
    IMU_AXES reads it (spin: a rotation speed, which a mirror image turns round)."""
    sign = 1
    if spin:
        pick = [axis for axis, _ in axes]
        parity = 1
        for i in range(3):
            for j in range(i + 1, 3):
                if pick[i] > pick[j]:
                    parity = -parity
        sign = parity * axes[0][1] * axes[1][1] * axes[2][1]
    sensor = [0.0, 0.0, 0.0]
    for k, (axis, axis_sign) in enumerate(axes):
        sensor[axis] = sign * axis_sign * pano[k]
    return sensor


def camera_samples(axes, seconds, rate, up0, spin=(0.0, 0.0, 0.0), start=0.0, shake=0.0,
                   shake_hz=2.0):
    """Samples [(seconds, ax, ay, az, gx, gy, gz)] of a camera turning steadily.

    up0 is up in its panorama's axes at time 0; spin its rotation speed
    (radians per second, about the panorama's axes): up at t is up0 turned
    by -spin * t (the gyroscope's sense in insta360.UpFinder). shake adds an
    acceleration of that many g along the panorama's x axis, back and forth
    shake_hz times a second (a hand's jolts), which a level track must not
    follow. Readings are in the sensor's axes (axes: insta360.IMU_AXES).
    """
    samples = []
    speed = math.sqrt(sum(value * value for value in spin))
    unit = tuple(value / speed for value in spin) if speed else (0.0, 1.0, 0.0)
    count = int(round(seconds * rate))
    for n in range(count + 1):
        t = start + n / float(rate)
        up = apply(rodrigues(unit, -speed * t), up0) if speed else tuple(up0)
        acc = list(up)
        if shake:
            acc[0] += shake * math.sin(2.0 * math.pi * shake_hz * t)
        samples.append((t,) + tuple(to_sensor(axes, acc)) + tuple(to_sensor(axes, spin, True)))
    return samples


def rolled(axes, seconds, rate, roll_deg):
    """Samples [(seconds, ax, ay, az, gx, gy, gz)] of a camera rolled about its
    forward axis (z) by roll_deg(t) degrees at t seconds (towards +x, as tilt_up
    rolls; the rotation speed from the change of the roll)."""
    samples = []
    step = 1e-4
    for n in range(int(round(seconds * rate)) + 1):
        t = n / float(rate)
        roll = math.radians(roll_deg(t))
        speed = math.radians(roll_deg(t + step) - roll_deg(t - step)) / (2 * step)
        up = (math.sin(roll), math.cos(roll), 0.0)
        # (Up turned by -roll about z: a rotation speed of +speed about z, in
        # camera_samples' sense.)
        spin = (0.0, 0.0, speed)
        samples.append((t,) + tuple(to_sensor(axes, up)) + tuple(to_sensor(axes, spin, True)))
    return samples


def stamped(samples, unit, first_frame, sensor_offset_ms=0.0):
    """Samples with their times on a camera's clock: seconds -> units (1e-6 for
    microseconds, 1e-3 for milliseconds) after first_frame, as a sensor
    sensor_offset_ms behind would stamp them."""
    out = []
    for t, *values in samples:
        out.append((int(round((t + sensor_offset_ms / 1000.0) / unit)) + first_frame,)
                   + tuple(values))
    return out


def tilt_up(roll_deg=0.0, pitch_deg=0.0):
    """Up in a tilted camera's panorama axes: leaning roll_deg towards +x (the
    camera rolled about its forward axis), then turned pitch_deg about the x
    axis (the camera tipped forward or back)."""
    roll, pitch = math.radians(roll_deg), math.radians(pitch_deg)
    up = (math.sin(roll), math.cos(roll), 0.0)
    return apply(rodrigues((1.0, 0.0, 0.0), pitch), up)
