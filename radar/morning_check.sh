#!/usr/bin/env bash
# One command for "is the radar fine?" - run it whenever you want the verdict.
#
#   ./morning_check.sh          # last 24h
#   ./morning_check.sh 3        # last 3 days
#
# It combines the four things that together mean "clean", because any one of
# them alone can look fine while another is broken:
#
#   1. did every lane RUN            logq --gaps      (a lane that never fired
#                                                      leaves no error to find)
#   2. did any lane FAIL             logq --failures  (with the captured tail)
#   3. is anything undelivered       notify_send --status  (an ntfy outage
#                                                      queues rather than loses)
#   4. is the system itself sound    selftest --quick (services, config, db)
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
if [ "$bad" -eq 0 ]; then
  echo "VERDICT: clean"
else
  echo "VERDICT: something above needs a look"
fi
exit "$bad"
