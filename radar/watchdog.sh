#!/usr/bin/env bash
# Is the radar actually running? Run from a systemd USER timer, never cron.
#
# cronwrap.sh reports a lane that FAILS, and board_health reports a sweep that
# is stale - but both of those are themselves cron jobs. If cron stops, every
# check stops with it, and the failure is silent in the worst possible way: the
# web app keeps serving yesterday's jobs and looks perfectly healthy.
#
# This lives outside cron on purpose. systemd is PID 1, the user manager runs
# with Linger=yes so it survives logout, and its timer fires whether or not
# cron exists. It checks three things cron cannot check about itself:
#
#   1. cron.service is active
#   2. the crontab still contains the expected jobs   (a `crontab <file>` typo
#      silently replaces every line - easy to do, impossible to notice)
#   3. a full sweep was recorded recently
#
# Alerts are rate-limited per problem so a persistent outage does not push
# every 15 minutes for a week.
set -uo pipefail
cd "$(dirname "$0")"

DB=seen.sqlite3
STATE=.watchdog_state
EXPECTED_JOBS=10          # lines in `crontab -l` that invoke this directory
REALERT_HOURS=6
problems=()

note() { problems+=("$1"); echo "  !! $1"; }

# --- 1. cron itself --------------------------------------------------------
if ! systemctl is-active --quiet cron.service 2>/dev/null; then
  note "cron.service is NOT active"
  # Restart needs privileges this user does not have without a sudoers rule.
  # Try anyway, non-interactively, and report either way - a watchdog that
  # silently fails to act is worse than one that says it could not.
  if sudo -n systemctl restart cron.service 2>/dev/null; then
    note "cron.service was restarted by the watchdog"
  else
    note "could not restart cron.service (no passwordless sudo - see README)"
  fi
else
  echo "  cron.service active"
fi

# --- 2. the crontab still has its jobs -------------------------------------
n=$(crontab -l 2>/dev/null | grep -c "devops/jobs/radar" || true)
if [ "$n" -lt "$EXPECTED_JOBS" ]; then
  note "crontab has $n radar jobs, expected $EXPECTED_JOBS - was it overwritten?"
else
  echo "  crontab has $n radar jobs"
fi

# --- 3. a sweep happened recently ------------------------------------------
# Sweeps run 07-19 local, so the gap legitimately grows overnight: at 06:50 the
# newest sweep is from ~19:41 the night before, about 11 hours. Two thresholds
# rather than one, or the overnight gap alerts every night.
age=$(python3 - "$DB" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
try:
    c = sqlite3.connect(sys.argv[1])
    last = c.execute("SELECT MAX(ts) FROM sweeps").fetchone()[0]
    print(f"{(datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()/3600:.2f}")
except Exception:
    print("-1")
PY
)
hour=$(date +%-H)
if [ "$hour" -ge 8 ] && [ "$hour" -le 19 ]; then limit=2; else limit=14; fi
if [ "$(echo "$age < 0" | bc)" = "1" ]; then
  note "cannot read the last sweep time from $DB"
elif [ "$(echo "$age > $limit" | bc)" = "1" ]; then
  note "last full sweep was ${age}h ago (limit ${limit}h at this hour)"
else
  echo "  last full sweep ${age}h ago (limit ${limit}h)"
fi

[ ${#problems[@]} -eq 0 ] && exit 0

# --- alert, rate-limited ---------------------------------------------------
key=$(printf '%s\n' "${problems[@]}" | md5sum | cut -c1-8)
now=$(date +%s)
last=0
[ -r "$STATE" ] && last=$(grep "^$key " "$STATE" 2>/dev/null | awk '{print $2}' || echo 0)
: "${last:=0}"
if [ $((now - last)) -lt $((REALERT_HOURS * 3600)) ]; then
  echo "  (already alerted for this within ${REALERT_HOURS}h, not pushing again)"
  exit 1
fi

body=$(printf '%s\n' "${problems[@]}")
if ./notify_send.py --title "JOB RADAR IS NOT RUNNING" \
     --message "$body" --priority 5 --tags rotating_light; then
  echo "  alert pushed"
  # Only record the rate-limit stamp when the alert ACTUALLY went out.
  # Recording it unconditionally meant a failed push still suppressed the
  # next six hours of retries - so an ntfy outage during an incident hid the
  # incident for the rest of the morning. notify_send queues an undeliverable
  # alert, and leaving the stamp unwritten means this also keeps re-checking.
  grep -v "^$key " "$STATE" 2>/dev/null > "$STATE.tmp" || true
  echo "$key $now" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
else
  echo "  alert queued or undeliverable - NOT recording it as sent, so the"
  echo "  next run will try again rather than going quiet for ${REALERT_HOURS}h"
fi
exit 1
