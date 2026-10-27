# Architecture and findings

Notes from building a job radar that reads eight applicant-tracking systems
directly. Most of this was learned by probing live endpoints and being wrong
first, so it is written down to save the next person the same afternoon.

---

## The premise

A careers page is a JavaScript app. It gets its job list from a public,
unauthenticated endpoint — the same one anyone can call:

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs"
```

A recruiter clicks *Publish* and that endpoint returns the job within seconds.
Aggregators poll the same endpoints on their own schedule, and search engines
have to crawl a mostly client-rendered page before they index anything.

Measured lag, from posting timestamps across ~16,000 requisitions:

| Source | Lag behind publish |
|---|---|
| ATS JSON API | seconds |
| Sitemap `<lastmod>` | minutes |
| Large aggregators | 1–7 days |
| Search engines | 3–20 days, inconsistently |

So the tool reads the source. Everything else is plumbing.

**The honest caveat, measured:** using true publication dates, the median
matched posting is **15 days old and still open**, and long-lived requisitions
are common — one had been open 713 days. Speed is a real edge on a handful of
reqs a week, not a general one. Anyone building this should know that before
optimising latency, and should verify that the "posted" field they are reading
means publication rather than last-modified.

---

## Pipeline

```
targets.json ──► FETCH ──► FILTER ──► HYDRATE ──► SCORE ──► DIFF ──► REPORT
  469 boards    ~49,200     ~200        ~200      ~200     new only
                postings   survive    descriptions
