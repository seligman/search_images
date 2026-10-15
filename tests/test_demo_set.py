"""The demo set (sample/demo-set): its manifest, its credits and fetch_demo_set.py.

Nothing here uses the network: downloads go to a stand-in for urllib's urlopen, and
the copy step runs on small pictures made in the test, read back as a scan reads
them (images.read_image).
"""

import contextlib
import email.message
import hashlib
import importlib.util
import io
import json
import os
import re
import ssl
import subprocess
import unittest
import urllib.error
from unittest import mock

import support

from PIL import Image

import images

DEMO = os.path.join(support.REPO, "sample", "demo-set")
MANIFEST = os.path.join(DEMO, "manifest.json")
CREDITS = os.path.join(DEMO, "CREDITS.md")


def _load_fetch():
    spec = importlib.util.spec_from_file_location(
        "fetch_demo_set", os.path.join(DEMO, "fetch_demo_set.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load_fetch()

LICENCES = {
    "CC0 1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "Public Domain Mark 1.0": "https://creativecommons.org/publicdomain/mark/1.0/",
    "CC BY 2.0": "https://creativecommons.org/licenses/by/2.0/",
    "CC BY 3.0": "https://creativecommons.org/licenses/by/3.0/",
    "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
}
GROUPS = ("trip", "videos", "bursts", "text", "varied")
ITEM_FIELDS = {"file", "group", "title", "creator", "credit", "licence", "licence_url",
               "source_page", "file_url", "sha256", "bytes", "copy", "copy_bytes"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_RE = re.compile(r"^(trip|videos|bursts|text|varied)-(\d\d)-[a-z0-9]+(?:-[a-z0-9]+)*"
                     r"\.(jpg|mp4)$")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_manifest():
    with open(MANIFEST, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Pictures made for the tests

def picture(size=(64, 48), exif=None, icc=None, xmp=None, quality=90):
    """JPEG bytes of a small picture (a colour ramp), with EXIF (a PIL.Image.Exif),
    an ICC profile and an XMP packet (bytes) when given."""
    width, height = size
    image = Image.new("RGB", size)
    image.putdata([(x * 255 // max(1, width - 1), y * 255 // max(1, height - 1), 90)
                   for y in range(height) for x in range(width)])
    options = {"quality": quality}
    if exif is not None:
        options["exif"] = exif.tobytes()
    if icc is not None:
        options["icc_profile"] = icc
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", **options)
    data = buffer.getvalue()
    if xmp is not None:
        payload = fetch.XMP_HEADER + xmp
        data = (data[:2] + b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
                + data[2:])
    return data


def camera_exif(date=None, void_gps=False):
    """EXIF as a camera might write it: a make, a model, an artist in UTF-8 (not ASCII),
    a date when given, and a GPS directory holding only "no position" (V) when void_gps."""
    exif = Image.Exif()
    exif[0x010F] = "Testmaker"
    exif[0x0110] = "Testcam 1"
    exif[0x013B] = "Jakub Ha\u0142un".encode("utf-8")
    exif[0x8769] = {0x829A: fetch.TiffImagePlugin.IFDRational(1, 250)}
    if date:
        exif[0x8769][0x9003] = date
    if void_gps:
        exif[0x8825] = {0x09: "V", 0x12: "WGS-84"}
    return exif


XMP_PACKET = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
    'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
    'xmlns:xmp="http://ns.adobe.com/xap/1.0/" photoshop:City="Oldtown" '
    'xmp:CreatorTool="Testtool"><photoshop:Country>Oldland</photoshop:Country>'
    '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>').encode("utf-8")


class TempFiles(support.TempBaseMixin):

    def write(self, name, data):
        return self.base.write_file("work/" + name, data)

    def read_back(self, data, name="copy.jpg"):
        """What a scan reads from a picture (images.read_image)."""
        return images.read_image(self.write(name, data), want_preview=False)


# ---------------------------------------------------------------------------
# The manifest and the credits

class ManifestTest(unittest.TestCase):

    def setUp(self):
        self.manifest = load_manifest()
        self.items = self.manifest["items"]

    def test_items_have_their_fields(self):
        self.assertEqual(list(self.manifest["groups"]), list(GROUPS))
        for item in self.items:
            with self.subTest(file=item.get("file")):
                self.assertLessEqual(ITEM_FIELDS, set(item))
                self.assertLessEqual(set(item), ITEM_FIELDS | {"copy_sha256"})
                for key in ("file", "group", "title", "creator", "credit"):
                    self.assertIsInstance(item[key], str)
                    self.assertEqual(item[key], item[key].strip())
                    self.assertTrue(item[key])
                self.assertIn(item["group"], GROUPS)
                self.assertIn(item["licence"], LICENCES)
                self.assertEqual(item["licence_url"], LICENCES[item["licence"]])
                for key in ("licence_url", "source_page", "file_url"):
                    self.assertTrue(item[key].startswith("https://"), item[key])
                    self.assertNotRegex(item[key], r"\s")
                self.assertRegex(item["sha256"], SHA256_RE)
                for key in ("bytes", "copy_bytes"):
                    self.assertIsInstance(item[key], int)
                    self.assertGreater(item[key], 0)
                self.assertIsInstance(item["copy"], dict)

    def test_file_names(self):
        names = [item["file"] for item in self.items]
        self.assertEqual(len(names), len(set(names)))
        for group in GROUPS:
            mine = [item for item in self.items if item["group"] == group]
            for number, item in enumerate(mine, 1):
                with self.subTest(file=item["file"]):
                    match = FILE_RE.match(item["file"])
                    self.assertTrue(match, item["file"])
                    self.assertEqual((match.group(1), int(match.group(2))), (group, number))
                    self.assertEqual(match.group(3) == "mp4", group == "videos")
                    self.assertEqual(fetch.is_video(item), group == "videos")
                    fetch.original_name(item)           # (a known file type)

    def test_counts_and_totals(self):
        self.assertEqual(len(self.items), 44)
        self.assertEqual(sum(1 for item in self.items if item["group"] == "videos"), 5)
        self.assertEqual(self.manifest["originals_total_bytes"],
                         sum(item["bytes"] for item in self.items))
        self.assertEqual(self.manifest["copies_total_bytes"],
                         sum(item["copy_bytes"] for item in self.items))

    def test_copy_instructions(self):
        for item in self.items:
            copy = item["copy"]
            with self.subTest(file=item["file"]):
                self.assertLessEqual(set(copy), fetch.COPY_KEYS)
                video_keys = {"transcode", "video_date", "video_location"}
                if item["group"] == "videos":
                    self.assertIs(copy.get("transcode"), True)
                    self.assertLessEqual(set(copy), video_keys)
                else:
                    self.assertFalse(set(copy) & video_keys)
                if not copy:
                    # (Unchanged: the copy is the original, byte for byte.)
                    self.assertEqual(item.get("copy_sha256"), item["sha256"])
                    self.assertEqual(item["copy_bytes"], item["bytes"])
                else:
                    self.assertNotIn("copy_sha256", item)
                if "max_edge" in copy:
                    self.assertIsInstance(copy["max_edge"], int)
                if "exif_date" in copy:
                    self.assertRegex(copy["exif_date"],
                                     r"^[0-9]{4}:[0-9]{2}:[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")
                if "exif_offset" in copy:
                    self.assertRegex(copy["exif_offset"], r"^[+-][0-9]{2}:[0-9]{2}$")
                if "gps" in copy:
                    gps = copy["gps"]
                    self.assertLessEqual(set(gps), {"lat", "lon", "accuracy_m"})
                    self.assertTrue(-90 <= gps["lat"] <= 90 and -180 <= gps["lon"] <= 180)
                    if "accuracy_m" in gps:
                        self.assertIsInstance(gps["accuracy_m"], int)
                if copy.get("remove_gps_void"):
                    self.assertIn("gps", copy)
                if "xmp_place" in copy:
                    self.assertLessEqual(set(copy["xmp_place"]),
                                         {"City", "State", "Country", "CountryCode"})
                    if "CountryCode" in copy["xmp_place"]:
                        self.assertRegex(copy["xmp_place"]["CountryCode"], r"^[A-Z]{2}$")
                if "video_date" in copy:
                    self.assertRegex(copy["video_date"],
                                     r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                                     r"[+-][0-9]{4}$")

    def test_what_the_demo_did(self):
        # (The copies the demo used: two pictures with passers-by made smaller, the
        # fox's "no position" status gone with its geotag, the videos copied, and a
        # misspelt region in the photographer's own XMP of two pictures corrected.)
        copies = {item["file"]: item["copy"] for item in self.items}
        self.assertEqual(sorted(name for name, copy in copies.items() if "max_edge" in copy),
                         ["trip-07-praca-gomes-teixeira.jpg", "trip-13-ribeira-riverfront.jpg"])
        self.assertEqual(copies["trip-07-praca-gomes-teixeira.jpg"], {"max_edge": 1600})
        self.assertEqual([name for name, copy in copies.items()
                          if copy.get("remove_gps_void")], ["varied-08-red-fox-drinking.jpg"])
        self.assertEqual(sum(1 for copy in copies.values() if copy.get("transcode")), 5)
        self.assertEqual(copies["trip-13-ribeira-riverfront.jpg"],
                         {"max_edge": 1600, "xmp_place": {"State": "Norte Region"}})
        self.assertEqual(copies["trip-14-riverfront-at-night.jpg"],
                         {"xmp_place": {"State": "Norte Region"}})
        self.assertEqual(sum(1 for copy in copies.values() if not copy), 14)

    def test_no_local_paths(self):
        with open(MANIFEST, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("\\", text)
        self.assertNotRegex(text, r"\b[A-Za-z]:/|\bfile:/|/[Uu]sers/|/home/")


def credits_sections():
    """{heading: [row cells]} of the tables in CREDITS.md, and its text."""
    with open(CREDITS, "r", encoding="utf-8") as handle:
        text = handle.read()
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("| ") and not re.match(r"^\| File \|", line):
            cells = [cell.strip().replace("\\|", "|")
                     for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            sections[current].append(cells)
    return sections, text


def link(cell):
    match = re.match(r"^\[(.+?)\]\((https://\S+)\)$", cell)
    return (match.group(1), match.group(2)) if match else (cell, None)


class CreditsTest(unittest.TestCase):

    def setUp(self):
        self.manifest = load_manifest()
        self.sections, self.text = credits_sections()

    def test_every_item_credited_as_in_the_manifest(self):
        for group, heading in self.manifest["groups"].items():
            items = [item for item in self.manifest["items"] if item["group"] == group]
            rows = self.sections.get(heading)
            self.assertIsNotNone(rows, heading)
            self.assertEqual([row[0] for row in rows], [item["file"] for item in items])
            for row, item in zip(rows, items):
                with self.subTest(file=item["file"]):
                    file_name, title, creator, licence, source = row
                    self.assertEqual(title, item["title"])
                    self.assertEqual(creator, item["credit"])
                    self.assertEqual(link(licence), (item["licence"], item["licence_url"]))
                    self.assertEqual(link(source)[1], item["source_page"])
                    self.assertIn(link(source)[0], ("Wikimedia Commons", "Flickr"))

    def test_licence_texts_listed(self):
        for licence in {item["licence"] for item in self.manifest["items"]}:
            self.assertIn("- %s: %s" % (licence, LICENCES[licence]), self.text)

    def test_changes_name_every_changed_copy(self):
        changes = self.text.split("## Changes", 1)[1]
        for item in self.manifest["items"]:
            with self.subTest(file=item["file"]):
                self.assertEqual(item["file"] in changes, bool(item["copy"]))

    def test_changes_tables_match_the_instructions(self):
        written = {}        # file: the second cells of its rows (what was written)
        for row in self.sections["Changes"]:
            written.setdefault(row[0], []).append(row[1])
        for item in self.manifest["items"]:
            copy = item["copy"]
            with self.subTest(file=item["file"]):
                wanted = []
                if copy.get("gps"):
                    wanted.append("%s, %s" % (copy["gps"]["lat"], copy["gps"]["lon"]))
                if copy.get("exif_date"):
                    wanted.append(" ".join([copy["exif_date"].replace(":", "-", 2)]
                                           + ([copy["exif_offset"]]
                                              if copy.get("exif_offset") else [])))
                if copy.get("video_date"):
                    wanted.append(copy["video_date"])
                place = copy.get("xmp_place") or {}
                if "CountryCode" in place:
                    # (Names from the source page: a table of them.)
                    wanted.append("%s (%s)" % (", ".join(
                        place[key] for key in ("City", "State", "Country") if key in place),
                        place["CountryCode"]))
                elif place:
                    # (A correction of the file's own names: said in words.)
                    note = self.text.split("Place name corrected:", 1)[1]
                    self.assertIn(item["file"], note)
                    for value in place.values():
                        self.assertIn('"%s"' % value, " ".join(note.split()))
                self.assertEqual(sorted(written.get(item["file"], [])), sorted(wanted))

    def test_the_demo_credit_line_points_here(self):
        # (The published demo's credits name this file's address.)
        self.assertTrue(os.path.isfile(os.path.join(support.REPO, "sample", "demo-set",
                                                    "CREDITS.md")))


class GitIgnoreTest(unittest.TestCase):

    def test_downloads_are_not_committed(self):
        paths = ["sample/demo-set/files/trip-01-x.jpg", "sample/demo-set/originals/trip-01-x.jpg",
                 "sample/demo-set/manifest.json", "sample/demo-set/CREDITS.md",
                 "sample/demo-set/fetch_demo_set.py"]
        try:
            result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                                    input="\n".join(paths).encode("utf-8"), cwd=support.REPO,
                                    capture_output=True)
        except OSError:
            self.skipTest("git is not available")
        if result.returncode not in (0, 1):
            self.skipTest("not a git checkout")
        self.assertEqual(sorted(result.stdout.decode("utf-8").split()), sorted(paths[:2]))


# ---------------------------------------------------------------------------
# The copy step

class CopyStepTest(TempFiles, unittest.TestCase):

    def test_date_and_gps_written_and_read_back(self):
        original = picture(exif=camera_exif(void_gps=True))
        copy = {"exif_date": "2025:10:18 20:00:00", "exif_offset": "+03:00",
                "gps": {"lat": 50.1495, "lon": 20.57623906, "accuracy_m": 100},
                "remove_gps_void": True}
        made = fetch.edit_picture(original, copy)
        result = self.read_back(made)
        self.assertEqual((result["taken_at"], result["taken_source"], result["tz_offset"]),
                         ("2025-10-18T20:00:00", "exif", "+03:00"))
        self.assertAlmostEqual(result["lat"], 50.1495, places=6)
        self.assertAlmostEqual(result["lon"], 20.57623906, places=6)
        gps = result["exif"]["GPS"]
        self.assertEqual(gps["GPSHPositioningError"], 100)
        self.assertEqual((gps["GPSVersionID"], gps["GPSMapDatum"]), ([2, 3, 0, 0], "WGS-84"))
        self.assertNotIn("GPSStatus", gps)
        self.assertEqual(result["exif"]["Make"], "Testmaker")
        # Only the metadata changed: the picture's JPEG data is the original's.
        self.assertEqual(fetch.split_jpeg(made)[1], fetch.split_jpeg(original)[1])
        # Text that is not ASCII (UTF-8 here) keeps its bytes.
        with Image.open(io.BytesIO(made)) as image:
            artist = image.getexif()[0x013B]
        self.assertEqual(artist.encode("latin-1"), "Jakub Ha\u0142un".encode("utf-8"))
        self.assertEqual(result["exif"]["ExposureTime"], 0.004)

    def test_exif_made_for_a_file_without_any(self):
        original = picture()
        with Image.open(io.BytesIO(original)) as image:
            self.assertNotIn("exif", image.info)
        made = fetch.edit_picture(original, {"exif_date": "2017:07:23 13:46:23",
                                             "gps": {"lat": -33.856784, "lon": -70.5}})
        result = self.read_back(made)
        self.assertEqual((result["taken_at"], result["tz_offset"]), ("2017-07-23T13:46:23", ""))
        self.assertAlmostEqual(result["lat"], -33.856784, places=6)
        self.assertAlmostEqual(result["lon"], -70.5, places=6)
        self.assertNotIn("GPSHPositioningError", result["exif"]["GPS"])
        segments, _rest = fetch.split_jpeg(made)
        # (After the JFIF segment, as a camera writes it.)
        self.assertEqual([marker for marker, _payload in segments[:2]],
                         [fetch.APP0, fetch.APP1])
        self.assertTrue(segments[1][1].startswith(fetch.EXIF_HEADER))

    def test_gps_written_exactly(self):
        made = fetch.edit_picture(picture(), {"gps": {"lat": 41.141371, "lon": -8.615879}})
        gps = self.read_back(made)["exif"]["GPS"]
        self.assertEqual((gps["GPSLatitudeRef"], gps["GPSLatitude"]), ("N", [41, 8, 28.9356]))
        self.assertEqual((gps["GPSLongitudeRef"], gps["GPSLongitude"]), ("W", [8, 36, 57.1644]))

    def test_place_names_in_a_new_xmp_packet(self):
        place = {"City": "Ayr & Troon", "State": "Scotland", "Country": "United Kingdom",
                 "CountryCode": "GB"}
        made = fetch.edit_picture(picture(), {"xmp_place": place})
        result = self.read_back(made)
        self.assertEqual(result["exif"]["XMPPlace"], {"city": "Ayr & Troon", "region": "Scotland",
                                                      "country": "United Kingdom", "cc": "GB"})
        self.assertIsNone(result["lat"])
        self.assertNotIn("GPS", result["exif"])

    def test_place_names_into_an_xmp_packet(self):
        original = picture(exif=camera_exif(date="2014:08:11 11:10:54"), xmp=XMP_PACKET)
        made = fetch.edit_picture(original, {"xmp_place": {"Country": "Iceland",
                                                           "CountryCode": "IS"}})
        result = self.read_back(made)
        # (The fields written replace what the file had; the others stay, as exiftool
        # leaves them.)
        self.assertEqual(result["exif"]["XMPPlace"], {"city": "Oldtown", "country": "Iceland",
                                                      "cc": "IS"})
        self.assertEqual(result["taken_at"], "2014-08-11T11:10:54")
        segments, rest = fetch.split_jpeg(made)
        packets = [payload for marker, payload in segments
                   if marker == fetch.APP1 and payload.startswith(fetch.XMP_HEADER)]
        self.assertEqual(len(packets), 1)
        self.assertIn(b'xmp:CreatorTool="Testtool"', packets[0])
        self.assertNotIn(b"Oldland", packets[0])
        self.assertEqual(rest, fetch.split_jpeg(original)[1])
        # Only the XMP changed: the EXIF segment is the original's, byte for byte.
        exif = [payload for marker, payload in fetch.split_jpeg(original)[0]
                if payload.startswith(fetch.EXIF_HEADER)]
        self.assertIn((fetch.APP1, exif[0]), segments)

    def test_one_place_name_corrected(self):
        packet = XMP_PACKET.replace(b'photoshop:City="Oldtown"',
                                    b'photoshop:City="Oldtown" photoshop:State="Noorte Region"')
        original = picture(xmp=packet)
        self.assertEqual(self.read_back(original, "in.jpg")["exif"]["XMPPlace"],
                         {"city": "Oldtown", "region": "Noorte Region", "country": "Oldland"})
        made = fetch.edit_picture(original, {"xmp_place": {"State": "Norte Region"}})
        self.assertEqual(self.read_back(made)["exif"]["XMPPlace"],
                         {"city": "Oldtown", "region": "Norte Region", "country": "Oldland"})
        self.assertNotIn(b"Noorte", made)

    def test_resized_with_its_metadata(self):
        icc = b"fake ICC profile " * 8
        original = picture(size=(400, 300), exif=camera_exif(date="2026:04:22 16:10:58"),
                           icc=icc, xmp=XMP_PACKET)
        made = fetch.edit_picture(original, {"max_edge": 100})
        result = self.read_back(made)
        self.assertEqual((result["width"], result["height"]), (100, 75))
        self.assertEqual(result["taken_at"], "2026-04-22T16:10:58")
        self.assertEqual(result["exif"]["XMPPlace"], {"city": "Oldtown", "country": "Oldland"})
        with Image.open(io.BytesIO(made)) as image:
            self.assertEqual(image.info.get("icc_profile"), icc)
            exif_ifd = image.getexif().get_ifd(0x8769)
            self.assertEqual((exif_ifd[0xA002], exif_ifd[0xA003]), (100, 75))
        # Never enlarged: a picture small enough already stays as it is.
        self.assertEqual(fetch.edit_picture(original, {"max_edge": 1000}), original)

    def test_unknown_instruction_and_not_a_jpeg(self):
        source = self.write("in.jpg", picture())
        target = os.path.join(os.path.dirname(source), "out.jpg")
        with self.assertRaises(fetch.FetchError):
            fetch.make_copy({"file": "out.jpg", "copy": {"sharpen": True}}, source, target)
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "PNG")
        png = self.write("in.png", buffer.getvalue())
        with self.assertRaises(fetch.FetchError):
            fetch.make_copy({"file": "out.jpg", "copy": {"exif_date": "2020:01:01 00:00:00"}},
                            png, target)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))

    def test_unchanged_copy_is_the_original(self):
        data = picture()
        source = self.write("in.jpg", data)
        target = os.path.join(os.path.dirname(source), "out.jpg")
        item = {"file": "out.jpg", "copy": {}, "copy_sha256": sha256(data)}
        self.assertEqual(fetch.make_copy(item, source, target), "as downloaded")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertEqual(fetch.make_copy(item, source, target), "copy already there")

    def test_video_copy(self):
        source = self.write("in.webm", b"a video")
        target = os.path.join(os.path.dirname(source), "out.mp4")
        item = {"file": "out.mp4", "copy": {"transcode": True,
                                            "video_date": "2021-12-10T12:00:00+0900",
                                            "video_location": "+43.0018+144.3500/"}}
        calls = []

        def run(argv):
            calls.append(argv)
            with open(argv[-1], "wb") as handle:
                handle.write(b"an mp4")
            return 0, ""

        with mock.patch.object(fetch, "run_program", run):
            with self.assertRaises(fetch.FetchError):
                fetch.make_copy(item, source, target, ffmpeg=None)
            fetch.make_copy(item, source, target, ffmpeg="ffmpeg-here")
        argv = calls[0]
        self.assertEqual(argv[0], "ffmpeg-here")
        self.assertEqual(argv[argv.index("-i") + 1], source)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertEqual(argv[argv.index("-crf") + 1], "26")
        self.assertIn("com.apple.quicktime.creationdate=2021-12-10T12:00:00+0900", argv)
        self.assertIn("com.apple.quicktime.location.ISO6709=+43.0018+144.3500/", argv)
        self.assertEqual(argv[-3:], ["-f", "mp4", target + ".part"])
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"an mp4")

        def fail(argv):
            return 1, "no such codec"

        with mock.patch.object(fetch, "run_program", fail):
            with self.assertRaisesRegex(fetch.FetchError, "no such codec"):
                fetch.make_copy(item, source, target + "2", ffmpeg="ffmpeg-here")
        self.assertFalse(os.path.exists(target + "2.part"))


# ---------------------------------------------------------------------------
# Downloading (a stand-in for urlopen)

class FakeResponse:

    def __init__(self, data, length=None):
        self.stream = io.BytesIO(data)
        self.headers = email.message.Message()
        self.headers["Content-Length"] = str(len(data) if length is None else length)

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeWeb:
    """urlopen for a few addresses: each answers with its replies in turn (bytes,
    (status, headers) for an error, or an exception its read() raises), the last one
    again and again. clock and sleep keep
    a pretend time."""

    def __init__(self, replies):
        self.replies = {url: list(answers) for url, answers in replies.items()}
        self.requests = []
        self.now = 1000.0
        self.slept = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, request.get_header("User-agent"), self.now))
        answers = self.replies.get(url)
        if not answers:
            raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            # (A transfer broken off while it is read.)
            response = FakeResponse(b"")
            response.read = mock.Mock(side_effect=answer)
            return response
        if isinstance(answer, tuple) and isinstance(answer[0], int):
            status, extra = answer
            headers = email.message.Message()
            for name, value in (extra or {}).items():
                headers[name] = value
            raise urllib.error.HTTPError(url, status, "Error %d" % status, headers, None)
        if isinstance(answer, tuple):
            return FakeResponse(*answer)
        return FakeResponse(answer)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def downloader(self):
        return fetch.Downloader(opener=self, sleep=self.sleep, clock=self.clock)


