#!/usr/bin/env python3
"""
canada_employers.py - which companies on your board list can actually employ you.

A US company can only pay a Canadian if it has a Canadian entity, uses an
Employer of Record, or engages you as a contractor. You cannot tell which from
a job ad. But you CAN tell from their board: if a company has *any* posting in
Canada - sales, support, engineering, anything - it is set up to employ here.

That converts "do they hire Canadians?" from guesswork into evidence, and it
tells you which US-remote roles are worth an application.

    python3 canada_employers.py                 # rank every board by Canada presence
    python3 canada_employers.py --min-ca 1      # only companies with a CA footprint
    python3 canada_employers.py --vendors       # DevOps tool vendors only
    python3 canada_employers.py --out ca.md
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-ca", type=int, default=0)
    ap.add_argument("--vendors", action="store_true", help="DevOps tool vendors only")
    ap.add_argument("--out", help="write a markdown table here")
    ap.add_argument("--targets", default=os.path.join(HERE, "targets.json"))
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()

    tg = json.load(open(a.targets))
    vendors = {x.lower() for x in tg.get("devops_vendors", [])}
    jr.VENDORS.update(vendors)

    tasks = []
    for src, fn in jr.ADAPTERS.items():
        for tok in tg.get(src, []):
            if src == "workday":
                for q in jr.WD_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "successfactors":
                for q in jr.SF_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            else:
                tasks.append((src, tok, fn))

    jobs = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fn, tok): src for src, tok, fn in tasks}
        for f in cf.as_completed(futs):
            try:
                jobs.extend(f.result())
            except Exception:
                pass

    seen, uniq = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            uniq.append(j)

    stats = {}
    for j in uniq:
        c = j["company"]
        s = stats.setdefault(c, {"total": 0, "ca": 0, "na": 0, "src": j["source"],
                                 "cities": {}, "example": ""})
        s["total"] += 1
        bucket, _ = jr.classify_location(j["location"])
        if bucket == "CA":
            s["ca"] += 1
            key = j["location"][:40]
            s["cities"][key] = s["cities"].get(key, 0) + 1
            if not s["example"]:
                s["example"] = f'{j["title"][:44]} — {j["location"][:34]}'
        elif bucket == "NA-REMOTE":
            s["na"] += 1

    rows = []
    for c, s in stats.items():
        if s["ca"] + s["na"] < a.min_ca:
            continue
        if a.vendors and c.lower() not in vendors:
            continue
        rows.append((s["ca"] + s["na"], s["ca"], s["na"], s["total"], c, s))
    rows.sort(reverse=True)

    hdr = ("DevOps tool vendors that employ in Canada"
           if a.vendors else "Companies on your board list that employ in Canada")
    L = [f"# {hdr}", "",
         f"_{len(rows)} of {len(stats)} companies · {len(uniq):,} postings scanned_", "",
         "A Canadian posting of any kind proves the company can legally employ you "
         "here. For these, a role advertised as US-remote is worth asking about "
         "rather than skipping.", "",
         "| Company | CA roles | NA-remote | Total open | Example Canadian posting |",
         "|---|---:|---:|---:|---|"]
    for tot_ca, ca, na, total, c, s in rows:
        L.append(f"| {c} | {ca} | {na} | {total} | {s['example'] or '—'} |")

    out = "\n".join(L)
    print(out)
    if a.out:
        open(a.out, "w").write(out + "\n")
        print(f"\nwrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
