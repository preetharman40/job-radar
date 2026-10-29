#!/usr/bin/env python3
"""
purge_expired.py - delete tracked jobs that are gone, too old, or both, and
leave the tracker, the web app and the database agreeing with each other.

Why this exists as a script rather than an ad-hoc loop: purging the 37 expired
rows by hand on 2026-10-13 also deleted two jobs that had already been APPLIED
to - PagerDuty (2026-09-16) and Manulife (2026-09-23). A dead posting is
clutter; a lost application record is history you cannot rebuild, and it is
exactly what you need for a follow-up or an interview. They had to be restored
from a backup.

So the guard is the point: anything whose status is not `toapply` is kept, no
matter how long the posting has been gone. Pass --include-applied to override,
and it will tell you what you are about to lose.

An age rule needs a date, and 25 of 110 rows have no `Posted` one - an ATS that
omits it, and every mail-sourced row, arrive as `Posted: ?`. Those rows are not
old; some were found this morning. So age falls back to **Found**, the day the
row entered the tracker, which every block carries. With that fallback nothing
in the tracker is undatable.

It also reconciles `tracked_live`. Deleting a job used to leave its live row
behind, and by 2026-10-28 there were 187 orphans against 110 real rows - all
187 in `dismissed`, so every one a deliberate delete that never finished.
serve.py now removes the live row with the job; this cleans up what history
left behind.

    python3 purge_expired.py                     # dry run - deletes nothing
    python3 purge_expired.py --older-than 20     # dry run, gone OR >20 days
    python3 purge_expired.py --older-than 20 --yes
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "seen.sqlite3")
API = "http://127.0.0.1:8737"


def _date(s):
    try:
        return datetime.date.fromisoformat((s or "").strip()[:10])
    except Exception:
        return None


def age_days(j, today):
    """How old a row is, or None.

    `Posted` is the honest answer and comes first. `Found` is the fallback and
    is a LOWER bound - a req found on the 20th may have been posted on the 1st
    - which is the safe direction for a rule that deletes: it under-states age,
    so it keeps a row too long rather than removing one too early.
    """
    d = _date(j.get("posted")) or _date(j.get("found"))
    return (today - d).days if d else None


def live_orphans(tracker_urls):
    """tracked_live rows with no tracker row left to describe."""
    try:
        c = sqlite3.connect(DB)
        rows = [u for (u,) in c.execute("SELECT url FROM tracked_live")
                if u not in tracker_urls]
        c.close()
        return rows
    except Exception as e:
        print(f"  (cannot read tracked_live: {type(e).__name__}: {e})")
        return []


def fetch():
    with urllib.request.urlopen(f"{API}/api/jobs", timeout=20) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument("--older-than", type=int, metavar="DAYS",
                    help="also delete jobs older than DAYS (by Posted, or by "
                         "Found when the posting carries no date)")
    ap.add_argument("--include-applied", action="store_true",
                    help="also delete jobs you have applied to (you will lose "
                         "the record)")
    a = ap.parse_args()

    try:
        d = fetch()
    except Exception as e:
        sys.exit(f"cannot reach the web app at {API}: {type(e).__name__}: {e}\n"
                 "  start it with: systemctl --user start job-radar-web.service")

    today = datetime.date.today()
    cand, undatable = [], []
    for j in d["jobs"]:
        why = ["gone"] if j["expired"] else []
        age = age_days(j, today)
        if a.older_than is not None:
            if age is None:
                undatable.append(j)
            elif age > a.older_than:
                why.append(f"{age}d")
        if why:
            cand.append((j, "+".join(why), age))

    # The guard, and the reason this is a script. Purging the 37 expired rows
    # by hand on 2026-10-13 also deleted two APPLIED jobs - PagerDuty and
    # Manulife - which had to be restored from a backup. A dead or old posting
    # is clutter; an application record is history that cannot be rebuilt.
    kept = [t for t in cand if t[0]["status"] != "toapply"]
    doomed = cand if a.include_applied else [t for t in cand
                                             if t[0]["status"] == "toapply"]

    older = sum(1 for j, w, g in cand if "d" in w.split("+")[-1])
    print(f"  {d['count']} tracked · {d['expired']} gone"
          + (f" · {older} older than {a.older_than}d" if a.older_than else ""))
    if undatable:
        # Cannot happen today - every block has a Found date - so if it ever
        # prints, a row was written by something that skipped that field, and
        # the right answer is to keep it rather than guess its age.
        print(f"\n  {len(undatable)} with no date at all - kept:")
        for j in undatable:
            print(f"    #{j['n']:<4} {j['company'][:20]:22} {j['role'][:38]}")
    if kept:
        verb = "WILL ALSO BE DELETED" if a.include_applied else "kept (not toapply)"
        print(f"\n  {len(kept)} already actioned - {verb}:")
        for j, why, age in sorted(kept, key=lambda t: -t[0]["score"]):
            print(f"    [{j['score']:>2}] #{j['n']:<4} {j['status']:<13} "
                  f"{why:<9} {j['company'][:20]:22} {j['role'][:38]}")
    print(f"\n  {len(doomed)} to delete:")
    for j, why, age in sorted(doomed, key=lambda t: -(t[2] or 0)):
        print(f"    [{j['score']:>2}] #{j['n']:<4} {why:<9} "
              f"{j['company'][:20]:22} {j['role'][:38]}")

    tracker_urls = {j["url"] for j in d["jobs"] if j.get("url")}
    orphans = live_orphans(tracker_urls)
    if orphans:
        print(f"\n  {len(orphans)} orphan tracked_live rows to clear "
              f"(deleted jobs whose live row was left behind)")

    if not a.yes:
        print("\n  dry run - nothing deleted. Re-run with --yes.")
        return 0
    if not doomed and not orphans:
        print("\n  nothing to do.")
        return 0

    ok = fail = 0
    for j, why, age in doomed:
        req = urllib.request.Request(
            f"{API}/api/delete", data=json.dumps({"url": j["url"]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as f:
                ok += 1 if json.load(f).get("ok") else 0
        except Exception as e:
            fail += 1
            print(f"    FAILED #{j['n']}: {type(e).__name__}: {e}")

    # Re-read rather than reusing the set from before the deletions: the rows
    # just removed are orphans now too, and each delete already took its own
    # live row, so this must see the state it actually left behind.
    cleared = 0
    try:
        d2 = fetch()
        left = live_orphans({j["url"] for j in d2["jobs"] if j.get("url")})
        if left:
            c = sqlite3.connect(DB)
            c.executemany("DELETE FROM tracked_live WHERE url = ?",
                          [(u,) for u in left])
            c.commit()
            cleared = c.total_changes
            c.close()
    except Exception as e:
        print(f"    live-table reconcile FAILED: {type(e).__name__}: {e}")
    print(f"\n  deleted {ok} · failed {fail} · live rows cleared {cleared}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
