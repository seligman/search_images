"""The bundled sample pictures (sample/images; see its CREDITS.md and README.md).

Read as a scan reads them (images.read_image): every picture keeps the
date it has for the demo's timeline, with no time zone and no GPS time --
no UTC moment, so the local times (local_times.py) never move it; the
pictures whose source page gives a position (a geotag) carry it as EXIF GPS
with its accuracy (GPSHPositioningError; 19 the camera's own, without one),
and those whose page names only a town, a region or a country carry its
names in their XMP location fields: no position, so no pin that would look
more exact than the page is. 11 carries both: its geotag, and the village
its page names (its place names are the file's, see
place_estimates.gps_file_columns).
"""

import os
import unittest

import support

from PIL import Image

import images

SAMPLES = os.path.join(support.REPO, "sample", "images")

# The dates (local time) of the demo's timeline: 01-18 made up (CREDITS.md), 19 its own.
DATES = {
    "01-cat.jpg": "2026-02-08T09:41:15", "02-dog.jpg": "2026-05-23T13:52:19",
    "03-mountain-lake.jpg": "2026-05-24T10:29:57", "04-beach.jpg": "2026-04-08T15:48:09",
    "05-coffee.jpg": "2026-04-06T09:03:52", "06-sunflower.jpg": "2026-06-27T17:38:02",
    "07-bicycle.jpg": "2026-03-14T15:32:36", "08-autumn-forest.jpg": "2026-09-27T17:52:14",
    "09-city-skyline.jpg": "2026-02-14T20:37:08", "10-fruit.jpg": "2026-01-01T10:18:44",
    "11-butterfly.jpg": "2026-06-07T11:15:03", "12-hot-air-balloon.jpg": "2026-04-04T07:18:26",
    "13-lighthouse.jpg": "2026-07-11T20:48:31", "14-train.jpg": "2026-01-17T13:26:27",
    "15-vegetables.jpg": "2026-04-07T10:46:21", "16-penguin.jpg": "2026-01-24T14:05:18",
    "17-stop-sign.jpg": "2026-03-21T16:05:49", "18-chalkboard-menu.jpg": "2026-04-02T12:21:37",
    "19-roys-motel-360.jpg": "2026-04-05T12:43:59",
}
# file: (lat, lon, GPSHPositioningError in metres; None: not stated)
POSITIONS = {
    "03-mountain-lake.jpg": (47.785, -121.571, 100),
    "09-city-skyline.jpg": (51.109746, -114.084777, 400),
    "11-butterfly.jpg": (46.074123, 9.050502, 100),
    "13-lighthouse.jpg": (57.581848, 9.936811, 200),
    "18-chalkboard-menu.jpg": (48.518324, -122.612679, 200),
    "19-roys-motel-360.jpg": (34.558582, -115.743331, None),
}
PLACES = {
    "02-dog.jpg": {"city": "Cottonwood", "region": "Idaho", "country": "United States",
                   "cc": "US"},
    "04-beach.jpg": {"city": "Alnmouth", "region": "England", "country": "United Kingdom",
                     "cc": "GB"},
    "05-coffee.jpg": {"city": "Olinda", "region": "Victoria", "country": "Australia",
                      "cc": "AU"},
    "16-penguin.jpg": {"country": "Antarctica", "cc": "AQ"},
    # (A geotag a few km from Italy, and the village its page names: the names win
    # over the nearest town of the place names, which is Italian.)
    "11-butterfly.jpg": {"city": "Cimadera", "region": "Ticino", "country": "Switzerland",
                         "cc": "CH"},
}
GPS_TIME_TAGS = (0x0007, 0x001D)        # GPSTimeStamp, GPSDateStamp


class SampleImagesTest(unittest.TestCase):

    def test_dates_and_locations(self):
        names = sorted(name for name in os.listdir(SAMPLES) if name.endswith(".jpg"))
        self.assertEqual(names, sorted(DATES))
        for name in names:
            with self.subTest(name=name):
                path = os.path.join(SAMPLES, name)
                result = images.read_image(path, want_preview=False)
                self.assertEqual((result["taken_at"], result["taken_source"],
                                  result["tz_offset"]), (DATES[name], "exif", ""))
                gps = result["exif"].get("GPS") or {}
                if name in POSITIONS:
                    lat, lon, metres = POSITIONS[name]
                    self.assertAlmostEqual(result["lat"], lat, places=6)
                    self.assertAlmostEqual(result["lon"], lon, places=6)
                    self.assertEqual(gps.get("GPSHPositioningError"), metres)
                else:
                    self.assertIsNone(result["lat"])
                    self.assertEqual(gps, {})
                self.assertEqual(result["exif"].get("XMPPlace"), PLACES.get(name))
                if name != "19-roys-motel-360.jpg":
                    # (The photographer's own GPS time is 19's; the others have none.)
                    with Image.open(path) as image:
                        tags = image.getexif().get_ifd(0x8825)
                    self.assertFalse(set(GPS_TIME_TAGS) & set(tags), sorted(tags))
                    self.assertNotIn("OffsetTimeOriginal", result["exif"])


if __name__ == "__main__":
    unittest.main()
