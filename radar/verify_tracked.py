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
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetchcore as fc  # noqa: E402
import job_radar as jr  # noqa: E402

TRACKER = os.path.abspath(os.path.join(HERE, "..", "applications.md"))
MISSES_BEFORE_GONE = 3
# Transient. A busy board answering 403 or 429 is not evidence a job is gone -
# RBC and Sun Life both returned 403 under load and 200 moments later.
TRANSIENT = {403, 408, 429, 500, 502, 503, 504}
RETRIES = 3
PAGE_TIMEOUT = 35     # some careers pages are genuinely slow (Global Relay)

# Text that means "this posting is closed", not "the server is unhappy".
#
# `not found` used to be a bare alternative here and it was wrong in the worst
# possible way: it fired on EVERY amazon.jobs page, live or closed, because the
# page ships this telemetry boilerplate -
#   /Object Not Found Matching Id:\d+, MethodName:\w+, ParamCount:\d+/
# so every Amazon posting was marked gone forever. The AFT req that prompted
# the Amazon adapter sat at misses=50 - fifty consecutive "gone" verdicts,
# never once seen open - while the page returned HTTP 200 with its real title.
# It was one `purge_expired.py --yes` away from being deleted out of the
# tracker as a dead link, along with a second live Amazon req.
#
# Two changes, because either alone is insufficient:
#  1. `not found` now requires a noun it could plausibly be about, so a
#     JavaScript error string no longer qualifies.
#  2. The search runs on script- and style-stripped HTML (see _closed_text).
#     Closed-posting language is something a human reads on the page; it does
#     not live in a <script> block, and matching inside one is always a bug.
CLOSED = re.compile(
    r"no longer (accepting|available|open)|position (has been )?(filled|closed)|"
    r"posting (has )?(expired|closed)|job (is )?no longer|"
    r"(job|posting|position|page|vacancy|requisition) not found|"
    r"this job is closed", re.I)


def _closed_text(html):
    """HTML with <script>/<style> removed, for CLOSED to search."""
    return fc.drop_scripts(html)


# Hosts this script must never fetch, matched on the host or any subdomain.
#
# inbox.py tracks jobs out of job-alert email, and those rows carry the
# aggregator's own link - 116 of them are www.linkedin.com/jobs/view/{id}.
# Verifying them would be wrong twice over:
#
#  1. Volume against a host that polices it. The walk below groups by host and
#     goes serially, so one shared host is one chain: 116 URLs x 11 runs a day
#     is ~1,276 requests/day to linkedin.com from a single residential IP.
#     Scraping LinkedIn risks the account and the IP, and we deliberately do
#     not do it anywhere else in this tool.
#  2. The verdict would be noise anyway. A logged-out aggregator page is a
#     login wall or an SEO stub, not the posting, so "is this req still open"
#     has no reliable answer in the HTML - and a false `gone` is not harmless:
#     three consecutive ones hide the row, and purge_expired.py deletes it.
#
# Absent from tracked_live is the right state for these: serve.py:99 only
# collects rows whose state is `gone`, so a skipped row shows normally and is
# never purged.
SKIP_HOSTS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.ca",
    "ziprecruiter.com", "monster.com", "monster.ca", "jobillico.com",
)


def skipped(url):
    """True if url's host is in SKIP_HOSTS (exact host or a subdomain)."""
    host = urlparse(url).netloc.split("@")[-1].split(":")[0].lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


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


# How long an aggregator row is given before it is called stale. The posting
# cannot be fetched to find out (see SKIP_HOSTS), so the only evidence is when
# it entered the tracker.
#
# Was 45 days, dropped to 10 on 2026-10-28 at the user's request: "the jobs
# might be 24 hours old, that is acceptable, but a week old" is the standard
# these rows are being read against, and 45 days is nowhere near it. 10 rather
# than 7 because the Found date is when WE first saw the posting, not when it
# was published - it is already an undercount of the req's real age, so a few
# days of slack keeps a job that is merely a week old out of the label.
#
# Measured before the change: of the 76 LinkedIn rows in the tracker, the
# OLDEST is 5 days old, so this has no immediate effect and nothing is
# relabelled today. It starts applying around 2026-11-07, as the rows the mail
# lane tracked from 2026-10-23 onward age past it. The old 45-day value had
# never fired at all - the web app reported `stale 0`.
#
# `stale` is a label, never a deletion: serve.py keeps it separate from
# `expired` and purge_expired.py deletes on `expired`, so lowering this
# surfaces rows for the reader to judge and cannot throw an application record
# away.
AGGREGATOR_STALE_DAYS = 10


def aggregator_cutoff(now, days=None):
    """Found dates STRICTLY BEFORE this are old enough to call stale.

    ISO dates compare correctly as strings, which is why the whole comparison
    is done on `YYYY-MM-DD` text rather than parsed dates. Strictly before, so
    a row found exactly `days` ago is not yet stale - it becomes stale the
    following day.
    """
    d = AGGREGATOR_STALE_DAYS if days is None else days
    return (now - timedelta(days=d)).strftime("%Y-%m-%d")


def stale_rows(found, skip, now, days=None):
    """tracked_live tuples for unfetchable rows old enough to call stale.

    `found` is {url: "YYYY-MM-DD"} from the tracker, `skip` the urls on hosts
    we do not fetch. A url with no Found date is left alone: the date is the
    only evidence there is, and no evidence is not evidence of staleness.
    """
    cutoff = aggregator_cutoff(now, days)
    stamp = now.isoformat(timespec="seconds")
    return [(u, "stale", 0, stamp, f"not fetchable; found {found[u]}")
            for u in skip if found.get(u) and found[u] < cutoff]