class DownloadTest(TempFiles, unittest.TestCase):

    URL = "https://upload.example.org/a.jpg"

    def setUp(self):
        super().setUp()
        self.data = picture()
        self.target = os.path.join(self.base.make_dir("originals"), "a.jpg")

    def download(self, web):
        web.downloader().download(self.URL, self.target, len(self.data), sha256(self.data))

    def test_download_checked_and_kept(self):
        web = FakeWeb({self.URL: [self.data]})
        self.download(web)
        with open(self.target, "rb") as handle:
            self.assertEqual(handle.read(), self.data)
        self.assertFalse(os.path.exists(self.target + ".part"))
        url, agent, _when = web.requests[0]
        self.assertEqual(url, self.URL)
        self.assertIn("Search-Images", agent)
        self.assertIn("https://github.com/seligman/search_images", agent)

    def test_sha256_mismatch_fails_loudly(self):
        web = FakeWeb({self.URL: [bytes(len(self.data))]})
        with self.assertRaisesRegex(fetch.FetchError, "does not match the manifest"):
            self.download(web)
        self.assertFalse(os.path.exists(self.target))
        self.assertFalse(os.path.exists(self.target + ".part"))
        self.assertEqual(len(web.requests), 1)

    def test_larger_than_the_manifest(self):
        web = FakeWeb({self.URL: [self.data + b"more"]})
        with self.assertRaisesRegex(fetch.FetchError, "larger"):
            self.download(web)
        self.assertFalse(os.path.exists(self.target + ".part"))

    def test_429_waits_as_asked(self):
        web = FakeWeb({self.URL: [(429, {"Retry-After": "7"}), self.data]})
        self.download(web)
        self.assertEqual(len(web.requests), 2)
        self.assertEqual(web.slept, [7])
        self.assertTrue(os.path.isfile(self.target))

    def test_5xx_backs_off_then_gives_up(self):
        web = FakeWeb({self.URL: [(503, None)]})
        with self.assertRaisesRegex(fetch.FetchError, "gave up after 5 tries"):
            self.download(web)
        self.assertEqual(len(web.requests), 5)
        self.assertEqual(web.slept, [30, 60, 120, 240])
        self.assertFalse(os.path.exists(self.target))

    def test_broken_off_transfer_is_asked_again(self):
        web = FakeWeb({self.URL: [(self.data[:100], len(self.data)), self.data]})
        self.download(web)
        self.assertEqual(len(web.requests), 2)
        self.assertEqual(web.slept, [30])

    def test_a_transfer_broken_off_mid_way_is_asked_again(self):
        # (A server that drops a long transfer often shows as an SSLError, "EOF occurred
        # in violation of protocol"; a read that times out, as socket.timeout.)
        import socket
        for broken in (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
                       socket.timeout("The read operation timed out")):
            web = FakeWeb({self.URL: [broken, self.data]})
            self.download(web)
            self.assertEqual(len(web.requests), 2, repr(broken))
            self.assertEqual(web.slept, [30], repr(broken))
            with open(self.target, "rb") as handle:
                self.assertEqual(handle.read(), self.data)
            os.remove(self.target)

    def test_404_is_not_asked_again(self):
        web = FakeWeb({})
        with self.assertRaisesRegex(fetch.FetchError, "404"):
            self.download(web)
        self.assertEqual(len(web.requests), 1)

    def test_one_request_a_second(self):
        web = FakeWeb({self.URL: [self.data], self.URL + "?2": [self.data]})
        downloader = web.downloader()
        downloader.download(self.URL, self.target, len(self.data), sha256(self.data))
        downloader.download(self.URL + "?2", self.target + "2", len(self.data),
                            sha256(self.data))
        self.assertEqual(web.slept, [1.0])
        self.assertEqual(web.requests[1][2] - web.requests[0][2], 1.0)

    def test_tls_settings(self):
        # (Certificates checked as always; no post-handshake authentication, which
        # Flickr's image servers refuse with 429.)
        context = fetch.tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(context.post_handshake_auth)
        self.assertIs(fetch.Downloader().opener, fetch.urlopen)

    def test_resume(self):
        item = {"file": "a.jpg", "file_url": self.URL, "bytes": len(self.data),
                "sha256": sha256(self.data)}
        web = FakeWeb({self.URL: [self.data]})
        self.assertEqual(fetch.ensure_original(item, self.target, web.downloader()),
                         "downloaded")
        self.assertEqual(fetch.ensure_original(item, self.target, web.downloader()),
                         "already there")
        self.assertEqual(len(web.requests), 1)
        # A damaged (or older) file is downloaded again.
        with open(self.target, "r+b") as handle:
            handle.write(b"\0\0\0\0")
        logged = []
        self.assertEqual(fetch.ensure_original(item, self.target, web.downloader(),
                                               logged.append), "downloaded")
        self.assertEqual(len(web.requests), 2)
        self.assertIn("does not match the manifest", logged[0])
        with open(self.target, "rb") as handle:
            self.assertEqual(handle.read(), self.data)


