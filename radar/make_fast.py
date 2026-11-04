#!/usr/bin/env python3
"""
make_fast.py - build fast.json, the subset of boards worth polling often.

Polling all 566 boards every few minutes would be abusive and slow; polling
the ones that actually produce is neither. The lane is a FIXED SIZE, ranked
from the database:

    1. days in the last 30 on which the board produced a posting that passed
       the gate  (`seen`)
    2. how many postings that was
    3. whether it produced in the sweep that triggered this rebuild (digest)
    4. peak req volume  (`board_health.best_count`)

Keys 1 and 2 are relevance, key 4 is "posts often enough to be worth the
slot", key 3 is only a tiebreak. Before 2026-11-02 key 3 WAS the whole
selection, so one quiet Sunday could evict a board that produces every week -
and did: the lane fell 100 -> 71 in a single rebuild.

    python3 job_radar.py --all --level entry,mid,senior --md /tmp/who.md
    python3 make_fast.py /tmp/who.md
"""
import argparse
import collections, json, os, re, sqlite3, sys
from datetime import datetime, timedelta, timezone

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


WINDOW_DAYS = 30
TARGET = 100
MIN_FAST = 60


def _key(platform, t):
    """The identity board_health uses: "platform/<token or name>"."""
    return f"{platform}/{t if isinstance(t, str) else (t.get('name') or '')}"


def _name(t):
    """The identity the digest and `seen.company` use, lowercased."""
    return (t if isinstance(t, str) else (t.get("name") or "")).lower()


def _gate():
    """-> keep(title, company) using the LIVE title rules, or None.

    Imported here rather than at module scope because job_radar reads
    rules/*.json and opens the tracker on import, and `make_fast.py --help`
    should not need either. If it cannot be imported the ranking still works,
    it just ranks on the unfiltered history - so this says so loudly instead
    of pretending the numbers mean the same thing.
    """
    try:
        sys.path.insert(0, HERE)
        import job_radar as jr
        jr.VENDORS.update(x.lower() for x in
                          json.load(open(jr.TARGETS)).get("devops_vendors", []))
    except Exception as e:
        print(f"  cannot import job_radar ({type(e).__name__}: {e}) - ranking "
              f"on UNFILTERED history, which will favour boards whose "
              f"postings no longer pass the gate", file=sys.stderr)
        return None
    return lambda title, company: bool(jr.title_match(title, company)[0])


def history(conn, window_days=WINDOW_DAYS, keep=None, now=None):
    """-> {company: (active_days, postings)} over the trailing window.

    "Frequent" is counted in DAYS first and postings second, and the order
    matters. A board that dropped eight reqs in one Tuesday burst and nothing
    since is not a board worth polling every five minutes; one that posts a
    single relevant req on five separate days is. Sorting by raw count would
    rank them the other way round.

    `seen` is the right table for this and `board_health` is not: `seen` holds
    postings that PASSED the gate, so it measures relevance, while
    board_health.last_count is every req on the board including the 300 the
    banks always have open.

    But `seen` records what passed the gate ON THE DAY, not what passes now,
    and `keep` is what makes the difference matter. The first build of this
    ranking promoted abcellera, Dexterra, SGS, City of Toronto, Aritzia and
    the Government of Alberta straight back into the fast lane - the exact
    boards the veto groups had just demoted, because their rows are still
    sitting in `seen` from before those groups existed. A board earns a slot
    on what it produces that is wanted TODAY, so every row is re-judged
    through the live gate before it counts. Pass keep=None to rank on the raw
    history, which is only useful for showing what the re-judging changed.
    """
    cut = ((now or datetime.now(timezone.utc))
           - timedelta(days=window_days)).isoformat()
    days, posts = {}, collections.Counter()
    for co, title, first_seen in conn.execute(
            "SELECT company, title, first_seen FROM seen "
            "WHERE first_seen >= ?", (cut,)):
        if keep and not keep(title or "", co or ""):
            continue
        k = (co or "").lower()
        days.setdefault(k, set()).add((first_seen or "")[:10])
        posts[k] += 1
    return {k: (len(v), posts[k]) for k, v in days.items()}


