#!/usr/bin/env python3
"""Place names for pictures with a GPS position: the nearest town, offline.

GeoNames (https://www.geonames.org/) publishes the towns of the world with
1,000 people or more, and the seats of regions, as free files under the
Creative Commons Attribution 4.0 licence. download() fetches three of them
into data/places/ -- only when the owner asks (make_places.py, or the
Manage page's "Download place names"); this program does not ship them --
and build() turns them into data/places/places.db:

    cities     id, name, lat, lon, cell, country, region, population, zone
               (sorted by grid cell, see CELL_DEGREES)
    countries  id, cc (ISO code), name             (countryInfo.txt)
    regions    id, code ("US.HI"), name            (admin1CodesASCII.txt)
    zones      id, name (the town's time zone: "Europe/Paris")
    meta       format, built (time), source, cities

PlaceIndex reads that into memory and finds the nearest town of a
position by great-circle distance. It looks at the towns of the grid
cells around the position only: first those within START_KM, then twice
as far, ... up to MAX_KM, stopping as soon as the nearest town found is
nearer than the distance searched (no town outside it can be nearer).

A town's time zone (GeoNames' IANA time zone id) is the time zone of
the pictures taken near it (PlaceIndex.zone_of, zones_near;
local_times.py turns it into the local time of a picture). A places.db
of the first format has no time zones: it still names places, and
rebuild_if_needed() builds it again from the downloaded files (no
download).

place_columns() gives the images columns of a position (db.SCHEMA_V4):

    place_city     the town, when it is at most CITY_KM away; else ""
    place_region   the town's region (state, province, ...), or ""
    place_country  the town's country, place_cc its ISO code ("US")
    place_km       how far the town is (km, to 0.1 km); -1 when no town
                   is within MAX_KM (the open sea) or the position is not
                   usable; NULL while the position was not looked up

The scanner fills them in for new and changed pictures, and
label_pictures() for the others (after each library's scan, and the
"places" job). Place names are only words about where a picture was
taken: the search page searches them and offers them as a filter; an
export holds them only when its Location option is on.

This module uses only the standard library.
"""

import array
import http.client
import io
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile

import common
import db
import world_map

# Where the files come from, and the files.
GEONAMES_URL = "https://download.geonames.org/export/dump/"
CITIES_FILE = "cities1000.zip"
CITIES_TEXT = "cities1000.txt"
REGIONS_FILE = "admin1CodesASCII.txt"
COUNTRIES_FILE = "countryInfo.txt"
FILES = (CITIES_FILE, REGIONS_FILE, COUNTRIES_FILE)
# The folder (in data/places) a download goes to until it is known to be good.
INCOMING = "incoming"
# Linked wherever place names are shown to others (an export's credits).
ATTRIBUTION_URL = "https://www.geonames.org/"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
USER_AGENT = "Search-Images (place names)"
DOWNLOAD_TIMEOUT = 60           # seconds without data before a download gives up
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_CITIES_TEXT_BYTES = 600 * 1024 * 1024   # the text inside the zip file
CHUNK = 64 * 1024

# The layout of places.db; raise it when it changes (an older file is then
# built again from the downloaded files, see rebuild_if_needed).
PLACES_FORMAT = 2
# Older layouts that can still be read (for place names; the first has no
# time zones) until it is built again.
READABLE_FORMATS = (1, 2)
# The grid: cells of this many degrees of latitude and longitude.
CELL_DEGREES = 0.5
ROWS = int(round(180 / CELL_DEGREES))
COLUMNS = int(round(360 / CELL_DEGREES))
# A town at most this far away names the place; farther, only its region
# and country do (the picture was taken out in the country, at sea near
# the coast, ...).
CITY_KM = 50.0
# Farther than this from any town there is no place at all: the middle of
# an ocean is not "in" the country of the nearest island.
MAX_KM = 250.0
# The first distance searched (km); doubled until a town is found.
START_KM = 25.0
EARTH_KM = 6371.0088
# GeoNames feature codes left out: parts of a town (a neighbourhood would
# stand in for the town) and places that are no more.
SKIPPED_FEATURES = frozenset(("PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"))
# A position looked up recently is remembered (pictures of one place come
# in bursts); at most this many.
MEMO_SIZE = 20000
# How often (seconds) shared() looks whether places.db has changed.
RECHECK_SECONDS = 10.0
# Rows labelled between commits.
COMMIT_ROWS = 500
# A download whose files leave more than this share of the towns without
# their country's name (or region's name) is taken for a broken one, and
# the place index is not replaced. (In the GeoNames files every country has
# its name; a few towns have a region code the region file does not list.)
MAX_UNNAMED_COUNTRIES = 0.05
MAX_UNNAMED_REGIONS = 0.2

# Towns a name search (PlaceIndex.find_towns) lists at most.
FIND_LIMIT = 8
FIND_MIN_CHARS = 2

