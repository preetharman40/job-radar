"""Breezy HR public board JSON.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import json
import re

from fetchcore import days_ago, get_text, job, req, strip_html


# Breezy publishes the whole board as JSON at a fixed path, no auth and no
# paging:
#   GET https://{tok}.breezy.hr/json
# The token is the subdomain. robots.txt disallows only /css, /fonts,
# /stylesheets and /javascripts, so both the board and the postings are fair
# game - unlike iCIMS, whose careers host answers "Disallow: /" and is
# therefore not polled at all.
#
# The list call carries a structured location (country, state, city, plus an
# is_remote flag) and a real published_date, but NO description, which the
# scorer needs. Each posting page embeds a schema.org JobPosting in ld+json
# carrying the full description and datePosted, so hydrate reads that one
# structured field instead of hunting for a content div.

BZ_LD = re.compile(
    r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)


def _bz_part(loc, key):
    v = loc.get(key)
    return v.get("name") if isinstance(v, dict) else None


def _bz_loc(loc):
    """"Vancouver, British Columbia, Canada" - or just "Canada" when the
    posting gives nothing finer, which Breezy allows and Auctria's remote
    DevOps req actually does."""
    if not isinstance(loc, dict):
        return ""
    out = ", ".join(p for p in (loc.get("city"), _bz_part(loc, "state"),
                                _bz_part(loc, "country")) if p)
    if not out:
        out = loc.get("name") or ""
    if loc.get("is_remote") and "remote" not in out.lower():
        out = "Remote" + (" - " + out if out else "")
    return out


def fetch_breezy(tok):
    d = req("https://%s.breezy.hr/json" % tok)
    out = []
    for j in d if isinstance(d, list) else []:
        locs = (j.get("locations")
                or ([j["location"]] if j.get("location") else []))
        loc = " / ".join(dict.fromkeys(
            x for x in (_bz_loc(l) for l in locs) if x))
        posted = str(j.get("published_date") or "")[:10]
        url = j.get("url") or ""
        company = (j.get("company") or {}).get("name") or tok
        out.append(job("breezy", company, j.get("name", ""), loc, url,
                       posted, days_ago(posted),
                       " ".join([j.get("name") or "", loc,
                                 j.get("department") or "",
                                 (j.get("type") or {}).get("name") or ""]),
                       hyd=("breezy", url)))
    return out


def hydrate(j, kind):
    """Add the description from the posting page's ld+json JobPosting."""
    h = get_text(kind[1])
    for m in BZ_LD.finditer(h):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            j["blob"] += " " + strip_html(d.get("description") or "")[:8000]
            # The list call's published_date is when the req went up; ld+json
            # agrees, but a board that omits it in the list still has it here.
            if not j.get("posted") and d.get("datePosted"):
                j["posted"] = str(d["datePosted"])[:10]
            return
    # No JobPosting block: fall back to the whole page rather than scoring the
    # title alone, which is what a thin blob would amount to.
    j["blob"] += " " + strip_html(h)[:8000]
