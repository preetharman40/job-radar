#!/usr/bin/env python3
"""
board_health.py - catch boards that quietly stopped returning jobs.

The JSON adapters fail loudly: a dead Greenhouse token 404s and the sweep
reports it. The HTML-parsing ones do not. If Phenom, SuccessFactors, Jobvite or
the generic scraper meets a redesigned page, the parser matches nothing, the
adapter returns an empty list, and the sweep reports success. A board can stop
working for weeks without a single error.

This records how many jobs each board returns, and alerts when one that used to
produce drops to zero.

    python3 board_health.py              # record a sample, report regressions
    python3 board_health.py --report     # show history, record nothing
    python3 board_health.py --notify     # push regressions to ntfy

Standard library only.
"""

import argparse
import concurrent.futures as cf
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402

# How many consecutive zero samples before it counts as a regression. One zero
# could be a blip; three in a row on a board with history is a broken parser.
ZEROES_BEFORE_ALERT = 3
# A board whose best-ever count was 1 or 2 going quiet is a small employer with
# nothing open, not a broken adapter. Alerting on those teaches you to ignore
# the monitor - 35 of the tracked boards have a best_count of 1-2, so each would
# eventually fire the same false alarm. greenhouse/community (Rome Community
# Partners: one posting, never produced a match) was the first to do it.
MIN_BEST_TO_ALERT = 3
# A deleted board token 404s forever. Three strikes is enough to tell that from
# a transient network failure.
ERRORS_BEFORE_ALERT = 3
DICT_SRC = ("workday", "phenom", "successfactors", "oracle", "jobvite", "html")


