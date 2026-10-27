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
| `job_radar.py` | The engine — filtering, scoring, state, tracker, notification, reporting |
| `adapters/` | **One module per ATS platform**, 21 of them. `fetch_*` (pass 1), `hydrate` (pass 2), `expand` (per-query fan-out). Adding a platform is one file plus two lines in `adapters/__init__.py` |
| `fetchcore.py` | HTTP transport (`req`, `get_text`), HTML→text, and `job()` — the record shape the whole pipeline agrees on. Imports nothing from the radar |
| `fast.sh` | Cron lane 1: the productive boards from `fast.json`, every 5 min |
| `daily.sh` | Cron lane 2: all 544 boards, every 30 min |
| `serve.py` | Web app for the tracker (`serve_page.html` is its frontend) |
| `make_fast.py` | Regenerate `fast.json` from a full sweep's digest |
| `backup.sh` | Daily snapshot of the three unregeneratable files |
| `board_health.py` | Boards that quietly stopped returning jobs |
| `verify_tracked.py` | Are the tracked postings still open |
| `ratecheck.py` | Did the Workday burst cap hold |
| `purge_expired.py` | Delete dead postings — keeps anything already applied to |
| `prune_live.py` | Bound the `live` table — 60-day retention, weekly via cron |
| `watchdog.sh` | **Is the radar running at all?** systemd USER timer, every 15 min — outside cron, because cron is what it watches. Alerts to your phone |
| `cronwrap.sh` | Wraps a cron lane and pushes its failure to ntfy. A silent failure is a data gap |
| `refresh_fast.sh` | Weekly `fast.json` rebuild from a full `--no-claim` sweep (Sun 05:10) |
| `SUDO_SETUP.md` | The two root commands that let cron be restarted automatically |
| `morning_check.sh` | **One command for "is it fine?"** — lane activity, gaps, failures with tails, undelivered pushes, and the selftest. Exits 0 only if all five are clean |
| `logq.py` | **Query the run history.** `--failures`, `--gaps`, `--lane X`. Reads `events.jsonl`, which `cronwrap.sh` appends one JSON record to per run |
| `alert.sh` | systemd `OnFailure=` handler, so a crash in the *watchdog* is not silent |
| `notify_send.py` | **The only path to your phone.** Retries, then queues anything ntfy cannot take and re-sends it on recovery. `--status` shows the backlog, `--drain` flushes it |
| `logrotate.conf` | 12 weekly generations, compressed. Replaced a cron job that **deleted** any log over 5MB |
| `test_pure.py` | **26 unit tests** for the pure functions, plus a 259-case golden master over real postings. 0.05s, runs inside `selftest` on every commit |
| `selftest.py` | **Run after every change.** 23 checks in 4 tiers: scripts parse, cron paths exist, services up, DB sound, and each lane actually runs. `--quick` skips the network tier |
| `pre-commit.hook` | Runs `selftest.py --quick` before each commit — **installed** as a symlink at `.git/hooks/pre-commit`, so edits here take effect with no reinstall. Verified to refuse a commit whose Python does not parse. Bypass once with `git commit --no-verify`; add the smoke tier with `RADAR_FULL_SELFTEST=1` |
| `install_sudo_bits.sh` | The two root changes, as one auditable script: `sudo bash radar/install_sudo_bits.sh` |
| `discover.py` | Fingerprint one company's ATS · `--verify` prunes dead boards |
| `harvest.py` | Bulk: company names → boards |
| `hunt.py` | Workday / SuccessFactors tenants |
| `agencies.txt` | Curated staffing-firm list, input for `harvest.py` |
| `canada_employers.py` | Which companies can legally employ you in Canada |
| `inbox.py` | Job-alert emails over IMAP — reaches what the poller cannot. `--boards` reads ATS tokens out of the links; `--employers` resolves the employer **by name** for LinkedIn mail, which carries no token at all, and vets each board against the title that was mailed. Runs daily 19:52 with `--notify --track --add-employer-boards` |
| `hn.py` | Hacker News "Who is hiring?" |
| `signals.py` | Funding rounds, before the requisition exists |

**Module boundaries.** Three modules declare a public surface and nothing
re-exports anything: `job_radar.__all__` (paths, classifier, scorer, tracker,
locks, push), `fetchcore.__all__` (transport and HTML), `adapters.__all__`
(`ADAPTERS`, `HYDRATORS`, `expand`, `platform`, `WD_TRUNCATED`). Platform
specifics are reached through the module — `from adapters import workday` —
so an import line says which platform a file depends on. `selftest.py`
tier 1 reads the tree with `ast` and fails if any script reads a name that is
private or absent from the owning module's `__all__`.

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
