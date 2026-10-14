"""Tests for named_places.py (named places from the street maps) and where they show:
the search records, exports (a library's and a trip's), the admin's answers, jobs and
the command line.

The street maps are made up (tests/namedgen.py): a made-up area with a
made-up aquarium, cafe, zoo, airport, beach, park and homes, a diner by a
casino, a gift shop by a snack bar, two cafes side by side, one tower with
two names, a theatre by a museum, a street with a kiosk, a town hall and an
inn, and a college, as vector tiles in a PMTiles archive. Nothing here uses
the network.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unittest
from unittest import mock

import support
import test_trip_export

import common
import db
import export
import jobs
import named_places
import place_estimates
import search_index
import serve
import street_maps
import tracks
import trip_export
from namedgen import area, offset, poi, square, write_streets
from test_search_index import insert_image

try:
    import flask
except ImportError:
    flask = None

NODE = shutil.which("node")
# The made-up area: a harbour (BASE) and what is around it.
BASE = (10.3, 20.4)
ZOO = offset(*BASE, north_m=-1500)
HOMES = offset(*BASE, east_m=1500)
AIRPORT = offset(*BASE, north_m=-1500, east_m=1500)
BAKERY = offset(*BASE, north_m=-600, east_m=600)
PARK = offset(*BASE, north_m=3000)
TOWER = offset(*BASE, north_m=-600, east_m=-600)
BEACH_WEST = offset(*BASE, north_m=1500, east_m=-2000)
BEACH_EAST = offset(*BASE, north_m=1550, east_m=2000)
# (For the rules of the description and of doubt, far from the rest.)
DINER = offset(*BASE, east_m=-3000)
SHOPS = offset(*BASE, east_m=-3000, north_m=-1500)
TWINS = offset(*BASE, east_m=-3000, north_m=1500)
TOWERS = offset(*BASE, east_m=-3000, north_m=3000)
THEATRE = offset(*BASE, east_m=-1500, north_m=-3000)
STREET = offset(*BASE, east_m=1500, north_m=-3000)
CAMPUS = offset(*BASE, east_m=3000, north_m=-3000)
FEATURES = [
    poi(1001, "Harbour Aquarium", "aquarium", *offset(*BASE, east_m=60), min_zoom=16),
    poi(1002, "Seaside Cafe", "cafe", *offset(*BASE, north_m=10), min_zoom=16),
    poi(1003, "Stop 4", "bus_stop", *offset(*BASE, east_m=5), min_zoom=18),
    poi(1004, "Old Lighthouse", "lighthouse", *offset(*BASE, north_m=600)),
    area(2001, "zoo", square(*ZOO, 250)),
    poi(2001, "City Zoo", "zoo", *ZOO, min_zoom=13),
    poi(2002, "Animal Stage", "theatre", *offset(*ZOO, east_m=40), min_zoom=16),
    area(3001, "residential", square(*HOMES, 200)),
    poi(3001, "Quiet Acres", "residential", *HOMES, min_zoom=14),
    poi(3002, "Number Nine", "house", *offset(*HOMES, east_m=5), min_zoom=17),
    area(4001, "aerodrome", square(*AIRPORT, 600)),
    poi(4001, "Central Airport", "aerodrome", *AIRPORT, min_zoom=11),
    poi(4002, "Gate Cafe", "cafe", *offset(*AIRPORT, north_m=5), min_zoom=16),
    poi(4003, "Gate Station", "station", *offset(*AIRPORT, east_m=20), min_zoom=15),
    poi(4101, "Thimble Bakery", "bakery", *BAKERY, min_zoom=16),
    # A long beach: its name's point is 4 km from its west end, in other tiles.
    area(5001, "beach", [BEACH_WEST, (BEACH_WEST[0], BEACH_EAST[1]), BEACH_EAST,
                         (BEACH_EAST[0], BEACH_WEST[1])]),
    poi(5001, "Gull Beach", "beach", *offset(*BEACH_EAST, north_m=-25, east_m=-10),
        min_zoom=12),
    area(6001, "park", square(*PARK, 300)),
    poi(6001, "Pine Park", "park", *PARK, min_zoom=14),
    # A name in another script, with an English one.
    poi(7001, "\u6e2f\u306e\u5854", "attraction", *TOWER, min_zoom=13,
        **{"name:en": "Harbour Tower"}),
    # A diner 15 m away, a casino 70 m away (a meal: the diner).
    poi(8001, "Corner Diner", "restaurant", *offset(*DINER, north_m=15), min_zoom=16),
    poi(8002, "Lucky Casino", "casino", *offset(*DINER, east_m=70), min_zoom=16),
    # A gift shop 14 m away, a snack bar 16 m away (the nearer one).
    poi(8101, "Gift Corner", "gift", *offset(*SHOPS, north_m=14), min_zoom=16),
    poi(8102, "Wrap Place", "fast_food", *offset(*SHOPS, north_m=-16), min_zoom=16),
    # Two cafes side by side (no position tells them apart).
    poi(8201, "Twin Cafe North", "cafe", *offset(*TWINS, north_m=9), min_zoom=16),
    poi(8202, "Twin Cafe South", "cafe", *offset(*TWINS, north_m=-12), min_zoom=16),
    # One tower, two names (at one spot: no doubt).
    poi(8301, "Clock Tower", "attraction", *offset(*TOWERS, east_m=40), min_zoom=13),
    poi(8302, "Old Clock", "attraction", *offset(*TOWERS, east_m=42, north_m=2), min_zoom=13),
    # A theatre 14 m away, a museum 54 m away (the scene says which).
    poi(8401, "Grand Theatre", "theatre", *offset(*THEATRE, north_m=14), min_zoom=16),
    poi(8402, "Toy Museum", "museum", *offset(*THEATRE, north_m=-54), min_zoom=16),
    # A street: a kiosk 50 m away, the town hall 80 m away (a landmark), an inn 30 m
    # away, and a boat-tour operator's stand 10 m away.
    poi(8501, "Ferry Kiosk", "attraction", *offset(*STREET, east_m=50), min_zoom=16),
    poi(8502, "Town Hall", "townhall", *offset(*STREET, east_m=-80), min_zoom=13),
    poi(8503, "Corner Inn", "hotel", *offset(*STREET, north_m=30), min_zoom=16),
    poi(8504, "Canal Boat Tours", "attraction", *offset(*STREET, north_m=-10), min_zoom=16),
    # A college's point (no outline).
    poi(8601, "Example College", "college", *CAMPUS, min_zoom=15),
]
# What the describing model says about pictures (named_places.picture_facts).
INDOOR = {"title": "Fish", "setting": "indoor", "food": []}
INDOOR_MEAL = {"title": "Dinner", "setting": "indoor", "food": ["fish"], "event": "meal"}
OUTDOOR = {"title": "A street", "setting": "outdoor", "food": []}
OUTDOOR_MEAL = {"title": "Lunch", "setting": "outdoor", "food": ["bread"], "event": "meal"}
VEHICLE = {"title": "From the car", "setting": "vehicle", "food": []}


class Fixture(support.TempBaseMixin):
    """A settings folder with one library and the made-up street maps."""

    def setUp(self):
        super().setUp()
        self.family = self.base.library_folder("family")
        self.base.write_config([support.library("Family", self.family, id="lib_family1")])
        self.config = common.load_config()
        db.init(self.config, log=support.quiet)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(street_maps.shared.forget)
        write_streets(FEATURES)

    def reader(self):
        archive = street_maps.open_local(street_maps.archive_path())
        self.addCleanup(archive.read.close)
        return named_places.Reader(archive)

    def picture(self, path, lat=None, lon=None, info=None, gps=None, **fields):
        if info is not None:
            fields.update(info=json.dumps(info), described=1, prompt_ver=6)
        if gps is not None:
            fields["exif"] = json.dumps({"GPS": gps})
        return insert_image(self.conn, "lib_family1", path, lat=lat, lon=lon,
                            content_hash="hash-" + path, **fields)


def facts(**values):
    """named_places.Facts: ANY with some values changed (venues: shops and places to eat
    alike)."""
    if "venues" in values:
        venues = values.pop("venues")
        values.setdefault("shops", venues)
        values.setdefault("eat", venues)
    return named_places.ANY._replace(**values)


class FindTest(Fixture, unittest.TestCase):

    def find(self, where, accuracy=10.0, points=True, venues=True, **values):
        found = named_places.find(where[0], where[1], accuracy, self.reader(),
                                  facts(points=points, venues=venues, **values))
        return (found.name, found.kind, found.inside) if found else None

    def names(self, where, accuracy=10.0, **values):
        return [place.name for place in named_places.candidates(
            where[0], where[1], accuracy, self.reader(), facts(**values))]

    def test_a_sight_near_wins_over_a_cafe_beside_it_and_a_bus_stop_never(self):
        self.assertEqual(self.find(BASE), ("Harbour Aquarium", "aquarium", False))
        found = named_places.candidates(BASE[0], BASE[1], 10.0, self.reader())
        names = [place.name for place in found]
        self.assertIn("Seaside Cafe", names)
        self.assertNotIn("Stop 4", names)
        self.assertNotIn("Old Lighthouse", names)      # (600 m: out of its reach)
        aquarium = found[0]
        self.assertTrue(55 <= aquarium.metres <= 65, aquarium)

    def test_a_venue_names_a_picture_taken_in_it_not_one_of_the_street(self):
        at = offset(*BAKERY, north_m=5)
        self.assertEqual(self.find(at), ("Thimble Bakery", "bakery", False))
        self.assertIsNone(self.find(at, venues=False))
        self.assertIsNone(self.find(at, points=False))
        self.assertIsNone(self.find(offset(*BAKERY, north_m=60)))

    def test_inside_an_area_its_name_wins_over_a_stage_in_it(self):
        self.assertEqual(self.find(offset(*ZOO, east_m=10)), ("City Zoo", "zoo", True))
        # Areas count also without points of interest (a track, a vehicle).
        self.assertEqual(self.find(offset(*ZOO, east_m=10), points=False),
                         ("City Zoo", "zoo", True))

    def test_never_a_home(self):
        self.assertIsNone(self.find(HOMES))
        self.assertIsNone(self.find(offset(*HOMES, east_m=5)))

    def test_an_airport_is_the_place_whatever_is_inside_it(self):
        self.assertEqual(self.find(offset(*AIRPORT, north_m=5)),
                         ("Central Airport", "aerodrome", True))
        self.assertEqual(named_places.kind_words("aerodrome"), "airport")

    def test_an_area_named_far_away_is_named_from_the_zooms_above(self):
        middle = offset(BEACH_WEST[0], BEACH_WEST[1], north_m=25, east_m=100)
        self.assertEqual(self.find(middle), ("Gull Beach", "beach", True))

    def test_a_position_that_may_be_off_must_be_deep_enough_inside(self):
        near_edge = offset(*PARK, north_m=280)
        self.assertEqual(self.find(near_edge, accuracy=10), ("Pine Park", "park", True))
        # (Off by 100 m: it may well be outside; its name's point is not inside it.)
        self.assertIsNone(self.find(near_edge, accuracy=100))
        self.assertEqual(self.find(PARK, accuracy=500), ("Pine Park", "park", True))
        # Far off: points of interest no more, only areas.
        self.assertIsNone(self.find(BASE, accuracy=named_places.MAX_ACCURACY_M + 1))

    def test_a_name_in_another_script_shows_its_english_one(self):
        self.assertEqual(self.find(TOWER), ("Harbour Tower", "attraction", False))
        self.assertEqual(named_places.display_name({"name": "Caf\u00e9 Nord"}), "Caf\u00e9 Nord")

    def test_tiles_are_read_back_as_written(self):
        archive = street_maps.open_local(street_maps.archive_path())
        self.addCleanup(archive.read.close)
        z = 15
        fx, fy = named_places._tile_xy(BASE[0], BASE[1], z)
        layers = named_places.decode_tile(archive.get(z, int(fx), int(fy)),
                                          layers=("pois", "earth"))
        names = {feature.props.get("name") for feature in layers["pois"][1]}
        self.assertIn("Harbour Aquarium", names)
        self.assertEqual(layers["earth"][1][0].type, 3)
        with self.assertRaises(named_places.TileError):
            named_places.decode_tile(b"\x1a\x05\x0a\x09")

    def test_a_place_to_eat_only_for_food_or_a_picture_of_such_a_place(self):
        # (The cafe is 10 m away, the aquarium 60 m.)
        indoor = named_places.picture_facts(json.dumps(INDOOR))
        self.assertFalse(indoor.eat)
        self.assertNotIn("Seaside Cafe", self.names(BASE, 30, **indoor._asdict()))
        cafe = named_places.picture_facts(json.dumps(
            {"title": "Two cups", "setting": "indoor", "scene": "coffee shop"}))
        self.assertTrue(cafe.eat)
        self.assertIn("Seaside Cafe", self.names(BASE, 30, **cafe._asdict()))
        meal = named_places.picture_facts(json.dumps(INDOOR_MEAL))
        self.assertTrue(meal.eat and meal.meal)
        # Outdoors a meal names the place to eat (not as a sight: no meal indoors).
        outside = named_places.picture_facts(json.dumps(OUTDOOR_MEAL))
        self.assertTrue(outside.eat and not outside.meal)
        # A shop or a store aisle is not the cafe next to it, but a shop is a shop.
        store = named_places.picture_facts(json.dumps(
            {"title": "Shelves", "setting": "indoor", "scene": "store aisle"}))
        self.assertEqual((store.eat, store.shops), (False, True))
        self.assertEqual(self.find(SHOPS, 30, **store._asdict()),
                         ("Gift Corner", "gift", False))

    def test_a_meal_indoors_names_the_place_to_eat_not_the_casino_down_the_street(self):
        meal = named_places.picture_facts(json.dumps(INDOOR_MEAL))._asdict()
        plain = named_places.picture_facts(json.dumps(INDOOR))._asdict()
        self.assertEqual(self.find(DINER, 65, **plain), ("Lucky Casino", "casino", False))
        self.assertEqual(self.find(DINER, 65, **meal), ("Corner Diner", "restaurant", False))
        # ... but by an aquarium, the aquarium (its own restaurant).
        self.assertEqual(self.find(BASE, 65, **meal), ("Harbour Aquarium", "aquarium", False))

    def test_places_to_eat_shop_and_stay_go_by_their_distance(self):
        # (A gift shop 14 m away, a snack bar 16 m: the gift shop, whatever their reach.)
        self.assertEqual(self.find(SHOPS, 30), ("Gift Corner", "gift", False))

    def test_a_position_that_cannot_tell_two_places_apart_names_neither(self):
        self.assertIsNone(self.find(TWINS, 5))
        found = named_places.candidates(TWINS[0], TWINS[1], 5, self.reader())
        self.assertEqual([place.name for place in found[:2]],
                         ["Twin Cafe North", "Twin Cafe South"])
        self.assertIsNone(named_places.choose(found, 5))
        # The area the position is in, rather.
        twins = named_places.choose(found + [named_places.Place("Old Market", "pedestrian", 0,
                                                                True, 3.0)], 5)
        self.assertEqual(twins.name, "Old Market")
        # One tower, two names at one spot: no doubt.
        self.assertEqual(self.find(TOWERS, 10), ("Clock Tower", "attraction", False))

    def test_the_kind_its_scene_says_and_the_name_its_description_says(self):
        # (A theatre 14 m away, a museum 54 m: taken indoors, 65 m off, no telling.)
        self.assertIsNone(self.find(THEATRE, 65, indoor=True))
        theatre = named_places.picture_facts(json.dumps(
            {"title": "Velvet seats", "setting": "indoor", "scene": "theater"}))
        self.assertIn("theatre", theatre.hints)
        self.assertEqual(self.find(THEATRE, 65, **theatre._asdict()),
                         ("Grand Theatre", "theatre", False))
        toys = named_places.picture_facts(json.dumps(
            {"title": "Children at the Toy Museum", "setting": "indoor", "scene": "hall"}))
        self.assertEqual(self.find(THEATRE, 65, **toys._asdict()),
                         ("Toy Museum", "museum", False))
        # A place it names wins even where the facts would leave it out: the diner of a
        # street picture that names it.
        street = named_places.picture_facts(json.dumps(
            {"title": "Friends by the Corner Diner sign", "setting": "outdoor",
             "scene": "city street"}))
        self.assertEqual(self.find(DINER, 30, **street._asdict()),
                         ("Corner Diner", "restaurant", False))

    def test_names_said_in_a_description(self):
        def said(name, text):
            return named_places.named_in(name, named_places.plain_words(text))
        self.assertTrue(said("Harbour Aquarium", "Fish at the Harbour Aquarium."))
        self.assertTrue(said("Grand Theatre", "the grand theater at night"))
        self.assertTrue(said("Westpoint: Museum of Toys", "Westpoint at dusk"))
        self.assertTrue(said("Gull\u2019s Gift Shop", "Posing in Gull's shop"))
        self.assertTrue(said("Caf\u00e9 Nord", "cups at cafe nord"))
        self.assertFalse(said("Harbour Aquarium", "harbour views"))
        self.assertFalse(said("Grand Theatre", "a grand old theatre"))
        # A one-word name too common, or too short, is not looked for.
        self.assertFalse(said("Waterfront", "the city waterfront"))
        self.assertFalse(said("Dam", "by the dam"))
        self.assertTrue(said("Brightbeans", "a Brightbeans cup"))

    def test_a_street_scene_names_landmarks_and_what_is_right_there(self):
        street = named_places.picture_facts(json.dumps(
            {"title": "Bicycles", "setting": "outdoor", "scene": "city street"}))
        self.assertTrue(street.street)
        names = self.names(STREET, 30, **street._asdict())
        self.assertIn("Town Hall", names)
        self.assertNotIn("Ferry Kiosk", names)
        self.assertNotIn("Corner Inn", names)
        # Not a street: the kiosk, and the inn's grounds.
        lawn = named_places.picture_facts(json.dumps(
            {"title": "Picnic", "setting": "outdoor", "scene": "lawn"}))
        self.assertFalse(lawn.street)
        names = self.names(STREET, 30, **lawn._asdict())
        self.assertIn("Ferry Kiosk", names)
        self.assertIn("Corner Inn", names)
        # A parking lot is the lot of the place by it, not a street.
        self.assertFalse(named_places.picture_facts(json.dumps(
            {"setting": "outdoor", "scene": "parking lot"})).street)
        # A boat-tour operator's stand names no place.
        self.assertNotIn("Canal Boat Tours", self.names(STREET, 30))

    def test_indoors_the_point_of_an_area_only_close_by(self):
        at = offset(*CAMPUS, north_m=100)
        self.assertEqual(self.find(at, 30), ("Example College", "college", False))
        self.assertIsNone(self.find(at, 30, indoor=True))
        self.assertEqual(self.find(offset(*CAMPUS, north_m=10), 30, indoor=True),
                         ("Example College", "college", False))

    def test_an_approximate_position_names_no_venue_and_only_what_reaches_it(self):
        rough = dict(approximate=True, shops=False, eat=False)
        self.assertNotIn("Seaside Cafe", self.names(BASE, 100, **rough))
        self.assertEqual(self.find(BASE, 100, **rough), ("Harbour Aquarium", "aquarium",
                                                         False))
        # (The theatre reaches 27 m: an estimate of 100 m is not at it.)
        self.assertNotIn("Grand Theatre", self.names(THEATRE, 100, **rough))

    def test_settings(self):
        self.assertEqual(named_places.settings({}), named_places.DEFAULTS)
        self.assertEqual(named_places.settings({"named_places": {
            "enabled": False, "max_estimate_km": 1}}), {"enabled": False,
                                                        "max_estimate_km": 1.0})
        self.assertEqual(named_places.settings({"named_places": {
            "enabled": "no", "max_estimate_km": 99}}), named_places.DEFAULTS)


class FakeIndex:
    """places.PlaceIndex's nearest() for one made-up town."""

    def __init__(self, town):
        self.town = town

    def nearest(self, lat, lon, max_km=250):
        km = street_maps._km((lat, lon), self.town)
        return (0, km) if km <= max_km else None


