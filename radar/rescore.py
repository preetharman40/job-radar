#!/usr/bin/env python3
"""
rescore.py - bring every tracked posting onto the CURRENT scoring scale.

applications.md is append-only: job_radar.py writes a row when it first sees a
posting and never touches it again. The score in that row is therefore a
SNAPSHOT of whatever the scoring code did on the day it was found. That is
fine until the scorer changes - and on 2026-10-22 it changed enough to matter.
Rebuilding SKILLS into alias groups, anchoring the matches, and adding 25
resume-derived skills moved the mean score of 120 live descriptions from 22.8
to 27.3. Nothing was wrong with either number, but a sorted list holding both
is wrong: an older row sits ~4.5 points low against a newer one for no reason
the reader can see, and the web app's "25+" chip stops meaning one thing.

This script re-reads the postings through the normal adapters, rescores them
with today's code, and writes the new score back in place. It changes ONLY the
score, the level and the matched-keyword list. Status, the Applied column,
contacts, notes and checkboxes are never touched.

    python3 rescore.py                  # dry run - prints what would change
    python3 rescore.py --yes            # apply, after taking a backup
    python3 rescore.py --digest d.md    # reuse a sweep you already ran

WHY IT SWEEPS INSTEAD OF FETCHING EACH URL: a tracked URL is a human-facing
posting page, and for Workday, Phenom and SuccessFactors that page is a
JavaScript shell with no description in the HTML. The real text only comes
from each platform's JSON API, which is what the adapters already speak. So
the cheapest correct way to get a comparable blob is to run the ordinary
sweep and match on URL.

WHAT IT CANNOT DO: mail-sourced rows (ATS is a mail domain like linkedin.com)
have no job description anywhere in the system - they were scored from a
~200-char alert snippet - so they are skipped, not guessed at. Rows whose
posting has since closed no longer appear in a sweep and keep their old score;
verify_tracked.py is what notices those.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402

LEVELS = frozenset(("entry", "mid", "senior", "exec", "intern"))

# "### [43] devops engineer"
H = re.compile(r"^### \[(\d+)\] (.+?)\s*$")
# a bare URL bullet, and the keyword bullet
U = re.compile(r"^- (https?://\S+)")
K = re.compile(r"^- matched:\s*(.*)$")
TICK = re.compile(r"`([^`]+)`")

# tracker shapes
ROW = re.compile(r"(?m)^\|\s*(\d+)\s*\|.*$")
BLOCK = re.compile(r"(?m)^## (\d+)\. ")
FIELD_SCORE = "- **Level:** {lvl} · **Score:** {score} ·"
RE_FIELD = re.compile(
    r"- \*\*Level:\*\*\s*(\w+)\s*·\s*\*\*Score:\*\*\s*(\d+)\s*·")
RE_KW = re.compile(r"(?m)^- \*\*Resume keywords matched:\*\*\s*(.*)$")
RE_ATS = re.compile(r"\*\*ATS:\*\*\s*(\S+)")
RE_APPLY = re.compile(r"\*\*Apply:\*\*\s*(\S+)")


def norm(u):
    """Match the tracker's URL to the digest's. Both are written by this repo,
    but one has been through a markdown round-trip, so trailing punctuation
    and a stray query string must not decide whether a row is found."""
    return (u or "").rstrip(".,);").split("#")[0]


def parse_digest(path):
    """-> {url: (score, level, 'kw, kw')} from a `--md` digest."""
    out, score, title, level = {}, None, None, ""
    for line in open(path, encoding="utf-8"):
        m = H.match(line)
        if m:
            score, title, level = int(m.group(1)), m.group(2), ""
            continue
        if score is None:
            continue
        ticks = TICK.findall(line)
        if ticks:
            for t in ticks:
                if t in LEVELS:
                    level = t
        m = U.match(line.strip())
        if m:
            out[norm(m.group(1))] = [score, level, ""]
            cur = norm(m.group(1))
            continue
        m = K.match(line.strip())
        if m and out:
            # the matched: bullet follows its own URL bullet
            out[cur][2] = m.group(1).strip()
            score = None
    return {k: tuple(v) for k, v in out.items()}


def sweep(targets, keep_md=None, max_age=180):
    """Run the ordinary sweep against a SCRATCH database and return its digest.

    The scratch DB is not a tidiness choice. job_radar.py records every
    posting it shows in `seen`, and `seen` is what makes a posting "new"
    exactly once. Rescoring against the live DB would mark every currently
    open posting as already seen, and the next real sweep would push nothing
    to the phone - a silent, total loss of notifications. --notify and
    --track are likewise never passed.
    """
    tmpdir = tempfile.mkdtemp(prefix="rescore-")
    md = keep_md or os.path.join(tmpdir, "digest.md")
    real_db = jr.DB
    jr.DB = os.path.join(tmpdir, "scratch.sqlite3")
    argv = sys.argv[:]
    try:
        # --max-age is the one knob here that costs real time and real
        # requests: it prunes BEFORE the hydrate pass, so a huge window makes
        # the sweep fetch the full description of every stale requisition on
        # 469 boards. 3650 was the first thing tried and it is indefensible.
        # 180 days comfortably covers anything still in the tracker - the
        # tracker itself is ~1 month old - while leaving the normal pre-hydrate
        # pruning doing most of its job. --min-score 0 is free by contrast:
        # scoring happens after hydrate, so it drops no work, it only stops
        # the digest hiding a row this script needs to see.
        sys.argv = ["job_radar.py", "--targets", targets, "--all",
                    "--min-score", "0", "--max-age", str(max_age),
                    "--level", "entry,mid,senior,exec,intern",
                    "--md", md]
        jr.main()
    finally:
        sys.argv = argv
        jr.DB = real_db
    return md, tmpdir


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tracker", default=jr.TRACKER)
    ap.add_argument("--targets", default=jr.TARGETS)
    ap.add_argument("--digest", help="reuse an existing --md digest")
    ap.add_argument("--keep-digest", help="write the sweep's digest here too")
    ap.add_argument("--min-delta", type=int, default=1,
                    help="ignore changes smaller than this (default 1)")
    ap.add_argument("--max-age", type=int, default=180,
                    help="sweep window in days (default 180); larger means "
                         "hydrating stale reqs for no benefit")
    ap.add_argument("--yes", action="store_true", help="actually write")
    a = ap.parse_args()

    if not os.path.exists(a.tracker):
        sys.exit("no tracker at %s" % a.tracker)

    if a.digest:
        md, tmp = a.digest, None
        print(">> reusing digest %s" % md)
    else:
        print(">> sweeping to rescore (scratch DB, no notify, no track) ...")
        md, tmp = sweep(a.targets, a.keep_digest, a.max_age)

    fresh = parse_digest(md)
    print(">> sweep produced %d scored postings" % len(fresh))

    src = open(a.tracker, encoding="utf-8").read()

    # ---- pair each tracker record with the sweep ------------------------
    plan, skipped_mail, unseen, unchanged = [], 0, 0, 0
    for blk in re.split(r"(?m)^(?=## \d+\. )", src):
        m = re.match(r"## (\d+)\. (.+)", blk)
        if not m:
            continue
        n, head = int(m.group(1)), m.group(2).strip()
        fld = RE_FIELD.search(blk)
        ap_ = RE_APPLY.search(blk)
        ats = RE_ATS.search(blk)
        if not (fld and ap_):
            continue
        if ats and "." in ats.group(1):
            skipped_mail += 1
            continue
        hit = fresh.get(norm(ap_.group(1)))
        if not hit:
            unseen += 1
            continue
        old_lvl, old_sc = fld.group(1), int(fld.group(2))
        new_sc, new_lvl, new_kw = hit
        new_lvl = new_lvl or old_lvl
        kw_now = (RE_KW.search(blk).group(1).strip()
                  if RE_KW.search(blk) else "")
        if (abs(new_sc - old_sc) < a.min_delta and new_lvl == old_lvl
                and new_kw == kw_now):
            unchanged += 1
            continue
        plan.append({"n": n, "head": head, "old": old_sc, "new": new_sc,
                     "old_lvl": old_lvl, "new_lvl": new_lvl,
                     "kw": new_kw or "-"})

    print("\n%-5s %-44s %8s  %s" % ("#", "posting", "score", "level"))
    print("-" * 78)
    for p in sorted(plan, key=lambda p: -(p["new"] - p["old"])):
        lv = ("%s -> %s" % (p["old_lvl"], p["new_lvl"])
              if p["old_lvl"] != p["new_lvl"] else p["new_lvl"])
        print("%-5d %-44s %3d -> %3d  %s"
              % (p["n"], p["head"][:44], p["old"], p["new"], lv))

    ups = sum(1 for p in plan if p["new"] > p["old"])
    dns = sum(1 for p in plan if p["new"] < p["old"])
    print("\n%d to restate (%d up, %d down) · %d already current · "
          "%d not in this sweep · %d mail-sourced (no JD, skipped)"
          % (len(plan), ups, dns, unchanged, unseen, skipped_mail))

    if not plan:
        print(">> nothing to do")
        return 0
    if not a.yes:
        print("\n>> dry run. re-run with --yes to write these.")
        return 0

    # ---- apply ----------------------------------------------------------
    stamp = datetime.now(timezone.utc).date().isoformat()
    bak = os.path.join(os.path.dirname(a.tracker), "backups",
                       "applications.%s-pre-rescore.md.bak" % stamp)
    os.makedirs(os.path.dirname(bak), exist_ok=True)

    # Guarded like the batch writers. rescore rewrites EVERY row, so it
    # is the longest single hold of this lock. Unhandled, a TimeoutError
    # surfaced as a traceback after the plan and the backup path had
    # already been printed, which reads as though the rescore half
    # happened. It did not - nothing is written outside this block - and
    # saying so plainly is worth the four lines.
    try:
        with jr.FileLock(timeout=60):
            src = open(a.tracker, encoding="utf-8").read()
            shutil.copy2(a.tracker, bak)
            print(">> backup %s" % bak)
            out, done = src, 0
            for p in plan:
                # the detail block, bounded so a replace cannot stray into its
                # neighbour, which is how an id like 31 corrupts 310
                bm = re.search(r"(?m)^## %d\. .*$" % p["n"], out)
                if not bm:
                    print("   !! block %d vanished, skipping" % p["n"])
                    continue
                end = out.find("\n---\n", bm.start())
                end = len(out) if end < 0 else end
                blk = out[bm.start():end]
                old_f = FIELD_SCORE.format(lvl=p["old_lvl"], score=p["old"])
                new_f = FIELD_SCORE.format(lvl=p["new_lvl"], score=p["new"])
                if blk.count(old_f) != 1:
                    print("   !! #%d level/score line not unique, skipping" % p["n"])
                    continue
                blk = blk.replace(old_f, new_f)
                blk = RE_KW.sub("- **Resume keywords matched:** " + p["kw"]
                                .replace("\\", "\\\\"), blk, count=1)
                out = out[:bm.start()] + blk + out[end:]

                # the summary table's score cell, and nothing else on the line
                rm = re.search(r"(?m)^\|\s*%d\s*\|.*$" % p["n"], out)
                if rm:
                    cells = rm.group(0).split("|")
                    if len(cells) > 7 and cells[7].strip() == str(p["old"]):
                        cells[7] = cells[7].replace(str(p["old"]),
                                                    str(p["new"]), 1)
                        out = out[:rm.start()] + "|".join(cells) + out[rm.end():]
                done += 1

            tmp_path = a.tracker + ".tracker-rescore.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(out)
            os.replace(tmp_path, a.tracker)
    except TimeoutError as e:
        print("\n!! could not lock the tracker after 60s: %s" % e)
        print("   Another writer holds it - a sweep, inbox.py, or the web")
        print("   app mid-update. NOTHING was changed; run this again.")
        return 1
    print(">> restated %d of %d record(s)" % (done, len(plan)))
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
