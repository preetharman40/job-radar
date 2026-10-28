"""Workday CXS (myworkdayjobs.com).

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import re
import threading
import time

from fetchcore import days_ago, job, req, strip_html


# Workday's GUID for Canada is the same value in every tenant, but the NAME of
# the facet that takes it is configured per tenant - TD calls it
# "locationCountry", CIBC calls it "Country", Sun Life "Location_Country".
# Hardcoding one of them returns HTTP 400 on the others, so we detect it.
WD_CANADA = "a30a87ed25634629aa6c3958aa2b91ea"
_WD_FACET_CACHE = {}

WD_AGE = re.compile(r"(\d+)\+?\s*(day|month|week)", re.I)


def wd_age(p):
    if not p:
        return None
    p = p.lower()
    if "today" in p or "just posted" in p:
        return 0
    if "yesterday" in p:
        return 1
    m = WD_AGE.search(p)
    return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()] if m else None


# Some tenants send NO locationsText at all. Thomson Reuters sends it empty on
# every one of its 78 postings, which classified them all as location "" ->
# bucket OTHER -> keep=False, so a board we have polled for weeks contributed
# nothing and said nothing. Accenture does the same across 156 Canadian reqs.
#
# Two other fields carry the geography, and NEITHER is sufficient alone:
#
#   bulletFields   Thomson Reuters: ["Toronto", "Ontario", "JREQ204759"] - the
#                  complete city list, including every city of a multi-site
#                  req. Accenture: ["R00360485", "Location Negotiable"] - the
#                  useful half is missing and the req id is in slot 0, so a
#                  fixed index cannot be used either.
#   externalPath   Accenture: /job/Ottawa-150-Elgin-St-Corp/... - the real
#                  office. Thomson Reuters: a multi-site req open in Toronto
#                  reads /job/United-States-of-America-Eagan-Minnesota/...,
#                  which classifies US-ONLY and would DROP a Toronto job.
#
# So both are joined and the classifier is left to decide. Measured over the
# live boards with the Canada facet applied: Accenture 19/19 and Thomson
# Reuters 14/14 come out CA, including a "Cloud DevOps Specialist" and a "Lead
# Cloud Platform Engineer, Azure" that were previously discarded.
#
# Req ids are dropped by SHAPE, not by position: upper-case alphanumerics with
# at least one digit and no lower-case letters. That matches JREQ204759,
# R00360485 and ATCI-5447017-S1998490 while leaving "Toronto - 100 Adelaide St
# W" alone, which has lower-case letters.
WD_REQID = re.compile(r"^[A-Z0-9][A-Z0-9_.\-]*\d[A-Z0-9_.\-]*$")


def _wd_path_location(path):
    """The location segment of /job/<Location>/<Title>_<ReqId>.

    Dashes become spaces: the classifier reads
    "United-States-of-America-Eagan-Minnesota" as OTHER but
    "United States of America Eagan Minnesota" as US-ONLY, and getting a US
    posting correctly labelled US is the whole point of including the path.
    """
    parts = [p for p in (path or "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "job":
        return re.sub(r"[-_]+", " ", parts[1]).strip()
    return ""


def _dedupe_words(s):
    """Drop repeated words, keeping order, so the joined location reads like a
    place: Thomson Reuters' bullets and path together give "Toronto, Ontario,
    Canada Toronto Ontario", which this reduces to "Toronto, Ontario, Canada".
    """
    seen, out = set(), []
    for w in s.replace(",", " , ").split():
        k = w.lower().strip(",;")
        if not k or k == ",":
            out.append(w)
            continue
        if k in seen:
            continue
        seen.add(w.lower().strip(",;"))
        out.append(w)
    return re.sub(r"\s+,", ",", " ".join(out)).strip(" ,")


def wd_location(p):
    """A posting's location, for tenants that omit locationsText."""
    txt = (p.get("locationsText") or "").strip()
    if txt:
        return txt
    bits = [b.strip() for b in (p.get("bulletFields") or [])
            if b and b.strip() and not WD_REQID.match(b.strip())]
    tail = _wd_path_location(p.get("externalPath"))
    if tail:
        bits.append(tail)
    return _dedupe_words(", ".join(bits))