_NAME_SPACE_RE = re.compile(r"[\s|]+")
_CC_RE = re.compile(r"^[A-Z]{2}$")
# An IANA time zone id as GeoNames writes it ("America/Argentina/Ushuaia").
_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}$")


class PlacesError(Exception):
    """Place names cannot be downloaded, built or read; the message says why."""


class OutdatedIndex(PlacesError):
    """places.db was built by another version of this program (PLACES_FORMAT)."""


# ---------------------------------------------------------------------------
# Paths

def places_dir():
    """data/places: the downloaded GeoNames files and places.db."""
    return os.path.join(common.data_dir(), "places")


def places_db_path():
    return os.path.join(places_dir(), "places.db")


# ---------------------------------------------------------------------------
# Grid and distances

def cell_of(lat, lon):
    """The grid cell of a position: row * COLUMNS + column."""
    row = min(ROWS - 1, max(0, int(math.floor((lat + 90.0) / CELL_DEGREES))))
    column = int(math.floor((lon + 180.0) / CELL_DEGREES)) % COLUMNS
    return row * COLUMNS + column


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    half = (math.sin((phi2 - phi1) / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(half)))


def cells_near(lat, lon, km):
    """The grid cells holding every point at most km from a position (and a few more).

    The box around the circle: its latitudes, and the longitudes the circle
    reaches at its widest (asin(sin r / cos lat)); every longitude when the
    circle reaches a pole.
    """
    angle = km / EARTH_KM
    spread = math.degrees(angle)
    low, high = lat - spread, lat + spread
    first = min(ROWS - 1, max(0, int(math.floor((low + 90.0) / CELL_DEGREES))))
    last = min(ROWS - 1, max(0, int(math.floor((high + 90.0) / CELL_DEGREES))))
    sine = math.sin(angle) / max(1e-12, math.cos(math.radians(lat)))
    if low <= -90.0 or high >= 90.0 or sine >= 1.0:
        columns = range(COLUMNS)
    else:
        wide = math.degrees(math.asin(sine))
        start = int(math.floor((lon - wide + 180.0) / CELL_DEGREES))
        stop = int(math.floor((lon + wide + 180.0) / CELL_DEGREES))
        if stop - start + 1 >= COLUMNS:
            columns = range(COLUMNS)
        else:
            columns = [column % COLUMNS for column in range(start, stop + 1)]
    return [row * COLUMNS + column for row in range(first, last + 1) for column in columns]


# ---------------------------------------------------------------------------
# Downloading

def _report(ctx, message, level="info"):
    if ctx is not None:
        ctx.log(message, level)
    else:
        print(message)


def _stopping(ctx):
    return ctx is not None and ctx.should_stop()


def _content_length(response):
    """The Content-Length of a response (int), or None when it has none (or a strange one)."""
    value = (response.headers.get("Content-Length") or "").strip()
    return int(value) if value.isdigit() else None


def _fetch(url, target, ctx, timeout):
    """Download url to target (through target.part); returns the size in bytes.

    The file is only kept when it arrived whole: as many bytes as the
    server said it would send (a connection that breaks off early does
    not raise an error by itself).
    """
    part = target + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            expected = _content_length(response)
            with open(part, "wb") as handle:
                while True:
                    if _stopping(ctx):
                        raise PlacesError("The download was stopped.")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise PlacesError("%s is larger than expected (over %d MB); not used."
                                          % (url, MAX_DOWNLOAD_BYTES // (1024 * 1024)))
                    handle.write(chunk)
        if not size:
            raise PlacesError("%s was empty." % url)
        if expected is not None and size != expected:
            raise PlacesError("%s arrived incomplete (%d of %d bytes); try again."
                              % (url, size, expected))
        os.replace(part, target)
    except urllib.error.HTTPError as error:
        raise PlacesError("Could not download %s: the server answered %d %s."
                          % (url, error.code, error.reason))
    except (http.client.HTTPException, ValueError) as error:
        # (A reply cut off in the middle, or one that is not HTTP at all.)
        raise PlacesError("Could not download %s: the connection broke off (%s)."
                          % (url, type(error).__name__))
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", None) or error
        raise PlacesError("Could not download %s: %s" % (url, reason))
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    return size


def download(folder=None, ctx=None, base_url=None, timeout=DOWNLOAD_TIMEOUT):
    """Download the GeoNames files (FILES) into folder (default data/places).

    base_url is where they are (default GEONAMES_URL). A file is only
    replaced once it has arrived whole. Raises PlacesError when one cannot
    be downloaded (or ctx asks to stop).
    """
    folder = common.ensure_dir(folder or places_dir())
    base_url = base_url or GEONAMES_URL
    if not base_url.endswith("/"):
        base_url += "/"
    if ctx is not None:
        ctx.stage("download", total=len(FILES))
    total = 0
    for name in FILES:
        url = base_url + name
        _report(ctx, "Downloading %s" % url)
        size = _fetch(url, os.path.join(folder, name), ctx, timeout)
        total += size
        _report(ctx, "  %s: %.1f MB" % (name, size / 1048576.0))
        if ctx is not None:
            ctx.step(item=name)
    return total


def have_files(folder=None):
    """True when folder (default data/places) holds the three files build() needs."""
    folder = folder or places_dir()
    return (_cities_source(folder) is not None
            and os.path.isfile(os.path.join(folder, REGIONS_FILE))
            and os.path.isfile(os.path.join(folder, COUNTRIES_FILE)))


# ---------------------------------------------------------------------------
# Building places.db

def _clean_name(text):
    """A name as stored: single spaces, no "|" (the search page's separator)."""
    return _NAME_SPACE_RE.sub(" ", text or "").strip()[:200]


def _cities_source(folder):
    """The cities file in folder: cities1000.zip, else cities1000.txt, else None."""
    for name in (CITIES_FILE, CITIES_TEXT):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def _open_text(path):
    return open(path, "r", encoding="utf-8", errors="replace", newline="")


def read_countries(path):
    """{ISO code: name} from countryInfo.txt (lines starting with # are notes)."""
    countries = {}
    with _open_text(path) as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) > 4 and _CC_RE.match(fields[0]) and fields[4].strip():
                countries[fields[0]] = _clean_name(fields[4])
    return countries


def read_regions(path):
    """{"CC.CODE": name} from admin1CodesASCII.txt."""
    regions = {}
    with _open_text(path) as handle:
        for line in handle:
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) >= 2 and "." in fields[0] and fields[1].strip():
                regions[fields[0]] = _clean_name(fields[1])
    return regions


