#!/usr/bin/env python3
"""
serve.py - read applications.md in a browser, from any device on your LAN.

Watches the file and pushes updates over Server-Sent Events, so when cron files
a new job the page updates itself without a refresh.

    python3 serve.py                 # http://<your-lan-ip>:8djust/
    python3 serve.py --port 9000
    python3 serve.py --host 127.0.0.1   # this machine only

Read-only. Standard library only. Bind stays on your LAN - do not port-forward
this, it contains your application notes and has no authentication.
"""

import argparse
import json
import os
import queue
import ipaddress
import re
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from job_radar import FileLock, atomic_write, DB          # noqa: E402

STATUSES = ["toapply", "applied", "interviewing", "rejected"]

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.abspath(os.path.join(HERE, "..", "applications.md"))

ROW = re.compile(r"^\|\s*(\d+)\s*\|(.*)\|\s*$", re.M)
BLOCK = re.compile(r"^## (\d+)\.\s*(.+?)\n(.*?)(?=\n## |\Z)", re.M | re.S)
# A detail line can carry several fields:
#   - **Level:** entry · **Score:** 42 · **ATS:** greenhouse
# so capture each **Key:** value pair, stopping at the next ** or end of line.
FIELD = re.compile(r"\*\*(.+?):\*\*\s*(.*?)(?=\s*·\s*\*\*|\n|$)")
CHECK = re.compile(r"^- \[([ xX])\]\s*(.+)$", re.M)


LEVELS = ("entry", "mid", "senior", "exec", "intern")
_MD = re.compile(r"[*_`]+")


# Fields whose value is a URL. These must survive verbatim: stripping markdown
# from them deletes any underscore in the path or query, and the 120-char cap
# truncates long ones. Either corruption silently breaks every lookup keyed on
# the URL - delete, status changes, the Apply link and expiry detection all
# match on the exact string. Measured on a live tracker: 53 of 106 URLs
# contained an underscore (Workday req ids almost always do - "..._R-0000180718")
# and 21 exceeded 120 characters.
URL_FIELDS = {"Apply"}


def _clean_field(v, raw=False):
    """Strip markdown, collapse whitespace, and cap runaway values.

    `raw` keeps the value byte-exact apart from surrounding whitespace - use it
    for anything that is matched rather than displayed.
    """
    v = v or ""
    if raw:
        return v.strip()
    v = _MD.sub("", v)
    v = re.sub(r"\s+", " ", v).strip().rstrip("·").strip()
    return v[:120]


def _norm_level(v):
    """Return a known level, or '' - never a sentence."""
    if not v:
        return ""
    low = v.lower()
    if low in LEVELS:
        return low
    # Prose sometimes names the level it is discussing; take the first one.
    for lv in LEVELS:
        if re.search(rf"(?<![a-z]){lv}(?![a-z])", low):
            return lv
    return ""


def live_urls():
    """Which tracked jobs are gone, from verify_tracked.py.

    This used to infer liveness from whether a URL turned up in the last sweep.
    That was wrong: a Manulife req posted 2026-09-22 was live and fetchable at
    its own endpoint while absent from Workday's keyword search for every query,
    with and without the country facet, through 200 results. Search indexes lag;
    job endpoints do not. verify_tracked.py asks each posting directly and only
    calls one gone after three consecutive misses."""
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = list(c.execute("SELECT url, state FROM tracked_live"))
        checked = c.execute("SELECT MAX(checked) FROM tracked_live").fetchone()[0]
        c.close()
        return {u for u, st in rows if st == "gone"}, checked
    except Exception:
        return set(), None


