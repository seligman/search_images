"""Drives: runs of video clips recorded one after the other (drives.py).

Finding them (names, times, counters in the names), their search records,
their descriptions (data/drives.json) and the describe step, the sped-up
previews and the server's routes. ffmpeg and ffprobe are played by
tests/fake_ffmpeg.py, the AI model by tests/fake_llm.py; everything runs in
a temporary data root. Every name, time and description is made up.
"""

import datetime
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

import support

import fake_ffmpeg
import videotools

import common
import db
import drives
import jobs
import prompts
import search_index

quiet = support.quiet
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}


def clip(image_id, name, start, duration=60.0, folder="", width=2560, height=1440,
         codec="hevc", content_hash=None, hidden=False):
    """A Clip that starts start seconds after an arbitrary moment."""
    pattern, numbers = drives.name_pattern(name)
    return drives.Clip(image_id, (folder + "/" if folder else "") + name, name, folder, pattern,
                       numbers, 1000000.0 + start, duration, width, height, codec,
                       content_hash if content_hash is not None else "h%d" % image_id, hidden,
                       "")


def dashcam(first_id, first_number, starts, durations=None, folder=""):
    """Clips named as a dashcam names them (YYYYMMDD_HHMMSS_NNNN_N_A.MP4)."""
    found = []
    for offset, start in enumerate(starts):
        moment = datetime.datetime(2023, 8, 17, 14, 0, 0) + datetime.timedelta(seconds=start)
        name = "%s_%04d_N_A.MP4" % (moment.strftime("%Y%m%d_%H%M%S"), first_number + offset)
        duration = durations[offset] if durations else 60.0
        found.append(clip(first_id + offset, name, start, duration, folder))
    return found


