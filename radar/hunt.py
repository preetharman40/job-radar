#!/usr/bin/env python3
"""
hunt.py - find Workday and SuccessFactors tenants for a list of employers.

Neither platform can be harvested by token like Greenhouse: Workday needs a
tenant/cluster/site triple and SuccessFactors needs a careers base URL. Both are
discoverable from a company's real careers page, so this tries a set of likely
careers hostnames per employer and fingerprints whatever answers.

    python3 hunt.py --file employers.txt --add
    python3 hunt.py --names "Queen's University" "Western University" --add

Each hit is verified against the live API before it is written.
"""
import argparse, concurrent.futures as cf, json, os, re, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
WD = re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com"
                r"/(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)")
BOT = re.compile(r"perfdrive|datadome|captcha|incapsula|cloudflare/challenge", re.I)


def _get(url, timeout=18):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as f:
        return f.read(400000).decode("utf-8", "replace"), f.geturl()


def hosts_for(name):
    """Guessed careers hostnames. Guessing is unreliable (Queen's is queensu.ca,
    not queensuniversity.ca), so prefer the explicit "Name|host" input form."""
    if "|" in name:
        name, host = name.split("|", 1)
        h = host.strip()
        return [h] if "." in h else [f"jobs.{h}", f"careers.{h}"]
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    dash = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    out = []
    for base in dict.fromkeys([slug, dash]):
        for tld in (".com", ".ca"):
            out += [f"jobs.{base}{tld}", f"careers.{base}{tld}"]
    return out[:8]


CAREER_LINK = re.compile(
    r'href="([^"#]*(?:career|careers|jobs|join-us|work-with-us|emplois)[^"]*)"', re.I)


def careers_urls(domain):
    """Follow a company's main site to whatever it calls its careers page.

    Far more reliable than guessing hostnames - Queen's University is
    queensu.ca, not queensuniversity.ca, and no amount of slug-mangling gets
    there. Returns candidate URLs, most specific first.
    """
    out = []
    for root in (f"https://{domain}", f"https://www.{domain}"):
        try:
            html, final = _get(root)
        except Exception:
            continue
        base = re.match(r"(https?://[^/]+)", final).group(1)
        for href in CAREER_LINK.findall(html)[:40]:
            if href.startswith("//"):
                u = "https:" + href
            elif href.startswith("http"):
                u = href
            else:
                u = base + "/" + href.lstrip("/")
            if u not in out and not re.search(r"\.(pdf|jpg|png|svg|css|js)$", u, re.I):
                out.append(u)
        if out:
            break
    # Prefer hosts that look like a dedicated careers domain.
    out.sort(key=lambda u: (0 if re.search(r"//(jobs|careers|emplois)\.", u) else 1, len(u)))
    return out[:6]


def try_sf_url(url):
    try:
        html, final = _get(url.rstrip("/") + "/search/?q=engineer")
    except Exception:
        return None
    if BOT.search(final):
        return None
    if jr.SF_ROW.search(html) or jr.SF_TILE.search(html):
        # Redirects often land on /go/<saved-search>/<id> or /search/... - the
        # adapter needs the bare site root, not whatever view we arrived at.
        base = re.sub(r"/(search|go)/.*$", "", final).rstrip("/")
        return {"base": base}
    return None


def try_wd_url(url):
    try:
        html, final = _get(url)
    except Exception:
        return None
    if BOT.search(final):
        return None
    for m in WD.finditer(final + "\n" + html):
        t, c, site = m.groups()
        if site.lower() in ("en-us", "fr-ca", "en-ca", "job", "wday"):
            continue
        api = f"https://{t}.{c}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs"
        facet = jr._wd_country_facet(api)
        try:
            d = jr._req(api, {"appliedFacets": {facet: [jr.WD_CANADA]} if facet else {},
                              "limit": 1, "offset": 0, "searchText": "engineer"})
        except Exception:
            continue
        if isinstance(d.get("total"), int):
            return {"tenant": t, "cluster": c, "site": site,
                    "country_facet": facet, "ca": d["total"]}
    return None


