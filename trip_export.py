#!/usr/bin/env python3
"""Export one trip (trips.py) as a static website whose first page is the trip's page.

The site is a library export (export.py) of the trip's pictures -- those of
every library the trip holds pictures of, with the same privacy rules and
options, the same search, viewer, picture and map pages -- whose index.html
is the trip's page (static/trips.js, as /trip/<id> shows it to visitors):

    index.html  the trip: its title, dates, places, map and days
    search.html the search page, of the trip's pictures only
    detail.html [map.html]
    data/trips.js  SEARCH_DATA.trips({enabled, best_per_day, trips: [the trip]})
    ... the rest as export.py makes it

The trip in data/trips.js carries only what its page shows of the published
pictures: its title, dates (the first and last day of the published
pictures), kind, the export ids of its pictures by day in the order taken,
how far apart one after the other were taken (trips.gap_classes: "within
20 s", "within two minutes" or "further" -- never a time), the look units of
each day (trips.look_units, of the published pictures), and with the location
option the places of the trip and of each day, from the published pictures'
place names. Its title is the export's title option, else the owner's or the
library's name for the trip, else one made from the places of the published
pictures that are not near home (trips.told_by: so neither a hidden picture
nor home names it). The trip's id in the export is "trip"; the libraries'
names and ids, the pictures left out and home never go.

Home is never shown. Home is where trips.py finds it (or the "home" setting of
"trips"); with home_km (an option of trip exports only: 0 or one of
HOME_KM_STEPS, default HOME_KM) above 0 the home area is the square of a grid
of home_km-wide squares that holds home, with the eight squares around it
(HomeArea): at least home_km from home every way, at most twice that, and --
as the grid does not move with home -- its edges do not tell where in its
middle square home is. The grids of the sizes allowed nest (each square of
one is four of the next smaller size), so exports made with different sizes
tell no more together than the one of the smallest size. With the location
option, no position in the home area goes out (the picture keeps its region
and country, not its town, nor the AI model's landmark and place-guess words;
nor does any picture whose town has pictures in the home area), no route
enters it (routes are cut at its edge, besides routes_trim at both ends of
every drive), and no estimated place in it is shown. Whatever the options, a
dashcam's clip whose GPS track enters the home area, or that has no fix (its
overlay prints the last one it had, often where the drive started), is left
out with the flag "home" (the review step shows them; one can be published by
choice), and so are the words of its drive (written from its frames too). A
home_km of 0 turns all of this off. Titles and descriptions are published as
written (the review step lists the pictures).

The options of a trip's last export are kept in data/trips.json (trips.py)
and are the next export's defaults; the export's state in
data/exports/state/ goes by "trip:<the trip's key>", so a trip whose first
day moves keeps its folder and picture ids.

    python export.py --trip ID [--out DIR] [--home-km KM] [the options of a
                     library export: --location --routes ...]
"""

import collections
import math
import os

import common
import db
import export
import gps_check
import image_edits
import jobs
import local_times
import place_estimates
import search_index
import tracks
import trips

# Options of a trip export besides export.DEFAULT_OPTIONS: home_km is 0 (off) or one of
# HOME_KM_STEPS, sizes each twice the one before, so that their grids nest.
HOME_KM = 10
HOME_KM_STEPS = (5, 10, 20, 40, 80, 160)
TRIP_DEFAULTS = {"home_km": HOME_KM}
# The key a trip export's state goes by (export._Export.owner_key).
KEY_PREFIX = export.TRIP_OWNER_PREFIX
# The trip's id in the export.
EXPORT_TRIP_ID = "trip"
# (file in the export, template) of a trip export's pages.
PAGES = (("index.html", "trips.html"), ("search.html", "page.html"),
         ("detail.html", "detail.html"))