# ---------------------------------------------------------------------------
# The command line

class ListTest(unittest.TestCase):

    def run_main(self, *argv):
        out = io.StringIO()
        web = FakeWeb({})
        with contextlib.redirect_stdout(out):
            code = fetch.main(list(argv), downloader=web.downloader())
        self.assertEqual(web.requests, [])
        return code, out.getvalue()

    def test_list(self):
        manifest = load_manifest()
        code, text = self.run_main("--list")
        self.assertEqual(code, 0)
        for item in manifest["items"]:
            self.assertIn(item["file"], text)
        for heading in manifest["groups"].values():
            self.assertIn(heading, text)
        self.assertIn("44 items (39 pictures, 5 videos): 564.1 MB to download; the copies take "
                      "about %.1f MB." % (manifest["copies_total_bytes"] / 1e6), text)
        self.assertEqual(round(manifest["copies_total_bytes"] / 1e6), 465)
        self.assertNotIn("Command line:", text)

    def test_list_one_group(self):
        code, text = self.run_main("--list", "--only", "videos")
        self.assertEqual(code, 0)
        self.assertEqual(len(re.findall(r"videos-\d\d-\S+\.mp4", text)), 5)
        self.assertNotIn(".jpg", text)
        self.assertIn("5 items (0 pictures, 5 videos)", text)


