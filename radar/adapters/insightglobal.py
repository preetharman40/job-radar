"""Insight Global (staffing agency) jobs API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from fetchcore import days_ago, job, req, strip_html


# Insight Global - a staffing agency running its own job board, not an ATS
# product, so harvest.py and discover.py --company can never fingerprint it.
#
# `size` is CAPPED AT 50 and silently clamps: ask for 500 and the response says
# `"size": 50`. Reading pageMetadata.totalPages rather than trusting the request
# is what keeps paging honest here.
IG_BASE = "https://insightglobal.com"
IG_PER_PAGE = 50
IG_MAX_PAGES = 14          # 700 postings per province; ON, the busiest, is 192

# Province codes, used as the server-side location facet (see fetch_insightglobal).
# Not one of these collides with a US state code - Nebraska is NE, Pennsylvania
# is PA - which is what makes the facet safe to drive from this list.
IG_PROVINCES = ("ON", "BC", "AB", "QC", "MB", "SK", "NS", "NB",
                "NL", "PE", "YT", "NT", "NU")


def fetch_insightglobal(t):
    """Enumerate the agency's Canadian reqs by province, not by keyword.

    Two findings shaped this, both measured against the live API:

    1. KEYWORD SEARCH IS THE WRONG AXIS HERE. The endpoint ORs loosely, so
       `platform engineer` reports 1,486 results and `systems engineer` 1,567 -
       mostly US - while the *entire* Canadian inventory is 282 reqs. Running
       our 13 standard queries cost 78 requests, truncated most of them, and
       still found only 60 Canadian rows. Enumerating the 13 provinces costs
       ~11 requests, returns all 282, and lets TITLE_RULES do the filtering it
       already does better than a remote relevance ranker. Fewer requests,
       complete recall, no truncation register needed.

    2. `administrativeArea` IS NOT COUNTRY-SAFE. In this data `CA` means
       California - 22 US cities carried it - and the feed also contains Indian
       state codes (TG, KA, MH, AN) and spelled-out Brazilian states. So the
       country filter MUST be the server-side `filter.locations` facet, which is
       exact: `ON` returns Ontario only, with no California leakage. Pattern
       matching that field locally would have swept 22 California jobs in as
       Canadian. classify_location() reads `San Diego, CA` as OTHER and would
       have caught most of it downstream, but the right place to be correct is
       here, before 282 rows become 500.

    The list call carries no description - only the per-req summary endpoint
    does - so this sets `hyd` and the full JD arrives in pass 2 for finalists
    only, the same bargain greenhouse and ashby make.

    `company` is the agency, not the client: these are placements and the client
    is usually withheld ("A leading home healthcare organization"), so naming
    Insight Global is the only honest label available.
    """
    name = t.get("name", "Insight Global")
    provinces = t.get("provinces") or IG_PROVINCES
    out, seen = [], set()
    for prov in provinces:
        for page in range(1, IG_MAX_PAGES + 1):
            try:
                d = req(f"{IG_BASE}/all/jobs?size={IG_PER_PAGE}&page={page}"
                         f"&filter.status=active&filter.locations={prov}")
            except Exception:
                break
            rows = d.get("jobs") or []
            if not rows:
                break
            for r in rows:
                rid = r.get("requisitionId")
                title = (r.get("jobTitle") or "").strip()
                if not rid or not title or rid in seen:
                    continue
                seen.add(rid)
                addr = r.get("workAddress") or {}
                loc = ", ".join(x for x in (addr.get("locality"),
                                            addr.get("administrativeArea")) if x)
                # workRemote is a real boolean here, and the location string is
                # the city regardless - so without this, a remote req reads as
                # on-site in Toronto and arrangement() never sees "remote".
                if r.get("workRemote"):
                    loc = f"{loc} / Remote" if loc else "Remote"
                posted = str(r.get("postedDate") or "")[:10]
                # Contract vs Perm is the single most useful thing the list call
                # knows that the title does not, and it is worth scoring on.
                blob = " ".join([title, loc, r.get("jobType") or ""])
                out.append(job("insightglobal", name, title, loc,
                               f"{IG_BASE}/jobs/{rid}", posted, days_ago(posted),
                               blob, hyd=("insightglobal", rid)))
            meta = d.get("pageMetadata") or {}
            if page >= (meta.get("totalPages") or page):
                break
    return out


def hydrate(j, kind):
    """The summary endpoint already carries descriptionHtml."""
    # The SUMMARY endpoint already carries descriptionHtml, so there is
    # no reason to fetch /details as well - it returns the same text
    # under a different key. One request per finalist, no HTML scraping.
    d = req(f"{IG_BASE}/all/jobs/{kind[1]}")
    j["blob"] += " " + strip_html(d.get("descriptionHtml") or "")[:8000]
