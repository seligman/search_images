"""Sheet talk: video and drive descriptions that describe the contact sheet
(sheet_talk.py), asking again once (describe.describe_checked, drives.py),
and such descriptions counting as out of date (prompts.stale_sql).

The AI model is played by tests/fake_llm.py, ffmpeg and ffprobe by
tests/fake_ffmpeg.py; everything runs in a temporary data root. Every
title and description here is made up.
"""

import json
import os
import sqlite3
import unittest
from unittest import mock

import support

import fake_llm

import db
import describe
import drives
import jobs
import progress
import prompts
import sheet_talk

# The video and drive libraries of these tests (they need Pillow).
try:
    import test_video_library
except unittest.SkipTest:
    test_video_library = None
try:
    import test_drives
except unittest.SkipTest:
    test_drives = None
NO_PILLOW = test_video_library is None or test_drives is None


class DetectorTest(unittest.TestCase):

    def assertCaught(self, text, *phrases):
        found = sheet_talk.find(text)
        self.assertTrue(found, "not caught: %r" % text)
        for phrase in phrases:
            self.assertIn(phrase, found, text)

    def assertLeftAlone(self, text):
        self.assertEqual(sheet_talk.find(text), [], text)

    def test_counted_frames_panels_and_moments(self):
        self.assertCaught("Four frames show a dog in a yard.", "Four frames")
        self.assertCaught("4 sequential images of a train passing a crossing.",
                          "4 sequential images")
        self.assertCaught("Nine panels follow a drive along a coast.", "Nine panels")
        self.assertCaught("Dark room with four video frames", "four video frames")
        self.assertCaught("Four sequential moments from a video show a candle burning.",
                          "Four sequential moments")
        self.assertCaught("Four distinct shots from a boat tour.", "Four distinct shots")
        self.assertCaught("Four sequential dashcam views of a highway.",
                          "Four sequential dashcam views")
        self.assertCaught("Three views of a red shed before the car leaves.", "Three views")
        self.assertCaught("Four nearly identical still frames of a meadow.",
                          "Four nearly identical still frames")
        self.assertCaught("Four sequential dashcam-style photos capture a bridge.",
                          "Four sequential dashcam-style photos")
        self.assertCaught("A four-panel montage of a city drive.", "four-panel")
        self.assertCaught("A four-frame sequence from a phone video.", "four-frame")
        self.assertCaught("A 2x2 collage of a beach at noon.", "2x2 collage")
        self.assertCaught("Several frames show the kite rising.", "Several frames")
        self.assertCaught("The video captures four moments from a minute of traffic.",
                          "four moments")

    def test_sequences_grids_and_splits(self):
        self.assertCaught("A sequence of four frames from a video showing a harbor.",
                          "sequence of four frames from a video")
        self.assertCaught("A sequence of stills of a toddler stacking blocks.",
                          "sequence of stills")
        self.assertCaught("A sequence of dashcam footage captures a drive.",
                          "sequence of dashcam footage")
        self.assertCaught("A series of dashcam shots capture a wet road.",
                          "series of dashcam shots")
        self.assertCaught("A grid of frames showing a soccer match.", "grid of frames")
        self.assertCaught("A montage of frames from a ferry ride.", "montage of frames")
        self.assertCaught("A collage of video stills from a party.", "collage of video stills")
        self.assertCaught("Ferry ride video collage", "video collage")
        self.assertCaught("A contact sheet of a birthday party.", "contact sheet")
        self.assertCaught("The picture is split into four panels.", "split into four panels")
        self.assertCaught("The scene is divided into nine frames.", "divided into nine frames")
        self.assertCaught("Sequential views of a tram stop.", "Sequential views")
        self.assertCaught("A shark circles the tank in a series of four shots.",
                          "series of four shots")

    def test_one_or_some_of_the_frames(self):
        self.assertCaught("A hat is visible in each frame.", "in each frame")
        self.assertCaught("A logo sits in the corner of each panel.", "each panel")
        self.assertCaught("Each snapshot has a timestamp.", "Each snapshot")
        self.assertCaught("A cyclist appears in one frame to the left.", "one frame")
        self.assertCaught("In the final frame, the runner crosses the line.", "final frame")
        self.assertCaught("The first frame shows a gate; the second frame a lane.",
                          "first frame", "second frame")
        self.assertCaught("The third panel shows a bus turning.", "third panel")
        self.assertCaught("The second shot looks up at the tower.", "second shot")
        self.assertCaught("A heron lands in one of the frames.", "one of the frames")
        self.assertCaught("A truck passes in two of the frames.", "two of the frames")
        self.assertCaught("A bridge is seen in one of the shots.", "one of the shots")
        self.assertCaught("The rabbit sits still in some frames.", "some frames")
        self.assertCaught("Glare grows in the later frames.", "later frames")
        self.assertCaught("The speed reads 0 in all frames.", "all frames")
        self.assertCaught("The other views show a pine forest.", "other views")
        self.assertCaught("The kite stays central in all views.", "all views")
        self.assertCaught("A dog appears in a separate shot.", "in a separate shot")
        self.assertCaught("The child waves at the camera in the first moment.", "first moment")
        self.assertCaught("The first two frames show a tunnel; the third and fourth frames "
                          "show a bridge.", "first two frames", "fourth frames")

    def test_places_on_the_sheet(self):
        self.assertCaught("The top-left frame shows a red car.", "top-left frame")
        self.assertCaught("In the bottom right frame, a sign reads EXIT.", "bottom right frame")
        self.assertCaught("The bottom two panels show the beach.", "bottom two panels")
        self.assertCaught("A boat is visible in the lower panels.", "lower panels")
        self.assertCaught("The top frames show a toll booth.", "top frames")
        self.assertCaught("A woman appears in the middle right panel.", "middle right panel")
        self.assertCaught("In the top right view, a plane crosses the sky.", "top right view")
        self.assertCaught("The bottom two images show the tree lit in blue.",
                          "bottom two images")
        self.assertCaught("Top left shows a cockpit. Top right shows a runway.",
                          "Top left shows", "Top right shows")

    def test_across_the_frames(self):
        self.assertCaught("The light changes across the frames.", "across the frames")
        self.assertCaught("Clouds drift across the panels.", "across the panels")
        self.assertCaught("The tide shifts slightly between the frames.", "between the frames")
        self.assertCaught("The scene stays the same throughout the frames.",
                          "throughout the frames")
        self.assertCaught("A swimmer practices strokes across four different moments.",
                          "across four different moments")
        self.assertCaught("They face different ways across the shots.", "across the shots")
        self.assertCaught("The colours change across the views, from red to green.",
                          "across the views")

    def test_the_picture_called_a_frame(self):
        self.assertCaught("A still frame from a video shows a calm lake.",
                          "A still frame from a video")
        self.assertCaught("A video frame shows waves on a reef.", "A video frame")
        self.assertCaught("It seems to be a single frame from a video.",
                          "a single frame from a video")
        self.assertCaught("Frames from a dashcam video show a parking lot.",
                          "Frames from a dashcam video")

    def test_normal_video_language_is_left_alone(self):
        for text in (
                "A kayak drifts across the frame from left to right.",
                "A cat walks into the frame and sits in the frame's centre.",
                "A cat is in the frame for a moment.",
                "The dog runs out of frame at the end.",
                "Light falls through the frame of a door into a dark hall.",
                "Two people walk along a beach at sunset.",
                "A sequence of events unfolds as the cake is cut.",
                "Hikers stop to enjoy the views from the summit.",
                "Hikers enjoy the views.",
                "The camera's high frame rate makes the water look smooth.",
                "A picture frame hangs above the sofa; picture frames line the hall.",
                "A 360-degree view of a plaza with a fountain.",
                "Tall pines frame the lake on both sides, and the windshield frames the road.",
                "A wide shot of a waterfall, then a close-up shot of a fern.",
                "A point-of-view shot from the driver's seat.",
                "A first-person view of a bike ride on a forest trail.",
                "Aerial views of a rocky coastline.",
                "The film runs at 24 frames per second; eight frames a second in the burst.",
                "Solar panels cover the roof, and the control panel lights up.",
                "Fish swim in the upper-left quadrant of the tank.",
                "A ball rolls from the top left of the frame to the bottom right.",
                "The top right corner shows a clock.",
                "At the last moment the dog jumps the fence.",
                "In the final moments of the clip, fireworks fill the sky.",
                "A series of exits pass by on the highway.",
                "A sequence of movements turns the paper into a crane.",
                "A divided highway with four lanes in each direction.",
                "One still stands while the other falls; each still holding a flag.",
                "Four people sit at a table with two dogs.",
        ):
            self.assertLeftAlone(text)

    def test_tricky_cases(self):
        # What every frame of the video carries (a dashcam's overlay, the
        # hood) is ordinary language; "in each frame" is about the sheet.
        self.assertLeftAlone("Overlays at the bottom of each frame show the time and speed.")
        self.assertLeftAlone("A timestamp is overlaid on each frame.")
        self.assertLeftAlone("The hood of the car is visible at the bottom of the frames.")
        self.assertCaught("The hood of the car is visible in each frame.", "in each frame")
        # Real frames and panels.
        self.assertLeftAlone("The window frames cast a grid of shadows over the floor.")
        self.assertLeftAlone("The frames of his glasses are red.")
        self.assertLeftAlone("A white six-panel door opens into a bedroom.")
        self.assertLeftAlone("The panel has white labels and small lights.")
        self.assertLeftAlone("A man lifts the frames of a beehive one by one.")
        # Taking pictures, and shots in a game.
        self.assertLeftAlone("She takes two photos of the fountain.")
        self.assertLeftAlone("The couple poses for several photos by the car.")
        self.assertLeftAlone("The golfer makes three shots to reach the green.")
        # Views of a place, not of the sheet.
        self.assertLeftAlone("The camera pans across the views of the canyon.")
        # Ordinary film words are left to the model (a few videos do have
        # cuts); being "in" the first or final shot or view is the sheet's.
        self.assertLeftAlone("The final shot shows a quiet harbor.")
        self.assertLeftAlone("The final shots show the garage.")
        self.assertLeftAlone("In one segment, a truck passes on the left.")
        self.assertLeftAlone("The whole scene is filmed in one shot.")

    def test_in_a_shot_or_view_and_left_or_right(self):
        self.assertCaught("A fountain makes a dome-like shape in one view.", "in one view")
        self.assertCaught("Three men cross the street in the first view.", "in the first view")
        self.assertCaught("A bus is seen in the distance in the first shot.", "in the first shot")
        self.assertCaught("In the final shot, a person walks by.", "In the final shot")
        self.assertCaught("In the final shots, a monitor sits on a desk.", "In the final shots")
        self.assertCaught("A road leads to an overpass in the final view.", "in the final view")
        self.assertCaught("In another shot, the dog sleeps.", "In another shot")
        self.assertCaught("In the left frame, the car is stopped.", "left frame")
        self.assertCaught("The right panel shows a truck.", "right panel")
        # (Not a frame's edge, a door's frame, or a car's panel.)
        self.assertLeftAlone("A man enters from the right frame edge.")
        self.assertLeftAlone("The left frame of the door is painted blue.")
        self.assertLeftAlone("The car's left panel is dented.")

    def test_split_screens_composites_and_counted_pictures(self):
        self.assertCaught("A split-screen video captures a quiet road.", "split-screen")
        self.assertCaught("A split screen shows a highway drive.", "split screen")
        self.assertCaught("A composite of four frames shows a pier.", "composite of four frames")
        self.assertCaught("The four images show a dog.", "four images")
        self.assertCaught("Four pictures show a beach at dusk.", "Four pictures")
        self.assertCaught("Four sequential photos capture a bridge.", "Four sequential photos")
        # Real pictures, and pictures taken.
        self.assertLeftAlone("She takes the two photos and leaves.")
        self.assertLeftAlone("The two photos on the shelf show a wedding.")
        self.assertLeftAlone("Two photos hang above the desk.")

    def test_games_taking_and_real_things(self):
        # Shots in a game, pictures taken, and real panels.
        self.assertLeftAlone("His second shot lands on the green.")
        self.assertLeftAlone("The golfer's second shot lands short.")
        self.assertLeftAlone("The first shot misses the basket, the second shot goes in.")
        self.assertLeftAlone("A basketball player takes a series of shots at the hoop.")
        self.assertLeftAlone("A woman takes a series of photos of the waterfall.")
        self.assertLeftAlone("A collage of photos hangs on the living room wall.")
        self.assertLeftAlone("A needle moves along each panel of the quilt.")
        self.assertLeftAlone("He installs two panels of drywall.")
        # The sheet's own, still.
        self.assertCaught("The second shot looks up at the tower.", "second shot")
        self.assertCaught("The video's second shot pans to the left.", "second shot")
        self.assertCaught("A series of shots of a tram stop.", "series of shots")

    def test_what_every_frame_carries(self):
        # A dashcam's (or a camera's) overlay is in every frame of the video,
        # whatever the words; other things "in each frame" are the sheet's.
        self.assertLeftAlone("The dashboard displays GPS coordinates, time and speed in each "
                             "frame.")
        self.assertLeftAlone("A timestamp shows the date in each frame.")
        self.assertCaught("Reflections show on the glass in each frame. The overlay reads "
                          "45 mph.", "in each frame")
        self.assertCaught("A hat is visible in each frame.", "in each frame")

    def test_title_and_description_only(self):
        info = {"title": "Four frames of a pond", "description": "Ducks swim in each panel.",
                "tags": ["four frames"], "text": "nine panels"}
        self.assertEqual(sheet_talk.in_info(info), ["Four frames", "each panel"])
        self.assertEqual(sheet_talk.in_info({"title": "Ducks on a pond"}), [])
        self.assertEqual(sheet_talk.in_info(None), [])
        self.assertEqual(sheet_talk.find(None), [])

    def test_asked_again_and_unsettled(self):
        talk = {"title": "Pond", "description": "Four frames show ducks."}
        clean = {"title": "Pond", "description": "Ducks swim across a pond."}
        self.assertTrue(sheet_talk.unsettled(talk, {}))
        self.assertTrue(sheet_talk.unsettled(talk, {"reasked": "other"}))
        self.assertFalse(sheet_talk.unsettled(talk, {"reasked": "sheet"}))
        self.assertFalse(sheet_talk.unsettled(clean, {}))
        self.assertTrue(sheet_talk.was_reasked({"reasked": "sheet"}))
        self.assertFalse(sheet_talk.was_reasked(None))

    def test_the_sql_function(self):
        conn = sqlite3.connect(":memory:")
        sheet_talk.register(conn)
        talk = json.dumps({"title": "Pond", "description": "Four frames show ducks."})

        def ask(info, provenance):
            return conn.execute("SELECT %s(?, ?)" % sheet_talk.SQL_FUNCTION,
                                (info, provenance)).fetchone()[0]

        self.assertEqual(ask(talk, "{}"), 1)
        self.assertEqual(ask(talk, json.dumps({"reasked": "sheet"})), 0)
        self.assertEqual(ask(talk, '{"reasked": "sheet", broken'), 1)
        self.assertEqual(ask(json.dumps({"title": "Ducks on a pond"}), None), 0)
        self.assertEqual(ask("not json", None), 0)
        self.assertEqual(ask(None, None), 0)
        conn.close()

    def test_setting_and_note(self):
        self.assertTrue(sheet_talk.enabled({}))
        self.assertTrue(sheet_talk.enabled(None))
        self.assertTrue(sheet_talk.enabled({"video_sheet_check": True}))
        self.assertFalse(sheet_talk.enabled({"video_sheet_check": False}))
        note = sheet_talk.reask_note("video")
        self.assertIn("described the frames or panels", note)
        self.assertIn("one continuous scene", note)
        self.assertIn("one continuous trip", sheet_talk.reask_note("drive"))
        self.assertEqual(note.count(". "), 0)       # (one sentence)


