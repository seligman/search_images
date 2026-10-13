"""Insta360 trailers and level tracks (insta360.py): finding the records (a
table, or walking back from the footer), the camera's details, the motion
samples in either layout, which way is up over a video's time (a tilted
camera, a turning one, a shaking hand, one upside down, a timelapse, readings
that stop early), thinning the track out, using it, the photos levelled
(also with a stale first frame time) and the setting.

The trailers are made up (tests/insta360gen.py), shaped like real ones.
"""

import io
import json
import math
import os
import struct
import unittest

import support
import insta360gen as gen

import insta360

X4 = insta360.IMU_AXES["insta360x4"]
ONE_R = insta360.IMU_AXES["insta360oner"]
FIRST_US = 2000000              # the first frame on an X4 style sensor clock (us)
FIRST_MS = 1200000              # ... on a ONE R style one (ms)


def angle(a, b):
    """Degrees between two directions."""
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / norm))))


def x4_file(samples, first=FIRST_US, offset_ms=1.6, table=True, model="Insta360 X4",
            extra=()):
    """The bytes of an X4 style file's trailer (raw 20-byte samples, a table)."""
    stamped = gen.stamped(samples, 1e-6, first, offset_ms)
    records = [(1, 1, gen.details(model, first, offset_ms, (32, 2000))),
               (2, 0, b"\xff\xd8 not a thumbnail \xff\xd9"),
               (3, 0, gen.raw_motion(stamped, 32.0, 2000.0))] + list(extra)
    return b"\x00" * 100 + gen.trailer(records, table=table)


def one_r_file(samples, first=FIRST_MS, offset_ms=-6.0, model="Insta360 OneR"):
    """The bytes of a ONE R style file's trailer (56-byte samples, no table)."""
    stamped = gen.stamped(samples, 1e-3, first, offset_ms)
    records = [(10, 0, b"anchors"), (4, 0, b"\x00" * 32),
               (3, 0, gen.double_motion(stamped)),
               (1, 1, gen.details(model, first, offset_ms))]
    return b"\x00" * 64 + gen.trailer(records, table=False)


