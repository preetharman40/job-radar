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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as html_unescape
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "seen.sqlite3")
TARGETS = os.path.join(HERE, "targets.json")

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Content-Type": "application/json",
}

# Workday's GUID for Canada is the same value in every tenant, but the NAME of
# the facet that takes it is configured per tenant - TD calls it
# "locationCountry", CIBC calls it "Country", Sun Life "Location_Country".
# Hardcoding one of them returns HTTP 400 on the others, so we detect it.
WD_CANADA = "a30a87ed25634629aa6c3958aa2b91ea"
_WD_FACET_CACHE = {}

VENDORS = set()   # filled from targets.json at startup

# ------------------------------------------------------------------ titles ---
# (regex, tier, family). Tier drives the base score; family drives --family.

TITLE_RULES = [
    # --- family: devops -------------------------------------------------
    # Covers "dev ops" with a space and the whole devsecops family. Measured
    # on 205 distinct titles from a fortnight of real alert mail: 15
    # infra-shaped titles scored tier 0, and SIX were caught by widening this
    # one rule - "DevSecOps Engineer", "Senior DevSecOps Engineer", "Cloud
    # Security DevSecOps Engineer", "Azure DevSecOps Architect", "Dev Ops I"
    # and "Dev Ops II, Public Cloud". A tier-0 title never reaches scoring at
    # all, so these were invisible rather than ranked low.
    #
    # `ops` keeps its word boundary so "operations" cannot match, and bare
    # "secops" is deliberately excluded - that is a SOC analyst title, and
    # analyst roles are out by request. Costs 21 extra postings across a
    # 56,524-posting snapshot, nearly all of them genuine DevSecOps reqs.
    (r"\b(dev[\s-]?sec[\s-]?ops|dev[\s-]?ops)\b",                 4, "devops"),
    (r"\b(sre|site\s+reliability)\b",                           4, "devops"),
    (r"\bplatform\s+engineer",                                  4, "devops"),
    (r"\b(cloud|infrastructure|infra)\s+"
     r"(engineer|developer|specialist|analyst|administrator)",  4, "devops"),
    # Adjacency with at most one word between, which is what "Kubernetes
    # (K8S) / HPC Engineer" needs - four characters of punctuation and one
    # token sit between the platform and the role noun. Bounded deliberately:
    # the unbounded "kubernetes anywhere AND engineer anywhere" form admitted
    # 19 postings, mostly "Senior Software Engineer - Python/Golang -
    # Kubernetes" coding roles, while this one admits exactly 1 extra posting
    # in 56,524 and still rescues the title it was written for.
    (r"\b(kubernetes|k8s)\b[\s/()\[\],.-]*"
     r"(?:[a-z0-9]+[\s/()\[\],.-]+)?engineer",                  4, "devops"),
    # "Platform Administration Lead", "Data Platform Operations Engineer".
    # The power-platform lookbehind is load-bearing: "Power BI & Power
    # Platform Administrator" is an M365 reporting role, and it arrived in the
    # same fortnight of mail as the titles above. Requiring the role noun
    # (operations ENGINEER, or administrator/administration) rather than a
    # bare "platform operations" is what keeps out "Sr Program Manager, GTM
    # Platform Operations" and the support-engineer variants - the loose form
    # admitted 32 postings of which 22 were program-manager or support roles.
    # "Manager, AMS/SAP Platform Administration and Support" still matches but
    # is harmless: level_of reads it as exec, which is -10.
    (r"(?<!power )\bplatform\s+"
     r"(?:operations?\s+engineer|administrat(?:or|ion))\b",     4, "devops"),
    (r"\b(reliability|observability)\s+engineer",               4, "devops"),
    (r"\bmlops\b",                                              3, "devops"),
    # A gap is allowed between "cloud" and "operations" because vendors put
    # their product name there: "VMware Cloud Foundation (VCF) Operations
    # Engineer". Admits 0 extra postings in the 56,524-posting snapshot, so
    # the widening is free - the title it rescues came through alert mail from
    # a board the radar does not poll.
    (r"\bcloud\s+(operations|ops)\b"
     r"|\bcloud\b[a-z0-9\s()&./-]{0,26}\boperations?\s+engineer", 3, "devops"),

    # --- family: adjacent (infra work that hires from support) ----------
    (r"\b(build|release|automation|tooling)\s+engineer",        3, "adjacent"),
    (r"\bdeveloper\s+(experience|productivity|platform)",       3, "adjacent"),
    (r"\b(linux|unix|systems?)\s+"
     r"(engineer|administrator|admin|analyst)",                 3, "adjacent"),
    (r"\b(endpoint|mdm|intune|workspace\s*one|"
     r"unified\s+endpoint|modern\s+workplace|device\s+management)", 4, "adjacent"),
    (r"\b(noc|network\s+operations)\b",                         2, "adjacent"),
    (r"\bit\s+(engineer|operations|infrastructure)",            2, "adjacent"),

    # --- family: vendor (only counts at a DevOps tool vendor) -----------
    (r"\b(technical|customer|enterprise)\s+support\s+engineer", 4, "vendor"),
    (r"\bsupport\s+engineer\b",                                 4, "vendor"),
    (r"\b(solutions?|sales|field)\s+engineer\b",                3, "vendor"),
    (r"\b(implementation|onboarding|deployment)\s+engineer",    3, "vendor"),
    (r"\b(technical\s+account\s+manager|professional\s+services)", 3, "vendor"),
    (r"\b(customer\s+success\s+engineer|escalation\s+engineer)", 4, "vendor"),
    (r"\bdeveloper\s+(advocate|relations)\b",                   2, "vendor"),
]

# Straight off the resume. Ranks, never filters.
#
# One SKILL, one score, many spellings. This was a flat {phrase: weight} dict
# until 2026-10-22, which carried three defects measured against the 177 rows
# then in the tracker:
#
#  1. ALIASES DOUBLE-COUNTED. "kubernetes" and "k8s" were separate entries, so
#     a JD reading "Kubernetes (K8s)" scored the one skill twice - and at two
#     different weights (3 and 2), so the total depended on which spellings the
#     recruiter happened to use. 37 rows double-counted infrastructure-as-code,
#     6 kubernetes, 2 ci/cd: 45 of 177 rows were inflated by 1-2 points.
#  2. NO WORD BOUNDARY. Matching was a plain `kw in blob`, so "aws" matched
#     "laws" and "draws", "helm" matched "overwhelming", "iac" matched
#     "maniac". Every form is now anchored on both sides.
#  3. DISPLAY SORTED ALPHABETICALLY, then truncated to 10 - so the shown list
#     was the first ten by name, not the ten that earned the score. No row was
#     actually hiding a weight-3 skill when this was fixed, but it was one new
#     keyword away from doing so.
#
# What is deliberately NOT here is as important as what is. A keyword earns a
# place by DISCRIMINATING, not by being relevant: "automation", "monitoring",
# "pipeline", "deployment", "scripting" and "cloud" appear in essentially every
# DevOps JD, so scoring them would raise every role by the same amount and rank
# nothing. They are in the resume and they are still omitted. Likewise absent
# are azure and gcp - real skills, but not HIS, and rewarding a JD for
# requiring them would promote the roles he is least able to land.
#
# (canonical name, weight, surface forms that mean it)
SKILL_GROUPS = (
    # --- the rare, highly diagnostic things: few JDs name these ----------
    ("intune",          4, ("intune", "microsoft intune", "endpoint manager")),
    ("workspace one",   4, ("workspace one", "workspace 1", "airwatch")),
    ("loki",            3, ("loki",)),
    ("grafana",         3, ("grafana",)),
    ("mdm",             3, ("mdm", "mobile device management",
                            "device management")),
    ("terraform",       3, ("terraform", "terragrunt")),
    ("github actions",  3, ("github actions", "gh actions",
                            "github workflow", "github workflows")),
    ("kubernetes",      3, ("kubernetes", "k8s", "kubectl", "eks", "aks",
                            "gke", "openshift", "rancher")),
    # --- core toolchain --------------------------------------------------
    ("docker",          2, ("docker", "dockerfile", "containerization",
                            "containerisation", "container image",
                            "oci image", "podman")),
    ("ci/cd",           2, ("ci/cd", "cicd", "ci cd", "continuous integration",
                            "continuous delivery", "continuous deployment")),
    ("jenkins",         2, ("jenkins", "jenkinsfile")),
    ("aws",             2, ("aws", "amazon web services", "ec2", "s3",
                            "cloudformation")),
    ("digitalocean",    2, ("digitalocean", "digital ocean")),
    ("linux",           2, ("linux", "ubuntu", "rhel",
                            "red hat enterprise", "centos", "debian",
                            "unix")),
    ("bash",            2, ("bash", "shell scripting", "shell script",
                            "sh scripting")),
    ("python",          2, ("python", "python3")),
    ("infrastructure as code", 2, ("infrastructure as code", "iac")),
    ("prometheus",      2, ("prometheus", "promql", "alertmanager")),
    ("helm",            2, ("helm", "helm chart", "helm charts")),
    ("observability",   2, ("observability", "telemetry",
                            "distributed tracing")),
    # --- operations vocabulary: how he actually spent six years ----------
    ("incident",        2, ("incident", "incident response",
                            "incident management", "major incident")),
    ("root cause",      2, ("root cause", "rca", "root cause analysis",
                            "postmortem", "post-mortem")),
    ("mttr",            2, ("mttr", "mean time to repair",
                            "mean time to resolution")),
    # "endpoint" alone matched "a REST endpoint", which is an API, not a
    # managed device. A word boundary cannot fix that - the word really is
    # there - so the form itself has to carry the meaning.
    ("endpoint management", 2, ("endpoint management", "endpoint security",
                                "endpoint protection", "device compliance")),
    ("servicenow",      2, ("servicenow", "service now")),
    ("escalation",      2, ("escalation", "escalations", "escalate")),
    ("troubleshooting", 2, ("troubleshooting", "troubleshoot")),
    # --- supporting, and genuinely in the resume -------------------------
    ("ansible",         1, ("ansible",)),
    ("argo cd",         1, ("argo cd", "argocd", "argo-cd")),
    ("microservices",   1, ("microservices", "micro-services",
                            "service mesh", "istio")),
    ("on-call",         1, ("on-call", "on call", "pagerduty", "pager duty")),
    ("jira",            1, ("jira", "confluence", "atlassian")),
    ("rest api",        1, ("rest api", "rest apis", "restful",
                            "rest endpoint", "rest endpoints")),
    ("fastapi",         1, ("fastapi",)),
    ("playwright",      1, ("playwright",)),
    ("pytest",          1, ("pytest",)),
    ("ms sql",          1, ("ms sql", "mssql", "sql server", "t-sql")),
    ("vmware",          1, ("vmware", "esxi", "vsphere")),

    # --- added 2026-10-22 from Harmanpreet_Singh_DevOps_Resume_Latest.docx --
    #
    # Weights here are MEASURED, not guessed. Each candidate was counted
    # against a corpus of 135 real job descriptions pulled from the fast lane
    # (mean 6,857 chars), and weighted by how much naming it DISCRIMINATES:
    #
    #     >=60% of JDs  ->  dropped, it ranks nothing
    #     35-60%        ->  weight 1
    #     12-35%        ->  weight 2
    #     <12%          ->  weight 3, rare and his
    #
    # The controls are the proof the method works: "cloud" appears in 65% of
    # JDs and "automation" in 63%, so both are omitted even though the resume
    # names them - scoring them would add the same points to nearly every role
    # and reorder nothing. "monitoring" (49%) and "pipeline" (43%) are out for
    # the same reason at lower cost.
    #
    # Rarity is then CAPPED BY PROFICIENCY. The resume separates what he runs
    # from what he lists under "Familiar with:" and "Learning", and a skill he
    # is merely familiar with must not outrank one he operates daily however
    # rare it is. ECS, GitOps and Kafka are 4-7% terms that would otherwise
    # score 3; they are held at 1. GCP and Go are omitted outright - "Learning
    # GCP" should not promote a GCP-heavy JD, and bare "go" is unmatchable.
    ("llm",             2, ("llm", "large language model", "genai",
                            "generative ai")),
    ("ai platform",     3, ("ml platform", "ai platform", "mlops", "llmops",
                            "real-time inference")),
    ("iam",             2, ("iam", "least privilege", "least-privilege")),
    ("alerting",        2, ("alerting",)),
    ("runbook",         2, ("runbook", "sop", "knowledge base")),
    ("disaster recovery", 2, ("disaster recovery", "business continuity")),
    ("slo",             3, ("slo", "sli", "error budget",
                            "service level objective")),
    ("secrets management", 3, ("secrets management", "secret management")),
    ("oauth",           3, ("oauth", "oauth2", "oidc")),
    ("vpc",             3, ("vpc", "subnet", "elastic load balancing")),
    ("rbac",            3, ("rbac", "role-based access")),
    ("dns",             3, ("dns",)),
    ("tls",             3, ("tls", "mtls")),
    ("drift detection", 3, ("drift detection", "remote state",
                            "state locking")),
    ("dynamodb",        3, ("dynamodb",)),
    ("sast",            3, ("sast", "dependency scanning",
                            "vulnerability scanning")),
    ("ingress",         3, ("ingress",)),
    ("kustomize",       3, ("kustomize",)),
    ("rolling deploys", 3, ("rolling update", "rolling deployment")),
    ("defender",        3, ("microsoft defender", "defender for endpoint")),
    ("ckad",            2, ("ckad", "cka")),
    # held at 1 by proficiency, not by rarity - see the note above
    ("ecs",             1, ("amazon ecs", "fargate")),
    ("gitops",          1, ("gitops",)),
    ("kafka",           1, ("kafka",)),
    ("policy as code",  1, ("policy-as-code", "policy as code",
                            "open policy agent")),
)

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