def volume(conn):
    """-> {board_key: best_count}. The fallback signal, and only a fallback.

    A board with no relevant posting in the window still has to be ranked
    somehow, because the lane is a fixed size and has to be filled. Peak req
    volume is the best available proxy for "this employer posts often, so it
    will post something relevant sooner than a board that posts twice a year".
    It never outranks real relevance - it is the fourth sort key.
    """
    return {b: (best or 0) for b, best in
            conn.execute("SELECT board, best_count FROM board_health")}


def rank(full, hist, vol, producers, target=TARGET, ever=None):
    """-> (fast subset, ranked candidates). Pure, so it can be tested.

    Sort keys, in order: active days in the window, postings in the window,
    whether the board has EVER produced something the gate still wants,
    whether it produced in the sweep that triggered this rebuild, then peak
    req volume. The last two keys are `platform` and `name`, which are there
    purely so the sort is TOTAL: with every tie resolved, two runs on the same
    data produce a byte-identical fast.json, and a diff in git therefore means
    the evidence changed rather than that dict order did.

    `ever` sits above req volume because volume alone is a bad filler. The
    lane is a fixed size, so the tail of it is always filled by boards with no
    recent production - and ranking that tail on req count alone handed slots
    to AECOM, Artelia and Continental: large civil-engineering and staffing
    firms that post constantly and whose postings this tool now vetoes by
    design. "Has produced something wanted at least once" is a weak signal but
    it is a signal, and it beats "is big".
    """
    cands = []
    for platform in TOKEN_SRC + DICT_SRC:
        if platform in NEVER_FAST:
            continue
        for t in full.get(platform, []):
            if isinstance(t, dict) and not _fast_ok(t):
                continue
            nm = _name(t)
            d, n = hist.get(nm, (0, 0))
            cands.append((d, n, 1 if ever and nm in ever else 0,
                          1 if nm in producers else 0,
                          vol.get(_key(platform, t), 0), platform, nm, t))
    cands.sort(key=lambda c: (-c[0], -c[1], -c[2], -c[3], -c[4], c[5], c[6]))
    chosen = cands[:target]
    picked = {(c[5], c[6]) for c in chosen}
    fast = {}
    for platform in TOKEN_SRC + DICT_SRC:
        # Emitted in targets.json order, not rank order, so the diff between
        # two weeks shows which boards moved rather than re-ordering all 100.
        fast[platform] = [t for t in full.get(platform, [])
                          if (platform, _name(t)) in picked]
    return fast, chosen


