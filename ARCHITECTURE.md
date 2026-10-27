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
  544 boards    ~56,500     ~200        ~200      ~200     new only
                postings   survive    descriptions
```

**The order is the only interesting part.** Full job descriptions for 48,000
postings is roughly 80 MB per run. Filtering on title and location first — both
available in the cheap list call — cuts that to ~200 detail fetches. Whole run:
**~215 seconds** for 544 boards expanded into 1,126 concurrent tasks. Job Bank
accounts for about half the growth from ~130s on its own: it honours a 5s
`Crawl-delay` on every request, list and hydrate alike, so a run with 16 Job
Bank finalists spends ~105s of that total waiting on purpose.

Workday and SuccessFactors targets fan out to one task per search keyword rather
than one per tenant — `adapters.expand()` asks the adapter — so a nine-tenant
sweep parallelises across the shared pool instead of running ten queries back
to back. That change alone took a run from over two minutes to under thirty
seconds.

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

## the mail pipeline resolves employers by NAME, because a LinkedIn alert has no board in it

`inbox.py --boards` has always read ATS account tokens out of the links in an
alert email. That works for mail sent *by* an ATS, and it cannot work at all
for the sender that dominates the channel. A LinkedIn digest links only to
`linkedin.com/jobs/view/{id}` — there is no Greenhouse token, no Lever token,
no careers host anywhere in the message — so `board_from_url` has nothing to
read. The measured consequence: **135 of 345 tracked rows (39%) are LinkedIn
URLs**, each one a posting with no board behind it, which is why a role could
be read on the phone and then never be found in the web app by company.

The employer's **name** is the only handle such a message gives. Turning that
name into a board is `discover.resolve_employer`, and `inbox.py --employers`
runs it over every employer the mail names.

This is the one expansion channel that escapes the verdict in *Coverage is an
allowlist* above — "adding boards is the obvious lever and nearly the worst
one". Brute expansion sweeps 425 Amazon reqs to find one match. This is
**demand-driven**: the employers that turn up are the ones already mailing
about these titles in this geography, so the Canada density is far higher than
anything a remote-jobs directory produces. The first 11 boards it found run
trolley 23/26 Canadian, smiledigitalhealth 12/16, shyftlabs 17/25,
coconutsoftware 3/3, layerzerolabs 6/7, maintainx 98/197.

### a name probe plus HTTP 200 is not identity, so there are two checks

An earlier expansion pass added four boards that answered 200 under a
plausible name and belonged to somebody else. A token is only vetted here when
it passes both of:

1. **Identity — the title from the email is still listed on the board.** This
   evidence is specific to this channel and available nowhere else in the
   tool: the employer is in the mail *because of that requisition*, so a board
   still carrying it is that employer's board and not a lookalike. It is what
   rejected `greenhouse/nasdaq`, which answers 200 with two postings — an AVP
   and a sysadmin — and nothing resembling the mailed `DevOps Cloud Engineer`.
   Matching is containment in either direction, because the two sources trim
   differently (the mail said `Cloud Engineer`, Smile Digital Health's board
   says `Cloud Engineer, Automation (Remote Ontario)`), with an 8-character
   floor so `SRE` cannot match half the internet.
2. **Value — the board carries a matching title in a location the radar would
   keep.** Measured on the board's own contents, deliberately *not* on the
   alert's title: an employer worth polling can easily appear in one week's
   mail under a Sales req, and dropping Autodesk on that basis would be the
   expensive mistake — the same reasoning that makes `all_job_links` ignore
   the title filter. It is also what keeps a seniors' home that mailed one
   `Second Cook` out of `targets.json`.

   Both halves are the tool's own existing definitions — `title_match` and
   `classify_location` — so the test asks exactly "would this board ever have
   shown me a row" rather than inventing a second rule set. The location half
   is not decoration. `ashby/ramp` carries 173 postings, 25 of them Canadian
   and 3 with matching titles, but the matching three are all New York and San
   Francisco, so the intersection is empty; a title-only test would have added
   it for nothing. `ashby/cerebras` has 7 in the intersection. Without the
   location half an auto-adding nightly job drifts into exactly the board
   bloat that *Coverage is an allowlist* warns about, and because a miss is
   re-probed once `stale_days` elapses, a US company that later opens a
   Toronto infra req is picked up then rather than never.

   `title_match`'s second argument is the **company**, not the platform — it
   is what decides `is_vendor`, and a vendor-family title only counts at a
   vendor. The adapters set each posting's company to the board token, which
   is what the sweep passes, so this passes the same thing. Judging a board by
   different inputs than the sweep uses is how a token gets added that then
   behaves differently in production.

Only `verified` — both checks — is ever added. `unverified` and
`no-target-roles` are reported and left alone.

The two checks also catch a trap this repo has already paid for once.
`recruitee/accenture` resolves from the mail, answers HTTP 200 and returns 2
postings — and it is the **demo account** documented in `_recruitee_note`, one
of five that were added on the strength of a status code in an earlier pass and
later removed. Identity rejects it, because `Senior Marketer (Sample)` is not
the title that was mailed.

The identity check has one honest failure mode: **mail is days old and a req
can close before the probe runs.** MaintainX verified on
`Site Reliability Engineer` one afternoon and failed the same check 40 minutes
later, because the board had gone 197 → 195 postings and that req was one of
the two. The mitigation is to pass *every* title the employer appeared under,
not one — MaintainX re-verified immediately on
`Infrastructure Developer, Platform Engineering`.

### the logo `alt` is where the employer name actually lives

A digest glues the company onto the end of the title with no delimiter —
`Infrastructure Engineer ElevenLabs` — and the old subtraction rule could only
recover it when the title had its own anchor to subtract. The employer name is
present as a *field* in exactly one place: the `alt` of its logo image, inside
that job's own segment.

Measured over **349 records from 65 digests** across a fortnight:

| | before | after |
|---|---|---|
| records parsed | 349 | 349 |
| with an employer name | 305 | **348** |
| titles the rules match | 226 | **232** |
| records lost | — | **0** |

Used as a fallback rather than a replacement: on all 305 the subtraction rule
already got right the two agree, differing only by a trailing period. The rule
is not wrong, just incomplete.

Naming the employer then unlocked two things that were impossible without it.
The company can be **cut off the end of the title**, so `DevOps Analyst TMX
Group` is filed as `DevOps Analyst` — a title carrying the employer's name
scores against the wrong blob. And where every anchor in a segment was chrome,
the title can be recovered by subtracting the company off the row instead,
which is how adidas' `Platform & Site Reliability Engineer`, BDO's `DevOps
Engineer - New Grad (January 2027)` and four more stopped being filed under
`Learn why we included this.`

That last bug is worth recording for how it was fixed. Blocklisting the junk
anchor by name fixed six rows and immediately mis-titled eight others as
`Help`, then as `Manage recommendations` — the account footer falls inside the
last segment and every one of its links is a plausible 4-to-140-character
string. The durable fix is structural: **consider only anchors whose href
points at this segment's own job id.** A footer link never does, so LinkedIn
cannot add a seventh one and break it again.

### the request budget is the design constraint

Probing names is the only thing in the tool that fires dozens of requests at
one host in seconds, and an unpaced run of it has drawn 429s from Ashby
before. Three measures keep it affordable:

- **Tiered variants.** Tier 1 is the plain concatenation, the dashed form, and
  both with any legal suffix stripped; tier 2 adds `+inc`, `+hq`, `+labs` and
  the rest. Of the 12 boards this channel has found, **all twelve answered on
  tier 1** — not one needed a dash or a suffix. Tier 2 is ~80% of the requests
  (48 per miss against 12), so the nightly run stops at tier 1 and
  `--employer-deep` turns it on for a backfill. Stripping the legal suffix is
  what makes `Tecsys Inc.` probe `tecsys` at all; only true suffixes are
  stripped, never descriptive words, because taking `Solutions` off
  `Enterprise Solutions Inc` would probe `enterprise`, a live Recruitee *demo*
  account (see `_recruitee_note`).
- **A 404 is an answer, not a blip.** `discover._fetch` retried every failure
  twice with 1.5s and 3.0s of backoff, which turned a definitive "no board"
  into 4.5 wasted seconds — and a miss 404s on nearly every variant. Ruling an
  employer out took **~30s before, ~4s after**; only `408/425/429/5xx` are
  retried now. `job_radar._req` deliberately keeps retrying everything, because
  there a blip silently drops a whole board from a 1,100-task sweep.
- **Everything is cached** in `employer_board`, keyed on the name. A fortnight
  of mail names ~200 distinct employers; re-probing all of them nightly would
  be thousands of requests a day at four hosts to re-learn what it already
  knew. A miss is re-probed after 60 days — a company without a board today may
  have one next quarter — and a hit never needs re-probing, because the board
  either entered `targets.json` or is reported again from cache for free.

The scan and the title fetch are also split: the cheap single-request probes in
`PROBES` rule names out, and only the winning token is read through the
radar's own adapter, which is the one that has to agree with what the sweep
will later see. One expensive call per employer instead of up to 36.

### "no board found" must not mean "not already polled"

A name probe covers the four platforms whose account names are guessable, so a
tenant-based board always comes back dead. The first run of the report believed
that and listed **RBC, BMO, Manulife, Scotiabank, TMX Group, PwC Canada,
Interac, Strathcona Resources, Amazon and Insight Global** under "no board
under any name variant" — every one of them a board the radar had been polling
for weeks on Workday, SuccessFactors, Phenom or an in-house adapter. The report
was telling you to go hunt careers URLs for boards you already had.

The already-polled test therefore runs against every platform in
`targets.json`, matching the employer name and its legal-suffix-stripped form
against both the string tokens and the `name` of every dict target. It matches
on those two forms and never on a prefix: `Interac Corp` has to reach the
Workday target named `Interac`, but `BMO Capital Markets` stays listed as
missing rather than being silently credited to `BMO`. Over-claiming here
*hides* a board worth adding, which is the more expensive of the two errors.

### what it cannot reach

Every enterprise it misses, it misses structurally. `--company` probing covers
Greenhouse, Lever, Ashby and Recruitee, whose account names are *guessable from
a company name*. Workday, SuccessFactors, Taleo and Oracle are **tenant-based**
— Loblaw's Workday tenant is `myview` — so no token guess can ever find them.
BMO, WestJet, Tecsys, CI Financial, Tangerine, Porter Airlines, Roche, Alstom,
Broadridge, Livingston, OCS, MDA Space and Stikeman Elliott all came back dead
for that reason and not because they are unreachable. Each needs one
`discover.py --url <careers page>`, which costs a single request but requires
the careers URL — the one input this pipeline cannot derive.

---

## title rules: a tier-0 title is invisible, not merely ranked low

`title_match` is the gate. A title it scores 0 never reaches `score()` at all,
so a recall gap there is categorically worse than a weighting gap - the posting
does not appear anywhere, at any threshold.

Measured on 205 distinct titles from a fortnight of real alert mail: 111 were
infra-shaped and **15 scored tier 0**. The causes were specific, not diffuse:

| missed title | why |
|---|---|
| `DevSecOps Engineer` and 4 variants | `\b(devops\|dev-ops)\b` cannot match *devsec*ops |
| `Dev Ops I`, `Dev Ops II, Public Cloud` | the space form was absent |
| `Kubernetes (K8S) / HPC Engineer` | `\bkubernetes\s+engineer` demands adjacency |
| `Platform Administration Lead` | only `platform engineer` existed |
| `VMware Cloud Foundation (VCF) Operations Engineer` | `cloud operations` demands adjacency |
| `Software Engineer - Infrastructure` and similar | the rule needs `infrastructure` BEFORE the role noun |

Adding rules is monotonic - `title_match` returns the best tier among all
matching rules, so a new rule can only raise a title, never lower one. The A/B
over the 56,524-posting snapshot confirms it: **0 titles lost a tier.**

### the broad fix was wrong, and only measurement showed it

The obvious rule for the last row is "`infrastructure` anywhere AND a role noun
anywhere". It admits **268 postings, 194 distinct titles** - and the additions
are `Specialist, Infrastructure Planning`, `MEP Engineer - Building &
Infrastructure`, `Engineer in Training I Infrastructure Planning`, `GIS Industry
Solutions Specialist - Infrastructure`, `Manager, AMS/SAP Platform
Administration`, `Sr Program Manager, GTM Platform Operations`, and a wave of
`Senior Java Engineer - Core / Infra - Elasticsearch`. Civil engineering, SAP,
GIS sales, programme management and backend software - almost none of it this
search. Constraining it to adjacent-reverse order still admitted 56.

So that rule was **dropped**, and the three that survived were each measured on
their own against the same snapshot:

| rule | extra postings admitted | verdict |
|---|---|---|
| `dev[\s-]?sec[\s-]?ops\|dev[\s-]?ops` | 21 | kept - nearly all real DevSecOps reqs |
| kubernetes/k8s within one word of `engineer` | **1** | kept - the unbounded form admitted 19 coding roles |
| `platform (operations engineer\|administrat…)` | 14 | kept - requiring the role noun kept out 22 PM/support roles |
| `cloud …(≤26 chars)… operations engineer` | **0** | kept - free; the title it rescues arrives by mail |
| `infrastructure` + role noun anywhere | 268 | **dropped** |

Net: **36 newly matched, 0 lost**, and 9 of the 36 also clear the location and
level filters - `Data Platform Operations Engineer` at Scotiabank Toronto,
`Senior AI Platform Operations Engineer` at EQ Bank, `Senior IT Platform
Administrator` at Desjardins, `Dev Ops` reqs at Conga and David Aplin.

Two details that matter for anyone widening these further. The
`(?<!power )` lookbehind on the platform rule is load-bearing: `Power BI & Power
Platform Administrator` is an M365 reporting role and it arrived in the same
fortnight of mail as the titles above. And `Manager, AMS/SAP Platform
Administration and Support` still matches, harmlessly - `level_of` reads
`Manager` as exec, which is -10.

---

## body negative signals: measured, and NOT implemented

The plan was to penalise two things the scorer cannot see, because `level_of`
reads seniority from the title and nothing read the description at all: US
security clearance or citizenship requirements, and demands for far more
experience than the profile has. Both were abandoned on measurement, and the
reason is recorded here so it is not re-attempted.

**US clearance is not what the pattern was finding.** A loose regex
(`security clearance|ts/sci|top secret|secret clearance|us citizen|…`) flagged
10 of the 135 hydrated JDs at a median score of 23 - above the notify threshold
of 15, which looked like a real problem. Reading the matches, **all ten are
Canadian**: five say "ability to obtain and maintain a **Canadian government**
security clearance", one requires **Reliability** status, one "reliability
status security clearance", and one wants an active Top Secret clearance in
**Ottawa**. These are Government-of-Canada and Ottawa defence roles - desirable
in a Canadian search, not disqualifying - and one of them is the
highest-scoring posting in the corpus at **50**. Penalising them would have
demoted the best match in the corpus.

A tightened, genuinely US-specific pattern (`U.S. citizen`, `TS/SCI`, `public
trust`, `ITAR`, `green card`, `authorized to work in the United States` without
a nearby `canada`) matches **0 of 135**. Six JDs carry Canadian clearance
language and none of them trip it, which is the control that proves the
tightening works - but with no positives there is nothing to score.

**Experience demands point the wrong way.** JDs demanding 10+ years alongside
the word "experience" score a median of **29** against a corpus median of
**27**. Asking for more years correlates with a slightly *better* keyword match
here, because those are the deep infrastructure reqs. Senior titles are already
handled where they belong, by `level_of`, at -2.

The honest conclusion is that on the corpus available, body negative signals
are a solution to a problem that is not there. The eligibility question is real
but it lives in the **US-ONLY pile that the location filter already drops**,
not in the postings that survive it.

### two claims that measurement killed

Recorded because both sound obviously true and both are false here.

**There is no length bias.** `r(blob length, score) = +0.06`, and the median
score is identical - 25.0 - in the shortest and longest quartiles of the
corpus. No length normalisation is warranted. The one caveat: hydrate truncates
at 8,000 characters, so the corpus spans ~1.9k-8.5k and this says nothing about
the ~200-character mail snippets, whose cap is already known and handled by a
separate threshold.

**Correlated skill groups barely stack.** The worry was that
kubernetes + helm + ingress + kustomize + argo cd + gitops gives 13 points for
what is one skill. In practice only **9 of 135** JDs fire two of those six and
**none fire three or more**.

### the gap that measurement does support

Coverage is one-directional. The score counts what the JD names that is on the
resume and never what the JD demands that is not, and
`r(score, count of non-resume technologies demanded) = **+0.46**` - positive,
so the kitchen-sink JDs rank highest. The top of the list reads `Engineer,
DevOps` at 46 while also demanding java, ruby, php, snowflake and elastic, and
`Senior Platform Engineer, AI & Fabrics` at 45 demanding java, splunk, datadog,
typescript and azure. Those are polyglot backend roles topping a DevOps search.
Unfixed; it changes the score scale, so it needs the thresholds retuned with it.

### outcome data does not exist yet

`applications.md` has a `toapply -> applied -> interviewing -> rejected`
schema, and weights could in principle be fitted to it. Parsed locally over 368
scored rows: **351 `toapply`, 17 `applied`, and 0 rows in any state beyond
`applied`.** There is no reply, interview or rejection data, so no weight can be
fitted to outcomes today.

What the file does show is a validation rather than a criticism: the median
score of the roles chosen for application is **41**, against **21** for those
never applied to. The scorer already agrees with the human judgement that
followed it, by roughly 2x.

---

## the coverage gap was an artifact of a biased corpus and a careless regex

The claim was that the scorer is one-directional - it counts what a JD names
that is on the resume and never what the JD demands that is not - evidenced by
`r(score, count of non-resume technologies demanded) = +0.46`. That number came
from the 135-JD corpus, which was captured from **fast.json**: the boards
already selected for producing matches. Re-measured against 217 JDs captured
from a full 544-board sweep:

| | fast-lane corpus (135) | full-sweep corpus (217) |
|---|---|---|
| `r(score, distinct foreign tech)` | **+0.46** | **+0.08** |
| `r(score, max mentions of one)` | - | **-0.04** |
| `r(score, blob length)` | +0.06 | **-0.00** |

**160 of 217 JDs name no foreign technology at all.** The correlation was
mostly the corpus, and the rest was the measurement counting things that are
not demands:

- `proficiency in scripting languages (e.g., python, bash, java, ruby, php,
  powershell)` counted as four unmet demands. It is one demand, offering six
  alternatives, and python and bash are both named and both his. That JD -
  `Engineer, DevOps` at Recurrent Energy, score 46 - was the single worst
  "offender" in the original measurement and is a straightforwardly good match.
- `at least one enterprise observability platform (datadog, splunk, grafana,
  new relic or similar)` - grafana is his, so the requirement is **met**.
- `...openai, snap inc., and snowflake` is a **customer list**.
- `...sequoia capital, and spark capital` is a **venture firm**, not Apache
  Spark. A bare `\bgo\b` for golang has the same defect.

Counting only enumeration markers caught a third of it; reading the
"standalone" two thirds showed nearly all of those were alternations too.

The honest test is frequency, not presence: a role really built on a foreign
stack names it repeatedly. Across 217 JDs only four name one more than three
times, and the scorer already ranks the three genuine mismatches at the bottom
of the distribution without any help, because a Salesforce or SAP role does not
hit DevOps skill groups:

```
score 23   DevOps Engineer(Salesforce)                 salesforce x20
score 11   Business System Analyst - Pharmaceutical    salesforce x5
score  8   IT - Senior Solution Systems Analyst        sap x6
score 39   DevOps Engineer                             java x4   <- real DevOps role
```

Median score is 28. **No fix was made, because there is nothing to fix.**

### thresholds were not retuned either, and the reason is the dedupe

`--notify-min-score 15` admits **181 of 217** scored postings - 83% - which
looks far too loose, and 16 of the 17 roles actually applied to score 30 or
more. That argues for raising it, until you notice that a threshold governs a
*per-sweep* pass rate while a notification only fires for a posting not already
in `seen`. Measured from the log: **58 push events and 93 notifications over
~40 days**, about 2.3 a day from the daily lane, with 47 of the 58 pushing
exactly one. New rows entering `seen` run at a median of 8 a day. There is no
alert-volume problem for a higher threshold to solve, and raising it would
discard the one applied role that scored 14.

The lesson worth keeping: the pass rate of a threshold is not the volume it
produces, whenever a dedupe sits downstream of it.

---

## two lanes, one writer: `database is locked`

Four `sqlite3.OperationalError: database is locked` tracebacks, all at the
`INSERT OR IGNORE INTO seen` that claims a posting. `seen.sqlite3` is WAL with
`busy_timeout=30000`, so this means another process held the write lock for
more than thirty seconds.

The intuitive culprit is the full sweep, which upserts one `live` row per
*scanned* posting - 56,524 of them - inside the same transaction as the `seen`
claims, committing only at the end. Measured: that `executemany` plus commit
takes **0.27s**. Not it.

It was `inbox.py`, in two places, both holding a write transaction across
network I/O because the first INSERT opens one implicitly and nothing committed
until the work was done:

| window | transaction opens | held across |
|---|---|---|
| the message loop | first `mail_seen` insert | every remaining `M.fetch()` - 175 IMAP round-trips on a `--days 14 --all` backfill |
| `report_employers` | first `employer_board` insert | the whole probe loop, minutes on a backfill |

The second is why this matters in production rather than only during a manual
backfill: `inbox.py` runs from cron at 19:52 with `--add-employer-boards`, and
the fast lane's last run of the day fires at 19:55.

Both now commit per row. At these volumes - a few dozen messages, a handful of
new employers a night - a commit per row costs nothing and bounds the lock to a
single insert. Reproduced and verified in isolation: a writer that inserts then
sleeps makes a second connection fail with `database is locked`, and the same
writer committing per row lets it through.

### the failure was invisible, which is its own bug

`fast.sh` sent stdout to `/dev/null`, so the only trace of a dying fast lane
was a bare traceback in `fast.log` with nothing around it - no timestamp, no
indication of which run it belonged to, and no record of what that run would
have pushed. The full digest genuinely is too much to keep 156 times a day, so
the lane now logs just its summary line and any `>> pushed` / `>> Tracked`
lines, timestamped: one line on a quiet run against 1,162 for the digest.

---

## robustness: what actually failed, and what was only waiting to

Worth stating the record first, because it changes what is worth fixing. Over
the 31 days to 2026-10-26 the daily lane recorded **26 of 26 sweeps every
single day**. Every failure in the tool's entire log history is six
`sqlite3.OperationalError: database is locked` - four fast-lane runs, one daily
sweep, one verify_tracked - and all six happened on 2026-10-27, caused by a
manual `inbox.py --days 14 --all` backfill holding the write lock across 175
IMAP round-trips. (`web.log` also carries 7 BrokenPipeError and 3
ConnectionResetError, which are browsers closing tabs.)

So the tool was not flaky. One long-running manual command made it flaky, and
it could do so because of four real weaknesses:

### 1. a blocked write threw away the whole run

`seen.sqlite3` is WAL with `busy_timeout=30000`, and five processes write to
it - both poller lanes, verify_tracked, board_health, inbox.py - coordinated by
nothing but cron's clock. Contention is a normal condition, not an error. But
when the 30s wait expired the exception propagated and killed the process,
discarding work that was already complete: 1,126 board fetches for a full
sweep, every HTTP check for verify_tracked.

`db_write(conn, fn, …)` retries instead, up to 4 attempts inside a **75-second
total budget**. The budget matters: each attempt can itself block for the full
`busy_timeout`, so four unbounded attempts would be ~129s, and the fast lane
runs on a 5-minute cadence and already takes ~148s - an unlucky run would
overrun into the next one and *become* the contention it was waiting out.
Verified against a deliberately held 40-second lock: the write succeeded at
38.7s where it previously raised at 30s.

Three consequences shaped how it is applied:

- **The sweep's two writes are now separate transactions.** Losing the `live`
  liveness stamp costs one cycle of prune_live accuracy; losing the `seen`
  claims means either a missed notification or a duplicate one. They no longer
  share a fate.
- **`fresh` is rebuilt on every attempt.** A retry re-runs the INSERTs after a
  rollback, so appending without clearing would count a posting twice and
  notify twice.
- **A final failure is no longer fatal.** The digest still prints, because the
  matches are real and the fetching is done; notify and track are suppressed
  instead, since nothing was claimed and the next sweep will claim it properly.

`verify_tracked` and `board_health` had the opposite structure - execute inside
the loop, commit at the end - which **cannot be retried at all**, because
`db_write` rolls back before retrying and that would discard every row the loop
had queued. Both now collect rows and write once through `executemany`.

### 2. a dead lane was silent

Nothing reported the failed 14:42Z sweep. It was found by grepping logs by
hand, which is not monitoring. `cronwrap.sh <label> <cmd…>` runs a job and, on
a non-zero exit, pushes the last 12 lines of its output to the same ntfy topic
the job alerts use. Six lanes are wrapped: fast, daily, verify, health, prune,
inbox. The topic is read the same way `job_radar.py` reads it and is never
echoed to stdout.

### 3. nothing caught a lane that never starts

`cronwrap.sh` needs an exit code, and a crashed cron daemon, a sleeping machine
or a `.sh` that cron refuses produce none. The symptom is the worst kind: the
radar looks fine and quietly serves yesterday's jobs. `board_health` now also
checks how old the newest full sweep is and alerts past `STALE_SWEEP_HOURS`
(20). It runs at 06:50, before the lanes start, so the newest sweep is normally
from about 19:41 the previous evening - roughly 11 hours - and 20 hours means a
whole evening of sweeps never happened.

This cannot catch everything: if cron itself is dead, so is the check. Only an
external observer can close that gap, and there is not one.

### 4. fast.json could be silently destroyed

`make_fast.py` derives the entire fast lane from one digest's producer list, so
a digest that is truncated, from an incremental run, or from a half-failed
sweep yields a near-empty `fast.json` - and the fast lane then polls almost
nothing every 5 minutes while looking perfectly healthy.

This is not hypothetical. `digest.md` is exactly that trap: `daily.sh` writes
it on every run that found something, but those runs are *incremental*, so it
lists only the new rows. Pointing `make_fast.py` at it looks right and is
wrong - tested, and it would have written **1 board in place of 81**. It now
refuses any drop beyond 40% and writes through a temp file and `os.replace`.

### the remaining sync gaps

- **`fast.json` drifts.** It is regenerated by hand, and sat at 69 boards while
  `targets.json` grew past it, so a productive new board reaches the 5-minute
  lane only when someone remembers. Safe to automate now that the shrink guard
  exists, but it needs a weekly `--all` sweep because an incremental digest
  cannot produce the producer list.
- **`board_health` lags `targets.json` by one run** - it recorded 529 boards
  while targets held 544. Self-correcting: the next run sampled all 544.
- **135 tracked rows are LinkedIn URLs** that `verify_tracked` will not fetch,
  so they never age out. Resolving employers to boards stops new ones
  accumulating; the existing 135 need a policy decision.

---

## the watchdog cannot live in cron, because cron is what it watches

`cronwrap.sh` reports a lane that fails and `board_health` reports a sweep that
is stale, but both are themselves cron jobs. If cron stops, every check stops
with it, and the symptom is the worst available: the web app keeps serving
yesterday's jobs and looks perfectly healthy.

`watchdog.sh` therefore runs from a systemd **user** timer. The user manager is
started by PID 1, `Linger=yes` is already set so it survives logout and reboot,
and its timer fires whether or not cron exists. Every 15 minutes it checks the
three things cron cannot check about itself:

1. **`cron.service` is active.**
2. **The crontab still contains its jobs.** `crontab <file>` silently replaces
   every line, and there is no undo. This was nearly done twice by hand on
   2026-10-27 while wiring cronwrap in; a count is cheap insurance.
3. **A full sweep was recorded recently** - 2h during 08:00-19:00, 14h outside
   it. Two thresholds rather than one, because sweeps stop at 19:00 and the gap
   legitimately reaches ~11h by 06:50; a single 2h limit would alert every
   night.

Alerts are rate-limited to one per distinct problem per 6 hours, so a weekend
outage does not push every 15 minutes for two days.

### what it cannot do by itself

Restarting a system service needs privileges this user does not have, so the
watchdog attempts `sudo -n systemctl restart cron.service`, reports that it
could not, and alerts anyway. A watchdog that silently fails to act is worse
than one that says so. `SUDO_SETUP.md` holds the two root commands that close
the gap: `Restart=always` on `cron.service` (it currently has
`Restart=on-failure`, which covers a crash but not a clean exit or a
`systemctl stop`), and a sudoers rule permitting exactly that one command.

And it cannot catch everything. If the machine is off, or systemd and cron are
both gone, nothing running on this machine can report it. That needs an
external observer; there is not one.

### the web app was the other unsupervised thing

`serve.py` was a bare `python3 serve.py` started by hand - no restart on crash,
no survival across reboot. `job-radar-web.service.example` had been sitting in
the repo uninstalled since it was written. It is now installed as a user
service with `Restart=always` and `StartLimitBurst=0`, verified by `kill -9`:
systemd returned it in under six seconds.

---

## `--no-claim`: a reporting sweep must not touch notification state

`fast.json` is the subset of boards the 5-minute lane polls, derived from which
boards produced a match in one **full** sweep. It was regenerated by hand,
which meant it drifted - it sat at 69 boards while `targets.json` grew past
540, so a productive new board reached the fast lane only when someone
remembered. It is now refreshed weekly (Sunday 05:10, after prune_live and
before the lanes start).

Automating it needed a new flag, because both obvious ways to run the refresh
are wrong:

- **`--all` alone** claims every match it shows. That is correct for a normal
  sweep and silently destructive here: the refresh would consume whatever was
  new that morning and the real lanes would never notify it. This is the trap
  the README warns about - "`--all` without `--track` consumes postings into
  `seen`".
- **`--all --notify`** is worse. `notify()` reads the *shown* set, which under
  `--all` is every match - 181 of 213 on a measured sweep, all pushed to the
  phone at once.

`--no-claim` skips the `seen` write and forces `notify` and `track` off
together, so the run can only produce a digest. Verified on a real refresh:
`seen` held 476 rows before and 476 after, and `fast.json` went 81 -> 83.

The shrink guard in `make_fast.py` is what makes the automation safe rather
than merely convenient - see the robustness section above for why `digest.md`
would otherwise reduce the fast lane to one board.

---

## aggregator rows age by date, because they cannot be asked

`verify_tracked` deliberately refuses to fetch LinkedIn and the other
aggregators: the volume would be ~1,276 requests/day at a host that polices it,
and a logged-out page is a login wall, so the verdict would be noise. Absent
from `tracked_live` was the right call for "do not hide it".

It was the wrong call for "never changes". Measured on 2026-10-27: of 135
LinkedIn rows in the tracker, **0 were in `tracked_live` and 0 had a
`first_seen`** - mail rows never enter `seen`, so there was no state and no age
on any of them, and a req found in September was indistinguishable from one
found yesterday.

They are now aged on the one date that is definitely true for a mail row:
`- **Found:** YYYY-MM-DD`, which inbox.py writes. (`Posted` is
"? (unknown age)" for these, because a digest snippet carries no publication
date.) Past `AGGREGATOR_STALE_DAYS` (45) the row is marked **`stale`**, not
`gone`, and the distinction is the whole point:

- `serve.py` collects only `state='gone'` into its expired set, and
  `purge_expired.py` deletes on that field. A `stale` row therefore surfaces to
  the reader and **can never be deleted by the purge** - which matters, because
  one of these may be a job that was applied to, and purging by hand on
  2026-10-13 already destroyed two applied records that had to be restored from
  backup.
- `serve.py` exposes `stale` as its own flag beside `expired`, so the app can
  say "probably closed, unverifiable" rather than making a claim the tool
  cannot support.

45 days is deliberately generous: the radar already refuses anything over
`--max-age 30` at discovery, so a row found 45 days ago describes a requisition
that was open at least that long.

**It currently matches nothing.** Every one of the 135 rows reads
`Found: 2026-10-23` or later, because mail rows only began being tracked on
2026-10-22 when `--track-min-score` dropped to 12. The oldest is four days old.
This is a mechanism for a backlog that does not exist yet, which is the right
time to build it.

---

## ratecheck was crying wolf, and hiding its worst offender

Three bugs, all found by wiring `ratecheck.py` into `cronwrap.sh` and reading
what it printed.

**A substring is not a status code.** The test was `if "429" in line`. Across
40 days of `radar.log` that counted 714 "rate-limit errors" where 708 were
real; the other six were four sweep-summary lines whose scanned-posting count
happened to contain those digits, and two job URLs. Every genuine line has the
same shape - `- workday/TC Energy: HTTPError HTTP Error 429: Too Many
Requests` - so the pattern now requires 429 adjacent to an error, and cannot
match a job id.

**Grouping by UTC date splits a day's cron window in half.** Sweeps run
07:00-19:55 local, which is 13:00Z-01:55Z, so every evening sweep landed on the
*next* UTC day's row. The consequence was not cosmetic: the Ashby regression of
2026-10-26 evening was reported under **2026-10-27**, the day *after* the fix
went in, while the real 2026-10-27 sweeps were all clean. The script's own
"only alert on CURRENT trouble" logic then fired, because it compares against
`max(sweeps)` - so the old version pushed

```
!! Radar rate-limited: 14 429s in one sweep (2026-10-27), 39 over 3d
```

an urgent phone alert for a problem that had been fixed the previous evening.
The same log through the fixed version reads

```
note   : worst sweep in window was 14 on 2026-10-27T00:11Z,
         but 2026-10-27 is clean - already fixed
