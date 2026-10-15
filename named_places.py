#!/usr/bin/env python3
"""Named places from the street maps: the park, beach, museum or cafe where a picture was taken.

The street maps (street_maps.py: vector tiles of the Protomaps basemap,
OpenStreetMap data, downloaded once around the places of the pictures)
hold the names of what is there: points of interest (the "pois" layer:
museums, aquariums, viewpoints, beaches, stations, restaurants, ...) and
areas (the "landuse" layer: parks, nature reserves, zoos, airports,
campuses, ...; an area's name is on the point of interest with the same
id, at its label). For each picture with a position, find() picks the
most fitting named place from the tiles around it, reading the local
street maps only (nothing is fetched):

  - an area the position is inside of (a zoo, a park, a beach, an
    airport), at least half as deep inside as the position may be off;
    an airport (DOMINANT) is the place of everything inside it, or
  - a point of interest within its reach (KINDS: a viewpoint or a museum
    is seen from further away than a cafe is sat in; the reach of kinds
    that name small things as well as large ones, SCALED, shrinks with
    their label's min_zoom: a chapel or a ride is not a cathedral) plus
    how far off the position may be, up to half the reach again;

the most specific of them wins (candidates' score: a sight near the
position before the park it is in, a small area before a large one, a
zoo or an aquarium before the stage or the cafe in it; places to eat,
shop and stay by how near they are). Kinds that name no place a picture
is taken at -- bus stops, parking, cash machines, offices, most shops --
are never chosen, and neither are homes: residential areas and
buildings, houses, flats and guest houses (EXCLUDED; a kind not listed
in KINDS is left out too), nor a boat-tour operator's stand (OPERATOR).
When the position cannot tell the best point of interest from another
of its class (choose: their distances differ by less than how far off
it may be, at least MARGIN_M), neither is chosen: the area the position
is in, or none -- the cafe next door is not a guess worth showing.

The picture's description counts too (picture_facts, Facts):

  - a place it names (named_in: "the Harbour Museum and the Old Tower at
    dusk", "a Gull Cafe cup on a table") wins, and is never in doubt; a
    place of a kind its scene or title says (KIND_HINTS: "theater", "hotel
    room") counts more, and is in doubt only with another of such a kind;
  - one taken from a vehicle (or moving at MOVING_KMH or more, by its GPS
    speed) gets areas only: a drive passes many places; one taken from a
    plane (trips.flying, or faster than FLYING_KMH; or by its description,
    FLIGHT_WORDS: above the clouds) gets none;
  - a place to eat or drink (EAT) only for a picture that shows food or a
    meal, or whose scene, title or tags say it was taken in one
    (EAT_WORDS) -- a shop or a theatre is not the cafe beside it; an
    indoor meal (MEAL_BONUS) makes them as specific as a sight;
  - a shop (a souvenir shop, a market) not for a picture taken outdoors
    without food (a street of shops);
  - in a street or a square (STREET_WORDS) only areas and
    landmarks (labelled from LANDMARK_ZOOM out) count from further than
    NEAR_M: the museum across the street is not where a street scene was
    taken; indoors, the point of an area (a college's) only within NEAR_M.

How far off a position may be (accuracy_m):

  gps       the GPS position of the file: its GPSHPositioningError (a
            phone writes it), else DEFAULT_ACCURACY_M (INDOOR_ACCURACY_M
            for a picture taken indoors: a fix indoors is often worse); none
            when its file names a country other than the position's by the
            place names (its names are the file's, see
            place_estimates.gps_file_columns: a place there might be across
            the border)
  track     a video's position from its GPS track (tracks.py: its middle
            fix): how far its furthest fix is from it; areas only (a
            drive passes many places)
  estimate  a place estimated from copies or the pictures taken around
            the same time (place_estimates.py), with a radius of at most
            max_estimate_km (default 0.3): that radius; an approximate
            position, which gets no place to eat, shop or stay and only
            points of interest whose reach is at least the radius
  owner     a position the owner set (or an estimate the owner accepted:
            its radius, approximate as an estimate); a town the owner chose
            (its middle: place_estimates.town_pick) has no named place

A position that may be more than MAX_ACCURACY_M off gets no point of
interest, only an area it is deep enough inside of. The distance shown
(record_keys) is left out when the position may be off by more than it,
or is approximate: "nearby", "probably taken inside it".

The rules and their numbers were chosen by looking at some 170 pictures
of a real library and the place found for each (see the README's "Named
places").

The named places are kept in index.db (ensure_table; a table made on
first use, not a schema version: everything in it can be worked out
again), one row for each picture looked up (also when none was found):

    named_places  image_id, name ('' when none), kind (the point of
                  interest's kind: "museum", "park", ...), metres (how far
                  it is; 0 inside an area), inside (1 for an area the
                  position is in), source ("gps", "track", "estimate" or
                  "owner"), lat, lon, accuracy_m (the position looked up
                  and how far off it may be), maps (the street maps and
                  rules it was found with: maps_key), updated

update() looks up the pictures whose position (or its source or accuracy)
changed since, and all of them when the street maps or RULES_VERSION
changed; a change touches the picture's images.updated, so the search
index notices. It runs after each scan, previews and places job, after the
street maps are downloaded, when the owner decides about a picture's place
(at once, for a few pictures) and as a job of its own ("named_places",
python named_places.py). Without street maps there are none: deleting
them forgets them (clear).

Search records (search_index) carry the name (np), the kind (nk) and the
distance in metres (nd; left out inside an area, with ni 1, and for an
approximate position or distance, with na 1): the viewer
shows it before the town ("Harbour Aquarium", then "Seaport, Region, Country"),
the search finds its words (place: too) and plain-language search offers
it as a place; the map lists it with its pictures. A named place is
location data: an export holds it only with its location option, for an
estimated place only with estimated_places as well, and only when the
position it was found from is published (a video's track position only
with its routes; never in the home area of a trip's export), its
distance rounded to EXPORT_METRES. Turned off (config.json "named_places"
enabled false) or without street maps, the records have none at once
(for_libraries, shown_key), before the next job forgets them.

    python named_places.py [--status]      how many pictures have one
    python named_places.py --update        look up the pictures that changed
    python named_places.py --redo          look up every picture again
    python named_places.py --lookup LAT LON
                                           the named places of a position, best first

This module uses only the standard library.
"""

import argparse
import collections
import gzip
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import unicodedata
import zlib

import common
import street_maps
import world_map

# Raise when the rules (KINDS, the scores, how areas are named) change: every
# picture is looked up again. (3: one name per place, see first_name.)
RULES_VERSION = 3
# The tiles read: the most detailed zoom of the street maps (at most this),
# and, for the names of areas whose label is elsewhere, the tiles at the
# position down to LABEL_ZOOM_MIN.
ZOOM = 15
LABEL_ZOOM_MIN = 10
# How far off a GPS position may be when its file does not say (indoors: worse; about
# the 75th percentile of the fixes taken indoors that do say).
DEFAULT_ACCURACY_M = 30.0
INDOOR_ACCURACY_M = 65.0
# A position that may be off by more than this gets areas only.
MAX_ACCURACY_M = 150.0
# By the speed its GPS states (km/h): moving at least MOVING_KMH, a picture gets areas
# only (taken from a car, a train or a boat); faster than FLYING_KMH (or trips.flying:
# high up), none (taken from a plane).
MOVING_KMH = 30.0
FLYING_KMH = 250.0
# Two points of interest of one class (choose) are told apart only when their
# distances differ by more than how far off the position may be, and at least this.
MARGIN_M = 25.0
# ... unless the other one is far less prominent: its label needs more than this many
# zooms closer than the best one's.
RIVAL_ZOOMS = 1
# In a street scene, a point of interest that is no landmark (labelled on the map from
# LANDMARK_ZOOM out) counts within NEAR_M only: it is where the picture was taken, or
# none; so does the point of an area (a park's, a college's) for a picture taken
# indoors (not in the area: in a building near it).
NEAR_M = 20.0
LANDMARK_ZOOM = 14
# Two sights whose points are this close are one (two names of one tower).
SAME_SPOT_M = 15.0
# A point of interest the description names (named_in: "the Harbour Museum at dusk")
# counts this much more, and one of a kind its scene or title says
# (KIND_HINTS: "theater") this much more.
NAMED_BONUS = 1.0
HINT_BONUS = 0.6
# Of a description's text (what is written in the picture), this many characters are
# looked at for names.
WORDS_TEXT = 1000
# Of an exported record, the distance is rounded to this many metres (the position
# is rounded too, see export.LOCATION_DECIMALS).
EXPORT_METRES = 10
# Of an estimated place, at most this radius gets a named place (km; settings).
DEFAULTS = {"enabled": True, "max_estimate_km": 0.3}
RANGES = {"max_estimate_km": (0.0, 2.0)}
EARTH_M = 6371008.8
# Decoded tiles kept while looking up (pictures of one place come together).
TILE_CACHE = 64
# The places are looked up with no write lock held, and written this many at a
# time, or what was looked up in this many seconds, whichever comes first: a
# write of the server (a favorite, a place chosen) never waits long.
COMMIT_ROWS = 500
COMMIT_SECONDS = 2.0
# The admin's choices look up at most this many pictures at once (the rest
# wait for the next job).
QUICK_LIMIT = 200

