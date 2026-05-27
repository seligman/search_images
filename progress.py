#!/usr/bin/env python3
"""Show current progress of described images in local database."""
import common
from scan import PROMPT_VER
from collections import defaultdict
import argparse

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
        chars = "\u2500\u2502\u253C"
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


def show_counts():
    conn = common.open_db()
    totals = defaultdict(int)

    sql = ("SELECT library, COUNT(*) AS count, "
           "SUM(described) AS described, "
           "SUM(CASE WHEN described = 1 AND prompt_ver = ? THEN 1 ELSE 0 END) AS current "
           "FROM images GROUP BY library;")

    grid = Grid(["library", "done", f"at v={PROMPT_VER}", "left", "total", "done"])

    for row in conn.execute(sql, (PROMPT_VER,)).fetchall():
        grid.add_row(
            row['library'],
            row['described'],
            row['current'],
            row['count'] - row['current'],
            row['count'],
            "-" if row['current'] == 0 else f"{row['current'] / row['count'] * 100:.2f}%", 
        )
        for key in ['current', 'count', 'described']:
            totals[key] += row[key]

    grid.add_row("break")
    grid.add_row(
        "  (all)",
        totals['described'],
        totals['current'],
        totals['count'] - totals['current'],
        totals['count'],
        f"{totals['current'] / totals['count'] * 100:.2f}%",
    )
    grid.print()


def show_files():
    conn = common.open_db()
    fresh = conn.execute("SELECT * FROM images WHERE described = 0").fetchall()
    stale = conn.execute(
        "SELECT * FROM images WHERE described = 1 AND prompt_ver < ?",
        (PROMPT_VER,)).fetchall()

    for i, cur in enumerate(fresh + stale, 1):
        print(f"{i:7,}: {cur['library']}: {cur['path']}")


def main():
    parser = argparse.ArgumentParser(
        description="Show progress on the current work.",
        # epilog="Run '%(prog)s <command> --help' for options on a specific command.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("counts", help="Show overall counts.")

    sub.add_parser("files", help="Show a list of images that need processing.")

    args = parser.parse_args()

    if args.command == "counts":
        show_counts()
    elif args.command == "files":
        show_files()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