# --------------------------------------------------------------- transport ---

# Python 3.10's urllib handles 301/302/303/307 but NOT 308 - a permanent
# redirect just raises HTTPError. Instacart's careers host uses 308, which made
# a live posting look unreachable. Teach the default opener to follow it.
class _Redirect308(urllib.request.HTTPRedirectHandler):
    # Overriding http_error_308 alone is not enough: redirect_request itself
    # whitelists 301/302/303/307 and raises on anything else.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code == 308:
            code = 301
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, code, msg, headers)


urllib.request.install_opener(urllib.request.build_opener(_Redirect308))


def _req(url, data=None, timeout=25, retries=2):
    """Retry on transport failures - at 400+ concurrent tasks a single blip
    would otherwise silently drop a whole board from the results."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = urllib.request.Request(
                url, data=json.dumps(data).encode() if data is not None else None,
                headers=UA, method="POST" if data is not None else "GET")
            with urllib.request.urlopen(r, timeout=timeout) as f:
                return json.loads(f.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    raise last


def _get_text(url, timeout=25):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.read().decode("utf-8", "replace")


_TAG = re.compile(r"<[^>]+>")
_ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
        "&nbsp;": " ", "&rsquo;": "'", "&ndash;": "-", "&mdash;": "-"}
_NUM_ENT = {"&#8211;": "-", "&#8212;": "-", "&#8217;": "'", "&#8216;": "'",
            "&#8220;": '"', "&#8221;": '"', "&#160;": " ", "&#8230;": "...",
            "&#x27;": "'", "&#x2019;": "'"}


# <script> and <style> bodies are not markup - the tag stripper turns them into
# prose. A Global Relay posting came back as 25,588 characters of Google Tag
# Manager config and IE11 polyfills with the words "Kubernetes",
# "responsibilities" and "qualifications" appearing exactly zero times; the real
# description started past character 20,000. Anything reading these blobs - the
# radar's skill scoring, gaps.py, tailor.py - was reading JavaScript.
_SCRIPT = re.compile(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")


def strip_html(h):
    if not h:
        return ""
    h = _SCRIPT.sub(" ", h)
    h = _COMMENT.sub(" ", h)
    t = _TAG.sub(" ", h)
    for k, v in _ENT.items():
        t = t.replace(k, v)
    # Numeric entities survived this stripper until now, so WordPress REST
    # titles came through as "Business Analyst &#8211; Senior" - visible in the
    # tracker and in push notifications. _ENT deliberately folds dashes and
    # quotes to ASCII, so fold the numeric spellings the same way rather than
    # letting html_unescape turn them into real en-dashes, then unescape
    # whatever is left (&amp;#x27;, &hellip;, named entities not in _ENT).
    for k, v in _NUM_ENT.items():
        t = t.replace(k, v)
    if "&" in t:
        t = html_unescape(t)
    return re.sub(r"\s+", " ", t)


def days_ago(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return None


def job(src, company, title, loc, url, posted=None, age=None, blob="", hyd=None,
        updated=None):
    """`posted` is true publication. `updated` is last-modified where the
    platform exposes it - an old req edited recently often means the recruiter
    is actively working it, which a publication date alone does not show."""
    return dict(source=src, company=company, title=title, location=loc or "",
                url=url, posted=posted or "", age=age, blob=blob, hyd=hyd,
                updated=updated or "", offices=[])


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


# ------------------------------------------------- adapters (pass 1: light) ---

def fetch_greenhouse(tok):
    d = _req(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=false")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        # `updated_at` is last-modified, not publication. Measured across 13,528
        # postings it understates true age by a median of 36 days and is correct
        # only 17% of the time - a recruiter editing an old req makes it look
        # posted today. `first_published` is the real date; fall back only if a
        # board omits it.
        posted = j.get("first_published") or j.get("updated_at", "")
        out.append(job("greenhouse", tok, j.get("title", ""), loc,
                       j.get("absolute_url", ""), posted,
                       days_ago(posted), f"{j.get('title','')} {loc}",
                       hyd=("greenhouse", tok, j.get("id")),
                       updated=j.get("updated_at", "")))
    return out


def fetch_lever(tok):
    d = _req(f"https://api.lever.co/v0/postings/{tok}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        ts = j.get("createdAt")
        posted = (datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
                  if ts else "")
        # Lever already ships the full description - no hydration needed.
        blob = " ".join(filter(None, [
            j.get("text", ""), loc, cat.get("team") or "",
            j.get("descriptionPlain", "")[:6000],
            " ".join(strip_html(l.get("text", "") + " " + l.get("content", ""))
                     for l in (j.get("lists") or []))[:4000]]))
        out.append(job("lever", tok, j.get("text", ""), loc,
                       j.get("hostedUrl", ""), posted, days_ago(posted), blob))
    return out


# The board-level type (JobPostingBriefsWithIdsAndTeamId) exposes NO date field
# at all - publishedDate lives only on the single-posting type. Asking for it
# here makes the whole query error and the adapter return an empty list, which
# silently zeroed every Ashby board. Dates come from the hydrate call instead.
ASHBY_LIST = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!)"
              " { jobBoard: jobBoardWithTeams("
              "organizationHostedJobsPageName: $organizationHostedJobsPageName)"
              " { jobPostings { id title locationName "
              "secondaryLocations { locationName } } } }")


# Ashby serves every board from one host, and the sweep asks it for 95 of them
# through a 24-worker pool. That burst started answering HTTP 429 the day the
# board list grew from 55 to 95: 6-14 boards failed per sweep, rotating, and
# seven of the casualties were boards that had worked for months - slant,
# solace, synthetic, telus-digital, thinkific, trulioo, wealthsimple. Nothing
# was wrong with any of them. Asked serially, all 95 answer 200, so the failure
# was our own concurrency rather than a block, and the digest's "Dead boards"
# footer was reporting live boards.
#
# A concurrency cap is the wrong tool here: 55 boards through the same
# 24-worker pool never tripped the limit, so what Ashby counts is requests per
# window, not simultaneous connections. A minimum gap is the control that
# matches, and it is the same shape as _jb_wait.
#
# 0.4s was the first value tried and it is NOT enough. One sweep of all 95
# boards passed cleanly at 0.4s - 95/95, 2,484 postings, 37.9s - but a second
# sweep started immediately after it failed partway through, so the bucket is
# depleted by sustained rate and not just by the opening burst. It refills
# fast: every board answered 200 again within two minutes, and Ashby exposes
# no Retry-After or rate headers to read it off.
#
# 0.8s holds: two back-to-back sweeps of all 95 boards, 24 workers, 95/95 and
# 2,484 postings each, zero 429s. That is ~190 requests over ~160s of
# continuous load, which is harsher than anything production does - the worst
# real overlap is the fast lane at :35 (14 Ashby boards) running into the
# 30-minute lane at :37 (95), about 109 requests in ~87s. The cost is a 76s
# serialised chain on the 30-minute lane and ~11s on the fast lane, both of
# which run alongside the other platforms rather than adding to them.
#
# Both passes take it, because hydrate asks the same host for each finalist.
ASHBY_GAP = 0.8
_ASHBY_LOCK = threading.Lock()
_ASHBY_LAST = [0.0]


def _ashby_wait():
    """Space out requests to jobs.ashbyhq.com across the whole sweep."""
    with _ASHBY_LOCK:
        gap = time.time() - _ASHBY_LAST[0]
        if gap < ASHBY_GAP:
            time.sleep(ASHBY_GAP - gap)
        _ASHBY_LAST[0] = time.time()


def fetch_ashby(tok):
    _ashby_wait()
    d = _req("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
             {"operationName": "ApiJobBoardWithTeams",
              "variables": {"organizationHostedJobsPageName": tok},
              "query": ASHBY_LIST})
    jb = (d.get("data") or {}).get("jobBoard") or {}
    out = []
    for j in jb.get("jobPostings", []):
        locs = [j.get("locationName") or ""] + [
            (s or {}).get("locationName", "") for s in (j.get("secondaryLocations") or [])]
        loc = " / ".join(x for x in locs if x)
        out.append(job("ashby", tok, j.get("title", ""), loc,
                       f"https://jobs.ashbyhq.com/{tok}/{j.get('id')}",
                       "", None, f"{j.get('title','')} {loc}",
                       hyd=("ashby", tok, j.get("id"))))
    return out


def fetch_recruitee(tok):
    d = _req(f"https://{tok}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        pub = j.get("published_at") or ""
        blob = f"{j.get('title','')} {loc} {strip_html(j.get('description',''))[:4000]}"
        out.append(job("recruitee", tok, j.get("title", ""), loc,
                       j.get("careers_url", ""), pub, days_ago(pub), blob))
    return out


def fetch_rippling(tok):
    d = _req(f"https://api.rippling.com/platform/api/ats/v1/board/{tok}/jobs")
    out = []
    for j in d if isinstance(d, list) else []:
        loc = j.get("workLocation", {}).get("label", "") or j.get("locationName", "")
        out.append(job("rippling", tok, j.get("name", ""), loc,
                       j.get("url", ""), "", None, f"{j.get('name','')} {loc}"))
    return out


WD_AGE = re.compile(r"(\d+)\+?\s*(day|month|week)", re.I)


def wd_age(p):
    if not p:
        return None
    p = p.lower()
    if "today" in p or "just posted" in p:
        return 0
    if "yesterday" in p:
        return 1
    m = WD_AGE.search(p)
    return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()] if m else None


def _wd_country_facet(api):
    """Ask the tenant which facet parameter carries Country, and cache it."""
    if api in _WD_FACET_CACHE:
        return _WD_FACET_CACHE[api]
    name = None
    try:
        d = _req(api, {"appliedFacets": {}, "limit": 1, "offset": 0,
                       "searchText": ""})

        def walk(vals):
            for v in vals or []:
                desc = (v.get("descriptor") or "").strip().lower()
                if desc in ("country", "location country", "job country",
                            "pays", "country/region") and v.get("facetParameter"):
                    return v["facetParameter"]
                hit = walk(v.get("values"))
                if hit:
                    return hit
            return None

        name = walk(d.get("facets"))
    except Exception:
        pass
    _WD_FACET_CACHE[api] = name
    return name


# Enterprise Workday tenants carry thousands of Canadian reqs (TD alone has
# ~1,029). Paging all of them costs ~51 requests per tenant to then throw 98%
# away. Ask the server for the roles we want instead.
WD_QUERIES = ["devops", "site reliability", "platform engineer", "cloud engineer",
              "infrastructure engineer", "kubernetes", "linux", "support engineer",
              "automation engineer", "systems engineer",
              # Endpoint/device-management work is a weight-4 title family and a
              # weight-4 skill family, but none of the terms above surface it -
              # Workday matches job text, and an "Endpoint Engineer" req rarely
              # says "devops" or "kubernetes". Without these three, roles like
              # TD's Senior MDM Engineer and CIBC's Endpoint Engineer were
              # invisible on every one of the 31 Workday tenants.
              "endpoint", "intune", "modern workplace"]


# Workday rate-limits per TENANT, not per request. The sweep fans every
# WD_QUERIES term out concurrently, so a single tenant could see 13 simultaneous
# requests - and at 31 tenants x 13 terms that is 403 requests a sweep, with the
# fast lane adding ~180 more at :00 and :30. Measured result: bursts of 60+
# "HTTP 429 Too Many Requests" in radar.log, on productive boards - Moneris,
# Interac and Ledcor all contribute matches and all got throttled.
#
# A semaphore per tenant caps the burst without serialising it completely;
# full serialisation would be far too slow, since one RBC query alone can take
# 13s. Different tenants still run fully in parallel.
# This semaphore is PER PROCESS, which is the trap. fast.sh and daily.sh are
# separate processes, so a cap of 6 allowed 6 concurrent requests each - 12 to
# the same tenant whenever the two lanes overlapped. After the cron window was
# widened to 7-19 daily, radar.log showed 285 rate-limit errors on Sunday alone,
# clustered precisely at :01 and :31 (5.4 and 7.3 per sweep) while every other
# cron slot sat near zero. Ledcor, Moneris and Interac took the worst of it, and
# all three produce matches, so the throttling was costing real postings.
#
# Two fixes together: the lanes are now offset in cron (daily.sh at :07/:37) so
# they never start at the same minute, and this drops to 3 - which measured 0
# errors in testing, and leaves headroom of 6 even if the two lanes do collide.
# The cost is about 47s on a full sweep, which a 30-minute cadence absorbs.
WD_TENANT_BURST = 3
_WD_SEM = {}
_WD_SEM_LOCK = threading.Lock()

# Workday hard-caps `limit` at 20 and answers HTTP 400 Bad Request above it -
# loudly, unlike Amazon's result_limit=500 returning hits:0 or SmartRecruiters'
# totalFound:0 - so paging is the only way to read a board past 20 rows.
#
# This used to be `while offset < 40`, a blind two-page ceiling, which silently
# truncated every query with more than 40 Canadian hits. Measured on the day it
# was fixed: Manulife lost rows on 4 of 13 queries (automation engineer 61,
# support engineer 59, platform engineer 50, systems engineer 41) and RBC on 6
# of 13 (support engineer 93, modern workplace 80, platform engineer 70, systems
# engineer 65, automation engineer 60, cloud engineer 42). A req whose title and
# description miss all 13 WD_QUERIES terms was unreachable regardless, but one
# that merely ranked 41st for a broad term was being dropped for no reason.
#
# Paging to `total` costs less than it looks, because the old code also fetched
# a pointless second page whenever total was 21+: Manulife goes 18 -> 22
# requests and RBC 22 -> 31. It adds no burst pressure either, since _wd_sem is
# held across the whole function, so extra pages lengthen one slot rather than
# widening concurrency against a tenant that rate-limits per tenant.
# The list call collapses a multi-location requisition to the literal string
# "2 Locations" / "3 Locations", with no city anywhere in the row. That string
# classifies as MULTI and is KEPT, which is the right default - it might include
# Canada - but it means a req open only in Bratislava and Madrid arrives looking
# like a candidate, and the digest shows "2 Locations" where a city belongs.
#
# The detail call (already fetched by the hydrate pass, so this costs nothing
# extra) carries the real set: jobPostingInfo.location is the primary and
# .additionalLocations is the rest. So the placeholder is repaired in hydrate
# and the geography fields derived from it in pass 1 are recomputed, rather than
# guessing a city out of the externalPath slug - a slug cannot be split
# reliably, since the hyphen is both the separator and part of real names
# ("Toronto-Ontario" vs "Los-Angeles-Del-Rey" vs "Saint-Laurent").
WD_NLOC = re.compile(r"^\s*\d+\s+Locations?\s*$", re.I)

WD_PAGE = 20
# 300 rows per query, overridable per target with "max_pages".
#
# Sized from the deepest query on each large tenant: TD Bank 123 ("platform
# engineer"), RBC 93, BMO 84, Thomson Reuters 67, Manulife 61 - so 7 pages
# covers every tenant whose search actually discriminates, and 15 leaves room.
#
# CIBC is the one board that exceeds it, and deliberately is not chased. Its
# searchText works (devops -> 5, a nonsense term -> 0) but "linux" matches 489
# of its 490 Canadian reqs, because the word sits in CIBC's posting boilerplate
# rather than in 489 Linux jobs - page one of that query is Mobile Mortgage
# Advisors. Raising the global ceiling to 25 to swallow it would add ~130
# requests a sweep to read retail-banking listings that title_match rejects at
# tier 0, while the DevOps-titled CIBC reqs are already reachable through the
# cheap "devops" query. So CIBC reports a truncation every sweep and that is the
# correct outcome, not a bug to silence - and if a tenant ever genuinely needs
# deeper paging, give that one target "max_pages" instead of moving this.
WD_MAX_PAGES = 15

# Queries that hit WD_MAX_PAGES with rows still unread. Silent truncation is the
# bug being fixed here, so hitting the new ceiling is recorded and surfaced in
# the run footer instead of being swallowed a second time.
WD_TRUNCATED = []
_WD_TRUNC_LOCK = threading.Lock()


def _wd_sem(tenant):
    with _WD_SEM_LOCK:
        if tenant not in _WD_SEM:
            _WD_SEM[tenant] = threading.BoundedSemaphore(WD_TENANT_BURST)
        return _WD_SEM[tenant]


def fetch_workday(t):
    with _wd_sem(t.get("tenant", "")):
        """Fetch ONE keyword query against one tenant.

        main() expands each Workday target into one task per query so the shared
        thread pool parallelises them; doing all ten queries inline made a 9-tenant
        run take minutes.
        """
        tenant, cluster, site = t["tenant"], t["cluster"], t["site"]
        base = f"https://{tenant}.{cluster}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        facet = t.get("country_facet") or _wd_country_facet(api)
        applied = {facet: [WD_CANADA]} if facet else {}
        name = t.get("name", tenant)
        query = t.get("_query", "")

        # An explicit per-target max_pages is a DELIBERATE shallow read, so it
        # must not be reported as accidental truncation. PwC is the case this
        # exists for: it has no country facet, so every query searches 3,859
        # global reqs and four of them hit the ceiling. Warning about that every
        # half hour would train the reader to ignore WD_TRUNCATED, which is the
        # register that caught CIBC. Only the global default warns.
        want = t.get("max_pages")
        max_pages = int(want or WD_MAX_PAGES)
        shallow_by_choice = bool(want)
        out, offset, total = [], 0, None
        for _ in range(max_pages):
            d = _req(api, {"appliedFacets": applied, "limit": WD_PAGE,
                           "offset": offset, "searchText": query})
            posts = d.get("jobPostings", [])
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath", "")
                loc = j.get("locationsText", "")
                out.append(job("workday", name, j.get("title", ""), loc,
                               f"{base}/en-US/{site}{path}",
                               j.get("postedOn", ""), wd_age(j.get("postedOn")),
                               f"{j.get('title','')} {loc} {j.get('remoteType','')}",
                               hyd=("workday", f"{base}/wday/cxs/{tenant}/{site}{path}")))
            # `total` is reported only on page 1; later pages say 0, so capture
            # it once and page to it instead of guessing a ceiling.
            if total is None:
                total = d.get("total") or 0
            offset += WD_PAGE
            # A short page is the end of the result set whatever `total` says -
            # checked first so a tenant that omits or zeroes `total` still pages
            # correctly rather than stopping after one page.
            if len(posts) < WD_PAGE:
                break
            if total and offset >= total:
                break
        else:
            if total and total > offset and not shallow_by_choice:
                with _WD_TRUNC_LOCK:
                    WD_TRUNCATED.append(
                        f"workday/{name} · {query!r}: read {offset} of {total} "
                        f"({max_pages} pages)")
        return out


def _braces(h, i):
    """Slice the balanced {...} starting at or after index i."""
    start = h.index("{", i)
    depth = 0
    for k in range(start, len(h)):
        if h[k] == "{":
            depth += 1
        elif h[k] == "}":
            depth -= 1
            if depth == 0:
                return h[start:k + 1]
    return None


def fetch_phenom(t):
    """Phenom People careers sites (Bell, Air Canada, many bank front-ends).

    Phenom ships the first page of results inline as `phApp.ddo`, so there is no
    API to reverse-engineer - parse the page. Note that some Phenom sites are
    only a skin over Workday; where that is true, poll the Workday tenant
    instead (it gives structured locations and full descriptions). RBC and BMO
    both turned out to be Workday underneath.
    """
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    out, frm, total = [], 0, None
    while frm < 120:
        try:
            html = _get_text(f"{base}/search-results?from={frm}&s=1")
        except Exception:
            break
        m = re.search(r"phApp\.ddo\s*=", html)
        if not m:
            break
        try:
            d = json.loads(_braces(html, m.end()) or "{}")
        except Exception:
            break
        els = d.get("eagerLoadRefineSearch") or {}
        posts = ((els.get("data") or {}).get("jobs")) or []
        if not posts:
            break
        if total is None:
            total = els.get("totalHits") or 0
        for j in posts:
            # Prefer the clean city/state fields. Some tenants (City of
            # Edmonton) put a street address in `location` and `multi_location`,
            # which no location classifier can read as a city.
            loc = (j.get("cityStateCountry") or j.get("cityState") or "").strip()
            if not loc:
                locs = [x for x in (j.get("multi_location") or []) if x]
                loc = " / ".join(locs) if locs else (
                    j.get("location") or j.get("country") or "")
            if not loc:
                loc = t.get("default_location", "")
            # Same trap as Greenhouse: `postedDate` is a refresh/repost date
            # and can sit months after `dateCreated`, which is the true origin.
            # One Bell req showed postedDate 2026-09-03 vs dateCreated
            # 2026-06-26. Report the origin, carry the refresh separately.
            created = str(j.get("dateCreated") or "")[:10]
            reposted = str(j.get("postedDate") or "")[:10]
            posted = created or reposted
            jid = j.get("jobId") or j.get("reqId") or ""
            out.append(job("phenom", name, j.get("title", ""), loc,
                           f"{base}/job/{jid}/", posted, days_ago(posted),
                           " ".join([j.get("title", ""), loc,
                                     j.get("category", "") or "",
                                     (j.get("descriptionTeaser") or "")[:1500]]),
                           hyd=("phenom", f"{base}/job/{jid}/"),
                           updated=reposted if reposted != created else ""))
        frm += 10
        if total and frm >= total:
            break
    return out


# --- SAP SuccessFactors (RMK career sites) ----------------------------------
# Rogers, Scotiabank, Bombardier and much of Canadian enterprise. No JSON API
# and no RSS - the search page renders results server-side, which is fine: the
# markup is stable and `sortColumn=referencedate` gives newest-first.

SF_ROW = re.compile(r'<tr class="data-row".*?</tr>', re.S)
# SuccessFactors ships at least two themes. City of Toronto uses a tile layout
# (<li class="job-tile">) with no location column - the location lives in the
# URL slug instead, e.g. /job/Toronto-CIVIL-ENGINEERING-TECHNOLOGIST-ON-M5V3C6/
SF_TILE = re.compile(r'<li class="job-tile\b.*?</li>', re.S)
SF_TILE_DATE = re.compile(r'section-field date[^>]*>(.*?)</div>\s*</div>', re.S)
SF_SLUG_LOC = re.compile(
    r"/job/([A-Za-z\u00C0-\u017F'.\-]+?)-.*?-(ON|QC|BC|AB|MB|SK|NS|NB|NL|PE)"
    r"(?:-[A-Z0-9]+)?/", re.I)
# Attribute order differs between the two themes: the table layout puts href
# first, the tile layout puts class first. Match either.
SF_LINK = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*jobTitle-link)[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>',
    re.S)
SF_LOC = re.compile(r'<span class="jobLocation">\s*(.*?)\s*</span>', re.S)
SF_DATE = re.compile(r'<span class="jobDate[^"]*">\s*(.*?)\s*</span>', re.S)
SF_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# SuccessFactors ORs multi-word queries into noise ("site reliability" returns
# anything matching "site"). Single tokens only.
SF_QUERIES = ["devops", "sre", "reliability", "kubernetes", "linux", "cloud",
              "infrastructure", "platform", "automation", "support"]


def _sf_text(x):
    return re.sub(r"\s+", " ", html_unescape(_TAG.sub("", x))).strip()


def _sf_date(x):
    """'Sep 16, 2026' -> '2026-09-16'."""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", x or "")
    if not m:
        return ""
    mon = SF_MONTHS.get(m.group(1))
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}" if mon else ""


def fetch_successfactors(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    query = t.get("_query", "")
    out, start = [], 0
    while start < 75:                       # 3 pages of 25 per query
        # Adding sortColumn makes SuccessFactors ignore q= entirely and return
        # everything by date, so rely on the query and sort locally by age.
        url = (f"{base}/search/?q={urllib.parse.quote(query)}"
               f"&startrow={start}")
        try:
            h = _get_text(url)
        except Exception:
            break
        rows, tiled = SF_ROW.findall(h), False
        if not rows:
            rows, tiled = SF_TILE.findall(h), True
        if not rows:
            break
        for r in rows:
            link = SF_LINK.search(r)
            if not link:
                continue
            href = html_unescape(link.group(1))
            title = _sf_text(link.group(2))
            if tiled:
                m = SF_SLUG_LOC.search(href)
                loc = (f"{m.group(1).replace('-', ' ')}, {m.group(2).upper()}"
                       if m else t.get("default_location", ""))
                d = SF_TILE_DATE.search(r)
                raw = _sf_text(d.group(1)).replace("Posting Date", "").strip() if d else ""
            else:
                loc_m = SF_LOC.search(r)
                loc = _sf_text(loc_m.group(1)) if loc_m else ""
                d = SF_DATE.search(r)
                raw = _sf_text(d.group(1)) if d else ""
            # The date SuccessFactors shows is the RMK "Reference Date", NOT a
            # posting date. SAP's own docs say it is set on import then cycled
            # every 28 days to keep content fresh for SEO, and that the real
            # RCM posting date is not exposed on the career site. Verified: on
            # four of five tenants NOTHING on the whole board is older than
            # 28-29 days. Treat it as unknown age and carry the reference date
            # separately rather than reporting a number that means nothing.
            refdate = _sf_date(raw)
            posted = ""
            full = href if href.startswith("http") else base + href
            out.append(job("successfactors", name, title, loc, full,
                           posted, None, f"{title} {loc}",
                           hyd=("successfactors", full),
                           updated=refdate))
        if len(rows) < 25:
            break
        start += 25
    return out


# --- Oracle Recruiting Cloud (ORC) -------------------------------------------
# Used by universities, utilities and energy companies. A proper JSON REST API
# with real posted dates - far better than the HTML-parsing adapters. The
# careers URL looks like:
#   https://<host>/hcmUI/CandidateExperience/en/sites/<SITE>/jobs
# and the site number (usually CX_1) is in that page's markup.

ORC_QUERIES = ["devops", "cloud", "infrastructure", "systems", "network",
               "engineer", "analyst", "linux", "security", "platform"]


def fetch_oracle(t):
    host = t["host"].rstrip("/")
    site = t.get("site_number", "CX_1")
    name = t.get("name", host)
    q = t.get("_query", "")
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList"
           f"&finder=findReqs;siteNumber={site},limit=50,sortBy=POSTING_DATES_DESC")
    if q:
        api += f",keyword={urllib.parse.quote(q)}"
    d = _req(api)
    items = d.get("items") or []
    reqs = items[0].get("requisitionList", []) if items else []
    out = []
    for r in reqs:
        loc = r.get("PrimaryLocation") or r.get("PrimaryLocationCountry") or ""
        posted = str(r.get("PostedDate") or "")[:10]
        rid = r.get("Id")
        out.append(job("oracle", name, r.get("Title", ""), loc,
                       f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                       f"{t.get('site', 'CX_1')}/job/{rid}",
                       posted, days_ago(posted),
                       " ".join(filter(None, [r.get("Title", ""), loc,
                                              r.get("JobFamily") or "",
                                              r.get("JobFunction") or "",
                                              r.get("WorkplaceTypeCode") or ""]))))
    return out


# --- Jobvite -----------------------------------------------------------------
# Server-rendered HTML, no JSON API, but the markup is stable and simple:
#   <td class="jv-job-list-name"><a href="/<tenant>/job/<id>">Title</a></td>
#   <td class="jv-job-list-location">City, Province</td>
# The list carries no date; each job page has schema.org "datePosted", so dates
# come from the hydrate pass.

JV_ROW = re.compile(
    r'class="jv-job-list-name">\s*<a href="([^"]+)">(.*?)</a>'
    r'.*?class="jv-job-list-location">\s*(.*?)\s*</td>', re.S)
JV_DATE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')

JV_QUERIES = ["devops", "cloud", "infrastructure", "systems", "engineer",
              "linux", "network", "analyst", "security", "support"]


def fetch_jobvite(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    q = t.get("_query", "")
    url = f"{base}/search" + (f"?q={urllib.parse.quote(q)}" if q else "")
    try:
        h = _get_text(url)
    except Exception:
        return []
    out = []
    for href, title, loc in JV_ROW.findall(h):
        title = re.sub(r"\s+", " ", strip_html(title)).strip()
        # Jobvite wraps locations across lines: "Round Rock,\n   Texas"
        loc = re.sub(r"\s+", " ", strip_html(loc)).strip()
        if not title:
            continue
        full = href if href.startswith("http") else \
            "https://jobs.jobvite.com" + href
        out.append(job("jobvite", name, title, loc or t.get("default_location", ""),
                       full, "", None, f"{title} {loc}",
                       hyd=("jobvite", full)))
    return out


# --- Job Bank (Government of Canada) ----------------------------------------
# The federal board, and the only source here that is neither an ATS product nor
# an employer's own site. No JSON, no RSS (tried /jobsearch/rss and
# /sitemap.xml - both 404), so the search results page is parsed. The markup is
# server-rendered and stable: one <article id="article-{id}"> per posting with
# .noctitle, .date, .business and .location in a definition list.
#
# robots.txt is `User-agent: * / Crawl-delay: 5` - fully permissive and the only
# host here that asks for a delay, so it gets one. Requests are serialised
# through _jb_wait() with a 5s minimum gap, shared by the list AND hydrate
# passes, because the courtesy is per host and not per code path. That is why
# this is a single task issuing its queries in sequence rather than one task per
# query like Workday: fanning it out would mean parallel hits on a host that
# explicitly asked us not to.
#
# Three traps, all of which would have been silent:
#  1. jsessionid is IN THE PATH, not a query parameter -
#     /jobsearch/jobposting/50534243;jsessionid=656BB06A...jobsearch76 - and it
#     rotates per visit. Left in, every sweep would see a brand-new URL for the
#     same posting, so `seen` would never match and the same job would notify
#     forever. JB_SESS strips it, leaving /jobsearch/jobposting/{id}.
#  2. Dates are localised English month names ("October 06, 2026"), the exact
#     trap documented for Amazon: strptime("%B") is locale-dependent and cron
#     runs with a minimal environment, so it would work by hand and fail on
#     every scheduled sweep. _amz_date already maps the months by hand, so it is
#     reused rather than reimplemented.
#  3. The location cell carries a screen-reader label, so strip_html yields
#     "Location Toronto (ON)". The leading word has to come off or the string
#     stops classifying.
#
# Measured 2026-10-21: "devops" returns 5 results Canada-wide (the page's own
# results-summary agrees, so that is the whole set, not a page), "site
# reliability engineer" and "cloud infrastructure" return 0, "linux
# administrator" returns 10, and NONE of them had an Alberta posting. Job Bank
# is overwhelmingly trades and service work - a location-only query returns
# cooks and diesel mechanics - so this board is kept for completeness and
# because its inventory changes, not because it is expected to carry the search.
JB_BASE = "https://www.jobbank.gc.ca"
# "systems administrator" was in this list for one run and is deliberately out.
# It was the single biggest source of rows and almost all of them were the IT
# sysadmin work this search is explicitly not looking for: 14 of the 17
# Infra-adjacent matches it produced were "network system administrator" or
# "information technology (it) system administrator" at scores 11-13, four of
# them the same employer posting the same role four times, and one crossed the
# tracking threshold at 15 and would have been filed. Job Bank's title
# vocabulary is NOC-derived, so that query maps onto help-desk and desktop work
# rather than platform work. The DevOps-shaped queries below find the real roles
# on this board without it.
JB_QUERIES = ["devops", "site reliability", "cloud engineer", "linux",
              "platform engineer"]
JB_DELAY = 5.0

JB_ROW = re.compile(r'<article id="article-\d+".*?</article>', re.S)
JB_LINK = re.compile(r'href="(/jobsearch/jobposting/[^"]+)"')
JB_TITLE = re.compile(r'class="noctitle">(.*?)</span>', re.S)
JB_DATE = re.compile(r'class="date">(.*?)</li>', re.S)
JB_BIZ = re.compile(r'class="business">(.*?)</li>', re.S)
JB_LOC = re.compile(r'class="location">(.*?)</li>', re.S)
JB_SAL = re.compile(r'class="salary">(.*?)</li>', re.S)
JB_SESS = re.compile(r";jsessionid=[^?#]*")

_JB_LOCK = threading.Lock()
_JB_LAST = [0.0]


def _jb_wait():
    """Honour robots.txt Crawl-delay: 5 across every Job Bank request."""
    with _JB_LOCK:
        gap = time.time() - _JB_LAST[0]
        if gap < JB_DELAY:
            time.sleep(JB_DELAY - gap)
        _JB_LAST[0] = time.time()


def fetch_jobbank(t):
    name = t.get("name", "Job Bank")
    queries = t.get("queries") or JB_QUERIES
    out, seen = [], set()
    for q in queries:
        _jb_wait()
        url = (f"{JB_BASE}/jobsearch/jobsearch?sort=M&searchstring="
               + urllib.parse.quote(q))
        try:
            h = _get_text(url)
        except Exception:
            continue
        for row in JB_ROW.findall(h):
            lk = JB_LINK.search(row)
            ti = JB_TITLE.search(row)
            if not lk or not ti:
                continue
            path = JB_SESS.sub("", lk.group(1)).split("?")[0]
            full = JB_BASE + path
            if full in seen:
                continue
            seen.add(full)
            title = strip_html(ti.group(1)).strip()
            if not title:
                continue
            loc = strip_html(JB_LOC.search(row).group(1)) if JB_LOC.search(row) else ""
            # strip_html leaves the screen-reader label in front of the city
            loc = re.sub(r"^\s*Location\s+", "", loc).strip()
            comp = strip_html(JB_BIZ.search(row).group(1)).strip() \
                if JB_BIZ.search(row) else ""
            raw_date = strip_html(JB_DATE.search(row).group(1)).strip() \
                if JB_DATE.search(row) else ""
            posted = _amz_date(raw_date) or ""
            sal = strip_html(JB_SAL.search(row).group(1)).strip() \
                if JB_SAL.search(row) else ""
            out.append(job("jobbank", comp or name, title, loc, full,
                           posted, days_ago(posted),
                           " ".join(filter(None, [title, loc, comp, sal])),
                           hyd=("jobbank", full)))
    return out


# --- UltiPro / UKG Recruiting ------------------------------------------------
# UKG's board is a JSON POST endpoint, not a REST GET:
#   POST {board}/JobBoardView/LoadSearchResults
#   {"opportunitySearch": {"Top": N, "Skip": M, "QuerySort": [],
#     "OpportunityFilter": {...}}}
# and `board` is the whole prefix including the company code and the board GUID,
# e.g. https://recruiting.ultipro.ca/HAV5100HBANK/JobBoard/<guid>. The GUID is
# not derivable - it has to be read off the employer's careers page - and the
# host is .ca or .com depending on which UKG region the tenant sits in, so the
# base belongs in targets.json verbatim rather than being assembled.
#
# Paging advances by len(opportunities), NOT by the requested Top. `Top` was
# honoured on the tenant tested (Top=5 gave 5, Top=1000 gave all 17), but if a
# tenant ever capped it server-side, advancing Skip by the asked-for size would
# step straight over the rows that were not returned - a silent hole in the
# middle of the board rather than a short read at the end. `totalCount` is
# returned on every page here (unlike Workday, which gives it only on page one),
# so it is the authoritative stop.
#
# LOCATION TRAP: do not build the location from `LocalizedLocationId`. It is a
# bare province code, and bare codes do not classify - measured, "AB" and "ON"
# both land in OTHER and would be DROPPED, while "Calgary, AB, Canada" and
# "Ontario" classify correctly. Every entry carries a full nested `Address`, so
# city + State.Code + Country.Name is used instead. Note Country.Code is the
# three-letter "CAN", which is why Country.NAME is read rather than the code.
#
# BriefDescription is a 100-800 char teaser, not the posting, so unlike wpjobs,
# amazon and teamtailor this DOES need a hydrate pass. The detail page embeds
# the full text in a JSON "Description" field (9,101 chars on the role this was
# built against).
UKG_TOP = 50
UKG_MAX_PAGES = 12
UKG_DESC = re.compile(r'"Description"\s*:\s*("(?:[^"\\]|\\.)*")')


def _ukg_loc(locs):
    out = []
    for L in locs or []:
        a = (L or {}).get("Address") or {}
        st = a.get("State") or {}
        co = a.get("Country") or {}
        one = ", ".join(x for x in (a.get("City"),
                                    st.get("Code") or st.get("Name"),
                                    co.get("Name") or co.get("Code")) if x)
        # Fallback only if the address is empty: the localized DESCRIPTION is a
        # province name ("Ontario"), which classifies; the localized ID is a
        # bare code ("ON"), which does not.
        if not one:
            one = (L or {}).get("LocalizedDescription") or ""
        if one:
            out.append(one)
    return " / ".join(dict.fromkeys(out))


def fetch_ultipro(t):
    board = t["board"].rstrip("/")
    name = t.get("name", board)
    out, skip, total = [], 0, None
    for _ in range(UKG_MAX_PAGES):
        try:
            d = _req(board + "/JobBoardView/LoadSearchResults",
                     {"opportunitySearch": {
                         "Top": UKG_TOP, "Skip": skip, "QuerySort": [],
                         "OpportunityFilter": {
                             "FilterTerm": "", "SearchText": "", "Keyword": "",
                             "OpportunityId": None, "Locations": [],
                             "Departments": [], "PostedDates": []}}})
        except Exception:
            break
        ops = d.get("opportunities") or []
        if not ops:
            break
        if total is None:
            total = d.get("totalCount") or 0
        for o in ops:
            title = (o.get("Title") or "").strip()
            if not title:
                continue
            loc = _ukg_loc(o.get("Locations"))
            posted = str(o.get("PostedDate") or "")[:10]
            oid = o.get("Id")
            out.append(job("ultipro", name, title, loc,
                           f"{board}/OpportunityDetail?opportunityId={oid}",
                           posted, days_ago(posted),
                           " ".join(filter(None, [
                               title, loc, o.get("JobCategoryName") or "",
                               o.get("RequisitionNumber") or "",
                               o.get("BriefDescription") or ""])),
                           hyd=("ultipro",
                                f"{board}/OpportunityDetail?opportunityId={oid}")))
        skip += len(ops)
        if total and skip >= total:
            break
    return out


# --- Teamtailor -------------------------------------------------------------
# The best-shaped platform in this file. `{tenant}.teamtailor.com/jobs.json` is
# a JSON Feed (jsonfeed.org 1.1) and each item carries everything needed in one
# request: a real ISO `date_published`, the full `content_html` description
# inline, and a schema.org JobPosting under `_jobposting` with a structured
# PostalAddress. So like `wpjobs` and `amazon` this needs **no hydrate pass**,
# and unlike `avature` it has genuine dates, so --max-age works normally.
#
# robots.txt allows it: only /app/, /messages/, /messenger/, /facebook/tab/ and
# /jobs/internal/ are disallowed for *, and the host advertises
# `Content-Signal: search=yes, ai-input=yes` (ai-train=no, which this is not).
#
# THE TRAP, and it would have silently cost Alberta postings: `addressCountry`
# is the ISO code, and bare "CA" does not read as Canada to classify_location -
# it only survives when the city itself is in the classifier's Canadian city
# list. Measured: "Toronto, CA" classifies CA/ON, but "Lethbridge, CA",
# "Medicine Hat, CA", "Grande Prairie, CA" and "Nisku, CA" all land in OTHER
# and are DROPPED, while the same cities with ", Canada" classify correctly as
# CA. That is the Amazon Edmonton-suburb failure wearing a different hat - a
# real Alberta req invisible because of how its country was spelled - so the
# code expands the ISO code to a country name before handing it over. Codes
# that are not in the map pass through unchanged, which is harmless: "SE" and
# "DE" reach OTHER either way.
TT_ISO = {"CA": "Canada", "US": "United States", "GB": "United Kingdom",
          "IE": "Ireland", "AU": "Australia", "NZ": "New Zealand",
          "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
          "DE": "Germany", "NL": "Netherlands", "FR": "France", "ES": "Spain",
          "IT": "Italy", "PL": "Poland", "IN": "India", "SG": "Singapore"}

# `addressRegion` is not always a region. Teamtailor's own board files Stockholm
# under region "Sweden" and Chicago under "North America", and pasting that into
# the location string is not merely untidy - it CHANGES THE BUCKET. Measured:
# "Chicago, North America, United States" classifies NA-REMOTE and is KEPT,
# because "North America" reads as a remote-anywhere-in-NA signal, so an on-site
# Illinois job would have been shown as a remote opportunity. Continents and a
# region that just repeats the country are dropped for that reason.
TT_SKIP_REGION = frozenset((
    "north america", "south america", "latin america", "central america",
    "americas", "europe", "asia", "africa", "oceania", "antarctica",
    "middle east", "emea", "apac", "latam", "anz", "nordics", "worldwide",
    "global", "remote"))

TT_PER_PAGE = 100
# 1,000 postings. `per_page` was honoured up to 500 on test tenants and paging
# ends with an EMPTY page (per_page=3 over 5 jobs gives 3, then 2, then 0).
# The loop therefore stops on an empty page and on seeing no new ids - never on
# a SHORT page, because a server that silently capped per_page below the asked
# value would make every full page look short and end the sweep after one. That
# is precisely how the Workday 40-row cap hid itself.
TT_MAX_PAGES = 10


def fetch_teamtailor(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    out, seen = [], set()
    for page in range(1, TT_MAX_PAGES + 1):
        try:
            d = _req(f"{base}/jobs.json?per_page={TT_PER_PAGE}&page={page}")
        except Exception:
            break
        items = d.get("items") or []
        if not items:
            break
        fresh = 0
        for it in items:
            key = it.get("id") or it.get("url")
            if not key or key in seen:
                continue
            seen.add(key)
            fresh += 1
            jp = it.get("_jobposting") or {}
            posted = str(it.get("date_published") or jp.get("datePosted") or "")[:10]
            locs = []
            for L in (jp.get("jobLocation") or []):
                a = (L or {}).get("address") or {}
                cc = (a.get("addressCountry") or "").strip()
                country = TT_ISO.get(cc.upper(), cc)
                region = (a.get("addressRegion") or "").strip()
                if region.lower() in TT_SKIP_REGION or \
                        region.lower() == country.lower() or \
                        region.lower() == cc.lower():
                    region = ""
                parts = [a.get("addressLocality"), region, country]
                one = ", ".join(x for x in parts if x)
                if one:
                    locs.append(one)
            loc = " / ".join(dict.fromkeys(locs))
            body = strip_html(it.get("content_html") or jp.get("description") or "")
            title = strip_html(it.get("title") or jp.get("title") or "").strip()
            if not title:
                continue
            out.append(job("teamtailor", name, title, loc,
                           it.get("url", ""), posted, days_ago(posted),
                           f"{title} {loc} {body[:8000]}"))
        if not fresh:
            break
    return out


# --- Avature --------------------------------------------------------------
# Avature is the first adapter here with no JSON endpoint AND no date anywhere.
# Greenhouse, Lever, Ashby, Oracle and the rest hand over a publication date;
# Jobvite withholds it from the list but puts schema.org "datePosted" on the
# posting page. Avature has neither: no JSON-LD, no datePosted, no date in the
# markup of either the result row or the posting page. So every Avature posting
# is reported with `posted=""` and `age=None`, i.e. "age unknown", which is the
# honest answer and is what `--max-age` is written to tolerate. The cost is
# real and worth stating: a req that has sat open for a year looks exactly like
# one published this morning, so stale postings cannot be suppressed the way
# Fortinet's 393-day-old DevOps req was. Inventing a date from first-seen would
# be worse - it would read as publication and quietly age out real postings.
#
# Rows are server-rendered and the markup is stable:
#   <article class="article article--result ...">
#     <a class="link link_result" href="https://jobs.ea.com/.../JobDetail/Slug/216407">
#     <div class="article__header__text__subtitle">
#        Vancouver, Canada - Role ID 216407 - Regular Employee - EA Studios
#
# The subtitle is bullet-separated and the segments before "Role ID" are all
# locations - a posting open in several offices lists each one, and the extras
# use a different field order than the primary ("Vancouver, Canada, British
# Columbia" vs "Vancouver, Canada"). They are joined with " / " the way the
# Ashby adapter joins secondary locations, so a req that is Austin-primary but
# also open in Vancouver still classifies as CA rather than US-ONLY. The
# segments after "Role ID" are the employment type and the department
# ("CT - Infrastructure & Platform"), both of which are kept in the blob
# because they score.
#
# Page size is fixed server-side at 20 - jobRecordsPerPage=100 and =200 both
# still return 20 - so the only lever is jobOffset, and pagination stops as
# soon as a page comes back short rather than walking to a fixed ceiling.

AV_ROW = re.compile(r'<article class="article article--result.*?</article>', re.S)
AV_LINK = re.compile(r'<a class="link link_result" href="([^"]+)"[^>]*>(.*?)</a>', re.S)
AV_SUB = re.compile(r'<div class="article__header__text__subtitle">(.*?)</div>', re.S)
AV_ROLE_ID = re.compile(r"^Role ID\b", re.I)

AV_QUERIES = ["devops", "site reliability", "infrastructure", "platform",
              "cloud", "build engineer", "release engineer", "linux"]

AV_PAGE = 20          # Avature ignores jobRecordsPerPage; 20 is the server's size
AV_MAX_PAGES = 4      # 80 rows per query, and it stops early on a short page


def fetch_avature(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    q = t.get("_query", "")
    out, seen = [], set()
    for page in range(AV_MAX_PAGES):
        url = (f"{base}/SearchJobs/?jobRecordsPerPage={AV_PAGE}"
               f"&jobOffset={page * AV_PAGE}")
        if q:
            url += "&search=" + urllib.parse.quote(q)
        try:
            h = _get_text(url)
        except Exception:
            break
        rows = AV_ROW.findall(h)
        for row in rows:
            m = AV_LINK.search(row)
            if not m:
                continue
            full = html_unescape(m.group(1)).strip()
            title = strip_html(m.group(2)).strip()
            if not title or full in seen:
                continue
            seen.add(full)
            sub = AV_SUB.search(row)
            locs, extra = [], []
            if sub:
                parts = [x.strip() for x in strip_html(sub.group(1)).split("\u2022")]
                hit_id = False
                for seg in parts:
                    if not seg:
                        continue
                    if AV_ROLE_ID.match(seg):
                        hit_id = True
                        continue
                    (extra if hit_id else locs).append(seg)
            loc = " / ".join(locs)
            out.append(job("avature", name, title, loc, full,
                           "", None,
                           " ".join([title, loc] + extra),
                           hyd=("avature", full)))
        if len(rows) < AV_PAGE:
            break
    return out


# --- Generic HTML scrape -----------------------------------------------------
# Last resort for careers sites that render their job list server-side but run
# no recognisable ATS. Rather than a bespoke parser per employer, this pulls
# every anchor that looks like a job posting and takes surrounding table cells
# as location/date. Configured entirely in targets.json:
#
#   {"name": "City of Calgary",
#    "url": "https://www.calgary.ca/careers.html",
#    "link_re": "recruiting\\.calgary\\.ca",      # hrefs that are real postings
#    "default_location": "Calgary, AB"}
#
# Fragile by nature - a redesign breaks it. discover.py --verify catches that.

GEN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
GEN_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
GEN_LINK = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
GEN_DATE = re.compile(r"(\d{4})[/-](\d{2})[/-](\d{2})")


def fetch_html(t):
    url = t["url"]
    name = t.get("name", url)
    keep = re.compile(t["link_re"], re.I) if t.get("link_re") else None
    default_loc = t.get("default_location", "")
    try:
        h = _get_text(url)
    except Exception:
        return []

    out, seen = [], set()
    for row in GEN_ROW.findall(h):
        link = GEN_LINK.search(row)
        if not link:
            continue
        href, anchor = html_unescape(link.group(1)), strip_html(link.group(2)).strip()
        if keep and not keep.search(href):
            continue
        if not anchor or len(anchor) < 4 or len(anchor) > 140:
            continue
        full = href if href.startswith("http") else urllib.parse.urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        cells = [strip_html(c).strip() for c in GEN_CELL.findall(row)]
        # A closing date is not a posting date - only take a date if the cell
        # says so, otherwise leave age unknown rather than inventing one.
        posted = ""
        for c in cells:
            if re.search(r"post(ed|ing date)", c, re.I):
                m = GEN_DATE.search(c)
                if m:
                    posted = "-".join(m.groups())
        loc = default_loc
        for c in cells:
            if re.search(r"\b(AB|ON|BC|QC)\b|alberta|ontario|calgary|edmonton", c, re.I) \
                    and len(c) < 60 and "date" not in c.lower():
                loc = c
                break
        out.append(job("html", name, anchor, loc, full, posted,
                       days_ago(posted) if posted else None,
                       f"{anchor} {loc} {' '.join(cells)[:400]}"))
    return out


# --- Workable -----------------------------------------------------------------
# Public widget endpoint, no auth, and it ships full descriptions plus a real
# `published_on` date in the list call - so no hydrate pass is needed.
#   GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
# The account name is the path segment on workable.com/{account}/, which is
# usually visible in the embed on the company's own careers page.


def fetch_workable(tok):
    d = _req(f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true")
    out = []
    for j in d.get("jobs", []):
        locs = j.get("locations") or []
        loc = " / ".join(
            ", ".join(filter(None, [l.get("city"), l.get("region") or l.get("state"),
                                    l.get("country")])) for l in locs if l)
        if not loc:
            loc = ", ".join(filter(None, [j.get("city"), j.get("state"),
                                          j.get("country")]))
        if j.get("telecommuting") and "remote" not in loc.lower():
            loc = f"Remote{' - ' + loc if loc else ''}"
        posted = str(j.get("published_on") or j.get("created_at") or "")[:10]
        out.append(job("workable", tok, j.get("title", ""), loc,
                       j.get("shortlink") or j.get("url") or j.get("application_url", ""),
                       posted, days_ago(posted),
                       " ".join([j.get("title", ""), loc,
                                 j.get("department") or "", j.get("function") or "",
                                 strip_html(j.get("description", ""))[:6000]])))
    return out


# SmartRecruiters sorts every listing newest-first, so capping pages keeps the
# freshest rather than an arbitrary slice. Five pages covers the largest tenants
# we track (SGS posts 4,506 reqs; its newest 100 still span only two days).
SR_MAX_PAGES = 5


def fetch_smartrecruiters(tok):
    """SmartRecruiters publishes two APIs and only one of them is usable.

    The cross-company search (jobs.smartrecruiters.com/sr-jobs/search) silently
    ignores every location and paging parameter it is given - `location=Canada`,
    `country=ca`, `filter=country:ca` and `offset=1000` all return the identical
    first ~96 global rows - so it can only ever see a fixed window of the newest
    postings worldwide. It is good for harvesting tenant identifiers and nothing
    else; this per-company endpoint pages correctly and returns full inventory.

    Beware: an unknown tenant returns HTTP 200 with totalFound 0, which is
    byte-identical to a real tenant with no openings. A typo here cannot be
    distinguished from an empty board at fetch time - board_health.py is what
    catches it.
    """
    out, offset = [], 0
    for _ in range(SR_MAX_PAGES):
        d = _req(f"https://api.smartrecruiters.com/v1/companies/{tok}"
                 f"/postings?limit=100&offset={offset}")
        rows = d.get("content") or []
        for j in rows:
            L = j.get("location") or {}
            loc = L.get("fullLocation") or ", ".join(filter(None, [
                L.get("city"), L.get("region"), (L.get("country") or "").upper()]))
            if L.get("remote"):
                loc = f"Remote{' - ' + loc if loc else ''}"
            elif L.get("hybrid") and loc:
                loc = f"Hybrid - {loc}"
            posted = str(j.get("releasedDate") or "")[:10]
            jid = str(j.get("id", ""))
            out.append(job("smartrecruiters", tok, (j.get("name") or "").strip(),
                           loc, f"https://jobs.smartrecruiters.com/{tok}/{jid}",
                           posted, days_ago(posted),
                           " ".join(filter(None, [
                               j.get("name") or "", loc,
                               (j.get("function") or {}).get("label", ""),
                               (j.get("experienceLevel") or {}).get("label", ""),
                               (j.get("typeOfEmployment") or {}).get("label", "")])),
                           hyd=("smartrecruiters", tok, jid)))
        offset += len(rows)
        if not rows or offset >= d.get("totalFound", 0):
            break
    return out


# Taleo behind a Radancy TalentBrew career site (careers.<employer>.ca). Three
# quirks shape this adapter, all of them found the hard way:
#
#   1. The keyword facet needs the term in BOTH the path and the query string.
#      /add/keywords/engineer on its own returns the *unfiltered* board (1,228
#      rows) with Status OK - it looks like it worked. Only
#      /add/keywords/engineer?keywords=engineer actually filters, to 20.
#   2. Adding a facet does not return jobs. It mints a NEW search id, which you
#      then page through; the count comes back in UserMessage.
#   3. Pagination is /jobs/search/<id>/page<n> - no slash before the number.
#      Every ?page=/&offset= form silently returns page 1.
#
# There is no posting date anywhere on this platform: not on the result card,
# not on the job page, not in a meta tag or JSON-LD block. `age` is therefore
# always None. The only freshness signal offered is a "New" badge, which we
# pass through verbatim rather than converting into a day count we cannot
# actually justify.
TALEO_PAGE = 10          # results per page, fixed by the platform
TALEO_MAX_PAGES = 12     # 120 jobs per query is far past anything IT-sized

TALEO_TITLE = re.compile(r'class="job_link[^"]*"[^>]*>(.*?)</a>', re.S)
TALEO_HREF = re.compile(r'href="([^"]+/jobs/[^"]+)"[^>]*class="job_link')
TALEO_LOC = re.compile(r'class="location">\s*(.*?)\s*</span>', re.S)
TALEO_CAT = re.compile(r'class="category">\s*(.*?)\s*</span>', re.S)


def fetch_taleo(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    facet, value = t.get("_query") or ("category", "177")
    enc = urllib.parse.quote(str(value))

    # The keyword facet is the one that needs the redundant query string; the
    # category facet ignores it. Sending it for both is harmless and keeps the
    # call site simple.
    claim = (f"{base}/ajax/jobs/{t['search_id']}/add/{facet}/{enc}"
             f"?keywords={enc}")
    try:
        d = _req(claim)
    except Exception:
        return []
    if (d.get("Status") or "") != "OK":
        return []
    sid = str(d.get("Result") or "")
    if not sid:
        return []
    try:
        total = int(str(d.get("UserMessage", "0")).replace(",", ""))
    except ValueError:
        total = 0
    if not total:
        return []

    out, seen = [], set()
    pages = min(TALEO_MAX_PAGES, -(-total // TALEO_PAGE))
    for pg in range(1, pages + 1):
        url = f"{base}/jobs/search/{sid}" + (f"/page{pg}" if pg > 1 else "")
        try:
            h = _get_text(url)
        except Exception:
            break
        for blk in re.findall(
                r'<div id="job_list_\d+.*?(?=<div id="job_list_|jPaginationHldr)',
                h, re.S):
            ti = TALEO_TITLE.search(blk)
            hr = TALEO_HREF.search(blk)
            if not ti or not hr:
                continue
            href = html_unescape(hr.group(1))
            if href in seen:
                continue
            seen.add(href)
            title = strip_html(ti.group(1)).strip()
            lo = TALEO_LOC.search(blk)
            loc = strip_html(lo.group(1)).strip() if lo else ""
            ca = TALEO_CAT.search(blk)
            cat = strip_html(ca.group(1)).strip() if ca else ""
            desc = re.search(r'class="jlr_description">(.*?)</p>', blk, re.S)
            out.append(job("taleo", name, title, loc, href, "", None,
                           " ".join(filter(None, [
                               title, loc, cat,
                               strip_html(desc.group(1))[:2000] if desc else ""])),
                           updated="New" if "new_flg" in blk else ""))
    return out


# WordPress job plugins cap per_page at 100 and serve newest-first by default,
# so three pages is the whole of both boards we poll (Aplin 301, Raise 308) with
# room to grow. Paging past the end is an HTTP 400, not an empty list.
WP_PER_PAGE = 100
WP_MAX_PAGES = 4

# Matador exposes its taxonomies as bare term IDs in the record, but WordPress
# also emits readable slugs in class_list - `matador-localities-toronto`,
# `matador-regions-ontario`, `matador-workplaces-hybrid`. That is the only place
# the location is human-readable without a second request per taxonomy.
_WP_CLS = re.compile(r"^matador-(localities|regions|countries|workplaces)-(.+)$")


def _wp_location(j):
    """Location, from whichever place this site's plugin keeps it."""
    # WP Job Manager (Aplin): a plain string in post meta.
    loc = ((j.get("meta") or {}).get("_job_location") or "").strip()
    if loc:
        return loc
    # Matador (Raise): rebuild it from the class slugs.
    parts = {}
    for c in j.get("class_list") or []:
        m = _WP_CLS.match(c)
        if m:
            parts.setdefault(m.group(1), m.group(2).replace("-", " ").title())
    loc = ", ".join(filter(None, [parts.get("localities"), parts.get("regions")]))
    if not loc:
        loc = parts.get("countries", "")
    wp = (parts.get("workplaces") or "").lower()
    if wp in ("remote", "hybrid") and wp not in loc.lower():
        loc = f"{wp.title()}{' - ' + loc if loc else ''}"
    return loc