```

**The order is the only interesting part.** Full job descriptions for 48,000
postings is roughly 80 MB per run. Filtering on title and location first — both
available in the cheap list call — cuts that to ~200 detail fetches. Whole run:
**~215 seconds** for 469 boards expanded into 1,051 concurrent tasks. Job Bank
accounts for about half the growth from ~130s on its own: it honours a 5s
`Crawl-delay` on every request, list and hydrate alike, so a run with 16 Job
Bank finalists spends ~105s of that total waiting on purpose.

Workday and SuccessFactors targets fan out to one task per search keyword rather
than one per tenant, so a nine-tenant sweep parallelises across the shared pool
instead of running ten queries back to back. That change alone took a run from
over two minutes to under thirty seconds.

State is a SQLite table of seen URLs, so a run reports only what is new.

---

## Per-platform notes

### Greenhouse
`boards-api.greenhouse.io/v1/boards/{token}/jobs`. Clean JSON. `content=true`
returns the full description but inflates the payload — fetch descriptions
per-job in the hydrate pass instead.

**Use `first_published`, not `updated_at`.** This one cost real credibility.
`updated_at` is last-modified: a recruiter editing a six-month-old requisition
makes it look posted today. Measured across 13,528 postings:

| | |
|---|---|
| Median true age (`first_published`) | **49 days** |
| Median age implied by `updated_at` | **6 days** |
| Median understatement | **36 days** |
| Correct (0 days off) | only **17%** of postings |
| Off by 90+ days | **28%** of postings |

The worst case reported a job as one day old when it was first published 2,709
days earlier. A tool whose entire premise is freshness was reading the wrong
field — worth checking what a timestamp actually means before building on it.

`boards.greenhouse.io` now 301s to `job-boards.greenhouse.io`. Anything matching
on the old domain silently misses.

### Lever
`api.lever.co/v0/postings/{token}?mode=json`. The only adapter with a true
creation timestamp (`createdAt`, epoch ms) — everything else exposes
last-modified, which conflates a new req with an edit to an old one. Ships the
full description in the list call, so no hydrate pass needed.

### Ashby
GraphQL at `jobs.ashbyhq.com/api/non-user-graphql`. Where most recent scale-ups
are.

**The board-level type exposes no date field at all.** Asking for
`publishedDate` there fails validation, and because the endpoint answers HTTP
200 with `{"errors": [...], "data": null}`, the adapter read an empty board and
returned zero jobs — **silently, across every Ashby tenant**. A GraphQL API that
returns 200 on a schema error is a good argument for asserting on payload shape,
not status code.

Dates and descriptions both come from the single-posting query, where the
fields are `publishedDate` and `descriptionHtml` (not `descriptionPlainText`).

### Workday — five traps

The biggest enterprise platform and the most quirks.

**1. The country facet parameter is per-tenant.** The Canada GUID
`a30a87ed25634629aa6c3958aa2b91ea` is identical across every tenant on earth,
but the *name of the facet that takes it* is tenant-configured, and guessing
wrong returns HTTP 400 rather than an empty list:

| Tenant | Parameter |
|---|---|
| TD Bank | `locationCountry` |
| CIBC, RBC, BMO | `Country` |
| Sun Life, Manulife | `Location_Country` |

Query with `appliedFacets: {}` first, read the `facets` array, find the entry
whose descriptor is Country, cache it per tenant.

**2. `total` is returned only on page one.** Every subsequent page reports
`total: 0`, so a naive `while offset < total` loop stops after two pages.

The first fix for that was `while offset < 40` — which removed the crash but
*kept the cap*, at exactly 40 rows per query, for a long time afterwards. It
read as deliberate ("2 pages per query") rather than as a workaround, and
nothing reported the rows it was dropping, so the truncation was invisible until
a board was audited by hand. Measured when it was finally fixed: Manulife lost
rows on 4 of 13 queries (`automation engineer` 61, `support engineer` 59,
`platform engineer` 50, `systems engineer` 41) and RBC on 6 of 13 (`support
engineer` 93, `modern workplace` 80, `platform engineer` 70, `systems engineer`
65, `automation engineer` 60, `cloud engineer` 42). Recovering them raised a
full sweep from 149 matches to 154.

`limit` is not a shortcut: Workday caps it at 20 and answers **HTTP 400** above
that — loudly, unlike Amazon's `result_limit=500` returning `hits: 0`. Paging is
the only route. The loop now captures `total` on page one and pages to it,
stopping on whichever comes first: a **short page** (authoritative even when a
tenant omits or zeroes `total`, checked before `total` for that reason),
reaching `total`, or `WD_MAX_PAGES`.

Two things make this cheaper than it sounds. The old code already fetched a
useless second page whenever `total` was 21+, so Manulife goes 18 → 22 requests
and RBC 22 → 31. And it adds no burst pressure, because `_wd_sem` is held across
the whole function: extra pages lengthen one slot rather than widening
concurrency against a platform that rate-limits per tenant. A full sweep went
110.9s → 125.9s with **zero** rate-limit errors.

Hitting `WD_MAX_PAGES` is recorded in `WD_TRUNCATED` and printed in the run
footer. That register exists because silent truncation was the original bug and
a 300-row silence would have been the same mistake at a larger number — and it
earned itself immediately by catching a tenant the sizing sample had missed.

**Where the ceiling came from, and the one board that exceeds it.** The deepest
genuinely-selective query across the large tenants is TD Bank's `platform
engineer` at 123 (7 pages); RBC 93, BMO 84, Thomson Reuters 67, Manulife 61. So
15 pages covers all of them with room. CIBC is the exception and is left
truncating on purpose: its `searchText` works (`devops` → 5, a nonsense term →
0) but `linux` matches **489 of its 490** Canadian reqs, because the word lives
in CIBC's posting boilerplate rather than in 489 Linux jobs — page one of that
query is Mobile Mortgage Advisors. Raising the global ceiling to 25 to absorb it
would spend ~130 extra requests a sweep reading retail-banking listings that
`title_match` rejects at tier 0, while CIBC's real DevOps reqs are already
reachable through the cheap `devops` query. A tenant that genuinely needs deeper
paging gets `"max_pages"` on its own target instead of moving the global.

**3. Enterprise tenants are enormous.** One bank had ~1,029 open Canadian reqs;
paging all of them costs ~51 requests to then discard 98%. Sending keyword
queries and deduping on `externalPath` is far cheaper.

**4. Tenant names are not derivable.** Loblaw's Workday tenant is `myview`.
RBC's site is `RBCGLOBAL1`. Brute-forcing tenant/cluster/site combinations
returned zero hits across 1,755 attempts — the only reliable route is resolving
a real careers URL.

**5. A multi-location req has no city at all in the list call.** `locationsText`
collapses to the literal string `"2 Locations"` / `"3 Locations"`, and nothing
else in the row names a place. That classifies as `MULTI`, which is *kept* —
correctly, because it might include Canada — so the failure is not a dropped
posting but two quieter ones: the digest printed `2 Locations` where a city
belongs, `provinces` came back empty so `--province` could not see it, and a req
open only in Bratislava and Madrid arrived looking like a Canadian candidate.
Measured on PwC and Manulife together: five matches, four of them placeholders,
one of those genuinely non-Canadian.

The fix costs no extra requests, because the detail call is already fetched by
the hydrate pass and carries the real set — `jobPostingInfo.location` is the
primary and `.additionalLocations` is the rest. So hydrate repairs the location
and recomputes the geography pass 1 derived from the placeholder (`bucket`,
`provinces`, `arrangement`), and `main()` re-applies the geography filter
afterwards for exactly those rows. `Senior Platform Engineer` now reads
`Toronto, Ontario / Waterloo, Ontario` with `provinces=['ON']` instead of
`MULTI` with none, and the Bratislava req is dropped — counted in the run footer
as `N dropped once "N Locations" resolved`, so a jump in that number means a
board started collapsing its locations rather than that the search narrowed.

Deliberately **not** done by parsing the city out of `externalPath`. The slug
carries one (`/job/Toronto/…`), but a hyphen is both the separator and part of
real place names — `Toronto-Ontario`, `Los-Angeles-Del-Rey`, `Saint-Laurent` —
so there is no reliable split, and it would have given one city where the point
is that there are several.

Descriptions live at `/wday/cxs/{tenant}/{site}{externalPath}` and include an
exact `startDate`, which beats the list view's "Posted 12 Days Ago". Without
them, Workday postings score on title alone and systematically lose to platforms
whose descriptions are cheap to fetch.

### SuccessFactors — two themes, three traps

No JSON API and no RSS. The search page renders server-side, which is workable,
but:

**1. Two different themes.** Most tenants use a table (`<tr class="data-row">`);
others use tiles (`<li class="job-tile">`) with **no location column at all** —
the location is only in the URL slug
(`/job/Toronto-CIVIL-ENGINEERING-TECHNOLOGIST-ON-M5V3C6/`).

**2. Anchor attribute order differs between them.** The table theme writes
`<a href=… class="jobTitle-link">`; the tile theme writes
`<a class="jobTitle-link" … href=…>`. A regex requiring href first matches
nothing on half the tenants and fails silently.

**3. The date it shows is not a posting date.** The only sortable date field is
`referencedate`, and SAP's own documentation is explicit: the Reference Date
*"is not the date when the job was posted in RCM"* — it is set when the job is
first imported into RMK and then **cycled every 28 days automatically** to keep
content fresh for SEO. The real posting date (the RCM Start Date) is *"not
viewable on the RMK career site"* at all.

Verified empirically by sorting each board ascending — the oldest date on the
entire board:

| Tenant | Oldest date anywhere on the board |
|---|---|
| Scotiabank | 28 days ago |
| Rogers | 29 days ago |
| Telus | 28 days ago |
| Canada Life | 28 days ago |

Across boards of several hundred requisitions, **nothing is older than the
cycle length**. Any age computed from this field is meaningless. The adapter
reports SuccessFactors postings as *age unknown* and carries the reference date
labelled as what it is, rather than inventing a freshness number.

**4. Query handling is hostile.** Multi-word `q=` values are OR'd into noise —
`q=site reliability` returns anything matching *"site"*. Use single tokens. And
adding `sortColumn` makes the endpoint **ignore `q=` entirely** and return
everything by date, which looks like a working search returning irrelevant
results.

### Phenom — often just a skin, and `postedDate` lies

Same trap as Greenhouse: `postedDate` is a repost/refresh stamp and `dateCreated`
is the origin. One requisition showed `postedDate` 2026-09-03 against
`dateCreated` 2026-06-26 — 69 days understated. Report the origin; carry the
repost date separately, since a recently refreshed old req is a signal the
recruiter is actively working it.

Ships its jobs inline in a `phApp.ddo` script block; paginate with
`?from=N&s=1`. But check a job's `applyUrl` first: for two major banks it
pointed at `myworkdayjobs.com`. Phenom was a front-end over Workday, and polling
the Workday tenant directly gives structured locations and full descriptions
instead of parsed HTML.

---

## Location classification

Deciding whether a posting is reachable from one country turns out to be the
subtlest part of the system, and it broke three times — every time from
substring matching.

| Bug | Cause |
|---|---|
| `"United States of America"` classified as North-America-remote | `"amer"` matched inside "A**mer**ica" |
| `"Hamilton, New Zealand"` classified as Canadian | `"milton"` matched inside "Ha**milton**" |
| `"Hamilton, New Zealand"` again | region marker `" on,"` matched "Hamilt**on,**" |

All three were `substring in string`. Everything now matches on word boundaries.

Two further refinements were needed:

**Ambiguous city names.** London, Victoria, Vancouver, Burlington, Cambridge,
Hamilton, Richmond, Halifax and Kingston all exist in multiple countries.
`"Burlington, MA"` and `"Melbourne, Victoria, Australia"` were both being read
as Canadian. These now require a province or country qualifier *and* the absence
of a foreign marker.

**`", CA"` is genuinely ambiguous.** It means California on US boards and Canada
on SuccessFactors (`"Toronto, ON, CA"`). It is excluded from the US-state list;
US cities are caught instead by having no Canadian marker at all.

The test suite for this function is 29 cases and worth keeping.

---

## What is not reachable

Three dead ends, all verified, all worth not re-discovering:

**Bot-protected.** One major Canadian ATS sits behind Radware Bot Manager —
every request redirects to a validation challenge, including with full browser
headers. Getting past it is fingerprint spoofing; it would break on every rule
change and risks an IP ban on a site a user may need manually.

**Client-side rendering with no discoverable endpoint.** One platform is not bot
blocked but renders jobs client-side, and the search endpoint appears in none of
its fourteen JS bundles. It needs a headless browser, probably per tenant.

**Custom careers SPAs.** Nine large employers return HTTP 200 with no ATS
signature **and no sitemap in robots.txt**, so even sitemap-diffing does not
reach them. These are a manual list, not an automation target.

---

## Coverage is an allowlist, not a crawl

No platform exposes a "list every board" endpoint —
`boards-api.greenhouse.io/v1/boards` 404s, Lever's 404s, Ashby's needs auth. The
only way to widen coverage is to take real company names and test each one.

| Platform | Approximate customers |
|---|---|
| Workday | ~11,000 tenants |
| Greenhouse | 7,500+ |
| Lever | ~5,000 |
| SuccessFactors | ~4,000 |
| Ashby | 2,700+ |
| Phenom | ~1,000 |

469 boards is roughly **1%** of that. Worth stating plainly rather than implying
completeness.

### Diminishing returns, measured

| Effort | Result |
|---|---|
| 163 → 366 boards | **+4 matches** |
| 94 employers hunted for tenants | 6 tenants → **+4 matches** |
| 12 VC portfolio pages | 17 boards → ~0 matches |
| +90 SmartRecruiters tenants | **+15 matches** |
| +1 Taleo tenant | **0 matches** |
| 5 Alberta public-sector employers | **+1 match** |
| 36 staffing agencies fingerprinted | 2 boards → **+3 matches** |
| 1 in-house ATS (Amazon Canada) | 425 postings → **+1 match** |

The funnel explains it:

Measured when the radar polled 374 boards and scanned 19,500 postings. The
absolute numbers have since roughly doubled (469 boards, ~49,200 scanned); the
stage-by-stage proportions are the point, and they have not moved.

```
19,500 scanned
  11,900  not reachable from the target country   (67%)
   2,900  explicitly US-only                      (17%)
   2,950  reachable                               (17%)
     210  + matching title                        (1.2%)
      60  + score and recency thresholds
