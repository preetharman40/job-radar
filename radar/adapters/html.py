"""Generic HTML table scrape - the fallback for sites with no API.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from html import unescape as html_unescape
import re
import urllib.parse

from fetchcore import days_ago, get_text, job, strip_html


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
        h = get_text(url)
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
