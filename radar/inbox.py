#!/usr/bin/env python3
"""
inbox.py - harvest job-alert emails, including from sites that block scraping.

Every employer that refuses automated requests will still happily email you.
Njoyn behind its bot manager, Taleo, Dayforce, Alberta Health Services, the City
of Edmonton, LinkedIn itself - none of them can block their own notification
email. Subscribe to their alerts with a dedicated address, and this reads them.

This is the only channel that reaches the employers in manual_check.md.

    python3 inbox.py --setup            # write a config template
    python3 inbox.py                    # parse new alert mail
    python3 inbox.py --days 7 --all     # re-read recent mail, ignore seen-state
    python3 inbox.py --md inbox.md --track

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
import email
import email.policy
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


def extract(msg):
    """Pull candidate postings out of one alert email."""
    h, t = body_parts(msg)
    sender = str(msg.get("From", ""))
    subject = str(msg.get("Subject", ""))
    dom = re.search(r"@([\w.-]+)", sender)
    source = (dom.group(1) if dom else "email").replace("www.", "")

    out, seen = [], set()
    for href, anchor in ANCHOR.findall(h or ""):
        href = htmlmod.unescape(href).strip()
        title = strip_html(anchor)
        if not href.startswith("http") or SKIP_LINK.search(href):
            continue
        if not JOB_LINK.search(href):
            continue
        if not title or len(title) < 4 or len(title) > 140:
            continue
        if title.lower() in ("view job", "apply now", "see job", "view", "apply",
                             "learn more", "click here", "view all jobs"):
            continue
        key = href.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": href})

    # Plain-text alerts (Job Bank, some government systems) have no anchors.
    if not out and t:
        for line in t.split("\n"):
            m = re.search(r"https?://\S+", line)
            if m and JOB_LINK.search(m.group(0)) and not SKIP_LINK.search(m.group(0)):
                title = line.replace(m.group(0), "").strip(" -–—•\t")
                if 4 < len(title) < 140:
                    out.append({"title": title, "url": m.group(0)})
    return source, subject, out


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


def db():
    c = sqlite3.connect(jr.DB)
    c.execute("CREATE TABLE IF NOT EXISTS mail_seen("
              "msgid TEXT PRIMARY KEY, at TEXT)")
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="write inbox.json template")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--all", action="store_true", help="ignore seen-state")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--md")
    ap.add_argument("--track", action="store_true",
                    help="append high scorers to applications.md")
    ap.add_argument("--track-min-score", type=int, default=25)
    a = ap.parse_args()

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

    rows, msgs, now = [], 0, datetime.now(timezone.utc).isoformat(timespec="seconds")
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
        conn.execute("INSERT OR IGNORE INTO mail_seen VALUES(?,?)", (mid, now))
        source, subject, found = extract(msg)
        h, t = body_parts(msg)
        text = strip_html(h) or (t or "")
        for f in found:
            tier, fam = jr.title_match(f["title"], source)
            if not tier:
                continue
            loc = guess_location(text, f["title"])
            bucket, ok = jr.classify_location(loc) if loc else ("?", True)
            if not ok:
                continue
            s, kw, lvl = jr.score(f["title"], source, tier, fam,
                                  f"{f['title']} {text[:2000]}")
            if s < a.min_score:
                continue
            rows.append({**f, "source": source, "subject": subject[:70],
                         "score": s, "kw": kw, "level": lvl, "family": fam,
                         "location": loc or "not stated", "bucket": bucket})
    conn.commit()
    M.logout()

    # Drop anything the ATS poller already found.
    live = {r[0] for r in conn.execute("SELECT url FROM live")}
    fresh = [r for r in rows if r["url"].split("?")[0] not in
             {u.split("?")[0] for u in live}]

    rows.sort(key=lambda r: -r["score"])
    L = ["# Inbox — job alert emails",
         f"_{len(rows)} roles from {msgs} new messages · "
         f"{len(fresh)} not already found by the ATS poller_", ""]
    for r in rows:
        dup = "" if r in fresh else "  _(also found by the radar)_"
        L += [f"### [{r['score']}] {r['title']}{dup}",
              f"- via **{r['source']}** · `{r['bucket']}` · `{r['level']}` · {r['location']}",
              f"- {r['url']}",
              f"- matched: {', '.join(r['kw']) or '—'}", ""]
    if not rows:
        L.append("_no new job-alert mail matched. Subscribe to alerts on the "
                 "employers in manual_check.md - that is what this channel is for._")
    out = "\n".join(L)
    print(out)
    if a.md:
        open(a.md, "w").write(out + "\n")

    if a.track and fresh:
        payload = [{**r, "company": r["source"], "posted": "", "age": None,
                    "updated": "", "url": r["url"], "title": r["title"]}
                   for r in fresh if r["score"] >= a.track_min_score]
        if payload:
            with jr.FileLock():
                n, _ = jr.append_to_tracker(payload, jr.TRACKER, a.track_min_score)
            print(f"\n>> tracked {n} new posting(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