def _city_lines(path):
    """The lines of the cities file (a zip holding cities1000.txt, or the text itself)."""
    if path.lower().endswith(".zip"):
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as error:
            raise PlacesError("%s is not a usable zip file: %s" % (path, error))
        with archive:
            names = [info for info in archive.infolist()
                     if info.filename.rsplit("/", 1)[-1].lower().endswith(".txt")
                     and not info.is_dir()]
            if not names:
                raise PlacesError("%s holds no text file of towns." % path)
            member = names[0]
            if member.file_size > MAX_CITIES_TEXT_BYTES:
                raise PlacesError("The towns file in %s is larger than expected; not used."
                                  % path)
            with archive.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
                for line in text:
                    yield line
        return
    with _open_text(path) as handle:
        for line in handle:
            yield line


def read_cities(path):
    """[(name, lat, lon, cc, admin1 code, population, time zone)] of the towns in a GeoNames
    cities file.

    Lines that are not a town (wrong number of columns, an unusable
    position) and the feature codes in SKIPPED_FEATURES are left out. The
    time zone is "" when the line has none (or not one that looks like an
    IANA id).
    """
    cities = []
    for line in _city_lines(path):
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) < 15 or fields[6] != "P" or fields[7] in SKIPPED_FEATURES:
            continue
        try:
            lat = float(fields[4])
            lon = float(fields[5])
        except ValueError:
            continue
        if not world_map.valid_position(lat, lon):
            continue
        name = _clean_name(fields[1]) or _clean_name(fields[2])
        cc = fields[8].strip().upper()
        if not name or not _CC_RE.match(cc):
            continue
        try:
            population = max(0, int(fields[14] or 0))
        except ValueError:
            population = 0
        zone = fields[17].strip() if len(fields) > 17 else ""
        if len(zone) > 64 or not _ZONE_RE.match(zone):
            zone = ""
        cities.append((name, lat, lon, cc, fields[10].strip(), population, zone))
    return cities