SIGHT, VENUE, AREA, NATURE = "sight", "venue", "area", "nature"
# kind -> (group, reach in metres). sight: a place people go to see, named
# from around it; venue: a place to eat, drink or stay, named only from
# right there; area: named from inside (and from near its label point when
# no outline holds the position); nature: an area of land cover with a
# name of its own (a meadow, a rock), the least specific.
KINDS = {
    # sights
    "attraction": (SIGHT, 150), "viewpoint": (SIGHT, 150), "museum": (SIGHT, 100),
    "aquarium": (SIGHT, 150), "castle": (SIGHT, 150), "monument": (SIGHT, 100),
    "memorial": (SIGHT, 60), "lighthouse": (SIGHT, 150), "waterfall": (SIGHT, 150),
    "cave_entrance": (SIGHT, 100), "fountain": (SIGHT, 60),
    "place_of_worship": (SIGHT, 80), "stadium": (SIGHT, 200), "theatre": (SIGHT, 50),
    "cinema": (SIGHT, 50), "gallery": (SIGHT, 50), "arts_centre": (SIGHT, 60),
    "ruins": (SIGHT, 100), "archaeological_site": (SIGHT, 150), "fort": (SIGHT, 150),
    "tower": (SIGHT, 80), "planetarium": (SIGHT, 80), "observatory": (SIGHT, 100),
    "library": (SIGHT, 50), "townhall": (SIGHT, 80), "marina": (SIGHT, 100),
    "ferry_terminal": (SIGHT, 100), "station": (SIGHT, 150), "peak": (SIGHT, 300),
    "volcano": (SIGHT, 1000), "cape": (NATURE, 150), "hot_spring": (SIGHT, 100),
    "geyser": (SIGHT, 100), "winery": (SIGHT, 150), "brewery": (SIGHT, 80),
    "casino": (SIGHT, 120), "mall": (SIGHT, 120),
    "events_venue": (SIGHT, 80), "conference_centre": (SIGHT, 100),
    "exhibition_centre": (SIGHT, 120), "picnic_site": (SIGHT, 60),
    "amusement_arcade": (SIGHT, 40), "bridge": (SIGHT, 100),
    "sports_centre": (SIGHT, 80), "ice_rink": (SIGHT, 60),
    "bus_station": (SIGHT, 60), "ski": (SIGHT, 300), "winter_sports": (SIGHT, 300),
    # places to eat, drink and stay
    "restaurant": (VENUE, 30), "cafe": (VENUE, 30), "bar": (VENUE, 30), "pub": (VENUE, 30),
    "fast_food": (VENUE, 25), "ice_cream": (VENUE, 25), "food_court": (VENUE, 50),
    "bakery": (VENUE, 25), "nightclub": (VENUE, 30), "biergarten": (VENUE, 40),
    "wine_bar": (VENUE, 30), "hotel": (VENUE, 60), "motel": (VENUE, 50),
    "hostel": (VENUE, 40), "resort": (VENUE, 150), "camp_site": (VENUE, 150),
    "caravan_site": (VENUE, 100), "alpine_hut": (VENUE, 60),
    # shops people go to for themselves (a souvenir shop, a market), named only from
    # right there
    "department_store": (VENUE, 40), "gift": (VENUE, 15), "marketplace": (VENUE, 40),
    "chocolate": (VENUE, 15), "confectionery": (VENUE, 15), "deli": (VENUE, 15),
    "cheese": (VENUE, 15), "wine": (VENUE, 15), "books": (VENUE, 15), "art": (VENUE, 15),
    "antiques": (VENUE, 15),
    # areas (and their label points)
    "park": (AREA, 150), "nature_reserve": (AREA, 200), "national_park": (AREA, 1000),
    "protected_area": (AREA, 200), "zoo": (AREA, 200), "theme_park": (AREA, 250),
    "beach": (AREA, 200), "garden": (AREA, 100), "golf_course": (AREA, 200),
    "cemetery": (AREA, 100), "university": (AREA, 200), "college": (AREA, 150),
    "school": (AREA, 60), "hospital": (AREA, 80), "aerodrome": (AREA, 300),
    "airport": (AREA, 300), "dog_park": (AREA, 60), "playground": (AREA, 40),
    "pitch": (AREA, 60), "pedestrian": (AREA, 60), "commercial": (AREA, 60),
    "pier": (AREA, 100), "railway": (AREA, 100), "water_park": (AREA, 100),
    "recreation_ground": (AREA, 100), "village_green": (AREA, 80),
    # land cover with a name
    "meadow": (NATURE, 100), "grass": (NATURE, 40), "bare_rock": (NATURE, 100),
    "scrub": (NATURE, 100), "wood": (NATURE, 150), "forest": (NATURE, 200),
    "sand": (NATURE, 150), "heath": (NATURE, 150), "wetland": (NATURE, 150),
    "glacier": (NATURE, 1000),
}
# Venues where one stays: their grounds and views name them too (see Facts.shops).
LODGING = frozenset(("hotel", "motel", "hostel", "resort", "camp_site", "caravan_site",
                     "alpine_hut"))
# Venues to eat and drink at: only for a picture of food or a meal, or one taken in
# such a place (Facts.eat).
EAT = frozenset(("restaurant", "cafe", "bar", "pub", "fast_food", "ice_cream", "food_court",
                 "bakery", "nightclub", "biergarten", "wine_bar"))
# The words of a description (its scene, title and tags) that say a picture was taken
# where one eats or drinks (Facts.eat), and those of a street scene (Facts.street).
EAT_WORDS = re.compile(r"\b(restaurants?|cafés?|cafes?|coffee|bars?|pubs?|diners?|"
                       r"bistros?|brasserie|bakery|bakeries|cafeteria|canteen|eatery|"
                       r"pizzeria|tavern|taproom|brewpub|food court|nightclub|"
                       r"night club|beer garden|biergarten|ice cream parlou?r)\b")
STREET_WORDS = re.compile(r"\b(streets?|roads?|highway|plaza|square|intersection|"
                          r"crossing|crosswalk|sidewalk|avenue|boulevard|alley|traffic|"
                          r"toll|driveway)\b")
# A picture taken from a plane by its description: above the clouds (its title or
# scene), or an aerial view (its title or scene) from a plane (also its tags) -- it gets
# no named place (trips.flying needs the GPS altitude, which a phone does not always
# write).
FLIGHT_WORDS = re.compile(r"\b((?:above|over) (?:the |a )?(?:clouds|cloud sea|sea of clouds)|"
                          r"cloud layer|cloud sea|sea of clouds)\b")
AERIAL_WORDS = re.compile(r"\baerial\b")
PLANE_WORDS = re.compile(r"\b(airplanes?|aeroplanes?|aircraft|planes?|flight|in-flight)\b")
# An "attraction" that is a tour operator's stand or kiosk, by its name (a street by a
# canal is not the boat tour that starts there).
OPERATOR = re.compile(r"\b(tours?|charters?|cruises|rederij|rentals?|hire|excursions|"
                      r"sightseeing)\b", re.IGNORECASE)
# The kinds of place the scene or the title of a description says (Facts.hints): the
# theatre, not the church next to it, for a picture of a theatre's inside.
KIND_HINTS = tuple((re.compile(r"\b(%s)\b" % words), frozenset(kinds)) for words, kinds in (
    (r"theat(?:er|re)s?|auditorium|playhouse|opera house", ("theatre", "cinema")),
    (r"cinemas?|movie theat(?:er|re)", ("cinema", "theatre")),
    (r"museums?|exhibits?|exhibition", ("museum", "gallery", "exhibition_centre")),
    (r"galler(?:y|ies)", ("gallery", "museum", "arts_centre")),
    (r"aquariums?", ("aquarium",)),
    (r"zoo", ("zoo",)),
    (r"(?:train|railway|subway|metro|bus) station|station platform|train platform",
     ("station", "bus_station")),
    (r"church|cathedral|chapel|temple|mosque|synagogue|basilica|shrine",
     ("place_of_worship",)),
    (r"stadium|ballpark|arena", ("stadium", "sports_centre", "events_venue")),
    (r"library", ("library",)),
    (r"hotels?|motels?|hostel", ("hotel", "motel", "hostel", "resort")),
    (r"restaurants?|diner", ("restaurant", "fast_food", "food_court")),
    (r"caf\u00e9|cafe|coffee shop", ("cafe",)),
    (r"bar|pub|tavern|nightclub", ("bar", "pub", "nightclub", "biergarten", "wine_bar")),
    (r"brewery|taproom", ("brewery",)),
    (r"winery|vineyard", ("winery",)),
    (r"casino", ("casino",)),
    (r"(?:shopping )?mall", ("mall", "department_store")),
    (r"market", ("marketplace",)),
    (r"lighthouse", ("lighthouse",)),
    (r"castle|palace", ("castle",)),
    (r"conference|convention|expo", ("conference_centre", "exhibition_centre")),
    (r"planetarium", ("planetarium",)),
    (r"observation deck|observatory|lookout|aerial view", ("viewpoint", "observatory",
                                                           "tower")),
    (r"ferry", ("ferry_terminal",)),
    (r"marina", ("marina",)),
    (r"(?:ice|skating) rink", ("ice_rink",)),
))
# Of a name, the words that do not count when looking for it in a description, and
# the one-word names too common to be looked for ("Waterfront", "Central").
NAME_STOP = frozenset(("the", "of", "and", "at", "on", "in", "de", "la", "le", "les", "el",
                       "du", "des", "di", "da", "del", "der", "den", "het", "van", "by", "a",
                       "an", "ltd", "inc", "llc", "co"))