def fetch_wpjobs(t):
    """Staffing agencies run their boards on WordPress, not on a corporate ATS.

    Fingerprinting 36 Canadian IT staffing firms turned up zero Greenhouse,
    Lever, Ashby or Workday boards: agencies live on recruiting CRMs (Bullhorn,
    JobDiva, iCIMS) whose public faces are JS search pages with no feed. The
    exceptions are the ones that publish through a WordPress plugin, because
    WordPress ships a REST API and the plugins register their job post type on
    it. That makes this one adapter worth more than the platform it names.

    Two traps, both of which cost a 404 before they were understood:

    1. The endpoint is the post type's `rest_base`, NOT its name. Aplin's type
       is `job_listing` but its route is `/wp/v2/job-listings`; Raise's is
       `matador-job-listings`. Guessing the type name returns HTTP 404
       `rest_no_route`. Read {base}/wp-json/wp/v2/types to find the real one -
       look for the entry whose key or rest_base mentions job.
    2. Asking for a page past the end is HTTP 400 `rest_post_invalid_page_number`,
       not an empty array, so the paging loop has to treat a hard failure after
       the first page as "that was the end" rather than as a broken board.

    The full description arrives inline in content.rendered, so unlike every
    other adapter here this one needs no hydrate pass.
    """
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    rb = t.get("type", "job-listings")
    out = []
    for page in range(1, WP_MAX_PAGES + 1):
        try:
            rows = _req(f"{base}/wp-json/wp/v2/{rb}"
                        f"?per_page={WP_PER_PAGE}&page={page}"
                        f"&orderby=date&order=desc")
        except Exception:
            if page == 1:
                raise
            break
        if not isinstance(rows, list) or not rows:
            break
        for j in rows:
            title = strip_html((j.get("title") or {}).get("rendered", "")).strip()
            if not title:
                continue
            loc = _wp_location(j)
            posted = str(j.get("date") or "")[:10]
            desc = strip_html((j.get("content") or {}).get("rendered", ""))
            out.append(job("wpjobs", name, title, loc, j.get("link", ""),
                           posted, days_ago(posted),
                           " ".join([title, loc, desc[:8000]]),
                           updated=str(j.get("modified") or "")[:10]))
        if len(rows) < WP_PER_PAGE:
            break
    return out


