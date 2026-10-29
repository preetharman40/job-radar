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

# Refuse to run a lane on top of itself. Nothing prevented that before: the
# fast lane fires every 300s and takes 147s at its worst today, but it took
# ~110s when the board list was smaller and has grown with it - at 300s two
# copies would overlap, contend for the same SQLite writer and do the same work
# twice. verify_tracked already runs ~370s on an hourly cadence, so it has less
# headroom than it looks.
#
# -n so a busy lock SKIPS this tick instead of queueing: a backlog of waiting
# sweeps is worse than a missed one, because the next tick is only minutes away
# and the work is idempotent. Exits 0, because a deliberate skip is not a
# failure and must not page anyone - it is recorded in events.jsonl as
# skipped=true so it is visible if it starts happening often.
LOCKFILE="$(dirname "$0")/.lane-$label.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  skipped_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "$skipped_at $label: previous run still in progress, skipping this tick"
  printf '{"ts":"%s","label":"%s","rc":0,"secs":0,"skipped":true}\n' \
    "$skipped_at" "$label" >> "$(dirname "$0")/events.jsonl" 2>/dev/null || true
  exit 0
fi

out=$(mktemp) || { echo "$label: mktemp failed, cannot capture output" >&2; exit 1; }
trap 'rm -f "$out"' EXIT
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
t0=$(date +%s)
"$@" > "$out" 2>&1
rc=$?
t1=$(date +%s)
cat "$out"

# One structured record per run, appended to events.jsonl. The human logs stay
# exactly as they are - radar.log's format is parsed by ratecheck.py and
# make_fast.py, and prefixing or reshaping it would break them - so this is a
# second, machine-readable view rather than a change to the first.
#
# It exists because the prose logs cannot answer the questions you actually ask
# after something goes wrong quietly: when did this lane last succeed, how long
# has it been getting slower, which run did the 429s start in, did it run at
# all on the 14th. Answering those from prose meant regex archaeology, and
# doing that by hand produced three wrong conclusions in ratecheck.py alone -
# a substring read as a status code, a UTC date read as a local day, and \S+
# read as a name.
#
# Python does the encoding rather than hand-built JSON: a stray quote or
# newline in a job's output would otherwise produce a line nothing can parse,
# which is precisely the failure mode this is meant to remove. The whole block
# is best-effort and must never change the job's exit status.
python3 - "$label" "$started" "$rc" "$((t1 - t0))" "$out" <<'PYEOF' >> "$(dirname "$0")/events.jsonl" 2>/dev/null || true
import json, re, sys
label, started, rc, secs, path = sys.argv[1:6]
text = open(path, errors="replace").read()
rec = {"ts": started, "label": label, "rc": int(rc), "secs": int(secs)}

# Not anchored with ^: fast.sh pipes its own output through a timestamping
# sed, so its summary line starts "2026-10-27T17:40Z _0 shown · ..." while
# daily.sh's starts at column 0. An anchored pattern silently extracted
# metrics from one lane and not the other.
m = re.search(r"_(\d+) shown · (\d+) matched · (\d+) scanned · (\d+) boards",
              text)
if m:
    rec["shown"], rec["matched"], rec["scanned"], rec["boards"] = map(int, m.groups())
for key, rx in (("shortlist", r"(\d+) shortlisted"),
                ("recurring", r"(\d+) recurring"),
                ("hydrated",  r"(\d+) hydrated"),
                ("pushed",  r">> pushed (\d+) notification"),
                ("tracked", r">> [Tt]racked (\d+) new posting"),
                ("sampled", r"sampled (\d+) boards"),
                ("checked", r"\((\d+) checked\)"),
                ("mail_roles", r"_(\d+) roles from (?:\d+) new messages")):
    m = re.search(rx, text)
    if m:
        rec[key] = int(m.group(1))
# Counted the same way ratecheck counts them, so the two never disagree.
rec["http429"] = len(re.findall(r"HTTP\s*Error\s*429\b", text, re.I))
rec["tracebacks"] = text.count("Traceback (most recent call last)")
# The tail is kept only when something went wrong: on a good run it is just
# the digest again, and this file has to stay cheap to append to forever.
# rec["rc"], not rc: rc is still the string from argv, and "0" != 0 is True in
# Python, so comparing the raw value stored a tail on every single run.
if rec["rc"] != 0 or rec["tracebacks"]:
    rec["tail"] = text[-1200:]
print(json.dumps(rec, separators=(",", ":")))
PYEOF

if [ "$rc" -ne 0 ]; then
  # Last 12 lines is enough to show the exception without pushing a digest.
  body=$(tail -12 "$out" | cut -c1-300)
  echo ">> $label FAILED rc=$rc"
  # Through notify_send.py, not curl: it retries and then QUEUES. This path
  # especially needs it, because the alert about a failure travels over the
  # same channel that may be the thing that failed - without a queue, an ntfy
  # outage silences both the lane and the warning about the lane.
  "$(dirname "$0")/notify_send.py" \
    --title "job radar: $label failed (rc=$rc)" \
    --message "$body" --priority 4 --tags warning \
    && echo ">> failure pushed to ntfy" \
    || echo ">> failure alert queued or undeliverable (see above)"
fi
exit "$rc"
