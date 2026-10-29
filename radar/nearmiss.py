#!/usr/bin/env python3
"""
nearmiss.py - which titles did the title gate throw away?

A posting is dropped in pass 1 when no rule in rules/titles.json matches its
title. That drop is silent: tier-0 postings are never hydrated, never scored,
never printed. So the one question that mattered - "are we missing relevant
jobs?" - could only ever be answered by spotting something by hand.

Full sweeps now record the rejects that still look like infra work, and this
reads them back. The list is the evidence for editing rules/titles.json:
title_match() returns the MAX tier among matching rules, so a rule added there
can only ever raise a posting's tier, never lower one.

    python3 nearmiss.py                  # last 14 days, grouped by title
    python3 nearmiss.py --days 60 --by company
    python3 nearmiss.py --urls            # with links, to go and look
    python3 nearmiss.py --prune 180       # bound the table
"""
import argparse
import collections
import os
import sqlite3
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr                                             # noqa: E402


def rows(conn, days):
    cut = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    return list(conn.execute(
        "SELECT title, company, location, url, first_seen FROM nearmiss "
        "WHERE first_seen >= ? ORDER BY first_seen DESC", (cut,)))


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--by", choices=("title", "company", "none"),
                    default="title", help="group the report (default: title)")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--urls", action="store_true", help="print the job links")
    ap.add_argument("--prune", type=int, metavar="DAYS",
                    help="delete rows older than DAYS and exit")
    a = ap.parse_args()

    if not os.path.exists(jr.DB):
        print(f"  no database at {jr.DB} - run a full sweep first")
        return 0
    conn = sqlite3.connect(jr.DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("SELECT 1 FROM nearmiss LIMIT 1")
    except sqlite3.OperationalError:
        print("  no `nearmiss` table yet - it is created by the next full "
              "sweep (the :07/:37 lane), not by the 5-minute fast lane")
        return 0

    if a.prune is not None:
        cut = (datetime.utcnow()
               - timedelta(days=a.prune)).isoformat(timespec="seconds")
        # Through db_write, not a bare execute: five processes share this file
        # and a maintenance read-out must never be the writer that kills a
        # sweep holding the lock.
        n = [0]

        def _go():
            n[0] = conn.execute("DELETE FROM nearmiss WHERE first_seen < ?",
                                (cut,)).rowcount
        if jr.db_write(conn, _go, label="nearmiss prune"):
            print(f"  deleted {n[0]} rows older than {a.prune} days")
        return 0

    rs = rows(conn, a.days)
    total = conn.execute("SELECT COUNT(*) FROM nearmiss").fetchone()[0]
    if not rs:
        print(f"  no near-miss titles in the last {a.days} days "
              f"({total} in the table overall)")
        return 0

    print(f"  {len(rs)} rejected titles in the last {a.days} days "
          f"that still look like infra work  ({total} all time)")
    print("  these were location-eligible and dropped ONLY by the title rules\n")

    if a.by == "none":
        for title, co, loc, url, seen in rs[:a.limit]:
            print(f"  {seen[:10]}  {co[:22]:22}  {title[:52]}")
            if a.urls:
                print(f"              {url}")
        return 0

    key = 0 if a.by == "title" else 1
    groups = collections.Counter(r[key].strip() for r in rs)
    first = {}
    for r in rs:
        first.setdefault(r[key].strip(), r)
    width = max((len(k) for k in groups), default=10)
    width = min(width, 56)
    print(f"  {'count':>5}  {a.by}")
    for name, n in groups.most_common(a.limit):
        r = first[name]
        extra = r[1] if a.by == "title" else r[0]
        print(f"  {n:>5}  {name[:56]:{width}}  {extra[:26]}")
        if a.urls:
            print(f"         {r[3]}")
    if len(groups) > a.limit:
        print(f"\n  ... and {len(groups) - a.limit} more "
              f"(raise --limit to see them)")
    print(f"\n  To act on one: add a rule to rules/titles.json. A new rule can "
          f"only raise\n  a posting's tier, never lower one, so the list is "
          f"safe to extend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
