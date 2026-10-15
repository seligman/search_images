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
import math
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
from namedgen import address, area, building, offset, poi, square, write_streets
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

    def test_a_place_with_several_names_is_shown_by_the_first(self):
        # OpenStreetMap keeps the several names of one place in one ("A;B"): the
        # first, trimmed, is the name shown, searched and published.
        first = named_places.first_name
        self.assertEqual(first(" Southern Hall ; Hall Two "), "Southern Hall")
        self.assertEqual(first(";;Hall Two;"), "Hall Two")
        self.assertEqual([first(" ; "), first(""), first(None)], ["", "", ""])
        self.assertEqual(named_places.display_name({"name": "\u5357;\u5317",
                                                    "name:en": "South Hall;North Hall"}),
                         "South Hall")
        self.assertEqual(named_places.all_names({"name": "Southern Hall; Hall Two",
                                                 "name:en": "Hall Two;Hall 2"}),
                         ["Southern Hall", "Hall Two", "Hall 2"])
        write_streets([poi(8701, "Southern Hall;Hall Two", "attraction", *BASE, min_zoom=13),
                       poi(8702, "Gull Gifts", "gift", *offset(*BASE, north_m=300),
                           min_zoom=16)])
        self.assertEqual(self.find(offset(*BASE, north_m=20)),
                         ("Southern Hall", "attraction", False))
        # A description that says its other name names it too.
        said = named_places.plain_words("A visit to Hall Two")
        found = named_places.candidates(BASE[0], BASE[1], 10.0, self.reader(),
                                        facts(words=said))
        self.assertEqual((found[0].name, found[0].named), ("Southern Hall", True))

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
        # By an aquarium, the place to eat the picture was taken in, the aquarium's own
        # (named_places 4: what the picture shows, see IndoorTest); a picture that shows
        # nothing to eat, the aquarium.
        self.assertEqual(self.find(BASE, 65, **meal), ("Seaside Cafe", "cafe", False))
        self.assertEqual(self.find(BASE, 65, **plain), ("Harbour Aquarium", "aquarium", False))

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


# Indoors: a made-up quay (far from the rest) with building outlines.
QUAY = offset(*BASE, east_m=6000)
AQUARIUM_HALL = offset(*QUAY, east_m=80)            # (the aquarium's own building)
HOTEL_HALL = offset(*QUAY, north_m=300)             # (a hotel's own large building)
PIER_HALL = offset(*QUAY, north_m=-300)             # (one building, two restaurants)
GALLERIES = offset(*QUAY, east_m=600)
THAI = offset(*QUAY, east_m=600, north_m=300)
OUTFITTERS = offset(*QUAY, east_m=600, north_m=-300)
FIELD = offset(*QUAY, east_m=-600)                  # (an airport's outline, east of it)
ARTS_HALL = offset(*QUAY, east_m=-600, north_m=300)
MARKET = offset(*QUAY, east_m=-600, north_m=-300)
STADIUM = offset(*QUAY, east_m=1200)
INDOOR_FEATURES = [
    building(9001, square(*AQUARIUM_HALL, 25)),
    poi(9001, "Reef Aquarium", "aquarium", *AQUARIUM_HALL, min_zoom=15),
    # The aquarium's restaurant, in a building of its own next door (the position).
    building(9100, square(*QUAY, 15)),
    poi(9101, "Reef Grill", "restaurant", *offset(*QUAY, north_m=5), min_zoom=17),
    address(9150, *offset(*QUAY, north_m=-5)),
    # A hotel's building (its own id), its label 100 m from its lobby; a cafe in it.
    building(9201, square(*HOTEL_HALL, 70)),
    # (Its lobby a building part inside it, as buildings mapped in 3D have them.)
    building(9203, square(*offset(*HOTEL_HALL, east_m=-50), 15), kind="building_part"),
    poi(9201, "Harbour View Hotel", "hotel", *offset(*HOTEL_HALL, east_m=50), min_zoom=16),
    poi(9202, "Lobby Coffee", "cafe", *offset(*HOTEL_HALL, east_m=-45), min_zoom=17),
    # A pier building with a chip shop counter and a sushi bar.
    building(9300, square(*PIER_HALL, 30)),
    poi(9301, "Pier Chips", "restaurant", *offset(*PIER_HALL, north_m=8), min_zoom=17),
    poi(9302, "Pier Sushi", "restaurant", *offset(*PIER_HALL, north_m=-25, east_m=-25),
        min_zoom=17, kind_detail="sushi"),
    # Two galleries side by side, outside any building outline.
    poi(9401, "North Gallery", "gallery", *offset(*GALLERIES, north_m=50), min_zoom=16),
    poi(9402, "South Gallery", "gallery", *offset(*GALLERIES, north_m=-72), min_zoom=16),
    poi(9501, "Lotus Thai", "restaurant", *offset(*THAI, north_m=10), min_zoom=17,
        kind_detail="thai"),
    poi(9502, "Example Outfitters", "clothes", *offset(*OUTFITTERS, north_m=8), min_zoom=17),
    area(9601, "aerodrome", square(*offset(*FIELD, east_m=230), 200)),
    poi(9601, "Quay Field Airport", "aerodrome", *offset(*FIELD, east_m=230), min_zoom=12),
    # A ferry terminal by the airport's outline (30 m west of it).
    poi(9602, "Example Ferry Terminal", "ferry_terminal", *offset(*FIELD, north_m=150),
        min_zoom=16),
    building(9701, square(*ARTS_HALL, 40)),
    poi(9701, "Example Arts Hall", "arts_centre", *offset(*ARTS_HALL, east_m=10), min_zoom=16),
    poi(9801, "Quay Market Mall", "mall", *offset(*MARKET, east_m=80), min_zoom=15),
    # A stadium's stands around its field (a hole in its outline), its point mid-field.
    building(9901, square(*STADIUM, 120), holes=[square(*STADIUM, 70)]),
    poi(9901, "Example Park Stadium", "stadium", *STADIUM, min_zoom=14),
    building(9902, square(*offset(*STADIUM, east_m=150), 20), kind="building_part"),
]
TANK = {"title": "Fish in a tank", "setting": "indoor", "scene": "aquarium",
        "subjects": ["fish tank", "fish"], "food": []}
