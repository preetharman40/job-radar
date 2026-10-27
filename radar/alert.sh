#!/usr/bin/env bash
# Push a one-line alert. Used by systemd OnFailure= for the units that are not
# cron lanes, so a failure in the watchdog itself is not silent.
#
# The chain this completes:
#   a lane fails              -> cronwrap.sh  -> phone
#   a lane never runs         -> watchdog.sh  -> phone
#   the WATCHDOG fails        -> this, via OnFailure= -> phone
#   the web app fails         -> systemd restarts it; persistent failure -> this
#
# Without the third line the whole scheme rests on a process whose own death is
# unreported, which is the failure mode it exists to prevent.
set -uo pipefail
cd "$(dirname "$0")"
unit="${1:-unknown unit}"
topic="${RADAR_NTFY_TOPIC:-}"
[ -z "$topic" ] && [ -r .ntfy_topic ] && topic=$(tr -d '[:space:]' < .ntfy_topic)
detail=$(journalctl --user -u "$unit" -n 8 --no-pager -q 2>/dev/null | tail -8 | cut -c1-240)
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALERT: $unit failed"
echo "$detail"
[ -z "$topic" ] && { echo "  no ntfy topic configured"; exit 0; }
curl -fsS -m 15 \
  -H "Title: job radar: $unit FAILED" \
  -H "Priority: urgent" -H "Tags: rotating_light" \
  -d "${detail:-$unit entered a failed state}" \
  "https://ntfy.sh/$topic" >/dev/null && echo "  pushed" || echo "  push failed"
