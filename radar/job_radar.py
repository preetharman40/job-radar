#!/usr/bin/env python3
"""
job_radar.py - Canada DevOps / SRE / vendor-support job radar.

Polls ATS JSON APIs directly - the same endpoints a company's own careers page
calls. A req shows up here seconds after a recruiter clicks Publish, hours to
weeks before LinkedIn, Indeed or Google have it.

Three role families, all scored against your resume:
  1. DevOps / SRE / Platform / Cloud Infrastructure
  2. Support-adjacent infra (build & release, sysadmin, observability, endpoint)
  3. Technical Support / Solutions Engineer *at DevOps tool vendors* - the
     highest-conversion target for a 6-yr-support + 1-yr-DevOps profile

Usage:
    python3 job_radar.py --seed              # first run: record state, print nothing
    python3 job_radar.py                     # only NEW postings since last run
    python3 job_radar.py --all               # everything currently matching
    python3 job_radar.py --level entry,mid   # drop Senior-titled roles
    python3 job_radar.py --family vendor     # only vendor support/solutions roles
    python3 job_radar.py --md digest.md      # write a markdown digest

No third-party dependencies. Python 3.8+.
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import fcntl
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as html_unescape
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "seen.sqlite3")
TARGETS = os.path.join(HERE, "targets.json")

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Content-Type": "application/json",
}

# Workday's GUID for Canada is the same value in every tenant, but the NAME of
# the facet that takes it is configured per tenant - TD calls it
# "locationCountry", CIBC calls it "Country", Sun Life "Location_Country".
# Hardcoding one of them returns HTTP 400 on the others, so we detect it.
WD_CANADA = "a30a87ed25634629aa6c3958aa2b91ea"
_WD_FACET_CACHE = {}

VENDORS = set()   # filled from targets.json at startup

# ------------------------------------------------------------------ titles ---
# (regex, tier, family). Tier drives the base score; family drives --family.

TITLE_RULES = [
    # --- family: devops -------------------------------------------------
    (r"\b(devops|dev-ops)\b",                                   4, "devops"),
    (r"\b(sre|site\s+reliability)\b",                           4, "devops"),
    (r"\bplatform\s+engineer",                                  4, "devops"),
    (r"\b(cloud|infrastructure|infra)\s+"
     r"(engineer|developer|specialist|analyst|administrator)",  4, "devops"),
    (r"\bkubernetes\s+engineer",                                4, "devops"),
    (r"\b(reliability|observability)\s+engineer",               4, "devops"),
    (r"\bmlops\b",                                              3, "devops"),
    (r"\bcloud\s+(operations|ops)\b",                           3, "devops"),

    # --- family: adjacent (infra work that hires from support) ----------
    (r"\b(build|release|automation|tooling)\s+engineer",        3, "adjacent"),
    (r"\bdeveloper\s+(experience|productivity|platform)",       3, "adjacent"),
    (r"\b(linux|unix|systems?)\s+"
     r"(engineer|administrator|admin|analyst)",                 3, "adjacent"),
    (r"\b(endpoint|mdm|intune|workspace\s*one|"
     r"unified\s+endpoint|modern\s+workplace|device\s+management)", 4, "adjacent"),
    (r"\b(noc|network\s+operations)\b",                         2, "adjacent"),
    (r"\bit\s+(engineer|operations|infrastructure)",            2, "adjacent"),

    # --- family: vendor (only counts at a DevOps tool vendor) -----------
    (r"\b(technical|customer|enterprise)\s+support\s+engineer", 4, "vendor"),
    (r"\bsupport\s+engineer\b",                                 4, "vendor"),
    (r"\b(solutions?|sales|field)\s+engineer\b",                3, "vendor"),
    (r"\b(implementation|onboarding|deployment)\s+engineer",    3, "vendor"),
    (r"\b(technical\s+account\s+manager|professional\s+services)", 3, "vendor"),
    (r"\b(customer\s+success\s+engineer|escalation\s+engineer)", 4, "vendor"),
    (r"\bdeveloper\s+(advocate|relations)\b",                   2, "vendor"),
]

# Straight off the resume. Ranks, never filters.
SKILLS = {
    "terraform": 3, "kubernetes": 3, "k8s": 2, "docker": 2, "aws": 2,
    "github actions": 3, "jenkins": 2, "ci/cd": 2, "cicd": 1,
    "grafana": 3, "loki": 3, "prometheus": 2, "observability": 2,
    "python": 2, "bash": 2, "shell scripting": 1, "linux": 2,
    "digitalocean": 2, "infrastructure as code": 2, "iac": 1,
    "ansible": 1, "helm": 2, "argocd": 1, "argo cd": 1, "microservices": 1,
    "incident": 2, "root cause": 2, "rca": 1, "mttr": 2, "on-call": 1,
    "intune": 4, "workspace one": 4, "mdm": 3, "endpoint": 2,
    "servicenow": 2, "jira": 1, "escalation": 2, "troubleshooting": 2,
    "fastapi": 1, "playwright": 1, "pytest": 1, "rest api": 1,
}

# Levels --------------------------------------------------------------------
L_EXEC = re.compile(
    r"\b(staff|principal|distinguished|architect|director|head\s+of|"
    r"vp\b|vice\s+president|manager|lead\b|fellow)\b", re.I)
L_SENIOR = re.compile(r"\b(senior|sr\.?|iii|iv)\b", re.I)
L_ENTRY = re.compile(
    r"\b(junior|jr\.?|associate|entry[\s-]?level|graduate|new\s+grad|"
    r"early\s+career|tier\s*1|t1|l1|level\s*1|\bi\b|\bii\b)\b", re.I)
L_INTERN = re.compile(r"\b(intern|internship|co-?op|student|placement)\b", re.I)


def level_of(title):
    t = title or ""
    if L_INTERN.search(t):
        return "intern"
    if L_EXEC.search(t):
        return "exec"
    if L_SENIOR.search(t):
        return "senior"
    if L_ENTRY.search(t):
        return "entry"
    return "mid"


# --------------------------------------------------------------- locations ---

# Cities whose name alone is enough - no non-Canadian city shares them.
CA_CITIES_SAFE = [
    "toronto", "mississauga", "brampton", "markham", "vaughan", "scarborough",
    "north york", "etobicoke", "oakville", "kitchener", "guelph", "oshawa",
    "barrie", "pickering", "ajax", "whitby", "milton", "burnaby", "coquitlam",
    "kelowna", "saskatoon", "regina", "winnipeg", "calgary", "edmonton",
    "montreal", "montréal", "gatineau", "sherbrooke", "laval", "quebec city",
    "québec", "moncton", "fredericton", "charlottetown", "st. john's",
    "mont-royal", "brossard", "longueuil", "ottawa", "waterloo",
]
# Cities that exist in several countries. "Burlington, MA" and "Melbourne,
# Victoria, Australia" were both being read as Canadian - these now need a
# province or "canada" alongside them.
CA_CITIES_AMBIGUOUS = [
    "london", "windsor", "hamilton", "cambridge", "victoria", "burlington",
    "richmond", "kingston", "vancouver", "halifax", "surrey", "waterloo",
    "sudbury", "peterborough", "chatham", "stratford", "newmarket", "aurora",
]
# Substring matching burned us twice: "amer" inside "United States of America"
# and "milton" inside "Hamilton". City and region names are matched on word
# boundaries, never with `in`.
def _word_re(terms):
    return re.compile(r"(?<![a-z])(" + "|".join(
        re.escape(t).replace(r"\ ", r"\s+") for t in terms) + r")(?![a-z])", re.I)


CA_REGIONS = [
    "canada", "canadian", "ontario", "on", "quebec", "québec",
    "qc", "british columbia", "bc", "alberta", "ab", "manitoba", "mb",
    "saskatchewan", "sk", "nova scotia", "ns", "new brunswick", "nb",
    "newfoundland", "nl", "prince edward island",
]
# Bare province codes only count after a comma or bracket - "ON" and "AB" are
# far too common in free text otherwise.
CA_PROVINCE_CODE = re.compile(r"[,(/]\s*(on|qc|bc|ab|mb|sk|ns|nb|nl|pe)\b", re.I)
CA_SAFE_RE = _word_re(CA_CITIES_SAFE)
CA_AMBIG_RE = _word_re(CA_CITIES_AMBIGUOUS)
CA_REGION_RE = _word_re([r for r in CA_REGIONS if len(r) > 2])
# Wordings a Canadian resident can actually hold.
NORTH_AMERICA = re.compile(
    r"north\s+america(n)?|remote\s*[-,]?\s*americas?\b|\bamer\b"
    r"|u\.?s\.?a?\s*(or|/|&|and)\s*canada"
    r"|united\s+states\s+(or|and|/)\s+canada", re.I)
# Reached only after the Canada check fails, so a bare "United States" here
# means the req really is US-only.
US_ONLY = re.compile(
    r"united\s+states|\busa?\b|u\.s\.|us[\s-]+(only|based|remote)", re.I)
MULTI_HINT = re.compile(r"\b\d+\s+locations?\b|\bmultiple locations\b", re.I)

# Province detection, so results can be filtered to where you actually live.
# Word boundaries, for the same reason everything else here uses them.
PROVINCES = {
    "AB": ["alberta", "calgary", "edmonton", "red deer", "lethbridge",
           "fort mcmurray", "grande prairie", "medicine hat", "airdrie",
           "sherwood park", "st. albert", "banff", "canmore", "leduc",
           "spruce grove"],
    "ON": ["ontario", "toronto", "ottawa", "mississauga", "brampton", "markham",
           "vaughan", "scarborough", "north york", "etobicoke", "oakville",
           "kitchener", "waterloo", "guelph", "hamilton", "london", "windsor",
           "kingston", "barrie", "oshawa", "burlington", "cambridge", "milton"],
    "BC": ["british columbia", "vancouver", "burnaby", "surrey", "richmond",
           "victoria", "kelowna", "coquitlam", "langley", "abbotsford"],
    "QC": ["quebec", "qu\u00e9bec", "montreal", "montr\u00e9al", "laval",
           "gatineau", "sherbrooke", "longueuil", "brossard"],
    "MB": ["manitoba", "winnipeg"],
    "SK": ["saskatchewan", "saskatoon", "regina"],
    "NS": ["nova scotia", "halifax"],
    "NB": ["new brunswick", "moncton", "fredericton"],
    "NL": ["newfoundland", "st. john's"],
    "PE": ["prince edward island", "charlottetown"],
}
PROV_RE = {k: _word_re(v) for k, v in PROVINCES.items()}
PROV_CODE = re.compile(r"[,(/]\s*(AB|ON|BC|QC|MB|SK|NS|NB|NL|PE)\b")


def provinces_of(loc):
    """Every Canadian province a posting names. Empty = remote or unclear."""
    if not loc:
        return set()
    out = {k for k, rx in PROV_RE.items() if rx.search(loc)}
    out |= set(PROV_CODE.findall(loc.upper()))
    return out


REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
ONSITE_RE = re.compile(r"\bon[\s-]?site\b|\bin[\s-]?office\b", re.I)


def arrangement(loc, blob=""):
    """remote / hybrid / onsite where the posting actually says so."""
    hay = f"{loc} {blob[:600]}"
    if HYBRID_RE.search(hay):
        return "hybrid"
    if ONSITE_RE.search(hay):
        return "onsite"
    if REMOTE_RE.search(loc):
        return "remote"
    return ""
# Country/state markers that disqualify an ambiguous city outright.
NOT_CANADA = re.compile(
    r"\b(australia|united kingdom|u\.k\.|england|scotland|ireland|"
    r"new zealand|india|singapore|germany|france|netherlands|spain|japan|"
    r"brazil|mexico|philippines|poland|israel|victoria,\s*australia)\b", re.I)
# US state codes are matched CASE-SENSITIVELY against the original string.
# Folding them to lowercase made "or" match the English word in
# "Remote (ON, AB, BC, or NS Only)", which silently dropped 53 Canadian
# postings - every one of them Alberta-eligible.
# ", CA" is deliberately absent: SuccessFactors writes Canadian locations as
# "Toronto, ON, CA". A US city with ", CA" is caught by having no Canadian
# city or region marker at all.
US_STATE_CODE = re.compile(
    r"[,(]\s*(MA|VA|WA|OR|NY|TX|IL|GA|NC|PA|OH|MI|AZ|CO|UT|NJ|MD|MN|MO|"
    r"IN|TN|WI|SC|AL|KY|LA|OK|CT|IA|AR|MS|KS|NV|NM|NE|ID|NH|ME|RI|MT|DE|"
    r"SD|ND|AK|VT|WY|HI|WV|FL)\b")


def classify_location(loc):
    """-> (bucket, keep). CA | NA-REMOTE | MULTI | US-ONLY | OTHER"""
    if not loc:
        return "OTHER", False
    l = loc.lower()
    # An explicit "Canada" wins outright, before any foreign veto. Boards list
    # one requisition under several countries sharing a single URL - an MLabs
    # SRE role read "Remote - United Kingdom / Remote - Poland / ... / Remote -
    # Canada" - and an absolute veto threw away a role that is genuinely open
    # to you.
    if re.search(r"(?<![a-z])canad(a|ian)(?![a-z])", l):
        return "CA", True
    # Country names on the lowercased string; state codes on the original, so
    # an uppercase "OR" is Oregon but a lowercase "or" is just the word.
    foreign = bool(NOT_CANADA.search(l)) or bool(US_STATE_CODE.search(loc))
    has_region = bool(CA_REGION_RE.search(l)) or bool(CA_PROVINCE_CODE.search(l))
    if (CA_SAFE_RE.search(l) or has_region) and not foreign:
        return "CA", True
    # An ambiguous city counts only with a Canadian qualifier and no foreign one.
    if CA_AMBIG_RE.search(l) and has_region and not foreign:
        return "CA", True
    if NORTH_AMERICA.search(l):
        return "NA-REMOTE", True
    if MULTI_HINT.search(l):
        return "MULTI", True          # Workday collapses "3 Locations"
    if US_ONLY.search(l):
        return "US-ONLY", False
    return "OTHER", False


# ----------------------------------------------------------------- scoring ---

def title_match(title, company):
    """-> (tier, family) or (0, None). Vendor titles only count at vendors."""
    t = (title or "").lower()
    is_vendor = company.lower() in VENDORS
    best = (0, None)
    for rx, tier, fam in TITLE_RULES:
        if not re.search(rx, t):
            continue
        if fam == "vendor" and not is_vendor:
            continue
        if tier > best[0]:
            best = (tier, fam)
    return best


def score(title, company, tier, family, blob):
    s = tier * 3
    hits = []
    b = (blob or "").lower()
    for kw, w in SKILLS.items():
        if kw in b:
            s += w
            hits.append(kw)

    lvl = level_of(title)
    s += {"entry": 4, "mid": 2, "senior": -2, "exec": -10, "intern": -8}[lvl]

    # A vendor support role is the thesis of this search - weight it like one.
    if family == "vendor":
        s += 4
        if re.search(r"\b(kubernetes|linux|cloud|devops|platform|infra)\b",
                     (title or "").lower()):
            s += 2
    return s, sorted(set(hits))[:10], lvl


# --------------------------------------------------------------- transport ---

# Python 3.10's urllib handles 301/302/303/307 but NOT 308 - a permanent
# redirect just raises HTTPError. Instacart's careers host uses 308, which made
# a live posting look unreachable. Teach the default opener to follow it.
class _Redirect308(urllib.request.HTTPRedirectHandler):
    # Overriding http_error_308 alone is not enough: redirect_request itself
    # whitelists 301/302/303/307 and raises on anything else.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code == 308:
            code = 301
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, code, msg, headers)


urllib.request.install_opener(urllib.request.build_opener(_Redirect308))


def _req(url, data=None, timeout=25, retries=2):
    """Retry on transport failures - at 400+ concurrent tasks a single blip
    would otherwise silently drop a whole board from the results."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(
                url, data=json.dumps(data).encode() if data is not None else None,
                headers=UA, method="POST" if data is not None else "GET")
            with urllib.request.urlopen(r, timeout=timeout) as f:
                return json.loads(f.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    raise last


def _get_text(url, timeout=25):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.read().decode("utf-8", "replace")


_TAG = re.compile(r"<[^>]+>")
_ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
        "&nbsp;": " ", "&rsquo;": "'", "&ndash;": "-", "&mdash;": "-"}


