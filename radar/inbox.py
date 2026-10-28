#!/usr/bin/env python3
"""
inbox.py - harvest job-alert emails, including from sites that block scraping.

Every employer that refuses automated requests will still happily email you.
Njoyn behind its bot manager, Taleo, Dayforce, Alberta Health Services, the City
of Edmonton, LinkedIn itself - none of them can block their own notification
email. Subscribe to their alerts with a dedicated address, and this reads them.

This is the only channel that reaches the employers in manual_check.md.

It harvests two different things from the same mail, and the second is worth
more than the first. `--boards` reads the account token out of every posting
link and reports the ATS boards that are not in targets.json yet - one alert
email about one job hands over that employer's entire board. Unlike every other
expansion channel it is demand-driven: the companies that turn up are the ones
already mailing you about your titles in your geography, which is the opposite
of sweeping 425 Amazon reqs to find one match.

    python3 inbox.py --setup            # write a config template
    python3 inbox.py --creds             # fill it in without echoing the password
    python3 inbox.py                    # parse new alert mail
    python3 inbox.py --days 7 --all     # re-read recent mail, ignore seen-state
    python3 inbox.py --md inbox.md --track
    python3 inbox.py --boards            # report ATS boards found in the mail
    python3 inbox.py --boards --add-boards   # ...and append the live ones

Config lives in inbox.json (gitignored) or these environment variables:

    RADAR_IMAP_HOST   imap.gmail.com
    RADAR_IMAP_USER   your.alerts@gmail.com
    RADAR_IMAP_PASS   an APP PASSWORD, never your real password
    RADAR_IMAP_FOLDER INBOX

Use a dedicated address and an app password. For Gmail that means enabling
2FA then creating an app password; the tool only ever reads mail.

Standard library only.
"""

import argparse
import collections
import email
import email.policy
import getpass
import html as htmlmod
import imaplib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import job_radar as jr  # noqa: E402
import discover as dc  # noqa: E402

CONFIG = os.path.join(HERE, "inbox.json")

TEMPLATE = {
    "host": "imap.gmail.com",
    "user": "your.alerts@example.com",
    "password": "APP PASSWORD - not your account password",
    "folder": "INBOX",
    "_note": "This file is gitignored. Prefer RADAR_IMAP_* environment "
             "variables if you would rather not have it on disk at all.",
}

# Links that look like an actual posting rather than an unsubscribe footer.
JOB_LINK = re.compile(
    r"(job-boards\.greenhouse\.io|boards\.greenhouse\.io|jobs\.lever\.co|"
    r"jobs\.ashbyhq\.com|myworkdayjobs\.com|\.icims\.com|taleo\.net|"
    r"njoyn\.com|dayforcehcm\.com|successfactors|linkedin\.com/jobs|"
    # Job Bank is the federal posting board, and the one source here that is
    # neither an ATS product nor an aggregator. Its posting path is
    # /jobsearch/jobposting/{id}, which slips past every generic rule on this
    # line: "/jobs/" does not match "/jobsearch/", and the URL contains no
    # "/job/", "jobid=" or "requisition" anywhere. Without naming it the
    # extractor returns zero postings from a Job Bank alert while looking like
    # it parsed the mail fine - a silent miss, not an error.
    r"jobbank\.gc\.ca/jobsearch/jobposting|"
    r"indeed\.com/(viewjob|rc/clk)|/job/|/jobs/|/careers?/|jobid=|requisition)",
    re.I)
SKIP_LINK = re.compile(
    r"(unsubscribe|optout|opt-out|preferences|privacy|terms|"
    r"facebook\.com|twitter\.com|x\.com|instagram\.com|youtube\.com|"
    r"/help|/support|manage-alerts|email-settings)", re.I)
ANCHOR = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)


def load_config():
    env = {
        "host": os.environ.get("RADAR_IMAP_HOST"),
        "user": os.environ.get("RADAR_IMAP_USER"),
        "password": os.environ.get("RADAR_IMAP_PASS"),
        "folder": os.environ.get("RADAR_IMAP_FOLDER", "INBOX"),
    }
    if env["host"] and env["user"] and env["password"]:
        return env
    if os.path.exists(CONFIG):
        c = json.load(open(CONFIG))
        if "APP PASSWORD - not" in str(c.get("password", "")):
            print("  inbox.json is still the template - fill in your details.",
                  file=sys.stderr)
            return None
        return c
    return None


def strip_html(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h or "")
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmlmod.unescape(h)).strip()


def body_parts(msg):
    """Return (html, text) for a message, preferring the richest parts."""
    h = t = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if part.get_filename():
                continue
            try:
                payload = part.get_content()
            except Exception:
                continue
            if ct == "text/html" and not h:
                h = payload
            elif ct == "text/plain" and not t:
                t = payload
    else:
        try:
            payload = msg.get_content()
        except Exception:
            payload = ""
        (h, t) = (payload, "") if msg.get_content_type() == "text/html" else ("", payload)
    return h, t


# LinkedIn's own posting URL. Both /jobs/view/{id} and the email-tracking
# /comm/jobs/view/{id} form appear, often in the same message, and the tracking
# form carries ~900 characters of otpToken/midToken/midSig that are single-use
# and tied to the recipient. Those must never reach the tracker, so every
# LinkedIn posting is canonicalised to the bare id.
LI_VIEW = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")

# A digest ends in account boilerplate, and the last job's segment otherwise
# swallows all of it - 31,562 characters in one measured message, which is how
# "Install LinkedIn Widgets" ended up in a job's keyword blob.
LI_STOP = re.compile(
    r"(See all jobs|New jobs from your other alerts|Edit alert"
    r"|Install LinkedIn Widgets|This email was intended for"
    r"|Unsubscribe|You are receiving|Also Available on"
    r"|Get the new LinkedIn)", re.I)

# "Lakeview Loan Servicing, LLC. · North York, ON (Remote)" - the one piece
# of per-job structure a digest reliably carries, and the only place a job's own
# company and city appear. Every real job row in both sampled digest formats has
# it; the digest's own "Jobs similar to X at Y" header does not, which is what
# makes it a usable filter for telling rows from headers.
LI_META = re.compile(r"(?P<company>[^·|\n]{2,70}?)\s*·\s*"
                     r"(?P<loc>[^·|\n(]{2,70})")

JUNK_ANCHOR = frozenset((
    "view job", "apply now", "see job", "view", "apply", "learn more",
    "click here", "view all jobs", "see all jobs", "view jobs", "see more jobs",
    "unsubscribe", "edit alert", "see all", "more jobs"))