COMMON_NAMES = frozenset((
    "waterfront", "central", "centre", "center", "square", "market", "plaza", "park",
    "station", "museum", "beach", "harbour", "harbor", "pier", "garden", "gardens", "tower",
    "bridge", "terminal", "mall", "library", "theatre", "theater", "cinema", "hotel", "cafe",
    "restaurant", "bar", "pub", "inn", "church", "cathedral", "chapel", "temple", "castle",
    "palace", "stadium", "arena", "airport", "zoo", "aquarium", "gallery", "lookout",
    "viewpoint", "fountain", "monument", "memorial", "lighthouse", "falls", "lake", "river",
    "island", "point", "rock", "hill", "peak", "mountain", "valley", "canyon", "bay", "cove",
    "coast", "shore", "port", "marina", "dock", "wharf", "waterfall", "forest", "woods",
    "field", "meadow", "campus", "college", "university", "school", "hospital", "clinic",
    "office", "offices", "building", "house", "hall", "court", "place", "street", "avenue",
    "road", "lane", "drive", "boulevard", "downtown", "uptown", "midtown", "city", "town",
    "village", "north", "south", "east", "west", "old", "new", "grand", "great", "main",
    "public", "royal", "national", "international", "union", "pacific", "atlantic",
    "commons", "corner", "crossing", "junction", "landing", "gate", "gateway", "entrance",
    "dining", "kitchen", "lounge", "studio", "studios", "shop", "store", "outlet", "deli",
    "bakery", "pizza", "sushi", "grill", "tavern", "diner", "taproom", "brewery", "winery",
    "vista", "summit", "overlook", "plateau", "trail", "trailhead", "playground", "sunset",
    "sunrise", "paradise", "heritage", "liberty", "victory", "freedom", "harmony", "unity",
))
# Never a picture's named place: homes, and what names no place to be at.
EXCLUDED = frozenset((
    "residential", "apartment", "apartments", "house", "houses", "building", "yes",
    "place", "guest_house", "chalet", "dormitory", "detached", "terrace", "bungalow",
    "farm", "farmyard", "allotments", "military", "industrial", "construction",
    "brownfield", "landfill", "garages", "parking", "office",
))
# Scores (lower wins): a point of interest base + how far it is (0..1 of its
# reach); an area by its group, plus a little for a large one (its label's
# min_zoom: large areas are labelled from further out).
# Places to eat, shop and stay go by their distance alone (of VENUE_RANK_M, whatever
# their reach: the souvenir shop the picture is in before the snack bar next door).
POINT_BASE = {SIGHT: 1.0, VENUE: 1.4, AREA: 1.8, NATURE: 1.9}
POINT_SPAN = {SIGHT: 1.0, VENUE: 1.2, AREA: 1.0, NATURE: 1.0}
VENUE_RANK_M = 45.0
# A meal indoors (Facts.meal): a place to eat counts this much more (as specific as a
# sight: the restaurant, not the casino down the street), unless the position is in a
# destination (DISTINCT) or near one (DESTINATIONS: the aquarium's own restaurant).
MEAL_BONUS = 0.5
AREA_BASE = {SIGHT: 1.5, VENUE: 1.6, AREA: 1.75, NATURE: 2.6}
AREA_SIZE_STEP = 0.05
# Areas that are a destination of their own (inside one, it is the place, unless
# a sight or a venue is right there): as specific as a sight.
DISTINCT = frozenset(("zoo", "theme_park", "aquarium", "water_park", "beach", "garden",
                      "stadium", "museum", "castle", "golf_course", "cemetery", "marina",
                      "pier", "attraction", "camp_site", "university", "college", "school",
                      "hospital", "resort"))
# Points of interest that are a destination of their own: one counts a little more
# than another sight as far away, and a venue near one (DESTINATION_NEAR) a
# little less.
DESTINATIONS = frozenset(("zoo", "theme_park", "aquarium", "water_park", "stadium", "museum",
                          "castle"))
DESTINATION_BONUS = 0.1
DESTINATION_NEAR = 100.0
# Areas that are the place whatever is inside them: an airport's gates, shops and
# stations name no place a picture is taken at.
DOMINANT = frozenset(("aerodrome", "airport"))
# Inside a destination (DISTINCT), what else is there counts this much less, and
# a venue near one this much less again.
INSIDE_DISTINCT = 0.3
VENUE_IN_DESTINATION = 0.4
# Kinds that name small things as well as large ones: their reach shrinks with
# their label's min_zoom (how close the map must be to show it: a cathedral or a
# famous tower from zoom 13, a chapel or a ride from 16): min_zoom -> share.
SCALED = frozenset(("attraction", "theatre", "place_of_worship", "events_venue", "memorial",
                    "monument", "station", "museum"))
SCALED_REACH = {13: 1.0, 14: 0.85, 15: 0.7, 16: 0.55}
SCALED_LEAST = 0.4
# Prominence: a point counts this much less for each zoom past MINOR_ZOOM its label
# needs (a famous tower is labelled from zoom 13, a plaque from 17).
MINOR_ZOOM = 14
MINOR_STEP = 0.1

Place = collections.namedtuple("Place", "name kind metres inside score zoom at named "
                                        "hinted")
Place.__new__.__defaults__ = (99, None, False, False)
Place.__doc__ = """A named place found for a position.

name    its name, as the street maps have it
kind    the kind of the point of interest that carries the name ("museum")
metres  how far it is from the position (0 inside an area), whole metres
inside  True for an area the position is inside of
score   how well it fits (lower is better; see candidates)
zoom    the min_zoom of its label (how close the map must be to show it)
at      (lat, lon) of a point of interest (None for an area)
named   True when the picture's description names it (named_in)
hinted  True when its kind is one the description's scene or title says (KIND_HINTS)
"""

Facts = collections.namedtuple("Facts", "points shops eat meal street indoor approximate "
                                        "hints words flying")
Facts.__doc__ = """What a picture's description and position say fits it (picture_facts).

points       points of interest count (not for a video's track, nor a picture taken
             from a vehicle: areas only)
shops        shops count (not for a picture taken outdoors without food)
eat          places to eat and drink count (EAT: a picture of food or a meal, or
             one taken in such a place)
meal         a meal indoors: places to eat count as much as sights (MEAL_BONUS)
street       taken in a street or a square: only areas and landmarks
             count from further than NEAR_M
indoor       taken indoors (a fix there is often worse: INDOOR_ACCURACY_M; the point
             of an area counts within NEAR_M only)
approximate  an estimated place (or the owner's, with a radius): no place to eat,
             shop or stay, and only points of interest whose reach is the radius
hints        the kinds of place its scene or title says (KIND_HINTS; a frozenset)
words        its description's words (title, description, landmarks, tags, subjects,
             text; plain_words): a point of interest it names wins (named_in)
flying       taken from a plane, by its description (FLIGHT_WORDS; or an aerial view
             with a plane): no named place at all
"""
# A picture nothing is known of: everything counts.
ANY = Facts(True, True, True, False, False, False, False, frozenset(), "", False)
_FACT_LETTERS = "psemtia"


# ---------------------------------------------------------------------------
# Settings