```

Two thirds of what a new board returns is geographically irrelevant. Nine
Workday tenants produce more matches than 190 Greenhouse boards, because
enterprise tenants are dense with roles in one country. **Adding boards is the
obvious lever and nearly the worst one.**

---

## Design decisions

**No dependencies.** Standard library only, Python 3.8+. A job-search tool that
needs a virtualenv rebuild after six months of not running is a tool you stop
running.

**Retries in the transport layer.** At 1,051 concurrent tasks a single network
blip silently drops an entire board, and the run reports a clean number while
missing jobs. Two retries with backoff; an instrumented run shows zero fetch
failures.

**Verification probes twice.** Concurrent probing of 148 boards trips rate
limits and reported nine live boards as dead. With an auto-prune flag that would
have silently deleted good targets. The second pass is serial.

**Appending, never rewriting.** The tracker dedupes on URL and never touches a
row a human has edited. A tool that overwrites your notes gets abandoned.

**Empty runs do not clobber output.** Running on a 30-minute schedule means a
quiet run would otherwise overwrite the digest from the run that found
something.

**Score bands over a score threshold.** Lowering the score threshold turned out
to do nothing — measured at 10, 8 and 4, all returned the same count, because
the title filter binds long before the score does. Widening the level filter
roughly doubles results. Worth measuring which knob actually moves before
documenting one.

---

## Posting-time data

From ~16,000 timestamped postings, local time:

- **~60% land between 10am and 4pm**, ramping sharply at 9am, collapsing after 5pm
- Tuesday–Thursday are mildly the busiest days
- **Weekends are ~1% of volume** — scheduling a poller for Saturday is waste

A caveat on the method: only one platform exposes true creation timestamps. The
rest report last-modified, where an edit to an old requisition is
indistinguishable from a new one. The clean sample is smaller than the total,
and one platform's midnight spike is almost certainly batch processing rather
than recruiters working at 3am.

## SmartRecruiters

Two public APIs; only one is usable.

**`jobs.smartrecruiters.com/sr-jobs/search` (cross-company) - do not build on
this.** It silently ignores every filter and paging parameter it is handed.
`location=Canada`, `country=ca`, `countryCode=ca`, `filter=country:ca`,
`loc=`, `geo=`, `region=`, `locations=` and `offset=1000` all return the
identical first ~96 rows, and `totalFound` stays pinned at the unfiltered
count. Sampling 573 rows across ten keywords returned **zero** Canadian
postings - not because SmartRecruiters has no Canadian inventory, but because
the window only ever shows the newest ~96 postings worldwide. It is useful for
exactly one thing: harvesting tenant identifiers (`company.identifier`).

**`api.smartrecruiters.com/v1/companies/{tenant}/postings` (per-company) - this
is the adapter.** Keyless, pages correctly via `offset`, returns full
inventory, and is sorted newest-first, so capping at `SR_MAX_PAGES` keeps the
freshest rather than an arbitrary slice. Records carry a true `releasedDate`,
a structured `location` with `remote`/`hybrid` booleans and an ISO country
code, plus `function` and `experienceLevel` labels that feed the level filter.
Descriptions hydrate from `/postings/{id}` -> `jobAd.sections`.

**Trap: an unknown tenant returns HTTP 200 with `totalFound: 0`,** which is
byte-identical to a real tenant with no openings. Guessed identifiers are
frequently wrong in ways that look like an empty board - `Ubisoft` is empty,
`Ubisoft2` has 300 postings. Never trust a hand-typed token; confirm it has
produced at least once. `board_health.py` is the safety net.

Tenants were found by harvesting `company.identifier` from the cross-company
search across 24 keywords (415 distinct tenants), then scanning each
per-company board for `location.country == "ca"`. 90 of 415 had Canadian
inventory on page one.

## Taleo (via Radancy TalentBrew career sites)

Most large Canadian employers have left Taleo - of 19 guessed tenants
(`rbc`, `td`, `cibc`, `bell`, `telus`, `cn`, ...) only `aircanada.taleo.net`
still resolves, and its public careersection redirects to the SmartOrg admin
login. The Taleo worth reaching is the kind fronted by a Radancy TalentBrew
career site on the employer's own domain, e.g.
`careers.albertahealthservices.ca`. Alberta Health Services is the largest
employer in Alberta and was previously on the manual-check list.

The job list is client-side, but it is driven by a plain JSON facet API:

    GET /ajax/jobs/<search_id>/add/<facet>/<value>
      -> {"Status":"OK","UserMessage":"<count>","Result":"<new_search_id>"}

Adding a facet does not return jobs; it mints a **new search id** that you then
page through. Three traps, each of which silently returns plausible data:

1. **The keyword facet needs the term in both the path and the query string.**
   `/add/keywords/engineer` returns `Status: OK` and the *unfiltered* board
   (1,228 rows). `/add/keywords/engineer?keywords=engineer` returns 20. Nothing
   distinguishes the two responses except the count.
2. **Pagination is `/jobs/search/<id>/page<n>`** - no slash before the number.
   `?page=2`, `?offset=10`, `/page/2` and six other forms all silently serve
   page 1. The pattern is built in `job_list.js`, not exposed in the HTML.
3. **`POST /ajax/jobs/search/create`** - the endpoint the search box itself
   uses - answers `Invalid Access` to every scripted call, with or without
   cookies, Referer and Origin. The facet API above is the way in.

Keyword matching runs over full job text, not titles, so broad terms are
useless: `support` returns 1,156 of 1,228 rows. Narrow facets are what work -
`category/177` ("Information Technology") is the precise slice, with
`analyst`, `cloud`, `infrastructure`, `network` and `security` as backstops for
IT roles filed under other categories.

**There is no posting date anywhere on this platform** - not on the result card,
not on the job page, not in a meta tag, and there is no JSON-LD block at all.
`age` is always `None`; the only freshness signal is a "New" badge, which the
adapter passes through in `updated` rather than converting into a day count it
cannot justify. This is the same honesty rule applied to SuccessFactors
reference dates.

Result cards carry title, location, category and a description snippet, so no
per-job hydration request is needed.

## Workday: a withdrawn requisition answers 403, not 404

`verify_tracked.py` asks the CXS job endpoint directly rather than trusting the
sweep, because Workday's keyword index lags its job endpoints. That part was
right. What was wrong was the status handling: only **404** was treated as
gone, and 403 was lumped in with 429/500/502 as transient.

Workday does not 404 a pulled requisition. It answers **403 with a JSON body
carrying `errorCode: "S22"`**, while the HTML job URL keeps returning **200** -
it is a single-page app whose shell renders "not found" client-side, so a
surface check of the page cannot tell the difference (6.5KB of shell, no job
text, empty `<title>`).

The consequence was silent: those jobs sat in state `checking` with `misses=0`
forever, never accumulating a miss, never hidden. Seven tracked postings were
stale this way - a Sun Life Cloud Engineer req was still on the board three days
after being pulled, alongside reqs from RBC (x3), TD, Desjardins and one more.
They had been reported as "unclear", which read like harmless noise and was
actually seven dead jobs held open.

**403 alone is not sufficient evidence** - a throttled or bot-blocked tenant
answers 403 too, which is why it was transient in the first place. The check
therefore keys on the S22 body specifically. Verified by firing 10 concurrent
requests at one tenant, the documented 403 trigger: the live requisition
returned 200 all ten times while the pulled one returned 403/S22 all ten, so the
code tracks the requisition's state and not the server's load.

`MISSES_BEFORE_GONE = 3` still applies on top, so a job is hidden only after
three consecutive S22 readings.

---

## Staffing agencies: the platforms they are actually on

LinkedIn's Canadian DevOps volume is heavily agency-posted, and the radar had
zero agency boards. Closing that looked like the obvious next move. It mostly
is not, and the reason is structural.

**Token guessing does not find agencies.** `harvest.py` probed 59 agency and
systems-integrator names across the six token platforms — 648 probes — and
returned 10 hits, of which **one** was the company we were looking for:

| Hit | What it actually was |
|---|---|
| `ashby/harvey` (345 jobs) | Harvey, the legal-AI company — not Harvey Nash |
| `ashby/altis` | a New York venture-intelligence firm — not Altis Recruitment |
| `ashby/quantum` | a London data company — not Quantum Technology Recruitment |
| `ashby/eagle`, `rippling/eagle` | unrelated NYC and New Orleans startups |
| `rippling/motion` | unrelated — not Motion Recruitment |
| `greenhouse/david` | a Madrid ad agency — from "David Aplin Group" |
| `recruitee/adecco` | a Recruitee demo account, one job: "Senior Marketer (Sample)" |
| `greenhouse/vaco` | really Vaco — but 51 *internal* recruiter and BD reqs, no client roles |
| `greenhouse/thoughtworks` | genuine, and kept |

That is `harvest.py`'s first-word heuristic working as designed and still being
wrong: a four-or-more-letter distinctive first word is a good rule for
"Vena Solutions" and a terrible one for agencies, whose names are built out of
common nouns. **Every hit needs its board read before it is added** — three of
those would have poisoned the allowlist with a company on another continent.

**Agencies are not on corporate ATS platforms.** Fingerprinting 36 Canadian IT
staffing firms' own sites found:

| Platform | Firms |
|---|---|
| iCIMS | Insight Global |
| Bullhorn | Motion Recruitment |
| JobDiva | Diverse Lynx |
| UKG | Hays (global) |
| WordPress job plugin | David Aplin Group, Raise |
| nothing detectable | the other 30 |

Agencies run recruiting CRMs — Bullhorn, JobDiva, Avionté — whose public faces
are JavaScript search pages with no feed. The 30 "nothing detectable" sites are
SPAs calling a private XHR endpoint on their own domain.

Two incidental findings: **Eagle Professional Resources now redirects to
sisystems.com** and **Lannick redirects to vaco.com** — both acquired, so
neither is a separate board. Worth re-checking any agency before adding it.

**Hays has a real JSON API and it is gated.** `mapi.hays.com/jobportalapi/...`
is public enough to enumerate but every job route answers **HTTP 401**; the
portal mints a bearer token from
`/usermanagement/create/v1/token` first. Minting tokens against a private API to
read it is a different kind of thing from reading a published feed, and it
breaks the first time they rotate the scheme. Not done.

## `wpjobs` — one adapter, because WordPress ships a REST API

The agencies that *are* reachable are reachable for the same reason: their
careers page is WordPress, and WP job plugins register their post type on
`/wp-json/wp/v2/`. That makes `fetch_wpjobs` worth more than the two boards it
currently serves — it fits any careers site on WP Job Manager or Matador (which
is itself a Bullhorn→WordPress bridge, so it is common among agencies).

Two traps, each of which cost a 404 first:

1. **The route is the post type's `rest_base`, not its name.** Aplin's post type
   is `job_listing` but its route is `/wp/v2/job-listings`; Raise's is
   `matador-job-listings`. Guessing the type name returns HTTP 404
   `rest_no_route`. Read `{base}/wp-json/wp/v2/types` and take `rest_base`.
2. **Paging past the end is HTTP 400**, `rest_post_invalid_page_number`, not an
   empty array — so the loop treats a failure *after* page 1 as the end and only
   a failure *on* page 1 as a broken board.

Location lives in a different place per plugin. WP Job Manager keeps a plain
string in `meta._job_location`; Matador exposes only bare term IDs in the
record, but WordPress also emits readable slugs in `class_list`
(`matador-localities-toronto`, `matador-regions-ontario`,
`matador-workplaces-hybrid`), which is the only way to read it without one
request per taxonomy. `_wp_location` tries meta first, then the slugs.

`content.rendered` carries the full description inline, so this is the only
adapter here that needs no hydrate pass.

**Measured:** 609 postings → 5 matches → **3 above the tracking threshold**, all
from Raise. Aplin returned 301 postings and nothing above 15 — it is largely
accounting, finance and manufacturing recruiting — but it is Edmonton-based, so
it is kept and `board_health` watches it.

### A numeric-entity bug this exposed

`strip_html` folded only the named entities in `_ENT`, so numeric ones survived
the stripper. WordPress REST returns titles like
`Business Analyst &#8211; Senior`, which reached the tracker and the push
notification verbatim. `_NUM_ENT` now folds the numeric dashes and quotes to
ASCII — deliberately, matching what `_ENT` already did, rather than letting
`html_unescape` produce real en-dashes — and anything left over goes through
`html_unescape`.