class TrailerTest(unittest.TestCase):

    def test_records_from_a_table(self):
        data = x4_file(gen.camera_samples(X4, 2.0, 100, (0.0, 1.0, 0.0)))
        handle = io.BytesIO(data)
        trailer = insta360.read_trailer(handle)
        self.assertEqual(trailer.version, 3)
        self.assertEqual(sorted(trailer.records), [1, 2, 3])
        offset, size, fmt = trailer.records[3]
        self.assertEqual(size, 201 * 20)
        self.assertEqual(fmt, 0)
        self.assertEqual(trailer.start, len(data) - trailer.size)
        handle.seek(offset)
        self.assertEqual(struct.unpack("<Q", handle.read(8))[0], FIRST_US + 1600)
        details = insta360.read_details(handle, trailer)
        self.assertEqual(details.model, "Insta360 X4")
        self.assertEqual(details.first_frame, FIRST_US)
        self.assertAlmostEqual(details.sensor_offset_ms, 1.6)
        self.assertEqual((details.acc_range, details.gyro_range), (32.0, 2000.0))

    def test_records_walked_back(self):
        data = one_r_file(gen.camera_samples(ONE_R, 2.0, 50, (0.0, 1.0, 0.0)))
        handle = io.BytesIO(data)
        trailer = insta360.read_trailer(handle)
        self.assertEqual(sorted(trailer.records), [1, 3, 4, 10])
        self.assertEqual(trailer.records[3][1], 101 * 56)
        details = insta360.read_details(handle, trailer)
        self.assertEqual((details.model, details.first_frame), ("Insta360 OneR", FIRST_MS))
        self.assertEqual((details.acc_range, details.gyro_range), (None, None))
        self.assertEqual(insta360.camera_model(data), "Insta360 OneR")

    def test_not_a_trailer(self):
        for data in (b"", b"short", b"\x00" * 500, b"\x00" * 500 + gen.MAGIC,
                     # A size larger than the file, or too small to hold anything.
                     bytes(32) + struct.pack("<II", 10 ** 6, 3) + gen.MAGIC,
                     bytes(32) + struct.pack("<II", 10, 3) + gen.MAGIC):
            self.assertIsNone(insta360.read_trailer(io.BytesIO(data)), data[-40:])
            self.assertEqual(insta360.camera_model(data), "")
            self.assertIsNone(insta360.level_track(data))
            self.assertIsNone(insta360.capture_up(data))
        self.assertEqual(insta360.camera_model(os.path.join(support.REPO, "no such file")), "")

    def test_records_that_do_not_fit_are_left_out(self):
        # A walk that runs off the start of the trailer stops; a table
        # entry pointing past the table is skipped.
        broken = gen.trailer([(3, 0, b"\x01" * 40)])
        broken = broken[:40] + struct.pack("<BBI", 0, 3, 10 ** 6) + broken[46:]
        trailer = insta360.read_trailer(io.BytesIO(broken))
        self.assertNotIn(3, trailer.records)
        good = gen.trailer([(1, 1, gen.details()), (3, 0, b"\x02" * 40)], table=True)
        at = good.index(struct.pack("<BBII", 3, 0, 40, len(gen.details())))
        bad = good[:at] + struct.pack("<BBII", 3, 0, 4000, 0) + good[at + 10:]
        trailer = insta360.read_trailer(io.BytesIO(bad))
        self.assertEqual(sorted(trailer.records), [1])

    def test_protobuf_reading_survives_damage(self):
        fields = insta360.protobuf_fields(gen.protobuf({2: "Insta360 X4", 24: 7, 28: -6.0,
                                                        65: {1: 16, 2: 1000}}))
        self.assertEqual(fields[2], b"Insta360 X4")
        self.assertEqual(fields[24], 7)
        self.assertEqual(fields[28], -6.0)
        self.assertEqual(insta360.protobuf_fields(fields[65]), {1: 16, 2: 1000})
        # Cut short, or a wire type it does not know: what came before.
        whole = gen.protobuf({2: "Insta360 X4", 24: 7})
        self.assertEqual(insta360.protobuf_fields(whole[:-1]), {2: b"Insta360 X4"})
        self.assertEqual(insta360.protobuf_fields(whole + b"\x0f"), {2: b"Insta360 X4", 24: 7})
        self.assertEqual(insta360.protobuf_fields(b"\xff" * 12), {})


