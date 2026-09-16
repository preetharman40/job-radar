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