def settings(config):
    """config.json "named_places" checked, with defaults: {"enabled", "max_estimate_km"}."""
    stored = config.get("named_places") if isinstance(config, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    result = dict(DEFAULTS)
    if isinstance(stored.get("enabled"), bool):
        result["enabled"] = stored["enabled"]
    for key, (low, high) in RANGES.items():
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and \
                math.isfinite(value) and low <= value <= high:
            result[key] = float(value)
    return result


# ---------------------------------------------------------------------------
# Vector tiles (MVT: protocol buffers): only what find() reads

class TileError(ValueError):
    """A tile that cannot be read."""


def _varint(data, pos):
    shift = 0
    value = 0
    while True:
        if pos >= len(data):
            raise TileError("cut short")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise TileError("a number too long")


def _fields(data):
    """(field number, wire type, value) of a protocol buffer message: a number, or bytes
    (a memoryview) for length-delimited and fixed-size values."""
    pos = 0
    end = len(data)
    while pos < end:
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            if pos + size > end:
                raise TileError("cut short")
            value = data[pos:pos + size]
            pos += size
        elif wire == 1:
            value = data[pos:pos + 8]
            pos += 8
        elif wire == 5:
            value = data[pos:pos + 4]
            pos += 4
        else:
            raise TileError("an unknown wire type %d" % wire)
        if pos > end:
            raise TileError("cut short")
        yield number, wire, value


def _value(data):
    for number, _wire, value in _fields(data):
        if number == 1:
            return bytes(value).decode("utf-8", "replace")
        if number == 2:
            return struct.unpack("<f", bytes(value))[0]
        if number == 3:
            return struct.unpack("<d", bytes(value))[0]
        if number in (4, 5):
            return value
        if number == 6:
            return (value >> 1) ^ -(value & 1)
        if number == 7:
            return bool(value)
    return None


def _packed(data):
    out = []
    pos = 0
    while pos < len(data):
        value, pos = _varint(data, pos)
        out.append(value)
    return out


def _geometry(commands):
    """The parts of a geometry ([[(x, y), ...]]: points, a line's or a ring's points)."""
    parts = []
    x = y = 0
    current = None
    k = 0
    while k < len(commands):
        command = commands[k]
        k += 1
        op, count = command & 7, command >> 3
        if op in (1, 2):
            for _ in range(count):
                if k + 1 >= len(commands):
                    raise TileError("a geometry cut short")
                dx, dy = commands[k], commands[k + 1]
                k += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                if op == 1:
                    current = [(x, y)]
                    parts.append(current)
                elif current is not None:
                    current.append((x, y))
        elif op != 7:
            raise TileError("an unknown geometry command %d" % op)
    return parts


Feature = collections.namedtuple("Feature", "id type props parts")


def decode_tile(data, layers=("pois", "landuse"), keep=None):
    """{layer name: (extent, [Feature(id, type (1 point, 2 line, 3 polygon), props,
    parts)])} of a vector tile (gzip or not), of the layers named. keep(layer, props,
    type): only the features it says yes to (default all). Raises TileError."""
    try:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
    except (OSError, EOFError, ValueError) as error:
        raise TileError("a damaged tile (%s)" % error)
    data = memoryview(data)
    found = {}
    try:
        for number, wire, value in _fields(data):
            if number != 3 or wire != 2:
                continue
            name = ""
            keys = []
            values = []
            features = []
            extent = 4096
            for lnumber, _lwire, lvalue in _fields(value):
                if lnumber == 1:
                    name = bytes(lvalue).decode("utf-8", "replace")
                elif lnumber == 2:
                    features.append(lvalue)
                elif lnumber == 3:
                    keys.append(bytes(lvalue).decode("utf-8", "replace"))
                elif lnumber == 4:
                    values.append(_value(lvalue))
                elif lnumber == 5:
                    extent = lvalue
            if name not in layers:
                continue
            listed = []
            for raw in features:
                fid = None
                tags = []
                kind = 0
                commands = []
                for fnumber, _fwire, fvalue in _fields(raw):
                    if fnumber == 1:
                        fid = fvalue
                    elif fnumber == 2:
                        tags = _packed(fvalue)
                    elif fnumber == 3:
                        kind = fvalue
                    elif fnumber == 4:
                        commands = fvalue
                props = {}
                for k in range(0, len(tags) - 1, 2):
                    if tags[k] < len(keys) and tags[k + 1] < len(values):
                        props[keys[tags[k]]] = values[tags[k + 1]]
                if keep is not None and not keep(name, props, kind):
                    continue
                listed.append(Feature(fid, kind, props, _geometry(_packed(commands))))
            found[name] = (extent or 4096, listed)
    except (IndexError, struct.error) as error:
        raise TileError("a damaged tile (%s)" % error)
    return found


# ---------------------------------------------------------------------------
# Finding the named place of a position

def _keep(layer, props, kind):
    """The features find() reads: named points of interest, and outlined areas."""
    if layer == "pois":
        return kind == 1 and isinstance(props.get("name"), str) and bool(props["name"].strip())
    return kind == 3


def _latin(text):
    """True when text is written in Latin letters (and digits, punctuation)."""
    return all(ord(char) < 0x250 or not char.isalpha() for char in text)


def first_name(text):
    """The first of the names in a name, trimmed: OpenStreetMap keeps the several names of
    one place in one, separated by ";" ("Harbour Hall;Hall of the Harbour"); ""
    when there is none."""
    if not isinstance(text, str):
        return ""
    for part in text.split(";"):
        part = part.strip()
        if part:
            return part
    return ""


def all_names(props):
    """Every name of a point of interest (its own and its English one, each of their
    ";"-separated names, trimmed), display_name's first, each once."""
    names = [display_name(props)] if display_name(props) else []
    for key in ("name", "name:en"):
        value = props.get(key)
        for part in value.split(";") if isinstance(value, str) else ():
            part = part.strip()
            if part and part not in names:
                names.append(part)
    return names


def display_name(props):
    """The name a point of interest is shown by: its own, or its English name when its own
    is not in Latin letters and it has one -- the first of each (first_name)."""
    name = first_name(props.get("name"))
    english = first_name(props.get("name:en"))
    if name and not _latin(name) and english:
        return english
    return name


class Reader:
    """The tiles of a street maps archive (street_maps.Archive), decoded once (the
    last TILE_CACHE kept)."""

    def __init__(self, archive):
        self.archive = archive
        self.zoom = max(0, min(ZOOM, archive.header["max_zoom"]))
        self.cache = collections.OrderedDict()
        self.read = 0
        self.failed = 0

    def tile(self, z, x, y):
        """decode_tile's layers of a tile ({} when there is none, or it cannot be read)."""
        key = (z, x, y)
        found = self.cache.get(key)
        if found is not None:
            self.cache.move_to_end(key)
            return found
        found = {}
        try:
            data = self.archive.get(z, x, y)
            if data:
                self.read += 1
                found = decode_tile(data, keep=_keep)
        except (TileError, street_maps.MapsError, OSError):
            self.failed += 1
            found = {}
        self.cache[key] = found
        while len(self.cache) > TILE_CACHE:
            self.cache.popitem(last=False)
        return found


def _tile_xy(lat, lon, z):
    """(x, y) of a position in tiles of zoom z, with fractions (Web Mercator)."""
    n = float(1 << z)
    lat = max(-street_maps.MAX_LAT, min(street_maps.MAX_LAT, lat))
    r = math.radians(lat)
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)


def _metres_per_unit(lat, z, extent):
    """Metres of one unit of a tile's grid (zoom z, extent units across) at a latitude."""
    return 2 * math.pi * EARTH_M * math.cos(math.radians(lat)) / ((1 << z) * float(extent))


def _inside(px, py, rings):
    """True when (px, py) is inside the polygon of rings (even-odd: holes are out)."""
    inside = False
    for ring in rings:
        count = len(ring)
        for k in range(count):
            x1, y1 = ring[k]
            x2, y2 = ring[(k + 1) % count]
            if (y1 > py) != (y2 > py):
                if px < x1 + (py - y1) * (x2 - x1) / float(y2 - y1):
                    inside = not inside
    return inside


def _edge_distance(px, py, rings, extent):
    """The distance (grid units) from (px, py) to the nearest edge of the polygon,
    leaving out the edges the tile's border cut it along."""
    best = None
    for ring in rings:
        count = len(ring)
        for k in range(count):
            x1, y1 = ring[k]
            x2, y2 = ring[(k + 1) % count]
            if (x1 <= 0 and x2 <= 0) or (x1 >= extent and x2 >= extent) or \
                    (y1 <= 0 and y2 <= 0) or (y1 >= extent and y2 >= extent):
                continue        # (the tile's edge, not the area's)
            dx, dy = x2 - x1, y2 - y1
            length = dx * dx + dy * dy
            share = 0.0 if not length else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy)
                                                         / float(length)))
            distance = math.hypot(px - (x1 + share * dx), py - (y1 + share * dy))
            if best is None or distance < best:
                best = distance
    return float("inf") if best is None else best


def _kind_of(kind):
    """(group, reach) of a kind, or None when it names no place (EXCLUDED, unknown)."""
    if not isinstance(kind, str) or kind in EXCLUDED:
        return None
    return KINDS.get(kind)


def _min_zoom(props, default):
    value = props.get("min_zoom")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return default


def _tiles_around(fx, fy, z, reach_tiles):
    """[(z, x, y)] of the tiles of zoom z within reach_tiles (tiles) of (fx, fy), and the
    tiles at the position of the zooms above down to LABEL_ZOOM_MIN, with the eight
    around it at LABEL_ZOOM_MIN + 2."""
    n = 1 << z
    tx, ty = int(math.floor(fx)), int(math.floor(fy))
    columns = range(int(math.floor(fx - reach_tiles)), int(math.floor(fx + reach_tiles)) + 1)
    rows = range(max(0, int(math.floor(fy - reach_tiles))),
                 min(n - 1, int(math.floor(fy + reach_tiles))) + 1)
    tiles = [(z, x % n, y) for x in columns for y in rows]
    for zoom in range(z - 1, LABEL_ZOOM_MIN - 1, -1):
        shift = z - zoom
        tiles.append((zoom, tx >> shift, ty >> shift))
    wide = LABEL_ZOOM_MIN + 2
    if wide < z:
        shift = z - wide
        size = 1 << wide
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (dx or dy) and 0 <= (ty >> shift) + dy < size:
                    tiles.append((wide, ((tx >> shift) + dx) % size, (ty >> shift) + dy))
    return tiles


def _points(tiles, reader):
    """{key (the id, else name and kind): (zoom, Feature, lat, lon)} of the named points of
    interest of tiles (each once: from the most detailed zoom that has it)."""
    pois = {}
    for zoom, x, y in tiles:
        layer = reader.tile(zoom, x, y).get("pois")
        if not layer:
            continue
        extent, features = layer
        size = float(1 << zoom)
        for feature in features:
            if not feature.parts or not feature.parts[0]:
                continue
            key = feature.id if feature.id is not None else (
                feature.props.get("name"), feature.props.get("kind"))
            if key in pois and pois[key][0] >= zoom:
                continue
            gx, gy = feature.parts[0][0]
            plon = (x + gx / float(extent)) / size * 360.0 - 180.0
            plat = math.degrees(math.atan(math.sinh(
                math.pi * (1 - 2 * (y + gy / float(extent)) / size))))
            pois[key] = (zoom, feature, plat, plon)
    return pois