class MotionTest(unittest.TestCase):

    def read(self, data):
        handle = io.BytesIO(data)
        trailer = insta360.read_trailer(handle)
        return insta360.read_motion(handle, trailer, insta360.read_details(handle, trailer))

    def test_raw_samples_on_the_video_clock(self):
        samples = gen.camera_samples(X4, 1.0, 1000, (0.6, 0.8, 0.0), spin=(0.0, 0.5, 0.0),
                                     start=-0.2)
        times, readings = self.read(x4_file(samples))
        # Binned by 10 ms, starting 0.2 s before the first frame (the sensor
        # offset taken out), in g and radians per second.
        self.assertAlmostEqual(times[0], -0.195, places=6)
        self.assertAlmostEqual(times[-1] - times[0], 1.0, places=6)
        self.assertEqual(len(times), 101)
        # (camera_samples gives the sensor's readings.)
        first = [column[0] for column in readings]
        for got, want in zip(first, samples[0][1:]):
            self.assertAlmostEqual(got, want, delta=0.005)
        # (An X4's up is its sensor's -x; its rotation speeds turn round: the
        # panorama's axes are a mirror image of the sensor's.)
        self.assertEqual(gen.to_sensor(X4, (0.0, 0.5, 0.0)), [-0.5, 0.0, 0.0])
        self.assertEqual(gen.to_sensor(X4, (0.0, 0.5, 0.0), True), [0.5, 0.0, 0.0])

    def test_double_samples(self):
        samples = gen.camera_samples(ONE_R, 2.0, 500, (0.0, 1.0, 0.0), spin=(0.3, 0.0, 0.0))
        times, readings = self.read(one_r_file(samples))
        self.assertAlmostEqual(times[0], 0.005, places=6)
        self.assertAlmostEqual(readings[3][0], gen.to_sensor(ONE_R, (0.3, 0, 0), True)[0])

    def test_a_record_both_layouts_fit(self):
        # 280 raw samples are 5600 bytes, also 100 samples of 56: the times
        # and readings that make sense decide.
        samples = gen.camera_samples(X4, 0.279, 1000, (0.0, 1.0, 0.0))
        self.assertEqual(len(samples) * 20 % 56, 0)
        times, readings = self.read(x4_file(samples))
        self.assertEqual(len(times), 28)
        self.assertAlmostEqual(readings[0][0], -1.0, delta=0.002)     # (X4: up is -x)

    def test_bad_samples_are_dropped(self):
        samples = gen.camera_samples(X4, 1.0, 1000, (0.0, 1.0, 0.0))
        stamped = gen.stamped(samples, 1e-6, FIRST_US, 1.6)
        # A time going back, one far ahead, and a reading no camera feels.
        stamped[50] = (stamped[10][0],) + stamped[50][1:]
        stamped[55] = (stamped[55][0] + 10 ** 12,) + stamped[55][1:]
        stamped[60] = (stamped[60][0], 30.0) + stamped[60][2:]
        records = [(1, 1, gen.details("Insta360 X4", FIRST_US, 1.6, (32, 2000))),
                   (3, 0, gen.raw_motion(stamped, 32.0, 2000.0))]
        handle = io.BytesIO(gen.trailer(records, table=True))
        trailer = insta360.read_trailer(handle)
        times, readings = insta360.read_motion(handle, trailer,
                                               insta360.read_details(handle, trailer))
        self.assertEqual(len(times), 101)
        self.assertTrue(all(abs(value + 1.0) < 0.01 for value in readings[0]))
        self.assertTrue(all(abs(value) < 0.01 for value in readings[1]))

    def test_no_motion(self):
        # No first frame time, no samples, or samples that make no sense.
        samples = gen.stamped(gen.camera_samples(X4, 1.0, 100, (0.0, 1.0, 0.0)), 1e-6, 0)
        for records in ([(1, 1, gen.details("Insta360 X4", None, None, (32, 2000))),
                         (3, 0, gen.raw_motion(samples))],
                        [(1, 1, gen.details("Insta360 X4", FIRST_US))],
                        [(1, 1, gen.details("Insta360 X4", FIRST_US)), (3, 0, b"\xff" * 400)]):
            self.assertIsNone(self.read(gen.trailer(records)), records[-1][0])


