#!/usr/bin/env python3
"""
ACR Step 5 — closure / sweeper job.

Emits the two remaining canonical lifecycle events that require an out-of-band
sweep rather than a request-path signal:

  * ACTION_EXPIRED    — PENDING navigation actions that outlived their TTL.
  * SESSION_OUTCOME   — the single immutable outcome of an archived
                        *conversation* (ESCALATED > FAILED > ABANDONED >
                        ANSWERED).
  * COMMERCE_OUTCOME  — purchase attribution for an archived session, recorded
                        separately so an order arriving after the conversation
                        closed never rewrites its immutable outcome.
  * OUTCOME_DEFERRED  — emitted instead of guessing when an authoritative truth
                        source cannot be read. The session is retried next run.

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
    an atomic PENDING->EXPIRED UPDATE rowcount; SESSION_OUTCOME and
    COMMERCE_OUTCOME by an atomic INSERT-claim into their ledger
    (session_id PRIMARY KEY). Two overlapping runs therefore cannot double-emit
    any of them.
  * Truth probes are tri-state. A denied or missing truth table yields UNKNOWN,
    never False, and an UNKNOWN that could change the answer defers the session
    rather than writing an immutable outcome that may be wrong.

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


def _log_deferrals(result, label):
    """Report sessions held back for want of authoritative truth.

    Deferrals are the signal that a grant is missing or a truth table is
    unreadable, so they are printed with their reason and their count rather
    than being invisible in a 'nothing to do' line.
    """
    deferred = result.get("deferred") or []
    if not deferred:
        return
    reasons = {}
    for d in deferred:
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    _log("  %s deferred: %d (%s)" % (
        label, len(deferred),
        ", ".join("%s=%d" % (k, reasons[k]) for k in sorted(reasons))))
    for d in deferred[:20]:
        _log("    deferred %s reason=%s" % (d["session_id"], d["reason"]))
    if len(deferred) > 20:
        _log("    ... and %d more" % (len(deferred) - 20))


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
            # the table has been created once by the migration step. A dry run
            # only reports the schema work: the collation alignment rebuilds a
            # primary key, which a run that writes nothing must not do.
            try:
                pending = acr.ensure_closure_schema(_connect,
                                                    allow_ddl=not dry_run)
                for item in (pending or []):
                    _log("schema %s: %s" % (
                        "pending (dry run)" if dry_run else "applied", item))
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
            for c in r1.get("event_failed") or []:
                _log("  WARNING action %s expired but ACTION_EXPIRED event write "
                     "failed (state correct, event missing)" % c["action_id"])

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
            _log_deferrals(r2, "session")
            for c in r2.get("event_failed") or []:
                _log("  WARNING session %s claimed but SESSION_OUTCOME event write "
                     "failed; will be retried next run" % c["session_id"])

            # ── Sweep 3: commerce attribution (separate from outcome) ──
            t0 = time.time()
            r3 = acr.attribute_archived_session_commerce(
                db, dry_run=dry_run, limit=batch)
            dt3 = (time.time() - t0) * 1000.0
            verb = "would attribute" if dry_run else "attributed"
            _log("commerce: %d attributable; %s %d [%.1f ms]" % (
                len(r3["candidates"]), verb, len(r3["attributed"]), dt3))
            for c in r3["attributed"]:
                # The two numbers are measured from different points and the
                # production dry run showed why saying so matters: an order
                # +163887s after the session *started* read as a contradiction
                # of the 24h ceiling, which is measured from last activity.
                _log("  session %s -> PURCHASED order=%s (%s, ordered +%ss "
                     "after session start, within %dh of last activity)" % (
                         c["session_id"], c["order_id"], c["attribution_type"],
                         c.get("attribution_delta_seconds"),
                         c["attribution_window_hours"]))
            # An order the query offered to a second session is the rule and the
            # unique key disagreeing, which is worth a warning rather than a
            # silent skip: it is how double-counted revenue would begin.
            for c in r3.get("already_claimed") or []:
                _log("  WARNING order %s already attributed to another session; "
                     "session %s not credited" % (c["order_id"], c["session_id"]))
            _log_deferrals(r3, "commerce")
            for c in r3.get("event_failed") or []:
                _log("  WARNING session %s claimed but COMMERCE_OUTCOME event "
                     "write failed; will be retried next run" % c["session_id"])
        finally:
            _release_lock(db)
    finally:
        db.close()

    _log("done (%s)." % mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
