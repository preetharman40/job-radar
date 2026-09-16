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

**The honest caveat, measured:** the median matched posting is **13 days old and
still open**. Speed is a real edge on maybe four reqs a week, not a general one.
Anyone building this should know that before optimising latency.

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
`boards-api.greenhouse.io/v1/boards/{token}/jobs`. Clean JSON, `updated_at` is
ISO-8601. `content=true` returns the full description but inflates the payload —
fetch descriptions per-job in the hydrate pass instead.

`boards.greenhouse.io` now 301s to `job-boards.greenhouse.io`. Anything matching
on the old domain silently misses.

### Lever
`api.lever.co/v0/postings/{token}?mode=json`. The only adapter with a true
creation timestamp (`createdAt`, epoch ms) — everything else exposes
last-modified, which conflates a new req with an edit to an old one. Ships the
full description in the list call, so no hydrate pass needed.

### Ashby
GraphQL at `jobs.ashbyhq.com/api/non-user-graphql`. Where most recent scale-ups
are. Descriptions need a second query per posting.

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

**3. Query handling is hostile.** Multi-word `q=` values are OR'd into noise —
`q=site reliability` returns anything matching *"site"*. Use single tokens. And
adding `sortColumn` makes the endpoint **ignore `q=` entirely** and return
everything by date, which looks like a working search returning irrelevant
results.

### Phenom — often just a skin

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