class LevelTrackTest(unittest.TestCase):

    def test_a_tilted_camera(self):
        up = gen.tilt_up(roll_deg=25, pitch_deg=-40)
        track = insta360.level_track(x4_file(gen.camera_samples(X4, 10.0, 1000, up)),
                                     duration=10.0)
        # Nothing changes: the first and the last sample are enough.
        self.assertEqual(len(track), 2)
        self.assertEqual([track[0][0], track[-1][0]], [0.0, 10.0])
        for sample in track:
            self.assertLess(angle(sample[1:], up), 0.05)
        # (The raw readings hold about 0.06 degrees.)
        self.assertAlmostEqual(insta360.tilt_degrees(track[0][1:]),
                               angle(up, (0, 1, 0)), delta=0.1)

    def test_the_same_from_either_camera(self):
        up = gen.tilt_up(roll_deg=-15, pitch_deg=30)
        x4 = insta360.level_track(x4_file(gen.camera_samples(X4, 4.0, 1000, up)))
        one_r = insta360.level_track(one_r_file(gen.camera_samples(ONE_R, 4.0, 500, up)))
        self.assertLess(angle(x4[0][1:], one_r[0][1:]), 0.05)
        self.assertLess(angle(x4[0][1:], up), 0.05)

    def test_a_turning_camera_with_a_shaking_hand(self):
        # Rolling at 20 degrees a second for 6 s while the hand jolts the
        # camera sideways (0.4 g, twice a second): the track follows the
        # roll from the gyroscope, not the jolts.
        spin = (0.0, 0.0, math.radians(20))
        samples = gen.camera_samples(X4, 6.0, 1000, (0.0, 1.0, 0.0), spin=spin, shake=0.4)
        track = insta360.level_track(x4_file(samples), duration=6.0)
        self.assertGreater(len(track), 5)
        for t in (0.5, 1.7, 3.0, 4.25, 5.5):
            truth = gen.apply(gen.rodrigues((0, 0, 1), -spin[2] * t), (0.0, 1.0, 0.0))
            self.assertLess(angle(insta360.up_at(track, t), truth), 1.0, t)

    def test_times_follow_the_first_frame_and_the_sensor_offset(self):
        # Upright for 3 s, then tipped forward by 30 degrees: the change
        # shows at 3 s of the video whatever the clocks.
        def jump(first, offset_ms, unit, axes, layout):
            samples = (gen.camera_samples(axes, 2.99, 1000, (0.0, 1.0, 0.0), start=0.0)
                       + gen.camera_samples(axes, 3.0, 1000, gen.tilt_up(pitch_deg=30),
                                            start=3.0))
            stamped = gen.stamped(samples, unit, first, offset_ms)
            model = "Insta360 X4" if axes is X4 else "Insta360 OneR"
            ranges = (32, 2000) if layout is gen.raw_motion else None
            records = [(1, 1, gen.details(model, first, offset_ms, ranges)),
                       (3, 0, layout(stamped))]
            return insta360.level_track(gen.trailer(records, table=axes is X4), duration=6.0)
        for track in (jump(FIRST_US, 30.0, 1e-6, X4, gen.raw_motion),
                      jump(FIRST_MS, -6.0, 1e-3, ONE_R, gen.double_motion)):
            # (The window of +-2 s spreads the step from 1 s to 5 s.)
            self.assertLess(insta360.tilt_degrees(insta360.up_at(track, 0.5)), 0.5)
            self.assertAlmostEqual(insta360.tilt_degrees(insta360.up_at(track, 3.0)), 15,
                                   delta=1.5)
            self.assertGreater(insta360.tilt_degrees(insta360.up_at(track, 5.5)), 29.5)

    def test_cut_to_the_video_and_enough_of_it(self):
        data = x4_file(gen.camera_samples(X4, 8.0, 200, (0.0, 1.0, 0.0), start=-0.5))
        track = insta360.level_track(data, duration=5.0)
        self.assertEqual([track[0][0], track[-1][0]], [0.0, 5.0])
        # (To the middle of the last 10 ms the readings fill.)
        self.assertEqual(insta360.level_track(data)[-1][0], 7.505)
        # Samples for less than half of the video: no track.
        self.assertIsNone(insta360.level_track(data, duration=20.0))

    def test_only_cameras_whose_axes_are_known(self):
        data = x4_file(gen.camera_samples(X4, 2.0, 100, (0.0, 1.0, 0.0)),
                       model="Insta360 Something")
        self.assertIsNone(insta360.level_track(data))
        self.assertIsNotNone(insta360.level_track(data, model="Insta360 X4"))
        self.assertIsNotNone(insta360.level_track(data, model="insta360  x4"))

    def test_from_a_path_and_a_handle(self):
        data = x4_file(gen.camera_samples(X4, 2.0, 100, gen.tilt_up(roll_deg=10)))
        with support.TempBase() as base:
            path = base.write_file("VID_20190514_142301_00_007.insv", data)
            from_path = insta360.level_track(path)
            with open(path, "rb") as handle:
                self.assertEqual(insta360.level_track(handle), from_path)
        self.assertEqual(insta360.level_track(data), from_path)
        self.assertEqual(json.loads(json.dumps(from_path)), from_path)


