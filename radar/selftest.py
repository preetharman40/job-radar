#!/usr/bin/env python3
"""
selftest.py - after changing anything, does the whole system still work?

Run it after every edit, and before every commit (the git pre-commit hook does
this for you):

    python3 selftest.py            # all checks
    python3 selftest.py --quick    # skip the network smoke tests
    python3 selftest.py --verbose  # show output of failing checks

Why it exists: on 2026-10-27 a single non-atomic edit to cronwrap.sh was exec'd
half-written by a cron lane and killed that run, and because the broken file
WAS cronwrap, nothing reported it. Separately, a stray `rc` type confusion and
an over-anchored regex each shipped and ran before being noticed. Every one of
those would have been caught in seconds by a check that simply ran the thing.

Four tiers, cheapest first, and it stops at nothing - a failure in tier 1 does
not prevent tier 3 from running, because when something is broken you want the
whole picture, not the first symptom.

  1 STATIC   every script parses, every cron path exists, config files are
             valid, no secret is tracked by git
  2 SERVICE  cron and both systemd units are active, the web app answers
  3 DATA     the database passes integrity_check, the board lists are sane
  4 SMOKE    each entry point actually RUNS - including a one-board sweep
             through the real pipeline, with --no-claim so it cannot touch
             notification state

Exit status is the number of failed checks, so `selftest.py && git commit`
works. Standard library only.
"""
import argparse
import ast
import glob
import json
import os
import re
import subprocess
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = []


def check(tier, name):
    """Decorator: register a check. It returns (ok, detail)."""
    def wrap(fn):
        RESULTS.append((tier, name, fn))
        return fn
    return wrap


def sh(*cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=kw.get("timeout", 120), cwd=kw.get("cwd", HERE))
    return p.returncode, (p.stdout + p.stderr)


# ----------------------------------------------------------------- 1 STATIC --

@check(1, "every python file parses")
def _py():
    bad = []
    files = sorted(glob.glob(os.path.join(HERE, "*.py")) +
                   glob.glob(os.path.join(HERE, "adapters", "*.py")))
    for f in files:
        try:
            compile(open(f).read(), f, "exec")
        except SyntaxError as e:
            bad.append(f"{os.path.relpath(f, HERE)}:{e.lineno} {e.msg}")
    return not bad, f"{len(files)} files" if not bad else "; ".join(bad)


@check(1, "every shell script parses")
def _sh():
    bad = []
    files = sorted(glob.glob(os.path.join(HERE, "*.sh")))
    for f in files:
        rc, out = sh("bash", "-n", f)
        if rc:
            bad.append(f"{os.path.basename(f)}: {out.strip()[:80]}")
    return not bad, f"{len(files)} files" if not bad else "; ".join(bad)


@check(1, "every cron command exists and is executable")
def _cron_paths():
    rc, out = sh("crontab", "-l")
    if rc:
        return False, "cannot read crontab"
    bad, seen = [], set()
    for p in re.findall(r"/home/\S+?\.(?:sh|py)", out):
        if p in seen:
            continue
        seen.add(p)
        if not os.path.isfile(p):
            bad.append(f"missing {p}")
        elif not os.access(p, os.X_OK):
            bad.append(f"not executable {p}")
    return not bad, f"{len(seen)} commands" if not bad else "; ".join(bad)


@check(1, "crontab still holds every expected lane")
def _cron_lanes():
    want = {"fast", "daily", "verify", "health", "inbox", "backup",
            "ratecheck", "prune", "refresh", "logrotate"}
    rc, out = sh("crontab", "-l")
    have = set(re.findall(r"cronwrap\.sh (\w+)", out))
    missing = want - have
    return not missing, (f"{len(have)} wrapped" if not missing
                         else f"MISSING: {sorted(missing)}")


@check(1, "json config files are valid")
def _json():
    bad = []
    for f in ("targets.json", "fast.json"):
        try:
            json.load(open(os.path.join(HERE, f)))
        except Exception as e:
            bad.append(f"{f}: {e}")
    return not bad, "targets.json fast.json" if not bad else "; ".join(bad)