def main():
    # argparse rather than sys.argv[1], so `--help` prints help instead of
    # being treated as a digest filename and dying with FileNotFoundError.
    # selftest.py found that on its first run: every other entry point here
    # answers --help, and a tool that crashes when asked how to use it is a
    # tool nobody reads the guard rail of.
    ap = argparse.ArgumentParser(
        description=f"Rebuild fast.json: the {TARGET} boards that produce "
                    f"relevant postings most often, by days active in the "
                    f"last {WINDOW_DAYS} days. Refuses to write fewer than "
                    f"--min boards, or to shrink the lane by over 40%.")
    ap.add_argument("digest", nargs="?", default="/tmp/who.md",
                    help="markdown digest from `job_radar.py --all --md ...`. "
                         "Only a TIEBREAK now - see --target. Missing is fine.")
    ap.add_argument("--target", type=int, default=TARGET,
                    help=f"how many boards the lane should hold "
                         f"(default {TARGET})")
    ap.add_argument("--min", type=int, default=MIN_FAST,
                    help=f"absolute floor; refuse to write fewer "
                         f"(default {MIN_FAST}, 0 disables)")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS,
                    help=f"trailing days of history to rank on "
                         f"(default {WINDOW_DAYS})")
    ap.add_argument("--db", default=os.path.join(HERE, "seen.sqlite3"))
    ap.add_argument("--out", default=os.path.join(HERE, "fast.json"),
                    help="where to write (tests point this elsewhere)")
    a = ap.parse_args()
    digest = a.digest
    # The digest used to BE the selection, which made a truncated or
    # incremental digest produce a near-empty fast.json - the trap the 40%
    # guard was bolted on to catch. It is now one tiebreak among four and is
    # read best-effort, so that failure mode is designed out rather than
    # guarded against. Still reported, because a digest that silently stopped
    # parsing should be visible rather than merely harmless.
    producers = set()
    try:
        producers = {c.lower() for c in
                     re.findall(r"^- \*\*(.+?)\*\* ·",
                                open(digest).read(), re.M)}
    except OSError as e:
        print(f"  no digest ({e.__class__.__name__}) - ranking on history "
              f"alone", file=sys.stderr)

    # Loud, not lenient: the whole selection now rests on these two tables,
    # and a fast lane built from an unreadable database would be a fast lane
    # built from nothing while printing a cheerful board count.
    try:
        conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        gate = _gate()
        hist, vol = history(conn, a.window, keep=gate), volume(conn)
        # Same query over the whole table: "has this board EVER produced
        # something the gate still wants". `seen` only reaches back to
        # 2026-09-18 today, so this is a thin signal now and a real one later.
        ever = set(history(conn, 3650, keep=gate))
    except sqlite3.Error as e:
        print(f"  cannot read {a.db}: {e} - refusing to rebuild the fast "
              f"lane without the history it is ranked on", file=sys.stderr)
        return 1
    if not vol:
        print("  board_health is empty - refusing, because every board would "
              "rank identically and the lane would be arbitrary",
              file=sys.stderr)
        return 1
    full = json.load(open(os.path.join(HERE, "targets.json")))
    picked, ranked = rank(full, hist, vol, producers, a.target, ever=ever)
    fast = {"_comment": f"FAST LANE - the {a.target} boards that produce "
                        f"relevant postings most often, ranked by days active "
                        f"in the trailing {a.window} days then by req volume. "
                        f"Polled every few minutes so a new req reaches your "
                        f"phone quickly. A subset of targets.json, not a "
                        f"replacement. Regenerate with make_fast.py.",
            "devops_vendors": full.get("devops_vendors", [])}
    fast.update(picked)
    kept = sum(len(v) for v in picked.values())
    path = a.out
    prev = 0
    if os.path.exists(path):
        old = json.load(open(path))
        prev = sum(len(v) for k, v in old.items()
                   if isinstance(v, list) and k != "devops_vendors")

    # TWO guards, because they fail differently.
    #
    # The ABSOLUTE floor is the one that matters. The relative guard below
    # compares against the PREVIOUS file, so it cannot see a ratchet: on
    # 2026-11-02 the lane had gone 100 -> 71 in one Sunday, a 29% drop that
    # passed, and 71 -> 50 -> 35 -> 25 would each have passed the same test
    # one Sunday at a time until the fast lane polled almost nothing. Every
    # check in the system would have stayed green throughout, because each
    # individual step is within tolerance. A floor has no memory to lose.
    if a.min and kept < a.min:
        print(f"  REFUSING to write: {kept} boards is below the floor of "
              f"{a.min}.\n"
              f"  The lane is ranked from `seen` and `board_health`, so this "
              f"means one of them is thin, not that the boards went quiet.\n"
              f"  Check: python3 board_health.py --report | tail -5\n"
              f"  Override with --min 0 if {kept} is genuinely all there is.",
              file=sys.stderr)
        return 1
    if prev and kept < prev * 0.6:
        print(f"  REFUSING to write: {kept} boards would replace {prev} - a "
              f"drop of {100 - 100 * kept // prev}%.\n"
              f"  With --target {a.target} this should not happen unless "
              f"targets.json shrank or the history window went empty.\n"
              f"  Override by deleting {os.path.basename(path)} first if the "
              f"drop is real.", file=sys.stderr)
        return 1
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(fast, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    total = sum(len(full.get(k, [])) for k in TOKEN_SRC + DICT_SRC)
    chosen = ranked[:a.target]
    recent = sum(1 for c in chosen if c[0])
    proven = sum(1 for c in chosen if not c[0] and c[2])
    print(f"  {os.path.basename(path)}: {kept} boards of {total}"
          + (f"  (was {prev})" if prev else "")
          + f"\n    {recent} produced in the last {a.window}d, {proven} have "
            f"produced before, {kept - recent - proven} on req volume alone"
          + f"\n    {len(producers)} producers read from the digest "
            f"(tiebreak only)")
    for k in TOKEN_SRC + DICT_SRC:
        if fast.get(k):
            print(f"    {k:16} {len(fast[k])}")


if __name__ == "__main__":
    sys.exit(main() or 0)