# amazon.jobs caps result_limit at 100 and pages with offset. Asking for more
# does NOT clamp: result_limit=500 returns {"hits": 0, "jobs": []}, which is
# byte-identical to a country with no openings. Never raise AMZ_PER_PAGE above
# 100. Eight pages covers Amazon Canada (425 reqs on 2026-10-17) with headroom.
AMZ_PER_PAGE = 100
AMZ_MAX_PAGES = 8

# posted_date arrives as "September 25, 2026", and sometimes space-padded to a
# fixed width - "September  1, 2026". strptime("%B %d, %Y") would read it, but
# %B is locale-dependent and cron runs with a minimal environment where LC_TIME
# is not guaranteed to be English, so the date would silently become None on
# exactly the runs that matter. Map the months by hand instead.
_AMZ_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), 1)}
_AMZ_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")


def _amz_date(s):
    """"September 25, 2026" -> "2026-09-25". days_ago() needs ISO."""
    m = _AMZ_DATE.search(s or "")
    if not m:
        return ""
    mon = _AMZ_MONTHS.get(m.group(1).lower())
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}" if mon else ""


def fetch_amazon(t):
    """Amazon runs its own ATS, so no amount of token harvesting reaches it.

    This is the first adapter here for an in-house ATS rather than a third-party
    product, and it exists because of a measured miss: "DevOps Engineer, AFT" in
    Toronto (job 10560828, posted 2026-09-25) scores 16-19, above the notify
    threshold, and the radar never saw it - Amazon was not in targets.json at
    all. It surfaced on LinkedIn instead, as a paid syndication.

    The endpoint is better than most: a real country facet, structured
    city/state/country_code, a genuine posted_date, and the full description
    inline - so like `wpjobs` this needs no hydrate pass.

    Two traps:

    1. result_limit is capped at 100 and exceeding it fails SILENTLY. See
       AMZ_PER_PAGE above.
    2. posted_date is a localised English string, not ISO. See _amz_date.

    `country` is an ISO 3166 alpha-3 facet (CAN), not the alpha-2 used elsewhere
    in this file. `query` is optional and normally empty: the whole country is
    only ~425 reqs, so there is no reason to filter server-side and risk its
    relevance ranking hiding something the local title rules would have kept.
    """
    name = t.get("name", "Amazon")
    country = t.get("country", "CAN")
    query = t.get("query", "")
    out, seen = [], set()
    for page in range(AMZ_MAX_PAGES):
        d = _req("https://www.amazon.jobs/en/search.json"
                 f"?base_query={urllib.parse.quote(query)}&loc_query="
                 f"&country={urllib.parse.quote(country)}"
                 f"&result_limit={AMZ_PER_PAGE}&offset={page * AMZ_PER_PAGE}")
        rows = d.get("jobs") or []
        if not rows:
            break
        for j in rows:
            path = j.get("job_path") or ""
            jid = str(j.get("id") or j.get("id_icims") or path)
            title = (j.get("title") or "").strip()
            if not path or not title or jid in seen:
                continue
            seen.add(jid)
            # city/state are the structured pair; normalized_location is the
            # display string and is the only one populated for virtual reqs.
            loc = (", ".join(filter(None, [j.get("city"), j.get("state")]))
                   or j.get("normalized_location") or j.get("location") or "")
            posted = _amz_date(j.get("posted_date"))
            desc = " ".join(strip_html(j.get(k) or "") for k in
                            ("description", "basic_qualifications",
                             "preferred_qualifications"))
            # `team` is an object, not a string - its readable part is
            # `label`. Joining it raw raises TypeError on the first posting.
            team = (j.get("team") or {}).get("label") or ""
            out.append(job("amazon", name, title, loc,
                           "https://www.amazon.jobs" + path,
                           posted, days_ago(posted),
                           " ".join([title, loc, team,
                                     j.get("job_category") or "",
                                     j.get("job_family") or "", desc[:8000]])))
        if len(rows) < AMZ_PER_PAGE:
            break
    return out


