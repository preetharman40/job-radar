#!/usr/bin/env bash
# One command for "is the radar fine?" - run it whenever you want the verdict.
#
#   ./morning_check.sh          # last 24h
#   ./morning_check.sh 3        # last 3 days
#
# It combines the six things that together mean "clean", because any one of
# them alone can look fine while another is broken:
#
#   1. did every lane RUN            logq --gaps      (a lane that never fired
#                                                      leaves no error to find)
#   2. did any lane FAIL             logq --failures  (with the captured tail)
#   3. is anything undelivered       notify_send --status  (an ntfy outage
#                                                      queues rather than loses)
#   4. is the system itself sound    selftest --quick (services, config, db)
#   5. is the sweep still producing  floorcheck      (every lane can run, exit
#                                                     0, and return nothing -
#                                                     see its docstring)
#   6. did a lane change its own      boards_added.log (the inbox lane writes
#      config behind your back                         new boards itself)
#
# Exit 0 only if all four are clean, so it can be used in a conditional.
set -uo pipefail
cd "$(dirname "$0")"
DAYS="${1:-1}"
bad=0

echo "================ job radar: $(date '+%Y-%m-%d %H:%M %Z') ================"
echo
echo "---- 1. lane activity (last ${DAYS}d) ----"
./logq.py --days "$DAYS" || bad=1
echo
echo "---- 2. lanes overdue for their cadence ----"
./logq.py --days "$DAYS" --gaps || bad=1
echo
echo "---- 3. failures, with tails ----"
./logq.py --days "$DAYS" --failures || bad=1
echo
echo "---- 4. undelivered notifications ----"
./notify_send.py --status || bad=1
echo
echo "---- 5. system selftest ----"
./selftest.py --quick || bad=1
echo
echo "---- 6. sweep output levels ----"
./floorcheck.py || bad=1
echo
echo "---- 7. boards the mail lane added itself ----"
# The 19:52 inbox lane resolves employers from the mail to their own ATS board
# and appends them to targets.json with no human in the loop. Before this
# section, `git status` was the only place that showed up - so a live Toronto
# SRE board sat uncommitted for days looking like an unexplained change on
# main. Informational: it never sets `bad`, because a board being added is the
# lane working, not a fault.
if [ -s boards_added.log ]; then
  python3 - "$DAYS" <<'EOJ'
import sys
from datetime import datetime, timedelta, timezone
days = float(sys.argv[1])
cut = datetime.now(timezone.utc) - timedelta(days=days)
rows = []
with open("boards_added.log") as fh:
    for line in fh:
        head, _, rest = line.partition(" ")
        if not rest:
            continue
        try:
            ts = datetime.fromisoformat(head.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cut:
            rows.append(line.rstrip())
print(f"  {len(rows)} added in the last {days:g}d" + (":" if rows else ""))
for r in rows[-10:]:
    print(f"    {r}")
EOJ
else
  echo "  none recorded yet"
fi
if ! git -C .. diff --quiet -- radar/targets.json 2>/dev/null; then
  echo "  !! targets.json has uncommitted board additions - commit when convenient"
fi
echo
if [ "$bad" -eq 0 ]; then
  echo "VERDICT: clean"
else
  echo "VERDICT: something above needs a look"
fi
exit "$bad"