def _wd_country_facet(api):
    """Ask the tenant which facet parameter carries Country, and cache it."""
    if api in _WD_FACET_CACHE:
        return _WD_FACET_CACHE[api]
    name = None
    try:
        d = req(api, {"appliedFacets": {}, "limit": 1, "offset": 0,
                       "searchText": ""})

        def walk(vals):
            for v in vals or []:
                desc = (v.get("descriptor") or "").strip().lower()
                if desc in ("country", "location country", "job country",
                            "pays", "country/region") and v.get("facetParameter"):
                    return v["facetParameter"]
                hit = walk(v.get("values"))
                if hit:
                    return hit
            return None

        name = walk(d.get("facets"))
    except Exception:
        pass
    _WD_FACET_CACHE[api] = name
    return name


# Enterprise Workday tenants carry thousands of Canadian reqs (TD alone has
# ~1,029). Paging all of them costs ~51 requests per tenant to then throw 98%
# away. Ask the server for the roles we want instead.

def probe_canada(api):
    """Is this a live Workday jobs API, and how many Canadian reqs has it?

    -> {"country_facet": <name or None>, "ca": <count>} or None.

    The facet parameter that carries Country is named per tenant, so a probe
    has to detect it before it can filter; hunt.py had both halves of this
    inlined twice. Keeping it here means the discovery probe and the sweep
    agree about what a Workday board is.
    """
    facet = _wd_country_facet(api)
    try:
        d = req(api, {"appliedFacets": {facet: [WD_CANADA]} if facet else {},
                      "limit": 1, "offset": 0, "searchText": "engineer"})
    except Exception:
        return None
    return {"country_facet": facet, "ca": d["total"]} \
        if isinstance(d.get("total"), int) else None


WD_QUERIES = ["devops", "site reliability", "platform engineer", "cloud engineer",
              "infrastructure engineer", "kubernetes", "linux", "support engineer",
              "automation engineer", "systems engineer",
              # Endpoint/device-management work is a weight-4 title family and a
              # weight-4 skill family, but none of the terms above surface it -
              # Workday matches job text, and an "Endpoint Engineer" req rarely
              # says "devops" or "kubernetes". Without these three, roles like
              # TD's Senior MDM Engineer and CIBC's Endpoint Engineer were
              # invisible on every one of the 31 Workday tenants.
              "endpoint", "intune", "modern workplace"]


# Workday rate-limits per TENANT, not per request. The sweep fans every
# WD_QUERIES term out concurrently, so a single tenant could see 13 simultaneous
# requests - and at 31 tenants x 13 terms that is 403 requests a sweep, with the
# fast lane adding ~180 more at :00 and :30. Measured result: bursts of 60+
# "HTTP 429 Too Many Requests" in radar.log, on productive boards - Moneris,
# Interac and Ledcor all contribute matches and all got throttled.
#
# A semaphore per tenant caps the burst without serialising it completely;
# full serialisation would be far too slow, since one RBC query alone can take
# 13s. Different tenants still run fully in parallel.
# This semaphore is PER PROCESS, which is the trap. fast.sh and daily.sh are
# separate processes, so a cap of 6 allowed 6 concurrent requests each - 12 to
# the same tenant whenever the two lanes overlapped. After the cron window was
# widened to 7-19 daily, radar.log showed 285 rate-limit errors on Sunday alone,
# clustered precisely at :01 and :31 (5.4 and 7.3 per sweep) while every other
# cron slot sat near zero. Ledcor, Moneris and Interac took the worst of it, and
# all three produce matches, so the throttling was costing real postings.
#
# Two fixes together: the lanes are now offset in cron (daily.sh at :07/:37) so
# they never start at the same minute, and this drops to 3 - which measured 0
# errors in testing, and leaves headroom of 6 even if the two lanes do collide.
# The cost is about 47s on a full sweep, which a 30-minute cadence absorbs.
WD_TENANT_BURST = 3
_WD_SEM = {}
_WD_SEM_LOCK = threading.Lock()