OK - no sweep exceeded 5 rate-limit errors
```

Grouping is now by local day. The sweep's own identity stays the **true UTC
stamp** exactly as the log prints it, with a separate `day_of` map for
grouping, because a composite "local-date + UTC-clock" key reads like a
timestamp and is not one - `2026-10-26 00:11Z` appears nowhere in the log and
could not be grepped for.

**`- (\S+):` cannot match a board name containing a space.** This is the worst
of the three, because naming the throttled board is the script's entire
purpose. `\S+` must be followed immediately by the colon, which fails on
`- workday/TC Energy:`, so every Workday tenant with a space in its name -
TC Energy, Sun Life, Canadian Tire - was silently dropped from the "boards
affected" tally. Over 45 days that is **146 of 714 errors attributed to no
board at all (20%)**, and the single worst offender, `workday/TC Energy` with
**116** errors, never appeared in the report. After the fix every error is
attributed and TC Energy ranks first.

Measured old vs new over the same log:

| | old | new |
|---|---|---|
| errors counted | 714 | **708** |
| attributed to a board | 568 | **708** |
| unattributed | **146** | **0** |
| days with errors (14d window) | `10-22: 1, 10-23: 1, 10-27: 39` | **`10-26: 39`** |
| verdict on a clean day | **false urgent alert** | `OK ... already fixed` |

The pattern across all three is the same one that produced four wrong
conclusions elsewhere on 2026-10-27: a regex that matches a token being read as
a fact about the world. `429` is not an HTTP status, a UTC date is not a local
day, and `\S+` is not a name.

---

## logging: the weekly cron was deleting the evidence

`17 3 * * 0 find radar -maxdepth 1 -name '*.log' -size +5M -delete`.

Not rotating. **Deleting.** Nothing had tripped it yet - the largest log was
841K - but it was certain to: `verify.log` grows 21K/day and would have passed
5MB in about **204 days**, `radar.log` in **362**, and at that moment every
record of what the radar had ever done would have been removed, silently, by a
cron job whose output nobody reads. A log you delete is a log you cannot
analyse.

Replaced with `logrotate` (already installed, run from the user's cron with its
own `--state`, so no root): 12 weekly generations, compressed, date-stamped
filenames rather than `.1/.2/.3` because reconstructing an incident means
opening the week it happened, not counting backwards through generations.

`copytruncate` for every log, including the cron ones that would not strictly
need it. `serve.py` runs under systemd with `StandardOutput=append:`, which
holds `web.log`'s descriptor open for the life of the process; renaming that
file would leave the app writing to an inode nothing can find and the log would
appear to simply stop - exactly the silent failure this is about. A separate
stanza for it is impossible, because `*.log` already matches it and logrotate
rejects a duplicate entry. The cost is a microsecond window between copy and
truncate where a write can be lost, which on logs written a few times an hour
is the right trade for never losing the file.

### the prose logs cannot answer the questions you actually ask

After something goes quietly wrong the questions are: when did this lane last
*succeed*, which run did the 429s start in, is it getting slower, did it run at
all on the 14th. Answering those from prose means regex archaeology over
`radar.log`, and doing that archaeology by hand produced **three** wrong
conclusions in `ratecheck.py` alone - a substring read as a status code, a UTC
date read as a local day, and `\S+` read as a name.

So `cronwrap.sh` now also appends one JSON record per run to `events.jsonl`:

```json
{"ts":"2026-10-27T17:42:10Z","label":"daily","rc":0,"secs":349,
 "shown":0,"matched":213,"scanned":56391,"boards":1126,"http429":0,"tracebacks":0}
