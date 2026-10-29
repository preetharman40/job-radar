#!/usr/bin/env python3
"""
job_radar.py - Canada DevOps / SRE / vendor-support job radar.

Polls ATS JSON APIs directly - the same endpoints a company's own careers page
calls. A req shows up here seconds after a recruiter clicks Publish, hours to
weeks before LinkedIn, Indeed or Google have it.

Three role families, all scored against your resume:
  1. DevOps / SRE / Platform / Cloud Infrastructure
  2. Support-adjacent infra (build & release, sysadmin, observability, endpoint)
  3. Technical Support / Solutions Engineer *at DevOps tool vendors* - the
     highest-conversion target for a 6-yr-support + 1-yr-DevOps profile

Usage:
    python3 job_radar.py --seed              # first run: record state, print nothing
    python3 job_radar.py                     # only NEW postings since last run
    python3 job_radar.py --all               # everything currently matching
    python3 job_radar.py --level entry,mid   # drop Senior-titled roles
    python3 job_radar.py --family vendor     # only vendor support/solutions roles
    python3 job_radar.py --md digest.md      # write a markdown digest

No third-party dependencies. Python 3.8+.
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import fcntl
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "seen.sqlite3")
TARGETS = os.path.join(HERE, "targets.json")

# The adapters live in their own package and share fetchcore's transport, so
# HERE has to be importable - and it is not always. discover.py loads this file
# with spec_from_file_location() from a different working directory, which puts
# *its* directory on sys.path and not this one; the import below would then
# fail, discover.py would swallow the error (it is written to keep working when
# the engine does not) and silently lose classify_location, title_match and the
# shared Ashby rate limiter. One line prevents that for every loader.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import adapters                                                   # noqa: E402
from adapters import ADAPTERS, WD_TRUNCATED                       # noqa: E402
from fetchcore import days_ago                                     # noqa: E402

# What the rest of the radar may read off this module. Everything here is
# this file's own: paths, the classifier, the scorer, the tracker, the locks
# and the push. Transport lives in `fetchcore` and anything platform-specific
# in `adapters`, and callers import those directly - this module used to
# re-export both, which is how nine scripts ended up depending on privates
# like `jr._req` and `jr.SF_TILE`. selftest asserts every `jr.X` in the tree
# is named below.
__all__ = [
    # paths and config
    "DB", "TARGETS", "TRACKER", "NTFY_FILE", "RULES_DIR", "HERE", "VENDORS",
    # title / level / location / score - the pure core
    "TITLE_RULES", "SKILL_GROUPS", "SKILLS", "NOT_CANADA",
    "title_match", "level_of", "classify_location", "provinces_of",
    "arrangement", "canadian_option", "score", "band_of",
    # per-posting checks that need the classifier
    "offshore_marker", "office_mismatch",
    # pipeline
    "hydrate", "append_to_tracker", "main",
    # tracker duplicate guard
    "dupe_norm", "loc_compatible", "parse_tracker_rows",
    "tracker_row_active", "find_tracker_dupe",
    # infrastructure
    "FileLock", "TRACKER_LOCK", "db", "db_write", "atomic_write",
    "notify", "ntfy_topic",
]

VENDORS = set()   # filled from targets.json at startup

# ------------------------------------------------------------------ titles ---
# The tuning data - title rules and skill weights - lives in rules/*.json
# rather than here. Two reasons. A weight change should not be able to put a
# syntax error in the engine; and these are the only two things in the radar
# that actually get tuned, so the measurement passes that set them become data
# transformations rather than code edits. The justification for each entry
# travels with it in a `note` field, because a rule whose reasoning has been
# lost is a rule nobody can safely change.

RULES_DIR = os.path.join(HERE, "rules")


def _load_rules(filename, key, floor):
    """Read rules/<filename>[key], and refuse anything that looks truncated.

    Loudly, because the quiet failure here is unbounded: an empty TITLE_RULES
    makes every title tier 0, and a sweep then reads 56,000 postings, matches
    none, prints "Nothing new" and exits 0. `floor` is the same shrink guard
    make_fast.py grew after fast.json was nearly rewritten with one board.
    """
    path = os.path.join(RULES_DIR, filename)
    try:
        with open(path) as f:
            items = json.load(f)[key]
    except Exception as e:
        raise RuntimeError(f"cannot read {path}: {type(e).__name__}: {e}")
    if not isinstance(items, list) or len(items) < floor:
        raise RuntimeError(f"{path}: {len(items)} {key}, expected at least "
                           f"{floor} - refusing to run on a truncated rule "
                           f"set")
    return items


def _title_rules():
    """-> [(regex, tier, family)]. Tier drives the base score, family --family.

    title_match() returns the MAX tier among matching rules, so a rule added
    here can only raise a posting's tier, never lower it. That is what makes
    the list safe to extend.
    """
    out = []
    for i, r in enumerate(_load_rules("titles.json", "rules", 20)):
        try:
            re.compile(r["re"])
            out.append((r["re"], int(r["tier"]), str(r["family"])))
        except Exception as e:
            raise RuntimeError(f"rules/titles.json rule {i} "
                               f"({r.get('re')!r}): {type(e).__name__}: {e}")
    return out


def _skill_groups():
    """-> ((canonical name, weight, surface forms), ...).

    The uniqueness check is the invariant the 2026-10-22 restructure existed
    for: one skill, one score, many spellings. Two groups claiming the same
    surface form is exactly how a JD reading "Kubernetes (K8s)" used to score
    the one skill twice, at two different weights.
    """
    out, seen = [], {}
    for i, g in enumerate(_load_rules("skills.json", "groups", 50)):
        forms = g.get("forms")
        # `forms` must be non-empty, and emptiness is the dangerous case
        # rather than the harmless one: "|".join(()) is "", re.compile("")
        # matches every string, and the group would then score on every JD
        # ever read. all() over an empty list is True, so this is checked
        # separately rather than inside the comprehension.
        if not g.get("skill") or g.get("weight") not in (1, 2, 3, 4) \
                or not isinstance(forms, list) or not forms \
                or not all(isinstance(f, str) and f for f in forms):
            raise RuntimeError(f"rules/skills.json group {i} "
                               f"({g.get('skill')!r}): needs a skill, a "
                               f"weight of 1-4, and one or more forms")
        for f in forms:
            if f in seen:
                where = (f"listed twice under {g['skill']!r}"
                         if seen[f] == g["skill"] else
                         f"claimed by both {seen[f]!r} and {g['skill']!r}")
                raise RuntimeError(f"rules/skills.json: {f!r} is {where} - a "
                                   f"surface form belongs to one skill, or a "
                                   f"JD naming it scores twice")
            seen[f] = g["skill"]
        out.append((g["skill"], int(g["weight"]), tuple(forms)))
    return tuple(out)


TITLE_RULES = _title_rules()

# Straight off the resume. Ranks, never filters. The groups, their weights and
# the measurement that set them are in rules/skills.json - including why
# "cloud", "automation" and "monitoring" are deliberately absent despite being
# on the resume, and why ECS, GitOps and Kafka are held below their rarity.
SKILL_GROUPS = _skill_groups()

# Anchored on both sides so a form only matches as a whole token. \b is wrong
# for forms that END in a non-word character, and several of these contain "/"
# or "-", so the boundary is spelled out as "no alphanumeric either side".
#
# The trailing `s?` is not cosmetic. Anchoring alone made the matcher STRICTER
# than the old substring test in one direction: "incident" stopped matching
# "incidents", and JDs overwhelmingly write the plural ("manage incidents",
# "resolve escalations", "harden endpoints"). Without it, fixing the false
# positives would have introduced a crop of false negatives - six of the eight
# largest score drops in the A/B were a lost plural, not a lost double-count.
_SKILL_RX = tuple(
    (canon, weight,
     re.compile("|".join(r"(?<![a-z0-9])" + re.escape(f) + r"s?(?![a-z0-9])"
                         for f in forms)))
    for canon, weight, forms in SKILL_GROUPS)

# Kept flat for hn.py, which ranks Hacker News "who is hiring" comments with
# the same vocabulary. Every surface form maps to its group's single weight,
# so the two lanes cannot disagree about what a skill is worth.
SKILLS = {f: w for _, w, fs in SKILL_GROUPS for f in fs}

# Levels --------------------------------------------------------------------
L_EXEC = re.compile(
    r"\b(staff|principal|distinguished|architect|director|head\s+of|"
    r"vp\b|vice\s+president|manager|lead\b|fellow)\b", re.I)
# "Senior Associate" is a MID rank on the banking ladder - Analyst, Associate,
# Senior Associate, then VP - not a senior engineer. L_SENIOR is tested before
# L_ENTRY, so "Sr Associate Platform Engineer" came out `senior`, and the web
# app's default view hides senior. A score-36 Scotiabank role was invisible
# while its notification had already reached the phone, which is exactly the
# "I got a push but cannot find it" report. Checked ahead of L_SENIOR.
L_SENIOR_ASSOC = re.compile(r"\b(senior|sr\.?)\s+associate\b", re.I)
L_SENIOR = re.compile(r"\b(senior|sr\.?|iii|iv)\b", re.I)
# Engineering-ladder numerals: I is entry, II is MID, III/IV are senior.
# "ii" sat in L_ENTRY until 2026-10-22, so every "Engineer II" came out
# entry-level and collected the +4 entry bonus in score() that belongs to
# genuinely junior reqs. commonsku's DevOps Engineer II scored 43 as entry
# and scores 41 as mid, which is the more honest number for a role a rung
# up the ladder. A numeral never overrides a seniority WORD: L_SENIOR runs
# first, so "Senior Software Engineer II" stays senior, and "Specialist
# II-IC ... (New or Recent Graduate)" stays entry on `graduate`. Of the 12
# "II" titles seen so far, 9 move entry -> mid and all 9 read correctly as
# mid - one of them literally says "Intermediate Infrastructure Analyst".
# Bare digits are deliberately NOT handled: "Engineer 2" already falls to
# the mid default, while a bare \b1\b would also match requisition numbers
# that some boards append to the title.
L_ENTRY = re.compile(
    r"\b(junior|jr\.?|associate|entry[\s-]?level|graduate|new\s+grad|"
    r"early\s+career|tier\s*1|t1|l1|level\s*1|\bi\b)\b", re.I)
L_INTERN = re.compile(r"\b(intern|internship|co-?op|student|placement)\b", re.I)


def level_of(title):
    t = title or ""
    if L_INTERN.search(t):
        return "intern"
    if L_EXEC.search(t):
        return "exec"
    if L_SENIOR_ASSOC.search(t):
        return "mid"
    if L_SENIOR.search(t):
        return "senior"
    if L_ENTRY.search(t):
        return "entry"
    return "mid"


# --------------------------------------------------------------- locations ---

# Cities whose name alone is enough - no non-Canadian city shares them.
CA_CITIES_SAFE = [
    "toronto", "mississauga", "brampton", "markham", "vaughan", "scarborough",
    "north york", "etobicoke", "oakville", "kitchener", "guelph", "oshawa",
    "barrie", "pickering", "ajax", "whitby", "milton", "burnaby", "coquitlam",
    "kelowna", "saskatoon", "regina", "winnipeg", "calgary", "edmonton",
    "montreal", "montréal", "gatineau", "sherbrooke", "laval", "quebec city",
    "québec", "moncton", "fredericton", "charlottetown", "st. john's",
    "mont-royal", "brossard", "longueuil", "ottawa", "waterloo",
]
# Cities that exist in several countries. "Burlington, MA" and "Melbourne,
# Victoria, Australia" were both being read as Canadian - these now need a
# province or "canada" alongside them.
CA_CITIES_AMBIGUOUS = [
    "london", "windsor", "hamilton", "cambridge", "victoria", "burlington",
    "richmond", "kingston", "vancouver", "halifax", "surrey", "waterloo",
    "sudbury", "peterborough", "chatham", "stratford", "newmarket", "aurora",
]
# Substring matching burned us twice: "amer" inside "United States of America"
# and "milton" inside "Hamilton". City and region names are matched on word
# boundaries, never with `in`.
def _word_re(terms):
    return re.compile(r"(?<![a-z])(" + "|".join(
        re.escape(t).replace(r"\ ", r"\s+") for t in terms) + r")(?![a-z])", re.I)


CA_REGIONS = [
    "canada", "canadian", "ontario", "on", "quebec", "québec",
    "qc", "british columbia", "bc", "alberta", "ab", "manitoba", "mb",
    "saskatchewan", "sk", "nova scotia", "ns", "new brunswick", "nb",
    "newfoundland", "nl", "prince edward island",
]
# Bare province codes only count after a comma or bracket - "ON" and "AB" are
# far too common in free text otherwise.
CA_PROVINCE_CODE = re.compile(r"[,(/]\s*(on|qc|bc|ab|mb|sk|ns|nb|nl|pe)\b", re.I)
CA_SAFE_RE = _word_re(CA_CITIES_SAFE)
CA_AMBIG_RE = _word_re(CA_CITIES_AMBIGUOUS)
CA_REGION_RE = _word_re([r for r in CA_REGIONS if len(r) > 2])
# Wordings a Canadian resident can actually hold.
NORTH_AMERICA = re.compile(
    r"north\s+america(n)?|remote\s*[-,]?\s*americas?\b|\bamer\b"
    r"|north\s*(?:&|and|\+|/)\s*south\s+america"
    r"|u\.?s\.?a?\s*(or|/|&|and)\s*canada"
    r"|united\s+states\s+(or|and|/)\s+canada", re.I)
# "Americas Remote" means the same thing as "Remote - Americas", but the regex
# above only matches the second word order, so Customer.io's whole Americas
# lane read as OTHER and was discarded.
#
# A bare "Americas" - no direction word, nothing to anchor on - is the same
# hemisphere a third time, and two real wordings are written that way:
# "Home Based - Americas" (74 postings in one sweep, the EOR phrasing) and a
# plain "Americas" (every Resend req, including its Platform Engineer). Both
# read as OTHER and were thrown away. Plural only, deliberately: an
# unqualified singular "America" is vanishingly rare, while "Americana" is
# both a Brazilian city and a Los Angeles mall, so matching the singular buys
# nothing and risks those three strings.
#
# LATAM is the one case none of these may admit - "Latin America Remote" and
# "Sao Paulo, Latin America, Brazil" are not roles you can hold - so the veto
# no longer requires "remote" to be present either. It cannot swallow
# "North & South America": NORTH_AMERICA claims that string first, above.
AMERICAS = re.compile(r"americas?\s*[-,/]?\s*remote\b"
                      r"|(?<![a-z])americas(?![a-z])", re.I)
AMERICAS_LATAM = re.compile(r"(?:latin|south|central)\s+americas?|\blatam\b", re.I)
# Wordings that mean "anywhere on earth", which includes Canada. Deliberately
# narrow: a bare "Remote" or "Fully remote" is usually remote-within-the-US, and
# "work from anywhere in the United States" says so outright, so neither counts.
# Measured against one full sweep this moves 148 of 56,524 postings.
GLOBAL_REMOTE = re.compile(
    r"remote[\s,\-/(]*global(?![a-z])|global(?:ly)?[\s,\-]*remote(?![a-z])"
    r"|\bworldwide\b|anywhere in the world"
    r"|work from anywhere(?!\s+in\s+the\s+(?:us\b|u\.s|united\s+states))"
    r"|remote\s*[-(]\s*anywhere", re.I)
# Reached only after the Canada check fails, so a bare "United States" here
# means the req really is US-only.
US_ONLY = re.compile(
    r"united\s+states|\busa?\b|u\.s\.|us[\s-]+(only|based|remote)", re.I)
MULTI_HINT = re.compile(r"\b\d+\s+locations?\b|\bmultiple locations\b", re.I)
# GLOBAL exists only because a req is open to the whole world, so it carries no
# evidence that THIS role suits you - and the vendor-family rules (support,
# sales, field, solutions engineer) are the loosest ones we have. Admitting both
# at once put seven Canonical support and sales reqs above every real SRE role
# in the same batch: "Associate Linux Support Engineer" scored 24, the
# "Site Reliability / Gitops Engineer" 15. Support roles are excluded by
# request, so vendor titles have to earn their place somewhere geographic.
# CA-OPTION is deliberately NOT in here: a named Canadian city on the req is the
# same evidence CA has, where vendor-family already counts.
NO_VENDOR_BUCKETS = {"GLOBAL"}

# Province detection, so results can be filtered to where you actually live.
# Word boundaries, for the same reason everything else here uses them.
PROVINCES = {
    "AB": ["alberta", "calgary", "edmonton", "red deer", "lethbridge",
           "fort mcmurray", "grande prairie", "medicine hat", "airdrie",
           "sherwood park", "st. albert", "banff", "canmore", "leduc",
           "spruce grove"],
    "ON": ["ontario", "toronto", "ottawa", "mississauga", "brampton", "markham",
           "vaughan", "scarborough", "north york", "etobicoke", "oakville",
           "kitchener", "waterloo", "guelph", "hamilton", "london", "windsor",
           "kingston", "barrie", "oshawa", "burlington", "cambridge", "milton"],
    "BC": ["british columbia", "vancouver", "burnaby", "surrey", "richmond",
           "victoria", "kelowna", "coquitlam", "langley", "abbotsford"],
    "QC": ["quebec", "qu\u00e9bec", "montreal", "montr\u00e9al", "laval",
           "gatineau", "sherbrooke", "longueuil", "brossard"],
    "MB": ["manitoba", "winnipeg"],
    "SK": ["saskatchewan", "saskatoon", "regina"],
    "NS": ["nova scotia", "halifax"],
    "NB": ["new brunswick", "moncton", "fredericton"],
    "NL": ["newfoundland", "st. john's"],
    "PE": ["prince edward island", "charlottetown"],
}
PROV_RE = {k: _word_re(v) for k, v in PROVINCES.items()}
PROV_CODE = re.compile(r"[,(/]\s*(AB|ON|BC|QC|MB|SK|NS|NB|NL|PE)\b")


def provinces_of(loc):
    """Every Canadian province a posting names. Empty = remote or unclear."""
    if not loc:
        return set()
    out = {k for k, rx in PROV_RE.items() if rx.search(loc)}
    out |= set(PROV_CODE.findall(loc.upper()))
    return out


REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
ONSITE_RE = re.compile(r"\bon[\s-]?site\b|\bin[\s-]?office\b", re.I)


def arrangement(loc, blob=""):
    """remote / hybrid / onsite where the posting actually says so."""
    hay = f"{loc} {blob[:600]}"
    if HYBRID_RE.search(hay):
        return "hybrid"
    if ONSITE_RE.search(hay):
        return "onsite"
    if REMOTE_RE.search(loc):
        return "remote"
    return ""
# Country/state markers that disqualify an ambiguous city outright.
NOT_CANADA = re.compile(
    r"\b(australia|united kingdom|u\.k\.|england|scotland|ireland|"
    r"new zealand|india|singapore|germany|france|netherlands|spain|japan|"
    r"brazil|mexico|philippines|poland|israel|victoria,\s*australia)\b", re.I)
# US state codes are matched CASE-SENSITIVELY against the original string.
# Folding them to lowercase made "or" match the English word in
# "Remote (ON, AB, BC, or NS Only)", which silently dropped 53 Canadian
# postings - every one of them Alberta-eligible.
# ", CA" is deliberately absent: SuccessFactors writes Canadian locations as
# "Toronto, ON, CA". A US city with ", CA" is caught by having no Canadian
# city or region marker at all.
US_STATE_CODE = re.compile(
    r"[,(]\s*(MA|VA|WA|OR|NY|TX|IL|GA|NC|PA|OH|MI|AZ|CO|UT|NJ|MD|MN|MO|"
    r"IN|TN|WI|SC|AL|KY|LA|OK|CT|IA|AR|MS|KS|NV|NM|NE|ID|NH|ME|RI|MT|DE|"
    r"SD|ND|AK|VT|WY|HI|WV|FL)\b")


_LOC_SPLIT = re.compile(r"\s*(?:/|;|\|)\s*")


def canadian_option(loc):
    """True when one segment of a multi-location req is plainly Canadian.

    A req listing "London / Dublin / Hungary / Toronto / Berlin" offers Toronto
    as a real choice, but the foreign veto below sees Germany and throws the
    whole thing away - which is how an ElevenLabs Infrastructure Engineer req
    filed in Toronto stayed invisible. Judged per segment instead, so a foreign
    city in the same list cannot veto a Canadian one.

    Recursion is bounded: segments contain no separator, so the nested call
    takes the len < 2 exit immediately.
    """
    segs = [s.strip() for s in _LOC_SPLIT.split(loc or "") if s.strip()]
    if len(segs) < 2:
        return False
    return any(classify_location(s) == ("CA", True) for s in segs)


def classify_location(loc):
    """-> (bucket, keep). CA | NA-REMOTE | MULTI | US-ONLY | OTHER"""
    if not loc:
        return "OTHER", False
    l = loc.lower()
    # An explicit "Canada" wins outright, before any foreign veto. Boards list
    # one requisition under several countries sharing a single URL - an MLabs
    # SRE role read "Remote - United Kingdom / Remote - Poland / ... / Remote -
    # Canada" - and an absolute veto threw away a role that is genuinely open
    # to you.
    if re.search(r"(?<![a-z])canad(a|ian)(?![a-z])", l):
        return "CA", True
    # Country names on the lowercased string; state codes on the original, so
    # an uppercase "OR" is Oregon but a lowercase "or" is just the word.
    foreign = bool(NOT_CANADA.search(l)) or bool(US_STATE_CODE.search(loc))
    has_region = bool(CA_REGION_RE.search(l)) or bool(CA_PROVINCE_CODE.search(l))
    if (CA_SAFE_RE.search(l) or has_region) and not foreign:
        return "CA", True
    # An ambiguous city counts only with a Canadian qualifier and no foreign one.
    if CA_AMBIG_RE.search(l) and has_region and not foreign:
        return "CA", True
    # A Canadian city offered as one option among many. Ahead of US_ONLY, which
    # otherwise claims "Calgary, Alberta / Atlanta, GA / Arizona" (Benevity).
    if canadian_option(loc):
        return "CA-OPTION", True
    if GLOBAL_REMOTE.search(l):
        return "GLOBAL", True
    if NORTH_AMERICA.search(l) or (AMERICAS.search(l)
                                   and not AMERICAS_LATAM.search(l)):
        return "NA-REMOTE", True
    if MULTI_HINT.search(l):
        return "MULTI", True          # Workday collapses "3 Locations"
    if US_ONLY.search(l):
        return "US-ONLY", False
    return "OTHER", False


# ----------------------------------------------------------------- scoring ---

def title_match(title, company):
    """-> (tier, family) or (0, None). Vendor titles only count at vendors."""
    t = (title or "").lower()
    is_vendor = company.lower() in VENDORS
    best = (0, None)
    for rx, tier, fam in TITLE_RULES:
        if not re.search(rx, t):
            continue
        if fam == "vendor" and not is_vendor:
            continue
        if tier > best[0]:
            best = (tier, fam)
    return best


def score(title, company, tier, family, blob):
    s = tier * 3
    hits = []
    b = (blob or "").lower()
    # One group scores once, however many of its spellings the JD uses, and
    # the hit is recorded under the group's canonical name so the displayed
    # list reads as skills rather than as whichever synonym appeared.
    for canon, w, rx in _SKILL_RX:
        if rx.search(b):
            s += w
            hits.append((w, canon))

    lvl = level_of(title)
    s += {"entry": 4, "mid": 2, "senior": -2, "exec": -10, "intern": -8}[lvl]

    # A vendor support role is the thesis of this search - weight it like one.
    if family == "vendor":
        s += 4
        if re.search(r"\b(kubernetes|linux|cloud|devops|platform|infra)\b",
                     (title or "").lower()):
            s += 2
    # Heaviest first, name as the tie-break, so a truncated list is the
    # ten skills that earned the score and not the first ten by name.
    hits.sort(key=lambda h: (-h[0], h[1]))
    return s, [c for _, c in hits][:10], lvl


# Offshore hiring markers that staffing agencies put in the TITLE while filing
# the req at the client's Canadian address. Insight Global's "- INTL India"
# convention is the case that prompted this: an MLOps Engineer scoring 39 -
# well above the notify threshold - sat at workAddress Vancouver, BC, and its
# description mentions neither India nor Canada, so the title is the only
# geographic signal there is.
#
# Flagged rather than dropped, deliberately. "Marketing Campaign Manager
# (Intl.)" in Kanata is plausibly a Canadian role with an international remit,
# the evidence for the India one is a single title token, and silently hiding a
# Vancouver req on that basis is the worse error. 3 of 282 rows carry a marker.
OFFSHORE_TITLE = re.compile(
    r"\bINTL\b[\s.\-]*(india|mexico|philippines|brazil|colombia|argentina|"
    r"costa\s*rica|poland|romania)\b|\b(latam|emea|apac)\b", re.I)


def offshore_marker(j):
    """True when the title says offshore but the address says Canada."""
    m = OFFSHORE_TITLE.search(j.get("title") or "")
    if not m or classify_location(j.get("location") or "")[0] != "CA":
        return None
    return (f"title carries an offshore hiring marker ({m.group(0).strip()}) "
            f"though the req is filed at a Canadian address - confirm where "
            f"the role is actually hired before applying")


def office_mismatch(j):
    """True when the location text names a Canadian city the office tags omit.

    Seen on PagerDuty's SRE I: location "Atlanta; Toronto", offices ["Atlanta"].
    The role really is Toronto-eligible - the application form asks which office
    you live near - but PagerDuty's careers page files it under Atlanta, so it is
    invisible to anyone filtering that page by Toronto. Worth surfacing, not
    hiding: fewer applicants ever see these."""
    offs = j.get("offices") or []
    if not offs:
        return None
    l = j["location"].lower()
    blob = " ".join(offs).lower()
    for city in ("toronto", "vancouver", "montreal", "ottawa", "calgary",
                 "waterloo", "mississauga", "edmonton", "halifax"):
        if city in l and city not in blob:
            return f"location says {city.title()} but office tag is "\
                   f"{'/'.join(offs)} - confirm on the job page"
    return None


# ---------------------------------------- pass 2: hydrate only the finalists --
# Pulling full descriptions for ~12,000 jobs would move ~80MB, so we pull them
# only for the few hundred that already passed the title and location filters.
# The per-platform work is in adapters/<platform>.hydrate(); this dispatches to
# it and re-derives whatever the adapter changed.


def hydrate(j):
    kind = j.get("hyd")
    fn = adapters.HYDRATORS.get(kind[0]) if kind else None
    if not fn:
        return j
    before = j.get("location") or ""
    try:
        fn(j, kind)
    except Exception:
        pass
    # An adapter may correct the location while hydrating: Workday's
    # "3 Locations" placeholder resolves to real cities only on the posting
    # detail. Pass 1 derived bucket, provinces and arrangement from the
    # placeholder, so re-derive them from the corrected text. Doing it here
    # rather than in the adapter is what keeps adapters out of the classifier.
    # Same reasoning as the location below, for the date. Several adapters
    # cannot give a posting date in pass 1 and fill it from the detail page -
    # breezy and the schema.org hydrators read `datePosted` there - so `age`
    # was None through the pass-1 filter and stayed None afterwards. Re-derive
    # it, and main() re-applies --max-age to the corrected value.
    if j.get("age") is None and j.get("posted"):
        j["age"] = days_ago(j["posted"])
    now = j.get("location") or ""
    if now != before:
        bucket, keep = classify_location(now)
        j["bucket"], j["geo_keep"] = bucket, keep
        j["provinces"] = sorted(provinces_of(now))
        j["arrangement"] = arrangement(now, j.get("blob", ""))
    return j


# --------------------------------------------------------- shared file lock --
# cron appends to the tracker while the web app edits statuses in it. Both take
# this lock and write atomically, or one silently clobbers the other.

TRACKER_LOCK = os.path.join(HERE, ".applications.lock")


class FileLock:
    def __init__(self, path=TRACKER_LOCK, timeout=15):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        self.fh = open(self.path, "w")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise TimeoutError(f"could not lock {self.path}")
                time.sleep(0.15)

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def db_write(conn, fn, tries=4, budget=75.0, label="write"):
    """Run a write transaction against seen.sqlite3, retrying a lock.

    `busy_timeout=30000` already makes SQLite wait 30 seconds for the write
    lock. What it does not do is survive that wait expiring: the
    OperationalError propagates and takes the entire run with it, including
    every network fetch it had already done - 1,126 board requests for a full
    sweep. On 2026-10-27 a long-running inbox.py backfill held the lock across
    175 IMAP round-trips and killed four fast-lane runs, one daily sweep and
    one verify_tracked run exactly that way, all of them after their work was
    finished.

    SQLite in WAL mode allows exactly one writer, so contention is a normal
    condition here, not an error: five processes share this file (both poller
    lanes, verify_tracked, board_health, inbox.py) and the only coordination
    between them is cron's clock. A retry is the difference between "another
    writer was busy for a moment" and "this sweep is lost".

    Returns True on success, False when every attempt was blocked - the caller
    decides whether that is fatal. Anything other than a lock is re-raised
    immediately, because a real error should not be retried four times.

    `budget` caps the TOTAL time spent waiting, which matters because each
    attempt can itself block for busy_timeout (30s). Four unbounded attempts
    would be ~129s, and the fast lane has a 5-minute cadence and already takes
    ~148s, so an unlucky run could overrun into the next one and become the
    contention it was waiting out. 75s keeps the worst case near 220s. A
    verified 40s lock is survived comfortably inside that.
    """
    deadline = time.time() + budget
    for i in range(tries):
        try:
            fn()
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            try:
                conn.rollback()
            except Exception:
                pass
            if i == tries - 1 or time.time() >= deadline:
                print(f">> WARNING: {label} gave up after {i + 1} attempt(s) "
                      f"and {budget:.0f}s - database stayed locked ({e})",
                      file=sys.stderr)
                return False
            time.sleep(1.5 * (i + 1))
    return False


def atomic_write(path, text):
    """Write via temp file + rename so a reader never sees a half-written file."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tracker-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ tracker --