def db():
    c = sqlite3.connect(jr.DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS board_health("
              "board TEXT PRIMARY KEY, platform TEXT, last_count INTEGER, "
              "best_count INTEGER, zeroes INTEGER DEFAULT 0, checked TEXT)")
    return c


def sample(task):
    src, tok, fn = task
    name = tok.get("name", str(tok)) if isinstance(tok, dict) else tok
    try:
        # Probe query-driven platforms with an EMPTY query, never a keyword.
        # "engineer" made small tenants look dead: WCB Alberta returns 7 jobs
        # but none of its titles contain the word, so health read 0 and would
        # have alerted on a board that was working perfectly. An empty query
        # asks the only question health cares about - is anything coming back -
        # and measured no slower on the largest tenants.
        if src == "workday":
            n = len(fn({**tok, "_query": ""}))
        elif src == "successfactors":
            n = len(fn({**tok, "_query": ""}))
        elif src == "oracle":
            n = len(fn({**tok, "_query": ""}))
        elif src == "jobvite":
            n = len(fn({**tok, "_query": ""}))
        else:
            n = len(fn(tok))
        return src, name, n, None
    except Exception as e:
        return src, name, None, f"{type(e).__name__}"


# Hours without a full sweep before board_health calls the poller dead. See the
# reasoning where it is used: 11h is normal at 06:50, so 20h means an evening
# of sweeps never happened.
STALE_SWEEP_HOURS = 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="show history, record nothing")
    ap.add_argument("--notify", action="store_true", help="push regressions to ntfy")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    conn = db()
    if a.report:
        rows = list(conn.execute(
            "SELECT board, platform, last_count, best_count, zeroes, checked "
            "FROM board_health ORDER BY zeroes DESC, best_count DESC"))
        print(f"  {'board':24}{'platform':16}{'last':>6}{'best':>6}{'zeroes':>8}")
        for b, p, last, best, z, _ in rows[:40]:
            flag = "  <-- REGRESSED" if z >= ZEROES_BEFORE_ALERT else ""
            print(f"  {b[:24]:24}{p:16}{last if last is not None else '?':>6}"
                  f"{best:>6}{z:>8}{flag}")
        print(f"\n  {len(rows)} boards tracked")
        return 0

    tg = json.load(open(jr.TARGETS))
    tasks = [(src, tok, fn) for src, fn in jr.ADAPTERS.items()
             for tok in tg.get(src, [])]
    with cf.ThreadPoolExecutor(a.workers) as ex:
        results = list(ex.map(sample, tasks))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prior = {b: (best, z) for b, best, z in
             conn.execute("SELECT board, best_count, zeroes FROM board_health")}
    regressed, broken, new_zero = [], [], 0
    prior_err = {b: e for b, e in
                 conn.execute("SELECT board, COALESCE(errors,0) FROM board_health")}
    pend_err, pend_ok = [], []
    for src, name, n, err in results:
        key = f"{src}/{name}"
        best, zeroes = prior.get(key, (0, 0))
        if n is None:
            # A fetch error used to `continue` - deliberately, so one network
            # blip could not look like a dead parser. But a board whose token
            # has been deleted answers 404 forever, and `continue` meant it was
            # never counted, never alerted, and silently polled for weeks. Four
            # Greenhouse boards (amplitude, civicplus, enthought, linkedin) sat
            # 404ing and were only found by reading the sweep's footer by hand.
            # Count consecutive errors instead and alert once it is clearly not
            # a blip. A single bad run still resets on the next success.
            errs = prior_err.get(key, 0) + 1
            if errs >= ERRORS_BEFORE_ALERT:
                broken.append((key, err or "fetch error", errs))
            pend_err.append((key, src, 0, best, zeroes, now, errs))
            continue
        if n > 0:
            best, zeroes = max(best, n), 0
        else:
            zeroes += 1
            new_zero += 1
            if best >= MIN_BEST_TO_ALERT and zeroes >= ZEROES_BEFORE_ALERT:
                regressed.append((key, best, zeroes))
        # Name the columns. A bare VALUES(...) broke the moment `errors` was
        # added to the table.
        pend_ok.append((key, src, n, best, zeroes, now))

    # Collected during the loop and written once here so the write can be
    # retried on a lock. The other order - execute inside the loop, commit at
    # the end - cannot be retried at all, because db_write rolls back before
    # retrying and that would discard every row the loop had queued. This
    # samples every board in targets.json before it writes, so a blocked
    # commit used to throw the whole sample away.
    def _save():
        if pend_err:
            conn.executemany(
                "INSERT INTO board_health(board,platform,last_count,"
                "best_count,zeroes,checked,errors) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(board) DO UPDATE SET errors=excluded.errors,"
                "checked=excluded.checked", pend_err)
        if pend_ok:
            conn.executemany(
                "INSERT INTO board_health"
                "(board,platform,last_count,best_count,zeroes,checked,errors) "
                "VALUES(?,?,?,?,?,?,0) "
                "ON CONFLICT(board) DO UPDATE SET last_count=excluded.last_count,"
                "best_count=excluded.best_count, zeroes=excluded.zeroes,"
                "checked=excluded.checked, errors=0", pend_ok)
    if not jr.db_write(conn, _save, label="board_health"):
        print("  !! board_health could not save - database stayed locked")

    n_err = sum(1 for r in results if r[2] is None)
    print(f"  sampled {len(results)} boards · {n_err} fetch errors · "
          f"{new_zero} returned zero · "
          f"{len(regressed)} regressed")
    for key, best, z in regressed:
        print(f"  !! REGRESSED {key} - used to return {best}, now 0 "
              f"for {z} consecutive checks")
    if broken:
        print(f"  !! {len(broken)} board(s) failing to fetch for "
              f"{ERRORS_BEFORE_ALERT}+ consecutive checks - the token is "
              f"probably gone, prune them:")
        for k, e, n in broken[:12]:
            print(f"     {k:34} {e} (x{n})")

    # Did the poller actually RUN? cronwrap.sh catches a lane that fails, but
    # nothing catches a lane that never started - a crashed cron daemon, a
    # machine that was asleep, a syntax error in a .sh that makes cron give up
    # silently. Those produce no exit code to react to, and the symptom is the
    # worst kind: the radar looks fine and quietly shows yesterday's jobs.
    #
    # This runs at 06:50, before the lanes start at 07:00, so the newest sweep
    # should be from about 19:41 the previous evening - roughly 11 hours. 20
    # hours means a full evening of sweeps is missing. Measured over the 31
    # days to 2026-10-27 the daily lane recorded 26 sweeps every single day, so
    # a gap this size is a real outage and not normal variance.
    stale_hours = None
    last = conn.execute("SELECT MAX(ts) FROM sweeps").fetchone()[0]
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            stale_hours = age.total_seconds() / 3600.0
        except Exception:
            stale_hours = None
    if stale_hours is None:
        print("  !! no sweep has ever been recorded - has the poller run?")
    elif stale_hours > STALE_SWEEP_HOURS:
        print(f"  !! LAST FULL SWEEP WAS {stale_hours:.1f}h AGO "
              f"(expected under {STALE_SWEEP_HOURS}h) - the poller is not running")
    else:
        print(f"  last full sweep {stale_hours:.1f}h ago")

    stale = stale_hours is None or stale_hours > STALE_SWEEP_HOURS
    if (regressed or broken or stale) and a.notify:
        topic = jr.ntfy_topic()
        if topic:
            lines = [f"{k} (was {b})" for k, b, _ in regressed[:6]]
            lines += [f"{k} ERR {e}" for k, e, _ in broken[:6]]
            what = []
            if stale:
                hrs = "never" if stale_hours is None else f"{stale_hours:.0f}h ago"
                what.append(f"POLLER NOT RUNNING (last sweep {hrs})")
                lines.insert(0, f"last sweep {hrs}")
            if regressed:
                what.append(f"{len(regressed)} stopped returning jobs")
            if broken:
                what.append(f"{len(broken)} failing to fetch")
            jr.notify([{"score": 0, "title": "board health: " + ", ".join(what),
                        "company": "board health",
                        "location": "; ".join(lines)[:60], "age": None,
                        "kw": [], "family": "devops", "url": ""}], topic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
