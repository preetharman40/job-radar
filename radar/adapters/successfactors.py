"""SAP SuccessFactors RMK career sites.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from html import unescape as html_unescape
import re
import urllib.parse

from fetchcore import TAG, get_text, job, strip_html


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


# ---------------------------------------------------------------- feed mode --
#
# A second, far better-shaped source on the same platform. RMK also publishes a
# Google-Jobs RSS feed carrying the WHOLE board in ONE request with the full
# description inline, so a feed target needs no query fan-out and no hydrate
# pass. Opt in per target with "feed": true.
#
# Deloitte Canada is only reachable this way. Its search page runs the newer
# "SearchResultsUnify" theme, which renders no results server-side at all -
# SF_ROW and SF_TILE both find zero rows - and fetches them from
# /services/recruiting/v1/jobs, a path careers.deloitte.ca/robots.txt
# disallows. The feed is not disallowed.
#
# The path is a catch-all on the EXTENSION, not an endpoint: /sitemap.xml,
# /jobs.xml, /feed.xml and /totally-bogus-path-xyz.xml all return the same
# bytes. So a 200 here proves nothing about the path being right - the same
# silent-success class as Oracle's unvalidated siteNumber. Judge a new feed by
# whether <item> count and g:employer match the employer you meant.
SF_FEED_PATH = "/sitemap.xml"
SF_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
SF_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
SF_POSTAL = re.compile(r"^[A-Z]\d[A-Z]\s*\d[A-Z]\d$", re.I)


def _feed_field(item, tag):
    """One RSS field, unescaped to real markup.

    The unescape MUST happen here, before strip_html, and this is the trap on
    this feed: <description> is a CDATA block holding ESCAPED html
    (&lt;p&gt;...), so the only literal angle brackets in all 14 KB of it are
    the CDATA delimiters themselves. fetchcore.TAG is <[^>]+>, which therefore
    matches the entire description as a single tag and replaces the whole job
    description with one space. Measured before this existed: strip_html on the
    raw field returns 1 character, and every posting would have been scored on
    its title alone.
    """
    m = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), item, re.S)
    if not m:
        return ""
    c = SF_CDATA.match(m.group(1))
    return re.sub(r"\s+", " ", html_unescape(c.group(1) if c else m.group(1)))


def _feed_loc(raw):
    """'Toronto, ON, ON, CA, M5H 0A9' -> 'Toronto, ON, CA'.

    g:location appends a postal code, which is noise in the tracker and in a
    push notification, and sometimes repeats the province. Verified against
    all 18 distinct values on Deloitte's board: neither classify_location nor
    provinces_of returns anything different for any of them after tidying.
    """
    out = []
    for p in (raw or "").split(","):
        p = p.strip()
        if not p or SF_POSTAL.match(p):
            continue
        if out and out[-1].lower() == p.lower():
            continue
        out.append(p)
    return ", ".join(out)


def _fetch_feed(t):
    base = t["base"].rstrip("/")
    name = t.get("name", base)
    try:
        # 9.16 MB plain, 1.53 MB gzipped - the one payload here big enough to
        # be worth asking for compression.
        h = get_text(base + t.get("feed_path", SF_FEED_PATH),
                     timeout=60, accept_gzip=True)
    except Exception:
        return []
    out = []
    for item in SF_ITEM.findall(h):
        url = _feed_field(item, "link").strip()
        title = _feed_field(item, "title").strip()
        raw_loc = _feed_field(item, "g:location").strip()
        if not url or not title:
            continue
        # RMK repeats the location in the title: "Senior DevOps Engineer
        # (Toronto, ON, CA, M5H 0A9)" - true of 609 of 609 postings. It has to
        # be matched against g:location rather than stripped as "whatever is
        # in the last parentheses", because several titles legitimately end in
        # one: "Senior Cloud Infrastructure Engineer - Operate (Future
        # Opportunity) (Toronto, ON, CA, M5H 0A9)" has both.
        tail = "(%s)" % raw_loc
        if raw_loc and title.endswith(tail):
            title = title[:-len(tail)].strip()
        loc = _feed_loc(raw_loc)
        jd = strip_html(_feed_field(item, "description"))
        # No date, deliberately. g:expiration_date is the SAME value for every
        # posting on the board - 2026-11-27 for all 609 - so it carries no age
        # information at all: the RMK reference-date trap wearing a new hat.
        # Reporting it as `updated` would put a number in the tracker that
        # means nothing. Age stays unknown, as it already does on the
        # search-page path.
        out.append(job("successfactors", name, title, loc, url,
                       "", None, f"{title} {loc} {jd[:8000]}"))
    return out


def looks_like(html):
    """Does this page's markup say "SuccessFactors RMK"?

    Public because hunt.py identifies careers pages before it knows which
    adapter to hand them to, and the two result-row shapes this adapter parses
    are the identifying evidence. It used to read SF_ROW and SF_TILE directly.
    """
    return bool(SF_ROW.search(html or "") or SF_TILE.search(html or ""))


def _sf_text(x):
    return re.sub(r"\s+", " ", html_unescape(TAG.sub("", x))).strip()


def _sf_date(x):
    """'Sep 16, 2026' -> '2026-09-16'."""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", x or "")
    if not m:
        return ""
    mon = SF_MONTHS.get(m.group(1))
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}" if mon else ""


def fetch_successfactors(t):
    if t.get("feed"):
        return _fetch_feed(t)
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
            h = get_text(url)
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


def expand(tok):
    """One task per search term - see WD comment; RMK has the same shape.

    A feed target is a single request for the entire board, so it must NOT be
    fanned out: ten copies of the same 9 MB download would be this platform's
    whole cost for no extra coverage.
    """
    if tok.get("feed"):
        return [tok]
    return [{**tok, "_query": q} for q in SF_QUERIES]

def hydrate(j, kind):
    """The description sits in a jobdescription div on the posting page."""
    h = get_text(kind[1])
    m = re.search(r'<div[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)'
                  r'</div>\s*(?:<div[^>]*class="[^"]*(?:jobbottom|clear))',
                  h, re.S | re.I)
    body = m.group(1) if m else h
    j["blob"] += " " + strip_html(body)[:8000]
