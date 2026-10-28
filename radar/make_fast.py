#!/usr/bin/env python3
"""
make_fast.py - build fast.json, the subset of boards worth polling often.

Only ~45 of 387 boards ever produce a match. Polling all of them every few
minutes would be abusive and slow; polling the productive ones is neither.
Run a full sweep, then regenerate:

    python3 job_radar.py --all --level entry,mid,senior --md /tmp/who.md
    python3 make_fast.py /tmp/who.md
"""
import argparse
import collections, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_SRC = ("greenhouse", "lever", "ashby", "recruitee", "rippling",
              "workable", "smartrecruiters", "breezy")
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


def _fast_ok(t):
    """Is this individual board cheap enough for the 5-minute lane?

    NEVER_FAST above excludes whole PLATFORMS; this excludes a single board by
    its SHAPE. A successfactors target carrying "feed": true is one request
    that downloads the ENTIRE board - Deloitte's is 9.16 MB, or 1.53 MB
    gzipped - which is the right trade at 26 sweeps a day on the 30-minute
    lane and the wrong one at 156 on the 5-minute lane. It has to be by shape
    rather than by name for two reasons: the platform itself must stay in the
    fast lane, because the other 14 SuccessFactors tenants are ordinary
    kilobyte search pages, and the next feed board added then inherits this
    protection without anyone having to remember it.
    """
    return not t.get("feed")


def main():
    # argparse rather than sys.argv[1], so `--help` prints help instead of
    # being treated as a digest filename and dying with FileNotFoundError.
    # selftest.py found that on its first run: every other entry point here
    # answers --help, and a tool that crashes when asked how to use it is a
    # tool nobody reads the guard rail of.
    ap = argparse.ArgumentParser(
        description="Rebuild fast.json from a FULL sweep's digest. "
                    "Refuses to shrink the fast lane by more than 40%.")
    ap.add_argument("digest", nargs="?", default="/tmp/who.md",
                    help="markdown digest from `job_radar.py --all --md ...` "
                         "(NOT digest.md, which is incremental)")
    digest = ap.parse_args().digest
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
                   if (t.get("name") or "").lower() in producers
                   and _fast_ok(t)]
        kept += len(fast[k])
    # Refuse to shrink the fast lane drastically. make_fast.py derives the
    # whole fast lane from one digest, so a digest that is truncated, from an
    # incremental run rather than --all, or from a sweep that half failed
    # produces a tiny producer list and a near-empty fast.json - and the fast
    # lane would then poll almost nothing every 5 minutes while looking
    # perfectly healthy. Nothing downstream would notice.
    #
    # digest.md is exactly that trap: daily.sh writes it on every run that
    # found something, but those runs are incremental, so it lists only the NEW
    # rows. Pointing this script at it looks right and is wrong.
    path = os.path.join(HERE, "fast.json")
    prev = 0
    if os.path.exists(path):
        old = json.load(open(path))
        prev = sum(len(v) for k, v in old.items()
                   if isinstance(v, list) and k != "devops_vendors")
    if prev and kept < prev * 0.6:
        print(f"  REFUSING to write: {kept} boards would replace {prev} - a "
              f"drop of {100 - 100 * kept // prev}%.\n"
              f"  That usually means the digest came from an incremental run "
              f"rather than `--all`, or the sweep partly failed.\n"
              f"  Re-run: python3 job_radar.py --all --level entry,mid,senior "
              f"--md /tmp/who.md && python3 make_fast.py /tmp/who.md\n"
              f"  Override by deleting fast.json first if the drop is real.",
              file=sys.stderr)
        return 1
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(fast, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    total = sum(len(full.get(k, [])) for k in TOKEN_SRC + DICT_SRC)
    print(f"  fast.json: {kept} boards of {total}"
          + (f"  (was {prev})" if prev else ""))
    for k in TOKEN_SRC + DICT_SRC:
        if fast.get(k):
            print(f"    {k:16} {len(fast[k])}")


if __name__ == "__main__":
    sys.exit(main() or 0)