def _areas(lat, lon, accuracy_m, reader, pois):
    """([Place] of the outlined areas the position is inside of (deep enough), the ids
    of every outlined area of its tile)."""
    z = reader.zoom
    n = 1 << z
    fx, fy = _tile_xy(lat, lon, z)
    tx, ty = int(math.floor(fx)), int(math.floor(fy))
    found = []
    outlined = set()
    layer = reader.tile(z, tx % n, ty).get("landuse")
    if not layer:
        return found, outlined
    extent, features = layer
    px, py = (fx - tx) * extent, (fy - ty) * extent
    metres = _metres_per_unit(lat, z, extent)
    for feature in features:
        if feature.id is None:
            continue
        outlined.add(feature.id)
        if feature.props.get("kind") in EXCLUDED or not _inside(px, py, feature.parts):
            continue
        label = pois.get(feature.id)
        if label is None:
            continue            # (an area without a name)
        props = label[1].props
        kind = props.get("kind") if isinstance(props.get("kind"), str) else ""
        if kind in EXCLUDED:
            continue
        if _kind_of(kind) is None:
            kind = feature.props.get("kind")
        fits = _kind_of(kind)
        name = display_name(props)
        if fits is None or not name:
            continue
        if _edge_distance(px, py, feature.parts, extent) * metres < accuracy_m * 0.5:
            continue            # (the position may well be outside it)
        level = _min_zoom(props, 15)
        larger = max(0.0, 15 - max(5, min(15, level)))
        score = (AREA_BASE[SIGHT] if kind in DISTINCT else AREA_BASE[fits[0]]) + \
            larger * AREA_SIZE_STEP
        found.append(Place(name, kind, 0, True, score, level))
    return found, outlined


def point_class(kind):
    """The class of a kind of point of interest that choose() tells apart: "eat",
    "stay" or "shop" for a VENUE, else its group (SIGHT, AREA, NATURE); None when it
    names no place."""
    fits = _kind_of(kind)
    if fits is None:
        return None
    if fits[0] != VENUE:
        return fits[0]
    return "eat" if kind in EAT else "stay" if kind in LODGING else "shop"


def plain_words(text):
    """text as plain words for named_in: lowercased, accents and apostrophes gone ("Caf\u00e9
    Nord's" -> "cafe nords"), anything but letters and digits a space."""
    text = unicodedata.normalize("NFKD", text if isinstance(text, str) else "")
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    text = re.sub("['\u2019`]", "", text)
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _same_word(name_word, word):
    """True when a word of a name is a word of a text: the same, or (both of five letters
    or more) the same start and nearly the same length ("theatre", "theater";
    "centraal", "central")."""
    if name_word == word:
        return True
    return (len(name_word) >= 5 and len(word) >= 5 and name_word[:5] == word[:5]
            and abs(len(name_word) - len(word)) <= 2)


class _Said:
    """A description's words (plain_words), ready for named_in."""

    def __init__(self, words):
        self.text = [word for word in (words or "").split() if word not in NAME_STOP]
        self.exact = set(self.text)
        self.starts = set(word[:5] for word in self.text if len(word) >= 5)

    def _has(self, word):
        return word in self.exact or (len(word) >= 5 and word[:5] in self.starts)

    def names(self, name):
        """True when the words name a place (see named_in)."""
        if not self.text or not name:
            return False
        variants = [name] + [part for part in re.split(r"\s*(?::|\s-\s|\(|,)\s*", name)[:1]
                             if part and part != name]
        possessive = re.match("\\s*([^\\s'\u2019]{3,})['\u2019]s\\b", name)
        if possessive:
            variants.append(possessive.group(1) + "s")     # ("Gull's Gifts")
        for variant in variants:
            wanted = [word for word in plain_words(variant).split() if word not in NAME_STOP]
            if not wanted or (len(wanted) == 1 and (len(wanted[0]) < 4
                                                    or wanted[0] in COMMON_NAMES)):
                continue
            if not all(self._has(word) for word in wanted):
                continue
            size = len(wanted)
            text = self.text
            for start in range(len(text) - size + 1):
                if all(_same_word(wanted[k], text[start + k]) for k in range(size)):
                    return True
        return False


def named_in(name, words):
    """True when a description's words (plain_words) name a place: its name's words (but
    NAME_STOP), one after the other -- or those of the part before a colon, a dash or a
    bracket ("Westpoint: Museum of Toys"), or the first word of a name that starts
    with one's ("Gull's Gift Shop"). A name of one word must have four letters or
    more and not be a common word (COMMON_NAMES)."""
    return _Said(words).names(name)


def _metres_between(first, second):
    lat = math.radians((first[0] + second[0]) / 2.0)
    return math.hypot(math.radians(second[1] - first[1]) * math.cos(lat) * EARTH_M,
                      math.radians(second[0] - first[0]) * EARTH_M)


def candidates(lat, lon, accuracy_m, reader, facts=ANY):
    """[Place] of every named place that fits a position (see the module notes), best
    first, each name once. facts (a Facts; default ANY): what fits the picture -- its
    points of interest at all (not for a video's track, or a picture taken from a
    vehicle: a drive passes them), places to eat, shop and stay, a meal, a street
    scene, indoors, an approximate position, the kinds its scene says (hinted, a little
    better) and the names its description says (named: much better, and whatever the
    picture's other facts -- but never for an approximate position)."""
    z = reader.zoom
    fx, fy = _tile_xy(lat, lon, z)
    accuracy_m = max(0.0, float(accuracy_m or 0.0))
    reach_tiles = (400.0 + min(accuracy_m, 400.0)) / _metres_per_unit(lat, z, 1)
    pois = _points(_tiles_around(fx, fy, z, reach_tiles), reader)
    areas, outlined = _areas(lat, lon, accuracy_m, reader, pois)
    dominant = [place for place in areas if place.kind in DOMINANT]
    if dominant:
        areas = dominant
    in_distinct = any(place.kind in DISTINCT for place in areas)
    said = _Said(facts.words) if facts.words and not facts.approximate else None
    near = []
    if facts.points and accuracy_m <= MAX_ACCURACY_M and not dominant:
        cos_lat = math.cos(math.radians(lat))
        for _zoom, feature, plat, plon in pois.values():
            props = feature.props
            kind = props.get("kind")
            fits = _kind_of(kind)
            if fits is None:
                continue
            group, reach = fits
            if group in (AREA, NATURE) and feature.id is not None and feature.id in outlined:
                continue        # (an outlined area names only what is inside it)
            name = display_name(props)
            if not name or (kind == "attraction" and OPERATOR.search(name)):
                continue
            named = said is not None and any(said.names(each) for each in all_names(props))
            if group == VENUE and (facts.approximate or not named and (
                    not facts.eat if kind in EAT
                    else not facts.shops and kind not in LODGING)):
                continue
            level = _min_zoom(props, 99)
            if kind in SCALED:
                scaled = int(_min_zoom(props, 16))
                reach *= 1.0 if scaled <= 13 else SCALED_REACH.get(scaled, SCALED_LEAST)
            if facts.approximate and reach < accuracy_m:
                continue        # (an estimate of 100 m is not at a fountain of 60)
            allowed = reach + min(accuracy_m, reach) * 0.5
            if not named and ((facts.street and level > LANDMARK_ZOOM)
                              or (facts.indoor and group in (AREA, NATURE))):
                allowed = min(allowed, NEAR_M)
            distance = math.hypot(math.radians(plon - lon) * cos_lat * EARTH_M,
                                  math.radians(plat - lat) * EARTH_M)
            if distance > allowed:
                continue
            minor = max(0.0, _min_zoom(props, MINOR_ZOOM) - MINOR_ZOOM) * MINOR_STEP
            if group == VENUE:
                share = min(1.0, distance / VENUE_RANK_M)
            else:
                share = distance / allowed
            score = POINT_BASE[group] + POINT_SPAN[group] * share + minor
            if kind in DESTINATIONS:
                score -= DESTINATION_BONUS
            if in_distinct and kind not in DISTINCT:
                score += INSIDE_DISTINCT
            hinted = kind in facts.hints
            if hinted and group != VENUE:
                score -= HINT_BONUS     # (a venue's: below)
            if named:
                score -= NAMED_BONUS
            near.append(Place(name, kind, int(round(distance)), False, score, level,
                              (plat, plon), named, hinted))
    if in_distinct or any(place.kind in DESTINATIONS and place.metres <= DESTINATION_NEAR
                          for place in near):
        # (A cafe of the zoo, the restaurant of the aquarium: the zoo, the aquarium.)
        near = [place._replace(score=place.score + VENUE_IN_DESTINATION)
                if KINDS[place.kind][0] == VENUE and not place.named else place
                for place in near]
    if not in_distinct and not any(place.kind in DESTINATIONS for place in near):
        # (A meal indoors, a restaurant's table: the restaurant, before the casino down
        # the street -- but in or by an aquarium, the aquarium.)
        near = [place._replace(score=place.score - (
            MEAL_BONUS if facts.meal and place.kind in EAT else 0.0) - (
            HINT_BONUS if place.hinted and KINDS[place.kind][0] == VENUE else 0.0))
            for place in near]
    found = sorted(areas + near, key=lambda place: (place.score, place.metres, place.name))
    seen = set()
    result = []
    for place in found:
        if place.name.lower() not in seen:
            seen.add(place.name.lower())
            result.append(place)
    return result


