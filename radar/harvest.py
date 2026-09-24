#!/usr/bin/env python3
"""
harvest.py - grow targets.json from company names, in bulk.

The radar is an allowlist, not a crawl: none of these ATS platforms expose a
"list every board" endpoint (Greenhouse and Lever 404, Ashby 401). The only way
to widen coverage is to take real company names and test whether each one has a
board. Hit rate runs ~15-35% depending on the source.

    python3 harvest.py --wiki "Category:Software companies of Canada"
    python3 harvest.py --url https://www.betakit.com/  --add
    python3 harvest.py --names "Vena Solutions" Nuvei Ecobee --add
    python3 harvest.py --file companies.txt --add --workers 16

Nothing is written unless you pass --add. Every hit is verified live first.
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
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(HERE, "targets.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Content-Type": "application/json"}


def _fetch(url, data=None, timeout=12):
    r = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers=UA, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.read()


def _text(url, timeout=25):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.read().decode("utf-8", "replace")


# --------------------------------------------------------------- ATS probes --

def p_greenhouse(t):
    try:
        d = json.loads(_fetch(
            f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=false"))
        return len(d.get("jobs", []))
    except Exception:
        return 0


def p_lever(t):
    try:
        d = json.loads(_fetch(f"https://api.lever.co/v0/postings/{t}?mode=json"))
        return len(d) if isinstance(d, list) else 0
    except Exception:
        return 0


_AQ = ("query B($n: String!) { jobBoard: jobBoardWithTeams("
       "organizationHostedJobsPageName: $n) { jobPostings { id } } }")


def p_ashby(t):
    try:
        d = json.loads(_fetch(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
            {"operationName": "B", "variables": {"n": t}, "query": _AQ}))
        jb = (d.get("data") or {}).get("jobBoard")
        return len(jb["jobPostings"]) if jb else 0
    except Exception:
        return 0


def p_recruitee(t):
    try:
        return len(json.loads(
            _fetch(f"https://{t}.recruitee.com/api/offers/")).get("offers", []))
    except Exception:
        return 0


def p_rippling(t):
    try:
        d = json.loads(_fetch(
            f"https://api.rippling.com/platform/api/ats/v1/board/{t}/jobs"))
        return len(d) if isinstance(d, list) else 0
    except Exception:
        return 0


def p_workable(t):
    try:
        d = json.loads(_fetch(
            f"https://apply.workable.com/api/v1/widget/accounts/{t}?details=true"))
        return len(d.get("jobs", []))
    except Exception:
        return 0


PROBES = {"greenhouse": p_greenhouse, "workable": p_workable, "lever": p_lever, "ashby": p_ashby,
          "recruitee": p_recruitee, "rippling": p_rippling}


# ------------------------------------------------------------ name -> token --

# First words too generic to identify a company on their own.
GENERIC_FIRST = set("""international national canadian american global united
general standard advanced digital modern smart open first best great pacific
atlantic northern southern eastern western central metro urban prime alpha
beta delta omega nova terra vista royal crown empire summit apex pinnacle
constellation dominion imperial premier superior universal continental""".split())

SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|"
    r"company|group|holdings|plc|sa|nv|gmbh|ag|oy|ab|pty)\b\.?", re.I)


def variants(name):
    n = SUFFIXES.sub("", name).strip(" .,-&")
    flat = re.sub(r"[^a-z0-9]+", "", n.lower())
    dash = re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")
    out = {flat, dash}
    # "Vena Solutions" -> "vena". Only when the first word is distinctive:
    # a generic one like "International" or "Canadian" matches some unrelated
    # company's board and quietly poisons targets.json.
    words = [w for w in re.split(r"[^A-Za-z0-9]+", n.lower()) if w]
    if len(words) > 1 and len(words[0]) >= 5 and words[0] not in GENERIC_FIRST:
        out.add(words[0])
    return sorted(v for v in out if 3 < len(v) <= 40)


# ------------------------------------------------------------- name sources --

STOP = set("""the a an and or of for in on at to with from by list see also
references external links category wikipedia portal help about contact privacy
terms careers jobs news blog home page index main menu search login sign
company companies limited group holdings inc ltd corp""".split())


def names_from_html(html, limit=1200):
    """Pull plausible company names out of an arbitrary page."""
    names = []
    # Wikipedia list/category items and generic anchor text both live in <a>
    for m in re.finditer(r"<a\b[^>]*>(.*?)</a>", html, re.S):
        t = unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if not (2 < len(t) <= 45):
            continue
        if t.lower() in STOP or t.startswith(("http", "#")):
            continue
        if not re.match(r"^[A-Z0-9]", t):
            continue
        if re.search(r"[\\/|<>{}\[\]@]|\.(com|org|net)$", t):
            continue
        words = t.split()
        if len(words) > 5:
            continue
        if all(w.lower() in STOP for w in words):
            continue
        names.append(t)
    seen, out = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out[:limit]


def names_from_wikipedia(category, limit=600):
    """Use the MediaWiki API - reliable, paginated, and polite."""
    out, cont = [], None
    api = "https://en.wikipedia.org/w/api.php"
    while len(out) < limit:
        q = {"action": "query", "list": "categorymembers", "format": "json",
             "cmtitle": category, "cmlimit": "500", "cmtype": "page"}
        if cont:
            q["cmcontinue"] = cont
        d = json.loads(_fetch(api + "?" + urllib.parse.urlencode(q)))
        for m in d.get("query", {}).get("categorymembers", []):
            t = re.sub(r"\s*\(.*?\)\s*$", "", m["title"]).strip()
            if 2 < len(t) <= 45:
                out.append(t)
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
    return out[:limit]


# --------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    src = ap.add_argument_group("where the names come from")
    src.add_argument("--names", nargs="+")
    src.add_argument("--file", help="one company name per line")
    src.add_argument("--url", help="scrape company names off any page")
    src.add_argument("--wiki", help='Wikipedia category, e.g. "Category:Software companies of Canada"')
    ap.add_argument("--add", action="store_true", help="write hits into targets.json")
    ap.add_argument("--targets", default=TARGETS)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=600, help="max company names")
    a = ap.parse_args()

    names = []
    if a.names:
        names += a.names
    if a.file:
        names += [l.strip() for l in open(a.file) if l.strip()]
    if a.wiki:
        got = names_from_wikipedia(a.wiki, a.limit)
        print(f"  {len(got)} names from {a.wiki}")
        names += got
    if a.url:
        got = names_from_html(_text(a.url), a.limit)
        print(f"  {len(got)} candidate names from {a.url}")
        names += got
    if not names:
        ap.print_help()
        return 1

    tg = json.load(open(a.targets))
    known = {s: set(tg.get(s, [])) for s in PROBES}

    pairs, seen = [], set()
    for n in names[:a.limit]:
        for v in variants(n):
            for s in PROBES:
                if v in known[s] or (s, v) in seen:
                    continue
                seen.add((s, v))
                pairs.append((s, v))

    print(f"  {len(set(n for n in names[:a.limit]))} companies -> "
          f"{len(pairs)} token probes across {len(PROBES)} platforms\n")

    found, done, t0 = [], 0, time.time()
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(PROBES[s], t): (s, t) for s, t in pairs}
        for f in cf.as_completed(futs):
            s, t = futs[f]
            done += 1
            if done % 400 == 0:
                print(f"  ... {done}/{len(pairs)} probed, {len(found)} found, "
                      f"{time.time()-t0:.0f}s", file=sys.stderr)
            try:
                n = f.result()
            except Exception:
                n = 0
            if n:
                found.append((s, t, n))
                print(f"  HIT {s:12} {t:28} {n:4} postings")

    print(f"\n  {len(found)} new boards in {time.time()-t0:.0f}s "
          f"({len(pairs)} probes)")
    if found and a.add:
        for s, t, _ in found:
            tg.setdefault(s, [])
            if t not in tg[s]:
                tg[s].append(t)
        for s in PROBES:
            tg[s] = sorted(set(tg.get(s, [])))
        json.dump(tg, open(a.targets, "w"), indent=2)
        total = sum(len(tg[k]) for k in
                    ("greenhouse", "lever", "ashby", "recruitee", "rippling",
                     "workday", "phenom", "successfactors") if k in tg)
        print(f"  targets.json now has {total} boards")
    elif found:
        print("  (dry run - pass --add to write them into targets.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