TRACKER = os.path.abspath(os.path.join(HERE, "..", "applications.md"))

TRACKER_HEADER = """# Application Tracker

Statuses: `toapply` -> `applied` -> `interviewing` -> `rejected`  (set them in the web app or here)

| # | Company | Role | Location | Posted | Updated | Score | Applied | Status | Next action |
|---|---------|------|----------|--------|---------|-------|---------|--------|-------------|

---
"""

TRACKER_FOOTER = """
## Outreach template

> Hi - saw the [ROLE] req go up on your careers page and applied. Quick context:
> I spent 6 years doing L2 escalation and RCA on production outages, and the last
> year running Terraform/Kubernetes/GitHub Actions on AWS with a Grafana Loki
> stack that cut our MTTR ~45%. Happy to answer anything ahead of screening.

Send to the **hiring manager**, not the recruiter. Search
`<company> "support manager" OR "director of support" OR "engineering manager"`
filtered to Canada or Remote.
"""

FAMILY_WHY = {
    "vendor": "Support/solutions role at a DevOps tool vendor - the 6-yr-support "
              "+ 1-yr-DevOps profile lands here better than anywhere else, and "
              "the internal path to SRE is well-worn.",
    "devops": "Core DevOps/SRE target.",
    "adjacent": "Infra-adjacent role that routinely hires from a support background.",
}