class StaleSqlTest(unittest.TestCase):

    def rows(self, sql):
        conn = sqlite3.connect(":memory:")
        sheet_talk.register(conn)
        conn.execute("CREATE TABLE images (name TEXT, media TEXT, prompt_ver INTEGER, "
                     "info TEXT, provenance TEXT)")
        talk = json.dumps({"title": "Pond", "description": "Four frames show ducks."})
        clean = json.dumps({"title": "Pond", "description": "Ducks swim across a pond."})
        asked = json.dumps({"reasked": "sheet"})
        conn.executemany("INSERT INTO images VALUES (?, ?, ?, ?, ?)", [
            ("video talk", "video", 6, talk, "{}"),
            ("video talk asked again", "video", 6, talk, asked),
            ("video clean", "video", 6, clean, "{}"),
            ("video old", "video", 5, clean, "{}"),
            ("photo talk", "image", 6, talk, "{}"),
            ("photo current", "image", 5, clean, "{}"),
            ("photo old", "image", 4, clean, "{}"),
        ])
        found = [row[0] for row in conn.execute(
            "SELECT name FROM images WHERE %s ORDER BY rowid" % sql)]
        conn.close()
        return found

    def test_out_of_date(self):
        self.assertEqual(self.rows(prompts.stale_sql()),
                         ["video talk", "video old", "photo old"])
        self.assertEqual(self.rows(prompts.stale_sql({})), self.rows(prompts.stale_sql()))
        self.assertEqual(self.rows(prompts.stale_sql({"video_sheet_check": False})),
                         ["video old", "photo old"])
        self.assertEqual(prompts.stale_sql({"video_sheet_check": False}), prompts.STALE_SQL)


