"""Recruitee offers API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import days_ago, job, strip_html, _req


def fetch_recruitee(tok):
    d = _req(f"https://{tok}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        pub = j.get("published_at") or ""
        blob = f"{j.get('title','')} {loc} {strip_html(j.get('description',''))[:4000]}"
        out.append(job("recruitee", tok, j.get("title", ""), loc,
                       j.get("careers_url", ""), pub, days_ago(pub), blob))
    return out
