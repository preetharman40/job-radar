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

It reads every lane that polls boards - the 30-minute sweeps, the 5-minute fast
lane and the weekly fast.json rebuild - and reports each separately, because
their request volumes differ by a factor of six and a lane only looks healthy
next to its own history.

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

# Every lane that polls boards, and the log it writes to.
#
# This read radar.log alone until 2026-10-28, so it saw the 26 daily sweeps a
# day and none of the 156 fast-lane runs - the lane with six times the request
# volume was the one lane it could not see. Two things hid in that gap on the
# same day: the first run of refresh_fast.sh produced three 429s on
# workday/BDO Canada while `ratecheck --days 1` printed "0 rate-limit errors",
# and fast.sh was filtering error lines out of its own log entirely, so the
# fast lane had no 429 history to read even by hand.
#
# A log that does not exist yet is skipped rather than being an error:
# refresh.log appears the first Sunday the rebuild runs.
LANES = (("daily", "radar.log"),
         ("fast", "fast.log"),
         ("refresh", "refresh.log"))
# Below this, a burst is normal jitter; above it, something is being throttled
# hard enough to lose postings.
PER_SWEEP_ALERT = 5

# The per-run threshold alone cannot see the fast lane's shape of trouble. A
# tenant that throttles it steadily yields 2-4 errors in each of 156 runs -
# hundreds of lost queries a day, never once tripping a per-run count of 5. So
# a lane's DAY total alerts too.
#
# 20 is read off the history rather than chosen: every real incident in 40 days
# of radar.log ran 29, 38, 39, 156, 222 and 222 errors in a day, and every
# other day was 0 or 2. Nothing between 3 and 28 has ever happened, so the
# threshold sits in that gap and fires on all six incidents, none of the
# quiet days.
PER_DAY_ALERT = 20

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

# How each log delimits one run. The key has to come out of the line itself
# rather than from the file's structure, because the three lanes write three
# different shapes:
#
#   radar.log    `# Job Radar - NEW SINCE LAST RUN - 2026-10-28T18:12Z`
#   fast.log     every line prefixed `2026-10-28T19:22Z ` by fast.sh, no header
#   refresh.log  `=== 2026-10-28T19:13Z refreshing fast.json ===`
RUN_HDR = re.compile(r"^# Job Radar.*?(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})Z")
RUN_REFRESH = re.compile(r"^=== (\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})Z")
RUN_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})Z\s")


def _run_of(line):
    """((utc date, utc hh:mm) or None, the part of the line to scan).

    Splitting the payload off the stamp is what makes fast.log work: its lines
    carry their run stamp AND their content, so the same line both opens a run
    and may be the error in it. Handing BOARD_RE the raw line would make the
    board name start at the timestamp. radar.log's header has no payload - its
    error lines come later, under their own heading in the digest.
    """
    m = RUN_HDR.match(line)
    if m:
        return (m.group(1), m.group(2)), ""
    for rx in (RUN_REFRESH, RUN_STAMP):
        m = rx.match(line)
        if m:
            return (m.group(1), m.group(2)), line[m.end():]
    return None, line


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


