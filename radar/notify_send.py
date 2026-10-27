#!/usr/bin/env python3
"""
notify_send.py - the one way anything in this tool reaches your phone.

    notify_send.py --title "..." --message "..." [--priority 4] [--tags warning]
    notify_send.py --drain          # try to deliver anything queued, then exit
    notify_send.py --status         # how many are queued and how old

Before this, four places pushed to ntfy independently - job_radar.notify(),
cronwrap.sh, watchdog.sh and alert.sh - each with its own curl or urllib call,
no retries, and no memory. When ntfy.sh was unreachable the message was simply
gone. That matters more here than it looks:

  * a job push is lost FOREVER, because the posting is already claimed in
    `seen` and no later sweep re-notifies it;
  * and the failure alert that should tell you about it goes over the same
    channel, so an outage silences both the signal and the alarm about the
    signal.

So delivery is retried, and anything still undeliverable is QUEUED to
.pending_pushes.jsonl and re-sent by the next push attempt or by --drain. That
turns "ntfy was down" from silent loss into late delivery.

The queue is bounded in both directions: at most MAX_QUEUE entries, oldest
dropped first, and nothing older than MAX_AGE_H is delivered - a job alert from
four days ago is noise, and a flood of them on recovery would be worse than
useless. Both limits are reported when they bite.

Standard library only.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(HERE, ".pending_pushes.jsonl")
NTFY_FILE = os.path.join(HERE, ".ntfy_topic")
MAX_QUEUE = 100
MAX_AGE_H = 48
TRIES = 3
UA = "Mozilla/5.0 (X11; Linux x86_64) job-radar"


def topic():
    t = os.environ.get("RADAR_NTFY_TOPIC")
    if t:
        return t.strip()
    if os.path.exists(NTFY_FILE):
        return open(NTFY_FILE).read().strip()
    return ""


def _post(payload, tp, timeout=12):
    """One attempt. Returns None on success, or a reason string."""
    body = dict(payload, topic=tp)
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=timeout)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def deliver(payload, tp, tries=TRIES):
    """Attempt one message, with backoff. Returns None or the last reason.

    Retries are short and few on purpose: this runs inside a cron lane with a
    5-minute cadence, so spending a minute here to beat a blip is reasonable
    and spending ten is not. Anything that survives the retries goes on the
    queue, which is the real safety net.
    """
    reason = None
    for i in range(tries):
        reason = _post(payload, tp)
        if reason is None:
            return None
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    return reason


def _load():
    if not os.path.exists(PENDING):
        return []
    out = []
    for line in open(PENDING, errors="replace"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass          # a torn line is not worth failing a push over
    return out


def _save(rows):
    # Atomic, because a cron lane may be reading this while another writes it.
    tmp = PENDING + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows[-MAX_QUEUE:]:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, PENDING)


def queue(payload, reason):
    rows = _load()
    rows.append({"queued": time.time(), "reason": reason, "payload": payload})
    dropped = max(0, len(rows) - MAX_QUEUE)
    _save(rows)
    return len(rows[-MAX_QUEUE:]), dropped


def drain(tp, verbose=True):
    """Deliver whatever is queued. Returns (sent, still_queued, expired)."""
    rows = _load()
    if not rows:
        return 0, 0, 0
    now = time.time()
    fresh = [r for r in rows if now - r.get("queued", 0) <= MAX_AGE_H * 3600]
    expired = len(rows) - len(fresh)
    sent, left = 0, []
    for i, r in enumerate(fresh):
        if left:
            # Once one fails, stop trying the rest: ntfy is still down and
            # hammering it achieves nothing. Everything after keeps its place.
            left.append(r)
            continue
        # One attempt each while draining, not TRIES - the queue is already
        # the retry, and a long drain would delay the lane that triggered it.
        if deliver(r["payload"], tp, tries=1) is None:
            sent += 1
        else:
            left.append(r)
    _save(left)
    if verbose and (sent or expired or left):
        print(f"  ntfy queue: {sent} delivered, {len(left)} still waiting"
              + (f", {expired} expired (older than {MAX_AGE_H}h)" if expired else ""))
    return sent, len(left), expired


def send(payload, tp=None, verbose=True):
    """Deliver now, or queue. Drains anything waiting first.

    Draining first means recovery is automatic: the next successful push after
    an outage carries the backlog with it, and nothing has to notice that ntfy
    came back.
    """
    tp = tp or topic()
    if not tp:
        if verbose:
            print(f"  no ntfy topic ({NTFY_FILE} missing and RADAR_NTFY_TOPIC "
                  f"unset) - not queuing, there is nowhere to send it")
        return False
    drain(tp, verbose=verbose)
    reason = deliver(payload, tp)
    if reason is None:
        return True
    n, dropped = queue(payload, reason)
    if verbose:
        print(f"  !! ntfy unreachable ({reason}); QUEUED for later "
              f"({n} waiting"
              + (f", {dropped} dropped - queue is capped at {MAX_QUEUE}" if dropped else "")
              + ")")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="job radar")
    ap.add_argument("--message", default="")
    ap.add_argument("--priority", type=int, default=3)
    ap.add_argument("--tags", default="")
    ap.add_argument("--click", default="")
    ap.add_argument("--drain", action="store_true",
                    help="only flush the queue, send nothing new")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        rows = _load()
        if not rows:
            print("  ntfy queue is empty")
            return 0
        oldest = min(r.get("queued", 0) for r in rows)
        print(f"  {len(rows)} push(es) queued, oldest "
              f"{(time.time() - oldest) / 3600:.1f}h ago")
        for r in rows[:5]:
            print(f"    {r['payload'].get('title', '')[:60]}  ({r.get('reason', '')[:40]})")
        return 0

    tp = topic()
    if a.drain:
        if not tp:
            print("  no ntfy topic configured")
            return 1
        sent, left, _ = drain(tp)
        if not sent and not left:
            print("  ntfy queue is empty")
        return 1 if left else 0

    payload = {"title": a.title, "message": a.message, "priority": a.priority}
    if a.tags:
        payload["tags"] = [t for t in a.tags.split(",") if t]
    if a.click:
        payload["click"] = a.click
    return 0 if send(payload) else 1


if __name__ == "__main__":
    sys.exit(main())
