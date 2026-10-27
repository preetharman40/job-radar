# radar/

Scripts only. **The full manual is one level up: [`../README.md`](../README.md).**

```bash
cd ~/devops/jobs/radar
python3 job_radar.py --seed     # first run ever: record state, print nothing
./daily.sh                      # from then on: what is new
python3 serve.py                # web view on :8737
```

| Script | Purpose |
|---|---|
| `job_radar.py` | The engine — 21 ATS adapters, 529 boards, filters, scoring, output |
| `fast.sh` | Cron lane 1: the 60 productive boards, every 5 min |
| `daily.sh` | Cron lane 2: all 529 boards, every 30 min |
| `serve.py` | Web app for the tracker (`serve_page.html` is its frontend) |
| `make_fast.py` | Regenerate `fast.json` from a full sweep's digest |
| `backup.sh` | Daily snapshot of the three unregeneratable files |
| `board_health.py` | Boards that quietly stopped returning jobs |
| `verify_tracked.py` | Are the tracked postings still open |
| `ratecheck.py` | Did the Workday burst cap hold |
| `purge_expired.py` | Delete dead postings — keeps anything already applied to |
| `prune_live.py` | Bound the `live` table — 60-day retention, weekly via cron |
| `discover.py` | Fingerprint one company's ATS · `--verify` prunes dead boards |
| `harvest.py` | Bulk: company names → boards |
| `hunt.py` | Workday / SuccessFactors tenants |
| `agencies.txt` | Curated staffing-firm list, input for `harvest.py` |
| `canada_employers.py` | Which companies can legally employ you in Canada |
| `inbox.py` | Job-alert emails over IMAP — reaches what the poller cannot. `--boards` reads ATS tokens out of the links; `--employers` resolves the employer **by name** for LinkedIn mail, which carries no token at all, and vets each board against the title that was mailed. Runs daily 19:52 with `--notify --track --add-employer-boards` |
| `hn.py` | Hacker News "Who is hiring?" |
| `signals.py` | Funding rounds, before the requisition exists |

| Data | |
|---|---|
| `targets.json` | 529 boards + 85 DevOps-vendor tags |
| `fast.json` | The productive subset — generated, not hand-edited |
| `seen.sqlite3` | Local memory. Delete and re-`--seed` to reset. |
| `.ntfy_topic` | Push topic — **this string is a password** |
| `inbox.json` | IMAP **app password** |

Two traps worth knowing before you run anything:

- **`--all` without `--track` consumes postings** into `seen`, so cron will
  never notify you about them again.
- **Never start both cron lanes on the same minute.** The Workday burst
  semaphore is per process; colliding lanes caused 285 rate-limit errors in a
  day. They are offset to `:07/:37` deliberately.

Every script takes `--help`. No dependencies, Python 3.8+.
