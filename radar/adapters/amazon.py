"""amazon.jobs search API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import re
import urllib.parse

from fetchcore import days_ago, job, long_date, strip_html, _req


# amazon.jobs caps result_limit at 100 and pages with offset. Asking for more
# does NOT clamp: result_limit=500 returns {"hits": 0, "jobs": []}, which is
# byte-identical to a country with no openings. Never raise AMZ_PER_PAGE above
# 100. Eight pages covers Amazon Canada (425 reqs on 2026-10-17) with headroom.
AMZ_PER_PAGE = 100
AMZ_MAX_PAGES = 8

def fetch_amazon(t):
    """Amazon runs its own ATS, so no amount of token harvesting reaches it.

    This is the first adapter here for an in-house ATS rather than a third-party
    product, and it exists because of a measured miss: "DevOps Engineer, AFT" in
    Toronto (job 10560828, posted 2026-09-25) scores 16-19, above the notify
    threshold, and the radar never saw it - Amazon was not in targets.json at
    all. It surfaced on LinkedIn instead, as a paid syndication.

    The endpoint is better than most: a real country facet, structured
    city/state/country_code, a genuine posted_date, and the full description
    inline - so like `wpjobs` this needs no hydrate pass.

    Two traps:

    1. result_limit is capped at 100 and exceeding it fails SILENTLY. See
       AMZ_PER_PAGE above.
    2. posted_date is a localised English string, not ISO. See fetchcore.long_date.

    `country` is an ISO 3166 alpha-3 facet (CAN), not the alpha-2 used elsewhere
    in this file. `query` is optional and normally empty: the whole country is
    only ~425 reqs, so there is no reason to filter server-side and risk its
    relevance ranking hiding something the local title rules would have kept.
    """
    name = t.get("name", "Amazon")
    country = t.get("country", "CAN")
    query = t.get("query", "")
    out, seen = [], set()
    for page in range(AMZ_MAX_PAGES):
        d = _req("https://www.amazon.jobs/en/search.json"
                 f"?base_query={urllib.parse.quote(query)}&loc_query="
                 f"&country={urllib.parse.quote(country)}"
                 f"&result_limit={AMZ_PER_PAGE}&offset={page * AMZ_PER_PAGE}")
        rows = d.get("jobs") or []
        if not rows:
            break
        for j in rows:
            path = j.get("job_path") or ""
            jid = str(j.get("id") or j.get("id_icims") or path)
            title = (j.get("title") or "").strip()
            if not path or not title or jid in seen:
                continue
            seen.add(jid)
            # city/state are the structured pair; normalized_location is the
            # display string and is the only one populated for virtual reqs.
            loc = (", ".join(filter(None, [j.get("city"), j.get("state")]))
                   or j.get("normalized_location") or j.get("location") or "")
            posted = long_date(j.get("posted_date"))
            desc = " ".join(strip_html(j.get(k) or "") for k in
                            ("description", "basic_qualifications",
                             "preferred_qualifications"))
            # `team` is an object, not a string - its readable part is
            # `label`. Joining it raw raises TypeError on the first posting.
            team = (j.get("team") or {}).get("label") or ""
            out.append(job("amazon", name, title, loc,
                           "https://www.amazon.jobs" + path,
                           posted, days_ago(posted),
                           " ".join([title, loc, team,
                                     j.get("job_category") or "",
                                     j.get("job_family") or "", desc[:8000]])))
        if len(rows) < AMZ_PER_PAGE:
            break
    return out