_PLACES_SCHEMA = (
    "CREATE TABLE countries (id INTEGER PRIMARY KEY, cc TEXT NOT NULL UNIQUE, "
    "name TEXT NOT NULL)",
    "CREATE TABLE regions (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, "
    "name TEXT NOT NULL)",
    "CREATE TABLE zones (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
    "CREATE TABLE cities (id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, "
    "lon REAL NOT NULL, cell INTEGER NOT NULL, country INTEGER NOT NULL, "
    "region INTEGER NOT NULL, population INTEGER NOT NULL, zone INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _replace(source, target):
    """os.replace, tried again for a moment while another reader holds target (Windows)."""
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 19:
                raise PlacesError("%s is in use; try again in a moment." % target)
            time.sleep(0.1 * (attempt + 1))


def build(folder=None, out_path=None, ctx=None, source="", built=None):
    """Build places.db (default data/places/places.db) from the GeoNames files in folder.

    folder (default data/places) holds cities1000.zip (or cities1000.txt),
    admin1CodesASCII.txt and countryInfo.txt. The new file replaces the old
    one only when it is complete. source is noted in the file (where the
    files came from), and built (default now: when the files were
    downloaded, for an index built again from the same files). Returns
    {"cities", "countries", "regions", "zones"}. Raises
    PlacesError when a file is missing, cannot be read or written, holds no
    towns, or looks cut off (too many towns without their country's or
    region's name, see MAX_UNNAMED_COUNTRIES); the old file is then kept.
    """
    folder = folder or places_dir()
    out_path = out_path or places_db_path()
    cities_path = _cities_source(folder)
    missing = [name for name, there in ((CITIES_FILE, cities_path),
                                        (REGIONS_FILE, os.path.isfile(
                                            os.path.join(folder, REGIONS_FILE))),
                                        (COUNTRIES_FILE, os.path.isfile(
                                            os.path.join(folder, COUNTRIES_FILE))))
               if not there]
    if missing:
        raise PlacesError("%s: missing %s. Download them first." % (folder, ", ".join(missing)))
    if ctx is not None:
        ctx.stage("build places")
    _report(ctx, "Building the place index from %s" % folder)
    try:
        countries = read_countries(os.path.join(folder, COUNTRIES_FILE))
        regions = read_regions(os.path.join(folder, REGIONS_FILE))
        cities = read_cities(cities_path)
    except OSError as error:
        raise PlacesError("Could not read the place names in %s: %s" % (folder, error))
    if not cities:
        raise PlacesError("%s holds no towns; is it the GeoNames cities file?" % cities_path)
    check_names(cities, countries, regions)
    if _stopping(ctx):
        raise PlacesError("Building the place index was stopped.")
    try:
        summary = _write_index(cities, countries, regions, out_path, source, built)
    except (sqlite3.Error, OSError) as error:
        raise PlacesError("Could not write the place index %s: %s" % (out_path, error))
    _report(ctx, "Place index ready: %d towns in %d countries." % (summary["cities"],
                                                                  summary["countries"]))
    return summary


def check_names(cities, countries, regions):
    """Raise PlacesError when the country or region names look cut off.

    cities as read_cities() gives them, countries and regions as
    read_countries() and read_regions() do. A file that arrived only in
    part (or is something else) leaves many towns without the name of
    their country or region (MAX_UNNAMED_COUNTRIES, MAX_UNNAMED_REGIONS).
    """
    if not countries:
        raise PlacesError("%s holds no countries; download the place names again."
                          % COUNTRIES_FILE)
    if not regions:
        raise PlacesError("%s holds no regions; download the place names again."
                          % REGIONS_FILE)
    total = len(cities)
    no_country = sum(1 for city in cities if city[3] not in countries)
    no_region = sum(1 for city in cities
                    if city[4] and "%s.%s" % (city[3], city[4]) not in regions)
    for name, count, share, what in ((COUNTRIES_FILE, no_country, MAX_UNNAMED_COUNTRIES,
                                      "country"),
                                     (REGIONS_FILE, no_region, MAX_UNNAMED_REGIONS, "region")):
        if count > total * share:
            raise PlacesError("%s looks incomplete: %d of %d towns are in a %s it does not "
                              "name. Download the place names again."
                              % (name, count, total, what))


def _write_index(cities, countries, regions, out_path, source, built=None):
    """Write places.db (see build) through a temporary file; returns build()'s summary."""
    common.ensure_dir(os.path.dirname(os.path.abspath(out_path)))
    tmp = "%s.tmp-%d" % (out_path, os.getpid())
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        for statement in _PLACES_SCHEMA:
            conn.execute(statement)
        country_ids = {}
        for cc in sorted({city[3] for city in cities}):
            country_ids[cc] = conn.execute("INSERT INTO countries (cc, name) VALUES (?, ?)",
                                           (cc, countries.get(cc) or cc)).lastrowid
        region_ids = {}
        for code in sorted({"%s.%s" % (city[3], city[4]) for city in cities if city[4]}):
            if code in regions:
                region_ids[code] = conn.execute(
                    "INSERT INTO regions (code, name) VALUES (?, ?)",
                    (code, regions[code])).lastrowid
        zone_ids = {}
        for zone in sorted({city[6] for city in cities if city[6]}):
            zone_ids[zone] = conn.execute("INSERT INTO zones (name) VALUES (?)",
                                          (zone,)).lastrowid
        rows = []
        for name, lat, lon, cc, admin1, population, zone in cities:
            rows.append((cell_of(lat, lon), -population, name, lat, lon, country_ids[cc],
                         region_ids.get("%s.%s" % (cc, admin1), 0), population,
                         zone_ids.get(zone, 0)))
        rows.sort()
        conn.executemany(
            "INSERT INTO cities (name, lat, lon, cell, country, region, population, zone) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(name, lat, lon, cell, country, region, population, zone)
             for cell, _order, name, lat, lon, country, region, population, zone in rows])
        meta = {"format": str(PLACES_FORMAT),
                "built": str(int(time.time() if built is None else built)),
                "source": source or GEONAMES_URL, "cities": str(len(rows)),
                "cell_degrees": str(CELL_DEGREES), "zones": str(len(zone_ids))}
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", sorted(meta.items()))
        conn.commit()
    except BaseException:
        conn.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    conn.close()
    release()
    try:
        _replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return {"cities": len(rows), "countries": len(country_ids), "regions": len(region_ids),
            "zones": len(zone_ids)}


