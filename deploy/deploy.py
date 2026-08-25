#!/usr/bin/env python3
"""Reproducible, reversible deployment of Optiwar application files.

Production runs a hand-copied tree inside a venv's site-packages, with no git
checkout, so the running code and ``main`` can silently diverge. They already
have, in both directions. This tool makes a deployment an auditable operation
instead of an scp:

    plan      what would change, proven safe, writing nothing
    migrate   apply the additive schema, on its own, before any code moves
    apply     back up, replace, restart once, smoke test
    canary    drive a staff conversation, prove canonical events are written
    release   migrate -> apply -> canary as one unattended run, rolling itself
              back when the release is at fault
    rollback  restore the previous release (code only)

Two guards do the real work.

*Provenance*: every file being replaced must hash-match a blob that exists in
this repository's history for that path. If it doesn't, production is carrying
an edit that was never committed, and overwriting it would destroy the only
copy. The deployment refuses — unless that exact content is recorded in
``REVIEWED_DRIFT``, which is how a diff someone has actually read is
distinguished from one nobody has.

*Scope*: only files in ``DEPLOY_SET`` are touched. ``main`` is currently
*behind* production on several files — GA4, Google Customer Reviews and the
ticket-notification retry are live but merged nowhere — so a whole-tree deploy
would silently revert working features. Widening the set is a deliberate act
that has to survive the provenance guard.

Nothing here is specific to the current host beyond the settings at the top,
all overridable from the environment. A new node needs a checkout, its
dependencies, its secrets file, this migration and this command.

Usage:
    python3 deploy/deploy.py plan
    python3 deploy/deploy.py migrate --confirm
    python3 deploy/deploy.py apply --confirm
    python3 deploy/deploy.py release --confirm
    python3 deploy/deploy.py rollback --confirm
"""
import argparse
import datetime
import hashlib
import importlib.util
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
DB = os.environ.get("OPTIWAR_DB", "optiwar2")
RELEASES = os.environ.get("OPTIWAR_RELEASES", "/root/deploy_releases")

# The ACR Part B canonical instrumentation, plus crm.py for the MSG91 delivery
# webhook. crm.py could not be here until #3 was merged: production ran main +
# #3, so a crm.py built from main alone would have reverted the live
# ticket-notification retry. Every other file either matches main already or is
# still *ahead* of it; see the module docstring.
# Paths are relative to the application root, so a template is deployable the
# same way a module is — success.html decides what a customer is told about
# their money, and shipping models.py without it says "paid" on an unpaid order.
DEPLOY_SET = ("acr.py", "ai_client.py", "chat_gateway.py", "crm.py",
              "models.py", "payments.py", "paid_orders.py",
              "razorpay_events.py", "csrf_guard.py",
              "templates/success.html")

# Files that do not exist in production yet. Absence is otherwise a block, so
# that a path typo or a file missing from the release cannot be mistaken for a
# new module; listing one here says the absence is expected and the file is to
# be created. A rollback restores only what it replaced, so these stay behind —
# harmless, because the code that imports them is reverted with them.
NEW_IN_RELEASE = ("paid_orders.py", "razorpay_events.py")

# Running content that matches no commit but has been read line by line and
# found safe to replace, keyed by md5. The provenance guard exists to stop a
# deploy destroying the only copy of an edit; when the whole of that edit is
# known and deliberately not being kept, recording it here is the audit trail.
REVIEWED_DRIFT = {
    # Reviewed 2026-08-25 against origin/main: main is ahead on every hunk
    # (append-only status writes, the paid-order pipeline, the .com lens
    # localisation) and the file's only production-unique content is a Google
    # Maps browser key hardcoded where main reads GOOGLE_MAPS_API_KEY from the
    # environment. REQUIRED_ENV below makes the deploy refuse until that name
    # is set on the box, so nothing is lost by replacing it.
    "models.py": {"0de7d17b5605a8368e353ffc1ca76026":
                  "hardcoded GOOGLE_MAPS_API_KEY, now read from the environment"},
}

