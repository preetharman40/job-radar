# radar/

Scripts only. **The full manual is one level up: `../README.md`.**

    cd ~/devops/jobs/radar && ./daily.sh

| Script | Purpose |
|---|---|
| `daily.sh` | The one command you run each morning |
| `job_radar.py` | The engine — polls 374 boards, filters, scores, reports |
| `discover.py` | Identify one company's ATS · `--verify` prunes dead boards |
| `harvest.py` | Add boards in bulk from company names |
| `hunt.py` | Add Workday / SuccessFactors tenants |
| `canada_employers.py` | Which companies can legally employ you in Canada |
| `targets.json` | 374 boards + 85 DevOps-vendor tags |
| `seen.sqlite3` | Local state — delete and re-`--seed` to reset |

Every script takes `--help`. No dependencies, Python 3.8+.
