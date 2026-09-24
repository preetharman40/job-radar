#!/usr/bin/env python3
"""
verify_tracked.py - is each tracked job still open? Ask it directly.

The radar infers liveness from whether a URL turned up in the last sweep. That
inference is wrong: a Manulife req posted 2026-09-22 was live and fetchable at
its own endpoint while being absent from Workday's keyword search for every
query, with and without the country facet, through 200 results. Search indexes
lag; job endpoints do not.

This fetches each tracked job's own URL and records what it finds. ~45 requests,
a few seconds, and it is authoritative rather than inferential.

    python3 verify_tracked.py            # check and record
    python3 verify_tracked.py --dry-run

Writes to the `tracked_live` table in seen.sqlite3, which serve.py reads.
A job is only called gone after MISSES_BEFORE_GONE consecutive failures, so one
network blip cannot hide a live posting.
"""

import argparse
import concurrent.futures as cf
import os
import re
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402

TRACKER = os.path.abspath(os.path.join(HERE, "..", "applications.md"))
MISSES_BEFORE_GONE = 3
# Transient. A busy board answering 403 or 429 is not evidence a job is gone -
# RBC and Sun Life both returned 403 under load and 200 moments later.
TRANSIENT = {403, 408, 429, 500, 502, 503, 504}
RETRIES = 3
PAGE_TIMEOUT = 35     # some careers pages are genuinely slow (Global Relay)

# Text that means "this posting is closed", not "the server is unhappy".
CLOSED = re.compile(
    r"no longer (accepting|available|open)|position (has been )?(filled|closed)|"
    r"posting (has )?(expired|closed)|job (is )?no longer|not found|"
    r"this job is closed", re.I)


