#!/usr/bin/env bash
# Fast lane. The ~45 boards that actually produce matches, polled every few
# minutes so a new req reaches your phone in minutes rather than up to 30.
# Shares seen.sqlite3 with daily.sh, so nothing is notified twice.
set -euo pipefail
cd "$(dirname "$0")"
# stdout went to /dev/null until 2026-10-27, which hid two things: how many
# notifications this lane actually sends, and the fact that it had been dying.
# Four "sqlite3.OperationalError: database is locked" tracebacks sat in
# fast.log with nothing around them to say which runs they belonged to or what
# those runs would have pushed. The full digest really is too big to keep every
# 5 minutes - 156 runs a day - so keep only the summary and the push/track
# lines. A run that matches nothing still prints its summary line, and `|| true`
# keeps a no-match grep from tripping `set -e`.
out=$(python3 job_radar.py \
  --targets fast.json \
  --level entry,mid,senior \
  --min-score 8 \
  --max-age 30 \
  --notify --notify-min-score 15 \
  --track --track-min-score 15)
printf '%s\n' "$out" | grep -E '^_.*scanned|^>> ' \
  | sed "s|^|$(date -u +%Y-%m-%dT%H:%MZ) |" || true