def update(ctx=None, base_url=None, folder=None):
    """Download the GeoNames files and build places.db from them; returns build()'s summary.

    The files are downloaded into folder/incoming (folder: data/places by
    default) and take the place of the old ones only once places.db was
    built from them: a download that fails, breaks off or looks cut off
    (see build) leaves the old files and place index as they were.
    """
    folder = folder or places_dir()
    incoming = os.path.join(folder, INCOMING)
    try:
        download(incoming, ctx, base_url)
        summary = build(incoming, ctx=ctx, source=base_url or GEONAMES_URL)
        for name in FILES:
            _replace(os.path.join(incoming, name), os.path.join(folder, name))
    except OSError as error:
        raise PlacesError("Could not save the downloaded place names in %s: %s"
                          % (folder, error))
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    return summary


# ---------------------------------------------------------------------------
# Looking up places

class PlaceIndex:
    """The towns of places.db in memory, found by position (see the module notes).

    Loading takes a moment (a few hundred thousand towns); lookups are
    then quick. Thread safe: nothing changes after loading but the memo.
    """

    def __init__(self, path=None):
        path = path or places_db_path()
        meta = read_meta(path)      # (raises PlacesError when it is from another version)
        # (The first format has no time zones: has_zones is False.)
        self.has_zones = has_zones(meta)
        try:
            conn = db.connect_readonly(path)
        except db.DatabaseError as error:
            raise PlacesError(str(error))
        try:
            self.countries = {row[0]: (row[1], row[2])
                              for row in conn.execute("SELECT id, cc, name FROM countries")}
            self.regions = {row[0]: row[1]
                            for row in conn.execute("SELECT id, name FROM regions")}
            self.zone_names = ({row[0]: row[1] for row in conn.execute(
                "SELECT id, name FROM zones")} if self.has_zones else {})
            self.lat = array.array("d")
            self.lon = array.array("d")
            self.cos = array.array("d")
            self.country = array.array("i")
            self.region = array.array("i")
            self.zone = array.array("i")
            self.names = []
            self.cells = {}
            current, start = None, 0
            for index, row in enumerate(conn.execute(
                    "SELECT cell, lat, lon, name, country, region, %s FROM cities "
                    "ORDER BY cell, id" % ("zone" if self.has_zones else "0"))):
                cell = row[0]
                if cell != current:
                    if current is not None:
                        self.cells[current] = (start, index)
                    current, start = cell, index
                self.lat.append(math.radians(row[1]))
                self.lon.append(math.radians(row[2]))
                self.cos.append(math.cos(math.radians(row[1])))
                self.names.append(row[3])
                self.country.append(row[4])
                self.region.append(row[5])
                self.zone.append(row[6])
            if current is not None:
                self.cells[current] = (start, len(self.names))
        except sqlite3.Error as error:
            raise PlacesError("Could not read %s: %s" % (path, error))
        finally:
            conn.close()
        if not self.names:
            raise PlacesError("%s holds no towns." % path)
        self.path = path
        self._memo = {}
        self._search = None             # (folded names, populations), made when first wanted
        self._search_lock = threading.Lock()

    def __len__(self):
        return len(self.names)

    def nearest(self, lat, lon, max_km=MAX_KM):
        """(town number, km) of the nearest town at most max_km away, or None."""
        phi = math.radians(lat)
        lam = math.radians(lon)
        cos_phi = math.cos(phi)
        best, best_half = -1, 2.0
        seen = set()
        km = min(START_KM, max_km)
        sin = math.sin
        lats, lons, coss = self.lat, self.lon, self.cos
        while True:
            for cell in cells_near(lat, lon, km):
                if cell in seen:
                    continue
                seen.add(cell)
                span = self.cells.get(cell)
                if span is None:
                    continue
                for index in range(span[0], span[1]):
                    half = (sin((lats[index] - phi) / 2) ** 2
                            + cos_phi * coss[index] * sin((lons[index] - lam) / 2) ** 2)
                    if half < best_half:
                        best, best_half = index, half
            found = (2 * EARTH_KM * math.asin(min(1.0, math.sqrt(best_half)))
                     if best >= 0 else None)
            if (found is not None and found <= km) or km >= max_km:
                break
            km = min(km * 2, max_km)
        if best < 0 or found > max_km:
            return None
        return best, found

    def town(self, index):
        """{"name", "region", "country", "cc", "lat", "lon", "zone"} of a town number (zone:
        its time zone id, "" when unknown)."""
        cc, country = self.countries.get(self.country[index], ("", ""))
        return {"name": self.names[index], "region": self.regions.get(self.region[index], ""),
                "country": country, "cc": cc, "lat": math.degrees(self.lat[index]),
                "lon": math.degrees(self.lon[index]),
                "zone": self.zone_names.get(self.zone[index], "")}

    def zone_of(self, lat, lon):
        """(time zone id, km) of the nearest town at most MAX_KM away when it has a time
        zone, or None (no town near, or a place index without time zones).

        The nearest town's time zone is the place's: near a border between
        two time zones it may be the other one (see zones_near).
        """
        if not self.has_zones or not world_map.valid_position(lat, lon):
            return None
        key = ("zone", lat, lon)
        if key in self._memo:
            return self._memo[key]
        hit = self.nearest(float(lat), float(lon))
        found = None
        if hit is not None and self.zone[hit[0]] in self.zone_names:
            found = (self.zone_names[self.zone[hit[0]]], hit[1])
        if len(self._memo) >= MEMO_SIZE:
            self._memo.clear()
        self._memo[key] = found
        return found

    def zones_near(self, lat, lon, km):
        """The time zone ids of the towns at most km from a position (a set; empty without
        time zones). Slower than zone_of: for the few places where it matters."""
        if not self.has_zones or not world_map.valid_position(lat, lon):
            return set()
        lat, lon = float(lat), float(lon)
        phi, lam = math.radians(lat), math.radians(lon)
        cos_phi = math.cos(phi)
        limit = math.sin(min(math.pi / 2, km / (2 * EARTH_KM))) ** 2
        found = set()
        for cell in cells_near(lat, lon, km):
            span = self.cells.get(cell)
            if span is None:
                continue
            for index in range(span[0], span[1]):
                zone = self.zone[index]
                if zone in found or zone not in self.zone_names:
                    continue
                half = (math.sin((self.lat[index] - phi) / 2) ** 2 + cos_phi * self.cos[index]
                        * math.sin((self.lon[index] - lam) / 2) ** 2)
                if half <= limit:
                    found.add(zone)
        return {self.zone_names[zone] for zone in found}

    def _search_lists(self):
        """(folded names, populations) in town order, read once (see find_towns)."""
        with self._search_lock:
            if self._search is None:
                try:
                    conn = db.connect_readonly(self.path)
                except db.DatabaseError as error:
                    raise PlacesError(str(error))
                try:
                    population = array.array("i", (row[0] for row in conn.execute(
                        "SELECT population FROM cities ORDER BY cell, id")))
                except sqlite3.Error as error:
                    raise PlacesError("Could not read %s: %s" % (self.path, error))
                finally:
                    conn.close()
                self._search = ([fold(name) for name in self.names], population)
            return self._search

    def find_towns(self, text, limit=FIND_LIMIT):
        """The towns whose names start with text, most people first (a name that is all of
        text before the others): [town() dicts with "population"].

        "Name, Region" (or "Name, Country") also wants the region or the
        country (its name, or its code) to start with what follows the
        comma. Case, accents and repeated spaces do not matter. Fewer than
        FIND_MIN_CHARS letters find nothing.
        """
        parts = [fold(part) for part in str(text or "").split(",")]
        parts = [part for part in parts if part]
        if not parts or len(parts[0]) < FIND_MIN_CHARS:
            return []
        names, population = self._search_lists()
        wanted, rest = parts[0], parts[1:]
        found = []
        for index, name in enumerate(names):
            if not name.startswith(wanted):
                continue
            if rest:
                town = self.town(index)
                where = (fold(town["region"]), fold(town["country"]), town["cc"].casefold())
                if not all(any(value.startswith(part) for value in where) for part in rest):
                    continue
            found.append((name != wanted, -population[index], self.names[index], index))
        found.sort()
        result = []
        for _not_exact, people, _name, index in found[:max(0, int(limit))]:
            town = self.town(index)
            town["population"] = -people
            result.append(town)
        return result

    def columns(self, lat, lon):
        """The place columns (see the module notes) of a usable position."""
        key = (lat, lon)
        found = self._memo.get(key)
        if found is not None:
            return dict(found)
        hit = self.nearest(lat, lon)
        if hit is None:
            found = far_columns()
        else:
            town = self.town(hit[0])
            km = round(hit[1], 1)
            found = {"place_city": town["name"] if km <= CITY_KM else "",
                     "place_region": town["region"], "place_country": town["country"],
                     "place_cc": town["cc"], "place_km": km}
        if len(self._memo) >= MEMO_SIZE:
            self._memo.clear()
        self._memo[key] = found
        return dict(found)