---

## `amazon` — the first in-house ATS, and why harvesting could never find it

Every other adapter here targets a *third-party ATS product*. Amazon wrote its
own, so it was invisible to the entire discovery toolchain: `harvest.py` probes
token-based platforms, `discover.py --company` probes the same list, and neither
has an Amazon-shaped thing to find. It was not in `targets.json`, not in
`board_health`, and — worse — not in `manual_check.md` either. An unreachable
employer that is *written down* is a known limitation; this was a blind spot.

**How it surfaced.** Not from the radar. A `DevOps Engineer, AFT` req in Toronto
was spotted on LinkedIn, and the URL carried `utm_source=linkedin.com`,
`cmpid=SPLICX0248M` and `ss=paid`. Amazon had *paid* to syndicate it there. That
is the push/pull asymmetry in one artifact: LinkedIn is fed by recruiting
budgets, and an allowlist poller has to go to the source. The req scored **22**
and would have cleared the notify threshold — the radar simply had nowhere to
look.

The endpoint turns out to be one of the better ones on offer:
`/en/search.json` has a real country facet, structured `city`/`state`/
`country_code`, a genuine `posted_date`, and the full description inline, so
like `wpjobs` this needs **no hydrate pass**. One `targets.json` entry covers a
whole country: Amazon Canada is ~425 requisitions in five requests.

