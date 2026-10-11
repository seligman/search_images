#!/usr/bin/env python3
"""Show how far describing has got in the local database, per library."""
import argparse
import sys

import common
import db
from prompts import PROMPT_VER


class Grid:
    def __init__(self, headers):
        self.headers = [str(h) for h in headers]
        self.rows = []

    def add_row(self, *values):
        if len(values) == 1:
            self.rows.append(values)
        else:
            if len(values) != len(self.headers):
                raise ValueError(f"row has {len(values)} values, expected {len(self.headers)}")
            self.rows.append([("-" if v == 0 else f"{v:,}") if isinstance(v, int) else f"{v}" for v in values])

    def render(self):
        # chars = "-|+"
        chars = "─│┼"
        widths = [len(h) for h in self.headers]
        for row in self.rows:
            for i, val in enumerate(row):
                if len(val) > widths[i]:
                    widths[i] = len(val)

        sep = " " + chars[1] + " "
        lines = [" " + sep.join(h.center(w) for h, w in zip(self.headers, widths))]
        break_line = chars[0] + (chars[0] + chars[2] + chars[0]).join(chars[0] * w for w in widths) + chars[0]
        lines.append(break_line)
        for row in self.rows:
            if len(row) == 1:
                if row[0] == "break":
                    lines.append(break_line)
                else:
                    raise Exception("Unknown row type")
            else:
                parts = [row[0].ljust(widths[0])]
                parts.extend(val.rjust(w) for val, w in zip(row[1:], widths[1:]))
                lines.append(" " + sep.join(parts))
        return "\n".join(lines)

    def print(self):
        print(self.render())


COUNT_KEYS = ("total", "described", "stale", "failed", "missing", "hidden")


def percent(part, whole):
    """part/whole as "12.34%", or "-" when whole is 0."""
    if not whole:
        return "-"
    return f"{part / whole * 100:.2f}%"


def library_rows(conn, config):
    """[(label, counts)] for each configured library, then each orphan.

    counts is a db.library_counts() entry. Orphans (libraries in the database
    that are no longer in config.json) are listed only if they hold images.
    """
    counts = db.library_counts(conn, PROMPT_VER)
    names = db.library_names(conn)
    empty = dict.fromkeys(COUNT_KEYS, 0)
    rows = []
    configured = set()
    for library in config.get("libraries", []):
        library_id = library.get("id")
        configured.add(library_id)
        label = common.library_label(library)
        if not library.get("enabled", True):
            label += " (disabled)"
        rows.append((label, counts.get(library_id, empty)))
    for library_id in sorted(set(counts) - configured):
        entry = counts[library_id]
        if entry["total"] or entry["missing"]:
            rows.append(("%s (orphan)" % names.get(library_id, library_id), entry))
    return rows


def show_counts(conn=None, config=None):
    """Print a table of described / pending images per library."""
    if config is None:
        config = common.load_config()
    close_after = conn is None
    if close_after:
        conn = db.connect()
    try:
        rows = library_rows(conn, config)
    finally:
        if close_after:
            conn.close()

    grid = Grid(["library", "described", f"at v={PROMPT_VER}", "left", "failed",
                 "missing", "total", "done"])
    totals = dict.fromkeys(("described", "current", "failed", "missing", "total"), 0)
    for label, counts in rows:
        current = counts["described"] - counts["stale"]
        grid.add_row(
            label,
            counts["described"],
            current,
            counts["total"] - current,
            counts["failed"],
            counts["missing"],
            counts["total"],
            percent(current, counts["total"]),
        )
        totals["described"] += counts["described"]
        totals["current"] += current
        for key in ("failed", "missing", "total"):
            totals[key] += counts[key]

    grid.add_row("break")
    grid.add_row(
        "  (all)",
        totals["described"],
        totals["current"],
        totals["total"] - totals["current"],
        totals["failed"],
        totals["missing"],
        totals["total"],
        percent(totals["current"], totals["total"]),
    )
    grid.print()


def pending_rows(conn, config):
    """Present, non-hidden images of enabled libraries that still need a
    (current) description, as (library label, row): new ones first."""
    labels = {library["id"]: common.library_label(library)
              for library in common.enabled_libraries(config) if library.get("id")}
    if not labels:
        return []
    rows = conn.execute(
        "SELECT library_id, path, described, describe_attempts, describe_error "
        "FROM images WHERE missing_since IS NULL AND hidden = 0 "
        "AND (described = 0 OR prompt_ver < ?) AND library_id IN (%s) "
        "ORDER BY described, id" % ",".join("?" * len(labels)),
        [PROMPT_VER] + list(labels)).fetchall()
    return [(labels[row["library_id"]], row) for row in rows]


def show_files(conn=None, config=None):
    """Print every image that still needs describing."""
    if config is None:
        config = common.load_config()
    close_after = conn is None
    if close_after:
        conn = db.connect()
    try:
        pending = pending_rows(conn, config)
    finally:
        if close_after:
            conn.close()
    for i, (label, row) in enumerate(pending, 1):
        line = f"{i:7,}: {label}: {row['path']}"
        if not row["described"] and row["describe_attempts"]:
            line += f"  (failed {row['describe_attempts']}x: {row['describe_error']})"
        print(line)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Show progress on the current work.",
        # epilog="Run '%(prog)s <command> --help' for options on a specific command.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("counts", help="Show overall counts.")

    sub.add_parser("files", help="Show a list of images that need processing.")

    args = parser.parse_args()
    if args.command not in ("counts", "files"):
        parser.print_help()
        return

    try:
        db.init()
        if args.command == "counts":
            show_counts()
        else:
            show_files()
    except (db.DatabaseError, common.ConfigError) as error:
        raise SystemExit("Error: %s" % error)


if __name__ == "__main__":
    main()