def reply(title, text, usage=(10, 20)):
    """A fake model's reply with this title and description."""
    return fake_llm.Reply(json.dumps(dict(fake_llm.DEFAULT_REPLY, title=title, description=text,
                                          photo_type="photo")), usage=usage)


# Four phrases of sheet talk, one, and none.
SHEET = ("Four frames of a dog in a yard",
         "Four sequential frames show a brown dog chasing a ball. The top-left frame shows it "
         "running; in the final frame it lies down.")
SHEET_ONE = ("Dog chasing a ball in a yard",
             "A brown dog chases a ball across a lawn; a fence shows in some frames.")
CLEAN = ("Dog chasing a ball in a yard",
         "A brown dog chases a ball across a lawn, then lies down in the grass.")


@unittest.skipIf(NO_PILLOW, "Pillow is not installed")
class VideoCase(test_video_library.VideoLibraryCase if test_video_library else object):
    """A library with some of a photo (cat.jpg), a clip (trip/IMG_0001.MOV) and an
    upright video (IMG_0002.MOV)."""

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(describe, "UNREACHABLE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self):
        return {"name": "local", "kind": "openai", "base_url": self.server.base_url,
                "api_key": "", "model": "fake-model"}

    def setup_library(self, keep=("clip",), **extra):
        files = {"photo": os.path.join(self.folder, "cat.jpg"), "clip": self.clip,
                 "upright": self.upright}
        for name, path in files.items():
            if name not in keep:
                os.remove(path)
        self.configure(endpoints=[self.endpoint()], default_endpoint="local", **extra)
        self.scan()

    def describe(self, **kwargs):
        self.ctx = jobs.JobContext("describe", log_limit=5000)
        conn = db.connect()
        try:
            return describe.describe_pending(conn, self.config, self.ctx,
                                             sleep=lambda seconds: None, **kwargs)
        finally:
            conn.close()

    def video(self, path="trip/IMG_0001.MOV"):
        row = self.rows()[path]
        return row, json.loads(row["info"] or "{}"), json.loads(row["provenance"] or "{}")

    def stale(self, config=None):
        conn = db.connect()
        try:
            counts = db.library_counts(conn, stale_sql=prompts.stale_sql(
                self.config if config is None else config))
            return counts["lib_family1"]["stale"]
        finally:
            conn.close()


class AskAgainTest(VideoCase):

    def test_sheet_talk_is_asked_about_again_and_fixed(self):
        self.setup_library()
        self.server.queue(reply(*SHEET, usage=(100, 40)), reply(*CLEAN, usage=(110, 30)))
        summary = self.describe()
        first, second = self.server.describe_requests()
        # The same picture, the same prompt with one sentence more.
        self.assertEqual(second["image"], first["image"])
        self.assertEqual(second["prompt"],
                         first["prompt"] + "\n\n" + sheet_talk.reask_note("video"))
        self.assertEqual(second["system"], first["system"])
        _row, info, provenance = self.video()
        self.assertEqual((info["title"], info["description"]), CLEAN)
        self.assertEqual(provenance["reasked"], "sheet")
        self.assertEqual(provenance["reask_kept"], "second")
        self.assertNotIn("sheet_talk", provenance)
        # Both requests count.
        self.assertEqual(provenance["requests"], 2)
        self.assertEqual((provenance["tokens_in"], provenance["tokens_out"]), (210, 70))
        self.assertEqual((summary["requests"], summary["tokens_in"]), (2, 210))
        self.assertEqual((summary["reasked"], summary["reask_fixed"]), (1, 1))
        log = self.log_text()
        self.assertIn("1 video description(s) asked again because they described the frames; "
                      "1 fixed.", log)
        self.assertIn('it described the frames ("Four frames", "Four sequential frames"', log)
        self.assertEqual(self.stale(), 0)

    def test_both_answers_talk_about_the_frames(self):
        self.setup_library()
        # The second answer talks less about them: it is kept.
        self.server.queue(reply(*SHEET), reply(*SHEET_ONE))
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), 2)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], SHEET_ONE[0])
        self.assertEqual((provenance["reasked"], provenance["reask_kept"]), ("sheet", "second"))
        self.assertEqual(provenance["sheet_talk"], ["some frames"])
        self.assertEqual((summary["reasked"], summary["reask_fixed"]), (1, 0))
        self.assertIn("still about the frames; kept the second answer", self.log_text())
        # Asked again once: current, even though it still talks about them.
        self.assertEqual(self.stale(), 0)
        before = len(self.server.describe_requests())
        self.describe(redescribe_stale=True)
        self.assertEqual(len(self.server.describe_requests()), before)

    def test_a_tie_keeps_the_first_answer(self):
        self.setup_library()
        self.server.queue(reply(*SHEET_ONE), reply("Dog in a yard",
                                                   "A dog runs; grass shows in all frames."))
        self.describe()
        _row, info, provenance = self.video()
        self.assertEqual(info["description"], SHEET_ONE[1])
        self.assertEqual((provenance["reasked"], provenance["reask_kept"]), ("sheet", "first"))
        self.assertEqual(provenance["sheet_talk"], ["some frames"])
        self.assertIn("kept the first answer", self.log_text())

    def test_a_clean_answer_takes_one_request(self):
        self.setup_library()
        self.server.queue(reply(*CLEAN))
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), 1)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], CLEAN[0])
        self.assertNotIn("reasked", provenance)
        self.assertEqual(provenance["requests"], 1)
        self.assertEqual((summary["reasked"], summary["reask_fixed"]), (0, 0))
        self.assertNotIn("asked again", self.log_text())

    def test_the_second_request_waits_out_a_rate_limit(self):
        self.setup_library()
        # (The first request is rate limited too, and retried, as always.)
        self.server.queue(fake_llm.HttpError(429), reply(*SHEET), fake_llm.HttpError(429),
                          reply(*CLEAN))
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), 4)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], CLEAN[0])
        self.assertEqual((provenance["reasked"], provenance["reask_kept"]), ("sheet", "second"))
        self.assertEqual(provenance["requests"], 4)
        self.assertEqual((summary["reasked"], summary["reask_fixed"], summary["reask_failed"]),
                         (1, 1, 0))
        self.assertEqual(self.stale(), 0)

    def test_a_failed_second_request_keeps_the_first_answer(self):
        self.setup_library()
        retries = mock.patch.object(describe, "MAX_RETRIES", 1)
        retries.start()
        self.addCleanup(retries.stop)
        self.server.queue(reply(*SHEET), fake_llm.HttpError(500), fake_llm.HttpError(500),
                          reply(*CLEAN))
        summary = self.describe()
        # The second request is retried like any other, then given up on.
        self.assertEqual(len(self.server.describe_requests()), 3)
        self.assertEqual(summary["described"], 1)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], SHEET[0])
        self.assertEqual(provenance["requests"], 3)
        self.assertNotIn("reasked", provenance)
        # Not counted as asked again, but as could not be.
        self.assertEqual((summary["reasked"], summary["reask_fixed"], summary["reask_failed"]),
                         (0, 0, 1))
        log = self.log_text()
        self.assertIn("the second request failed", log)
        self.assertIn("1 video description(s) described the frames but could not be asked "
                      "again (the second request failed): they stay out of date.", log)
        self.assertNotIn("asked again because they described the frames", log)
        # Not asked again for real: still out of date.
        self.assertEqual(self.stale(), 1)

    def test_an_incomplete_second_answer_does_not_replace_a_complete_first(self):
        self.setup_library()
        # The second answer has no sheet talk but was cut off before its tags.
        text = json.dumps(dict(fake_llm.DEFAULT_REPLY, title=CLEAN[0], description=CLEAN[1],
                               photo_type="photo"))
        self.server.queue(reply(*SHEET_ONE),
                          fake_llm.Reply(text[:text.index('"tags"')], finish="length"))
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), 2)
        _row, info, provenance = self.video()
        self.assertEqual(info["description"], SHEET_ONE[1])
        self.assertEqual(info["tags"], fake_llm.DEFAULT_REPLY["tags"])
        self.assertIs(info["private_info"], False)
        self.assertEqual((provenance["reasked"], provenance["reask_kept"]), ("sheet", "first"))
        self.assertNotIn("truncated", provenance)
        self.assertNotIn("missing_fields", provenance)
        self.assertEqual((summary["reasked"], summary["reask_fixed"]), (1, 0))
        self.assertIn("the second answer was incomplete; kept the first answer",
                      self.log_text())

    def test_a_complete_second_answer_replaces_an_incomplete_first(self):
        self.setup_library()
        text = json.dumps(dict(fake_llm.DEFAULT_REPLY, title=SHEET_ONE[0],
                               description=SHEET_ONE[1], photo_type="photo"))
        # The first is cut off before its tags (used as it is: nothing loops);
        # the second is complete, with as much sheet talk.
        self.server.queue(fake_llm.Reply(text[:text.index('"tags"')], finish="length"),
                          reply("Dog in a yard", "A dog runs; grass shows in all frames."))
        self.describe()
        self.assertEqual(len(self.server.describe_requests()), 2)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], "Dog in a yard")
        self.assertEqual(info["tags"], fake_llm.DEFAULT_REPLY["tags"])
        self.assertEqual(provenance["reask_kept"], "second")
        self.assertNotIn("truncated", provenance)

    def test_a_reply_the_model_could_not_answer_counts_as_asked(self):
        self.setup_library()
        self.server.queue(reply(*SHEET), fake_llm.Reply("not json at all"))
        self.describe()
        self.assertEqual(len(self.server.describe_requests()), 2)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], SHEET[0])
        self.assertEqual((provenance["reasked"], provenance["reask_kept"]), ("sheet", "first"))
        self.assertEqual(self.stale(), 0)

    def test_the_setting_turns_it_off(self):
        self.setup_library(video_sheet_check=False)
        self.server.queue(reply(*SHEET))
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), 1)
        _row, info, provenance = self.video()
        self.assertEqual(info["title"], SHEET[0])
        self.assertNotIn("reasked", provenance)
        self.assertEqual(summary["reasked"], 0)
        # Nor is it out of date while the check is off.
        self.assertEqual(self.stale(), 0)
        self.assertEqual(self.stale({}), 1)

    def test_describing_one_picture_again_checks_it_too(self):
        self.setup_library()
        self.describe()
        row, _info, _provenance = self.video()
        self.server.queue(reply(*SHEET), reply(*CLEAN))
        before = len(self.server.describe_requests())
        summary = self.describe(ids=[row["id"]], force=True)
        self.assertEqual(len(self.server.describe_requests()), before + 2)
        self.assertEqual(summary["reasked"], 1)
        self.assertEqual(self.video()[2]["reasked"], "sheet")

    def test_pictures_are_never_asked_again(self):
        self.setup_library(keep=("photo",))
        self.server.reply = reply(*SHEET)
        summary = self.describe()
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.assertEqual(summary["reasked"], 0)