SEARCH_PAGE = "search.html"
TRIP_PAGE = "index.html"
# The scripts and style sheets of a trip export (map.js: the trip page's map).
ASSETS = export.ASSETS + ("map.js", "trips.js", "trips.css")
KM_PER_DEGREE = 111.2


class HomeArea:
    """The area around home that a trip export never shows (see the module notes): the
    square holding home of a grid of km-wide squares, and the eight around it. The grid
    starts at latitude and longitude 0; its latitude steps are km, its longitude steps
    km at the edge (the one further from the equator) of home's row of the squares of
    the largest size: the same latitude for every size, so the grids of HOME_KM_STEPS
    nest, and at home the squares are at least km wide."""

    def __init__(self, home, km):
        self.km = float(km)
        self.lat_step = self.km / KM_PER_DEGREE
        row = math.floor(home[0] / self.lat_step)
        largest = HOME_KM_STEPS[-1] / KM_PER_DEGREE
        band = math.floor(home[0] / largest)
        edge = (band + 1) * largest if home[0] >= 0 else band * largest
        self.lon_step = min(120.0, self.km / (KM_PER_DEGREE * max(
            0.01, math.cos(math.radians(max(-89.9, min(89.9, edge)))))))
        column = math.floor(home[1] / self.lon_step)
        self.south = (row - 1) * self.lat_step
        self.north = (row + 2) * self.lat_step
        self.west = (column - 1) * self.lon_step
        self.width = 3 * self.lon_step

    def _x(self, lon):
        """Degrees east of the area's west edge, from -180 up to 180."""
        return (lon - self.west + 180.0) % 360.0 - 180.0

    def contains(self, lat, lon):
        """True for a position in the area."""
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return False
        return self.south <= lat < self.north and 0.0 <= self._x(lon) < self.width

    def crosses(self, first, second):
        """True when the straight line between two positions ((lat, lon)) passes through
        the area (in degrees, as the map draws a short line)."""
        x0 = self._x(first[1])
        dx = (second[1] - first[1] + 180.0) % 360.0 - 180.0
        y0, dy = first[0], second[0] - first[0]
        low, high = 0.0, 1.0
        for p, q in ((-dx, x0), (dx, self.width - x0), (-dy, y0 - self.south),
                     (dy, self.north - y0)):
            if p == 0:
                if q < 0:
                    return False
                continue
            ratio = q / p
            if p < 0:
                low = max(low, ratio)
            else:
                high = min(high, ratio)
            if low > high:
                return False
        return True

    def trim(self, route):
        """A route (tracks.routes') without its points in the area, cut where it enters the
        area (as tracks.trim_route), or None when nothing is left of it."""
        clips = []
        for clip in route["c"]:
            points = tracks.decode(clip["p"])
            runs, run = [], []
            for point in points:
                if self.contains(point[1], point[2]):
                    if run:
                        runs.append(run)
                        run = []
                    continue
                if run and self.crosses(run[-1][1:3], point[1:3]):
                    runs.append(run)
                    run = []
                run.append(point)
            if run:
                runs.append(run)
            cut = len(runs) != 1 or len(runs[0]) != len(points)
            for run in runs:
                entry = dict(clip, p=tracks.encode_points(run))
                if cut:
                    entry["t"] = 1
                clips.append(entry)
        if not clips:
            return None
        return dict(route, d=clips[0]["i"], c=clips)


# ---------------------------------------------------------------------------
# Options