class UpdateTest(Fixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.aquarium = self.picture("fish.jpg", *BASE, info=INDOOR)
        self.zoo = self.picture("zoo.jpg", *offset(*ZOO, east_m=10), info=OUTDOOR)
        self.home = self.picture("home.jpg", *HOMES, info=INDOOR)
        self.bakery = self.picture("street.jpg", *offset(*BAKERY, north_m=5), info=OUTDOOR)
        self.lunch = self.picture("lunch.jpg", *offset(*BAKERY, north_m=5), info=OUTDOOR_MEAL)
        self.nowhere = self.picture("nowhere.jpg")

    def rows(self):
        return named_places.stored(self.conn)

    def updated(self, image_id):
        return self.conn.execute("SELECT updated FROM images WHERE id = ?",
                                 (image_id,)).fetchone()[0]

    def test_the_pictures_get_their_named_places_once(self):
        before = self.updated(self.aquarium)
        changed = set()
        summary = named_places.update(self.conn, self.config, now=before + 50, changed=changed)
        self.assertEqual(summary["pictures"], 5)
        self.assertEqual(summary["looked_up"], 5)
        self.assertEqual(summary["named"], 3)
        rows = self.rows()
        self.assertEqual(rows[self.aquarium]["name"], "Harbour Aquarium")
        self.assertEqual(rows[self.aquarium]["source"], "gps")
        self.assertEqual(rows[self.zoo]["name"], "City Zoo")
        self.assertEqual(rows[self.zoo]["inside"], 1)
        self.assertEqual(rows[self.home]["name"], "")
        self.assertEqual(rows[self.bakery]["name"], "")     # (outdoors, no food)
        self.assertEqual(rows[self.lunch]["name"], "Thimble Bakery")
        self.assertNotIn(self.nowhere, rows)
        # The pictures whose named place changed are touched (the search index notices).
        self.assertEqual(changed, {self.aquarium, self.zoo, self.lunch})
        self.assertEqual(self.updated(self.aquarium), before + 50)
        self.assertEqual(self.updated(self.home), before)
        # Nothing changed: nothing is looked up again.
        again = named_places.update(self.conn, self.config)
        self.assertEqual((again["looked_up"], again["changed"], again["named"]), (0, 0, 3))

    def test_a_moved_position_and_new_street_maps_are_looked_up_again(self):
        named_places.update(self.conn, self.config)
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?",
                          offset(*ZOO, north_m=20) + (self.aquarium,))
        self.conn.commit()
        summary = named_places.update(self.conn, self.config)
        self.assertEqual(summary["looked_up"], 1)
        self.assertEqual(self.rows()[self.aquarium]["name"], "City Zoo")
        # A picture without a position any more loses its row.
        self.conn.execute("UPDATE images SET lat = NULL, lon = NULL WHERE id = ?",
                          (self.zoo,))
        self.conn.commit()
        summary = named_places.update(self.conn, self.config)
        self.assertEqual(summary["removed"], 1)
        self.assertNotIn(self.zoo, self.rows())
        # New street maps (another file): every picture again.
        time.sleep(1.1)
        write_streets(FEATURES[:4])
        summary = named_places.update(self.conn, self.config)
        self.assertEqual(summary["looked_up"], 4)
        self.assertEqual(self.rows()[self.aquarium]["name"], "")
        self.assertEqual(named_places.update(self.conn, self.config, redo=True)["looked_up"], 4)

    def test_without_street_maps_or_turned_off_there_are_none(self):
        named_places.update(self.conn, self.config)
        street_maps.remove()
        changed = set()
        summary = named_places.update(self.conn, self.config, changed=changed)
        self.assertFalse(summary["maps"])
        self.assertEqual(summary["removed"], 3)
        self.assertEqual(self.rows(), {})
        self.assertEqual(changed, set())
        write_streets(FEATURES)
        named_places.update(self.conn, self.config)
        self.base.write_config([support.library("Family", self.family, id="lib_family1")],
                               named_places={"enabled": False})
        config = common.load_config()
        self.assertEqual(named_places.update(self.conn, config)["removed"], 3)
        self.assertEqual(named_places.status(self.conn, config)["named"], 0)

    def test_estimated_places_of_a_small_radius_and_the_owners_positions(self):
        place_estimates.ensure_tables(self.conn)
        close = self.picture("close.jpg", info=INDOOR)
        rough = self.picture("rough.jpg", info=INDOOR)
        owner = self.picture("owner.jpg", info=INDOOR)
        town = self.picture("town.jpg", info=INDOOR)
        rows = [(close, "time", BASE, 0.1), (rough, "time", BASE, 0.5),
                (owner, "owner", offset(*ZOO, east_m=10), None),
                (town, "owner", offset(*ZOO, north_m=-30), None)]
        for image_id, source, where, radius in rows:
            self.conn.execute("INSERT INTO place_estimates (image_id, source, lat, lon, "
                              "radius_km, place_country) VALUES (?, ?, ?, ?, ?, 'X')",
                              (image_id, source, where[0], where[1], radius))
        self.conn.commit()
        found = {position.image_id: position for position in named_places.positions(
            self.conn, self.config, index=FakeIndex(offset(*ZOO, north_m=-30)))}
        self.assertEqual(found[close].source, "estimate")
        self.assertAlmostEqual(found[close].accuracy_m, 100.0)
        # (An approximate position: no place to eat, shop or stay.)
        self.assertTrue(found[close].facts.approximate)
        self.assertFalse(found[close].facts.eat or found[close].facts.shops)
        self.assertNotIn(rough, found)          # (more than max_estimate_km)
        self.assertEqual(found[owner].source, "owner")
        self.assertNotIn(town, found)           # (a town the owner chose: its middle)
        with mock.patch("places.shared", lambda: FakeIndex(offset(*ZOO, north_m=-30))):
            named_places.update(self.conn, self.config)
        rows = self.rows()
        self.assertEqual(rows[close]["name"], "Harbour Aquarium")
        self.assertEqual(rows[owner]["name"], "City Zoo")
        self.assertNotIn(town, rows)

    def test_a_file_that_names_another_country_gets_no_named_place(self):
        # (Its names are the file's: a named place where the position is might be
        # across the border. Of the file's own country: as any other.)
        index = FakeIndex(offset(*ZOO, north_m=-300))
        index.countries = {1: ("XA", "Examplia"), 2: ("XB", "Testland")}
        same = self.picture("same.jpg", *offset(*ZOO, east_m=10), info=OUTDOOR,
                            place_cc="XA", exif=json.dumps(
                                {"XMPPlace": {"city": "Zooville", "cc": "XA"}}))
        across = self.picture("across.jpg", *offset(*ZOO, east_m=12), info=OUTDOOR,
                              place_cc="XA", exif=json.dumps(
                                  {"XMPPlace": {"city": "Borderdorf", "cc": "XB"}}))
        unknown = self.picture("unknown.jpg", *offset(*ZOO, east_m=14), info=OUTDOOR,
                               exif=json.dumps({"XMPPlace": {"city": "Borderdorf", "cc": "XB"}}))
        with mock.patch("places.shared", lambda: index):
            named_places.update(self.conn, self.config)
        rows = self.rows()
        self.assertEqual(rows[same]["name"], "City Zoo")
        self.assertNotIn(across, rows)
        # (No place names of the position: nothing to tell.)
        self.assertEqual(rows[unknown]["name"], "City Zoo")

    def test_a_tracks_position_gets_areas_only(self):
        tracks.ensure_table(self.conn)
        clip = insert_image(self.conn, "lib_family1", "drive.mp4", media="video",
                            content_hash="clip", lat=BASE[0], lon=BASE[1])
        far = offset(*BASE, north_m=40)
        fixes = [tracks.Fix(0.0, 1.6e9, BASE[0], BASE[1], 30.0, None),
                 tracks.Fix(1.0, 1.6e9 + 1, far[0], far[1], 30.0, None)]
        tracks.store(self.conn, "clip", tracks.Track(fixes, "text"))
        self.conn.commit()
        found = {position.image_id: position
                 for position in named_places.positions(self.conn, self.config)}
        self.assertEqual(found[clip].source, "track")
        self.assertFalse(found[clip].facts.points)
        named_places.update(self.conn, self.config)
        self.assertEqual(self.rows()[clip]["name"], "")

    def test_a_picture_from_a_vehicle_gets_areas_only(self):
        car = self.picture("car.jpg", *BASE, info=VEHICLE)
        parked = self.picture("parked.jpg", *offset(*ZOO, east_m=10), info=VEHICLE)
        named_places.update(self.conn, self.config)
        self.assertEqual(self.rows()[car]["name"], "")
        self.assertEqual(self.rows()[parked]["name"], "City Zoo")

    def test_from_a_plane_none_from_a_car_areas_only_and_indoors_further_off(self):
        sky = {"title": "Clouds", "setting": "outdoor"}
        high = self.picture("high.jpg", *BASE, info=sky, gps={"GPSAltitude": 10000.0})
        fast = self.picture("fast.jpg", *BASE, info=sky,
                            gps={"GPSSpeed": 400.0, "GPSSpeedRef": "K"})
        car = self.picture("car-gps.jpg", *BASE, info=sky,
                           gps={"GPSSpeed": 40.0, "GPSSpeedRef": "K"})
        clouds = self.picture("clouds.jpg", *BASE,
                              info={"title": "A peak above the clouds", "setting": "outdoor"})
        aerial = self.picture("aerial.jpg", *BASE,
                              info={"title": "Aerial view of a bay", "setting": "outdoor",
                                    "tags": ["airplane", "bay"]})
        tower = self.picture("tower-view.jpg", *BASE,
                             info={"title": "Aerial view from the tower",
                                   "setting": "outdoor"})
        told = self.picture("told.jpg", *BASE, info=INDOOR, gps={"GPSHPositioningError": 12.0})
        found = {position.image_id: position
                 for position in named_places.positions(self.conn, self.config)}
        for gone in (high, fast, clouds, aerial):
            self.assertNotIn(gone, found)
        self.assertFalse(found[car].facts.points)
        self.assertTrue(found[tower].facts.points)
        self.assertEqual(found[self.aquarium].accuracy_m, named_places.INDOOR_ACCURACY_M)
        self.assertEqual(found[told].accuracy_m, 12.0)
        self.assertEqual(found[self.zoo].accuracy_m, named_places.DEFAULT_ACCURACY_M)
        self.assertEqual(named_places.motion({"GPSSpeed": 10.0}), "")

    def test_a_new_description_is_looked_up_again(self):
        named_places.update(self.conn, self.config)
        self.assertEqual(named_places.stored(self.conn)[self.bakery]["name"], "")
        # (Described again: now a picture of the bakery's window, by name.)
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (json.dumps({"title": "Thimble Bakery window", "setting": "outdoor",
                                       "food": []}), self.bakery))
        self.conn.commit()
        summary = named_places.update(self.conn, self.config)
        self.assertEqual(summary["looked_up"], 1)
        self.assertEqual(named_places.stored(self.conn)[self.bakery]["name"], "Thimble Bakery")

    def test_the_owners_choices_look_up_a_few_at_once(self):
        named_places.update(self.conn, self.config)
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?",
                          offset(*ZOO, east_m=10) + (self.aquarium,))
        self.conn.commit()
        with mock.patch.object(named_places, "QUICK_LIMIT", 0):
            self.assertEqual(named_places.after_check(self.conn, self.config), set())
        changed = set()
        self.assertEqual(named_places.after_check(self.conn, self.config, changed),
                         {self.aquarium})
        self.assertEqual(changed, {self.aquarium})
        # Before a first look-up (no table yet), nothing is done at once.
        self.conn.execute("DROP TABLE named_places")
        self.conn.commit()
        self.assertEqual(named_places.after_check(self.conn, self.config), set())
        self.assertFalse(named_places.have_table(self.conn))

    def test_a_stopped_update_keeps_what_it_did(self):
        ctx = jobs.JobContext("named_places")
        steps = []
        ctx.should_stop = lambda: len(steps) >= 2
        original = ctx.step

        def step(n=1, item=None, quick=False):
            steps.append(n)
            original(n, item)
        ctx.step = step
        summary = named_places.update(self.conn, self.config, ctx)
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["looked_up"], 2)
        self.assertEqual(named_places.update(self.conn, self.config)["looked_up"], 3)

    def test_other_writers_get_in_while_places_are_looked_up(self):
        # (The server saving a favorite while a scan looks up named places: the
        # places are looked up with no write lock held, and written in short
        # transactions, every COMMIT_ROWS or COMMIT_SECONDS.)
        other = db.connect()
        self.addCleanup(other.close)
        other.execute("PRAGMA busy_timeout=200")
        wrote = []
        real = named_places.find

        def find(*args, **kwargs):
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("UPDATE images SET hidden = 0 WHERE id = ?", (self.zoo,))
                other.commit()
                wrote.append(True)
            except sqlite3.OperationalError:
                wrote.append(False)
            return real(*args, **kwargs)

        with mock.patch.object(named_places, "find", find):
            summary = named_places.update(self.conn, self.config)
        self.assertEqual(wrote, [True] * 5)
        self.assertEqual((summary["looked_up"], summary["named"]), (5, 3))
        # What was looked up in COMMIT_SECONDS is written then (here: each one,
        # seen by the other connection before the next is looked up).
        seen = []

        def counting(*args, **kwargs):
            seen.append(other.execute("SELECT COUNT(*) FROM named_places WHERE updated = ?",
                                      (12345.0,)).fetchone()[0])
            return real(*args, **kwargs)

        with mock.patch.object(named_places, "find", counting),                 mock.patch.object(named_places, "COMMIT_SECONDS", 0.0):
            summary = named_places.update(self.conn, self.config, redo=True, now=12345.0)
        self.assertEqual(summary["looked_up"], 5)
        self.assertEqual(seen, [0, 1, 2, 3, 4])


