"""WordPress + Matador Jobs REST API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import re

from fetchcore import days_ago, job, req, strip_html


# WordPress job plugins cap per_page at 100 and serve newest-first by default,
# so three pages is the whole of both boards we poll (Aplin 301, Raise 308) with
# room to grow. Paging past the end is an HTTP 400, not an empty list.
WP_PER_PAGE = 100
WP_MAX_PAGES = 4

# Matador exposes its taxonomies as bare term IDs in the record, but WordPress
# also emits readable slugs in class_list - `matador-localities-toronto`,
# `matador-regions-ontario`, `matador-workplaces-hybrid`. That is the only place
# the location is human-readable without a second request per taxonomy.
_WP_CLS = re.compile(r"^matador-(localities|regions|countries|workplaces)-(.+)$")


def _wp_location(j):
    """Location, from whichever place this site's plugin keeps it."""
    # WP Job Manager (Aplin): a plain string in post meta.
    loc = ((j.get("meta") or {}).get("_job_location") or "").strip()
    if loc:
        return loc
    # Matador (Raise): rebuild it from the class slugs.
    parts = {}
    for c in j.get("class_list") or []:
        m = _WP_CLS.match(c)
        if m:
            parts.setdefault(m.group(1), m.group(2).replace("-", " ").title())
    loc = ", ".join(filter(None, [parts.get("localities"), parts.get("regions")]))
    if not loc:
        loc = parts.get("countries", "")
    wp = (parts.get("workplaces") or "").lower()
    if wp in ("remote", "hybrid") and wp not in loc.lower():
        loc = f"{wp.title()}{' - ' + loc if loc else ''}"
    return loc


def fetch_wpjobs(t):
    """Staffing agencies run their boards on WordPress, not on a corporate ATS.

    Fingerprinting 36 Canadian IT staffing firms turned up zero Greenhouse,
    Lever, Ashby or Workday boards: agencies live on recruiting CRMs (Bullhorn,
    JobDiva, iCIMS) whose public faces are JS search pages with no feed. The
    exceptions are the ones that publish through a WordPress plugin, because
    WordPress ships a REST API and the plugins register their job post type on
    it. That makes this one adapter worth more than the platform it names.

    Two traps, both of which cost a 404 before they were understood:

    1. The endpoint is the post type's `rest_base`, NOT its name. Aplin's type
       is `job_listing` but its route is `/wp/v2/job-listings`; Raise's is
       `matador-job-listings`. Guessing the type name returns HTTP 404
       `rest_no_route`. Read {base}/wp-json/wp/v2/types to find the real one -
       look for the entry whose key or rest_base mentions job.
    2. Asking for a page past the end is HTTP 400 `rest_post_invalid_page_number`,
       not an empty array, so the paging loop has to treat a hard failure after
       the first page as "that was the end" rather than as a broken board.

    The full description arrives inline in content.rendered, so unlike every
    other adapter here this one needs no hydrate pass.
    """
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    rb = t.get("type", "job-listings")
    out = []
    for page in range(1, WP_MAX_PAGES + 1):
        try:
            rows = req(f"{base}/wp-json/wp/v2/{rb}"
                        f"?per_page={WP_PER_PAGE}&page={page}"
                        f"&orderby=date&order=desc")
        except Exception:
            if page == 1:
                raise
            break
        if not isinstance(rows, list) or not rows:
            break
        for j in rows:
            title = strip_html((j.get("title") or {}).get("rendered", "")).strip()
            if not title:
                continue
            loc = _wp_location(j)
            posted = str(j.get("date") or "")[:10]
            desc = strip_html((j.get("content") or {}).get("rendered", ""))
            out.append(job("wpjobs", name, title, loc, j.get("link", ""),
                           posted, days_ago(posted),
                           " ".join([title, loc, desc[:8000]]),
                           updated=str(j.get("modified") or "")[:10]))
        if len(rows) < WP_PER_PAGE:
            break
    return out
