"""Taleo behind a Radancy TalentBrew career site.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

from html import unescape as html_unescape
import re
import urllib.parse

from fetchcore import job, strip_html, _get_text, _req


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


def expand(tok):
    """Taleo facets are per-employer, so the queries come from the target
    itself. The default is category 177 (Information Technology)."""
    return [{**tok, "_query": tuple(q)}
            for q in (tok.get("queries") or [["category", "177"]])]