Two traps:

1. **`result_limit` is capped at 100 and exceeding it fails silently.**
   `result_limit=500` returns `{"hits": 0, "jobs": []}` — byte-identical to a
   country with no openings, and indistinguishable from success. Same class as
   the SmartRecruiters `totalFound: 0` trap. `AMZ_PER_PAGE` is pinned at 100.
2. **`posted_date` is a localised English string, not ISO** — `"September 25,
   2026"`, sometimes space-padded to a fixed width (`"September  1, 2026"`).
   `days_ago()` parses ISO, and a date it cannot read becomes `None`, which
   means `--max-age` stops filtering that posting. `strptime("%B %d, %Y")` reads
   it but `%B` is **locale-dependent**, and cron runs with a minimal environment
   where `LC_TIME` is not guaranteed to be English — so it would have worked in
   an interactive shell and silently failed on every scheduled sweep. `_amz_date`
   maps the month names by hand for that reason.

One more shape to watch: `team` is an *object*, not a string. Joining it into
the blob raises `TypeError` on the first posting; the readable part is
`team.label`.

**Measured:** 425 postings → 2 passing the title filter → **1 above the tracking
threshold**. The other is `Software Development Manager, Agentic AI DevOps`,
which scores 8 once the `exec` penalty applies. This adapter bought exactly one
match, and it was still worth five requests, because that match was a confirmed
miss rather than a hypothetical.

Geography is unchanged by it: Vancouver 159, Toronto 153, Montreal 20, Ottawa 9,
Calgary 9. Amazon is a relocation-or-remote source, not a local one.

`Edmonton` itself returns 0, but that number needs care: Amazon files its
Edmonton-area sites under suburb names — **Nisku** (4) and **Acheson** (4), in
Leduc and Parkland County, plus Balzac and Rocky View in the Calgary ring —
which is 24 Alberta postings under six place names and not one of them
containing "Edmonton". A human filtering a job board by city misses all of
them. `classify_location` gets it right regardless (`bucket=CA`,
`provinces={'AB'}`), and `title_match` then rejects every one at `tier=0`
because they are fulfillment-centre roles: *Area Manager II, YEG1*, *ICQA Data
Analyst, YEG2*, *Delivery Station Customer Service Associate*. The pipeline
behaves correctly here; the lesson is that a city-name filter is not a
geography filter, which is why province classification exists.

### The rest of the in-house class

Amazon is not special in running its own ATS, only in being reachable. Probed
2026-10-17:

| Employer | Result |
|---|---|
| Amazon | public JSON, no auth — **now polled** |
| Apple | `jobs.apple.com/api/v1/search` → HTTP 401 |
| Microsoft | `gcsservices.careers.microsoft.com` → answered, non-JSON on a first pass |
| Shopify, Nvidia, Intuit, IBM, Telus | `discover.py --company` → no hits; not on a token platform, so each needs `discover.py --url` against its real careers page |

`google` and `meta` *appear* in `targets.json` but are harvest false positives of
the kind warned about above — `recruitee/google` has a best-ever count of 1
posting and is not Google. Presence in the allowlist is not evidence of
coverage.

---

## `avature` — the first platform with no date at all

Every other adapter here gets a publication date from somewhere. Greenhouse,
Lever, Oracle, Workday and Amazon put one in the list response; Ashby withholds
it from the board query but exposes `publishedDate` on the single-posting type;
Jobvite withholds it from the list and leaves schema.org `datePosted` on the
posting page. Avature has **none of those**: no JSON endpoint, no JSON-LD, no
`datePosted`, and no date in the markup of either the result row or the posting
page. Checked both, twice.

So `fetch_avature` reports `posted=""` and `age=None` — "posted age unknown" —
and that is the honest answer rather than a defect. The cost is specific and
worth stating plainly: **`--max-age` stops working on this platform.** A req
that has been open for a year is indistinguishable from one published this
morning. The contrast is Fortinet, added the same day: its four DevOps reqs
scored 10-16 and were *correctly* suppressed at 148, 176, 323 and 393 days old.
Nothing suppresses the equivalent on Avature.

Synthesising a date from first-seen was considered and rejected. It would read
downstream as publication, so every posting that existed before the board was
added would look new on the day it was found and would then age out on a
schedule that has nothing to do with when the recruiter published it — wrong in
both directions, and silently.

The usable substitute is the **Role ID**, which Avature puts in the subtitle and
at the end of every posting URL. EA's 354 live reqs span `209831`-`216503` and
the IDs are roughly sequential, so `216xxx` is current and `213xxx` has been
open a while. That is a proxy, not a date, and it is per-tenant.

### Parsing

Rows are server-rendered and the markup is stable:

```
<article class="article article--result ...">
  <a class="link link_result" href=".../JobDetail/Site-Reliability-Engineer/215922">
  <div class="article__header__text__subtitle">
     Vancouver, Canada - Role ID 215922 - Regular Employee - EA Studios
```

The subtitle is bullet-separated, and **the segments before `Role ID` are all
locations**. A posting open in several offices lists each one, and the extras
use a different field order than the primary — `Vancouver, Canada, British
Columbia` against `Vancouver, Canada` — so neither a positional nor a
`City, Country` parse survives contact with it. Splitting on the bullet and
treating `Role ID` as the terminator is what works. Locations are joined with
`" / "` the way `fetch_ashby` joins secondaries, which is load-bearing:
`Senior Software Engineer (Devops)` is Austin-primary but also open in
Vancouver, and without the join it classifies `US-ONLY` and is dropped. The
segments *after* `Role ID` are the employment type and the department
(`CT - Infrastructure & Platform`) and both stay in the blob, because both
score.

Two traps:

1. **`jobRecordsPerPage` is accepted and ignored.** `=100` and `=200` both
   return 20 rows and a near-identical page length. Nothing errors, so code
   written to request the board in one call would quietly index only the first
   20 of 354 and look like it had swept everything. Same silent-cap class as
   Amazon's `result_limit=500` returning `hits: 0`, and SmartRecruiters'
   `totalFound: 0`. `AV_PAGE` is pinned at 20 and coverage comes from
   `AV_QUERIES`, not from paging the board.
2. **`make_fast.py` rewrites `fast.json` from scratch**, keyed off `TOKEN_SRC`
   and `DICT_SRC`. A new platform missing from those tuples is not an error and
   produces no warning — the next `make_fast.py` run simply writes a `fast.json`
   with no `avature` key, and the board is dropped from the fast lane forever.
   Caught by auditing both tuples against `targets.json` rather than by anything
   failing.

Only `en_US` is polled. EA publishes the same requisitions under `es_ES`,
`fr_CA`, `zh_CN` and others; a second locale would duplicate every posting under
a different URL and defeat the `live`-table dedupe, which keys on URL.

