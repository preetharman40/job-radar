# Manual check list

Eleven employers the radar **cannot** reach. Each is here for a specific,
verified reason — not because nobody tried. Budget ~10 minutes, once a week,
same cadence as the radar. Links go straight to a filtered search, so this is
click-and-scan, not a hunt.

Last verified: 2026-09-16

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
| Definity | https://careers.definity.com | Waterloo — fetch failed entirely |
| York University | https://jobs.yorku.ca | Toronto |
| UHN | https://www.uhn.ca/corporate/Careers | sitemap exists, 2 URLs, no jobs |
| SickKids | https://www.sickkids.ca/en/careers-volunteer/careers/ | Toronto |
| Ontario Public Service | https://www.gojobs.gov.on.ca/Search.aspx | Custom portal; huge IT org |

---

## The 10-minute routine

1. Open the nine links above in tabs (browser bookmark folder: "Weekly manual").
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