# Environment names the deployed code reads and production does not set yet.
# Checked by name only — this tool never reads a secret's value. A missing name
# is a block, because the symptom otherwise is a feature that silently stops
# working (an autocomplete that returns nothing, a webhook that rejects every
# delivery) rather than an error anyone sees.
REQUIRED_ENV = (
    ("GOOGLE_MAPS_API_KEY", "models.py reads it for /api/places_autocomplete"),
    ("RAZORPAY_WEBHOOK_SECRET", "payments.py verifies /razorpay/webhook with it"),
)

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
    # 405 on a GET proves the delivery webhook route is registered. A broken
    # crm.py import would 404 here while every page above still answered 200,
    # and a webhook that 404s is auto-paused by MSG91 within minutes.
    ("delivery webhook registered",
     "https://optiwar.com/support/msg91_delivery_event", 405),
    # Same reasoning for the paid-order webhook: a models.py that failed to
    # import paid_orders would 404 here while every page above still answered.
    ("razorpay webhook registered",
     "https://optiwar.com/razorpay/webhook", 405),
    # A registered route is not a reachable one. Razorpay POSTs from its own
    # servers, so the delivery carries no Origin and no Referer, and with
    # CSRF_ENFORCE on the origin guard answers 403 before the view runs — every
    # payment silently unprocessed. 400 is the signature check refusing this
    # unsigned body, which is proof the request reached the view at all.
    ("razorpay webhook not origin-blocked",
     "https://optiwar.com/razorpay/webhook", 400, "POST"),
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


def python_set():
    """The deploy set's modules — the only members that can be compiled."""
    return tuple(n for n in DEPLOY_SET if n.endswith(".py"))


