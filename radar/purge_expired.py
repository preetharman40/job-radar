#!/usr/bin/env python3
"""
purge_expired.py - delete tracked jobs that verify_tracked has confirmed gone.

Why this exists as a script rather than an ad-hoc loop: purging the 37 expired
rows by hand on 2026-10-13 also deleted two jobs that had already been APPLIED
to - PagerDuty (2026-09-16) and Manulife (2026-09-23). A dead posting is
clutter; a lost application record is history you cannot rebuild, and it is
exactly what you need for a follow-up or an interview. They had to be restored
from a backup.

So the guard is the point: anything whose status is not `toapply` is kept, no
matter how long the posting has been gone. Pass --include-applied to override,
and it will tell you what you are about to lose.

    python3 purge_expired.py              # dry run - lists, deletes nothing
    python3 purge_expired.py --yes        # do it
"""
import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "http://127.0.0.1:8737"


def fetch():
    with urllib.request.urlopen(f"{API}/api/jobs", timeout=20) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument("--include-applied", action="store_true",
                    help="also delete jobs you have applied to (you will lose "
                         "the record)")
    a = ap.parse_args()

    try:
        d = fetch()
    except Exception as e:
        sys.exit(f"cannot reach the web app at {API}: {type(e).__name__}: {e}\n"
                 "  start it with: systemctl --user start job-radar-web.service")

    expired = [j for j in d["jobs"] if j["expired"]]
    kept = [j for j in expired if j["status"] != "toapply"]
    doomed = expired if a.include_applied else [j for j in expired
                                                if j["status"] == "toapply"]

    print(f"  {d['count']} tracked · {len(expired)} expired")
    if kept:
        verb = "WILL ALSO BE DELETED" if a.include_applied else "kept (not toapply)"
        print(f"\n  {len(kept)} expired but already actioned - {verb}:")
        for j in sorted(kept, key=lambda x: -x["score"]):
            print(f"    [{j['score']:>2}] #{j['n']:<4} {j['status']:<13} "
                  f"{j['company'][:20]:22} {j['role'][:38]}")
    print(f"\n  {len(doomed)} to delete:")
    for j in sorted(doomed, key=lambda x: -x["score"]):
        print(f"    [{j['score']:>2}] #{j['n']:<4} {j['company'][:20]:22} "
              f"{j['role'][:40]}")
    if not a.yes:
        print("\n  dry run - nothing deleted. Re-run with --yes.")
        return 0
    if not doomed:
        print("\n  nothing to do.")
        return 0

    ok = fail = 0
    for j in doomed:
        req = urllib.request.Request(
            f"{API}/api/delete", data=json.dumps({"url": j["url"]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as f:
                ok += 1 if json.load(f).get("ok") else 0
        except Exception as e:
            fail += 1
            print(f"    FAILED #{j['n']}: {type(e).__name__}: {e}")
    print(f"\n  deleted {ok} · failed {fail}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
