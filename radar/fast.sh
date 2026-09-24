#!/usr/bin/env bash
# Fast lane. The ~45 boards that actually produce matches, polled every few
# minutes so a new req reaches your phone in minutes rather than up to 30.
# Shares seen.sqlite3 with daily.sh, so nothing is notified twice.
set -euo pipefail
cd "$(dirname "$0")"
python3 job_radar.py \
  --targets fast.json \
  --level entry,mid,senior \
  --min-score 8 \
  --max-age 30 \
  --notify --notify-min-score 15 \
  --track --track-min-score 15 \
  > /dev/null