def scan(days, lanes=None):
    # Cutoff in LOCAL dates, to match how sweeps are now grouped.
    cutoff = (datetime.now().astimezone()
              - timedelta(days=days)).strftime("%Y-%m-%d")
    runs = collections.Counter()      # (local day, lane) -> runs
    errs = collections.Counter()      # (lane, utc stamp) -> 429s
    boards = collections.Counter()
    day_of = {}
    worst = (0, None, None)           # count, utc stamp, lane
    for lane, fname in (lanes or LANES):
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            continue                  # refresh.log, before the first Sunday
        cur = None
        for line in open(path, errors="replace"):
            stamp, rest = _run_of(line)
            if stamp:
                # Two separate things, deliberately not mashed into one key:
                # the run is identified by its TRUE UTC stamp, exactly as the
                # log prints it so it can be grepped, and `day_of` maps that to
                # the local day it belongs to for grouping. A composite
                # "local-date + UTC-clock" key reads like a timestamp and is
                # not one - "2026-10-26 00:11Z" appears nowhere in any log.
                #
                # The lane is part of the key because the stamps collide: the
                # fast lane and a sweep both run at :07, and merging them would
                # add their errors together and report one run that never ran.
                cur = (lane, "%sT%sZ" % stamp)
                if cur not in day_of:
                    day_of[cur] = _local_day(*stamp)
                    if day_of[cur] >= cutoff:
                        runs[(day_of[cur], lane)] += 1
            if cur and day_of.get(cur, "") >= cutoff and HTTP_429.search(rest):
                errs[cur] += 1
                b = BOARD_RE.match(rest)
                if b:
                    boards[b.group(1).strip()] += 1
                if errs[cur] > worst[0]:
                    worst = (errs[cur], cur[1], lane)
    return runs, errs, boards, worst, day_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--notify", action="store_true")
    a = ap.parse_args()
    runs, errs, boards, worst, day_of = scan(a.days)
    if not runs:
        print(f"  no sweeps logged in the last {a.days} days")
        return 0

    # Both tallies are per (day, LANE), never per day. 26 daily sweeps and 156
    # fast runs summed into one row would hide a throttled fast lane inside a
    # denominator six times too big, and that is the lane this was blind to.
    byday = collections.Counter()     # (day, lane) -> 429s
    worst_of = collections.Counter()  # (day, lane) -> worst single run
    for k, v in errs.items():
        key = (day_of.get(k, k[1][:10]), k[0])
        byday[key] += v
        worst_of[key] = max(worst_of[key], v)
    print(f"  {'day':12}{'lane':10}{'runs':>7}{'429s':>7}{'worst run':>11}")
    for key in sorted(runs):
        print(f"  {key[0]:12}{key[1]:10}{runs[key]:>7}"
              f"{byday.get(key, 0):>7}{worst_of.get(key, 0):>11}")
    print(f"\n  {sum(runs.values())} runs over "
          f"{len({k[1] for k in runs})} lanes, "
          f"{sum(byday.values())} rate-limit errors")
    if boards:
        print("  boards affected:")
        for b, c in boards.most_common(8):
            print(f"    {b:28} {c}")

    # Only alert on CURRENT trouble. The first version flagged the worst sweep
    # anywhere in the window, so after the :07/:37 offset fixed the collision on
    # 2026-09-28 it kept citing the Sunday before - a fix looked like a live
    # problem for as long as the window reached back to it. What matters is
    # whether the most recent day is still throttled.
    today = max(d for d, _ in runs)
    # Each lane on the latest day as (worst single run, day total, lane), so
    # the two triggers can be read off the same list and either one can name
    # the lane it fired on.
    hot = [(worst_of.get((today, ln), 0), byday.get((today, ln), 0), ln)
           for d, ln in runs if d == today]
    by_run = max(hot)
    by_day = max((t, w, ln) for w, t, ln in hot)
    bad = by_run[0] >= PER_SWEEP_ALERT or by_day[0] >= PER_DAY_ALERT
    if worst[0] >= PER_SWEEP_ALERT and not bad:
        print(f"  note   : worst run in window was {worst[0]} on {worst[1]} "
              f"({worst[2]}), but {today} is clean - already fixed")
    if bad:
        if by_run[0] >= PER_SWEEP_ALERT:
            what = f"{by_run[0]} 429s in one {by_run[2]} run"
            lane, total = by_run[2], by_run[1]
        else:
            what = f"{by_day[0]} 429s across the {by_day[2]} lane"
            lane, total = by_day[2], by_day[0]
        msg = (f"Radar rate-limited: {what} on {today}, {total} in the {lane} "
               f"lane today. Worst board: "
               f"{boards.most_common(1)[0][0] if boards else '?'}. "
               f"Lower WD_TENANT_BURST in adapters/workday.py.")
        print(f"\n  !! {msg}")
        if a.notify:
            topic = jr.ntfy_topic()
            if topic:
                jr.notify([{"score": 0, "title": f"Rate limited: {what}",
                            "company": "job radar",
                            "location": (boards.most_common(1)[0][0]
                                         if boards else "")[:60],
                            "age": None, "kw": [], "family": "devops",
                            "url": ""}], topic)
            else:
                print("  (no ntfy topic configured)")
    else:
        print(f"\n  OK - no run exceeded {PER_SWEEP_ALERT} rate-limit errors "
              f"and no lane exceeded {PER_DAY_ALERT} in a day")
    return 0


if __name__ == "__main__":
    sys.exit(main())
