#!/usr/bin/env python3
"""Show current progress of described images in local database.
"""

import common
from scan import PROMPT_VER

def main():
    conn = common.open_db()
    total_count, total_described, total_current = 0, 0, 0
    sql = ("SELECT library, COUNT(*) AS count, "
           "SUM(described) AS described, "
           "SUM(CASE WHEN described = 1 AND prompt_ver = ? THEN 1 ELSE 0 END) AS current "
           "FROM images GROUP BY library;")
    head_fmt = "%-20s | %9s | %8s | %6s | %8s"
    row_fmt = "%-20s | %9d | %8d | %6d | %7.2f%%"
    print(head_fmt % ("  library", "described", "at v=%d" % PROMPT_VER, "total", "done"))
    print(head_fmt.replace(" | ", "-+-") % ("-" * 20, "-" * 9, "-" * 8, "-" * 6, "-" * 8))
    for row in conn.execute(sql, (PROMPT_VER,)).fetchall():
        print(row_fmt % (
            row['library'], row['described'], row['current'], row['count'],
            row['described'] / row['count'] * 100))
        total_count += row['count']
        total_described += row['described']
        total_current += row['current']
    print(row_fmt % (
        '  (all)', total_described, total_current, total_count,
        total_described / total_count * 100))


if __name__ == "__main__":
    main()
