"""Tests for where 360-degree pictures open (opening_views.py) and what uses it.

The helpers (views checked and tidied, the view chosen from the image
model's scores, the words compared), the opening_views table (the owner's
view by content, the automatic one, records, details, clean-up), the views
and thumbnails cut from a panorama (images.py), the looks run that works
the automatic views out (looks.py, with the fake image model of
fake_image_embedder.py, which embeds words too), the admin's "Open here"
(admin.py), /api/image and the scanner's thumbnails. Panoramas are drawn
here; no real picture is used.
"""

import io
import json
import time
import unittest
from unittest import mock

import support

import common
import db
import imageinfo
import opening_views
import search_index
from support import quiet

try:
    from PIL import Image, ImageDraw
except ImportError:            # (the tests that need Pillow are skipped)
    Image = ImageDraw = None

NOW = 1700000000.0


def insert_image(conn, library_id, path, **fields):
    row = {"library_id": library_id, "path": path, "name": path.split("/")[-1],
           "size": 100, "mtime": NOW, "ctime": NOW, "width": 2048, "height": 1024,
           "added": NOW, "updated": NOW, "content_hash": "hash-" + path,
           "projection": imageinfo.PROJECTION_EQUIRECT}
    row.update(fields)
    columns = sorted(row)
    cursor = conn.execute("INSERT INTO images (%s) VALUES (%s)"
                          % (",".join(columns), ",".join("?" * len(columns))),
                          [row[column] for column in columns])
    conn.commit()
    return cursor.lastrowid


def described(title, description=""):
    return json.dumps({"title": title, "description": description})


def panorama(size=(512, 256), marker_yaw=90, colour=(220, 30, 30)):
    """A 2:1 panorama: sky, ground, and a large coloured disc on the horizon at
    marker_yaw degrees (right of the middle)."""
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        shade = (150, 190, 250) if y < height // 2 else (90, 120, 50)
        draw.line([(0, y), (width, y)], fill=shade)
    x = (marker_yaw / 360.0 + 0.5) * width
    radius = width // 14
    draw.ellipse([x - radius, height / 2.0 - radius, x + radius, height / 2.0 + radius],
                 fill=colour)
    return image