def trip_options(saved=None, overrides=None):
    """A trip export's options checked: export.validate_options' (saved, then overrides)
    and home_km. An album cannot be chosen (the trip's pictures go). Raises
    export.OptionsError naming every bad option."""
    saved = dict(saved) if isinstance(saved, dict) else {}
    if overrides is not None and not isinstance(overrides, dict):
        raise export.OptionsError({"options": "must be an object"})
    overrides = dict(overrides or {})
    errors = {}
    home_km = saved.pop("home_km", HOME_KM)
    if not _good_km(home_km):
        home_km = HOME_KM
    if "home_km" in overrides:
        value = overrides.pop("home_km")
        if _good_km(value):
            home_km = value
        else:
            errors["home_km"] = ("must be one of %s kilometres, or 0 (home is not kept out)"
                                 % ", ".join(str(step) for step in HOME_KM_STEPS))
    if overrides.get("album") is not None:
        errors["album"] = "cannot be chosen for a trip: the trip's pictures go"
    overrides.pop("album", None)
    saved.pop("album", None)
    try:
        options = export.validate_options(overrides, base=saved)
    except export.OptionsError as error:
        errors.update(error.errors)
        options = None
    if errors:
        raise export.OptionsError(errors)
    options["album"] = None
    options["home_km"] = home_km
    return options


def _good_km(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and (value == 0 or value in HOME_KM_STEPS))


def options_to_save(options):
    """The options worth keeping for a trip: those that differ from the defaults."""
    saved = export.options_to_save({key: value for key, value in options.items()
                                    if key in export.DEFAULT_OPTIONS})
    saved.pop("album", None)
    if options.get("home_km", HOME_KM) != HOME_KM:
        saved["home_km"] = options["home_km"]
    return saved


# ---------------------------------------------------------------------------
# The trip and its pictures

def _library_ids(config):
    return [library["id"] for library in common.enabled_libraries(config) if library.get("id")]


def load(conn, config, trip_id):
    """(trips.Trips, the trip, [library dicts of its pictures]). Raises ExportError."""
    if not isinstance(trip_id, str) or not trips.ID_RE.match(trip_id):
        raise export.ExportError("There is no trip %r." % (trip_id,))
    built = trips.build(conn, config, _library_ids(config))
    try:
        trip = built.get(trip_id)
    except trips.TripError as error:
        raise export.ExportError("%s (Trips are listed on the Trips page.)" % error)
    wanted = set(picture.library for picture in trip.pictures + trip.other)
    order = list(trip.libraries) + sorted(wanted - set(trip.libraries))
    libraries = [common.get_library(config, library_id) for library_id in order
                 if library_id in wanted]
    return built, trip, [library for library in libraries if library is not None]


def candidates(trip):
    """The ids of the pictures of a trip (an export never publishes the hidden ones: they
    are counted as such)."""
    return {picture.id for picture in trip.pictures + trip.other}


def home_area(built, options):
    """The HomeArea of an export with these options, or None (no home, or home_km 0)."""
    if built.home is None or not options.get("home_km"):
        return None
    return HomeArea(built.home, options["home_km"])


def home_flags(conn, config, library_ids, ids, area):
    """{id: [export.HOME_FLAG]} of the pictures ids that are a dashcam's clips
    (tracks.related_ids) whose track enters area, or that have no fix."""
    if area is None:
        return {}
    related = set()
    for library_id in library_ids:
        related |= tracks.related_ids(conn, config, library_id)
    wanted = sorted(set(ids) & related)
    rows = []
    for start in range(0, len(wanted), 500):
        part = wanted[start:start + 500]
        rows.extend(conn.execute("SELECT id, content_hash, media FROM images WHERE id IN (%s)"
                                 % ",".join("?" * len(part)), part).fetchall())
    found = tracks.for_rows(conn, rows)
    flags = {}
    for image_id in wanted:
        facts = found.get(image_id)
        if facts is None or any(area.contains(lat, lon) for _at, lat, lon, _speed in facts.points):
            flags[image_id] = [export.HOME_FLAG]
    return flags