# Anchors that are chrome but are not a fixed string, so the set above cannot
# hold them. Each of these was observed sitting inside a real job's segment and
# being chosen as that job's TITLE: "Learn why we included this." is the
# recommendation footnote LinkedIn renders under a suggested req, "DevOps jobs"
# is the digest's own section heading, and the widget promo runs on into the
# following text once the HTML is flattened. Six records in a fortnight of mail
# were filed under one of these instead of their real title, which also cost
# them their employer name, since the company is recovered by subtracting the
# title off the row.
JUNK_RE = re.compile(r"^(?:learn why we included this|install linkedin widgets"
                     r"|stay updated at a glance|manage (?:recommendations"
                     r"|job alerts|alerts|settings)|(?:dev\s*ops|devops|sre|cloud"
                     r"|engineering|software)\s+jobs\b|jobs?\s+(?:you|similar)"
                     r"|see\s|view\s|apply\s|unsubscribe|get the new linkedin)",
                     re.I)

# The employer's logo carries its name in the img alt, inside that job's own
# segment - "ElevenLabs", "KData AI", "Smile Digital Health". It is the only
# place in a digest where the company appears as a FIELD rather than glued to
# the title with no delimiter, which is what made it unrecoverable in the
# single-anchor format. Measured over 349 records from 65 digests: it agrees
# with the subtraction rule on all 305 the rule already got right (differing
# only by a trailing period it preserves) and supplies a name for 43 of the 44
# it left empty. Used as a FALLBACK rather than a replacement for exactly that
# reason - the rule it backs up is not wrong, just incomplete.
LI_ALT = re.compile(r'<img\b[^>]*\balt="([^"]{2,70})"', re.I)
LI_ALT_CHROME = re.compile(
    r"^(?:linkedin|.*\bicon|easy apply|promoted|viewed|applied|logo"
    r"|company logo|profile photo|actively recruiting|.*\bwidget)$", re.I)


def _alt_company(frag):
    """Employer name from the logo alt in one segment, or ""."""
    for a in LI_ALT.findall(frag):
        a = htmlmod.unescape(a).strip()
        if a and not LI_ALT_CHROME.match(a):
            return a.strip(" .,·")
    return ""


LI_VIEW_HREF = re.compile(r'<a\b[^>]*href="([^"]*)"')

# LinkedIn pads a message with blocks it chose itself, under their own
# headings, and they are NOT the alerts you subscribed to. The application
# confirmation for J&M Group carried the job applied to, then "similar jobs you
# may be interested in", then three more - Keyfactor, Borrowell and CARET, all
# of which reached the digest as if they were alerts you had asked for, and
# which also carried the oldest job ids in the batch. They are labelled, not
# dropped - see _reco_ids.
#
# "Jobs similar to <title> at <company>" is deliberately NOT here: that one is
# a digest's own HEADER and sits at the top, so keying on it would relabel every
# row in the message. _reco_ids guards against that anyway by requiring a job
# anchor before the heading, but naming the reason is cheaper than
# rediscovering it.
LI_RECO = re.compile(
    r"similar\s+jobs\s+you\s+may\s+be\s+interested\s+in"
    r"|people\s+also\s+viewed"
    r"|more\s+jobs\s+for\s+you", re.I)

# "Applied on October 28, 2026" - LinkedIn says so on the job you just applied
# to. Only the dated form is matched: a bare "Applied" appears as a card badge
# but also inside prose ("Applied Sciences", "applied research"), and dropping
# a real posting is worse than letting one duplicate through - which the
# tracker check in main() catches anyway.
LI_APPLIED = re.compile(r"\bApplied\s+on\s+\w+\s+\d{1,2},\s*\d{4}", re.I)


def _reco_ids(html):
    """Job ids that appear ONLY under a recommendation heading.

    These are LABELLED, never removed. An earlier draft truncated the message
    at the heading, which would have been a coverage regression: the three jobs
    below J&M Group's "similar jobs" heading were a Keyfactor, a Borrowell and
    a CARET DevOps role in Toronto - real postings worth reading, just not ones
    that arrived because you subscribed to them. Losing a real job to cut noise
    is the wrong trade; the digest keeps everything and the label says where it
    came from.

    Refuses to treat anything as a recommendation unless a job anchor appears
    BEFORE the heading, so a digest whose own header matches can never have
    every row relabelled.
    """
    h = html or ""
    m = LI_RECO.search(h)
    if not m:
        return set()
    ids = lambda t: {a.group(1) for x in LI_VIEW_HREF.finditer(t)
                     for a in [LI_VIEW.search(x.group(1))] if a}
    before = ids(h[:m.start()])
    if not before:
        return set()
    return ids(h[m.start():]) - before


def li_segments(html):
    """Split a LinkedIn digest into one HTML fragment per posting.

    Each job is rendered as consecutive anchors sharing a single job id - a logo
    link with empty text, then the title. Grouping on that id is the whole fix:
    without it the message is one document, and scoring eight jobs against a
    shared body gave all eight the identical keyword set pooled from each other
    and every one of them the first job's city.
    """
    marks = []
    for m in LI_VIEW_HREF.finditer(html or ""):
        j = LI_VIEW.search(m.group(1))
        if j and (not marks or marks[-1][1] != j.group(1)):
            marks.append((m.start(), j.group(1)))
    return [(jid, html[off:(marks[i + 1][0] if i + 1 < len(marks) else len(html))])
            for i, (off, jid) in enumerate(marks)]


# The location ends a table cell, and the badges that follow it ("Actively
# recruiting", "13 school alumni", "View all jobs") land on the same line once
# the HTML is flattened to text.
LI_LOC_NOISE = re.compile(
    r"\s+(?:Actively recruiting|Easy Apply|Promoted|Viewed|Applied"
    r"|Be an early applicant|View all jobs|See all jobs|Your profile matches"
    r"|\d+\s+(?:school alumni|alumni|connections?|applicants?))\b.*$", re.I)

# Preferred over the trimmed cell whenever it matches: a province code is an
# unambiguous end-of-location marker, and a list of badge names never will be -
# the next LinkedIn redesign adds one more badge, not one more province.
LI_CITY = re.compile(
    r"([A-Z][\w.'\-]*(?:[\s\-][A-Z][\w.'\-]*){0,3}),\s*"
    r"(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b")


def _clean_loc(cell):
    m = LI_CITY.search(cell)
    if m:
        return f"{m.group(1)}, {m.group(2)}"
    return LI_LOC_NOISE.sub("", cell).strip(" .,·")


