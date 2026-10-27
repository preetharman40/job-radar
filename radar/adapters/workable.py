"""Workable public jobs API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import days_ago, job, strip_html, _req


# Public widget endpoint, no auth, and it ships full descriptions plus a real
# `published_on` date in the list call - so no hydrate pass is needed.
#   GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
# The account name is the path segment on workable.com/{account}/, which is
# usually visible in the embed on the company's own careers page.


def fetch_workable(tok):
    d = _req(f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true")
    out = []
    for j in d.get("jobs", []):
        locs = j.get("locations") or []
        loc = " / ".join(
            ", ".join(filter(None, [l.get("city"), l.get("region") or l.get("state"),
                                    l.get("country")])) for l in locs if l)
        if not loc:
            loc = ", ".join(filter(None, [j.get("city"), j.get("state"),
                                          j.get("country")]))
        if j.get("telecommuting") and "remote" not in loc.lower():
            loc = f"Remote{' - ' + loc if loc else ''}"
        posted = str(j.get("published_on") or j.get("created_at") or "")[:10]
        out.append(job("workable", tok, j.get("title", ""), loc,
                       j.get("shortlink") or j.get("url") or j.get("application_url", ""),
                       posted, days_ago(posted),
                       " ".join([j.get("title", ""), loc,
                                 j.get("department") or "", j.get("function") or "",
                                 strip_html(j.get("description", ""))[:6000]])))
    return out
