#!/usr/bin/env python3
"""
hn.py - Hacker News "Who is hiring?" for Canada-eligible infra roles.

The monthly thread is posted by the `whoishiring` account on the first weekday
of each month. These postings are written by engineers, not recruiters, usually
carry a direct contact address, and mostly never reach an ATS - so they do not
overlap with anything job_radar.py polls.

    python3 hn.py                    # this month's thread, Canada-eligible infra roles
    python3 hn.py --months 3         # last three threads
    python3 hn.py --all              # every matching post, not just Canada
    python3 hn.py --md hn_digest.md

Standard library only. Uses the public Algolia HN API (no key required).
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402

ALGOLIA = "https://hn.algolia.com/api/v1"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# HN posts follow a loose convention:
#   Company | Role | Location | Full-time | tech stack | contact
ROLE_RE = re.compile(
    r"\b(devops|sre|site\s+reliability|platform\s+engineer|infrastructure|"
    r"cloud\s+engineer|kubernetes|systems?\s+engineer|observability|"
    r"support\s+engineer|solutions?\s+engineer)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
REMOTE_RE = re.compile(r"\bremote\b", re.I)


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as f:
        return json.loads(f.read())


def threads(n=1):
    """Newest `Who is hiring?` threads, excluding `Who wants to be hired?`."""
    d = _get(f"{ALGOLIA}/search_by_date?tags=story,author_whoishiring&hitsPerPage=24")
    out = []
    for h in d.get("hits", []):
        t = (h.get("title") or "").lower()
        if "who is hiring" in t and "wants to be hired" not in t:
            out.append((h["objectID"], h["title"]))
        if len(out) >= n:
            break
    return out


def clean(t):
    t = re.sub(r"<p>", "\n", t or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", html.unescape(t)).strip()


def posts(thread_id):
    d = _get(f"{ALGOLIA}/items/{thread_id}")
    out = []
    for c in d.get("children", []):
        txt = clean(c.get("text"))
        if not txt:
            continue
        head = txt.split("\n")[0][:160]
        company = head.split("|")[0].strip()[:48] if "|" in head else ""
        out.append({
            "id": c.get("id"), "company": company, "text": txt,
            "head": head,
            "emails": sorted(set(EMAIL_RE.findall(txt)))[:2],
            "urls": [u for u in URL_RE.findall(txt)
                     if not u.startswith("https://news.ycombinator")][:2],
            "url": f"https://news.ycombinator.com/item?id={c.get('id')}",
        })
    return out


def score_post(p):
    """Reuse the radar's skill weights so scores mean the same thing."""
    t = p["text"].lower()
    s, hits = 0, []
    for kw, w in jr.SKILLS.items():
        if kw in t:
            s += w
            hits.append(kw)
    if ROLE_RE.search(p["head"]):
        s += 6                      # role named in the headline, not buried
    return s, sorted(set(hits))[:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=1)
    ap.add_argument("--all", action="store_true", help="skip the Canada filter")
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--md")
    a = ap.parse_args()

    L, total = [], 0
    for tid, title in threads(a.months):
        ps = posts(tid)
        total += len(ps)
        rows = []
        for p in ps:
            if not ROLE_RE.search(p["text"]):
                continue
            bucket, ok = jr.classify_location(p["head"])
            # "Remote (US)" and "Remote (USA only)" are not Canada-eligible.
            # Only treat a bare "remote" as open if the headline does not pin it
            # to somewhere you cannot work from.
            remote = bool(REMOTE_RE.search(p["head"])) and bucket != "US-ONLY" \
                and not jr.NOT_CANADA.search(p["head"].lower())
            if not a.all and not (ok or remote):
                continue
            s, kw = score_post(p)
            if s < a.min_score:
                continue
            p.update(score=s, kw=kw, bucket=bucket if ok else ("REMOTE" if remote else "?"))
            rows.append(p)
        rows.sort(key=lambda r: -r["score"])
        L += [f"# {title}", f"_{len(rows)} infra roles of {len(ps)} posts_", ""]
        for r in rows:
            L += [f"### [{r['score']}] {r['head'][:110]}",
                  f"- `{r['bucket']}`" + (f" · **{r['company']}**" if r["company"] else ""),
                  f"- {r['url']}"]
            if r["urls"]:
                L.append(f"- apply: {r['urls'][0]}")
            if r["emails"]:
                L.append(f"- contact: {r['emails'][0]}")
            L += [f"- matched: {', '.join(r['kw']) or '—'}",
                  f"> {r['text'][:300].replace(chr(10), ' ')}…", ""]

    out = "\n".join(L) or "_nothing matched_"
    print(out)
    if a.md:
        open(a.md, "w").write(out + "\n")
    print(f"\n_scanned {total} posts_", file=sys.stderr)


if __name__ == "__main__":
    main()