def _title_from(frag, jid=None):
    """Best anchor text in a fragment to use as the job title.

    Two digest formats are in circulation. One gives the title its own anchor
    ("Infrastructure Engineer") beside a logo link with empty text; the other
    puts the entire row in a single anchor ("DevOps Engineer - New Grad 2026
    Cerebras · Toronto, ON (Hybrid)"). Prefer an anchor with no middle dot,
    since that is the clean title, and fall back to the whole row for the
    caller to trim.

    When the segment's job id is known, only anchors pointing at THAT posting
    are considered. This is structural where a word list is not: the last
    segment of a digest also contains the account footer, whose anchors are
    "Help", "Manage recommendations", "Unsubscribe", "Install LinkedIn
    Widgets" and whatever LinkedIn adds next, and every one of them is a
    plausible-looking 4-to-140-character string. Blocklisting them by name
    fixed six rows and immediately mis-titled eight more as "Help"; matching
    on the href cannot be outrun that way, because a footer link never points
    at the job.
    """
    best = ""
    for href, anchor in ANCHOR.findall(frag):
        if jid:
            m = LI_VIEW.search(htmlmod.unescape(href))
            if not m or m.group(1) != jid:
                continue
        t = strip_html(anchor)
        if not t or not (4 <= len(t) <= 140) or t.lower() in JUNK_ANCHOR:
            continue
        if JUNK_RE.match(t):
            continue
        if "·" not in t:
            return t
        best = best or t
    return best


def _li_record(jid, frag):
    title = _title_from(frag, jid)
    if not title:
        return None
    blob = strip_html(frag)
    cut = LI_STOP.search(blob)
    if cut:
        blob = blob[:cut.start()]
    m = LI_META.search(blob)
    if not m:
        # No "Company - City" pair means this is the digest's own header
        # ("Jobs similar to Infrastructure Engineer at ElevenLabs"), which links
        # to a real job id but carries no row. Dropping it is deliberate: a lost
        # row is recoverable, an invented one pollutes the tracker.
        return None
    # m.group("company") is everything before the middle dot, which is
    # "<Title> <Company>" with no delimiter between them. Where the title came
    # from its own anchor it can be subtracted off; in the single-anchor format
    # it cannot, so the company is left empty rather than guessed at. An empty
    # column is a known gap; a wrong company name is a wrong application.
    row = m.group("company").strip(" .·")
    title = title.split("·")[0].strip()
    company = ""
    if title and row.lower().startswith(title.lower()) and len(row) > len(title):
        company = row[len(title):].strip(" .,·-")
    if not company:
        company = _alt_company(frag)
    # Subtraction the other way round, for the rows where every anchor in the
    # segment was chrome. The row is still "<Title><Company>" with no
    # delimiter, and once the logo has supplied the company the title is
    # whatever precedes it - which is how adidas' "Platform & Site Reliability
    # Engineer", BDO's "DevOps Engineer - New Grad (January 2027)" and six
    # others stopped being filed under "Learn why we included this." and
    # started being filed under their real titles.
    if not title and company and row.lower().rstrip(" .").endswith(
            company.lower().rstrip(" .")):
        title = row[:len(row.rstrip(" .")) - len(company.rstrip(" ."))].strip(" .,·-")
    if not title:
        title = row
    if not title:
        return None
    # The row formats glue the company onto the end of the title with no
    # delimiter ("Site Reliability Engineer MaintainX"). Once the logo has
    # named the company it can simply be cut off, which it could not be while
    # the name was unknown - and a title carrying the employer's name scores
    # against the wrong keyword blob and reads wrong in the tracker.
    if company and len(title) > len(company) + 2:
        trimmed = title.rstrip(" .,·-")
        if trimmed.lower().endswith(company.lower().rstrip(" .")):
            cut = trimmed[:len(trimmed) - len(company.rstrip(" ."))].strip(" .,·-")
            if len(cut) >= 4:
                title = cut
    return {"title": title,
            "url": f"https://www.linkedin.com/jobs/view/{jid}",
            "company": company,
            "location": _clean_loc(m.group("loc")),
            "blob": blob,
            # LinkedIn mails a confirmation when you apply, and it renders the
            # job you applied to exactly like an alert row. One of those was
            # pushed to the phone as a new role at score 14 while the tracker
            # already held it as `applied` from the day before.
            "applied": bool(LI_APPLIED.search(blob))}


def _window(html, pos, before=300, after=1500):
    """Text around one anchor, for alert formats with no per-job structure.

    Not the whole body. A bounded window still pools a little from neighbouring
    postings, but it cannot give every job in an eight-job digest the same ten
    keywords, which is what the whole-body blob did.
    """
    return strip_html(html[max(0, pos - before):pos + after])


def extract(msg):
    """Pull candidate postings out of one alert email.

    Each returned record carries its OWN blob and location rather than the
    message's, which is the difference between a score and a number. Three
    strategies, most structured first:

      1. LinkedIn digests, segmented per job id. By far the common case.
      2. Any other HTML alert: anchors that look like postings, each scored
         against a window of surrounding text.
      3. Plain-text alerts (Job Bank, some government systems) have no anchors
         at all, so one line is one posting.
    """
    h, t = body_parts(msg)
    sender = str(msg.get("From", ""))
    subject = str(msg.get("Subject", ""))
    dom = re.search(r"@([\w.-]+)", sender)
    source = (dom.group(1) if dom else "email").replace("www.", "")

    out, seen = [], set()

    reco = _reco_ids(h or "")
    for jid, frag in li_segments(h or ""):
        if jid in seen:
            continue
        rec = _li_record(jid, frag)
        if rec:
            seen.add(jid)
            rec["reco"] = jid in reco
            out.append(rec)
    if out:
        return source, subject, out

    for m in ANCHOR.finditer(h or ""):
        href = htmlmod.unescape(m.group(1)).strip()
        title = strip_html(m.group(2))
        if not href.startswith("http") or SKIP_LINK.search(href):
            continue
        if not JOB_LINK.search(href):
            continue
        if not title or len(title) < 4 or len(title) > 140:
            continue
        if title.lower() in JUNK_ANCHOR:
            continue
        key = href.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        blob = _window(h, m.start())
        out.append({"title": title, "url": href, "company": "",
                    "location": "", "blob": blob})

    if not out and t:
        for line in t.split("\n"):
            m = re.search(r"https?://\S+", line)
            if m and JOB_LINK.search(m.group(0)) and not SKIP_LINK.search(m.group(0)):
                title = line.replace(m.group(0), "").strip(" -–—•\t")
                if 4 < len(title) < 140:
                    # The title is left as the line gave it, including any city
                    # it names. Cutting a location out of a title needs a real
                    # sample of each sender's format to do safely, and a noisy
                    # title is recoverable where a truncated one is not.
                    out.append({"title": title, "url": m.group(0), "company": "",
                                "location": _clean_loc(line), "blob": line})
    return source, subject, out