class CommandLineTest(TempFiles, unittest.TestCase):
    """main() on a manifest of three items made in the test."""

    def setUp(self):
        super().setUp()
        self.plain, self.geo = picture(), picture(size=(48, 64))
        self.video = b"a video, as far as this test knows"
        base = {"title": "T", "creator": "C", "credit": "C", "licence": "CC0 1.0",
                "licence_url": LICENCES["CC0 1.0"], "source_page": "https://example.org/p"}
        self.items = [
            dict(base, file="trip-01-plain.jpg", group="trip",
                 file_url="https://example.org/plain.jpg", sha256=sha256(self.plain),
                 bytes=len(self.plain), copy={}, copy_sha256=sha256(self.plain),
                 copy_bytes=len(self.plain)),
            dict(base, file="text-01-geo.jpg", group="text",
                 file_url="https://example.org/geo.JPG", sha256=sha256(self.geo),
                 bytes=len(self.geo), copy_bytes=len(self.geo) + 200,
                 copy={"exif_date": "2007:05:01 00:00:00", "gps": {"lat": 60.866019,
                                                                   "lon": 26.698837,
                                                                   "accuracy_m": 800}}),
            dict(base, file="videos-01-clip.mp4", group="videos",
                 file_url="https://example.org/clip.webm", sha256=sha256(self.video),
                 bytes=len(self.video), copy_bytes=20,
                 copy={"transcode": True, "video_date": "2019-06-09T12:00:00+1200"}),
        ]
        manifest = {"groups": {group: group.title() for group in GROUPS}, "items": self.items}
        path = self.write("manifest.json", json.dumps(manifest))
        patcher = mock.patch.object(fetch, "MANIFEST", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = os.path.join(self.base.path, "work", "copies")
        self.originals = os.path.join(self.base.path, "work", "originals")
        self.web = FakeWeb({"https://example.org/plain.jpg": [self.plain],
                            "https://example.org/geo.JPG": [self.geo],
                            "https://example.org/clip.webm": [self.video]})

    def run_main(self, *extra, ffmpeg=(None, "ffmpeg was not found.")):
        out = io.StringIO()
        made = []

        def run(argv):
            made.append(argv)
            with open(argv[-1], "wb") as handle:
                handle.write(b"an mp4")
            return 0, ""

        with contextlib.redirect_stdout(out), \
                mock.patch.object(fetch, "find_ffmpeg", lambda option=None: ffmpeg), \
                mock.patch.object(fetch, "run_program", run):
            code = fetch.main(["--out", self.out, "--originals", self.originals] + list(extra),
                              downloader=self.web.downloader())
        return code, out.getvalue(), made

    def test_fetch_without_ffmpeg(self):
        code, text, made = self.run_main()
        self.assertEqual(code, 0, text)
        first = text.splitlines()[0]
        self.assertIn("Command line: python sample/demo-set/fetch_demo_set.py --out ", first)
        self.assertIn("The videos (1) are skipped, and not downloaded: they need ffmpeg.", text)
        self.assertEqual(sorted(os.listdir(self.out)), ["text-01-geo.jpg", "trip-01-plain.jpg"])
        self.assertEqual(sorted(os.listdir(self.originals)),
                         ["text-01-geo.jpg", "trip-01-plain.jpg"])
        self.assertEqual([url for url, _agent, _when in self.web.requests],
                         ["https://example.org/plain.jpg", "https://example.org/geo.JPG"])
        self.assertEqual(made, [])
        with open(os.path.join(self.out, "trip-01-plain.jpg"), "rb") as handle:
            self.assertEqual(handle.read(), self.plain)
        result = images.read_image(os.path.join(self.out, "text-01-geo.jpg"),
                                   want_preview=False)
        self.assertEqual(result["taken_at"], "2007-05-01T00:00:00")
        self.assertAlmostEqual(result["lat"], 60.866019, places=6)
        self.assertEqual(result["exif"]["GPS"]["GPSHPositioningError"], 800)

        # Again: nothing is downloaded, and the copies are made again (or kept).
        code, text, made = self.run_main()
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.web.requests), 2)
        self.assertIn("2 made, 0 failed (0 downloaded, 2 already there)", text)

    def test_videos_with_ffmpeg(self):
        code, text, made = self.run_main("--only", "videos", ffmpeg=("ffmpeg-here", ""))
        self.assertEqual(code, 0, text)
        self.assertEqual(os.listdir(self.out), ["videos-01-clip.mp4"])
        self.assertEqual(os.listdir(self.originals), ["videos-01-clip.webm"])
        self.assertEqual(made[0][0], "ffmpeg-here")

    def test_a_bad_download_fails_the_run(self):
        self.web.replies["https://example.org/geo.JPG"] = [bytes(len(self.geo))]
        code, text, _made = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("does not match the manifest", text)
        self.assertIn("Failed: text-01-geo.jpg.", text)
        self.assertEqual(os.listdir(self.out), ["trip-01-plain.jpg"])

    def test_dry_run_writes_nothing(self):
        code, text, made = self.run_main("--dry-run", ffmpeg=("ffmpeg-here", ""))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.web.requests, [])
        self.assertEqual(made, [])
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.originals))
        self.assertIn("would download", text)
        self.assertIn("Dry run: 3 to download, 0 already there", text)

    def test_stopped_is_not_done(self):
        # Ctrl+C (or the abort file) stops after the item in progress: the run says the
        # set is not complete, gives no "add it as a library" advice, and fails.
        calls = []

        def should_stop(_ctx):
            calls.append(1)
            return len(calls) > 1

        # (As if the copies went into the program folder: that advice comes when done.)
        with mock.patch.object(fetch, "REPO", self.base.path):
            with mock.patch.object(fetch.jobs.ConsoleContext, "should_stop", should_stop):
                code, text, _made = self.run_main()
            self.assertEqual(code, 130, text)
            self.assertIn("Stopped: 1 of 2 items done.", text)
            self.assertIn("The set is not complete: run the same command again to finish "
                          "it.", text)
            self.assertNotIn("add it as a library", text)
            self.assertEqual(os.listdir(self.out), ["trip-01-plain.jpg"])
            # Again: the rest.
            code, text, _made = self.run_main()
        self.assertEqual(code, 0, text)
        self.assertIn("2 made, 0 failed (1 downloaded, 1 already there)", text)
        self.assertNotIn("not complete", text)
        self.assertIn("add it as a library", text)

    def test_copies_and_originals_need_folders_of_their_own(self):
        # (Most copies have their original's name: one would replace the other.)
        for originals in (self.out, os.path.join(self.out, "originals"),
                          os.path.dirname(self.out)):
            code, text, _made = self.run_main("--originals", originals)
            self.assertEqual(code, 2, text)
            self.assertIn("need folders of their own", text)
            self.assertEqual(self.web.requests, [])
            self.assertFalse(os.path.exists(self.out))

    def test_ffmpeg_option_not_found(self):
        code, text, _made = self.run_main("--ffmpeg", os.path.join(self.base.path, "nothing"),
                                          ffmpeg=(None, "not there"))
        self.assertEqual(code, 2)
        self.assertEqual(self.web.requests, [])


if __name__ == "__main__":
    unittest.main()
