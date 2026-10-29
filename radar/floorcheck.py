#!/usr/bin/env python3
"""
floorcheck.py - did a sweep lane's output collapse?

cronwrap.sh alerts when a lane FAILS and board_health.py alerts when a single
board goes quiet, but neither covers the failure in between: every lane exits 0,
every board still answers, and the number of real candidates falls off a cliff.
That has three plausible causes - a rules file edited into uselessness, an
adapter returning empty lists without raising, or a platform changing its API
shape - and all three look perfectly healthy in the logs.

The metric is `shortlist`: the count of postings that survived every pass-1
gate (title, location, family, level, age, score). It is deliberately not
`matched`, which counts only what is NEW, so it legitimately sits at zero on a
quiet afternoon; and not `recurring` or `hydrated`, which describe pass 2 and
move whenever the seen-skip's behaviour changes rather than when the boards do.

    ./floorcheck.py                  # the table, per lane
    ./floorcheck.py --metric recurring
    ./floorcheck.py --quiet          # only problems; exit 1 if any

Judging rule, per lane: take the last few successful runs, compare them against
the median of that lane's earlier runs in the trailing window, and complain
only when EVERY recent run is below a fraction of it. "Every" rather than "any"
is the whole design - one short run is a network blip, and alerting on blips
trains you to ignore the alert.

Deliberately silent in two cases, because a false alarm here costs more than a
late one:

  * fewer usable runs than the rule needs. A lane that has not run at all is
    the watchdog's sweep-recency check and logq.py --gaps, not this.
  * a baseline median too small to divide. Early in a lane's life, or for a
    narrow board list, 40% of 4 is noise.

Standard library only.
"""
import argparse
import os
import statistics
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logq                                                    # noqa: E402

# Lanes that produce a shortlist. The other eight lanes in the crontab are
# verify/backup/health/prune/etc - they have no sweep metrics to collapse.
LANES = ("fast", "daily")

RECENT_N = 3            # fast runs every 5 min, so this is a 15-minute window
BASELINE_DAYS = 14
MIN_BASELINE = 10       # below this the median is not worth dividing
MIN_MEDIAN = 20
FLOOR_FRAC = 0.4        # a 60% fall, sustained across every recent run

Verdict = namedtuple("Verdict", "lane ok judged values median floor note")


def when(row):
    """logq.load() attaches _dt; synthetic rows in tests may carry only ts."""
    dt = row.get("_dt")
    if dt is None:
        dt = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    return dt


def usable(row, lane, metric):
    """One run this check is allowed to reason about.

    Three exclusions, each of which was a real way to get this wrong:

      rc != 0      a failed run's footer is absent or truncated, and cronwrap
                   already alerted on it. Counting it as a low reading would
                   turn one failure into a second, different alert.
      missing key  events.jsonl predates `shortlist` - 410 of 468 records had
                   no such field the day this was written. Reading a missing
                   count as 0 would drag the median to the floor and then
                   report the collapse it had just invented.
      bool         isinstance(True, int) is True in Python, so a future
                   boolean field named like a metric would sail through.
    """
    v = row.get(metric)
    return (row.get("label") == lane and row.get("rc") == 0
            and isinstance(v, int) and not isinstance(v, bool))


def assess(rows, lane, metric="shortlist", recent_n=RECENT_N,
           baseline_days=BASELINE_DAYS, min_baseline=MIN_BASELINE,
           min_median=MIN_MEDIAN, floor_frac=FLOOR_FRAC, now=None):
    """Pure: event dicts in, one Verdict out. No clock, no disk, no network."""
    now = now or datetime.now(timezone.utc)
    runs = sorted((r for r in rows if usable(r, lane, metric)), key=when)
    need = recent_n + min_baseline
    if len(runs) < need:
        return Verdict(lane, True, False, [], None, None,
                       f"{len(runs)} usable runs, needs {need} - not judging")

    recent = runs[-recent_n:]
    cutoff = now - timedelta(days=baseline_days)
    # The baseline excludes the recent window on purpose: including it would
    # let a collapse pull down the very median it is being measured against.
    # It does still age out, so a collapse that lasts past the window becomes
    # the new normal and this goes quiet - which is why it alerts, rather than
    # being the only thing anyone looks at.
    base = [r[metric] for r in runs[:-recent_n] if when(r) >= cutoff]
    if len(base) < min_baseline:
        return Verdict(lane, True, False, [r[metric] for r in recent], None,
                       None, f"{len(base)} runs in the last {baseline_days}d, "
                             f"needs {min_baseline} - not judging")

    med = statistics.median(base)
    vals = [r[metric] for r in recent]
    if med < min_median:
        return Verdict(lane, True, False, vals, med, None,
                       f"median {med:g} is too small to judge")

    floor = floor_frac * med
    ok = any(v >= floor for v in vals)
    return Verdict(lane, ok, True, vals, med, floor,
                   f"{vals} vs median {med:g}, floor {floor:.0f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metric", default="shortlist",
                    choices=("shortlist", "recurring", "hydrated", "matched",
                             "scanned", "boards"),
                    help="which footer count to watch (default: shortlist)")
    ap.add_argument("--lane", choices=LANES, help="check one lane only")
    ap.add_argument("--recent", type=int, default=RECENT_N,
                    help=f"runs in the recent window (default {RECENT_N})")
    ap.add_argument("--frac", type=float, default=FLOOR_FRAC,
                    help=f"floor as a fraction of the median (default {FLOOR_FRAC})")
    ap.add_argument("--quiet", action="store_true",
                    help="print only problems - for watchdog.sh")
    a = ap.parse_args()

    rows = logq.load(BASELINE_DAYS + 2)
    lanes = (a.lane,) if a.lane else LANES
    bad = 0
    for lane in lanes:
        v = assess(rows, lane, a.metric, recent_n=a.recent, floor_frac=a.frac)
        if not v.ok:
            bad += 1
            print(f"{lane} {a.metric} collapsed: {v.note}")
        elif not a.quiet:
            mark = "ok" if v.judged else "--"
            print(f"  {lane:7}{a.metric:11}{mark:4}{v.note}")
    if not a.quiet and not bad:
        print(f"  no lane's {a.metric} has collapsed")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Exit 2, never 1: watchdog.sh must be able to tell "the sweep
        # collapsed" from "this check is broken", or a bug in here would
        # masquerade as the outage it is supposed to find.
        print(f"floorcheck.py failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
