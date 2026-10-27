"""Teamtailor public jobs API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import days_ago, job, req, strip_html


# The best-shaped platform in this file. `{tenant}.teamtailor.com/jobs.json` is
# a JSON Feed (jsonfeed.org 1.1) and each item carries everything needed in one
# request: a real ISO `date_published`, the full `content_html` description
# inline, and a schema.org JobPosting under `_jobposting` with a structured
# PostalAddress. So like `wpjobs` and `amazon` this needs **no hydrate pass**,
# and unlike `avature` it has genuine dates, so --max-age works normally.
#
# robots.txt allows it: only /app/, /messages/, /messenger/, /facebook/tab/ and
# /jobs/internal/ are disallowed for *, and the host advertises
# `Content-Signal: search=yes, ai-input=yes` (ai-train=no, which this is not).
#
# THE TRAP, and it would have silently cost Alberta postings: `addressCountry`
# is the ISO code, and bare "CA" does not read as Canada to classify_location -
# it only survives when the city itself is in the classifier's Canadian city
# list. Measured: "Toronto, CA" classifies CA/ON, but "Lethbridge, CA",
# "Medicine Hat, CA", "Grande Prairie, CA" and "Nisku, CA" all land in OTHER
# and are DROPPED, while the same cities with ", Canada" classify correctly as
# CA. That is the Amazon Edmonton-suburb failure wearing a different hat - a
# real Alberta req invisible because of how its country was spelled - so the
# code expands the ISO code to a country name before handing it over. Codes
# that are not in the map pass through unchanged, which is harmless: "SE" and
# "DE" reach OTHER either way.
TT_ISO = {"CA": "Canada", "US": "United States", "GB": "United Kingdom",
          "IE": "Ireland", "AU": "Australia", "NZ": "New Zealand",
          "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
          "DE": "Germany", "NL": "Netherlands", "FR": "France", "ES": "Spain",
          "IT": "Italy", "PL": "Poland", "IN": "India", "SG": "Singapore"}

# `addressRegion` is not always a region. Teamtailor's own board files Stockholm
# under region "Sweden" and Chicago under "North America", and pasting that into
# the location string is not merely untidy - it CHANGES THE BUCKET. Measured:
# "Chicago, North America, United States" classifies NA-REMOTE and is KEPT,
# because "North America" reads as a remote-anywhere-in-NA signal, so an on-site
# Illinois job would have been shown as a remote opportunity. Continents and a
# region that just repeats the country are dropped for that reason.
TT_SKIP_REGION = frozenset((
    "north america", "south america", "latin america", "central america",
    "americas", "europe", "asia", "africa", "oceania", "antarctica",
    "middle east", "emea", "apac", "latam", "anz", "nordics", "worldwide",
    "global", "remote"))

TT_PER_PAGE = 100
# 1,000 postings. `per_page` was honoured up to 500 on test tenants and paging
# ends with an EMPTY page (per_page=3 over 5 jobs gives 3, then 2, then 0).
# The loop therefore stops on an empty page and on seeing no new ids - never on
# a SHORT page, because a server that silently capped per_page below the asked
# value would make every full page look short and end the sweep after one. That
# is precisely how the Workday 40-row cap hid itself.
TT_MAX_PAGES = 10


def fetch_teamtailor(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    out, seen = [], set()
    for page in range(1, TT_MAX_PAGES + 1):
        try:
            d = req(f"{base}/jobs.json?per_page={TT_PER_PAGE}&page={page}")
        except Exception:
            break
        items = d.get("items") or []
        if not items:
            break
        fresh = 0
        for it in items:
            key = it.get("id") or it.get("url")
            if not key or key in seen:
                continue
            seen.add(key)
            fresh += 1
            jp = it.get("_jobposting") or {}
            posted = str(it.get("date_published") or jp.get("datePosted") or "")[:10]
            locs = []
            for L in (jp.get("jobLocation") or []):
                a = (L or {}).get("address") or {}
                cc = (a.get("addressCountry") or "").strip()
                country = TT_ISO.get(cc.upper(), cc)
                region = (a.get("addressRegion") or "").strip()
                if region.lower() in TT_SKIP_REGION or \
                        region.lower() == country.lower() or \
                        region.lower() == cc.lower():
                    region = ""
                parts = [a.get("addressLocality"), region, country]
                one = ", ".join(x for x in parts if x)
                if one:
                    locs.append(one)
            loc = " / ".join(dict.fromkeys(locs))
            body = strip_html(it.get("content_html") or jp.get("description") or "")
            title = strip_html(it.get("title") or jp.get("title") or "").strip()
            if not title:
                continue
            out.append(job("teamtailor", name, title, loc,
                           it.get("url", ""), posted, days_ago(posted),
                           f"{title} {loc} {body[:8000]}"))
        if not fresh:
            break
    return out
