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
    regressed, new_zero = [], 0
    for src, name, n, err in results:
        key = f"{src}/{name}"
        best, zeroes = prior.get(key, (0, 0))
        if n is None:                      # fetch error - not a parser problem
            continue
        if n > 0:
            best, zeroes = max(best, n), 0
        else:
            zeroes += 1
            new_zero += 1
            if best > 0 and zeroes >= ZEROES_BEFORE_ALERT:
                regressed.append((key, best, zeroes))
        conn.execute("INSERT INTO board_health VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(board) DO UPDATE SET last_count=excluded.last_count,"
                     "best_count=excluded.best_count, zeroes=excluded.zeroes,"
                     "checked=excluded.checked",
                     (key, src, n, best, zeroes, now))
    conn.commit()

    print(f"  sampled {len(results)} boards · {new_zero} returned zero · "
          f"{len(regressed)} regressed")
    for key, best, z in regressed:
        print(f"  !! REGRESSED {key} - used to return {best}, now 0 "
              f"for {z} consecutive checks")
    if regressed and a.notify:
        topic = jr.ntfy_topic()
        if topic:
            body = "\n".join(f"{k} (was {b})" for k, b, _ in regressed[:10])
            jr.notify([{"score": 0, "title": f"{len(regressed)} board(s) stopped returning jobs",
                        "company": "board health", "location": body[:60], "age": None,
                        "kw": [], "family": "devops", "url": ""}], topic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
