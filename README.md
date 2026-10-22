# Job Radar

Finds DevOps / SRE / platform / infra-support jobs in Canada by reading company
ATS APIs directly — the same JSON endpoints a careers page calls in your browser
— so a requisition surfaces **minutes after a recruiter clicks Publish**, hours
to weeks before it reaches LinkedIn, Indeed or Google Jobs.

**469 boards · 20 ATS adapters · ~49,200 postings scanned in ~215 seconds.**

Design notes, per-platform quirks, and the things that turned out not to work:
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Why read ATS APIs instead of a job board

| | Aggregator (LinkedIn, Indeed) | This tool |
|---|---|---|
| Latency | hours to days | seconds |
| Description | truncated teaser | the full requisition |
| Post date | "3 days ago", often wrong | the ATS's own timestamp |
| Link | a redirect or mirror | the canonical apply URL |
| Duplicates | same req posted by 4 recruiters | one row per req |
| Blocking | scraping gets your account limited | these are public JSON endpoints |

The trade-off is coverage: this is an **allowlist, not a crawl**. It sees the 469
boards it has been told about and nothing else. Growing that list is covered in
[§8](#8-growing-coverage).

---

## 1. Quick start

Requires **Python 3.8+** and nothing else. No `pip install`, no virtualenv — the
whole project is standard library.

```bash
git clone <this repo> ~/devops/jobs
cd ~/devops/jobs/radar

# 1. First run EVER: record what is currently open without printing it.
#    Skip this and your first real run reports 130 jobs as "new".
python3 job_radar.py --seed

# 2. From now on: only what appeared since the last run.
./daily.sh

# 3. See everything currently open that matches, ignoring memory.
python3 job_radar.py --all --track
```

> ### ⚠ The one mistake to avoid
> **`--all` without `--track` silently consumes postings.** Every job it prints
> is marked as seen, so the next cron tick treats it as old and never notifies
> you. Seven real jobs were lost this way and had to be back-filled by hand.
> If you run `--all`, either pass `--track`, or accept that those jobs will
> never push to your phone.

Then open the web view:

```bash
python3 serve.py          # http://localhost:8737
```

---

## 2. How it works

Two passes. The first is cheap and wide, the second is expensive and narrow.

```
  469 boards × 20 adapters          ~1,000 parallel fetches
            │
            ▼
  PASS 1   listing data only        ~48,000 postings, titles + locations
            │                        (pulling every description would move ~80MB)
            ▼
  dedupe by URL
            │
            ▼
  TITLE filter    ──── 21 regex rules → (weight, family)   no match = dropped
            │                                               ~1.2% survive
            ▼
  LOCATION filter ──── CA / NA-REMOTE / MULTI kept
            │          US-ONLY / OTHER dropped
            ▼
  PASS 2   hydrate ──── fetch the FULL job description, but only for the
            │           few hundred that already passed both filters
            ▼
  SCORE    tier×3 + skill keywords + level adjustment + vendor bonus
            │
            ▼
  thresholds      ──── --min-score 8, --max-age 30 days
            │
            ▼
  claim atomically in seen.sqlite3        (so the two lanes never double-report)
            │
            ├──► stdout + digest.md
            ├──► ntfy push          score ≥ 15
            └──► applications.md    score ≥ 15
```

The funnel is brutal and that is the point — roughly two thirds of what a new
board returns is in the wrong country:

```
  48,000 scanned
     ~67%  not reachable from Canada
     ~17%  explicitly US-only
     ~17%  reachable
      1.2% + a matching title
       130 + score and recency thresholds
```

### Scoring

```
score = tier × 3                     the title rule's weight (2–4)
      + Σ skill weights               42 resume keywords found in the full JD
      + level adjustment              entry +4 · mid +2 · senior −2 · exec −10 · intern −8
      + vendor bonus                  +4 if it is support at a DevOps tool vendor
                                      +2 more if the title also says cloud/k8s/linux/platform
```

Three families, because three different kinds of role are worth applying to:

| Family | What it is | Example rules |
|---|---|---|
| `devops` | the target role | `devops`, `sre`, `platform engineer`, `cloud engineer` (weight 4) |
| `adjacent` | infra work that hires from support | `linux admin`, `build/release engineer`, `endpoint`/`intune`/`MDM` (weight 3–4) |
| `vendor` | support **at a DevOps tool vendor** — the highest-converting family for a support→DevOps pivot | `support engineer`, `solutions engineer`, `escalation engineer` |

`vendor` only fires at one of the 85 companies tagged `devops_vendors` in
`targets.json`. A support role at a bank is not the same job as a support role
at HashiCorp.

**Skills rank, they never filter.** A job with zero keyword hits can still
appear if its title is strong; a job stuffed with keywords still fails if its
title is wrong.

Tune the thresholds for your own search:

```bash
--min-score 8            floor to appear at all
--notify-min-score 15    floor to push to your phone
--track-min-score 15     floor to file in the tracker
--max-age 30             ignore reqs older than N days
--level entry,mid        drop Senior-titled roles
--family vendor          only vendor support roles
--province AB,BC         only these provinces
--bucket CA              Canada-based only, drop US/Canada-ambiguous remote
```

---

## 3. The two lanes

Polling all 469 boards every five minutes would be abusive and slow. Polling
only the productive ones is neither.

| Lane | Boards | Cadence | Runtime |
|---|---|---|---|
| `fast.sh` | **60** — the ones that have actually produced a match | every 5 min | ~52 s |
| `daily.sh` | **all 469** | every 30 min | ~215 s |

They share `seen.sqlite3` and claim postings atomically, so nothing is reported
twice. Regenerate the fast lane after any full sweep:

```bash
python3 job_radar.py --all --track --md /tmp/sweep.md
python3 make_fast.py /tmp/sweep.md
```

### Cron

```cron
PATH=/usr/bin:/bin

# Fast lane — a new req reaches your phone within ~5 minutes.
*/5  7-19 * * *   ~/devops/jobs/radar/fast.sh   >> ~/devops/jobs/radar/fast.log  2>&1

# Full sweep. Offset to :07/:37 ON PURPOSE — see the warning below.
7,37 7-19 * * *   ~/devops/jobs/radar/daily.sh  >> ~/devops/jobs/radar/radar.log 2>&1

25   8-18 * * *   ~/devops/jobs/radar/verify_tracked.py      >> .../verify.log 2>&1
40   18   * * *   ~/devops/jobs/radar/backup.sh              >> .../backup.log 2>&1
50   6    * * 1-5 ~/devops/jobs/radar/board_health.py --notify >> .../health.log 2>&1
40   7    * * 1   ~/devops/jobs/radar/ratecheck.py --days 3 --notify >> .../rate.log 2>&1
17   3    * * 0   find ~/devops/jobs/radar -maxdepth 1 -name '*.log' -size +5M -delete
```

> **Do not start both lanes on the same minute.** `WD_TENANT_BURST` caps
> concurrent requests per Workday tenant, but the semaphore is **per process** —
> two lanes starting together each got the full allowance, doubling the burst.
> That produced **285 rate-limit errors in one day**, clustered exactly at `:01`
> and `:31`. Offsetting to `:07/:37` and lowering the cap to 3 brought it to
> 1 error in 185 sweeps. For the same reason, **never run two copies in
> parallel** or scale to replicas.

> **Cron hours are machine-local and there is no `CRON_TZ`** on Debian cron.
> `7-19` on a machine set to `America/Edmonton` is 09:00–21:00 Toronto. If you
> run this in a container, set `TZ` explicitly or the whole window silently
> shifts.

---

## 4. The web app — `serve.py`

```bash
python3 serve.py                      # 0.0.0.0:8737
python3 serve.py --port 9000
```

Reads and writes `applications.md`. You can change a job's status, delete rows,
and filter:

| Filter | Shows |
|---|---|
| **open** *(default)* | not expired, not senior/exec |
| **to apply** | the queue |
| **25+** | high scorers only |
| **vendor** / **devops** | by family |
| **senior** | the senior/exec roles the default view hides |
| **applied** | everything you have actioned |
| **expired** | postings `verify_tracked.py` has confirmed are gone |
| **everything** | no filter |

> ### ⚠ No authentication
> It binds `0.0.0.0` and anyone who can reach the port can read your tracker —
> which contains hiring-manager names and notes — and delete rows. **LAN only.
> Never port-forward it, never tunnel it, never publish it from a container
> without binding to `127.0.0.1`.** Docker's iptables rules sit in front of
> `ufw`, so `-p 8737:8737` will expose it even if your firewall blocks that port.

### Run it at boot

```bash
cp radar/job-radar-web.service.example ~/.config/systemd/user/job-radar-web.service
systemctl --user daemon-reload
systemctl --user enable --now job-radar-web
loginctl enable-linger "$USER"      # REQUIRED, or it dies when you log out
```

---

## 5. The tracker — `applications.md`

A markdown table plus one detail block per job. Anything scoring ≥ 15 is filed
automatically; **your own edits are never overwritten.** This is the durable
artefact — the database is just memory.

It is **gitignored on purpose**: it accumulates hiring-manager names, contact
details and candid notes on role fit. That is third-party personal data.

> **Never send `applications.md` to a model, an API or a free LLM tier.** The
> companion resume-tailoring tool (`~/devops/tailor/`) only ever sends the
> resume and one job description, never the tracker.

---

## 6. Keeping it honest

Four scripts exist because four distinct failure modes were found the hard way.

```bash
# Boards that quietly stopped returning jobs. The JSON adapters 404 loudly;
# the HTML ones just return an empty list when a site is redesigned, and the
# sweep looks perfectly healthy. Alerts after 3 consecutive zeroes on a board
# that used to produce, and counts fetch errors separately.
python3 board_health.py --notify

# Are the tracked jobs still open? Asks each posting directly rather than
# inferring from search results — Workday's keyword index lags its own job
# endpoints, which was hiding live jobs from the web app. A job is hidden only
# after 3 consecutive misses, and 403/429/5xx are treated as transient.
python3 verify_tracked.py

# Did the Workday burst cap hold over the weekend?
python3 ratecheck.py --days 3 --notify

# Delete tracked jobs that verify_tracked has confirmed are gone.
python3 purge_expired.py          # dry run — lists, deletes nothing
python3 purge_expired.py --yes
```

> **`purge_expired.py` keeps anything whose status is not `toapply`**, however
> long the posting has been gone. An ad-hoc purge once deleted two jobs that had
> already been *applied to* — a dead posting is clutter, but a lost application
> record is history you cannot rebuild. Pass `--include-applied` to override.

### Backups

`backup.sh` snapshots the three things that cannot be regenerated —
`applications.md`, `seen.sqlite3`, `targets.json` — keeps 30 of each, and skips
writing when nothing changed. `applications.md` is gitignored, so **git is not
protecting it.** 30 copies of the database is ~400 MB; trim the `keep` counts in
`backup.sh` if disk is tight.

---

## 7. Notifications

```bash
head -c 32 /dev/urandom | base64 | tr -d '/+=' > radar/.ntfy_topic
chmod 600 radar/.ntfy_topic
```

Install the [ntfy](https://ntfy.sh) app and subscribe to that topic. Priority
scales with score, and the push links straight to the apply URL.

> **The topic string is effectively a password.** Anyone who knows it can read
> every job alert. It is gitignored; keep it that way. `RADAR_NTFY_TOPIC` works
> too if you would rather not have it on disk.

Pushes are published as JSON, not via HTTP headers — `urllib` encodes headers as
latin-1, so a single en-dash in a Canadian job title used to kill the
notification outright with `UnicodeEncodeError`.

---

## 8. Growing coverage

No ATS exposes a "list every board" endpoint — Greenhouse's 404s, Lever's 404s,
Ashby's needs auth. Coverage grows only by taking real company names and testing
each one.

```bash
# What ATS does this company run, and what is its JSON endpoint?
python3 discover.py --url https://careers.example.com/
python3 discover.py --company jobber --add
python3 discover.py --verify                 # re-probe every board, flag the dead

# Bulk: company names → boards. Hit rate 15–35%.
python3 harvest.py --wiki "Category:Software companies of Canada"
python3 harvest.py --file agencies.txt --add
python3 harvest.py --names "Vena Solutions" Nuvei --add

# Workday and SuccessFactors need a tenant/cluster/site triple, not a token.
python3 hunt.py --file employers.txt --add

# Which of these companies can actually employ you in Canada? (Any Canadian
# posting at all — sales, support, anything — proves they have an entity here.)
python3 canada_employers.py --min-ca 1
```

> **Verify every harvest hit before adding it.** The token heuristic matches
> *some* board, not necessarily the one you meant: probing 59 staffing firms
> returned 10 hits of which **one** was correct — `ashby/harvey` was Harvey the
> legal-AI company, not Harvey Nash. Read the board's actual job titles first.

**Expect diminishing returns.** This is measured, not a guess:

| Effort | Result |
|---|---|
| 163 → 366 boards | +4 matches |
| +90 SmartRecruiters tenants | +15 matches |
| +1 Taleo tenant | 0 matches |
| 5 Alberta public-sector employers | +1 match |
| 36 staffing agencies fingerprinted | 2 boards → +3 matches |

Nine Workday tenants out-produce 190 Greenhouse boards, because enterprise
tenants are dense with roles in one country. **Adding boards is the obvious
lever and nearly the worst one.**

---

## 9. Sources that are not an ATS

```bash
python3 inbox.py --setup      # then fill in inbox.json
python3 inbox.py              # parse job-alert emails over IMAP
python3 hn.py                 # Hacker News "Who is hiring?", Canada infra roles
python3 signals.py --untracked  # funding rounds = infra hiring in ~6 weeks
```

`inbox.py` is the only channel that reaches employers the poller cannot —
anything behind a bot manager will still happily *email* you its alerts. It is
also the best way to widen the allowlist: every alert email names employers, and
those names go straight into `harvest.py`.

> Use a **dedicated** email address and an **app password**, never your real
> account password. `inbox.json` is gitignored.

---

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| "I got a push but cannot find the job in the web app" | The default view hides `senior`. Check the **senior** filter. `level_of` reads the title, and e.g. "Sr Associate" is a *mid* rank on the banking ladder, not a senior engineer. |
| A board returns 0 but its careers page has jobs | Check the base URL. Imperial Oil's `/search/` path returned 0 rows at **HTTP 200** while the account root returned 25. Silent success is the most common failure here. |
| Lots of 429s in `radar.log` | The two lanes are colliding. See the cron warning in [§3](#3-the-two-lanes). `ratecheck.py --days 3` will tell you which sweep minute. |
| A tracked job vanished from the web app | `verify_tracked.py` marked it gone after 3 consecutive misses. Workday answers **403 with `errorCode: S22`** for a withdrawn req, not 404. |
| Notification never arrived | No topic — write one to `radar/.ntfy_topic`. The run prints a warning when jobs matched but no push went out. |
| Cron runs at the wrong hours | Hours are machine-local; Debian cron has no `CRON_TZ`. |
| `database is locked` | Two processes on a filesystem that does not implement POSIX locks — typically a Docker Desktop file share. Use a native bind mount or a named volume. |

Logs: `radar.log`, `fast.log`, `verify.log`, `web.log`, `health.log`, `rate.log`.

---

## 11. What this tool cannot do

- **See boards nobody named.** It is an allowlist. 469 boards is roughly 1% of
  the ~31,000 companies on these platforms.
- **Read sites behind a bot manager.** Njoyn sits behind Radware Bot Manager;
  getting past it would mean fingerprint spoofing. Out of scope by choice.
- **Scrape LinkedIn or Google results.** Account restriction and ToS
  respectively. Use `inbox.py` and their own alert emails instead.
- **Judge fit.** A high score means the title and keywords line up, nothing
  more. In practice the **number of unmet requirements** in a job description
  predicts fit better than this score does.

---

## 12. File map

```
~/devops/jobs/
├── README.md                  this file
├── ARCHITECTURE.md            design notes, per-platform traps, measurements
├── manual_check.md            employers the poller cannot reach — check weekly
├── applications.md            your tracker          ← gitignored (contains PII)
├── backups/                   30 daily snapshots    ← gitignored
└── radar/
    ├── job_radar.py           the engine: 20 adapters, filters, scoring, output
    ├── targets.json           469 boards + 85 vendor tags
    ├── fast.json              the 60 productive boards  (make_fast.py writes it)
    ├── seen.sqlite3           memory                ← gitignored
    ├── .ntfy_topic            push topic            ← gitignored (it is a secret)
    ├── inbox.json             IMAP app password     ← gitignored (it is a secret)
    │
    ├── fast.sh daily.sh       the two cron lanes
    ├── serve.py               web app  ─ serve_page.html is its frontend
    ├── backup.sh              daily snapshots
    │
    ├── board_health.py        boards that went quiet
    ├── verify_tracked.py      are the tracked jobs still open
    ├── ratecheck.py           did the Workday burst cap hold
    ├── purge_expired.py       delete dead postings, keep applied ones
    ├── make_fast.py           regenerate fast.json
    │
    ├── discover.py            fingerprint one company's ATS
    ├── harvest.py             bulk: names → boards
    ├── hunt.py                Workday / SuccessFactors tenants
    ├── agencies.txt           curated staffing-firm list for harvest.py
    ├── canada_employers.py    who can legally employ you in Canada
    │
    ├── inbox.py               job-alert emails over IMAP
    ├── hn.py                  Hacker News "Who is hiring?"
    └── signals.py             funding rounds, before the req exists
```

Every script takes `--help`.

---

## 13. Honest note on priorities

The radar surfaces far more than one person can apply to — around 280 matches a
month. Once that is true, **sourcing is no longer the bottleneck; ranking and
actually applying are.** Widening coverage feels productive and mostly is not.
Before adding boards, ask whether the queue you already have is empty.
