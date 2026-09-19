# Manual check list

Employers the radar **cannot** reach. Each is here for a specific,
verified reason — not because nobody tried. Budget ~10 minutes, once a week,
same cadence as the radar. Links go straight to a filtered search, so this is
click-and-scan, not a hunt.

Last verified: 2026-09-18

---

## Blocked by bot protection

These have a real ATS, but it refuses automated requests.

### CGI — Njoyn
- **Search:** https://cgi.njoyn.com/CORP/xweb/xweb.asp?page=joblisting&CLID=21001&lang=1
- **Why manual:** every request redirects to `validate.perfdrive.com` (Radware
  Bot Manager). Three attempts with full browser headers, all challenged.
  Getting past it means fingerprint spoofing — not worth an IP ban on a site you
  may want to apply through.
- **Worth checking:** CGI is one of Canada's largest IT employers and hires
  heavily for infrastructure and managed services in Toronto/Ottawa/Montreal.

### WestJet — Dayforce
- **Search:** https://jobs.dayforcehcm.com/en-CA/westjet/CANDIDATEPORTAL?keyword=engineer
- **Why manual:** not bot-blocked, but jobs load client-side. The server renders
  only site config; the job-search endpoint isn't in any of the 14 Next.js
  chunks. Finding it needs a headless browser, and it likely differs per tenant.

---

## No machine-readable job data at all

HTTP 200, no ATS signature, and **no sitemap in robots.txt** — so even the
sitemap-diffing fallback (§2 of the guide) doesn't reach them.

| Employer | Search link | Note |
|---|---|---|
| Intact Financial | https://careers.intactfc.com | Large Toronto tech org |
| CN Rail | https://careers.cn.ca | Montreal/Toronto, big infra team |
| CPKC | https://www.cpkcr.com/en/careers | Calgary |
| Canada Post | https://jobs.canadapost.ca | Ottawa |
| Definity | https://www.definity.com/careers/ | Waterloo — returns 403 to any script |
| York University | https://www.yorku.ca/jobs/ | Toronto — runs Technomedia, no public feed |
| UHN | https://www.uhn.ca/corporate/Careers | sitemap exists, 2 URLs, no jobs |
| SickKids | https://www.sickkids.ca/en/careers-volunteer/careers/ | Toronto |
| Ontario Public Service | https://www.gojobs.gov.on.ca/Search.aspx | Custom portal; huge IT org |

---

## Alberta — your local market

You live in Edmonton, so hybrid and onsite roles here are viable in a way Toronto
ones are not. These are the biggest Alberta IT employers, and **none of them are
reachable by the tool** — checked 2026-09-17.

### Taleo (no adapter — Oracle Taleo exposes no usable public feed)

| Employer | Search link |
|---|---|
| Alberta Health Services | https://careers.albertahealthservices.ca/jobs/search/146469417 |

AHS is the single largest employer in Alberta and runs a substantial IT
organisation. Verified 2026-09-18 against the real job-search URL — still Taleo,
still no usable public feed.

### Custom portals and blocked platforms

| Employer | Search link | Why it cannot be polled |
|---|---|---|
| City of Calgary | https://www.calgary.ca/careers.html#jobs | No ATS signature at all |
| ATB Financial | https://careers.atb.com/careers | Eightfold — API rejects unauthenticated calls |
| SAIT | https://sait-jobs.multiscreensite.com/#career-openings | multiscreensite, returns 403 |
| MacEwan University | https://www.macewan.ca/about-macewan/careers/opportunities/ | No ATS signature |
| University of Calgary | https://careers.ucalgary.ca/ | Returns 403 to scripts |

**ATB is the closest to solvable.** It runs Eightfold, whose API *does* work —
it simply rejected a guessed `domain` parameter. Open the careers page with
DevTools on the Network tab, find the XHR that returns the job list, and that URL
is very likely enough to automate it.

U of C refuses automated requests outright (HTTP 403), so no amount of adapter
work reaches it. The rest are ordinary pages with no machine-readable feed.

### Already automated — do **not** check these by hand

These Alberta employers are in `targets.json` and the radar polls them:

| Employer | Platform | Postings |
|---|---|---|
| **Government of Alberta** | SuccessFactors | — |
| **City of Edmonton** | Phenom | 49 |
| **University of Alberta** | Oracle Recruiting Cloud | 73 |
| **EPCOR** | Jobvite | 17 |
| **NAIT** | Workday | — |
| ENMAX · Ovintiv · TC Energy · Strathcona · Ledcor · Pembina · Imperial Oil | Workday / SuccessFactors | — |

The last three were added on 2026-09-18 from real job-search URLs, and two of
them required new adapters:

- **Oracle Recruiting Cloud** — a proper JSON REST API with real posted dates.
  Careers URLs look like `<host>/hcmUI/CandidateExperience/en/sites/<SITE>/jobs`.
  Widely used by Canadian universities and utilities, so this adapter is reusable.
- **Jobvite** — server-rendered HTML at `{base}/search?q=<token>`; dates come
  from schema.org `datePosted` on each job page.

City of Edmonton's first hit was an **Application & Infrastructure Analyst I**,
entry level — the kind of municipal role that never reaches a tech job board.

**The lesson worth repeating:** the landing page told us nothing. The URL of the
actual job-search results is what made three of these automatable. If an
employer here matters to you, click through to their real search page and check
the address bar for `myworkdayjobs`, `oraclecloud`, `jobvite`, `successfactors`
or `phApp` — any of those means the radar can probably take it.

### Search terms for the Alberta links

`DevOps` · `Cloud` · `Infrastructure` · `Linux` · `Systems Administrator` ·
`Endpoint` · `Site Reliability` · `Platform`

Public-sector Alberta postings often use *Systems Analyst*, *Technology Services*
or *IT Specialist* rather than the private-sector titles, so search broadly.

---

## The 10-minute routine

1. Open the links above in tabs (browser bookmark folder: "Weekly manual").
   The Alberta ones matter most — they are your only local hybrid/onsite options.
2. Search each for: `DevOps` · `SRE` · `Cloud` · `Infrastructure` · `Linux` ·
   `Endpoint` · `Systems`.
3. Anything promising → add it to `applications.md` by hand, same format as the
   radar's rows.
4. **Set each site's own email job alert** where one exists. These are exactly
   the sites where a native alert earns its keep, because nothing else is
   watching them for you.

Step 4 matters more than steps 1–3. A native alert turns this weekly chore into
push notifications, and for these eleven it is the only automation available.

---

## Re-check quarterly

Career sites get replatformed. If any of these moves to Greenhouse, Lever,
Ashby, Workday, SuccessFactors or Phenom, the radar picks it up immediately:

```bash
cd ~/devops/jobs/radar
python3 discover.py --url https://careers.intactfc.com
```

If it reports a known ATS, add it and delete that row from this file.
