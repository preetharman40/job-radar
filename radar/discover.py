#!/usr/bin/env python3
"""
discover.py - resolve a company (or its careers URL) to a machine-readable
ATS endpoint, and keep targets.json honest.

    python3 discover.py --verify                    # re-probe every token, flag dead ones
    python3 discover.py --company shopify vena nuvei   # brute-probe name variants
    python3 discover.py --url https://careers.foo.com/  # fingerprint ANY careers site
    python3 discover.py --company jobber --add      # write the hits into targets.json

The --url mode is the important one: it tells you which ATS a company runs and
what its public JSON endpoint is, including platforms this radar can't poll yet
(iCIMS, Taleo, Phenom, Dayforce, Njoyn, SuccessFactors, Oracle ORC).
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(HERE, "targets.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Content-Type": "application/json"}


# A board that does not exist answers 404, and retrying it twice with 1.5s and
# 3.0s of backoff turns a definitive "no" into 4.5 wasted seconds. That is the
# whole cost of brute-probing names: a miss fires every variant against every
# platform and nearly all of them 404, so an employer with no board took ~30s
# to rule out and 200 employers would have taken 95 minutes. Only transport
# blips and the statuses that mean "ask again" are worth a second attempt.
RETRY_STATUS = frozenset((408, 425, 429, 500, 502, 503, 504))


def _fetch(url, data=None, timeout=15, raw=False, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            return _fetch_once(url, data, timeout, raw)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS:
                raise
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last


def _fetch_once(url, data=None, timeout=15, raw=False):
    r = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers=UA, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as f:
        b = f.read()
        return (b.decode("utf-8", "replace"), f.geturl()) if raw else json.loads(b)


# ------------------------------------------------------------ token probing --

def p_greenhouse(t):
    try:
        return len(_fetch(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs"
                          "?content=false").get("jobs", []))
    except Exception:
        return 0


def p_lever(t):
    try:
        d = _fetch(f"https://api.lever.co/v0/postings/{t}?mode=json")
        return len(d) if isinstance(d, list) else 0
    except Exception:
        return 0


_AQ = ("query B($n: String!) { jobBoard: jobBoardWithTeams("
       "organizationHostedJobsPageName: $n) { jobPostings { id } } }")


def p_ashby(t):
    try:
        d = _fetch("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
                   {"operationName": "B", "variables": {"n": t}, "query": _AQ})
        jb = (d.get("data") or {}).get("jobBoard")
        return len(jb["jobPostings"]) if jb else 0
    except Exception:
        return 0


def p_recruitee(t):
    try:
        return len(_fetch(f"https://{t}.recruitee.com/api/offers/").get("offers", []))
    except Exception:
        return 0


PROBES = {"greenhouse": p_greenhouse, "lever": p_lever,
          "ashby": p_ashby, "recruitee": p_recruitee}


# Legal suffixes carry no information for a board token and actively PREVENT a
# hit. LinkedIn renders the employer exactly as registered - "Tecsys Inc.",
# "Interac Corp.", "Porter Airlines Inc." - so every variant built from that
# string is tecsysinc/tecsys-inc and the real board, if there is one, is
# `tecsys`. Stripped repeatedly because "Foo Pvt Ltd" carries two.
#
# Only true legal suffixes are stripped, never descriptive words. Taking
# "Solutions" off "Enterprise Solutions Inc" would probe `enterprise`, which is
# a live Recruitee DEMO account answering HTTP 200 with sample postings - see
# _recruitee_note in targets.json for the five that got added that way.
LEGAL_SUFFIX = re.compile(
    r"[\s,]*\b(?:inc|llc|l\.l\.c|ltd|ltda|limited|corp|corporation|co|company"
    r"|llp|lp|plc|gmbh|s\.a|sa|srl|pvt|pte|bv|nv|ag|ab|oy|a/s|as|sas|kk)\b\.?[\s.]*$",
    re.I)


def variants(name, tier=1):
    """Board-token guesses for a company name, likeliest tier first.

    Tier 1 is the plain concatenation, the dashed form, and the same two with
    any legal suffix removed. Tier 2 adds the embellishments.

    Tiering is measured, not assumed: of the 12 boards this found from a
    fortnight of job-alert mail - maintainx, cresta, shyftlabs, trolley,
    smiledigitalhealth, facilityos, reflow, bosonai, coconutsoftware,
    layerzerolabs, practicebetter, nasdaq - ALL TWELVE answered on the bare
    concatenation. Not one needed a dash or a suffix. So probing tier 2 only
    when tier 1 comes up empty cuts the common case from ~32 requests per
    employer to ~12 without losing a single known hit, and it narrows the
    false-positive surface: every extra guess is another chance to land on a
    demo account that answers 200 with sample data.
    """
    def toks(s):
        return (re.sub(r"[^a-z0-9]+", "", s.lower()),
                re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-"))

    bare = name
    while True:
        stripped = LEGAL_SUFFIX.sub("", bare).strip()
        if stripped == bare or not stripped:
            break
        bare = stripped
    base = set(toks(name)) | set(toks(bare))
    if tier >= 2:
        n = re.sub(r"[^a-z0-9]+", "", bare.lower())
        base |= {n + "inc", n + "hq", n + "technologies", n + "labs",
                 n + "software", n + "ai", n + "io", n + "app", "the" + n}
    return sorted(x for x in base if 2 < len(x) < 40)


# -------------------------------------------------------- ATS fingerprinting --

# (label, regex on final URL + page HTML, how to reach its machine-readable feed)
SIGNATURES = [
    ("workday", r"myworkdayjobs\.com|workdayjobs",
     "POST https://{tenant}.{cluster}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"),
    ("greenhouse", r"(job-)?boards\.greenhouse\.io|greenhouse\.io/embed|gh_jid",
     "GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"),
    ("lever", r"jobs\.lever\.co|api\.lever\.co",
     "GET https://api.lever.co/v0/postings/{token}?mode=json"),
    ("ashby", r"jobs\.ashbyhq\.com|ashbyhq",
     "POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"),
    ("recruitee", r"\.recruitee\.com",
     "GET https://{token}.recruitee.com/api/offers/"),
    ("workable", r"apply\.workable\.com|workable\.com/embed",
     "GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"),
    ("smartrecruiters", r"smartrecruiters\.com",
     "GET https://api.smartrecruiters.com/v1/companies/{id}/postings (often gated now)"),
    ("bamboohr", r"\.bamboohr\.com",
     "GET https://{token}.bamboohr.com/careers/list"),
    ("jobvite", r"jobvite\.com", "GET https://jobs.jobvite.com/{token}/search (HTML)"),
    ("breezy", r"breezy\.hr", "GET https://{token}.breezy.hr/json"),
    ("teamtailor", r"teamtailor\.com", "GET https://{token}.teamtailor.com/jobs.json"),
    ("personio", r"jobs\.personio\.", "GET https://{token}.jobs.personio.de/xml"),
    ("icims", r"icims\.com",
     "GET https://careers-{token}.icims.com/jobs/search?ss=1&in_iframe=1 (HTML+RSS)"),
    ("taleo", r"taleo\.net", "Taleo: scrape /careersection/ HTML; RSS on some tenants"),
    ("successfactors", r"successfactors\.(com|eu)|careers\.sap",
     "SuccessFactors: /search endpoint returns JSON on most tenants"),
    ("oracle-orc", r"oraclecloud\.com/hcmUI|/hcmUI/CandidateExperience",
     "POST https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"),
    ("phenom", r"phenom|phenompeople",
     "GET https://{host}/api/apply/v2/jobs?domain={corp}&start=0&num=20 (needs Referer)"),
    ("radancy", r"radancy|talentbrew", "Radancy: /search-jobs/results?ActiveFacetID=... (JSON)"),
    ("eightfold", r"eightfold\.ai",
     "GET https://{host}/api/apply/v2/jobs?domain={corp}&start=0&num=20"),
    ("dayforce", r"dayforcehcm\.com",
     "Dayforce: https://{tenant}.dayforcehcm.com/CandidatePortal/en-CA/{site} (HTML)"),
    ("njoyn", r"njoyn\.com",
     "Njoyn (Canadian, CGI-owned): clients.njoyn.com/CL4/xweb/xweb.asp?clid={id}&page=joblisting"),
    ("adp", r"workforcenow\.adp\.com", "ADP WFN: /mascsr/default/mdf/recruitment/ (HTML)"),
    ("ukg", r"ultipro\.com|ukg\.",
     "UKG: GET https://{host}/JobBoardView/html/{guid} + /opportunity/ JSON"),
    ("paylocity", r"recruiting\.paylocity\.com", "Paylocity: /recruiting/jobs/All/{guid} (HTML)"),
    ("rippling", r"ats\.rippling\.com", "GET https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"),
    ("applytoeducation", r"applytoeducation\.com",
     "ApplyToEducation (CA schools/boards): HTML search"),
]


def fingerprint(url):
    try:
        html, final = _fetch(url, timeout=25, raw=True)
    except Exception as e:
        return [("error", f"{type(e).__name__}: {e}", "")]
    hay = final + "\n" + html
    out = []
    for label, rx, how in SIGNATURES:
        if re.search(rx, hay, re.I):
            out.append((label, rx, how))
    if not out:
        # Fall back to robots.txt -> sitemap, which works on ANY careers site.
        pr = urllib.parse.urlparse(final)
        root = f"{pr.scheme}://{pr.netloc}"
        try:
            rob, _ = _fetch(root + "/robots.txt", timeout=10, raw=True)
            sm = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", rob)
            if sm:
                out.append(("sitemap-fallback", root + "/robots.txt",
                            "Poll these sitemaps and diff <lastmod>: " + ", ".join(sm)))
        except Exception:
            pass
    # Workday tenants can be fully derived from the public URL.
    m = re.search(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", hay)
    if m:
        t, c, s = m.groups()
        out.append(("workday-resolved", f"{t}/{c}/{s}",
                    json.dumps({"tenant": t, "cluster": c, "site": s})))
    return out


# ------------------------------------------------------- posting url -> board --

# An alert email links straight at the posting, and for most ATS products the
# account token is right there in the path. That is what lets inbox.py grow the
# board list: a company that emails you about one job hands you its whole board,
# for free, with no probing and no guessing at name variants. It is also the only
# expansion channel that is demand-driven - the companies that turn up are the
# ones already sending you alerts for your titles in your geography.
#
# Three classes, and which one a URL falls into decides what happens next:
#
#   1. Invertible. The token IS a path segment, so the board is addable after a
#      single confirming probe: greenhouse, lever, ashby, recruitee, rippling,
#      smartrecruiters, jobvite, workday.
#   2. Platform certain, token absent. A vanity careers domain carrying
#      `gh_jid=` is Greenhouse without question - databricks.com and stripe.com
#      both publish exactly that shape - and apply.workable.com/j/{id} names no
#      account at all. These resolve with `discover.py --url` on the domain.
#   3. Something else. Either not a third-party ATS, or one with no public JSON.
#      manual_check.md exists for this class.
#
# Case is preserved deliberately. SmartRecruiters tokens appear uppercase in
# posting URLs (jobs.smartrecruiters.com/ADVENS/743...) and its API is
# case-sensitive, so lowercasing the token would produce a dead board that
# looks fine in targets.json.

# (platform, regex over "host/path"). First match wins, so the greenhouse embed
# form is tried before the generic host rule.
_INVERT = (
    ("greenhouse",      r"^(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/"
                        r"(?!embed\b)([A-Za-z0-9._-]+)/jobs?/"),
    ("lever",           r"^jobs\.lever\.co/([A-Za-z0-9._-]+)/"),
    ("ashby",           r"^jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)/"),
    ("recruitee",       r"^([A-Za-z0-9-]+)\.recruitee\.com/"),
    ("rippling",        r"^ats\.rippling\.com/([A-Za-z0-9._-]+)/jobs?/"),
    ("smartrecruiters", r"^jobs\.smartrecruiters\.com/([A-Za-z0-9._-]+)/\d"),
    ("jobvite",         r"^jobs\.jobvite\.com/([A-Za-z0-9._-]+)/job/"),
)

# Platform is certain, the account is not in the URL. Resolve with --url.
_PARTIAL = (
    ("greenhouse", r"[?&]gh_jid="),
    ("greenhouse", r"greenhouse\.io/embed/job_app"),
    ("workable",   r"^apply\.workable\.com/j/"),
    ("icims",      r"\.icims\.com/"),
    ("taleo",      r"\.taleo\.net/"),
    ("dayforce",   r"\.dayforcehcm\.com/"),
    ("njoyn",      r"njoyn\.com/"),
    ("eightfold",  r"eightfold\.ai/"),
    # Avature is reachable (job_radar has an adapter), but the account token is
    # never in the posting URL: tenants serve from a vanity domain such as
    # jobs.ea.com while the real tenant is ea.avature.net, and the adapter wants
    # the locale-scoped careers ROOT, not a token. So this is a lead, not an
    # inversion - .../careers/JobDetail/ is the shape to look for.
    ("avature",    r"\.avature\.net/|/careers/JobDetail/"),
    ("phenom",     r"[?&]ref=phenom|phenompeople"),
)

# Workday hands over tenant, cluster and site in one go, and unlike the token
# platforms its targets.json entry is a dict. The locale segment is optional:
# rbc.wd3.myworkdayjobs.com/en-US/RBC/job/... and .../RBC/job/... both occur.
_TT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9-]*)\.(?:[a-z]{2}\.)?teamtailor\.com/", re.I)

_WD = re.compile(
    r"^([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)

# The query-string token forms. ?for= is greenhouse's embed parameter.
_Q_TOKEN = (("greenhouse", "for"),)


def board_from_url(url):
    """Invert a public posting URL into a targets.json entry.

    Returns (platform, target, note). `target` is a bare token string for the
    token platforms and a dict for workday - exactly what targets.json holds for
    that platform, so the caller can append it without reshaping. On failure
    platform is "" and note says why, because "why not" is the useful half of
    the output: it routes a URL to --url, to manual_check.md, or to the bin.
    """
    raw = (url or "").strip()
    if not raw:
        return "", None, "empty"
    if "//" not in raw:
        raw = "https://" + raw
    try:
        pr = urllib.parse.urlparse(raw)
    except Exception as e:
        return "", None, f"unparseable: {type(e).__name__}"
    host = (pr.netloc or "").split("@")[-1].split(":")[0]
    # Not host.lstrip("www.") - lstrip strips any leading w/./ character and
    # would turn "wwa.example.com" into "a.example.com".
    if host.lower().startswith("www."):
        host = host[4:]
    if not host:
        return "", None, "no host"
    hp = host + (pr.path or "/")

    m = _WD.match(hp)
    if m:
        tenant, cluster, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
        return "workday", {"name": tenant, "tenant": tenant,
                           "cluster": cluster, "site": site}, "tenant from url"

    # Teamtailor puts the tenant in the subdomain, so a single posting URL
    # yields the whole board. The region segment is part of it and must be kept:
    # commonsku is commonsku.na.teamtailor.com, while European tenants have no
    # region at all, so the base is rebuilt from the real host rather than
    # assembled from the tenant name. Returns the dict shape targets.json holds.
    m = _TT.match(hp)
    if m:
        return "teamtailor", {"name": m.group(1).lower(),
                              "base": f"https://{m.group(0).split('/')[0]}"}, \
            "tenant from subdomain"

    for platform, rx in _INVERT:
        m = re.match(rx, hp, re.I)
        if m:
            return platform, m.group(1), "token from url path"

    q = urllib.parse.parse_qs(pr.query or "")
    for platform, key in _Q_TOKEN:
        v = (q.get(key) or [""])[0].strip()
        if v and re.fullmatch(r"[A-Za-z0-9._-]+", v):
            return platform, v, f"token from ?{key}="

    for platform, rx in _PARTIAL:
        if re.search(rx, hp + "?" + (pr.query or ""), re.I):
            return "", None, (f"{platform}, but the account is not in the URL - "
                              f"run: discover.py --url https://{host}/")
    return "", None, f"no ATS signature in the URL - run: discover.py --url https://{host}/"


def probe_board(platform, target):
    """Count postings a candidate board returns. 0 means do not add it.

    PROBES covers four platforms with one cheap request. For the rest, borrow
    the radar's own adapter, so a candidate is validated by exactly the code
    that will later poll it - a token that probes fine with a hand-rolled
    request but throws inside fetch_smartrecruiters is worse than no token at
    all, because it lands in targets.json and shows up as a board_health error
    every half hour. Returns -1 when the platform cannot be validated here.
    """
    if platform in PROBES and isinstance(target, str):
        try:
            return PROBES[platform](target)
        except Exception:
            return 0
    jr = _radar()
    fn = getattr(jr, "fetch_" + platform, None) if jr else None
    if not fn:
        return -1
    t = dict(target) if isinstance(target, dict) else target
    if isinstance(t, dict):
        # Workday/SuccessFactors/Oracle adapters are keyword-driven and return
        # nothing without a query, so a live tenant would look dead.
        t.setdefault("_query", "engineer")
    try:
        return len(fn(t))
    except Exception:
        return 0


# ------------------------------------------------- employer -> board lookup --

# One minimum gap per PLATFORM, not one global gap and not a worker cap.
# Brute-probing name variants is the only thing here that fires dozens of
# requests at one host in a few seconds, and an unpaced run of it is what
# produced a burst of HTTP 429s from Ashby during an earlier expansion pass.
# The gap is per-platform so four hosts still progress in parallel.
#
# Ashby is deliberately NOT given a number here: it defers to the radar's own
# _ashby_wait, so the probe and the sweep share ONE budget for
# jobs.ashbyhq.com. Setting it to 0 on the reasoning that "fetch_ashby paces
# itself" was wrong and worth recording - the scan does not go through
# fetch_ashby at all, it goes through p_ashby, which is this file's own
# request. That left ~500 unpaced requests aimed at the one host in
# targets.json already known to rate-limit by volume.
PROBE_GAP = {"greenhouse": 0.35, "lever": 0.35, "recruitee": 0.35}
_GAP_LOCK = threading.Lock()
_GAP_LAST = {}


def _probe_wait(platform):
    if platform == "ashby":
        jr = _radar()
        if jr and hasattr(jr, "_ashby_wait"):
            jr._ashby_wait()
            return
    gap = PROBE_GAP.get(platform, 0.35)
    if not gap:
        return
    with _GAP_LOCK:
        wait = gap - (time.time() - _GAP_LAST.get(platform, 0.0))
        if wait > 0:
            time.sleep(wait)
        _GAP_LAST[platform] = time.time()


def board_postings(platform, target):
    """Every posting a candidate board returns, or None if it cannot be read.

    Returns the postings rather than a count because an employer resolved from
    a job-alert email comes with a piece of evidence nothing else here has -
    the TITLE that put it in the mail - and checking that title against the
    board is what separates the real employer from a lookalike token. A count
    cannot do that. Uses the radar's own adapter for the same reason
    probe_board does: a token validated by different code than the code that
    will poll it is not validated.
    """
    jr = _radar()
    fn = getattr(jr, "fetch_" + platform, None) if jr else None
    if not fn:
        return None
    _probe_wait(platform)
    try:
        return fn(target)
    except Exception:
        return []


def _norm_title(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def title_overlap(mail_titles, board_titles):
    """The title from the email, if it is still listed on this board.

    Containment in either direction, because the two sources trim differently:
    the mail said "Cloud Engineer" where Smile Digital Health's board says
    "Cloud Engineer, Automation (Remote Ontario)", and the mail said
    "Infrastructure Engineer" where Cresta's board says "Infrastructure
    Engineer/SRE". An 8-character floor on the shorter string keeps "SRE" or
    "Dev Ops I" from matching half the internet.
    """
    nb = [(_norm_title(t), t) for t in board_titles]
    for mt in mail_titles:
        a = _norm_title(mt)
        if len(a) < 8:
            continue
        for b, raw in nb:
            if len(b) < 8:
                continue
            if a in b or b in a:
                return mt, raw
    return None, None


def resolve_employer(name, mail_titles, have=None, tier2=True):
    """Find and vet the ATS board of one employer named in a job-alert email.

    This is the half of the mail pipeline that board_from_url cannot do. A
    LinkedIn alert links only to linkedin.com/jobs/view/{id}, so there is no
    ATS token anywhere in the message and the employer's own board has to be
    found from its NAME - which is why 135 of 345 tracked rows are linkedin.com
    URLs with no board behind them.

    A name probe plus HTTP 200 is NOT proof of identity, so every hit is vetted
    on two independent signals before this calls it verified:

      1. IDENTITY - the title from the email is still listed on the board.
         Strong because it is specific to this situation: the employer is in
         the mail BECAUSE of that req. This is the check that rejected
         greenhouse/nasdaq, whose two postings were an AVP and a sysadmin and
         contained nothing resembling the "DevOps Cloud Engineer" that was
         mailed - a satellite board or a different entity.
      2. VALUE - the board carries at least one title the radar's rules match.
         Measured on the board itself, deliberately NOT on the alert's title:
         an employer worth polling can easily turn up in one week's mail under
         a Sales req, and dropping Autodesk on that basis would be the
         expensive mistake. It is also what keeps a seniors' home that mailed
         one "Second Cook" out of targets.json.

    Returns (verdict, platform, target, postings, evidence). Verdicts:
      verified        both checks pass - safe to add
      no-target-roles identity confirmed, but nothing the rules would ever match
      unverified      live board, but the mailed title is not on it
      dead            nothing answered
    """
    jr = _radar()
    have = have or {}

    # Two passes on purpose. The SCAN uses the cheap single-request probes in
    # PROBES, because ruling a name out is most of the work - a miss fires
    # every variant at every platform - and those probes neither retry a 404
    # nor build job records. Only the winner is then read through the radar's
    # own adapter, which is the one that has to agree with what the sweep will
    # see. One expensive call per employer instead of up to 36.
    hits, done = [], set()
    for tier in (1, 2):
        if tier == 2 and (hits or not tier2):
            break
        pairs = [(p, t) for t in variants(name, tier) for p in PROBES
                 if t not in have.get(p, ()) and (p, t) not in done]
        done |= set(pairs)
        with cf.ThreadPoolExecutor(8) as ex:
            def scan(pt):
                _probe_wait(pt[0])
                try:
                    return PROBES[pt[0]](pt[1])
                except Exception:
                    return 0
            for (p, t), n in zip(pairs, ex.map(scan, pairs)):
                if n > 0:
                    hits.append((n, p, t))
    if not hits:
        return "dead", None, None, [], None
    # Prefer the board with the most postings when a name answers on more than
    # one platform: a company that migrated ATS often leaves the old token
    # answering with a handful of stale reqs.
    hits.sort(reverse=True)
    _n, p, t = hits[0]
    posts = board_postings(p, t) or []
    if not posts:
        return "dead", p, t, [], None
    titles = [j.get("title", "") for j in posts]
    mt, bt = title_overlap(mail_titles, titles)
    # title_match's second argument is the COMPANY, not the platform: it is
    # what decides is_vendor, and a vendor-family title only counts at a
    # vendor. The adapters set each posting's company to the board token, which
    # is exactly what the sweep passes, so pass the same thing here - judging
    # the board by different inputs than the sweep uses is how a token gets
    # added that then behaves differently in production.
    # The value check requires a matching title in a location the radar would
    # KEEP, not merely a matching title somewhere. Both halves are the tool's
    # own existing definitions, so this asks exactly "would this board ever
    # have shown me a row" rather than inventing a second rule set.
    #
    # Measured on the first five boards this resolved from mail: ashby/ramp
    # carries 173 postings, 25 of them Canadian and 3 with matching titles -
    # but the matching three are all New York and San Francisco, so the
    # intersection is empty and a title-only test would have added it for
    # nothing. ashby/cerebras has 7 in the intersection. Without the location
    # half, a nightly auto-add drifts toward exactly the board bloat that
    # "Coverage is an allowlist" warns about. A board that misses today is
    # re-probed once stale_days elapse, so a US company that later opens a
    # Toronto infra req is picked up then.
    co = posts[0].get("company") or t
    matched = [x for x in posts
               if jr and jr.title_match(x.get("title", ""), co)[0]
               and jr.classify_location(x.get("location") or "")[1]] if jr else []
    if not mt:
        return "unverified", p, t, posts, None
    if not matched:
        return "no-target-roles", p, t, posts, (mt, bt)
    return "verified", p, t, posts, (mt, bt)


def _radar():
    """Import job_radar lazily, and only once.

    discover.py has to keep working when the engine does not - diagnosing a
    broken adapter is exactly when you reach for it - so this never raises.
    """
    if _radar.mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "job_radar", os.path.join(HERE, "job_radar.py"))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            _radar.mod = mod
        except Exception:
            _radar.mod = False
    return _radar.mod or None


_radar.mod = None


def add_boards(targets_path, found):
    """Append confirmed boards to targets.json. Returns the list actually added.

    Writes through a temp file and os.replace: targets.json is read by every
    cron tick, and a sweep that starts mid-write gets a truncated file and
    sweeps nothing.
    """
    tg = json.load(open(targets_path))
    added = []
    for platform, target in found:
        lst = tg.setdefault(platform, [])
        if isinstance(target, dict):
            if any(isinstance(x, dict)
                   and x.get("tenant") == target.get("tenant")
                   and x.get("site") == target.get("site") for x in lst):
                continue
        elif target in lst:
            continue
        lst.append(target)
        added.append((platform, target))
    if not added:
        return []
    for platform in {p for p, _ in added}:
        lst = tg[platform]
        if all(isinstance(x, str) for x in lst):
            tg[platform] = sorted(set(lst), key=str.lower)
    tmp = targets_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(tg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, targets_path)
    return added


# ---------------------------------------------------------------------- cli --

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", nargs="+", help="company names to brute-probe")
    ap.add_argument("--url", help="careers page URL to fingerprint")
    ap.add_argument("--verify", action="store_true", help="re-probe targets.json")
    ap.add_argument("--add", action="store_true", help="write hits into targets.json")
    ap.add_argument("--targets", default=TARGETS)
    a = ap.parse_args()

    if a.url:
        print(f"Fingerprinting {a.url}\n")
        res = fingerprint(a.url)
        if not res:
            print("  No known ATS signature and no sitemap. Likely a custom careers page -\n"
                  "  open DevTools > Network > XHR and copy whatever JSON the page fetches.")
        for label, sig, how in res:
            print(f"  [{label}]\n    matched: {sig}\n    feed:    {how}\n")
        return

    if a.verify:
        tg = json.load(open(a.targets))
        dead = []

        # Workday and Phenom targets are dicts, not tokens - verify them via
        # the radar's own adapters so a broken tenant/site shows up here.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "job_radar", os.path.join(HERE, "job_radar.py"))
        jr = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(jr)
        except Exception:
            jr = None
        if jr:
            for src, fn in (("workday", getattr(jr, "fetch_workday", None)),
                            ("phenom", getattr(jr, "fetch_phenom", None)),
                            ("successfactors",
                             getattr(jr, "fetch_successfactors", None))):
                for tgt in tg.get(src, []):
                    if not fn:
                        continue
                    try:
                        probe_t = dict(tgt)
                        if src == "workday":
                            probe_t["_query"] = "engineer"
                        elif src == "successfactors":
                            probe_t["_query"] = "cloud"
                        n = len(fn(probe_t))
                    except Exception as e:
                        n = 0
                        print(f"  DEAD {src:12} {tgt.get('name','?'):22} "
                              f"{type(e).__name__}")
                        continue
                    print(f"  {'ok  ' if n else 'DEAD'} {src:12} "
                          f"{tgt.get('name','?'):22} {n}")
        jobs = [(src, t) for src in PROBES for t in tg.get(src, [])]
        suspect = []
        with cf.ThreadPoolExecutor(8) as ex:
            futs = {ex.submit(PROBES[s], t): (s, t) for s, t in jobs}
            for f in cf.as_completed(futs):
                s, t = futs[f]
                n = f.result()
                if n:
                    print(f"  ok   {s:12} {t:22} {n}")
                else:
                    suspect.append((s, t))
        # Second, serial pass. A board is only dead if it fails twice.
        for s, t in suspect:
            time.sleep(0.4)
            n = PROBES[s](t)
            if n:
                print(f"  ok   {s:12} {t:22} {n} (recovered on retry)")
            else:
                print(f"  DEAD {s:12} {t:22} 0")
                dead.append((s, t))
        print(f"\n{len(jobs) - len(dead)}/{len(jobs)} live."
              + (f" Dead: {dead}" if dead else ""))
        if dead and a.add:
            for s, t in dead:
                tg[s].remove(t)
            json.dump(tg, open(a.targets, "w"), indent=2)
            print("Pruned dead tokens from targets.json")
        return

    if a.company:
        tg = json.load(open(a.targets)) if a.add else None
        found = []
        # tier 2 explicitly, so this keeps probing the embellished variants it
        # always did - the mail pipeline is the caller that wants tier 1 only.
        # Paced through _probe_wait and 8 workers rather than 25 unpaced: a
        # multi-name run is dozens of requests per host in a few seconds, and
        # an unpaced one of those is what drew a burst of 429s from Ashby.
        pairs = [(src, v) for name in a.company for v in variants(name, 2)
                 for src in PROBES]

        def scan(pt):
            _probe_wait(pt[0])
            try:
                return PROBES[pt[0]](pt[1])
            except Exception:
                return 0

        with cf.ThreadPoolExecutor(8) as ex:
            for (s, t), n in zip(pairs, ex.map(scan, pairs)):
                if n:
                    print(f"  HIT {s:12} {t:22} {n} postings")
                    found.append((s, t))
        if not found:
            print("  No hits. The company is probably on Workday/iCIMS/Taleo/Phenom -\n"
                  "  run: discover.py --url <their careers page>")
        if a.add and found and tg is not None:
            for s, t in found:
                if t not in tg[s]:
                    tg[s].append(t)
            for s in PROBES:
                tg[s] = sorted(set(tg[s]))
            json.dump(tg, open(a.targets, "w"), indent=2)
            print(f"Added {len(found)} token(s) to targets.json")
        return

    ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