def choose(found, accuracy_m):
    """The named place of candidates() (best first), or None: the best, unless it is a
    point of interest the position cannot tell from another of its class (point_class:
    their distances differ by less than accuracy_m, at least MARGIN_M; the other no
    more than RIVAL_ZOOMS less prominent; of a kind the scene says when the best is;
    not a sight at the same spot, SAME_SPOT_M) -- then the best area the position is
    inside of, or None. One the description names (named_in), a destination
    (DESTINATIONS), or anything inside one (DISTINCT), is never in doubt."""
    if not found:
        return None
    best = found[0]
    if best.inside or best.named or best.kind in DESTINATIONS or any(
            place.inside and place.kind in DISTINCT for place in found):
        return best
    margin = max(float(accuracy_m or 0.0), MARGIN_M)
    group = point_class(best.kind)
    for other in found[1:]:
        if (other.inside or point_class(other.kind) != group
                or other.zoom > best.zoom + RIVAL_ZOOMS
                or abs(other.metres - best.metres) >= margin
                or (best.hinted and not other.hinted)):
            continue
        if (group == SIGHT and best.at is not None and other.at is not None
                and _metres_between(best.at, other.at) <= SAME_SPOT_M):
            continue        # (one tower, two names)
        return next((place for place in found if place.inside), None)
    return best


def find(lat, lon, accuracy_m, reader, facts=ANY):
    """The named place of a position (a Place), or None (see candidates and choose)."""
    if not world_map.valid_position(lat, lon):
        return None
    return choose(candidates(float(lat), float(lon), accuracy_m, reader, facts), accuracy_m)


# ---------------------------------------------------------------------------
# The pictures' positions

Position = collections.namedtuple("Position", "image_id lat lon accuracy_m source facts")
Position.__doc__ = """A picture's position to look up (see positions).

source    "gps", "track", "estimate" or "owner"
facts     what fits the picture (a Facts: its description, how it moved, how
          approximate the position is)
"""

_IMAGES_SQL = """
    SELECT id, content_hash, media, lat, lon, exif, info, described, camera, place_cc
    FROM images WHERE missing_since IS NULL AND lat IS NOT NULL AND library_id IN (%s)"""
_ESTIMATES_SQL = """
    SELECT e.image_id, e.lat, e.lon, e.radius_km, e.source, i.info, i.described
    FROM place_estimates AS e JOIN images AS i ON i.id = e.image_id
    WHERE i.missing_since IS NULL AND i.lat IS NULL AND e.lat IS NOT NULL
        AND i.library_id IN (%s)"""


def _library_ids(config):
    return [library.get("id") for library in (config or {}).get("libraries", [])
            if isinstance(library, dict) and library.get("id")]


def _words(info, keys):
    """The text of some fields of a description (strings and lists of them), lowercased."""
    out = []
    for key in keys:
        value = info.get(key)
        values = value if isinstance(value, list) else [value]
        out.extend(item for item in values if isinstance(item, str))
    return " ".join(out).lower()


def picture_facts(info_text, described=True):
    """The Facts of a picture by its description (see Facts; never approximate): taken
    from a vehicle, areas only; places to eat for a picture of food or a meal, or of
    such a place (EAT_WORDS in its scene, title or tags); shops not outdoors without
    food; a street scene (STREET_WORDS in its scene) outdoors; the kinds of place its
    scene and title say (KIND_HINTS); its words (for the names it says); taken from a
    plane (FLIGHT_WORDS, AERIAL_WORDS with PLANE_WORDS). An undescribed picture (or one
    whose description cannot be read): ANY."""
    if not described or not info_text:
        return ANY
    try:
        info = json.loads(info_text, parse_constant=lambda _name: None)
    except (TypeError, ValueError):
        return ANY
    if not isinstance(info, dict):
        return ANY
    setting = info.get("setting")
    food = bool(info.get("food")) or info.get("event") == "meal"
    scene = _words(info, ("scene",))
    seen = " ".join((scene, _words(info, ("title",))))
    eating = bool(EAT_WORDS.search(" ".join((seen, _words(info, ("tags",))))))
    outdoor = setting == "outdoor"
    hints = frozenset(kind for pattern, kinds in KIND_HINTS if pattern.search(seen)
                      for kind in kinds)
    flying = bool(FLIGHT_WORDS.search(seen)) or (bool(AERIAL_WORDS.search(seen)) and bool(
        PLANE_WORDS.search(" ".join((seen, _words(info, ("tags",)))))))
    words = plain_words(" ".join((_words(info, ("title", "description", "landmarks", "tags",
                                                 "subjects", "location_guess")),
                                  _words(info, ("text",))[:WORDS_TEXT])))
    return Facts(points=setting != "vehicle", shops=not outdoor or food, eat=food or eating,
                 meal=food and setting == "indoor",
                 street=outdoor and bool(STREET_WORDS.search(scene)),
                 indoor=setting == "indoor", approximate=False, hints=hints, words=words,
                 flying=flying)


def _gps(exif_text):
    """The GPS dict of an images.exif text, or None."""
    try:
        exif = json.loads(exif_text or "{}", parse_constant=lambda _name: None)
    except ValueError:
        return None
    gps = exif.get("GPS") if isinstance(exif, dict) else None
    return gps if isinstance(gps, dict) else None


def _gps_accuracy(gps, facts):
    """How far off a GPS position may be (metres): what its file says, else the default
    (INDOOR_ACCURACY_M for a picture taken indoors)."""
    import gps_check
    metres = gps_check.accuracy_km(gps) * 1000.0 if gps is not None else 0.0
    if metres > 0:
        return metres
    return INDOOR_ACCURACY_M if facts.indoor else DEFAULT_ACCURACY_M


def motion(gps, camera=""):
    """"flying" (trips.flying, or faster than FLYING_KMH), "moving" (MOVING_KMH or
    more) or "" of a picture by its EXIF GPS dict (or None)."""
    import gps_check
    import trips
    if gps is None:
        return ""
    speed = gps_check.speed_kmh(gps, camera or "")
    if trips.flying(gps) or (speed is not None and speed > FLYING_KMH):
        return "flying"
    return "moving" if speed is not None and speed >= MOVING_KMH else ""


def _across_border(row):
    """True when a located picture's file names a country (place_estimates.gps_file_columns)
    other than the one its position is in by the place names (its place_cc): the names
    it shows are the file's, so a named place of the street maps there might be across
    the border."""
    import place_estimates
    named = place_estimates.gps_file_columns(row)
    return bool(named and named["place_cc"] and row["place_cc"]
                and named["place_cc"] != row["place_cc"])


def positions(conn, config, library_ids=None, index=None):
    """[Position] of the present pictures of the configured libraries with a place to look
    up (see the module notes): their own position, a video's from its GPS track, or
    their estimated place (of a radius of at most max_estimate_km) or the owner's.
    A picture taken from a plane (motion) has none; one moving gets areas only.
    index: a places.PlaceIndex (to know the owner's town picks; default: shared())."""
    import place_estimates
    import tracks
    library_ids = list(library_ids if library_ids is not None else _library_ids(config))
    if not library_ids:
        return []
    marks = ",".join("?" * len(library_ids))
    rows = conn.execute(_IMAGES_SQL % marks, library_ids).fetchall()
    found = tracks.for_rows(conn, rows, config)
    result = []
    for row in rows:
        if not world_map.valid_position(row["lat"], row["lon"]):
            continue
        facts = picture_facts(row["info"], row["described"])
        if facts.flying:
            continue            # (what is under a plane is not where it was taken)
        track = found.get(row["id"])
        if tracks.is_position(track, row["lat"], row["lon"]):
            result.append(Position(row["id"], float(row["lat"]), float(row["lon"]),
                                   (track.extent_km or 0.0) * 1000.0, "track",
                                   facts._replace(points=False)))
            continue
        gps = _gps(row["exif"])
        moved = motion(gps, row["camera"])
        if moved == "flying":
            continue            # (what is under a plane is not where it was taken)
        if _across_border(row):
            continue            # (its file names another country: see place_estimates)
        if moved:
            facts = facts._replace(points=False)
        result.append(Position(row["id"], float(row["lat"]), float(row["lon"]),
                               _gps_accuracy(gps, facts), "gps", facts))
    if not place_estimates._have_tables(conn):
        return result
    limit = settings(config)["max_estimate_km"]
    owner_index = None
    for row in conn.execute(_ESTIMATES_SQL % marks, library_ids):
        if not world_map.valid_position(row["lat"], row["lon"]):
            continue
        radius = row["radius_km"]
        lat, lon = float(row["lat"]), float(row["lon"])
        facts = picture_facts(row["info"], row["described"])
        if facts.flying:
            continue
        approximate = facts._replace(shops=False, eat=False, meal=False, approximate=True)
        if row["source"] == "owner":
            if owner_index is None:
                if index is None:
                    import places
                    index = places.shared()
                owner_index = index if index is not None else False
            if place_estimates.town_pick(lat, lon, owner_index or None):
                continue
            if isinstance(radius, (int, float)) and radius > 0:
                # (An estimate the owner accepted: as approximate as it was.)
                result.append(Position(row["image_id"], lat, lon, radius * 1000.0, "owner",
                                       approximate))
            else:
                result.append(Position(row["image_id"], lat, lon, DEFAULT_ACCURACY_M, "owner",
                                       facts))
            continue
        if not isinstance(radius, (int, float)) or radius > limit:
            continue
        result.append(Position(row["image_id"], lat, lon, max(radius * 1000.0, 0.0),
                               "estimate", approximate))
    return result


# ---------------------------------------------------------------------------
# The database