class UpsideDownTrackTest(unittest.TestCase):

    def test_a_camera_rolling_over(self):
        # Rolling at 20 degrees a second from upright to 230 degrees: upside
        # down (marked) from 150 degrees on, still at 130 on the way back.
        samples = gen.rolled(X4, 11.5, 1000, lambda t: 20.0 * t)
        track = insta360.level_track(x4_file(samples), duration=11.5)
        self.assertIs(insta360.checked_track(track), track)
        flags = [len(sample) > 4 for sample in track]
        switches = [k for k in range(1, len(flags)) if flags[k] != flags[k - 1]]
        self.assertEqual(len(switches), 1)
        self.assertAlmostEqual(track[switches[0]][0], 7.5, delta=0.1)
        self.assertEqual([insta360.inverted_at(track, t) for t in (1.0, 7.3, 7.7, 11.4)],
                         [False, False, True, True])
        for t in (2.0, 7.0, 9.0, 11.0):
            truth = gen.tilt_up(roll_deg=20.0 * t)
            self.assertLess(angle(insta360.up_at(track, t), truth), 1.0, t)

    def test_a_camera_that_wobbles_upside_down_stays_so(self):
        # Hanging at 140 degrees, wobbling 12 degrees either way: once upside
        # down it stays so (a turn that switched at 135 degrees would swing
        # the view round twice a second).
        samples = gen.rolled(X4, 6.0, 1000, lambda t: 140.0 + 12.0 * math.sin(2 * math.pi * t))
        track = insta360.level_track(x4_file(samples), duration=6.0)
        tilts = [insta360.tilt_degrees(sample[1:4]) for sample in track]
        self.assertLess(min(tilts), 130)
        self.assertGreater(max(tilts), 150)
        self.assertTrue(all(len(sample) == 5 and sample[4] == 1 for sample in track))


class TimelapseTest(unittest.TestCase):
    """A TimeShift: 4 s of video (120 frames, 30 a second) of a minute of rolling."""

    def make(self, frames=True, first=FIRST_US):
        self.roll = lambda seconds: -20.0 + 1.0 * seconds
        samples = gen.rolled(X4, 61.0, 200, self.roll)
        # (The readings start 0.3 s before the first frame.)
        samples = [(t - 0.3,) + tuple(values) for t, *values in samples]
        # Frame k was taken at 0.5 k seconds.
        extra = [(6, 0, gen.frame_times(first + 500000 * k for k in range(120)))] if frames else []
        return x4_file(samples, extra=extra)

    def test_each_frame_is_levelled_for_when_it_was_taken(self):
        data = self.make()
        handle = io.BytesIO(data)
        trailer = insta360.read_trailer(handle)
        times = insta360.read_frame_times(handle, trailer, insta360.read_details(handle, trailer))
        self.assertEqual((len(times), times[0], times[-1]), (120, 0.0, 59.5))
        track = insta360.level_track(data, duration=4.0, fps=30.0)
        self.assertEqual(track[-1][0], 3.967)
        for k in (0, 17, 60, 90, 119):
            truth = gen.tilt_up(roll_deg=self.roll(0.5 * k + 0.3))
            self.assertLess(angle(insta360.up_at(track, k / 30.0), truth), 0.6, k)
        # (Not the moment at the same time of the recording: 3 s of video is
        # 45 s of it.)
        self.assertGreater(angle(insta360.up_at(track, 3.0),
                                 gen.tilt_up(roll_deg=self.roll(3.0 + 0.3))), 40)
        # The frame rate defaults to the frames over the length.
        self.assertEqual(insta360.level_track(data, duration=4.0), track)

    def test_without_frame_times_a_timelapse_has_no_track(self):
        # A minute of readings for a 4 s video, and no frame times (or ones
        # that do not start at the first frame): no telling which is which.
        self.assertIsNone(insta360.level_track(self.make(frames=False), duration=4.0))
        self.assertIsNone(insta360.level_track(self.make(first=FIRST_US + 5 * 10 ** 6),
                                               duration=4.0))
        # (A video that lasts as long as its readings does.)
        self.assertIsNotNone(insta360.level_track(self.make(frames=False), duration=60.0))


