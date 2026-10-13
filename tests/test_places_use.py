"""Place names where they are used: scanning, the Manage page's routes,
/api/image, the search index and exports.

The place names are the made-up GeoNames files of test_places; the
download comes from a fake server on 127.0.0.1.
"""

import io
import json
import os
import re
import sqlite3
import unittest
from unittest import mock

import support

import cache
import common
import db
import export
import jobs
import places
import search_index
from test_admin import AdminFixture, insert_image
from test_places import USED, FakeGeoNames, install_places, write_geonames

try:
    import flask
except ImportError:
    flask = None

quiet = support.quiet
REMOTE = {"REMOTE_ADDR": "192.0.2.5"}
TOWN = (37.08, -8.234)          # 1.6 km from Albufeira (test_places.TOWNS)
OFF_COAST = (36.3, -9.5)        # out at sea, about 90 km from Sagres


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class ScanTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import imagegen
        self.imagegen = imagegen
        self.folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips001")])
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        self.addCleanup(places.release)

    def picture(self, name, gps):
        return self.imagegen.jpeg_with_exif(self.folder, name, gps=gps)

    def scan(self):
        import scan
        ctx = jobs.JobContext("t")
        return scan.run(self.config, ctx, describe_images=False)["libraries"][0], ctx

    def rows(self):
        conn = db.connect()
        try:
            return {row["path"]: dict(row) for row in conn.execute("SELECT * FROM images")}
        finally:
            conn.close()

    def test_new_pictures_get_their_place(self):
        install_places()
        self.picture("town.jpg", TOWN)
        self.picture("sea.jpg", OFF_COAST)
        self.picture("none.jpg", None)
        summary, _ctx = self.scan()
        rows = self.rows()
        self.assertEqual((rows["town.jpg"]["place_city"], rows["town.jpg"]["place_region"],
                          rows["town.jpg"]["place_country"]),
                         ("Albufeira", "Algarve", "Portugal"))
        self.assertLess(rows["town.jpg"]["place_km"], 3)
        self.assertEqual(rows["sea.jpg"]["place_city"], "")
        self.assertEqual(rows["sea.jpg"]["place_region"], "Algarve")
        self.assertIsNone(rows["none.jpg"]["place_km"])
        # Looked up as they were read: nothing was left for afterwards.
        self.assertEqual(summary["places"]["checked"], 0)
        self.assertIsNone(places._shared)

    def test_pictures_scanned_before_get_their_place_later(self):
        self.picture("town.jpg", TOWN)
        summary, _ctx = self.scan()
        self.assertNotIn("places", summary)
        self.assertIsNone(self.rows()["town.jpg"]["place_km"])
        install_places()
        summary, ctx = self.scan()
        self.assertEqual(summary["places"]["checked"], 1)
        self.assertEqual(self.rows()["town.jpg"]["place_city"], "Albufeira")
        self.assertIn("Place names for 1 picture", "\n".join(
            entry["message"] for entry in ctx.snapshot(log_tail=100)["log_tail"]))

    def test_a_picture_that_moved_gets_its_new_place(self):
        install_places()
        path = self.picture("trip.jpg", TOWN)
        self.scan()
        self.assertEqual(self.rows()["trip.jpg"]["place_city"], "Albufeira")
        # The same name, a new position (the file was edited): looked up again.
        os.remove(path)
        self.imagegen.jpeg_with_exif(self.folder, "trip.jpg", gps=(48.86, 2.35),
                                     image=self.imagegen.textured_image((80, 60)))
        os.utime(path, (2000000000, 2000000000))
        self.scan()
        self.assertEqual(self.rows()["trip.jpg"]["place_city"], "Paris")