def jpeg(image, quality=90):
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def middle(data):
    """The colour of the middle of a JPEG."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    return image.getpixel((image.size[0] // 2, image.size[1] // 2))


def reddish(colour):
    return colour[0] > 150 and colour[1] < 110 and colour[2] < 110


class HelperTest(unittest.TestCase):

    def test_views_are_checked_and_tidied(self):
        self.assertEqual(opening_views.clean_view(190, 10), (-170.0, 10.0, None))
        self.assertEqual(opening_views.clean_view(-540, -12.345, 50.0), (-180.0, -12.35, 50.0))
        # The viewer's own field of view is no view of its own; off a little: clamped.
        self.assertEqual(opening_views.clean_view(0, 0, 75.2)[2], None)
        self.assertEqual(opening_views.clean_view(0, 0, 100.3)[2], 100.0)
        for bad in ((True, 0, None), ("90", 0, None), (0, 90.5, None), (0, None, None),
                    (float("nan"), 0, None), (0, 0, 10), (0, 0, "wide"),
                    (float("inf"), 0, None)):
            with self.assertRaises(opening_views.ViewError) as raised:
                opening_views.clean_view(*bad)
            self.assertTrue(raised.exception.fields, bad)

    def test_the_view_chosen_from_the_scores(self):
        yaws = [-180, -135, -90, -45, 0, 45, 90, 135]
        # Nearly as good as the front: the front.
        self.assertEqual(opening_views.choose([0.5, 0.5, 0.5, 0.5, 0.60, 0.5, 0.605, 0.5],
                                              yaws), 0.0)
        # Clearly better: there, nudged towards the better neighbour.
        yaw = opening_views.choose([0.5, 0.5, 0.5, 0.5, 0.60, 0.62, 0.70, 0.5], yaws)
        self.assertGreater(yaw, 67.5)
        self.assertLess(yaw, 90.0)
        self.assertEqual(opening_views.choose([0.5, 0.5, 0.5, 0.5, 0.6, 0.6, 0.7, 0.6], yaws),
                         90.0)
        # Round the back: -180 and 135 are neighbours.
        yaw = opening_views.choose([0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.8], yaws)
        self.assertTrue(yaw > 157.5 or yaw <= -180.0 + 1e-9, yaw)
        # Nothing to choose from: the front.
        self.assertEqual(opening_views.choose([], []), 0.0)
        self.assertEqual(opening_views.choose([0.5, 0.9], [10, 20]), 0.0)
        self.assertEqual(opening_views.choose([0.5, 0.9], [0]), 0.0)

    def test_the_words_compared(self):
        info = {"title": "Red barn", "description": "A red barn stands in a field. Cows graze."}
        self.assertEqual(opening_views.query_text(info), "Red barn. A red barn stands in a field.")
        self.assertEqual(opening_views.query_text({"title": "Is it?", "description": "Yes."}),
                         "Is it? Yes.")
        self.assertEqual(opening_views.query_text({"title": " Lake  view "}), "Lake view")
        # No title: nothing to compare (it stays at the front).
        self.assertEqual(opening_views.query_text({"description": "A lake."}), "")
        self.assertEqual(opening_views.query_text(None), "")
        # The owner's words count.
        edit = {"title": "Our cabin", "description": None, "tags_add": [], "tags_remove": []}
        self.assertEqual(opening_views.query_text(info, edit),
                         "Our cabin. A red barn stands in a field.")
        self.assertEqual(opening_views.query_text(None, edit), "Our cabin")
        long_text = "word " * 200
        self.assertLessEqual(len(opening_views.query_text({"title": "T",
                                                           "description": long_text})), 310)

    def test_what_a_view_is_worked_out_from(self):
        first = opening_views.auto_key("3 n0 z0", "Red barn")
        self.assertNotEqual(first, opening_views.auto_key("3 n0 z0", "Red barn."))
        self.assertNotEqual(first, opening_views.auto_key("4 n0 z0", "Red barn"))
        self.assertTrue(first.startswith("%d|" % opening_views.METHOD_VERSION))

    def test_record_values(self):
        self.assertIsNone(opening_views.record_value(None))
        self.assertIsNone(opening_views.record_value({"yaw": 0.2, "pitch": -0.1, "fov": None}))
        self.assertEqual(opening_views.record_value({"yaw": 90.04, "pitch": -5.06, "fov": None}),
                         [90.0, -5.1])
        self.assertEqual(opening_views.record_value({"yaw": 0, "pitch": 0, "fov": 50}),
                         [0, 0, 50])
        self.assertEqual(opening_views.wrap_yaw(180), -180.0)
        self.assertEqual(opening_views.wrap_yaw(-190), 170.0)


class TableTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.folder = self.base.library_folder("trips")
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips01")])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.pano = insert_image(self.conn, "lib_trips01", "pano.jpg",
                                 info=described("Red barn"), described=1, prompt_ver=6)
        self.copy = insert_image(self.conn, "lib_trips01", "copy/pano.jpg",
                                 content_hash="hash-pano.jpg")
        self.flat = insert_image(self.conn, "lib_trips01", "flat.jpg", projection="")
        self.gone = insert_image(self.conn, "lib_trips01", "gone.jpg", missing_since=NOW)

    def updated(self, image_id):
        return self.conn.execute("SELECT updated FROM images WHERE id = ?",
                                 (image_id,)).fetchone()[0]

    def records(self):
        config = common.load_config()
        return {record["i"]: record for record in search_index.build_records(
            self.conn, config, ["lib_trips01"], use_cache=False)}

    def test_no_table_no_views(self):
        self.assertFalse(opening_views.any_views(self.conn))
        self.assertEqual(opening_views.views_for(self.conn, [self.pano]), {})
        self.assertEqual(opening_views.detail(self.conn, self.pano, "hash-pano.jpg"),
                         {"yaw": 0.0, "pitch": 0.0, "fov": None})
        self.assertNotIn("ov", self.records()[self.pano])
        self.assertEqual(opening_views.forget_unreferenced(self.conn), 0)
        self.assertEqual(opening_views.drop_auto(self.conn), [])
        self.assertIsNotNone(opening_views.clear_owner(self.conn, self.pano))

    def test_the_owners_view_goes_with_the_content(self):
        etag = search_index.index_etag(self.conn, {"lib_trips01": "Trips"}, True)
        with self.conn:
            row = opening_views.set_owner(self.conn, self.pano, 120, -10, 60, now=NOW + 50)
        self.assertEqual(row["id"], self.pano)
        # Its copy (the same content) opens there too; both are "changed".
        self.assertEqual(self.updated(self.pano), NOW + 50)
        self.assertEqual(self.updated(self.copy), NOW + 50)
        self.assertNotEqual(search_index.index_etag(self.conn, {"lib_trips01": "Trips"}, True),
                            etag)
        records = self.records()
        self.assertEqual(records[self.pano]["ov"], [120.0, -10.0, 60.0])
        self.assertEqual(records[self.copy]["ov"], [120.0, -10.0, 60.0])
        self.assertNotIn("ov", records[self.flat])
        self.assertEqual(search_index.picture_record(self.conn, self.copy)["ov"],
                         [120.0, -10.0, 60.0])
        self.assertEqual(opening_views.detail(self.conn, self.pano, "hash-pano.jpg", owner=True),
                         {"yaw": 120.0, "pitch": -10.0, "fov": 60.0, "owner": True,
                          "auto": None})
        self.assertEqual(opening_views.copies(self.conn, self.pano), [self.pano, self.copy])
        # Not a 360 picture, or not there: nothing.
        self.assertIsNone(opening_views.set_owner(self.conn, self.flat, 10, 0))
        self.assertIsNone(opening_views.set_owner(self.conn, self.gone, 10, 0))
        self.assertIsNone(opening_views.set_owner(self.conn, 999999, 10, 0))
        with self.assertRaises(opening_views.ViewError):
            opening_views.set_owner(self.conn, self.pano, 10, 95)

    def test_the_owners_view_wins_over_the_automatic_one(self):
        key = opening_views.content_key("hash-pano.jpg", self.pano)
        with self.conn:
            self.assertTrue(opening_views.store_auto(self.conn, key, 45.0, 0.0, "k1", now=NOW + 1))
        self.assertEqual(self.updated(self.pano), NOW + 1)
        self.assertEqual(self.records()[self.pano]["ov"], [45.0, 0.0])
        # The same view again: no change.
        with self.conn:
            self.assertFalse(opening_views.store_auto(self.conn, key, 45.0, 0.0, "k1",
                                                      now=NOW + 2))
        self.assertEqual(self.updated(self.pano), NOW + 1)
        with self.conn:
            opening_views.set_owner(self.conn, self.pano, -90, 5, now=NOW + 3)
        self.assertEqual(self.records()[self.pano]["ov"], [-90.0, 5.0])
        # A new automatic view hides behind the owner's.
        with self.conn:
            self.assertFalse(opening_views.store_auto(self.conn, key, 10.0, 0.0, "k2",
                                                      now=NOW + 4))
        self.assertEqual(self.records()[self.pano]["ov"], [-90.0, 5.0])
        self.assertEqual(opening_views.detail(self.conn, self.pano, "hash-pano.jpg", owner=True),
                         {"yaw": -90.0, "pitch": 5.0, "fov": None, "owner": True,
                          "auto": [10.0, 0.0]})
        # Cleared: the automatic one again.
        with self.conn:
            opening_views.clear_owner(self.conn, self.pano, now=NOW + 5)
        self.assertEqual(self.records()[self.pano]["ov"], [10.0, 0.0])
        self.assertEqual(self.updated(self.copy), NOW + 5)
        # The automatic views forgotten (the setting is off): the front again.
        with self.conn:
            turned = opening_views.drop_auto(self.conn, now=NOW + 6)
        self.assertEqual(sorted(turned), [self.pano, self.copy])
        self.assertNotIn("ov", self.records()[self.pano])
        self.assertFalse(opening_views.any_views(self.conn))

    def test_views_of_contents_that_are_gone_are_dropped(self):
        with self.conn:
            opening_views.set_owner(self.conn, self.pano, 30, 0)
            opening_views.store_auto(self.conn, "hash-elsewhere", 10.0, 0.0, "k")
            opening_views.store_auto(self.conn, "#%d" % self.flat, 10.0, 0.0, "k")
            opening_views.store_auto(self.conn, "#999999", 10.0, 0.0, "k")
            # (A row only missing for now keeps its view.)
            opening_views.store_auto(self.conn, "hash-gone.jpg", 10.0, 0.0, "k")
        with self.conn:
            self.assertEqual(opening_views.forget_unreferenced(self.conn), 2)
        keys = {row[0] for row in self.conn.execute("SELECT content_key FROM opening_views")}
        self.assertEqual(keys, {"hash-pano.jpg", "#%d" % self.flat, "hash-gone.jpg"})

    def test_the_photos_whose_view_is_due(self):
        insert_image(self.conn, "lib_trips01", "untitled.jpg", has_preview=1)
        insert_image(self.conn, "lib_trips01", "video.insv", has_preview=1, media="video",
                     info=described("A walk"), described=1,
                     projection=imageinfo.PROJECTION_DUAL_FISHEYE)
        stitched = insert_image(self.conn, "lib_trips01", "two.insp", has_preview=1,
                                info=described("Cabin", "A cabin. Trees."), described=1,
                                projection=imageinfo.PROJECTION_DUAL_FISHEYE)
        insert_image(self.conn, "lib_trips01", "nopreview.jpg", info=described("No preview"),
                     described=1)
        self.conn.execute("UPDATE images SET has_preview = 1 WHERE id IN (?, ?)",
                          (self.pano, self.copy))
        self.conn.commit()
        due = opening_views.pending(self.conn, ["lib_trips01"], "3 n0 z0")
        # Newest first, one per content (the newest copy with words: the copy of the
        # pano has none), photos with a title and a preview only.
        self.assertEqual([row["id"] for row, _text, _key in due], [stitched, self.pano])
        self.assertEqual(due[0][1], "Cabin. A cabin.")
        self.assertEqual(due[0][2], opening_views.auto_key("3 n0 z0", "Cabin. A cabin."))
        self.assertEqual(due[1][2], opening_views.auto_key("", "Red barn"))
        # Worked out: no longer due, until the words or the stitch change.
        with self.conn:
            opening_views.store_auto(self.conn, "hash-two.insp", 0.0, 0.0, due[0][2])
        due = opening_views.pending(self.conn, ["lib_trips01"], "3 n0 z0")
        self.assertEqual([row["id"] for row, _text, _key in due], [self.pano])
        self.assertEqual(len(opening_views.pending(self.conn, ["lib_trips01"], "4 n0 z0")), 2)
        self.assertEqual(opening_views.pending(self.conn, [], "3 n0 z0"), [])


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class PictureTest(unittest.TestCase):

    def test_views_round_the_horizon(self):
        import images
        data = jpeg(panorama(marker_yaw=90))
        shots = images.heading_views(data)
        self.assertEqual([yaw for yaw, _jpeg in shots], list(range(-180, 180, 45)))
        for yaw, shot in shots:
            self.assertEqual(Image.open(io.BytesIO(shot)).size, images.HEADING_SIZE)
            # (Looking a little down: the disc is just above the middle.)
            self.assertEqual(reddish(middle(shot)) or yaw != 90, True)
        self.assertTrue(reddish(middle(dict(shots)[90])))
        self.assertFalse(reddish(middle(dict(shots)[0])))
        with self.assertRaises(images.ImageReadError):
            images.heading_views(b"not a picture")

    def test_the_thumbnail_at_a_view(self):
        import images
        data = jpeg(panorama(marker_yaw=-60))
        front = images.view_thumbnail(data)
        turned = images.view_thumbnail(data, -60, 0)
        self.assertEqual(Image.open(io.BytesIO(turned)).size, (images.THUMB_SIZE,
                                                               images.THUMB_SIZE * 3 // 4))
        self.assertFalse(reddish(middle(front)))
        self.assertTrue(reddish(middle(turned)))
        view = images.front_view(panorama(marker_yaw=-60), yaw=-60, pitch=0)
        self.assertTrue(reddish(view.getpixel((view.size[0] // 2, view.size[1] // 2))))

    def test_a_scanned_thumbnail_shows_the_opening_view(self):
        with support.TempBase():
            folder_config = {"libraries": []}
            common.save_config(folder_config)
            db.init(log=quiet)
            conn = db.connect()
            try:
                image_id = insert_image(conn, "lib_trips01", "pano.jpg")
                result = {"thumb": b"front", "projection": imageinfo.PROJECTION_EQUIRECT,
                          "preview": jpeg(panorama(marker_yaw=150))}
                # No view: the scanner's own thumbnail.
                self.assertEqual(opening_views.scanned_thumbnail(conn, image_id,
                                                                 "hash-pano.jpg", result),
                                 b"front")
                with conn:
                    opening_views.set_owner(conn, image_id, 150, 0)
                thumb = opening_views.scanned_thumbnail(conn, image_id, "hash-pano.jpg", result)
                self.assertTrue(reddish(middle(thumb)))
                # Not a 360 picture, or no preview to cut it from: its own.
                self.assertEqual(opening_views.scanned_thumbnail(
                    conn, image_id, "hash-pano.jpg", dict(result, projection="")), b"front")
                self.assertEqual(opening_views.scanned_thumbnail(
                    conn, image_id, "hash-pano.jpg", dict(result, preview=None)), b"front")
            finally:
                conn.close()


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class LooksRunTest(support.TempBaseMixin, unittest.TestCase):
    """The looks run works out where 360 photos open (the fake model embeds words too)."""

    def setUp(self):
        super().setUp()
        import cache
        import fake_image_embedder
        import looks
        self.looks = looks
        self.server = fake_image_embedder.FakeImageEmbedder()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.folder = self.base.library_folder("trips")
        self.configure()
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.previews = cache.PreviewStore()
        self.preview = jpeg(panorama(marker_yaw=90))
        self.pano = self.add("pano.jpg", info=described("Red barn",
                                                        "A red barn in a field. Cows."),
                             described=1, prompt_ver=6)
        self.copy = self.add("copy.jpg", content_hash="hash-pano.jpg")
        self.flat = self.add("flat.jpg", projection="", preview=jpeg(panorama()))
        self.teach(looks.DEFAULTS["text_prefix"])

    def teach(self, prefix):
        """The fake model's words (sent after prefix): those the photo is compared with
        look like its view at 90 degrees, the probe words like their probe pictures
        (the model matches words to pictures)."""
        import images
        shots = dict(images.heading_views(self.preview))
        self.server.words[prefix + "Red barn. A red barn in a field."] = shots[90]
        for (_name, picture), words in zip(
                self.looks.probe_pictures(self.looks.settings(self.config)),
                self.looks.PROBE_WORDS):
            self.server.words[prefix + words] = picture

    def configure(self, **settings):
        image_embedding = {"endpoint": "looks", "dims": 16, "batch": 4}
        image_embedding.update(settings)
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips01")],
                               endpoints=[{"name": "looks", "preset": "llamacpp",
                                           "base_url": self.server.base_url}],
                               image_embedding=image_embedding)
        self.config = common.load_config()

    def add(self, path, preview=None, **fields):
        import cache
        fields.setdefault("has_preview", 1)
        image_id = insert_image(self.conn, "lib_trips01", path, **fields)
        self.previews.write("lib_trips01", image_id, preview or self.preview)
        cache.ThumbStore().put(image_id, b"old thumbnail")
        return image_id

    def run_looks(self):
        from test_looks import Context
        ctx = Context()
        summary = self.looks.update(self.conn, self.config, ctx, sleep=lambda _seconds: None)
        return summary, ctx

    def thumb(self, image_id):
        import cache
        with cache.ThumbStore() as thumbs:
            return thumbs.get(image_id)

    def view(self):
        return opening_views.views_for(self.conn, [self.pano]).get(self.pano)

    def test_a_photo_opens_where_its_words_are(self):
        summary, ctx = self.run_looks()
        self.assertEqual(summary["error"], "")
        self.assertEqual((summary["views_pending"], summary["views"], summary["views_turned"]),
                         (1, 1, 1))
        view = self.view()
        self.assertLessEqual(abs(view["yaw"] - 90.0), 22.5, view)
        self.assertEqual((view["pitch"], view["owner"]), (0.0, False))
        self.assertIn("Worked out where 1 360 photo(s) open", ctx.text())
        # Its thumbnail (and its copy's) shows that view now; the flat picture's is as it was.
        self.assertTrue(reddish(middle(self.thumb(self.pano))))
        self.assertTrue(reddish(middle(self.thumb(self.copy))))
        self.assertEqual(self.thumb(self.flat), b"old thumbnail")
        records = {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, ["lib_trips01"], use_cache=False)}
        self.assertEqual(records[self.pano]["ov"][0], round(view["yaw"], 1))
        # Again: nothing is due, nothing is sent.
        before = len(self.server.embed_requests())
        summary, _ctx = self.run_looks()
        self.assertEqual((summary["views_pending"], summary["views"]), (0, 0))
        self.assertEqual(len(self.server.embed_requests()), before)
        # New words: worked out again.
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (described("A field", "Grass. Sky."), self.pano))
        self.conn.commit()
        summary, _ctx = self.run_looks()
        self.assertEqual((summary["views_pending"], summary["views"]), (1, 1))
        # (Grey words: no view beats the front by enough.)
        self.assertEqual(self.view()["yaw"], 0.0)
        self.assertFalse(reddish(middle(self.thumb(self.pano))))

    def test_the_looks_line_says_the_looks_own_time(self):
        # The opening views take their own time (seconds a photo): the looks'
        # line, before theirs, says how long the looks took, without them.
        real = self.looks._opening_views
        seen = {}

        def views(conn, endpoint, options, todo, ctx, summary, *args):
            seen["seconds"] = summary["seconds"]
            seen["lines"] = len(ctx.lines)
            real(conn, endpoint, options, todo, ctx, summary, *args)
            time.sleep(0.5)

        with mock.patch.object(self.looks, "_opening_views", views):
            summary, ctx = self.run_looks()
        self.assertEqual(summary["error"], "")
        self.assertEqual(summary["views"], 1)
        self.assertGreater(summary["embedded"], 0)
        self.assertGreater(seen["seconds"], 0.0)
        self.assertEqual(summary["seconds"], seen["seconds"])
        self.assertGreaterEqual(summary["views_seconds"], 0.5)
        text = ctx.text()
        made = text.index("Made the looks of %d picture(s)" % summary["embedded"])
        self.assertLess(made, text.index("Worked out where 1 360 photo(s) open"))
        self.assertIn("Made the looks", ctx.lines[seen["lines"] - 1][1])

    def test_the_views_due_are_counted_with_the_looks(self):
        # (The Manage page offers Update looks for them, and a scan says so.)
        import scan
        from test_looks import Context
        counted = self.looks.counts(self.conn, self.config, ["lib_trips01"])["lib_trips01"]
        self.assertEqual(counted["views"], 1)
        self.run_looks()
        counted = self.looks.counts(self.conn, self.config, ["lib_trips01"])["lib_trips01"]
        self.assertEqual((counted["pending"], counted["views"]), (0, 0))
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (described("A field", "Grass. Sky."), self.pano))
        self.conn.commit()
        ctx = Context()
        self.assertEqual(scan.looks_waiting(self.conn, self.config, ctx, ["lib_trips01"]), (0, 1))
        self.assertIn("Opening views: 1 360 photo(s) wait", ctx.text())
        self.assertEqual(scan.looks_pending(self.conn, self.config, ctx, ["lib_trips01"]), 0)
        self.configure(opening_views=False)
        counted = self.looks.counts(self.conn, self.config, ["lib_trips01"])["lib_trips01"]
        self.assertEqual(counted["views"], 0)

    def test_pages_that_list_pictures_get_their_views(self):
        # (So their thumbnail addresses change with the view: SITE.thumbUrl.)
        import duplicates
        import export
        self.run_looks()
        flagged = {item["id"]: item for item in export.preview_export(
            self.conn, self.config, "lib_trips01")["flagged"]}
        self.assertEqual(flagged[self.copy]["ov"][0], round(self.view()["yaw"], 1))
        self.assertTrue(all("ov" not in item for image_id, item in flagged.items()
                            if image_id not in (self.pano, self.copy)))
        self.assertEqual(duplicates._member({"i": 3, "ov": [30, 0]}, {})["ov"], [30, 0])
        self.assertNotIn("ov", duplicates._member({"i": 4}, {}))
        self.assertIn("ov", duplicates._MEMBER_KEYS)

    def test_a_model_that_does_not_match_words_leaves_the_front(self):
        self.server.words.clear()
        summary, ctx = self.run_looks()
        self.assertEqual(summary["error"], "")
        self.assertEqual((summary["views_pending"], summary["views"]), (1, 0))
        self.assertIsNone(self.view())
        self.assertIn("does not match words to pictures", ctx.text())
        self.assertEqual(self.thumb(self.pano), b"old thumbnail")

    def test_a_model_of_pictures_only(self):
        self.server.images_only = True
        summary, ctx = self.run_looks()
        self.assertEqual(summary["error"], "")
        self.assertGreater(summary["embedded"], 0)
        self.assertIsNone(self.view())
        self.assertIn("cannot be worked out", ctx.text())

    def test_turned_off(self):
        self.run_looks()
        self.assertIsNotNone(self.view())
        self.configure(opening_views=False)
        texts = len(self.server.texts())
        summary, ctx = self.run_looks()
        self.assertEqual(summary["views_dropped"], 2)
        self.assertEqual(len(self.server.texts()), texts)
        self.assertIsNone(self.view())
        self.assertFalse(reddish(middle(self.thumb(self.pano))))
        self.assertIn("open at the front again", ctx.text())

    def test_a_preview_that_cannot_be_read(self):
        self.previews.write("lib_trips01", self.pano, b"broken")
        summary, ctx = self.run_looks()
        self.assertEqual((summary["views"], summary["views_failed"]), (0, 1))
        self.assertIn("its opening view cannot be worked out", ctx.text())

    def test_the_words_sent(self):
        self.run_looks()
        # Another prefix: the view is worked out again, with the words after it.
        self.configure(text_prefix="query: ")
        self.teach("query: ")
        summary, _ctx = self.run_looks()
        self.assertEqual(summary["views_pending"], 1)
        self.assertIn("query: Red barn. A red barn in a field.", self.server.texts())


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class RouteTest(unittest.TestCase):
    """The admin's "Open here" and what /api/image says."""

    def setUp(self):
        import cache
        import jobs
        import serve
        self.base = support.TempBase()
        self.base.__enter__()
        self.addCleanup(self.base.__exit__, None, None, None)
        folder = self.base.library_folder("trips")
        self.base.write_config([support.library("Trips", folder, id="lib_trips01")])
        db.init(log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.pano = insert_image(self.conn, "lib_trips01", "pano.jpg", has_preview=1)
        self.copy = insert_image(self.conn, "lib_trips01", "copy.jpg",
                                 content_hash="hash-pano.jpg", has_preview=1)
        self.flat = insert_image(self.conn, "lib_trips01", "flat.jpg", projection="")
        previews = cache.PreviewStore()
        for image_id in (self.pano, self.copy):
            previews.write("lib_trips01", image_id, jpeg(panorama(marker_yaw=-120)))
        self.app = serve.build_app(init_db=False, job_manager=jobs.JobManager())
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def post(self, image_id, body, **kwargs):
        import admin
        return self.client.post("%s/images/%d/opening-view" % (admin.URL_PREFIX, image_id),
                                data=json.dumps(body), content_type="application/json",
                                headers={"X-Search-Images": "1"}, **kwargs)

    def thumb(self, image_id):
        return self.client.get("/thumb/%d" % image_id).get_data()

    def test_open_here(self):
        answer = self.post(self.pano, {"yaw": -120, "pitch": 4, "fov": 75})
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        answer = answer.get_json()
        self.assertEqual(answer["record"], {"ov": [-120.0, 4.0]})
        self.assertEqual(answer["ids"], [self.pano, self.copy])
        self.assertEqual(answer["opening_view"], {"yaw": -120.0, "pitch": 4.0, "fov": None,
                                                  "owner": True, "auto": None})
        # The thumbnails (the copy's too) show it.
        self.assertTrue(reddish(middle(self.thumb(self.pano))))
        self.assertTrue(reddish(middle(self.thumb(self.copy))))
        detail = self.client.get("/api/image/%d" % self.copy).get_json()
        self.assertTrue(detail["opening_view"]["owner"])
        index = self.client.get("/api/index").get_json()
        ov = {record["i"]: record.get("ov") for record in index["records"]}
        self.assertEqual(ov[self.pano], [-120.0, 4.0])
        self.assertIsNone(ov[self.flat])
        # A visitor is told where it opens, not whose view it is.
        theirs = self.client.get("/api/image/%d" % self.pano,
                                 environ_base={"REMOTE_ADDR": "192.0.2.5"})
        self.assertEqual(theirs.status_code, 200)
        self.assertEqual(theirs.get_json()["opening_view"],
                         {"yaw": -120.0, "pitch": 4.0, "fov": None})
        self.assertNotIn("opening_view", self.client.get("/api/image/%d" % self.flat).get_json())
        # Forgotten: the front again.
        answer = self.post(self.pano, {"clear": True}).get_json()
        self.assertEqual(answer["record"], {})
        self.assertEqual(answer["opening_view"]["owner"], False)
        self.assertFalse(reddish(middle(self.thumb(self.pano))))

    def test_refused(self):
        response = self.post(self.pano, {"yaw": "left", "pitch": 100})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.get_json()["fields"]), {"yaw", "pitch"})
        self.assertEqual(self.post(self.flat, {"yaw": 0, "pitch": 0}).status_code, 404)
        self.assertEqual(self.post(999999, {"yaw": 0, "pitch": 0}).status_code, 404)
        self.assertEqual(self.post(self.flat, {"clear": True}).status_code, 404)
        # Only the admin, from this computer.
        self.assertEqual(self.post(self.pano, {"yaw": 0, "pitch": 0},
                                   environ_base={"REMOTE_ADDR": "192.0.2.5"}).status_code, 403)
        self.assertFalse(opening_views.any_views(self.conn))


if __name__ == "__main__":
    unittest.main()