# Workday hard-caps `limit` at 20 and answers HTTP 400 Bad Request above it -
# loudly, unlike Amazon's result_limit=500 returning hits:0 or SmartRecruiters'
# totalFound:0 - so paging is the only way to read a board past 20 rows.
#
# This used to be `while offset < 40`, a blind two-page ceiling, which silently
# truncated every query with more than 40 Canadian hits. Measured on the day it
# was fixed: Manulife lost rows on 4 of 13 queries (automation engineer 61,
# support engineer 59, platform engineer 50, systems engineer 41) and RBC on 6
# of 13 (support engineer 93, modern workplace 80, platform engineer 70, systems
# engineer 65, automation engineer 60, cloud engineer 42). A req whose title and
# description miss all 13 WD_QUERIES terms was unreachable regardless, but one
# that merely ranked 41st for a broad term was being dropped for no reason.
#
# Paging to `total` costs less than it looks, because the old code also fetched
# a pointless second page whenever total was 21+: Manulife goes 18 -> 22
# requests and RBC 22 -> 31. It adds no burst pressure either, since _wd_sem is
# held across the whole function, so extra pages lengthen one slot rather than
# widening concurrency against a tenant that rate-limits per tenant.
# The list call collapses a multi-location requisition to the literal string
# "2 Locations" / "3 Locations", with no city anywhere in the row. That string
# classifies as MULTI and is KEPT, which is the right default - it might include
# Canada - but it means a req open only in Bratislava and Madrid arrives looking
# like a candidate, and the digest shows "2 Locations" where a city belongs.
#
# The detail call (already fetched by the hydrate pass, so this costs nothing
# extra) carries the real set: jobPostingInfo.location is the primary and
# .additionalLocations is the rest. So the placeholder is repaired in hydrate
# and the geography fields derived from it in pass 1 are recomputed, rather than
# guessing a city out of the externalPath slug - a slug cannot be split
# reliably, since the hyphen is both the separator and part of real names
# ("Toronto-Ontario" vs "Los-Angeles-Del-Rey" vs "Saint-Laurent").
WD_NLOC = re.compile(r"^\s*\d+\s+Locations?\s*$", re.I)

WD_PAGE = 20
# 300 rows per query, overridable per target with "max_pages".
#
# Sized from the deepest query on each large tenant: TD Bank 123 ("platform
# engineer"), RBC 93, BMO 84, Thomson Reuters 67, Manulife 61 - so 7 pages
# covers every tenant whose search actually discriminates, and 15 leaves room.
#
# CIBC is the one board that exceeds it, and deliberately is not chased. Its
# searchText works (devops -> 5, a nonsense term -> 0) but "linux" matches 489
# of its 490 Canadian reqs, because the word sits in CIBC's posting boilerplate
# rather than in 489 Linux jobs - page one of that query is Mobile Mortgage
# Advisors. Raising the global ceiling to 25 to swallow it would add ~130
# requests a sweep to read retail-banking listings that title_match rejects at
# tier 0, while the DevOps-titled CIBC reqs are already reachable through the
# cheap "devops" query. So CIBC reports a truncation every sweep and that is the
# correct outcome, not a bug to silence - and if a tenant ever genuinely needs
# deeper paging, give that one target "max_pages" instead of moving this.
WD_MAX_PAGES = 15

# Queries that hit WD_MAX_PAGES with rows still unread. Silent truncation is the
# bug being fixed here, so hitting the new ceiling is recorded and surfaced in
# the run footer instead of being swallowed a second time.
WD_TRUNCATED = []
_WD_TRUNC_LOCK = threading.Lock()


