#!/usr/bin/env python3
"""
make_fast.py - build fast.json, the subset of boards worth polling often.

Only ~45 of 387 boards ever produce a match. Polling all of them every few
minutes would be abusive and slow; polling the productive ones is neither.
Run a full sweep, then regenerate:

    python3 job_radar.py --all --level entry,mid,senior --md /tmp/who.md
    python3 make_fast.py /tmp/who.md
"""
import collections, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_SRC = ("greenhouse", "lever", "ashby", "recruitee", "rippling",
              "workable", "smartrecruiters")
DICT_SRC = ("workday", "phenom", "successfactors", "oracle", "jobvite", "html",
             "taleo", "wpjobs", "amazon", "avature", "teamtailor", "ultipro", "jobbank",
             "insightglobal")


# Platforms that must NEVER enter the fast lane, whatever they produced.
#
# jobbank honours robots.txt Crawl-delay: 5 through a per-host limiter shared by
# the list and hydrate passes, so one sweep of it costs ~25s of queries plus ~5s
# per finalist - measured at ~105s on a run with 16 Job Bank matches. The fast
# lane fires every 5 MINUTES, and two lanes that overlap on the same host is
# exactly what produced 285 rate-limit errors in a day on Workday. A board that
# asks to be crawled slowly belongs on the 30-minute lane only.
#
# Today jobbank is also excluded by accident: fetch_jobbank sets `company` to
# the EMPLOYER ("AptPay Inc"), not the board, so "job bank" never appears in the
# digest's producer list and the name match below cannot hit. That is luck, not
# design - it would silently stop protecting us the day that field changed - so
# the exclusion is written down.
# insightglobal is excluded for request VOLUME, not Crawl-delay. The adapter
# enumerates 13 province codes through the locations facet, so one sweep of it
# is ~11-13 requests even with the early breaks. The fast lane fires every 5
# minutes for 13 hours, which is 156 runs a day, so putting it here would mean
# roughly 1,700-2,000 requests a day at insightglobal.com from one residential
# IP. That site is Cloudflare-fronted and already answers a MANAGED challenge on
# /jobs and /companies - it actively polices automation, and the open /all/jobs
# endpoint we depend on is exactly what we would lose first. robots.txt sets no
# Crawl-delay, so this is a judgement about not being the reason it closes,
# rather than a rule being honoured. The 30-minute lane already polls it 26
# times a day, which is ample for a board whose reqs are not minutes-fresh.
NEVER_FAST = ("jobbank", "insightglobal")


def main():
    digest = sys.argv[1] if len(sys.argv) > 1 else "/tmp/who.md"
    producers = {c.lower() for c in
                 collections.Counter(re.findall(r"^- \*\*(.+?)\*\* ·",
                                                open(digest).read(), re.M))}
    full = json.load(open(os.path.join(HERE, "targets.json")))
    fast = {"_comment": "FAST LANE - the boards that actually produce matches, polled "
                        "every few minutes so a new req reaches your phone quickly. A "
                        "subset of targets.json, not a replacement. Regenerate with "
                        "make_fast.py after a full sweep.",
            "devops_vendors": full.get("devops_vendors", [])}
    kept = 0
    for k in TOKEN_SRC:
        fast[k] = [t for t in full.get(k, []) if t.lower() in producers]
        kept += len(fast[k])
    for k in DICT_SRC:
        if k in NEVER_FAST:
            fast[k] = []
            continue
        fast[k] = [t for t in full.get(k, [])
                   if (t.get("name") or "").lower() in producers]
        kept += len(fast[k])
    json.dump(fast, open(os.path.join(HERE, "fast.json"), "w"), indent=2)
    total = sum(len(full.get(k, [])) for k in TOKEN_SRC + DICT_SRC)
    print(f"  fast.json: {kept} boards of {total}")
    for k in TOKEN_SRC + DICT_SRC:
        if fast.get(k):
            print(f"    {k:16} {len(fast[k])}")


if __name__ == "__main__":
    main()