class TrackEndsTest(unittest.TestCase):

    def test_readings_that_stop_early_or_start_late(self):
        # Readings for 6 s of a 10 s video: after them the turn fades out
        # (within LEVEL_WINDOW) rather than holding a tilt that may be wrong.
        up = gen.tilt_up(roll_deg=40)
        track = insta360.level_track(x4_file(gen.camera_samples(X4, 6.0, 500, up, start=-0.2)),
                                     duration=10.0)
        self.assertLess(angle(insta360.up_at(track, 5.0), up), 0.1)
        self.assertAlmostEqual(track[-1][0], 5.795 + insta360.LEVEL_WINDOW, delta=0.01)
        self.assertEqual(insta360.up_at(track, 9.0), (0.0, 1.0, 0.0))
        self.assertLess(insta360.tilt_degrees(insta360.up_at(track, 6.8)), 40)
        # Readings from 3 s on.
        track = insta360.level_track(x4_file(gen.camera_samples(X4, 6.0, 500, up, start=3.0)),
                                     duration=9.0)
        self.assertEqual(insta360.up_at(track, 0.5), (0.0, 1.0, 0.0))
        self.assertLess(angle(insta360.up_at(track, 4.0), up), 0.1)
        # Readings to the end (a few milliseconds short is the end).
        track = insta360.level_track(x4_file(gen.camera_samples(X4, 9.95, 500, up)),
                                     duration=10.0)
        self.assertLess(angle(insta360.up_at(track, 10.0), up), 0.1)


class SimplifyTest(unittest.TestCase):

    def test_straight_runs_and_the_tolerance(self):
        def unit(degrees):
            return (math.sin(math.radians(degrees)), math.cos(math.radians(degrees)), 0.0)
        steady = [(n / 30.0, unit(0.0)) for n in range(300)]
        self.assertEqual(insta360.simplify(steady), [steady[0], steady[-1]])
        # A roll that goes out and back: the turning points stay, and every
        # sample is within the tolerance of the lines between kept ones.
        wave = [(n / 30.0, unit(20.0 * math.sin(n / 30.0))) for n in range(400)]
        kept = insta360.simplify(wave, 0.5)
        self.assertLess(len(kept), len(wave) / 4)
        self.assertEqual((kept[0], kept[-1]), (wave[0], wave[-1]))
        track = [[t] + list(up) for t, up in kept]
        for t, up in wave:
            self.assertLess(angle(insta360.up_at(track, t), up), 0.5 + 1e-6)
        finer = insta360.simplify(wave, 0.1)
        self.assertGreater(len(finer), len(kept))
        self.assertEqual(insta360.simplify(wave[:2]), wave[:2])