TABLE = "named_places"
_TABLE = """CREATE TABLE IF NOT EXISTS named_places (
    image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    metres INTEGER NOT NULL DEFAULT 0,
    inside INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    accuracy_m REAL,
    facts TEXT NOT NULL DEFAULT '',
    maps TEXT NOT NULL DEFAULT '',
    updated REAL
)"""
# The search record keys of a named place.
RECORD_KEYS = ("np", "nk", "nd", "ni", "na")


def have_table(conn):
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (TABLE,)).fetchone()[0] == 1


def ensure_table(conn):
    """Create named_places when it is not there yet (commits what conn had pending first
    then; writes nothing when it is)."""
    if have_table(conn):
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute(_TABLE)
    conn.commit()


def maps_key():
    """What the named places were found with: RULES_VERSION and the street maps file
    (its size and time); "" without street maps."""
    try:
        stat = os.stat(street_maps.archive_path())
    except OSError:
        return ""
    return "%d:%d:%d" % (RULES_VERSION, stat.st_size, int(stat.st_mtime))


def _facts_text(position):
    """The Facts of a position as letters (_FACT_LETTERS: one for each that is true), and
    a checksum of its hints and words ("psi:1a2b3c4d") when it has any."""
    facts = position.facts
    letters = "".join(letter for letter, value in zip(_FACT_LETTERS, facts) if value)
    if not facts.hints and not facts.words:
        return letters
    said = "|".join(sorted(facts.hints)) + "|" + facts.words
    return "%s:%08x" % (letters, zlib.crc32(said.encode("utf-8")) & 0xffffffff)


def _same_spot(row, position):
    return (row["source"] == position.source and row["lat"] is not None
            and abs(row["lat"] - position.lat) <= 1e-7 and abs(row["lon"] - position.lon) <= 1e-7
            and abs((row["accuracy_m"] or 0.0) - position.accuracy_m) < 0.5
            and row["facts"] == _facts_text(position))


def _shown(row):
    """What a row shows (name, kind, metres, inside), or None for no named place."""
    if row is None or not row["name"]:
        return None
    return (row["name"], row["kind"], row["metres"], row["inside"])


def stored(conn):
    """{image id: the named_places row (a dict)} ({} before there is a table)."""
    if not have_table(conn):
        return {}
    return {row["image_id"]: dict(row) for row in conn.execute("SELECT * FROM named_places")}


def shown(config):
    """True when the records show named places: they are on (settings) and there are
    street maps (maps_key). Off, or without street maps, the rows still there are not
    shown (the next update forgets them)."""
    return bool(settings(config or {})["enabled"]) and bool(maps_key())


def shown_key(config):
    """What the records' named places depend on beyond the rows (search_index's ETags):
    maps_key() while they are shown, else ""."""
    return maps_key() if settings(config or {})["enabled"] else ""


def for_libraries(conn, library_ids, config=None):
    """{image id: the named_places row (a dict)} of the present pictures of some libraries
    that have a named place ({} when they are not shown: config's settings, see
    shown)."""
    library_ids = list(library_ids)
    if not library_ids or not have_table(conn) or not shown(config):
        return {}
    return {row["image_id"]: dict(row) for row in conn.execute(
        "SELECT n.* FROM named_places AS n JOIN images AS i ON i.id = n.image_id "
        "WHERE i.missing_since IS NULL AND n.name <> '' AND i.library_id IN (%s)"
        % ",".join("?" * len(library_ids)), library_ids)}


def for_picture(conn, image_id, config=None):
    """The named_places row (a dict) of a picture with a named place, or None (also when
    they are not shown: config's settings, see shown)."""
    if not have_table(conn) or not shown(config):
        return None
    row = conn.execute("SELECT * FROM named_places WHERE image_id = ? AND name <> ''",
                       (image_id,)).fetchone()
    return dict(row) if row is not None else None


def applies(named, lat, lon, sources):
    """True when a named_places row (or None) was found for the position lat, lon of one of
    sources (the position the picture's record shows: its own, "gps" and "track", or its
    estimate's or the owner's)."""
    return (named is not None and bool(named.get("name")) and named.get("source") in sources
            and named.get("lat") is not None and lat is not None and lon is not None
            and abs(named["lat"] - lat) <= 1e-7 and abs(named["lon"] - lon) <= 1e-7)


def approximate(named):
    """True when a named_places row was found for an approximate position: an estimated
    place, or the owner's with a radius (Facts.approximate, its facts letter "a")."""
    letters = str(named.get("facts") or "").split(":", 1)[0]
    return named.get("source") == "estimate" or "a" in letters


def record_keys(named, for_export=False):
    """The search record keys of a named_places row: np (the name), nk (its kind), and
    ni 1 (inside an area) or nd (how far, in metres; for_export: rounded to
    EXPORT_METRES, at least that). na 1 says the position is approximate: inside an
    area, probably; else nd is left out ("nearby") -- also when the position may be off
    by more than the distance."""
    # (first_name: a row found before RULES_VERSION 3 may hold several names.)
    keys = {"np": first_name(named["name"]), "nk": named["kind"] or ""}
    rough = approximate(named)
    metres = int(named.get("metres") or 0)
    if named.get("inside"):
        keys["ni"] = 1
    elif not rough and metres >= float(named.get("accuracy_m") or 0.0):
        keys["nd"] = max(EXPORT_METRES, int(round(metres / float(EXPORT_METRES))) *
                         EXPORT_METRES) if for_export else metres
    else:
        rough = True
    if rough:
        keys["na"] = 1
    if not keys["nk"]:
        del keys["nk"]
    return keys


def kind_words(kind):
    """A kind as words: "theme park", "airport" for an aerodrome."""
    if kind in ("aerodrome", "airport"):
        return "airport"
    return str(kind or "").replace("_", " ")


def _touch(conn, ids, now):
    ids = list(ids)
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        conn.execute("UPDATE images SET updated = ? WHERE id IN (%s)"
                     % ",".join("?" * len(part)), [now] + part)


