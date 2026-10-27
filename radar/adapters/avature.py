"""Avature career sites.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from html import unescape as html_unescape
import re
import urllib.parse

from fetchcore import job, strip_html, _get_text


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


def expand(tok):
    """One task per search term."""
    return [{**tok, "_query": q} for q in AV_QUERIES]

def hydrate(j, kind):
    """Enriches the blob only - Avature exposes no date anywhere."""
    # No date to recover here - unlike Jobvite, the posting page has no
    # datePosted either - so this pass only enriches the blob. The
    # description lives in article__content__view; fall back to the
    # whole page rather than returning nothing if the class is renamed.
    h = _get_text(kind[1])
    m = re.search(r'class="article__content__view"[^>]*>(.*)', h, re.S)
    j["blob"] += " " + strip_html(m.group(1) if m else h)[:8000]
