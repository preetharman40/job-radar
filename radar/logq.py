#!/usr/bin/env python3
"""
logq.py - ask questions of the radar's run history.

Every lane goes through cronwrap.sh, which appends one JSON record per run to
events.jsonl. This reads that file so the questions you actually have after a
quiet failure are one command instead of an afternoon of grepping:

    python3 logq.py                  # last 24h per lane + failures + gaps
    python3 logq.py --days 7         # wider window
    python3 logq.py --lane daily     # one lane's history and trend
    python3 logq.py --failures       # every failure, with its captured tail
    python3 logq.py --gaps           # runs that should have happened and did not

Why it exists: the prose logs cannot answer "when did this lane last succeed",
"which run did the 429s start in", or "did it run at all on the 14th" without
regex archaeology - and doing that archaeology by hand produced three wrong
conclusions in ratecheck.py alone.

Standard library only.
"""
import argparse
import collections
import glob
import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(HERE, "events.jsonl")

# How often each lane is supposed to run, for --gaps. Taken from the crontab;
# if you change the schedule, change this, because a gap check that does not
# know the schedule cannot tell "did not run" from "was not due".
CADENCE = {
    "fast":      ("every 5 min, 07-19", 5 / 60),
    "daily":     (":07 and :37, 07-19", 0.5),
    "verify":    (":25, 08-18", 1.0),
    "health":    ("06:50 Mon-Fri", 24.0),
    "inbox":     ("19:52 daily", 24.0),
    "backup":    ("18:40 daily", 24.0),
    "ratecheck": ("07:40 Mondays", 168.0),
    "prune":     ("03:40 Sundays", 168.0),
    "refresh":   ("05:10 Sundays", 168.0),
    "logrotate": ("03:17 Sundays", 168.0),
}

# logq owns the event log, so floorcheck.py reads it from here rather than
# growing a second copy of the rotated-generation walk in _sources() - that
# walk is load-bearing, and a duplicate of it would go stale at the first
# logrotate. Declaring the surface puts the dependency under selftest's
# boundary check instead of leaving it as a reach-in.
__all__ = ["CADENCE", "EVENTS", "load"]


def _sources():
    """events.jsonl plus every rotated generation, oldest first.

    Reading the rotated files matters: logrotate keeps 12 monthly generations,
    and if this only read the live file then every rotation would silently
    shorten the window you can ask about - which is the same class of problem
    as the `find -delete` this replaced.
    """
    files = [EVENTS] if os.path.exists(EVENTS) else []
    files += sorted(glob.glob(EVENTS + "-*"))
    return files


def load(days):
    srcs = _sources()
    if not srcs:
        print(f"  {EVENTS} does not exist yet - no wrapped lane has run since "
              f"event logging was added.", file=sys.stderr)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for path in srcs:
        opener = gzip.open if path.endswith(".gz") else open
        try:
            fh = opener(path, "rt", errors="replace")
        except Exception as e:
            print(f"  !! cannot read {path} ({e})", file=sys.stderr)
            continue
        with fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    r["_dt"] = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                except Exception as e:
                    # A corrupt line is itself a finding, not something to hide.
                    print(f"  !! {os.path.basename(path)}:{n} is not parseable "
                          f"({e})", file=sys.stderr)
                    continue
                if r["_dt"] >= cutoff:
                    out.append(r)
    return sorted(out, key=lambda r: r["_dt"])


def _ago(dt):
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s / n:.0f}{unit} ago"
    return "just now"