class FindingTest(unittest.TestCase):

    def test_name_patterns(self):
        self.assertEqual(drives.name_pattern("20230817_142005_0412_N_A.MP4"),
                         ("#_#_#_n_a.mp4", [20230817, 142005, 412]))
        self.assertEqual(drives.name_pattern("GX010123.MP4"), ("gx#.mp4", [10123]))
        self.assertEqual(drives.name_pattern("clip.mp4"), ("clip.mp4", []))
        self.assertEqual(drives.name_pattern(""), ("", []))

    def test_clips_one_after_another_make_a_drive(self):
        # Four minutes, the last clip cut short where the engine stopped;
        # an hour later three more.
        clips = dashcam(1, 101, [0, 60, 120, 180], [60, 60, 60, 25.0])
        clips += dashcam(5, 105, [3600, 3660, 3720])
        found = drives.detect(clips)
        self.assertEqual([drive.ids for drive in found], [[1, 2, 3, 4], [5, 6, 7]])
        first = found[0]
        self.assertEqual(first.id, "h1")
        self.assertEqual(first.key, 1)
        self.assertAlmostEqual(first.length, 205.0)
        self.assertEqual(first.holes, [0.0, 0.0, 0.0, 0.0])
        self.assertNotEqual(found[0].clips_key, found[1].clips_key)

    def test_a_camera_that_dates_clips_by_their_end(self):
        # The last clip's date is its end: only 25 s after the one before.
        clips = dashcam(1, 101, [60, 120, 180, 205.0], [60, 60, 60, 25.0])
        self.assertEqual([drive.ids for drive in drives.detect(clips)], [[1, 2, 3, 4]])

    def test_slack_and_the_smallest_drive(self):
        clips = dashcam(1, 1, [0, 60, 135])          # 15 s after the one before ended
        self.assertEqual(len(drives.detect(clips)), 1)
        self.assertEqual(drives.detect(clips, slack=10), [])
        # Two clips are no drive, unless min_clips says so.
        self.assertEqual(drives.detect(clips[:2]), [])
        self.assertEqual(len(drives.detect(clips[:2], min_clips=2)), 1)

    def test_a_counter_in_the_names_bridges_missing_clips(self):
        # 104 and 105 are missing (deleted, or kept elsewhere): the drive
        # goes on with a hole of two minutes.
        clips = dashcam(1, 101, [0, 60, 120]) + dashcam(4, 106, [300, 360, 420])
        found = drives.detect(clips)
        self.assertEqual([drive.ids for drive in found], [[1, 2, 3, 4, 5, 6]])
        self.assertEqual(found[0].holes, [0.0, 0.0, 0.0, 120.0, 0.0, 0.0])
        # The camera was off for longer than the missing clips take: two drives.
        clips = dashcam(1, 101, [0, 60, 120]) + dashcam(4, 106, [900, 960, 1020])
        self.assertEqual(len(drives.detect(clips)), 2)
        # More missing clips than MAX_SKIPPED: two drives.
        clips = dashcam(1, 101, [0, 60, 120]) + dashcam(4, 110, [660, 720, 780])
        self.assertEqual(len(drives.detect(clips)), 2)
        # A counter that went up by one: the camera stopped, two drives.
        clips = dashcam(1, 101, [0, 60, 120]) + dashcam(4, 104, [240, 300, 360])
        self.assertEqual(len(drives.detect(clips)), 2)

    def test_names_without_a_counter_need_the_times(self):
        clips = [clip(index + 1, "VID_%d.mp4" % index, start)
                 for index, start in enumerate((0, 60, 120, 240, 300, 360))]
        # (The names count, but the time between 3 and 4 is a minute too long.)
        self.assertEqual([drive.ids for drive in drives.detect(clips)],
                         [[1, 2, 3], [4, 5, 6]])
        # Names that hold the time only (no counter: it goes up by 100 a minute).
        timed = [clip(index + 1, "VID_20230817_14%02d00.mp4" % (start // 60), start)
                 for index, start in enumerate((0, 60, 120, 240, 300, 360))]
        self.assertEqual([drive.ids for drive in drives.detect(timed)], [[1, 2, 3], [4, 5, 6]])

    def test_clips_stopped_by_hand_make_no_drive(self):
        # A phone's videos of one evening, one after another: their lengths vary.
        clips = []
        start = 0.0
        for index, length in enumerate((20.0, 47.0, 15.0, 28.0, 35.0, 41.0)):
            clips.append(clip(index + 1, "IMG_%04d.MOV" % (100 + index), start, length))
            start += length + 8
        self.assertEqual(drives.detect(clips), [])
        # Short clips, about as long as each other (a camera's few seconds each): neither.
        short = [clip(index + 1, "MVI_%04d.MOV" % index, index * 22, 9.5) for index in range(5)]
        self.assertEqual(drives.detect(short), [])
        self.assertIsNone(drives.cut_length(short))

    def test_a_clip_cut_short_ends_the_drive(self):
        # The third clip is half as long: the recording stopped there.
        clips = dashcam(1, 1, [0, 60, 120, 150, 210, 270], [60, 60, 30, 60, 60, 60])
        self.assertEqual([drive.ids for drive in drives.detect(clips)], [[1, 2, 3], [4, 5, 6]])

    def test_the_long_segments_of_one_recording(self):
        # A camera that cuts a recording every nine minutes or so.
        clips = []
        start = 0.0
        for index, length in enumerate((560.0, 552.0, 545.0, 566.0, 540.0, 300.0)):
            clips.append(clip(index + 1, "part_%02d.mp4" % index, start, length))
            start += length
        found = drives.detect(clips)
        self.assertEqual([len(drive.clips) for drive in found], [6])
        self.assertAlmostEqual(drives.cut_length(clips), 552.0)

    def test_other_folders_cameras_sizes_and_codecs_are_other_drives(self):
        clips = dashcam(1, 1, [0, 60, 120])
        rear = [clip(10 + index, item.name.replace("_A.MP4", "_B.MP4"), start)
                for index, (item, start) in enumerate(zip(clips, (0, 60, 120)))]
        moved = dashcam(20, 1, [0, 60, 120], folder="copy")
        small = [item._replace(id=30 + index, width=1920, height=1080)
                 for index, item in enumerate(dashcam(30, 1, [0, 60, 120]))]
        other = [item._replace(id=40 + index, codec="h264")
                 for index, item in enumerate(dashcam(40, 1, [0, 60, 120]))]
        found = drives.detect(clips + rear + moved + small + other)
        self.assertEqual(sorted(drive.ids for drive in found),
                         [[1, 2, 3], [10, 11, 12], [20, 21, 22], [30, 31, 32], [40, 41, 42]])

    def test_two_cameras_recording_at_once_are_not_one_drive(self):
        # Same names, same size: their clips overlap in time.
        first = [clip(index + 1, "CAM_%04d.MP4" % (index + 1), index * 60)
                 for index in range(3)]
        second = [clip(index + 11, "CAM_%04d.MP4" % (index + 501), index * 60 + 5)
                  for index in range(3)]
        found = drives.detect(first + second)
        self.assertEqual(sorted(drive.ids for drive in found), [[1, 2, 3], [11, 12, 13]])

    def test_each_recording_is_judged_by_its_own_clips(self):
        # One folder, one camera: its clip length was changed from one to
        # three minutes. Every drive is found, at either length.
        clips = []
        number, start = 100, 0.0
        for count, length in ((10, 60.0), (10, 60.0), (6, 180.0), (6, 180.0), (6, 180.0)):
            lengths = [length] * (count - 1) + [25.0]
            clips += dashcam(len(clips) + 1, number, [start + sum(lengths[:k])
                                                      for k in range(count)], lengths)
            number += count
            start += 7200
        found = drives.detect(clips)
        self.assertEqual([len(drive.clips) for drive in found], [10, 10, 6, 6, 6])
        # An action camera: single recordings stopped by hand, and one long
        # recording it split into chapters of the same length.
        clips, start = [], 0.0
        for index, length in enumerate((35, 48, 62, 75, 90, 120, 140, 200, 260, 310)):
            clips.append(clip(index + 1, "DJI_%04d.MP4" % (index + 1), start, length))
            start += length + 3600
        for index, length in enumerate((1063, 1063, 1063, 400)):
            clips.append(clip(index + 11, "DJI_%04d.MP4" % (index + 11), start, length))
            start += length
        self.assertEqual([drive.ids for drive in drives.detect(clips)], [[11, 12, 13, 14]])

    def test_a_last_clip_of_a_few_seconds_dated_by_its_end(self):
        # The engine stopped 0.8 s into the last clip; the camera dates a clip
        # by its end, to the second: it is hardly after the one before.
        clips = dashcam(1, 101, [60, 120, 180, 180], [60, 60, 60, 0.8])
        self.assertEqual([drive.ids for drive in drives.detect(clips)], [[1, 2, 3, 4]])
        # One of 5 s dated 2 s early: the counter says it is the next one.
        clips = dashcam(1, 101, [60, 120, 180, 182.5], [60, 60, 60, 5.0])
        self.assertEqual([drive.ids for drive in drives.detect(clips)], [[1, 2, 3, 4]])
        plain = [item._replace(name="clip.mp4", pattern="clip.mp4", numbers=[])
                 for item in clips]
        self.assertEqual([drive.ids for drive in drives.detect(plain)], [[1, 2, 3]])

    def test_two_cameras_of_one_kind_started_apart(self):
        # Two dashcams of the same kind, copied into one folder, the second
        # started 35 s (45 s) after the first: their counters keep them apart.
        for offset in (10, 35, 45):
            first = [clip(index + 1, "VID_%04d.MP4" % (100 + index), index * 60)
                     for index in range(6)]
            second = [clip(index + 11, "VID_%04d.MP4" % (500 + index), offset + index * 60)
                      for index in range(6)]
            self.assertEqual(sorted(drive.ids for drive in drives.detect(first + second)),
                             [[1, 2, 3, 4, 5, 6], [11, 12, 13, 14, 15, 16]], offset)

    def test_videos_recorded_over_each_other_are_no_drive(self):
        # Videos of about the same length, each starting before the one
        # before it ended (several people's, or copies dated alike).
        clips = [clip(1, "MVI_0101.MOV", 0, 36.0), clip(2, "MVI_0240.MOV", 20, 34.0),
                 clip(3, "MVI_0333.MOV", 40, 12.0), clip(4, "MVI_0241.MOV", 20.5, 33.0),
                 clip(5, "MVI_0336.MOV", 40.5, 35.0), clip(6, "MVI_0405.MOV", 60, 300.0)]
        self.assertEqual(drives.detect(clips), [])

    def test_a_counter_that_wraps_goes_on(self):
        clips = dashcam(1, 9998, [0, 60]) + [
            clip(3, "20230817_140200_0000_N_A.MP4", 120), clip(4, "20230817_140300_0001_N_A.MP4",
                                                             180)]
        self.assertEqual([drive.ids for drive in drives.detect(clips)], [[1, 2, 3, 4]])

    def test_clips_without_a_time_or_length_are_left_out(self):
        rows = [{"id": 1, "path": "a/1.mp4", "name": "1.mp4", "taken_at": "2023-08-17T14:00:00",
                 "duration": 60.0, "width": 10, "height": 10, "content_hash": "x",
                 "hidden": 0, "exif": json.dumps({"video": {"codec": "hevc"}})},
                dict(id=2, path="a/2.mp4", name="2.mp4", taken_at=None, duration=60.0,
                     width=10, height=10, content_hash="y", hidden=0, exif="{}"),
                dict(id=3, path="a/3.mp4", name="3.mp4", taken_at="2023-08-17T14:01:00",
                     duration=None, width=10, height=10, content_hash="z", hidden=1, exif="")]

        class Row(dict):
            def keys(self):
                return list(dict.keys(self))
        made = [drives.clip_from_row(Row(row)) for row in rows]
        # A 360 video plays on its own: never in a drive.
        self.assertIsNone(drives.clip_from_row(Row(rows[0], projection="dual_fisheye")))
        self.assertIsNotNone(drives.clip_from_row(Row(rows[0], projection="")))
        self.assertEqual(made[0].codec, "hevc")
        self.assertEqual(made[0].folder, "a")
        self.assertEqual(made[0].start, drives.moment("2023-08-17T14:00:00"))
        self.assertEqual(made[1:], [None, None])
        self.assertEqual(drives.moment("2023-08-17T14:00:01") - drives.moment("2023-08-17"
                                                                               "T14:00:00"),
                         1.0)
        self.assertIsNone(drives.moment("May 10"))
        self.assertIsNone(drives.moment("2024-02-30T08:00:00"))

    def test_insv_files_are_never_in_a_drive(self):
        # An Insta360 camera that videos.py reads as one lens (projection "")
        # still makes no drive of its chapters: its .insv files are 360 videos.
        class Row(dict):
            def keys(self):
                return list(dict.keys(self))
        row = Row(id=1, path="x/VID_20230820_090000_00_010.insv",
                  name="VID_20230820_090000_00_010.insv", taken_at="2023-08-20T09:00:00",
                  duration=32.0, width=2880, height=2880, content_hash="x", hidden=0,
                  exif="{}", projection="")
        self.assertIsNone(drives.clip_from_row(row))
        self.assertIsNotNone(drives.clip_from_row(Row(row, name="VID_0010.mp4",
                                                      path="x/VID_0010.mp4")))

    def test_holes_where_clips_are_left_out(self):
        # Clips 1-3, then 4-6 after two missing minutes.
        found = drives.detect(dashcam(1, 101, [0, 60, 120]) + dashcam(4, 106, [300, 360, 420]))
        self.assertEqual(found[0].offsets(), [0.0, 60.0, 120.0, 300.0, 360.0, 420.0])
        # A visitor's records without clip 3 (hidden): clip 4 comes after the
        # two missing minutes and clip 3's minute.
        records = [{"i": image_id, "l": "lib_a"} for image_id in (1, 2, 4, 5, 6)]
        with mock.patch.object(drives, "descriptions", lambda: {}):
            drives.annotate(records, found, "lib_a", owner=False)
        by_id = {record["i"]: record for record in records}
        self.assertEqual(by_id[4]["dh"], 180)
        self.assertEqual([image_id for image_id in by_id if "dh" in by_id[image_id]], [4])
        self.assertEqual(by_id[4]["dx"], 3)
        # An export also leaves out clip 5: its minute is missing before 6.
        places = drives_places(found)
        exported = [dict(record) for record in records if record["i"] != 5]
        drives.regroup(exported, places=places)
        by_id = {record["i"]: record for record in exported}
        self.assertEqual((by_id[4]["dh"], by_id[6]["dh"]), (180, 60))
        self.assertNotIn("dh", by_id[2])
        # Without clips 1 and 2 the drive starts at 4: nothing missing before it.
        exported = [dict(record) for record in records if record["i"] not in (1, 2)]
        drives.regroup(exported, places=places)
        self.assertEqual([record.get("dh") for record in exported], [None, None, None])
        self.assertEqual({record["dv"] for record in exported}, {4})

    def test_settings(self):
        self.assertEqual(drives.settings({}), {"slack": 20, "min_clips": 3})
        self.assertEqual(drives.settings({"drives": {"slack": 5000, "min_clips": 1}}),
                         {"slack": 600, "min_clips": 2})
        self.assertEqual(drives.settings({"drives": {"slack": "x", "min_clips": True}}),
                         {"slack": 20, "min_clips": 3})
        self.assertEqual(drives.preview_settings({}),
                         {"enabled": False, "speed": 60, "height": 720})
        self.assertEqual(drives.preview_settings({"drive_previews": {
            "enabled": True, "speed": 1, "height": 5000}}),
            {"enabled": True, "speed": 2, "height": 1080})
        self.assertFalse(drives.preview_settings({"drive_previews": {"enabled": "yes"}})
                         ["enabled"])

    def test_the_sheet_spreads_nine_frames_over_the_whole_drive(self):
        drive = drives.detect(dashcam(1, 1, [0, 60, 120, 180], [60, 60, 60, 30]))[0]
        places = drives.sheet_places(drive)
        self.assertEqual(len(places), 9)
        # 210 s: frames at 11.7 s, 35 s, ... 198.3 s.
        self.assertEqual([index for index, _offset in places], [0, 0, 0, 1, 1, 2, 2, 2, 3])
        self.assertAlmostEqual(places[0][1], 210 / 18.0)
        self.assertAlmostEqual(places[-1][1], 210 * 17 / 18.0 - 180)
        note = drives.drive_note(9, drive)
        self.assertIn("This picture holds 9 frames, in order", note)
        self.assertIn("from a drive of 4 minutes made of 4 clips", note)

    def test_records_and_regrouping(self):
        found = drives.detect(dashcam(1, 101, [0, 60, 120]) + dashcam(4, 106, [300, 360, 420]))
        records = [{"i": image_id, "l": "lib_a"} for image_id in range(1, 7)]
        records.append({"i": 9, "l": "lib_a"})
        with mock.patch.object(drives, "descriptions", lambda: {"lib_a": {"h1": {
                "info": {"title": "Coast road", "description": "A drive by the sea.",
                         "tags": ["Coast", "coast", "road"]}}}}):
            self.assertEqual(drives.annotate(records, found, "lib_a"), 1)
        by_id = {record["i"]: record for record in records}
        self.assertEqual(by_id[1], {"i": 1, "l": "lib_a", "dv": 1, "dvt": "Coast road",
                                    "dvd": "A drive by the sea.", "dvg": ["coast", "road"]})
        self.assertEqual(by_id[4], {"i": 4, "l": "lib_a", "dv": 1, "dx": 3, "dh": 120})
        self.assertEqual(by_id[6]["dx"], 5)
        self.assertNotIn("dv", by_id[9])
        # Without its first clip (an export left it out) the drive goes on
        # from the next one, which takes its text.
        rest = [dict(record) for record in records if record["i"] != 1]
        drives.regroup(rest, texts={2: {"title": "Coast road"}})
        by_id = {record["i"]: record for record in rest}
        self.assertEqual(by_id[2]["dv"], 2)
        self.assertEqual(by_id[2]["dvt"], "Coast road")
        self.assertEqual({record.get("dv") for record in rest if record["i"] != 9}, {2})
        # Fewer than three clips left: no drive.
        few = [dict(record) for record in records if record["i"] in (5, 6, 9)]
        drives.regroup(few)
        self.assertTrue(all("dv" not in record and "dx" not in record for record in few))
        # Clips missing from the records (hidden ones for a visitor) are left out.
        some = [{"i": image_id, "l": "lib_a"} for image_id in (2, 3)]
        self.assertEqual(drives.annotate(some, found, "lib_a"), 0)
        self.assertEqual(some, [{"i": 2, "l": "lib_a"}, {"i": 3, "l": "lib_a"}])

    def test_a_drives_words_and_flags_and_who_gets_them(self):
        clips = dashcam(1, 101, [0, 60, 120, 180])
        info = {"title": "Past the border", "description": "Plates are readable.",
                "tags": ["Border"], "private_info": True, "sensitive": "graphic"}
        stored = {"lib_a": {"h1": {"info": info}}}

        def records():
            return [{"i": image_id, "l": "lib_a"} for image_id in range(1, 5)]
        with mock.patch.object(drives, "descriptions", lambda: stored):
            plain = records()
            drives.annotate(plain, drives.detect(clips), "lib_a")
            self.assertEqual(plain[0], {"i": 1, "l": "lib_a", "dv": 1, "dvt": "Past the border",
                                        "dvd": "Plates are readable.", "dvg": ["border"],
                                        "dvp": 1, "dvs": "graphic"})
            # The first clip is hidden: the admin's records have the words on
            # the next one (the clip whose meaning vector would hold them);
            # visitors' none (the description was made from that clip too).
            hidden = [clips[0]._replace(hidden=True)] + clips[1:]
            admin = records()
            drives.annotate(admin, drives.detect(hidden), "lib_a")
            self.assertEqual(admin[0], {"i": 1, "l": "lib_a", "dv": 1})
            self.assertEqual((admin[1]["dv"], admin[1]["dvt"]), (1, "Past the border"))
            visitor = records()[1:]
            drives.annotate(visitor, drives.detect(hidden), "lib_a", owner=False)
            self.assertEqual([record.get("dv") for record in visitor], [2, 2, 2])
            self.assertFalse(any("dvt" in record for record in visitor))
            # A middle clip hidden: the same.
            hidden = clips[:2] + [clips[2]._replace(hidden=True)] + clips[3:]
            visitor = [record for record in records() if record["i"] != 3]
            drives.annotate(visitor, drives.detect(hidden), "lib_a", owner=False)
            self.assertFalse(any("dvt" in record for record in visitor))
        self.assertEqual(drives.text_fields({"title": " ", "private_info": True}), {})
        self.assertEqual(drives.text_fields({"title": "A", "sensitive": "none"}), {"dvt": "A"})

    def test_an_export_keeps_the_drive_words_its_options_allow(self):
        def records():
            made = [{"i": image_id, "l": "lib_a", "dv": 1} for image_id in range(1, 4)]
            made[0].update(dvt="Past the border", dvp=1)
            for index, record in enumerate(made[1:], 1):
                record["dx"] = index
            return made
        kept = records()
        options = {"exclude_private": True, "exclude_sensitive": True}
        self.assertEqual(drives.export_texts(kept, options),
                         {"drives": 1, "described": 0, "private": 1, "sensitive": 0})
        self.assertNotIn("dvt", kept[0])
        self.assertNotIn("dvp", kept[0])
        self.assertEqual(kept[0]["dv"], 1)
        kept = records()
        self.assertEqual(drives.export_texts(kept, {"exclude_private": False})["described"], 1)
        self.assertEqual((kept[0]["dvt"], kept[0]["dvp"]), ("Past the border", 1))

    def test_an_export_without_the_location_has_no_positions_in_drive_words(self):
        # (The model reads the position a dashcam prints over its video.)
        def records():
            made = [{"i": image_id, "l": "lib_a", "dv": 1} for image_id in range(1, 4)]
            made[0].update(dvt="Stop at N:12.34567 E:65.43210",
                           dvd="Parked at 12.34567, -65.43210 for fuel.")
            for index, record in enumerate(made[1:], 1):
                record["dx"] = index
            return made
        kept = records()
        drives.export_texts(kept, {"exclude_private": True})
        self.assertEqual((kept[0]["dvt"], kept[0]["dvd"]), ("Stop at", "Parked at for fuel."))
        kept = records()
        drives.export_texts(kept, {"exclude_private": True, "location": True})
        self.assertEqual(kept[0]["dvt"], "Stop at N:12.34567 E:65.43210")

    def test_concat_lists_and_preview_commands(self):
        text = drives.concat_list([r"D:\Photos\Vacation\It's here.mp4",
                                   "/home/you/Pictures/b c.mp4"])
        self.assertEqual(text, "ffconcat version 1.0\n"
                               "file 'file:D:\\Photos\\Vacation\\It'\\''s here.mp4'\n"
                               "file 'file:/home/you/Pictures/b c.mp4'\n")
        tools = drives.videos.Tools(["ffmpeg"], ["ffprobe"])
        argv = drives.preview_command(tools, "list.txt", "out.mp4", 60, 720, True)
        self.assertEqual(argv[:8], ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                                    "-y", "-skip_frame", "nokey"])
        self.assertIn("-an", argv)
        self.assertEqual(argv[argv.index("-f") + 1], "concat")
        self.assertEqual(argv[argv.index("-i") + 1], "file:list.txt")
        chain = argv[argv.index("-vf") + 1]
        self.assertTrue(chain.startswith("setpts=PTS/60,fps=30,scale="), chain)
        self.assertIn("min(720,", chain)
        self.assertEqual(argv[-1], "file:out.mp4")
        self.assertNotIn("-skip_frame", drives.preview_command(tools, "l", "o", 30, 480, False))


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
def drives_places(found):
    """{clip id: (seconds from its drive's start, length)} of drives (as clip_places)."""
    return {clip.id: (offset, clip.duration) for drive in found
            for clip, offset in zip(drive.clips, drive.offsets())}


class DriveLibraryCase(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):
    """A folder of dashcam clips (two drives, one with a missing clip) and a phone video."""

    def setUp(self):
        super().setUp()
        self.use_fake_tools()
        self.fake_ffmpeg = fake_ffmpeg
        self.folder = self.base.library_folder("Trip")
        start = datetime.datetime(2023, 8, 17, 9, 0, 0)
        plan = [(1, 0, 60), (2, 60, 60), (3, 120, 60), (4, 180, 30),       # drive A
                (5, 7200, 60), (6, 7260, 60), (8, 7380, 60)]               # drive B (7 missing)
        self.names = []
        for number, offset, duration in plan:
            moment = start + datetime.timedelta(seconds=offset)
            name = "%s_%04d_N_A.MP4" % (moment.strftime("%Y%m%d_%H%M%S"), number)
            # The camera dates a clip by its end (UTC), as many dashcams do.
            end = moment + datetime.timedelta(seconds=duration)
            probe = fake_ffmpeg.probe_json(
                width=2560, height=1440, duration=duration, codec="hevc", brand="mp42",
                format_tags={"creation_time": end.strftime("%Y-%m-%dT%H:%M:%S.000000Z")},
                stream_tags={})
            fake_ffmpeg.make_video(os.path.join(self.folder, name), probe, keyframe_every=0.5)
            self.names.append(name)
        fake_ffmpeg.make_video(os.path.join(self.folder, "IMG_0001.MOV"),
                               videotools.clip_probe())

    def configure(self, **extra):
        self.base.write_config([support.library("Trip", self.folder, id="lib_trip1")], **extra)
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        return self.config

    def scan(self):
        import scan
        self.ctx = jobs.JobContext("test", log_limit=5000)
        return scan.run(self.config, self.ctx, describe_images=False)["libraries"][0]

    def conn(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        return conn

    def ids(self):
        return {row["path"]: row["id"] for row in self.conn().execute(
            "SELECT id, path FROM images")}

    def execute(self, sql, args=()):
        conn = db.connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def log_text(self):
        return "\n".join(entry["message"]
                         for entry in self.ctx.snapshot(log_tail=5000)["log_tail"])

    def found(self):
        return drives.library_drives(self.conn(), "lib_trip1", self.config)


class ScanAndRecordsTest(DriveLibraryCase):

    def test_a_scan_finds_the_drives(self):
        self.configure()
        self.scan()
        ids = self.ids()
        found = self.found()
        self.assertEqual([drive.ids for drive in found],
                         [[ids[name] for name in self.names[:4]],
                          [ids[name] for name in self.names[4:]]])
        self.assertEqual(found[1].holes, [0.0, 0.0, 60.0])
        self.assertEqual(found[0].id, self.conn().execute(
            "SELECT content_hash FROM images WHERE id = ?", (found[0].key,)).fetchone()[0])
        # The same rows: the same (remembered) drives.
        self.assertIs(self.found(), found)
        records = {record["i"]: record for record in search_index.build_records(
            self.conn(), self.config, ["lib_trip1"])}
        first = [records[ids[name]] for name in self.names[:4]]
        self.assertEqual([record.get("dv") for record in first], [ids[self.names[0]]] * 4)
        self.assertEqual([record.get("dx") for record in first], [None, 1, 2, 3])
        self.assertEqual(records[ids[self.names[6]]]["dh"], 60)
        self.assertNotIn("dv", records[ids["IMG_0001.MOV"]])

    def test_visitors_get_the_drives_without_hidden_clips(self):
        self.configure()
        self.scan()
        ids = self.ids()
        self.execute("UPDATE images SET hidden = 1 WHERE id IN (?, ?)",
                     (ids[self.names[0]], ids[self.names[5]]))
        records = {record["i"]: record for record in search_index.build_records(
            self.conn(), self.config, ["lib_trip1"], include_hidden=False)}
        second = ids[self.names[1]]
        self.assertEqual([records[ids[name]].get("dv") for name in self.names[1:4]],
                         [second] * 3)
        self.assertEqual(records[second]["dx"], 1)
        # Drive B has two clips left: no drive.
        self.assertNotIn("dv", records[ids[self.names[4]]])
        # The admin sees both drives whole; the hidden clip stays the first.
        admin = {record["i"]: record for record in search_index.build_records(
            self.conn(), self.config, ["lib_trip1"])}
        self.assertEqual(admin[second]["dv"], ids[self.names[0]])
        self.assertEqual(admin[ids[self.names[4]]]["dv"], ids[self.names[4]])

    def test_settings_change_the_drives(self):
        self.configure(drives={"min_clips": 4})
        self.scan()
        self.assertEqual([len(drive.clips) for drive in self.found()], [4])

    def test_the_index_answer_follows_drive_descriptions(self):
        self.configure()
        self.scan()
        service = search_index.IndexService(watch=False)
        names = {"lib_trip1": "Trip"}
        _status, body, headers = service.respond(self.conn(), self.config, names)
        before = headers["ETag"]
        found = self.found()
        drives.save_description("lib_trip1", found[0], {
            "title": "Afternoon drive", "description": "Through hills to a lake.",
            "tags": ["Hills", "lake"]}, {"model": "fake"})
        status, body, headers = service.respond(self.conn(), self.config, names)
        self.assertNotEqual(headers["ETag"], before)
        record = {item["i"]: item for item in json.loads(body)["records"]}[found[0].key]
        self.assertEqual((record["dvt"], record["dvd"], record["dvg"]),
                         ("Afternoon drive", "Through hills to a lake.", ["hills", "lake"]))
        # Unchanged: 304.
        status, _body, _headers = service.respond(
            self.conn(), self.config, names,
            etag_matches=lambda tag: '"%s"' % tag == headers["ETag"])
        self.assertEqual(status, 304)
        # The search by meaning finds the drive by its first clip.
        import embeddings
        texts = {image_id: text for image_id, _library, _hidden, text in
                 embeddings.library_texts(self.conn(), ["lib_trip1"])}
        self.assertIn("Drive: Afternoon drive.", texts[found[0].key])
        self.assertIn("Through hills to a lake.", texts[found[0].key])
        self.assertNotIn(found[0].clips[1].id, texts)        # (not described)

    def test_the_search_by_meaning_holds_only_what_the_records_publish(self):
        import embeddings
        self.configure()
        self.scan()
        first, second = self.found()
        drives.save_description("lib_trip1", first, {
            "title": "Afternoon drive", "description": "Through hills to a lake.",
            "tags": ["Hills"], "subjects": ["a secret cabin"], "text": "ABC 123"}, {})
        drives.save_description("lib_trip1", second, {
            "title": "Night drive", "private_info": True}, {})

        def texts():
            return {image_id: text for image_id, _library, _hidden, text in
                    embeddings.library_texts(self.conn(), ["lib_trip1"])}
        text = texts()[first.key]
        self.assertEqual(text, "Drive: Afternoon drive.\nThrough hills to a lake.\n"
                               "Tags: hills.")
        # Flagged private (or sensitive): not in any vector.
        self.assertNotIn(second.key, texts())
        # A clip of the drive hidden: neither (visitors get the vectors).
        self.execute("UPDATE images SET hidden = 1 WHERE id = ?", (first.clips[2].id,))
        self.assertNotIn(first.key, texts())

    def test_an_exported_vector_never_holds_drive_words_the_export_does_not(self):
        import embeddings
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps({"title": "Road", "description": "A road."}),
                      prompts.PROMPT_VER))
        first, _second = self.found()
        drives.save_description("lib_trip1", first, {"title": "Lake drive"}, {})
        store = embeddings.connect()
        self.addCleanup(store.close)
        embeddings.prepare_model(store, "m", 4)
        rows = [(image_id, "lib_trip1", embeddings.text_hash(text, "m", 4), b"\x01\x02\x03\x04")
                for image_id, _library, _hidden, text in
                embeddings.library_texts(self.conn(), ["lib_trip1"])]
        embeddings.write(store, rows)
        ids = first.ids
        # The export publishes the drive's words on its first clip: all fresh.
        found = embeddings.fresh_vectors(self.conn(), "lib_trip1", ids, store,
                                         carried={first.key: {"dvt": "Lake drive"}})
        self.assertEqual(sorted(found["vectors"]), sorted(ids))
        # It does not (left out by its options, or no drive in the export):
        # the vector made with them is stale, the others go.
        found = embeddings.fresh_vectors(self.conn(), "lib_trip1", ids, store, carried={})
        self.assertEqual(sorted(found["vectors"]), sorted(ids[1:]))
        self.assertEqual(found["stale"], 1)
        # On another clip (the first was left out): neither of the two goes.
        found = embeddings.fresh_vectors(self.conn(), "lib_trip1", ids, store,
                                         carried={ids[1]: {"dvt": "Lake drive"}})
        self.assertEqual(sorted(found["vectors"]), sorted(ids[2:]))