def strip_html(h):
    if not h:
        return ""
    t = _TAG.sub(" ", h)
    for k, v in _ENT.items():
        t = t.replace(k, v)
    return re.sub(r"\s+", " ", t)


def days_ago(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return None


def job(src, company, title, loc, url, posted=None, age=None, blob="", hyd=None,
        updated=None):
    """`posted` is true publication. `updated` is last-modified where the
    platform exposes it - an old req edited recently often means the recruiter
    is actively working it, which a publication date alone does not show."""
    return dict(source=src, company=company, title=title, location=loc or "",
                url=url, posted=posted or "", age=age, blob=blob, hyd=hyd,
                updated=updated or "", offices=[])


def office_mismatch(j):
    """True when the location text names a Canadian city the office tags omit.

    Seen on PagerDuty's SRE I: location "Atlanta; Toronto", offices ["Atlanta"].
    The role really is Toronto-eligible - the application form asks which office
    you live near - but PagerDuty's careers page files it under Atlanta, so it is
    invisible to anyone filtering that page by Toronto. Worth surfacing, not
    hiding: fewer applicants ever see these."""
    offs = j.get("offices") or []
    if not offs:
        return None
    l = j["location"].lower()
    blob = " ".join(offs).lower()
    for city in ("toronto", "vancouver", "montreal", "ottawa", "calgary",
                 "waterloo", "mississauga", "edmonton", "halifax"):
        if city in l and city not in blob:
            return f"location says {city.title()} but office tag is "\
                   f"{'/'.join(offs)} - confirm on the job page"
    return None


# ------------------------------------------------- adapters (pass 1: light) ---

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


def fetch_lever(tok):
    d = _req(f"https://api.lever.co/v0/postings/{tok}?mode=json")
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


# The board-level type (JobPostingBriefsWithIdsAndTeamId) exposes NO date field
# at all - publishedDate lives only on the single-posting type. Asking for it
# here makes the whole query error and the adapter return an empty list, which
# silently zeroed every Ashby board. Dates come from the hydrate call instead.
ASHBY_LIST = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!)"
              " { jobBoard: jobBoardWithTeams("
              "organizationHostedJobsPageName: $organizationHostedJobsPageName)"
              " { jobPostings { id title locationName "
              "secondaryLocations { locationName } } } }")