# Insight Global - a staffing agency running its own job board, not an ATS
# product, so harvest.py and discover.py --company can never fingerprint it.
#
# `size` is CAPPED AT 50 and silently clamps: ask for 500 and the response says
# `"size": 50`. Reading pageMetadata.totalPages rather than trusting the request
# is what keeps paging honest here.
IG_BASE = "https://insightglobal.com"
IG_PER_PAGE = 50
IG_MAX_PAGES = 14          # 700 postings per province; ON, the busiest, is 192

# Province codes, used as the server-side location facet (see fetch_insightglobal).
# Not one of these collides with a US state code - Nebraska is NE, Pennsylvania
# is PA - which is what makes the facet safe to drive from this list.
IG_PROVINCES = ("ON", "BC", "AB", "QC", "MB", "SK", "NS", "NB",
                "NL", "PE", "YT", "NT", "NU")


def fetch_insightglobal(t):
    """Enumerate the agency's Canadian reqs by province, not by keyword.

    Two findings shaped this, both measured against the live API:

    1. KEYWORD SEARCH IS THE WRONG AXIS HERE. The endpoint ORs loosely, so
       `platform engineer` reports 1,486 results and `systems engineer` 1,567 -
       mostly US - while the *entire* Canadian inventory is 282 reqs. Running
       our 13 standard queries cost 78 requests, truncated most of them, and
       still found only 60 Canadian rows. Enumerating the 13 provinces costs
       ~11 requests, returns all 282, and lets TITLE_RULES do the filtering it
       already does better than a remote relevance ranker. Fewer requests,
       complete recall, no truncation register needed.

    2. `administrativeArea` IS NOT COUNTRY-SAFE. In this data `CA` means
       California - 22 US cities carried it - and the feed also contains Indian
       state codes (TG, KA, MH, AN) and spelled-out Brazilian states. So the
       country filter MUST be the server-side `filter.locations` facet, which is
       exact: `ON` returns Ontario only, with no California leakage. Pattern
       matching that field locally would have swept 22 California jobs in as
       Canadian. classify_location() reads `San Diego, CA` as OTHER and would
       have caught most of it downstream, but the right place to be correct is
       here, before 282 rows become 500.

    The list call carries no description - only the per-req summary endpoint
    does - so this sets `hyd` and the full JD arrives in pass 2 for finalists
    only, the same bargain greenhouse and ashby make.

    `company` is the agency, not the client: these are placements and the client
    is usually withheld ("A leading home healthcare organization"), so naming
    Insight Global is the only honest label available.
    """
    name = t.get("name", "Insight Global")
    provinces = t.get("provinces") or IG_PROVINCES
    out, seen = [], set()
    for prov in provinces:
        for page in range(1, IG_MAX_PAGES + 1):
            try:
                d = _req(f"{IG_BASE}/all/jobs?size={IG_PER_PAGE}&page={page}"
                         f"&filter.status=active&filter.locations={prov}")
            except Exception:
                break
            rows = d.get("jobs") or []
            if not rows:
                break
            for r in rows:
                rid = r.get("requisitionId")
                title = (r.get("jobTitle") or "").strip()
                if not rid or not title or rid in seen:
                    continue
                seen.add(rid)
                addr = r.get("workAddress") or {}
                loc = ", ".join(x for x in (addr.get("locality"),
                                            addr.get("administrativeArea")) if x)
                # workRemote is a real boolean here, and the location string is
                # the city regardless - so without this, a remote req reads as
                # on-site in Toronto and arrangement() never sees "remote".
                if r.get("workRemote"):
                    loc = f"{loc} / Remote" if loc else "Remote"
                posted = str(r.get("postedDate") or "")[:10]
                # Contract vs Perm is the single most useful thing the list call
                # knows that the title does not, and it is worth scoring on.
                blob = " ".join([title, loc, r.get("jobType") or ""])
                out.append(job("insightglobal", name, title, loc,
                               f"{IG_BASE}/jobs/{rid}", posted, days_ago(posted),
                               blob, hyd=("insightglobal", rid)))
            meta = d.get("pageMetadata") or {}
            if page >= (meta.get("totalPages") or page):
                break
    return out


ADAPTERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "recruitee": fetch_recruitee,
            "rippling": fetch_rippling, "workday": fetch_workday,
            "phenom": fetch_phenom, "successfactors": fetch_successfactors,
            "oracle": fetch_oracle, "jobvite": fetch_jobvite,
            "html": fetch_html, "workable": fetch_workable,
            "smartrecruiters": fetch_smartrecruiters,
            "taleo": fetch_taleo, "wpjobs": fetch_wpjobs,
            "amazon": fetch_amazon, "avature": fetch_avature,
            "teamtailor": fetch_teamtailor, "ultipro": fetch_ultipro,
            "jobbank": fetch_jobbank, "insightglobal": fetch_insightglobal}


# ---------------------------------------- pass 2: hydrate only the finalists --
# Pulling full descriptions for ~12,000 jobs would move ~80MB. We pull them only
# for the few hundred that already passed the title + location filter.

# On JobPostingDetails the description field is descriptionHtml (not
# descriptionPlainText) and publishedDate IS available - this is where Ashby
# dates come from.
ASHBY_ONE = ("query ApiJobPosting($organizationHostedJobsPageName: String!, "
             "$jobPostingId: String!) { jobPosting("
             "organizationHostedJobsPageName: $organizationHostedJobsPageName, "
             "jobPostingId: $jobPostingId) { descriptionHtml publishedDate } }")


