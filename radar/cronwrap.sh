#!/usr/bin/env bash
# Run a radar job and tell you if it dies.
#
# Every lane here is a cron job whose only output goes to a log file nobody
# reads until something is already wrong. On 2026-10-27 a long-running
# inbox.py backfill held the SQLite write lock and killed four fast-lane runs,
# one daily sweep and one verify_tracked run. Nothing said so. The tracebacks
# sat in fast.log and radar.log for hours and were found by grepping the logs
# by hand, which is not monitoring.
#
# So: run the job, and on a non-zero exit push the tail of its output to the
# same ntfy topic the job alerts use. A failure you hear about is an incident;
# a failure you do not is a silent gap in the data.
#
#   cronwrap.sh <label> <command> [args...]
#
# The topic is read the same way job_radar.py reads it and is never echoed.
set -uo pipefail
cd "$(dirname "$0")"

label="$1"; shift
out=$(mktemp); trap 'rm -f "$out"' EXIT
"$@" > "$out" 2>&1
rc=$?
cat "$out"

if [ "$rc" -ne 0 ]; then
  topic="${RADAR_NTFY_TOPIC:-}"
  [ -z "$topic" ] && [ -r .ntfy_topic ] && topic=$(tr -d '[:space:]' < .ntfy_topic)
  # Last 12 lines is enough to show the exception without pushing a digest.
  body=$(tail -12 "$out" | cut -c1-300)
  echo ">> $label FAILED rc=$rc"
  if [ -n "$topic" ]; then
    curl -fsS -m 15 \
      -H "Title: job radar: $label failed (rc=$rc)" \
      -H "Priority: high" -H "Tags: warning" \
      -d "$body" "https://ntfy.sh/$topic" >/dev/null \
      && echo ">> failure pushed to ntfy" \
      || echo ">> could not push failure alert"
  else
    echo ">> no ntfy topic configured, failure not pushed"
  fi
fi
exit "$rc"