# ATS tokens are lowercase slugs. Fix the ones title-casing gets wrong.
DISPLAY = {
    "gitlab": "GitLab", "jfrog": "JFrog", "grafanalabs": "Grafana Labs",
    "pagerduty": "PagerDuty", "circleci": "CircleCI", "stackadapt": "StackAdapt",
    "newrelic": "New Relic", "launchdarkly": "LaunchDarkly", "beyondtrust": "BeyondTrust",
    "clickhouse": "ClickHouse", "cockroachlabs": "Cockroach Labs", "mongodb": "MongoDB",
    "planetscale": "PlanetScale", "sumologic": "Sumo Logic", "goteleport": "Teleport",
    "jumpcloud": "JumpCloud", "wizinc": "Wiz", "ngrokinc": "ngrok", "runzero": "runZero",
    "octopusdeploy": "Octopus Deploy", "orcasecurity": "Orca Security",
    "endorlabs": "Endor Labs", "keepersecurity": "Keeper Security",
    "catonetworks": "Cato Networks", "coreweave": "CoreWeave", "lastpass": "LastPass",
    "purestorage": "Pure Storage", "sonarsource": "SonarSource", "yugabyte": "YugabyteDB",
    "bluecatnetworks": "BlueCat Networks", "magnetforensics": "Magnet Forensics",
    "pointclickcare": "PointClickCare", "eqbank": "EQ Bank", "waveapps": "Wave",
    "lightspeedhq": "Lightspeed", "1password": "1Password", "d2l": "D2L",
    "waabi": "Waabi", "cloudzero": "CloudZero", "buildops": "BuildOps",
    "gocardless": "GoCardless", "deepgenomics": "Deep Genomics", "airtable": "Airtable",
}


