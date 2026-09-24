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
             "taleo")


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