`robots.txt` is permissive here and was checked before anything was fetched:
`Allow: /careers`, `Allow: /*/careers`, and the only `Disallow` is `*qtvc=`,
a tracking parameter the adapter never sends.

**Measured (Electronic Arts, 2026-10-21):** 354 reqs → 160 scanned across 8
queries → **7 matches, all 7 above the tracking threshold** — 48, 45, 44, 33,
26, 24, 17. For scale, the Amazon `DevOps Engineer, AFT` req that exposed the
in-house blind spot scored 22. This is the strongest single board added to the
radar, and it needed a new adapter rather than a `targets.json` line because
`fetch_html`'s `GEN_ROW` is `<tr>`-only and Avature renders `<article>`.

Geography is the reason it scores so well: of 354 reqs, **200 classify `CA`** —
BC 194, QC 30, ON 2 — against 113 `OTHER` and 41 `US-ONLY`. By city that is
Vancouver 170 plus Vancouver-Great Northern Way 27, Montreal 30, Victoria 4,
Toronto 3. It is the most Canada-weighted board in `targets.json` by proportion,
and **none of it is Alberta** — EA has no Edmonton or Calgary presence, so every
match here is a relocation or a remote negotiation.

### What this says about the platform, not the employer

Avature is sold to large enterprises and EA is one tenant of many; the adapter
is tenant-agnostic and takes the locale-scoped careers root, so another Avature
employer is a `targets.json` entry rather than new code. The discovery path is
the weak point: the account token is never in the posting URL — tenants serve
from a vanity domain (`jobs.ea.com`) while the real tenant is `ea.avature.net` —
so `discover.py` classifies these as a **lead**, not an inversion, and
`inbox.py --boards` will surface an Avature link for a human to resolve with
`discover.py --url` instead of adding it automatically.

---

## `teamtailor` — the shape every other platform should have

`{tenant}.teamtailor.com/jobs.json` is a JSON Feed (jsonfeed.org 1.1), and one
request returns everything the pipeline needs per posting:

| field | what it gives |
|---|---|
| `date_published` | a real ISO timestamp — `--max-age` works normally |
| `content_html` | the **full description inline** |
| `_jobposting` | schema.org JobPosting with a structured `PostalAddress` |

So this is the third adapter needing **no hydrate pass** (with `wpjobs` and
`amazon`), and the only one that pairs that with clean dates. It is the direct
opposite of `avature`, added the same day, which has no date anywhere and needs
`<article>` scraping; the two make a decent before-and-after for what a careers
platform can choose to publish.

`robots.txt` was checked before anything was fetched. For `*` it disallows only
`/app/`, `/messages/`, `/messenger/`, `/facebook/tab/` and `/jobs/internal/`,
and the host advertises `Content-Signal: search=yes, ai-input=yes, ai-train=no`
— reading a feed to answer a person's job search is the first two, not the
third.

### Both traps are in the location, and both change the result

**1. `addressCountry` is an ISO code, and bare `CA` is not Canada.** It only
survives `classify_location` when the city itself is already in the
classifier's Canadian list, which the smaller Alberta towns are not:

| location string | bucket | kept |
|---|---|---|
| `Toronto, CA` | `CA` | yes |
| `Lethbridge, CA` | `OTHER` | **dropped** |
| `Medicine Hat, CA` | `OTHER` | **dropped** |
| `Grande Prairie, CA` | `OTHER` | **dropped** |
| `Nisku, CA` | `OTHER` | **dropped** |
| `Lethbridge, Canada` | `CA` | yes |

This is the Amazon Edmonton-suburb failure in different clothing — a genuine
Alberta requisition made invisible by how its country was spelled, in the one
province that matters most here. `TT_ISO` expands the code to a country name
before the string is handed over. Unmapped codes pass through, harmlessly:
`SE` and `DE` reach `OTHER` either way.

**2. `addressRegion` is sometimes a continent, and that silently flips the
bucket.** Teamtailor's own board files Stockholm under region `Sweden` and
Chicago under `North America`. Pasting that through is not just untidy:
`Chicago, North America, United States` classifies **`NA-REMOTE` and is KEPT**,
because "North America" reads as a remote-anywhere-in-NA signal. An on-site
Illinois job would have been shown as a remote opportunity. `TT_SKIP_REGION`
drops continents and a region that merely repeats the country, after which
Chicago reads `US-ONLY` and is correctly excluded.

Neither of these announces itself. Both were found by running the classifier
over the real values before the adapter was written, which is the only reason
they are notes here rather than a quiet hole in the Alberta coverage.

### Paging, written against the Workday lesson

`per_page` and `page` both work and `per_page` was honoured up to 500. The loop
stops on an **empty** page, or when a page contributes no new ids — and
deliberately **never on a short page**, because a server that silently capped
`per_page` below the asked value would make every full page look short and end
the sweep after one. That is precisely how the Workday 40-row cap concealed
itself for months, and the same mistake was available here for free.

### The second `make_fast.py` omission in one day

`make_fast.py` rewrites `fast.json` from scratch from `TOKEN_SRC` and
`DICT_SRC`. A platform missing from those tuples raises nothing, warns nothing,
and simply vanishes from the fast lane on the next regeneration. `avature` hit
this earlier the same day; `teamtailor` hit it again. Two for two means it is
not an oversight but a missing invariant, so adding a platform now ends with
diffing both tuples against `targets.json` keys in each direction.

### Discovery is the easy case here

Unlike Avature, the tenant **is** the subdomain, so one posting URL yields the
whole board and `discover.py` inverts it outright rather than filing it as a
lead. The region segment is part of the identity and is rebuilt from the real
host, never assembled from the company name: commonsku is
`commonsku.na.teamtailor.com`, while European tenants carry no region at all.
`probe_board` then validates the candidate through `fetch_teamtailor` itself,
so a token that inverts cleanly but throws inside the adapter never reaches
`targets.json`.

**Measured (commonsku, 2026-10-21):** 5 reqs → **1 match at score 43** —
`DevOps Engineer II`, Toronto, classified `entry`, which is a closer profile
fit than most of the higher scorers on bigger boards. The employer did not come
from harvesting: it arrived in a LinkedIn job-alert email and was surfaced by
`inbox.py`, then the posting URL inverted straight into a target. That is the
whole intended path from alert mail to polled board, working end to end for the
first time.

---

## `insightglobal` — the agency board that *is* pollable, and a wrong verdict

"Staffing agencies: the platforms they are actually on" above concluded that
agencies are structurally hard: their names defeat token guessing, and the ones
with real boards post through their own sites. Insight Global is the
counter-example that fits that conclusion rather than contradicting it. It was
found by a human — a recruiter pointed it out, 2026-10-23 — and its board is
not an ATS product at all. Like `amazon`, it is an **in-house ATS**, so
`harvest.py` and `discover.py --company` can never fingerprint it; it belongs
with "the rest of the in-house class", not with the agency-token experiment.

### The verdict that was wrong, and why

The board was first investigated and reported as **unreachable**. That was
wrong, and the way it was wrong is the useful part.

`/jobs/` is an Angular SPA. Its runtime config names an API and a Microsoft
Entra CIAM tenant, and every path probed under that API answered
`401 WWW-Authenticate: Bearer` — `/my/jobs`, `/my/jobs/search`, GET, POST, even
OPTIONS. A blanket 401 across an entire prefix reads exactly like a login wall,
and it was reported as one. The bundle says otherwise:

