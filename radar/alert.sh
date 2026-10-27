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
detail=$(journalctl --user -u "$unit" -n 8 --no-pager -q 2>/dev/null | tail -8 | cut -c1-240)
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALERT: $unit failed"
echo "$detail"
./notify_send.py --title "job radar: $unit FAILED" \
  --message "${detail:-$unit entered a failed state}" \
  --priority 5 --tags rotating_light \
  && echo "  pushed" || echo "  queued or undeliverable"
