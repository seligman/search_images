#!/usr/bin/env python3
"""Show current progress of described images in local database.
"""

import common

def main():
    conn = common.open_db()
    total_count, total_described = 0, 0
    for row in conn.execute("SELECT library, COUNT(*) AS count, SUM(described) AS described FROM images GROUP BY library;").fetchall():
        print("%-20s | %5d | %5d | %6.2f%%" % (row['library'], row['described'], row['count'], row['described'] / row['count'] * 100))
        total_count += row['count']
        total_described += row['described']
    print("%-20s | %5d | %5d | %6.2f%%" % ('  (all)', total_described, total_count, total_described / total_count * 100))


if __name__ == "__main__":
    main()