def acr_module():
    """Load the repo's acr.py by path (stdlib only, no package import).

    The migration is read from the application rather than restated here: a
    second copy of the column and index list is a copy that can disagree with
    ``ensure_schema()``, and a disagreement means unplanned DDL running during
    the restart, which is the thing this tool exists to prevent.
    """
    spec = importlib.util.spec_from_file_location(
        "acr_for_deploy", os.path.join(REPO, "acr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def migration():
    """The additive Part-B schema as (label, DDL), in application order."""
    acr = acr_module()
    items = [("ai_events.%s (column)" % name,
              "ALTER TABLE ai_events ADD COLUMN %s %s" % (name, decl))
             for name, decl in acr._AI_EVENTS_EXTRA_COLS]
    for table, idx in (("ai_events", acr._AI_EVENTS_EXTRA_IDX),
                       ("ai_actions", acr._AI_ACTIONS_EXTRA_IDX)):
        items += [("%s.%s (index)" % (table, name),
                   "ALTER TABLE %s ADD KEY %s (%s)" % (table, name, cols))
                  for name, cols in idx]
    return items


def remote_md5s():
    """md5 of every production module, plus the non-module deploy set.

    Templates are named rather than globbed: the tree holds hundreds and only
    the ones being deployed are being reasoned about. A missing one must not
    abort the hash of everything else — absence is a manifest decision.
    """
    cmd = "cd %s && md5sum *.py" % shlex.quote(APP_DIR)
    extra = [n for n in DEPLOY_SET if not n.endswith(".py")]
    if extra:
        cmd += " " + " ".join(shlex.quote(n) for n in extra)
    out = remote(cmd, check=False)
    return {name: h for h, name in
            (ln.split(None, 1) for ln in out.splitlines()) if name}


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
    for name in python_set():
        sh("python3 -m py_compile %s" % shlex.quote(name))
    out = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_*.py"], cwd=REPO, capture_output=True, text=True)
    tail = (out.stderr or "").strip().splitlines()
    return out.returncode == 0, tail[-3:] if tail else []


def missing_env():
    """``REQUIRED_ENV`` names the running service does not define.

    Names only: the unit file is read on the box and only the presence of each
    name is brought back, never a value.
    """
    # Both sources, because a name set in an EnvironmentFile is invisible to
    # the unit's own Environment= property, and treating that as unset would
    # block a correctly configured box.
    defined = remote_script(
        "svc=%s\n"
        "systemctl show \"$svc\" -p Environment --value | tr ' ' '\\n' "
        "| cut -d= -f1\n"
        "for f in $(systemctl show \"$svc\" -p EnvironmentFiles --value "
        "| tr ' ' '\\n' | sed 's/ (ignore_errors=.*//'); do\n"
        "  [ -r \"$f\" ] && grep -oE '^[A-Za-z_][A-Za-z0-9_]*' \"$f\"\n"
        "done" % shlex.quote(SERVICE), check=False).split()
    return [(name, why) for name, why in REQUIRED_ENV if name not in defined]


def pending_ddl():
    """Schema ``ensure_schema`` would add at boot, read from production.

    Reported so the migration is a deliberate step rather than a side effect of
    the restart: DDL and a code swap should not be the same event.
    """
    cols = remote(
        "mysql -N -e \"SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='%s' AND table_name='ai_events'\"" % DB,
        check=False).split()
    have = {}
    for table in ("ai_events", "ai_actions"):
        have[table] = remote(
            "mysql -N -e \"SELECT DISTINCT index_name FROM information_schema."
            "statistics WHERE table_schema='%s' AND table_name='%s'\""
            % (DB, table), check=False).split()

    # Driven by the application's own list, so an index added to ensure_schema()
    # and not here can no longer slip through unplanned.
    pending = []
    for label, ddl in migration():
        table, rest = label.split(".", 1)
        name = rest.split(" ", 1)[0]
        missing = (name not in cols if label.endswith("(column)")
                   else name not in have[table])
        if missing:
            pending.append((label, ddl))
    return pending


def manifest():
    """old hash -> new hash for the deploy set, plus repo/prod divergence."""
    prod = remote_md5s()
    rows, blocked = [], []
    for name in DEPLOY_SET:
        old, new = prod.get(name), md5(os.path.join(REPO, name))
        if old is None:
            new_ok = name in NEW_IN_RELEASE
            rows.append((name, old, new, "NEW" if new_ok else None))
            if not new_ok:
                blocked.append("%s: not present in production" % name)
            continue
        if old == new:
            rows.append((name, old, new, "HEAD"))
            continue
        tmp = "/tmp/.deploy_prod_%s" % name.replace("/", "_")
        subprocess.run(["scp", "-q", "%s:%s/%s" % (HOST, APP_DIR, name), tmp],
                       check=True)
        rev = known_to_git(name, sh("git hash-object %s" % tmp))
        reviewed = REVIEWED_DRIFT.get(name, {}).get(old)
        rows.append((name, old, new, rev or ("reviewed drift" if reviewed else None)))
        if not rev and not reviewed:
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
    only_prod = sorted(n for n in prod
                       if n.endswith(".py") and n not in local)
    return rows, blocked, ahead, only_prod


def smoke():
    """Run the smoke suite from the production host itself."""
    results = []
    for label, url, want, *rest in SMOKE:
        # No Origin/Referer and no body is deliberate for the POST cases: that
        # is exactly the shape of a provider's server-to-server delivery.
        method = "-X %s -H Content-Type:application/json" % rest[0] if rest else ""
        code = remote("curl -s -o /dev/null -w '%%{http_code}' -m 20 %s %s"
                      % (method, shlex.quote(url)), check=False)
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
        if old is None:
            state = "   (new file)" if rev == "NEW" else "   MISSING"
        elif old == new:
            state = "   (unchanged)"
        else:
            state = "   running = %s" % (rev or "UNCOMMITTED")
        print("    %-18s %s -> %s%s"
              % (name, (old or "absent")[:12], new[:12], state))
    for b in blocked:
        print("    BLOCKED  %s" % b)

    absent = missing_env()
    print("\n  ENVIRONMENT (names only, never values)")
    if not absent:
        print("    all %d required name(s) set on %s" % (len(REQUIRED_ENV), SERVICE))
    for name, why in absent:
        print("    BLOCKED  %s is not set — %s" % (name, why))

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
    if not ddl:
        print("    (none — already applied)")
    for label, _sql in ddl:
        print("    %s" % label)
    if ddl:
        print("    Apply these deliberately BEFORE the restart, so a code swap\n"
              "    and a DDL change are not the same event:\n"
              "      python3 deploy/deploy.py migrate --confirm")

    print("\n  SMOKE (current, pre-deploy baseline)")
    for label, _url, code, good in smoke():
        print("    %-24s HTTP %-4s %s" % (label, code, "ok" if good else "UNEXPECTED"))

    print("\n  Rollback after apply:  python3 deploy/deploy.py rollback --confirm")
    return 1 if (problems or blocked or not ok) else 0


def cmd_migrate(args):
    """Apply the additive schema as its own operation, before any code moves.

    Every item is additive — nullable columns and new indexes — so it is
    compatible in both directions: the running pre-Part-B code neither reads
    nor writes them, and a rollback deliberately leaves them in place.
    """
    pending = pending_ddl()
    print("SCHEMA — %d item(s) pending" % len(pending))
    for label, _sql in pending:
        print("  %s" % label)
    if not pending:
        return 0
    if not args.confirm:
        print("refusing to migrate without --confirm", file=sys.stderr)
        return 1

    for label, sql in pending:
        remote("mysql %s -e %s" % (shlex.quote(DB), shlex.quote(sql)))
        print("  applied  %s" % label)

    # Re-read information_schema rather than trusting the ALTERs returned zero:
    # the guarantee being bought is that the restart finds nothing left to do.
    left = pending_ddl()
    if left:
        print("\nSTILL PENDING after migrate: %s"
              % ", ".join(l for l, _ in left), file=sys.stderr)
        return 1
    print("\nall %d item(s) verified present — the restart will run no DDL"
          % len(pending))
    return 0


def cmd_apply(args):
    branch, head, problems = preflight()
    rows, blocked, _ahead, _only = manifest()
    ok, tail = verify_locally()
    # Only asked once nothing else has already blocked: a deploy that is not
    # going to happen has no business touching the box.
    if not (problems or blocked):
        blocked += ["%s is not set on %s — %s" % (name, SERVICE, why)
                    for name, why in missing_env()]
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
    for name, old, _new, _rev in rows:
        if old is None:
            continue        # nothing to back up: the file is being created
        remote("mkdir -p %s && cp -p %s/%s %s/%s"
               % (shlex.quote(os.path.dirname("%s/%s" % (rel, name))),
                  APP_DIR, name, rel, name))
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
           % (APP_DIR, " ".join(python_set())))
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
        # The new code is already live and serving: printing the rollback
        # command and stopping leaves a broken storefront up for as long as it
        # takes someone to read the output. Restore it here, where the failure
        # is known, rather than making recovery depend on an audience.
        print("\nSMOKE FAILED — restoring the previous release",
              file=sys.stderr)
        if cmd_rollback(args) != 0:
            print("ROLLBACK ALSO FAILED — the box needs hands",
                  file=sys.stderr)
            return 2
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
    names = [n for n in remote("cd %s && find . -type f -printf '%%P\\n'"
                               % shlex.quote(rel)).split()
             if n != "manifest.txt"]
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
    results = smoke()
    for label, _u, code, good in results:
        print("  %-24s HTTP %-4s %s" % (label, code, "ok" if good else "FAIL"))
    # A rollback that restored files but left the site down is not a recovery,
    # and the caller's "the box needs hands" alarm can only fire if the failure
    # is reported. Same checks the deployment was judged by.
    if active != "active" or any(not r[3] for r in results):
        print("ROLLBACK DID NOT RESTORE SERVICE", file=sys.stderr)
        return 1
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
        "mysql -N -e \"SELECT event_type, COUNT(*) FROM %s.ai_events "
        "WHERE session_id='%s' GROUP BY event_type\"" % (DB, sid))
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
        "mysql -N -e \"SELECT event_type, COUNT(*) FROM %s.ai_events "
        "WHERE created_at > NOW() - INTERVAL 7 DAY AND event_type LIKE 'AI\\_%%%%' "
        "GROUP BY event_type\"" % DB, check=False)
    if legacy:
        print("\n  legacy names still emitted in the last 7 days:")
        for ln in legacy.splitlines():
            print("    %s" % ln.replace("\t", "  "))
        print("  (the canonical-vs-legacy reconciliation window runs from here)")

    if missing:
        print("\n  MISSING: %s \u2014 the instrumentation is not fully live"
              % ", ".join(missing), file=sys.stderr)
    return 1 if missing else 0


