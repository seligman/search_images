#!/usr/bin/env python3
"""Download the GeoNames place names and give pictures with a GPS position their place.

The search page then finds pictures by the town, region or country they
were taken in (see places.py). The Manage page's "Download place names"
does the same from the browser.

    python make_places.py --download       download the files, build the place
                                           index, name the pictures' places
    python make_places.py                  name the places of the pictures not
                                           looked up yet (nothing is downloaded;
                                           the place index is built again from
                                           the files in data/places when needed)
    python make_places.py --from DIR       build from files downloaded by hand
                                           into DIR: cities1000.zip (or
                                           cities1000.txt), admin1CodesASCII.txt
                                           and countryInfo.txt from
                                           https://download.geonames.org/export/dump/
    python make_places.py --no-label       only build the place index
    python make_places.py --redo           look up every picture's place again
    python make_places.py --lookup LAT LON print the place of a position

The pictures of every library (enabled or not) get their place, as with
the Manage page's buttons; then the GPS check and the local times follow
(as after a scan). The files (about 10 MB) and the index
(data/places/places.db) stay in the data folder; only --download goes
over the network. The place names are GeoNames data under the Creative
Commons Attribution 4.0 licence. It takes the job lock, like scan.py
(but with --no-label).
"""

import argparse
import os
import sys

import common
import db
import gps_check
import jobs
import local_times
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
    if town["zone"]:
        print("Time zone: %s" % town["zone"])
    shown = ", ".join(part for part in (columns["place_city"], columns["place_region"],
                                        columns["place_country"]) if part)
    print("Place: %s" % (shown or "(none)"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Download GeoNames place names and name the places of pictures "
                    "with a GPS position.")
    parser.add_argument("--download", action="store_true",
                        help="Download the GeoNames files (again, when they are there) and "
                             "build the place index from them. Without it nothing is "
                             "downloaded.")
    parser.add_argument("--from", dest="source", metavar="DIR",
                        help="Build from GeoNames files in this folder; nothing is downloaded.")
    parser.add_argument("--no-label", action="store_true",
                        help="Only build the place index; leave the pictures alone.")
    parser.add_argument("--redo", action="store_true",
                        help="Look up the place of every picture again, not only of new ones.")
    parser.add_argument("--lookup", nargs=2, type=float, metavar=("LAT", "LON"),
                        help="Print the place of a position and exit.")
    jobs.add_settings_base_argument(parser)
    args = parser.parse_args(argv)
    jobs.use_settings_base(parser, args)
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
    jobs.log_tool_command(ctx, "make_places.py", argv)
    if args.no_label:
        # (Only the place index: no job lock, as the lock message below suggests.)
        return _build_index(args, ctx)[0]
    missing = jobs.missing_settings()
    if missing:
        print(missing)
        return 2
    try:
        config = common.load_config()
    except common.ConfigError as error:
        print("Error: %s" % error)
        return 2
    # (As the places job: the lock is held while the index is built too.)
    lock = jobs.WorkerLock()
    if not lock.acquire("make_places.py"):
        print("Another job is running: %s. Try again when it has finished (or run "
              "make_places.py --no-label now)." % (lock.holder() or "another process"))
        return 1
    try:
        code, rebuilt = _build_index(args, ctx)
        if code:
            return code
        db.init(config, log=ctx.log)
        conn = db.connect()
        try:
            with ctx.stop_on_ctrl_c():
                summary = places.label_pictures(conn, ctx, redo=args.redo or rebuilt)
                # (As the places job: the positions are checked, and the local
                # times follow the time zones of the places.)
                gps_check.run_safely(conn, config, ctx)
                local_times.run_safely(conn, config, ctx)
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


def _build_index(args, ctx):
    """Build the place index as the switches say, as the places job does: (exit code, True
    when it was built anew). --from DIR: from the files there; --download: download the
    files first; otherwise nothing is downloaded: a place index of another version is
    built again from the downloaded files (one that is fine is used as it is), a missing
    one too, and with neither the index nor the files there it says so (exit code 1)."""
    try:
        if args.source:
            folder = os.path.abspath(os.path.expanduser(args.source))
            places.build(folder, ctx=ctx, source=folder)
            return 0, True
        if args.download:
            # (The old files and index stay unless the new ones are good.)
            places.update(ctx)
            return 0, True
        if os.path.isfile(places.places_db_path()):
            return 0, places.rebuild_if_needed(ctx) is not None
        if places.have_files():
            places.build(ctx=ctx)
            return 0, True
    except places.PlacesError as error:
        print("Error: %s" % error)
        return 1, False
    print("There are no place names yet: download them with python make_places.py "
          "--download (about 10 MB from download.geonames.org), or build them from the files "
          "downloaded by hand: --from DIR.")
    return 1, False


if __name__ == "__main__":
    sys.exit(main())