def home_towns(conn, area):
    """{(town, region, country)} of the places of the pictures (and estimated places)
    in the area: towns a trip export never names."""
    towns = set()
    if area is None:
        return towns
    for row in conn.execute("SELECT lat, lon, place_city, place_region, place_country FROM "
                            "images WHERE missing_since IS NULL AND lat IS NOT NULL AND "
                            "place_city IS NOT NULL AND place_city <> ''"):
        if area.contains(row[0], row[1]):
            towns.add((row[2], row[3] or "", row[4] or ""))
    try:
        rows = conn.execute("SELECT lat, lon, place_city, place_region, place_country FROM "
                            "place_estimates WHERE lat IS NOT NULL AND place_city <> ''"
                            ).fetchall()
    except Exception:           # (no estimates yet: sqlite3.OperationalError)
        rows = []
    for row in rows:
        if area.contains(row[0], row[1]):
            towns.add((row[2], row[3] or "", row[4] or ""))
    return towns


def near_home(picture, area, towns):
    """True when a trip's picture (trips._Picture) was taken in the home area (its
    position or estimated position), or its place is a town with pictures there."""
    if area is None:
        return False
    for position in (picture.position, picture.estimated):
        if position is not None and area.contains(position[0], position[1]):
            return True
    return picture.place is not None and tuple(picture.place[:3]) in towns


def export_title(trip, options, ids, area, towns):
    """The title of a trip's export: the title option, else the owner's or the library's
    name for the trip, else one made from the places of the pictures ids (those it
    publishes) that are not near home (trips.told_by); with none of its dated pictures
    published, the trip's title as visitors see it."""
    if options.get("title"):
        return options["title"]
    pictures = [picture for picture in trip.pictures if picture.id in ids]
    if not pictures:
        return trips.told_by(trip, [picture for picture in trip.pictures
                                    if not picture.hidden and not near_home(
                                        picture, area, towns)])[2]
    start, end, _title = trips.told_by(trip, pictures)
    away = [picture for picture in pictures if not near_home(picture, area, towns)]
    title = trips.told_by(trip, away)[2]
    if not away and getattr(trip, "auto_titled", False):
        title = trips.dated_title(start, end)
    return title


def plain_words(conn, library_ids, ids):
    """{id: the extended profile's words without the location's (landmarks, the place
    guess: search_index's ex as an export without the location makes it)} of some
    pictures."""
    ids = sorted(ids)
    if not ids:
        return {}
    edits = image_edits.for_libraries(conn, library_ids)
    result = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        for row in conn.execute("SELECT id, info, described FROM images WHERE id IN (%s)"
                                % ",".join("?" * len(part)), part):
            info = search_index._load_info(row["info"]) if row["described"] else None
            edit = edits.get(row["id"])
            if not image_edits.is_empty(edit):
                info = image_edits.apply(info, edit)
            if isinstance(info, dict):
                result[row["id"]] = search_index._extended_words(info, False)
    return result


# ---------------------------------------------------------------------------
# Folders

def owner_key(built, trip):
    """The key the state of a trip's exports goes by."""
    return KEY_PREFIX + built.key(trip)


def _folder_owner(folder):
    """The owner key of the export in folder (its state's), or None."""
    try:
        marker = export.read_marker(folder) if os.path.isdir(folder) else None
    except export.ExportError:
        return None
    if not marker or not marker.get("export_id"):
        return None
    state = export.load_state(marker["export_id"])
    return state.get("library_id") if state else None


def owner_title(config, key):
    """The title of the trip whose export's state goes by key ("trip:<its key>"), or None
    when no such trip is found any more (export.check_output_dir: a folder holding the
    export of a trip still found is refused to the others). A trip that cannot be
    looked for (no index) counts as found: "a trip"."""
    try:
        conn = db.connect()
    except (db.DatabaseError, OSError):
        return "a trip"
    try:
        built = trips.build(conn, config, _library_ids(config))
    except Exception as error:      # (a broken index: refuse, rather than replace)
        print("Warning: the trips cannot be found (%s)." % error)
        return "a trip"
    finally:
        conn.close()
    for trip in built.trips:
        if KEY_PREFIX + built.key(trip) == key:
            return trip.title
    return None


