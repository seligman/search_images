#!/usr/bin/env python3
"""Show how far describing has got in the local database, per library."""
import argparse
import sys

import common
import db
import prompts
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


COUNT_KEYS = ("total", "described", "stale", "failed", "gave_up", "missing", "hidden")
MAX_FAILURES = common.MAX_DESCRIBE_FAILURES


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
    counts = db.library_counts(conn, max_failures=MAX_FAILURES,
                               stale_sql=prompts.stale_sql(config))
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


def count_cells(counts):
    """The table cells for one library's counts (see show_counts)."""
    current = counts["described"] - counts["stale"]
    return [
        counts["described"],
        current,
        counts["stale"],
        counts["total"] - counts["described"] - counts["gave_up"],
        counts["failed"],
        counts["gave_up"],
        counts["missing"],
        counts["total"],
        percent(current, counts["total"]),
    ]


def show_counts(conn=None, config=None):
    """Print a table of described / pending images per library.

    "older" are descriptions that are out of date: from an older prompt, or
    of a video and talking about its frames (described again only with
    scan.py --redescribe-stale); "left" is what the next run describes
    (images without a description, but not those it gave up on); "failed"
    counts undescribed images with a failed attempt, and "gave up" those
    that failed MAX_FAILURES times (retried with --retry-failed).
    """
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

    grid = Grid(["library", "described", "current", "older", "left", "failed",
                 "gave up", "missing", "total", "done"])
    totals = dict.fromkeys(COUNT_KEYS, 0)
    for label, counts in rows:
        grid.add_row(label, *count_cells(counts))
        for key in COUNT_KEYS:
            totals[key] += counts[key]
    grid.add_row("break")
    grid.add_row("  (all)", *count_cells(totals))
    grid.print()


def pending_rows(conn, config):
    """Present images of enabled libraries that still need a (current)
    description, as (library label, row): new ones first, then those
    given up on, then those that are out of date (prompts.stale_sql; the
    row's "older" is 1 for an older prompt). Hidden images are included:
    they are described too."""
    labels = {library["id"]: common.library_label(library)
              for library in common.enabled_libraries(config) if library.get("id")}
    if not labels:
        return []
    rows = conn.execute(
        "SELECT library_id, path, described, describe_attempts, describe_error, "
        "%s AS older FROM images WHERE missing_since IS NULL "
        "AND (described = 0 OR %s) AND library_id IN (%s) "
        "ORDER BY described, describe_attempts >= ?, id"
        % (prompts.STALE_SQL, prompts.stale_sql(config), ",".join("?" * len(labels))),
        list(labels) + [MAX_FAILURES]).fetchall()
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
        if row["described"]:
            line += "  (%s; run scan.py --redescribe-stale)" % (
                "older prompt" if row["older"] else "the description talks about the frames")
        elif row["describe_attempts"] >= MAX_FAILURES:
            line += (f"  (skipped after {row['describe_attempts']} failures: "
                     f"{row['describe_error']}; run scan.py --retry-failed)")
        elif row["describe_attempts"]:
            line += f"  (failed {row['describe_attempts']}x: {row['describe_error']})"
        print(line)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Show progress on the current work. With no command, shows the counts.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("counts", help="Show overall counts (the default).")

    sub.add_parser("files", help="Show a list of images that need processing.")

    args = parser.parse_args()
    if args.command is None:
        args.command = "counts"

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
