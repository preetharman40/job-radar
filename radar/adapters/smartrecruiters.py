"""SmartRecruiters postings API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import days_ago, job, req, strip_html


# SmartRecruiters sorts every listing newest-first, so capping pages keeps the
# freshest rather than an arbitrary slice. Five pages covers the largest tenants
# we track (SGS posts 4,506 reqs; its newest 100 still span only two days).
SR_MAX_PAGES = 5


def fetch_smartrecruiters(tok):
    """SmartRecruiters publishes two APIs and only one of them is usable.

    The cross-company search (jobs.smartrecruiters.com/sr-jobs/search) silently
    ignores every location and paging parameter it is given - `location=Canada`,
    `country=ca`, `filter=country:ca` and `offset=1000` all return the identical
    first ~96 global rows - so it can only ever see a fixed window of the newest
    postings worldwide. It is good for harvesting tenant identifiers and nothing
    else; this per-company endpoint pages correctly and returns full inventory.

    Beware: an unknown tenant returns HTTP 200 with totalFound 0, which is
    byte-identical to a real tenant with no openings. A typo here cannot be
    distinguished from an empty board at fetch time - board_health.py is what
    catches it.
    """
    out, offset = [], 0
    for _ in range(SR_MAX_PAGES):
        d = req(f"https://api.smartrecruiters.com/v1/companies/{tok}"
                 f"/postings?limit=100&offset={offset}")
        rows = d.get("content") or []
        for j in rows:
            L = j.get("location") or {}
            loc = L.get("fullLocation") or ", ".join(filter(None, [
                L.get("city"), L.get("region"), (L.get("country") or "").upper()]))
            if L.get("remote"):
                loc = f"Remote{' - ' + loc if loc else ''}"
            elif L.get("hybrid") and loc:
                loc = f"Hybrid - {loc}"
            posted = str(j.get("releasedDate") or "")[:10]
            jid = str(j.get("id", ""))
            out.append(job("smartrecruiters", tok, (j.get("name") or "").strip(),
                           loc, f"https://jobs.smartrecruiters.com/{tok}/{jid}",
                           posted, days_ago(posted),
                           " ".join(filter(None, [
                               j.get("name") or "", loc,
                               (j.get("function") or {}).get("label", ""),
                               (j.get("experienceLevel") or {}).get("label", ""),
                               (j.get("typeOfEmployment") or {}).get("label", "")])),
                           hyd=("smartrecruiters", tok, jid)))
        offset += len(rows)
        if not rows or offset >= d.get("totalFound", 0):
            break
    return out


def hydrate(j, kind):
    """The posting detail splits the description across three jobAd sections."""
    _, tok, jid = kind
    d = req(f"https://api.smartrecruiters.com/v1/companies/{tok}"
             f"/postings/{jid}")
    secs = (d.get("jobAd") or {}).get("sections") or {}
    j["blob"] += " " + strip_html(" ".join(
        (secs.get(k) or {}).get("text", "") for k in
        ("jobDescription", "qualifications", "additionalInformation")
    ))[:8000]