def default_output_dir(config, built, trip):
    """data/exports/trip-<first day>-<name>/ (the folder of the trip's last export there,
    if any; with the trip's key when another trip's export is in that folder)."""
    key = owner_key(built, trip)
    last = export.last_export(key)
    if last and export._is_within(last["output"], common.exports_dir()):
        return last["output"]
    name = "-".join(part for part in ("trip", trip.start or "",
                                      export.library_slug(trip.title)) if part)
    folder = os.path.join(common.exports_dir(), name[:80].strip("-"))
    owner = _folder_owner(folder)
    if owner and owner != key:
        folder += "-" + (export.library_slug(built.key(trip)) or "trip")
    return folder


# ---------------------------------------------------------------------------
# The export

class _OneStage:
    """The context of one library's part of a trip's pictures: everything goes to ctx but
    the stage, which the trip export sets once for all its pictures."""

    def __init__(self, ctx):
        self._ctx = ctx

    def stage(self, name, total=0):
        pass

    def __getattr__(self, name):
        return getattr(self._ctx, name)


class _Placed:
    """A published picture's place, as trips.trip_places reads it."""

    __slots__ = ("place",)

    def __init__(self, record):
        self.place = ((record.get("ci") or "", record.get("rg") or "", record.get("co") or "",
                       record.get("cc") or "") if record.get("co") else None)