def fold(text):
    """A name as a search compares it: no accents, single spaces, any case."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return " ".join("".join(char for char in decomposed
                            if not unicodedata.combining(char)).casefold().split())


def empty_columns():
    """The place columns of a picture not looked up (no position, or no place names yet)."""
    return {"place_city": "", "place_region": "", "place_country": "", "place_cc": "",
            "place_km": None}


def far_columns():
    """The place columns of a position with no town near it (or no usable position)."""
    return dict(empty_columns(), place_km=-1.0)


_shared_lock = threading.Lock()
_shared = None                  # (path, (size, mtime), PlaceIndex or None, when checked)
_warned = set()


def _signature(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def shared():
    """The PlaceIndex of data/places/places.db, or None when there is none (or it is unusable).

    Loaded once per process, and again when the file changes (looked at
    every RECHECK_SECONDS at most). A file that cannot be read is
    reported once.
    """
    global _shared
    path = places_db_path()
    with _shared_lock:
        now = time.monotonic()
        if (_shared is not None and _shared[0] == path
                and now - _shared[3] < RECHECK_SECONDS):
            return _shared[2]
        signature = _signature(path)
        if _shared is not None and _shared[0] == path and _shared[1] == signature:
            _shared = _shared[:3] + (now,)
            return _shared[2]
        index = None
        if signature is not None:
            try:
                index = PlaceIndex(path)
            except PlacesError as error:
                if (path, signature) not in _warned:
                    _warned.add((path, signature))
                    print("Warning: place names are not used: %s" % error, file=sys.stderr)
        _shared = (path, signature, index, now)
        return index


def release():
    """Forget the shared PlaceIndex (its memory); the next shared() loads it again."""
    global _shared
    with _shared_lock:
        _shared = None


def place_columns(lat, lon, index=None):
    """The place columns of a picture at lat, lon (see the module notes).

    index defaults to shared(); without place names (no places.db) the
    picture is left to be looked up later (place_km NULL).
    """
    if lat is None or lon is None:
        return empty_columns()
    if index is None:
        index = shared()
    if index is None:
        return empty_columns()
    if not world_map.valid_position(lat, lon):
        return far_columns()
    return index.columns(float(lat), float(lon))


def read_meta(path=None):
    """The meta table of places.db ({key: value}); raises PlacesError when it is unusable."""
    path = path or places_db_path()
    try:
        conn = db.connect_readonly(path)
    except db.DatabaseError as error:
        raise PlacesError(str(error))
    try:
        meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error as error:
        raise PlacesError("Could not read %s: %s" % (path, error))
    finally:
        conn.close()
    if meta.get("format") not in [str(number) for number in READABLE_FORMATS]:
        raise OutdatedIndex("%s was built by another version of this program (format %s)."
                            % (path, meta.get("format")))
    return meta


def has_zones(meta):
    """True when a places.db (its read_meta) holds the towns' time zones (not the first
    format)."""
    return bool(meta) and meta.get("format") != "1"


def rebuild_if_needed(ctx=None):
    """Build places.db again from the downloaded files when it cannot be used as it is.

    That is when it was built by another version of this program (or is
    damaged) while the GeoNames files are still in data/places: there is
    no need to download them again. One of an older format that can still
    be read (READABLE_FORMATS: place names without time zones) is built
    again too. Returns build()'s summary, or None when nothing was built
    (places.db is fine or missing, or the files are not there). Raises
    PlacesError when the build fails.
    """
    path = places_db_path()
    if not os.path.exists(path) or not have_files():
        return None
    try:
        meta = read_meta(path)
    except PlacesError as error:
        _report(ctx, "The place index cannot be used (%s); building it again from the "
                     "downloaded files." % error)
    else:
        if meta.get("format") == str(PLACES_FORMAT):
            return None
        _report(ctx, "The place index has no time zones yet; building it again from the "
                     "downloaded files (nothing is downloaded).")
        try:
            built = int(meta.get("built") or 0) or None
        except ValueError:
            built = None
        return build(ctx=ctx, source=meta.get("source", ""), built=built)
    return build(ctx=ctx)


def status(conn=None):
    """What the Manage page shows: {"available", "cities", "built", "source", "error",
    "rebuild", "zones", "attribution_url", "license_url", "city_km", "max_km",
    "pictures": {"located", "named", "region_only", "far", "pending"}}.

    Reads only the facts of places.db, not its towns. rebuild: places.db
    cannot be used, or has no time zones (zones False), but the downloaded
    files are there, so the places job (or the next scan) builds it again
    (rebuild_if_needed). pictures counts the present
    pictures with a GPS position of every library -- what the places job
    looks up (when conn, an index.db connection, is given) -- and then
    also "gps_ignored", the present pictures whose position the GPS check
    set aside (gps_check.py: they count nowhere else), "estimates"
    (place_estimates.status: the pictures without a position that have an
    estimated place, or one the owner set) and "local_times"
    (local_times.status: the pictures that have the local time of their
    place).
    """
    result = {"available": False, "cities": 0, "built": None, "source": "", "error": "",
              "rebuild": False, "zones": False, "attribution_url": ATTRIBUTION_URL,
              "license_url": LICENSE_URL, "city_km": CITY_KM, "max_km": MAX_KM}
    if os.path.exists(places_db_path()):
        try:
            meta = read_meta()
        except PlacesError as error:
            result["rebuild"] = have_files()
            result["error"] = "%s; %s." % (
                "The place index was made by another version of this program"
                if isinstance(error, OutdatedIndex) else
                "The place index cannot be used (%s)" % error,
                "it is built again from the downloaded files when you add place names"
                if result["rebuild"] else "download the place names again")
        else:
            try:
                result["cities"] = int(meta.get("cities") or 0)
                result["built"] = int(meta.get("built") or 0) or None
            except ValueError:
                pass
            result.update(available=True, source=meta.get("source", ""),
                          zones=has_zones(meta))
            result["rebuild"] = not result["zones"] and have_files()
    if conn is not None:
        row = conn.execute(
            "SELECT COUNT(*), SUM(place_km IS NOT NULL AND place_city <> ''), "
            "SUM(place_km IS NOT NULL AND place_city = '' AND place_country <> ''), "
            "SUM(place_km IS NOT NULL AND place_country = ''), SUM(place_km IS NULL) "
            "FROM images WHERE missing_since IS NULL AND lat IS NOT NULL "
            "AND lon IS NOT NULL").fetchone()
        result["pictures"] = {key: int(value or 0) for key, value in
                              zip(("located", "named", "region_only", "far", "pending"), row)}
        result["gps_ignored"] = conn.execute(
            "SELECT COUNT(*) FROM gps_checks AS g JOIN images AS i ON i.id = g.image_id "
            "WHERE g.ignored = 1 AND i.lat IS NULL AND i.missing_since IS NULL").fetchone()[0]
        import place_estimates
        result["estimates"] = place_estimates.status(conn)
        import local_times
        result["local_times"] = local_times.status(conn)
    return result


# ---------------------------------------------------------------------------
# Labelling pictures

_UPDATE_SQL = ("UPDATE images SET place_city = ?, place_region = ?, place_country = ?, "
               "place_cc = ?, place_km = ?, updated = ? WHERE id = ?")


def label_pictures(conn, ctx, library_ids=None, redo=False, index=None):
    """Fill in the place columns of the present pictures that have a GPS position.

    Only those not looked up yet (place_km NULL), or all of them with redo
    (after the place names were downloaded again). library_ids limits it
    to those libraries. Returns None when there are no place names
    (places.db is missing or unusable), else {"checked", "named",
    "region_only", "far", "aborted"}. Commits as it goes (and before it
    waits out a pause, so that other writers are not locked out); ctx can
    stop it. The towns are only loaded when a picture needs them.
    """
    if index is None and not os.path.exists(places_db_path()):
        return None
    sql = ("SELECT id, lat, lon FROM images WHERE missing_since IS NULL "
           "AND lat IS NOT NULL AND lon IS NOT NULL")
    params = []
    if not redo:
        sql += " AND place_km IS NULL"
    if library_ids is not None:
        library_ids = list(library_ids)
        if not library_ids:
            return {"checked": 0, "named": 0, "region_only": 0, "far": 0, "aborted": False}
        sql += " AND library_id IN (%s)" % ",".join("?" * len(library_ids))
        params.extend(library_ids)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    summary = {"checked": 0, "named": 0, "region_only": 0, "far": 0, "aborted": False}
    if not rows:
        return summary
    index = index or shared()
    if index is None:
        return None
    ctx.stage("places", total=len(rows))
    now = time.time()
    pending = 0
    try:
        for row in rows:
            if ctx.should_stop():
                summary["aborted"] = True
                break
            if ctx.paused and pending:
                conn.commit()
                pending = 0
            ctx.wait_if_paused()
            values = place_columns(row["lat"], row["lon"], index)
            conn.execute(_UPDATE_SQL, (values["place_city"], values["place_region"],
                                       values["place_country"], values["place_cc"],
                                       values["place_km"], now, row["id"]))
            summary["checked"] += 1
            if values["place_city"]:
                summary["named"] += 1
            elif values["place_country"]:
                summary["region_only"] += 1
            else:
                summary["far"] += 1
            pending += 1
            if pending >= COMMIT_ROWS:
                conn.commit()
                pending = 0
            ctx.step()
    finally:
        conn.commit()
    if summary["checked"]:
        ctx.log("Place names for %d picture(s): %d near a town, %d farther than %d km from "
                "one (region and country only), %d with no town within %d km."
                % (summary["checked"], summary["named"], summary["region_only"], CITY_KM,
                   summary["far"], MAX_KM))
    return summary