class StoreTest(DriveLibraryCase):

    def test_descriptions_are_kept_by_drive_and_clips(self):
        self.configure()
        self.scan()
        first, second = self.found()
        self.assertEqual(drives.version(), "")
        drives.save_description("lib_trip1", first, {"title": "One"}, {"model": "m"})
        version = drives.version()
        self.assertTrue(version)
        self.assertEqual(drives.description("lib_trip1", first), {"title": "One"})
        self.assertIsNone(drives.description("lib_trip1", second))
        with open(drives.store_path(), encoding="ascii") as handle:
            stored = json.load(handle)
        entry = stored["libraries"]["lib_trip1"][first.id]
        self.assertEqual((entry["clips"], entry["count"], entry["length"]),
                         (first.clips_key, 4, 210.0))
        # A drive that grew (another clip at its end) keeps its id, and its
        # description until it is described again.
        grown = drives.Drive(first.clips + second.clips[:1], first.holes + [0.0])
        self.assertEqual(grown.id, first.id)
        self.assertEqual(drives.description("lib_trip1", grown), {"title": "One"})
        work = drives.pending(self.conn(), self.config, self.config["libraries"])
        self.assertEqual([drive.id for _library, drive in work], [second.id])
        # Drives that are gone are forgotten.
        drives.save_description("lib_trip1", second, {"title": "Two"}, {})
        self.assertEqual(drives.prune("lib_trip1", [second]), 1)
        self.assertIsNone(drives.description("lib_trip1", first))
        # A library's data moves and goes.
        drives.move_library("lib_trip1", "lib_other1")
        self.assertIsNone(drives.description("lib_trip1", second))
        self.assertEqual(drives.description("lib_other1", second), {"title": "Two"})
        drives.forget_library("lib_other1")
        self.assertEqual(drives.descriptions(), {})

    def test_failures_wait_for_retry_failed(self):
        self.configure()
        self.scan()
        first, second = self.found()
        for _attempt in range(common.MAX_DESCRIBE_FAILURES):
            drives.record_failure("lib_trip1", first, "no frames")
        libraries = self.config["libraries"]
        self.assertEqual([drive.id for _l, drive in drives.pending(self.conn(), self.config,
                                                                   libraries)], [second.id])
        self.assertEqual(len(drives.pending(self.conn(), self.config, libraries,
                                            retry_failed=True)), 2)
        self.assertEqual(drives.entry_for("lib_trip1", first)["error"], "no frames")