def cmd_release(args):
    """The whole approved sequence, unattended: migrate, apply, canary.

    Unattended is the reason this exists rather than three commands typed in
    order. ``apply`` already rolls back a failed boot or a failed smoke test,
    but the canary ran afterwards and only *printed* the rollback command —
    fine with an operator watching, useless at 02:00 with nobody there. Here a
    verdict of "the release is at fault" acts.

    The distinction the canary draws is what makes acting safe: exit 1 is the
    deployed code misbehaving, exit 2 is no evidence either way — a busy model
    provider, a transport failure, an unreadable ai_events. Rolling back on
    exit 2 would revert a healthy release because something else was briefly
    unwell, so exit 2 stops and reports instead.
    """
    if not args.confirm:
        print("refusing to release without --confirm", file=sys.stderr)
        return 1

    print("=== 1/3 SCHEMA " + "=" * 45)
    if cmd_migrate(args) != 0:
        print("\nschema step failed — no code deployed", file=sys.stderr)
        return 1

    print("\n=== 2/3 APPLY " + "=" * 46)
    rc = cmd_apply(args)
    if rc == 2:
        # Deployed, unhealthy, and the restore did not bring the service back.
        # Nothing here can fix that, so say so as loudly as the exit code
        # allows rather than reporting a tidy failure.
        print("\nPRODUCTION IS NOT SERVING — deploy failed and the rollback "
              "did not restore it.\nThis needs hands on the box now.",
              file=sys.stderr)
        return 3
    if rc != 0:
        # Either nothing was copied (a preflight or provenance block) or the
        # boot/smoke check failed and the previous release is back up. Both are
        # reported above.
        print("\napply failed — nothing left running from this release",
              file=sys.stderr)
        return 1

    print("\n=== 3/3 CANARY " + "=" * 45)
    verdict = cmd_canary(args)
    if verdict == 0:
        print("\nRELEASE PROVEN — canonical events are being written.\n"
              "The canonical-vs-legacy observation window starts now.")
        return 0
    if verdict == 2:
        print("\nNO EVIDENCE — the canary could not run, which says nothing "
              "about the\nrelease. Deployed code is LIVE and smoke-clean; "
              "re-run the canary.", file=sys.stderr)
        return 2

    print("\nRELEASE AT FAULT — rolling back automatically", file=sys.stderr)
    if cmd_rollback(args) != 0:
        # Same verdict as a failed rollback after a failed smoke test: which
        # check condemned the release does not change the fact that the store
        # is down and nothing automated is going to fix it.
        print("\nPRODUCTION IS NOT SERVING — the canary condemned the release "
              "and the rollback\ndid not restore it. This needs hands on the "
              "box now.", file=sys.stderr)
        return 3
    print("\nrolled back. Schema additions and hardening are deliberately "
          "left in place.", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("migrate", cmd_migrate),
                     ("apply", cmd_apply), ("canary", cmd_canary),
                     ("release", cmd_release), ("rollback", cmd_rollback)):
        p = sub.add_parser(name)
        p.add_argument("--confirm", action="store_true")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
