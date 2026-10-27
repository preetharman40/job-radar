"""UltiPro / UKG Recruiting.

Part of the adapters package: one module per ATS platform. See
adapters/__init__.py for the contract every module here implements.
"""

import json
import re

from fetchcore import days_ago, job, strip_html, _get_text, _req


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


def hydrate(j, kind):
    """The real posting is a JSON-encoded string inside the detail page."""
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
