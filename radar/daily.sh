#!/usr/bin/env bash
# Daily job hunt. Run by cron every 30 min, 09:00-17:00 Toronto, weekdays.
#   ./daily.sh          -> new postings since last run
#   ./daily.sh --all    -> everything currently open that matches
#
# Where results land:
#   ../applications.md  durable tracker; anything scoring 25+ is filed here
#                       automatically and your edits are never overwritten
#   digest.md           the last run THAT FOUND SOMETHING (empty runs do not
#                       clobber it - that matters when cron runs 18x a day)
#   radar.log           append-only history of every run
set -euo pipefail
cd "$(dirname "$0")"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

python3 job_radar.py "$@" \
  --level entry,mid \
  --min-score 8 \
  --max-age 30 \
  --md "$TMP" \
  --track --track-min-score 25

# Only replace digest.md when this run actually found something, otherwise a
# quiet 09:30 run erases what the 09:00 run surfaced.
if ! grep -qE '^### nothing|_0 shown' "$TMP"; then
  cp "$TMP" digest.md
  # Optional phone push. Uncomment, pick your own topic, install the ntfy app.
  # curl -s -H "Title: Job Radar" --data-binary @- https://ntfy.sh/CHANGE-ME-9f3k \
  #      < <(head -c 3500 digest.md) > /dev/null || true
fi