```js
get jobsBasePath() {
  return this.msalService.instance.getActiveAccount() ? "/my/jobs" : "/all/jobs"
}
```

There are **two lanes**. `/my/` is the signed-in one and really is gated;
`/all/` is anonymous and entirely open. The 401s were real but proved only that
the wrong door had been knocked on. `/all/jobs/suggested` and `/all/locations`
were both sitting in the grep output that produced the wrong verdict and were
read straight past.

The lesson is narrow and worth keeping: **an auth gate on one prefix is not
evidence about the origin.** Before calling an SPA's data unreachable, find the
base-path expression in the bundle rather than probing paths — a ternary on
"is anyone signed in" is a common shape, and it is invisible to path guessing.

Two other dead ends were real, and are why the first pass went looking for a
scraper at all: the search route has **no SSR** (`/jobs/search/...` and every
`/jobs/{id}` shape return the same 29,697-byte shell), and the **old board is
decommissioned** — `insightglobal.com/jobs/find_a_job/.../job-433450/` style
URLs, still indexed by search engines, now answer **HTTP 410 Gone**. Anything
found for this employer in a search index is a ghost of that board.

### The API

| call | gives |
|---|---|
| `GET /all/jobs?filter.status=active&filter.locations=ON&page=1&size=50` | the list, plus `pageMetadata{size,number,totalElements,totalPages}` |
| `GET /all/jobs/{requisitionId}` | the same row **plus `descriptionHtml`** |
| `GET /all/jobs/{requisitionId}/details` | the same description under a different key — redundant, not fetched |

Other accepted facets, from the bundle: `keyword`, `sort`, `filter.jobType`,
`filter.includeOnlyRemoteWork`, `filter.minimumPayRateGreaterThan`,
`filter.distance`. Nested filters flatten to dot notation (`filter.locations`)
because the client builds params with a recursive flattener.

`size` is **capped at 50 and clamps silently** — ask for 500 and the response
says `"size": 50`. This is the `AMZ_PER_PAGE` trap again in a politer form: it
does not fail, it just quietly returns less. Paging reads
`pageMetadata.totalPages` rather than trusting the request.

Human URL is `https://insightglobal.com/jobs/{requisitionId}` (the SPA's
`:id` route), which is what the tracker links to.

### Enumerate by province, not by keyword

This is the first adapter here where **keyword search is the wrong axis**, and
the numbers are not close. Measured 2026-10-23:

| approach | requests | Canadian reqs found | truncation |
|---|---|---|---|
| 13 × `WD_QUERIES`, 6 pages each | 78 | 60 | heavy — `platform engineer` reported 1,486 hits, 300 read |
| 13 × province via `filter.locations` | ~11 | **282 (all of them)** | none |

The endpoint ORs loosely, so a two-word query matches most of the US
inventory and the relevance ranking decides what survives. Enumerating
provinces makes the server do the only filtering it is good at — geography —
and leaves title filtering to `TITLE_RULES`, which is better at it than a
remote ranker. Fewer requests, complete recall, and no truncation register to
maintain.

Full Canadian inventory at the time of writing: ON 192, BC 45, QC 25, AB 9,
NS 4, MB 3, SK 2, NB 2 — **282 active**, six pages' worth.

### `administrativeArea` is not country-safe

The field looks like a province code and is not one. In this feed:

| value | what it means |
|---|---|
| `CA` | **California** — 22 US cities carried it (San Diego, Irvine, San Jose…) |
| `TG`, `KA`, `MH`, `AN` | Indian states (Telangana, Karnataka, Maharashtra, Andaman) |
| `São Paulo`, `Rio de Janeiro` | Brazilian states, spelled out |

So the country filter **must** be the server-side `filter.locations` facet,
which is exact — `ON` returns Ontario only. Filtering this field locally would
have swept 22 California jobs in as Canadian on the first run.
`classify_location` reads `San Diego, CA` as `OTHER` and would have caught most
of it one stage later, but the right place to be correct is in the fetch, before
282 rows become 500. The province codes are safe to drive the facet from for a
separate reason worth stating: **none of the 13 collides with a US state code**
— Nebraska is `NE`, Pennsylvania is `PA`.

`workRemote` is a real boolean, and the location string stays the client's city
regardless, so the adapter appends `/ Remote` for those rows — without it a
remote req reads as on-site and `arrangement()` never sees it. 26 of 282.

### Offshore markers in the title: flagged, not dropped

