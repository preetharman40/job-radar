#!/usr/bin/env python3
"""
signals.py - companies that are about to hire, before the job exists.

A requisition is approved weeks before it is published. The signals leak first:
a funding round means infrastructure hiring within roughly six weeks, and a
company that has just appeared on a real ATS has usually just outgrown a
spreadsheet.

This is the only part of the system that is not downstream of a job posting.

    python3 signals.py                 # funding news, newest first
    python3 signals.py --days 30
    python3 signals.py --untracked     # only companies NOT already in targets.json
    python3 signals.py --md signals.md

What to do with a hit: email the CTO or VP Eng the week the round is announced,
before the req exists. See the outreach note in README.

Standard library only.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

FEEDS = [
    ("BetaKit", "https://betakit.com/feed/"),
    ("TechCrunch Venture", "https://techcrunch.com/category/venture/feed/"),
]

# A company raising money, not a fund closing one. Requires an actual raise
# verb - "investment" and "valuation" alone matched policy announcements and
# conference write-ups, which are not hiring signals.
FUNDING = re.compile(
    r"\b(raise[sd]|raises|raising|secures?|lands?|nets?|closes?\s+(?:a|its)?\s*"
    r"\$?\d|bags?|picks?\s+up)\b.{0,40}?[\$€£]?\s?\d"
    r"|\bseries\s+[a-e]\b|\bseed\s+round\b|\bpre-seed\b", re.I)

# Venture funds closing their own fund are not employers hiring infra people.
FUND_NOISE = re.compile(
    r"\b(fund|ventures?|capital|partners|LP\b|limited\s+partners|final\s+close|"
    r"commits?|pledge[sd]?|investment\s+summit|tax\s+incentive|catalyst\s+milestone|"
    r"portfolio|accelerator|government|minister|budget)\b", re.I)
AMOUNT = re.compile(r"[\$€£]\s?\d[\d.,]*\s?(million|billion|m\b|bn\b|k\b)?", re.I)
# Infrastructure-shaped companies hire infrastructure people.
INFRA_HINT = re.compile(
    r"\b(cloud|infrastructure|platform|devops|data|ai|ml|saas|api|security|"
    r"fintech|developer|kubernetes|observability|analytics)\b", re.I)
CANADA_HINT = re.compile(
    r"\b(canad\w+|toronto|vancouver|montr[eé]al|calgary|edmonton|ottawa|"
    r"waterloo|kitchener|halifax|winnipeg|alberta|ontario|quebec|bc)\b", re.I)


def _get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as f:
        return f.read().decode("utf-8", "replace")


def strip(t):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t or "")).split())


ITEM = re.compile(r"<item>(.*?)</item>", re.S | re.I)


def field(block, name):
    m = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>",
                  block, re.S | re.I)
    return strip(m.group(1)) if m else ""


def parse_date(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc)
        except Exception:
            continue
    return None


def company_from(title):
    """Headlines usually lead with the company: 'Foo raises $20M ...'"""
    t = re.split(r"\s+(?:raise[sd]?|closes?|secures?|lands?|nets?|announces?|"
                 r"gets?|receives?)\b", title, maxsplit=1, flags=re.I)[0]
    t = re.sub(r"^(Exclusive|Breaking|Q&A|Opinion)[:\s-]+", "", t, flags=re.I)
    return t.strip(" .,:–—’\"'")[:48]


def known_tokens():
    try:
        tg = json.load(open(os.path.join(HERE, "targets.json")))
    except Exception:
        return set()
    out = set()
    for k in ("greenhouse", "lever", "ashby", "recruitee", "rippling"):
        out |= {t.lower() for t in tg.get(k, [])}
    for k in ("workday", "phenom", "successfactors"):
        out |= {(x.get("name") or "").lower() for x in tg.get(k, [])}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--untracked", action="store_true",
                    help="only companies not already in targets.json")
    ap.add_argument("--canada-only", action="store_true")
    ap.add_argument("--md")
    a = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    known = known_tokens()
    rows, scanned = [], 0

    for source, url in FEEDS:
        try:
            xml = _get(url)
        except Exception as e:
            print(f"  {source}: {type(e).__name__}", file=sys.stderr)
            continue
        for block in ITEM.findall(xml):
            scanned += 1
            title = field(block, "title")
            if not title or not FUNDING.search(title):
                continue
            if FUND_NOISE.search(title):
                continue
            # A real headline names the company first; a summary or listicle
            # does not. Anything without a raise verb near the start is prose.
            if not re.search(r"^[A-Z0-9][\w.&\- ']{1,40}\s+(raise|secure|land|net|"
                             r"close|bag|pick)", title, re.I):
                continue
            when = parse_date(field(block, "pubDate"))
            if when and when < cutoff:
                continue
            desc = field(block, "description")[:400]
            blob = f"{title} {desc}"
            if a.canada_only and not CANADA_HINT.search(blob):
                continue
            co = company_from(title)
            slug = re.sub(r"[^a-z0-9]+", "", co.lower())
            tracked = slug in known or co.lower() in known
            if a.untracked and tracked:
                continue
            rows.append({
                "source": source, "company": co, "title": title,
                "link": field(block, "link"),
                "when": when.strftime("%Y-%m-%d") if when else "?",
                "amount": (AMOUNT.search(title) or AMOUNT.search(desc) or [None])
                          and (AMOUNT.search(title) or AMOUNT.search(desc)).group(0).strip()
                          if AMOUNT.search(blob) else "",
                "infra": bool(INFRA_HINT.search(blob)),
                "canada": bool(CANADA_HINT.search(blob)),
                "tracked": tracked,
            })

    rows.sort(key=lambda r: (not r["canada"], not r["infra"], r["when"]), reverse=False)
    rows.sort(key=lambda r: r["when"], reverse=True)

    L = ["# Hiring signals — funding rounds",
         f"_{len(rows)} of {scanned} feed items, last {a.days} days_", "",
         "A round means infrastructure hiring in roughly six weeks. Email the CTO "
         "or VP Eng now, before the requisition exists.", "",
         "| Date | Company | Amount | CA | Infra | On our boards | Story |",
         "|---|---|---|:-:|:-:|:-:|---|"]
    for r in rows:
        L.append(f"| {r['when']} | **{r['company']}** | {r['amount'] or '—'} | "
                 f"{'yes' if r['canada'] else ''} | {'yes' if r['infra'] else ''} | "
                 f"{'tracked' if r['tracked'] else '**no**'} | [link]({r['link']}) |")
    L += ["", "**On our boards = no** is the interesting column: a funded company "
              "the radar cannot see yet. Run `discover.py --url <their careers page>` "
              "to add them."]
    out = "\n".join(L)
    print(out)
    if a.md:
        open(a.md, "w").write(out + "\n")


if __name__ == "__main__":
    main()
