#!/usr/bin/env python3
"""Download the GeoNames place names and give pictures with a GPS position their place.

The search page then finds pictures by the town, region or country they
were taken in (see places.py). The Manage page's "Download place names"
does the same from the browser.

    python make_places.py                  download the files (unless they are
                                           in data/places already), build the
                                           place index, name the pictures' places
    python make_places.py --download       download the files again first
    python make_places.py --from DIR       build from files downloaded by hand
                                           into DIR: cities1000.zip (or
                                           cities1000.txt), admin1CodesASCII.txt
                                           and countryInfo.txt from
                                           https://download.geonames.org/export/dump/
    python make_places.py --no-label       only build the place index
    python make_places.py --redo           look up every picture's place again
    python make_places.py --lookup LAT LON print the place of a position

The pictures of every library (enabled or not) get their place, as with
the Manage page's buttons. The files (about 10 MB) and the index
(data/places/places.db) stay in the data folder; nothing else goes over
the network. The place names are GeoNames data under the Creative
Commons Attribution 4.0 licence. Naming the pictures' places takes the
job lock, like scan.py.
"""

import argparse
import os
import sys

import common
import db
import jobs
import places


def _print_place(lat, lon):
    index = places.shared()
    if index is None:
        print("There is no place index yet (%s); run make_places.py first."
              % places.places_db_path())
        return 1
    hit = index.nearest(lat, lon)
    if hit is None:
        print("No town within %d km." % places.MAX_KM)
        return 0
    town = index.town(hit[0])
    columns = index.columns(lat, lon)
    print("Nearest town: %s, %s, %s (%s), %.1f km away"
          % (town["name"], town["region"] or "-", town["country"], town["cc"], hit[1]))
    shown = ", ".join(part for part in (columns["place_city"], columns["place_region"],
                                        columns["place_country"]) if part)
    print("Place: %s" % (shown or "(none)"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Download GeoNames place names and name the places of pictures "
                    "with a GPS position.")
    parser.add_argument("--download", action="store_true",
                        help="Download the GeoNames files again even when they are there.")
    parser.add_argument("--from", dest="source", metavar="DIR",
                        help="Build from GeoNames files in this folder; nothing is downloaded.")
    parser.add_argument("--no-label", action="store_true",
                        help="Only build the place index; leave the pictures alone.")
    parser.add_argument("--redo", action="store_true",
                        help="Look up the place of every picture again, not only of new ones.")
    parser.add_argument("--lookup", nargs=2, type=float, metavar=("LAT", "LON"),
                        help="Print the place of a position and exit.")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    if args.lookup:
        return _print_place(*args.lookup)
    if args.source and args.download:
        print("--from and --download do not go together.")
        return 2
    ctx = jobs.ConsoleContext("places")
    rebuilt = False
    try:
        if args.source:
            folder = os.path.abspath(os.path.expanduser(args.source))
            places.build(folder, ctx=ctx, source=folder)
            rebuilt = True
        else:
            if args.download or not places.have_files():
                # (The old files and index stay unless the new ones are good.)
                places.update(ctx)
                rebuilt = True
            elif not os.path.isfile(places.places_db_path()):
                places.build(ctx=ctx)
                rebuilt = True
            else:
                # (A place index of another version is built again.)
                rebuilt = places.rebuild_if_needed(ctx) is not None
    except places.PlacesError as error:
        print("Error: %s" % error)
        return 1
    if args.no_label:
        return 0
    try:
        config = common.load_config()
    except common.ConfigError as error:
        print("Error: %s" % error)
        return 2
    lock = jobs.WorkerLock()
    if not lock.acquire("make_places.py"):
        print("Another job is running: %s. Try again when it has finished (or run "
              "make_places.py --no-label now)." % (lock.holder() or "another process"))
        return 1
    try:
        db.init(config, log=ctx.log)
        conn = db.connect()
        try:
            with ctx.stop_on_ctrl_c():
                summary = places.label_pictures(conn, ctx, redo=args.redo or rebuilt)
        finally:
            conn.close()
    except (db.DatabaseError, common.ConfigError) as error:
        print("Error: %s" % error)
        return 2
    finally:
        lock.release()
    if summary is None:
        print("The place index could not be read; see the warning above.")
        return 1
    if summary["aborted"]:
        ctx.log("Stopped as asked; run make_places.py again to continue.")
    elif not summary["checked"]:
        ctx.log("Every picture with a GPS position has its place already.")
    else:
        ctx.log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
