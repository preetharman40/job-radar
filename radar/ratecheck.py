#!/usr/bin/env python3
"""
ratecheck.py - are the sweeps getting rate-limited?

The cron window was widened on 2026-09-25 from 07:00-15:59 weekdays to
07:00-19:59 every day, roughly doubling request volume. That is safe only while
the Workday per-tenant burst cap (WD_TENANT_BURST in adapters/workday.py) holds. This
reads radar.log and says whether it did.

A 429 is not cosmetic: the board returns nothing for that query and the jobs on
it are invisible until the next sweep. Moneris, Interac and Ledcor all produce
matches and all have been throttled before.

    python3 ratecheck.py            # last 7 days
    python3 ratecheck.py --days 3 --notify
"""
import argparse
import collections
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr                                             # noqa: E402

LOG = os.path.join(HERE, "radar.log")
# Below this, a burst is normal jitter; above it, something is being throttled
# hard enough to lose postings.
PER_SWEEP_ALERT = 5

# A real rate-limit line, not the digits 4-2-9 anywhere in a line. This was
# `"429" in line` until 2026-10-27, which counted 6 extra "errors" out of 714
# across the log: four sweep-summary lines whose scanned-posting count happened
# to contain 429, and two job URLs. Every genuine one in 40 days of log has the
# same shape - `- workday/TC Energy: HTTPError HTTP Error 429: Too Many
# Requests` - and the alternatives below cover plausible rewordings without
# ever being able to match a job id.
HTTP_429 = re.compile(r"HTTP\s*Error\s*429\b"
                      r"|\b429\s*:?\s*Too\s+Many\s+Requests"
                      r"|\bHTTPError\b[^\n]*\b429\b", re.I)

# The board label runs to the colon before the error. `- (\S+):` could not
# match a name containing a space, so every Workday tenant - TC Energy, Sun
# Life, Canadian Tire - was silently absent from the "boards affected" tally,
# on the one platform this script exists to watch. It is the Workday tenants
# that produced 708 of the 714 lines.
BOARD_RE = re.compile(r"^\s*-\s+([^:]+):")


def _local_day(utc_date, utc_hm):
    """The LOCAL date a sweep belongs to.

    radar.log stamps sweeps in UTC and cron runs in local time, so grouping on
    the UTC date splits one day's cron window across two rows: the 07:00-19:55
    MDT window is 13:00Z-01:55Z, so every evening sweep landed on the NEXT
    day's row. That made a fixed problem look live - the Ashby regression of
    2026-10-26 evening was reported under 2026-10-27, the day after the fix
    went in, while the real 2026-10-27 sweeps were all clean.
    """
    dt = datetime.strptime(f"{utc_date} {utc_hm}", "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")


def scan(days):
    # Cutoff in LOCAL dates, to match how sweeps are now grouped.
    cutoff = (datetime.now().astimezone()
              - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = None
    sweeps, errs, boards = collections.Counter(), collections.Counter(), collections.Counter()
    day_of = {}
    worst = (0, None)
    for line in open(LOG, errors="replace"):
        m = re.match(r"^# Job Radar.*?(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})Z", line)
        if m:
            # Two separate things, deliberately not mashed into one key: the
            # sweep is identified by its TRUE UTC stamp, exactly as the log
            # prints it so it can be grepped, and `day_of` maps that to the
            # local day it belongs to for grouping. A composite
            # "local-date + UTC-clock" key reads like a timestamp and is not
            # one - "2026-10-26 00:11Z" appears nowhere in the log.
            cur = f"{m.group(1)}T{m.group(2)}Z"
            day = _local_day(m.group(1), m.group(2))
            day_of[cur] = day
            if day >= cutoff:
                sweeps[day] += 1
            continue
        if cur and day_of.get(cur, "") >= cutoff and HTTP_429.search(line):
            errs[cur] += 1
            b = BOARD_RE.match(line)
            if b:
                boards[b.group(1).strip()] += 1
            if errs[cur] > worst[0]:
                worst = (errs[cur], cur)
    return sweeps, errs, boards, worst, day_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--notify", action="store_true")
    a = ap.parse_args()
    sweeps, errs, boards, worst, day_of = scan(a.days)
    if not sweeps:
        print(f"  no sweeps logged in the last {a.days} days")
        return 0

    byday = collections.Counter()
    for k, v in errs.items():
        byday[day_of.get(k, k[:10])] += v
    print(f"  {'day':12}{'sweeps':>8}{'429s':>8}{'worst sweep':>14}")
    for d in sorted(sweeps):
        w = max([v for k, v in errs.items() if day_of.get(k) == d] or [0])
        print(f"  {d:12}{sweeps[d]:>8}{byday.get(d, 0):>8}{w:>14}")
    total = sum(byday.values())
    print(f"\n  {sum(sweeps.values())} sweeps, {total} rate-limit errors")
    if boards:
        print("  boards affected:")
        for b, c in boards.most_common(8):
            print(f"    {b:28} {c}")

    # Only alert on CURRENT trouble. The first version flagged the worst sweep
    # anywhere in the window, so after the :07/:37 offset fixed the collision on
    # 2026-09-28 it kept citing the Sunday before - a fix looked like a live
    # problem for as long as the window reached back to it. What matters is
    # whether the most recent day is still throttled.
    today = max(sweeps)
    recent = byday.get(today, 0)
    recent_worst = max([v for k, v in errs.items()
                        if day_of.get(k) == today] or [0])
    bad = recent_worst >= PER_SWEEP_ALERT
    if worst[0] >= PER_SWEEP_ALERT and not bad:
        print(f"  note   : worst sweep in window was {worst[0]} on {worst[1]}, "
              f"but {today} is clean - already fixed")
    worst = (recent_worst, today) if bad else worst
    total = recent
    if bad:
        msg = (f"Radar rate-limited: {worst[0]} 429s in one sweep ({worst[1]}), "
               f"{total} over {a.days}d. Worst board: "
               f"{boards.most_common(1)[0][0] if boards else '?'}. "
               f"Lower WD_TENANT_BURST in adapters/workday.py.")
        print(f"\n  !! {msg}")
        if a.notify:
            topic = jr.ntfy_topic()
            if topic:
                jr.notify([{"score": 0, "title": f"Rate limited: {worst[0]} 429s "
                            "in one sweep", "company": "job radar",
                            "location": (boards.most_common(1)[0][0]
                                         if boards else "")[:60],
                            "age": None, "kw": [], "family": "devops",
                            "url": ""}], topic)
            else:
                print("  (no ntfy topic configured)")
    else:
        print(f"\n  OK - no sweep exceeded {PER_SWEEP_ALERT} rate-limit errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
