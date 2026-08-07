#!/usr/bin/env python3
"""
ACR Step 5 — closure / sweeper job.

Emits the two remaining canonical lifecycle events that require an out-of-band
sweep rather than a request-path signal:

  * ACTION_EXPIRED   — PENDING navigation actions that outlived their TTL.
  * SESSION_OUTCOME  — the single immutable outcome of an archived session
                       (PURCHASED > ESCALATED > FAILED > ABANDONED > ANSWERED).

Live-site safeguards (all defaults are the safe ones):

  * ACR_CLOSURE_ENABLED (default "false") — global kill switch. When false the
    job exits immediately and does nothing.
  * ACR_CLOSURE_DRY_RUN (default "true")  — when true (the default) the job only
    computes and prints what it WOULD change; it writes nothing. Flip to
    "false" only after the dry-run evidence has been reviewed.
  * A DB advisory lock (GET_LOCK) makes this a single-runner: if a previous run
    is still active the next run exits cleanly, so overlapping crons never race.
  * Every sweep is bounded (ACR_CLOSURE_BATCH, default 500) and uses indexed,
    short statements — no broad table locks, no long transactions.
  * Idempotency is structural, not advisory: ACTION_EXPIRED is edge-triggered by
    an atomic PENDING->EXPIRED UPDATE rowcount; SESSION_OUTCOME by an atomic
    INSERT-claim into ai_session_outcomes (session_id PRIMARY KEY). Two
    overlapping runs therefore cannot double-emit either event.

Intended cron (only after dry-run evidence is reviewed and writes are enabled):
*/15 * * * * ACR_CLOSURE_ENABLED=true ACR_CLOSURE_DRY_RUN=false \
  /var/www/.../venv/bin/python /var/www/.../flaskr/acr_closure_job.py \
  >> /var/log/optiwar/acr_closure.log 2>&1

Credentials come from the environment (root-only 0600 env file in production,
loaded by the cron wrapper); nothing is inlined here. The DB user should be the
least-privilege ``optiwar_closer`` (see ACR_PART_B_STEP5_CLOSURE_DESIGN.md).
"""
import os
import sys
import time
from datetime import datetime

import MySQLdb
import MySQLdb.cursors

try:
    from . import acr  # package import (installed as flaskr.acr)
except Exception:  # pragma: no cover - direct-script fallback
    import acr

ADVISORY_LOCK = "acr_closure_job"
ADVISORY_LOCK_WAIT = 0  # do not wait; a still-running previous run means skip

DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "user": os.environ.get("MYSQL_USER", "optiwar_closer"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DB", "optiwar2"),
}


def _bool_env(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] acr_closure: %s" % (ts, msg), flush=True)


def _connect():
    return MySQLdb.connect(
        cursorclass=MySQLdb.cursors.DictCursor,
        charset="utf8mb4",
        autocommit=True,
        **DB_CONFIG,
    )


def _acquire_lock(db):
    cur = db.cursor()
    cur.execute("SELECT GET_LOCK(%s, %s) AS got", (ADVISORY_LOCK, ADVISORY_LOCK_WAIT))
    row = cur.fetchone()
    return bool(row and row.get("got") == 1)


def _release_lock(db):
    try:
        cur = db.cursor()
        cur.execute("SELECT RELEASE_LOCK(%s)", (ADVISORY_LOCK,))
    except Exception:
        pass


def main():
    enabled = _bool_env("ACR_CLOSURE_ENABLED", False)
    dry_run = _bool_env("ACR_CLOSURE_DRY_RUN", True)
    batch = int(os.environ.get("ACR_CLOSURE_BATCH", "500"))

    if not enabled:
        _log("ACR_CLOSURE_ENABLED is false — kill switch engaged, doing nothing.")
        return 0

    mode = "DRY-RUN (no writes)" if dry_run else "LIVE (writes enabled)"
    _log("starting %s, batch=%d" % (mode, batch))

    db = _connect()
    try:
        if not _acquire_lock(db):
            _log("another closure run holds the advisory lock — exiting cleanly.")
            return 0
        try:
            # The ledger table is additive; ensure it exists (idempotent). In a
            # least-privilege deployment CREATE may be denied — that is fine once
            # the table has been created once by the migration step.
            try:
                acr.ensure_closure_schema(_connect)
            except Exception as e:
                _log("ensure_closure_schema skipped (%s)" % e)

            # ── Sweep 1: expire due PENDING actions ──
            t0 = time.time()
            r1 = acr.expire_due_actions(db, dry_run=dry_run, limit=batch)
            dt1 = (time.time() - t0) * 1000.0
            verb = "would expire" if dry_run else "expired"
            _log("actions: %d due; %s %d [%.1f ms]" % (
                len(r1["candidates"]), verb, len(r1["expired"]), dt1))
            for c in r1["expired"]:
                _log("  action %s (%s) session=%s" % (
                    c["action_id"], c["action_type"], c["session_id"]))

            # ── Sweep 2: finalize archived session outcomes ──
            t0 = time.time()
            r2 = acr.finalize_archived_session_outcomes(db, dry_run=dry_run, limit=batch)
            dt2 = (time.time() - t0) * 1000.0
            verb = "would close" if dry_run else "closed"
            _log("sessions: %d awaiting outcome; %s %d [%.1f ms]" % (
                len(r2["candidates"]), verb, len(r2["closed"]), dt2))
            counts = {}
            for c in r2["candidates"]:
                counts[c["outcome"]] = counts.get(c["outcome"], 0) + 1
            if counts:
                _log("  outcome distribution: " + ", ".join(
                    "%s=%d" % (k, counts[k]) for k in sorted(counts)))
            for c in r2["closed"]:
                _log("  session %s -> %s" % (c["session_id"], c["outcome"]))
        finally:
            _release_lock(db)
    finally:
        db.close()

    _log("done (%s)." % mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