@check(1, "logrotate config is valid")
def _logrotate():
    state = "/tmp/.selftest-lr.state"
    rc, out = sh("/usr/sbin/logrotate", "--debug", "--state", state,
                 os.path.join(HERE, "logrotate.conf"))
    errs = [l for l in out.splitlines() if l.startswith("error:")]
    for p in (state,):
        if os.path.exists(p):
            os.remove(p)
    return not errs, "no errors" if not errs else "; ".join(errs)


@check(1, "unit tests for the pure functions")
def _units():
    """Tier 1 because they are instant (~0.05s) and because they catch the
    class of bug selftest's own smoke tests cannot: a sweep that completes
    happily while classify_location has quietly changed its mind."""
    rc, out = sh(sys.executable, "-m", "unittest", "test_pure", timeout=120)
    if rc == 0:
        n = re.search(r"Ran (\d+) tests", out)
        return True, f"{n.group(1) if n else '?'} tests pass"
    fails = [l for l in out.splitlines() if l.startswith(("FAIL:", "ERROR:"))]
    return False, "; ".join(fails[:3]) or out.strip()[-120:]


@check(1, "module boundaries: every cross-module read is a declared name")
def _interface():
    """Three modules declare a public surface - job_radar, fetchcore and
    adapters - and every other script reads only from those lists.

    Before this existed, nine scripts reached for privates: jr._req,
    jr.SF_TILE, jr._ashby_wait, jr._SCRIPT. That is how the adapter extraction
    nearly broke discover.py silently, so the rule is checked rather than
    documented. hasattr() is not enough: a name that happens to exist but is
    not in __all__ is a reach-in that works today and moves tomorrow.

    Read with ast, not grep - a regex for `job_radar.X` also matches the
    string "job_radar.py" in a comment, and the import aliases have to be
    resolved per file anyway."""
    sys.path.insert(0, HERE)
    try:
        import adapters
        import fetchcore
        import job_radar
    except Exception as e:
        return False, f"cannot import: {type(e).__name__}: {e}"

    problems = []
    # The layering only holds if it is one-way. An adapter that imports
    # job_radar makes the package circular and pointless.
    for f in sorted(glob.glob(os.path.join(HERE, "adapters", "*.py"))):
        if re.search(r"^\s*(?:import|from)\s+job_radar\b", open(f).read(), re.M):
            problems.append(f"adapters/{os.path.basename(f)} imports job_radar")

    targets = json.load(open(job_radar.TARGETS))
    # keys starting with _ are the per-platform notes kept in targets.json
    platforms = {k for k in targets
                 if k != "devops_vendors" and not k.startswith("_")}
    if platforms - set(adapters.ADAPTERS):
        problems.append(f"targets.json has no adapter for "
                        f"{sorted(platforms - set(adapters.ADAPTERS))}")

    WATCHED = {"job_radar": job_radar, "fetchcore": fetchcore,
               "adapters": adapters}
    files = sorted(glob.glob(os.path.join(HERE, "*.py")) +
                   ["/home/harman/devops/tailor/gaps.py",
                    "/home/harman/devops/tailor/tailor.py"])
    # job_radar and fetchcore define their own surfaces, and this checker has
    # to read __all__ and _MODULES to do the checking, so all three would flag
    # themselves.
    reads = 0
    for f in files:
        if os.path.basename(f) in ("job_radar.py", "fetchcore.py",
                                   "selftest.py") or not os.path.exists(f):
            continue
        name = os.path.basename(f)
        tree = ast.parse(open(f).read())

        alias = {}                       # local name -> watched module
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name in WATCHED:
                        alias[a.asname or a.name] = WATCHED[a.name]
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)):
                continue
            mod = alias.get(n.value.id)
            if mod is None:
                continue
            reads += 1
            if n.attr in getattr(mod, "__all__", ()):
                continue
            # a submodule reached through the package is fine: the import
            # names the platform, which is the point
            if mod is adapters and n.attr in adapters._MODULES:
                continue
            why = ("is private" if n.attr.startswith("_") else
                   "exists but is not in __all__" if hasattr(mod, n.attr)
                   else "does not exist")
            problems.append(f"{name}:{n.lineno} reads "
                            f"{n.value.id}.{n.attr} - {why}")

            # Dynamic lookups are the dangerous ones: discover.py used
            # getattr(jr, "fetch_" + platform, None), which returns None
            # without raising, so the adapter move turned every board probe
            # into a no-op and nothing said so.
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "getattr" and n.args
                    and isinstance(n.args[0], ast.Name)):
                continue
            mod = alias.get(n.args[0].id)
            if mod is None:
                continue
            arg = n.args[1] if len(n.args) > 1 else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                continue                 # a literal name: checked above in spirit
            prefix = (arg.left.value if isinstance(arg, ast.BinOp)
                      and isinstance(arg.left, ast.Constant) else None)
            if isinstance(prefix, str):
                for plat in sorted(adapters.ADAPTERS):
                    if not hasattr(mod, prefix + plat):
                        problems.append(f"{name}:{n.lineno} builds "
                                        f"{n.args[0].id}.{prefix}<platform>, "
                                        f"but {prefix}{plat} does not resolve")
                        break
            else:
                problems.append(f"{name}:{n.lineno} reads "
                                f"{n.args[0].id}.<computed> - "
                                f"cannot be verified here")

    return not problems, (f"{len(adapters.ADAPTERS)} adapters, "
                          f"{len(job_radar.__all__)}+{len(fetchcore.__all__)}"
                          f"+{len(adapters.__all__)} declared names, "
                          f"{reads} cross-module reads ok"
                          if not problems else "; ".join(problems))