class OutOfDateTest(VideoCase):

    def set_description(self, path, info, provenance, prompt_ver=None):
        self.execute("UPDATE images SET info = ?, provenance = ?, described = 1, "
                     "prompt_ver = ? WHERE path = ?",
                     (json.dumps(dict(fake_llm.DEFAULT_REPLY, **info)), json.dumps(provenance),
                      prompts.PROMPT_VER if prompt_ver is None else prompt_ver, path))

    def setup_rows(self, **extra):
        self.setup_library(keep=("photo", "clip", "upright"), **extra)
        talk = {"title": SHEET[0], "description": SHEET[1]}
        self.set_description("trip/IMG_0001.MOV", talk, {"model": "m"})
        self.set_description("IMG_0002.MOV", talk, {"model": "m", "reasked": "sheet"})
        self.set_description("cat.jpg", talk, {"model": "m"})

    def test_counts_pending_and_progress_agree(self):
        self.setup_rows()
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(self.stale(), 1)
        # (The counts by prompt version alone, as before.)
        self.assertEqual(db.library_counts(conn, prompts.CURRENT_SINCE)["lib_family1"]["stale"],
                         0)
        libraries = self.config["libraries"]
        self.assertEqual(describe.stale_count(conn, libraries, prompts.stale_sql(self.config)),
                         1)
        rows, _skipped = describe.pending_rows(conn, libraries, redescribe_stale=True,
                                               stale_sql=prompts.stale_sql(self.config))
        self.assertEqual([row["path"] for row in rows], ["trip/IMG_0001.MOV"])
        rows, _skipped = describe.pending_rows(conn, libraries)
        self.assertEqual(rows, [])
        (_label, counts), = progress.library_rows(conn, self.config)
        self.assertEqual(counts["stale"], 1)
        (_label, row), = progress.pending_rows(conn, self.config)
        self.assertEqual((row["path"], row["older"]), ("trip/IMG_0001.MOV", 0))
        with mock.patch("builtins.print") as printed:
            progress.show_files(conn, self.config)
        self.assertIn("(the description talks about the frames; run scan.py "
                      "--redescribe-stale)", printed.call_args[0][0])
        # A run that does not redescribe says how many are out of date.
        self.describe()
        self.assertIn("1 image(s) have out-of-date descriptions", self.log_text())

    def test_the_manage_page_counts_them(self):
        try:
            import serve
            import admin
        except ImportError:
            self.skipTest("Flask is not installed")
        self.setup_rows()
        manager = jobs.JobManager(poll_interval=0.05, idle_interval=0.05)
        self.addCleanup(manager.stop, 30)
        app = serve.build_app(init_db=False, job_manager=manager)
        app.testing = True
        self.addCleanup(app.extensions["search_images"].close)
        client = app.test_client()
        headers = {"X-Search-Images": "1"}
        state = client.get(admin.URL_PREFIX + "/state", headers=headers).get_json()
        self.assertEqual(state["libraries"][0]["counts"]["stale"], 1)
        self.assertIs(state["videos"]["sheet_check"], True)
        # The library card a library change answers with counts them the same.
        answer = client.post(admin.URL_PREFIX + "/libraries/lib_family1",
                             data=json.dumps({"name": "Family videos"}),
                             content_type="application/json", headers=headers)
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        self.assertEqual(answer.get_json()["library"]["counts"]["stale"], 1)

    def test_the_cloud_question_mentions_the_second_request(self):
        try:
            import admin
        except ImportError:
            self.skipTest("Flask is not installed")
        self.assertIs(admin.videos_view({})["sheet_check"], True)
        self.assertIs(admin.videos_view({"video_sheet_check": False})["sheet_check"], False)
        folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(folder, "static", "manage.js"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("state.videos.sheet_check === false", script)
        self.assertIn("frames instead of the video is asked once more, so a video can cost up to",
                      script)

    def test_a_removed_library_counts_them(self):
        import library_ops
        self.setup_rows()
        conn = db.connect()
        self.addCleanup(conn.close)
        config = dict(self.config, libraries=[])
        (orphan,) = library_ops.orphans(conn, config)
        self.assertEqual((orphan["id"], orphan["counts"]["stale"]), ("lib_family1", 1))
        (orphan,) = library_ops.orphans(conn, dict(config, video_sheet_check=False))
        self.assertEqual(orphan["counts"]["stale"], 0)

    def test_exact_copies_are_settled_together(self):
        self.setup_rows()
        # IMG_0002.MOV is an exact copy of the clip, not asked again either.
        clip = self.rows()["trip/IMG_0001.MOV"]
        self.execute("UPDATE images SET content_hash = ?, duration = ?, exif = ?, "
                     "provenance = ? WHERE path = ?",
                     (clip["content_hash"], clip["duration"], clip["exif"],
                      json.dumps({"model": "m"}), "IMG_0002.MOV"))
        self.assertEqual(self.stale(), 2)
        # (One at a time: the copy waits until the first is described.)
        self.config["endpoints"][0]["concurrency"] = 1
        self.server.queue(reply(*SHEET), reply(*CLEAN))
        summary = self.describe(redescribe_stale=True)
        # Described (and asked again) once; the copy shares the answer.
        self.assertEqual(len(self.server.describe_requests()), 2)
        self.assertEqual((summary["described"], summary["shared"]), (1, 1))
        for path in ("trip/IMG_0001.MOV", "IMG_0002.MOV"):
            _row, info, provenance = self.video(path)
            self.assertEqual(info["title"], CLEAN[0], path)
            self.assertEqual(provenance["reasked"], "sheet", path)
        self.assertEqual(self.stale(), 0)

    def test_redescribing_out_of_date_asks_once_and_settles(self):
        self.setup_rows()
        self.server.reply = reply(*SHEET)
        summary = self.describe(redescribe_stale=True)
        # Only the video that was not asked again: two requests, both bad.
        self.assertEqual(len(self.server.describe_requests()), 2)
        self.assertEqual((summary["described"], summary["reasked"]), (1, 1))
        _row, _info, provenance = self.video()
        self.assertEqual(provenance["reasked"], "sheet")
        self.assertEqual(self.stale(), 0)
        self.describe(redescribe_stale=True)
        self.assertEqual(len(self.server.describe_requests()), 2)

    def test_with_the_check_off_nothing_is_out_of_date(self):
        self.setup_rows(video_sheet_check=False)
        self.assertEqual(self.stale(), 0)
        conn = db.connect()
        self.addCleanup(conn.close)
        (_label, counts), = progress.library_rows(conn, self.config)
        self.assertEqual(counts["stale"], 0)
        self.describe(redescribe_stale=True)
        self.assertEqual(self.server.describe_requests(), [])

    def test_with_the_check_off_copies_keep_their_description(self):
        self.setup_rows(video_sheet_check=False)
        # The clip is out of date by its prompt; IMG_0002.MOV, an exact copy,
        # has sheet talk, which does not count while the check is off.
        clip = self.rows()["trip/IMG_0001.MOV"]
        self.execute("UPDATE images SET prompt_ver = 5 WHERE id = ?", (clip["id"],))
        self.execute("UPDATE images SET content_hash = ?, provenance = ? WHERE path = ?",
                     (clip["content_hash"], json.dumps({"model": "m"}), "IMG_0002.MOV"))
        self.server.queue(reply(*CLEAN))
        summary = self.describe(redescribe_stale=True)
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.assertEqual((summary["described"], summary["shared"]), (1, 0))
        self.assertEqual(self.video()[1]["title"], CLEAN[0])
        self.assertEqual(self.video("IMG_0002.MOV")[1]["title"], SHEET[0])


@unittest.skipIf(NO_PILLOW, "Pillow is not installed")
class DriveTest(test_drives.DriveLibraryCase if test_drives else object):

    def setUp(self):
        super().setUp()
        self.server = fake_llm.FakeLLM().start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.object(describe, "UNREACHABLE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self):
        return {"name": "local", "kind": "openai", "base_url": self.server.base_url,
                "api_key": "", "model": "fake-model"}

    def describe_drives(self, **kwargs):
        self.ctx = jobs.JobContext("describe", log_limit=5000)
        return drives.describe_drives(self.conn(), self.config, self.ctx,
                                      self.config["libraries"], sleep=lambda seconds: None,
                                      **kwargs)

    def test_a_drive_that_talks_about_its_frames_is_asked_again(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        talk = ("Highway drive", "A sequence of dashcam footage captures a highway drive.")
        self.server.queue(reply(*talk), reply("Highway drive", "A car drives along a highway."))
        summary = self.describe_drives()
        sent = self.server.describe_requests()
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1]["prompt"], sent[0]["prompt"] + "\n\n" +
                         sheet_talk.reask_note("drive"))
        self.assertEqual(sent[1]["image"], sent[0]["image"])
        self.assertEqual((summary["described"], summary["requests"]), (2, 3))
        self.assertEqual((summary["reasked"], summary["reask_fixed"]), (1, 1))
        self.assertIn("1 drive description(s) asked again because they described the frames; "
                      "1 fixed.", self.log_text())
        entries = [drives.entry_for("lib_trip1", drive) for drive in self.found()]
        asked = [entry for entry in entries if entry["provenance"].get("reasked")]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["info"]["description"], "A car drives along a highway.")
        self.assertEqual(asked[0]["provenance"]["requests"], 2)
        self.assertEqual(drives.pending(self.conn(), self.config, self.config["libraries"]), [])

    def test_old_drive_descriptions_that_talk_about_frames_are_described_again(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        first, second = self.found()
        talk = {"title": "Drive", "description": "Nine panels show a drive."}
        drives.save_description("lib_trip1", first, talk, {"prompt_ver": prompts.PROMPT_VER})
        drives.save_description("lib_trip1", second, talk,
                                {"prompt_ver": prompts.PROMPT_VER, "reasked": "sheet"})
        libraries = self.config["libraries"]
        # Out of date, as a video's: only a run that re-describes those
        # describes it again.
        self.assertEqual(drives.pending(self.conn(), self.config, libraries), [])
        self.assertEqual(self.describe_drives()["pending"], 0)
        work = drives.pending(self.conn(), self.config, libraries, redescribe_stale=True)
        self.assertEqual([drive.id for _library, drive in work], [first.id])
        self.assertEqual(drives.pending(self.conn(), dict(self.config, video_sheet_check=False),
                                        libraries, redescribe_stale=True), [])
        summary = self.describe_drives(redescribe_stale=True)
        self.assertEqual(summary["described"], 1)
        self.assertEqual(len(self.server.describe_requests()), 1)
        self.assertEqual(drives.description("lib_trip1", first)["title"],
                         fake_llm.DEFAULT_REPLY["title"])

    def test_a_drive_whose_second_request_failed_waits_for_redescribe(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local")
        self.scan()
        retries = mock.patch.object(describe, "MAX_RETRIES", 0)
        retries.start()
        self.addCleanup(retries.stop)
        talk = ("Highway drive", "A sequence of dashcam footage captures a highway drive.")
        # One drive talks about its frames and its second request fails; the
        # other is clean.
        self.server.queue(reply(*talk), fake_llm.HttpError(429), reply("Drive", "A drive."))
        summary = self.describe_drives()
        self.assertEqual((summary["described"], summary["requests"]), (2, 3))
        self.assertEqual((summary["reasked"], summary["reask_failed"]), (0, 1))
        log = self.log_text()
        self.assertIn("the second request failed", log)
        self.assertIn("1 drive description(s) described the frames but could not be asked "
                      "again (the second request failed): they stay out of date.", log)
        libraries = self.config["libraries"]
        # Not described again by every run (no endpoint is asked) ...
        self.assertEqual(drives.pending(self.conn(), self.config, libraries), [])
        again = self.describe_drives()
        self.assertEqual((again["pending"], again["requests"]), (0, 0))
        self.assertEqual(len(self.server.describe_requests()), 3)
        # ... but by Re-describe out of date.
        self.assertEqual(len(drives.pending(self.conn(), self.config, libraries,
                                            redescribe_stale=True)), 1)
        self.server.queue(reply("Highway drive", "A car drives along a highway."))
        summary = self.describe_drives(redescribe_stale=True)
        self.assertEqual((summary["described"], summary["requests"]), (1, 1))
        self.assertEqual(drives.pending(self.conn(), self.config, libraries,
                                        redescribe_stale=True), [])

    def test_the_setting_turns_it_off_for_drives(self):
        self.configure(endpoints=[self.endpoint()], default_endpoint="local",
                       video_sheet_check=False)
        self.scan()
        self.server.reply = reply("Drive", "Nine panels show a drive.")
        summary = self.describe_drives()
        self.assertEqual(len(self.server.describe_requests()), 2)
        self.assertEqual(summary["reasked"], 0)


if __name__ == "__main__":
    unittest.main()