def fetch_ashby(tok):
    d = _req("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
             {"operationName": "ApiJobBoardWithTeams",
              "variables": {"organizationHostedJobsPageName": tok},
              "query": ASHBY_LIST})
    jb = (d.get("data") or {}).get("jobBoard") or {}
    out = []
    for j in jb.get("jobPostings", []):
        locs = [j.get("locationName") or ""] + [
            (s or {}).get("locationName", "") for s in (j.get("secondaryLocations") or [])]
        loc = " / ".join(x for x in locs if x)
        out.append(job("ashby", tok, j.get("title", ""), loc,
                       f"https://jobs.ashbyhq.com/{tok}/{j.get('id')}",
                       "", None, f"{j.get('title','')} {loc}",
                       hyd=("ashby", tok, j.get("id"))))
    return out


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


def fetch_rippling(tok):
    d = _req(f"https://api.rippling.com/platform/api/ats/v1/board/{tok}/jobs")
    out = []
    for j in d if isinstance(d, list) else []:
        loc = j.get("workLocation", {}).get("label", "") or j.get("locationName", "")
        out.append(job("rippling", tok, j.get("name", ""), loc,
                       j.get("url", ""), "", None, f"{j.get('name','')} {loc}"))
    return out


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


def _wd_country_facet(api):
    """Ask the tenant which facet parameter carries Country, and cache it."""
    if api in _WD_FACET_CACHE:
        return _WD_FACET_CACHE[api]
    name = None
    try:
        d = _req(api, {"appliedFacets": {}, "limit": 1, "offset": 0,
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
# 6 halves the worst-case burst (13 concurrent -> 6) for about 10s of sweep
# time; 3 costs 47s for little extra safety. Tuning this empirically is not
# possible in isolation - Workday only throttles when something else is hitting
# the same tenant at the same moment, so a quiet test returns 0 at every value.
WD_TENANT_BURST = 6
_WD_SEM = {}
_WD_SEM_LOCK = threading.Lock()


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

        out, offset = [], 0
        while offset < 40:            # 2 pages per query
            d = _req(api, {"appliedFacets": applied, "limit": 20,
                           "offset": offset, "searchText": query})
            posts = d.get("jobPostings", [])
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath", "")
                loc = j.get("locationsText", "")
                out.append(job("workday", name, j.get("title", ""), loc,
                               f"{base}/en-US/{site}{path}",
                               j.get("postedOn", ""), wd_age(j.get("postedOn")),
                               f"{j.get('title','')} {loc} {j.get('remoteType','')}",
                               hyd=("workday", f"{base}/wday/cxs/{tenant}/{site}{path}")))
            # `total` is reported only on page 1; later pages say 0.
            if offset == 0 and d.get("total", 0) <= 20:
                break
            offset += 20
        return out


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
                           updated=reposted if reposted != created else ""))
        frm += 10
        if total and frm >= total:
            break
    return out


# --- SAP SuccessFactors (RMK career sites) ----------------------------------
# Rogers, Scotiabank, Bombardier and much of Canadian enterprise. No JSON API
# and no RSS - the search page renders results server-side, which is fine: the
# markup is stable and `sortColumn=referencedate` gives newest-first.

SF_ROW = re.compile(r'<tr class="data-row".*?</tr>', re.S)
# SuccessFactors ships at least two themes. City of Toronto uses a tile layout
# (<li class="job-tile">) with no location column - the location lives in the
# URL slug instead, e.g. /job/Toronto-CIVIL-ENGINEERING-TECHNOLOGIST-ON-M5V3C6/
SF_TILE = re.compile(r'<li class="job-tile\b.*?</li>', re.S)
SF_TILE_DATE = re.compile(r'section-field date[^>]*>(.*?)</div>\s*</div>', re.S)
SF_SLUG_LOC = re.compile(
    r"/job/([A-Za-z\u00C0-\u017F'.\-]+?)-.*?-(ON|QC|BC|AB|MB|SK|NS|NB|NL|PE)"
    r"(?:-[A-Z0-9]+)?/", re.I)
# Attribute order differs between the two themes: the table layout puts href
# first, the tile layout puts class first. Match either.
SF_LINK = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*jobTitle-link)[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>',
    re.S)
SF_LOC = re.compile(r'<span class="jobLocation">\s*(.*?)\s*</span>', re.S)
SF_DATE = re.compile(r'<span class="jobDate[^"]*">\s*(.*?)\s*</span>', re.S)
SF_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# SuccessFactors ORs multi-word queries into noise ("site reliability" returns
# anything matching "site"). Single tokens only.
SF_QUERIES = ["devops", "sre", "reliability", "kubernetes", "linux", "cloud",
              "infrastructure", "platform", "automation", "support"]