class ReachTest(unittest.TestCase):

    def test_the_map_page_knows_how_far_each_kind_reaches(self):
        # static/streets.js PLACE_REACH (the circle a picked place searches) is KINDS'.
        with open(os.path.join(support.REPO, "static", "streets.js"), encoding="utf-8") as handle:
            text = handle.read()
        table = text[text.index("var PLACE_REACH = {"):]
        table = table[table.index("{") + 1:table.index("}")]
        reach = {name.strip().strip('"'): int(value) for name, value in
                 (pair.split(":") for pair in table.split(",") if pair.strip())}
        self.assertEqual(reach, {kind: metres for kind, (_group, metres)
                                 in named_places.KINDS.items()})
        indoor = re.search(r"var PLACE_INDOOR_M = (\d+);", text).group(1)
        self.assertEqual(float(indoor), named_places.INDOOR_ACCURACY_M)


class RecordTest(Fixture, unittest.TestCase):
    """np, nk, nd, ni and na in the search records, the server's and an export's."""

    def setUp(self):
        super().setUp()
        self.aquarium = self.picture("fish.jpg", *BASE, info=INDOOR)
        self.zoo = self.picture("zoo.jpg", *offset(*ZOO, east_m=10), info=OUTDOOR)
        # (Its file says it is at most 5 m off: 57 m from the aquarium is a distance.)
        self.precise = self.picture("tank.jpg", *offset(*BASE, east_m=3), info=INDOOR,
                                    gps={"GPSHPositioningError": 5.0})
        place_estimates.ensure_tables(self.conn)
        self.estimated = self.picture("guess.jpg", info=INDOOR)
        self.conn.execute("INSERT INTO place_estimates (image_id, source, lat, lon, radius_km, "
                          "place_city, place_country, place_cc, detail) VALUES (?, 'time', ?, "
                          "?, 0.1, 'Seaport', 'Exampleland', 'XX', '{}')",
                          (self.estimated, BASE[0], BASE[1]))
        self.conn.commit()
        named_places.update(self.conn, self.config)

    def records(self, **kwargs):
        return {record["i"]: record for record in search_index.build_records(
            self.conn, self.config, ["lib_family1"], **kwargs)}

    def test_the_servers_records(self):
        records = self.records()
        tank = records[self.precise]
        self.assertEqual((tank["np"], tank["nk"], tank["nd"]),
                         ("Harbour Aquarium", "aquarium", 57))
        self.assertNotIn("ni", tank)
        self.assertNotIn("na", tank)
        zoo = records[self.zoo]
        self.assertEqual((zoo["np"], zoo["ni"]), ("City Zoo", 1))
        self.assertNotIn("nd", zoo)
        self.assertNotIn("na", zoo)
        # Taken indoors, its file silent: it may be 65 m off, more than the 60 m the
        # aquarium is away -- "nearby", no distance.
        fish = records[self.aquarium]
        self.assertEqual((fish["np"], fish["na"]), ("Harbour Aquarium", 1))
        self.assertNotIn("nd", fish)
        # An estimated place is approximate too.
        guess = records[self.estimated]
        self.assertEqual((guess["pe"], guess["np"], guess["na"]), (1, "Harbour Aquarium", 1))
        self.assertNotIn("nd", guess)
        self.assertEqual(search_index.picture_record(self.conn, self.zoo)["np"], "City Zoo")

    def test_the_keys_of_a_named_place(self):
        def keys(for_export=False, **row):
            named = dict({"name": "Toy Museum", "kind": "museum", "metres": 57, "inside": 0,
                          "accuracy_m": 5.0, "source": "gps", "facts": "p"}, **row)
            return named_places.record_keys(named, for_export)
        self.assertEqual(keys(), {"np": "Toy Museum", "nk": "museum", "nd": 57})
        # An export's distance is rounded as its positions are (to 10 m, at least 10).
        self.assertEqual(keys(True)["nd"], 60)
        self.assertEqual(keys(True, metres=3, accuracy_m=2.0)["nd"], 10)
        # Nearer than the position may be off: nearby.
        self.assertEqual(keys(metres=20, accuracy_m=30.0),
                         {"np": "Toy Museum", "nk": "museum", "na": 1})
        # Inside an area; an estimate's probably.
        self.assertEqual(keys(inside=1, metres=0), {"np": "Toy Museum", "nk": "museum", "ni": 1})
        self.assertEqual(keys(inside=1, metres=0, source="estimate", accuracy_m=100.0),
                         {"np": "Toy Museum", "nk": "museum", "ni": 1, "na": 1})
        # The owner's place with a radius (an accepted estimate) is approximate too.
        self.assertEqual(keys(source="owner", facts="pa", metres=120, accuracy_m=100.0),
                         {"np": "Toy Museum", "nk": "museum", "na": 1})

    def test_turned_off_or_without_street_maps_none_show_at_once(self):
        libraries = {"lib_family1": "Family"}
        self.assertEqual(self.records()[self.zoo]["np"], "City Zoo")
        service = search_index.IndexService(watch=False)
        self.addCleanup(service.close)

        def served(config):
            _status, body, _headers = service.respond(self.conn, config, libraries, True)
            return {record["i"]: record for record in json.loads(body)["records"]}
        self.assertEqual(served(self.config)[self.zoo]["np"], "City Zoo")
        on = search_index.index_etag(self.conn, libraries, True, config=self.config)
        # Turned off: no job has run, the rows are still there -- and shown nowhere.
        self.base.write_config([support.library("Family", self.family, id="lib_family1")],
                               named_places={"enabled": False})
        off = common.load_config()
        self.assertIn(self.zoo, named_places.stored(self.conn))
        self.assertNotIn("np", search_index.build_records(
            self.conn, off, ["lib_family1"])[1])
        self.assertFalse(any("np" in record for record in served(off).values()))
        self.assertNotEqual(search_index.index_etag(self.conn, libraries, True, config=off), on)
        self.assertNotIn("np", search_index.picture_record(self.conn, self.zoo, config=off))
        # The street maps deleted by hand: likewise.
        os.remove(street_maps.archive_path())
        self.assertFalse(any("np" in record for record in self.records().values()))
        self.assertFalse(any("np" in record for record in served(self.config).values()))
        self.assertNotEqual(search_index.index_etag(self.conn, libraries, True,
                                                    config=self.config), on)

    def test_a_named_place_found_for_another_position_is_not_shown(self):
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?",
                          HOMES + (self.aquarium,))
        self.conn.commit()
        self.assertNotIn("np", self.records()[self.aquarium])

    def test_an_export_holds_them_only_with_its_location(self):
        off = self.records(for_export=True, export_options={"location": False})
        self.assertFalse(any("np" in record for record in off.values()))
        on = self.records(for_export=True, export_options={"location": True})
        self.assertEqual(on[self.aquarium]["np"], "Harbour Aquarium")
        # An estimated place's only with estimated_places.
        self.assertNotIn("np", on[self.estimated])
        both = self.records(for_export=True, export_options={"location": True,
                                                             "estimated_places": True})
        self.assertEqual(both[self.estimated]["np"], "Harbour Aquarium")

    def test_an_export_keeps_one_only_with_the_position_it_was_found_for(self):
        class Routes:
            def __init__(self, own, estimate):
                self.own, self.estimated = own, estimate

            def position(self, row):
                return self.own

            def estimate(self, row):
                return self.estimated

        rows = {row["id"]: row for row in self.conn.execute("SELECT * FROM images")}
        records = self.records(for_export=True, export_options={"location": True,
                                                                "estimated_places": True})
        estimate = place_estimates.for_picture(self.conn, self.estimated)

        def published(image_id, own, estimated, estimate_row=None):
            job = mock.Mock(track_routes=Routes(own, estimated))
            picture = mock.Mock(row=rows[image_id], record=records[image_id])
            return export._Export.named_published(job, picture, estimate_row)
        self.assertTrue(published(self.aquarium, True, False))
        self.assertFalse(published(self.aquarium, False, True))
        self.assertTrue(published(self.estimated, False, True, estimate))
        self.assertFalse(published(self.estimated, True, False, estimate))
        self.assertFalse(published(self.estimated, True, True, None))

    def test_the_index_etag_changes_with_them(self):
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?",
                          HOMES + (self.aquarium,))
        self.conn.commit()
        moved = search_index.data_state(self.conn, ["lib_family1"])
        named_places.update(self.conn, self.config, now=time.time() + 100)
        self.assertNotEqual(search_index.data_state(self.conn, ["lib_family1"]), moved)