def all_job_links(msg):
    """Every posting-shaped link in one message, for board extraction.

    Deliberately not reusing extract(): that one keeps only links whose anchor
    text passes title_match, which is right for postings and wrong here. A board
    is worth adding even when this particular email is about a Sales role - the
    point is the employer's whole board, and the title filter runs later against
    everything it returns. Dropping Netflix because today's alert was for a
    recruiter req would be the expensive mistake.
    """
    h, t = body_parts(msg)
    out = set()
    for href, _anchor in ANCHOR.findall(h or ""):
        href = htmlmod.unescape(href).strip()
        if href.startswith("http") and not SKIP_LINK.search(href):
            out.add(href)
    for m in re.finditer(r"https?://[^\s<>\"\')]+", t or ""):
        u = m.group(0).rstrip(".,;)]")
        if not SKIP_LINK.search(u):
            out.add(u)
    return out


def report_boards(links, targets_path, add=False, workers=12):
    """Turn posting links into targets.json entries. Returns the markdown report.

    Every candidate is probed before it is reported as addable. An unprobed
    token is not a board: recruitee postings in particular carry the account's
    own vanity subdomain rather than the API token (facebookdata.recruitee.com
    is served by a different token entirely), and greenhouse returns canonical
    URLs for boards polled under an alias. Probing is what separates those from
    a genuine new board, so --add-boards never writes anything that returned
    zero.
    """
    tg = json.load(open(targets_path))
    have = {p: {v for v in tg.get(p, []) if isinstance(v, str)}
            for p in ("greenhouse", "lever", "ashby", "recruitee", "rippling",
                      "smartrecruiters", "jobvite", "workable")}
    have_wd = {(x.get("tenant"), x.get("site")) for x in tg.get("workday", [])
               if isinstance(x, dict)}

    cands, partial, unknown = {}, {}, set()
    for u in sorted(links):
        platform, target, note = dc.board_from_url(u)
        if not platform:
            # "greenhouse, but the account is not in the URL" is a lead worth
            # one request; "no ATS signature" is usually a footer or a content
            # link and is counted but not reported. Both notes end in the same
            # "- run: discover.py --url" suffix, so match on the reason.
            if "not in the URL" in note:
                partial[note] = u
            else:
                unknown.add(u)
            continue
        if platform == "workday":
            if (target.get("tenant"), target.get("site")) in have_wd:
                continue
            cands.setdefault((platform, json.dumps(target, sort_keys=True)), u)
        else:
            if target in have.get(platform, set()):
                continue
            cands.setdefault((platform, target), u)

    results = []
    if cands:
        import concurrent.futures as cf
        def one(key):
            platform, tok = key
            target = json.loads(tok) if platform == "workday" else tok
            return key, target, dc.probe_board(platform, target)
        with cf.ThreadPoolExecutor(workers) as ex:
            for key, target, n in ex.map(one, list(cands)):
                results.append((key[0], target, n, cands[key]))
    results.sort(key=lambda r: (-r[2], r[0]))

    live = [(p, t) for p, t, n, _ in results if n > 0]
    L = ["", "## Boards found in the mail", "",
         f"_{len(links)} links · {len(cands)} boards not already in "
         f"targets.json · {len(live)} confirmed live_", ""]
    if results:
        L += ["| platform | board | postings | first seen in |",
              "|---|---|---|---|"]
        for p, t, n, u in results:
            label = (f"{t['tenant']}/{t['site']}" if isinstance(t, dict) else t)
            cnt = "**dead (0)**" if n == 0 else ("?" if n < 0 else f"**{n}**")
            L.append(f"| `{p}` | `{label}` | {cnt} | {u[:60]} |")
        L.append("")
    if partial:
        L += ["### Platform known, token is not in the URL", "",
              "These need one `discover.py --url` each:", ""]
        for note, u in sorted(partial.items())[:20]:
            L.append(f"- {note}")
        L.append("")
    if not results and not partial:
        L.append("_no new boards in this mail._")

    if add and live:
        added = dc.add_boards(targets_path, live)
        wd = [t for p, t in added if isinstance(t, dict)]
        L += ["", f"**Added {len(added)} board(s) to targets.json.** "
                  "Run `python3 make_fast.py <digest>` after the next full "
                  "sweep to let the fast lane pick up the productive ones."]
        if wd:
            # `name` is what notifications and board_health display, and a raw
            # Workday tenant is rarely the company's actual name (`ubc`, `td`,
            # `pcl`). Harmless, but worth one edit.
            L.append(f"  Workday entries were named after the tenant "
                     f"({', '.join(t['name'] for t in wd)}) - rename them in "
                     f"targets.json if you want readable alerts.")
    elif live and not add:
        L += ["", f"_Re-run with `--add-boards` to append the {len(live)} live "
                  "board(s) to targets.json._"]
    return "\n".join(L)


# Not employers. Every one of these turned up in the mail where a company name
# belongs: the aggregators re-mail other people's postings under their own
# brand, and "Linkedin.com" is the sender domain leaking into the column on a
# digest whose rows carried no name of their own.
EMP_SKIP = re.compile(
    r"^(?:linkedin|jobgether|indeed|ziprecruiter|glassdoor|monster|dice"
    r"|talent\.com|jooble|careerbuilder|workopolis|eluta|neuvoo|adzuna"
    r"|job\s*bank|simplyhired|lensa|jobget|recruiter\.com)\b", re.I)


def all_employers(msg):
    """Every employer named in one alert, with the titles it appeared under.

    The companion to all_job_links, for the channel that one cannot serve. A
    LinkedIn alert links only to linkedin.com/jobs/view/{id}, so there is no
    ATS token anywhere in the message and board_from_url has nothing to read -
    which is why 135 of 345 tracked rows are LinkedIn URLs with no board behind
    them. The employer's NAME is the only handle the mail gives, and
    discover.resolve_employer turns it into a board.

    The titles travel with the name because they are the identity evidence:
    the employer is in this mail BECAUSE of those reqs, so a board that still
    lists one of them is that employer's board and not a lookalike token.

    No title filter, deliberately - same reasoning as all_job_links. An
    employer worth polling can easily turn up in one week's mail under a Sales
    req, and the value test belongs on the board's own contents, which
    resolve_employer applies, not on one alert's title.
    """
    h, _t = body_parts(msg)
    out = {}
    for jid, frag in li_segments(h or ""):
        rec = _li_record(jid, frag)
        if not rec:
            continue
        co = (rec.get("company") or "").strip()
        if not co or EMP_SKIP.match(co) or len(co) < 2:
            continue
        out.setdefault(co, set()).add(rec["title"])
    return out


