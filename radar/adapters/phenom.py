"""Phenom People career sites.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import json
import re

from fetchcore import days_ago, job, strip_html, _get_text


def _braces(h, i):
    """Slice the balanced {...} starting at or after index i."""
    start = h.index("{", i)
    depth = 0
    for k in range(start, len(h)):
        if h[k] == "{":
            depth += 1
        elif h[k] == "}":
            depth -= 1
            if depth == 0:
                return h[start:k + 1]
    return None


def fetch_phenom(t):
    """Phenom People careers sites (Bell, Air Canada, many bank front-ends).

    Phenom ships the first page of results inline as `phApp.ddo`, so there is no
    API to reverse-engineer - parse the page. Note that some Phenom sites are
    only a skin over Workday; where that is true, poll the Workday tenant
    instead (it gives structured locations and full descriptions). RBC and BMO
    both turned out to be Workday underneath.
    """
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    out, frm, total = [], 0, None
    while frm < 120:
        try:
            html = _get_text(f"{base}/search-results?from={frm}&s=1")
        except Exception:
            break
        m = re.search(r"phApp\.ddo\s*=", html)
        if not m:
            break
        try:
            d = json.loads(_braces(html, m.end()) or "{}")
        except Exception:
            break
        els = d.get("eagerLoadRefineSearch") or {}
        posts = ((els.get("data") or {}).get("jobs")) or []
        if not posts:
            break
        if total is None:
            total = els.get("totalHits") or 0
        for j in posts:
            # Prefer the clean city/state fields. Some tenants (City of
            # Edmonton) put a street address in `location` and `multi_location`,
            # which no location classifier can read as a city.
            loc = (j.get("cityStateCountry") or j.get("cityState") or "").strip()
            if not loc:
                locs = [x for x in (j.get("multi_location") or []) if x]
                loc = " / ".join(locs) if locs else (
                    j.get("location") or j.get("country") or "")
            if not loc:
                loc = t.get("default_location", "")
            # Same trap as Greenhouse: `postedDate` is a refresh/repost date
            # and can sit months after `dateCreated`, which is the true origin.
            # One Bell req showed postedDate 2026-09-03 vs dateCreated
            # 2026-06-26. Report the origin, carry the refresh separately.
            created = str(j.get("dateCreated") or "")[:10]
            reposted = str(j.get("postedDate") or "")[:10]
            posted = created or reposted
            jid = j.get("jobId") or j.get("reqId") or ""
            out.append(job("phenom", name, j.get("title", ""), loc,
                           f"{base}/job/{jid}/", posted, days_ago(posted),
                           " ".join([j.get("title", ""), loc,
                                     j.get("category", "") or "",
                                     (j.get("descriptionTeaser") or "")[:1500]]),
                           hyd=("phenom", f"{base}/job/{jid}/"),
                           updated=reposted if reposted != created else ""))
        frm += 10
        if total and frm >= total:
            break
    return out


def hydrate(j, kind):
    """The listing page gives only a ~400-character teaser."""
    # Phenom was the only platform with no hydrate branch, so every
    # Phenom row scored on the listing page's teaser - about 400
    # characters - and matched zero resume keywords. Five boards were
    # affected, City of Edmonton and Bell among them. The job page
    # carries the full text twice: in the phApp.ddo blob and in a
    # schema.org JobPosting block. Prefer the blob, fall back to JSON-LD.
    h = _get_text(kind[1])
    desc = ""
    m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){200,})"', h)
    if m:
        try:
            desc = json.loads(f'"{m.group(1)}"')
        except Exception:
            desc = m.group(1)
    if not desc:
        for mm in re.finditer(
                r'application/ld\+json[^>]*>(.*?)</script>', h, re.S):
            try:
                d = json.loads(mm.group(1))
                d = d[0] if isinstance(d, list) else d
                if d.get("@type") == "JobPosting" and d.get("description"):
                    desc = d["description"]
                    break
            except Exception:
                continue
    j["blob"] += " " + strip_html(desc)[:8000]