@check(1, "no secret is tracked by git")
def _secrets():
    must_ignore = ["radar/inbox.json", "radar/.ntfy_topic", "applications.md",
                   "radar/events.jsonl", "radar/seen.sqlite3"]
    leaked = []
    for p in must_ignore:
        rc, _ = sh("git", "check-ignore", "-q", p, cwd=ROOT)
        if rc != 0:
            leaked.append(f"{p} NOT ignored")
    rc, out = sh("git", "ls-files", cwd=ROOT)
    for p in must_ignore:
        if p in out.split("\n"):
            leaked.append(f"{p} IS TRACKED")
    return not leaked, f"{len(must_ignore)} checked" if not leaked else "; ".join(leaked)


# ---------------------------------------------------------------- 2 SERVICE --

@check(2, "cron.service is active")
def _cron_up():
    rc, out = sh("systemctl", "is-active", "cron.service")
    return out.strip() == "active", out.strip()


@check(2, "systemd user units active and enabled")
def _units():
    bad = []
    for u in ("radar-watchdog.timer", "job-radar-web.service"):
        _, a = sh("systemctl", "--user", "is-active", u)
        _, e = sh("systemctl", "--user", "is-enabled", u)
        if a.strip() != "active" or e.strip() != "enabled":
            bad.append(f"{u} active={a.strip()} enabled={e.strip()}")
    return not bad, "watchdog.timer + web" if not bad else "; ".join(bad)


@check(2, "linger is on (units survive logout/reboot)")
def _linger():
    _, out = sh("loginctl", "show-user", os.environ.get("USER", "harman"),
                "-p", "Linger", "--value")
    return out.strip() == "yes", out.strip()


