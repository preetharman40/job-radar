#!/usr/bin/env bash
# Regenerate fast.json from a full sweep. Weekly, from cron.
#
# fast.json is the subset of boards the 5-minute lane polls. It was regenerated
# by hand, which meant it drifted: it sat at 69 boards while targets.json grew
# past it to 544, so a productive new board reached the fast lane only when
# someone remembered to run make_fast.py.
#
# WHAT THIS SWEEP IS STILL FOR, since 2026-11-02. make_fast.py no longer
# derives the lane from this run's digest - it ranks all 566 boards on 30 days
# of `seen` and `board_health` and takes a fixed 100. The digest is one
# tiebreak out of five. That change was forced by this lane: on 2026-11-01 it
# rebuilt the lane at 71 boards, down from 100, because the single sweep it
# looked at happened not to show a match for 33 boards that produce regularly.
# So the sweep is now here for its SIDE EFFECTS - it touches every board, which
# refreshes board_health for all 566 including the ones the 30-minute lane's
# filters never reach - and the digest it writes is a bonus.
#
# Three things make it safe to automate, and all three are load-bearing:
#
#   --no-claim   a reporting sweep must not touch notification state. Plain
#                `--all` claims every match it shows, so this run would
#                silently consume whatever was new that morning and the real
#                lanes would never notify it. Adding --notify instead is
#                worse: notify() reads the shown set, which under --all is
#                every match - 181 of 213 on a measured sweep.
#
#   the floor    make_fast.py refuses to write fewer than 60 boards, full
#                stop. The older guard only compared against the PREVIOUS
#                file, which cannot see a ratchet: 100 -> 71 -> 50 -> 35 is
#                four passing Sundays and a lane that polls nothing.
#
#   the gate     the ranking re-judges every historical posting through the
#                live title rules, so a board whose matches were all vetoed
#                since does not keep its slot on them.
set -euo pipefail
cd "$(dirname "$0")"

TMP="$(mktemp)"
RAW="$(mktemp)"
trap 'rm -f "$TMP" "$RAW"' EXIT

echo "=== $(date -u +%Y-%m-%dT%H:%MZ) refreshing fast.json ==="
# Keep the summary and every dead-board/rate-limit line, not `tail -3`. The
# first real run of this lane, on 2026-10-28, hit three 429s on
# workday/BDO Canada and they reached the log only by luck: they happened to be
# the last three lines of the digest, so `tail -3` caught them. Ten errors, or
# one Workday truncation line after them, and the log would have shown none.
#
# Written to a file and grepped rather than piped, so `pipefail` still sees
# job_radar's own exit status - a pipeline ending in `|| true` would mask a
# sweep that died outright.
python3 job_radar.py --all --no-claim \
  --level entry,mid,senior \
  --min-score 8 \
  --max-age 30 \
  --md "$TMP" > "$RAW"
grep -E '^_.*scanned|^>> |^- [a-z]+/' "$RAW" || true

python3 make_fast.py "$TMP"
