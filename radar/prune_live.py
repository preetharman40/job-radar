#!/usr/bin/env python3
"""
prune_live.py - bound the `live` table, which had no retention at all.

`live` records every posting URL any board returned, with the last sweep that
saw it. job_radar.py WRITES it on every full sweep and never reads it back -
verified: zero `SELECT ... FROM live` in job_radar.py - so it grew ~1,200 rows a
day forever. At 100,311 rows it was the largest thing in a 19MB database, and
the daily backup copies that file 30 times.

Pruning it cannot cause a re-notification. "Is this posting new?" is answered by
the `seen` table through an atomic INSERT OR IGNORE, which is a different table
and is NOT pruned. The only consumer of `live` is inbox.py, which uses it to
suppress an alert-mail row the ATS poller already found; that check also falls
back to a normalised title+company match against `seen`, so a pruned URL
degrades to the weaker-but-still-present comparison rather than to nothing.

Retention is therefore set by what inbox.py needs, not by sentiment. It runs
once a day with a 2-day lookback, so it cares about the poller's recent view -
days, not months. The default below is 60 days, which is a wide margin on that
and bounds the table at roughly 72,000 rows instead of ~440,000 after a year.
The cost of pruning too hard is one duplicated line in a daily email report; the
cost of not pruning is a database that grows without limit. Those are not
symmetric.

    python3 prune_live.py              # dry run - counts, deletes nothing
    python3 prune_live.py --yes        # delete
    python3 prune_live.py --yes --vacuum   # delete and reclaim the file space
"""
import argparse
import datetime
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "seen.sqlite3")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60,
                    help="keep rows seen within this many days (default 60)")
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument("--vacuum", action="store_true",
                    help="VACUUM afterwards to shrink the file (needs --yes)")
    a = ap.parse_args()

    if a.days < 14:
        print(f"refusing --days {a.days}: inbox.py runs daily with a 2-day "
              f"lookback and compares against the poller's recent view. "
              f"Under two weeks starts eroding that for no space worth having.",
              file=sys.stderr)
        return 2

    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=a.days)).isoformat(timespec="seconds")
    size_before = os.path.getsize(DB)
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")

    total = c.execute("SELECT COUNT(*) FROM live").fetchone()[0]
    old = c.execute("SELECT COUNT(*) FROM live WHERE last_seen < ?",
                    (cut,)).fetchone()[0]
    oldest = c.execute("SELECT MIN(last_seen) FROM live").fetchone()[0]
    print(f"  live: {total} rows, oldest {str(oldest)[:10]}")
    print(f"  cutoff {cut[:10]} (--days {a.days}) -> {old} rows older than that")
    print(f"  db file: {size_before / 1e6:.1f} MB")

    if not old:
        # Say so plainly rather than implying work was done. Early on the table
        # is younger than the retention window, so there is nothing to remove
        # and the value of this job is preventive.
        print("  nothing to prune - the table is younger than the retention "
              "window. This job exists to stop it growing without bound, so a "
              "no-op run is the expected result until it ages past the cutoff.")
        return 0

    if not a.yes:
        print("  dry run - nothing deleted. Re-run with --yes.")
        return 0

    with c:
        n = c.execute("DELETE FROM live WHERE last_seen < ?", (cut,)).rowcount
    left = c.execute("SELECT COUNT(*) FROM live").fetchone()[0]
    assert left == total - n, (left, total, n)
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"  deleted {n} · {left} remain · integrity {ok}")

    if a.vacuum:
        # SQLite does not return freed pages to the filesystem on DELETE, so
        # without this the row count drops and the file does not. VACUUM needs
        # an exclusive lock, which is why this runs at 03:40 - the cron lanes
        # are 07:00-19:00 only.
        c.execute("VACUUM")
        print(f"  vacuumed: {size_before / 1e6:.1f} MB -> "
              f"{os.path.getsize(DB) / 1e6:.1f} MB")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