def pretty_company(tok):
    t = (tok or "").strip()
    if " " in t:          # Workday entries already carry a real name
        return t
    return DISPLAY.get(t.lower(), t[:1].upper() + t[1:])


def posted_date(j):
    """Best-effort ISO date. Workday only gives relative ages."""
    p = str(j.get("posted") or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", p)
    if m:
        return m.group(1)
    if j.get("age") is not None:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(days=j["age"])).date().isoformat()
    return "?"


# --------------------------------------------------- tracker duplicates ---
# The URL guard inside append_to_tracker catches the same posting arriving
# twice under the same link. It cannot catch one job arriving down two
# different channels: the Deloitte Azure req came in from a LinkedIn alert as
# row 387, located "Canada", scoring 14, and again from the SuccessFactors
# feed as row 518, located "Toronto, ON, CA", scoring 36. Two rows, one job.
#
# What this must NOT do is collapse genuinely separate requisitions, which
# look identical from outside. TMX opened R-6460, R-6461 and R-6462 in one
# day, all titled "DevOps Analyst", two of them at the same Montreal address.
# Scotiabank's two "Cloud Engineer" rows sit two weeks and two postal codes
# apart. Those are five real openings, not two duplicates.
#
# The signal that separates the cases is the SOURCE. One board listing a title
# twice is advertising two reqs; two different channels carrying it once each
# are syndicating one. So a duplicate requires a DIFFERENT source, and a
# same-source repeat is always let through. Measured against every collision
# in the tracker, that rule suppresses the Deloitte pair and nothing else.
#
# The asymmetry runs opposite to the alert dedupe. A missed duplicate costs
# one row dismissed with a click; a wrong match silently costs a job you never
# see. So this keeps the row whenever it is unsure, and every suppression is
# printed with the row it matched rather than being swallowed.