```

Deliberately a **second view, not a change to the first**: `radar.log`'s format
is parsed by `ratecheck.py` and `make_fast.py`, and prefixing or reshaping it
would break both. Python does the encoding rather than hand-assembled JSON,
because a stray quote or newline in a job's output would otherwise produce a
line nothing can parse - precisely the failure this is meant to remove. The
whole block is best-effort and cannot change the job's exit status.

The captured `tail` is stored **only** when `rc != 0` or a traceback appeared.
The first version stored it on every run, because `rc` arrives from `argv` as a
string and `"0" != 0` is True in Python - a type confusion of the same family
as the regex errors above, caught by reading the output of a test rather than
assuming it.

`logq.py` queries it: `--days`, `--lane`, `--failures` (with tails), `--gaps`
(lanes overdue for their cadence). It reads the rotated and compressed
generations too, so rotation never shortens the window you can ask about -
which would have been the same mistake as the `find -delete`.

### closing the chain on the watchers themselves

| failure | reported by |
|---|---|
| a lane errors out | `cronwrap.sh` -> phone, and `rc` in `events.jsonl` |
| a lane never runs | `watchdog.sh` -> phone |
| **the watchdog itself fails** | systemd `OnFailure=radar-alert@%n` -> phone |
| the web app fails | systemd restarts it; a persistent failure -> same alert |
| anything at all, after the fact | `logq.py`, 12 months of `events.jsonl` |

The third row is the one that was missing. The whole scheme rested on a process
whose own death was unreported - and `SuccessExitStatus=0 1` means the
watchdog's "I found a problem" exit is not a unit failure, so only a genuine
crash trips `OnFailure`. `alert.sh` pulls the last eight journal lines for the
failed unit into the alert body, so the push says what happened rather than
only that something did.

The systemd units are covered by the journal rather than by `events.jsonl`:
`/var/log/journal` exists, so the user journal is persistent across reboot and
currently retains 25 days. `journalctl --user -u radar-watchdog` and
`journalctl --user -u job-radar-web` are the equivalents of a lane's log.

**What is still not covered:** a machine that is off, and a disk that is full.
Neither can be reported by anything running on the machine.

---

## selftest.py: after a change, does the whole thing still work?

Three defects shipped on 2026-10-27, and every one would have been caught in
seconds by a check that simply **ran** the thing:

- a non-atomic edit to `cronwrap.sh` was exec'd half-written by a cron lane and
  killed that run - and because the broken file *was* cronwrap, nothing
  reported it;
- `rc` arrived from `argv` as a string, and `"0" != 0` is True in Python, so a
  failure tail was stored on every single run;
- a metrics regex anchored at `^` extracted from `daily` and silently not from
  `fast`, whose output is timestamped.

So `selftest.py` runs four tiers, cheapest first, and never stops early -
when something is broken you want the whole picture, not the first symptom.
Exit status is the number of failures, so `selftest.py && git commit` works.

| tier | what it proves | cost |
|---|---|---|
| 1 STATIC | every script parses, every cron path exists and is executable, the crontab still holds all 10 lanes, the JSON and logrotate configs are valid, **no secret is tracked by git** | instant |
| 2 SERVICE | cron and both systemd units are active *and enabled*, linger is on, the web app answers and its API has the expected keys, the ntfy topic is readable at mode 600 and ntfy.sh is reachable | instant |
| 3 DATA | `integrity_check`, all 8 tables present, board counts sane, and **fast.json is a subset of targets.json** | instant |
| 4 SMOKE | every entry point answers `--help`, and each lane actually runs | ~2 min |

The smoke tier is the part that matters, and it is built so it cannot damage
anything:

- **A one-board sweep through the real pipeline**, with `--no-claim`, and the
  check *asserts the database is byte-identical afterwards*. That is the test
  that proves fetch -> filter -> hydrate -> score -> digest still works end to
  end, and it is only safe to run on every edit because `--no-claim` exists.
- `verify_tracked --dry-run`, again asserting no state changed.
- `board_health --report`, which records nothing by design.
- `make_fast.py` fed a deliberately tiny digest, asserting the shrink guard
  **refuses** and `fast.json` is untouched - a test that the guard rail works,
  not just that the tool runs.
- `watchdog.sh`, accepting exit 0 or 1 (1 means "found a problem", which is a
  verdict, not a crash) and requiring a verdict line in the output.

It earned its place on its first full run by failing on something real:
`make_fast.py --help` crashed, because it parsed `sys.argv[1]` directly and
treated `--help` as a digest filename. Every other entry point answered
`--help`; a tool that dies when asked how to use it is a tool whose guard rail
nobody reads. Now fixed with argparse.

`pre-commit.hook` runs tiers 1-3 on every commit - about a second, cheap enough
not to be resented - and `RADAR_FULL_SELFTEST=1` adds the smoke tier. Install
it as a symlink so edits take effect without reinstalling:

```sh
ln -sf ../../radar/pre-commit.hook .git/hooks/pre-commit
```

### the two root changes

`install_sudo_bits.sh` does what `SUDO_SETUP.md` describes, as one auditable
script rather than pasted heredocs: `Restart=always` plus
`StartLimitIntervalSec=0` on `cron.service` (the default `on-failure` covers a
crash but not a clean exit, a `systemctl stop`, or a cron that exits 0 after
refusing to parse something), and one passwordless command -
`systemctl restart cron.service`, exactly that, no wildcards - so `watchdog.sh`
can act instead of only reporting that it could not.

The sudoers line is written to a temp file and validated with `visudo -c -f`
**before** being installed, because a malformed file in `/etc/sudoers.d` breaks
`sudo` for every user on the machine and checking afterwards is too late. On a
validation failure it changes nothing and exits non-zero.

---

## reliability audit: two real loopholes, and three of my own false positives

Asked to find what could still break a lane, the audit turned up two genuine
holes - and, usefully, three things that looked like holes and were not.

### 1. no lane was protected from overlapping itself

`flock` appeared nowhere as a lane guard. Nothing stopped a second copy of a
lane starting on top of the first, contending for the single SQLite writer and
doing the same work twice.

It had not happened yet, but the margin is shrinking rather than growing: the
fast lane fires every 300s and its worst observed run is **147s** - it was
~110s when the board list was smaller, and it has grown with every board
added. `verify_tracked` runs ~370s on an hourly cadence. The daily lane is
~340s on a 1,800s cadence and is comfortable.

`cronwrap.sh` now takes `flock -n` on a per-label lockfile, so one change
covers all ten lanes. `-n` means a busy lock **skips** the tick rather than
queueing: a backlog of waiting sweeps is worse than a missed one, because the
next tick is minutes away and the work is idempotent. It exits 0, because a
deliberate skip is not a failure and must not page anyone, and records
`{"skipped":true}` in `events.jsonl` so it is visible if it starts happening
often.

### 2. a tracker-lock timeout killed the sweep AFTER it had notified

`FileLock` raises `TimeoutError` when it cannot acquire in 15s, and the call in
`job_radar.main()` was unguarded. Five processes write `applications.md` - both
poller lanes, `inbox.py`, `rescore.py` and the web app - so contention is a
normal condition.

The ordering is what made it serious. `if a.notify:` runs **before**
`if a.track:`, so the failure mode was: push the phone alert, raise, die, never
write the row - and the posting is already claimed in `seen`, so no later sweep
retries it. That is the precise shape of *"lots of jobs in my phone alerts that
I cannot see on the job radar app"*, which was reported as a symptom earlier in
this project and attributed to other causes.

Now guarded, with `timeout=60` because the lock is only ever held for a single
read-modify-write, and `main()` returns 1 so `cronwrap.sh` alerts - a sweep
that notified but could not record is an incident, not a cosmetic warning, and
returning 0 would have made it invisible to every check downstream. `inbox.py`
has the same guard for the same reason: it also tracks after notifying.

### three things that looked broken and were not

Recorded because each cost time, and each was the same mistake in a different
costume - a cheap check mistaken for a conclusion.

- **`serve.py`'s locks are already safe.** The audit flagged all five
  `FileLock()` call sites as unguarded by scanning six lines above each one.
  `serve.py` catches `TimeoutError` at the HTTP handler and returns a clean
  503; the guard was simply further up the call stack than the scan looked.
  Acting on the false positive broke the file, which had to be restored from
  git.
- **`grep`, `printf` and `find` are not off cron's PATH.** A check that
  compared `command -v` output against `/usr/bin:/bin` reported them as
  missing, because they resolve as shell builtins. The only genuinely
  off-PATH binary is `logrotate` at `/usr/sbin`, and it is already invoked by
  absolute path.
- **Disk and WAL are fine.** 126G free at 47% used; the SQLite WAL is 0 bytes,
  so checkpointing is working and the weekly `VACUUM` is keeping up.

### what the audit could not rule out

- **ntfy.sh being down** silently swallows every alert. `curl -m 15` means it
  cannot hang a lane, but a failed push is only visible in the lane's log.
- **A machine that is off, or a disk that fills.** Neither can be reported by
  anything running on the machine.
- `rescore.py`'s lock is still unguarded, deliberately: it is a manual tool run
  by hand, where a traceback is the right feedback. Its timeout was widened to
  60s to match the batch writers.

---

## an ntfy outage used to be silent loss; now it is late delivery

Four places pushed to ntfy independently - `job_radar.notify()`,
`cronwrap.sh`, `watchdog.sh` and `alert.sh` - each with its own `curl` or
`urllib` call, no retries, and no memory. When ntfy.sh was unreachable the
message was simply gone. That is worse here than it first looks, for two
reasons that compound:

- **A job push is lost forever.** By the time `notify()` runs, the posting is
  already claimed in `seen`, so no later sweep re-notifies it. One blip and a
  matching job never reaches the phone at all.
- **The alarm travels over the same wire as the signal.** `cronwrap.sh` reports
  a failed lane by pushing to ntfy, so an outage silences both the lane and the
  warning about the lane. No amount of extra alerting fixes that, because every
  alert uses the channel that is down.

The second point is why a queue, rather than more retries, is the answer: the
information has to survive until the channel returns.

`notify_send.py` is now the only way anything reaches the phone. It retries,
and anything still undeliverable is appended to `.pending_pushes.jsonl` and
re-sent by the next push attempt or by `--drain`. Every send drains the queue
first, so recovery is automatic - nothing has to notice that ntfy came back.

The queue is bounded in both directions, because an unbounded one trades a
silent failure for a noisy one: at most 100 entries, oldest dropped first, and
nothing older than 48h is delivered. A job alert from four days ago is noise,
and a flood of them on recovery is worse than useless. Both limits are reported
when they bite. While draining, one failure stops the rest - ntfy is still
down, and hammering it only delays the lane that triggered the drain.

Measured through a simulated outage: three pushes queued with nothing lost, the
next successful push delivered all three plus itself in order, and a 72-hour-old
entry expired instead of being sent.

### the bug this exposed in watchdog.sh

`watchdog.sh` rate-limits its alerts to one per problem per six hours, and it
wrote that timestamp **whether or not the push succeeded**. So an ntfy outage
during an incident recorded "already alerted" for an alert that never arrived,
and the watchdog then went quiet about a live outage for the rest of the
morning. The stamp is now written only on confirmed delivery; an undeliverable
alert is queued *and* left unstamped, so the next 15-minute tick tries again.

### rescore.py's lock

Guarded like the batch writers, and it needed it most: `rescore` rewrites every
row in the tracker, so it is the longest single hold of that lock. Unhandled,
the `TimeoutError` surfaced as a traceback *after* the plan and the backup path
had already been printed, which reads as though the rescore half happened. It
did not - nothing is written outside the locked block - and saying so in three
plain lines is worth more than the traceback.

Verified by making the lock unobtainable and running the real `main()` against
a **copy** of the tracker: clean message, no traceback, `rc=1`, file byte-identical.
The copy matters. The first attempt used the live `applications.md` as its
control and reported the guard had failed - the file really had changed, but
because the fast and daily lanes had each tracked a new posting during the
180-second window (373 rows -> 375). The giveaway was that `rescore` writes a
`pre-rescore` backup inside the lock before touching anything, and the newest
one on disk was five days old. Never use a file that production writes to as
the control in a test.

---

## a gap check that cannot tell must say so

`logq.py --gaps` reports lanes overdue for their cadence. Run on a Tuesday with
`--days 1` it flagged **seven of ten lanes as missing** - the three weekly ones
and the Monday one, which were not due; and `backup` and `inbox`, which run at
18:40 and 19:52 and had simply not come round yet.

All seven were false. A window shorter than a lane's cadence cannot answer the
question at all: asking "did the Sunday lane run?" on a Tuesday within a
one-day window is not a gap, it is an unanswerable query. A health check that
cries wolf about most of the system is worse than none, because the real
signal is then indistinguishable from the noise.

It now assesses a lane only when the window is at least 1.5x its cadence - so a
daily lane needs a day and a half of silence, not twenty-four hours and a
minute - and prints the lanes it could *not* assess, with the window that would
be long enough. That figure is computed from the cadences present rather than
written down, because a hint naming a window still too short to answer the
question is worse than no hint: the first version suggested `--days 8` for
lanes needing 10.5.

`morning_check.sh` wraps the five things that together mean "clean", because
any one of them alone can look fine while another is broken: did every lane
run, did any fail, is anything undelivered, and is the system itself sound. It
exits 0 only if all five pass.

---

## unit tests, as the safety net a refactor needs

`selftest.py` proves the lanes **run**. `test_pure.py` proves they still
**decide the same way**, which is a different question and the one that matters
when moving code. A sweep that completes happily while `classify_location` has
started reading `Americana, Los Angeles` as American is a worse failure than a
crash, because nothing reports it.

52 tests over the pure functions - `classify_location`, `title_match`,
`level_of`, `score`, `discover.variants`, `title_overlap`, the LinkedIn digest
parsers, ratecheck's two patterns, `fetchcore`'s HTML and date helpers, the
adapters package's own contract, and the shipped tuning data. They run in
0.04s, so they sit in tier 1 of the selftest and therefore run on every commit
through the pre-commit hook.

Two rules shaped the file.

**Every expectation was produced by running the function, never by reasoning
about what it ought to return.** That is not pedantry. Two expectations written
the other way round earlier the same day were wrong, and
`Remote (United States / Canada)` returns `CA`, not the `CA-OPTION` the name
suggests. The first run of the suite also failed on
`test_li_segments_groups_by_job_id` - and the **fixture** was wrong, not the
parser: `LI_VIEW` anchors on the `linkedin.com` host, so a relative href is not
a posting link. Keeping the real URL shape rather than the convenient one is
the point of the test.

**Most cases are regressions for bugs that actually happened**, taken from the
comments that record them: `Americana` as a Brazilian city and a Los Angeles
mall, `Burlington, MA` read as Canadian, `Kubernetes (K8s)` scoring one skill
twice, `Power BI & Power Platform Administrator` slipping past the platform
rule, `enterprise` resolving to a Recruitee demo account, `SRE` matching half
the internet, `429` matched as a substring of a job id, and `workday/TC Energy`
dropped from a report because `\S+` cannot hold a space. Each of those has
already cost somebody a day; they are the highest-value assertions in the file.

### the golden master is what protects the refactor

The hand-written cases cover what someone thought to write down.
`testdata_golden.jsonl` covers what nobody did: **259 real postings**,
stratified across every one of the 24 `(bucket, tier)` combinations present in
a 56,524-posting snapshot, each carrying the verdict the code gives today. A
uniform random sample would have been 95% `OTHER`/tier-0 and would have tested
almost nothing.

It asserts only that behaviour has not **changed**, not that it is right. When
a change is deliberate, regenerating the file turns the diff into a record of
exactly which postings moved - which is the review you actually want before
shipping a scoring or geography change.

Verified by breaking the code on purpose: changing one character in the
Americas rule (`americas` -> `america`) failed three hand-written cases and
named the six real postings that moved, including `United States of America`
flipping from `US-ONLY` to `NA-REMOTE`. A failing unit test fails the selftest,
which blocks the commit.

---

## the adapters are a package now, and the seam was already there

`job_radar.py` was 3,169 lines, and 1,520 of them — 48% — were the 21 ATS
adapters. That is also the only part of the file that structurally grows: every
new platform is another `fetch_*`, another block of constants, another `elif` in
`hydrate`, another arm of the per-query fan-out in `main`. Nothing else in the
file gets longer when the radar covers more employers.

**The cut was measured before it was made, not guessed at.** Walking the AST
for every name the adapter region reads from outside itself returns exactly
eight, and all eight are transport:

```
_req  16 uses    job        21    strip_html  23    days_ago  14
_get_text  7     _TAG        1    WD_CANADA    1    _WD_FACET_CACHE  3
```

No adapter touched `classify_location`, `score`, `title_match`, the database or
the tracker. The layering the code wanted was already latent in it:

```
fetchcore.py      HTTP transport, HTML -> text, job()      imports nothing from the radar
     |
