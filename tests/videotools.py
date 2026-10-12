"""Helpers for the video tests: the fake ffmpeg (fake_ffmpeg.py) as videos.py's tools.

    class MyTest(videotools.FakeToolsMixin, support.TempBaseMixin, unittest.TestCase):
        def setUp(self):
            super().setUp()
            self.use_fake_tools()           # in the test's process (fast)

use_fake_tools(subprocess=True) runs the fake as a real program instead;
available=False makes ffmpeg missing. Every command line videos.py runs is
in self.commands. clip_probe() and portrait_probe() are ffprobe answers
shaped like a phone's (the values are made up).
"""

import os
import sys
from unittest import mock

import fake_ffmpeg
import videos

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_ffmpeg.py")


def fake_tools():
    """videos.Tools that run fake_ffmpeg.py with this Python."""
    return videos.Tools([sys.executable, FAKE, "ffmpeg"], [sys.executable, FAKE, "ffprobe"])


def clip_probe(duration=24.0, **changes):
    """A landscape H.264 phone video: local date with offset, a position, the phone's name."""
    tags = {"com.apple.quicktime.creationdate": "2021-07-04T11:30:15-0700",
            "com.apple.quicktime.location.ISO6709": "+47.6062-122.3321+010.000/",
            "com.apple.quicktime.make": "Acme", "com.apple.quicktime.model": "Phone 9"}
    tags.update(changes.pop("format_tags", {}))
    return fake_ffmpeg.probe_json(duration=duration, format_tags=tags, **changes)


def portrait_probe(duration=8.0, **changes):
    """An upright HEVC HDR (HLG) video: stored sideways, turned by its display matrix."""
    changes.setdefault("rotation", -90)
    return fake_ffmpeg.probe_json(duration=duration, codec="hevc", pix_fmt="yuv420p10le",
                                  transfer="arib-std-b67", **changes)


class FakeToolsMixin:
    """Make videos.py use the fake ffmpeg (see the module notes)."""

    def use_fake_tools(self, subprocess=False, available=True):
        tools = fake_tools() if available else None
        self.tools = tools
        self.commands = []
        real_run = videos._run

        def run(argv, timeout):
            self.commands.append(list(argv))
            if not subprocess and len(argv) > 2 and argv[1] == FAKE:
                return fake_ffmpeg.run(argv[2:])
            return real_run(argv, timeout)

        for patcher in (mock.patch.object(videos, "_run", run),
                        mock.patch.object(videos, "find_tools",
                                          lambda config=None: tools)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return tools

    def make_video(self, folder, name, probe, **extra):
        path = os.path.join(folder, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return fake_ffmpeg.make_video(path, probe, **extra)