def tracked_found(path):
    """{apply_url: Found date} for every tracked row that records one.

    Aggregator rows carry no fetchable state, so their age is the only signal
    available, and `- **Found:** YYYY-MM-DD` is the one date inbox.py writes
    that is definitely true - `Posted` is "? (unknown age)" for mail rows
    because a digest snippet carries no publication date.
    """
    src = open(path, encoding="utf-8").read()
    out, url = {}, None
    for line in src.splitlines():
        m = re.match(r"^- \*\*Apply:\*\* (\S+)", line)
        if m:
            url = m.group(1)
            continue
        m = re.match(r"^- \*\*Found:\*\* (\d{4}-\d{2}-\d{2})", line)
        if m and url:
            out[url] = m.group(1)
            url = None
    return out


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
        req = urllib.request.Request(api, headers={**fc.UA, "Accept": "application/json"})
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
            d = fc.req(f"https://boards-api.greenhouse.io/v1/boards/"
                        f"{m.group(1)}/jobs/{m.group(2)}")
            return ("open", d.get("title", "")[:60]) if d.get("title") else ("gone", "no title")
        except urllib.error.HTTPError as e:
            return ("gone", f"HTTP {e.code}") if e.code in (404, 410) else ("unknown", f"HTTP {e.code}")
        except Exception as e:
            return "unknown", type(e).__name__

    # Everything else: fetch the page and look for closed-posting language.
    try:
        html = fc.get_text(url, timeout=PAGE_TIMEOUT)
        if CLOSED.search(_closed_text(html)[:60000]):
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
    skip = [u for u in urls if skipped(u)]
    urls = [u for u in urls if not skipped(u)]
    conn = db()
    # A row written before SKIP_HOSTS existed could still be sitting at
    # state='gone', hiding a job nothing will ever re-check. Clear them.
    if skip and not a.dry_run:
        legacy = [(u,) for u in skip if u in
                  {r[0] for r in conn.execute(
                      "SELECT url FROM tracked_live WHERE state='gone'")}]
        if legacy:
            conn.executemany("DELETE FROM tracked_live WHERE url=?", legacy)
            conn.commit()
            print(f"  cleared {len(legacy)} stale 'gone' row(s) for skipped hosts")

        # Age aggregator rows instead of leaving them in no state at all.
        # Absent from tracked_live was the right call for "do not hide it", but
        # it also meant these rows never changed: 135 LinkedIn rows had no
        # state, no first_seen and no way to tell a req found yesterday from
        # one found in September, so the tracker kept showing all of them as
        # current forever.
        #
        # `stale` is deliberately NOT `gone`. serve.py's expired set collects
        # only state='gone', and purge_expired.py deletes on that field, so a
        # stale row is surfaced to the reader and can never be deleted by the
        # purge - which matters, because one of these may be a job that was
        # applied to and a lost application record cannot be rebuilt.
        found = tracked_found(a.tracker)
        # Its own clock reading rather than `now`, which is assigned further
        # down: referencing it here was an UnboundLocalError.
        aged = stale_rows(found, skip, datetime.now(timezone.utc))
        if aged:
            def _age():
                conn.executemany(
                    "INSERT INTO tracked_live VALUES(?,?,?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET state=excluded.state, "
                    "checked=excluded.checked, detail=excluded.detail", aged)
            if jr.db_write(conn, _age, label="aggregator ageing"):
                print(f"  marked {len(aged)} aggregator row(s) stale "
                      f"(found over {AGGREGATOR_STALE_DAYS} days ago)")
    prior = {u: (s, m) for u, s, m in
             conn.execute("SELECT url, state, misses FROM tracked_live")}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Group by host and walk each host's URLs serially. Firing ten concurrent
    # requests at one Workday tenant earned a 403 from RBC, TD, Sun Life and
    # Desjardins every run; different hosts still run in parallel, so this
    # costs almost nothing in wall-clock.
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
    pending = []
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
        pending.append((url, final, misses, now, detail))
        if state != "open":
            print(f"  {state:8} misses={misses} {detail[:28]:30} {url[:60]}")
    # Collected first, written once, and retried on a lock. This used to write
    # inside the loop and commit at the end, so a blocked write threw away the
    # whole run - every HTTP check included. One of these died that way on
    # 2026-10-27 while a backfill held the lock.
    if not a.dry_run and pending:
        def _save():
            conn.executemany(
                "INSERT INTO tracked_live VALUES(?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET state=excluded.state, "
                "misses=excluded.misses, checked=excluded.checked, "
                "detail=excluded.detail", pending)
        if not jr.db_write(conn, _save, label="tracked_live"):
            print("  !! could not save results - database stayed locked. The "
                  "checks above are correct but miss counters did not advance.")
    print(f"\n  {counts['open']} open · {counts['gone']} not found · "
          f"{counts['unknown']} unclear   ({len(urls)} checked)")
    if skip:
        hosts = sorted({urlparse(u).netloc for u in skip})
        print(f"  {len(skip)} not checked - aggregator links we don't fetch: "
              f"{', '.join(hosts)}")
    print(f"  a job is hidden only after {MISSES_BEFORE_GONE} consecutive misses")


if __name__ == "__main__":
    main()
