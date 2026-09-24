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

### Taleo — **now automated** (2026-09-24)

Alberta Health Services is no longer a manual check. Its TalentBrew facet API
turned out to be reachable; see the Taleo section of `ARCHITECTURE.md`. The
radar polls `category/177` (Information Technology) plus five keyword facets.

Note it exposes **no posting date at all**, so AHS rows always show
`age unknown`.

### Custom portals and blocked platforms

Re-verified 2026-09-24.

| Employer | Search link | Why it cannot be polled |
|---|---|---|
| City of Calgary | https://www.calgary.ca/careers.html | No ATS signature at all |
| ATB Financial | https://careers.atb.com/careers | Eightfold — API rejects unauthenticated calls |
| SAIT | https://www.sait.ca/about-sait/work-at-sait | No ATS signature |
| MacEwan University | https://www.macewan.ca/about-macewan/careers/ | No ATS signature |
| University of Calgary | https://careers.ucalgary.ca/ | Returns 403 to scripts |
| Alberta Blue Cross | https://www.ab.bluecross.ca/careers/ | Returns 403 to scripts |
| Covenant Health | https://covenanthealth.ca/join-our-team | UKG/UltiPro — no adapter |
| Olds College | https://www.oldscollege.ca/about-us/work-at-olds-college | ADP WorkforceNow — no adapter |
| City of Lethbridge | https://www.lethbridge.ca/careers | Taleo + BambooHR mix — no usable feed found |
| Calgary Board of Education | https://www.cbe.ab.ca/careers | No ATS signature |
| Edmonton Public Schools | https://epsb.ca/ourdistrict/careers/ | No ATS signature |

**ATB is still the closest to solvable.** It runs Eightfold, whose API does work
— it simply rejected a guessed `domain` parameter. Open the careers page with
DevTools on the Network tab, find the XHR that returns the job list, and that
URL is very likely enough to automate it.

### Already automated — do **not** check these by hand

These Alberta employers are in `targets.json` and the radar polls them:

| Employer | Platform | Postings |
|---|---|---|
| **Government of Alberta** | SuccessFactors | — |
| **Alberta Health Services** | Taleo | 6 (IT category) |
| **AIMCo** | Workday | 7 |
| **WCB Alberta** | Workday | 7 |
| **Alberta Energy Regulator** | Workday | 3 |
| **Alberta Motor Association** | Workday | 45 |
| **City of Red Deer** | Oracle Recruiting Cloud | 14 |
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