TRACK_VAGUE_LOC = frozenset((
    "", "canada", "ca", "remote", "remote canada", "canada remote",
    "anywhere", "various", "multiple locations",
))

TRACK_ROW_RE = re.compile(
    r"(?m)^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|"
    r"[^|]*\|([^|]*)\|")
TRACK_BLOCK_RE = re.compile(r"(?ms)^## (\d+)\. .*?(?=^## \d+\.|\Z)")
TRACK_CLOSED = ("rejected", "declined", "closed", "withdrawn")


def dupe_norm(s):
    """Fold a company, title or location for duplicate comparison."""
    s = re.sub(r"\(.*?\)", " ", (s or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def loc_compatible(a, b):
    """Could these two location strings describe the same posting?

    True when they fold equal, when one is too vague to contradict the other
    ("Canada" against "Toronto, ON, CA"), or when one refines the other
    ("Toronto, ON, CA" against "Toronto, ON, CA, M5H 0A9"). Two different
    postal codes are NOT compatible, which is what keeps Scotiabank's two
    Cloud Engineer reqs apart.
    """
    na, nb = dupe_norm(a), dupe_norm(b)
    if na == nb or na in TRACK_VAGUE_LOC or nb in TRACK_VAGUE_LOC:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return ta <= tb or tb <= ta


def parse_tracker_rows(body):
    """Existing tracker rows as dicts.

    The table carries company, title and status; the detail block carries the
    full location, the source and the URL. Both halves are needed: the table's
    location column is truncated at 45 characters, and a truncated location
    cannot be compared.
    """
    meta = {}
    for m in TRACK_BLOCK_RE.finditer(body):
        blk = m.group(0)
        u = re.search(r"\*\*Apply:\*\*\s*(\S+)", blk)
        lo = re.search(r"\*\*Location:\*\*\s*(.*?)\s*-\s*bucket", blk)
        s = re.search(r"\*\*ATS:\*\*\s*(\S+)", blk)
        meta[m.group(1)] = (u.group(1) if u else "",
                            lo.group(1) if lo else "",
                            s.group(1) if s else "")
    rows = []
    for num, co, ti, status in TRACK_ROW_RE.findall(body):
        url, loc, src = meta.get(num, ("", "", ""))
        rows.append({"num": int(num), "company": co.strip(), "title": ti.strip(),
                     "status": status.strip().strip("` "), "location": loc,
                     "source": src, "url": url})
    return rows


def tracker_row_active(row, dismissed=(), closed=()):
    """Is this row still something you would act on?

    A rejected row, a dismissed row, or one whose posting verify_tracked has
    found gone, releases its claim: the same title reappearing later is a new
    opening, not a duplicate of a dead one.
    """
    if (row.get("status") or "").lower() in TRACK_CLOSED:
        return False
    u = row.get("url") or ""
    return not (u and (u in dismissed or u in closed))


def find_tracker_dupe(cand, rows):
    """The number of the active row `cand` duplicates, or None."""
    c_co, c_ti = dupe_norm(cand.get("company")), dupe_norm(cand.get("title"))
    if not c_co or not c_ti:
        return None                       # too little to match on; keep it
    c_src = dupe_norm(cand.get("source"))
    for r in rows:
        if dupe_norm(r.get("source")) == c_src:
            continue                      # same board twice = two reqs
        if dupe_norm(r.get("company")) != c_co:
            continue
        if dupe_norm(r.get("title")) != c_ti:
            continue
        if loc_compatible(r.get("location"), cand.get("location")):
            return r["num"]
    return None


def append_to_tracker(jobs, path, min_score):
    """Append new high-scorers. Never rewrites existing rows or your notes."""
    if not os.path.exists(path):
        body = TRACKER_HEADER + TRACKER_FOOTER
    else:
        body = open(path).read()

    seen_urls = set(re.findall(r"https?://[^\s)\]]+", body))
    dismissed_urls, closed_urls = set(), set()
    try:
        conn = db()
        dismissed_urls = {r[0] for r in conn.execute("SELECT url FROM dismissed")}
        closed_urls = {r[0] for r in conn.execute(
            "SELECT url FROM tracked_live WHERE state IN ('gone', 'stale')")}
        conn.close()
    except Exception:
        pass
    seen_urls |= dismissed_urls
    nums = [int(n) for n in re.findall(r"(?m)^\|\s*(\d+)\s*\|", body)]
    nxt = max(nums) + 1 if nums else 1

    todo = [j for j in jobs
            if j["score"] >= min_score and j["url"].rstrip(".,)") not in seen_urls]
    if not todo:
        return 0, nxt, []

    # Highest score first, so when one job arrives down two channels in the
    # same batch the better-scored row is the one that survives.
    todo.sort(key=lambda j: -j["score"])

    active = [r for r in parse_tracker_rows(body)
              if tracker_row_active(r, dismissed_urls, closed_urls)]
    keep, dropped = [], []
    for j in todo:
        cand = {"company": pretty_company(j["company"]), "title": j["title"],
                "location": j["location"], "source": j.get("source", "")}
        hit = find_tracker_dupe(cand, active)
        if hit is not None:
            dropped.append((j, hit))
            continue
        # Added to `active` so the batch also dedupes against itself.
        active.append({**cand, "num": nxt + len(keep), "url": j["url"],
                       "status": "toapply"})
        keep.append(j)
    todo = keep
    if not todo:
        return 0, nxt, dropped
    rows, blocks = [], []
    today = datetime.now(timezone.utc).date().isoformat()

    for j in todo:
        company = pretty_company(j["company"]).replace("|", "/")
        title = j["title"].strip().replace("|", "/")
        loc = j["location"].strip().replace("|", "/")
        loc_short = loc if len(loc) <= 45 else loc[:42] + "..."
        pdate = posted_date(j)
        nxt_action = ("Apply, then find the hiring manager on LinkedIn"
                      if j["score"] >= 25 else "Read the JD, then decide")
        udate = str(j.get("updated") or "")[:10] or "—"
        rows.append(f"| {nxt} | {company} | {title} | {loc_short} | {pdate} | "
                    f"{udate} | {j['score']} | | `toapply` | {nxt_action} |")

        age = "unknown age" if j["age"] is None else (
            "posted today" if j["age"] == 0 else f"{j['age']}d old when found")
        blocks.append(f"""
## {nxt}. {company} - {title}

- **Apply:** {j['url']}
- **Location:** {loc} - bucket `{j['bucket']}`
- **Level:** {j['level']} · **Score:** {j['score']} · **ATS:** {j['source']}
- **Posted:** {pdate} ({age}) · **Last updated by recruiter:** {udate}
- **Found:** {today}
- **Resume keywords matched:** {', '.join(j['kw']) or '-'}

**Why this fits:** {FAMILY_WHY.get(j['family'], '')}

- [ ] Applied
- [ ] Hiring manager found on LinkedIn
- [ ] Outreach note sent
- Contact:
- Notes:

---
""")
        nxt += 1

    # Table rows go after the last existing table row; detail blocks go just
    # before the outreach template so the file stays readable.
    lines = body.split("\n")
    last_row = max((i for i, l in enumerate(lines)
                    if l.startswith("|") and l.rstrip().endswith("|")), default=None)
    if last_row is None:
        lines = (TRACKER_HEADER + TRACKER_FOOTER).split("\n")
        last_row = max(i for i, l in enumerate(lines) if l.startswith("|"))
    lines[last_row + 1:last_row + 1] = rows
    body = "\n".join(lines)

    detail = "\n".join(blocks)
    anchor = body.find("## Outreach template")
    body = (body[:anchor] + detail.lstrip("\n") + "\n" + body[anchor:]
            if anchor != -1 else body.rstrip() + "\n" + detail)

    atomic_write(path, body)
    return len(todo), nxt, dropped


# ------------------------------------------------------------------- notify --
# Push new matches to a phone via ntfy.sh. The topic is effectively a password:
# anyone who knows it can read your job alerts, so it is generated random and
# kept in a gitignored file.

NTFY_FILE = os.path.join(HERE, ".ntfy_topic")


def ntfy_topic():
    t = os.environ.get("RADAR_NTFY_TOPIC")
    if t:
        return t.strip()
    if os.path.exists(NTFY_FILE):
        return open(NTFY_FILE).read().strip()
    return ""


def _ns():
    """notify_send, imported lazily.

    Lazy because job_radar is imported by discover.py, board_health.py,
    verify_tracked.py and inbox.py, and none of them push - making every one
    of them pay for an import they do not use would be gratuitous. It is also
    one less module that can break `import job_radar` if it is ever edited
    badly, which matters when five other tools depend on that import working.
    """
    if _ns.mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "notify_send", os.path.join(HERE, "notify_send.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _ns.mod = m
    return _ns.mod


_ns.mod = None


def notify(jobs, topic, limit=6):
    """One push per strong match, then a single roll-up for the rest."""
    if not topic or not jobs:
        return 0
    jobs = sorted(jobs, key=lambda j: -j["score"])
    sent, failed = 0, []
    for j in jobs[:limit]:
        prio = "urgent" if j["score"] >= 35 else "high" if j["score"] >= 25 else "default"
        tag = {"vendor": "handshake", "devops": "rocket"}.get(j["family"], "wrench")
        age = "" if j["age"] is None else (
            " · posted today" if j["age"] == 0 else f" · {j['age']}d old")
        body = (f"{j['company']} · {j['location'][:60]}{age}\n"
                f"matched: {', '.join(j['kw'][:6]) or '—'}")
        # Publish as JSON, not via HTTP headers. urllib encodes headers as
        # latin-1, so a single en-dash in a title killed the push outright:
        #   UnicodeEncodeError: 'latin-1' codec can't encode character '\u2013'
        #   - Software Engineer - MLOPS
        # Canadian titles are full of en-dashes and French accents
        # ("Specialiste DevOps"), so this was not an edge case - it was a
        # standing hole that dropped matching jobs with no push at all. A JSON
        # body is UTF-8 end to end and has no such limit.
        payload = {"title": f"[{j['score']}] {j['title'][:80]}",
                   "message": body,
                   "priority": {"urgent": 5, "high": 4, "default": 3}[prio],
                   "tags": [tag], "click": j["url"]}
        # Through notify_send, which retries and then QUEUES anything it still
        # cannot deliver. That matters most here of all the push sites: the
        # posting has already been claimed in `seen` by the time this runs, so
        # a dropped push is a job you never hear about again. Queued, it
        # arrives late instead of never.
        try:
            if _ns().send(payload, tp=topic, verbose=False):
                sent += 1
            else:
                raise RuntimeError("queued for later - ntfy unreachable")
        except Exception as e:
            # Never swallow this. A rate limit, a network drop or a bad topic
            # used to look exactly like "nothing matched" - the alerts simply
            # stopped and nothing said so. cron captures stdout, so a failure
            # here lands in the log where it can be seen.
            failed.append(f"{type(e).__name__}: {e} — {j['title'][:40]}")
    rest = jobs[limit:]
    if rest:
        body = "\n".join(f"[{j['score']}] {j['title'][:52]} — {j['company']}"
                          for j in rest[:14])
        try:
            if _ns().send({"title": f"+{len(rest)} more matches",
                           "message": body, "priority": 2, "tags": ["mag"]},
                          tp=topic, verbose=False):
                sent += 1
            else:
                raise RuntimeError("queued for later - ntfy unreachable")
        except Exception as e:
            failed.append(f"{type(e).__name__}: {e} — roll-up")
    if failed:
        print(f"  !! {len(failed)} notification(s) FAILED to send:")
        for f in failed[:5]:
            print(f"     {f}")
        if sent == 0:
            print("     every push failed - check ntfy.sh reachability and "
                  f"the topic in {NTFY_FILE}")
    return sent


# -------------------------------------------------------------------- state --

def db():
    # The fast lane and the full sweep can overlap, and the web app reads this
    # too. Wait for the writer instead of throwing "database is locked".
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("CREATE TABLE IF NOT EXISTS seen("
              "url TEXT PRIMARY KEY, title TEXT, company TEXT, first_seen TEXT)")
    # Every URL returned by any board this run - not just the ones that matched.
    # A tracked job whose URL stops appearing here has been taken down, which is
    # how the web app knows to stop showing it.
    c.execute("CREATE TABLE IF NOT EXISTS live("
              "url TEXT PRIMARY KEY, last_seen TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS sweeps(ts TEXT)")
    # Jobs the user deleted from the tracker. Without this the next sweep just
    # re-adds them, because dedupe only looks at URLs currently in the file.
    c.execute("CREATE TABLE IF NOT EXISTS dismissed("
              "url TEXT PRIMARY KEY, at TEXT)")
    return c


# --------------------------------------------------------------------- main --

FAMILY_LABEL = {"devops": "DevOps/SRE", "adjacent": "Infra-adjacent",
                "vendor": "VENDOR SUPPORT"}

# Lowering --min-score should add depth, not noise. Bands keep the top of the
# list obvious no matter how wide the net is cast.
BANDS = [(25, "APPLY TODAY"), (15, "THIS WEEK"), (8, "WORTH A LOOK"),
         (-999, "LONG SHOT")]


def band_of(score):
    for floor, label in BANDS:
        if score >= floor:
            return label
    return "LONG SHOT"


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    ap.add_argument("--all", action="store_true", help="show all matches, not just new")
    ap.add_argument("--seed", action="store_true", help="record state, print nothing")
    ap.add_argument("--no-claim", action="store_true",
                    help="do not record postings in `seen` (for reporting runs)")
    ap.add_argument("--min-score", type=int, default=8)
    ap.add_argument("--max-age", type=int, default=30, help="skip reqs older than N days")
    ap.add_argument("--level", default="entry,mid,senior",
                    help="comma list of entry,mid,senior,exec,intern")
    ap.add_argument("--family", default="devops,adjacent,vendor",
                    help="comma list of devops,adjacent,vendor")
    ap.add_argument("--bucket", default="CA,CA-OPTION,NA-REMOTE,MULTI,GLOBAL",
                    help="comma list of CA,CA-OPTION,NA-REMOTE,MULTI,GLOBAL,US-ONLY,OTHER")
    ap.add_argument("--province", default="",
                    help="comma list of province codes e.g. AB,BC. Postings that "
                         "name no province (remote) are always kept")
    ap.add_argument("--md", help="also write a markdown digest here")
    ap.add_argument("--notify", action="store_true",
                    help="push new matches to your phone via ntfy")
    ap.add_argument("--notify-min-score", type=int, default=15)
    ap.add_argument("--track", action="store_true",
                    help="append shown postings to the application tracker")
    ap.add_argument("--track-file", default=TRACKER)
    ap.add_argument("--track-min-score", type=int, default=15,
                    help="only track postings at or above this score (default 15, matching --notify-min-score so nothing buzzes your phone without landing in the tracker)")
    ap.add_argument("--targets", default=TARGETS)
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()

    levels = {x.strip() for x in a.level.split(",") if x.strip()}
    fams = {x.strip() for x in a.family.split(",") if x.strip()}
    buckets = {x.strip() for x in a.bucket.split(",") if x.strip()}
    provs = {x.strip().upper() for x in a.province.split(",") if x.strip()}

    tg = json.load(open(a.targets))
    VENDORS.update(x.lower() for x in tg.get("devops_vendors", []))

    # Platforms with no "all jobs" view are swept once per search term. Which
    # terms, and whether a platform needs the fan-out at all, is the adapter's
    # own business: adapters.expand() asks it.
    tasks = []
    for src, fn in ADAPTERS.items():
        for tok in tg.get(src, []):
            for task in adapters.expand(src, tok):
                tasks.append((src, task, fn))

    jobs, errors = [], []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fn, tok): (src, tok) for src, tok, fn in tasks}
        for f in cf.as_completed(futs):
            src, tok = futs[f]
            label = tok["name"] if isinstance(tok, dict) else tok
            try:
                jobs.extend(f.result())
            except Exception as e:
                errors.append(f"{src}/{label}: {type(e).__name__} {e}")

    # The same posting arrives more than once: a Workday req matches several
    # keyword queries, and some boards list one req under several countries
    # sharing a single URL. Keeping the first occurrence silently dropped the
    # Canada-eligible variant - an MLabs SRE role was listed for UK, Poland,
    # Germany, Netherlands and Canada under one shortlink, and the UK entry won.
    # Merge the locations instead so the classifier sees every one of them.
    _byurl = {}
    for j in jobs:
        prev = _byurl.get(j["url"])
        if prev is None:
            _byurl[j["url"]] = j
            continue
        locs = [p.strip() for p in prev["location"].split(" / ") if p.strip()]
        if j["location"] and j["location"] not in locs:
            locs.append(j["location"])
            prev["location"] = " / ".join(locs)
        if len(j.get("blob", "")) > len(prev.get("blob", "")):
            prev["blob"] = j["blob"]
    jobs = list(_byurl.values())

    # Pass 1: cheap filters on title + location only.
    shortlist = []
    for j in jobs:
        bucket, keep = classify_location(j["location"])
        if not keep or bucket not in buckets:
            continue
        tier, fam = title_match(j["title"], j["company"])
        if tier == 0 or fam not in fams:
            continue
        if fam == "vendor" and bucket in NO_VENDOR_BUCKETS:
            continue
        if level_of(j["title"]) not in levels:
            continue
        if j["age"] is not None and j["age"] > a.max_age:
            continue
        j.update(bucket=bucket, tier=tier, family=fam,
                 provinces=sorted(provinces_of(j["location"])),
                 arrangement=arrangement(j["location"], j.get("blob", "")))
        shortlist.append(j)

    # Pass 2: fetch descriptions for the shortlist, then score properly.
    with cf.ThreadPoolExecutor(a.workers) as ex:
        shortlist = list(ex.map(hydrate, shortlist))

    # Rows that arrived as "N Locations" only have a real location after hydrate,
    # so the geography filter is re-applied to the corrected value. Everything
    # else keeps the verdict pass 1 already reached (geo_keep defaults True).
    before_geo = len(shortlist)
    shortlist = [j for j in shortlist
                 if j.get("geo_keep", True) and j["bucket"] in buckets
                 and not (j["family"] == "vendor"
                          and j["bucket"] in NO_VENDOR_BUCKETS)]
    dropped_geo = before_geo - len(shortlist)

    # --max-age again, now that hydrate may have supplied the date.
    #
    # Pass 1 reads `if j["age"] is not None and ...`, so a board that omits the
    # date sails through the cap entirely - "unknown" is not "too old", and it
    # must not be, or every undated posting would be discarded. The cost showed
    # up in the tracker on 2026-10-28: 29 rows filed in three days whose
    # postings turned out to be 22 to 274 days old, Accenture at 92-93 days and
    # Nasdaq at 231, all of them through this hole. The date was available by
    # then - hydrate had read it - and nothing looked at it again.
    before_age = len(shortlist)
    shortlist = [j for j in shortlist
                 if j["age"] is None or j["age"] <= a.max_age]
    dropped_age = before_age - len(shortlist)

    if provs:
        # Keep the wanted provinces, plus anything naming no province at all -
        # those are remote or unclear, never "definitely somewhere else".
        shortlist = [j for j in shortlist
                     if not j["provinces"] or (set(j["provinces"]) & provs)]

    hits = []
    for j in shortlist:
        s, kw, lvl = score(j["title"], j["company"], j["tier"], j["family"], j["blob"])
        if s < a.min_score:
            continue
        j.update(score=s, kw=kw, level=lvl)
        hits.append(j)

    conn = db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Record liveness for everything scanned, and stamp the sweep. Only a full
    # sweep (no --targets override) is authoritative about what is still up.
    if a.targets == TARGETS:
        # Its own transaction, separate from the seen claims below. These two
        # writes have different consequences: losing the liveness stamp costs
        # one cycle of prune_live accuracy, while losing the seen claims means
        # either no notification or a duplicate one. Keeping them apart means a
        # blocked bulk write cannot take the claims down with it.
        def _live():
            conn.executemany(
                "INSERT INTO live(url,last_seen) VALUES(?,?) "
                "ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen",
                [(j["url"], now) for j in jobs])
            conn.execute("INSERT INTO sweeps(ts) VALUES(?)", (now,))
            conn.execute("DELETE FROM sweeps WHERE ts NOT IN "
                         "(SELECT ts FROM sweeps ORDER BY ts DESC LIMIT 50)")
        db_write(conn, _live, label="live/sweeps stamp")
    # Claiming a posting must be atomic. The fast lane and the full sweep can
    # run in the same minute; when both read `seen` before either wrote, they
    # each decided the same job was new and both pushed a notification. Insert
    # first and let the row count decide - only the writer that actually
    # inserted owns it.
    fresh = []

    # A reporting run must not touch notification state. `--all` claims every
    # match it shows, which is right for a normal sweep and wrong for a sweep
    # whose only purpose is to produce a digest - the weekly fast.json refresh
    # is exactly that. Without this flag the refresh would silently consume
    # whatever was new that morning (the trap the README warns about: "--all
    # without --track consumes postings into seen, so cron will never notify
    # you about them again"), and adding --notify instead is worse, because
    # notify() reads `show`, which under --all is every match - 181 of 213 on
    # a measured sweep, all pushed to the phone at once.
    if a.no_claim:
        print(">> --no-claim: not recording anything in `seen`", file=sys.stderr)
        a.notify = a.track = False

    def _claim():
        fresh.clear()
        for j in hits:
            cur = conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?)",
                               (j["url"], j["title"], j["company"], now))
            if cur.rowcount:
                fresh.append(j)
    # fresh is rebuilt from scratch on each attempt: a retry re-runs the
    # INSERTs after a rollback, so appending without clearing would count a
    # posting twice and notify twice.
    if a.no_claim:
        fresh = []
    elif not db_write(conn, _claim, label="seen claim"):
        # The fetching is already done and the matches are real, so the digest
        # is still worth printing - but nothing was claimed, so suppress
        # notify/track rather than push alerts this run cannot dedupe. The next
        # sweep claims them properly.
        print(">> database stayed locked: showing results but NOT notifying "
              "or tracking this run (the next sweep will)", file=sys.stderr)
        fresh = []
        a.notify = a.track = False

    if a.seed:
        print(f"Seeded {len(hits)} postings into {DB}.\n"
              f"From now on, a plain `python3 job_radar.py` shows only NEW ones.")
        return

    show = sorted(hits if a.all else fresh,
                  key=lambda j: (-j["score"], j["age"] if j["age"] is not None else 99))

    tally = {}
    for j in show:
        tally[band_of(j["score"])] = tally.get(band_of(j["score"]), 0) + 1
    headline = " · ".join(f"**{tally[b]}** {b.lower()}" for _, b in BANDS
                          if b in tally) or "nothing"
    L = [f"# Job Radar — {'ALL MATCHES' if a.all else 'NEW SINCE LAST RUN'} — {now[:16]}Z",
         f"### {headline}",
         f"_{len(show)} shown · {len(hits)} matched · {len(jobs)} scanned · "
         f"{len(tasks)} boards · levels={a.level} · families={a.family}"
         # Worth showing: these are rows that looked like candidates until their
         # real cities were resolved, so a sudden jump means a board started
         # collapsing its locations rather than that the search got narrower.
         + (f" · {dropped_geo} dropped once \"N Locations\" resolved"
            if dropped_geo else "")
         + (f" · {dropped_age} dropped once the posting date arrived"
            if dropped_age else "") + "_", ""]

    by_fam = {}
    for j in show:
        by_fam.setdefault(j["family"], []).append(j)
    for fam in ("vendor", "devops", "adjacent"):
        group = by_fam.get(fam)
        if not group:
            continue
        counts = {}
        for j in group:
            counts[band_of(j["score"])] = counts.get(band_of(j["score"]), 0) + 1
        tally = " · ".join(f"{n} {b.lower()}" for _, b in BANDS
                           if (n := counts.get(b)))
        L += ["---", f"## {FAMILY_LABEL[fam]}  ({len(group)})", f"_{tally}_", ""]
        current_band = None
        for j in group:
            b = band_of(j["score"])
            if b != current_band:
                current_band = b
                L += [f"**— {b} —**", ""]
            if j["age"] is None:
                # SuccessFactors does not expose a real posting date; show the
                # SEO reference date for what it is instead of inventing an age.
                age = (f"age unknown (ref {j['updated']})" if j.get("updated")
                       else "age unknown")
            else:
                age = "**TODAY**" if j["age"] == 0 else f"{j['age']}d"
            loc = j["location"] if len(j["location"]) <= 80 else j["location"][:77] + "..."
            tags = [j["bucket"], j["level"]]
            if j.get("provinces"):
                tags.append("/".join(j["provinces"]))
            if j.get("arrangement"):
                tags.append(j["arrangement"])
            tagstr = " · ".join("`%s`" % t for t in tags)
            L += [f"### [{j['score']}] {j['title']}",
                  f"- **{j['company']}** · {loc}",
                  f"- {tagstr} · posted {age}",
                  f"- {j['url']}",
                  f"- matched: {', '.join(j['kw']) or '—'}"]
            for warn in (office_mismatch(j), offshore_marker(j)):
                if warn:
                    L.append(f"- ⚠️ {warn}")
            L.append("")

    if not show:
        L.append("_Nothing new. Try `--all`, or lower `--min-score`._")
    if errors:
        L += ["---", "**Dead boards — run `discover.py --verify --add`:**"] + \
             [f"- {e}" for e in errors]
    if WD_TRUNCATED:
        L += ["---", "**Workday queries truncated — postings were not read. "
                     "Give the target a higher `max_pages` if the term is "
                     "selective there:**"] + \
             [f"- {w}" for w in sorted(WD_TRUNCATED)]

    out = "\n".join(L)
    print(out)
    if a.md:
        open(a.md, "w").write(out + "\n")

    if a.notify:
        topic = ntfy_topic()
        if not topic:
            print("\n>> --notify needs a topic: write one to radar/.ntfy_topic "
                  "or set RADAR_NTFY_TOPIC")
        else:
            worth = [j for j in show if j["score"] >= a.notify_min_score]
            n = notify(worth, topic)
            if n:
                print(f"\n>> pushed {n} notification(s) to ntfy topic {topic[:6]}…")
            elif worth:
                print(f"\n>> WARNING: {len(worth)} job(s) matched but NO "
                      "notification was delivered")

    track_failed = False
    if a.track:
        # Guarded, and with a far longer timeout than the 15s default. Five
        # processes write applications.md - both poller lanes, inbox.py,
        # rescore.py and the web app - and an uncaught TimeoutError here killed
        # the sweep AFTER notify() had already pushed. That is the precise
        # shape of "jobs in my phone alerts that I cannot find in the web app":
        # the alert goes out, the process dies, the row is never written, and
        # the posting is already claimed in `seen` so no later sweep retries
        # it. The lock is only ever held for a single read-modify-write, so 60s
        # is generous rather than slow.
        try:
            with FileLock(timeout=60):
                n, _, dup = append_to_tracker(show, a.track_file,
                                              a.track_min_score)
            for j, row in dup:
                print(f"   not tracked: {j['title']} at "
                      f"{pretty_company(j['company'])} - already row #{row} "
                      f"under a different source")
        except TimeoutError as e:
            track_failed = True
            n = 0
            print(f"\n>> WARNING: could not lock the tracker ({e}). "
                  f"{len([j for j in show if j['score'] >= a.track_min_score])} "
                  f"posting(s) were NOT written to applications.md"
                  + (" and notifications for them have already been sent."
                     if a.notify else "."), file=sys.stderr)
        rel = os.path.relpath(a.track_file, os.getcwd())
        if rel.count("..") > 1:
            rel = a.track_file
        if n:
            print(f"\n>> Tracked {n} new posting(s) (score >= {a.track_min_score}) "
                  f"in {rel}")
        else:
            print(f"\n>> Nothing new to track (score >= {a.track_min_score}); "
                  f"{rel} already covers what was shown.")
    # Non-zero so cronwrap.sh alerts. A sweep that notified but could not
    # record is a real incident, not a cosmetic warning, and returning 0 here
    # would make it invisible to every check downstream.
    return 1 if track_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