class DescribeTest(DriveLibraryCase):

    def setUp(self):
        super().setUp()
        import describe
        import fake_llm
        self.describe_module = describe
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(describe, "UNREACHABLE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self, **changes):
        return dict({"name": "local", "kind": "openai", "base_url": self.server.base_url,
                     "api_key": "", "model": "fake-model"}, **changes)

    def describe(self, **kwargs):
        self.ctx = jobs.JobContext("describe", log_limit=5000)
        return self.describe_module.describe_pending(self.conn(), self.config, self.ctx,
                                                     sleep=lambda seconds: None, **kwargs)

    def drive_prompts(self):
        return [entry for entry in self.server.describe_requests()
                if "from a drive of" in entry["prompt"]]

    def test_each_drive_is_described_once_from_nine_frames(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        summary = self.describe()
        self.assertEqual(summary["described"], 8)
        self.assertEqual(summary["drives"]["described"], 2)
        sent = self.drive_prompts()
        self.assertEqual(len(sent), 2)
        texts = sorted(entry["prompt"] for entry in sent)
        self.assertIn("This picture holds 9 frames, in order", texts[0])
        self.assertIn("a drive of 3 minutes made of 3 clips", texts[0])
        self.assertIn("a drive of 4 minutes made of 4 clips", texts[1])
        from PIL import Image
        import io
        with Image.open(io.BytesIO(sent[0]["image"])) as sheet:
            self.assertEqual(sheet.size[0], common.DEFAULT_MAX_IMAGE_EDGE)
        first, second = self.found()
        info = drives.description("lib_trip1", first)
        self.assertEqual(info["title"], "Red square on a blue background")
        entry = drives.entry_for("lib_trip1", first)
        self.assertEqual(entry["provenance"]["frames"], 9)
        self.assertEqual(entry["provenance"]["prompt_ver"], prompts.PROMPT_VER)
        records = {record["i"]: record for record in search_index.build_records(
            self.conn(), self.config, ["lib_trip1"])}
        self.assertEqual(records[first.key]["dvt"], "Red square on a blue background")
        self.assertNotIn("dvt", records[first.clips[1].id])
        # What the drives used counts in the run's totals too.
        self.assertEqual(summary["drives"]["requests"], 2)
        self.assertEqual(summary["requests"], 10)
        self.assertGreater(summary["drives"]["tokens_in"], 0)
        self.assertGreaterEqual(summary["tokens_in"], summary["drives"]["tokens_in"])
        self.assertIn("In all (pictures and drives): Tokens:", self.log_text())
        # Nothing new: nothing is sent again.
        count = len(self.server.describe_requests())
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), count)
        self.assertNotIn("drives", summary)

    def test_a_run_for_chosen_pictures_or_with_a_limit_leaves_drives_alone(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        self.describe(limit=3)
        self.assertEqual(self.drive_prompts(), [])
        self.describe(ids=[self.found()[0].key], force=True)
        self.assertEqual(self.drive_prompts(), [])

    def test_a_drive_with_a_hidden_clip_stays_on_this_computer(self):
        self.configure(endpoints=[self.endpoint(name="cloud")], default_endpoint="cloud")
        self.scan()
        first, second = self.found()
        self.execute("UPDATE images SET hidden = 1 WHERE id = ?", (first.clips[2].id,))
        ctx = jobs.JobContext("describe", log_limit=5000)
        with mock.patch.object(common, "is_local_endpoint", lambda endpoint: False):
            summary = drives.describe_drives(self.conn(), self.config, ctx,
                                             self.config["libraries"],
                                             sleep=lambda seconds: None)
        self.assertEqual((summary["described"], summary["skipped"]), (1, 1))
        self.assertIsNone(drives.description("lib_trip1", self.found()[0]))
        self.assertIsNotNone(drives.description("lib_trip1", self.found()[1]))

    def test_the_drives_of_an_offline_library_wait(self):
        import library_ops
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        with mock.patch.object(library_ops, "folder_status",
                               lambda root, timeout=None: {"reachable": False}):
            summary = self.describe()
        self.assertEqual(self.drive_prompts(), [])
        self.assertNotIn("drives", summary)
        self.assertIn("2 drive(s) wait for a description: the folder", self.log_text())
        ctx = jobs.JobContext("describe", log_limit=5000)
        with mock.patch.object(library_ops, "folder_status",
                               lambda root, timeout=None: {"reachable": False}):
            summary = drives.describe_drives(self.conn(), self.config, ctx,
                                             self.config["libraries"])
        self.assertEqual((summary["described"], summary["skipped"]), (0, 2))

    def test_without_ffmpeg_drives_wait(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        ctx = jobs.JobContext("describe", log_limit=5000)
        with mock.patch.object(drives.videos, "find_tools", lambda config=None: None):
            summary = drives.describe_drives(self.conn(), self.config, ctx,
                                             self.config["libraries"])
        self.assertEqual((summary["described"], summary["skipped"]), (0, 2))


class PreviewTest(DriveLibraryCase):

    def test_the_previews_job_makes_sped_up_copies(self):
        self.configure(drive_previews={"enabled": True, "speed": 30, "height": 480})
        self.scan()
        ctx = jobs.JobContext("drive_previews", log_limit=5000)
        summary = jobs.run_drive_previews(ctx, self.conn(), self.config,
                                          {"library_id": "lib_trip1"})
        self.assertEqual((summary["made"], summary["kept"], summary["failed"]), (2, 0, 0))
        first, second = self.found()
        path, speed = drives.existing_preview("lib_trip1", first, self.config)
        self.assertEqual(speed, 30)
        self.assertTrue(path.endswith("-30x-480.mp4"))
        spec = self.fake_ffmpeg._load(path)
        self.assertEqual(spec["sources"], 4)
        self.assertEqual(spec["probe"]["streams"][0]["codec_name"], "h264")
        self.assertAlmostEqual(float(spec["probe"]["format"]["duration"]), 210 / 30.0)
        # Key frames every half second: only those are decoded.
        self.assertEqual(spec["skip_frame"], "nokey")
        self.assertEqual(drives.preview_usage()["lib_trip1"]["count"], 2)
        # Nothing new: kept; other settings: made again, the old ones removed.
        summary = jobs.run_drive_previews(ctx, self.conn(), self.config, {})
        self.assertEqual((summary["made"], summary["kept"]), (0, 2))
        config = dict(self.config, drive_previews={"enabled": True, "speed": 60})
        summary = drives.make_previews(self.conn(), config, ctx, ["lib_trip1"])
        self.assertEqual((summary["made"], summary["removed"]), (2, 2))

    def test_the_job_is_refused_while_previews_are_off(self):
        self.configure()
        self.scan()
        ctx = jobs.JobContext("drive_previews", log_limit=5000)
        with self.assertRaises(jobs.JobError) as caught:
            jobs.run_drive_previews(ctx, self.conn(), self.config, {})
        self.assertIn("drive_previews", str(caught.exception))


try:
    import flask
except ImportError:
    flask = None


@unittest.skipIf(flask is None, "Flask is not installed")
class ServerTest(DriveLibraryCase):

    def setUp(self):
        super().setUp()
        import serve
        self.configure(drive_previews={"enabled": True})
        self.scan()
        self.app = serve.build_app(init_db=False)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def test_the_sped_up_preview_and_who_may_see_it(self):
        first, second = self.found()
        url = "/drive-preview/%d" % first.clips[1].id
        self.assertEqual(self.client.get(url).status_code, 404)        # not made yet
        detail = self.client.get("/api/image/%d" % first.key).get_json()
        self.assertEqual(detail["drive"], {"clips": 4, "preview": False, "speed": None,
                                           "copies": []})
        # Which clips have a playable copy (the viewer plays those of a codec
        # the browser cannot decode).
        clip = first.clips[2]
        path = drives.videos.proxy_path("lib_trip1", clip.id, clip.content_hash)
        common.ensure_dir(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"copy")
        detail = self.client.get("/api/image/%d" % first.key).get_json()
        self.assertEqual(detail["drive"]["copies"], [clip.id])
        ctx = jobs.JobContext("drive_previews", log_limit=5000)
        drives.make_previews(self.conn(), self.config, ctx, ["lib_trip1"])
        path, _speed = drives.existing_preview("lib_trip1", first, self.config)
        with open(path, "rb") as handle:
            data = handle.read()
        response = self.client.get(url, environ_base=REMOTE)
        self.assertEqual((response.status_code, response.mimetype), (200, "video/mp4"))
        self.assertEqual(response.get_data(), data)
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        response.close()
        response = self.client.get(url, headers={"Range": "bytes=2-5"})
        self.assertEqual((response.status_code, response.get_data()), (206, data[2:6]))
        response.close()
        detail = self.client.get("/api/image/%d" % first.key).get_json()
        self.assertEqual(detail["drive"], {"clips": 4, "preview": True, "speed": 60,
                                           "copies": [first.clips[2].id]})
        # A drive with a hidden clip: the admin's only.
        self.execute("UPDATE images SET hidden = 1 WHERE id = ?", (first.clips[3].id,))
        self.assertEqual(self.client.get(url, environ_base=REMOTE).status_code, 404)
        detail = self.client.get("/api/image/%d" % first.key, environ_base=REMOTE).get_json()
        self.assertFalse(detail["drive"]["preview"])
        self.assertEqual(detail["drive"]["clips"], 3)       # (the visitor's)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response.close()
        # Not a clip of a drive, not a video: nothing.
        ids = self.ids()
        self.assertEqual(self.client.get("/drive-preview/%d" % ids["IMG_0001.MOV"]).status_code,
                         404)
        self.assertNotIn("drive", self.client.get("/api/image/%d" % ids["IMG_0001.MOV"])
                         .get_json())
        self.assertEqual(self.client.get("/drive-preview/999999").status_code, 404)

    def test_the_manage_page_counts_drives(self):
        import admin
        state = self.client.get(admin.URL_PREFIX + "/state",
                                headers={"X-Search-Images": "1"}).get_json()
        self.assertTrue(state["videos"]["drive_previews"]["enabled"])
        view = state["libraries"][0]["video_info"]["drives"]
        self.assertEqual((view["count"], view["described"], view["seconds"]), (2, 0, 390.0))
        self.assertEqual(view["previews"], {"count": 0, "bytes": 0, "pending": 2})


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminJobTest(DriveLibraryCase):

    def test_the_job_kind_is_known_and_checked(self):
        import admin
        import serve
        self.configure()
        self.scan()
        manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(manager.stop, 30)
        app = serve.build_app(init_db=False, job_manager=manager)
        app.testing = True
        self.addCleanup(app.extensions["search_images"].close)
        client = app.test_client()
        refused = client.post(admin.URL_PREFIX + "/jobs", data=json.dumps(
            {"kind": "drive_previews", "library_id": "lib_trip1"}),
            content_type="application/json", headers={"X-Search-Images": "1"})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("drive_previews", refused.get_json()["error"])


class ExportTest(DriveLibraryCase):

    def test_an_export_keeps_its_drives_with_export_ids(self):
        import export
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps({"title": "Road", "description": "A road.", "tags": [],
                                  "photo_type": "photo", "sensitive": "none",
                                  "private_info": False}), prompts.PROMPT_VER))
        first, second = self.found()
        drives.save_description("lib_trip1", first, {"title": "Hill road"}, {})
        # The first clip is left out (hidden): the drive goes on from the second.
        self.execute("UPDATE images SET hidden = 1 WHERE id = ?", (first.key,))
        lines = []
        conn = self.conn()
        summary = export.export_library(export.LogContext(lines.append), conn,
                                        common.load_config(), "lib_trip1")
        records = []
        data = os.path.join(summary["output"], "data")
        for name in sorted(os.listdir(data)):
            if name.startswith("index-"):
                with open(os.path.join(data, name), encoding="ascii") as handle:
                    text = handle.read()
                records.extend(json.loads(text[text.index("(") + 1:text.rindex(")")]))
        in_drives = [record for record in records if "dv" in record]
        self.assertEqual(len(in_drives), 6)
        covers = {record["dv"] for record in in_drives}
        self.assertEqual(len(covers), 2)
        by_id = {record["i"]: record for record in records}
        for cover in covers:
            self.assertIn(cover, by_id)                     # an export id
        # (Its description was made from the hidden clip's frames too: not
        # published.)
        self.assertEqual([record for record in in_drives if record.get("dvt")], [])
        self.assertFalse(any(isinstance(record["dv"], int) for record in in_drives))
        # The second drive's hole (clip 7 is missing) is published; nothing
        # comes before the first drive's first published clip.
        self.assertEqual(sorted(record.get("dh", 0) for record in in_drives),
                         [0, 0, 0, 0, 0, 60])
        self.assertEqual(drives.clip_places(conn, "lib_trip1", common.load_config())[
            second.clips[2].id], (180.0, 60.0))

    def exported(self, options=None):
        import export
        summary = export.export_library(export.LogContext(lambda line: None), self.conn(),
                                        common.load_config(), "lib_trip1", options=options)
        records = []
        data = os.path.join(summary["output"], "data")
        for name in sorted(os.listdir(data)):
            if name.startswith("index-"):
                with open(os.path.join(data, name), encoding="ascii") as handle:
                    text = handle.read()
                records.extend(json.loads(text[text.index("(") + 1:text.rindex(")")]))
        return records

    def test_an_export_leaves_out_drive_words_flagged_private_or_sensitive(self):
        import export
        self.configure()
        self.scan()
        self.execute("UPDATE images SET info = ?, described = 1, prompt_ver = ?",
                     (json.dumps({"title": "Road", "description": "A road.", "tags": [],
                                  "photo_type": "photo", "sensitive": "none",
                                  "private_info": False}), prompts.PROMPT_VER))
        first, second = self.found()
        drives.save_description("lib_trip1", first, {
            "title": "Past 12 Elm Street", "description": "Plates are readable.",
            "private_info": True, "sensitive": "none"}, {})
        drives.save_description("lib_trip1", second, {
            "title": "The crash", "sensitive": "graphic"}, {})
        # The review step says so.
        review = export.preview_export(self.conn(), common.load_config(), "lib_trip1")
        self.assertEqual(review["counts"]["drives"],
                         {"drives": 2, "described": 0, "private": 1, "sensitive": 1})
        records = self.exported()
        self.assertEqual([record for record in records if "dvt" in record or "dvd" in record],
                         [])
        self.assertEqual(len([record for record in records if "dv" in record]), 7)
        # Options that publish them: with their sensitive level (the page
        # filters by it), never the private flag (export.PRIVATE_FLAG_KEYS).
        library = common.get_library(common.load_config(), "lib_trip1")
        options = export.library_options(library, {"exclude_private": False,
                                                   "exclude_sensitive": False})
        texts = {record["dvt"]: record for record in self.exported(options) if "dvt" in record}
        self.assertNotIn("dvp", texts["Past 12 Elm Street"])
        self.assertEqual(texts["The crash"]["dvs"], "graphic")


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class NodeTest(unittest.TestCase):

    def test_drives_on_the_search_page(self):
        script = os.path.join(support.REPO, "tests", "js", "app_drives_test.js")
        result = subprocess.run([shutil.which("node"), script], cwd=support.REPO,
                                capture_output=True, timeout=300)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)


if __name__ == "__main__":
    unittest.main()
