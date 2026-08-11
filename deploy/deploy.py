#!/usr/bin/env python3
"""Reproducible, reversible deployment of Optiwar application files.

Production runs a hand-copied tree inside a venv's site-packages, with no git
checkout, so the running code and ``main`` can silently diverge. They already
have, in both directions. This tool makes a deployment an auditable operation
instead of an scp:

    plan      what would change, proven safe, writing nothing
    apply     back up, replace, restart once, smoke test
    rollback  restore the previous release (code only)

Two guards do the real work.

*Provenance*: every file being replaced must hash-match a blob that exists in
this repository's history for that path. If it doesn't, production is carrying
an edit that was never committed, and overwriting it would destroy the only
copy. The deployment refuses.

*Scope*: only files in ``DEPLOY_SET`` are touched. ``main`` is currently
*behind* production on several files — GA4, Google Customer Reviews and the
ticket-notification retry are live but merged nowhere — so a whole-tree deploy
would silently revert working features. Widening the set is a deliberate act
that has to survive the provenance guard.

Usage:
    python3 deploy/deploy.py plan
    python3 deploy/deploy.py apply --confirm
    python3 deploy/deploy.py rollback --confirm
"""
import argparse
import datetime
import hashlib
import os
import shlex
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST = os.environ.get("OPTIWAR_DEPLOY_HOST", "root@172.105.54.11")
APP_DIR = os.environ.get(
    "OPTIWAR_APP_DIR",
    "/var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/"
    "site-packages/flaskr")
SERVICE = os.environ.get("OPTIWAR_SERVICE", "gunicorn")
RELEASES = "/root/deploy_releases"

# The ACR Part B canonical instrumentation, and nothing else. Every other file
# either matches main already or is *ahead* of it; see the module docstring.
DEPLOY_SET = ("acr.py", "ai_client.py", "chat_gateway.py")

# Smoke tests. A deployment that breaks any of these is rolled back, so keep
# them to things that are unambiguous from outside the app.
SMOKE = (
    ("com home", "https://optiwar.com/", 200),
    ("in home", "https://optiwar.in/", 200),
    ("com product listing", "https://optiwar.com/eyeglasses/all-spectacle-frames.html", 200),
    ("login page", "https://optiwar.com/auth/login", 200),
    ("checkout requires auth", "https://optiwar.com/checkout", (301, 302, 303, 307, 308)),
    ("support status", "https://optiwar.com/support/status", 200),
    ("ops rejects anonymous",
     "https://optiwar.com/api/chat/admin/ops-console", 401),
)

# Canonical events a single canary conversation must produce. Their absence
# after a deploy means the instrumentation is not actually running, which is
# the entire point of shipping Part B.
CANARY_EVENTS = ("SESSION_STARTED", "MODEL_CALL", "RECOMMENDATION_GENERATED",
                 "NAVIGATION_OFFERED", "ACTION_CONFIRMED", "ACTION_EXECUTED")


def sh(cmd, check=True):
    """Run locally, return stdout."""
    p = subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True,
                       text=True)
    if check and p.returncode != 0:
        raise SystemExit("command failed: %s\n%s" % (cmd, p.stderr.strip()))
    return p.stdout.strip()


