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
import re
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


def remote_try(cmd):
    """Run remotely, returning (ok, stdout) instead of swallowing the status.

    ``check=False`` hides the difference between "the query said nothing" and
    "the query failed", which for a verification step is the difference between
    a bad release and no evidence at all.
    """
    p = subprocess.run(["ssh", HOST, cmd], capture_output=True, text=True)
    return p.returncode == 0, p.stdout.strip()


def remote_script(script, check=True):
    """Run a script on the production host, fed over stdin.

    Anything that touches a credential goes through here rather than
    ``remote()``: a command passed as an ssh argument is visible in the remote
    process list and is echoed back in the error message on failure.
    """
    p = subprocess.run(["ssh", HOST, "bash -s"], input=script,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit("remote script failed:\n%s" % p.stderr.strip())
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
    if branch != "main":
        # Being level with origin/main is not the same as being main: a feature
        # branch contains it and carries unreviewed commits on top.
        problems.append("HEAD is on %s — deploy builds from main" % branch)
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
    def indexes(table):
        return remote(
            "mysql -N -e \"SELECT DISTINCT index_name FROM information_schema."
            "statistics WHERE table_schema='optiwar2' AND table_name='%s'\""
            % table, check=False).split()

    want_cols = ("request_id", "provider", "model", "workload", "consent_scope")
    pending = ["ai_events.%s (column)" % c for c in want_cols if c not in cols]
    # Every index ensure_schema() creates, not just the one on ai_actions —
    # an unlisted index is still DDL running during the restart, which is the
    # thing this is meant to rule out.
    for table, names in (("ai_events", ("idx_provider_model", "idx_request")),
                         ("ai_actions", ("idx_status_expires",))):
        have = indexes(table)
        pending += ["%s.%s (index)" % (table, n) for n in names
                    if n not in have]
    return pending


def manifest():
    """old hash -> new hash for the deploy set, plus repo/prod divergence."""
    prod = remote_md5s()
    rows, blocked = [], []
    for name in DEPLOY_SET:
        old, new = prod.get(name), md5(os.path.join(REPO, name))
        if old is None:
            rows.append((name, old, new, None))
            blocked.append("%s: not present in production" % name)
            continue
        if old == new:
            rows.append((name, old, new, "HEAD"))
            continue
        tmp = "/tmp/.deploy_prod_%s" % name
        subprocess.run(["scp", "-q", "%s:%s/%s" % (HOST, APP_DIR, name), tmp],
                       check=True)
        rev = known_to_git(name, sh("git hash-object %s" % tmp))
        rows.append((name, old, new, rev))
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
    print("\n  FILE MANIFEST (old -> new, and the commit production is at)")
    for name, old, new, rev in rows:
        print("    %-18s %s -> %s%s"
              % (name, (old or "absent")[:12], new[:12],
                 "   (unchanged)" if old == new
                 else "   running = %s" % (rev[:9] if rev else "UNCOMMITTED")))
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
    for name, _old, _new, _rev in rows:
        remote("cp -p %s/%s %s/%s" % (APP_DIR, name, rel, name))
    with open("/tmp/manifest.txt", "w") as fh:
        fh.write("release %s\nrepo %s @ %s\n" % (stamp, branch, head))
        for name, old, new, rev in rows:
            fh.write("%s %s -> %s (was %s)\n" % (name, old, new, rev or "?"))
    subprocess.run(["scp", "-q", "/tmp/manifest.txt",
                    "%s:%s/manifest.txt" % (HOST, rel)], check=True)
    remote("ln -sfn %s %s/previous" % (shlex.quote(rel), RELEASES))
    print("backed up to %s" % rel)

    for name, _o, _n, _r in rows:
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

    # One run, printed and judged: two runs can disagree, and the operator
    # deciding whether to roll back must be looking at the results the exit
    # code was computed from.
    results = smoke()
    failed = [r for r in results if not r[3]]
    for label, _u, code, good in results:
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
    # -e, not -f: readlink -f prints a path for a symlink that does not exist,
    # so the "nothing to roll back to" case would fall through to a raw ls
    # failure instead of saying so.
    rel = remote("readlink -e %s/previous" % RELEASES, check=False)
    if not rel:
        print("no previous release recorded — nothing to roll back to",
              file=sys.stderr)
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


CANARY_SCRIPT = r"""
set -u
umask 077
jar=$(mktemp); cfg=$(mktemp); body=$(mktemp)
trap 'rm -f "$jar" "$cfg" "$body"' EXIT
# The ops token goes into a curl config file rather than the command line:
# an argv is visible in the host's process list to every local user.
printf 'header = "Authorization: Bearer %s"\n' \
  "$(grep -h '^OPS_API_TOKEN=' /etc/optiwar/optiwar-secrets.env | cut -d= -f2-)" > "$cfg"
BASE=https://optiwar.com
J="-b $jar -c $jar"
# Origin/Referer satisfy the CSRF origin check; without them every POST here
# is a 403. A fresh address each run matters: /start resumes an existing
# session for a known email, and SESSION_STARTED fires only on real creation.
JSON="-H Content-Type:application/json -H Origin:$BASE -H Referer:$BASE/"
EMAIL="deploy-canary+$(date +%s)@optiwar.com"
# Two different verdicts, so two different markers. A precondition the
# deployment cannot affect is no evidence; the deployed app returning non-200
# is the strongest evidence of a bad release we have, and nothing else catches
# it — the smoke suite makes no chat request.
fail() { echo "CANARY_FAIL $*"; exit 2; }
appfail() { echo "CANARY_APP_FAIL $*"; exit 3; }
post() {  # url json -> body in $body, prints status
  curl -s -o "$body" -w '%{http_code}' $J $JSON -X POST -d "$2" "$1"
}
classify() {  # code label — decide whether a non-200 is the release's fault
  case "$1" in
    200) return 0 ;;
    # curl could not complete the request at all: that is the network between
    # here and nginx, and it says nothing about the deployed code.
    000) fail "$2 no HTTP response (transport)" ;;
    503) if grep -q 'AI_TEMPORARILY_UNAVAILABLE' "$body"; then
           # The designed load-shedding contract, which the widget soft-retries.
           # A busy model provider is not a bad release.
           fail "$2 503 AI_TEMPORARILY_UNAVAILABLE — provider shedding load"
         else
           appfail "$2 HTTP 503: $(head -c 200 "$body")"
         fi ;;
    *) appfail "$2 HTTP $1: $(head -c 200 "$body")" ;;
  esac
}
post_ai() {  # url json label — bounded retry of the retryable 503 only
  n=0
  while : ; do
    code=$(post "$1" "$2")
    if [ "$code" = "503" ] && grep -q 'AI_TEMPORARILY_UNAVAILABLE' "$body" \
       && [ "$n" -lt 2 ]; then
      n=$((n + 1)); sleep 5; continue
    fi
    break
  done
  classify "$code" "$3"
}
field() { python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for k in sys.argv[1].split("."):
    if not isinstance(d, dict):
        sys.exit(0)
    d = d.get(k)
print(d if isinstance(d, str) else "")' "$1" 2>/dev/null; }

# Enrolment must be verified, not assumed. An empty or renamed OPS_API_TOKEN
# gives a 401 and no ow_acr_canary cookie; the ACR action path then stays off
# and three events can never appear — which would read as a failed deployment.
code=$(curl -s -o /dev/null -w '%{http_code}' -c "$jar" -K "$cfg" \
       "$BASE/api/chat/admin/acr-canary?on=1")
[ "$code" = "200" ] || fail "staff enrolment HTTP $code (ops token, or no route)"
grep -q 'ow_acr_canary' "$jar" || fail "enrolled but no ow_acr_canary cookie"

code=$(post "$BASE/api/chat/start" \
  "{\"email\":\"$EMAIL\",\"name\":\"Deploy Canary\",\"page_url\":\"$BASE/\"}")
classify "$code" /chat/start
sid=$(field session_id < "$body")
# 200 with no session_id is the deployed code misbehaving, not a canary
# problem, so it carries the rollback-worthy marker.
[ -n "$sid" ] || appfail "/chat/start 200 but no session_id: $(head -c 200 "$body")"
echo "SESSION=$sid"

# A product question: the model's search is what emits RECOMMENDATION_GENERATED,
# and an offered navigation is what emits NAVIGATION_OFFERED.
post_ai "$BASE/api/chat/message" \
  "{\"session_id\":\"$sid\",\"content\":\"show me round metal frames\",\"page_url\":\"$BASE/\"}" \
  /chat/message
aid=$(field action.action_id < "$body")

# Confirming the offer is the PENDING->CONFIRMED edge that emits
# ACTION_CONFIRMED; without this turn the event can never appear.
post_ai "$BASE/api/chat/message" \
  "{\"session_id\":\"$sid\",\"content\":\"yes\",\"page_url\":\"$BASE/\"}" \
  "/chat/message (confirm)"
aid2=$(field action.action_id < "$body")
[ -n "$aid2" ] && aid=$aid2

if [ -n "$aid" ]; then
  # The widget reporting execution is what emits ACTION_EXECUTED.
  code=$(post "$BASE/api/chat/action-result" \
    "{\"session_id\":\"$sid\",\"action_id\":\"$aid\",\"success\":true,\"duration_ms\":120}")
  classify "$code" /chat/action-result
  echo "ACTION=$aid"
else
  # No action offered is a fact about the deployment, not a broken canary.
  echo "ACTION=none-offered"
fi
"""


def cmd_canary(args):
    """Drive one staff conversation and prove canonical events were written.

    Runs on the production host so the request path is nginx and gunicorn,
    exactly as a shopper's would be. ``ACR_CANARY_ONLY`` keeps the action path
    staff-only, so nothing is exposed to live shoppers.

    The whole conversation is driven, not just its first turn: the model's
    product search is what emits ``RECOMMENDATION_GENERATED``, a confirmation
    turn is what drives the PENDING->CONFIRMED edge behind ``ACTION_CONFIRMED``,
    and the widget reporting back is what emits ``ACTION_EXECUTED``. Opening a
    session alone would leave five of the six events permanently MISSING and
    prove nothing about the deployment.
    """
    out = remote_script(CANARY_SCRIPT, check=False)
    print(out)
    # "the canary could not run" and "the instrumentation is missing" must not
    # look alike: the first is no evidence either way, the second is grounds to
    # roll a release back.
    for line in out.splitlines():
        if line.startswith("CANARY_APP_FAIL"):
            print("\n  DEPLOYED CHAT API FAILED — %s\n  This is the release, not "
                  "the canary: roll back with\n  python3 deploy/deploy.py "
                  "rollback --confirm"
                  % line[len("CANARY_APP_FAIL"):].strip(), file=sys.stderr)
            return 1
        if line.startswith("CANARY_FAIL"):
            print("\n  CANARY COULD NOT RUN — %s\n  This says nothing about the "
                  "deployment; fix the canary and re-run."
                  % line[len("CANARY_FAIL"):].strip(), file=sys.stderr)
            return 2
    sid = ""
    for line in out.splitlines():
        if line.startswith("SESSION="):
            sid = line.split("=", 1)[1].strip()
    if not sid:
        print("canary produced no session id at all", file=sys.stderr)
        return 2
    # Shaped like the ids chat_start mints. A malformed one is the application
    # misbehaving, and it is not going into a SQL statement either way.
    if not re.fullmatch(r"chat_[0-9a-f]{8,32}", sid):
        print("  /chat/start returned a malformed session id (%r) — that is the "
              "release: roll back" % sid, file=sys.stderr)
        return 1

    # Scoped to this session rather than a time window, so a concurrent
    # shopper's events cannot be mistaken for the canary's.
    ok, counts = remote_try(
        "mysql -N -e \"SELECT event_type, COUNT(*) FROM optiwar2.ai_events "
        "WHERE session_id='%s' GROUP BY event_type\"" % sid)
    if not ok:
        # An unreadable ai_events proves nothing about the release, and reading
        # its silence as "no events were written" would send a good deployment
        # into a rollback.
        print("\n  CANARY COULD NOT RUN — the ai_events query failed; no "
              "evidence either way", file=sys.stderr)
        return 2
    seen = dict((ln.split("\t")[0], ln.split("\t")[1])
                for ln in counts.splitlines() if "\t" in ln)
    print("\n  CANONICAL EVENTS for session %s" % sid)
    for ev in CANARY_EVENTS:
        print("    %-26s %s" % (ev, seen.get(ev, "MISSING")))
    missing = [e for e in CANARY_EVENTS if e not in seen]

    legacy = remote(
        "mysql -N -e \"SELECT event_type, COUNT(*) FROM optiwar2.ai_events "
        "WHERE created_at > NOW() - INTERVAL 7 DAY AND event_type LIKE 'AI\\_%%' "
        "GROUP BY event_type\"", check=False)
    if legacy:
        print("\n  legacy names still emitted in the last 7 days:")
        for ln in legacy.splitlines():
            print("    %s" % ln.replace("\t", "  "))
        print("  (the canonical-vs-legacy reconciliation window runs from here)")

    if missing:
        print("\n  MISSING: %s \u2014 the instrumentation is not fully live"
              % ", ".join(missing), file=sys.stderr)
    return 1 if missing else 0


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