class UsingTest(unittest.TestCase):

    def test_up_between_samples(self):
        track = [[1.0, 0.0, 1.0, 0.0], [3.0, 1.0, 0.0, 0.0]]
        self.assertEqual(insta360.up_at(track, 0.0), (0.0, 1.0, 0.0))
        self.assertEqual(insta360.up_at(track, 5.0), (1.0, 0.0, 0.0))
        middle = insta360.up_at(track, 2.0)
        self.assertAlmostEqual(middle[0], math.sqrt(0.5))
        self.assertAlmostEqual(middle[1], math.sqrt(0.5))
        self.assertEqual(insta360.up_at(None, 1.0), (0.0, 1.0, 0.0))

    def test_the_rotation(self):
        for up in ((0.0, 1.0, 0.0), gen.tilt_up(30, -20), gen.tilt_up(-89, 10),
                   (1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, -1.0, 0.0), (0.1, -0.9, 0.2)):
            norm = math.sqrt(sum(v * v for v in up))
            up = tuple(v / norm for v in up)
            for inverted in (None, False, True):
                rows = insta360.up_rotation(up, inverted)
                # It takes straight up to up ...
                got = insta360.turn(rows, (0.0, 1.0, 0.0))
                self.assertLess(angle(got, up), 1e-6, (up, inverted))
                # ... and is a rotation (orthonormal, no mirror).
                for i in range(3):
                    for j in range(3):
                        dot = sum(rows[i][k] * rows[j][k] for k in range(3))
                        self.assertAlmostEqual(dot, 1.0 if i == j else 0.0, places=9)
                det = (rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
                       - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
                       + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0]))
                self.assertAlmostEqual(det, 1.0, places=9)
            # Upright or tilted, the smallest turn: what is level and square to
            # the tilt stays (by default up to INVERTED_DEG).
            rows = insta360.up_rotation(up, False)
            side = (up[2], 0.0, -up[0])
            if math.hypot(side[0], side[2]) > 1e-6:
                self.assertLess(angle(insta360.turn(rows, side), side), 1e-6)
            if insta360.tilt_degrees(up) <= insta360.INVERTED_DEG:
                self.assertEqual(insta360.up_rotation(up), rows)

    def test_a_camera_upside_down_keeps_its_heading(self):
        # Hanging upside down and leaning a little, any way: the middle of the
        # level view is what the camera's front saw (no further from it than
        # the lean), and it does not swing round as the lean goes round.
        front = (0.0, 0.0, 1.0)
        for tilt in (150.0, 165.0, 175.0, 178.0, 179.9):
            previous = None
            for lean in range(0, 365, 5):
                t, b = math.radians(tilt), math.radians(lean)
                up = (math.sin(t) * math.cos(b), math.cos(t), math.sin(t) * math.sin(b))
                rows = insta360.up_rotation(up)
                self.assertLess(angle(insta360.turn(rows, (0.0, 1.0, 0.0)), up), 1e-6)
                ahead = insta360.turn(rows, front)
                self.assertLessEqual(angle(ahead, front), 180.0 - tilt + 1e-6, (tilt, lean))
                if previous is not None:
                    self.assertLess(angle(ahead, previous), 3.0, (tilt, lean))
                previous = ahead
        # (The smallest turn swings round by half a turn as the lean goes
        # round a quarter turn.)
        t = math.radians(175.0)
        sideways = insta360.up_rotation((math.sin(t), math.cos(t), 0.0), False)
        forward = insta360.up_rotation((0.0, math.cos(t), math.sin(t)), False)
        self.assertGreater(angle(insta360.turn(sideways, front), insta360.turn(forward, front)),
                           170.0)
        # Exactly upside down: half a turn about the forward axis.
        self.assertEqual(insta360.up_rotation((0.0, -1.0, 0.0)),
                         ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)))
        self.assertEqual(insta360.up_rotation((0.0, -1.0, 0.0), False),
                         ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)))

    def test_checked_tracks(self):
        good = [[0.0, 0.0, 1.0, 0.0], [1.5, 0.1, 0.995, 0.0]]
        self.assertEqual(insta360.checked_track(good), good)
        marked = [[0.0, 0.0, -1.0, 0.0, 1], [1.5, 0.1, -0.995, 0.0, 1], [2.0, 0, 1, 0]]
        self.assertEqual(insta360.checked_track(marked), marked)
        for bad in (None, [], "x", [[0.0, 0.0, 1.0]], [[0, 0, 1, 0], [0, 0, 1, 0]],
                    [[0.0, 0.0, 5.0, 0.0]], [[0.0, float("nan"), 1.0, 0.0]],
                    [[0.0, True, 1.0, 0.0]], [[1.0, 0, 1, 0], [0.5, 0, 1, 0]],
                    [[0.0, 0, 1, 0, 2]], [[0.0, 0, 1, 0, True]], [[0.0, 0, 1, 0, 1, 1]]):
            self.assertIsNone(insta360.checked_track(bad), bad)

    def test_upside_down_between_samples(self):
        track = [[1.0, 0, 1, 0], [2.0, 0, -1, 0, 1], [3.0, 0.1, -0.99, 0, 1], [4.0, 0, 1, 0]]
        self.assertEqual([insta360.inverted_at(track, t) for t in (0, 1.5, 2, 3.9, 4, 9)],
                         [False, False, True, True, False, False])
        self.assertTrue(insta360.inverted_at([[0.0, 0, -1, 0, 1]], -1.0))
        self.assertFalse(insta360.inverted_at(None, 1.0))
        # (The mark is no part of up.)
        self.assertEqual(insta360.up_at(track, 2.0), (0.0, -1.0, 0.0))


