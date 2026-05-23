#!/usr/bin/env python3
"""Show current progress of described images in local database."""
import common
from scan import PROMPT_VER


class Grid:
    def __init__(self, headers):
        self.headers = [str(h) for h in headers]
        self.rows = []

    def add_row(self, *values):
        if len(values) != len(self.headers):
            raise ValueError("row has %d values, expected %d" % (len(values), len(self.headers)))
        self.rows.append([str(v) for v in values])

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
        lines.append(chars[0] + (chars[0] + chars[2] + chars[0]).join(chars[0] * w for w in widths) + chars[0])
        for row in self.rows:
            parts = [row[0].ljust(widths[0])]
            parts.extend(val.rjust(w) for val, w in zip(row[1:], widths[1:]))
            lines.append(" " + sep.join(parts))
        return "\n".join(lines)

    def print(self):
        print(self.render())


def main():
    conn = common.open_db()
    total_count = total_described = total_current = 0
    sql = ("SELECT library, COUNT(*) AS count, "
           "SUM(described) AS described, "
           "SUM(CASE WHEN described = 1 AND prompt_ver = ? THEN 1 ELSE 0 END) AS current "
           "FROM images GROUP BY library;")

    grid = Grid(["library", "described", "at v=%d" % PROMPT_VER, "left", "total", "done"])

    for row in conn.execute(sql, (PROMPT_VER,)).fetchall():
        left = row['count'] - row['described']
        grid.add_row(
            row['library'],
            row['described'],
            row['current'],
            left,
            row['count'],
            "%.2f%%" % (row['described'] / row['count'] * 100),
        )
        total_count += row['count']
        total_described += row['described']
        total_current += row['current']

    grid.add_row(
        "  (all)",
        total_described,
        total_current,
        total_count - total_described,
        total_count,
        "%.2f%%" % (total_described / total_count * 100),
    )
    grid.print()


if __name__ == "__main__":
    main()