def _sf_text(x):
    return re.sub(r"\s+", " ", html_unescape(_TAG.sub("", x))).strip()


def _sf_date(x):
    """'Sep 16, 2026' -> '2026-09-16'."""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", x or "")
    if not m:
        return ""
    mon = SF_MONTHS.get(m.group(1))
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}" if mon else ""


def fetch_successfactors(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    query = t.get("_query", "")
    out, start = [], 0
    while start < 75:                       # 3 pages of 25 per query
        # Adding sortColumn makes SuccessFactors ignore q= entirely and return
        # everything by date, so rely on the query and sort locally by age.
        url = (f"{base}/search/?q={urllib.parse.quote(query)}"
               f"&startrow={start}")
        try:
            h = _get_text(url)
        except Exception:
            break
        rows, tiled = SF_ROW.findall(h), False
        if not rows:
            rows, tiled = SF_TILE.findall(h), True
        if not rows:
            break
        for r in rows:
            link = SF_LINK.search(r)
            if not link:
                continue
            href = html_unescape(link.group(1))
            title = _sf_text(link.group(2))
            if tiled:
                m = SF_SLUG_LOC.search(href)
                loc = (f"{m.group(1).replace('-', ' ')}, {m.group(2).upper()}"
                       if m else t.get("default_location", ""))
                d = SF_TILE_DATE.search(r)
                raw = _sf_text(d.group(1)).replace("Posting Date", "").strip() if d else ""
            else:
                loc_m = SF_LOC.search(r)
                loc = _sf_text(loc_m.group(1)) if loc_m else ""
                d = SF_DATE.search(r)
                raw = _sf_text(d.group(1)) if d else ""
            # The date SuccessFactors shows is the RMK "Reference Date", NOT a
            # posting date. SAP's own docs say it is set on import then cycled
            # every 28 days to keep content fresh for SEO, and that the real
            # RCM posting date is not exposed on the career site. Verified: on
            # four of five tenants NOTHING on the whole board is older than
            # 28-29 days. Treat it as unknown age and carry the reference date
            # separately rather than reporting a number that means nothing.
            refdate = _sf_date(raw)
            posted = ""
            full = href if href.startswith("http") else base + href
            out.append(job("successfactors", name, title, loc, full,
                           posted, None, f"{title} {loc}",
                           hyd=("successfactors", full),
                           updated=refdate))
        if len(rows) < 25:
            break
        start += 25
    return out


# --- Oracle Recruiting Cloud (ORC) -------------------------------------------
# Used by universities, utilities and energy companies. A proper JSON REST API
# with real posted dates - far better than the HTML-parsing adapters. The
# careers URL looks like:
#   https://<host>/hcmUI/CandidateExperience/en/sites/<SITE>/jobs
# and the site number (usually CX_1) is in that page's markup.

ORC_QUERIES = ["devops", "cloud", "infrastructure", "systems", "network",
               "engineer", "analyst", "linux", "security", "platform"]


def fetch_oracle(t):
    host = t["host"].rstrip("/")
    site = t.get("site_number", "CX_1")
    name = t.get("name", host)
    q = t.get("_query", "")
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList"
           f"&finder=findReqs;siteNumber={site},limit=50,sortBy=POSTING_DATES_DESC")
    if q:
        api += f",keyword={urllib.parse.quote(q)}"
    d = _req(api)
    items = d.get("items") or []
    reqs = items[0].get("requisitionList", []) if items else []
    out = []
    for r in reqs:
        loc = r.get("PrimaryLocation") or r.get("PrimaryLocationCountry") or ""
        posted = str(r.get("PostedDate") or "")[:10]
        rid = r.get("Id")
        out.append(job("oracle", name, r.get("Title", ""), loc,
                       f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                       f"{t.get('site', 'CX_1')}/job/{rid}",
                       posted, days_ago(posted),
                       " ".join(filter(None, [r.get("Title", ""), loc,
                                              r.get("JobFamily") or "",
                                              r.get("JobFunction") or "",
                                              r.get("WorkplaceTypeCode") or ""]))))
    return out


# --- Jobvite -----------------------------------------------------------------
# Server-rendered HTML, no JSON API, but the markup is stable and simple:
#   <td class="jv-job-list-name"><a href="/<tenant>/job/<id>">Title</a></td>
#   <td class="jv-job-list-location">City, Province</td>
# The list carries no date; each job page has schema.org "datePosted", so dates
# come from the hydrate pass.

JV_ROW = re.compile(
    r'class="jv-job-list-name">\s*<a href="([^"]+)">(.*?)</a>'
    r'.*?class="jv-job-list-location">\s*(.*?)\s*</td>', re.S)
JV_DATE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')

JV_QUERIES = ["devops", "cloud", "infrastructure", "systems", "engineer",
              "linux", "network", "analyst", "security", "support"]