class _TripExport(export._Export):
    """The work of export_trip(): export._Export over the trip's libraries."""

    def __init__(self, ctx, conn, config, built, trip, libraries, out_dir, options, helper):
        export._Export.__init__(self, ctx, conn, config, libraries[0], out_dir, options,
                                helper)
        self.raw_helper = helper
        self.built = built
        self.trip = trip
        self.libraries = libraries
        self.library_ids = [library["id"] for library in libraries]
        self.owner_key = owner_key(built, trip)
        self.only = candidates(trip)
        self.area = home_area(built, options)
        self.towns = home_towns(conn, self.area)
        self.title = export_title(trip, options, self.only, self.area, self.towns)
        self.flags = home_flags(conn, config, self.library_ids, self.only, self.area)
        # (Positions, routes and town names: only with the location.)
        self.home = self.area if options["location"] else None
        self.at_home_ids = set()        # pictures whose town is not named (see records)
        self.withheld = set()           # clips of drives that lose their words
        self.trip_data = None

    def select(self):
        chosen = []
        found = {}
        counts = {"total": 0, "exported": 0, "by_reason": {}}
        for library in self.libraries:
            some, records, _flagged, counted = export.select_records(
                self.conn, self.config, library, self.options, only=self.only,
                extra=self.flags)
            chosen.extend(some)
            found.update(records)
            export.add_counts(counts, counted)
        published = {record["i"] for record in chosen}
        self.withheld = export.withheld_clips(found, published, self.flags)
        self.title = export_title(self.trip, self.options, published, self.area, self.towns)
        return chosen, counts

    def check_sources(self, chosen, rows, sizes):
        """As export's, for each library of the trip."""
        missing = []
        for library in self.libraries:
            if export.folder_reachable(library, self.helper):
                continue
            needing = sum(1 for record in chosen if record.get("l") == library["id"]
                          and record["i"] in rows and sizes.needs_original(rows[record["i"]]))
            if needing:
                missing.append((library, needing))
        if not missing:
            return
        kept = "thumbs/" if self.options["images"] == "none" else "images/"
        earlier = any(rel.startswith(kept) for rel in self.writer.previous)
        if sum(needing for _library, needing in missing) >= len(chosen) and not earlier:
            raise export.ExportError(
                "The folder of %s is not available (offline?), and every picture to export "
                "needs it. Connect it and export again." % ", ".join(
                    common.library_label(library) for library, _needing in missing))
        for library, needing in missing:
            self.ctx.log("The folder of %s is not available (offline?): %d of the %d pictures "
                         "need it. Where an earlier export's files are still there they are "
                         "kept; the others are left out." % (
                             common.library_label(library), needing, len(chosen)), "warn")

    def pictures(self, chosen, rows):
        """export's, one library at a time (each its own folder and previews)."""
        by_library = collections.defaultdict(list)
        for record in chosen:
            by_library[record.get("l")].append(record)
        self.ctx.stage("export", total=sum(1 for record in chosen if record["i"] in rows))
        done = []
        for library in self.libraries:
            part = export._Export(_OneStage(self.ctx), self.conn, self.config, library,
                                  self.out_dir, self.options, self.raw_helper)
            part.writer, part.state, part.thumbs = self.writer, self.state, self.thumbs
            part.export_id, part.secret = self.export_id, self.secret
            # (No RAW file holding a position near home goes either: see raw_plan.)
            part.home = self.home
            part.video_tools = self.video_tools
            part.logged = self.logged
            pictures, stopped = part.pictures(by_library.get(library["id"], []), rows)
            done.extend(pictures)
            for name in ("failed", "pano_failed", "video_failed", "raw_failed", "raw_located",
                         "kept"):
                setattr(self, name, getattr(self, name) + getattr(part, name))
            self.logged = part.logged
            self.video_tools = part.video_tools
            if stopped:
                return done, True
        return done, False

    def records(self, pictures):
        records = export._Export.records(self, pictures)
        # (The words of the drives of the near-home clips left out: see the module notes.)
        export.drop_drive_words(records, {picture.xid for picture in pictures
                                          if picture.row["id"] in self.withheld})
        if self.home is not None:
            estimates = place_estimates.for_libraries(self.conn, self.library_ids)
            for picture in pictures:
                if self.at_home(picture, estimates.get(picture.row["id"]), self.towns):
                    picture.record.pop("ci", None)
                    picture.record.pop("km", None)
                    self.at_home_ids.add(picture.row["id"])
            plain = plain_words(self.conn, self.library_ids, self.at_home_ids)
            for picture in pictures:
                if picture.row["id"] in self.at_home_ids:
                    words = plain.get(picture.row["id"])
                    if words:
                        picture.record["ex"] = words
                    else:
                        picture.record.pop("ex", None)
        self.trip_data = self.trip_view(pictures)
        return records

    def at_home(self, picture, estimate, towns):
        """True when a published picture's town must not go (see the module notes)."""
        row, record = picture.row, picture.record
        if self.home.contains(row["lat"], row["lon"]):
            return True
        if (estimate is not None and (record.get("ps") or record.get("pe"))
                and self.home.contains(estimate.get("lat"), estimate.get("lon"))):
            return True
        return (record.get("ci") or "", record.get("rg") or "", record.get("co") or "") in towns

    def trip_view(self, pictures):
        """data/trips.js's {enabled, best_per_day, trips: [the trip]} (see the module notes)."""
        by_id = {picture.row["id"]: picture for picture in pictures}
        location = self.options["location"]
        days = collections.OrderedDict()
        members = {}                    # database id -> the trip's picture (trips._Picture)
        for item in self.trip.pictures:
            if item.id in by_id:
                days.setdefault(item.day, []).append(by_id[item.id])
                members[item.id] = item
        other = [by_id[item.id] for item in self.trip.other if item.id in by_id]
        members.update((item.id, item) for item in self.trip.other if item.id in by_id)
        groups = [[members[picture.row["id"]] for picture in shown]
                  for shown in list(days.values()) + [other]]
        units = trips.look_units(self.conn, self.config, groups)

        def unit_xids(found):
            return [[by_id[image_id].xid for image_id in ids] for ids in found]
        day_list = []
        for (day, shown), found in zip(days.items(), units):
            entry = {"date": day, "ids": [picture.xid for picture in shown],
                     "gaps": trips.gap_classes([picture.row["taken_at"] for picture in shown]),
                     "places": []}
            if location:
                entry["places"] = [place["name"] for place in trips.trip_places(
                    self.placed(shown), limit=6)]
            if found:
                entry["looks"] = unit_xids(found)
            day_list.append(entry)
        published = [picture for shown in days.values() for picture in shown] + other
        start, end, _title = trips.told_by(self.trip, [members[picture.row["id"]]
                                                       for shown in days.values()
                                                       for picture in shown])
        view = {"id": EXPORT_TRIP_ID, "title": self.title, "kind": self.trip.kind,
                "start": start, "end": end,
                "libraries": [self.export_id], "count": len(published),
                "videos": sum(1 for picture in published if picture.record.get("mv")),
                "places": trips.trip_places(self.placed(
                    [picture for shown in days.values() for picture in shown]))
                if location else [],
                "days": day_list, "other": [picture.xid for picture in other]}
        if units and units[-1]:
            view["other_looks"] = unit_xids(units[-1])
        return {"enabled": True, "best_per_day": trips.settings(self.config)["best_per_day"],
                "trips": [view]}

    def placed(self, pictures):
        """_Placed of published pictures for the places of the trip (home is no place it
        went to: those near home are left out, and so are those taken from a plane)."""
        flying = {item.id for item in self.trip.pictures if item.flying}
        return [_Placed(picture.record) for picture in pictures
                if picture.row["id"] not in self.at_home_ids
                and picture.row["id"] not in flying]

    def data_files(self, pictures, records, capabilities):
        files, manifest = export._Export.data_files(self, pictures, records, capabilities)
        text = "SEARCH_DATA.trips(%s);\n" % export._js_json(
            self.trip_data or self.trip_view(pictures))
        files["data/trips.js"] = text
        manifest["trips"] = "trips.js"
        manifest["versions"]["trips.js"] = export._version(text.encode("ascii"))
        return files, manifest

    def assets(self):
        return ASSETS

    def pages(self):
        return PAGES + (export.MAP_PAGES if self.options["location"] else ())

    def page_settings(self):
        return {"trip": EXPORT_TRIP_ID, "tripsPage": TRIP_PAGE, "searchPage": SEARCH_PAGE}

    def summary(self, counts, pictures, removed=0, zip_file=None, stopped=False):
        result = export._Export.summary(self, counts, pictures, removed=removed,
                                        zip_file=zip_file, stopped=stopped)
        result.update(library_id=None, trip=self.trip.id, title=self.title)
        return result