class PhotoTest(unittest.TestCase):

    def test_up_when_a_photo_was_taken(self):
        up = gen.tilt_up(roll_deg=7, pitch_deg=3)
        samples = gen.camera_samples(ONE_R, 2.0, 500, up, start=-1.2)
        found = insta360.capture_up(one_r_file(samples))
        self.assertLess(angle(found, up), 0.05)

    def test_only_cameras_whose_photos_are_levelled(self):
        # An X4 stitches (and levels) its own 360 photos.
        samples = gen.camera_samples(X4, 2.0, 1000, gen.tilt_up(roll_deg=7), start=-0.6)
        self.assertIsNone(insta360.capture_up(x4_file(samples)))

    def test_a_stale_first_frame_time(self):
        # Photos taken in quick succession: the trailer names the first frame
        # of the photo before (65 s earlier). The samples are this photo's:
        # turning until 0.75 s before they end, when it was taken, then still.
        up = gen.tilt_up(roll_deg=60, pitch_deg=5)
        spin = (0.0, 0.0, math.radians(30))
        taken = 66.0
        up0 = gen.apply(gen.rodrigues((0.0, 0.0, 1.0), spin[2] * taken), up)
        samples = (gen.camera_samples(ONE_R, 0.6, 500, up0, spin=spin, start=taken - 0.6)[:-1]
                   + gen.camera_samples(ONE_R, insta360.PHOTO_AFTER, 500, up, start=taken))
        found = insta360.capture_up(one_r_file(samples))
        self.assertLess(angle(found, up), 0.2)
        # (Where the samples start, it leaned 18 degrees less.)
        self.assertGreater(angle(samples[0][1:4], gen.to_sensor(ONE_R, up)), 15)


class SettingTest(support.TempBaseMixin, unittest.TestCase):

    def test_on_unless_turned_off(self):
        self.assertTrue(insta360.level_option({}))
        self.assertTrue(insta360.level_option(None))
        self.assertFalse(insta360.level_option({"level_horizon": False}))
        for value in (0, "false", None, [], 1):
            self.assertTrue(insta360.level_option({"level_horizon": value}), value)
        self.assertTrue(insta360.level_enabled())
        self.base.write_config([], level_horizon=False)
        self.assertFalse(insta360.level_enabled())
        self.base.write_config([], level_horizon=True, nadir_fill_deg=3)
        self.assertTrue(insta360.level_enabled())
        self.base.write_file("config.json", "{not json")
        self.assertTrue(insta360.level_enabled())


if __name__ == "__main__":
    unittest.main()
