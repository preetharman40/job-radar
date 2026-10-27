"""Ashby hosted job boards (GraphQL).

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import threading
import time

from fetchcore import days_ago, job, req, strip_html


# The board-level type (JobPostingBriefsWithIdsAndTeamId) exposes NO date field
# at all - publishedDate lives only on the single-posting type. Asking for it
# here makes the whole query error and the adapter return an empty list, which
# silently zeroed every Ashby board. Dates come from the hydrate call instead.
ASHBY_LIST = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!)"
              " { jobBoard: jobBoardWithTeams("
              "organizationHostedJobsPageName: $organizationHostedJobsPageName)"
              " { jobPostings { id title locationName "
              "secondaryLocations { locationName } } } }")


# Ashby serves every board from one host, and the sweep asks it for 95 of them
# through a 24-worker pool. That burst started answering HTTP 429 the day the
# board list grew from 55 to 95: 6-14 boards failed per sweep, rotating, and
# seven of the casualties were boards that had worked for months - slant,
# solace, synthetic, telus-digital, thinkific, trulioo, wealthsimple. Nothing
# was wrong with any of them. Asked serially, all 95 answer 200, so the failure
# was our own concurrency rather than a block, and the digest's "Dead boards"
# footer was reporting live boards.
#
# A concurrency cap is the wrong tool here: 55 boards through the same
# 24-worker pool never tripped the limit, so what Ashby counts is requests per
# window, not simultaneous connections. A minimum gap is the control that
# matches, and it is the same shape as _jb_wait.
#
# 0.4s was the first value tried and it is NOT enough. One sweep of all 95
# boards passed cleanly at 0.4s - 95/95, 2,484 postings, 37.9s - but a second
# sweep started immediately after it failed partway through, so the bucket is
# depleted by sustained rate and not just by the opening burst. It refills
# fast: every board answered 200 again within two minutes, and Ashby exposes
# no Retry-After or rate headers to read it off.
#
# 0.8s holds: two back-to-back sweeps of all 95 boards, 24 workers, 95/95 and
# 2,484 postings each, zero 429s. That is ~190 requests over ~160s of
# continuous load, which is harsher than anything production does - the worst
# real overlap is the fast lane at :35 (14 Ashby boards) running into the
# 30-minute lane at :37 (95), about 109 requests in ~87s. The cost is a 76s
# serialised chain on the 30-minute lane and ~11s on the fast lane, both of
# which run alongside the other platforms rather than adding to them.
#
# Both passes take it, because hydrate asks the same host for each finalist.
ASHBY_GAP = 0.8
_ASHBY_LOCK = threading.Lock()
_ASHBY_LAST = [0.0]


def wait():
    """Space out requests to jobs.ashbyhq.com across the whole sweep.

    Public because discover.py probes the same host and has to share the
    budget: two unpaced probers trip the limit exactly as one unpaced sweep
    does.
    """
    with _ASHBY_LOCK:
        gap = time.time() - _ASHBY_LAST[0]
        if gap < ASHBY_GAP:
            time.sleep(ASHBY_GAP - gap)
        _ASHBY_LAST[0] = time.time()


def fetch_ashby(tok):
    wait()
    d = req("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
             {"operationName": "ApiJobBoardWithTeams",
              "variables": {"organizationHostedJobsPageName": tok},
              "query": ASHBY_LIST})
    jb = (d.get("data") or {}).get("jobBoard") or {}
    out = []
    for j in jb.get("jobPostings", []):
        locs = [j.get("locationName") or ""] + [
            (s or {}).get("locationName", "") for s in (j.get("secondaryLocations") or [])]
        loc = " / ".join(x for x in locs if x)
        out.append(job("ashby", tok, j.get("title", ""), loc,
                       f"https://jobs.ashbyhq.com/{tok}/{j.get('id')}",
                       "", None, f"{j.get('title','')} {loc}",
                       hyd=("ashby", tok, j.get("id"))))
    return out



# On JobPostingDetails the description field is descriptionHtml (not
# descriptionPlainText) and publishedDate IS available - this is where Ashby
# dates come from.
ONE_QUERY = ("query ApiJobPosting($organizationHostedJobsPageName: String!, "
             "$jobPostingId: String!) { jobPosting("
             "organizationHostedJobsPageName: $organizationHostedJobsPageName, "
             "jobPostingId: $jobPostingId) { descriptionHtml publishedDate } }")

def hydrate(j, kind):
    """publishedDate exists only on the single-posting GraphQL type."""
    _, tok, jid = kind
    wait()
    d = req("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
             {"operationName": "ApiJobPosting",
              "variables": {"organizationHostedJobsPageName": tok,
                            "jobPostingId": jid},
              "query": ONE_QUERY})
    jp = (d.get("data") or {}).get("jobPosting") or {}
    j["blob"] += " " + strip_html(jp.get("descriptionHtml") or "")[:8000]
    if jp.get("publishedDate"):
        j["posted"] = jp["publishedDate"]
        j["age"] = days_ago(jp["publishedDate"])