def export_trip(ctx, conn, config, trip_id, out_dir=None, options=None, helper=None):
    """Export one trip as a static website; returns a summary dict (export_library's, with
    "trip" and "title"). options override the options of the trip's last export (see
    trip_options). Raises export.ExportError (OptionsError) when the export cannot be
    made."""
    if ctx is None:
        ctx = jobs.JobContext("export")
    lock = jobs.WorkerLock(os.path.join(common.data_dir(), export.LOCK_FILE))
    if not lock.acquire("export of the trip %s" % trip_id):
        raise export.ExportError("Another export is running: %s. Try again when it has "
                                 "finished." % (lock.holder() or "another process"))
    try:
        built, trip, libraries = load(conn, config, trip_id)
        checked = trip_options(built.export_options(trip), options)
        if checked["location"]:
            # (As a library export: positions the GPS check sets aside never go, and
            # the local times follow it.)
            if gps_check.run_safely(conn, config, ctx) is not None:
                local_times.after_check_safely(conn, config, ctx)
                built, trip, libraries = load(conn, config, trip_id)
        if not libraries:
            raise export.ExportError("The trip %s has no pictures to publish." % trip.title)
        out_dir = os.path.abspath(out_dir) if out_dir else default_output_dir(config, built,
                                                                              trip)
        job = _TripExport(ctx, conn, config, built, trip, libraries, out_dir, checked, helper)
        summary = job.run()
    finally:
        lock.release()
    if not summary.get("stopped"):
        try:
            trips.record_export(conn, config, _library_ids(config), trip.id)
        except (trips.TripError, OSError) as error:
            ctx.log("The trip's export could not be noted in %s: %s" % (trips.path(), error),
                    "warn")
    return summary