def parse(path):
    try:
        src = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        return {"jobs": [], "error": f"{path} not found"}

    blocks = {}
    for m in BLOCK.finditer(src):
        n, title, body = int(m.group(1)), m.group(2).strip(), m.group(3)
        fields = {k.strip(): _clean_field(v, raw=k.strip() in URL_FIELDS)
                  for k, v in FIELD.findall(body)}
        # A hand-written block can put prose where a value belongs - one entry
        # had "Level: radar tagged `mid` from the title, but the JD opens
        # with..." which rendered as a level in the UI. Normalise the fields
        # that drive display so prose degrades to a sane value instead.
        fields["Level"] = _norm_level(fields.get("Level", ""))
        checks = [{"done": c.lower() == "x", "label": l.strip()}
                  for c, l in CHECK.findall(body)]
        # Prose paragraphs, minus the bullet/field lines
        prose = [p.strip() for p in body.split("\n\n")
                 if p.strip() and not p.strip().startswith(("- ", "|", "#"))]
        blocks[n] = {"title": title, "fields": fields, "checks": checks,
                     "prose": prose}

    gone, swept = live_urls()
    jobs = []
    for m in ROW.finditer(src):
        n = int(m.group(1))
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 8:
            continue
        # The row regex already consumed the leading number, so the 10-column
        # table yields 9 cells here and the older 9-column one yields 8.
        if len(cells) >= 9:
            company, role, loc, posted, updated, score, applied, status, nxt = cells[:9]
        else:
            company, role, loc, posted, score, applied, status, nxt = cells[:8]
            updated = ""
        b = blocks.get(n, {})
        f = b.get("fields", {})
        try:
            sc = int(re.sub(r"\D", "", score) or 0)
        except ValueError:
            sc = 0
        jobs.append({
            "n": n, "company": company, "role": role, "location": loc,
            "posted": posted, "updated": updated, "score": sc,
            "applied": applied, "status": status.strip("`"), "next": nxt,
            "url": f.get("Apply", ""),
            "level": f.get("Level", ""),
            "ats": f.get("ATS", ""),
            # A mail-sourced row carries the mail DOMAIN as its ATS
            # ("linkedin.com"); a board row carries a platform key
            # ("workday"). The dot is the marker. It matters because a
            # mail row is scored from a ~200-char digest snippet and a
            # board row from a 5,000+ char JD, so the two numbers are
            # NOT comparable - the same req scored 14 from mail and 43
            # from its own description. The UI marks these and gives
            # them their own filter instead of inflating the score by a
            # made-up factor; there is no honest multiplier to apply.
            "snippet": "." in f.get("ATS", ""),
            "matched": f.get("Resume keywords matched", ""),
            "checks": b.get("checks", []),
            "prose": b.get("prose", []),
            # Expired only if verify_tracked.py has confirmed it gone. Absence
            # of evidence is not evidence - an unchecked job stays visible.
            "expired": f.get("Apply", "") in gone,
        })
    jobs.sort(key=lambda j: -j["score"])
    return {"jobs": jobs, "count": len(jobs),
            "expired": sum(1 for j in jobs if j["expired"]),
            "swept": swept or "",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S")}


# --------------------------------------------------------------- mutations --

def _row_num_for(src, url):
    """Find which numbered entry owns this apply URL."""
    for m in BLOCK.finditer(src):
        if f"**Apply:** {url}" in m.group(3):
            return int(m.group(1))
    return None


def set_status(path, url, status):
    if status not in STATUSES:
        return False, f"unknown status {status!r}"
    with FileLock():
        src = open(path, encoding="utf-8").read()
        n = _row_num_for(src, url)
        if n is None:
            return False, "job not found"

        def fix(m):
            cells = [c.strip() for c in m.group(0).strip().strip("|").split("|")]
            if str(cells[0]) != str(n):
                return m.group(0)
            # Status is the second-to-last cell in both table layouts.
            cells[-2] = f"`{status}`"
            # Stamp the Applied date the first time it is marked applied.
            if status == "applied" and not cells[-3].strip():
                cells[-3] = time.strftime("%Y-%m-%d")
            return "| " + " | ".join(cells) + " |"

        out = re.sub(r"(?m)^\|\s*\d+\s*\|.*\|\s*$", fix, src)
        if out == src:
            return False, "row not updated"
        atomic_write(path, out)
    return True, status


def delete_job(path, url):
    with FileLock():
        src = open(path, encoding="utf-8").read()
        n = _row_num_for(src, url)
        if n is None:
            return False, "job not found"
        # Drop the table row...
        lines = [l for l in src.split("\n")
                 if not re.match(rf"^\|\s*{n}\s*\|", l)]
        out = "\n".join(lines)
        # ...and its detail block.
        out = re.sub(rf"(?ms)^## {n}\..*?(?=^## |\Z)", "", out)
        if out == src:
            return False, "nothing removed"
        atomic_write(path, out)
    # Remember the dismissal so the next sweep does not re-add it.
    try:
        c = sqlite3.connect(DB)
        c.execute("CREATE TABLE IF NOT EXISTS dismissed(url TEXT PRIMARY KEY, at TEXT)")
        c.execute("INSERT OR REPLACE INTO dismissed VALUES(?,?)",
                  (url, time.strftime("%Y-%m-%dT%H:%M:%S")))
        c.commit(); c.close()
    except Exception:
        pass
    return True, "deleted"


# ---------------------------------------------------------------- watching --

class Watcher(threading.Thread):
    """Poll mtime+size and fan out to every connected browser."""

    def __init__(self, path, interval=2.0):
        super().__init__(daemon=True)
        self.path, self.interval = path, interval
        self.clients, self.lock = [], threading.Lock()
        self.stamp = self._stamp()

    def _stamp(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def run(self):
        while True:
            time.sleep(self.interval)
            s = self._stamp()
            if s != self.stamp:
                self.stamp = s
                with self.lock:
                    targets = list(self.clients)
                for q in targets:
                    q.put("reload")


# ------------------------------------------------------------------ guards --
#
# This server has no login and it writes to applications.md, so the only thing
# standing between a web page you happen to visit and your tracker is these
# two checks. Both were missing until 2026-10-22, and both holes were
# demonstrated against the running service:
#
#   POST, body as text/plain, bogus Origin  -> accepted and executed
#   GET /api/jobs with Host: evil.example   -> 200, 205 KB of job data
#
# WHY A CONTENT-TYPE CHECK IS NOT ENOUGH. A cross-origin fetch() sending
# application/json triggers a CORS preflight, and the preflight fails here
# (there is no do_OPTIONS, so it 501s), so that route is already closed. But an
# ordinary HTML form with enctype="text/plain" is a "simple request": no
# preflight, sent straight through, and its body can be shaped to parse as
# valid JSON. That is the hole, and only an Origin check closes it.
#
# WHY THE HOST CHECK MATTERS SEPARATELY. "LAN only, do not port-forward it" was
# the stated mitigation, and without Host validation it is not true: an
# attacker's domain that re-resolves to this machine's address (DNS rebinding)
# reaches the service from a page you are browsing, with no port forward
# anywhere. Checking Host is what makes "LAN only" real.

MAX_BODY = 64 * 1024          # a real POST here is ~200 bytes

# Names that mean "this machine". avahi is running on this box, so the phone
# reaches it as <hostname>.local and that spelling has to be allowed or the
# app 403s on the device it was built for.
def _default_allowed_names():
    h = socket.gethostname().lower().rstrip(".")
    names = {"localhost", "localhost.localdomain", h, h + ".local"}
    short = h.split(".")[0]
    names.update({short, short + ".local"})
    return names


def _split_host(value):
    """-> hostname, lowercased, port and brackets removed. '' if unusable."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v.startswith("["):                       # [::1]:8737
        return v[1:].split("]")[0]
    # Only strip a trailing :port, never a colon inside a bare IPv6 literal.
    if v.count(":") == 1:
        v = v.split(":")[0]
    return v.rstrip(".")


def _host_allowed(value, extra):
    """True if this Host header names this machine or a private address.

    Private-range literals are allowed because that is how the LAN reaches it.
    Note what this does NOT cover: Tailscale and other CGNAT addresses live in
    100.64.0.0/10, which is not private, so reaching the app over a VPN needs
    --allow-host rather than a code change.
    """
    host = _split_host(value)
    if not host:
        return False                            # HTTP/1.1 requires Host
    if host in extra or host in _default_allowed_names():
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                            # an unknown NAME -> rebinding
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _origin_ok(origin, host_header):
    """True if there is no browser origin, or it matches the host asked for.

    A browser sends Origin on same-origin POSTs too, and it always equals the
    host the page was loaded from - so comparing the two needs no hardcoded
    address and works for localhost, the LAN IP and <hostname>.local alike.
    An absent Origin means a non-browser client (curl, a script), which cannot
    be driven by a hostile page and so is not a CSRF vector. Literal "null" -
    a sandboxed iframe or a file:// page - is refused.
    """
    o = (origin or "").strip()
    if not o:
        return True
    if o.lower() == "null":
        return False
    rest = o.split("://", 1)[-1] if "://" in o else o
    return _split_host(rest) == _split_host(host_header) and bool(host_header)


# ------------------------------------------------------------------ server --

def make_handler(path, watcher, allow_hosts=()):
    allowed = {h.strip().lower().rstrip(".") for h in allow_hosts if h.strip()}
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass                                   # keep the console quiet

        def _send(self, code, ctype, body, extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _guard(self, writing):
            """-> None if the request may proceed, else a sent response."""
            host = self.headers.get("Host")
            if not _host_allowed(host, allowed):
                return self._send(403, "application/json",
                                  json.dumps({"error": "host not allowed",
                                              "host": (host or "")[:80],
                                              "hint": "start serve.py with "
                                                      "--allow-host <name>"}))
            if writing and not _origin_ok(self.headers.get("Origin"), host):
                return self._send(403, "application/json",
                                  '{"error":"cross-site request refused"}')
            return None

        def do_POST(self):
            if self._guard(writing=True) is not None:
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, "application/json",
                                  '{"error":"bad content-length"}')
            if n > MAX_BODY:
                # Read nothing. self.rfile.read(n) on an attacker-chosen length
                # allocates that many bytes before anything is validated.
                return self._send(413, "application/json",
                                  json.dumps({"error": "body too large",
                                              "max": MAX_BODY}))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, "application/json", '{"error":"bad json"}')
            url = payload.get("url", "")
            if not url:
                return self._send(400, "application/json", '{"error":"url required"}')
            try:
                if self.path.startswith("/api/status"):
                    ok, msg = set_status(path, url, payload.get("status", ""))
                elif self.path.startswith("/api/delete"):
                    ok, msg = delete_job(path, url)
                else:
                    return self._send(404, "application/json", '{"error":"no route"}')
            except TimeoutError:
                return self._send(503, "application/json",
                                  '{"error":"tracker busy, try again"}')
            except Exception as e:
                return self._send(500, "application/json",
                                  json.dumps({"error": f"{type(e).__name__}: {e}"}))
            self._send(200 if ok else 409, "application/json",
                       json.dumps({"ok": ok, "msg": msg}))

        def do_GET(self):
            # Reads are guarded too: /api/jobs hands over the whole tracker,
            # which is the half of the rebinding attack that needs no write.
            # Origin is NOT checked here - EventSource and plain navigation
            # must keep working - so Host alone carries the read side.
            if self._guard(writing=False) is not None:
                return
            if self.path.startswith("/api/jobs"):
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(parse(path)),
                           {"Cache-Control": "no-store"})
            elif self.path.startswith("/events"):
                self._sse()
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE,
                           {"Cache-Control": "no-store"})
            else:
                self._send(404, "text/plain; charset=utf-8", "not found")

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = watcher.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=20)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")   # hold the socket
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                watcher.unsubscribe(q)
    return H


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))           # no packets actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


PAGE = open(os.path.join(HERE, "serve_page.html"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(HERE, "serve_page.html")) else "<h1>missing serve_page.html</h1>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 = reachable on your LAN; 127.0.0.1 = this machine only")
    ap.add_argument("--port", type=int, default=8737)
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra Host header to accept, repeatable. Needed for "
                         "a VPN address (Tailscale is 100.64.0.0/10, which is "
                         "not a private range) or a reverse-proxy name.")
    a = ap.parse_args()

    path = os.path.abspath(a.file)
    w = Watcher(path)
    w.start()
    srv = ThreadingHTTPServer((a.host, a.port),
                              make_handler(path, w, a.allow_host))
    srv.daemon_threads = True

    n = len(parse(path).get("jobs", []))
    print(f"  serving {n} jobs from {path}")
    print(f"  this machine : http://localhost:{a.port}/")
    if a.host == "0.0.0.0":
        print(f"  on your LAN  : http://{lan_ip()}:{a.port}/")
        print("  (LAN only — no auth, do not port-forward this)")
    names = sorted(_default_allowed_names() |
                   {h.strip().lower() for h in a.allow_host if h.strip()})
    print(f"  accepts Host : {', '.join(names)}, + private/loopback IPs")
    print("  ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