def try_sf(host):
    """SuccessFactors RMK: table theme or tile theme."""
    for scheme in ("https://",):
        url = f"{scheme}{host}/search/?q=engineer"
        try:
            html, final = _get(url)
        except Exception:
            continue
        if BOT.search(final):
            return None
        if jr.SF_ROW.search(html) or jr.SF_TILE.search(html):
            base = re.sub(r"/search/.*$", "", final).rstrip("/")
            return {"base": base}
    return None


def try_wd(host):
    """Follow a careers page to a Workday tenant and verify it live."""
    for url in (f"https://{host}", f"https://{host}/search-results"):
        try:
            html, final = _get(url)
        except Exception:
            continue
        if BOT.search(final):
            return None
        for m in WD.finditer(final + "\n" + html):
            t, c, s = m.groups()
            if s.lower() in ("en-us", "fr-ca", "en-ca", "job", "wday"):
                continue
            api = f"https://{t}.{c}.myworkdayjobs.com/wday/cxs/{t}/{s}/jobs"
            facet = jr._wd_country_facet(api)
            try:
                d = jr._req(api, {"appliedFacets": {facet: [jr.WD_CANADA]} if facet else {},
                                  "limit": 1, "offset": 0, "searchText": "engineer"})
            except Exception:
                continue
            if isinstance(d.get("total"), int):
                return {"tenant": t, "cluster": c, "site": s,
                        "country_facet": facet, "ca": d["total"]}
    return None


def hunt(name):
    label = re.split(r"[|@]", name, 1)[0].strip()
    # "Name@domain.com" -> follow the real site to its careers page.
    if "@" in name:
        _, domain = name.split("@", 1)
        for u in careers_urls(domain.strip()):
            sf = try_sf_url(u)
            if sf:
                return ("successfactors", label, sf)
            wd = try_wd_url(u)
            if wd:
                return ("workday", label, wd)
        return None
    for h in hosts_for(name):
        sf = try_sf(h)
        if sf:
            return ("successfactors", label, sf)
        wd = try_wd(h)
        if wd:
            return ("workday", label, wd)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", nargs="+")
    ap.add_argument("--file")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--targets", default=os.path.join(HERE, "targets.json"))
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    names = list(a.names or [])
    if a.file:
        names += [l.strip() for l in open(a.file) if l.strip() and not l.startswith("#")]
    if not names:
        ap.print_help(); return 1

    tg = json.load(open(a.targets))
    have_wd = {(x["tenant"], x["site"]) for x in tg.get("workday", [])}
    have_sf = {x["base"] for x in tg.get("successfactors", [])}

    found = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for r in ex.map(hunt, names):
            if not r:
                continue
            kind, name, cfg = r
            name = re.split(r"[|@]", name, 1)[0].strip()
            if kind == "workday" and (cfg["tenant"], cfg["site"]) in have_wd:
                continue
            if kind == "successfactors" and cfg["base"] in have_sf:
                continue
            found.append(r)
            extra = f" CA_eng={cfg['ca']}" if "ca" in cfg else ""
            loc = (f"{cfg['tenant']}.{cfg['cluster']}/{cfg['site']}"
                   if kind == "workday" else cfg["base"])
            print(f"  HIT {kind:15} {name:30} {loc}{extra}")

    print(f"\n  {len(found)} new tenants from {len(names)} employers")
    if found and a.add:
        for kind, name, cfg in found:
            cfg.pop("ca", None)
            tg.setdefault(kind, []).append({"name": name, **cfg})
        json.dump(tg, open(a.targets, "w"), indent=2)
        print(f"  workday {len(tg['workday'])} · successfactors {len(tg['successfactors'])}")
    elif found:
        print("  (dry run - pass --add to write them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
