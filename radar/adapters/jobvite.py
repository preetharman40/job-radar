"""Jobvite career sites.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import re
import urllib.parse

from fetchcore import days_ago, job, strip_html, _get_text


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


def expand(tok):
    """One task per search term."""
    return [{**tok, "_query": q} for q in JV_QUERIES]

def hydrate(j, kind):
    """The posting page is the only place Jobvite exposes datePosted."""
    h = _get_text(kind[1])
    m = JV_DATE.search(h)
    if m:
        j["posted"] = m.group(1)
        j["age"] = days_ago(m.group(1))
    body = re.search(r'class="jv-job-detail-description".*?</div>', h, re.S)
    j["blob"] += " " + strip_html(body.group(0) if body else h)[:8000]
