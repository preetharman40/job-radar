"""Oracle Recruiting Cloud (ORC).

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import urllib.parse

from fetchcore import days_ago, job, req


# Used by universities, utilities and energy companies. A proper JSON REST API
# with real posted dates - far better than the HTML-parsing adapters. The
# careers URL looks like:
#   https://<host>/hcmUI/CandidateExperience/en/sites/<SITE>/jobs
# and the site number (usually CX_1) is in that page's markup.

ORC_QUERIES = ["devops", "cloud", "infrastructure", "systems", "network",
               "engineer", "analyst", "linux", "security", "platform"]


def fetch_oracle(t):
    host = t["host"].rstrip("/")
    site = t.get("site_number", "CX_1")
    name = t.get("name", host)
    q = t.get("_query", "")
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList"
           f"&finder=findReqs;siteNumber={site},limit=50,sortBy=POSTING_DATES_DESC")
    if q:
        api += f",keyword={urllib.parse.quote(q)}"
    d = req(api)
    items = d.get("items") or []
    reqs = items[0].get("requisitionList", []) if items else []
    out = []
    for r in reqs:
        loc = r.get("PrimaryLocation") or r.get("PrimaryLocationCountry") or ""
        posted = str(r.get("PostedDate") or "")[:10]
        rid = r.get("Id")
        out.append(job("oracle", name, r.get("Title", ""), loc,
                       f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                       f"{t.get('site', 'CX_1')}/job/{rid}",
                       posted, days_ago(posted),
                       " ".join(filter(None, [r.get("Title", ""), loc,
                                              r.get("JobFamily") or "",
                                              r.get("JobFunction") or "",
                                              r.get("WorkplaceTypeCode") or ""]))))
    return out


def expand(tok):
    """One task per search term."""
    return [{**tok, "_query": q} for q in ORC_QUERIES]