adapters/*.py     21 modules, one per platform             may import fetchcore, never job_radar
     |
job_radar.py      filter, score, state, notify, report     1,421 lines
```

The contract each module implements is three functions, two of them optional:
`fetch_<platform>(target)` for pass 1, `hydrate(job, kind)` for pass 2, and
`expand(target)` for the platforms with no "all jobs" view. Adding a platform
is now one new file plus two lines in `adapters/__init__.py`.

### the one genuine cross-adapter dependency, and the two things that had to change

A cross-reference check over the proposed split found exactly one: Job Bank
called Amazon's `_amz_date`. That is a locale-safe "September 1, 2026" parser
that cron needs because `%B` depends on `LC_TIME`, and two platforms wanting it
is the definition of shared — it moved to `fetchcore.long_date`.

**`hydrate` had to stop reaching into the classifier.** The Workday branch
corrected a `N Locations` placeholder to the real city list and then recomputed
`bucket`, `provinces` and `arrangement` itself. An adapter that imports the
classifier makes the dependency circular and the package pointless. Now the
adapter sets `job["location"]` and nothing else; the caller notices the change
and re-derives. Same output, and the rule is general rather than Workday's.

**`job_radar.py` now bootstraps its own `sys.path`.** `import adapters`
resolves only if the radar directory is importable, and it is not always:
`discover.py` loads `job_radar.py` with `spec_from_file_location()` from a
different working directory, which puts *its* directory on `sys.path` and not
this one. The import would have failed, and `discover.py` is deliberately
written to keep working when the engine does not — so it would have swallowed
the `ImportError` and silently continued without `classify_location`,
`title_match` and the shared Ashby rate limiter. Three lines in `job_radar.py`
prevent that for every loader, present and future.

### verified by equality, not by hope

A pure refactor has a testable claim: the output does not change. Three checks,
in increasing cost:

1. **The task list is byte-identical.** `adapters.expand()` replaced a
   six-armed `if/elif` chain in `main`. Dumping all 1,126 `(platform, target,
   function)` tasks over the real 544-board `targets.json` before and after:
   `diff` is empty.
2. **The pure functions are untouched.** 26 unit tests and the 259-case golden
   master pass unchanged — which is exactly what that file exists for.
3. **The adapters return the same postings.** Every adapter called directly
   against a 20-platform subset, 111 tasks, ~4,800 postings, 0 errors, before
   and after: 3,369 of ~3,400 distinct rows identical, and the remainder is
   live churn on three paginated boards (Air Canada posts continuously; Avature
   and SmartRecruiters page newest-first, so a slice shifts between runs). An
   old-module-against-new-package comparison inside a single process confirmed
   the differing boards agree when both are read at the same moment.

### the reach-ins are preserved deliberately, and now guarded

Eleven modules — plus `tailor/gaps.py`, which lives outside this tree and was
nearly missed — read **33 attributes** off `job_radar`, among them `jr._req`,
`jr.SF_TILE`, `jr.ASHBY_ONE` and `jr._wd_country_facet`. Most of those now live
in `fetchcore` or `adapters`, and survive as explicit re-exports.

Leaving them is the right call for a move commit, but a dropped re-export is a
nasty failure: not an import error, just a board that quietly returns nothing
inside somebody's `try/except`. So the surface is now a tier-1 selftest check
that greps every sibling module for `jr.<attr>`, asserts each one still
resolves, and asserts every platform in `targets.json` has an adapter wired
into the registry. The package `__init__` also refuses to import if the modules
on disk do not match the registry, because an adapter that exists but is not
wired in loses a whole platform in silence.

**The first version of that check was not enough, and a grep found what it
missed.** `discover.py` did not name adapters literally — it built them:

```python
fn = getattr(jr, "fetch_" + platform, None) if jr else None
if not fn:
    return -1
```

Every one of those 21 names had just moved into the package, so the `getattr`
returned `None` for all of them, and `discover.py` is written to treat a
missing adapter as "unverifiable" rather than as an error. Board probing and
the mail pipeline's board resolution would have gone quietly dead — no
traceback, no log line, just `-1` forever. It is exactly the failure the check
exists to prevent, and a check that only looks for `jr.X` cannot see it. Those
three call sites now use `jr.ADAPTERS.get(platform)`, which is the registry
that already exists for this, and the selftest additionally resolves any
`getattr(jr, "<prefix>" + ...)` against every platform and flags any dynamic
lookup it cannot evaluate. Verified by putting the `getattr` line back: the
check fails and names `fetch_amazon`.

Pointing those eleven callers at `fetchcore` and `adapters` directly is the
next step, and a separate one.

---

## three modules, three declared surfaces

The re-exports above were a bridge, and bridges get walked across. Each of the
three modules now states its own surface and nothing else re-exports anything:

| module | names | what it owns |
|---|---|---|
| `job_radar` | 30 | paths, the classifier, the scorer, the tracker, the locks, the push |
| `fetchcore` | 9 | `req`, `get_text`, `strip_html`, `drop_scripts`, `job`, `days_ago`, `long_date`, `TAG`, `UA` |
| `adapters` | 5 | `ADAPTERS`, `HYDRATORS`, `expand`, `platform`, `WD_TRUNCATED` |

**A public API spelled with leading underscores is not one.** `_req` and
`_get_text` were the transport every adapter and four other scripts depend on,
so they are now `req` and `get_text` - 47 call sites, mechanical.

Three reach-ins became functions, which is the part that actually improves the
design rather than relabelling it:

- `fetchcore.drop_scripts(html)` replaces `verify_tracked` reaching for the
  `_SCRIPT` and `_COMMENT` regexes to build the same expression by hand.
- `adapters.workday.probe_canada(api)` replaces `hunt.py` inlining
  `_wd_country_facet` + a `_req` payload + `WD_CANADA` - **twice**, in two
  functions, nine lines each. Both are now four. The facet constant and the
  detector went back to being private.
- `adapters.successfactors.looks_like(html)` replaces `hunt.py` testing
  `SF_ROW` and `SF_TILE` itself to decide whether a careers page is an RMK
  site. The adapter that will poll the board is the right place to answer what
  counts as that board.

`adapters.platform(name)` exists for the one caller that holds a platform
string rather than an import; everything else does
`from adapters import successfactors`, so the import line says which platform
the file depends on.

Two files stopped importing the engine altogether. `hunt.py` needed only those
two functions, and `tailor/gaps.py` needed transport plus one GraphQL
document - neither now pulls in sqlite3, the tracker, argparse or `main()` to
fetch a web page.

### the check is now `__all__`, not `hasattr`

The previous version asserted that every `jr.X` still resolved. That is too
weak: `jr.ADAPTERS` resolves - `job_radar` imports it for its own task loop -
but reading it from another module is exactly the undeclared dependency that
broke when the adapters moved. The check now requires membership in `__all__`,
reports `file:line` and says which of three things is wrong (private, exists
but undeclared, or gone), and covers 85 cross-module reads.

It reads the tree with `ast`, not grep, for two reasons found the hard way: a
regex for `job_radar\.X` also matches the string `"job_radar.py"` in a comment,
which produced eleven phantom failures; and the alias has to be resolved per
file, since `jr` is usually `job_radar` only because of that file's own import
line.

**And resolving aliases from import statements alone was not enough** - found
by auditing the check rather than trusting it. `discover.py` does not import
`job_radar`; it loads the file with `spec_from_file_location()` into a local
`jr`, deliberately, so it can keep working when the engine does not. There is
no `ast.Import` node to read, so the check silently examined none of that
file's reads - and `discover.py` is precisely the file that nearly broke in
silence when the adapters moved. Twice now the same file has slipped past a
check written to protect it, for the same underlying reason: it is the one
consumer that does not import the engine the ordinary way.

The alias map is therefore seeded with the project's conventional names
(`jr`, `fc`, `adapters`, and the full module names) and then extended by
whatever the imports declare. A local variable coincidentally named `jr` would
now raise a false positive, which is the safe direction for this check to
fail. Coverage went from 124 reads to 126, and a private read through that
lazily-loaded `jr` is now reported as
`discover.py:592 reads jr._skill_groups - is private`.

The check's own reach is worth stating: it scans `radar/*.py` plus
`tailor/gaps.py` and `tailor/tailor.py`, which today is all 18 files in
`~/devops` that reference these three modules. A consumer added somewhere else
would not be seen.

Verified in both directions: restoring `jr.ADAPTERS` in `board_health.py` is
caught as *"exists but is not in `__all__`"*, and restoring `jr._req` in
`verify_tracked.py` as *"is private"*.

The new surface got tests too, taking `test_pure.py` from 26 to 46: `strip_html`
had none at all despite feeding every score in the tracker, and the package
contract now asserts that each registry entry really is its module's
`fetch_*`, that the registry matches the modules on disk, and that `expand()`
is the identity for the platforms with an all-jobs view. One of those tests
failed on its first run and the **expectation** was wrong, again - `strip_html`
returns `" Role Terraform "`, with the leading space the `<div>` leaves behind,
and a column-aligned `print` had hidden it.

### one ordering question, measured rather than argued

`strip_html` removed `<script>` bodies before comments; `verify_tracked` built
its own stripper the other way round. Folding both into `drop_scripts` had to
pick one, and it had to be script-first, because `strip_html` feeds the scorer
and the golden master. The orders genuinely differ on HTML where a comment
opens before a `<script>` and closes inside its body. So it was measured on 60
real tracked job pages: `strip_html` byte-identical to the pre-refactor
version, and `drop_scripts` versus the old order identical in **output text**,
not merely in the CLOSED verdict. Zero differences either way.

### a latent bug the fan-out fix exposed

`canada_employers.py` had its own copy of the per-keyword fan-out, and that
copy only handled Workday and SuccessFactors. Replacing it with
`adapters.expand()` took its task list from 1,078 to 1,126: **oracle +27,
jobvite +9, avature +7, taleo +5**. Those four platforms were being polled with
one unfiltered query instead of their keyword list every time that report ran.
Duplicating a fan-out is how that happens, and there is now one copy.

---

## the tuning data is data: `rules/titles.json` and `rules/skills.json`

`TITLE_RULES` and `SKILL_GROUPS` were 220 lines of literal inside
`job_radar.py`, and they are the only two things in the radar that actually get
*tuned*. Every change to them so far has come out of a measurement pass over a
real corpus - which is a data transformation, not a code change. They are now
`rules/titles.json` (22 rules) and `rules/skills.json` (63 groups), and
`job_radar.py` is 1,302 lines.

The win is not tidiness. It is that **a tuning mistake is now a data error
rather than an edit to the engine**: a bad weight is caught by a loader that
names the file, the index and the entry, instead of being a stray bracket
somewhere in a 1,300-line module. Validation is the real gain - a Python
literal cannot check itself.

JSON, not TOML or YAML, for a dull reason: this box runs Python 3.10, there is
no `tomllib`, and the radar has no third-party dependencies and is not
acquiring one for a config format.

### the comments had to survive, because they are the valuable part

Each of these rules carries the measurement that justifies it - why bare
`secops` is excluded, why the `power platform` lookbehind is load-bearing, why
`cloud` and `automation` are absent from the skill list despite being on the
resume, why ECS and GitOps are held at weight 1 below their rarity. A rule
whose reasoning has been lost is a rule nobody can safely change, so JSON with
comments stripped would have been a downgrade dressed as a refactor.

So the comments became fields. The generator walked the AST, attached each
comment block to the element that followed it, promoted `# --- ... ---` lines
to `section`, and put the two long header blocks in `_doc`. Nothing was
retyped, and the round trip was asserted against the live module before the
literals were deleted: **22 rules and 63 groups identical, and the flat
`SKILLS` dict's 181 forms identical**. `test_pure.py` asserts the notes and
sections are still there, so a future edit cannot quietly drop them.

### the loader is strict because the quiet failures are unbounded

Two of them in particular:

- A **truncated `titles.json`** makes every title tier 0. A tier-0 title never
  reaches scoring, so the sweep would read 56,000 postings, match none, print
  *"Nothing new"* and exit **0**. That is the `fast.json` failure again, so it
  gets the same answer: a floor, below which the loader refuses to run.
- A skill group with **no surface forms** compiles to `"|".join(()) == ""`, and
  `re.compile("")` matches every string - so that skill would score on every
  JD ever read.

The second one was a bug in the first version of the loader, found by trying
it: `not all(isinstance(f, str) and f for f in forms)` is `False` for an empty
list, because `all(())` is `True`. Nine guards fired and that one did not.

The loader also enforces the invariant the 2026-10-22 skill restructure
existed for - **one surface form belongs to one skill** - which was previously
only a convention. Two groups claiming `kubernetes` is how a JD reading
"Kubernetes (K8s)" scored the one skill twice at two different weights,
inflating 45 of 177 tracker rows. There are no duplicates today; now there
cannot be one tomorrow.

Errors are `RuntimeError`, deliberately not `SystemExit`: `discover.py` catches
`Exception` so it can keep diagnosing a broken engine, and `SystemExit` is a
`BaseException` that would sail straight through that and take the diagnostics
tool down with the thing it exists to diagnose.

### verified by breaking it, in both directions

Ten sabotage cases - truncated, bad regex, missing field, duplicate form,
weight out of range, formless group, wrong type, missing file, malformed
JSON - and each is refused with a message naming the file, the index and the
entry. Seven of them are now a tier-1 selftest check that mutates copies in a
temp directory and points `RULES_DIR` at them, so the real files are never
touched.

That check is not vacuous either, which was also tested: deleting the floor
guard and the formless-group guard from `job_radar.py` makes it fail and name
exactly the three cases that stop being refused.

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

## Ashby rate-limits by volume, so the fix is a gap and not a worker cap

Growing the Ashby list from **55 to 95 boards** in one day broke boards that had
worked for months. Every sweep from that point answered `HTTP 429` for 6-14 of
them, rotating with whichever ones the thread pool reached after the limit
tripped, and **7 of the 16 casualties were pre-existing** boards — `slant`,
`solace`, `synthetic`, `telus-digital`, `thinkific`, `trulioo`, `wealthsimple`.
Nothing was wrong with any of them: asked serially, all 95 answer 200. So the
digest's **"Dead boards"** footer was reporting live boards, which is the
dangerous part — that footer is the signal `discover.py --verify` acts on.

The instinct is to cap concurrency, and it is wrong. 55 boards through the same
24-worker pool never tripped the limit, so what Ashby counts is requests per
window rather than simultaneous connections, and a semaphore does not reduce
volume. A minimum gap does, and `_jb_wait` was already the precedent.

Picking the value took two measurements, and the first was wrong:

| gap | test | result |
|---|---|---|
| 0.4s | one sweep, 95 boards | 95/95, 2,484 postings, 37.9s |
| 0.4s | a second sweep immediately after | **failed partway** |
| 0.8s | two sweeps back to back | 95/95 both, **zero 429s** |

That the *second* 0.4s run failed is the whole lesson: the bucket is depleted by
sustained rate, not just by the opening burst, so a value that survives one
sweep in isolation proves very little. It refills quickly — every board answered
200 again within two minutes — and Ashby sends no `Retry-After` or rate headers,
so the shape can only be inferred by probing it.

0.8s costs a 76s serialised chain on the 30-minute lane and ~11s on the fast
lane's 14 Ashby boards, both running alongside the other platforms rather than
adding to them. The two back-to-back sweeps are ~190 requests over ~160s of
continuous load, which is harsher than the worst real overlap: the fast lane at
`:35` running into the 30-minute lane at `:37`, about 109 requests in ~87s.

Both passes take the gate, because `hydrate` asks the same host once per
finalist. `board_health.py` inherits it for free by calling `jr.ADAPTERS` — which
matters, because it polls every board at 06:50 with its own 12-worker pool, and
its entire job is to alert when a board returns nothing. Without the gate it
would have rate-limited itself and pushed a false "stopped returning jobs"
alert for a dozen healthy Ashby boards.

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
`N Locations` path: the adapter corrects `location`, `job_radar.hydrate()`
re-derives the geo fields from it through `classify_location`, and so picks
up the new rules for free — the post-hydrate re-filter repeats the
vendor-family condition so a Workday row cannot enter `GLOBAL` through it.
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
