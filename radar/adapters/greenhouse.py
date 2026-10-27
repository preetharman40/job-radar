"""Greenhouse job boards API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import days_ago, job, strip_html, _req


def fetch_greenhouse(tok):
    d = _req(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=false")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        # `updated_at` is last-modified, not publication. Measured across 13,528
        # postings it understates true age by a median of 36 days and is correct
        # only 17% of the time - a recruiter editing an old req makes it look
        # posted today. `first_published` is the real date; fall back only if a
        # board omits it.
        posted = j.get("first_published") or j.get("updated_at", "")
        out.append(job("greenhouse", tok, j.get("title", ""), loc,
                       j.get("absolute_url", ""), posted,
                       days_ago(posted), f"{j.get('title','')} {loc}",
                       hyd=("greenhouse", tok, j.get("id")),
                       updated=j.get("updated_at", "")))
    return out


def hydrate(j, kind):
    """The job detail carries the full content plus the office tags."""
    _, tok, jid = kind
    d = _req(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs/{jid}")
    j["blob"] += " " + strip_html(d.get("content", ""))[:8000]
    j["offices"] = [o.get("name", "") for o in (d.get("offices") or [])]