def employer_cache(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS employer_board("
                 "name TEXT PRIMARY KEY, at TEXT, verdict TEXT, "
                 "platform TEXT, token TEXT, postings INTEGER, evidence TEXT)")
    return conn


def report_employers(employers, targets_path, add=False, conn=None,
                     stale_days=60, limit=None, deep=False):
    """Resolve employers named in the mail into boards. Returns the report.

    Every result is CACHED, because without that this is unaffordable on a
    daily schedule: a fortnight of mail names ~200 distinct employers, and
    re-probing all of them every night would be thousands of requests a day at
    four ATS hosts to re-learn what it already knew. Cached, day one costs that
    once and a steady night costs only the employers that are new - typically
    a handful. A miss is re-probed after stale_days, since a company without a
    board today may have one next quarter; a hit never needs re-probing,
    because the board either went into targets.json or is reported again from
    cache for free.
    """
    tg = json.load(open(targets_path))
    have = {p: {v.lower() for v in tg.get(p, []) if isinstance(v, str)}
            for p in dc.PROBES}

    # Every employer already polled on ANY platform, not just the four whose
    # account names can be guessed. Without this the report told you to go
    # find a careers URL for RBC, BMO, Manulife, Scotiabank, TMX Group, PwC
    # Canada, Amazon and Insight Global - all of which the radar has polled for
    # weeks on Workday, SuccessFactors, Phenom or an in-house adapter. A name
    # probe cannot find a tenant-based board, so they came back "dead" and the
    # report believed it.
    #
    # Matched on the name and on its legal-suffix-stripped form, never on a
    # prefix: "Interac Corp" has to reach the Workday target named "Interac",
    # but "BMO Capital Markets" is left listed as missing rather than silently
    # credited to "BMO". Over-claiming here HIDES a board worth adding, which
    # is the more expensive error of the two.
    def _names(s):
        bare = s
        while True:
            nxt = dc.LEGAL_SUFFIX.sub("", bare).strip()
            if nxt == bare or not nxt:
                break
            bare = nxt
        return {re.sub(r"[^a-z0-9]+", "", x.lower()) for x in (s, bare) if x}

    polled = set()
    for k, v in tg.items():
        if k.startswith("_") or k == "devops_vendors" or not isinstance(v, list):
            continue
        for x in v:
            if isinstance(x, str):
                polled |= _names(x)
            elif isinstance(x, dict) and x.get("name"):
                polled |= _names(x["name"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cached = {}
    if conn is not None:
        employer_cache(conn)
        for n, at, v, p, t, c, ev in conn.execute(
                "SELECT name, at, verdict, platform, token, postings, evidence "
                "FROM employer_board"):
            cached[n.lower()] = (at, v, p, t, c, ev)

    todo, results = [], []
    for name in sorted(employers):
        # Already polled under one of its own name tokens - nothing to find.
        if (_names(name) & polled
                or any(t in have.get(p, ()) for t in dc.variants(name, 1)
                       for p in dc.PROBES)):
            results.append(("already", name, None, None, 0, None))
            continue
        hit = cached.get(name.lower())
        if hit and (hit[1] not in ("dead", "unverified", "no-target-roles")
                    or hit[0] > cutoff):
            results.append((hit[1], name, hit[2], hit[3], hit[4] or 0, hit[5]))
            continue
        todo.append(name)
    if limit:
        todo = todo[:limit]

    for name in todo:
        titles = sorted(employers[name])
        # Tier-1 variants only unless asked. Tier 2 is ~80% of the requests
        # - a miss fires 48 of them instead of 12 - and all twelve boards this
        # channel has ever found answered on tier 1, so paying for it on a
        # nightly schedule would be four times the load at four ATS hosts for
        # no measured return. `--employer-deep` turns it on for a backfill.
        v, p, t, posts, ev = dc.resolve_employer(name, titles, have=have,
                                                 tier2=deep)
        evs = f"{ev[0]} -> {ev[1]}" if ev else None
        results.append((v, name, p, t, len(posts), evs))
        if conn is not None:
            # Commit per employer, not once at the end. seen.sqlite3 is shared
            # with both poller lanes and SQLite in WAL mode allows exactly one
            # writer, so an open transaction here blocks them. Holding it
            # across the whole probe loop - minutes on a backfill, and this
            # runs at 19:52 while the fast lane's last run of the day fires at
            # 19:55 - is what makes the other process die on
            # "database is locked" once its 30s busy_timeout expires. A commit
            # per row costs nothing at this volume and bounds the lock to one
            # insert.
            conn.execute("INSERT OR REPLACE INTO employer_board VALUES(?,?,?,?,?,?,?)",
                         (name, now, v, p, t, len(posts), evs))
            conn.commit()

    by = lambda v: [r for r in results if r[0] == v]
    verified, nt = by("verified"), by("no-target-roles")
    unver, dead, already = by("unverified"), by("dead"), by("already")
    L = ["", "## Employers found in the mail", "",
         f"_{len(employers)} employers named · {len(todo)} newly probed · "
         f"{len(results)-len(todo)} from cache or already polled · "
         f"**{len(verified)} verified board(s)**_", ""]
    if verified:
        L += ["### Verified - the mailed title is still on the board", "",
              "| employer | board | postings | evidence |", "|---|---|---|---|"]
        for _v, n, p, t, c, ev in sorted(verified, key=lambda r: -r[4]):
            L.append(f"| {n} | `{p}/{t}` | **{c}** | {ev or ''} |")
        L.append("")
    if nt:
        L += ["### Identity confirmed, but no role the rules would match", "",
              "Not added - polling these costs a request every sweep forever.", ""]
        for _v, n, p, t, c, ev in nt:
            L.append(f"- {n} — `{p}/{t}` ({c} postings)")
        L.append("")
    if unver:
        L += ["### Live board, but the mailed title is not on it", "",
              "Either the req closed since the alert, or the token is a "
              "lookalike. Check before adding.", ""]
        for _v, n, p, t, c, _e in unver:
            L.append(f"- {n} — `{p}/{t}` ({c} postings)")
        L.append("")
    if dead:
        L += ["### No board under any name variant", "",
              "Mostly enterprises on Workday/SuccessFactors/Taleo/Oracle, whose "
              "tenant cannot be guessed from a company name. Each needs one "
              "`discover.py --url <careers page>`.", "",
              "  " + ", ".join(n for _v, n, *_ in sorted(dead, key=lambda r: r[1])), ""]
    if already:
        L.append(f"_{len(already)} already polled: "
                 + ", ".join(n for _v, n, *_ in already[:25]) + "_")
        L.append("")

    addable = [(p, t) for _v, _n, p, t, _c, _e in verified
               if p and t and t not in have.get(p, ())]
    if add and addable:
        added = dc.add_boards(targets_path, addable)
        L.append(f"**Added {len(added)} board(s) to targets.json.** "
                 "Run `make_fast.py <digest>` after the next full sweep so the "
                 "fast lane picks up the productive ones.")
    elif addable:
        L.append(f"_Re-run with `--add-employer-boards` to append the "
                 f"{len(addable)} verified board(s)._")
    return "\n".join(L)


def guess_location(msg_text, title):
    """Alert emails rarely tag location; look for a Canadian marker nearby."""
    window = msg_text[:4000]
    # At most three capitalised words before the province code, or
    # "DevOps Engineer in Edmonton, AB" gets captured whole.
    for m in re.finditer(
            r"\b([A-Z][a-z.\-]+(?:\s+[A-Z][a-z.\-]+){0,2}),\s*"
            r"(AB|ON|BC|QC|MB|SK|NS|NB|NL|PE)\b", window):
        return f"{m.group(1)}, {m.group(2)}"
    if re.search(r"\bremote\b", f"{title} {window}", re.I):
        return "Remote"
    return ""


def _clean_url(u):
    """The form append_to_tracker compares on: no query, no trailing punctuation."""
    return (u or "").split("?")[0].rstrip(".,)")


def _tracker_urls(conn):
    """Every posting URL already in the tracker, plus everything dismissed.

    This is the set append_to_tracker itself dedupes against, which is why a
    mailed job never produced a duplicate ROW - but --notify compared against
    `live` and `seen`, the ATS poller's tables, and those hold no linkedin.com
    URLs at all. So a job already in the tracker could be pushed again every
    time LinkedIn put it in a new message. On 2026-10-27 that pushed a GCP SRE
    role at J&M Group that the tracker already held as `applied` from the day
    before.
    """
    urls = set()
    try:
        with open(jr.TRACKER) as fh:
            urls |= {_clean_url(u) for u in
                     re.findall(r"https?://[^\s)\]]+", fh.read())}
    except OSError:
        # No tracker yet is not an error - it just means nothing is known.
        pass
    try:
        urls |= {_clean_url(u) for (u,) in
                 conn.execute("SELECT url FROM dismissed")}
    except sqlite3.Error:
        pass
    return urls


# Only corporate-structure words. "canada", "global", "digital" and
# "technology" are deliberately absent: they are part of real distinctive
# names, and stripping them merged Canada Life with Sun Life when this was
# first measured.
CO_GENERIC = frozenset(("the", "group", "holdings", "holding"))


def _co_tokens(name):
    """A company name as comparable tokens, legal suffix removed.

    Mail and ATS boards spell the same employer differently - the mail says
    "BMO Capital Markets" where the board says "BMO" - so exact string
    equality under-matches and let a known duplicate through to the phone.
    Token SUBSET, not substring: raw containment reads "Interac" inside
    "GuestTek Interactive Entertainment" and would have dropped a real job.
    Measured over all 11,325 company pairs in `seen`: zero false merges.

    Note this is deliberately NOT the matcher report_employers uses. There,
    over-claiming hides a board worth adding, so "BMO Capital Markets" is left
    uncredited on purpose. Here the cost runs the other way - under-claiming
    buzzes the phone with a job you already have - so the two stay separate.
    """
    bare = name or ""
    while True:
        stripped = dc.LEGAL_SUFFIX.sub("", bare).strip()
        if stripped == bare or not stripped:
            break
        bare = stripped
    return frozenset(t for t in re.split(r"[^a-z0-9]+", bare.lower())
                     if t and t not in CO_GENERIC)


def _same_employer(a, b):
    """Is one token set the same employer as the other, or a parent of it?"""
    if not a or not b:
        return a == b
    return a <= b or b <= a


def db():
    c = sqlite3.connect(jr.DB)
    c.execute("CREATE TABLE IF NOT EXISTS mail_seen("
              "msgid TEXT PRIMARY KEY, at TEXT)")
    # When this URL was FIRST mailed to us. The only honest age signal the
    # mail channel has: LinkedIn alert mail carries no posting date and no
    # relative age - measured across 15 segments in 6 messages, not one
    # contained "2 days ago" or anything like it - and its pages cannot be
    # fetched. So a posting's true age on the day it first arrives is simply
    # unknown, while every day after that is exact.
    c.execute("CREATE TABLE IF NOT EXISTS mail_role("
              "url TEXT PRIMARY KEY, first_seen TEXT, title TEXT)")
    return c


def role_ages(conn, rows, now_iso):
    """Stamp each row with how many days we have been seeing its URL."""
    for r in rows:
        conn.execute("INSERT OR IGNORE INTO mail_role VALUES(?,?,?)",
                     (_clean_url(r["url"]), now_iso, r.get("title") or ""))
    conn.commit()
    first = dict(conn.execute("SELECT url, first_seen FROM mail_role"))
    today = datetime.fromisoformat(now_iso).date()
    for r in rows:
        try:
            seen_on = datetime.fromisoformat(
                first[_clean_url(r["url"])]).date()
            r["days_known"] = max(0, (today - seen_on).days)
        except Exception:
            r["days_known"] = 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="write inbox.json template")
    ap.add_argument("--creds", action="store_true",
                    help="prompt for address and app password, no echo")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--all", action="store_true", help="ignore seen-state")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--md")
    ap.add_argument("--track", action="store_true",
                    help="append high scorers to applications.md")
    # Was 25 until 2026-10-22, which meant --track was decorative: the
    # structural cap described below puts mail-derived scores in the
    # 10-20 band, so almost nothing ever cleared 25 and the mail lane
    # tracked nothing. Worse, cron ran --notify WITHOUT --track, so an
    # alert-mail role buzzed the phone and was then never written
    # anywhere the web app reads - the "lots of jobs in my alerts that I
    # cannot find on the radar" report. Tracking now matches notifying:
    # if a role is worth a push it is worth a row. Noise is held back by
    # title_match (tier 0 titles never reach scoring at all), not by
    # this number.
    ap.add_argument("--track-min-score", type=int, default=12)
    ap.add_argument("--notify", action="store_true",
                    help="push new alert-mail roles to your phone via ntfy")
    # Email scores are STRUCTURALLY capped and this threshold reflects that
    # rather than matching the poller's. A digest snippet is ~200 characters
    # against a real JD's 5,000+, so the same role scores 14 here and 43 from
    # its full description - measured on Haventree's DevOps Engineer. 12 keeps
    # the real roles (today's run: three at 14) and drops the thin ones (two at
    # 10). Do NOT align this with --notify-min-score 15 in the poller lanes;
    # that number assumes a full description behind the score.
    ap.add_argument("--notify-min-score", type=int, default=12)
    # "the jobs might be 24 hours old, that is acceptable, but a week old" -
    # so this counts days since WE first saw the URL, which is the only age
    # the mail channel can measure honestly. A job new to us is always pushed,
    # however old LinkedIn's copy may be, because suppressing on a guess would
    # lose real postings; one we have been carrying for a week stops buzzing.
    # It never removes a row from the digest.
    ap.add_argument("--stale-days", type=int, default=7,
                    help="stop pushing a mailed URL we have been seeing for "
                         "this many days (0 disables; digest is unaffected)")
    ap.add_argument("--boards", action="store_true",
                    help="also report ATS boards found in the mail")
    ap.add_argument("--add-boards", action="store_true",
                    help="append confirmed-live boards to targets.json")
    ap.add_argument("--employers", action="store_true",
                    help="resolve employers named in the mail into ATS boards")
    ap.add_argument("--add-employer-boards", action="store_true",
                    help="append identity-verified employer boards to targets.json")
    ap.add_argument("--employer-limit", type=int,
                    help="probe at most N new employers this run")
    ap.add_argument("--employer-deep", action="store_true",
                    help="also try embellished name variants (4x the requests)")
    a = ap.parse_args()
    if a.add_boards:
        a.boards = True
    if a.add_employer_boards:
        a.employers = True

    if a.setup:
        if os.path.exists(CONFIG):
            print(f"  {CONFIG} already exists - not overwriting")
            return 0
        json.dump(TEMPLATE, open(CONFIG, "w"), indent=2)
        os.chmod(CONFIG, 0o600)
        print(f"  wrote {CONFIG} (mode 600, gitignored)\n"
              "  Fill in host/user/password, then: python3 inbox.py\n"
              "  Use a dedicated address and an APP PASSWORD, never your real one.")
        return 0

    if a.creds:
        # Prompted rather than taken as an argument on purpose. An app password
        # passed on the command line lands in shell history, in `ps` output
        # while the process runs, and in the transcript of whatever is driving
        # the terminal. getpass reads straight from the tty with echo off, so
        # the only place it ends up is the file.
        cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else dict(TEMPLATE)
        # getpass falls back to reading stdin WITH ECHO - printing "Warning:
        # Password input may be echoed" - whenever it cannot open a tty. That
        # happens when this is driven by a wrapper rather than a real terminal
        # (`claude`'s ! prefix, a pipe, CI), and an echoed app password lands in
        # that wrapper's transcript or log. Refuse instead of echoing.
        if not sys.stdin.isatty():
            print("  --creds needs a real terminal: without a tty getpass "
                  "echoes what you type.\n"
                  "  Open a normal shell and run it there, or set the password "
                  "in the environment instead:\n"
                  "    read -rs RADAR_IMAP_PASS && export RADAR_IMAP_PASS",
                  file=sys.stderr)
            return 1
        host = input(f"IMAP host [{cfg.get('host') or 'imap.gmail.com'}]: ").strip()
        user = input("alerts address: ").strip()
        pw = getpass.getpass("app password (not echoed): ")
        # Google displays app passwords in four groups of four. The spaces are
        # presentation only and IMAP login fails with them left in.
        pw = pw.replace(" ", "").strip()
        if not user or not pw:
            print("  address and password are both required", file=sys.stderr)
            return 1
        if "@" not in user:
            print("  that does not look like an email address", file=sys.stderr)
            return 1
        if len(pw) != 16:
            print(f"  warning: Google app passwords are 16 characters, got "
                  f"{len(pw)}. Saving anyway - check it if login fails.",
                  file=sys.stderr)
        cfg.update({"host": host or cfg.get("host") or "imap.gmail.com",
                    "user": user, "password": pw,
                    "folder": cfg.get("folder") or "INBOX"})
        cfg.pop("_note", None)
        # Create with 0600 before writing, not after: a world-readable moment
        # between open() and chmod() is all a local reader needs.
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        print(f"  saved {CONFIG} (mode 600)\n"
              "  Test it with:  python3 inbox.py --days 1")
        return 0

    cfg = load_config()
    if not cfg:
        print("  No IMAP config. Run:  python3 inbox.py --setup\n"
              "  or set RADAR_IMAP_HOST / RADAR_IMAP_USER / RADAR_IMAP_PASS.",
              file=sys.stderr)
        return 1

    since = (datetime.now(timezone.utc) - timedelta(days=a.days)).strftime("%d-%b-%Y")
    try:
        M = imaplib.IMAP4_SSL(cfg["host"])
        M.login(cfg["user"], cfg["password"])
        M.select(cfg.get("folder", "INBOX"), readonly=True)
        typ, data = M.search(None, f'(SINCE "{since}")')
    except Exception as e:
        print(f"  IMAP failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    ids = data[0].split() if data and data[0] else []
    conn = db()
    seen_ids = {r[0] for r in conn.execute("SELECT msgid FROM mail_seen")}
    jr.VENDORS.update(x.lower() for x in
                      json.load(open(jr.TARGETS)).get("devops_vendors", []))

    rows, links, employers = [], set(), {}
    msgs, now = 0, datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in ids:
        try:
            typ, d = M.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(d[0][1], policy=email.policy.default)
        except Exception:
            continue
        mid = str(msg.get("Message-ID") or i.decode())
        if not a.all and mid in seen_ids:
            continue
        msgs += 1
        # Committed per message for the same reason the employer cache is:
        # the first INSERT opens a write transaction, and without this it
        # stayed open across every remaining M.fetch() in this loop - 175
        # network round-trips on a --days 14 --all backfill. Any other writer
        # on seen.sqlite3 blocks for that whole window.
        conn.execute("INSERT OR IGNORE INTO mail_seen VALUES(?,?)", (mid, now))
        conn.commit()
        source, subject, found = extract(msg)
        if a.boards:
            links |= all_job_links(msg)
        if a.employers:
            for co, titles in all_employers(msg).items():
                employers.setdefault(co, set()).update(titles)
        for f in found:
            tier, fam = jr.title_match(f["title"], source)
            if not tier:
                continue
            # The posting's own location when the format gave one, and only
            # then a guess - from this record's blob, never from the whole
            # message. The old whole-message guess handed all eight jobs in a
            # digest the first job's city.
            loc = f.get("location") or guess_location(f["blob"], f["title"])
            bucket, ok = jr.classify_location(loc) if loc else ("?", True)
            if not ok:
                continue
            s, kw, lvl = jr.score(f["title"], source, tier, fam,
                                  f"{f['title']} {f['blob'][:2000]}")
            if s < a.min_score:
                continue
            rows.append({**f, "source": source, "subject": subject[:70],
                         "score": s, "kw": kw, "level": lvl, "family": fam,
                         "location": loc or "not stated", "bucket": bucket})
    conn.commit()
    M.logout()

    # The same req arrives in several digests - BDO's New Grad posting came in
    # three separate alerts - and each copy scores a little differently because
    # each digest surrounds it with different text. Keep the best-scoring copy
    # of each posting instead of listing it three times.
    byurl = {}
    for r in sorted(rows, key=lambda r: -r["score"]):
        byurl.setdefault(r["url"].split("?")[0], r)
    rows = list(byurl.values())
    role_ages(conn, rows, now)

    # Drop anything the ATS poller already found. A URL comparison only
    # catches the same posting from the same source: the poller holds the
    # Amazon AFT req as an amazon.jobs URL, LinkedIn mails it as
    # linkedin.com/jobs/view/{id}, and no amount of normalising makes those
    # equal. So also compare normalised title+company against `seen`, which is
    # every posting the radar has ever shown. Title-only collisions are flagged
    # rather than dropped - "DevOps Engineer" is not a unique key.
    def _norm(s):
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    live = {u.split("?")[0] for (u,) in conn.execute("SELECT url FROM live")}
    # norm(title) -> the employers `seen` has posted that exact title for.
    # Compared employer-aware rather than by exact string, because the mail and
    # the ATS board rarely spell the company the same way.
    shown = collections.defaultdict(list)
    for ti, co in conn.execute("SELECT title, company FROM seen"):
        shown[_norm(ti)].append(_co_tokens(co))
    # Mail postings do not live in `live` or `seen` at all - those are the ATS
    # poller's tables and hold no linkedin.com URLs - so neither one can tell
    # you that a mailed job is one you already have. The tracker can, and
    # append_to_tracker already dedupes against it, which is why the row was
    # never duplicated while the PUSH fired anyway.
    known = _tracker_urls(conn)
    fresh = []
    for r in rows:
        nt, mine = _norm(r["title"]), _co_tokens(r.get("company"))
        same_title = shown.get(nt, [])
        if r["url"].split("?")[0] in live or any(
                _same_employer(mine, c) for c in same_title):
            continue
        # Flagged rather than dropped: "DevOps Engineer" is not a unique key,
        # and of the seven rows this flagged on 2026-10-27 exactly ONE was a
        # real duplicate. The employer test above is what actually drops them.
        r["maybe_dup"] = bool(same_title)
        r["in_tracker"] = _clean_url(r["url"]) in known
        fresh.append(r)
    fresh_urls = {r["url"] for r in fresh}
    # Held out of the push and the tracker write, but still LISTED, so the
    # digest stays a complete record of everything the mail carried. Nothing
    # here removes a job from the report - the mail channel exists to catch
    # what the poller cannot reach, and a dropped row is a missed job.
    def _stale(r):
        return a.stale_days and r.get("days_known", 0) >= a.stale_days

    sendable = [r for r in fresh
                if not r["in_tracker"] and not r.get("applied")
                and not _stale(r)]

    rows.sort(key=lambda r: -r["score"])
    L = ["# Inbox — job alert emails",
         f"_{len(rows)} roles from {msgs} new messages · "
         f"{len(fresh)} not already found by the ATS poller · "
         f"{len(sendable)} new to you_", ""]
    sendable_urls = {r["url"] for r in sendable}
    for r in rows:
        if r["url"] not in fresh_urls:
            flag = "  _(already found by the radar)_"
        elif r.get("in_tracker"):
            flag = "  _(already in your tracker — not pushed again)_"
        elif r.get("applied"):
            flag = "  _(you already applied to this one)_"
        elif _stale(r):
            flag = (f"  _(first mailed to you {r['days_known']} days ago — "
                    f"listed, not pushed)_")
        elif r.get("maybe_dup"):
            flag = "  _(same title as a job you have already seen — check)_"
        else:
            flag = ""
        if r.get("reco") and r["url"] in sendable_urls:
            flag += "  _(LinkedIn suggestion, not one of your alerts)_"
        who = f" · **{r['company']}**" if r.get("company") else ""
        L += [f"### [{r['score']}] {r['title']}{flag}",
              f"- via **{r['source']}**{who} · `{r['bucket']}` · `{r['level']}`"
              f" · {r['location']}",
              f"- {r['url']}",
              f"- matched: {', '.join(r['kw']) or '—'}", ""]
    if not rows:
        L.append("_no new job-alert mail matched. Subscribe to alerts on the "
                 "employers in manual_check.md - that is what this channel is for._")
    if a.boards:
        L.append(report_boards(links, jr.TARGETS, add=a.add_boards))
    if a.employers:
        L.append(report_employers(employers, jr.TARGETS,
                                  add=a.add_employer_boards, conn=conn,
                                  limit=a.employer_limit, deep=a.employer_deep))
    out = "\n".join(L)
    print(out)
    if a.md:
        open(a.md, "w").write(out + "\n")

    if a.notify and sendable:
        # Only `sendable` - deduped against the ATS poller, against the
        # tracker, against jobs already applied to, and against URLs we have
        # been carrying for --stale-days - so a req the radar pushed this
        # morning does not buzz again tonight from LinkedIn's copy of it. notify() reads j["age"], and an alert snippet
        # carries no posting date, so it is explicitly None rather than absent.
        push = [{**r, "age": None,
                 "company": r.get("company") or r["source"]}
                for r in sendable if r["score"] >= a.notify_min_score]
        if push:
            topic = jr.ntfy_topic()
            if not topic:
                print(f"\n>> --notify asked for but no ntfy topic found "
                      f"({jr.NTFY_FILE} missing and RADAR_NTFY_TOPIC unset)",
                      file=sys.stderr)
            else:
                n = jr.notify(push, topic)
                print(f"\n>> pushed {n} of {len(push)} new role(s)")
        else:
            print(f"\n>> nothing at or above --notify-min-score "
                  f"{a.notify_min_score} to push")

    if a.track and sendable:
        # The EMPLOYER, falling back to the source only when the digest
        # did not name one - the same rule --notify uses above. This
        # said r["source"] unconditionally until 2026-10-22, so every
        # mail-sourced row entered the tracker as "Linkedin.com" and the
        # web app showed 116 jobs with no employer. The phone alert had
        # the right name all along, which is why a role could be read on
        # the phone and not found by company in the app.
        payload = [{**r, "company": r.get("company") or r["source"],
                    "posted": "", "age": None,
                    "updated": "", "url": r["url"], "title": r["title"]}
                   for r in sendable if r["score"] >= a.track_min_score]
        if payload:
            try:
                with jr.FileLock(timeout=60):
                    n, _ = jr.append_to_tracker(payload, jr.TRACKER,
                                                a.track_min_score)
                print(f"\n>> tracked {n} new posting(s)")
            except TimeoutError as e:
                # Same reasoning as the poller: this runs after --notify has
                # already pushed, so dying here leaves an alert with no row.
                print(f"\n>> WARNING: could not lock the tracker ({e}); "
                      f"{len(payload)} mail posting(s) were NOT written",
                      file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