Staffing agencies sell offshore hiring, and Insight Global marks it in the
**title** while filing the req at the client's Canadian address. `MLOps
Engineer - INTL India` sits at `workAddress: Vancouver, BC` and scores **39** —
well above the notify threshold — and its description mentions neither India
nor Canada. The title is the only geographic signal that exists.

`offshore_marker()` adds a `⚠️` line to the digest instead of filtering the row
out. Three of 282 rows carry a marker, and the evidence is thin in both
directions: `Marketing Campaign Manager (Intl.)` in Kanata is plausibly a
Canadian role with an international remit, and silently hiding a Vancouver req
on one title token is the worse error. The check requires a **country name**
after `INTL` (or a bare `LATAM`/`EMEA`/`APAC`) *and* a Canadian location, so it
stays quiet on `LATAM - Lead Data Engineer` in San Francisco, which is simply a
US job. Run against all 295 titles then in the tracker: **zero** false flags.

### Hydrate, and the `make_fast.py` invariant

The list call carries no description, so this sets `hyd` and takes the JD from
`/all/jobs/{id}` in pass 2 — one request per finalist, no HTML scraping, the
same bargain `greenhouse` and `ashby` make. It is *not* in the no-hydrate class
with `wpjobs`, `amazon` and `teamtailor`.

`insightglobal` was added to `make_fast.py`'s `DICT_SRC` **in the same change**,
and the both-ways diff against `targets.json` keys was run before the change was
called done: 21 adapters, 21 entries, no gaps in either direction. `avature` and
`teamtailor` each hit that omission silently; writing the invariant down was
supposed to end it, and this is the first platform added since, so it is the
first test of whether it did.

**Measured (2026-10-23):** 282 reqs scanned → **7 matches**, in 20.5s with zero
non-Canadian leakage and zero duplicate URLs. Top of the run: `Platform
Engineer, Network Operations` (Vancouver) **44**, `Senior Kubernetes / Cloud
Platform Engineer` (Markham, remote) **36**, `AI Platform Engineer` (Toronto)
**21**. Mix across the Canadian inventory is 40 contract / 14 perm / 5
contract-to-perm — agency-shaped, as expected, and the pay rates are published
(`payRate.min`, hourly or salary) which most employer boards do not do.

`robots.txt` was read before anything was fetched. It is unusually explicit —
it names search and AI agents and states that "every public page is open",
closing only `/wp-content/uploads/`, the WordPress admin, internal site search,
and the job board's **logged-in** areas (`/jobs/applied`, `/jobs/saved`,
`/jobs/profile`). `/all/jobs` is unauthenticated and not disallowed, and job
search results and job pages are permitted by name.

## the location filter was silently dropping worldwide-remote work

`classify_location` sorts every posting into a bucket and the sweep keeps only
some of them. For most of this tool's life that list was `CA,NA-REMOTE,MULTI`,
and the failure it caused was invisible precisely because it looked like
correct behaviour: nothing errored, no board went quiet, the digest just never
mentioned a whole class of job.

Four wordings fell through to `OTHER`, which is dropped:

| location string | old bucket | new bucket |
|---|---|---|
| `Remote, Global` · `Global Remote` · `Home based - Worldwide` | `OTHER` | **`GLOBAL`** |
| `Americas Remote` | `OTHER` | **`NA-REMOTE`** |
| `Americas` · `Home Based - Americas` | `OTHER` | **`NA-REMOTE`** |
| `London / Dublin / Hungary / Toronto / Berlin` | `OTHER` | **`CA-OPTION`** |
| `Calgary, Alberta / Atlanta, GA / Arizona` | `US-ONLY` | **`CA-OPTION`** |

Each has its own cause. **Global remote** had no rule at all. **`Americas
Remote`** did have one — `remote\s*[-,]?\s*americas?` — but only in that word
order, so `Remote - Americas` classified and `Americas Remote` did not; that one
regex hid Customer.io's entire Americas lane. **`CA-OPTION`** is the subtlest: a
Canadian city in a multi-location req lost to the foreign veto, because
`NOT_CANADA` saw Germany in the same string and threw the whole posting away.
Judging each `/`-separated segment on its own fixes it, and `canadian_option`
recurses exactly one level deep because a segment contains no separator.

`GLOBAL` is kept deliberately narrow. A bare `Remote` or `Fully remote` does
**not** qualify, because in practice those mean remote-within-the-US, and
`work from anywhere in the United States` says so outright — the regex carries a
negative lookahead for it. `Latin America Remote` is the mirror-image trap on
the Americas rule: the reverse word order would have admitted it, so
`AMERICAS_LATAM` vetoes latin/south/central — and `latam` — explicitly.

The fourth wording is the same rule read one notch wider. `Americas Remote`
was fixed by matching either word order, but both orders still need the word
`remote` to be present, and two wordings in the wild do not supply it: a plain
`Americas` (how **every Resend req** is filed, including its `Platform
Engineer`) and `Home Based - Americas`, the EOR phrasing, which accounted for
**74 postings in one sweep**. A bare `Americas` is matched **plural only**,
because an unqualified singular `America` is vanishingly rare while
`Americana` is both a Brazilian city and a Los Angeles mall — all three appear
in real location fields, and the singular would have claimed them. Dropping
the `remote` requirement from the veto too means `Americas (LATAM)` is still
rejected; `North & South America` survives because `NORTH_AMERICA` claims that
string one branch earlier.

### vendor-family titles do not ride in on GLOBAL

The first measured run of this change was wrong in a way worth recording.
`GLOBAL` exists only because a req is open to the whole world, so it carries no
evidence that *this role* suits you — and the vendor-family rules (support,
sales, field, solutions engineer) are the loosest in `TITLE_RULES`. Admitted
together, they put seven Canonical support and sales reqs **above every real SRE
role in the same batch**: `Associate Linux Support Engineer` scored 24 while
`Site Reliability / Gitops Engineer` scored 15. Support roles are excluded by
request, so `NO_VENDOR_BUCKETS = {"GLOBAL"}` drops them. `CA-OPTION` is
deliberately *not* in that set: a named Canadian city on the req is the same
evidence `CA` has, where vendor-family already counts.

### what it actually changed, measured over one full sweep

56,524 postings, 505 boards:

```
bucket        before    after   delta
CA             11400    11400      +0     <- bit-identical
MULTI           1257     1257      +0     <- bit-identical
NA-REMOTE        128      147     +19
GLOBAL             0      148    +148
CA-OPTION          0       15     +15
OTHER          30189    30009    -180
US-ONLY        13550    13548      -2
```

**Postings kept before and dropped after: 0.** The change is purely additive by
construction — `shadow`/`classify_location` returns the existing verdict
untouched whenever `keep` is already true, so nothing that reached the tracker
before can be reclassified or lost. That also means no migration: unlike the
scorer rebuild, no tracker row needs rescoring.

20 postings are newly admitted with a matching title, 0 of them vendor-family.
**Only 9 actually surface**, because `--max-age 30` suppresses the rest, and
that is the honest number — the scoping pass that predicted 17 had not applied
the age filter. The suppressed 11 are evergreen reqs nobody ever closes:
Canonical's `Site Reliability / Gitops Engineer` is **2,678 days** old and its
`Senior SRE / Gitops Engineer` 1,071, while Metabase's `Senior SRE/DevOps
Engineer` is 2,359. Ashby boards supply no `posted` date before hydrate, so
Supabase's rows carry `age=None` and pass — pre-existing behaviour, not
something this change introduced.

The nine: eight Supabase platform/SRE reqs (`Remote, Global`, scoring 29–45) and
the ElevenLabs `Infrastructure Engineer` filed in **Toronto** at 29.

### the bare-Americas rule, measured on its own

The same 56,524-posting snapshot, classifier before vs after that one rule:

```
NA-REMOTE        147 ->   221    +74
OTHER          30009 -> 29935    -74
```

Every other bucket bit-identical, **0 kept before and dropped after**. All 74
are Canonical `Home Based - Americas` rows and exactly **one** carries a
matching title — `Telco Cloud Field Engineer`, vendor family, tier 3 — which
`--max-age 30` suppresses anyway at **1,645 days** old. So the measurable gain
is not in the snapshot at all: it is Resend, added after the snapshot was taken,
whose entire board is filed `Americas` and whose `Platform Engineer` and
`Security Engineer, Platform` were invisible for that reason alone.

That single Canonical hit is worth recording for a second reason. `NA-REMOTE`
is **not** in `NO_VENDOR_BUCKETS`, so vendor-family titles do count there. The
snapshot's 147 pre-existing `NA-REMOTE` rows contain **zero** vendor postings
and this rule adds one stale one, so the pressure that justified the `GLOBAL`
gate does not exist in this bucket — gating `NA-REMOTE` too would retire a
working bucket/family combination to fix nothing that happens.

### blast radius

Two edits in one file. `bucket` is never written to `applications.md` and is
never read by `serve.py` or `serve_page.html`, so the web app needed nothing;
neither `daily.sh` nor `fast.sh` passes `--bucket`, so both inherit the new
default. The only other place a bucket is recomputed is the Workday
`N Locations` path inside `hydrate`, which calls `classify_location` and so
picks up the new rules for free — the post-hydrate re-filter repeats the
vendor-family condition so a Workday row cannot enter `GLOBAL` through it.

### the class this does NOT fix

Two Canada-eligible reqs stay invisible because their location field carries no
Canadian signal at all, and only the JD does:

- KoBold `Staff Client Platform Engineer` — filed `Remote - US`, JD reads
  *"open to candidates residing anywhere in the United States or Canada"*.
- Databento `Site Reliability Engineer` — filed `Remote / San Francisco / New
  York / Salt Lake City`, JD reads *"100% remote, teammates living around the
  globe and paid in their local currency"*.

This cannot be fixed with a location rule, and it cannot be fixed cheaply with a
JD rule either: the evidence lives in the description, descriptions are only
hydrated for finalists, and finalists are chosen *after* the location filter
runs. The pool is **286 US-ONLY postings carrying a tier-4 title**, so checking
them means 286 extra fetches per sweep — far too much for a lane that fires
every five minutes. If it is ever worth doing it belongs in a separate
once-daily pass, not here.