def hydrate(j):
    kind = j.get("hyd")
    if not kind:
        return j
    try:
        if kind[0] == "greenhouse":
            _, tok, jid = kind
            d = _req(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs/{jid}")
            j["blob"] += " " + strip_html(d.get("content", ""))[:8000]
            j["offices"] = [o.get("name", "") for o in (d.get("offices") or [])]
        elif kind[0] == "workday":
            d = _req(kind[1])
            ji = d.get("jobPostingInfo") or {}
            j["blob"] += " " + strip_html(ji.get("jobDescription", ""))[:8000]
            if WD_NLOC.match(j.get("location") or ""):
                locs = [ji.get("location") or ""]
                locs += [x for x in (ji.get("additionalLocations") or []) if x]
                real = " / ".join(dict.fromkeys(x for x in locs if x))
                if real:
                    j["location"] = real
                    bucket, keep = classify_location(real)
                    j["bucket"], j["geo_keep"] = bucket, keep
                    j["provinces"] = sorted(provinces_of(real))
                    j["arrangement"] = arrangement(real, j.get("blob", ""))
            if ji.get("startDate"):
                j["posted"] = ji["startDate"]
                exact = days_ago(ji["startDate"])
                if exact is not None:
                    j["age"] = exact
        elif kind[0] == "smartrecruiters":
            _, tok, jid = kind
            d = _req(f"https://api.smartrecruiters.com/v1/companies/{tok}"
                     f"/postings/{jid}")
            secs = (d.get("jobAd") or {}).get("sections") or {}
            j["blob"] += " " + strip_html(" ".join(
                (secs.get(k) or {}).get("text", "") for k in
                ("jobDescription", "qualifications", "additionalInformation")
            ))[:8000]
        elif kind[0] == "phenom":
            # Phenom was the only platform with no hydrate branch, so every
            # Phenom row scored on the listing page's teaser - about 400
            # characters - and matched zero resume keywords. Five boards were
            # affected, City of Edmonton and Bell among them. The job page
            # carries the full text twice: in the phApp.ddo blob and in a
            # schema.org JobPosting block. Prefer the blob, fall back to JSON-LD.
            h = _get_text(kind[1])
            desc = ""
            m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){200,})"', h)
            if m:
                try:
                    desc = json.loads(f'"{m.group(1)}"')
                except Exception:
                    desc = m.group(1)
            if not desc:
                for mm in re.finditer(
                        r'application/ld\+json[^>]*>(.*?)</script>', h, re.S):
                    try:
                        d = json.loads(mm.group(1))
                        d = d[0] if isinstance(d, list) else d
                        if d.get("@type") == "JobPosting" and d.get("description"):
                            desc = d["description"]
                            break
                    except Exception:
                        continue
            j["blob"] += " " + strip_html(desc)[:8000]
        elif kind[0] == "jobvite":
            h = _get_text(kind[1])
            m = JV_DATE.search(h)
            if m:
                j["posted"] = m.group(1)
                j["age"] = days_ago(m.group(1))
            body = re.search(r'class="jv-job-detail-description".*?</div>', h, re.S)
            j["blob"] += " " + strip_html(body.group(0) if body else h)[:8000]
        elif kind[0] == "avature":
            # No date to recover here - unlike Jobvite, the posting page has no
            # datePosted either - so this pass only enriches the blob. The
            # description lives in article__content__view; fall back to the
            # whole page rather than returning nothing if the class is renamed.
            h = _get_text(kind[1])
            m = re.search(r'class="article__content__view"[^>]*>(.*)', h, re.S)
            j["blob"] += " " + strip_html(m.group(1) if m else h)[:8000]
        elif kind[0] == "jobbank":
            # Same 5s courtesy as the list pass - the delay is per host, so the
            # hydrate pass shares the limiter rather than having its own.
            _jb_wait()
            h = _get_text(kind[1])
            m = re.search(r'class="job-posting-detail-requirements".*', h, re.S) or \
                re.search(r'id="jobDescription".*', h, re.S)
            j["blob"] += " " + strip_html(m.group(0) if m else h)[:8000]
        elif kind[0] == "ultipro":
            # The list call gives only a teaser. The detail page embeds the real
            # posting in a JSON "Description" string, so decode it as JSON
            # rather than hand-unescaping, then strip the HTML inside it.
            h = _get_text(kind[1])
            m = UKG_DESC.search(h)
            body = h
            if m:
                try:
                    body = json.loads(m.group(1))
                except Exception:
                    body = m.group(1)
            j["blob"] += " " + strip_html(body)[:8000]
        elif kind[0] == "successfactors":
            h = _get_text(kind[1])
            m = re.search(r'<div[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)'
                          r'</div>\s*(?:<div[^>]*class="[^"]*(?:jobbottom|clear))',
                          h, re.S | re.I)
            body = m.group(1) if m else h
            j["blob"] += " " + strip_html(body)[:8000]
        elif kind[0] == "insightglobal":
            # The SUMMARY endpoint already carries descriptionHtml, so there is
            # no reason to fetch /details as well - it returns the same text
            # under a different key. One request per finalist, no HTML scraping.
            d = _req(f"{IG_BASE}/all/jobs/{kind[1]}")
            j["blob"] += " " + strip_html(d.get("descriptionHtml") or "")[:8000]
        elif kind[0] == "ashby":
            _, tok, jid = kind
            _ashby_wait()
            d = _req("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
                     {"operationName": "ApiJobPosting",
                      "variables": {"organizationHostedJobsPageName": tok,
                                    "jobPostingId": jid},
                      "query": ASHBY_ONE})
            jp = (d.get("data") or {}).get("jobPosting") or {}
            j["blob"] += " " + strip_html(jp.get("descriptionHtml") or "")[:8000]
            if jp.get("publishedDate"):
                j["posted"] = jp["publishedDate"]
                j["age"] = days_ago(jp["publishedDate"])
    except Exception:
        pass
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