def db():
    c = sqlite3.connect(jr.DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS tracked_live("
              "url TEXT PRIMARY KEY, state TEXT, misses INTEGER DEFAULT 0, "
              "checked TEXT, detail TEXT)")
    return c


def tracked_urls(path):
    src = open(path, encoding="utf-8").read()
    return re.findall(r"^- \*\*Apply:\*\* (\S+)", src, re.M)


def check(url):
    """-> (state, detail). Retries transient statuses before giving up."""
    for attempt in range(RETRIES + 1):
        state, detail = _check_once(url)
        if state != "unknown" or attempt == RETRIES:
            return state, detail
        code = re.search(r"HTTP (\d+)", detail)
        transient = (code and int(code.group(1)) in TRANSIENT) or \
                    detail.startswith(("TimeoutError", "URLError", "socket"))
        if not transient:
            return state, detail          # not transient - no point retrying
        time.sleep(1.5 * (attempt + 1))
    return state, detail


def _check_once(url):
    """-> (state, detail). 'open' | 'gone' | 'unknown'."""
    # Workday: ask the CXS job endpoint, which is authoritative.
    m = re.search(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/"
                  r"(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)(/job/.+)$", url)
    if m:
        t, c, site, path = m.groups()
        api = f"https://{t}.{c}.myworkdayjobs.com/wday/cxs/{t}/{site}{path}"
        req = urllib.request.Request(api, headers={**jr.UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as f:
                d = json.load(f)
            ji = d.get("jobPostingInfo") or {}
            if ji.get("title"):
                return "open", ji.get("title", "")[:60]
            return "gone", "no jobPostingInfo"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "gone", "HTTP 404"
            # Workday does not 404 a pulled requisition - it answers 403 with a
            # JSON body carrying errorCode S22. Treating every 403 as transient
            # meant those jobs sat in `checking` with misses=0 forever and were
            # never hidden: a Sun Life Cloud Engineer req stayed on the board
            # three days after it was pulled, and six others with it.
            #
            # 403 alone is NOT enough - a throttled or bot-blocked tenant also
            # answers 403 - so this keys on the S22 body specifically. Verified
            # by firing 10 concurrent requests at one tenant: the live req
            # returned 200 all ten times while the pulled one returned 403/S22
            # all ten, so the code tracks the requisition, not the load.
            if e.code == 403:
                try:
                    if json.loads(e.read(500).decode("utf-8", "replace")
                                  ).get("errorCode") == "S22":
                        return "gone", "HTTP 403 S22 (requisition withdrawn)"
                except Exception:
                    pass
            return "unknown", f"HTTP {e.code}"
        except Exception as e:
            return "unknown", type(e).__name__

    # Greenhouse: the board API 404s cleanly on a pulled req.
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url) or \
        re.search(r"gh_jid=(\d+)", url)
    if m and m.lastindex == 2:
        try:
            d = jr._req(f"https://boards-api.greenhouse.io/v1/boards/"
                        f"{m.group(1)}/jobs/{m.group(2)}")
            return ("open", d.get("title", "")[:60]) if d.get("title") else ("gone", "no title")
        except urllib.error.HTTPError as e:
            return ("gone", f"HTTP {e.code}") if e.code in (404, 410) else ("unknown", f"HTTP {e.code}")
        except Exception as e:
            return "unknown", type(e).__name__

    # Everything else: fetch the page and look for closed-posting language.
    try:
        html = jr._get_text(url, timeout=PAGE_TIMEOUT)
        if CLOSED.search(html[:60000]):
            return "gone", "closed-posting text"
        return ("open", "page loads") if len(html) > 1500 else ("unknown", "thin page")
    except urllib.error.HTTPError as e:
        return ("gone", f"HTTP {e.code}") if e.code in (404, 410) else ("unknown", f"HTTP {e.code}")
    except Exception as e:
        return "unknown", type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker", default=TRACKER)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()

    urls = tracked_urls(a.tracker)
    conn = db()
    prior = {u: (s, m) for u, s, m in
             conn.execute("SELECT url, state, misses FROM tracked_live")}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Group by host and walk each host's URLs serially. Firing ten concurrent
    # requests at one Workday tenant earned a 403 from RBC, TD, Sun Life and
    # Desjardins every run; different hosts still run in parallel, so this
    # costs almost nothing in wall-clock.
    from urllib.parse import urlparse
    by_host = {}
    for u in urls:
        by_host.setdefault(urlparse(u).netloc, []).append(u)

    def walk(host_urls):
        out = []
        for i, u in enumerate(host_urls):
            if i:
                time.sleep(0.4)
            out.append((u, check(u)))
        return out

    results_map = {}
    with cf.ThreadPoolExecutor(min(a.workers, len(by_host)) or 1) as ex:
        for chunk in ex.map(walk, by_host.values()):
            results_map.update(dict(chunk))
    results = [results_map[u] for u in urls]

    counts = {"open": 0, "gone": 0, "unknown": 0}
    for url, (state, detail) in zip(urls, results):
        _, misses = prior.get(url, ("open", 0))
        if state == "open":
            misses = 0
        elif state == "gone":
            misses += 1
        # `unknown` leaves the counter alone - a timeout is not evidence.
        final = "gone" if (state == "gone" and misses >= MISSES_BEFORE_GONE) else \
                "open" if state == "open" else "checking"
        counts[state] += 1
        if not a.dry_run:
            conn.execute("INSERT INTO tracked_live VALUES(?,?,?,?,?) "
                         "ON CONFLICT(url) DO UPDATE SET state=excluded.state, "
                         "misses=excluded.misses, checked=excluded.checked, "
                         "detail=excluded.detail",
                         (url, final, misses, now, detail))
        if state != "open":
            print(f"  {state:8} misses={misses} {detail[:28]:30} {url[:60]}")
    if not a.dry_run:
        conn.commit()
    print(f"\n  {counts['open']} open · {counts['gone']} not found · "
          f"{counts['unknown']} unclear   ({len(urls)} tracked)")
    print(f"  a job is hidden only after {MISSES_BEFORE_GONE} consecutive misses")


if __name__ == "__main__":
    main()
