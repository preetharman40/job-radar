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
#
# `^- [a-z]+/` keeps the digest's dead-board and truncation lines - the shape is
# `- workday/BDO Canada: HTTPError HTTP Error 429: Too Many Requests`. Until
# 2026-10-28 the filter dropped them, which meant the lane that makes six times
# the requests of the daily sweep had no error history at all: ratecheck.py read
# radar.log, fast.log held nothing but summaries, and a tenant throttling this
# lane 2-4 times in each of 156 runs would have been invisible in both. It
# cannot match a posting line - those are `- **Company** ·`, `- matched:`,
# `- https://...` and `- \`CA\` · ...`, none of which is lowercase-word-slash -
# and it costs nothing on a clean day: no board in fast.json produced a single
# error line in the last three sweeps.
out=$(python3 job_radar.py \
  --targets fast.json \
  --level entry,mid,senior \
  --min-score 8 \
  --max-age 30 \
  --notify --notify-min-score 15 \
  --track --track-min-score 15)
printf '%s\n' "$out" | grep -E '^_.*scanned|^>> |^- [a-z]+/' \
  | sed "s|^|$(date -u +%Y-%m-%dT%H:%MZ) |" || true