def _wd_sem(tenant):
    with _WD_SEM_LOCK:
        if tenant not in _WD_SEM:
            _WD_SEM[tenant] = threading.BoundedSemaphore(WD_TENANT_BURST)
        return _WD_SEM[tenant]


def fetch_workday(t):
    with _wd_sem(t.get("tenant", "")):
        """Fetch ONE keyword query against one tenant.

        main() expands each Workday target into one task per query so the shared
        thread pool parallelises them; doing all ten queries inline made a 9-tenant
        run take minutes.
        """
        tenant, cluster, site = t["tenant"], t["cluster"], t["site"]
        base = f"https://{tenant}.{cluster}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        facet = t.get("country_facet") or _wd_country_facet(api)
        applied = {facet: [WD_CANADA]} if facet else {}
        name = t.get("name", tenant)
        query = t.get("_query", "")

        # An explicit per-target max_pages is a DELIBERATE shallow read, so it
        # must not be reported as accidental truncation. PwC is the case this
        # exists for: it has no country facet, so every query searches 3,859
        # global reqs and four of them hit the ceiling. Warning about that every
        # half hour would train the reader to ignore WD_TRUNCATED, which is the
        # register that caught CIBC. Only the global default warns.
        want = t.get("max_pages")
        max_pages = int(want or WD_MAX_PAGES)
        shallow_by_choice = bool(want)
        out, offset, total = [], 0, None
        for _ in range(max_pages):
            d = req(api, {"appliedFacets": applied, "limit": WD_PAGE,
                           "offset": offset, "searchText": query})
            posts = d.get("jobPostings", [])
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath", "")
                loc = wd_location(j)
                out.append(job("workday", name, j.get("title", ""), loc,
                               f"{base}/en-US/{site}{path}",
                               j.get("postedOn", ""), wd_age(j.get("postedOn")),
                               f"{j.get('title','')} {loc} {j.get('remoteType','')}",
                               hyd=("workday", f"{base}/wday/cxs/{tenant}/{site}{path}")))
            # `total` is reported only on page 1; later pages say 0, so capture
            # it once and page to it instead of guessing a ceiling.
            if total is None:
                total = d.get("total") or 0
            offset += WD_PAGE
            # A short page is the end of the result set whatever `total` says -
            # checked first so a tenant that omits or zeroes `total` still pages
            # correctly rather than stopping after one page.
            if len(posts) < WD_PAGE:
                break
            if total and offset >= total:
                break
        else:
            if total and total > offset and not shallow_by_choice:
                with _WD_TRUNC_LOCK:
                    WD_TRUNCATED.append(
                        f"workday/{name} · {query!r}: read {offset} of {total} "
                        f"({max_pages} pages)")
        return out


def expand(tok):
    """One task per search term: Workday has no "all IT jobs" view, so the
    board is swept once per query and the results de-duplicated downstream."""
    return [{**tok, "_query": q} for q in WD_QUERIES]

def hydrate(j, kind):
    """The posting detail carries the real description, the exact start date,
    and - behind a "N Locations" placeholder - the actual city list."""
    d = req(kind[1])
    ji = d.get("jobPostingInfo") or {}
    j["blob"] += " " + strip_html(ji.get("jobDescription", ""))[:8000]
    if WD_NLOC.match(j.get("location") or ""):
        locs = [ji.get("location") or ""]
        locs += [x for x in (ji.get("additionalLocations") or []) if x]
        real = " / ".join(dict.fromkeys(x for x in locs if x))
        if real:
            # Only the location text. job_radar re-derives bucket,
            # provinces and arrangement whenever a hydrate changes
            # it, so no adapter here needs the classifier.
            j["location"] = real
    if ji.get("startDate"):
        j["posted"] = ji["startDate"]
        exact = days_ago(ji["startDate"])
        if exact is not None:
            j["age"] = exact
