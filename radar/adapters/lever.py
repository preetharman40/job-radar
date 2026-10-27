"""Lever postings API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from datetime import datetime, timezone

from fetchcore import days_ago, job, req, strip_html


def fetch_lever(tok):
    d = req(f"https://api.lever.co/v0/postings/{tok}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        ts = j.get("createdAt")
        posted = (datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
                  if ts else "")
        # Lever already ships the full description - no hydration needed.
        blob = " ".join(filter(None, [
            j.get("text", ""), loc, cat.get("team") or "",
            j.get("descriptionPlain", "")[:6000],
            " ".join(strip_html(l.get("text", "") + " " + l.get("content", ""))
                     for l in (j.get("lists") or []))[:4000]]))
        out.append(job("lever", tok, j.get("text", ""), loc,
                       j.get("hostedUrl", ""), posted, days_ago(posted), blob))
    return out