@check(2, "web app answers and its API parses")
def _web():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8737/api/jobs", timeout=15) as f:
            d = json.load(f)
        need = {"jobs", "count", "expired", "stale"}
        missing = need - set(d)
        return not missing, (f"{d['count']} jobs, expired {d['expired']}, "
                             f"stale {d['stale']}" if not missing
                             else f"API missing keys {missing}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@check(2, "alerting prerequisites")
def _alerting():
    topic = os.path.join(HERE, ".ntfy_topic")
    if not os.access(topic, os.R_OK):
        return False, ".ntfy_topic not readable"
    mode = oct(os.stat(topic).st_mode & 0o777)
    try:
        req = urllib.request.Request("https://ntfy.sh/", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as f:
            code = f.status
    except Exception as e:
        return False, f"ntfy.sh unreachable: {type(e).__name__}"
    return mode == "0o600", f"topic mode {mode}, ntfy.sh HTTP {code}"


# ------------------------------------------------------------------- 3 DATA --

@check(3, "database passes integrity_check")
def _db():
    try:
        c = sqlite3.connect(f"file:{os.path.join(HERE, 'seen.sqlite3')}?mode=ro",
                            uri=True)
        v = c.execute("PRAGMA integrity_check").fetchone()[0]
        return v == "ok", v
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@check(3, "expected tables all present")
def _tables():
    want = {"seen", "live", "sweeps", "dismissed", "tracked_live",
            "board_health", "mail_seen", "employer_board"}
    c = sqlite3.connect(f"file:{os.path.join(HERE, 'seen.sqlite3')}?mode=ro",
                        uri=True)
    have = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = want - have
    return not missing, f"{len(want)} tables" if not missing else f"MISSING {missing}"


@check(3, "board lists are sane and fast is a subset of targets")
def _boards():
    t = json.load(open(os.path.join(HERE, "targets.json")))
    f = json.load(open(os.path.join(HERE, "fast.json")))
    tot = lambda d: sum(len(v) for k, v in d.items()
                        if isinstance(v, list) and k != "devops_vendors")
    nt, nf = tot(t), tot(f)
    problems = []
    if nt < 400:
        problems.append(f"targets.json only {nt} boards")
    if nf < 30:
        problems.append(f"fast.json only {nf} boards - the fast lane is nearly empty")
    if nf > nt:
        problems.append("fast.json is larger than targets.json")
    for k, v in f.items():
        if not isinstance(v, list) or k == "devops_vendors":
            continue
        extra = [x for x in v if isinstance(x, str) and x not in t.get(k, [])]
        if extra:
            problems.append(f"fast.json {k} has boards absent from targets: {extra[:3]}")
    return not problems, f"targets {nt}, fast {nf}" if not problems else "; ".join(problems)


# ------------------------------------------------------------------ 4 SMOKE --

@check(4, "every entry point responds to --help")
def _help():
    bad = []
    for f in ("job_radar.py", "inbox.py", "discover.py", "verify_tracked.py",
              "board_health.py", "make_fast.py", "prune_live.py", "logq.py",
              "ratecheck.py", "rescore.py", "purge_expired.py"):
        rc, out = sh(sys.executable, os.path.join(HERE, f), "--help", timeout=30)
        if rc != 0:
            bad.append(f"{f} rc={rc} {out.strip()[:60]}")
    return not bad, "11 entry points" if not bad else "; ".join(bad)


@check(4, "one-board sweep runs the real pipeline without touching state")
def _sweep():
    before = _counts()
    tiny = "/tmp/.selftest-targets.json"
    json.dump({"greenhouse": ["trolley"], "devops_vendors": []}, open(tiny, "w"))
    rc, out = sh(sys.executable, os.path.join(HERE, "job_radar.py"),
                 "--targets", tiny, "--all", "--no-claim",
                 "--level", "entry,mid,senior", "--min-score", "0",
                 timeout=180)
    os.remove(tiny)
    after = _counts()
    if rc != 0:
        return False, f"rc={rc} {out.strip()[-160:]}"
    if "scanned" not in out:
        return False, f"no summary line in output: {out.strip()[-160:]}"
    if before != after:
        return False, f"STATE CHANGED despite --no-claim: {before} -> {after}"
    m = re.search(r"(\d+) scanned", out)
    return True, f"{m.group(1) if m else '?'} postings scanned, db untouched"


def _counts():
    c = sqlite3.connect(f"file:{os.path.join(HERE, 'seen.sqlite3')}?mode=ro",
                        uri=True)
    return tuple(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("seen", "live", "sweeps"))


@check(4, "verify_tracked --dry-run writes nothing")
def _verify_dry():
    before = _counts()
    rc, out = sh(sys.executable, os.path.join(HERE, "verify_tracked.py"),
                 "--dry-run", timeout=600)
    if rc != 0:
        return False, f"rc={rc} {out.strip()[-160:]}"
    if _counts() != before:
        return False, "state changed during --dry-run"
    m = re.search(r"(\d+) open", out)
    return True, f"{m.group(1) if m else '?'} open, nothing written"


@check(4, "board_health --report reads cleanly")
def _health_report():
    rc, out = sh(sys.executable, os.path.join(HERE, "board_health.py"),
                 "--report", timeout=120)
    m = re.search(r"(\d+) boards tracked", out)
    return rc == 0 and bool(m), (f"{m.group(1)} boards tracked" if m
                                 else f"rc={rc} {out.strip()[-120:]}")


@check(4, "make_fast refuses to shrink the fast lane")
def _guard():
    bad = "/tmp/.selftest-digest.md"
    open(bad, "w").write("- **onlyone** · nothing\n")
    before = open(os.path.join(HERE, "fast.json")).read()
    rc, out = sh(sys.executable, os.path.join(HERE, "make_fast.py"), bad, timeout=60)
    os.remove(bad)
    unchanged = open(os.path.join(HERE, "fast.json")).read() == before
    return rc != 0 and unchanged, ("guard held, fast.json untouched"
                                   if rc != 0 and unchanged
                                   else f"GUARD FAILED rc={rc}, unchanged={unchanged}")


@check(4, "watchdog runs and reports a verdict")
def _watchdog():
    env = dict(os.environ, RADAR_NTFY_TOPIC="")
    p = subprocess.run([os.path.join(HERE, "watchdog.sh")], capture_output=True,
                       text=True, timeout=120, cwd=HERE, env=env)
    out = p.stdout + p.stderr
    if p.returncode not in (0, 1):
        return False, f"rc={p.returncode} (expected 0 healthy or 1 problem found)"
    if "cron.service" not in out:
        return False, f"no verdict in output: {out.strip()[:120]}"
    return p.returncode == 0, ("healthy" if p.returncode == 0
                               else "ran, but reported: "
                                    + "; ".join(l.strip() for l in out.splitlines()
                                                if l.strip().startswith("!!")))


@check(4, "logq reads the event log")
def _logq():
    rc, out = sh(sys.executable, os.path.join(HERE, "logq.py"), "--days", "2",
                 timeout=60)
    if rc != 0 and "does not exist yet" in out:
        return True, "no events yet (fresh install)"
    m = re.search(r"(\d+) runs in the last", out)
    return rc == 0 and bool(m), (f"{m.group(1)} runs readable" if m
                                 else f"rc={rc} {out.strip()[-120:]}")


@check(4, "ratecheck parses radar.log")
def _ratecheck():
    rc, out = sh(sys.executable, os.path.join(HERE, "ratecheck.py"),
                 "--days", "3", timeout=120)
    return rc == 0 and ("rate-limit errors" in out or "no sweeps" in out), \
        (out.strip().splitlines()[-1][:90] if out.strip() else f"rc={rc}")


# -------------------------------------------------------------------- main --

TIER = {1: "STATIC", 2: "SERVICE", 3: "DATA", 4: "SMOKE"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="skip tier 4 (the smoke tests that use the network)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    failed = 0
    for tier in sorted(TIER):
        if a.quick and tier == 4:
            print(f"\n  -- tier {tier} {TIER[tier]}: skipped (--quick)")
            continue
        print(f"\n  -- tier {tier} {TIER[tier]} " + "-" * 46)
        for t, name, fn in RESULTS:
            if t != tier:
                continue
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            print(f"  [{'PASS' if ok else 'FAIL'}] {name:52} {detail}")
            if not ok:
                failed += 1
    total = len([r for r in RESULTS if not (a.quick and r[0] == 4)])
    print(f"\n  {total - failed}/{total} checks passed"
          + (f"  -- {failed} FAILED" if failed else "  -- all good"))
    return failed


if __name__ == "__main__":
    sys.exit(main())