def fetch_jobvite(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    q = t.get("_query", "")
    url = f"{base}/search" + (f"?q={urllib.parse.quote(q)}" if q else "")
    try:
        h = _get_text(url)
    except Exception:
        return []
    out = []
    for href, title, loc in JV_ROW.findall(h):
        title = re.sub(r"\s+", " ", strip_html(title)).strip()
        # Jobvite wraps locations across lines: "Round Rock,\n   Texas"
        loc = re.sub(r"\s+", " ", strip_html(loc)).strip()
        if not title:
            continue
        full = href if href.startswith("http") else \
            "https://jobs.jobvite.com" + href
        out.append(job("jobvite", name, title, loc or t.get("default_location", ""),
                       full, "", None, f"{title} {loc}",
                       hyd=("jobvite", full)))
    return out


# --- Generic HTML scrape -----------------------------------------------------
# Last resort for careers sites that render their job list server-side but run
# no recognisable ATS. Rather than a bespoke parser per employer, this pulls
# every anchor that looks like a job posting and takes surrounding table cells
# as location/date. Configured entirely in targets.json:
#
#   {"name": "City of Calgary",
#    "url": "https://www.calgary.ca/careers.html",
#    "link_re": "recruiting\\.calgary\\.ca",      # hrefs that are real postings
#    "default_location": "Calgary, AB"}
#
# Fragile by nature - a redesign breaks it. discover.py --verify catches that.

GEN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
GEN_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
GEN_LINK = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
GEN_DATE = re.compile(r"(\d{4})[/-](\d{2})[/-](\d{2})")


def fetch_html(t):
    url = t["url"]
    name = t.get("name", url)
    keep = re.compile(t["link_re"], re.I) if t.get("link_re") else None
    default_loc = t.get("default_location", "")
    try:
        h = _get_text(url)
    except Exception:
        return []

    out, seen = [], set()
    for row in GEN_ROW.findall(h):
        link = GEN_LINK.search(row)
        if not link:
            continue
        href, anchor = html_unescape(link.group(1)), strip_html(link.group(2)).strip()
        if keep and not keep.search(href):
            continue
        if not anchor or len(anchor) < 4 or len(anchor) > 140:
            continue
        full = href if href.startswith("http") else urllib.parse.urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        cells = [strip_html(c).strip() for c in GEN_CELL.findall(row)]
        # A closing date is not a posting date - only take a date if the cell
        # says so, otherwise leave age unknown rather than inventing one.
        posted = ""
        for c in cells:
            if re.search(r"post(ed|ing date)", c, re.I):
                m = GEN_DATE.search(c)
                if m:
                    posted = "-".join(m.groups())
        loc = default_loc
        for c in cells:
            if re.search(r"\b(AB|ON|BC|QC)\b|alberta|ontario|calgary|edmonton", c, re.I) \
                    and len(c) < 60 and "date" not in c.lower():
                loc = c
                break
        out.append(job("html", name, anchor, loc, full, posted,
                       days_ago(posted) if posted else None,
                       f"{anchor} {loc} {' '.join(cells)[:400]}"))
    return out


# --- Workable -----------------------------------------------------------------
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


# SmartRecruiters sorts every listing newest-first, so capping pages keeps the
# freshest rather than an arbitrary slice. Five pages covers the largest tenants
# we track (SGS posts 4,506 reqs; its newest 100 still span only two days).
SR_MAX_PAGES = 5


def fetch_smartrecruiters(tok):
    """SmartRecruiters publishes two APIs and only one of them is usable.

    The cross-company search (jobs.smartrecruiters.com/sr-jobs/search) silently
    ignores every location and paging parameter it is given - `location=Canada`,
    `country=ca`, `filter=country:ca` and `offset=1000` all return the identical
    first ~96 global rows - so it can only ever see a fixed window of the newest
    postings worldwide. It is good for harvesting tenant identifiers and nothing
    else; this per-company endpoint pages correctly and returns full inventory.

    Beware: an unknown tenant returns HTTP 200 with totalFound 0, which is
    byte-identical to a real tenant with no openings. A typo here cannot be
    distinguished from an empty board at fetch time - board_health.py is what
    catches it.
    """
    out, offset = [], 0
    for _ in range(SR_MAX_PAGES):
        d = _req(f"https://api.smartrecruiters.com/v1/companies/{tok}"
                 f"/postings?limit=100&offset={offset}")
        rows = d.get("content") or []
        for j in rows:
            L = j.get("location") or {}
            loc = L.get("fullLocation") or ", ".join(filter(None, [
                L.get("city"), L.get("region"), (L.get("country") or "").upper()]))
            if L.get("remote"):
                loc = f"Remote{' - ' + loc if loc else ''}"
            elif L.get("hybrid") and loc:
                loc = f"Hybrid - {loc}"
            posted = str(j.get("releasedDate") or "")[:10]
            jid = str(j.get("id", ""))
            out.append(job("smartrecruiters", tok, (j.get("name") or "").strip(),
                           loc, f"https://jobs.smartrecruiters.com/{tok}/{jid}",
                           posted, days_ago(posted),
                           " ".join(filter(None, [
                               j.get("name") or "", loc,
                               (j.get("function") or {}).get("label", ""),
                               (j.get("experienceLevel") or {}).get("label", ""),
                               (j.get("typeOfEmployment") or {}).get("label", "")])),
                           hyd=("smartrecruiters", tok, jid)))
        offset += len(rows)
        if not rows or offset >= d.get("totalFound", 0):
            break
    return out


# Taleo behind a Radancy TalentBrew career site (careers.<employer>.ca). Three
# quirks shape this adapter, all of them found the hard way:
#
#   1. The keyword facet needs the term in BOTH the path and the query string.
#      /add/keywords/engineer on its own returns the *unfiltered* board (1,228
#      rows) with Status OK - it looks like it worked. Only
#      /add/keywords/engineer?keywords=engineer actually filters, to 20.
#   2. Adding a facet does not return jobs. It mints a NEW search id, which you
#      then page through; the count comes back in UserMessage.
#   3. Pagination is /jobs/search/<id>/page<n> - no slash before the number.
#      Every ?page=/&offset= form silently returns page 1.
#
# There is no posting date anywhere on this platform: not on the result card,
# not on the job page, not in a meta tag or JSON-LD block. `age` is therefore
# always None. The only freshness signal offered is a "New" badge, which we
# pass through verbatim rather than converting into a day count we cannot
# actually justify.
TALEO_PAGE = 10          # results per page, fixed by the platform
TALEO_MAX_PAGES = 12     # 120 jobs per query is far past anything IT-sized

TALEO_TITLE = re.compile(r'class="job_link[^"]*"[^>]*>(.*?)</a>', re.S)
TALEO_HREF = re.compile(r'href="([^"]+/jobs/[^"]+)"[^>]*class="job_link')
TALEO_LOC = re.compile(r'class="location">\s*(.*?)\s*</span>', re.S)
TALEO_CAT = re.compile(r'class="category">\s*(.*?)\s*</span>', re.S)


def fetch_taleo(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    facet, value = t.get("_query") or ("category", "177")
    enc = urllib.parse.quote(str(value))

    # The keyword facet is the one that needs the redundant query string; the
    # category facet ignores it. Sending it for both is harmless and keeps the
    # call site simple.
    claim = (f"{base}/ajax/jobs/{t['search_id']}/add/{facet}/{enc}"
             f"?keywords={enc}")
    try:
        d = _req(claim)
    except Exception:
        return []
    if (d.get("Status") or "") != "OK":
        return []
    sid = str(d.get("Result") or "")
    if not sid:
        return []
    try:
        total = int(str(d.get("UserMessage", "0")).replace(",", ""))
    except ValueError:
        total = 0
    if not total:
        return []

    out, seen = [], set()
    pages = min(TALEO_MAX_PAGES, -(-total // TALEO_PAGE))
    for pg in range(1, pages + 1):
        url = f"{base}/jobs/search/{sid}" + (f"/page{pg}" if pg > 1 else "")
        try:
            h = _get_text(url)
        except Exception:
            break
        for blk in re.findall(
                r'<div id="job_list_\d+.*?(?=<div id="job_list_|jPaginationHldr)',
                h, re.S):
            ti = TALEO_TITLE.search(blk)
            hr = TALEO_HREF.search(blk)
            if not ti or not hr:
                continue
            href = html_unescape(hr.group(1))
            if href in seen:
                continue
            seen.add(href)
            title = strip_html(ti.group(1)).strip()
            lo = TALEO_LOC.search(blk)
            loc = strip_html(lo.group(1)).strip() if lo else ""
            ca = TALEO_CAT.search(blk)
            cat = strip_html(ca.group(1)).strip() if ca else ""
            desc = re.search(r'class="jlr_description">(.*?)</p>', blk, re.S)
            out.append(job("taleo", name, title, loc, href, "", None,
                           " ".join(filter(None, [
                               title, loc, cat,
                               strip_html(desc.group(1))[:2000] if desc else ""])),
                           updated="New" if "new_flg" in blk else ""))
    return out


ADAPTERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "recruitee": fetch_recruitee,
            "rippling": fetch_rippling, "workday": fetch_workday,
            "phenom": fetch_phenom, "successfactors": fetch_successfactors,
            "oracle": fetch_oracle, "jobvite": fetch_jobvite,
            "html": fetch_html, "workable": fetch_workable,
            "smartrecruiters": fetch_smartrecruiters,
            "taleo": fetch_taleo}


# ---------------------------------------- pass 2: hydrate only the finalists --
# Pulling full descriptions for ~12,000 jobs would move ~80MB. We pull them only
# for the few hundred that already passed the title + location filter.

# On JobPostingDetails the description field is descriptionHtml (not
# descriptionPlainText) and publishedDate IS available - this is where Ashby
# dates come from.
ASHBY_ONE = ("query ApiJobPosting($organizationHostedJobsPageName: String!, "
             "$jobPostingId: String!) { jobPosting("
             "organizationHostedJobsPageName: $organizationHostedJobsPageName, "
             "jobPostingId: $jobPostingId) { descriptionHtml publishedDate } }")


def hydrate(j):
    kind = j.get("hyd")
    if not kind:
        return j
    try:
        if kind[0] == "greenhouse":
            _, tok, jid = kind
            d = _req(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs/{jid}")
            j["blob"] += " " + strip_html(d.get("content", ""))[:8000]
            j["offices"] = [o.get("name", "") for o in (d.get("offices") or [])]
        elif kind[0] == "workday":
            d = _req(kind[1])
            ji = d.get("jobPostingInfo") or {}
            j["blob"] += " " + strip_html(ji.get("jobDescription", ""))[:8000]
            if ji.get("startDate"):
                j["posted"] = ji["startDate"]
                exact = days_ago(ji["startDate"])
                if exact is not None:
                    j["age"] = exact
        elif kind[0] == "smartrecruiters":
            _, tok, jid = kind
            d = _req(f"https://api.smartrecruiters.com/v1/companies/{tok}"
                     f"/postings/{jid}")
            secs = (d.get("jobAd") or {}).get("sections") or {}
            j["blob"] += " " + strip_html(" ".join(
                (secs.get(k) or {}).get("text", "") for k in
                ("jobDescription", "qualifications", "additionalInformation")
            ))[:8000]
        elif kind[0] == "jobvite":
            h = _get_text(kind[1])
            m = JV_DATE.search(h)
            if m:
                j["posted"] = m.group(1)
                j["age"] = days_ago(m.group(1))
            body = re.search(r'class="jv-job-detail-description".*?</div>', h, re.S)
            j["blob"] += " " + strip_html(body.group(0) if body else h)[:8000]
        elif kind[0] == "successfactors":
            h = _get_text(kind[1])
            m = re.search(r'<div[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)'
                          r'</div>\s*(?:<div[^>]*class="[^"]*(?:jobbottom|clear))',
                          h, re.S | re.I)
            body = m.group(1) if m else h
            j["blob"] += " " + strip_html(body)[:8000]
        elif kind[0] == "ashby":
            _, tok, jid = kind
            d = _req("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
                     {"operationName": "ApiJobPosting",
                      "variables": {"organizationHostedJobsPageName": tok,
                                    "jobPostingId": jid},
                      "query": ASHBY_ONE})
            jp = (d.get("data") or {}).get("jobPosting") or {}
            j["blob"] += " " + strip_html(jp.get("descriptionHtml") or "")[:8000]
            if jp.get("publishedDate"):
                j["posted"] = jp["publishedDate"]
                j["age"] = days_ago(jp["publishedDate"])
    except Exception:
        pass
    return j


# --------------------------------------------------------- shared file lock --
# cron appends to the tracker while the web app edits statuses in it. Both take
# this lock and write atomically, or one silently clobbers the other.

TRACKER_LOCK = os.path.join(HERE, ".applications.lock")


class FileLock:
    def __init__(self, path=TRACKER_LOCK, timeout=15):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        self.fh = open(self.path, "w")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise TimeoutError(f"could not lock {self.path}")
                time.sleep(0.15)

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def atomic_write(path, text):
    """Write via temp file + rename so a reader never sees a half-written file."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tracker-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ tracker --

TRACKER = os.path.abspath(os.path.join(HERE, "..", "applications.md"))

TRACKER_HEADER = """# Application Tracker

Statuses: `toapply` -> `applied` -> `interviewing` -> `rejected`  (set them in the web app or here)

| # | Company | Role | Location | Posted | Updated | Score | Applied | Status | Next action |
|---|---------|------|----------|--------|---------|-------|---------|--------|-------------|

---
"""

TRACKER_FOOTER = """
## Outreach template

> Hi - saw the [ROLE] req go up on your careers page and applied. Quick context:
> I spent 6 years doing L2 escalation and RCA on production outages, and the last
> year running Terraform/Kubernetes/GitHub Actions on AWS with a Grafana Loki
> stack that cut our MTTR ~45%. Happy to answer anything ahead of screening.

Send to the **hiring manager**, not the recruiter. Search
`<company> "support manager" OR "director of support" OR "engineering manager"`
filtered to Canada or Remote.
"""

FAMILY_WHY = {
    "vendor": "Support/solutions role at a DevOps tool vendor - the 6-yr-support "
              "+ 1-yr-DevOps profile lands here better than anywhere else, and "
              "the internal path to SRE is well-worn.",
    "devops": "Core DevOps/SRE target.",
    "adjacent": "Infra-adjacent role that routinely hires from a support background.",
}


# ATS tokens are lowercase slugs. Fix the ones title-casing gets wrong.
DISPLAY = {
    "gitlab": "GitLab", "jfrog": "JFrog", "grafanalabs": "Grafana Labs",
    "pagerduty": "PagerDuty", "circleci": "CircleCI", "stackadapt": "StackAdapt",
    "newrelic": "New Relic", "launchdarkly": "LaunchDarkly", "beyondtrust": "BeyondTrust",
    "clickhouse": "ClickHouse", "cockroachlabs": "Cockroach Labs", "mongodb": "MongoDB",
    "planetscale": "PlanetScale", "sumologic": "Sumo Logic", "goteleport": "Teleport",
    "jumpcloud": "JumpCloud", "wizinc": "Wiz", "ngrokinc": "ngrok", "runzero": "runZero",
    "octopusdeploy": "Octopus Deploy", "orcasecurity": "Orca Security",
    "endorlabs": "Endor Labs", "keepersecurity": "Keeper Security",
    "catonetworks": "Cato Networks", "coreweave": "CoreWeave", "lastpass": "LastPass",
    "purestorage": "Pure Storage", "sonarsource": "SonarSource", "yugabyte": "YugabyteDB",
    "bluecatnetworks": "BlueCat Networks", "magnetforensics": "Magnet Forensics",
    "pointclickcare": "PointClickCare", "eqbank": "EQ Bank", "waveapps": "Wave",
    "lightspeedhq": "Lightspeed", "1password": "1Password", "d2l": "D2L",
    "waabi": "Waabi", "cloudzero": "CloudZero", "buildops": "BuildOps",
    "gocardless": "GoCardless", "deepgenomics": "Deep Genomics", "airtable": "Airtable",
}


def pretty_company(tok):
    t = (tok or "").strip()
    if " " in t:          # Workday entries already carry a real name
        return t
    return DISPLAY.get(t.lower(), t[:1].upper() + t[1:])


def posted_date(j):
    """Best-effort ISO date. Workday only gives relative ages."""
    p = str(j.get("posted") or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", p)
    if m:
        return m.group(1)
    if j.get("age") is not None:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(days=j["age"])).date().isoformat()
    return "?"


def append_to_tracker(jobs, path, min_score):
    """Append new high-scorers. Never rewrites existing rows or your notes."""
    if not os.path.exists(path):
        body = TRACKER_HEADER + TRACKER_FOOTER
    else:
        body = open(path).read()

    seen_urls = set(re.findall(r"https?://[^\s)\]]+", body))
    try:
        conn = db()
        seen_urls |= {r[0] for r in conn.execute("SELECT url FROM dismissed")}
        conn.close()
    except Exception:
        pass
    nums = [int(n) for n in re.findall(r"(?m)^\|\s*(\d+)\s*\|", body)]
    nxt = max(nums) + 1 if nums else 1

    todo = [j for j in jobs
            if j["score"] >= min_score and j["url"].rstrip(".,)") not in seen_urls]
    if not todo:
        return 0, nxt

    todo.sort(key=lambda j: -j["score"])
    rows, blocks = [], []
    today = datetime.now(timezone.utc).date().isoformat()

    for j in todo:
        company = pretty_company(j["company"]).replace("|", "/")
        title = j["title"].strip().replace("|", "/")
        loc = j["location"].strip().replace("|", "/")
        loc_short = loc if len(loc) <= 45 else loc[:42] + "..."
        pdate = posted_date(j)
        nxt_action = ("Apply, then find the hiring manager on LinkedIn"
                      if j["score"] >= 25 else "Read the JD, then decide")
        udate = str(j.get("updated") or "")[:10] or "—"
        rows.append(f"| {nxt} | {company} | {title} | {loc_short} | {pdate} | "
                    f"{udate} | {j['score']} | | `toapply` | {nxt_action} |")

        age = "unknown age" if j["age"] is None else (
            "posted today" if j["age"] == 0 else f"{j['age']}d old when found")
        blocks.append(f"""
## {nxt}. {company} - {title}

- **Apply:** {j['url']}
- **Location:** {loc} - bucket `{j['bucket']}`
- **Level:** {j['level']} · **Score:** {j['score']} · **ATS:** {j['source']}
- **Posted:** {pdate} ({age}) · **Last updated by recruiter:** {udate}
- **Found:** {today}
- **Resume keywords matched:** {', '.join(j['kw']) or '-'}

**Why this fits:** {FAMILY_WHY.get(j['family'], '')}

- [ ] Applied
- [ ] Hiring manager found on LinkedIn
- [ ] Outreach note sent
- Contact:
- Notes:

---
""")
        nxt += 1

    # Table rows go after the last existing table row; detail blocks go just
    # before the outreach template so the file stays readable.
    lines = body.split("\n")
    last_row = max((i for i, l in enumerate(lines)
                    if l.startswith("|") and l.rstrip().endswith("|")), default=None)
    if last_row is None:
        lines = (TRACKER_HEADER + TRACKER_FOOTER).split("\n")
        last_row = max(i for i, l in enumerate(lines) if l.startswith("|"))
    lines[last_row + 1:last_row + 1] = rows
    body = "\n".join(lines)

    detail = "\n".join(blocks)
    anchor = body.find("## Outreach template")
    body = (body[:anchor] + detail.lstrip("\n") + "\n" + body[anchor:]
            if anchor != -1 else body.rstrip() + "\n" + detail)

    atomic_write(path, body)
    return len(todo), nxt


# ------------------------------------------------------------------- notify --
# Push new matches to a phone via ntfy.sh. The topic is effectively a password:
# anyone who knows it can read your job alerts, so it is generated random and
# kept in a gitignored file.

NTFY_FILE = os.path.join(HERE, ".ntfy_topic")


def ntfy_topic():
    t = os.environ.get("RADAR_NTFY_TOPIC")
    if t:
        return t.strip()
    if os.path.exists(NTFY_FILE):
        return open(NTFY_FILE).read().strip()
    return ""


def notify(jobs, topic, limit=6):
    """One push per strong match, then a single roll-up for the rest."""
    if not topic or not jobs:
        return 0
    jobs = sorted(jobs, key=lambda j: -j["score"])
    sent, failed = 0, []
    for j in jobs[:limit]:
        prio = "urgent" if j["score"] >= 35 else "high" if j["score"] >= 25 else "default"
        tag = {"vendor": "handshake", "devops": "rocket"}.get(j["family"], "wrench")
        age = "" if j["age"] is None else (
            " · posted today" if j["age"] == 0 else f" · {j['age']}d old")
        body = (f"{j['company']} · {j['location'][:60]}{age}\n"
                f"matched: {', '.join(j['kw'][:6]) or '—'}")
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": f"[{j['score']}] {j['title'][:80]}",
                     "Priority": prio,
                     "Tags": tag,
                     "Click": j["url"],
                     "User-Agent": UA["User-Agent"]},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=12)
            sent += 1
        except Exception as e:
            # Never swallow this. A rate limit, a network drop or a bad topic
            # used to look exactly like "nothing matched" - the alerts simply
            # stopped and nothing said so. cron captures stdout, so a failure
            # here lands in the log where it can be seen.
            failed.append(f"{type(e).__name__}: {e} — {j['title'][:40]}")
    rest = jobs[limit:]
    if rest:
        body = "\n".join(f"[{j['score']}] {j['title'][:52]} — {j['company']}"
                          for j in rest[:14])
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                headers={"Title": f"+{len(rest)} more matches",
                         "Priority": "low", "Tags": "mag",
                         "User-Agent": UA["User-Agent"]}, method="POST"), timeout=12)
            sent += 1
        except Exception as e:
            failed.append(f"{type(e).__name__}: {e} — roll-up")
    if failed:
        print(f"  !! {len(failed)} notification(s) FAILED to send:")
        for f in failed[:5]:
            print(f"     {f}")
        if sent == 0:
            print("     every push failed - check ntfy.sh reachability and "
                  f"the topic in {NTFY_FILE}")
    return sent


# -------------------------------------------------------------------- state --

def db():
    # The fast lane and the full sweep can overlap, and the web app reads this
    # too. Wait for the writer instead of throwing "database is locked".
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("CREATE TABLE IF NOT EXISTS seen("
              "url TEXT PRIMARY KEY, title TEXT, company TEXT, first_seen TEXT)")
    # Every URL returned by any board this run - not just the ones that matched.
    # A tracked job whose URL stops appearing here has been taken down, which is
    # how the web app knows to stop showing it.
    c.execute("CREATE TABLE IF NOT EXISTS live("
              "url TEXT PRIMARY KEY, last_seen TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS sweeps(ts TEXT)")
    # Jobs the user deleted from the tracker. Without this the next sweep just
    # re-adds them, because dedupe only looks at URLs currently in the file.
    c.execute("CREATE TABLE IF NOT EXISTS dismissed("
              "url TEXT PRIMARY KEY, at TEXT)")
    return c


# --------------------------------------------------------------------- main --

FAMILY_LABEL = {"devops": "DevOps/SRE", "adjacent": "Infra-adjacent",
                "vendor": "VENDOR SUPPORT"}

# Lowering --min-score should add depth, not noise. Bands keep the top of the
# list obvious no matter how wide the net is cast.
BANDS = [(25, "APPLY TODAY"), (15, "THIS WEEK"), (8, "WORTH A LOOK"),
         (-999, "LONG SHOT")]


def band_of(score):
    for floor, label in BANDS:
        if score >= floor:
            return label
    return "LONG SHOT"


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    ap.add_argument("--all", action="store_true", help="show all matches, not just new")
    ap.add_argument("--seed", action="store_true", help="record state, print nothing")
    ap.add_argument("--min-score", type=int, default=8)
    ap.add_argument("--max-age", type=int, default=30, help="skip reqs older than N days")
    ap.add_argument("--level", default="entry,mid,senior",
                    help="comma list of entry,mid,senior,exec,intern")
    ap.add_argument("--family", default="devops,adjacent,vendor",
                    help="comma list of devops,adjacent,vendor")
    ap.add_argument("--bucket", default="CA,NA-REMOTE,MULTI",
                    help="comma list of CA,NA-REMOTE,MULTI")
    ap.add_argument("--province", default="",
                    help="comma list of province codes e.g. AB,BC. Postings that "
                         "name no province (remote) are always kept")
    ap.add_argument("--md", help="also write a markdown digest here")
    ap.add_argument("--notify", action="store_true",
                    help="push new matches to your phone via ntfy")
    ap.add_argument("--notify-min-score", type=int, default=15)
    ap.add_argument("--track", action="store_true",
                    help="append shown postings to the application tracker")
    ap.add_argument("--track-file", default=TRACKER)
    ap.add_argument("--track-min-score", type=int, default=15,
                    help="only track postings at or above this score (default 15, matching --notify-min-score so nothing buzzes your phone without landing in the tracker)")
    ap.add_argument("--targets", default=TARGETS)
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()

    levels = {x.strip() for x in a.level.split(",") if x.strip()}
    fams = {x.strip() for x in a.family.split(",") if x.strip()}
    buckets = {x.strip() for x in a.bucket.split(",") if x.strip()}
    provs = {x.strip().upper() for x in a.province.split(",") if x.strip()}

    tg = json.load(open(a.targets))
    VENDORS.update(x.lower() for x in tg.get("devops_vendors", []))

    tasks = []
    for src, fn in ADAPTERS.items():
        for tok in tg.get(src, []):
            if src == "workday":
                for q in WD_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "successfactors":
                for q in SF_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "oracle":
                for q in ORC_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "jobvite":
                for q in JV_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "taleo":
                for q in tok.get("queries") or [["category", "177"]]:
                    tasks.append((src, {**tok, "_query": tuple(q)}, fn))
            else:
                tasks.append((src, tok, fn))

    jobs, errors = [], []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fn, tok): (src, tok) for src, tok, fn in tasks}
        for f in cf.as_completed(futs):
            src, tok = futs[f]
            label = tok["name"] if isinstance(tok, dict) else tok
            try:
                jobs.extend(f.result())
            except Exception as e:
                errors.append(f"{src}/{label}: {type(e).__name__} {e}")

    # The same posting arrives more than once: a Workday req matches several
    # keyword queries, and some boards list one req under several countries
    # sharing a single URL. Keeping the first occurrence silently dropped the
    # Canada-eligible variant - an MLabs SRE role was listed for UK, Poland,
    # Germany, Netherlands and Canada under one shortlink, and the UK entry won.
    # Merge the locations instead so the classifier sees every one of them.
    _byurl = {}
    for j in jobs:
        prev = _byurl.get(j["url"])
        if prev is None:
            _byurl[j["url"]] = j
            continue
        locs = [p.strip() for p in prev["location"].split(" / ") if p.strip()]
        if j["location"] and j["location"] not in locs:
            locs.append(j["location"])
            prev["location"] = " / ".join(locs)
        if len(j.get("blob", "")) > len(prev.get("blob", "")):
            prev["blob"] = j["blob"]
    jobs = list(_byurl.values())

    # Pass 1: cheap filters on title + location only.
    shortlist = []
    for j in jobs:
        bucket, keep = classify_location(j["location"])
        if not keep or bucket not in buckets:
            continue
        tier, fam = title_match(j["title"], j["company"])
        if tier == 0 or fam not in fams:
            continue
        if level_of(j["title"]) not in levels:
            continue
        if j["age"] is not None and j["age"] > a.max_age:
            continue
        j.update(bucket=bucket, tier=tier, family=fam,
                 provinces=sorted(provinces_of(j["location"])),
                 arrangement=arrangement(j["location"], j.get("blob", "")))
        shortlist.append(j)

    # Pass 2: fetch descriptions for the shortlist, then score properly.
    with cf.ThreadPoolExecutor(a.workers) as ex:
        shortlist = list(ex.map(hydrate, shortlist))

    if provs:
        # Keep the wanted provinces, plus anything naming no province at all -
        # those are remote or unclear, never "definitely somewhere else".
        shortlist = [j for j in shortlist
                     if not j["provinces"] or (set(j["provinces"]) & provs)]

    hits = []
    for j in shortlist:
        s, kw, lvl = score(j["title"], j["company"], j["tier"], j["family"], j["blob"])
        if s < a.min_score:
            continue
        j.update(score=s, kw=kw, level=lvl)
        hits.append(j)

    conn = db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Record liveness for everything scanned, and stamp the sweep. Only a full
    # sweep (no --targets override) is authoritative about what is still up.
    if a.targets == TARGETS:
        conn.executemany("INSERT INTO live(url,last_seen) VALUES(?,?) "
                         "ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen",
                         [(j["url"], now) for j in jobs])
        conn.execute("INSERT INTO sweeps(ts) VALUES(?)", (now,))
        conn.execute("DELETE FROM sweeps WHERE ts NOT IN "
                     "(SELECT ts FROM sweeps ORDER BY ts DESC LIMIT 50)")
    # Claiming a posting must be atomic. The fast lane and the full sweep can
    # run in the same minute; when both read `seen` before either wrote, they
    # each decided the same job was new and both pushed a notification. Insert
    # first and let the row count decide - only the writer that actually
    # inserted owns it.
    fresh = []
    for j in hits:
        cur = conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?)",
                           (j["url"], j["title"], j["company"], now))
        if cur.rowcount:
            fresh.append(j)
    conn.commit()

    if a.seed:
        print(f"Seeded {len(hits)} postings into {DB}.\n"
              f"From now on, a plain `python3 job_radar.py` shows only NEW ones.")
        return

    show = sorted(hits if a.all else fresh,
                  key=lambda j: (-j["score"], j["age"] if j["age"] is not None else 99))

    tally = {}
    for j in show:
        tally[band_of(j["score"])] = tally.get(band_of(j["score"]), 0) + 1
    headline = " · ".join(f"**{tally[b]}** {b.lower()}" for _, b in BANDS
                          if b in tally) or "nothing"
    L = [f"# Job Radar — {'ALL MATCHES' if a.all else 'NEW SINCE LAST RUN'} — {now[:16]}Z",
         f"### {headline}",
         f"_{len(show)} shown · {len(hits)} matched · {len(jobs)} scanned · "
         f"{len(tasks)} boards · levels={a.level} · families={a.family}_", ""]

    by_fam = {}
    for j in show:
        by_fam.setdefault(j["family"], []).append(j)
    for fam in ("vendor", "devops", "adjacent"):
        group = by_fam.get(fam)
        if not group:
            continue
        counts = {}
        for j in group:
            counts[band_of(j["score"])] = counts.get(band_of(j["score"]), 0) + 1
        tally = " · ".join(f"{n} {b.lower()}" for _, b in BANDS
                           if (n := counts.get(b)))
        L += ["---", f"## {FAMILY_LABEL[fam]}  ({len(group)})", f"_{tally}_", ""]
        current_band = None
        for j in group:
            b = band_of(j["score"])
            if b != current_band:
                current_band = b
                L += [f"**— {b} —**", ""]
            if j["age"] is None:
                # SuccessFactors does not expose a real posting date; show the
                # SEO reference date for what it is instead of inventing an age.
                age = (f"age unknown (ref {j['updated']})" if j.get("updated")
                       else "age unknown")
            else:
                age = "**TODAY**" if j["age"] == 0 else f"{j['age']}d"
            loc = j["location"] if len(j["location"]) <= 80 else j["location"][:77] + "..."
            tags = [j["bucket"], j["level"]]
            if j.get("provinces"):
                tags.append("/".join(j["provinces"]))
            if j.get("arrangement"):
                tags.append(j["arrangement"])
            tagstr = " · ".join("`%s`" % t for t in tags)
            L += [f"### [{j['score']}] {j['title']}",
                  f"- **{j['company']}** · {loc}",
                  f"- {tagstr} · posted {age}",
                  f"- {j['url']}",
                  f"- matched: {', '.join(j['kw']) or '—'}"]
            warn = office_mismatch(j)
            if warn:
                L.append(f"- ⚠️ {warn}")
            L.append("")

    if not show:
        L.append("_Nothing new. Try `--all`, or lower `--min-score`._")
    if errors:
        L += ["---", "**Dead boards — run `discover.py --verify --add`:**"] + \
             [f"- {e}" for e in errors]

    out = "\n".join(L)
    print(out)
    if a.md:
        open(a.md, "w").write(out + "\n")

    if a.notify:
        topic = ntfy_topic()
        if not topic:
            print("\n>> --notify needs a topic: write one to radar/.ntfy_topic "
                  "or set RADAR_NTFY_TOPIC")
        else:
            worth = [j for j in show if j["score"] >= a.notify_min_score]
            n = notify(worth, topic)
            if n:
                print(f"\n>> pushed {n} notification(s) to ntfy topic {topic[:6]}…")
            elif worth:
                print(f"\n>> WARNING: {len(worth)} job(s) matched but NO "
                      "notification was delivered")

    if a.track:
        with FileLock():
            n, _ = append_to_tracker(show, a.track_file, a.track_min_score)
        rel = os.path.relpath(a.track_file, os.getcwd())
        if rel.count("..") > 1:
            rel = a.track_file
        if n:
            print(f"\n>> Tracked {n} new posting(s) (score >= {a.track_min_score}) "
                  f"in {rel}")
        else:
            print(f"\n>> Nothing new to track (score >= {a.track_min_score}); "
                  f"{rel} already covers what was shown.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
