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
  374 boards    ~19,500     ~200        ~200      ~200     new only
                postings   survive    descriptions
```

**The order is the only interesting part.** Full job descriptions for 19,500
postings is roughly 80 MB per run. Filtering on title and location first — both
available in the cheap list call — cuts that to ~200 detail fetches. Whole run:
**~45 seconds** for 374 boards expanded into 672 concurrent tasks.

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

### Workday — four traps

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
`total: 0`, so a naive `while offset < total` loop stops after two pages. This
capped results at exactly 40 per tenant until it was caught.

**3. Enterprise tenants are enormous.** One bank had ~1,029 open Canadian reqs;
paging all of them costs ~51 requests to then discard 98%. Sending keyword
queries and deduping on `externalPath` is far cheaper.

**4. Tenant names are not derivable.** Loblaw's Workday tenant is `myview`.
RBC's site is `RBCGLOBAL1`. Brute-forcing tenant/cluster/site combinations
returned zero hits across 1,755 attempts — the only reliable route is resolving
a real careers URL.

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

374 boards is roughly **1%** of that. Worth stating plainly rather than implying
completeness.

### Diminishing returns, measured

| Effort | Result |
|---|---|
| 163 → 366 boards | **+4 matches** |
| 94 employers hunted for tenants | 6 tenants → **+4 matches** |
| 12 VC portfolio pages | 17 boards → ~0 matches |

The funnel explains it:

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

**Retries in the transport layer.** At 672 concurrent tasks a single network
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