@unittest.skipIf(flask is None, "Flask is not installed")
class RouteTest(AdminFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(places.release)
        self.town = insert_image(self.conn, "lib_family1", "town.jpg", lat=TOWN[0],
                                 lon=TOWN[1])
        self.plain = insert_image(self.conn, "lib_family1", "plain.jpg")

    def test_the_state_without_and_with_place_names(self):
        state = self.ok(self.get("/places"))
        self.assertFalse(state["available"])
        self.assertFalse(state["rebuild"])
        self.assertEqual(state["pictures"], {"located": 1, "named": 0, "region_only": 0,
                                             "far": 0, "pending": 1})
        self.assertIsNone(state["job"])
        self.assertIn("geonames.org", state["attribution_url"])
        install_places()
        state = self.ok(self.get("/places"))
        self.assertTrue(state["available"])
        self.assertEqual(state["cities"], len(USED))
        self.assertEqual(state["error"], "")
        # Looking at the state never loads the towns.
        self.assertIsNone(places._shared)

    def test_label_needs_place_names(self):
        response = self.post("/places/label", {})
        self.assertEqual(response.status_code, 409)
        self.assertIn("download them first", response.get_json()["error"])
        install_places()
        answer = self.ok(self.post("/places/label", {}), 202)
        self.assertEqual(answer["job"]["kind"], "places")
        self.assertEqual(answer["job"]["params"], {})
        self.assertEqual(answer["places"]["job"]["id"], answer["job"]["id"])
        # The same job again is not queued twice.
        self.assertFalse(self.ok(self.post("/places/label", {}), 202)["added"])
        answer = self.ok(self.post("/places/label", {"redo": True}), 202)
        self.assertEqual(answer["job"]["params"], {"redo": True})

    def test_an_index_of_another_version_can_be_built_again(self):
        install_places()
        outdated = sqlite3.connect(places.places_db_path())
        outdated.execute("UPDATE meta SET value = '0' WHERE key = 'format'")
        outdated.commit()
        outdated.close()
        state = self.ok(self.get("/places"))
        self.assertFalse(state["available"])
        self.assertTrue(state["rebuild"])
        answer = self.ok(self.post("/places/label", {}), 202)
        self.assertEqual(answer["job"]["kind"], "places")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertTrue(self.manager.start())
            self.assertTrue(self.manager.wait_idle(30))
        state = self.ok(self.get("/places"))
        self.assertTrue(state["available"], state)
        self.assertEqual(state["last"]["state"], "done", state["last"])
        self.assertEqual(state["pictures"]["named"], 1)

    def test_requests_that_make_no_sense(self):
        install_places()
        self.assertEqual(self.post("/places/label", {"redo": "yes"}).status_code, 400)
        self.assertEqual(self.post("/places/fetch", {}).status_code, 404)
        self.assertEqual(self.get("/places/download").status_code, 405)
        response = self.client.post("/api/admin/places/download", data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post("/places/download", {}, environ_base=REMOTE).status_code,
                         403)
        self.assertEqual(self.get("/places", environ_base=REMOTE).status_code, 403)
        # The address of the download is never taken from a request.
        self.assertEqual(self.ok(self.post("/places/download", {"url": "http://x"}), 202)
                         ["job"]["params"], {"download": True})

    def test_download_job_end_to_end(self):
        source = write_geonames(self.base.join("source"))
        server = FakeGeoNames(source)
        self.addCleanup(server.close)
        with mock.patch.object(places, "GEONAMES_URL", server.url):
            self.ok(self.post("/places/download", {}), 202)
            self.assertTrue(self.manager.start())
            self.assertTrue(self.manager.wait_idle(30))
        state = self.ok(self.get("/places"))
        self.assertTrue(state["available"])
        self.assertEqual(state["last"]["state"], "done", state["last"])
        self.assertEqual(state["pictures"]["named"], 1)
        self.assertEqual(state["last"]["summary"]["places"]["named"], 1)
        detail = self.client.get("/api/image/%d" % self.town).get_json()
        self.assertEqual(detail["place"]["city"], "Albufeira")
        self.assertEqual(detail["place"]["country"], "Portugal")
        self.assertLess(detail["place"]["km"], 3)
        self.assertIsNone(self.client.get("/api/image/%d" % self.plain).get_json()["place"])
        records = {record["i"]: record for record in
                   self.client.get("/api/index").get_json()["records"]}
        self.assertEqual(records[self.town]["ci"], "Albufeira")
        self.assertNotIn("co", records[self.plain])

    def test_a_failed_download_is_reported(self):
        with mock.patch.object(places, "GEONAMES_URL", "http://127.0.0.1:9/"):
            self.ok(self.post("/places/download", {}), 202)
            self.assertTrue(self.manager.start())
            self.assertTrue(self.manager.wait_idle(30))
        state = self.ok(self.get("/places"))
        self.assertFalse(state["available"])
        self.assertEqual(state["last"]["state"], "failed")
        self.assertIn("Could not download", state["last"]["error"])

    def test_the_index_notices_new_places(self):
        install_places()
        etag = self.client.get("/api/index").headers["ETag"]
        places.label_pictures(self.conn, jobs.JobContext("t"))
        response = self.client.get("/api/index", headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 200)

    def test_the_manage_page_has_the_panel(self):
        html = self.client.get("/manage").get_data(as_text=True)
        self.assertIn('id="place-names"', html)
        self.assertIn("/static/place-names.js", html)


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class ExportTest(support.TempBaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        from PIL import Image
        self.folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", self.folder, id="lib_trips001")])
        self.config = common.load_config()
        db.init(self.config, log=quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(places.release)
        install_places()
        info = {"title": "Beach", "description": "A beach.", "photo_type": "photo",
                "document_type": "none", "tags": ["beach"], "people": "none",
                "sensitive": "none", "private_info": False}
        for number, (lat, lon) in enumerate((TOWN, (48.86, 2.35))):
            name = "p%d.jpg" % number
            Image.new("RGB", (40, 30), (number * 90, 120, 200)).save(
                os.path.join(self.folder, name), "JPEG")
            insert_image(self.conn, "lib_trips001", name, lat=lat, lon=lon, described=1,
                         prompt_ver=5, info=json.dumps(info),
                         taken_at="2019-05-1%dT10:00:00" % number, taken_source="exif")
        places.label_pictures(self.conn, jobs.JobContext("t"))
        with open(os.path.join(self.folder, "p0.jpg"), "rb") as handle:
            thumb = handle.read()
        with cache.ThumbStore() as thumbs:
            for row in self.conn.execute("SELECT id FROM images"):
                thumbs.put(row["id"], thumb)

    def export(self, location):
        out = self.base.join("site-%s" % location)
        export.export_library(export.LogContext(quiet), self.conn, self.config, "lib_trips001",
                              out, {"location": location, "images": "none"})
        records = []
        data = os.path.join(out, "data")
        for name in sorted(os.listdir(data)):
            if name.startswith("index-"):
                with open(os.path.join(data, name), encoding="utf-8") as handle:
                    text = handle.read()
                records.extend(json.loads(text[text.index("["):text.rindex("]") + 1]))
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            page = handle.read()
        return records, page

    def test_place_names_only_with_location_and_with_their_credit(self):
        records, page = self.export(False)
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertFalse({"ci", "rg", "co", "cc", "km"} & set(record), record)
        self.assertNotIn("GeoNames", page)
        self.assertNotIn("Albufeira", page)
        self.assertFalse(support.page_config(page)["capabilities"].get("places"))
        records, page = self.export(True)
        self.assertEqual(sorted(record["ci"] for record in records), ["Albufeira", "Paris"])
        self.assertIn('href="https://www.geonames.org/"', page)
        self.assertIn('rel="license"', page)
        self.assertTrue(support.page_config(page)["capabilities"]["places"])
        footers = re.findall(r'<footer class="export-credits">(.*?)</footer>', page)
        self.assertEqual(footers, [export.PLACES_CREDIT])

    def test_the_credit_follows_the_owners_credits(self):
        library = self.config["libraries"][0]
        library["export"] = {"credits": "Photos by me & mine"}
        page = export.render_page("page.html", "Trips", "Trips", {"places": True}, "x",
                                  export.library_options(library, {"location": True}))
        footer = re.search(r'<footer class="export-credits">(.*?)</footer>', page).group(1)
        self.assertEqual(footer, "Photos by me &amp; mine<br>" + export.PLACES_CREDIT)
        # The map page has no foot: only the GeoNames credit, in a corner.
        page = export.render_page("map.html", "Trips", "Trips", {"places": True}, "x",
                                  export.library_options(library, {"location": True}))
        self.assertNotIn("<footer", page)
        self.assertNotIn("Photos by me", page)
        credit = re.search(r'<p id="map-credit" class="export-credits map-credit">(.*?)</p>',
                           page).group(1)
        self.assertEqual(credit, export.PLACES_CREDIT)
        self.assertIn('href="https://www.geonames.org/"', credit)
        page = export.render_page("map.html", "Trips", "Trips", {"places": False}, "x",
                                  export.library_options(library, {"location": True}))
        self.assertNotIn("GeoNames", page)


class IndexRecordTest(support.TempBaseMixin, unittest.TestCase):

    def test_the_state_of_the_records_changes_with_the_place(self):
        folder = self.base.library_folder("Trips")
        self.base.write_config([support.library("Trips", folder, id="lib_trips001")])
        db.init(log=quiet)
        conn = db.connect()
        self.addCleanup(conn.close)
        image_id = insert_image(conn, "lib_trips001", "a.jpg", lat=1.0, lon=2.0)
        before = search_index.data_state(conn, ["lib_trips001"])
        conn.execute("UPDATE images SET place_km = 3, place_city = 'X', place_country = 'Y' "
                     "WHERE id = ?", (image_id,))
        conn.commit()
        self.assertNotEqual(search_index.data_state(conn, ["lib_trips001"]), before)


if __name__ == "__main__":
    unittest.main()
