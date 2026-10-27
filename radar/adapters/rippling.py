"""Rippling ATS board API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import job, _req


def fetch_rippling(tok):
    d = _req(f"https://api.rippling.com/platform/api/ats/v1/board/{tok}/jobs")
    out = []
    for j in d if isinstance(d, list) else []:
        loc = j.get("workLocation", {}).get("label", "") or j.get("locationName", "")
        out.append(job("rippling", tok, j.get("name", ""), loc,
                       j.get("url", ""), "", None, f"{j.get('name','')} {loc}"))
    return out
