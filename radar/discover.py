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
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(HERE, "targets.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Content-Type": "application/json"}


def _fetch(url, data=None, timeout=15, raw=False, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            return _fetch_once(url, data, timeout, raw)
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


def variants(name):
    n = re.sub(r"[^a-z0-9]+", "", name.lower())
    d = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    base = {n, d, n + "inc", n + "hq", n + "technologies", n + "labs",
            n + "software", n + "ai", "the" + n}
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
        pairs = [(src, v) for name in a.company for v in variants(name)
                 for src in PROBES]
        with cf.ThreadPoolExecutor(25) as ex:
            futs = {ex.submit(PROBES[s], t): (s, t) for s, t in pairs}
            for f in cf.as_completed(futs):
                s, t = futs[f]
                n = f.result()
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
