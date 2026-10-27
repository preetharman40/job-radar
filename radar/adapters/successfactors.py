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
    """One task per search term - see WD comment; RMK has the same shape."""
    return [{**tok, "_query": q} for q in SF_QUERIES]

def hydrate(j, kind):
    """The description sits in a jobdescription div on the posting page."""
    h = get_text(kind[1])
    m = re.search(r'<div[^>]*class="[^"]*jobdescription[^"]*"[^>]*>(.*?)'
                  r'</div>\s*(?:<div[^>]*class="[^"]*(?:jobbottom|clear))',
                  h, re.S | re.I)
    body = m.group(1) if m else h
    j["blob"] += " " + strip_html(body)[:8000]