LUNCH = {"title": "Lunch", "setting": "indoor", "scene": "dining table",
         "food": ["salad"], "event": "meal"}
ROOM = {"title": "A bed", "setting": "indoor", "scene": "hotel room", "food": []}


class IndoorTest(Fixture, unittest.TestCase):
    """Indoors: the building outlines and what the picture shows (refine), against the
    nearest place alone (choose)."""

    def setUp(self):
        super().setUp()
        write_streets(FEATURES + INDOOR_FEATURES)

    def find(self, where, info, accuracy=20.0):
        facts = named_places.picture_facts(json.dumps(info))
        found = named_places.find(where[0], where[1], accuracy, self.reader(), facts)
        return (found.name, found.kind, found.inside) if found else None

    def nearest(self, where, info, accuracy=20.0):
        """What the place was before the building outlines and what the picture shows
        counted: choose()'s, by the position and what the description says in words."""
        facts = named_places.picture_facts(json.dumps(info))._replace(
            shows=frozenset(), cuisines=frozenset(), food=False, nowhere=False)
        return named_places.choose(named_places.candidates(where[0], where[1], accuracy,
                                                           self.reader(), facts), accuracy)

    def test_the_buildings_layer_is_read_with_holes_filled(self):
        reader = self.reader()
        self.assertEqual(reader.zoom, 15)
        self.assertEqual(named_places.buildings_at(reader, *QUAY), {9100})
        self.assertEqual(named_places.buildings_at(reader, *offset(*QUAY, east_m=40)), set())
        # (A stadium's point mid-field: in the stands' outline, its hole filled; a
        # building part counts as a building, and so does the building around it.)
        self.assertEqual(named_places.buildings_at(reader, *STADIUM), {9901})
        self.assertEqual(named_places.buildings_at(reader, *offset(*STADIUM, north_m=100)),
                         {9901})
        self.assertEqual(named_places.buildings_at(reader, *offset(*STADIUM, east_m=150)),
                         {9902})
        self.assertEqual(named_places.buildings_at(reader, *offset(*HOTEL_HALL, east_m=-50)),
                         {9201, 9203})
        self.assertEqual(named_places.building_of(reader, 9001, *AQUARIUM_HALL), ({9001}, True))
        self.assertEqual(named_places.building_of(reader, 9101, *offset(*QUAY, north_m=5)),
                         ({9100}, False))
        self.assertEqual(named_places.building_of(reader, 9202, *offset(*HOTEL_HALL,
                                                                        east_m=-45)),
                         ({9201, 9203}, False))
        metres = named_places.building_metres(reader, {9001}, *AQUARIUM_HALL, *QUAY)
        self.assertTrue(53 <= metres <= 57, metres)
        # (Only buildings and building parts: no address point.)
        outlines = reader.outlines(*[int(value) for value in named_places._tile_xy(
            QUAY[0], QUAY[1], reader.zoom)])
        self.assertNotIn(9150, outlines.ids)

    def test_a_meal_in_the_restaurant_next_to_the_aquarium_names_the_restaurant(self):
        # (The fix is in the restaurant's building; the aquarium's point is 80 m off.)
        self.assertEqual(self.nearest(QUAY, LUNCH).name, "Reef Aquarium")
        self.assertEqual(self.find(QUAY, LUNCH), ("Reef Grill", "restaurant", False))
        # ... but the tanks the picture shows name the aquarium, whatever building the
        # fix drifted into.
        self.assertEqual(self.find(QUAY, TANK), ("Reef Aquarium", "aquarium", False))
        # Taken inside the aquarium's own building: "taken inside it".
        self.assertEqual(self.find(offset(*AQUARIUM_HALL, north_m=15), TANK),
                         ("Reef Aquarium", "aquarium", True))

    def test_a_venue_named_in_the_picture_is_taken_inside_its_own_building(self):
        # (A sign, or the title, naming the aquarium: no less "taken inside it".)
        inside = offset(*AQUARIUM_HALL, north_m=15)
        for info in (dict(TANK, text="REEF AQUARIUM - feeding at noon"),
                     dict(TANK, title="Fish in a tank at the Reef Aquarium")):
            facts = named_places.picture_facts(json.dumps(info))
            found = named_places.find(inside[0], inside[1], 20.0, self.reader(), facts)
            self.assertEqual((found.name, found.metres, found.inside, found.named, found.why),
                             ("Reef Aquarium", 0, True, True, "named"), info)
        # Outside its building (the restaurant's next door), in a parking garage in it, or
        # taken outdoors (a sign is most often seen from outside): named, not inside.
        signed = dict(TANK, text="REEF AQUARIUM")
        self.assertEqual(self.find(QUAY, signed), ("Reef Aquarium", "aquarium", False))
        for info in (dict(signed, scene="parking garage"), dict(signed, setting="outdoor")):
            self.assertEqual(self.find(inside, info), ("Reef Aquarium", "aquarium", False),
                             info)

    def test_a_venue_mapped_twice_is_taken_inside_its_own_building(self):
        # The aquarium also mapped as a point inside its own outline, 2 m from the position
        # (nearer than the outline's own place, which is its building's): still "taken
        # inside it", with or without a sign naming it.
        write_streets(FEATURES + INDOOR_FEATURES + [
            poi(9003, "Reef Aquarium", "aquarium", *offset(*AQUARIUM_HALL, north_m=13),
                min_zoom=16)])
        inside = offset(*AQUARIUM_HALL, north_m=15)
        for info in (TANK, dict(TANK, text="REEF AQUARIUM")):
            self.assertEqual(self.find(inside, info), ("Reef Aquarium", "aquarium", True), info)

    def test_the_buildings_own_place_before_what_is_mapped_inside_it(self):
        lobby = offset(*HOTEL_HALL, east_m=-50)
        # (The hotel's label is 100 m from the lobby: too far for its reach.)
        self.assertIsNone(self.nearest(lobby, ROOM, accuracy=65.0))
        self.assertEqual(self.find(lobby, ROOM, accuracy=65.0),
                         ("Harbour View Hotel", "hotel", True))
        # A coffee: the cafe in it.
        coffee = {"title": "A latte", "setting": "indoor", "scene": "cafe",
                  "food": ["latte"]}
        self.assertEqual(self.find(lobby, coffee, accuracy=65.0),
                         ("Lobby Coffee", "cafe", False))

    def test_a_building_part_and_the_building_around_it_are_one_building(self):
        # (The lobby is a building part inside the hotel's own outline: taken inside the
        # hotel, also for a picture that shows no kind of place.)
        lobby = offset(*HOTEL_HALL, east_m=-50)
        hall = {"title": "Waiting", "setting": "indoor", "scene": "hall", "food": []}
        self.assertEqual(self.find(lobby, hall, accuracy=65.0),
                         ("Harbour View Hotel", "hotel", True))
        self.assertEqual(self.find(offset(*lobby, north_m=30), hall, accuracy=65.0),
                         ("Harbour View Hotel", "hotel", True))

    def test_a_building_across_a_tile_border_is_measured_where_the_position_is(self):
        # A museum's own building reaches 15 m past the east border of its tile; the
        # tiles are cut 64 units past their borders, as the real ones are. A picture
        # 5 m east of its wall, in the next tile, is by the museum's building: the
        # museum (that the picture shows no kind of place does not drop it).
        near = offset(*QUAY, north_m=-900)
        fx, _fy = named_places._tile_xy(near[0], near[1], 15)
        edge = (near[0], (math.floor(fx) + 1) / float(1 << 15) * 360.0 - 180.0)
        museum = offset(*edge, east_m=-20)
        write_streets(FEATURES + INDOOR_FEATURES + [
            building(9951, [offset(*edge, -40, -40), offset(*edge, -40, 15),
                            offset(*edge, 40, 15), offset(*edge, 40, -40)]),
            poi(9951, "Example Border Museum", "museum", *museum, min_zoom=16)], buffer=64)
        reader = self.reader()
        position = offset(*edge, east_m=20)
        self.assertNotEqual(named_places._tile_xy(position[0], position[1], 15)[0] // 1,
                            fx // 1)
        metres = named_places.building_metres(reader, {9951}, *museum, *position)
        self.assertTrue(3 <= metres <= 7, metres)
        hall = {"title": "A hall", "setting": "indoor", "scene": "hall", "food": []}
        self.assertEqual(self.find(position, hall, accuracy=30.0),
                         ("Example Border Museum", "museum", False))

    def test_the_edges_a_tiles_border_cut_are_left_out_not_those_just_past_it(self):
        # (Tile units: one building whose west wall is 30 units past the tile's west
        # border, another cut by the border 64 units past it.)
        Feature = named_places.Feature
        outlines = named_places.Outlines(4096, [
            Feature(1, 3, {}, [[(-30, 1000), (500, 1000), (500, 1200), (-30, 1200)]]),
            Feature(2, 3, {}, [[(-64, 2000), (300, 2000), (300, 2200), (-64, 2200)]])])
        self.assertEqual(outlines.cuts, ({-64}, set()))
        self.assertAlmostEqual(outlines.distance(1, -50, 1100), 20.0)
        self.assertAlmostEqual(outlines.distance(2, -80, 2100), math.hypot(16, 100))
        self.assertEqual(named_places._cuts([[[(10, 10), (20, 10), (20, 20)]]], 4096),
                         (set(), set()))

    def test_two_restaurants_in_one_building_name_neither_unless_the_food_says(self):
        # (The chip shop is 8 m away, the sushi bar 35 m: the nearer one, before.)
        self.assertEqual(self.nearest(PIER_HALL, LUNCH).name, "Pier Chips")
        self.assertIsNone(self.find(PIER_HALL, LUNCH))
        sushi = dict(LUNCH, food=["sushi", "pickled ginger"])
        self.assertEqual(self.find(PIER_HALL, sushi), ("Pier Sushi", "restaurant", False))

    def test_a_dish_of_another_cuisine_is_not_from_the_place_next_door(self):
        burger = dict(LUNCH, food=["burger", "fries"])
        self.assertIsNone(self.find(THAI, burger))
        self.assertEqual(named_places.picture_facts(json.dumps(burger)).cuisines,
                         frozenset(("burger",)))
        # A drink fits any cuisine (a coffee with a curry).
        coffee = dict(LUNCH, food=["coffee"])
        self.assertEqual(self.find(THAI, coffee), ("Lotus Thai", "restaurant", False))

    def test_a_kind_of_shop_is_named_only_for_a_picture_that_shows_one(self):
        fitting = {"title": "Trying on a shirt", "setting": "indoor", "scene": "fitting room",
                   "food": []}
        self.assertIn("clothes", named_places.picture_facts(json.dumps(fitting)).shows)
        self.assertEqual(self.find(OUTFITTERS, fitting), ("Example Outfitters", "clothes",
                                                          False))
        self.assertIsNone(self.find(OUTFITTERS, {"title": "A face", "setting": "indoor"}))

    def test_a_picture_of_an_airport_near_the_edge_of_its_outline_names_it(self):
        terminal = {"title": "Waiting", "setting": "indoor", "scene": "airport terminal",
                    "subjects": ["baggage claim"]}
        outside = offset(*FIELD, east_m=10)         # (20 m outside the outline)
        self.assertIsNone(self.nearest(outside, terminal))
        self.assertEqual(self.find(outside, terminal), ("Quay Field Airport", "aerodrome",
                                                        True))
        self.assertIsNone(self.find(outside, {"title": "A face", "setting": "indoor"}))
        self.assertIsNone(self.find(offset(*FIELD, east_m=-60), terminal))
        # A ferry's or a bus's terminal by the airport's outline is no airport's.
        by_ferry = offset(*FIELD, north_m=150, east_m=10)     # (20 m outside the outline)
        for scene in ("ferry terminal", "bus terminal"):
            self.assertEqual(self.find(by_ferry, {"title": "Waiting", "setting": "indoor",
                                                  "scene": scene}),
                             ("Example Ferry Terminal", "ferry_terminal", False), scene)

    def test_only_a_place_to_eat_that_fits_counts_from_beyond_its_reach(self):
        # A scene word fits several kinds ("museum": a gallery too): the north gallery,
        # 70 m away (beyond its reach of 60), is not named for it.
        exhibits = {"title": "Old maps", "setting": "indoor", "scene": "museum"}
        self.assertIsNone(self.find(offset(*GALLERIES, north_m=120), exhibits))
        # A place to eat that fits the food shown, 45 m away (beyond its reach of 40):
        # where it is sat in is often further from a fix indoors.
        noodles = dict(LUNCH, food=["pad thai"])
        self.assertEqual(self.find(offset(*THAI, north_m=55), noodles),
                         ("Lotus Thai", "restaurant", False))

    def test_a_sign_naming_a_venue_reaches_further_than_its_kind(self):
        # A bookshop 40 m north of a position taken indoors: beyond its reach (15 m, and
        # half of the 15 m of the 20 m it may be off that count: 22.5 m), outside any
        # building outline. The sign in the picture names it: it is named from
        # NAMED_EXTRA_M further.
        shop = offset(*QUAY, east_m=-1200)
        write_streets(FEATURES + INDOOR_FEATURES + [
            poi(9961, "Example Page Books", "books", *offset(*shop, north_m=40),
                min_zoom=17)])
        sign = {"title": "Shelves of novels", "setting": "indoor", "scene": "shop",
                "text": "EXAMPLE PAGE BOOKS - new and used", "food": []}
        self.assertIsNone(self.nearest(shop, sign))
        self.assertEqual(self.find(shop, sign), ("Example Page Books", "books", False))
        with mock.patch.object(named_places, "NAMED_EXTRA_M", 0.0):
            self.assertIsNone(self.find(shop, sign))
        # Without the sign, nothing; with it but further than that, nothing either.
        self.assertIsNone(self.find(shop, dict(sign, text="")))
        self.assertIsNone(self.find(offset(*shop, north_m=-20), sign))

    def test_no_named_place_where_there_is_none(self):
        # A parking garage in the arts centre's building is not the arts centre.
        garage = {"title": "Our car on level 2", "setting": "indoor",
                  "scene": "parking garage", "food": []}
        self.assertEqual(self.nearest(ARTS_HALL, garage).name, "Example Arts Hall")
        self.assertTrue(named_places.picture_facts(json.dumps(garage)).nowhere)
        self.assertIsNone(self.find(ARTS_HALL, garage))
        # A plate on a picnic table by a mall was not eaten in the mall.
        plate = {"title": "Shrimp salad", "setting": "outdoor", "scene": "picnic table",
                 "food": ["shrimp", "salad"]}
        self.assertEqual(self.nearest(MARKET, plate).name, "Quay Market Mall")
        self.assertIsNone(self.find(MARKET, plate))
        self.assertEqual(self.find(MARKET, {"title": "Shoppers", "setting": "outdoor",
                                            "scene": "lawn"}),
                         ("Quay Market Mall", "mall", False))

    def test_two_galleries_side_by_side_name_neither(self):
        art = {"title": "Old paintings", "setting": "indoor", "scene": "art gallery"}
        # (The north one is 50 m away, the south one 72 m: beyond its reach, not by much.)
        self.assertEqual(self.nearest(GALLERIES, art).name, "North Gallery")
        self.assertIsNone(self.find(GALLERIES, art))
        self.assertEqual(self.find(offset(*GALLERIES, north_m=40), art),
                         ("North Gallery", "gallery", False))

    def test_only_the_pictures_taken_inside_read_the_building_outlines(self):
        reader = self.reader()
        facts = named_places.picture_facts(json.dumps({"title": "Boats", "setting": "outdoor",
                                                       "scene": "harbour"}))
        named_places.find(QUAY[0], QUAY[1], 20.0, reader, facts)
        self.assertEqual(len(reader.built), 0)
        named_places.find(QUAY[0], QUAY[1], 20.0, reader,
                          named_places.picture_facts(json.dumps(LUNCH)))
        self.assertGreater(len(reader.built), 0)

    def test_what_a_description_shows(self):
        facts = named_places.picture_facts(json.dumps(
            {"title": "Fish", "setting": "indoor", "scene": "public hall",
             "subjects": ["jellyfish"], "tags": ["shark"]}))
        self.assertIn("aquarium", facts.shows)
        self.assertNotIn("aquarium", facts.hints)
        view = named_places.picture_facts(json.dumps(
            {"title": "The city from the observation deck", "setting": "indoor",
             "subjects": ["museum"]}))
        self.assertEqual(view.shows, named_places.VIEW_KINDS)
        meal = named_places.picture_facts(json.dumps(
            {"title": "Pad thai", "setting": "indoor", "food": ["pad thai", "coffee"]}))
        self.assertEqual((meal.food, meal.cuisines), (True, frozenset(("thai", "coffee"))))
        self.assertEqual(named_places._cuisine_fit(frozenset(("thai",)), {"burger"}), -1)
        self.assertEqual(named_places._cuisine_fit(frozenset(("coffee",)), {"burger"}), 0)
        self.assertEqual(named_places._cuisine_fit(frozenset(("burger",)), {"burger"}), 1)
        self.assertEqual(named_places.cuisines_of({"kind_detail": "sushi;japanese"},
                                                  "Example House"), {"sushi", "ramen"})
        self.assertEqual(named_places.cuisines_of({}, "Example Pizza Corner"), {"pizza"})
        # (Outdoors, no parking garage counts: a lot is where many places are.)
        self.assertFalse(named_places.picture_facts(json.dumps(
            {"title": "Car", "setting": "outdoor", "scene": "parking garage"})).nowhere)
        # What it shows is part of the facts the rows are kept with: a new description
        # that shows something else is looked up again.
        plain = named_places.Position(1, 0.0, 0.0, 20.0, "gps", named_places.picture_facts(
            json.dumps({"title": "Fish", "setting": "indoor"})))
        shown = plain._replace(facts=facts._replace(words=plain.facts.words))
        self.assertNotEqual(named_places._facts_text(plain), named_places._facts_text(shown))
        # ... and so is the minute it was taken (its visit), when known.
        taken = plain._replace(taken="2024-05-01T10:05:42")
        self.assertEqual(named_places._facts_text(taken),
                         named_places._facts_text(plain) + "@2024-05-01T10:05")
        self.assertNotEqual(named_places._facts_text(taken._replace(taken="2024-05-01T13:05:42")),
                            named_places._facts_text(taken))
        self.assertTrue(named_places.approximate({"source": "owner",
                                                  "facts": "pa@2024-05-01T10:05"}))
        self.assertFalse(named_places.approximate({"source": "gps",
                                                   "facts": "psi@2024-05-01T10:05"}))

    def test_words_found_elsewhere_too_show_no_kind_of_place(self):
        def shows(setting="indoor", **info):
            return named_places.picture_facts(json.dumps(dict(info, setting=setting))).shows

        hotels = {"hotel", "motel", "hostel", "resort"}
        fairs = {"conference_centre", "exhibition_centre", "events_venue"}
        self.assertFalse(shows(title="Salmon on a bed of rice", food=["salmon"]) & hotels)
        self.assertFalse(shows(title="Pancakes", scene="diner booth", food=["pancakes"]) & fairs)
        self.assertFalse(shows(title="Dessert", tags=["food presentation"]) & fairs)
        self.assertNotIn("zoo", shows("outdoor", title="Sea lions on the dock"))
        self.assertNotIn("station", shows(title="Kids at the model trains",
                                          scene="living room"))
        self.assertNotIn("winery", shows(title="Steak dinner", scene="restaurant",
                                         subjects=["wine barrels"]))
        self.assertNotIn("supermarket", shows(title="Unpacking groceries", scene="kitchen"))
        self.assertFalse(shows(title="Flowers", scene="garden", subjects=["flower beds"]))
        self.assertFalse(named_places._indoorish(named_places.picture_facts(json.dumps(
            {"title": "Flowers", "setting": "outdoor", "scene": "garden",
             "subjects": ["flower beds"]}))))
        self.assertFalse(shows(title="Waiting", scene="ferry terminal") & {"aerodrome",
                                                                           "airport"})
        # What names the place still does.
        self.assertIn("hotel", shows(title="A made hotel bed"))
        self.assertIn("conference_centre", shows(title="Exhibitor booths"))
        self.assertIn("zoo", shows("outdoor", title="Lions resting"))
        self.assertIn("station", shows(title="Trains at the platform"))
        self.assertIn("airport", shows(title="Planes", subjects=["airline counter"]))
        self.assertIn("winery", shows(title="Casks", scene="barrel room"))


class VisitTest(Fixture, unittest.TestCase):
    """The pictures of one visit agree on its place."""

    def setUp(self):
        super().setUp()
        write_streets(FEATURES + INDOOR_FEATURES)
        self.inside = inside = offset(*AQUARIUM_HALL, north_m=10)
        self.tanks = [self.picture("tank-%d.jpg" % number, *inside, info=TANK,
                                   taken_at="2024-05-01T10:%02d:00" % number,
                                   gps={"GPSHPositioningError": 15.0})
                      for number in (1, 2)]
        # A close-up of a fish, its fix 200 m off (more than MAX_ACCURACY_M): areas only.
        far = offset(*AQUARIUM_HALL, north_m=150)
        self.fish = self.picture("fish-close.jpg", *far,
                                 info={"title": "A fish", "setting": "indoor",
                                       "scene": "close-up"},
                                 taken_at="2024-05-01T10:05:00",
                                 gps={"GPSHPositioningError": 200.0})
        # The same an hour later: another visit.
        self.later = self.picture("fish-later.jpg", *far,
                                  info={"title": "A fish", "setting": "indoor",
                                        "scene": "close-up"},
                                  taken_at="2024-05-01T11:30:00",
                                  gps={"GPSHPositioningError": 200.0})

    def test_a_picture_of_a_visit_takes_the_place_the_others_agree_on(self):
        summary = named_places.update(self.conn, self.config)
        rows = named_places.stored(self.conn)
        for tank in self.tanks:
            self.assertEqual((rows[tank]["name"], rows[tank]["inside"]), ("Reef Aquarium", 1))
        self.assertEqual(rows[self.fish]["name"], "Reef Aquarium")
        self.assertEqual(rows[self.later]["name"], "")
        self.assertEqual(summary["agreed"], 1)
        # (Its distance is the fix's: 150 m, and the fix may be 200 m off: "nearby".)
        keys = named_places.record_keys(rows[self.fish])
        self.assertEqual((keys["np"], keys.get("na"), keys.get("nd")), ("Reef Aquarium", 1, None))
        found = [position.image_id for members in named_places.visits(
            named_places.positions(self.conn, self.config)) for _seconds, position in members]
        self.assertEqual(sorted(found), sorted(self.tanks + [self.fish]))

    def test_a_visit_is_looked_up_whole(self):
        named_places.update(self.conn, self.config)
        self.conn.execute("UPDATE images SET info = ? WHERE id = ?",
                          (json.dumps(dict(TANK, title="Fish in a big tank")), self.tanks[0]))
        self.conn.commit()
        summary = named_places.update(self.conn, self.config)
        self.assertEqual(summary["looked_up"], 3)
        self.assertEqual(named_places.stored(self.conn)[self.fish]["name"], "Reef Aquarium")

    def set(self, image_id, **values):
        self.conn.execute("UPDATE images SET %s WHERE id = ?" % ", ".join(
            "%s = ?" % name for name in values), list(values.values()) + [image_id])
        self.conn.commit()

    def names(self, *ids):
        rows = named_places.stored(self.conn)
        return tuple(rows[image_id]["name"] for image_id in ids)

    def test_a_picture_whose_time_changed_is_in_another_visit(self):
        named_places.update(self.conn, self.config)
        self.assertEqual(self.names(self.fish, self.later), ("Reef Aquarium", ""))
        # (Their times set right: the close-up was taken later, the other one then.)
        self.set(self.fish, taken_at="2024-05-01T11:35:00")
        self.set(self.later, taken_at="2024-05-01T10:05:00")
        summary = named_places.update(self.conn, self.config)
        self.assertEqual(summary["looked_up"], 4)
        self.assertEqual(self.names(self.fish, self.later), ("", "Reef Aquarium"))

    def test_the_visit_of_a_picture_that_lost_its_position_is_looked_up_again(self):
        named_places.update(self.conn, self.config)
        self.assertEqual(self.names(self.fish), ("Reef Aquarium",))
        # (One tank picture's position set aside: one shot of the tanks is left, too few
        # for the close-up that shows nothing of them.)
        self.set(self.tanks[0], lat=None, lon=None)
        summary = named_places.update(self.conn, self.config)
        self.assertEqual((summary["removed"], summary["looked_up"]), (1, 2))
        self.assertEqual(self.names(self.tanks[1], self.fish), ("Reef Aquarium", ""))

    def test_copies_of_one_shot_vote_once(self):
        # (The second tank picture a copy of the first: the same time and position.)
        self.set(self.tanks[1], taken_at="2024-05-01T10:01:00")
        named_places.update(self.conn, self.config)
        self.assertEqual(self.names(self.fish), ("",))
        self.picture("tank-3.jpg", *self.inside, info=TANK, taken_at="2024-05-01T10:03:00",
                     gps={"GPSHPositioningError": 15.0})
        named_places.update(self.conn, self.config)
        self.assertEqual(self.names(self.fish), ("Reef Aquarium",))

    def test_a_visit_more_than_the_quick_limit_looks_up_the_changed_picture_at_once(self):
        named_places.update(self.conn, self.config)
        moved = offset(*self.inside, north_m=2)
        self.set(self.tanks[0], lat=moved[0], lon=moved[1])
        # (Its visit has three pictures: the moved one now, the others with it next time.)
        summary = named_places.update(self.conn, self.config, limit=2)
        self.assertEqual(summary["looked_up"], 1)
        rows = named_places.stored(self.conn)
        self.assertTrue(named_places.applies(rows[self.tanks[0]], moved[0], moved[1], ("gps",)))
        self.assertEqual(rows[self.tanks[0]]["name"], "Reef Aquarium")
        self.assertEqual((rows[self.tanks[1]]["maps"], rows[self.fish]["maps"]), ("", ""))
        self.assertEqual(rows[self.later]["maps"], named_places.maps_key())
        self.assertEqual(named_places.update(self.conn, self.config)["looked_up"], 3)
        rows = named_places.stored(self.conn)
        self.assertEqual({rows[image_id]["maps"] for image_id in self.tanks + [self.fish]},
                         {named_places.maps_key()})
        self.assertEqual(self.names(self.fish), ("Reef Aquarium",))

    def test_a_visits_area_is_taken_inside_it_only_from_inside(self):
        # Two pictures of the giraffes inside the zoo; a room 110 m north of its outline
        # (nothing of it shown, near enough: the zoo, "nearby"), and a corridor inside it
        # whose position may be too far off to tell (the zoo, taken inside it).
        house = {"title": "Giraffes", "setting": "indoor", "scene": "giraffe house"}
        for minute in (0, 2):
            self.picture("giraffes-%d.jpg" % minute, *offset(*ZOO, north_m=200), info=house,
                         taken_at="2024-05-02T14:%02d:00" % minute,
                         gps={"GPSHPositioningError": 20.0})
        room = self.picture("room.jpg", *offset(*ZOO, north_m=360),
                            info={"title": "Us", "setting": "indoor", "scene": "room"},
                            taken_at="2024-05-02T14:04:00", gps={"GPSHPositioningError": 20.0})
        corridor = self.picture("corridor.jpg", *offset(*ZOO, north_m=220),
                                info={"title": "Us", "setting": "indoor", "scene": "corridor"},
                                taken_at="2024-05-02T14:06:00",
                                gps={"GPSHPositioningError": 150.0})
        named_places.update(self.conn, self.config)
        rows = named_places.stored(self.conn)
        self.assertEqual(named_places.record_keys(rows[room]),
                         {"np": "City Zoo", "nk": "zoo", "na": 1})
        self.assertEqual(named_places.record_keys(rows[corridor]),
                         {"np": "City Zoo", "nk": "zoo", "ni": 1})

    def test_a_visit_is_written_as_it_goes(self):
        # (Other writers get in between the visits and the other pictures: see
        # test_other_writers_get_in_while_places_are_looked_up.)
        other = db.connect()
        self.addCleanup(other.close)
        seen = []
        real = named_places.find

        def counting(*args, **kwargs):
            seen.append(other.execute("SELECT COUNT(*) FROM named_places").fetchone()[0])
            return real(*args, **kwargs)

        with mock.patch.object(named_places, "find", counting), \
                mock.patch.object(named_places, "COMMIT_SECONDS", 0.0):
            named_places.update(self.conn, self.config)
        # (The visit of three first or last: the other picture's row is written before
        # or after it, never in its middle.)
        self.assertIn(seen, ([0, 0, 0, 3], [0, 1, 1, 1]))


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
        # static/streets.js PLACE_REACH (the circle a picked place searches) is KINDS' and
        # SHOWN_SHOPS' (a clothes shop is named for a picture that shows one).
        with open(os.path.join(support.REPO, "static", "streets.js"), encoding="utf-8") as handle:
            text = handle.read()
        table = text[text.index("var PLACE_REACH = {"):]
        table = re.sub(r"//[^\n]*", "", table[table.index("{") + 1:table.index("}")])
        reach = {name.strip().strip('"'): int(value) for name, value in
                 (pair.split(":") for pair in table.split(",") if pair.strip())}
        wanted = {kind: metres for kind, (_group, metres) in named_places.KINDS.items()}
        wanted.update(named_places.SHOWN_SHOPS)
        self.assertEqual(reach, wanted)
        indoor = re.search(r"var PLACE_INDOOR_M = (\d+);", text).group(1)
        self.assertEqual(float(indoor), named_places.INDOOR_ACCURACY_M)

    def test_the_viewer_says_what_a_shop_named_only_when_shown_sells(self):
        # static/viewer.js KIND_WORDS: a kind of SHOWN_SHOPS whose name alone says little
        # ("car", "outdoor") reads as the shop it is ("car dealer").
        with open(os.path.join(support.REPO, "static", "viewer.js"), encoding="utf-8") as handle:
            text = handle.read()
        table = text[text.index("var KIND_WORDS = {"):]
        table = table[table.index("{") + 1:table.index("}")]
        words = dict(re.findall(r'(\w+): "([^"]+)"', table))
        self.assertEqual(words["car"], "car dealer")
        for kind in named_places.SHOWN_SHOPS:
            self.assertTrue(kind in words or kind in ("supermarket", "florist", "variety_store"),
                            kind)


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

    def test_a_row_found_with_several_names_shows_one(self):
        # (A row found before the rules said so, until the next update finds it
        # again: RULES_VERSION.)
        self.conn.execute("UPDATE named_places SET name = ? WHERE image_id = ?",
                          ("City Zoo ; Zoo of the City", self.zoo))
        self.conn.commit()
        record = search_index.picture_record(self.conn, self.zoo)
        self.assertEqual(record["np"], "City Zoo")
        self.assertEqual(named_places.record_keys(
            {"name": "City Zoo;Zoo of the City", "kind": "zoo", "inside": 1}, True),
            {"np": "City Zoo", "nk": "zoo", "ni": 1})

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
        self.assertNotIn("older rules", printed)
        self.assertEqual(named_places.status(self.conn, self.config)["older"], 0)
        # Rows of older rules (a new version of them): the status says so, until an
        # update looks them up again.
        with mock.patch.object(named_places, "RULES_VERSION", named_places.RULES_VERSION + 1):
            self.assertEqual(named_places.status(self.conn, self.config)["older"], 1)
            code, printed = self.run_main()
            self.assertIn("1 of them was looked up with older rules or street maps: "
                          "python named_places.py --update", printed)
            self.run_main("--update")
            self.assertEqual(named_places.status(self.conn, self.config)["older"], 0)
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

    def test_a_smart_album_of_a_kind_of_place_goes_with_the_location(self):
        import albums
        from test_export import read_manifest
        searches = {"Zoos": "kind:zoo OR zoos", "At a zoo": "kind:zoo",
                    "Fish, no zoo": "fish -kind:zoo", "Zoos 2019": "zoos date:2019 OR 2019"}
        for name, query in searches.items():
            albums.create_album(self.conn, name, kind="smart", query=query)
        self.conn.commit()

        def published(location):
            out = self.site(location=location, marks=True)
            return out, {album["name"]: album["query"] for album in read_manifest(out)["albums"]}

        out, found = published(True)
        self.assertEqual(found, searches)
        # The published site finds the picture taken at the zoo by its kind
        # (search-core.js, as its page runs it).
        if NODE is not None:
            self.assertEqual(self.search_site(out, ["kind:zoo", "zoos", "kind:aquarium"]),
                             {"kind:zoo": ["City Zoo"], "zoos": ["City Zoo"],
                              "kind:aquarium": ["Harbour Aquarium"]})
        # Without location: no named place, no kind; the words typed stay.
        out, found = published(False)
        self.assertEqual(found, {"Zoos": "zoos", "Zoos 2019": "zoos date:2019 OR 2019"})
        # With location but no named place published (named places off): the
        # same -- kind: alone would find nothing there.
        self.base.write_config([support.library("Family", self.family, id="lib_family1")],
                               named_places={"enabled": False})
        self.config = common.load_config()
        out, found = published(True)
        self.assertFalse(any("nk" in record for record in self.records(location=True)))
        self.assertEqual(found, {"Zoos": "zoos", "Zoos 2019": "zoos date:2019 OR 2019"})

    def search_site(self, out, queries):
        """{query: [named places]} of the published site's records, searched (and
        understood: plain language) by its search-core.js under Node."""
        from test_export import read_records
        path = self.base.join("records.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(read_records(out), handle)
        script = ("var S = require(process.argv[1]); var r = require(process.argv[2]);"
                  "var x = S.prepare(r); var out = {};"
                  "JSON.parse(process.argv[3]).forEach(function (q) {"
                  "  var said = x.interpret(q, {});"
                  "  out[q] = x.search(said.query, {}).items.map(function (i) {"
                  "    return x.get(i).np; }); });"
                  "console.log(JSON.stringify(out));")
        result = subprocess.run([NODE, "-e", script, os.path.join(out, "assets",
                                                                  "search-core.js"),
                                 path, json.dumps(queries)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


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

    def test_no_named_place_found_in_the_home_area_for_a_picture_out_of_it(self):
        # A picture of a visit 1 km out of the home area (not in home's town) with the
        # place the visit agreed on, found for the positions of its pictures in the home
        # area (named_places.agree: up to a few km from its own): it does not go.
        home_area = trip_export.HomeArea(test_trip_export.HOME, trip_export.HOME_KM)
        east = home_area.west + home_area.width
        lat = test_trip_export.HOME[0]
        cursor = self.conn.execute(
            "INSERT INTO images (library_id, path, name, size, mtime, ctime, width, height, "
            "added, updated, taken_at, taken_source, content_hash, lat, lon, has_preview, "
            "info, described, prompt_ver, place_city, place_region, place_country, place_cc, "
            "place_km) VALUES (?, 'out.jpg', 'out.jpg', 1000, 1, 1, 160, 120, 1, 1, "
            "'2024-05-10T06:05:00', 'exif', 'out', ?, ?, 1, ?, 1, 5, 'Edgeton', "
            "'Region One', 'Countryland', 'CL', 1.0)",
            (test_trip_export.LAKES, lat, east + 0.012,
             json.dumps(test_trip_export.info("Picture out", ["view"]))))
        out_id = cursor.lastrowid
        import cache
        import imagegen
        thumb = imagegen.jpeg_bytes(imagegen.textured_image((160, 120)))
        with cache.ThumbStore() as thumbs:
            thumbs.put(out_id, thumb)
        cache.PreviewStore().write(test_trip_export.LAKES, out_id, thumb)
        self.conn.commit()
        self.assertFalse(home_area.contains(lat, east + 0.012))
        named_places.update(self.conn, common.load_config())
        self.conn.execute("UPDATE named_places SET name = 'Homeside Park', kind = 'park', "
                          "metres = 0, inside = 0 WHERE image_id = ?", (out_id,))
        self.conn.commit()
        out, _summary = self.export("named-visit", {"location": True, "story": True})
        records = self.records(out)
        self.assertEqual(records["Picture out"].get("ci"), "Edgeton")
        for key in named_places.RECORD_KEYS:
            self.assertNotIn(key, records["Picture out"])
        self.assertEqual(records["Picture 10-0"]["np"], "Lake Museum")
        self.assertNotIn("Homeside", "\n".join(self.texts(out).values()))

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

    def test_no_kind_of_place_when_only_home_has_named_places(self):
        # Only the picture in home's town (out of the home area) keeps a named place:
        # the trip leaves it out (after the albums are chosen), so no kind of place is
        # published, and a smart album of a kind goes as a library's export without
        # named places does: left out, or with the words typed only.
        import albums
        from test_export import read_manifest
        self.conn.execute("UPDATE named_places SET name = '', kind = '' WHERE image_id <> ?",
                          (self.edge_id,))
        for name, query in (("Museums", "kind:museum"),
                            ("Museums, or the word", "kind:museum OR museums")):
            albums.create_album(self.conn, name, kind="smart", query=query)
        self.conn.commit()
        out, _summary = self.export("home-kinds", {"location": True, "marks": True})
        records = self.records(out)
        self.assertIn("Picture edge", records)
        self.assertFalse(any(key in record for record in records.values()
                             for key in named_places.RECORD_KEYS))
        manifest = read_manifest(out)
        self.assertFalse(manifest["capabilities"].get("named_places"))
        self.assertEqual({album["name"]: album["query"] for album in manifest["albums"]},
                         {"Museums, or the word": "museums"})


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

    def test_choosing_a_place_answers_the_other_pictures_of_its_visit(self):
        # A visit at the made-up quay's aquarium: a picture of its tanks, a close-up whose
        # fix is 200 m off (one shot of the tanks is too few for it to take their place)
        # and a picture of the tanks without a position. Its place set in the aquarium,
        # it is a second shot of the tanks: the close-up is the aquarium's too.
        write_streets(FEATURES + INDOOR_FEATURES)
        place_estimates.ensure_tables(self.conn)
        inside = offset(*AQUARIUM_HALL, north_m=10)
        self.picture("tank.jpg", *inside, info=TANK, taken_at="2024-05-01T10:01:00",
                     gps={"GPSHPositioningError": 15.0})
        fish = self.picture("fish.jpg", *offset(*AQUARIUM_HALL, north_m=150),
                            info={"title": "A fish", "setting": "indoor", "scene": "close-up"},
                            taken_at="2024-05-01T10:05:00", gps={"GPSHPositioningError": 200.0})
        tank = self.picture("tank-2.jpg", info=TANK, taken_at="2024-05-01T10:03:00")
        named_places.update(self.conn, self.config)
        self.assertEqual(named_places.stored(self.conn)[fish]["name"], "")
        with mock.patch("places.shared", lambda: None):
            reply = self.post("/images/%d/place" % tank,
                              {"choice": "set", "lat": inside[0], "lon": inside[1]})
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        answer = reply.get_json()
        # (Its own record has it when its place has names: none here.)
        self.assertEqual(named_places.stored(self.conn)[tank]["name"], "Reef Aquarium")
        self.assertEqual((answer["others"][str(fish)]["np"], answer["more"]),
                         ("Reef Aquarium", 0))


@unittest.skipIf(NODE is None, "Node.js is not installed")
class KindsTest(unittest.TestCase):
    """The kinds of place the search knows (search-core.js PLACE_KIND_LIST, kind:) are
    kinds of named places (KINDS); the kinds it leaves to kind: by their own name
    are these."""

    # Shops of one thing, land cover, and areas with a name that is no place to go.
    BY_NAME_ONLY = {"antiques", "art", "cheese", "chocolate", "confectionery", "deli", "wine",
                    "bare_rock", "grass", "heath", "meadow", "sand", "scrub", "wetland", "cape",
                    "commercial", "pedestrian", "railway"}

    def test_the_kinds_of_the_search_are_those_of_the_named_places(self):
        result = subprocess.run(
            [NODE, "-e", "console.log(JSON.stringify(require(process.argv[1]).placeKinds()))",
             os.path.join(support.REPO, "static", "search-core.js")],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = json.loads(result.stdout)
        found = {kind for line in lines for kind in line["kinds"]}
        # (And shops named only for a picture that shows one, by what the viewer calls
        # them: "car dealers".)
        self.assertEqual(found - set(named_places.KINDS) - set(named_places.SHOWN_SHOPS),
                         set())
        self.assertEqual(set(named_places.KINDS) - found, self.BY_NAME_ONLY)
        with open(os.path.join(support.REPO, "static", "viewer.js"), encoding="utf-8") as handle:
            text = handle.read()
        table = text[text.index("var KIND_WORDS = {"):]
        words = dict(re.findall(r'(\w+): "([^"]+)"', table[:table.index("}")]))
        keys = {line["key"]: line for line in lines}
        for kind in named_places.SHOWN_SHOPS:
            self.assertIn(words.get(kind, kind.replace("_", " ")).lower(), keys[kind]["words"],
                          kind)
        # (An airport's kinds, as kind_words shows them.)
        self.assertIn({"key": "airport", "label": "airports",
                       "words": ["airport", "aerodrome", "airfield"],
                       "kinds": ["airport", "aerodrome"]}, lines)


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