def preview_trip(conn, config, trip_id, options=None, reachable=None):
    """What an export of a trip would hold (export.preview_export's answer), with "trip":
    {id, title, start, end, count, videos}, "home": whether home was found, and the
    trip's title in counts (the review's headline names it). options override the
    trip's last export's; reachable as export.preview_pictures'."""
    built, trip, libraries = load(conn, config, trip_id)
    checked = trip_options(built.export_options(trip), options)
    only = candidates(trip)
    area = home_area(built, checked)
    library_ids = [library["id"] for library in libraries]
    result = export.preview_pictures(
        conn, config, libraries, checked, only=only,
        extra=home_flags(conn, config, library_ids, only, area),
        home=area if checked["location"] else None, reachable=reachable,
        assets=ASSETS, with_ids=True)
    published = set(result.pop("ids"))
    result["trip"] = trip_summary(trip)
    result["counts"]["trip"] = {"title": export_title(trip, checked, published, area,
                                                      home_towns(conn, area))}
    result["home"] = built.home is not None
    return result


def trip_summary(trip):
    """{id, title, start, end, count, videos} of a trip (its pictures visitors may see)."""
    shown = trips.trip_view(trip)
    return {key: shown[key] for key in ("id", "title", "start", "end", "count", "videos")}


def export_info(conn, config, trip_id):
    """A trip's export for the export dialog: {"trip": trip_summary, "options" (the last
    export's, every default filled in), "default_output", "last" (export.last_export, or
    None), "meaning" {available, reason}, "home" (whether home was found), "libraries"
    (the names of the libraries its pictures are in)}."""
    built, trip, libraries = load(conn, config, trip_id)
    options = trip_options(built.export_options(trip))
    status = export.meaning_status(config, options)
    return {"trip": trip_summary(trip), "options": options,
            "default_output": default_output_dir(config, built, trip),
            "last": export.last_export(owner_key(built, trip)),
            "meaning": {"available": status["available"], "reason": status["reason"]},
            "home": built.home is not None,
            "libraries": [common.library_label(library) for library in libraries]}


def save_options(conn, config, trip_id, options):
    """Keep options (checked: trip_options) as a trip's export options; returns them."""
    built, trip, _libraries = load(conn, config, trip_id)
    checked = trip_options(built.export_options(trip), options)
    trips.record_export(conn, config, _library_ids(config), trip.id, options_to_save(checked))
    return checked


# ---------------------------------------------------------------------------
# Command line (export.py --trip)

def main_cli(args, cli_options):
    """export.py --trip: export a trip with the command line's options (cli_options(args,
    current options) makes them). Returns the summary; SystemExit on errors."""
    try:
        db.init()
        config = common.load_config()
        conn = db.connect()
        try:
            built, trip, _libraries = load(conn, config, args.trip)
            options = cli_options(args, trip_options(built.export_options(trip)))
            if getattr(args, "home_km", None) is not None:
                options["home_km"] = args.home_km
            ctx = jobs.ConsoleContext("export")
            with ctx.stop_on_ctrl_c():
                summary = export_trip(ctx, conn, config, trip.id, args.out, options,
                                      helper=common.load_helper(config.get("helper", "")))
        finally:
            conn.close()
    except KeyboardInterrupt:
        raise SystemExit("\nStopped (Ctrl+C). Run the same command again to finish the export.")
    except (export.ExportError, common.ConfigError, db.DatabaseError) as error:
        raise SystemExit("Error: %s" % error)
    except OSError as error:
        raise SystemExit("Error: %s" % export.os_error_text(error))
    status = export.report(summary)
    if status:
        raise SystemExit(status)
    return summary