class CommandLineTest(Fixture, unittest.TestCase):

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = named_places.main(list(argv))
        return code, out.getvalue()

    def test_status_update_redo_and_lookup(self):
        self.picture("fish.jpg", *BASE, info=INDOOR)
        code, printed = self.run_main("--status")
        self.assertEqual(code, 0)
        self.assertIn("0 of 0 picture(s)", printed)
        code, printed = self.run_main("--update")
        self.assertEqual(code, 0, printed)
        self.assertIn("Command line: python named_places.py --update --settings-base", printed)
        self.assertIn("Named places: 1 of 1 picture(s)", printed)
        code, printed = self.run_main()
        self.assertIn("1 of 1 picture(s) with a position have a named place (1 gps)", printed)
        code, printed = self.run_main("--redo")
        self.assertIn("1 looked up", printed)
        code, printed = self.run_main("--lookup", str(BASE[0]), str(BASE[1]))
        self.assertEqual(code, 0)
        lines = printed.splitlines()
        self.assertTrue(lines[0].startswith("Harbour Aquarium  (aquarium, "), printed)
        self.assertTrue(lines[0].endswith("<- chosen"), printed)
        code, printed = self.run_main("--lookup", str(TWINS[0]), str(TWINS[1]),
                                      "--accuracy", "5")
        self.assertIn("None is chosen", printed)
        self.assertTrue(any(line.startswith("Seaside Cafe") for line in lines))
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_main("--update", "--redo")

    def test_without_street_maps(self):
        street_maps.remove()
        code, printed = self.run_main()
        self.assertIn("no street maps", printed)
        code, printed = self.run_main("--update")
        self.assertEqual(code, 1)
        with self.assertRaises(jobs.JobError):
            jobs.RUNNERS["named_places"](jobs.JobContext("named_places"), self.conn,
                                         self.config, {})

    def test_the_job_and_its_command_line(self):
        self.picture("fish.jpg", *BASE, info=INDOOR)
        ctx = jobs.JobContext("named_places")
        summary = jobs.RUNNERS["named_places"](ctx, self.conn, self.config, {"redo": True})
        self.assertEqual(summary["named"], 1)
        self.assertEqual(jobs.job_command("named_places", {"redo": True}, self.config),
                         ("named_places.py", ["--redo", "--settings-base", self.base.path]))
        self.assertEqual(named_places.status(self.conn, self.config)["command"],
                         jobs.command_text("named_places.py",
                                           ["--update", "--settings-base", self.base.path]))

    def test_the_jobs_look_them_up_after_their_work(self):
        image_id = self.picture("fish.jpg", *BASE, info=INDOOR)
        summary = {}
        with mock.patch.object(named_places, "run_safely",
                               wraps=named_places.run_safely) as wrapped:
            jobs._named_places(summary, self.conn, self.config, jobs.JobContext("scan"))
        wrapped.assert_called_once()
        self.assertEqual(summary["named_places"]["named"], 1)
        self.assertEqual(named_places.stored(self.conn)[image_id]["name"], "Harbour Aquarium")

    def test_removing_the_street_maps_forgets_them(self):
        image_id = self.picture("fish.jpg", *BASE, info=INDOOR)
        named_places.update(self.conn, self.config)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = street_maps.main(["--remove"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn(image_id, named_places.stored(self.conn))
        self.assertIn("Forgot the named places of 1 picture(s).", out.getvalue())


class ExportTest(Fixture, unittest.TestCase):
    """A library's export: the named places go with the location option only."""

    def setUp(self):
        super().setUp()
        import imagegen
        import scan
        for name, where in (("fish.jpg", BASE), ("zoo.jpg", offset(*ZOO, east_m=10))):
            exif = imagegen.make_exif(gps=where)
            with open(os.path.join(self.family, name), "wb") as handle:
                handle.write(imagegen.jpeg_bytes(imagegen.textured_image((64, 48)), exif))
        scan.run(self.config, jobs.JobContext("scan"), describe_images=False)
        self.conn.execute("UPDATE images SET described = 1, prompt_ver = 6, info = ?",
                          (json.dumps(INDOOR),))
        self.conn.commit()
        named_places.update(self.conn, self.config)

    def records(self, **options):
        from test_export import read_records
        options = dict({"images": "none", "similar": False, "meaning": False,
                        "looks": False}, **options)
        out = self.base.join("site-%d" % len(options))
        export.export_library(export.LogContext(support.quiet), self.conn, self.config,
                              "lib_family1", out, options)
        return sorted(read_records(out), key=lambda record: record.get("t") or "")

    def test_only_with_the_location(self):
        self.assertFalse(any("np" in record for record in self.records(location=False)))
        names = sorted(record.get("np") for record in self.records(location=True))
        self.assertEqual(names, ["City Zoo", "Harbour Aquarium"])

    def site(self, **options):
        options = dict({"images": "none", "similar": False, "meaning": False,
                        "looks": False}, **options)
        self.sites = getattr(self, "sites", 0) + 1
        out = self.base.join("credit-%d" % self.sites)
        export.export_library(export.LogContext(support.quiet), self.conn, self.config,
                              "lib_family1", out, options)
        return out

    def read(self, out, name):
        with open(os.path.join(out, name), encoding="utf-8") as handle:
            return handle.read()

    def test_openstreetmap_is_credited_with_them_without_the_street_maps(self):
        out = self.site(location=True)
        credit = "Named places: &copy; OpenStreetMap contributors"
        self.assertIn(credit, self.read(out, "index.html"))
        self.assertIn(credit, self.read(out, "map.html"))
        self.assertIn("OpenStreetMap", self.read(out, os.path.join("maps", "LICENSE.txt")))
        self.assertFalse(os.path.exists(os.path.join(out, "maps", "index.js")))
        out = self.site(location=False)
        self.assertNotIn("OpenStreetMap", self.read(out, "index.html"))
        self.assertFalse(os.path.exists(os.path.join(out, "maps", "LICENSE.txt")))

    def test_an_export_looks_up_the_positions_that_changed_first(self):
        fish = self.conn.execute("SELECT id FROM images WHERE name = 'fish.jpg'").fetchone()[0]
        self.conn.execute("UPDATE images SET lat = ?, lon = ? WHERE id = ?",
                          offset(*ZOO, east_m=20) + (fish,))
        self.conn.commit()
        names = sorted(record.get("np") for record in self.records(location=True))
        self.assertEqual(names, ["City Zoo", "City Zoo"])

    def test_turned_off_an_export_has_none(self):
        self.base.write_config([support.library("Family", self.family, id="lib_family1")],
                               named_places={"enabled": False})
        self.config = common.load_config()
        self.assertFalse(any("np" in record for record in self.records(location=True)))


@unittest.skipUnless(support.pillow_available(), "Pillow is not installed")
class TripTest(test_trip_export.TripExportTest):
    """A trip's export (tests/test_trip_export.py's made-up trip, with made-up street maps):
    no named place in the home area or in home's town, none in a smart album's search,
    and a dashcam clip's only with its route."""

    def setUp(self):
        super().setUp()
        home, lake = test_trip_export.NEAR_HOME, test_trip_export.LAKE
        # (Out of the home area, 34 km from home, but in home's town.)
        self.edge = offset(*test_trip_export.HOME, north_m=34000)
        # The lake's dashcam clip drives from the lake 5 km east: inside Lake Park.
        park = [(lake[0] - 0.015, lake[1] - 0.02), (lake[0] - 0.015, lake[1] + 0.08),
                (lake[0] + 0.015, lake[1] + 0.08), (lake[0] + 0.015, lake[1] - 0.02)]
        write_streets([area(9001, "park", square(*home, 400)),
                       poi(9001, "Homeside Park", "park", *home, min_zoom=14),
                       poi(9002, "Lake Museum", "museum", *offset(*lake, east_m=20),
                           min_zoom=13),
                       area(9003, "park", park),
                       poi(9003, "Lake Park", "park", *offset(*lake, north_m=600), min_zoom=12),
                       poi(9004, "Hometown Museum", "museum", *offset(*self.edge, east_m=20),
                           min_zoom=13)], zooms=range(10, 16))
        self.addCleanup(street_maps.shared.forget)
        self.edge_id = self.add_edge_picture()
        config = common.load_config()
        named_places.update(self.conn, config)

    def add_edge_picture(self):
        """A picture of the trip out of the home area but in home's town."""
        import cache
        import imagegen
        thumb = imagegen.jpeg_bytes(imagegen.textured_image((160, 120)))
        place = test_trip_export.HOME_PLACE
        cursor = self.conn.execute(
            "INSERT INTO images (library_id, path, name, size, mtime, ctime, width, height, "
            "added, updated, taken_at, taken_source, content_hash, lat, lon, has_preview, "
            "info, described, prompt_ver, place_city, place_region, place_country, place_cc, "
            "place_km) VALUES (?, 'edge.jpg', 'edge.jpg', 1000, 1, 1, 160, 120, 1, 1, "
            "'2024-05-11T09:00:00', 'exif', 'edge', ?, ?, 1, ?, 1, 5, ?, ?, ?, ?, 1.0)",
            (test_trip_export.LAKES, self.edge[0], self.edge[1],
             json.dumps(test_trip_export.info("Picture edge", ["view"]))) + place)
        image_id = cursor.lastrowid
        with cache.ThumbStore() as thumbs:
            thumbs.put(image_id, thumb)
        cache.PreviewStore().write(test_trip_export.LAKES, image_id, thumb)
        self.conn.commit()
        return image_id

    def records(self, out):
        from test_export import read_records
        return {record.get("t"): record for record in read_records(out)}

    def test_no_named_place_near_home_and_a_clips_only_with_its_route(self):
        stored = named_places.stored(self.conn)
        leaving = self.ids["leaving-1.jpg"]
        self.assertEqual(stored[leaving]["name"], "Homeside Park")
        self.assertEqual(stored[self.edge_id]["name"], "Hometown Museum")
        clip = self.ids["dash/20240511_120000.MP4"]
        self.assertEqual((stored[clip]["name"], stored[clip]["source"]), ("Lake Park", "track"))
        out, _summary = self.export("named", {"location": True, "routes": True,
                                              "routes_trim": 0})
        records = self.records(out)
        self.assertEqual(records["Picture 10-0"]["np"], "Lake Museum")
        for title in ("Picture leaving-1", "Picture leaving-2", "Picture edge"):
            self.assertTrue(title in records, sorted(records))
            for key in named_places.RECORD_KEYS:
                self.assertNotIn(key, records[title], title)
        self.assertEqual(records["Drive 1"]["np"], "Lake Park")
        text = "\n".join(self.texts(out).values())
        for secret in ("Homeside", "Hometown Museum"):
            self.assertNotIn(secret, text)
        # Without the routes, the clip's position from its track does not go: nor its
        # named place.
        out, _summary = self.export("named-no-routes", {"location": True})
        records = self.records(out)
        self.assertNotIn("np", records["Drive 1"])
        self.assertEqual(records["Picture 10-0"]["np"], "Lake Museum")

    def test_a_smart_album_that_names_a_place_at_home_is_left_out(self):
        import albums
        for name, query in (("Park at home", 'place:"homeside park"'),
                            ("Home museum", 'place:"hometown museum" OR hometown museum'),
                            ("Lake museum", 'place:"lake museum"'),
                            ("Parks", "place:park")):
            albums.create_album(self.conn, name, kind="smart", query=query)
        self.conn.commit()
        out, _summary = self.export("named-albums", {"location": True, "marks": True,
                                                     "routes": True, "routes_trim": 0})
        from test_export import read_manifest
        published = sorted(album["name"] for album in read_manifest(out)["albums"])
        # ("park" is in a published place's name too: Lake Park.)
        self.assertEqual(published, ["Lake museum", "Parks"])
        text = "\n".join(self.texts(out).values())
        self.assertNotIn("Homeside", text)
        self.assertNotIn("hometown museum", text.lower())


# (TripTest runs its own tests only, not again those of the trip export's.)
for _name in dir(test_trip_export.TripExportTest):
    if _name.startswith("test") and _name not in TripTest.__dict__:
        setattr(TripTest, _name, None)


@unittest.skipIf(flask is None, "Flask is not installed")
class AdminTest(Fixture, unittest.TestCase):

    HEADERS = {"X-Search-Images": "1", "Sec-Fetch-Site": "same-origin"}

    def setUp(self):
        super().setUp()
        self.manager = jobs.JobManager()
        self.app = serve.build_app(init_db=False, job_manager=self.manager)
        self.app.testing = True
        self.addCleanup(self.app.extensions["search_images"].close)
        self.client = self.app.test_client()

    def post(self, path, body=None):
        return self.client.post("/api/admin" + path, json=body or {}, headers=self.HEADERS)

    def test_the_manage_page_counts_them_and_starts_the_job(self):
        self.picture("fish.jpg", *BASE, info=INDOOR)
        named_places.update(self.conn, self.config)
        state = self.client.get("/api/admin/maps", headers=self.HEADERS).get_json()
        self.assertEqual((state["named"]["pictures"], state["named"]["named"]), (1, 1))
        self.assertIsNone(state["named"]["job"])
        reply = self.post("/maps/named", {"redo": True})
        self.assertEqual(reply.status_code, 202)
        self.assertEqual(reply.get_json()["job"]["kind"], "named_places")
        self.assertEqual(reply.get_json()["job"]["params"], {"redo": True})
        self.assertEqual(reply.get_json()["maps"]["named"]["job"]["state"], "queued")
        self.assertEqual(self.post("/maps/named", {"redo": "yes"}).status_code, 400)
        # Deleting the street maps forgets them.
        self.manager.cancel(reply.get_json()["job"]["id"])
        support.wait_for(lambda: self.manager.snapshot(0)["current"] is None)
        reply = self.post("/maps/remove")
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        self.assertEqual(named_places.stored(self.conn), {})
        self.assertEqual(self.post("/maps/named").status_code, 409)

    def test_choosing_a_place_answers_the_named_place_too(self):
        place_estimates.ensure_tables(self.conn)
        image_id = self.picture("guess.jpg", info=INDOOR)
        named_places.update(self.conn, self.config)
        where = offset(*ZOO, east_m=10)
        with mock.patch("places.shared", lambda: None):
            reply = self.post("/images/%d/place" % image_id,
                              {"choice": "set", "lat": where[0], "lon": where[1]})
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        # (Looked up at once; the answer's record has it when the place has names.)
        row = named_places.stored(self.conn)[image_id]
        self.assertEqual((row["name"], row["source"]), ("City Zoo", "owner"))
        self.assertEqual(named_places.RECORD_KEYS, ("np", "nk", "nd", "ni", "na"))


@unittest.skipIf(NODE is None, "Node.js is not installed")
class PagesTest(unittest.TestCase):
    """The search finds named places; the viewer and the map show them (Node)."""

    def test_node(self):
        result = subprocess.run([NODE, os.path.join(support.REPO, "tests", "js",
                                                     "named_places_test.js")],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