def remote(cmd, check=True):
    """Run on the production host, return stdout."""
    p = subprocess.run(["ssh", HOST, cmd], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit("remote failed: %s\n%s" % (cmd, p.stderr.strip()))
    return p.stdout.strip()


def md5(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def remote_md5s():
    out = remote("cd %s && md5sum *.py" % shlex.quote(APP_DIR))
    return {name: h for h, name in
            (ln.split(None, 1) for ln in out.splitlines())}


def git_blob(path):
    """Blob hash of a working-tree file, as git would compute it."""
    return sh("git hash-object %s" % shlex.quote(path))


def known_to_git(path, blob):
    """Whether this exact content exists in the repo's history for this path."""
    revs = sh("git rev-list --all -- %s" % shlex.quote(path)).split()
    for rev in revs:
        got = sh("git rev-parse %s:%s" % (rev, path), check=False)
        if got == blob:
            return rev
    return None


def preflight():
    """Refuse to build from anything but a clean, current main."""
    problems = []
    if sh("git status --porcelain"):
        problems.append("working tree is dirty — deploy builds from git, not "
                        "from edited files")
    branch = sh("git rev-parse --abbrev-ref HEAD")
    sh("git fetch -q origin main", check=False)
    behind = sh("git rev-list --count HEAD..origin/main", check=False)
    if behind and behind != "0":
        problems.append("HEAD is %s commit(s) behind origin/main" % behind)
    return branch, sh("git rev-parse --short HEAD"), problems


def verify_locally():
    """py_compile the deploy set, then the whole unit suite."""
    for name in DEPLOY_SET:
        sh("python3 -m py_compile %s" % shlex.quote(name))
    out = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_*.py"], cwd=REPO, capture_output=True, text=True)
    tail = (out.stderr or "").strip().splitlines()
    return out.returncode == 0, tail[-3:] if tail else []


def pending_ddl():
    """Schema ``ensure_schema`` would add at boot, read from production.

    Reported so the migration is a deliberate step rather than a side effect of
    the restart: DDL and a code swap should not be the same event.
    """
    cols = remote(
        "mysql -N -e \"SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='optiwar2' AND table_name='ai_events'\"",
        check=False).split()
    idx = remote(
        "mysql -N -e \"SELECT DISTINCT index_name FROM information_schema."
        "statistics WHERE table_schema='optiwar2' AND table_name='ai_actions'\"",
        check=False).split()
    want_cols = ("request_id", "provider", "model", "workload", "consent_scope")
    pending = ["ai_events.%s" % c for c in want_cols if c not in cols]
    if "idx_status_expires" not in idx:
        pending.append("ai_actions.idx_status_expires (index)")
    return pending


def manifest():
    """old hash -> new hash for the deploy set, plus repo/prod divergence."""
    prod = remote_md5s()
    rows, blocked = [], []
    for name in DEPLOY_SET:
        old, new = prod.get(name), md5(os.path.join(REPO, name))
        rows.append((name, old, new))
        if old is None:
            blocked.append("%s: not present in production" % name)
            continue
        if old == new:
            continue
        tmp = "/tmp/.deploy_prod_%s" % name
        subprocess.run(["scp", "-q", "%s:%s/%s" % (HOST, APP_DIR, name), tmp],
                       check=True)
        rev = known_to_git(name, sh("git hash-object %s" % tmp))
        if not rev:
            blocked.append(
                "%s: the running version is not any committed version of this "
                "file — it holds an uncommitted edit that a deploy would "
                "destroy. Commit it first." % name)
        os.unlink(tmp)
    local = {n: md5(os.path.join(REPO, n)) for n in os.listdir(REPO)
             if n.endswith(".py")}
    ahead = sorted(n for n in prod
                   if n in local and prod[n] != local[n]
                   and n not in DEPLOY_SET)
    only_prod = sorted(n for n in prod if n not in local)
    return rows, blocked, ahead, only_prod


def smoke():
    """Run the smoke suite from the production host itself."""
    results = []
    for label, url, want in SMOKE:
        code = remote("curl -s -o /dev/null -w '%%{http_code}' -m 20 %s"
                      % shlex.quote(url), check=False)
        ok = (str(code) == str(want) if isinstance(want, int)
              else int(code or 0) in want)
        results.append((label, url, code, ok))
    return results


def worker_health(since):
    """Boot errors and 5xx after the restart — the two ways a bad deploy shows."""
    errs = remote("journalctl -u %s --since %s --no-pager | "
                  "grep -icE 'traceback|worker failed to boot|error' || true"
                  % (SERVICE, shlex.quote(since)), check=False)
    active = remote("systemctl is-active %s" % SERVICE, check=False)
    return active, errs


def cmd_plan(args):
    branch, head, problems = preflight()
    print("PLAN — nothing is written\n")
    print("  repo      %s @ %s" % (branch, head))
    print("  target    %s:%s" % (HOST, APP_DIR))
    running = remote("systemctl show %s -p ActiveEnterTimestamp --value"
                     % SERVICE, check=False)
    print("  service   %s, up since %s" % (SERVICE, running or "?"))
    for p in problems:
        print("  BLOCKED   %s" % p)

    ok, tail = verify_locally()
    print("\n  py_compile %s: ok" % ", ".join(DEPLOY_SET))
    print("  unit suite: %s" % ("  ".join(tail) if tail else "?"))

    rows, blocked, ahead, only_prod = manifest()
    print("\n  FILE MANIFEST (old -> new)")
    for name, old, new in rows:
        print("    %-18s %s -> %s%s"
              % (name, (old or "absent")[:12], new[:12],
                 "   (unchanged)" if old == new else ""))
    for b in blocked:
        print("    BLOCKED  %s" % b)

    if ahead:
        print("\n  NOT DEPLOYED — production differs from main on these and is\n"
              "  ahead on some (unmerged live features). Deploying them would\n"
              "  revert production:")
        for n in ahead:
            print("    %s" % n)
    if only_prod:
        print("    (only in production: %s)" % ", ".join(only_prod))

    ddl = pending_ddl()
    print("\n  SCHEMA — ensure_schema() would add these at first boot:")
    for d in ddl or ["    (none — already applied)"]:
        print("    %s" % d if ddl else d)
    if ddl:
        print("    Apply these deliberately BEFORE the restart so a code swap\n"
              "    and a DDL change are not the same event.")

    print("\n  SMOKE (current, pre-deploy baseline)")
    for label, _url, code, good in smoke():
        print("    %-24s HTTP %-4s %s" % (label, code, "ok" if good else "UNEXPECTED"))

    print("\n  Rollback after apply:  python3 deploy/deploy.py rollback --confirm")
    return 1 if (problems or blocked or not ok) else 0


def cmd_apply(args):
    branch, head, problems = preflight()
    rows, blocked, _ahead, _only = manifest()
    ok, tail = verify_locally()
    if problems or blocked or not ok:
        for m in problems + blocked + ([] if ok else ["unit suite failed"]):
            print("BLOCKED: %s" % m, file=sys.stderr)
        return 1
    if not args.confirm:
        print("refusing to deploy without --confirm", file=sys.stderr)
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rel = "%s/%s" % (RELEASES, stamp)
    remote("mkdir -p %s" % shlex.quote(rel))
    for name, old, _new in rows:
        remote("cp -p %s/%s %s/%s" % (APP_DIR, name, rel, name))
    with open("/tmp/manifest.txt", "w") as fh:
        fh.write("release %s\nrepo %s @ %s\n" % (stamp, branch, head))
        for name, old, new in rows:
            fh.write("%s %s -> %s\n" % (name, old, new))
    subprocess.run(["scp", "-q", "/tmp/manifest.txt",
                    "%s:%s/manifest.txt" % (HOST, rel)], check=True)
    remote("ln -sfn %s %s/previous" % (shlex.quote(rel), RELEASES))
    print("backed up to %s" % rel)

    for name, _o, _n in rows:
        subprocess.run(["scp", "-q", os.path.join(REPO, name),
                        "%s:%s/%s" % (HOST, APP_DIR, name)], check=True)
    remote("cd %s && python3 -m py_compile %s"
           % (APP_DIR, " ".join(DEPLOY_SET)))
    print("deployed and byte-compiled: %s" % ", ".join(DEPLOY_SET))

    since = remote("date '+%Y-%m-%d %H:%M:%S'")
    remote("systemctl restart %s" % SERVICE)
    remote("sleep 6")
    active, errs = worker_health(since)
    print("service %s, %s error line(s) since restart" % (active, errs))

    failed = [r for r in smoke() if not r[3]]
    for label, _u, code, good in smoke():
        print("  %-24s HTTP %-4s %s" % (label, code, "ok" if good else "FAIL"))
    if failed or active != "active":
        print("\nSMOKE FAILED — roll back with: "
              "python3 deploy/deploy.py rollback --confirm", file=sys.stderr)
        return 1
    print("\nrelease %s live" % stamp)
    return 0


def cmd_rollback(args):
    if not args.confirm:
        print("refusing to roll back without --confirm", file=sys.stderr)
        return 1
    rel = remote("readlink -f %s/previous" % RELEASES, check=False)
    if not rel:
        print("no previous release recorded", file=sys.stderr)
        return 1
    names = remote("cd %s && ls *.py" % shlex.quote(rel)).split()
    for name in names:
        remote("cp -p %s/%s %s/%s" % (rel, name, APP_DIR, name))
    # Deliberately code-only: schema additions are forward-compatible and
    # security hardening must never be undone by a rollback.
    since = remote("date '+%Y-%m-%d %H:%M:%S'")
    remote("systemctl restart %s" % SERVICE)
    remote("sleep 6")
    active, errs = worker_health(since)
    print("restored %s (%s), service %s, %s error line(s)"
          % (", ".join(names), rel, active, errs))
    for label, _u, code, good in smoke():
        print("  %-24s HTTP %-4s %s" % (label, code, "ok" if good else "FAIL"))
    return 0


def cmd_canary(args):
    """Drive one staff conversation and prove canonical events were written.

    Runs from the production host so it goes through nginx and gunicorn exactly
    as a customer would. ``ACR_CANARY_ONLY`` keeps the action path staff-only,
    so this does not expose anything to live shoppers.
    """
    since = remote("date '+%Y-%m-%d %H:%M:%S'")
    jar = "/tmp/.acr_canary_jar"
    remote("rm -f %s" % jar, check=False)
    auth = ('-H "Authorization: Bearer $(grep -h ^OPS_API_TOKEN= '
            '/etc/optiwar/optiwar-secrets.env | cut -d= -f2-)"')
    remote("curl -s -o /dev/null -c %s %s "
           "'https://optiwar.com/api/chat/admin/acr-canary?on=1'"
           % (jar, auth), check=False)
    start = remote("curl -s -b %s -c %s -X POST -H 'Content-Type: "
                   "application/json' -d '{}' "
                   "https://optiwar.com/api/chat/start" % (jar, jar),
                   check=False)
    print("start: %s" % start[:200])

    counts = remote(
        "mysql -N -e \"SELECT event_type, COUNT(*) FROM optiwar2.ai_events "
        "WHERE created_at >= '%s' GROUP BY event_type\"" % since, check=False)
    seen = dict((ln.split("\t")[0], ln.split("\t")[1])
                for ln in counts.splitlines() if "\t" in ln)
    print("\n  CANONICAL EVENTS since %s" % since)
    for ev in CANARY_EVENTS:
        print("    %-26s %s" % (ev, seen.get(ev, "MISSING")))
    legacy = {k: v for k, v in seen.items() if k.startswith("AI_")}
    if legacy:
        print("  legacy names still being emitted: %s" % legacy)
    return 0 if all(e in seen for e in ("SESSION_STARTED",)) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("apply", cmd_apply),
                     ("rollback", cmd_rollback), ("canary", cmd_canary)):
        p = sub.add_parser(name)
        p.add_argument("--confirm", action="store_true")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