def append_to_tracker(jobs, path, min_score):
    """Append new high-scorers. Never rewrites existing rows or your notes."""
    if not os.path.exists(path):
        body = TRACKER_HEADER + TRACKER_FOOTER
    else:
        body = open(path).read()

    seen_urls = set(re.findall(r"https?://[^\s)\]]+", body))
    try:
        conn = db()
        seen_urls |= {r[0] for r in conn.execute("SELECT url FROM dismissed")}
        conn.close()
    except Exception:
        pass
    nums = [int(n) for n in re.findall(r"(?m)^\|\s*(\d+)\s*\|", body)]
    nxt = max(nums) + 1 if nums else 1

    todo = [j for j in jobs
            if j["score"] >= min_score and j["url"].rstrip(".,)") not in seen_urls]
    if not todo:
        return 0, nxt

    todo.sort(key=lambda j: -j["score"])
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
    return len(todo), nxt


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
        req = urllib.request.Request(
            "https://ntfy.sh/",
            data=json.dumps({"topic": topic,
                             "title": f"[{j['score']}] {j['title'][:80]}",
                             "message": body,
                             "priority": {"urgent": 5, "high": 4,
                                          "default": 3}[prio],
                             "tags": [tag],
                             "click": j["url"]}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": UA["User-Agent"]},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=12)
            sent += 1
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
            urllib.request.urlopen(urllib.request.Request(
                "https://ntfy.sh/",
                data=json.dumps({"topic": topic,
                                 "title": f"+{len(rest)} more matches",
                                 "message": body, "priority": 2,
                                 "tags": ["mag"]}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": UA["User-Agent"]}, method="POST"),
                timeout=12)
            sent += 1
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

    tasks = []
    for src, fn in ADAPTERS.items():
        for tok in tg.get(src, []):
            if src == "workday":
                for q in WD_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "successfactors":
                for q in SF_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "oracle":
                for q in ORC_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "jobvite":
                for q in JV_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "avature":
                for q in AV_QUERIES:
                    tasks.append((src, {**tok, "_query": q}, fn))
            elif src == "taleo":
                for q in tok.get("queries") or [["category", "177"]]:
                    tasks.append((src, {**tok, "_query": tuple(q)}, fn))
            else:
                tasks.append((src, tok, fn))

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
        conn.executemany("INSERT INTO live(url,last_seen) VALUES(?,?) "
                         "ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen",
                         [(j["url"], now) for j in jobs])
        conn.execute("INSERT INTO sweeps(ts) VALUES(?)", (now,))
        conn.execute("DELETE FROM sweeps WHERE ts NOT IN "
                     "(SELECT ts FROM sweeps ORDER BY ts DESC LIMIT 50)")
    # Claiming a posting must be atomic. The fast lane and the full sweep can
    # run in the same minute; when both read `seen` before either wrote, they
    # each decided the same job was new and both pushed a notification. Insert
    # first and let the row count decide - only the writer that actually
    # inserted owns it.
    fresh = []
    for j in hits:
        cur = conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?)",
                           (j["url"], j["title"], j["company"], now))
        if cur.rowcount:
            fresh.append(j)
    conn.commit()

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
            if dropped_geo else "") + "_", ""]

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

    if a.track:
        with FileLock():
            n, _ = append_to_tracker(show, a.track_file, a.track_min_score)
        rel = os.path.relpath(a.track_file, os.getcwd())
        if rel.count("..") > 1:
            rel = a.track_file
        if n:
            print(f"\n>> Tracked {n} new posting(s) (score >= {a.track_min_score}) "
                  f"in {rel}")
        else:
            print(f"\n>> Nothing new to track (score >= {a.track_min_score}); "
                  f"{rel} already covers what was shown.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