def summary(rows, days):
    by = collections.defaultdict(list)
    for r in rows:
        by[r["label"]].append(r)
    print(f"  {len(rows)} runs in the last {days}d across {len(by)} lanes\n")
    print(f"  {'lane':11}{'runs':>6}{'fail':>6}{'last run':>13}{'last ok':>13}"
          f"{'median s':>10}  worst")
    for lane in sorted(by):
        rs = by[lane]
        fails = [r for r in rs if r["rc"] != 0]
        oks = [r for r in rs if r["rc"] == 0]
        secs = sorted(r["secs"] for r in rs)
        med = secs[len(secs) // 2] if secs else 0
        worst = max((r["secs"] for r in rs), default=0)
        print(f"  {lane:11}{len(rs):>6}{len(fails):>6}"
              f"{_ago(rs[-1]['_dt']):>13}"
              f"{(_ago(oks[-1]['_dt']) if oks else 'NEVER'):>13}"
              f"{med:>10}  {worst}s")
    bad = [r for r in rows if r["rc"] != 0 or r.get("tracebacks")]
    t429 = [r for r in rows if r.get("http429")]
    print()
    print(f"  failures / tracebacks : {len(bad)}")
    print(f"  runs with HTTP 429    : {len(t429)}"
          + (f"  ({sum(r['http429'] for r in t429)} total)" if t429 else ""))
    if bad:
        print("\n  most recent failures (use --failures for the tails):")
        for r in bad[-5:]:
            print(f"    {r['ts']}  {r['label']:10} rc={r['rc']} "
                  f"tracebacks={r.get('tracebacks', 0)}")


def failures(rows):
    bad = [r for r in rows if r["rc"] != 0 or r.get("tracebacks")]
    if not bad:
        print("  no failures in the window")
        return
    for r in bad:
        print(f"\n=== {r['ts']}  {r['label']}  rc={r['rc']}  {r['secs']}s "
              f"tracebacks={r.get('tracebacks', 0)} ===")
        print((r.get("tail") or "(no tail captured)").rstrip())


def lane(rows, name):
    rs = [r for r in rows if r["label"] == name]
    if not rs:
        print(f"  no runs recorded for lane {name!r}")
        return
    print(f"  {len(rs)} runs of {name!r}"
          + (f"  ({CADENCE[name][0]})" if name in CADENCE else "") + "\n")
    cols = [k for k in ("matched", "scanned", "boards", "shown", "pushed",
                        "tracked", "sampled", "checked", "http429")
            if any(k in r for r in rs)]
    print(f"  {'when':22}{'rc':>4}{'secs':>6}" + "".join(f"{c:>10}" for c in cols))
    for r in rs:
        print(f"  {r['ts']:22}{r['rc']:>4}{r['secs']:>6}"
              + "".join(f"{r.get(c, '-'):>10}" for c in cols))


def gaps(rows, days):
    by = collections.defaultdict(list)
    for r in rows:
        by[r["label"]].append(r)
    print(f"  lanes whose last run is overdue for their cadence:\n")
    found = False
    skipped = []
    for lane_name, (desc, hours) in sorted(CADENCE.items()):
        # A window shorter than the cadence cannot prove anything. Asking
        # "did the Sunday lane run?" on a Tuesday with --days 1 is not a
        # question the data can answer, and reporting it as a gap made this
        # command cry wolf about seven lanes at once - the weekly ones, the
        # Monday one, and the two that simply had not come round yet today.
        # 1.5x so a daily lane needs a day and a half of silence, not 24h
        # and a minute, before it is called missing.
        if days * 24 < hours * 1.5:
            skipped.append((lane_name, hours))
            continue
        rs = by.get(lane_name)
        if not rs:
            print(f"  {lane_name:11} NO RUNS in {days}d   ({desc})")
            found = True
            continue
        age = (datetime.now(timezone.utc) - rs[-1]["_dt"]).total_seconds() / 3600
        # 2.5x the cadence before calling it overdue: the lanes do not run
        # overnight, so a 30-minute lane is legitimately 11 hours stale at
        # 06:00 and a daily one nearly 24.
        limit = max(hours * 2.5, 13.0 if hours <= 1 else hours * 1.2)
        if age > limit:
            print(f"  {lane_name:11} last ran {age:.1f}h ago, expected every "
                  f"{hours}h   ({desc})")
            found = True
    if not found:
        print("  none - every lane has run within its expected interval")
    if skipped:
        need = max(h * 1.5 / 24 for _n, h in skipped)
        print(f"\n  not assessed, window too short to tell ({days}d):")
        print("    " + ", ".join(f"{n} (every {h:g}h)" for n, h in skipped))
        # Computed, not guessed: a hint that names a window still too short to
        # answer the question is worse than no hint.
        print(f"    use --days {need:.0f} or more to assess all of these")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--lane")
    ap.add_argument("--failures", action="store_true")
    ap.add_argument("--gaps", action="store_true")
    a = ap.parse_args()
    rows = load(a.days)
    if not rows:
        return 1
    if a.lane:
        lane(rows, a.lane)
    elif a.failures:
        failures(rows)
    elif a.gaps:
        gaps(rows, a.days)
    else:
        summary(rows, a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
