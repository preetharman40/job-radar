#!/usr/bin/env python3
"""fetchcore.py - the primitives every ATS adapter needs, and nothing else.

This is the bottom layer of the radar:

    fetchcore.py      HTTP transport, HTML -> text, the posting record
         |
    adapters/*.py     one module per ATS platform
         |
    job_radar.py      filtering, scoring, state, notification, reporting

It deliberately imports nothing from the radar. Keeping it dependency-free is
what lets 21 adapter modules share a transport without any of them being able
to reach back into the classifier or the database.

`job()` is the one record shape the whole pipeline agrees on: an adapter's only
job is to return a list of these.
"""

import json
import re
import time
import urllib.request
from html import unescape as html_unescape
from datetime import datetime, timezone


__all__ = ["UA", "TAG", "days_ago", "drop_scripts", "get_text", "job",
           "long_date", "req", "strip_html"]

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Content-Type": "application/json",
}


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


def req(url, data=None, timeout=25, retries=2):
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


def get_text(url, timeout=25):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.read().decode("utf-8", "replace")


# Public, unlike the rest of these: SuccessFactors needs tag removal without
# the space strip_html() inserts, so the regex itself is part of the interface.
TAG = re.compile(r"<[^>]+>")
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


def drop_scripts(h):
    """HTML with script/style bodies and comments removed, markup intact.

    What you want when you are searching the page's own text for a phrase
    rather than extracting prose - verify_tracked looks for "no longer
    accepting applications", and a Google Tag Manager blob can contain
    anything.
    """
    return _COMMENT.sub(" ", _SCRIPT.sub(" ", h or ""))


def strip_html(h):
    if not h:
        return ""
    h = drop_scripts(h)
    t = TAG.sub(" ", h)
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


# posted_date arrives as "September 25, 2026", and sometimes space-padded to a
# fixed width - "September  1, 2026". strptime("%B %d, %Y") would read it, but
# %B is locale-dependent and cron runs with a minimal environment where LC_TIME
# is not guaranteed to be English, so the date would silently become None on
# exactly the runs that matter. Map the months by hand instead.
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), 1)}
_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")


def long_date(s):
    """"September 25, 2026" -> "2026-09-25". days_ago() needs ISO."""
    m = _DATE.search(s or "")
    if not m:
        return ""
    mon = _MONTHS.get(m.group(1).lower())
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}" if mon else ""
