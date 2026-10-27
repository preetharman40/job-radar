"""Job Bank (Government of Canada).

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import re
import threading
import time
import urllib.parse

from fetchcore import days_ago, job, long_date, strip_html, _get_text


# The federal board, and the only source here that is neither an ATS product nor
# an employer's own site. No JSON, no RSS (tried /jobsearch/rss and
# /sitemap.xml - both 404), so the search results page is parsed. The markup is
# server-rendered and stable: one <article id="article-{id}"> per posting with
# .noctitle, .date, .business and .location in a definition list.
#
# robots.txt is `User-agent: * / Crawl-delay: 5` - fully permissive and the only
# host here that asks for a delay, so it gets one. Requests are serialised
# through _jb_wait() with a 5s minimum gap, shared by the list AND hydrate
# passes, because the courtesy is per host and not per code path. That is why
# this is a single task issuing its queries in sequence rather than one task per
# query like Workday: fanning it out would mean parallel hits on a host that
# explicitly asked us not to.
#
# Three traps, all of which would have been silent:
#  1. jsessionid is IN THE PATH, not a query parameter -
#     /jobsearch/jobposting/50534243;jsessionid=656BB06A...jobsearch76 - and it
#     rotates per visit. Left in, every sweep would see a brand-new URL for the
#     same posting, so `seen` would never match and the same job would notify
#     forever. JB_SESS strips it, leaving /jobsearch/jobposting/{id}.
#  2. Dates are localised English month names ("October 06, 2026"), the exact
#     trap documented for Amazon: strptime("%B") is locale-dependent and cron
#     runs with a minimal environment, so it would work by hand and fail on
#     every scheduled sweep. fetchcore.long_date already maps the months by hand, so it is
#     reused rather than reimplemented.
#  3. The location cell carries a screen-reader label, so strip_html yields
#     "Location Toronto (ON)". The leading word has to come off or the string
#     stops classifying.
#
# Measured 2026-10-21: "devops" returns 5 results Canada-wide (the page's own
# results-summary agrees, so that is the whole set, not a page), "site
# reliability engineer" and "cloud infrastructure" return 0, "linux
# administrator" returns 10, and NONE of them had an Alberta posting. Job Bank
# is overwhelmingly trades and service work - a location-only query returns
# cooks and diesel mechanics - so this board is kept for completeness and
# because its inventory changes, not because it is expected to carry the search.
JB_BASE = "https://www.jobbank.gc.ca"
# "systems administrator" was in this list for one run and is deliberately out.
# It was the single biggest source of rows and almost all of them were the IT
# sysadmin work this search is explicitly not looking for: 14 of the 17
# Infra-adjacent matches it produced were "network system administrator" or
# "information technology (it) system administrator" at scores 11-13, four of
# them the same employer posting the same role four times, and one crossed the
# tracking threshold at 15 and would have been filed. Job Bank's title
# vocabulary is NOC-derived, so that query maps onto help-desk and desktop work
# rather than platform work. The DevOps-shaped queries below find the real roles
# on this board without it.
JB_QUERIES = ["devops", "site reliability", "cloud engineer", "linux",
              "platform engineer"]
JB_DELAY = 5.0

JB_ROW = re.compile(r'<article id="article-\d+".*?</article>', re.S)
JB_LINK = re.compile(r'href="(/jobsearch/jobposting/[^"]+)"')
JB_TITLE = re.compile(r'class="noctitle">(.*?)</span>', re.S)
JB_DATE = re.compile(r'class="date">(.*?)</li>', re.S)
JB_BIZ = re.compile(r'class="business">(.*?)</li>', re.S)
JB_LOC = re.compile(r'class="location">(.*?)</li>', re.S)
JB_SAL = re.compile(r'class="salary">(.*?)</li>', re.S)
JB_SESS = re.compile(r";jsessionid=[^?#]*")

_JB_LOCK = threading.Lock()
_JB_LAST = [0.0]


def _jb_wait():
    """Honour robots.txt Crawl-delay: 5 across every Job Bank request."""
    with _JB_LOCK:
        gap = time.time() - _JB_LAST[0]
        if gap < JB_DELAY:
            time.sleep(JB_DELAY - gap)
        _JB_LAST[0] = time.time()


def fetch_jobbank(t):
    name = t.get("name", "Job Bank")
    queries = t.get("queries") or JB_QUERIES
    out, seen = [], set()
    for q in queries:
        _jb_wait()
        url = (f"{JB_BASE}/jobsearch/jobsearch?sort=M&searchstring="
               + urllib.parse.quote(q))
        try:
            h = _get_text(url)
        except Exception:
            continue
        for row in JB_ROW.findall(h):
            lk = JB_LINK.search(row)
            ti = JB_TITLE.search(row)
            if not lk or not ti:
                continue
            path = JB_SESS.sub("", lk.group(1)).split("?")[0]
            full = JB_BASE + path
            if full in seen:
                continue
            seen.add(full)
            title = strip_html(ti.group(1)).strip()
            if not title:
                continue
            loc = strip_html(JB_LOC.search(row).group(1)) if JB_LOC.search(row) else ""
            # strip_html leaves the screen-reader label in front of the city
            loc = re.sub(r"^\s*Location\s+", "", loc).strip()
            comp = strip_html(JB_BIZ.search(row).group(1)).strip() \
                if JB_BIZ.search(row) else ""
            raw_date = strip_html(JB_DATE.search(row).group(1)).strip() \
                if JB_DATE.search(row) else ""
            posted = long_date(raw_date) or ""
            sal = strip_html(JB_SAL.search(row).group(1)).strip() \
                if JB_SAL.search(row) else ""
            out.append(job("jobbank", comp or name, title, loc, full,
                           posted, days_ago(posted),
                           " ".join(filter(None, [title, loc, comp, sal])),
                           hyd=("jobbank", full)))
    return out


def hydrate(j, kind):
    """Shares the list pass's 5s-per-host courtesy delay."""
    # Same 5s courtesy as the list pass - the delay is per host, so the
    # hydrate pass shares the limiter rather than having its own.
    _jb_wait()
    h = _get_text(kind[1])
    m = re.search(r'class="job-posting-detail-requirements".*', h, re.S) or \
        re.search(r'id="jobDescription".*', h, re.S)
    j["blob"] += " " + strip_html(m.group(0) if m else h)[:8000]