def clear(conn, now=None):
    """Forget every named place (the street maps were removed, or named places turned off):
    the pictures that had one are touched. Returns how many had one."""
    if not have_table(conn):
        return 0
    if conn.in_transaction:
        conn.commit()
    now = time.time() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = [row[0] for row in conn.execute("SELECT image_id FROM named_places "
                                               "WHERE name <> ''")]
        conn.execute("DELETE FROM named_places")
        _touch(conn, ids, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(ids)


def _tile_order(position):
    """Pictures of one tile one after the other (the tiles are read once)."""
    x, y = _tile_xy(position.lat, position.lon, ZOOM)
    return (int(x) >> 2, int(y) >> 2, int(x), int(y), position.image_id)


def _empty_summary():
    return {"pictures": 0, "named": 0, "looked_up": 0, "changed": 0, "removed": 0,
            "stopped": False, "maps": False, "seconds": 0.0}


def update(conn, config, ctx=None, redo=False, ids=None, limit=None, now=None, changed=None,
           archive=None):
    """Look up the named places of the pictures whose position changed since (every one
    with redo, or when the street maps or the rules changed) and keep them.

    ids: only these pictures (the admin's choices; None: all); limit: look up at
    most that many, else none (they wait for the next job). Rows of pictures without
    a position any more are deleted. Without street maps (or with named places off)
    every row is deleted. A change touches the picture's images.updated; changed (a
    set, optional) gets those ids. Looks up without holding the write lock, and writes
    every COMMIT_ROWS rows or COMMIT_SECONDS in a short transaction; stops when ctx
    says so (what was done is kept). Returns {"pictures" (with a position), "named"
    (with a named place), "looked_up", "changed", "removed", "stopped", "maps" (there
    are street maps), "seconds"}.
    """
    started = time.perf_counter()
    summary = _empty_summary()
    now = time.time() if now is None else now
    key = maps_key()
    if not settings(config)["enabled"] or not key:
        removed = clear(conn, now)
        summary["removed"] = removed
        summary["maps"] = bool(key)
        if removed and ctx is not None:
            ctx.log("Named places: forgot %d (%s)." % (removed, "the street maps are gone"
                                                       if not key else "they are turned off"))
        return summary
    summary["maps"] = True
    ensure_table(conn)
    found = positions(conn, config)
    old = stored(conn)
    wanted = {position.image_id: position for position in found}
    todo = [position for position in found
            if (ids is None or position.image_id in ids)
            and (redo or position.image_id not in old or old[position.image_id]["maps"] != key
                 or not _same_spot(old[position.image_id], position))]
    gone = [image_id for image_id in old if image_id not in wanted
            and (ids is None or image_id in ids)]
    if limit is not None and len(todo) > limit:
        todo = []
    if conn.in_transaction:
        conn.commit()
    touched = []
    if gone:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for start in range(0, len(gone), 500):
                part = gone[start:start + 500]
                conn.execute("DELETE FROM named_places WHERE image_id IN (%s)"
                             % ",".join("?" * len(part)), part)
            shown = [image_id for image_id in gone if _shown(old[image_id]) is not None]
            _touch(conn, shown, now)
            touched.extend(shown)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    summary["removed"] = len(gone)
    if todo:
        if ctx is not None:
            ctx.stage("named places", total=len(todo))
        opened = archive is None
        if archive is None:
            archive = street_maps.open_local(street_maps.archive_path())
        reader = Reader(archive)

        def write(batch):
            """Keep a batch of (position, place) in one short transaction."""
            conn.execute("BEGIN IMMEDIATE")
            try:
                for position, place in batch:
                    values = (position.image_id, place.name if place else "",
                              place.kind if place else "", place.metres if place else 0,
                              1 if place and place.inside else 0, position.source,
                              position.lat, position.lon, round(position.accuracy_m, 1),
                              _facts_text(position), key, now)
                    conn.execute(
                        "INSERT OR REPLACE INTO named_places (image_id, name, kind, metres, "
                        "inside, source, lat, lon, accuracy_m, facts, maps, updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
                    before = old.get(position.image_id)
                    if _shown(before) != (None if place is None else (
                            place.name, place.kind, place.metres, 1 if place.inside else 0)) \
                            or (before is not None and place is not None
                                and before["source"] != position.source):
                        _touch(conn, [position.image_id], now)
                        touched.append(position.image_id)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            summary["looked_up"] += len(batch)

        try:
            todo.sort(key=_tile_order)
            batch = []
            last = time.monotonic()
            for position in todo:
                if ctx is not None and ctx.should_stop():
                    summary["stopped"] = True
                    break
                # (Read and decoded with no lock held: the slow part.)
                batch.append((position, find(position.lat, position.lon,
                                             position.accuracy_m, reader, position.facts)))
                if ctx is not None:
                    ctx.step()
                if len(batch) >= COMMIT_ROWS or time.monotonic() - last >= COMMIT_SECONDS:
                    write(batch)
                    batch = []
                    last = time.monotonic()
            if batch:
                write(batch)
        finally:
            if opened:
                archive.read.close()
    summary["pictures"] = len(found)
    summary["named"] = conn.execute("SELECT COUNT(*) FROM named_places WHERE name <> ''") \
        .fetchone()[0]
    summary["changed"] = len(touched)
    summary["seconds"] = round(time.perf_counter() - started, 3)
    if changed is not None:
        changed.update(touched)
    if ctx is not None and (summary["looked_up"] or summary["removed"]):
        ctx.log(summary_text(summary))
    return summary


def summary_text(summary):
    """One line about an update."""
    text = ("Named places: %s of %s picture(s) with a position have one" % (
        "{:,}".format(summary["named"]), "{:,}".format(summary["pictures"])))
    parts = []
    if summary["looked_up"]:
        parts.append("{:,} looked up".format(summary["looked_up"]))
    if summary["changed"]:
        parts.append("{:,} changed".format(summary["changed"]))
    if summary["removed"]:
        parts.append("{:,} without a place any more".format(summary["removed"]))
    if parts:
        text += " (" + ", ".join(parts) + ")"
    if summary["stopped"]:
        text += "; stopped (the rest are looked up next time)"
    return text + "."


def run_safely(conn, config, ctx=None):
    """update() for a job, after the GPS check and the local times: a busy database or a
    street maps file that cannot be read is only a warning in the log (and the answer
    None)."""
    try:
        return update(conn, config, ctx)
    except sqlite3.OperationalError as error:
        message = "Named places could not be looked up (%s); they are after the next scan." \
            % error
    except (street_maps.MapsError, OSError) as error:
        message = "Named places could not be looked up: the street maps cannot be read (%s)." \
            % common.redact(str(error))
    if ctx is not None:
        ctx.log(message, "warn")
    return None


def after_check(conn, config, changed=None):
    """update() right after the owner decided about a picture's position or place (admin.py):
    the pictures whose position changed are looked up at once -- when they are few
    (QUICK_LIMIT; else the next job does it) and the named places were looked up before
    with these street maps. changed (a set) gets the ids whose named place changed.
    Never raises for the street maps; a busy database raises sqlite3.OperationalError."""
    if not have_table(conn) or not maps_key():
        return set()
    found = set()
    try:
        update(conn, config, limit=QUICK_LIMIT, changed=found)
    except (street_maps.MapsError, OSError):
        return set()
    if changed is not None:
        changed.update(found)
    return found


def status(conn, config=None):
    """{"available" (street maps), "enabled", "pictures" (looked up), "named", "by_source":
    {source: named}, "command"} for the Manage page."""
    result = {"available": bool(maps_key()), "enabled": settings(config or {})["enabled"],
              "pictures": 0, "named": 0, "by_source": {}, "command": update_command()}
    if conn is None or not have_table(conn):
        return result
    for source, total, named in conn.execute(
            "SELECT n.source, COUNT(*), SUM(n.name <> '') FROM named_places AS n "
            "JOIN images AS i ON i.id = n.image_id WHERE i.missing_since IS NULL "
            "GROUP BY n.source"):
        result["pictures"] += total
        result["named"] += named or 0
        result["by_source"][source] = named or 0
    return result


# ---------------------------------------------------------------------------
# Command line

def command_args(params):
    """The arguments of named_places.py that do what a "named_places" job with params does:
    look up the pictures that changed ({}), or every picture ({"redo": True})."""
    return ["--redo"] if params.get("redo") else ["--update"]


def command_line(params):
    """The command line of a "named_places" job (without --settings-base)."""
    return " ".join(["python", "named_places.py"] + command_args(params))


def update_command():
    """The command line that looks up the named places, as the Manage page shows it: with
    --settings-base DIR when the settings are not in the program folder."""
    import jobs
    try:
        return jobs.command_text("named_places.py",
                                 command_args({}) + jobs.settings_base_tokens())
    except jobs.NoCommand:
        return command_line({})


def job_params(args):
    """The params of the job that does what the parsed command line does (None: --status)."""
    if args.redo:
        return {"redo": True}
    if args.update:
        return {}
    return None


def parser():
    import jobs
    found = argparse.ArgumentParser(
        prog="named_places.py",
        description="Named places from the street maps: the park, beach, museum or cafe "
                    "where each picture with a position was taken (offline: the street maps "
                    "downloaded by street_maps.py).")
    found.add_argument("--status", action="store_true",
                       help="Show how many pictures have a named place (the default).")
    found.add_argument("--update", action="store_true",
                       help="Look up the pictures whose position changed since (all of "
                            "them the first time, or after the street maps changed).")
    found.add_argument("--redo", action="store_true",
                       help="Look up every picture again.")
    found.add_argument("--lookup", nargs=2, type=float, metavar=("LAT", "LON"),
                       help="Print the named places that fit a position, best first, and "
                            "exit.")
    found.add_argument("--accuracy", type=float, default=DEFAULT_ACCURACY_M, metavar="M",
                       help="With --lookup: how far off the position may be, in metres "
                            "(default %d)." % DEFAULT_ACCURACY_M)
    jobs.add_settings_base_argument(found)
    return found


def _print_lookup(lat, lon, accuracy):
    try:
        archive = street_maps.open_local(street_maps.archive_path())
    except (OSError, street_maps.MapsError):
        print("There are no street maps (python street_maps.py --download first).")
        return 1
    try:
        found = candidates(lat, lon, accuracy, Reader(archive))
    finally:
        archive.read.close()
    chosen = choose(found, accuracy)
    if not found:
        print("No named place fits.")
    elif chosen is None:
        print("None is chosen: the position cannot tell the best ones apart.")
    for place in found[:10]:
        print("%s  (%s, %s, score %.2f)%s" % (
            place.name, kind_words(place.kind),
            "inside" if place.inside else "%d m" % place.metres, place.score,
            "  <- chosen" if place is chosen else ""))
    return 0


def main(argv=None):
    import db
    import jobs
    command = parser()
    args = command.parse_args(argv)
    if sum(1 for flag in (args.status, args.update, args.redo) if flag) > 1:
        command.error("give one of --status, --update and --redo")
    jobs.use_settings_base(command, args)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    if args.lookup:
        return _print_lookup(args.lookup[0], args.lookup[1], args.accuracy)
    ctx = jobs.ConsoleContext("named places")
    jobs.log_tool_command(ctx, "named_places.py", argv)
    missing = jobs.missing_settings()
    if missing:
        print(missing)
        return 2
    try:
        config = common.load_config()
        db.init(config, log=ctx.log)
        conn = db.connect()
    except (db.DatabaseError, common.ConfigError) as error:
        print("Error: %s" % error)
        return 2
    try:
        params = job_params(args)
        if params is None:
            found = status(conn, config)
            if not found["available"]:
                print("There are no street maps, so no named places: %s downloads them."
                      % street_maps.download_command())
            elif not found["enabled"]:
                print("Named places are turned off (config.json \"named_places\").")
            else:
                print("%s of %s picture(s) with a position have a named place%s." % (
                    "{:,}".format(found["named"]), "{:,}".format(found["pictures"]),
                    "" if not found["by_source"] else " (%s)" % ", ".join(
                        "%s %s" % ("{:,}".format(count), source)
                        for source, count in sorted(found["by_source"].items()))))
            return 0
        lock = jobs.WorkerLock()
        if not lock.acquire("named_places.py"):
            print("Another job is running: %s. Try again when it has finished."
                  % (lock.holder() or "another process"))
            return 1
        try:
            with ctx.stop_on_ctrl_c():
                summary = update(conn, config, ctx, redo=bool(params.get("redo")))
        finally:
            lock.release()
        if not summary["maps"]:
            print("There are no street maps, so no named places: %s downloads them."
                  % street_maps.download_command())
            return 1
        if not summary["looked_up"] and not summary["removed"]:
            ctx.log(summary_text(summary))
        return 1 if summary["stopped"] else 0
    except (street_maps.MapsError, OSError) as error:
        print("Error: the street maps cannot be read: %s" % common.redact(str(error)))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
